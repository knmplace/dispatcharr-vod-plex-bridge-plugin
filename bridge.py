import json
import logging
import os
import re
import threading
import time
from collections import deque
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.parse import urlparse

HEAD_CACHE_SIZE = 8 * 1024 * 1024   # 8MB — covers Plex fast-start and moov atom probes
TAIL_CACHE_SIZE = 256 * 1024         # 256KB — covers seek table / moov atom at end of file

logger = logging.getLogger("vod_plex_bridge.bridge")

# --- Proxy Activity Log ---
MAX_LOG_ENTRIES = 500
_proxy_log: deque = deque(maxlen=MAX_LOG_ENTRIES)
_log_lock = threading.Lock()


def log_event(level: str, movie_id, msg: str, movie_name: str = None, **extra):
    entry = {"ts": time.time(), "level": level, "movie_id": movie_id,
             "movie_name": movie_name, "msg": msg, **extra}
    with _log_lock:
        _proxy_log.append(entry)


def get_proxy_log():
    with _log_lock:
        return list(_proxy_log)

LANG_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "hi": "Hindi",
    "ar": "Arabic", "tr": "Turkish", "pl": "Polish", "sv": "Swedish",
    "da": "Danish", "no": "Norwegian", "fi": "Finnish", "el": "Greek",
    "he": "Hebrew", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian",
    "ms": "Malay", "tl": "Tagalog", "ro": "Romanian", "hu": "Hungarian",
    "cs": "Czech", "sk": "Slovak", "bg": "Bulgarian", "uk": "Ukrainian",
    "hr": "Croatian", "sr": "Serbian", "sl": "Slovenian", "lt": "Lithuanian",
    "lv": "Latvian", "et": "Estonian", "ka": "Georgian", "hy": "Armenian",
    "fa": "Persian", "ur": "Urdu", "bn": "Bengali", "ta": "Tamil",
    "te": "Telugu", "ml": "Malayalam", "kn": "Kannada", "mr": "Marathi",
    "gu": "Gujarati", "pa": "Punjabi", "cn": "Cantonese",
}


class BridgeCore:
    """Core bridge logic. Accesses Dispatcharr VOD data via Django ORM."""

    def __init__(self, settings):
        self.settings = settings
        self._activated = {}
        self._languages = {}
        self._data_dir = "/data/vod-plex-bridge"
        self._lang_detect_running = False
        self._lang_status = ""
        # session_id cache: key = "movie_id:stream_id", value = (resolved_url, timestamp)
        # Reusing a session URL within SESSION_TTL collapses burst requests (e.g. rclone double-GET)
        # to a single Dispatcharr session. After TTL, next request gets a fresh session so a
        # second independent viewer always gets their own connection.
        self._session_cache: dict = {}
        self._session_lock = threading.Lock()
        self._SESSION_TTL = 30  # seconds

        # Head/tail byte cache — keyed by movie_id string
        # Populated at activation. Serves Plex/rclone metadata probes without provider connections.
        self._cache_dir = os.path.join(self._data_dir, "cache")
        self._cache_fetching: set = set()   # movie_ids currently being fetched
        self._cache_lock = threading.Lock()

        # Active provider connections — keyed by movie_id string
        # Prevents a metadata probe from opening a second connection while playback is active.
        self._active_connections: dict = {}  # movie_id -> count
        self._conn_lock = threading.Lock()

    def initialize(self):
        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(self._cache_dir, exist_ok=True)
        self._load_state()
        logger.info(
            f"BridgeCore initialized. {len(self._activated)} activated movies."
        )

    def cleanup(self):
        self._save_state()

    def _load_state(self):
        state_file = os.path.join(self._data_dir, "bridge_state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r") as f:
                    state = json.load(f)
                self._activated = state.get("activated", {})
            except Exception as e:
                logger.error(f"Failed to load state: {e}")

        lang_file = os.path.join(self._data_dir, "language_cache.json")
        if os.path.exists(lang_file):
            try:
                with open(lang_file, "r") as f:
                    self._languages = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load language cache: {e}")

    def _save_state(self):
        state_file = os.path.join(self._data_dir, "bridge_state.json")
        try:
            with open(state_file, "w") as f:
                json.dump({"activated": self._activated}, f)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _save_languages(self):
        lang_file = os.path.join(self._data_dir, "language_cache.json")
        try:
            with open(lang_file, "w") as f:
                json.dump(self._languages, f)
        except Exception as e:
            logger.error(f"Failed to save language cache: {e}")

    def get_stats(self):
        return {
            "catalog_count": self._get_catalog_count(),
            "activated_count": len(self._activated),
            "active_streams": 0,
        }

    def _get_catalog_count(self):
        try:
            from apps.vod.models import Movie

            return Movie.objects.count()
        except Exception:
            return 0

    def get_catalog_summary(self):
        try:
            from apps.vod.models import Movie
            total = Movie.objects.count()
        except Exception as e:
            logger.error(f"Movie count error: {e}")
            total = 0

        activated = len(self._activated)

        categories = []
        try:
            from apps.vod.models import VODCategory, M3UMovieRelation
            from django.db.models import Count
            for cat in VODCategory.objects.annotate(
                movie_count=Count("m3umovierelation")
            ).filter(movie_count__gt=0).order_by("-movie_count"):
                categories.append(
                    {
                        "id": cat.id,
                        "name": cat.name,
                        "count": cat.movie_count,
                    }
                )
        except Exception as e:
            logger.error(f"Category summary error: {e}")

        return {
            "total": total,
            "activated": activated,
            "active_streams": 0,
            "categories": categories,
        }

    def list_movies(self, query):
        try:
            from apps.vod.models import Movie

            page = int(query.get("page", [1])[0])
            per_page = int(query.get("per_page", [50])[0])
            search = query.get("search", [""])[0]
            provider_ids = [v for v in query.get("provider_id", []) if v]
            category_ids = [v for v in query.get("category_id", []) if v]
            languages = [v for v in query.get("language", []) if v]
            activated_only = query.get("activated_only", [""])[0]

            qs = Movie.objects.all()

            if search:
                qs = qs.filter(name__icontains=search)

            if languages:
                wanted_ids = [
                    int(mid) for mid, lang in self._languages.items()
                    if lang in languages
                ]
                qs = qs.filter(id__in=wanted_ids)

            if provider_ids and category_ids:
                qs = qs.filter(
                    m3u_relations__m3u_account_id__in=[int(p) for p in provider_ids],
                    m3u_relations__category_id__in=[int(c) for c in category_ids],
                ).distinct()
            elif provider_ids:
                qs = qs.filter(m3u_relations__m3u_account_id__in=[int(p) for p in provider_ids]).distinct()
            elif category_ids:
                qs = qs.filter(m3u_relations__category_id__in=[int(c) for c in category_ids]).distinct()

            if activated_only:
                activated_ids = [int(mid) for mid in self._activated.keys() if mid.isdigit()]
                if activated_ids:
                    qs = qs.filter(id__in=activated_ids)
                else:
                    qs = qs.none()

            qs = qs.order_by("name")
            total = qs.count()

            offset = (page - 1) * per_page
            movies = []
            for m in qs.select_related("logo")[offset : offset + per_page]:
                mid = str(m.id)
                poster = ""
                try:
                    if m.logo and m.logo.url:
                        poster = m.logo.url
                except Exception:
                    pass
                trailer_key = None
                try:
                    cp = getattr(m, "custom_properties", None) or {}
                    if isinstance(cp, str):
                        import json as _json
                        cp = _json.loads(cp)
                    trailer_key = cp.get("youtube_trailer") or cp.get("trailer") or None
                except Exception:
                    pass

                movies.append(
                    {
                        "id": mid,
                        "name": m.name,
                        "year": getattr(m, "year", None),
                        "rating": getattr(m, "rating", None),
                        "genre": getattr(m, "genre", ""),
                        "tmdb_id": getattr(m, "tmdb_id", None),
                        "poster": poster,
                        "description": getattr(m, "description", ""),
                        "uuid": str(getattr(m, "uuid", "")),
                        "activated": mid in self._activated,
                        "trailer_key": trailer_key,
                        "language": self._languages.get(mid),
                    }
                )

            return {
                "movies": movies,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page,
            }
        except Exception as e:
            logger.error(f"list_movies error: {e}")
            return {"movies": [], "total": 0, "error": str(e)}

    def list_activated(self):
        return {
            "activated": list(self._activated.keys()),
            "count": len(self._activated),
        }

    def get_all_movie_ids(self, query):
        try:
            from apps.vod.models import Movie

            search = query.get("search", [""])[0]
            provider_ids = [v for v in query.get("provider_id", []) if v]
            category_ids = [v for v in query.get("category_id", []) if v]
            languages = [v for v in query.get("language", []) if v]

            qs = Movie.objects.all()
            if search:
                qs = qs.filter(name__icontains=search)
            if languages:
                wanted_ids = [
                    int(mid) for mid, lang in self._languages.items()
                    if lang in languages
                ]
                qs = qs.filter(id__in=wanted_ids)
            if provider_ids and category_ids:
                qs = qs.filter(
                    m3u_relations__m3u_account_id__in=[int(p) for p in provider_ids],
                    m3u_relations__category_id__in=[int(c) for c in category_ids],
                ).distinct()
            elif provider_ids:
                qs = qs.filter(m3u_relations__m3u_account_id__in=[int(p) for p in provider_ids]).distinct()
            elif category_ids:
                qs = qs.filter(m3u_relations__category_id__in=[int(c) for c in category_ids]).distinct()

            ids = list(qs.values_list("id", flat=True))
            return {"movie_ids": [str(i) for i in ids], "count": len(ids)}
        except Exception as e:
            return {"movie_ids": [], "error": str(e)}

    def list_categories(self, query):
        try:
            from apps.vod.models import VODCategory, M3UMovieRelation
            from django.db.models import Count, Q

            provider_ids = [v for v in query.get("provider_id", []) if v]

            qs = VODCategory.objects.all()
            if provider_ids:
                qs = qs.annotate(
                    movie_count=Count(
                        "m3umovierelation",
                        filter=Q(m3umovierelation__m3u_account_id__in=[int(p) for p in provider_ids]),
                    )
                )
            else:
                qs = qs.annotate(movie_count=Count("m3umovierelation"))

            cats = []
            for cat in qs.filter(movie_count__gt=0).order_by("name"):
                cats.append(
                    {"id": cat.id, "name": cat.name, "count": cat.movie_count}
                )
            return {"categories": cats}
        except Exception as e:
            return {"categories": [], "error": str(e)}

    def list_providers(self, query):
        try:
            from apps.m3u.models import M3UAccount
            from apps.vod.models import M3UMovieRelation
            from django.db.models import Count

            account_counts = {}
            for row in (
                M3UMovieRelation.objects
                .values("m3u_account_id")
                .annotate(cnt=Count("id"))
            ):
                account_counts[row["m3u_account_id"]] = row["cnt"]

            providers = []
            for acc in M3UAccount.objects.filter(
                is_active=True, id__in=account_counts.keys()
            ).order_by("name"):
                providers.append(
                    {"id": acc.id, "name": acc.name, "count": account_counts.get(acc.id, 0)}
                )
            return {"providers": providers}
        except Exception as e:
            return {"providers": [], "error": str(e)}

    def list_active_streams(self):
        return {"streams": [], "count": 0}

    # --- Language Detection (TMDB) ---

    def list_languages(self):
        counts = {}
        for lang in self._languages.values():
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        languages = [
            {"language": lang, "cnt": cnt}
            for lang, cnt in sorted(counts.items(), key=lambda x: -x[1])
        ]
        return {"languages": languages}

    def get_lang_status(self):
        return {"lang_status": self._lang_status, "running": self._lang_detect_running}

    def _tmdb_lookup_language(self, tmdb_id, api_key, read_token=None):
        import requests

        url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
        headers = {}
        params = {}
        if read_token:
            headers["Authorization"] = f"Bearer {read_token}"
        else:
            params["api_key"] = api_key
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=10)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "4"))
                    time.sleep(retry_after + 1)
                    continue
                if resp.status_code != 200:
                    return None
                return resp.json().get("original_language", "") or None
            except Exception as e:
                if attempt < 4:
                    time.sleep(2)
                    continue
                logger.warning(f"TMDB language lookup failed for tmdb_id={tmdb_id}: {e}")
                return None
        return None

    def detect_language(self, body):
        api_key = self.settings.get("tmdb_api_key", "")
        read_token = self.settings.get("tmdb_read_token", "")
        if not api_key and not read_token:
            return {"error": "TMDB API key not configured"}

        movie_ids = body.get("movie_ids", [])
        if not movie_ids:
            return {"error": "movie_ids required"}

        try:
            from apps.vod.models import Movie

            detected = 0
            skipped = 0
            no_tmdb = 0
            results = []
            for mid in movie_ids:
                mid = str(mid)
                try:
                    movie = Movie.objects.get(id=int(mid))
                except Exception:
                    skipped += 1
                    continue
                tmdb_id = getattr(movie, "tmdb_id", None)
                if not tmdb_id:
                    no_tmdb += 1
                    continue
                lang = self._tmdb_lookup_language(tmdb_id, api_key, read_token)
                if lang:
                    self._languages[mid] = lang
                    detected += 1
                    results.append({"id": mid, "language": lang, "language_name": LANG_NAMES.get(lang, lang)})
                else:
                    skipped += 1
                time.sleep(0.15)

            self._save_languages()
            return {"detected": detected, "skipped": skipped, "no_tmdb_id": no_tmdb, "results": results}
        except Exception as e:
            logger.error(f"detect_language error: {e}")
            return {"error": str(e)}

    def detect_single_language(self, movie_id):
        api_key = self.settings.get("tmdb_api_key", "")
        read_token = self.settings.get("tmdb_read_token", "")
        if not api_key and not read_token:
            return {"error": "TMDB API key not configured"}

        try:
            from apps.vod.models import Movie

            mid = str(movie_id)
            try:
                movie = Movie.objects.get(id=int(mid))
            except Exception:
                return {"error": "Movie not found"}

            tmdb_id = getattr(movie, "tmdb_id", None)
            if not tmdb_id:
                return {"id": mid, "language": None, "message": "No TMDB ID"}

            lang = self._tmdb_lookup_language(tmdb_id, api_key, read_token)
            if lang:
                self._languages[mid] = lang
                self._save_languages()
                return {"id": mid, "language": lang, "language_name": LANG_NAMES.get(lang, lang)}
            return {"id": mid, "language": None, "message": "Not found on TMDB"}
        except Exception as e:
            logger.error(f"detect_single_language error: {e}")
            return {"error": str(e)}

    def detect_language_all(self, body=None):
        api_key = self.settings.get("tmdb_api_key", "")
        read_token = self.settings.get("tmdb_read_token", "")
        if not api_key and not read_token:
            return {"error": "TMDB API key not configured"}

        if self._lang_detect_running:
            return {"status": "already_running"}

        limit = (body or {}).get("limit", "1000")
        thread = threading.Thread(target=self._bulk_detect_languages, args=(api_key, read_token, limit), daemon=True)
        thread.start()
        return {"status": "started", "message": "Bulk language detection started in background"}

    def _bulk_detect_languages(self, api_key, read_token="", limit="1000"):
        self._lang_detect_running = True
        try:
            from django.db import close_old_connections
            from apps.vod.models import Movie

            close_old_connections()

            known_ids = {int(mid) for mid in self._languages.keys()}

            if limit == "activated":
                activated_ids = [int(mid) for mid in self._activated.keys()]
                qs = Movie.objects.filter(id__in=activated_ids)
            else:
                qs = Movie.objects.all()

            movies = list(
                qs.exclude(id__in=known_ids)
                .exclude(tmdb_id__isnull=True)
                .exclude(tmdb_id="")
                .values("id", "tmdb_id")
            )

            try:
                limit_n = int(limit)
            except (ValueError, TypeError):
                limit_n = 0
            if limit_n > 0:
                movies = movies[:limit_n]

            total = len(movies)
            if not total:
                self._lang_status = "All languages detected"
                return

            est_minutes = max(1, round(total * 0.5 / 60))
            self._lang_status = f"Detecting languages in background: 0/{total} (~{est_minutes} min remaining)..."
            logger.info(f"Language detection started: {total} movies, estimated {est_minutes} min")

            detected = 0
            skipped = 0
            start_time = time.time()

            for i, movie in enumerate(movies):
                mid = str(movie["id"])
                tmdb_id = movie["tmdb_id"]
                lang = self._tmdb_lookup_language(tmdb_id, api_key, read_token)
                if lang:
                    self._languages[mid] = lang
                    detected += 1
                else:
                    skipped += 1

                time.sleep(0.5)
                processed = i + 1
                if processed % 25 == 0:
                    self._save_languages()
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 2
                    remaining = total - processed
                    est_min = max(1, round(remaining / rate / 60))
                    self._lang_status = (
                        f"Detecting languages in background: {processed}/{total} "
                        f"({detected} detected, ~{est_min} min remaining)..."
                    )

            self._save_languages()
            elapsed_min = round((time.time() - start_time) / 60, 1)
            self._lang_status = (
                f"Language detection complete: {detected} detected, {skipped} not found ({elapsed_min} min)"
            )
            logger.info(f"Bulk language detection complete: {detected} detected, {skipped} not found out of {total} in {elapsed_min} min")
        except Exception as e:
            logger.error(f"Bulk language detection failed: {e}")
            self._lang_status = f"Error: {str(e)[:200]}"
        finally:
            self._lang_detect_running = False

    def health_check(self, settings):
        checks = {}

        try:
            from apps.vod.models import Movie

            Movie.objects.count()
            checks["dispatcharr_db"] = {"status": "ok"}
        except Exception as e:
            checks["dispatcharr_db"] = {"status": "error", "message": str(e)}

        plex_url = settings.get("plex_url", "")
        plex_token = settings.get("plex_token", "")
        if plex_url and plex_token:
            try:
                import requests

                resp = requests.get(
                    f"{plex_url}/library/sections",
                    headers={"X-Plex-Token": plex_token},
                    timeout=5,
                )
                checks["plex"] = {
                    "status": "ok" if resp.status_code < 300 else "error",
                    "http_status": resp.status_code,
                }
            except Exception as e:
                checks["plex"] = {"status": "error", "message": str(e)}
        else:
            checks["plex"] = {"status": "unconfigured"}

        return {"health": checks}

    def get_plex_sessions(self, settings):
        plex_url = settings.get("plex_url", "")
        plex_token = settings.get("plex_token", "")
        if not plex_url or not plex_token:
            return {"sessions": [], "error": "Plex not configured"}

        try:
            import requests
            from xml.etree import ElementTree

            resp = requests.get(
                f"{plex_url}/status/sessions",
                headers={"X-Plex-Token": plex_token},
                timeout=5,
            )
            if resp.status_code != 200:
                return {"sessions": [], "error": f"HTTP {resp.status_code}"}

            root = ElementTree.fromstring(resp.text)
            sessions = []
            for video in root.findall(".//Video"):
                session = {
                    "title": video.get("title", ""),
                    "year": video.get("year", ""),
                    "state": "playing",
                    "view_offset": int(video.get("viewOffset", 0)),
                    "duration": int(video.get("duration", 0)),
                }

                player = video.find("Player")
                if player is not None:
                    session["player"] = player.get("title", "")
                    session["state"] = player.get("state", "playing")
                    session["device"] = player.get("device", "")
                    session["local"] = player.get("local", "1") == "1"

                media = video.find("Media")
                if media is not None:
                    session["video_codec"] = media.get("videoCodec", "")
                    session["audio_codec"] = media.get("audioCodec", "")
                    session["video_resolution"] = media.get("videoResolution", "")
                    session["bitrate"] = media.get("bitrate", "")

                    part = media.find("Part")
                    if part is not None:
                        file_path = part.get("file", "")
                        session["is_bridge"] = "vod-bridge" in file_path

                sessions.append(session)

            return {"sessions": sessions}
        except Exception as e:
            return {"sessions": [], "error": str(e)}

    def activate_movies(self, body):
        movie_ids = body.get("movie_ids", [])
        if not movie_ids:
            return {"status": "error", "message": "No movie_ids provided"}

        activated = []
        for mid in movie_ids:
            mid = str(mid)
            if mid not in self._activated:
                self._activated[mid] = {"activated_at": time.time()}
                activated.append(mid)

        self._save_state()

        if activated:
            strm_count = self._generate_strm_for_movies(activated)
            self._trigger_cache_fetch_for_movies(activated)
            self._trigger_plex_scan()
            return {"status": "ok", "activated": len(activated), "strm_generated": strm_count}

        return {"status": "ok", "activated": 0}

    def deactivate_movies(self, body):
        movie_ids = body.get("movie_ids", [])
        deactivated = []
        for mid in movie_ids:
            mid = str(mid)
            if mid in self._activated:
                del self._activated[mid]
                deactivated.append(mid)

        self._save_state()

        plex_removed = 0
        if deactivated:
            self._remove_strm_for_movies(deactivated)
            plex_removed = self._plex_delete_movies(deactivated)

        return {"status": "ok", "deactivated": len(deactivated), "plex_removed": plex_removed}

    def _generate_strm_for_movies(self, movie_ids):
        strm_dir = self.settings.get("strm_output_dir", "/data/strm")
        port = int(self.settings.get("http_port", 8888))
        host = self.settings.get("dashboard_host", "127.0.0.1")
        os.makedirs(strm_dir, exist_ok=True)

        count = 0
        try:
            from apps.vod.models import Movie
            for mid in movie_ids:
                try:
                    movie = Movie.objects.get(id=int(mid))
                except Movie.DoesNotExist:
                    continue

                name = self._clean_title(movie.name)
                year = getattr(movie, "year", None)
                folder_name = f"{name} ({year})" if year else name
                folder = os.path.join(strm_dir, folder_name)
                os.makedirs(folder, exist_ok=True)

                strm_url = f"http://{host}:{port}/vod/{mid}.mkv"
                strm_path = os.path.join(folder, f"{folder_name}.strm")
                with open(strm_path, "w") as f:
                    f.write(strm_url)

                self._write_nfo(movie, folder, folder_name)
                count += 1
                logger.info(f"STRM generated: {folder_name}")
        except Exception as e:
            logger.error(f"STRM generation error: {e}")
        return count

    def _remove_strm_for_movies(self, movie_ids):
        strm_dir = self.settings.get("strm_output_dir", "/data/strm")
        try:
            from apps.vod.models import Movie
            import shutil
            for mid in movie_ids:
                try:
                    movie = Movie.objects.get(id=int(mid))
                except Movie.DoesNotExist:
                    continue

                name = self._clean_title(movie.name)
                year = getattr(movie, "year", None)
                folder_name = f"{name} ({year})" if year else name
                folder = os.path.join(strm_dir, folder_name)
                if os.path.exists(folder):
                    shutil.rmtree(folder)
                    logger.info(f"STRM removed: {folder_name}")
        except Exception as e:
            logger.error(f"STRM removal error: {e}")

    def get_cache_status(self):
        """Return per-movie cache status for the dashboard."""
        result = []
        for mid in list(self._activated.keys()):
            head = os.path.exists(self._cache_path(mid, "head"))
            tail = os.path.exists(self._cache_path(mid, "tail"))
            fetching = mid in self._cache_fetching
            head_size = os.path.getsize(self._cache_path(mid, "head")) if head else 0
            result.append({"movie_id": mid, "head": head, "tail": tail,
                           "fetching": fetching, "head_bytes": head_size})
        return {"movies": result, "total": len(result),
                "cached": sum(1 for m in result if m["head"])}

    def trigger_cache_fetch_all(self):
        """Trigger background cache fetch for all activated movies missing cache."""
        dispatcharr_url = self.settings.get("dispatcharr_url", "").rstrip("/")
        if not dispatcharr_url:
            return {"status": "error", "message": "Dispatcharr URL not configured"}
        triggered = []
        try:
            from apps.vod.models import Movie
            for mid in list(self._activated.keys()):
                if not os.path.exists(self._cache_path(mid, "head")):
                    try:
                        movie = Movie.objects.get(id=int(mid))
                        relation = movie.m3u_relations.first()
                        if relation:
                            base_url = f"{dispatcharr_url}/proxy/vod/movie/{movie.uuid}?stream_id={relation.stream_id}"
                            self.start_cache_fetch(mid, base_url)
                            triggered.append(mid)
                    except Exception as e:
                        logger.warning(f"Cache fetch trigger failed for {mid}: {e}")
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "ok", "triggered": len(triggered)}

    def _trigger_cache_fetch_for_movies(self, movie_ids):
        """Start background head/tail cache fetch for newly activated movies."""
        dispatcharr_url = self.settings.get("dispatcharr_url", "").rstrip("/")
        if not dispatcharr_url:
            return
        try:
            from apps.vod.models import Movie
            for mid in movie_ids:
                try:
                    movie = Movie.objects.get(id=int(mid))
                    relation = movie.m3u_relations.first()
                    if not relation:
                        continue
                    base_url = f"{dispatcharr_url}/proxy/vod/movie/{movie.uuid}?stream_id={relation.stream_id}"
                    self.start_cache_fetch(mid, base_url)
                except Exception as e:
                    logger.warning(f"Cache fetch trigger failed for movie {mid}: {e}")
        except Exception as e:
            logger.error(f"Cache fetch trigger error: {e}")

    def _trigger_plex_scan(self):
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        section = self.settings.get("plex_library_section", 7)
        if not plex_url or not plex_token:
            return
        try:
            import requests
            requests.get(
                f"{plex_url}/library/sections/{section}/refresh",
                headers={"X-Plex-Token": plex_token},
                timeout=10,
            )
            logger.info("Plex library scan triggered")
        except Exception as e:
            logger.error(f"Plex scan failed: {e}")

    def _plex_delete_movies(self, movie_ids):
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        section = self.settings.get("plex_library_section", 7)
        if not plex_url or not plex_token:
            return 0
        try:
            import requests
            resp = requests.get(
                f"{plex_url}/library/sections/{section}/all",
                params={"X-Plex-Token": plex_token},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Plex library query failed: {resp.status_code}")
                return 0

            items = resp.json().get("MediaContainer", {}).get("Metadata", [])
            id_set = {str(mid) for mid in movie_ids}
            removed = 0

            for item in items:
                parts = item.get("Media", [{}])[0].get("Part", [])
                for part in parts:
                    filename = part.get("file", "")
                    m = re.search(r'[/\\](\d+)\.(mkv|mp4)$', filename)
                    if not m:
                        m = re.search(r'\[(\d+)\]\.(mkv|mp4)$', filename)
                    if m and m.group(1) in id_set:
                        rating_key = item.get("ratingKey")
                        title = item.get("title", "?")
                        del_resp = requests.delete(
                            f"{plex_url}/library/metadata/{rating_key}",
                            params={"X-Plex-Token": plex_token},
                            timeout=10,
                        )
                        if del_resp.status_code in (200, 204):
                            removed += 1
                            logger.info(f"Plex: deleted {title} (key {rating_key})")
                        else:
                            logger.warning(f"Plex delete {title} returned {del_resp.status_code}")
                        break

            logger.info(f"Plex cleanup: removed {removed} items")
            return removed
        except Exception as e:
            logger.error(f"Plex removal failed: {e}")
            return 0

    def generate_strm_files(self, settings, log):
        strm_dir = settings.get("strm_output_dir", "/data/strm")
        port = int(settings.get("http_port", 8888))
        host = settings.get("dashboard_host", "127.0.0.1")
        os.makedirs(strm_dir, exist_ok=True)

        count = 0
        try:
            from apps.vod.models import Movie

            for mid in list(self._activated.keys()):
                try:
                    movie = Movie.objects.get(id=int(mid))
                except Movie.DoesNotExist:
                    continue

                name = self._clean_title(movie.name)
                year = getattr(movie, "year", None)

                if year:
                    folder_name = f"{name} ({year})"
                else:
                    folder_name = name

                folder = os.path.join(strm_dir, folder_name)
                os.makedirs(folder, exist_ok=True)

                strm_url = f"http://{host}:{port}/vod/{mid}.mkv"

                strm_path = os.path.join(folder, f"{folder_name}.strm")
                with open(strm_path, "w") as f:
                    f.write(strm_url)

                self._write_nfo(movie, folder, folder_name)
                count += 1

        except Exception as e:
            log.error(f"STRM generation error: {e}")

        return count

    def _clean_title(self, name):
        name = re.sub(r"\s*\[.*?\]", "", name)
        name = re.sub(r"\s*\((?:4K|HDR|UHD|FHD|HD|SD)\)", "", name, flags=re.I)
        name = re.sub(r'[<>:"/\\|?*]', "", name)
        return name.strip()

    def _write_nfo(self, movie, folder, folder_name):
        nfo_path = os.path.join(folder, f"{folder_name}.nfo")
        tmdb_id = getattr(movie, "tmdb_id", None)
        if not tmdb_id:
            return

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<movie>",
            f"  <title>{self._xml_escape(movie.name)}</title>",
        ]

        year = getattr(movie, "year", None)
        if year:
            lines.append(f"  <year>{year}</year>")

        desc = getattr(movie, "description", "")
        if desc:
            lines.append(f"  <plot>{self._xml_escape(desc)}</plot>")

        rating = getattr(movie, "rating", None)
        if rating:
            lines.append(f"  <rating>{rating}</rating>")

        if tmdb_id:
            lines.append(f"  <tmdbid>{tmdb_id}</tmdbid>")
            lines.append(
                f"  <uniqueid type=\"tmdb\" default=\"true\">{tmdb_id}</uniqueid>"
            )

        genre = getattr(movie, "genre", "")
        if genre:
            for g in genre.split(","):
                g = g.strip()
                if g:
                    lines.append(f"  <genre>{self._xml_escape(g)}</genre>")

        poster = getattr(movie, "poster", "")
        if poster:
            lines.append(f"  <thumb>{self._xml_escape(poster)}</thumb>")

        lines.append("</movie>")

        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _xml_escape(self, s):
        if not s:
            return ""
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def list_vod_directory(self):
        if not self._activated:
            return "<html><body>\n</body></html>"

        links = []
        try:
            from apps.vod.models import Movie
            from urllib.parse import quote
            for mid in sorted(self._activated.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                try:
                    movie = Movie.objects.get(id=int(mid))
                except Movie.DoesNotExist:
                    continue

                name = self._clean_title(movie.name)
                year = getattr(movie, "year", None)
                if year:
                    fname = f"{name} ({year}) [{mid}].mkv"
                else:
                    fname = f"{name} [{mid}].mkv"

                links.append(f'<a href="{quote(fname)}">{fname}</a>')
        except Exception as e:
            logger.error(f"VOD directory listing error: {e}")

        return "<html><body>\n" + "\n".join(links) + "\n</body></html>"


    def _cache_path(self, movie_id, part):
        return os.path.join(self._cache_dir, f"{movie_id}_{part}.bin")

    def _fetch_and_cache(self, movie_id, base_url):
        """Fetch head+tail bytes from Dispatcharr and store to disk. Runs in background thread."""
        with self._cache_lock:
            if movie_id in self._cache_fetching:
                return
            self._cache_fetching.add(movie_id)
        try:
            # Resolve session first (safe — no provider connection opened)
            cache_key = f"{movie_id}:cache"
            resolved = self._resolve_session(base_url, cache_key)
            parsed = urlparse(resolved)
            path = parsed.path
            if parsed.query:
                path = f"{path}?{parsed.query}"
            Conn = HTTPSConnection if parsed.scheme == "https" else HTTPConnection

            # Fetch head
            head_path = self._cache_path(movie_id, "head")
            if not os.path.exists(head_path):
                conn = Conn(parsed.netloc, timeout=30)
                conn.request("GET", path, headers={"Range": f"bytes=0-{HEAD_CACHE_SIZE - 1}"})
                resp = conn.getresponse()
                if resp.status in (200, 206):
                    data = resp.read(HEAD_CACHE_SIZE)
                    conn.close()
                    with open(head_path, "wb") as f:
                        f.write(data)
                    logger.info(f"Head cache written for movie {movie_id}: {len(data)} bytes")
                    log_event("info", movie_id, "Head cache fetched", bytes=len(data))
                else:
                    conn.close()
                    logger.warning(f"Head cache fetch failed for movie {movie_id}: HTTP {resp.status}")
                    log_event("warn", movie_id, f"Head cache fetch failed: HTTP {resp.status}")
                    return

            # Fetch tail — need file size first from Content-Range header
            tail_path = self._cache_path(movie_id, "tail")
            if not os.path.exists(tail_path):
                conn = Conn(parsed.netloc, timeout=30)
                conn.request("GET", path, headers={"Range": "bytes=0-0"})
                resp = conn.getresponse()
                resp.read()
                cr = resp.getheader("Content-Range", "")
                conn.close()
                file_size = None
                if cr and "/" in cr:
                    try:
                        file_size = int(cr.split("/")[1])
                    except Exception:
                        pass
                if file_size and file_size > TAIL_CACHE_SIZE:
                    tail_start = file_size - TAIL_CACHE_SIZE
                    conn = Conn(parsed.netloc, timeout=30)
                    conn.request("GET", path, headers={"Range": f"bytes={tail_start}-{file_size - 1}"})
                    resp = conn.getresponse()
                    if resp.status in (200, 206):
                        data = resp.read(TAIL_CACHE_SIZE)
                        conn.close()
                        # Store tail with its start offset in a meta file
                        with open(tail_path, "wb") as f:
                            f.write(data)
                        with open(self._cache_path(movie_id, "tail_meta"), "w") as f:
                            json.dump({"start": tail_start, "size": file_size}, f)
                        logger.info(f"Tail cache written for movie {movie_id}: {len(data)} bytes @ {tail_start}")
                        log_event("info", movie_id, "Tail cache fetched", bytes=len(data), tail_start=tail_start, file_size=file_size)
                    else:
                        conn.close()
                        logger.warning(f"Tail cache fetch failed for movie {movie_id}: HTTP {resp.status}")
                        log_event("warn", movie_id, f"Tail cache fetch failed: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Cache fetch error for movie {movie_id}: {e}")
            log_event("error", movie_id, f"Cache fetch error: {e}")
        finally:
            with self._cache_lock:
                self._cache_fetching.discard(movie_id)

    def start_cache_fetch(self, movie_id, base_url):
        """Launch background cache fetch for a movie."""
        t = threading.Thread(target=self._fetch_and_cache, args=(movie_id, base_url), daemon=True)
        t.start()

    def get_cached_range(self, movie_id, range_start, range_end):
        """Return cached bytes if the requested range is fully covered, else None.
        range_end is inclusive. Returns (data, file_size) or None.
        """
        mid = str(movie_id)
        head_path = self._cache_path(mid, "head")
        tail_path = self._cache_path(mid, "tail")
        tail_meta_path = self._cache_path(mid, "tail_meta")

        file_size = None
        tail_start = None
        if os.path.exists(tail_meta_path):
            try:
                with open(tail_meta_path) as f:
                    meta = json.load(f)
                file_size = meta.get("size")
                tail_start = meta.get("start")
            except Exception:
                pass

        # Normalize range_end
        if range_end is None or (file_size and range_end >= file_size):
            range_end = (file_size - 1) if file_size else range_end

        # Check head cache
        if os.path.exists(head_path) and range_start is not None and range_start >= 0:
            try:
                head_data = open(head_path, "rb").read()
                head_size = len(head_data)
                if range_end is not None and range_end < head_size:
                    return head_data[range_start:range_end + 1], file_size
                elif range_end is None and range_start < head_size:
                    return head_data[range_start:], file_size
            except Exception:
                pass

        # Check tail cache
        if (os.path.exists(tail_path) and tail_start is not None
                and range_start is not None and range_start >= tail_start):
            try:
                tail_data = open(tail_path, "rb").read()
                offset = range_start - tail_start
                end_offset = (range_end - tail_start + 1) if range_end is not None else len(tail_data)
                if offset >= 0 and end_offset <= len(tail_data):
                    return tail_data[offset:end_offset], file_size
            except Exception:
                pass

        return None

    def has_cache(self, movie_id):
        return os.path.exists(self._cache_path(str(movie_id), "head"))

    def open_connection(self, movie_id):
        """Register an active provider connection. Returns False if one already exists."""
        mid = str(movie_id)
        with self._conn_lock:
            if self._active_connections.get(mid, 0) > 0:
                return False
            self._active_connections[mid] = 1
            return True

    def close_connection(self, movie_id):
        mid = str(movie_id)
        with self._conn_lock:
            if mid in self._active_connections:
                self._active_connections[mid] = max(0, self._active_connections[mid] - 1)
                if self._active_connections[mid] == 0:
                    del self._active_connections[mid]

    def has_active_connection(self, movie_id):
        return self._active_connections.get(str(movie_id), 0) > 0

    def _resolve_session(self, base_url, cache_key):
        """Follow Dispatcharr's 301 to get a session-scoped URL.

        Dispatcharr returns 301 to /proxy/vod/movie/{uuid}/{session_id}?stream_id=X
        when no session_id is in the path. We capture that Location header so
        subsequent requests reuse the same session instead of minting new ones.
        Returns the resolved URL, or base_url on failure.
        """
        from urllib.parse import urlparse
        from http.client import HTTPConnection, HTTPSConnection
        try:
            parsed = urlparse(base_url)
            path = parsed.path
            if parsed.query:
                path = f"{path}?{parsed.query}"
            Conn = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
            conn = Conn(parsed.netloc, timeout=15)
            conn.request("GET", path, headers={"Range": "bytes=0-0"})
            resp = conn.getresponse()
            location = resp.getheader("Location")
            resp.read()  # drain
            conn.close()
            if location and resp.status in (301, 302):
                if location.startswith("/"):
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                logger.info(f"Resolved session for {cache_key}: {location}")
                return location
        except Exception as e:
            logger.warning(f"Session resolve failed for {cache_key}: {e}")
        return base_url

    def get_redirect_url(self, movie_id):
        mid = str(movie_id)
        if mid not in self._activated:
            return None, "Movie not activated"

        dispatcharr_url = self.settings.get("dispatcharr_url", "").rstrip("/")
        if not dispatcharr_url:
            return None, "Dispatcharr URL not configured"

        try:
            from apps.vod.models import Movie
            movie = Movie.objects.get(id=int(mid))
        except Exception:
            return None, "Movie not found"

        uuid = str(movie.uuid)
        relation = movie.m3u_relations.first()
        if not relation:
            return None, "No stream mapping for movie"

        stream_id = relation.stream_id
        # Return bare Dispatcharr URL — no pre-resolution. Dispatcharr issues a fresh
        # 301 to a new session URL on each request. rclone gets a new session per play,
        # and Dispatcharr manages session lifecycle. Pre-resolving caused rclone to cache
        # the session URL and reuse it indefinitely, opening new provider connections
        # without the plugin knowing.
        return f"{dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={stream_id}", None

    def get_movie_info(self, movie_id):
        mid = str(movie_id)
        if mid not in self._activated:
            return None

        try:
            from apps.vod.models import Movie
            movie = Movie.objects.get(id=int(mid))
        except Exception:
            return None

        info = {
            "name": movie.name,
            "uuid": str(movie.uuid),
            "content_type": "video/x-matroska",
            "file_size": self._estimate_size(movie),
        }

        relation = movie.m3u_relations.first()
        if relation:
            info["stream_id"] = relation.stream_id
            ext = getattr(relation, "container_extension", None) or "mkv"
            if ext.lstrip(".") == "mp4":
                info["content_type"] = "video/mp4"

        return info

    def _estimate_size(self, movie):
        duration = getattr(movie, "duration_secs", None)
        if duration and duration > 0:
            return int(duration) * 250000
        return 2 * 1024 * 1024 * 1024

    def trigger_plex_scan(self, settings):
        plex_url = settings.get("plex_url", "")
        plex_token = settings.get("plex_token", "")
        section = settings.get("plex_library_section", 7)

        if not plex_url or not plex_token:
            return {"status": "error", "message": "Plex not configured"}

        try:
            import requests

            resp = requests.get(
                f"{plex_url}/library/sections/{section}/refresh",
                headers={"X-Plex-Token": plex_token},
                timeout=10,
            )
            return {
                "status": "ok" if resp.status_code < 300 else "error",
                "http_status": resp.status_code,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
