import json
import os
import socket
import threading
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


class Plugin:
    name = _manifest["name"]
    version = _manifest["version"]
    description = _manifest["description"]
    author = _manifest.get("author", "")
    help_url = _manifest.get("help_url", "")
    fields = _manifest.get("fields", [])
    actions = _manifest.get("actions", [])

    def start(self, context):
        settings = context.get("settings", {})
        log = context.get("logger", logger)
        log.info("VOD To Plex plugin starting...")
        self._start_server(settings, log)

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
        self._do_stop_server(log)

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
                # Something else (or a stale server this process lost track
                # of) is bound to the port. Don't claim we started a new
                # server when we didn't — report the real state instead.
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

            _server_instance = BridgeServer(port=port, settings=settings)
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
        return self._do_stop_server(log)

    def _do_stop_server(self, log):
        global _server_instance, _server_thread
        with _server_lock:
            if _server_instance is not None:
                _server_instance.shutdown()
                _server_instance = None
                _server_thread = None
                log.info("VOD To Plex server stopped")
                return {"status": "ok", "message": "Server stopped."}
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
            # Port is bound but not by anything this process is tracking —
            # surface that mismatch instead of just saying "running".
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
