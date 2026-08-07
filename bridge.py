import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

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

    # Interval (seconds) between maintenance-cycle sweeps that backfill
    # confirmed_size (Plex's own recorded file size, read back via its API)
    # for any activated movie that doesn't have one yet — catches movies
    # activated before this feature existed, and any fast-path miss below.
    # 600s (10 min) is frequent enough that a missed fast-path pick-up still
    # self-heals well inside the ~18-20 min window Plex's own re-scan timer
    # runs on (see bead npx size-mismatch investigation).
    SIZE_RECONCILE_INTERVAL_SECS = 600

    # Fast path after activation: Plex analyzes a freshly-added item almost
    # immediately (observed ~13s for a real title), so poll a few times at
    # increasing delay rather than waiting for the next maintenance sweep.
    # Gives up after ~2 min and leaves it to the maintenance sweep.
    SIZE_RECONCILE_FAST_PATH_DELAYS_SECS = (20, 20, 20, 30, 30)

    # Mirrors SIZE_RECONCILE_INTERVAL_SECS / SIZE_RECONCILE_FAST_PATH_DELAYS_SECS,
    # but for backfilling a real tmdb_id onto series activated with a
    # placeholder uniqueid (bead czo). Same two-tier shape: a fast retry
    # burst right after activation, then a slower maintenance sweep to catch
    # anything the fast path missed (e.g. Plex hadn't matched the show yet).
    TMDB_RECONCILE_INTERVAL_SECS = 600
    TMDB_RECONCILE_FAST_PATH_DELAYS_SECS = (20, 20, 20, 30, 30)

    # Daily TMDB detection for unresolved (placeholder) series.
    # Runs once per 24 hours, searches TMDB for series with is_placeholder=True
    # and searched=False, stores top results for manual user review.
    TMDB_DETECTION_INTERVAL_SECS = 86400  # 24 hours
    TMDB_DETECTION_CONFIDENCE_THRESHOLD = 80  # Accept matches >= 80% confidence

    # Delay between each series' TMDB search within one detection pass,
    # to avoid hammering the API.
    TMDB_SEARCH_DELAY_SECS = 0.5

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

    # _last_play_log, _redirect_locks, _recent_redirects, and
    # _stall_last_switch all gain one entry per distinct movie (or
    # movie+stream) ever played/redirected/stalled, and — unlike
    # _stall_watch, which self-prunes every watchdog tick — nothing ever
    # removed entries from them, so a long-lived server process accumulated
    # one permanently-retained dict entry per title touched over its entire
    # uptime. Each is only ever read via a short time-window comparison
    # (PLAY_LOG_DEDUP_SECS / REDIRECT_COALESCE_SECS / STALL_COOLDOWN_SECS,
    # all well under an hour), so anything older than this is guaranteed
    # stale for every one of those checks and safe to drop. Swept once per
    # watchdog tick alongside _check_for_stalls().
    STALE_TRACKING_ENTRY_MAXAGE_SECS = 3600
    STALE_TRACKING_SWEEP_INTERVAL_SECS = 600

    def __init__(self, settings):
        self.settings = settings
        self._activated = {}
        self._episodes_activated = {}  # episode_id (str) -> {activated_at, stream_pick, category_id, strm_folder}
        self._series_categories = []  # [{id, name, strm_folder, plex_library_section}]
        self._series_tmdb_state = {}  # series_id (str) -> {tmdb_id, is_placeholder, series_dir, series_name, category_id}
        self._plex_scan_lock = threading.Lock()  # serializes _trigger_plex_scan calls (movies + series)
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
        self._episode_redirect_locks = {}  # episode_id -> threading.Lock, mirrors _redirect_locks for episodes
        self._episode_redirect_locks_guard = threading.Lock()
        self._recent_episode_redirects = {}  # episode_id -> (timestamp, redirect_url, error, account_id, stream_id)
        self._watchdog_thread = None
        self._watchdog_stop = threading.Event()
        self._watchdog_ticks = 0
        # Episode activation queue: activate_episodes() only enqueues a job
        # and returns immediately; a single worker thread drains jobs
        # (and batches within a job) one at a time, so a 300-episode
        # activation can never stack concurrent Plex scans/DB load on top
        # of whatever else is already running, no matter how many more
        # activation clicks land while it's in progress (see bead vpv).
        self._episode_jobs = {}  # job_id -> {status, total, done, batches_total, batches_done, created_at, category_name}
        self._episode_job_queue = deque()  # job_ids waiting/running, FIFO
        self._episode_job_lock = threading.Lock()  # guards _episode_jobs + _episode_job_queue
        self._episode_job_wake = threading.Event()  # signals worker that a new job was queued
        self._episode_job_stop = threading.Event()
        self._episode_job_counter = 0
        self._episode_job_worker_thread = None
        self._last_removed_check = 0.0
        self._last_removed_episode_check = 0.0
        self._last_stream_refresh_check = 0.0
        self._last_size_reconcile = 0.0
        self._last_tmdb_reconcile = 0.0
        self._last_tmdb_detection = 0.0
        self._tmdb_detection_results = {}  # series_id (str) -> {results: [{tmdb_id, title, year, confidence, poster_path}], searched: bool, manual_pick: tmdb_id or None}
        self._last_tracking_sweep = 0.0
        self._activity_log = deque(maxlen=self.ACTIVITY_LOG_MAXLEN)
        self._diagnostic_log = deque(maxlen=5000)
        max_concurrent_heads = int(settings.get("head_request_max_concurrent", 5))
        self._head_request_semaphore = threading.Semaphore(max_concurrent_heads)
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
            "last_size_reconcile": None,  # {"ts", "checked", "confirmed", "names"}
        }

    def initialize(self):
        os.makedirs(self._data_dir, exist_ok=True)
        self._load_state()
        self._load_activity_log()
        logger.info(
            f"BridgeCore initialized. {len(self._activated)} activated movies."
        )
        self._start_stall_watchdog()
        self._start_episode_job_worker()
        threading.Thread(target=self._backfill_series_tmdb_state, daemon=True).start()

    def _start_episode_job_worker(self):
        self._episode_job_worker_thread = threading.Thread(
            target=self._episode_job_worker_loop,
            daemon=True,
            name="vod-bridge-episode-activation-worker",
        )
        self._episode_job_worker_thread.start()

    def _backfill_series_tmdb_state(self):
        """One-time-per-restart repair for series activated before bead czo
        shipped: those tvshow.nfo files already exist on disk with no
        <uniqueid>, and _write_tvshow_nfo()'s skip-if-exists guard means a
        newly-activated episode in the same series never touches them again.
        Rebuilds the series_dir the same way _generate_strm_for_episodes()
        does and force-rewrites tvshow.nfo through the normal resolve path
        (sibling-row check, else placeholder) for every series in
        _episodes_activated not yet tracked in _series_tmdb_state. Runs once
        per plugin start, off the main thread since it touches disk/DB for
        every distinct activated series."""
        try:
            from apps.vod.models import Episode
        except Exception as e:
            logger.error(f"Series tmdb backfill: model import failed: {e}")
            return

        pending = {}
        for entry in self._episodes_activated.values():
            sid = entry.get("series_id")
            if not sid or sid in self._series_tmdb_state or sid in pending:
                continue
            pending[sid] = entry.get("category_id")

        if not pending:
            return

        backfilled = 0
        for sid, category_id in pending.items():
            category = self._resolve_series_category(category_id)
            if category is None:
                continue
            try:
                episode = Episode.objects.select_related("series").filter(
                    series_id=int(sid)
                ).first()
                if episode is None:
                    continue
                series = episode.series
                series_name = self._clean_title(series.name)
                year = getattr(series, "year", None)
                series_folder_name = f"{series_name} ({year})" if year else series_name
                series_dir = os.path.join(
                    self._series_category_path(category["strm_folder"]), series_folder_name
                )
                if not os.path.isdir(series_dir):
                    continue
                self._write_tvshow_nfo(
                    series, series_dir, clean_title=series_name,
                    force=True, category_id=category_id,
                )
                backfilled += 1
            except Exception as e:
                logger.error(f"Series tmdb backfill failed for series {sid}: {e}")

        if backfilled:
            self._save_state()
            logger.info(f"Series tmdb backfill: repaired tvshow.nfo for {backfilled}/{len(pending)} pre-existing series")

    def cleanup(self):
        self._watchdog_stop.set()
        self._episode_job_stop.set()
        self._episode_job_wake.set()  # unblock the worker if it's waiting on a new job
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
        if self._episode_job_worker_thread is not None:
            # The worker only checks the stop event between batches, so it
            # may be mid-batch; give it a window to finish that batch and
            # exit cleanly rather than blocking Stop Server indefinitely.
            self._episode_job_worker_thread.join(timeout=15)
            if self._episode_job_worker_thread.is_alive():
                logger.warning(
                    "VOD To Plex: episode activation worker did not stop "
                    "within 15s of shutdown — it will keep running until it "
                    "finishes its current batch and observes the stop signal."
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

    def _log_diagnostic(self, level, message):
        """Log technical diagnostic info (hidden from dashboard, in bug reports)."""
        self._diagnostic_log.append({"ts": time.time(), "level": level, "message": message})

    def get_activity_log(self):
        return list(self._activity_log)

    def clear_activity_log(self):
        self._activity_log.clear()
        self._save_activity_log()
        return {"status": "ok"}

    # Matches http(s):// URLs anywhere in a log line — activity-log text
    # never contains raw provider stream URLs today (see server.py's
    # redirect logging, which goes to the stdlib logger instead), but this
    # is a content-based scrub rather than one that trusts that staying
    # true, so a future log line that does embed one is still caught.
    _URL_RE = re.compile(r"https?://\S+")

    def _sanitize_log_text(self, text, provider_map):
        text = self._URL_RE.sub("http://example.com/redacted", text)
        for real_name, placeholder in provider_map.items():
            if real_name:
                text = text.replace(real_name, placeholder)
        return text

    def _build_provider_scrub_map(self):
        """Map each M3U account's real name to a stable-within-this-export
        placeholder ("Provider 1", "Provider 2", ...), ordered by account id
        so the mapping is deterministic for a given catalog."""
        try:
            from apps.m3u.models import M3UAccount
            accounts = list(M3UAccount.objects.order_by("id").values_list("name", flat=True))
        except Exception:
            accounts = []
        return {name: f"Provider {i + 1}" for i, name in enumerate(accounts) if name}

    def build_bug_report_bundle(self, hours):
        """Return a sanitized snapshot of recent activity-log entries as a
        list of plain-text lines, newest last. Provider/M3U account names are
        replaced with generic placeholders and any literal URL is replaced
        with an example.com placeholder before this ever leaves the process
        — the caller (server.py) writes these lines straight into the
        exported zip with no further access to the unsanitized text."""
        provider_map = self._build_provider_scrub_map()
        cutoff = time.time() - (hours * 3600)

        lines = []
        lines.append("=== ACTIVITY LOG ===")
        lines.append("")
        for entry in self._activity_log:
            if entry.get("ts", 0) < cutoff:
                continue
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("ts", 0)))
            level = str(entry.get("level", "info")).upper()
            message = self._sanitize_log_text(str(entry.get("message", "")), provider_map)
            lines.append(f"[{ts_str}] {level}: {message}")

        lines.append("")
        lines.append("=== DIAGNOSTIC LOG (Technical Details) ===")
        lines.append("")
        for entry in self._diagnostic_log:
            if entry.get("ts", 0) < cutoff:
                continue
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("ts", 0)))
            level = str(entry.get("level", "info")).upper()
            message = self._sanitize_log_text(str(entry.get("message", "")), provider_map)
            lines.append(f"[{ts_str}] {level}: {message}")

        return lines

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

            try:
                self._enforce_max_concurrent_globally()
            except Exception as e:
                logger.error(f"Global max-concurrent sweep error: {e}")

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

            if now - self._last_removed_episode_check >= interval:
                self._last_removed_episode_check = now
                try:
                    self._reconcile_removed_episodes()
                except Exception as e:
                    logger.error(f"Removed-episode reconciliation error: {e}")

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

            if now - self._last_size_reconcile >= self.SIZE_RECONCILE_INTERVAL_SECS:
                self._last_size_reconcile = now
                try:
                    self._reconcile_all_confirmed_sizes()
                except Exception as e:
                    logger.error(f"Size reconcile sweep error: {e}")
                try:
                    self._reconcile_all_confirmed_episode_sizes()
                except Exception as e:
                    logger.error(f"Episode size reconcile sweep error: {e}")

            if now - self._last_tmdb_reconcile >= self.TMDB_RECONCILE_INTERVAL_SECS:
                self._last_tmdb_reconcile = now
                try:
                    self._reconcile_all_series_tmdb_ids()
                except Exception as e:
                    logger.error(f"Series tmdb reconcile sweep error: {e}")

            if now - self._last_tmdb_detection >= self.TMDB_DETECTION_INTERVAL_SECS:
                self._last_tmdb_detection = now
                try:
                    self._run_tmdb_detection_sweep()
                except Exception as e:
                    logger.error(f"TMDB detection sweep error: {e}")

            if now - self._last_tracking_sweep >= self.STALE_TRACKING_SWEEP_INTERVAL_SECS:
                self._last_tracking_sweep = now
                try:
                    self._prune_stale_tracking_entries()
                except Exception as e:
                    logger.error(f"Stale tracking sweep error: {e}")

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

    def _prune_stale_tracking_entries(self):
        """Evict entries older than STALE_TRACKING_ENTRY_MAXAGE_SECS from the
        per-movie tracking dicts that otherwise only ever grow (see the
        comment on STALE_TRACKING_ENTRY_MAXAGE_SECS). Each dict's own
        consumer only ever compares against a much shorter window, so
        nothing this old can still be "live" for any of them."""
        cutoff = time.time() - self.STALE_TRACKING_ENTRY_MAXAGE_SECS

        for mid, ts in list(self._stall_last_switch.items()):
            if ts < cutoff:
                self._stall_last_switch.pop(mid, None)

        for key, ts in list(self._last_play_log.items()):
            if ts < cutoff:
                self._last_play_log.pop(key, None)

        for mid, cached in list(self._recent_redirects.items()):
            if cached[0] < cutoff:
                self._recent_redirects.pop(mid, None)

        # _redirect_locks holds live threading.Lock objects with no
        # timestamp of their own, so age can't be judged directly. Instead,
        # only drop a lock if it's currently free (a non-blocking acquire
        # succeeds) — that means no in-flight get_redirect_url() call is
        # using it right now, so it's safe to discard; a future call for
        # that movie just creates a fresh one. A lock that's actually held
        # is left alone regardless of how long it's been in the dict.
        with self._redirect_locks_guard:
            for mid, lock in list(self._redirect_locks.items()):
                if lock.acquire(blocking=False):
                    lock.release()
                    self._redirect_locks.pop(mid, None)

        for eid, cached in list(self._recent_episode_redirects.items()):
            if cached[0] < cutoff:
                self._recent_episode_redirects.pop(eid, None)

        with self._episode_redirect_locks_guard:
            for eid, lock in list(self._episode_redirect_locks.items()):
                if lock.acquire(blocking=False):
                    lock.release()
                    self._episode_redirect_locks.pop(eid, None)

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

    def _reconcile_removed_episodes(self):
        """Mirrors _reconcile_removed_movies() for episodes/series. Dispatcharr
        can drop or renumber Episode/Series rows on an M3U/VOD catalog
        refresh (observed live: a series' id and all its episode ids changed
        entirely, then the whole series vanished from the catalog on a later
        refresh) — without this check a stale self._episodes_activated entry
        just points at nothing forever: dead STRM files on disk, a stale Plex
        entry, and "Series Activated: N" in the dashboard header for a title
        Activated Only search can never find.
        """
        if not self._episodes_activated:
            return

        try:
            from apps.vod.models import Episode
        except Exception:
            return

        activated_ids = [int(eid) for eid in self._episodes_activated.keys() if eid.isdigit()]
        if not activated_ids:
            return

        existing_ids = set(
            str(i) for i in Episode.objects.filter(id__in=activated_ids).values_list("id", flat=True)
        )
        missing = [eid for eid in self._episodes_activated.keys() if eid not in existing_ids]

        # Before treating a missing id as a real removal, check whether an
        # M3U/VOD catalog refresh simply reassigned it a new primary key --
        # observed live: 1899's 8 episodes kept their series name/season/
        # episode numbers but got entirely new Episode row ids (2157-2164 ->
        # 2165-2172) after a refresh, and this check used to nuke the STRM
        # files + Plex library entries for content that was never actually
        # removed by the provider. Match by (series name, season, episode
        # number) -- same approach _fetch_plex_episode_sizes() already uses
        # for matching against Plex's own metadata -- and migrate the
        # activation entry to the new id instead of deleting it.
        migrated = []
        removed = []
        if missing:
            candidates = Episode.objects.filter(
                season_number__in={self._episodes_activated[eid].get("season_number") for eid in missing},
            ).select_related("series").only(
                "id", "season_number", "episode_number", "series__name"
            )
            by_key = {}
            for ep in candidates:
                key = (self._clean_title(ep.series.name), ep.season_number, ep.episode_number)
                by_key.setdefault(key, ep.id)

            for eid in missing:
                entry = self._episodes_activated[eid]
                key = (
                    entry.get("series_name"),
                    entry.get("season_number"),
                    entry.get("episode_number"),
                )
                new_id = by_key.get(key)
                if new_id is not None and str(new_id) not in self._episodes_activated:
                    new_eid = str(new_id)
                    self._episodes_activated[new_eid] = entry
                    self._episodes_activated.pop(eid, None)
                    migrated.append((eid, new_eid))
                else:
                    removed.append(eid)

            if migrated:
                self._save_state()
                logger.info(
                    f"Reconciliation: {len(migrated)} activated episode(s) re-matched to "
                    f"new Dispatcharr ids after catalog refresh: {migrated}"
                )

        if not removed:
            self._maint_stats["last_removed_episode_check"] = {
                "ts": time.time(), "checked": len(activated_ids), "removed": 0,
            }
            self._save_state()
            self._log_event(
                "info",
                f"Cleanup check: {len(activated_ids)} activated episode(s) checked, none removed",
            )
            return

        removed_names = [
            f"{self._episodes_activated[eid].get('series_name', '?')} "
            f"S{self._episodes_activated[eid].get('season_number', '?')}"
            f"E{self._episodes_activated[eid].get('episode_number', '?')}"
            for eid in removed
        ]

        logger.warning(
            f"Reconciliation: {len(removed)} activated episode(s) no longer in "
            f"Dispatcharr's VOD catalog — removing: {removed}"
        )

        removal_info = {
            eid: (
                self._episodes_activated[eid].get("category_id"),
                self._episodes_activated[eid].get("strm_folder"),
                self._episodes_activated[eid].get("strm_stem"),
            )
            for eid in removed
        }
        plex_match_info = {eid: self._episodes_activated[eid] for eid in removed}

        self._remove_strm_for_episodes(removal_info)
        plex_removed = self._plex_delete_episodes(plex_match_info)

        for eid in removed:
            self._episodes_activated.pop(eid, None)

        self._maint_stats["removed_episode_total"] = (
            self._maint_stats.get("removed_episode_total", 0) + len(removed)
        )
        self._maint_stats["last_removed_episode_check"] = {
            "ts": time.time(), "checked": len(activated_ids), "removed": len(removed),
            "removed_names": removed_names, "plex_removed": plex_removed,
        }
        self._save_state()

        titles = ", ".join(f'"{n}"' for n in removed_names)
        self._log_event(
            "warn",
            f"Cleanup check: {len(activated_ids)} activated episode(s) checked, "
            f"{len(removed)} removed ({titles}) — no longer in Dispatcharr's catalog "
            f"({plex_removed} removed from Plex)",
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
                self._episodes_activated = state.get("episodes_activated", {})
                # Backfill mtime for episodes activated before this field
                # existed -- without it, list_series_vod_directory() falls
                # back to a fresh time.time() on every listing request,
                # which looks like a constantly-changing directory to Plex
                # and defeats the confirmed_size cache-stability fix.
                for _entry in self._episodes_activated.values():
                    if not _entry.get("mtime"):
                        _entry["mtime"] = _entry.get("activated_at") or time.time()
                self._maint_stats.update(state.get("maint_stats", {}))
                self._series_categories = state.get("series_categories", [])
                self._series_tmdb_state = state.get("series_tmdb_state", {})
                self._tmdb_detection_results = state.get("tmdb_detection_results", {})
                if "diagnostic_log" in state:
                    self._diagnostic_log.extend(state["diagnostic_log"])
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
                json.dump({
                "activated": self._activated,
                "episodes_activated": self._episodes_activated,
                "maint_stats": self._maint_stats,
                "series_categories": self._series_categories,
                "series_tmdb_state": self._series_tmdb_state,
                "tmdb_detection_results": self._tmdb_detection_results,
                "diagnostic_log": list(self._diagnostic_log)[-1000:],
            }, f)
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
        except Exception as e:
            if self.settings.get("debug_connections"):
                self._log_event("debug", f"Catalog count query failed: {e}")
            return 0

    def get_catalog_summary(self):
        try:
            from apps.vod.models import Movie
            import django
            total = Movie.objects.count()
            if self.settings.get("debug_connections"):
                try:
                    db_name = django.db.connection.settings_dict.get("NAME", "?")
                except Exception:
                    db_name = "?"
                self._log_event("debug", f"Catalog summary: Movie.objects.count()={total} (db={db_name})")
        except Exception as e:
            logger.error(f"Movie count error: {e}")
            if self.settings.get("debug_connections"):
                self._log_event("debug", f"Catalog summary count query failed: {e}")
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
            if self.settings.get("debug_connections"):
                self._log_event("debug", f"Catalog summary: {len(categories)} categor(y/ies) with movies")
        except Exception as e:
            logger.error(f"Category summary error: {e}")
            if self.settings.get("debug_connections"):
                self._log_event("debug", f"Category summary query failed: {e}")

        shows_activated = len({
            entry.get("series_id") for entry in self._episodes_activated.values()
            if entry.get("series_id")
        })

        return {
            "total": total,
            "activated": activated,
            "series_activated": len(self._episodes_activated),
            "shows_activated": shows_activated,
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
            if self.settings.get("debug_connections"):
                self._log_event(
                    "debug",
                    f"list_movies: total={total} page={page} per_page={per_page} "
                    f"filters(search={bool(search)}, director={bool(director)}, "
                    f"providers={provider_ids}, categories={category_ids}, "
                    f"languages={languages}, activated_only={bool(activated_only)})",
                )

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
            if self.settings.get("debug_connections"):
                self._log_event("debug", f"list_movies query failed: {e}")
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

    # --- Series Categories (dashboard-managed settings, separate from
    # plugin.json's single-value movie strm_output_dir/plex_library_section —
    # this is a variable-length, user-managed list stored in bridge_state.json) ---

    def list_series_categories(self):
        return {
            "categories": self._series_categories,
            "base_path": self._series_category_path(""),
        }

    def _series_category_path(self, folder_name):
        base = self.settings.get("strm_output_dir", "/data/plugin-strm")
        return os.path.join(base, "series", folder_name)

    def _clean_folder_name(self, raw):
        name = (raw or "").strip().strip("/\\")
        # Folder name only -- reject path separators/traversal so it can't
        # escape the series/ subtree under strm_output_dir.
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        return name

    # Sentinel used when the caller leaves Plex Library Section ID blank at
    # creation time (folder needs to exist before the Plex library can be
    # pointed at it, so the real ID isn't known yet). Deliberately not 0 --
    # some Plex setups could plausibly use a low section number, and 0 also
    # reads as "falsy"/unset in a way that's easy to overlook.
    PLEX_SECTION_UNSET = 999

    def create_series_category(self, body):
        name = (body.get("name") or "").strip()
        folder_name = self._clean_folder_name(body.get("strm_folder"))
        plex_library_section = body.get("plex_library_section")
        if not name:
            return {"status": "error", "error": "name is required"}
        if not folder_name:
            return {"status": "error", "error": "folder name is required (no slashes)"}
        if plex_library_section in (None, ""):
            plex_library_section = self.PLEX_SECTION_UNSET
        else:
            try:
                plex_library_section = int(plex_library_section)
            except (TypeError, ValueError):
                return {"status": "error", "error": "plex_library_section must be a number"}

        full_path = self._series_category_path(folder_name)
        try:
            os.makedirs(full_path, exist_ok=True)
        except OSError as e:
            return {"status": "error", "error": f"could not create folder: {e}"}

        next_id = (max((c["id"] for c in self._series_categories), default=0)) + 1
        entry = {
            "id": next_id,
            "name": name,
            "strm_folder": folder_name,
            "plex_library_section": plex_library_section,
        }
        self._series_categories.append(entry)
        self._save_state()
        return {"status": "ok", "category": entry}

    def update_series_category(self, category_id, body):
        entry = next((c for c in self._series_categories if c["id"] == category_id), None)
        if entry is None:
            return {"status": "error", "error": "category not found"}

        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                return {"status": "error", "error": "name is required"}
            entry["name"] = name
        if "strm_folder" in body:
            folder_name = self._clean_folder_name(body.get("strm_folder"))
            if not folder_name:
                return {"status": "error", "error": "folder name is required (no slashes)"}
            full_path = self._series_category_path(folder_name)
            try:
                os.makedirs(full_path, exist_ok=True)
            except OSError as e:
                return {"status": "error", "error": f"could not create folder: {e}"}
            entry["strm_folder"] = folder_name
        if "plex_library_section" in body:
            try:
                entry["plex_library_section"] = int(body.get("plex_library_section"))
            except (TypeError, ValueError):
                return {"status": "error", "error": "plex_library_section must be a number"}

        self._save_state()
        return {"status": "ok", "category": entry}

    def delete_series_category(self, category_id):
        before = len(self._series_categories)
        self._series_categories = [c for c in self._series_categories if c["id"] != category_id]
        if len(self._series_categories) == before:
            return {"status": "error", "error": "category not found"}
        self._save_state()
        return {"status": "ok"}

    # --- Series / Season / Episode browse ---

    def _activated_series_episode_counts(self):
        """series_id (str) -> count of currently-activated episodes, derived
        from self._episodes_activated (keyed by episode_id, each entry
        carrying series_id) -- there's no per-series activated flag, only
        per-episode, so callers needing series-level activated state
        (badges, Activated Only filter) aggregate through this."""
        counts = {}
        for entry in self._episodes_activated.values():
            sid = entry.get("series_id")
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
        return counts

    def list_series(self, query):
        try:
            from apps.vod.models import Series
            from django.db.models import F

            page = int(query.get("page", [1])[0])
            per_page = int(query.get("per_page", [50])[0])
            search = query.get("search", [""])[0]
            provider_ids = [v for v in query.get("provider_id", []) if v]
            category_ids = [v for v in query.get("category_id", []) if v]
            activated_only = query.get("activated_only", [""])[0]

            activated_counts = self._activated_series_episode_counts()

            # Series rows whose ONLY backing M3U relation(s) are to an inactive
            # account, or to a category the account has explicitly disabled,
            # are stale leftovers Dispatcharr keeps around after a
            # deactivate/disable -- they're invisible in the provider/category
            # filter dropdowns (both already filter on is_active/enabled) but
            # without this same filter here they still render as extra
            # duplicate cards on the unfiltered browse grid (e.g. a disabled
            # "NEWTON LINEUP" account contributing a 3rd copy of a title that
            # only has 2 real active-provider relations).
            qs = Series.objects.filter(
                m3u_relations__m3u_account__is_active=True,
                m3u_relations__category__m3u_relations__m3u_account=F("m3u_relations__m3u_account"),
                m3u_relations__category__m3u_relations__enabled=True,
            ).distinct()

            if search:
                qs = qs.filter(name__icontains=search)

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
                activated_ids = [int(sid) for sid in activated_counts.keys() if sid.isdigit()]
                if activated_ids:
                    qs = qs.filter(id__in=activated_ids)
                else:
                    qs = qs.none()

            qs = qs.order_by("name")
            total = qs.count()
            if self.settings.get("debug_connections"):
                self._log_event(
                    "debug",
                    f"list_series: total={total} page={page} per_page={per_page} "
                    f"filters(search={bool(search)}, providers={provider_ids}, categories={category_ids})",
                )

            offset = (page - 1) * per_page
            series_list = []
            for s in qs.select_related("logo")[offset : offset + per_page]:
                sid = str(s.id)
                poster = ""
                try:
                    if s.logo and s.logo.url:
                        poster = s.logo.url
                except Exception:
                    pass
                trailer_key = None
                try:
                    cp = getattr(s, "custom_properties", None) or {}
                    if isinstance(cp, str):
                        import json as _json
                        cp = _json.loads(cp)
                    trailer_key = cp.get("youtube_trailer") or cp.get("trailer") or None
                except Exception:
                    cp = {}

                activated_category = None
                if sid in activated_counts:
                    for entry in self._episodes_activated.values():
                        if str(entry.get("series_id")) == sid:
                            category_id = entry.get("category_id")
                            if category_id:
                                cat = self._resolve_series_category(category_id)
                                if cat:
                                    activated_category = cat.get("name")
                            break

                # The model's own episode_count is Dispatcharr's catalog-reported
                # figure and is frequently None/stale. Episodes are only actually
                # fetched into the DB lazily (see _ensure_episodes_fetched), so if
                # they're already present locally, use that real count instead --
                # without triggering a fetch here, since this runs per-page over
                # up to per_page series on every browse load.
                fetched_episode_count = s.episodes.count()
                episode_count = fetched_episode_count or getattr(s, "episode_count", None)
                sid_activated = activated_counts.get(sid, 0)
                fully_activated = bool(
                    fetched_episode_count and sid_activated >= fetched_episode_count
                )

                series_list.append(
                    {
                        "id": sid,
                        "name": s.name,
                        "year": getattr(s, "year", None),
                        "rating": getattr(s, "rating", None),
                        "genre": getattr(s, "genre", ""),
                        "tmdb_id": getattr(s, "tmdb_id", None),
                        "poster": poster,
                        "description": getattr(s, "description", ""),
                        "uuid": str(getattr(s, "uuid", "")),
                        "episode_count": episode_count,
                        "trailer_key": trailer_key,
                        "activated_episode_count": sid_activated,
                        "fully_activated": fully_activated,
                        "activated_category": activated_category,
                    }
                )

            return {
                "series": series_list,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page,
            }
        except Exception as e:
            logger.error(f"list_series error: {e}")
            if self.settings.get("debug_connections"):
                self._log_event("debug", f"list_series query failed: {e}")
            return {"series": [], "total": 0, "error": str(e)}

    def _ensure_episodes_fetched(self, series):
        """Mirror Dispatcharr's own on-demand episode fetch: episodes are not
        synced during the normal VOD scan, only lazily when a series is opened
        (Dispatcharr apps.vod.api_views series-info endpoint). Without this,
        series.episodes.all() stays empty for anything not yet opened in
        Dispatcharr's own UI."""
        try:
            relation = (
                series.m3u_relations.filter(m3u_account__is_active=True)
                .select_related("m3u_account")
                .order_by("-m3u_account__priority", "id")
                .first()
            )
            if not relation or not relation.m3u_account or not relation.m3u_account.is_active:
                return
            custom_props = relation.custom_properties or {}
            if custom_props.get("episodes_fetched"):
                return
            from apps.vod.tasks import refresh_series_episodes

            refresh_series_episodes(relation.m3u_account, series, relation.external_series_id)
            series.refresh_from_db()
        except Exception as e:
            logger.error(f"_ensure_episodes_fetched error for series {series.id}: {e}")
            if self.settings.get("debug_connections"):
                self._log_event("debug", f"_ensure_episodes_fetched failed for series {series.id}: {e}")

    def list_seasons(self, series_id):
        try:
            from apps.vod.models import Series

            series = Series.objects.get(id=int(series_id))
            self._ensure_episodes_fetched(series)
            seasons = {}
            for ep in series.episodes.all():
                seasons.setdefault(ep.season_number, 0)
                seasons[ep.season_number] += 1

            activated_counts = {}
            sid = str(series_id)
            for entry in self._episodes_activated.values():
                if entry.get("series_id") == sid:
                    sn = entry.get("season_number")
                    activated_counts[sn] = activated_counts.get(sn, 0) + 1

            return {
                "series_id": str(series_id),
                "series_name": series.name,
                "seasons": [
                    {
                        "season_number": num,
                        "episode_count": count,
                        "activated_episode_count": activated_counts.get(num, 0),
                        "fully_activated": activated_counts.get(num, 0) >= count,
                    }
                    for num, count in sorted(seasons.items())
                ],
            }
        except Exception as e:
            logger.error(f"list_seasons error: {e}")
            return {"seasons": [], "error": str(e)}

    def list_episodes(self, series_id, season_number):
        try:
            from apps.vod.models import Series

            series = Series.objects.get(id=int(series_id))
            self._ensure_episodes_fetched(series)
            qs = series.episodes.all()
            if season_number is not None:
                qs = qs.filter(season_number=int(season_number))
            qs = qs.order_by("season_number", "episode_number")

            episodes = []
            for ep in qs:
                eid = str(ep.id)
                relations = list(ep.m3u_relations.all())
                episodes.append(
                    {
                        "id": eid,
                        "name": ep.name,
                        "description": getattr(ep, "description", ""),
                        "air_date": str(getattr(ep, "air_date", "") or ""),
                        "rating": getattr(ep, "rating", None),
                        "duration_secs": getattr(ep, "duration_secs", None),
                        "season_number": ep.season_number,
                        "episode_number": ep.episode_number,
                        "tmdb_id": getattr(ep, "tmdb_id", None),
                        "provider_count": len(relations),
                        "activated": eid in self._episodes_activated,
                    }
                )

            return {
                "series_id": str(series_id),
                "series_name": series.name,
                "season_number": int(season_number) if season_number is not None else None,
                "episodes": episodes,
                "total": len(episodes),
            }
        except Exception as e:
            logger.error(f"list_episodes error: {e}")
            return {"episodes": [], "total": 0, "error": str(e)}

    # --- Series / Episode activation ---
    #
    # Mirrors activate_movies/deactivate_movies, but activation is always
    # scoped to a single destination Series Settings category (strm_folder +
    # plex_library_section) chosen by the caller for the whole batch --
    # series/episodes don't carry their own Plex-library mapping the way
    # movies do via the single global setting. No per-episode audio probe
    # (unlike movies) -- at series/season scale that would mean hundreds of
    # ffprobe calls per activate-all, holding a real provider connection
    # each time; capacity gating via _account_has_capacity is the safety
    # valve here instead, same mechanism movies use before probing.

    EPISODE_STRM_BATCH_SIZE = 50
    EPISODE_STRM_BATCH_DELAY_SECS = 5

    def _resolve_series_category(self, category_id):
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return None
        return next((c for c in self._series_categories if c["id"] == category_id), None)

    def _build_episode_proxy_url(self, episode, relation, settings=None):
        s = settings if settings is not None else self.settings
        dispatcharr_url = s.get("dispatcharr_url", "").rstrip("/")
        if not dispatcharr_url:
            return None
        return f"{dispatcharr_url}/proxy/vod/episode/{episode.uuid}?stream_id={relation.stream_id}"

    def activate_episodes(self, body):
        """Enqueue episode activation as a background job and return
        immediately. episode_ids are split into EPISODE_STRM_BATCH_SIZE
        chunks; a single dedicated worker thread (_episode_job_worker_loop)
        drains one batch at a time, across all queued jobs, so a 300-episode
        activation can never pile concurrent Plex scans/DB load on top of
        anything else -- including another activation click landing while
        this one is still running, which just appends to the same queue
        instead of starting a second worker (see bead vpv: 30+ series
        activations failing silently under Plex scan / provider-connection
        saturation).

        Returns immediately with {"status": "queued", "job_id", "total"} --
        poll get_episode_job_status(job_id) or list_episode_jobs() for
        progress. The actual per-episode activation logic (DB lookup, stream
        relation lookup, provider pick, activation record) lives in
        _activate_episode_batch(), unchanged from the old synchronous
        implementation aside from running one batch at a time instead of
        the whole list in one HTTP request.
        """
        episode_ids = body.get("episode_ids", [])
        category_id = body.get("category_id")
        if not episode_ids:
            return {"status": "error", "message": "No episode_ids provided"}
        if category_id is None:
            return {"status": "error", "message": "category_id is required — choose a destination Series Settings category"}

        category = self._resolve_series_category(category_id)
        if category is None:
            return {"status": "error", "message": "Destination category not found"}

        # Skip anything already activated up front so job totals/progress
        # reflect actual work, matching the old skip-if-activated behavior.
        pending_ids = [str(eid) for eid in episode_ids if str(eid) not in self._episodes_activated]
        if not pending_ids:
            return {"status": "ok", "activated": 0, "strm_generated": 0, "names": [], "failed": [], "failed_names": []}

        batches = [
            pending_ids[i:i + self.EPISODE_STRM_BATCH_SIZE]
            for i in range(0, len(pending_ids), self.EPISODE_STRM_BATCH_SIZE)
        ]

        with self._episode_job_lock:
            self._episode_job_counter += 1
            job_id = str(self._episode_job_counter)
            self._episode_jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "category_id": category["id"],
                "category_name": category["name"],
                "total": len(pending_ids),
                "done": 0,
                "batches_total": len(batches),
                "batches_done": 0,
                "batches": batches,
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "activated": [],
                "activated_names": [],
                "failed": [],
                "failed_names": [],
                "strm_generated": 0,
            }
            queue_position = len(self._episode_job_queue)
            self._episode_job_queue.append(job_id)

        self._log_diagnostic("info",
            f"Episode activation job {job_id} queued: {len(pending_ids)} episodes in {len(batches)} batch(es), "
            f"position {queue_position} in queue")
        self._episode_job_wake.set()

        return {
            "status": "queued",
            "job_id": job_id,
            "total": len(pending_ids),
            "batches": len(batches),
            "queue_position": queue_position,
        }

    def get_episode_job_status(self, job_id):
        with self._episode_job_lock:
            job = self._episode_jobs.get(str(job_id))
            if job is None:
                return {"status": "error", "message": "Job not found"}
            return dict(job, batches=None)  # omit raw batch id lists from the response payload

    def list_episode_jobs(self):
        """Returns active (queued/running) jobs, most-recent-first, for the
        dashboard's queue-depth indicator. Finished jobs are pruned by the
        worker loop after a short grace period (see _episode_job_worker_loop)
        rather than kept forever."""
        with self._episode_job_lock:
            jobs = [dict(j, batches=None) for j in self._episode_jobs.values()]
        jobs.sort(key=lambda j: j["created_at"], reverse=True)
        return {"jobs": jobs}

    def _episode_job_worker_loop(self):
        """Single consumer for _episode_job_queue: pulls one batch at a time
        (from the oldest still-incomplete job) and processes it via
        _activate_episode_batch(), so exactly one batch's worth of DB/Plex/
        provider load is ever in flight regardless of how many activation
        jobs are queued behind it."""
        while not self._episode_job_stop.is_set():
            job_id = None
            with self._episode_job_lock:
                if self._episode_job_queue:
                    job_id = self._episode_job_queue[0]

            if job_id is None:
                self._episode_job_wake.wait(timeout=10)
                self._episode_job_wake.clear()
                continue

            with self._episode_job_lock:
                job = self._episode_jobs.get(job_id)
                if job is None or not job["batches"]:
                    self._episode_job_queue.popleft()
                    continue
                if job["status"] == "queued":
                    job["status"] = "running"
                    job["started_at"] = time.time()
                batch = job["batches"].pop(0)
                category = self._resolve_series_category(job["category_id"])

            if category is None:
                self._log_diagnostic("error", f"Episode activation job {job_id}: destination category no longer exists, aborting job")
                with self._episode_job_lock:
                    job["status"] = "error"
                    job["finished_at"] = time.time()
                    self._episode_job_queue.popleft()
                continue

            try:
                self._activate_episode_batch(batch, category, job)
            except Exception as e:
                logger.error(f"Episode activation job {job_id} batch failed: {e}")
                self._log_diagnostic("error", f"Episode activation job {job_id}: batch failed: {type(e).__name__}: {str(e)[:200]}")

            with self._episode_job_lock:
                job["batches_done"] += 1
                job["done"] = len(job["activated"]) + len(job["failed"])
                if not job["batches"]:
                    job["status"] = "plex_scanning"
                    job["plex_confirmed"] = 0
                    self._episode_job_queue.popleft()
                    titles = ", ".join(f'"{n}"' for n in job["activated_names"])
                    self._log_event(
                        "info",
                        f'Activated {len(job["activated"])} episode(s) into "{job["category_name"]}": {titles} '
                        f'- generated {job["strm_generated"]} STRM file(s)',
                    )
                    if job["failed"]:
                        failed_titles = ", ".join(f'"{n}"' for n in job["failed_names"])
                        self._log_event("warn", f'Episode activation skipped {len(job["failed"])} episode(s): {failed_titles}')
                    self._log_diagnostic("info",
                        f'Episode activation job {job_id} complete: {len(job["activated"])} activated, {len(job["failed"])} failed - awaiting Plex confirmation')
                    run_plex_wait = True
                else:
                    run_plex_wait = False
                    # More batches remain for this job -- pace the next one
                    # so Plex isn't hit with another scan/HEAD wave back to
                    # back with the one this batch just triggered.
                    time.sleep(self.EPISODE_STRM_BATCH_DELAY_SECS)

            if run_plex_wait:
                self._wait_for_plex_episode_confirmation(job_id, job)

            # Old, finished jobs are pruned lazily here so the dashboard can
            # still show "just completed" status for a little while without
            # _episode_jobs growing unbounded over a long-running server.
            self._prune_finished_episode_jobs()

    PLEX_CONFIRM_POLL_SECS = 3
    PLEX_CONFIRM_TIMEOUT_SECS = 900

    def _wait_for_plex_episode_confirmation(self, job_id, job):
        """After our own batches finish, Plex is still analyzing the files
        in the background (HEAD/probe + library scan). Poll Plex's own
        library JSON via _fetch_plex_episode_sizes() every few seconds so
        the dashboard can show a live completed/pending count for this
        phase too, instead of the pill disappearing while Plex is still
        working -- this was the exact confusion the user hit live-testing
        a 47-episode activation (pill gone, but Plex still climbing
        11->14->17 episodes for several more minutes)."""
        eids = list(job["activated"])
        if not eids:
            with self._episode_job_lock:
                job["status"] = "done"
                job["finished_at"] = time.time()
            return

        deadline = time.time() + self.PLEX_CONFIRM_TIMEOUT_SECS
        pending = set(eids)
        while pending and time.time() < deadline and not self._episode_job_stop.is_set():
            try:
                sizes = self._fetch_plex_episode_sizes(list(pending))
            except Exception as e:
                logger.error(f"Episode activation job {job_id}: Plex confirmation poll failed: {e}")
                sizes = {}
            pending -= set(sizes.keys())
            with self._episode_job_lock:
                job["plex_confirmed"] = len(eids) - len(pending)
            if not pending:
                break
            time.sleep(self.PLEX_CONFIRM_POLL_SECS)

        with self._episode_job_lock:
            job["plex_confirmed"] = len(eids) - len(pending)
            job["status"] = "done"
            job["finished_at"] = time.time()
        if pending:
            self._log_diagnostic("warn",
                f'Episode activation job {job_id}: Plex confirmation timed out after {self.PLEX_CONFIRM_TIMEOUT_SECS}s '
                f'with {len(pending)}/{len(eids)} episode(s) still unconfirmed by Plex')
        else:
            self._log_diagnostic("info", f'Episode activation job {job_id}: Plex confirmed all {len(eids)} episode(s)')

    EPISODE_JOB_RETENTION_SECS = 600

    def _prune_finished_episode_jobs(self):
        cutoff = time.time() - self.EPISODE_JOB_RETENTION_SECS
        with self._episode_job_lock:
            stale = [
                jid for jid, j in self._episode_jobs.items()
                if j["status"] in ("done", "error") and j.get("finished_at") and j["finished_at"] < cutoff
            ]
            for jid in stale:
                del self._episode_jobs[jid]

    def _activate_episode_batch(self, episode_ids, category, job):
        """Activates one batch of episodes and, if any succeeded, generates
        their STRM/NFO, fetches confirmed sizes, and triggers a Plex scan
        for just this batch -- same per-episode logic as the old
        activate_episodes() (DB lookup w/ transient retry, stream relation
        lookup, least-loaded provider pick, activation record), but scoped
        to one batch so the Plex scan/size-fetch happens per-batch instead
        of once for the whole activation. Mutates job's activated/failed
        lists and strm_generated count in place."""
        try:
            from apps.vod.models import Episode
        except Exception as e:
            job["failed"].extend({"id": eid, "name": f"#{eid}", "message": str(e)} for eid in episode_ids)
            job["failed_names"].extend(f"#{eid}" for eid in episode_ids)
            return

        activated = []
        activated_names = []
        failed = []
        failed_names = []

        queue = deque(episode_ids)
        retry_counts = {}
        max_retries = 3

        while queue:
            eid = queue.popleft()
            eid = str(eid)

            if eid in self._episodes_activated:
                continue

            # Step 1: Fetch episode from DB
            try:
                episode = Episode.objects.select_related("series").get(id=int(eid))
            except Episode.DoesNotExist:
                self._log_diagnostic("error", f"Episode {eid}: not found in database")
                failed.append({"id": eid, "name": f"#{eid}", "message": "Episode not found"})
                failed_names.append(f"#{eid}")
                continue
            except Exception as e:
                retry_count = retry_counts.get(eid, 0)
                if retry_count < max_retries:
                    queue.append(eid)
                    retry_counts[eid] = retry_count + 1
                    self._log_diagnostic("warn",
                        f"Episode {eid}: DB lookup transient error (attempt {retry_count + 1}/{max_retries}): {type(e).__name__}: {str(e)[:100]}")
                    continue
                else:
                    self._log_diagnostic("error",
                        f"Episode {eid}: DB lookup failed after {max_retries} retries: {type(e).__name__}")
                    failed.append({"id": eid, "name": f"#{eid}", "message": "Episode lookup failed after retries"})
                    failed_names.append(f"#{eid}")
                    continue

            # Step 2: Get stream relations
            try:
                relations = list(episode.m3u_relations.all())
                if not relations:
                    self._log_diagnostic("warn",
                        f"Episode {eid} ({episode.series.name} S{episode.season_number}E{episode.episode_number}): no stream relations")
                    failed.append({"id": eid, "name": episode.name, "message": "No stream mapping for episode"})
                    failed_names.append(episode.name)
                    continue
            except Exception as e:
                retry_count = retry_counts.get(eid, 0)
                if retry_count < max_retries:
                    queue.append(eid)
                    retry_counts[eid] = retry_count + 1
                    self._log_diagnostic("warn",
                        f"Episode {eid}: stream relations lookup error (attempt {retry_count + 1}/{max_retries}): {type(e).__name__}")
                    continue
                else:
                    self._log_diagnostic("error",
                        f"Episode {eid}: stream relations lookup failed after {max_retries} retries")
                    failed.append({"id": eid, "name": episode.name, "message": "Stream relations lookup failed"})
                    failed_names.append(episode.name)
                    continue

            # Step 3: Pick least-loaded provider (no capacity check)
            try:
                best_relation = min(relations,
                    key=lambda r: self._get_provider_current_stream_count(r.m3u_account_id))
                provider_name = self._account_name(best_relation.m3u_account_id)
                current_streams = self._get_provider_current_stream_count(best_relation.m3u_account_id)
            except Exception as e:
                self._log_diagnostic("error",
                    f"Episode {eid}: failed to pick best provider: {type(e).__name__}")
                failed.append({"id": eid, "name": episode.name, "message": "Provider selection failed"})
                failed_names.append(episode.name)
                continue

            # Step 4: Record activation entry
            try:
                now = time.time()
                estimated_size = self._resolve_estimated_episode_size(episode, relations)
                self._episodes_activated[eid] = {
                    "activated_at": now,
                    "mtime": now,
                    "stream_pick": best_relation.stream_id,
                    "category_id": category["id"],
                    "series_id": str(episode.series_id),
                    "series_name": self._clean_title(episode.series.name),
                    "season_number": episode.season_number,
                    "episode_number": episode.episode_number,
                    "estimated_size": estimated_size,
                }
                activated.append(eid)
                activated_names.append(f"{episode.series.name} S{episode.season_number:02d}E{episode.episode_number:02d}")
                self._log_diagnostic("info",
                    f"Episode {eid}: activated (provider={provider_name}, current_streams={current_streams})")
            except Exception as e:
                retry_count = retry_counts.get(eid, 0)
                if retry_count < max_retries:
                    queue.append(eid)
                    retry_counts[eid] = retry_count + 1
                    self._log_diagnostic("warn",
                        f"Episode {eid}: activation transient error (attempt {retry_count + 1}/{max_retries}): {type(e).__name__}: {str(e)[:100]}")
                    continue
                else:
                    self._log_diagnostic("error",
                        f"Episode {eid}: activation failed after {max_retries} retries: {type(e).__name__}")
                    failed.append({"id": eid, "name": episode.name, "message": "Activation failed after retries"})
                    failed_names.append(episode.name)
                    continue

        self._save_state()

        strm_count = 0
        if activated:
            strm_count = self._generate_strm_for_episodes(activated, category)
            self._save_state()
            # Fetch confirmed sizes immediately before triggering the scan
            # for this batch, so Plex's probes during that scan already see
            # cached sizes (Option A, same rationale as the old single-shot
            # version, just scoped to this batch instead of the whole job).
            self._log_diagnostic("info", f"Episode activation: fetching {len(activated)} confirmed sizes from Plex before scan trigger...")
            sizes = self._fetch_plex_episode_sizes(activated)
            for eid in activated:
                result = sizes.get(eid)
                if result:
                    size, updated_at = result
                    entry = self._episodes_activated.get(eid)
                    if entry and entry.get("confirmed_size") != size:
                        entry["confirmed_size"] = size
                        entry["plex_updated_at"] = updated_at
                        entry["mtime"] = time.time()
                        self._log_diagnostic("debug", f"Episode {eid}: pre-scan size confirmed {size} bytes")
            if sizes:
                self._save_state()
                self._log_diagnostic("info", f"Episode activation: {len(sizes)}/{len(activated)} sizes confirmed before scan trigger")
            self._trigger_plex_scan(section=category["plex_library_section"])
            for eid in activated:
                entry = self._episodes_activated.get(eid)
                if not entry or not entry.get("confirmed_size"):
                    threading.Thread(
                        target=self._size_reconcile_fast_path_episode,
                        args=(eid,),
                        daemon=True,
                    ).start()
            placeholder_series_ids = {
                self._episodes_activated[eid]["series_id"] for eid in activated
                if self._series_tmdb_state.get(self._episodes_activated[eid]["series_id"], {}).get("is_placeholder")
            }
            for sid in placeholder_series_ids:
                threading.Thread(
                    target=self._tmdb_reconcile_fast_path_series,
                    args=(sid,),
                    daemon=True,
                ).start()

        job["activated"].extend(activated)
        job["activated_names"].extend(activated_names)
        job["failed"].extend(failed)
        job["failed_names"].extend(failed_names)
        job["strm_generated"] += strm_count

    def reactivate_episodes(self, body):
        """Manual 'Reactivate' for already-activated episodes, mirroring
        reactivate_movies(): clears the cached stream_pick and rewrites
        NFO/tvshow.nfo in place, but does NOT touch Plex (no delete, no
        scan) — Plex picks up the on-disk change on its own next scan.

        Exists because activate_episodes() silently skips any eid already
        in self._episodes_activated (`if eid in self._episodes_activated:
        continue`), so re-clicking "Activate" on an already-activated series
        is a complete no-op: no STRM/NFO regen, no tvshow.nfo backfill for
        series activated before that fix existed, and critically no Plex
        scan trigger — which looked like "activating does nothing" and
        "no call to Plex is happening" from the dashboard, when actually
        activate was just refusing to do anything a second time."""
        episode_ids = [str(eid) for eid in body.get("episode_ids", [])]
        targets = [eid for eid in episode_ids if eid in self._episodes_activated]
        if not targets:
            return {"status": "ok", "reactivated": 0, "names": []}

        try:
            from apps.vod.models import Episode
        except Exception as e:
            return {"status": "error", "message": str(e)}

        for eid in targets:
            entry = self._episodes_activated[eid]
            try:
                episode = Episode.objects.select_related("series").get(id=int(eid))
            except Episode.DoesNotExist:
                continue
            relations = list(episode.m3u_relations.all())
            if not relations:
                continue
            preferred = relations[0]
            cached_stream_id = entry.get("stream_pick")
            if cached_stream_id is not None:
                for r in relations:
                    if str(r.stream_id) == str(cached_stream_id):
                        preferred = r
                        break
            relation = self._pick_relation_with_capacity(relations, preferred)
            entry["stream_pick"] = relation.stream_id
            if not entry.get("estimated_size"):
                entry["estimated_size"] = self._resolve_estimated_episode_size(episode, relations)

        self._save_state()

        by_category = {}
        for eid in targets:
            cat_id = self._episodes_activated[eid].get("category_id")
            by_category.setdefault(cat_id, []).append(eid)

        strm_count = 0
        for cat_id, eids in by_category.items():
            category = self._resolve_series_category(cat_id)
            if category is None:
                continue
            strm_count += self._generate_strm_for_episodes(eids, category)
        self._save_state()

        reactivated = len(targets)
        names = [
            f'{self._episodes_activated[eid].get("series_name", "?")} '
            f'S{self._episodes_activated[eid].get("season_number", "?")}'
            f'E{self._episodes_activated[eid].get("episode_number", "?")}'
            for eid in targets
        ]
        self._maint_stats["reactivated_episode_total"] = (
            self._maint_stats.get("reactivated_episode_total", 0) + reactivated
        )
        self._maint_stats["last_reactivate_episode"] = {
            "ts": time.time(), "reactivated": reactivated, "names": names,
        }
        self._save_state()

        titles = ", ".join(f'"{n}"' for n in names)
        self._log_event(
            "info",
            f"Reactivated {reactivated} episode(s): {titles} — NFO refreshed, Plex untouched",
        )
        return {"status": "ok", "reactivated": reactivated, "names": names}

    def _generate_strm_for_episodes(self, episode_ids, category):
        """Creates the real Series/Season directories (required so Plex's TV
        agent can identify series/season from the folder structure) and a
        real .nfo per episode, but no .strm file -- the episode's .mkv entry
        is synthesized on the fly by list_series_vod_directory() from
        self._episodes_activated, same as movies' list_vod_directory().
        A real .strm file previously served here made Plex's "get a decision"
        step fail for playback even after the folder scanned/matched fine;
        switching to a synthetic .mkv + 302 redirect mirrors the movies path,
        which has no such issue (bead za8).

        Batched in chunks of EPISODE_STRM_BATCH_SIZE with a short delay
        between chunks, so a large series/season activate-all doesn't demand
        a Plex scan all at once -- the batching half of the safety valve
        alongside the capacity gate above and the single-scan Plex lock."""
        base_dir = self._series_category_path(category["strm_folder"])
        os.makedirs(base_dir, exist_ok=True)

        from apps.vod.models import Episode

        count = 0
        nfo_written_for = set()
        for batch_start in range(0, len(episode_ids), self.EPISODE_STRM_BATCH_SIZE):
            batch = episode_ids[batch_start : batch_start + self.EPISODE_STRM_BATCH_SIZE]
            for eid in batch:
                try:
                    episode = Episode.objects.select_related("series").get(id=int(eid))
                except Episode.DoesNotExist:
                    continue

                try:
                    series_name = self._clean_title(episode.series.name)
                    year = getattr(episode.series, "year", None)
                    series_folder_name = f"{series_name} ({year})" if year else series_name
                    series_dir = os.path.join(base_dir, series_folder_name)
                    season_folder_name = f"Season {episode.season_number:02d}"
                    folder = os.path.join(series_dir, season_folder_name)
                    os.makedirs(folder, exist_ok=True)

                    if series_folder_name not in nfo_written_for:
                        self._write_tvshow_nfo(
                            episode.series, series_dir, clean_title=series_name,
                            category_id=category["id"],
                        )
                        nfo_written_for.add(series_folder_name)

                    ep_label = f"S{episode.season_number:02d}E{episode.episode_number:02d}"
                    ep_title = self._clean_title(episode.name) if episode.name else ""
                    ep_title = self._strip_episode_name_prefix(ep_title, series_name, ep_label)
                    file_stem = f"{series_folder_name} - {ep_label}"
                    if ep_title:
                        file_stem += f" - {ep_title}"

                    self._write_episode_nfo(episode, folder, file_stem, clean_title=ep_title)

                    if eid in self._episodes_activated:
                        self._episodes_activated[eid]["strm_folder"] = os.path.join(
                            category["strm_folder"], series_folder_name, season_folder_name
                        )
                        self._episodes_activated[eid]["strm_stem"] = file_stem
                    count += 1
                    logger.info(f"Episode folder/NFO generated: {file_stem}")
                except Exception as e:
                    logger.error(f"Episode STRM generation failed for {eid}: {e}")
                    self._log_event("error", f"Episode STRM generation failed for id={eid}: {e}")

            remaining = episode_ids[batch_start + self.EPISODE_STRM_BATCH_SIZE :]
            if remaining:
                time.sleep(self.EPISODE_STRM_BATCH_DELAY_SECS)

        return count

    def _write_episode_nfo(self, episode, folder, file_stem, clean_title=None):
        nfo_path = os.path.join(folder, f"{file_stem}.nfo")
        title = clean_title if clean_title is not None else (episode.name or "")
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<episodedetails>",
            f"  <title>{self._xml_escape(title)}</title>",
            f"  <season>{episode.season_number}</season>",
            f"  <episode>{episode.episode_number}</episode>",
        ]
        desc = getattr(episode, "description", "")
        if desc:
            lines.append(f"  <plot>{self._xml_escape(desc)}</plot>")
        air_date = getattr(episode, "air_date", None)
        if air_date:
            lines.append(f"  <aired>{air_date}</aired>")
        rating = getattr(episode, "rating", None)
        if rating:
            lines.append(f"  <rating>{rating}</rating>")
        tmdb_id = getattr(episode, "tmdb_id", None)
        if tmdb_id:
            lines.append(f"  <uniqueid type=\"tmdb\" default=\"true\">{tmdb_id}</uniqueid>")
        lines.append("</episodedetails>")

        try:
            with open(nfo_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            logger.error(f"Episode NFO write failed for {file_stem}: {e}")

    def _find_sibling_series_tmdb_id(self, series):
        """Dispatcharr's VOD catalog can hold duplicate Series rows for the
        same real-world show (observed live: '13 Reasons Why' had 3 rows,
        only one with tmdb_id populated, and the row that got activated was
        an unrelated no-metadata duplicate). Cheap fix before falling back to
        a placeholder: look for another row with the exact same name that
        already has a tmdb_id and borrow it."""
        name = getattr(series, "name", None)
        if not name:
            return None
        try:
            from apps.vod.models import Series as SeriesModel
        except Exception:
            return None
        try:
            sibling = (
                SeriesModel.objects
                .filter(name=name)
                .exclude(id=series.id)
                .exclude(tmdb_id__isnull=True)
                .exclude(tmdb_id="")
                .first()
            )
        except Exception as e:
            logger.error(f"Sibling series tmdb lookup failed for '{name}': {e}")
            return None
        return getattr(sibling, "tmdb_id", None) if sibling else None

    def _placeholder_series_tmdb_id(self, series):
        """Deterministic stand-in uniqueid for a series with no real tmdb_id
        anywhere in Dispatcharr's catalog (own row or a sibling row). Not a
        real TMDB id -- just enough of a distinct identity, keyed off
        name+year, to stop Plex's TV agent from merging two anchor-less shows
        that land in the same library section. Replaced with a real id once
        the background reconcile sweep finds one (see
        _reconcile_series_tmdb_id / _reconcile_all_series_tmdb_ids)."""
        name = getattr(series, "name", "") or ""
        year = getattr(series, "year", "") or ""
        digest = hashlib.sha1(f"{name}|{year}".encode("utf-8")).hexdigest()[:12]
        return digest

    def _write_tvshow_nfo(self, series, series_dir, clean_title=None, force=False, category_id=None):
        """Series-level identity anchor for Plex's TV agent. Without a
        tvshow.nfo at the show's root folder, Plex has only the folder name
        and thin per-episode metadata to identify each show in a shared
        library section -- observed live: activating a second, unrelated
        show into the same "Comedy" Plex section caused Plex to attribute
        its episodes to the first show already in that library instead of
        creating its own show entry, because nothing pinned each folder to
        a distinct show identity. Written once per series folder (skipped
        if already present, unless force=True for the tmdb-id repair path
        in _reconcile_series_tmdb_id) alongside the per-episode NFOs.

        When series.tmdb_id is missing, tries a sibling Series row with the
        same name first (_find_sibling_series_tmdb_id), then falls back to a
        placeholder id (_placeholder_series_tmdb_id) rather than omitting
        <uniqueid> entirely -- see bead czo."""
        nfo_path = os.path.join(series_dir, "tvshow.nfo")
        if os.path.exists(nfo_path) and not force:
            return
        title = clean_title if clean_title is not None else (getattr(series, "name", "") or "")
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<tvshow>",
            f"  <title>{self._xml_escape(title)}</title>",
        ]
        year = getattr(series, "year", None)
        if year:
            lines.append(f"  <year>{year}</year>")
        desc = getattr(series, "description", "")
        if desc:
            lines.append(f"  <plot>{self._xml_escape(desc)}</plot>")
        genre = getattr(series, "genre", "")
        if genre:
            for g in str(genre).split(","):
                g = g.strip()
                if g:
                    lines.append(f"  <genre>{self._xml_escape(g)}</genre>")
        rating = getattr(series, "rating", None)
        if rating:
            lines.append(f"  <rating>{rating}</rating>")
        tmdb_id = getattr(series, "tmdb_id", None)
        is_placeholder = False
        if not tmdb_id:
            tmdb_id = self._find_sibling_series_tmdb_id(series)
        if not tmdb_id:
            tmdb_id = self._placeholder_series_tmdb_id(series)
            is_placeholder = True
        uniqueid_type = "vodbridge" if is_placeholder else "tmdb"
        lines.append(f"  <uniqueid type=\"{uniqueid_type}\" default=\"true\">{tmdb_id}</uniqueid>")
        if not is_placeholder:
            lines.append(f"  <tmdbid>{tmdb_id}</tmdbid>")
        lines.append("</tvshow>")

        try:
            with open(nfo_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(f"tvshow.nfo written for {title} (tmdb_id={tmdb_id}, placeholder={is_placeholder})")
        except Exception as e:
            logger.error(f"tvshow.nfo write failed for {title}: {e}")
            return

        sid = str(series.id)
        prior = self._series_tmdb_state.get(sid, {})
        self._series_tmdb_state[sid] = {
            "tmdb_id": tmdb_id,
            "is_placeholder": is_placeholder,
            "series_dir": series_dir,
            "series_name": getattr(series, "name", "") or "",
            "series_clean_title": title,
            "category_id": category_id if category_id is not None else prior.get("category_id"),
        }

    def deactivate_episodes(self, body):
        episode_ids = body.get("episode_ids", [])
        deactivated = []
        # (category_id, folder_rel, file_stem) captured before delete since
        # removal needs the category's base strm_folder to build the path.
        removal_info = {}
        plex_match_info = {}
        for eid in episode_ids:
            eid = str(eid)
            if eid in self._episodes_activated:
                entry = self._episodes_activated[eid]
                removal_info[eid] = (
                    entry.get("category_id"),
                    entry.get("strm_folder"),
                    entry.get("strm_stem"),
                )
                plex_match_info[eid] = entry
                del self._episodes_activated[eid]
                deactivated.append(eid)

        self._save_state()

        plex_removed = 0
        if deactivated:
            self._remove_strm_for_episodes(removal_info)
            plex_removed = self._plex_delete_episodes(plex_match_info)
            self._log_event(
                "info",
                f"Deactivated {len(deactivated)} episode(s) — removed {plex_removed} from Plex",
            )

        return {"status": "ok", "deactivated": len(deactivated), "plex_removed": plex_removed}

    def _plex_delete_episodes(self, plex_match_info):
        """Mirrors _plex_delete_movies() for episodes. Episode STRM filenames
        don't embed the episode id (unlike movies' {id}.mkv), so matching is
        by series name + season/episode number against Plex's own episode
        metadata (grandparentTitle/parentIndex/index) instead of filename
        regex. Grouped per plex_library_section since each Series Settings
        category can point at a different Plex TV library."""
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        if not plex_url or not plex_token:
            return 0

        by_section = {}
        for eid, entry in plex_match_info.items():
            category = self._resolve_series_category(entry.get("category_id"))
            section = category["plex_library_section"] if category else None
            if section in (None, "", self.PLEX_SECTION_UNSET):
                continue
            by_section.setdefault(section, []).append(entry)

        removed = 0
        for section, entries in by_section.items():
            try:
                resp = requests.get(
                    f"{plex_url}/library/sections/{section}/all",
                    params={"X-Plex-Token": plex_token, "type": "2"},
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning(f"Plex library query failed for section {section}: {resp.status_code}")
                    continue

                items = resp.json().get("MediaContainer", {}).get("Metadata", [])
                wanted = {
                    (e.get("series_name", ""), str(e.get("season_number", "")), str(e.get("episode_number", "")))
                    for e in entries
                }
                logger.info(f"Plex episode delete: wanted={wanted}")
                logger.info(
                    "Plex episode delete: available="
                    + str([(it.get("grandparentTitle", ""), it.get("parentIndex"), it.get("index")) for it in items])
                )

                for item in items:
                    key = (
                        item.get("grandparentTitle", ""),
                        str(item.get("parentIndex", "")),
                        str(item.get("index", "")),
                    )
                    if key not in wanted:
                        continue
                    rating_key = item.get("ratingKey")
                    title = item.get("title", "?")
                    del_resp = requests.delete(
                        f"{plex_url}/library/metadata/{rating_key}",
                        params={"X-Plex-Token": plex_token},
                        timeout=10,
                    )
                    if del_resp.status_code in (200, 204):
                        removed += 1
                        logger.info(f"Plex: deleted episode {title} (key {rating_key})")
                    else:
                        logger.warning(f"Plex delete episode {title} returned {del_resp.status_code}")
            except Exception as e:
                logger.error(f"Plex episode removal failed for section {section}: {e}")

        logger.info(f"Plex cleanup: removed {removed} episode(s)")
        return removed

    def _remove_strm_for_episodes(self, removal_info):
        import shutil as _shutil

        for eid, (category_id, folder_rel, file_stem) in removal_info.items():
            category = self._resolve_series_category(category_id)
            if not category or not folder_rel:
                logger.warning(f"Episode STRM removal skipped for {eid}: no known folder/category")
                continue
            try:
                series_root = self._series_category_path("")
                category_base = self._series_category_path(category["strm_folder"])
                folder = os.path.join(series_root, folder_rel)
                if file_stem:
                    nfo_path = os.path.join(folder, f"{file_stem}.nfo")
                    if os.path.exists(nfo_path):
                        os.remove(nfo_path)
                    # Clean up the season/series folders if now empty, but
                    # never delete the shared category base_dir itself.
                    try:
                        season_dir = folder
                        os.rmdir(season_dir)
                        series_dir = os.path.dirname(season_dir)
                        if os.path.abspath(series_dir) != os.path.abspath(category_base):
                            tvshow_nfo = os.path.join(series_dir, "tvshow.nfo")
                            if os.path.exists(tvshow_nfo):
                                os.remove(tvshow_nfo)
                            os.rmdir(series_dir)
                    except OSError:
                        pass  # not empty -- other episodes still activated
                logger.info(f"Episode STRM removed: {file_stem}")
            except Exception as e:
                logger.error(f"Episode STRM removal error for {eid}: {e}")

    def get_episode_info(self, episode_id):
        """Mirrors get_movie_info() -- Plex's analyzer HEAD-probes this URL
        during library scan and needs a real Content-Length or it concludes
        the file has no video/audio stream and marks it unplayable (movies
        already did this via _estimate_size(); episodes never did, which is
        why series episodes showed up in Plex but wouldn't play)."""
        eid = str(episode_id)
        if eid not in self._episodes_activated:
            self._log_diagnostic("debug", f"Episode {eid}: HEAD probe, not activated")
            return None

        try:
            from apps.vod.models import Episode
            episode = Episode.objects.get(id=int(eid))
        except Exception as e:
            self._log_diagnostic("warn", f"Episode {eid}: HEAD probe lookup failed: {e}")
            logger.warning(f"get_episode_info: episode {eid} lookup failed: {e}")
            return None

        entry = self._episodes_activated.get(eid, {})
        has_confirmed = entry.get("confirmed_size") is not None
        file_size = self._estimate_episode_size(episode)

        info = {
            "name": episode.name,
            "uuid": str(episode.uuid),
            "content_type": "video/x-matroska",
            "file_size": file_size,
        }

        relation = episode.m3u_relations.first()
        if relation:
            info["stream_id"] = relation.stream_id
            ext = getattr(relation, "container_extension", None) or "mkv"
            if ext.lstrip(".") == "mp4":
                info["content_type"] = "video/mp4"

        self._log_diagnostic("debug", f"Episode {eid}: HEAD probe response size={file_size} bytes, confirmed={has_confirmed}, series={entry.get('series_name')}")
        self._log_diagnostic("debug", f"Episode {eid}: HEAD probe OK (size={info.get('file_size')} bytes, confirmed={has_confirmed})")
        return info

    def _estimate_episode_size(self, episode):
        # Same three-tier, ground-truth-first pattern as movies' _estimate_size():
        # once Plex has analyzed the episode and reported back its own size, serve
        # that back so Content-Length never mismatches what Plex already
        # recorded, which is what triggers an unwanted re-analysis pass. Until
        # then, serve the bitrate-based estimate cached at activation time
        # (see _resolve_estimated_episode_size) rather than falling straight to
        # the crude duration guess.
        entry = self._episodes_activated.get(str(episode.id))
        if entry:
            if entry.get("confirmed_size"):
                return int(entry["confirmed_size"])
            if entry.get("estimated_size"):
                return int(entry["estimated_size"])

        duration = getattr(episode, "duration_secs", None)
        if duration and duration > 0:
            return int(duration) * 250000
        return 2 * 1024 * 1024 * 1024

    def _fetch_plex_episode_sizes(self, episode_ids=None):
        """Mirrors _fetch_plex_movie_sizes() for episodes. Episode STRM
        filenames don't embed the episode id, so matching is by series name +
        season/episode number (grandparentTitle/parentIndex/index) against
        Plex's own episode metadata, grouped per plex_library_section since
        each Series Settings category can point at a different Plex library.

        episode_ids=None fetches sizes for every matched episode across all
        known categories (maintenance sweep); pass a list to limit the match
        set (post-activation fast path).

        Returns {eid: (size, updated_at)} — updated_at is Plex's own
        top-level "updatedAt" timestamp for that item, stamped whenever Plex
        re-analyzes the file. Episode-only: used to detect when Plex's
        recorded size has drifted from what we last confirmed, so a stale
        confirmed_size doesn't sit unnoticed forever (see
        _reconcile_confirmed_episode_size). Movies deliberately untouched —
        that mechanism is working and out of scope for this change."""
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        if not plex_url or not plex_token:
            self._log_diagnostic("warn", f"_fetch_plex_episode_sizes: Plex URL or token not configured")
            return {}

        wanted_ids = {str(eid) for eid in episode_ids} if episode_ids is not None else None
        by_section = {}
        for eid, entry in self._episodes_activated.items():
            if wanted_ids is not None and eid not in wanted_ids:
                continue
            category = self._resolve_series_category(entry.get("category_id"))
            section = category["plex_library_section"] if category else None
            if section in (None, "", self.PLEX_SECTION_UNSET):
                continue
            by_section.setdefault(section, []).append((eid, entry))

        if not by_section:
            self._log_diagnostic("debug", f"_fetch_plex_episode_sizes: No episodes to query (wanted_ids={len(wanted_ids or [])})")
            return {}

        self._log_diagnostic("info", f"_fetch_plex_episode_sizes: Querying Plex for {len(by_section)} sections ({sum(len(p) for p in by_section.values())} episodes)")

        sizes = {}
        for section, pairs in by_section.items():
            try:
                self._log_diagnostic("debug", f"_fetch_plex_episode_sizes: Plex API query section={section}, episode_count={len(pairs)}")
                resp = requests.get(
                    f"{plex_url}/library/sections/{section}/all",
                    params={"X-Plex-Token": plex_token, "type": "4"},
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    self._log_diagnostic("warn", f"_fetch_plex_episode_sizes: Plex query failed section={section} status={resp.status_code}")
                    continue

                items = resp.json().get("MediaContainer", {}).get("Metadata", [])
                by_key = {}
                for it in items:
                    key = (it.get("grandparentTitle", ""), it.get("parentIndex"), it.get("index"))
                    by_key[key] = it

                found_count = 0
                for eid, entry in pairs:
                    key = (entry.get("series_name", ""), entry.get("season_number"), entry.get("episode_number"))
                    item = by_key.get(key)
                    if not item:
                        continue
                    parts = item.get("Media", [{}])[0].get("Part", [])
                    if parts and parts[0].get("size"):
                        size = int(parts[0]["size"])
                        updated_at = item.get("updatedAt")
                        sizes[eid] = (size, updated_at)
                        found_count += 1
                        self._log_diagnostic("debug", f"_fetch_plex_episode_sizes: Episode {eid} size confirmed {size} bytes (updatedAt={updated_at})")

                self._log_diagnostic("info", f"_fetch_plex_episode_sizes: Section {section} complete: {found_count}/{len(pairs)} episodes size confirmed")
            except Exception as e:
                self._log_diagnostic("error", f"_fetch_plex_episode_sizes: Section {section} failed: {type(e).__name__}: {str(e)[:100]}")

        self._log_diagnostic("info", f"_fetch_plex_episode_sizes: Complete, {len(sizes)} sizes confirmed total")
        return sizes

    def _reconcile_confirmed_episode_size(self, episode_id):
        """Fetch Plex's recorded size for one episode and store it as
        confirmed_size if found or if Plex has since re-analyzed the file
        (detected via Plex's own updatedAt timestamp advancing past what we
        last recorded — see plex_updated_at). Returns True if a size was
        stored/updated."""
        eid = str(episode_id)
        self._log_diagnostic("debug", f"_reconcile_confirmed_episode_size: Starting size reconciliation for episode {eid}")
        sizes = self._fetch_plex_episode_sizes([eid])
        result = sizes.get(eid)
        if not result:
            self._log_diagnostic("debug", f"_reconcile_confirmed_episode_size: Episode {eid} not found in Plex")
            return False
        size, updated_at = result
        entry = self._episodes_activated.get(eid)
        if entry is None:
            self._log_diagnostic("debug", f"_reconcile_confirmed_episode_size: Episode {eid} not in activated list")
            return False
        if entry.get("confirmed_size") == size and entry.get("plex_updated_at") == updated_at:
            self._log_diagnostic("debug", f"_reconcile_confirmed_episode_size: Episode {eid} already confirmed and in sync (size={size})")
            return False
        old_size = entry.get("confirmed_size")
        entry["confirmed_size"] = size
        entry["plex_updated_at"] = updated_at
        entry["mtime"] = time.time()
        self._save_state()
        self._log_diagnostic("info", f"_reconcile_confirmed_episode_size: Episode {eid} size confirmed {size} bytes (was {old_size}), mtime updated")
        logger.info(f"Confirmed Plex size for episode {eid}: {size} bytes")
        return True

    def _size_reconcile_fast_path_episode(self, episode_id):
        """Background retry loop started right after episode activation,
        mirroring _size_reconcile_fast_path() for movies."""
        self._log_diagnostic("info", f"_size_reconcile_fast_path_episode: Background reconciliation thread starting for episode {episode_id}")
        for attempt, delay in enumerate(self.SIZE_RECONCILE_FAST_PATH_DELAYS_SECS, 1):
            time.sleep(delay)
            try:
                self._log_diagnostic("debug", f"_size_reconcile_fast_path_episode: Episode {episode_id} attempt {attempt}/{len(self.SIZE_RECONCILE_FAST_PATH_DELAYS_SECS)}, querying Plex...")
                if self._reconcile_confirmed_episode_size(episode_id):
                    self._log_diagnostic("info", f"_size_reconcile_fast_path_episode: Episode {episode_id} size confirmed successfully in background thread")
                    return
            except Exception as e:
                self._log_diagnostic("error", f"_size_reconcile_fast_path_episode: Episode {episode_id} attempt {attempt} failed: {type(e).__name__}: {str(e)[:100]}")
        self._log_diagnostic("warn", f"_size_reconcile_fast_path_episode: Episode {episode_id} size reconciliation exhausted all retries")

    def _reconcile_all_confirmed_episode_sizes(self):
        """Maintenance-cycle sweep: backfill confirmed_size for every
        activated episode that doesn't have one yet, AND re-check every
        already-confirmed episode against Plex's updatedAt to catch drift
        from a later real re-analysis (e.g. Plex silently re-deriving a
        different size on its own schedule) — unlike movies' equivalent,
        which stays one-shot-only intentionally, per explicit instruction
        to scope this fix to episodes only for now."""
        candidates = list(self._episodes_activated.keys())
        if not candidates:
            self._log_diagnostic("debug", f"_reconcile_all_confirmed_episode_sizes: No episodes activated")
            return
        self._log_diagnostic("info", f"_reconcile_all_confirmed_episode_sizes: Starting maintenance sweep for {len(candidates)} episodes")
        sizes = self._fetch_plex_episode_sizes(candidates)
        confirmed = 0
        for eid in candidates:
            result = sizes.get(eid)
            if not result:
                continue
            size, updated_at = result
            entry = self._episodes_activated.get(eid)
            if entry is None:
                continue
            if entry.get("confirmed_size") == size and entry.get("plex_updated_at") == updated_at:
                continue
            entry["confirmed_size"] = size
            entry["plex_updated_at"] = updated_at
            entry["mtime"] = time.time()
            confirmed += 1
        if confirmed:
            self._save_state()
            self._log_diagnostic("info", f"_reconcile_all_confirmed_episode_sizes: Maintenance sweep complete, {confirmed}/{len(candidates)} sizes confirmed/updated")
            logger.info(f"Episode size reconcile sweep: confirmed/updated {confirmed}/{len(candidates)} episodes")

    def _resolve_estimated_episode_size(self, episode, relations):
        """Mirrors _resolve_estimated_size() for movies: try every M3U
        relation for this episode until one returns a usable bitrate, and
        cache a bitrate-based byte estimate for the gap between activation
        and confirmed_size landing. Dispatcharr has no episode equivalent of
        refresh_movie_advanced_data (episode metadata comes from
        get_series_info, which never carries bitrate) — so this calls
        Xtream's get_vod_info directly per relation, same API movies use,
        keyed by the episode's own stream_id. Returns None if no relation
        ever supplies bitrate (cheap fallback to the duration guess on every
        call — no need to cache a miss)."""
        episode.refresh_from_db()
        duration_secs = getattr(episode, "duration_secs", None)

        for relation in relations:
            bitrate = self._fetch_episode_relation_bitrate(relation)
            if bitrate is None:
                continue
            if not duration_secs:
                episode.refresh_from_db()
                duration_secs = getattr(episode, "duration_secs", None)
            size = self._estimate_size_from_bitrate(bitrate, duration_secs)
            if size:
                logger.info(
                    f"Episode {episode.id}: estimated size {size} bytes from "
                    f"bitrate {bitrate}kbps via relation {relation.id} "
                    f"(account {relation.m3u_account_id})"
                )
                return size

        logger.info(
            f"Episode {episode.id}: no relation returned bitrate info — "
            f"falling back to placeholder size estimate"
        )
        return None

    def _fetch_episode_relation_bitrate(self, relation):
        """Metadata-only Xtream get_vod_info() call for one episode's stream
        — no stream connection, no provider slot consumed. Returns bitrate
        in kbps, or None if the provider doesn't supply it for this
        relation. Unlike movies' _fetch_relation_bitrate(), this doesn't go
        through a Dispatcharr task (none exists for episodes) and doesn't
        write back to episode/series fields — bitrate is used in-memory only
        and never persisted to custom_properties, to avoid duplicating what
        Dispatcharr's own refresh_series_episodes already owns."""
        try:
            from core.xtream_codes import Client as XtreamCodesClient
            account = relation.m3u_account
            with XtreamCodesClient(
                server_url=account.server_url,
                username=account.username,
                password=account.password,
                user_agent=account.get_user_agent().user_agent,
            ) as client:
                vod_info = client.get_vod_info(relation.stream_id)
        except Exception as e:
            logger.debug(f"_fetch_episode_relation_bitrate: fetch failed for relation {relation.id}: {e}")
            return None

        if not vod_info:
            return None
        info = vod_info.get("info", {})
        if isinstance(info, list):
            info = info[0] if info and isinstance(info[0], dict) else {}
        elif not isinstance(info, dict):
            info = {}

        bitrate = info.get("bitrate")
        try:
            bitrate = float(bitrate) if bitrate else None
        except (TypeError, ValueError):
            bitrate = None
        return bitrate if bitrate and bitrate > 0 else None

    def _fetch_plex_series_tmdb_ids(self, series_ids=None):
        """Mirrors _fetch_plex_episode_sizes(): once Plex has scanned a
        series folder (even one anchored only by our placeholder uniqueid),
        its own metadata agent independently matches the show and resolves
        a real tmdb guid -- read that back rather than calling the TMDB API
        ourselves. Matches by show title (grandparentTitle-equivalent: the
        section's top-level show title) against _series_tmdb_state's
        series_clean_title, grouped by plex_library_section since each
        Series Settings category can point at a different Plex library.

        series_ids=None checks every tracked series across all known
        categories (maintenance sweep); pass a list to limit the match set
        (post-activation fast path)."""
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        if not plex_url or not plex_token:
            return {}

        wanted_ids = {str(sid) for sid in series_ids} if series_ids is not None else None
        by_section = {}
        for sid, entry in self._series_tmdb_state.items():
            if wanted_ids is not None and sid not in wanted_ids:
                continue
            category = self._resolve_series_category(entry.get("category_id"))
            section = category["plex_library_section"] if category else None
            if section in (None, "", self.PLEX_SECTION_UNSET):
                continue
            by_section.setdefault(section, []).append((sid, entry))

        resolved = {}
        for section, pairs in by_section.items():
            try:
                resp = requests.get(
                    f"{plex_url}/library/sections/{section}/all",
                    params={"X-Plex-Token": plex_token, "type": "2"},
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning(f"Plex library query failed for section {section}: {resp.status_code}")
                    continue

                items = resp.json().get("MediaContainer", {}).get("Metadata", [])
                by_title = {it.get("title", ""): it for it in items}

                for sid, entry in pairs:
                    item = by_title.get(entry.get("series_clean_title", ""))
                    if not item:
                        continue
                    for g in item.get("Guid", []):
                        guid_id = g.get("id", "")
                        if guid_id.startswith("tmdb://"):
                            resolved[sid] = guid_id[len("tmdb://"):]
                            break
            except Exception as e:
                logger.error(f"Plex series tmdb query failed for section {section}: {e}")

        return resolved

    def _reconcile_series_tmdb_id(self, series_id):
        """Look up one placeholder-anchored series against Plex's own
        resolved match and, if found, rewrite tvshow.nfo with the real
        tmdb_id. Returns True if a real id was found and written."""
        sid = str(series_id)
        entry = self._series_tmdb_state.get(sid)
        if entry is None or not entry.get("is_placeholder"):
            return False

        resolved = self._fetch_plex_series_tmdb_ids([sid])
        real_tmdb_id = resolved.get(sid)
        if not real_tmdb_id:
            return False

        try:
            from apps.vod.models import Series as SeriesModel
            series = SeriesModel.objects.get(id=int(sid))
        except Exception:
            return False

        entry["tmdb_id"] = real_tmdb_id
        entry["is_placeholder"] = False
        self._write_tvshow_nfo(
            series, entry["series_dir"], clean_title=entry.get("series_clean_title"),
            force=True, category_id=entry.get("category_id"),
        )
        self._save_state()
        logger.info(f"Series tmdb_id resolved via Plex for '{entry.get('series_name')}': {real_tmdb_id}")
        return True

    def _tmdb_reconcile_fast_path_series(self, series_id):
        """Background retry loop started right after series activation,
        mirroring _size_reconcile_fast_path_episode(). Only started for
        series that got a placeholder uniqueid at write time."""
        for delay in self.TMDB_RECONCILE_FAST_PATH_DELAYS_SECS:
            time.sleep(delay)
            try:
                if self._reconcile_series_tmdb_id(series_id):
                    return
            except Exception as e:
                logger.error(f"Fast-path tmdb reconcile error for series {series_id}: {e}")

    def _calculate_tmdb_confidence(self, title, year, tmdb_result):
        """Hybrid confidence scoring for TMDB search results.
        Combines popularity, vote weighting, and name/year match bonuses.
        Returns percentage 0-100."""
        if not isinstance(tmdb_result, dict):
            return 0

        base_score = 50.0

        # Popularity score (max +20): normalize popularity to 0-20 range (typical TMDB popularity is 0-100+)
        popularity = tmdb_result.get("popularity", 0)
        popularity_bonus = min(20, (popularity / 100) * 20)
        base_score += popularity_bonus

        # Vote weighting (max +15): higher vote count + higher vote average = more reliable
        vote_count = tmdb_result.get("vote_count", 0)
        vote_avg = tmdb_result.get("vote_average", 0)
        vote_bonus = 0
        if vote_count > 50:
            vote_bonus += 10
        if vote_avg >= 7.0:
            vote_bonus += 5
        base_score += vote_bonus

        # Name match bonus (max +15): exact or very close title match
        tmdb_title = tmdb_result.get("name", "").lower()
        clean_title = title.lower().strip()
        if tmdb_title == clean_title:
            base_score += 15
        elif tmdb_title.startswith(clean_title) or clean_title.startswith(tmdb_title):
            base_score += 10

        # Year match bonus (max +10): exact or within ±1 year
        tmdb_year_str = tmdb_result.get("first_air_date", "")
        if tmdb_year_str:
            try:
                tmdb_year = int(tmdb_year_str.split("-")[0])
                year_int = int(year)
                if tmdb_year == year_int:
                    base_score += 10
                elif abs(tmdb_year - year_int) == 1:
                    base_score += 5
            except (ValueError, IndexError):
                pass

        return min(100, base_score)

    def _search_tmdb_series(self, series_name, year):
        """Query TMDB /search/tv endpoint for series matches.
        Returns list of results sorted by confidence score (descending).
        Reads API key from Dispatcharr settings."""
        try:
            import requests
        except ImportError:
            logger.warning("requests library not available for TMDB search")
            return []

        tmdb_api_key = self.settings.get("tmdb_api_key", "")
        if not tmdb_api_key:
            logger.warning("No TMDB API key configured")
            return []

        try:
            url = "https://api.themoviedb.org/3/search/tv"
            params = {
                "api_key": tmdb_api_key,
                "query": series_name,
                "first_air_date_year": str(year) if year else None,
            }
            params = {k: v for k, v in params.items() if v}

            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            scored = []
            for result in results:
                confidence = self._calculate_tmdb_confidence(series_name, year, result)
                scored.append({
                    "tmdb_id": result.get("id"),
                    "name": result.get("name"),
                    "year": result.get("first_air_date", "")[:4],
                    "poster_path": result.get("poster_path"),
                    "overview": result.get("overview"),
                    "confidence": confidence,
                    "raw": result,
                })

            scored.sort(key=lambda x: x["confidence"], reverse=True)
            return scored
        except Exception as e:
            logger.error(f"TMDB search error for '{series_name}' ({year}): {e}")
            return []

    def _run_tmdb_detection_sweep(self):
        """Maintenance-cycle sweep: search TMDB for series with placeholder IDs
        that haven't been searched yet. Auto-accept >= 80% confidence matches,
        queue < 80% results for manual review."""
        from apps.vod.models import Series as SeriesModel
        candidates = [
            (sid, entry) for sid, entry in self._series_tmdb_state.items()
            if entry.get("is_placeholder") and not entry.get("searched")
        ]
        if not candidates:
            return

        auto_resolved = 0
        queued_for_review = 0

        for sid, entry in candidates:
            try:
                series_name = entry.get("name")
                year = entry.get("year")
                if not series_name:
                    continue

                time.sleep(self.TMDB_SEARCH_DELAY_SECS)
                results = self._search_tmdb_series(series_name, year)
                if not results:
                    entry["searched"] = True
                    continue

                best = results[0]
                if best["confidence"] >= self.TMDB_DETECTION_CONFIDENCE_THRESHOLD:
                    # Auto-accept
                    try:
                        series = SeriesModel.objects.get(id=sid)
                        series.tmdb_id = best["tmdb_id"]
                        series.save(update_fields=["tmdb_id"])
                        entry["is_placeholder"] = False
                        entry["tmdb_id"] = best["tmdb_id"]
                        entry["searched"] = True

                        # Rewrite NFO with real tmdb_id
                        series_dir = os.path.join(self._data_dir, "series", str(sid))
                        if os.path.isdir(series_dir):
                            self._write_tvshow_nfo(series, series_dir, force=True)

                        auto_resolved += 1
                        logger.info(f"TMDB detection auto-resolved series {sid}: {best['name']} (id={best['tmdb_id']}, confidence={best['confidence']:.0f}%)")
                    except Exception as e:
                        logger.error(f"TMDB detection auto-resolve error for series {sid}: {e}")
                        entry["searched"] = True
                else:
                    # Queue for manual review
                    self._tmdb_detection_results[sid] = {
                        "name": series_name,
                        "year": year,
                        "results": results[:5],  # Top 5
                        "ts": time.time(),
                    }
                    entry["searched"] = True
                    queued_for_review += 1
                    logger.debug(f"TMDB detection queued series {sid} for manual review (best={best['confidence']:.0f}%)")
            except Exception as e:
                logger.error(f"TMDB detection sweep error for series {sid}: {e}")
                try:
                    self._series_tmdb_state[sid]["searched"] = True
                except:
                    pass

        if auto_resolved or queued_for_review:
            logger.info(f"TMDB detection sweep: {auto_resolved} auto-resolved, {queued_for_review} queued for review")

    def _reconcile_all_series_tmdb_ids(self):
        """Maintenance-cycle sweep: backfill a real tmdb_id for every
        activated series still sitting on a placeholder uniqueid."""
        missing = [
            sid for sid, entry in self._series_tmdb_state.items()
            if entry.get("is_placeholder")
        ]
        if not missing:
            return
        resolved = 0
        for sid in missing:
            try:
                if self._reconcile_series_tmdb_id(sid):
                    resolved += 1
            except Exception as e:
                logger.error(f"Series tmdb reconcile sweep error for series {sid}: {e}")
        if resolved:
            logger.info(f"Series tmdb reconcile sweep: resolved {resolved}/{len(missing)} series")

    def _get_episode_redirect_lock(self, episode_id):
        with self._episode_redirect_locks_guard:
            lock = self._episode_redirect_locks.get(episode_id)
            if lock is None:
                lock = threading.Lock()
                self._episode_redirect_locks[episode_id] = lock
            return lock

    def get_episode_redirect_url(self, episode_id):
        # Mirrors get_redirect_url()'s coalesce/stagger pattern -- rclone's
        # VFS read-ahead opens several concurrent range requests for the
        # same episode file within milliseconds (synthetic .mkv entries hit
        # this exactly like movies' real ones do), and without coalescing
        # each one independently resolves its own stream pick and races
        # Dispatcharr for a connection slot, producing rapid vod_start/stop
        # churn and multiple simultaneous real provider connections for one
        # episode (observed live: 2 concurrent rclone connections for
        # #BringBackAlice S01E01, tens of start/stop cycles within a minute).
        eid = str(episode_id)
        lock = self._get_episode_redirect_lock(eid)

        with lock:
            cached = self._recent_episode_redirects.get(eid)
            if cached and (time.time() - cached[0]) < self.REDIRECT_COALESCE_SECS:
                _, redirect_url, error, account_id, stream_id = cached
                if redirect_url and account_id:
                    if self._account_has_capacity(account_id):
                        pass
                    else:
                        time.sleep(self.REDIRECT_BURST_STAGGER_SECS)
                        return redirect_url, error, account_id, stream_id
                else:
                    return redirect_url, error, account_id, stream_id

            if eid not in self._episodes_activated:
                result = (None, "Episode not activated", None, None)
                self._recent_episode_redirects[eid] = (time.time(), *result)
                return result

            dispatcharr_url = self.settings.get("dispatcharr_url", "").rstrip("/")
            if not dispatcharr_url:
                result = (None, "Dispatcharr URL not configured", None, None)
                self._recent_episode_redirects[eid] = (time.time(), *result)
                return result

            try:
                from apps.vod.models import Episode
                episode = Episode.objects.get(id=int(eid))
            except Exception:
                result = (None, "Episode not found", None, None)
                self._recent_episode_redirects[eid] = (time.time(), *result)
                return result

            relations = list(episode.m3u_relations.all())
            if not relations:
                result = (None, "No stream mapping for episode", None, None)
                self._recent_episode_redirects[eid] = (time.time(), *result)
                return result

            entry = self._episodes_activated.get(eid, {})
            cached_stream_id = entry.get("stream_pick")
            relation = relations[0]
            if cached_stream_id is not None:
                for r in relations:
                    if str(r.stream_id) == str(cached_stream_id):
                        relation = r
                        break

            relation = self._pick_relation_with_capacity(relations, relation)
            entry["stream_pick"] = relation.stream_id
            self._episodes_activated[eid] = entry
            self._save_state()

            stream_id = relation.stream_id
            account_id = str(relation.m3u_account_id) if relation.m3u_account_id else "unknown"
            redirect_url = self._build_episode_proxy_url(episode, relation)
            result = (redirect_url, None, account_id, stream_id)
            self._recent_episode_redirects[eid] = (time.time(), *result)
            self._enforce_max_concurrent_for_content(episode.uuid)
            return result

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

    def list_series_catalog_categories(self, query):
        try:
            from apps.vod.models import VODCategory, M3USeriesRelation
            from django.db.models import Count, Q

            provider_ids = [v for v in query.get("provider_id", []) if v]

            qs = VODCategory.objects.all()
            if provider_ids:
                qs = qs.annotate(
                    series_count=Count(
                        "m3useriesrelation",
                        filter=Q(m3useriesrelation__m3u_account_id__in=[int(p) for p in provider_ids]),
                    )
                )
            else:
                qs = qs.annotate(series_count=Count("m3useriesrelation"))

            cats = []
            for cat in qs.filter(series_count__gt=0).order_by("name"):
                cats.append(
                    {"id": cat.id, "name": cat.name, "count": cat.series_count}
                )
            return {"categories": cats}
        except Exception as e:
            return {"categories": [], "error": str(e)}

    def list_series_providers(self, query):
        try:
            from apps.m3u.models import M3UAccount
            from apps.vod.models import M3USeriesRelation
            from django.db.models import Count

            account_counts = {}
            for row in (
                M3USeriesRelation.objects
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
                if settings.get("debug_connections"):
                    self._log_event("debug", f"Health check -> Plex {plex_url}: HTTP {resp.status_code}")
            except Exception as e:
                checks["plex"] = {"status": "error", "message": str(e)}
                if settings.get("debug_connections"):
                    self._log_event("debug", f"Health check -> Plex {plex_url} failed: {e}")
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
                if settings.get("debug_connections"):
                    self._log_event("debug", f"Plex sessions -> {plex_url} failed: HTTP {resp.status_code}")
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
            if settings.get("debug_connections"):
                self._log_event("debug", f"Plex sessions -> {plex_url} failed: {e}")
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

    def _trigger_plex_scan(self, section=None):
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        if section is None:
            section = self.settings.get("plex_library_section", 7)
        if not plex_url or not plex_token:
            return
        # Large series/season activations can generate hundreds of STRM
        # files across several batches; without this lock each batch (or a
        # movie activation landing at the same moment) would fire its own
        # concurrent /refresh call, and Plex's scanner does not coalesce
        # overlapping scans of the same or different sections cleanly.
        with self._plex_scan_lock:
            try:
                requests.get(
                    f"{plex_url}/library/sections/{section}/refresh",
                    headers={"X-Plex-Token": plex_token},
                    timeout=10,
                )
                logger.info(f"Plex library scan triggered (section {section})")
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

    def _fetch_plex_movie_sizes(self, movie_ids=None):
        """Query Plex's own library JSON and return {movie_id_str: size_bytes}
        for whatever it has actually recorded for each item's Part. Used to
        reconcile our declared Content-Length with what Plex believes it is,
        which is what its periodic scan compares against — a mismatch there
        is what triggers a "changed size" Turbo analysis (a real provider
        connection through the rclone mount) even with nobody watching.

        movie_ids=None fetches sizes for every matched item in the library
        section (used by the maintenance sweep); pass a list to limit the
        match set (used by the post-activation fast path).
        """
        plex_url = self.settings.get("plex_url", "")
        plex_token = self.settings.get("plex_token", "")
        section = self.settings.get("plex_library_section", 7)
        if not plex_url or not plex_token:
            return {}
        try:
            resp = requests.get(
                f"{plex_url}/library/sections/{section}/all",
                params={"X-Plex-Token": plex_token},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Plex library query failed: {resp.status_code}")
                return {}

            items = resp.json().get("MediaContainer", {}).get("Metadata", [])
            id_set = {str(mid) for mid in movie_ids} if movie_ids is not None else None
            sizes = {}

            for item in items:
                parts = item.get("Media", [{}])[0].get("Part", [])
                for part in parts:
                    filename = part.get("file", "")
                    m = re.search(r'[/\\](\d+)\.(mkv|mp4)$', filename)
                    if not m:
                        m = re.search(r'\[(\d+)\]\.(mkv|mp4)$', filename)
                    if not m:
                        continue
                    mid = m.group(1)
                    if id_set is not None and mid not in id_set:
                        continue
                    size = part.get("size")
                    if size:
                        sizes[mid] = int(size)
                    break

            return sizes
        except Exception as e:
            logger.error(f"Plex size query failed: {e}")
            return {}

    def _reconcile_confirmed_size(self, movie_id):
        """Fetch Plex's recorded size for one movie and store it as
        confirmed_size if found. Returns True if a size was stored."""
        mid = str(movie_id)
        sizes = self._fetch_plex_movie_sizes([mid])
        size = sizes.get(mid)
        if not size:
            return False
        entry = self._activated.get(mid)
        if entry is None:
            return False
        if entry.get("confirmed_size") == size:
            return False
        entry["confirmed_size"] = size
        self._save_state()
        logger.info(f"Confirmed Plex size for movie {mid}: {size} bytes")
        return True

    def _size_reconcile_fast_path(self, movie_id):
        """Background retry loop started right after activation. Plex
        usually finishes analyzing a freshly-scanned item within seconds,
        so poll a few times before leaving it to the periodic maintenance
        sweep (_reconcile_all_confirmed_sizes)."""
        for delay in self.SIZE_RECONCILE_FAST_PATH_DELAYS_SECS:
            time.sleep(delay)
            try:
                if self._reconcile_confirmed_size(movie_id):
                    return
            except Exception as e:
                logger.error(f"Fast-path size reconcile error for movie {movie_id}: {e}")

    def _reconcile_all_confirmed_sizes(self):
        """Maintenance-cycle sweep: backfill confirmed_size for every
        activated movie that doesn't have one yet, including movies
        activated before this feature existed (their estimated_size is
        frozen at a stale/placeholder value that Plex will never match)."""
        missing = [
            mid for mid, entry in self._activated.items()
            if not entry.get("confirmed_size")
        ]
        if not missing:
            return
        sizes = self._fetch_plex_movie_sizes(missing)
        confirmed = 0
        names = []
        for mid in missing:
            size = sizes.get(mid)
            if not size:
                continue
            entry = self._activated.get(mid)
            if entry is None or entry.get("confirmed_size") == size:
                continue
            entry["confirmed_size"] = size
            confirmed += 1
            names.append(entry.get("strm_folder") or mid)
        if confirmed:
            self._save_state()
            logger.info(f"Size reconcile sweep: confirmed {confirmed}/{len(missing)} movies")
        self._maint_stats["last_size_reconcile"] = {
            "ts": time.time(),
            "checked": len(missing),
            "confirmed": confirmed,
            "names": names,
        }

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

    def _strip_episode_name_prefix(self, name, series_name, ep_label):
        """Dispatcharr's Episode.name sometimes already embeds 'SeriesName -
        SxxEyy - ' as a prefix -- strip it so STRM/NFO filenames don't
        duplicate the series/season/episode tag we add ourselves. Mirrors
        dashboard.html's stripEpisodeNamePrefix() (JS side, for card titles).

        series_name here is already year-stripped (via _clean_title), but
        Dispatcharr's embedded prefix keeps the year (e.g. "Show (2018) -
        S01E01 - ..."), so the year is optionally tolerated between the name
        and the separator -- without it the prefix never matched and the
        raw (still-prefixed) name fell through into the caller's file_stem,
        duplicating "Show (2018) - S01E01" in the generated filename."""
        if not name:
            return ""
        cleaned = name
        series_esc = re.escape(series_name or "")
        cleaned = re.sub(r"^\s*" + series_esc + r"(?:\s*\(\d{4}\))?\s*[-:]\s*", "", cleaned, flags=re.I)
        ep_label_esc = re.escape(ep_label or "")
        cleaned = re.sub(r"^\s*" + ep_label_esc + r"\s*[-:]\s*", "", cleaned, flags=re.I)
        return cleaned.strip()

    def _write_nfo(self, movie, folder, folder_name):
        nfo_path = os.path.join(folder, f"{folder_name}.nfo")
        tmdb_id = getattr(movie, "tmdb_id", None)
        if not tmdb_id:
            return

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<movie>",
            f"  <title>{self._xml_escape(self._clean_title(movie.name))}</title>",
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

    def list_series_vod_directory(self, subpath=""):
        """Nested HTML directory listing for the series/ tree, served at the
        separate /vod-series/ endpoint (own rclone mount + Plex TV Shows
        library) so series folders never appear inside the Movies library's
        scan root -- Plex's Movies agent would otherwise misread a
        Series/Season/Episode structure as a multi-part movie folder.
        subpath is the URL path under /vod-series/ already stripped of that
        prefix, e.g. "" (root), "comedy/", "comedy/1983 (2018)/Season 01/".

        Directories and .nfo files are real, listed straight off disk. Each
        episode's .mkv entry is synthetic -- built from self._episodes_activated,
        the same DB-driven approach movies use in list_vod_directory() -- and
        served via 302 redirect rather than being a real file on disk. A real
        .strm file here previously made Plex's "get a decision" playback step
        fail even after the folder scanned/matched fine (bead za8).

        Each entry's real mtime is appended in Apache mod_autoindex style
        (DD-Mon-YYYY HH:MM) after the anchor tag -- rclone's http backend
        parses that trailing text to populate modtime for both files and
        synthesized directory entries. Without it, rclone has no way to
        learn a directory's mtime from an HTML listing and substitutes its
        fixed 1999-12-31 sentinel for every dir, which makes Plex's TV
        scanner think the season/series folder never changes and skip
        walking it on every future scan (bead za8)."""
        from urllib.parse import quote

        base_dir = self._series_category_path("")
        rel = subpath.strip("/")
        target_dir = os.path.join(base_dir, rel) if rel else base_dir

        real_base = os.path.abspath(base_dir)
        real_target = os.path.abspath(target_dir)
        if os.path.commonpath([real_base, real_target]) != real_base:
            return "<html><body>\n</body></html>"

        links = []
        try:
            entries = sorted(os.listdir(real_target))
        except OSError:
            entries = []

        for entry in entries:
            full = os.path.join(real_target, entry)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(full)).strftime("%d-%b-%Y %H:%M")
            except OSError:
                mtime = ""
            if os.path.isdir(full):
                href = quote(entry) + "/"
                links.append(f'<a href="{href}">{entry}/</a> {mtime}')
            elif entry.endswith(".nfo"):
                links.append(f'<a href="{quote(entry)}">{entry}</a> {mtime}')

        for eid, entry in self._episodes_activated.items():
            if entry.get("strm_folder") != rel:
                continue
            stem = entry.get("strm_stem")
            if not stem:
                continue
            mtime_ts = entry.get("mtime") or entry.get("activated_at") or time.time()
            ep_mtime_str = datetime.fromtimestamp(mtime_ts).strftime("%d-%b-%Y %H:%M")
            fname = f"{stem} [{eid}].mkv"
            links.append(f'<a href="{quote(fname)}">{fname}</a> {ep_mtime_str}')

        return "<html><body>\n" + "\n".join(links) + "\n</body></html>"

    def read_series_vod_file(self, subpath):
        """Real file bytes + mtime for a .nfo under series/ -- unlike
        episode .mkv entries (always a virtual 302 redirect target, see
        list_series_vod_directory), .nfo files are actual files on disk that
        rclone/Plex read directly.

        mtime is returned so the route handler can send a real Last-Modified
        header -- rclone's http backend gets a file's modtime from that
        header (not from the directory listing HTML), and without it every
        file reads back as rclone's zero-epoch sentinel, which makes Plex's
        TV scanner treat the season/series folder as permanently unchanged
        and skip walking it on every future scan (bead za8)."""
        base_dir = self._series_category_path("")
        real_base = os.path.abspath(base_dir)
        target = os.path.abspath(os.path.join(base_dir, subpath.strip("/")))

        if os.path.commonpath([real_base, target]) != real_base:
            return None, None, "path escapes series root"
        if not os.path.isfile(target):
            return None, None, "not found"

        try:
            mtime = os.path.getmtime(target)
            with open(target, "rb") as f:
                return f.read(), mtime, None
        except OSError as e:
            return None, None, str(e)

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

    def log_episode_play_request(self, episode_id, client_ip, ok, detail=None, account_id=None, stream_id=None):
        eid = str(episode_id)
        try:
            from apps.vod.models import Episode
            ep = Episode.objects.select_related("series").get(id=int(eid))
            name = f"{ep.series.name} S{ep.season_number:02d}E{ep.episode_number:02d}"
        except Exception:
            name = f"episode #{eid}"

        # Separate namespace from movie IDs in the dedup key -- Movie and
        # Episode primary keys can collide since they're different tables.
        key = ("episode", eid, str(stream_id))
        if ok:
            now = time.time()
            last = self._last_play_log.get(key)
            if last is not None and (now - last) < self.PLAY_LOG_DEDUP_SECS:
                return
            self._last_play_log[key] = now
            via = f" (via {self._account_name(account_id)})" if account_id else ""
            self._log_event("info", f"Play request: \"{name}\" (id={eid}) from {client_ip} — redirect OK{via}")
        else:
            self._log_event("error", f"Play request: \"{name}\" (id={eid}) from {client_ip} — FAILED: {detail}")

    def _account_name(self, account_id):
        try:
            from apps.m3u.models import M3UAccount
            return M3UAccount.objects.get(id=int(account_id)).name
        except Exception:
            return f"account #{account_id}"

    def _get_provider_current_stream_count(self, account_id):
        """Get current number of active streams for a provider account.

        Used during activation to pick the least-loaded provider.
        Falls back to 0 (assume available) if the check can't be performed.
        """
        try:
            from apps.m3u.models import M3UAccountProfile
            from apps.m3u.connection_pool import get_profile_active_connection_count
            from core.utils import RedisClient

            profile = M3UAccountProfile.objects.filter(
                m3u_account_id=account_id, is_active=True
            ).order_by("-is_default").first()
            if profile is None:
                return 0

            redis_client = RedisClient.get_client()
            return get_profile_active_connection_count(profile, redis_client)
        except Exception as e:
            logger.debug(f"Stream count check skipped for account {account_id}: {e}")
            return 0

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

            estimated_size = self._resolve_estimated_size(movie, relations)

            self._activated[mid] = {
                "activated_at": time.time(),
                "audio_checks": audio_checks,
                "stream_pick": chosen_relation.stream_id,
                "estimated_size": estimated_size,
            }
            activated.append(mid)
            activated_names.append(movie.name)

        self._save_state()

        if activated:
            strm_count = self._generate_strm_for_movies(activated)
            self._save_state()
            # Option A: Fetch confirmed sizes immediately before triggering scan,
            # so that when Plex probes during scan, sizes are already cached.
            self._log_diagnostic("info", f"Movie activation: fetching {len(activated)} confirmed sizes from Plex before scan trigger...")
            sizes = self._fetch_plex_movie_sizes(activated)
            for mid in activated:
                size = sizes.get(mid)
                if size:
                    entry = self._activated.get(mid)
                    if entry and entry.get("confirmed_size") != size:
                        entry["confirmed_size"] = size
                        self._log_diagnostic("debug", f"Movie {mid}: pre-scan size confirmed {size} bytes")
            if sizes:
                self._save_state()
                self._log_diagnostic("info", f"Movie activation: {len(sizes)}/{len(activated)} sizes confirmed before scan trigger")
            self._trigger_plex_scan()
            # Background retry threads as fallback (in case initial query found no sizes yet)
            for mid in activated:
                entry = self._activated.get(mid)
                if not entry or not entry.get("confirmed_size"):
                    threading.Thread(
                        target=self._size_reconcile_fast_path,
                        args=(mid,),
                        daemon=True,
                    ).start()
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

    def _enforce_max_concurrent_for_content(self, content_uuid, new_session_hint=None):
        """Force-drop older real Dispatcharr VOD connections for the same
        movie/episode (by content_uuid) once more than
        `max_concurrent_per_title` are open at once.

        Exists because rclone's VFS re-opens a file's stream repeatedly over
        the course of a single watch (not just the millisecond read-ahead
        burst REDIRECT_COALESCE_SECS already absorbs) — each open independently
        resolves through get_redirect_url/get_episode_redirect_url and gets
        its own real Dispatcharr session, and those sessions are NOT
        deduplicated by Dispatcharr itself. Observed live: 3 simultaneous
        real sessions for one episode over about a minute, each logged as a
        separate "Play request" and each holding its own provider connection
        slot. Called right after a redirect is resolved (not before), so the
        just-issued session is included in the scan and never mistaken for
        one of the "older" ones to kill.

        Keeps the newest N sessions (by last_activity), kills the rest via
        Dispatcharr's own cleanup_persistent_connection (same call the ffprobe
        audio-check path uses), which works even while a session is actively
        streaming — unlike cleanup_stale_persistent_connections, which
        refuses to touch anything with active_streams > 0.
        """
        try:
            max_concurrent = int(self.settings.get("max_concurrent_per_title", 2) or 0)
        except (TypeError, ValueError):
            max_concurrent = 2
        if max_concurrent <= 0:
            return  # 0 = disabled, no enforcement

        try:
            from core.utils import RedisClient

            redis_client = RedisClient.get_client()
            if not redis_client:
                return

            pattern = "vod_persistent_connection:*"
            cursor = 0
            sessions = []  # (last_activity, session_id)
            while True:
                cursor, keys = redis_client.scan(cursor, match=pattern, count=100)
                for key in keys:
                    data = redis_client.hgetall(key)
                    if not data:
                        continue
                    if data.get("content_uuid") != str(content_uuid):
                        continue
                    session_id = key.split(":", 1)[1] if ":" in key else key
                    if isinstance(session_id, bytes):
                        session_id = session_id.decode()
                    try:
                        last_activity = float(data.get("last_activity", 0))
                    except (TypeError, ValueError):
                        last_activity = 0.0
                    sessions.append((last_activity, session_id))
                if cursor == 0:
                    break

            self._drop_excess_sessions(content_uuid, sessions, max_concurrent)
        except Exception as e:
            logger.debug(f"Max-concurrent enforcement skipped for {content_uuid}: {e}")

    def _drop_excess_sessions(self, content_uuid, sessions, max_concurrent):
        """Shared by _enforce_max_concurrent_for_content (single-title,
        request-triggered) and _enforce_max_concurrent_globally (periodic
        sweep across all titles) -- keeps the newest max_concurrent sessions
        (by last_activity) and force-drops the rest via Dispatcharr's own
        cleanup_persistent_connection, which works even mid-stream."""
        if len(sessions) <= max_concurrent:
            return

        try:
            from apps.proxy.vod_proxy.multi_worker_connection_manager import (
                MultiWorkerVODConnectionManager,
            )
        except Exception as e:
            logger.debug(f"Max-concurrent enforcement skipped for {content_uuid}: {e}")
            return

        # Newest first: keep max_concurrent, drop the rest.
        sessions.sort(key=lambda s: s[0], reverse=True)
        to_drop = sessions[max_concurrent:]

        manager = MultiWorkerVODConnectionManager.get_instance()
        for _, session_id in to_drop:
            try:
                manager.cleanup_persistent_connection(session_id)
                logger.info(
                    f"Max-concurrent enforcement: dropped extra VOD session "
                    f"{session_id} for content {content_uuid} "
                    f"({len(sessions)} open, limit {max_concurrent})"
                )
            except Exception as e:
                logger.warning(f"Max-concurrent enforcement: failed to drop {session_id}: {e}")

    def _enforce_max_concurrent_globally(self):
        """Periodic sweep (called from the stall watchdog loop, every ~10s)
        that catches over-limit sessions _enforce_max_concurrent_for_content
        misses: that function only runs inside get_redirect_url/
        get_episode_redirect_url, which coalesces repeat requests within
        REDIRECT_COALESCE_SECS and returns the cached redirect without
        re-resolving -- so once rclone's VFS starts hammering Dispatcharr's
        VOD proxy directly with the already-issued redirect URL (observed
        live: a fresh real Dispatcharr connection roughly every 2-10 seconds
        for the same episode, for minutes, well past the coalesce window),
        nothing ever routes back through our redirect functions again to
        trigger a fresh capacity check, and connections just pile up
        unchecked. This sweep groups all live vod_persistent_connection
        sessions by content_uuid regardless of how they were opened and
        prunes each group down to max_concurrent_per_title."""
        try:
            max_concurrent = int(self.settings.get("max_concurrent_per_title", 2) or 0)
        except (TypeError, ValueError):
            max_concurrent = 2
        if max_concurrent <= 0:
            return

        try:
            from core.utils import RedisClient

            redis_client = RedisClient.get_client()
            if not redis_client:
                return

            pattern = "vod_persistent_connection:*"
            cursor = 0
            by_content = {}  # content_uuid -> [(last_activity, session_id), ...]
            while True:
                cursor, keys = redis_client.scan(cursor, match=pattern, count=100)
                for key in keys:
                    data = redis_client.hgetall(key)
                    if not data:
                        continue
                    content_uuid = data.get("content_uuid")
                    if not content_uuid:
                        continue
                    session_id = key.split(":", 1)[1] if ":" in key else key
                    if isinstance(session_id, bytes):
                        session_id = session_id.decode()
                    try:
                        last_activity = float(data.get("last_activity", 0))
                    except (TypeError, ValueError):
                        last_activity = 0.0
                    by_content.setdefault(content_uuid, []).append((last_activity, session_id))
                if cursor == 0:
                    break

            for content_uuid, sessions in by_content.items():
                if len(sessions) > max_concurrent:
                    self._drop_excess_sessions(content_uuid, sessions, max_concurrent)
        except Exception as e:
            logger.debug(f"Global max-concurrent sweep skipped: {e}")

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

    # How long a resolved redirect for a movie/episode is reused for
    # duplicate/rapid follow-up requests, instead of re-resolving and issuing
    # a fresh 302. rclone's VFS read-ahead/startup behavior fires repeated
    # open/abort/reopen requests for the same file for several seconds at
    # the start of a stream (confirmed live 2026-08-05: a single episode saw
    # ~15 seconds of retries, 250ms-1.3s apart, each one -- without this --
    # opening its own brand new real provider connection); without
    # coalescing, each one independently races Dispatcharr's proxy for a
    # provider connection slot, and any that lose get a 429/503 and retry
    # immediately — a self-inflicted request storm on the same
    # already-at-capacity provider. Coalescing them behind one lock means
    # only one request per movie/episode resolves/redirects at a time; the
    # rest wait briefly and reuse that result instead of piling on. Kept
    # above the observed startup-burst window, well below a deliberate
    # pause/stop then resume, so a genuine resume click after playback
    # stalled/died doesn't get chained to a redirect resolved for the dead
    # connection.
    REDIRECT_COALESCE_SECS = 20

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
                if redirect_url and account_id:
                    if self._account_has_capacity(account_id):
                        # A slot is free even though this account was picked
                        # (and presumably still holding a connection) as
                        # recently as REDIRECT_COALESCE_SECS ago — the prior
                        # connection must have already dropped. Reusing the
                        # cached redirect here would hand a resuming client
                        # back the same now-dead connection instead of a
                        # fresh one, so fall through and re-resolve instead.
                        pass
                    else:
                        time.sleep(self.REDIRECT_BURST_STAGGER_SECS)
                        return redirect_url, error, account_id, stream_id
                else:
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
            self._enforce_max_concurrent_for_content(movie.uuid)
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
            self._log_diagnostic("debug", f"Movie {mid}: HEAD probe, not activated")
            return None

        try:
            from apps.vod.models import Movie
            movie = Movie.objects.get(id=int(mid))
        except Exception as e:
            self._log_diagnostic("warn", f"Movie {mid}: HEAD probe lookup failed: {e}")
            logger.warning(f"get_movie_info: movie {mid} lookup failed: {e}")
            return None

        entry = self._activated.get(mid, {})
        has_confirmed = entry.get("confirmed_size") is not None
        file_size = self._estimate_size(movie)

        info = {
            "name": movie.name,
            "uuid": str(movie.uuid),
            "content_type": "video/x-matroska",
            "file_size": file_size,
        }

        relation = movie.m3u_relations.first()
        if relation:
            info["stream_id"] = relation.stream_id
            ext = getattr(relation, "container_extension", None) or "mkv"
            if ext.lstrip(".") == "mp4":
                info["content_type"] = "video/mp4"

        self._log_diagnostic("debug", f"Movie {mid}: HEAD probe response size={file_size} bytes, confirmed={has_confirmed}, title={movie.name}")
        return info

    def _estimate_size(self, movie):
        # Plex's own recorded size (confirmed_size, read back via its API
        # after it finishes analyzing the file — see _reconcile_confirmed_size)
        # is ground truth and always wins: it's the exact value Plex compares
        # against on every periodic scan, so serving it back guarantees no
        # "changed size" mismatch, which is what triggers a Turbo
        # re-analysis (a real provider connection through the rclone mount)
        # every ~18-20 min even with nobody watching (see bead npx).
        #
        # Bitrate-derived size (cached at activation in
        # self._activated[mid]["estimated_size"]) is a fallback used until
        # Plex has confirmed a size of its own — it's an estimate, not
        # exact, so it does NOT reliably prevent the mismatch on its own
        # (proven live: Anaconda's bitrate estimate of 1,443,180,000 never
        # matched Plex's actual 1,289,110,670 and kept re-triggering).
        entry = self._activated.get(str(movie.id))
        if entry:
            if entry.get("confirmed_size"):
                return int(entry["confirmed_size"])
            if entry.get("estimated_size"):
                return int(entry["estimated_size"])

        duration = getattr(movie, "duration_secs", None)
        if duration and duration > 0:
            return int(duration) * 250000
        return 2 * 1024 * 1024 * 1024

    def _estimate_size_from_bitrate(self, bitrate_kbps, duration_secs):
        """bytes = kbps * 1000 / 8 * seconds. Returns None if either input
        is missing/non-positive."""
        if not bitrate_kbps or bitrate_kbps <= 0:
            return None
        if not duration_secs or duration_secs <= 0:
            return None
        return int(bitrate_kbps * 1000 / 8 * duration_secs)

    def _resolve_estimated_size(self, movie, relations):
        """Try every M3U relation for this movie until one returns a usable
        bitrate (provider-dependent — the same movie may have bitrate via
        one account and not another, see bead npx). Returns a bitrate-based
        byte estimate, or None if no relation ever supplies bitrate (in
        which case _estimate_size() falls back to the duration/placeholder
        guess on every call — cheap, no need to cache a miss)."""
        movie.refresh_from_db()
        duration_secs = getattr(movie, "duration_secs", None)

        for relation in relations:
            bitrate = self._fetch_relation_bitrate(relation, force_refresh=False)
            if bitrate is None:
                continue
            if not duration_secs:
                movie.refresh_from_db()
                duration_secs = getattr(movie, "duration_secs", None)
            size = self._estimate_size_from_bitrate(bitrate, duration_secs)
            if size:
                logger.info(
                    f"Movie {movie.id}: estimated size {size} bytes from "
                    f"bitrate {bitrate}kbps via relation {relation.id} "
                    f"(account {relation.m3u_account_id})"
                )
                return size

        logger.info(
            f"Movie {movie.id}: no relation returned bitrate info — "
            f"falling back to placeholder size estimate"
        )
        return None

    def _fetch_relation_bitrate(self, relation, force_refresh=False):
        """Metadata-only Xtream get_vod_info() call via Dispatcharr's own
        apps.vod.tasks.refresh_movie_advanced_data — no stream connection,
        no provider slot consumed. Returns bitrate in kbps, or None if the
        provider doesn't supply it for this relation. Bitrate availability
        is per-provider, not just per-movie (e.g. the same title may report
        bitrate via one M3U account but not another), so callers should try
        every relation for a movie rather than stopping at the first one."""
        try:
            from apps.vod.tasks import refresh_movie_advanced_data
            refresh_movie_advanced_data(relation.id, force_refresh=force_refresh)
            relation.refresh_from_db()
        except Exception as e:
            logger.debug(f"_fetch_relation_bitrate: refresh failed for relation {relation.id}: {e}")
            return None

        detailed = (relation.custom_properties or {}).get("detailed_info", {})
        bitrate = detailed.get("bitrate")
        try:
            bitrate = float(bitrate) if bitrate else None
        except (TypeError, ValueError):
            bitrate = None
        return bitrate if bitrate and bitrate > 0 else None

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
            if settings.get("debug_connections"):
                self._log_event("debug", f"Plex scan trigger -> {plex_url} section {section}: HTTP {resp.status_code}")
            return {
                "status": "ok" if resp.status_code < 300 else "error",
                "http_status": resp.status_code,
            }
        except Exception as e:
            if settings.get("debug_connections"):
                self._log_event("debug", f"Plex scan trigger -> {plex_url} section {section} failed: {e}")
            return {"status": "error", "message": str(e)}
