import json
import os
import threading
import logging

logger = logging.getLogger("vod_plex_bridge")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_manifest():
    with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r") as f:
        return json.load(f)


_manifest = _load_manifest()


class Plugin:
    name = _manifest["name"]
    version = _manifest["version"]
    description = _manifest["description"]
    author = _manifest.get("author", "")
    help_url = _manifest.get("help_url", "")
    fields = _manifest.get("fields", [])
    actions = _manifest.get("actions", [])

    def __init__(self):
        """Dispatcharr calls Plugin() at startup for every enabled plugin.
        We store server state on self so it survives across action calls on this instance."""
        self._server_instance = None
        self._server_thread = None
        self._server_lock = threading.Lock()

        settings = self._load_settings_from_db()
        if settings:
            logger.info("VOD To Plex: auto-starting on Dispatcharr startup...")
            self._start_server(settings, logger)
        else:
            logger.info("VOD To Plex: instantiated (no settings yet — start via action)")

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
        with self._server_lock:
            if self._server_instance and self._server_instance.is_running():
                port = settings.get("http_port", 8888)
                return {
                    "status": "ok",
                    "message": f"✓ Server already running on port {port}",
                }

            port = int(settings.get("http_port", 8888))

            from .server import BridgeServer

            self._server_instance = BridgeServer(port=port, settings=settings)
            self._server_thread = threading.Thread(
                target=self._server_instance.serve,
                daemon=True,
                name="vod-bridge-http",
            )
            self._server_thread.start()
            log.info(f"VOD To Plex server started on port {port}")
            return {
                "status": "ok",
                "message": f"Server started on port {port}. Dashboard: http://{settings.get('dashboard_host', 'localhost')}:{port}/",
            }

    def _stop_server(self, settings, log):
        return self._do_stop_server(log)

    def _do_stop_server(self, log):
        with self._server_lock:
            if self._server_instance:
                self._server_instance.shutdown()
                self._server_instance = None
                self._server_thread = None
                log.info("VOD To Plex server stopped")
                return {"status": "ok", "message": "Server stopped."}
            return {"status": "ok", "message": "Server was not running."}

    def _server_status(self, settings, log):
        with self._server_lock:
            if self._server_instance and self._server_instance.is_running():
                port = settings.get("http_port", 8888)
                stats = self._server_instance.get_stats()
                return {
                    "status": "ok",
                    "message": (
                        f"✓ Server running on port {port} | "
                        f"{stats.get('activated_count', 0)} activated | "
                        f"{stats.get('catalog_count', 0):,} in catalog"
                    ),
                }
            return {"status": "ok", "message": "✗ Server is not running — click Start Server to launch."}

    def _open_dashboard(self, settings, log):
        port = int(settings.get("http_port", 8888))
        host = settings.get("dashboard_host", "")
        if not host:
            host = self._detect_host()
        return {
            "status": "ok",
            "message": f"Dashboard: http://{host}:{port}/",
            "url": f"http://{host}:{port}/",
        }

    def _load_settings_from_db(self):
        try:
            from apps.plugins.models import PluginConfig
            cfg = PluginConfig.objects.get(key="vod_plex_bridge")
            return cfg.settings or {}
        except Exception as e:
            logger.debug(f"VOD To Plex: could not load settings from DB: {e}")
            return {}

    def _detect_host(self):
        return "localhost"

    def _generate_strm(self, settings, log):
        with self._server_lock:
            if not self._server_instance or not self._server_instance.is_running():
                return {"status": "error", "message": "Server not running. Start it first."}
            instance = self._server_instance
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
