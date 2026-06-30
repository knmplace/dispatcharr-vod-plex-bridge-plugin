"""WSGI HTTP server for VOD To Plex — dashboard + 302 redirect playback.

Uses stdlib wsgiref (threaded) to avoid async event loop conflicts with Django.
"""

import json
import logging
import os
import re
from io import BytesIO
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote
from wsgiref.simple_server import WSGIServer, make_server

logger = logging.getLogger("vod_plex_bridge.server")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class _ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class BridgeServer:
    def __init__(self, port=8888, settings=None):
        self.port = port
        self.settings = settings or {}
        self._bridge = None
        self._server = None
        self._running = False

    def serve(self):
        from .bridge import BridgeCore

        self._bridge = BridgeCore(self.settings)
        self._bridge.initialize()

        app = _create_app(self._bridge, self.settings)
        self._server = make_server("0.0.0.0", self.port, app,
                                   server_class=_ThreadedWSGIServer)
        self._running = True
        logger.info(f"VOD To Plex WSGI server on :{self.port}")
        self._server.serve_forever()
        self._running = False

    def shutdown(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._running = False
        if self._bridge:
            self._bridge.cleanup()
            self._bridge = None

    def is_running(self):
        return self._running

    def get_stats(self):
        if not self._bridge:
            return {}
        return self._bridge.get_stats()

    def generate_strm_files(self, settings, log):
        if not self._bridge:
            return 0
        return self._bridge.generate_strm_files(settings, log)


def _create_app(bridge, settings):
    """Return a WSGI application with URL routing."""

    def app(environ, start_response):
        try:
            return _dispatch(environ, start_response, bridge, settings)
        finally:
            try:
                from django.db import close_old_connections
                close_old_connections()
            except Exception:
                pass

    return app


def _dispatch(environ, start_response, bridge, settings):
    method = environ["REQUEST_METHOD"]
    path = unquote(environ.get("PATH_INFO", "/"))

    # --- Dashboard ---
    if path in ("/", "/dashboard"):
        return _serve_dashboard(environ, start_response)

    # --- API routes ---
    if path == "/api/catalog/summary" and method == "GET":
        return _json_response(start_response, bridge.get_catalog_summary())

    if path == "/api/movies" and method == "GET":
        query = _parse_query(environ)
        return _json_response(start_response, bridge.list_movies(query))

    if path == "/api/movies/activated" and method == "GET":
        return _json_response(start_response, bridge.list_activated())

    if path == "/api/movies/all-ids" and method == "GET":
        query = _parse_query(environ)
        return _json_response(start_response, bridge.get_all_movie_ids(query))

    if path == "/api/categories" and method == "GET":
        query = _parse_query(environ)
        return _json_response(start_response, bridge.list_categories(query))

    if path == "/api/providers" and method == "GET":
        query = _parse_query(environ)
        return _json_response(start_response, bridge.list_providers(query))

    if path == "/api/languages" and method == "GET":
        return _json_response(start_response, bridge.list_languages())

    if path == "/api/lang-status" and method == "GET":
        return _json_response(start_response, bridge.get_lang_status())

    if path == "/api/movies/detect-language" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.detect_language(body))

    if path == "/api/movies/detect-language-all" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.detect_language_all(body))

    if re.match(r"^/api/movies/\d+/detect-language$", path) and method == "POST":
        movie_id = path.split("/")[3]
        return _json_response(start_response, bridge.detect_single_language(movie_id))

    if path == "/api/health" and method == "GET":
        return _json_response(start_response, bridge.health_check(settings))

    if path == "/api/streams" and method == "GET":
        return _json_response(start_response, bridge.list_active_streams())

    if path == "/api/plex/sessions" and method == "GET":
        return _json_response(start_response, bridge.get_plex_sessions(settings))

    if path == "/api/stats" and method == "GET":
        return _json_response(start_response, bridge.get_stats())

    if path == "/api/movies/activate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.activate_movies(body))

    if path == "/api/movies/deactivate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.deactivate_movies(body))

    if path == "/api/strm/generate" and method == "POST":
        count = bridge.generate_strm_files(settings, logger)
        return _json_response(start_response, {"status": "ok", "count": count})

    if path == "/api/plex/scan" and method == "POST":
        return _json_response(start_response, bridge.trigger_plex_scan(settings))

    if path == "/api/proxy-log" and method == "GET":
        from .bridge import get_proxy_log
        return _json_response(start_response, get_proxy_log())

    if path == "/api/cache/status" and method == "GET":
        return _json_response(start_response, bridge.get_cache_status())

    if path == "/api/cache/fetch" and method == "POST":
        body = _read_body(environ)
        return _json_response(start_response, bridge.trigger_cache_fetch_all())

    # --- VOD filesystem ---
    if path == "/vod":
        start_response("301 Moved Permanently", [("Location", "/vod/")])
        return [b""]

    if path == "/vod/":
        if method == "HEAD":
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [b""]
        html = bridge.list_vod_directory()
        body = html.encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return [body]

    if path.startswith("/vod/"):
        filename = path[5:]
        movie_id = _extract_movie_id(filename)
        if not movie_id:
            return _text_response(start_response, 404, "Not found")

        if method == "HEAD":
            from .bridge import log_event
            info = bridge.get_movie_info(movie_id)
            if not info:
                return _text_response(start_response, 404, "Not found")
            headers = [
                ("Accept-Ranges", "bytes"),
                ("Content-Type", info.get("content_type", "video/x-matroska")),
            ]
            file_size = info.get("file_size")
            if file_size:
                headers.append(("Content-Length", str(file_size)))
            log_event("info", movie_id, "HEAD — served from cache", movie_name=info.get("name"), size=file_size)
            start_response("200 OK", headers)
            return [b""]

        # Parse Range header
        range_header = environ.get("HTTP_RANGE", "")
        range_start, range_end = _parse_range(range_header)

        from .bridge import log_event
        info = bridge.get_movie_info(movie_id)
        movie_name = info.get("name") if info else None

        # Try cache first — serves Plex/rclone metadata probes with zero provider connections
        cached = bridge.get_cached_range(movie_id, range_start or 0, range_end)
        if cached is not None:
            data, file_size = cached
            content_type = "video/x-matroska"
            if info:
                content_type = info.get("content_type", content_type)
                if not file_size:
                    file_size = info.get("file_size")
            actual_start = range_start or 0
            actual_end = actual_start + len(data) - 1
            headers = [
                ("Content-Type", content_type),
                ("Content-Length", str(len(data))),
                ("Accept-Ranges", "bytes"),
            ]
            if range_header and file_size:
                headers.append(("Content-Range", f"bytes {actual_start}-{actual_end}/{file_size}"))
                start_response("206 Partial Content", headers)
            else:
                start_response("200 OK", headers)
            log_event("info", movie_id, f"GET cache hit bytes {actual_start}-{actual_end}", movie_name=movie_name)
            return [data]

        # Not in cache — block concurrent probe if playback already active
        if bridge.has_active_connection(movie_id):
            log_event("warn", movie_id, "Blocked concurrent probe — connection active", movie_name=movie_name)
            return _text_response(start_response, 503, "Stream busy")

        # Real playback request — 302 to Dispatcharr
        redirect_url, error = bridge.get_redirect_url(movie_id)
        if error:
            log_event("error", movie_id, f"Redirect error: {error}", movie_name=movie_name)
            status = 404 if "not found" in error.lower() or "not activated" in error.lower() else 503
            return _text_response(start_response, status, error)

        log_event("info", movie_id, f"302 redirect bytes={range_start}-{range_end}", movie_name=movie_name)
        start_response("302 Found", [("Location", redirect_url)])
        return [b""]

    return _text_response(start_response, 404, "Not found")


def _serve_dashboard(environ, start_response):
    template_path = os.path.join(PLUGIN_DIR, "templates", "dashboard.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        body = html.encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return [body]
    except FileNotFoundError:
        return _json_response(start_response,
                              {"error": "Dashboard template not found"}, status=500)


def _json_response(start_response, data, status=200):
    body = json.dumps(data).encode("utf-8")
    status_line = f"{status} OK" if status == 200 else f"{status} Error"
    start_response(status_line, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Access-Control-Allow-Origin", "*"),
    ])
    return [body]


def _text_response(start_response, status, text):
    body = text.encode("utf-8")
    status_map = {200: "200 OK", 301: "301 Moved", 302: "302 Found",
                  404: "404 Not Found", 503: "503 Service Unavailable",
                  500: "500 Internal Server Error"}
    start_response(status_map.get(status, f"{status} Error"), [
        ("Content-Type", "text/plain"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def _parse_query(environ):
    qs = environ.get("QUERY_STRING", "")
    return parse_qs(qs, keep_blank_values=True)


def _read_json_body(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH", 0))
    except (ValueError, TypeError):
        length = 0
    if length == 0:
        return {}
    body = environ["wsgi.input"].read(length)
    return json.loads(body)


def _extract_movie_id(filename):
    m = re.search(r'\[(\d+)\]\.(mkv|mp4)$', filename)
    if m:
        return m.group(1)
    m = re.match(r'^(\d+)\.(mkv|mp4)$', filename)
    return m.group(1) if m else None


def _parse_range(range_header):
    """Parse HTTP Range header. Returns (start, end) as ints or None."""
    if not range_header or not range_header.startswith("bytes="):
        return None, None
    try:
        spec = range_header[6:]
        parts = spec.split("-")
        start = int(parts[0]) if parts[0] else None
        end = int(parts[1]) if len(parts) > 1 and parts[1] else None
        return start, end
    except Exception:
        return None, None
