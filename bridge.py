import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

import requests

logger = logging.getLogger("vod_plex_bridge.bridge")

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

    # Minimum time a bridge session must sit in "buffering" with a
    # non-advancing view_offset before we treat it as stuck and advance to
    # the next stream relation, rather than the one-time cached pick living
    # forever (see mark_stream_bad / get_redirect_url).
    STALL_THRESHOLD_SECS = 25
    # Once we've auto-advanced a movie's stream pick, don't do it again for
    # this long — prevents a genuinely bad batch of providers from being
    # burned through in rapid succession.
    STALL_COOLDOWN_SECS = 600

    # Default interval (seconds) between checks for whether activated movies
    # still exist in Dispatcharr's VOD catalog, if not overridden by the
    # "removed_check_interval_secs" plugin setting. 300s (5 min) is frequent
    # enough to catch removals soon after an M3U refresh without hammering
    # the DB every watchdog tick (~10s).
    DEFAULT_REMOVED_CHECK_INTERVAL_SECS = 300

    # Default days between automatic stream-pick refreshes for activated
    # movies, if not overridden by the "stream_refresh_interval_days" plugin
    # setting. Light touch only (clears the cached provider pick so the next
    # play re-resolves fresh) — not a full deactivate/reactivate, since this
    # runs unattended and shouldn't disrupt Plex watch state on a schedule.
    DEFAULT_STREAM_REFRESH_INTERVAL_DAYS = 7

    # Delay between each movie's refresh within one scheduled pass, so a
    # library-wide refresh doesn't burst requests against providers all at
    # once — spreads them out the same way a human clicking through movies
    # one at a time would.
    STREAM_REFRESH_DELAY_SECS = 7

    # Max number of activity-log entries kept (in memory and on disk) for the
    # dashboard's Logs tab. Oldest entries drop off as new ones are appended,
    # so this is also the natural archive point — a busy server with lots of
    # activity rotates through its history faster than a quiet one.
    ACTIVITY_LOG_MAXLEN = 500

    def __init__(self, settings):
        self.settings = settings
        self._activated = {}
        self._languages = {}
        self._data_dir = "/data/vod-plex-bridge"
        self._lang_detect_running = False
        self._lang_status = ""
        self._stall_watch = {}  # movie_id -> {"view_offset": int, "since": float}
        self._stall_last_switch = {}  # movie_id -> timestamp of last auto-advance
        self._last_play_log = {}  # (movie_id, stream_id) -> timestamp of last logged redirect
        self._redirect_locks = {}  # movie_id -> threading.Lock, serializes concurrent get_redirect_url calls
        self._redirect_locks_guard = threading.Lock()  # protects _redirect_locks dict itself
        self._recent_redirects = {}  # movie_id -> (timestamp, redirect_url, error, account_id, stream_id)
        self._watchdog_thread = None
        self._watchdog_stop = threading.Event()
        self._watchdog_ticks = 0
        self._last_removed_check = 0.0
        self._last_stream_refresh_check = 0.0
        self._activity_log = deque(maxlen=self.ACTIVITY_LOG_MAXLEN)
        # Running counters surfaced on the Health tab so cleanup/refresh
        # activity is visible without digging through the activity log.
        # Persisted in bridge_state.json alongside _activated.
        self._maint_stats = {
            "auto_refreshed_total": 0,
            "manual_refreshed_total": 0,
            "reactivated_total": 0,
            "removed_total": 0,
            "audio_checked_total": 0,
            "audio_missing_total": 0,
            "last_auto_refresh": None,    # {"ts", "refreshed", "skipped_playing", "names"}
            "last_manual_refresh": None,  # {"ts", "refreshed", "names"}
            "last_reactivate": None,      # {"ts", "reactivated", "names"}
            "last_removed_check": None,   # {"ts", "checked", "removed", "removed_names"}
            "last_audio_check": None,     # {"ts", "movie_id", "name", "stream_id", "provider", "status"}
        }

    def initialize(self):
        os.makedirs(self._data_dir, exist_ok=True)
        self._load_state()
        self._load_activity_log()
        logger.info(
            f"BridgeCore initialized. {len(self._activated)} activated movies."
        )
        self._start_stall_watchdog()

    def cleanup(self):
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            # Bound the wait: the loop only checks the stop event every 10s
            # and may be mid-way through a Plex/DB call, so give it a window
            # to exit cleanly rather than blocking Stop Server indefinitely.
            self._watchdog_thread.join(timeout=15)
            if self._watchdog_thread.is_alive():
                logger.warning(
                    "VOD To Plex: stall watchdog thread did not stop within "
                    "15s of shutdown — it will keep running until it next "
                    "wakes and observes the stop signal."
                )
        self._save_state()

    def _activity_log_path(self):
        return os.path.join(self._data_dir, "activity_log.json")

    def _load_activity_log(self):
        try:
            with open(self._activity_log_path(), "r") as f:
                entries = json.load(f)
            self._activity_log.extend(entries[-self.ACTIVITY_LOG_MAXLEN:])
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Failed to load activity log: {e}")

    def _save_activity_log(self):
        try:
            with open(self._activity_log_path(), "w") as f:
                json.dump(list(self._activity_log), f)
        except Exception as e:
            logger.error(f"Failed to save activity log: {e}")

    def _log_event(self, level, message):
        self._activity_log.append({"ts": time.time(), "level": level, "message": message})
        self._save_activity_log()

    def get_activity_log(self):
        return list(self._activity_log)

    def clear_activity_log(self):
        self._activity_log.clear()
        self._save_activity_log()
        return {"status": "ok"}

    def _start_stall_watchdog(self):
        self._watchdog_thread = threading.Thread(
            target=self._stall_watchdog_loop,
            daemon=True,
            name="vod-bridge-stall-watchdog",
        )
        self._watchdog_thread.start()

    def _stall_watchdog_loop(self):
        while not self._watchdog_stop.wait(10):
            try:
                self._check_for_stalls()
            except Exception as e:
                logger.error(f"Stall watchdog error: {e}")

            self._watchdog_ticks += 1
            try:
                interval = int(self.settings.get(
                    "removed_check_interval_secs",
                    self.DEFAULT_REMOVED_CHECK_INTERVAL_SECS,
                ))
            except (TypeError, ValueError):
                interval = self.DEFAULT_REMOVED_CHECK_INTERVAL_SECS
            interval = max(interval, 10)

            now = time.time()
            if now - self._last_removed_check >= interval:
                self._last_removed_check = now
                try:
                    self._reconcile_removed_movies()
                except Exception as e:
                    logger.error(f"Removed-movie reconciliation error: {e}")

            try:
                refresh_days = float(self.settings.get(
                    "stream_refresh_interval_days",
                    self.DEFAULT_STREAM_REFRESH_INTERVAL_DAYS,
                ))
            except (TypeError, ValueError):
                refresh_days = self.DEFAULT_STREAM_REFRESH_INTERVAL_DAYS

            if refresh_days > 0:
                refresh_secs = refresh_days * 86400
                if now - self._last_stream_refresh_check >= refresh_secs:
                    self._last_stream_refresh_check = now
                    try:
                        self._auto_refresh_stream_picks(refresh_secs)
                    except Exception as e:
                        logger.error(f"Scheduled stream-pick refresh error: {e}")

            # Background stream revalidation is disabled entirely (feature
            # flagged off pending investigation into a suspected link with
            # Dispatcharr VOD proxy connections getting stuck at near-zero
            # progress — see PLUGIN_SUMMARY.md incident log, 2026-07-11).
            # Deliberately does not read revalidation_interval_secs at all
            # so a stray configure_plugin write can't silently re-enable it.

    def _check_for_stalls(self):
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        if not plex_url or not plex_token or not self._activated:
            return

        result = self.get_plex_sessions(self.settings)
        sessions = result.get("sessions", [])
        bridge_sessions = [s for s in sessions if s.get("is_bridge")]

        now = time.time()
        seen_mids = set()

        for session in bridge_sessions:
            mid = self._match_session_to_movie(session)
            if mid is None:
                continue
            seen_mids.add(mid)

            if session.get("state") != "buffering":
                self._stall_watch.pop(mid, None)
                continue

            offset = session.get("view_offset", 0)
            watch = self._stall_watch.get(mid)
            if watch is None or watch["view_offset"] != offset:
                self._stall_watch[mid] = {"view_offset": offset, "since": now}
                continue

            stalled_for = now - watch["since"]
            if stalled_for < self.STALL_THRESHOLD_SECS:
                continue

            last_switch = self._stall_last_switch.get(mid, 0)
            if now - last_switch < self.STALL_COOLDOWN_SECS:
                continue

            entry = self._activated.get(mid, {})
            current_stream_id = entry.get("stream_pick")
            if current_stream_id is None:
                # Not resolved yet (never played) — nothing to advance away from.
                continue

            if self.mark_stream_bad(mid, current_stream_id):
                logger.warning(
                    f"Movie {mid}: stuck buffering for {stalled_for:.0f}s at offset "
                    f"{offset} — auto-advanced to next stream"
                )
                self._stall_last_switch[mid] = now
                self._stall_watch.pop(mid, None)

        # Drop stall-tracking for movies no longer actively buffering/playing.
        for mid in list(self._stall_watch.keys()):
            if mid not in seen_mids:
                self._stall_watch.pop(mid, None)

    def _reconcile_removed_movies(self):
        """Clean up activated movies that no longer exist in Dispatcharr's VOD
        catalog (e.g. dropped by an M3U account refresh).

        Dispatcharr owns the Movie/M3UMovieRelation rows and deletes them
        itself when a provider's VOD list no longer contains an item — this
        plugin only tracks activation state on top of that. Without this
        check, an activated movie that Dispatcharr removes leaves behind an
        orphaned STRM folder, a stale Plex library entry, and a dead entry in
        self._activated forever, since nothing else in the plugin re-checks
        existence after activation time.
        """
        if not self._activated:
            return

        try:
            from apps.vod.models import Movie
        except Exception:
            return

        activated_ids = [int(mid) for mid in self._activated.keys() if mid.isdigit()]
        if not activated_ids:
            return

        existing_ids = set(
            str(i) for i in Movie.objects.filter(id__in=activated_ids).values_list("id", flat=True)
        )
        removed = [mid for mid in self._activated.keys() if mid not in existing_ids]

        if not removed:
            self._maint_stats["last_removed_check"] = {
                "ts": time.time(), "checked": len(activated_ids), "removed": 0,
            }
            self._save_state()
            self._log_event(
                "info",
                f"Cleanup check: {len(activated_ids)} activated movie(s) checked, none removed",
            )
            return

        # Names must be resolved before removal — the movie's own catalog row
        # is already gone at this point, so folder_hints/strm_folder (captured
        # at activation time) is the only source left for a human-readable title.
        removed_names = [
            self._activated[mid].get("strm_folder", f"#{mid}") for mid in removed
        ]

        logger.warning(
            f"Reconciliation: {len(removed)} activated movie(s) no longer in "
            f"Dispatcharr's VOD catalog — removing: {removed}"
        )

        folder_hints = {mid: self._activated[mid].get("strm_folder") for mid in removed}
        self._remove_strm_for_movies(removed, folder_hints=folder_hints)
        self._plex_delete_movies(removed)

        for mid in removed:
            self._activated.pop(mid, None)

        self._maint_stats["removed_total"] += len(removed)
        self._maint_stats["last_removed_check"] = {
            "ts": time.time(), "checked": len(activated_ids), "removed": len(removed),
            "removed_names": removed_names,
        }
        self._save_state()

        titles = ", ".join(f'"{n}"' for n in removed_names)
        self._log_event(
            "warn",
            f"Cleanup check: {len(activated_ids)} activated movie(s) checked, "
            f"{len(removed)} removed ({titles}) — no longer in Dispatcharr's catalog",
        )

    def _auto_refresh_stream_picks(self, refresh_secs):
        """Scheduled refresh: clear the cached stream_pick for any activated
        movie whose last refresh is older than the configured interval, then
        resolve the new pick and audio-probe it once (same ffprobe check as
        the manual Audio Check button) so a stream_id that has gone dead
        since activation — e.g. a provider M3U refresh silently rewriting or
        orphaning the relation Dispatcharr had on file — gets caught and
        auto-advanced here instead of surfacing only when a human notices
        Plex playback is broken.

        This does NOT probe on every play (get_redirect_url stays untouched —
        rclone calls it on every Range/seek request, and a probe there would
        reintroduce the connection-holding/provider-churn problems that got
        head/tail caching and per-request liveness probing reverted in
        v0.1.6/v0.1.7/v0.1.9). One probe per movie per refresh interval is the
        bounded cost accepted here. Does not touch STRM files or the Plex
        library entry — the heavier full deactivate+reactivate remains a
        manual, deliberate action via reactivate_movies().

        Movies currently in an active Plex session are skipped for this pass
        (checked again next cycle) rather than interrupting playback by
        swapping the pick out from under it. A short delay is inserted between
        each movie so a large library doesn't burst requests at providers all
        at once.
        """
        if not self._activated:
            return

        now = time.time()
        due = [
            mid for mid, entry in self._activated.items()
            if now - entry.get("last_refreshed", entry.get("activated_at", 0)) >= refresh_secs
        ]
        if not due:
            return

        playing_mids = self._currently_playing_movie_ids()

        refreshed = 0
        skipped_playing = 0
        refreshed_mids = []
        audio_failed_mids = []
        for mid in due:
            if mid in playing_mids:
                skipped_playing += 1
                continue

            outcome = self._refresh_stream_pick(mid)
            if outcome:
                refreshed += 1
                refreshed_mids.append(mid)
                if outcome.get("audio_status") not in ("ok", None):
                    audio_failed_mids.append(mid)

            if self._watchdog_stop.wait(self.STREAM_REFRESH_DELAY_SECS):
                break

        self._maint_stats["auto_refreshed_total"] += refreshed
        self._maint_stats["last_auto_refresh"] = {
            "ts": time.time(), "refreshed": refreshed, "skipped_playing": skipped_playing,
            "names": self._movie_names(refreshed_mids),
            "audio_failed_names": self._movie_names(audio_failed_mids),
        }
        self._save_state()

        titles = ", ".join(f'"{n}"' for n in self._movie_names(due))
        self._log_event(
            "info",
            f"Scheduled stream refresh: {len(due)} movie(s) due ({titles}), {refreshed} "
            f"refreshed, {skipped_playing} skipped (currently playing)",
        )
        if audio_failed_mids:
            bad_titles = ", ".join(f'"{n}"' for n in self._movie_names(audio_failed_mids))
            self._log_event(
                "warn",
                f"Scheduled refresh: {len(audio_failed_mids)} movie(s) had no working audio "
                f"on every stream tried and could not be auto-fixed: {bad_titles}",
            )

    def _refresh_stream_pick(self, mid):
        """Clear the cached stream_pick for one movie, resolve a fresh pick,
        and audio-probe it — auto-advancing through the movie's other
        M3UMovieRelation options (mirrors mark_stream_bad's advance logic) if
        the first pick fails, so a dead stream_id doesn't just get silently
        re-cached. Returns a dict with the outcome, or False if the movie
        isn't activated / has no stream mapping."""
        entry = self._activated.get(mid)
        if entry is None:
            return False
        entry.pop("stream_pick", None)
        entry["last_refreshed"] = time.time()
        self._activated[mid] = entry

        try:
            from apps.vod.models import Movie
            movie = Movie.objects.get(id=int(mid))
        except Exception:
            return {"audio_status": None}

        relations = list(movie.m3u_relations.all())
        if not relations:
            return {"audio_status": None}

        tried_ids = set()
        result = None
        relation = None
        for _ in range(len(relations)):
            _movie, relation, _entry, error = self._resolve_relation(mid, persist_pick=True)
            if error or relation is None:
                break
            if str(relation.stream_id) in tried_ids:
                break
            tried_ids.add(str(relation.stream_id))

            result = self._probe_audio_for_relation(movie, relation)
            self._record_audio_probe_stats(movie, relation, result, persist=False)
            self._log_audio_probe_result(movie, relation, result)

            if result.get("status") == "ok":
                break

            # Probe failed — advance past this relation and try the next one.
            if not self.mark_stream_bad(mid, relation.stream_id):
                break

        return {"audio_status": result.get("status") if result else None}

    def _currently_playing_movie_ids(self):
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        if not plex_url or not plex_token:
            return set()
        try:
            result = self.get_plex_sessions(self.settings)
        except Exception:
            return set()
        sessions = result.get("sessions", [])
        playing = set()
        for session in sessions:
            if not session.get("is_bridge"):
                continue
            mid = self._match_session_to_movie(session)
            if mid is not None:
                playing.add(mid)
        return playing

    def refresh_movies(self, body):
        """Manual per-card/bulk 'Refresh' — same as the scheduled job (clear
        cached stream_pick, resolve fresh, audio-probe, auto-advance on
        failure), but on demand and without the currently-playing skip, since
        a manual click is an explicit user request for this specific movie
        right now."""
        movie_ids = body.get("movie_ids", [])
        refreshed = [str(mid) for mid in movie_ids if self._refresh_stream_pick(str(mid))]
        if refreshed:
            self._maint_stats["manual_refreshed_total"] += len(refreshed)
            names = self._movie_names(refreshed)
            self._maint_stats["last_manual_refresh"] = {
                "ts": time.time(), "refreshed": len(refreshed), "names": names,
            }
            self._save_state()
            titles = ", ".join(f'"{n}"' for n in names)
            self._log_event(
                "info",
                f"Manual refresh: {len(refreshed)} movie(s) stream pick cleared: {titles}",
            )
            return {"status": "ok", "refreshed": len(refreshed), "names": names}
        return {"status": "ok", "refreshed": 0, "names": []}

    def reactivate_movies(self, body):
        """Manual 'Reactivate' — fixes a stuck/dead stream for an already-
        activated movie by clearing its cached stream_pick and rewriting its
        STRM/NFO file in place (same folder/filename). Deliberately does NOT
        touch Plex: no delete, no scan trigger. Plex sees the STRM's target
        change on its own next scan. This is intentionally lighter than a
        manual deactivate+activate — the previous implementation called
        deactivate_movies()+activate_movies(), which deleted the Plex library
        entry as a side effect once Plex delete-matching started working
        correctly; that side effect is not wanted here, only on an explicit
        Deactivate."""
        movie_ids = [str(mid) for mid in body.get("movie_ids", [])]
        targets = [mid for mid in movie_ids if mid in self._activated]
        if not targets:
            return {"status": "ok", "reactivated": 0, "names": []}

        for mid in targets:
            self._refresh_stream_pick(mid)
        self._generate_strm_for_movies(targets)
        self._save_state()

        reactivated = len(targets)
        self._maint_stats["reactivated_total"] += reactivated
        names = self._movie_names(targets)
        self._maint_stats["last_reactivate"] = {
            "ts": time.time(), "reactivated": reactivated, "names": names,
        }
        self._save_state()

        titles = ", ".join(f'"{n}"' for n in names)
        self._log_event(
            "info",
            f"Reactivated {reactivated} movie(s): {titles} — STRM refreshed, Plex untouched",
        )
        return {"status": "ok", "reactivated": reactivated, "names": names}

    def _match_session_to_movie(self, session):
        title = session.get("title", "")
        year = str(session.get("year", ""))
        if not title:
            return None

        try:
            from apps.vod.models import Movie
        except Exception:
            return None

        for mid in self._activated.keys():
            try:
                movie = Movie.objects.get(id=int(mid))
            except Exception:
                continue
            if self._clean_title(movie.name) != title:
                continue
            movie_year = str(getattr(movie, "year", "") or "")
            if year and movie_year and year != movie_year:
                continue
            return mid
        return None

    def _load_state(self):
        state_file = os.path.join(self._data_dir, "bridge_state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r") as f:
                    state = json.load(f)
                self._activated = state.get("activated", {})
                self._maint_stats.update(state.get("maint_stats", {}))
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
                json.dump({"activated": self._activated, "maint_stats": self._maint_stats}, f)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _save_languages(self):
        lang_file = os.path.join(self._data_dir, "language_cache.json")
        try:
            with open(lang_file, "w") as f:
                json.dump(self._languages, f)
        except Exception as e:
            logger.error(f"Failed to save language cache: {e}")

    def _audio_checks(self, entry):
        checks = entry.get("audio_checks")
        return checks if isinstance(checks, dict) else {}

    def _get_cached_audio_check(self, entry, stream_id=None):
        if not isinstance(entry, dict):
            return None
        target_stream_id = str(stream_id or entry.get("stream_pick") or "")
        if not target_stream_id:
            return None
        return self._audio_checks(entry).get(target_stream_id)

    def _store_audio_check(self, movie_id, stream_id, result):
        mid = str(movie_id)
        sid = str(stream_id)
        entry = self._activated.get(mid, {})
        checks = self._audio_checks(entry)
        checks[sid] = result
        entry["audio_checks"] = checks
        self._activated[mid] = entry

    def _record_audio_probe_stats(self, movie, relation, result, persist=False):
        mid = str(movie.id)
        self._store_audio_check(mid, relation.stream_id, result)
        self._maint_stats["audio_checked_total"] += 1
        if result.get("status") == "missing":
            self._maint_stats["audio_missing_total"] += 1
        self._maint_stats["last_audio_check"] = {
            "ts": result.get("checked_at"),
            "movie_id": mid,
            "name": movie.name,
            "stream_id": str(relation.stream_id),
            "provider": result.get("provider_name"),
            "status": result.get("status"),
        }
        if persist:
            self._save_state()

    def _log_audio_probe_result(self, movie, relation, result):
        audio_count = result.get("audio_stream_count")
        codec_list = ", ".join(result.get("audio_codecs", [])) or "none"
        self._log_event(
            "info" if result.get("status") == "ok" else "warn",
            f'Audio check: "{movie.name}" via {result.get("provider_name")} '
            f'(stream {relation.stream_id}) - {result.get("status")} '
            f"(audio={audio_count if audio_count is not None else '?'}; codecs={codec_list})",
        )

    def _current_audio_summary(self, movie_id):
        entry = self._activated.get(str(movie_id), {})
        cached = self._get_cached_audio_check(entry)
        if cached:
            return {
                "status": cached.get("status", "unknown"),
                "checked_at": cached.get("checked_at"),
                "stream_id": str(cached.get("stream_id", entry.get("stream_pick", "")) or ""),
                "provider_name": cached.get("provider_name"),
                "audio_stream_count": cached.get("audio_stream_count"),
                "audio_codecs": cached.get("audio_codecs", []),
                "message": cached.get("message", ""),
            }
        if entry.get("stream_pick"):
            return {
                "status": "unknown",
                "checked_at": None,
                "stream_id": str(entry.get("stream_pick")),
                "provider_name": None,
                "audio_stream_count": None,
                "audio_codecs": [],
                "message": "Current stream has not been audio-checked yet",
            }
        return {
            "status": "unknown",
            "checked_at": None,
            "stream_id": "",
            "provider_name": None,
            "audio_stream_count": None,
            "audio_codecs": [],
            "message": "No stream selected yet",
        }

    def get_stats(self):
        return {
            "catalog_count": self._get_catalog_count(),
            "activated_count": len(self._activated),
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
            "categories": categories,
        }

    def list_movies(self, query):
        try:
            from apps.vod.models import Movie

            page = int(query.get("page", [1])[0])
            per_page = int(query.get("per_page", [50])[0])
            search = query.get("search", [""])[0]
            director = query.get("director", [""])[0]
            provider_ids = [v for v in query.get("provider_id", []) if v]
            category_ids = [v for v in query.get("category_id", []) if v]
            languages = [v for v in query.get("language", []) if v]
            activated_only = query.get("activated_only", [""])[0]

            qs = Movie.objects.all()

            if search:
                qs = qs.filter(name__icontains=search)

            if director:
                qs = qs.filter(custom_properties__director__icontains=director)

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
                director_name = None
                try:
                    cp = getattr(m, "custom_properties", None) or {}
                    if isinstance(cp, str):
                        import json as _json
                        cp = _json.loads(cp)
                    trailer_key = cp.get("youtube_trailer") or cp.get("trailer") or None
                    director_name = cp.get("director") or None
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
                        "director": director_name,
                        "language": self._languages.get(mid),
                        "audio_check": self._current_audio_summary(mid) if mid in self._activated else None,
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
            director = query.get("director", [""])[0]
            provider_ids = [v for v in query.get("provider_id", []) if v]
            category_ids = [v for v in query.get("category_id", []) if v]
            languages = [v for v in query.get("language", []) if v]

            qs = Movie.objects.all()
            if search:
                qs = qs.filter(name__icontains=search)
            if director:
                qs = qs.filter(custom_properties__director__icontains=director)
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

        return {"health": checks, "maintenance": self._maint_stats}

    def get_plex_sessions(self, settings):
        plex_url = settings.get("plex_url", "")
        plex_token = settings.get("plex_token", "")
        if not plex_url or not plex_token:
            return {"sessions": [], "error": "Plex not configured"}

        try:
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
                    file_path = part.get("file", "") if part is not None else ""
                    if "vod-plugin" in file_path:
                        session["is_bridge"] = True
                    elif file_path:
                        session["is_bridge"] = False
                    else:
                        session["is_bridge"] = self._match_session_to_movie(session) is not None

                sessions.append(session)

            return {"sessions": sessions}
        except Exception as e:
            return {"sessions": [], "error": str(e)}

    def deactivate_movies(self, body):
        movie_ids = body.get("movie_ids", [])
        deactivated = []
        folder_hints = {}
        for mid in movie_ids:
            mid = str(mid)
            if mid in self._activated:
                folder_hints[mid] = self._activated[mid].get("strm_folder")
                del self._activated[mid]
                deactivated.append(mid)

        self._save_state()

        plex_removed = 0
        names = []
        if deactivated:
            self._remove_strm_for_movies(deactivated, folder_hints=folder_hints)
            plex_removed = self._plex_delete_movies(deactivated)
            names = self._movie_names(deactivated)
            titles = ", ".join(f'"{n}"' for n in names)
            self._log_event(
                "info",
                f"Deactivated {len(deactivated)} movie(s): {titles} — removed {plex_removed} from Plex",
            )

        return {"status": "ok", "deactivated": len(deactivated), "plex_removed": plex_removed, "names": names}

    def _generate_strm_for_movies(self, movie_ids):
        strm_dir = self.settings.get("strm_output_dir", "/data/strm")
        port = int(self.settings.get("http_port", 8888))
        host = self.settings.get("dashboard_host", "127.0.0.1")
        os.makedirs(strm_dir, exist_ok=True)

        count = 0
        from apps.vod.models import Movie
        for mid in movie_ids:
            try:
                movie = Movie.objects.get(id=int(mid))
            except Movie.DoesNotExist:
                continue

            try:
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
                # Stored so removal (deactivation, or reconciliation when a
                # movie disappears from Dispatcharr) never has to recompute
                # this from a Movie row that may no longer exist.
                if mid in self._activated:
                    self._activated[mid]["strm_folder"] = folder_name
                count += 1
                logger.info(f"STRM generated: {folder_name}")
            except Exception as e:
                logger.error(f"STRM generation failed for movie {mid} ({movie.name}): {e}")
                self._log_event("error", f'STRM generation failed for "{movie.name}" (id={mid}): {e}')
        return count

    def _get_dispatcharr_stream_url(self, movie, settings=None):
        """Return the direct Dispatcharr proxy URL for a movie, or None if unavailable."""
        s = settings if settings is not None else self.settings
        dispatcharr_url = s.get("dispatcharr_url", "").rstrip("/")
        if not dispatcharr_url:
            return None
        try:
            relation = movie.m3u_relations.first()
            if not relation:
                return None
            return self._build_dispatcharr_proxy_url(movie, relation, settings=s)
        except Exception:
            return None

    def _build_dispatcharr_proxy_url(self, movie, relation, settings=None):
        s = settings if settings is not None else self.settings
        dispatcharr_url = s.get("dispatcharr_url", "").rstrip("/")
        if not dispatcharr_url:
            return None
        return f"{dispatcharr_url}/proxy/vod/movie/{movie.uuid}?stream_id={relation.stream_id}"

    def _remove_strm_for_movies(self, movie_ids, folder_hints=None):
        """Delete each movie's STRM/NFO folder.

        Prefers the folder name stored at activation time (folder_hints, or
        self._activated[mid]["strm_folder"]) so removal works even if the
        Movie row is already gone from Dispatcharr's DB (e.g. after an M3U
        refresh drops it). Only falls back to recomputing the name from the
        live Movie row for older activations from before strm_folder was
        tracked.
        """
        strm_dir = self.settings.get("strm_output_dir", "/data/strm")
        folder_hints = folder_hints or {}
        import shutil
        try:
            from apps.vod.models import Movie
        except Exception:
            Movie = None

        for mid in movie_ids:
            folder_name = folder_hints.get(mid) or self._activated.get(mid, {}).get("strm_folder")

            if not folder_name and Movie is not None:
                try:
                    movie = Movie.objects.get(id=int(mid))
                    name = self._clean_title(movie.name)
                    year = getattr(movie, "year", None)
                    folder_name = f"{name} ({year})" if year else name
                except Exception:
                    folder_name = None

            if not folder_name:
                logger.warning(f"STRM removal skipped for movie {mid}: no known folder name")
                continue

            try:
                folder = os.path.join(strm_dir, folder_name)
                if os.path.exists(folder):
                    shutil.rmtree(folder)
                    logger.info(f"STRM removed: {folder_name}")
            except Exception as e:
                logger.error(f"STRM removal error for {folder_name}: {e}")

    def _trigger_plex_scan(self):
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        section = self.settings.get("plex_library_section", 7)
        if not plex_url or not plex_token:
            return
        try:
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
        name = re.sub(r"\s*\(\d{4}\)\s*$", "", name)
        name = re.sub(r"\s*-\s*\d{4}\s*$", "", name)
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

    # Repeated redirects for the same (movie_id, stream_id) within this window
    # are collapsed into a single activity-log line — rclone re-hits
    # get_redirect_url() on every Range/seek request during one playback, so
    # without this a single movie can produce dozens of near-identical
    # "redirect OK" lines that bury real signal in the activity log.
    PLAY_LOG_DEDUP_SECS = 60

    def log_play_request(self, movie_id, client_ip, ok, detail=None, account_id=None, stream_id=None):
        mid = str(movie_id)
        try:
            from apps.vod.models import Movie
            name = Movie.objects.get(id=int(mid)).name
        except Exception:
            name = f"#{mid}"

        if ok:
            key = (mid, str(stream_id))
            now = time.time()
            last = self._last_play_log.get(key)
            if last is not None and (now - last) < self.PLAY_LOG_DEDUP_SECS:
                return
            self._last_play_log[key] = now
            via = f" (via {self._account_name(account_id)})" if account_id else ""
            self._log_event("info", f"Play request: \"{name}\" (id={mid}) from {client_ip} — redirect OK{via}")
        else:
            self._log_event("error", f"Play request: \"{name}\" (id={mid}) from {client_ip} — FAILED: {detail}")

    def _account_name(self, account_id):
        try:
            from apps.m3u.models import M3UAccount
            return M3UAccount.objects.get(id=int(account_id)).name
        except Exception:
            return f"account #{account_id}"

    def _resolve_relation(self, movie_id, persist_pick=False):
        mid = str(movie_id)
        if mid not in self._activated:
            return None, None, None, "Movie not activated"

        dispatcharr_url = self.settings.get("dispatcharr_url", "").rstrip("/")
        if not dispatcharr_url:
            return None, None, None, "Dispatcharr URL not configured"

        try:
            from apps.vod.models import Movie
            movie = Movie.objects.get(id=int(mid))
        except Exception:
            logger.warning(f"Movie not found: id={mid}")
            return None, None, None, "Movie not found"

        relations = list(movie.m3u_relations.all())
        if not relations:
            logger.warning(f"No stream mapping for movie {mid} ({movie.name})")
            return movie, None, None, "No stream mapping for movie"

        entry = self._activated.get(mid, {})
        cached_stream_id = entry.get("stream_pick")
        relation = relations[0]
        if cached_stream_id is not None:
            for r in relations:
                if str(r.stream_id) == str(cached_stream_id):
                    relation = r
                    break

        relation = self._pick_relation_with_capacity(relations, relation)

        if persist_pick:
            entry["stream_pick"] = relation.stream_id
            self._activated[mid] = entry
            self._save_state()

        return movie, relation, entry, None

    def _probe_audio_for_relation(self, movie, relation):
        stream_id = str(relation.stream_id)
        account_id = str(relation.m3u_account_id) if relation.m3u_account_id else "unknown"
        provider_name = self._account_name(account_id)
        started = time.time()
        result = {
            "status": "unknown",
            "checked_at": started,
            "stream_id": stream_id,
            "account_id": account_id,
            "provider_name": provider_name,
            "audio_stream_count": None,
            "audio_codecs": [],
            "video_stream_count": None,
            "format_name": None,
            "message": "",
            "method": "ffprobe",
        }

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            result["status"] = "probe_failed"
            result["message"] = "ffprobe is not installed in the plugin runtime"
            return result

        url = self._build_dispatcharr_proxy_url(movie, relation)
        if not url:
            result["status"] = "probe_failed"
            result["message"] = "Dispatcharr URL not configured"
            return result

        # Dispatcharr's VOD proxy always 301s this URL to a session-scoped
        # one (e.g. .../movie/{uuid}/vod_<session>?stream_id=...) before it
        # actually claims a connection slot. Left alone, ffprobe follows
        # that redirect itself — closing its first connection and opening a
        # second — so a single logical probe costs two capacity slots on
        # the provider. The 301 lookup itself is a cheap, bodyless Django
        # route (no slot claimed), so resolve it here and hand ffprobe the
        # final URL directly to collapse the probe back to one connection.
        session_id = None
        try:
            resp = requests.get(url, allow_redirects=False, stream=True, timeout=5)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                location = resp.headers["Location"]
                if location.startswith("/"):
                    base = url.split("/proxy/vod/", 1)[0]
                    location = base + location
                url = location
                m = re.search(r"/(vod_[^/?]+)", location)
                if m:
                    session_id = m.group(1)
            resp.close()
        except Exception:
            pass  # fall back to the unresolved URL; ffprobe will follow the redirect itself

        cmd = [
            ffprobe,
            "-v", "error",
            "-rw_timeout", "5000000",
            "-probesize", "262144",
            "-analyzeduration", "1000000",
            "-show_entries", "stream=index,codec_type,codec_name,channels:format=format_name",
            "-of", "json",
            url,
        ]

        try:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=12,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                result["status"] = "probe_failed"
                result["message"] = "ffprobe timed out while probing the stream"
                return result
            except Exception as e:
                result["status"] = "probe_failed"
                result["message"] = f"ffprobe execution failed: {e}"
                return result

            if proc.returncode != 0:
                result["status"] = "probe_failed"
                result["message"] = (proc.stderr or proc.stdout or "ffprobe failed").strip()[:240]
                return result

            try:
                data = json.loads(proc.stdout or "{}")
            except Exception as e:
                result["status"] = "probe_failed"
                result["message"] = f"Invalid ffprobe JSON: {e}"
                return result

            streams = data.get("streams", []) or []
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_codecs = sorted({s.get("codec_name", "") for s in audio_streams if s.get("codec_name")})

            result["audio_stream_count"] = len(audio_streams)
            result["video_stream_count"] = len(video_streams)
            result["audio_codecs"] = audio_codecs
            result["format_name"] = ((data.get("format") or {}).get("format_name") or "")
            result["status"] = "ok" if audio_streams else "missing"
            result["message"] = (
                f"Detected {len(audio_streams)} audio stream(s)"
                if audio_streams else
                "No audio streams detected"
            )
            return result
        finally:
            # ffprobe's connection is short-lived, but Dispatcharr's Redis
            # session hash (what Plex's Active Connections reads from)
            # otherwise lingers for up to ~57 minutes holding a real slot.
            # The stop-signal key (what stop_vod_client sets) is only ever
            # checked inside an active streaming loop every 100 chunks, so
            # it never fires for a probe this small. Call the connection
            # manager's own cleanup directly instead — the same call
            # cleanup_stale_persistent_connections() makes once a
            # connection is confirmed idle.
            if session_id:
                try:
                    from apps.proxy.vod_proxy.multi_worker_connection_manager import (
                        MultiWorkerVODConnectionManager,
                        RedisBackedVODConnection,
                    )
                    manager = MultiWorkerVODConnectionManager.get_instance()
                    # Dispatcharr's own active_streams decrement
                    # (decrement_active_streams_and_check) can silently no-op
                    # under lock contention -- observed live: two of a
                    # probe's three internal range-seek requests raced for
                    # the same per-session lock, both lost, and the
                    # documented "safe default" on lock-loss is to leave
                    # active_streams un-decremented ("assume streams
                    # remain"). That permanently strands this session above
                    # 0 with no natural path back down, so
                    # cleanup_persistent_connection() would refuse to delete
                    # it forever. We know definitively this probe-owned
                    # session has no real viewer by the time ffprobe exits,
                    # so force active_streams to 0 directly before cleanup.
                    conn = RedisBackedVODConnection(session_id, manager.redis_client)
                    state = conn._get_connection_state()
                    if state and state.active_streams > 0:
                        if conn._acquire_lock():
                            try:
                                state = conn._get_connection_state()
                                if state and state.active_streams > 0:
                                    state.active_streams = 0
                                    conn._save_connection_state(state)
                            finally:
                                conn._release_lock()
                    manager.cleanup_persistent_connection(session_id)
                except Exception as e:
                    logger.debug(f"Could not clean up probe session {session_id}: {e}")

    def check_movie_audio(self, body):
        movie_ids = body.get("movie_ids", [])
        if not movie_ids:
            return {"status": "error", "message": "No movie_ids provided"}

        mid = str(movie_ids[0])
        movie, relation, _entry, error = self._resolve_relation(mid, persist_pick=True)
        if error:
            return {"status": "error", "message": error}

        result = self._probe_audio_for_relation(movie, relation)
        self._store_audio_check(mid, relation.stream_id, result)
        self._maint_stats["audio_checked_total"] += 1
        if result.get("status") == "missing":
            self._maint_stats["audio_missing_total"] += 1
        self._maint_stats["last_audio_check"] = {
            "ts": result.get("checked_at"),
            "movie_id": mid,
            "name": movie.name,
            "stream_id": str(relation.stream_id),
            "provider": result.get("provider_name"),
            "status": result.get("status"),
        }
        self._save_state()

        audio_count = result.get("audio_stream_count")
        codec_list = ", ".join(result.get("audio_codecs", [])) or "none"
        self._log_event(
            "info" if result.get("status") == "ok" else "warn",
            f'Audio check: "{movie.name}" via {result.get("provider_name")} '
            f'(stream {relation.stream_id}) — {result.get("status")} '
            f"(audio={audio_count if audio_count is not None else '?'}; codecs={codec_list})",
        )

        return {
            "status": "ok",
            "movie_id": mid,
            "name": movie.name,
            "audio_check": result,
        }

    def activate_movies(self, body):
        movie_ids = body.get("movie_ids", [])
        if not movie_ids:
            return {"status": "error", "message": "No movie_ids provided"}

        activated = []
        activated_names = []
        failed = []
        failed_names = []
        try:
            from apps.vod.models import Movie
        except Exception as e:
            return {"status": "error", "message": str(e)}

        for mid in movie_ids:
            mid = str(mid)
            if mid in self._activated:
                continue

            try:
                movie = Movie.objects.get(id=int(mid))
            except Exception:
                failed.append({"id": mid, "name": f"#{mid}", "message": "Movie not found"})
                failed_names.append(f"#{mid}")
                continue

            relations = list(movie.m3u_relations.all())
            if not relations:
                failed.append({"id": mid, "name": movie.name, "message": "No stream mapping for movie"})
                failed_names.append(movie.name)
                continue

            chosen_relation = None
            audio_checks = {}
            for relation in relations:
                if not self._account_has_capacity(relation.m3u_account_id):
                    logger.info(
                        f"Skipping audio probe for movie {mid} via account "
                        f"{relation.m3u_account_id} — no free connection slot"
                    )
                    continue
                result = self._probe_audio_for_relation(movie, relation)
                audio_checks[str(relation.stream_id)] = result
                self._record_audio_probe_stats(movie, relation, result, persist=False)
                self._log_audio_probe_result(movie, relation, result)
                if result.get("status") == "ok":
                    chosen_relation = relation
                    break

            if chosen_relation is None:
                failed.append({
                    "id": mid,
                    "name": movie.name,
                    "message": "No provider stream with detectable audio found",
                })
                failed_names.append(movie.name)
                continue

            self._activated[mid] = {
                "activated_at": time.time(),
                "audio_checks": audio_checks,
                "stream_pick": chosen_relation.stream_id,
            }
            activated.append(mid)
            activated_names.append(movie.name)

        self._save_state()

        if activated:
            strm_count = self._generate_strm_for_movies(activated)
            self._save_state()
            self._trigger_plex_scan()
            names = activated_names or self._movie_names(activated)
            titles = ", ".join(f'"{n}"' for n in names)
            self._log_event(
                "info",
                f'Activated {len(activated)} movie(s): {titles} - generated {strm_count} STRM file(s)',
            )
            if failed:
                failed_titles = ", ".join(f'"{n}"' for n in failed_names)
                self._log_event(
                    "warn",
                    f"Activation skipped {len(failed)} movie(s) with no detectable audio: {failed_titles}",
                )
            return {
                "status": "ok",
                "activated": len(activated),
                "strm_generated": strm_count,
                "names": names,
                "failed": failed,
                "failed_names": failed_names,
            }

        if failed:
            failed_titles = ", ".join(f'"{n}"' for n in failed_names)
            self._log_event(
                "warn",
                f"Activation failed: no detectable audio on any provider for {len(failed)} movie(s): {failed_titles}",
            )
            return {
                "status": "error",
                "message": "No provider stream with detectable audio found",
                "activated": 0,
                "names": [],
                "failed": failed,
                "failed_names": failed_names,
            }

        return {"status": "ok", "activated": 0, "names": [], "failed": []}

    def check_movie_audio(self, body):
        movie_ids = body.get("movie_ids", [])
        if not movie_ids:
            return {"status": "error", "message": "No movie_ids provided"}

        mid = str(movie_ids[0])
        movie, relation, _entry, error = self._resolve_relation(mid, persist_pick=True)
        if error:
            return {"status": "error", "message": error}

        result = self._probe_audio_for_relation(movie, relation)
        self._record_audio_probe_stats(movie, relation, result, persist=True)
        self._log_audio_probe_result(movie, relation, result)

        return {
            "status": "ok",
            "movie_id": mid,
            "name": movie.name,
            "audio_check": result,
        }

    def _movie_names(self, movie_ids):
        try:
            from apps.vod.models import Movie
            ids = [int(m) for m in movie_ids]
            rows = Movie.objects.filter(id__in=ids).values_list("id", "name")
            names = {str(i): n for i, n in rows}
            return [names.get(str(m), f"#{m}") for m in movie_ids]
        except Exception:
            return [f"#{m}" for m in movie_ids]

    def _account_has_capacity(self, account_id):
        """True if the given M3U account's default active profile currently
        has a free connection slot, per Dispatcharr's own Redis-backed
        connection pool (apps.m3u.connection_pool) — the same check
        apps.proxy.vod_proxy.views uses before it 503s a request. Returns
        True (assume available) if the check can't be performed for any
        reason, so a lookup failure here never blocks playback outright —
        worst case we're back to today's behavior for that one request.
        """
        try:
            from apps.m3u.models import M3UAccountProfile
            from apps.m3u.connection_pool import pool_has_capacity_for_profile
            from core.utils import RedisClient

            profile = M3UAccountProfile.objects.filter(
                m3u_account_id=account_id, is_active=True
            ).order_by("-is_default").first()
            if profile is None:
                return True

            redis_client = RedisClient.get_client()
            return pool_has_capacity_for_profile(profile, redis_client)
        except Exception as e:
            logger.debug(f"Capacity check skipped for account {account_id}: {e}")
            return True

    def _pick_relation_with_capacity(self, relations, preferred):
        """Return `preferred` if its account has a free connection slot right
        now, otherwise the first other relation (in DB order) whose account
        does. Falls back to `preferred` unchanged if nothing has room, so
        callers still get the previous behavior (and its error message)
        rather than a new failure mode."""
        if self._account_has_capacity(preferred.m3u_account_id):
            return preferred

        for r in relations:
            if r is preferred:
                continue
            if self._account_has_capacity(r.m3u_account_id):
                logger.info(
                    f"Account {preferred.m3u_account_id} at capacity — "
                    f"switching movie stream pick to account {r.m3u_account_id}"
                )
                return r

        return preferred

    # How long a resolved redirect for a movie is reused for duplicate/rapid
    # follow-up requests, instead of re-resolving and issuing a fresh 302.
    # rclone (and Plex probing behavior) can fire several near-simultaneous
    # requests for the same movie+range; without this each one independently
    # races Dispatcharr's proxy for a provider connection slot, and any that
    # lose get a 429/503 and retry immediately — a self-inflicted request
    # storm on the same already-at-capacity provider. Coalescing them behind
    # one lock means only one request per movie resolves/redirects at a time;
    # the rest wait briefly and reuse that result instead of piling on.
    REDIRECT_COALESCE_SECS = 3

    def _get_redirect_lock(self, movie_id):
        with self._redirect_locks_guard:
            lock = self._redirect_locks.get(movie_id)
            if lock is None:
                lock = threading.Lock()
                self._redirect_locks[movie_id] = lock
            return lock

    # rclone's VFS read-ahead opens several concurrent connections for
    # different byte ranges of the *same* file within milliseconds of each
    # other — the coalescing cache above correctly gives them all the same
    # resolved stream, but each one still gets its own immediate 302 and
    # independently races Dispatcharr's proxy for a connection slot right
    # after. If the account is already out of room when a burst duplicate
    # (a cache hit, not the first resolution) comes through, redirecting it
    # immediately is a guaranteed 503 — so stagger it briefly instead, on
    # the chance an earlier connection in the same burst finishes seating
    # or drops before this one reaches Dispatcharr.
    REDIRECT_BURST_STAGGER_SECS = 0.4

    def get_redirect_url(self, movie_id):
        mid = str(movie_id)
        lock = self._get_redirect_lock(mid)

        with lock:
            cached = self._recent_redirects.get(mid)
            if cached and (time.time() - cached[0]) < self.REDIRECT_COALESCE_SECS:
                _, redirect_url, error, account_id, stream_id = cached
                if redirect_url and account_id and not self._account_has_capacity(account_id):
                    time.sleep(self.REDIRECT_BURST_STAGGER_SECS)
                return redirect_url, error, account_id, stream_id

            movie, relation, _entry, error = self._resolve_relation(movie_id, persist_pick=True)
            if error:
                result = (None, error, None, None)
                self._recent_redirects[mid] = (time.time(), *result)
                return result

            stream_id = relation.stream_id
            account_id = str(relation.m3u_account_id) if relation.m3u_account_id else "unknown"
            # Bare Dispatcharr URL, no pre-resolution and no liveness probe here —
            # a HEAD request against Dispatcharr's proxy doesn't open a real
            # streaming connection, so it can't catch a provider that accepts the
            # connection and then stalls/buffers (the actual common failure mode
            # here) — only the stall watchdog, which watches real Plex playback
            # state, can detect that. See mark_stream_bad() / _check_for_stalls().
            redirect_url = self._build_dispatcharr_proxy_url(movie, relation)
            result = (redirect_url, None, account_id, stream_id)
            self._recent_redirects[mid] = (time.time(), *result)
            return result

    def mark_stream_bad(self, movie_id, stream_id):
        """Advance the cached stream pick to the next available relation for a
        movie, so future plays skip a confirmed-dead stream_id. Called manually
        (e.g. from the dashboard) after a movie is confirmed not playing."""
        mid = str(movie_id)
        if mid not in self._activated:
            return False

        try:
            from apps.vod.models import Movie
            movie = Movie.objects.get(id=int(mid))
        except Exception as e:
            logger.warning(f"mark_stream_bad: movie {mid} lookup failed: {e}")
            return False

        relations = list(movie.m3u_relations.all())
        remaining = [r for r in relations if str(r.stream_id) != str(stream_id)]
        if not remaining:
            return False

        entry = self._activated.get(mid, {})
        entry["stream_pick"] = remaining[0].stream_id
        self._activated[mid] = entry
        self._save_state()
        logger.info(f"Movie {mid}: switched stream pick away from {stream_id} to {remaining[0].stream_id}")
        return True

    def get_movie_info(self, movie_id):
        mid = str(movie_id)
        if mid not in self._activated:
            return None

        try:
            from apps.vod.models import Movie
            movie = Movie.objects.get(id=int(mid))
        except Exception as e:
            logger.warning(f"get_movie_info: movie {mid} lookup failed: {e}")
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
