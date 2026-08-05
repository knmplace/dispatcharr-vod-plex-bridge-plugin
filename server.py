"""WSGI HTTP server for VOD To Plex — dashboard + 302 redirect playback.

Uses stdlib wsgiref (threaded) to avoid async event loop conflicts with Django.
"""

import json
import logging
import os
import re
import time
from email.utils import formatdate
from io import BytesIO
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote
from wsgiref.simple_server import WSGIServer, make_server

logger = logging.getLogger("vod_plex_bridge.server")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# /api/ping is an internal liveness probe (see plugin.py's _is_our_server),
# and /api/lang-status + /api/activity-log are polled by the dashboard's own
# UI every 2-5s while open — logging those in debug_connections mode would
# mean the Logs tab re-logs itself refreshing, drowning out the one-time
# signal (like the actual catalog/movies calls) this debug mode exists to
# surface.
_NOISY_POLL_PATHS = ("/api/ping", "/api/lang-status", "/api/activity-log")


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

    def bind(self):
        """Bind the listening socket synchronously, before handing off to a
        background thread. Doing the bind here (in the caller's thread,
        while it still holds _server_lock) closes the race where a second
        concurrent Start Server click could see the port as free during the
        window between spawning the server thread and that thread actually
        calling make_server() — the DB-backed BridgeCore.initialize() below
        used to run before the bind, making that window wide enough to hit
        in practice."""
        from .bridge import BridgeCore

        self._bridge = BridgeCore(self.settings)
        self._bridge.initialize()

        app = _create_app(self, self._bridge, self.settings)
        self._server = make_server("0.0.0.0", self.port, app,
                                   server_class=_ThreadedWSGIServer)
        self._running = True

    def serve(self):
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

    def request_shutdown(self):
        """Same as shutdown(), but safe to call from a request handler
        thread that belongs to this same server (see /api/shutdown) —
        must run off the request thread since server.shutdown() blocks
        until serve_forever()'s loop notices and exits, which can't
        happen while this thread is still inside a request."""
        self.shutdown()

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


def _create_app(server, bridge, settings):
    """Return a WSGI application with URL routing."""

    def app(environ, start_response):
        try:
            return _dispatch(environ, start_response, server, bridge, settings)
        except Exception as e:
            logger.exception("Unhandled error in request dispatch")
            path = unquote(environ.get("PATH_INFO", "/"))
            bridge._log_event("error", f"Unhandled error on {path}: {e}")
            return _text_response(start_response, 500, "Internal server error")
        finally:
            try:
                from django.db import close_old_connections
                close_old_connections()
            except Exception:
                pass

    return app


def _dispatch(environ, start_response, server, bridge, settings):
    method = environ["REQUEST_METHOD"]
    path = unquote(environ.get("PATH_INFO", "/"))

    if settings.get("debug_connections") and path not in _NOISY_POLL_PATHS:
        client_ip = environ.get("REMOTE_ADDR", "unknown")
        bridge._log_event("debug", f"Inbound {method} {path} from {client_ip}")

    if path == "/api/ping" and method == "GET":
        return _json_response(start_response, {"plugin": "vod_plex_bridge"})

    if path == "/api/shutdown" and method == "POST":
        # Lets Stop Server work even when the click lands in a different
        # worker process than the one that started the server (Celery
        # autoscale spreads plugin action calls across processes, each
        # with its own independent _server_instance). Rather than trying
        # to signal/kill a foreign OS process, ask the actual owning
        # process's server to shut itself down over loopback HTTP — this
        # request is already proof the server is reachable from outside.
        # request_shutdown() must run off this request's own handler
        # thread, since server.shutdown() blocks until serve_forever()'s
        # loop notices and exits, which can't happen while this thread
        # is still busy handling this very request.
        import threading as _threading
        _threading.Thread(target=server.request_shutdown, daemon=True).start()
        return _json_response(start_response, {"status": "ok"})

    # --- Dashboard ---
    if path in ("/", "/dashboard"):
        return _serve_dashboard(environ, start_response)

    if path in ("/favicon.ico", "/logo.png"):
        return _serve_logo(start_response)

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

    if path == "/api/series/catalog-categories" and method == "GET":
        query = _parse_query(environ)
        return _json_response(start_response, bridge.list_series_catalog_categories(query))

    if path == "/api/series/providers" and method == "GET":
        query = _parse_query(environ)
        return _json_response(start_response, bridge.list_series_providers(query))

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

    if path == "/api/series/categories" and method == "GET":
        return _json_response(start_response, bridge.list_series_categories())

    if path == "/api/series/categories" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.create_series_category(body))

    if re.match(r"^/api/series/categories/\d+$", path) and method == "PUT":
        category_id = int(path.split("/")[-1])
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.update_series_category(category_id, body))

    if re.match(r"^/api/series/categories/\d+$", path) and method == "DELETE":
        category_id = int(path.split("/")[-1])
        return _json_response(start_response, bridge.delete_series_category(category_id))

    if path == "/api/series" and method == "GET":
        query = _parse_query(environ)
        return _json_response(start_response, bridge.list_series(query))

    if re.match(r"^/api/series/\d+/seasons$", path) and method == "GET":
        series_id = path.split("/")[3]
        return _json_response(start_response, bridge.list_seasons(series_id))

    if re.match(r"^/api/series/\d+/episodes$", path) and method == "GET":
        series_id = path.split("/")[3]
        query = _parse_query(environ)
        season_number = query.get("season", [None])[0]
        return _json_response(start_response, bridge.list_episodes(series_id, season_number))

    if path == "/api/episodes/activate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.activate_episodes(body))

    if path == "/api/episodes/deactivate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.deactivate_episodes(body))

    if path == "/api/episodes/reactivate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.reactivate_episodes(body))

    if path == "/api/health" and method == "GET":
        return _json_response(start_response, bridge.health_check(settings))

    if path == "/api/plex/sessions" and method == "GET":
        return _json_response(start_response, bridge.get_plex_sessions(settings))

    if path == "/api/activity-log" and method == "GET":
        return _json_response(start_response, bridge.get_activity_log())

    if path == "/api/activity-log" and method == "DELETE":
        return _json_response(start_response, bridge.clear_activity_log())

    if path == "/api/stats" and method == "GET":
        return _json_response(start_response, bridge.get_stats())

    if path == "/api/movies/activate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.activate_movies(body))

    if path == "/api/movies/deactivate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.deactivate_movies(body))

    if path == "/api/movies/refresh" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.refresh_movies(body))

    if path == "/api/movies/reactivate" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.reactivate_movies(body))

    if path == "/api/movies/check-audio" and method == "POST":
        body = _read_json_body(environ)
        return _json_response(start_response, bridge.check_movie_audio(body))

    if path == "/api/strm/generate" and method == "POST":
        count = bridge.generate_strm_files(settings, logger)
        return _json_response(start_response, {"status": "ok", "count": count})

    if path == "/api/plex/scan" and method == "POST":
        return _json_response(start_response, bridge.trigger_plex_scan(settings))

    if path == "/api/bug-report" and method == "GET":
        query = _parse_query(environ)
        try:
            hours = float(query.get("hours", ["24"])[0])
        except (ValueError, TypeError):
            hours = 24.0
        hours = max(1.0, min(hours, 24 * 7))
        return _bug_report_response(start_response, bridge, hours)

    if path == "/api/series/tmdb-results" and method == "GET":
        results = bridge._tmdb_detection_results
        response_data = []
        for series_id, entry in results.items():
            response_data.append({
                "series_id": series_id,
                "name": entry.get("name"),
                "year": entry.get("year"),
                "results": entry.get("results", []),
            })
        return _json_response(start_response, {"results": response_data})

    if path == "/api/series/tmdb-accept" and method == "POST":
        body = _read_json_body(environ)
        series_id = body.get("series_id")
        result_index = body.get("result_index")
        if not series_id or result_index is None:
            return _json_response(start_response, {"error": "Missing series_id or result_index"}, status=400)

        entry = bridge._tmdb_detection_results.get(series_id)
        if not entry:
            return _json_response(start_response, {"error": "Series not found in results"}, status=404)

        results = entry.get("results", [])
        if not (0 <= result_index < len(results)):
            return _json_response(start_response, {"error": "Invalid result_index"}, status=400)

        selected = results[result_index]
        try:
            from apps.vod.models import Series
            series = Series.objects.get(id=series_id)
            series.tmdb_id = selected["tmdb_id"]
            series.save(update_fields=["tmdb_id"])

            # Update state and rewrite NFO
            bridge._series_tmdb_state[series_id]["is_placeholder"] = False
            bridge._series_tmdb_state[series_id]["tmdb_id"] = selected["tmdb_id"]

            series_dir = os.path.join(bridge._data_dir, "series", str(series_id))
            if os.path.isdir(series_dir):
                bridge._write_tvshow_nfo(series, series_dir, force=True)

            # Remove from results
            del bridge._tmdb_detection_results[series_id]

            logger.info(f"TMDB manual accept for series {series_id}: {selected['name']} (id={selected['tmdb_id']})")
            return _json_response(start_response, {"status": "ok", "series_id": series_id, "tmdb_id": selected["tmdb_id"]})
        except Exception as e:
            logger.error(f"TMDB accept error for series {series_id}: {e}")
            return _json_response(start_response, {"error": str(e)}, status=500)

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

    # --- Series VOD filesystem (separate endpoint/mount from /vod/ so Plex's
    # Movies library scan root never sees a Series/Season/Episode subtree --
    # see CLAUDE.md "Series Support") ---
    if path == "/vod-series":
        start_response("301 Moved Permanently", [("Location", "/vod-series/")])
        return [b""]

    if path.startswith("/vod-series/"):
        subpath = path[len("/vod-series/"):]
        if subpath == "" or subpath.endswith("/"):
            if method == "HEAD":
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
                return [b""]
            html = bridge.list_series_vod_directory(subpath)
            body = html.encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if subpath.endswith(".nfo"):
            file_bytes, mtime, error = bridge.read_series_vod_file(subpath)
            if error:
                return _text_response(start_response, 404, "Not found")
            # rclone's http backend reads a file's modtime from this header
            # (not from the directory listing HTML) -- without it every
            # series file reads back as rclone's zero-epoch sentinel, which
            # makes Plex's TV scanner treat the season/series folder as
            # permanently unchanged and skip walking it (bead za8).
            headers = [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(file_bytes))),
                ("Last-Modified", formatdate(mtime, usegmt=True)),
            ]
            if method == "HEAD":
                start_response("200 OK", headers)
                return [b""]
            start_response("200 OK", headers)
            return [file_bytes]

        if subpath.endswith(".mkv") or subpath.endswith(".mp4"):
            # Synthetic entry -- mirrors the movies /vod/ .mkv route and the
            # existing /vod/episode/ route: fake HEAD metadata, then a 302
            # redirect to Dispatcharr's real proxy stream on GET. No file
            # exists on disk for these; list_series_vod_directory() only
            # advertises them in the listing (bead za8 -- real .strm files
            # scanned/matched fine but failed Plex's "get a decision" step).
            filename = subpath.rsplit("/", 1)[-1]
            episode_id = _extract_movie_id(filename)
            if not episode_id:
                return _text_response(start_response, 404, "Not found")

            if method == "HEAD":
                with bridge._head_request_semaphore:
                    info = bridge.get_episode_info(episode_id)
                    if not info:
                        return _text_response(start_response, 404, "Not found")
                    headers = [
                        ("Accept-Ranges", "bytes"),
                        ("Content-Type", info.get("content_type", "video/x-matroska")),
                    ]
                    file_size = info.get("file_size")
                    if file_size:
                        headers.append(("Content-Length", str(file_size)))
                    start_response("200 OK", headers)
                    return [b""]

            # GET/Range/other requests: only redirect if not a Plex library scan.
            # Plex's scanner (MediaServer-Agent/Plex) uses GET to verify file
            # accessibility during library analysis, which would otherwise cause
            # unnecessary provider connections via get_episode_redirect_url().
            # Return 403 to signal "not for scanning", letting Plex skip analysis
            # and trust the HEAD metadata instead.
            user_agent = environ.get("HTTP_USER_AGENT", "").lower()
            if "mediaserver-agent" in user_agent or "plex" in user_agent:
                return _text_response(start_response, 403, "Forbidden (scan-time GET not allowed)")

            client_ip = environ.get("REMOTE_ADDR", "unknown")
            redirect_url, error, account_id, stream_id = bridge.get_episode_redirect_url(episode_id)
            if error:
                status = 404 if "not found" in error.lower() or "not activated" in error.lower() else 503
                bridge.log_episode_play_request(episode_id, client_ip, ok=False, detail=error)
                if settings.get("debug_connections"):
                    bridge._log_event("debug", f"Redirect failed for episode {episode_id} (client {client_ip}): {error}")
                return _text_response(start_response, status, error)

            logger.info(f"302 redirect: episode {episode_id} -> {redirect_url}")
            bridge.log_episode_play_request(episode_id, client_ip, ok=True, detail=None, account_id=account_id, stream_id=stream_id)
            if settings.get("debug_connections"):
                bridge._log_event("debug", f"Outbound redirect: episode {episode_id} -> {redirect_url} (client {client_ip})")
            start_response("302 Found", [("Location", redirect_url)])
            return [b""]

        return _text_response(start_response, 404, "Not found")

    if path.startswith("/vod/episode/"):
        filename = path[len("/vod/episode/"):]
        episode_id = _extract_movie_id(filename)
        if not episode_id:
            return _text_response(start_response, 404, "Not found")

        if method == "HEAD":
            with bridge._head_request_semaphore:
                info = bridge.get_episode_info(episode_id)
                if not info:
                    return _text_response(start_response, 404, "Not found")
                headers = [
                    ("Accept-Ranges", "bytes"),
                    ("Content-Type", info.get("content_type", "video/x-matroska")),
                ]
                file_size = info.get("file_size")
                if file_size:
                    headers.append(("Content-Length", str(file_size)))
                start_response("200 OK", headers)
                return [b""]

        # GET/Range/other requests: only redirect if not a Plex library scan.
        user_agent = environ.get("HTTP_USER_AGENT", "").lower()
        if "mediaserver-agent" in user_agent or "plex" in user_agent:
            return _text_response(start_response, 403, "Forbidden (scan-time GET not allowed)")

        client_ip = environ.get("REMOTE_ADDR", "unknown")

        redirect_url, error, account_id, stream_id = bridge.get_episode_redirect_url(episode_id)
        if error:
            status = 404 if "not found" in error.lower() or "not activated" in error.lower() else 503
            bridge.log_episode_play_request(episode_id, client_ip, ok=False, detail=error)
            if settings.get("debug_connections"):
                bridge._log_event("debug", f"Redirect failed for episode {episode_id} (client {client_ip}): {error}")
            return _text_response(start_response, status, error)

        logger.info(f"302 redirect: episode {episode_id} -> {redirect_url}")
        bridge.log_episode_play_request(episode_id, client_ip, ok=True, detail=None, account_id=account_id, stream_id=stream_id)
        if settings.get("debug_connections"):
            bridge._log_event("debug", f"Outbound redirect: episode {episode_id} -> {redirect_url} (client {client_ip})")
        start_response("302 Found", [("Location", redirect_url)])
        return [b""]

    if path.startswith("/vod/"):
        filename = path[5:]
        movie_id = _extract_movie_id(filename)
        if not movie_id:
            return _text_response(start_response, 404, "Not found")

        if method == "HEAD":
            with bridge._head_request_semaphore:
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
                start_response("200 OK", headers)
                return [b""]

        # GET/Range/other requests: only redirect if not a Plex library scan.
        # Plex's scanner uses GET to verify file accessibility during analysis,
        # which would otherwise cause unnecessary provider connections.
        user_agent = environ.get("HTTP_USER_AGENT", "").lower()
        if "mediaserver-agent" in user_agent or "plex" in user_agent:
            return _text_response(start_response, 403, "Forbidden (scan-time GET not allowed)")

        client_ip = environ.get("REMOTE_ADDR", "unknown")

        redirect_url, error, account_id, stream_id = bridge.get_redirect_url(movie_id)
        if error:
            status = 404 if "not found" in error.lower() or "not activated" in error.lower() else 503
            bridge.log_play_request(movie_id, client_ip, ok=False, detail=error)
            if settings.get("debug_connections"):
                bridge._log_event("debug", f"Redirect failed for movie {movie_id} (client {client_ip}): {error}")
            return _text_response(start_response, status, error)

        logger.info(f"302 redirect: movie {movie_id} -> {redirect_url}")
        bridge.log_play_request(movie_id, client_ip, ok=True, detail=None, account_id=account_id, stream_id=stream_id)
        if settings.get("debug_connections"):
            bridge._log_event("debug", f"Outbound redirect: movie {movie_id} -> {redirect_url} (client {client_ip})")
        start_response("302 Found", [("Location", redirect_url)])
        return [b""]

    return _text_response(start_response, 404, "Not found")


def _bug_report_response(start_response, bridge, hours):
    """Build a small zip containing sanitized recent activity-log lines and
    stream it back as a download. Sanitizing (URL/provider-name scrubbing)
    happens inside bridge.build_bug_report_bundle() before any of this text
    reaches the zip, so nothing unsanitized is ever written to the archive."""
    import io
    import zipfile

    lines = bridge.build_bug_report_bundle(hours)
    header = (
        f"VOD To Plex bug report\n"
        f"Generated: {_now_str()}\n"
        f"Window: last {hours:g} hour(s)\n"
        f"Plugin version: {_plugin_version()}\n"
        f"Entries: {len(lines)}\n"
        f"Provider names and URLs have been replaced with placeholders.\n"
        f"{'-' * 60}\n"
    )
    body_text = header + ("\n".join(lines) if lines else "(no activity in this window)")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("activity_log.txt", body_text)
    zip_bytes = buf.getvalue()

    filename = f"vod-plex-bridge-bug-report-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    start_response("200 OK", [
        ("Content-Type", "application/zip"),
        ("Content-Length", str(len(zip_bytes))),
        ("Content-Disposition", f'attachment; filename="{filename}"'),
        ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
    ])
    return [zip_bytes]


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _serve_dashboard(environ, start_response):
    template_path = os.path.join(PLUGIN_DIR, "templates", "dashboard.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("{{PLUGIN_VERSION}}", _plugin_version())
        body = html.encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
            ("Pragma", "no-cache"),
            ("Expires", "0"),
        ])
        return [body]
    except FileNotFoundError:
        return _json_response(start_response,
                              {"error": "Dashboard template not found"}, status=500)


def _plugin_version():
    try:
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("version", "?")
    except Exception:
        return "?"


def _serve_logo(start_response):
    logo_path = os.path.join(PLUGIN_DIR, "logo.png")
    try:
        with open(logo_path, "rb") as f:
            body = f.read()
        start_response("200 OK", [
            ("Content-Type", "image/png"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "public, max-age=86400"),
        ])
        return [body]
    except FileNotFoundError:
        return _text_response(start_response, 404, "Not found")


def _json_response(start_response, data, status=200):
    body = json.dumps(data).encode("utf-8")
    status_line = f"{status} OK" if status == 200 else f"{status} Error"
    start_response(status_line, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Access-Control-Allow-Origin", "*"),
        ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
        ("Pragma", "no-cache"),
        ("Expires", "0"),
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
