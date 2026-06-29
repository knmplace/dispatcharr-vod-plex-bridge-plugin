import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("vod_plex_bridge.bridge")


class BridgeCore:
    """Core bridge logic. Accesses Dispatcharr VOD data via Django ORM."""

    def __init__(self, settings):
        self.settings = settings
        self._activated = {}
        self._data_dir = "/data/vod-plex-bridge"

    def initialize(self):
        os.makedirs(self._data_dir, exist_ok=True)
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

    def _save_state(self):
        state_file = os.path.join(self._data_dir, "bridge_state.json")
        try:
            with open(state_file, "w") as f:
                json.dump({"activated": self._activated}, f)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

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
            category_id = query.get("category_id", [None])[0]
            activated_only = query.get("activated_only", [""])[0]

            qs = Movie.objects.all()

            if search:
                qs = qs.filter(name__icontains=search)

            if category_id:
                qs = qs.filter(m3u_relations__category_id=int(category_id)).distinct()

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
            category_id = query.get("category_id", [None])[0]

            qs = Movie.objects.all()
            if search:
                qs = qs.filter(name__icontains=search)
            if category_id:
                qs = qs.filter(m3u_relations__category_id=int(category_id)).distinct()

            ids = list(qs.values_list("id", flat=True))
            return {"movie_ids": [str(i) for i in ids], "count": len(ids)}
        except Exception as e:
            return {"movie_ids": [], "error": str(e)}

    def list_categories(self, query):
        try:
            from apps.vod.models import VODCategory
            from django.db.models import Count

            cats = []
            for cat in VODCategory.objects.annotate(
                movie_count=Count("m3umovierelation")
            ).filter(movie_count__gt=0).order_by("name"):
                cats.append(
                    {"id": cat.id, "name": cat.name, "count": cat.movie_count}
                )
            return {"categories": cats}
        except Exception as e:
            return {"categories": [], "error": str(e)}

    def list_providers(self, query):
        try:
            from apps.m3u.models import M3UAccount

            providers = []
            for acc in M3UAccount.objects.filter(is_active=True):
                providers.append({"id": acc.id, "name": acc.name})
            return {"providers": providers}
        except Exception as e:
            return {"providers": [], "error": str(e)}

    def list_active_streams(self):
        return {"streams": [], "count": 0}

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

        if deactivated:
            self._remove_strm_for_movies(deactivated)
            self._trigger_plex_scan()

        return {"status": "ok", "deactivated": len(deactivated)}

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
        url = f"{dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
        return url, None

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
