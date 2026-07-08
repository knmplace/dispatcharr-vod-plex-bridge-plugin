import json
import os
import socket
import threading
import time
import logging

logger = logging.getLogger("vod_plex_bridge")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_manifest():
    with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r") as f:
        return json.load(f)


_manifest = _load_manifest()

# Module-level, not instance-level: Dispatcharr's plugin runner is not
# guaranteed to reuse the same Plugin() object across action invocations
# (e.g. a fresh instance per button click would reset any self._server_*
# state to None every time, even while the real server thread from an
# earlier start() call is still alive). Tracking the running server here
# instead means Start/Stop/Status always see the same state regardless of
# how many Plugin instances get constructed.
_server_instance = None
_server_thread = None
_server_lock = threading.Lock()


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            return False
        except OSError:
            return True


def _is_our_server(port):
    """Check whether whatever is bound to `port` is this plugin's own WSGI
    server — e.g. started by another Celery worker process, which has its
    own independent _server_instance and won't know about this one."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("plugin") == "vod_plex_bridge"
    except Exception:
        return False


def _request_remote_shutdown(port):
    """Ask a server bound in a different worker process to shut itself
    down, over loopback HTTP (see /api/shutdown in server.py). This is
    the only reliable way to stop it: the WSGI server lives on a thread
    inside that other process, not as its own process, so there is no
    safe way to signal/kill it from here directly (that process also
    runs other Celery tasks unrelated to this plugin)."""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown", data=b"", method="POST"
        )
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


class Plugin:
    name = _manifest["name"]
    version = _manifest["version"]
    description = _manifest["description"]
    author = _manifest.get("author", "")
    help_url = _manifest.get("help_url", "")
    fields = _manifest.get("fields", [])
    actions = _manifest.get("actions", [])

    def start(self, context):
        log = context.get("logger", logger)
        log.info("VOD To Plex plugin loaded. Use the Start Server action to launch the server.")

    def run(self, action, params, context):
        settings = context.get("settings", {})
        log = context.get("logger", logger)

        handlers = {
            "start_server": self._start_server,
            "stop_server": self._stop_server,
            "server_status": self._server_status,
            "open_dashboard": self._open_dashboard,
            "generate_strm": self._generate_strm,
            "scan_plex": self._scan_plex,
        }

        handler = handlers.get(action)
        if not handler:
            return {"status": "error", "message": f"Unknown action: {action}"}

        try:
            return handler(settings, log)
        except Exception as e:
            log.error(f"Action '{action}' failed: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def stop(self, context):
        log = context.get("logger", logger)
        log.info("VOD To Plex plugin stopping...")
        settings = context.get("settings", {})
        self._do_stop_server(log, settings)

    def _start_server(self, settings, log):
        global _server_instance, _server_thread
        port = int(settings.get("http_port", 8888))

        with _server_lock:
            if _server_instance is not None and _server_instance.is_running():
                log.info(f"VOD To Plex: server already running on port {port}")
                return {
                    "status": "ok",
                    "message": f"✓ Server already running on port {port}",
                }

            if _port_in_use(port):
                # Something is bound to the port that this process's
                # _server_instance doesn't know about — could be a genuine
                # foreign process, or it could be our own server started by
                # a different Celery worker process (module-level state is
                # per-process, so each worker tracks its own instance).
                # Ask the port itself before reporting a false conflict.
                if _is_our_server(port):
                    log.info(f"VOD To Plex: server already running on port {port} (another worker process)")
                    return {
                        "status": "ok",
                        "message": f"✓ Server already running on port {port} (started by another worker process)",
                    }
                log.warning(
                    f"VOD To Plex: port {port} is already bound but not by "
                    f"our tracked instance — not starting a duplicate server"
                )
                return {
                    "status": "error",
                    "message": f"Port {port} is already in use by another process. "
                                f"Check Status, or stop the existing process first.",
                }

            from .server import BridgeServer

            candidate = BridgeServer(port=port, settings=settings)
            try:
                # Bind synchronously, still holding _server_lock, so a
                # concurrent Start Server call (e.g. a double-click) can't
                # see the port as free during the old async-bind window.
                candidate.bind()
            except OSError as e:
                log.warning(f"VOD To Plex: failed to bind port {port}: {e}")
                return {
                    "status": "error",
                    "message": f"Port {port} is already in use by another process. "
                                f"Check Status, or stop the existing process first.",
                }

            _server_instance = candidate
            _server_thread = threading.Thread(
                target=_server_instance.serve,
                daemon=True,
                name="vod-bridge-http",
            )
            _server_thread.start()
            log.info(f"VOD To Plex server started on port {port}")
            return {
                "status": "ok",
                "message": f"Server started on port {port}. Dashboard: http://{settings.get('dashboard_host', 'localhost')}:{port}/",
            }

    def _stop_server(self, settings, log):
        return self._do_stop_server(log, settings)

    def _do_stop_server(self, log, settings=None):
        global _server_instance, _server_thread
        with _server_lock:
            if _server_instance is not None:
                _server_instance.shutdown()
                _server_instance = None
                _server_thread = None
                log.info("VOD To Plex server stopped")
                return {"status": "ok", "message": "Server stopped."}

            # Nothing tracked in this process, but the server may actually
            # be bound and running in a different worker process (each
            # process has its own independent _server_instance) — Celery
            # autoscale spreads plugin action calls across processes, so
            # Start and Stop frequently land in different ones. Ask that
            # server to shut itself down over loopback HTTP instead of
            # falsely reporting "stopped"/"not running" while the
            # dashboard stays up.
            port = int((settings or {}).get("http_port", 8888))
            if _port_in_use(port) and _is_our_server(port):
                if _request_remote_shutdown(port):
                    for _ in range(20):
                        time.sleep(0.25)
                        if not _port_in_use(port):
                            log.info(f"VOD To Plex: server on port {port} (different worker process) stopped")
                            return {"status": "ok", "message": "Server stopped."}
                log.warning(
                    f"VOD To Plex: Stop Server called, but the running "
                    f"server on port {port} belongs to a different worker "
                    f"process and did not shut down in time."
                )
                return {
                    "status": "error",
                    "message": f"Server on port {port} (different worker process) "
                                f"did not shut down in time. Try again, or restart "
                                f"the Dispatcharr container to fully stop it.",
                }
            return {"status": "ok", "message": "Server was not running."}

    def _server_status(self, settings, log):
        port = int(settings.get("http_port", 8888))
        tracked_running = _server_instance is not None and _server_instance.is_running()
        port_bound = _port_in_use(port)

        if tracked_running:
            return {
                "status": "ok",
                "message": f"✓ Server running on port {port}",
            }
        if port_bound:
            if _is_our_server(port):
                return {
                    "status": "ok",
                    "message": f"✓ Server running on port {port} (started by another worker process — "
                                f"Stop Server from this session won't affect it).",
                }
            # Port is bound but not by anything this process is tracking, and
            # it doesn't answer as our own plugin either — surface that
            # mismatch instead of just saying "running".
            return {
                "status": "ok",
                "message": f"⚠ Port {port} is in use, but not by a server this "
                            f"plugin is tracking — Stop Server won't affect it. "
                            f"A container restart will clear it.",
            }
        return {"status": "ok", "message": "✗ Server is not running — click Start Server to launch."}

    def _open_dashboard(self, settings, log):
        port = int(settings.get("http_port", 8888))
        host = settings.get("dashboard_host", "")
        if not host:
            host = "localhost"
        return {
            "status": "ok",
            "message": f"Dashboard: http://{host}:{port}/",
            "url": f"http://{host}:{port}/",
        }

    def _generate_strm(self, settings, log):
        with _server_lock:
            if not _server_instance or not _server_instance.is_running():
                return {"status": "error", "message": "Server not running. Start it first."}
            instance = _server_instance
        count = instance.generate_strm_files(settings, log)
        return {
            "status": "ok",
            "message": f"Generated {count} STRM files.",
        }

    def _scan_plex(self, settings, log):
        plex_url = settings.get("plex_url", "")
        plex_token = settings.get("plex_token", "")
        section = settings.get("plex_library_section", 7)

        if not plex_url or not plex_token:
            return {
                "status": "error",
                "message": "Plex URL and token required. Configure in settings.",
            }

        import requests

        try:
            resp = requests.get(
                f"{plex_url}/library/sections/{section}/refresh",
                headers={"X-Plex-Token": plex_token},
                timeout=10,
            )
            if resp.status_code < 300:
                return {"status": "ok", "message": "Plex library scan triggered."}
            return {
                "status": "error",
                "message": f"Plex returned HTTP {resp.status_code}",
            }
        except Exception as e:
            return {"status": "error", "message": f"Plex scan failed: {e}"}
