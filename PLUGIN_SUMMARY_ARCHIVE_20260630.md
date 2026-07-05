# VOD To Plex — Plugin History Archive (through 2026-06-30)

> Archived from PLUGIN_SUMMARY.md on 2026-06-30.
> Pre-v0.1.3 history in `PLUGIN_SUMMARY_ARCHIVE_20260629.md`.

## v0.1.3 / v0.1.4 / v0.1.5 History

### v0.1.5 (2026-06-30)
- **Status button** — shows `✓ Server running on port 8888 | N activated | N in catalog` or `✗ not running`
- **Brighter text** — `--text2` raised from `#888` to `#b0b0b0`
- **Logo fix** — renamed `logo.jpg` → `logo.png` (Dispatcharr requires `.png`)
- **Auto-start tried and reverted** — `Plugin.__init__()` auto-start caused double VOD connections. Reverted.
  Server start is now MANUAL via "Start Server" action.
- **Port-in-use check** — `_start_server`/`_server_status` use `_port_in_use()` socket probe instead of
  instance state (Dispatcharr re-instantiates `Plugin` per request, module state unreliable).

### v0.1.6 head/tail cache — TRIED AND REVERTED (2026-06-30)
Root cause: Dispatcharr returns **301 Moved Permanently** (not 302). rclone caches 301 permanently →
stale session URL → playback dies at exactly 15s. Not fixable on plugin side without proxying bytes directly.
Reverted: `_fetch_movie_cache()`, `_background_cache_movies()`, head/tail cache routes and UI, `filesize_cache.json`.

### v0.1.4
- **TMDB language detection** — optional `tmdb_api_key` + `tmdb_read_token` plugin settings
- `bridge.py`: `language_cache.json` in `/data/vod-plex-bridge/`. Daemon thread bulk detect,
  0.5s/movie (2 req/sec — TMDB limit is 40/10s), 5-retry 429 backoff.
- **Limit dropdown**: Activated Only / 500 / 1k / 2k / 5k / All
- `server.py`: `/api/languages`, `/api/lang-status`, `/api/movies/detect-language[-all]`
- `dashboard.html`: language multi-select filter, globe icon on cards, amber ETA status bar

### v0.1.3
- **Multi-select provider + category dropdowns** — checkbox panel UI (Set-based `msState`)
- **Per-page selector** — 300 / 800 / 1300 / 1800 / All
- **Deactivation deletes from Plex** — `_plex_delete_movies()` queries library JSON, DELETEs by ratingKey
- **ORM filter bug fixed** — chaining `.filter()` on reverse multi-valued relation `m3u_relations` caused
  cross-provider contamination. Fixed: both conditions in ONE `.filter()` call.

## Double VOD Connection — Root Cause & Fix (2026-06-30)

**Symptom**: Dispatcharr showed 2 VOD connections per movie. One dropped after a few seconds.

**Root cause**: `get_redirect_url()` returned a bare, session-less URL every call. rclone issues two GETs
per file (normal HTTP mount behavior). Each bare URL caused Dispatcharr to mint a new `session_id` and open
a new provider connection — two simultaneous provider connections per playback.

**Key finding from Dispatcharr source** (`/app/apps/proxy/vod_proxy/views.py`):
- Request to `/proxy/vod/movie/{uuid}` with **no session_id** → Dispatcharr mints new session, returns **301**
  to `/proxy/vod/movie/{uuid}/{session_id}?stream_id=X` — no provider connection yet
- Request WITH session_id in path → calls `stream_content_with_session()` → opens provider connection

**Fix** (v0.1.6, `bridge.py`): `_resolve_session()` makes a `Range: bytes=0-0` probe to get the 301 Location,
extracts the session-scoped URL, caches it for 30 seconds (`SESSION_TTL`). Subsequent requests within TTL
reuse the same session URL → one provider connection. After TTL, next request resolves fresh session
(allows a second independent viewer their own connection).

**What we ruled out before finding root cause**:
- server.py HEAD handler is correct — returns 200 with no 302, HEAD does NOT cause a provider connection
- Both connections were GET requests from rclone (not HEAD)
- Movie played fine — one connection won, other dropped early

## EPG Brown Channels on .94 (open)

**Symptom**: Channels on .94 show brown (unmatched EPG). Same providers on .251 correct.
- All channels have `epg_data_id` in DB — assignments exist
- EPG sources show `status: "success"`
- `match_epg_all` only matches channels WITHOUT EPG — no effect on already-assigned channels
**Next step**: Check if EPG program records exist for those epg_data_ids, or force re-import EPG data.

## v0.1.9 probe-based failover — TRIED AND REVERTED (2026-07-04)

**Problem investigated**: a handful of specific activated movies failed to play in Plex with
Dispatcharr logging `[VOD-ERROR] No suitable M3U profile found`, while most others played fine.
Confirmed via `git log` the plugin code hadn't changed in 3-4 days before these failures started —
pointed at a provider-side change, not a plugin regression.

**Design considered and approved ("Option A")**: probe for a live stream only on the first real play
attempt (not at activation, not on Plex's periodic metadata-refresh reconnects), cache the result in
`_activated[mid]["stream_pick"]`, only re-probe if the cached choice starts failing. Steady-state
behavior for single-relation movies was supposed to stay exactly one clean 302, no added latency.

**What actually happened (v0.1.9)**: added `_probe_stream()` — an HTTP `Range: bytes=0-0` GET to
Dispatcharr's proxy inside `get_redirect_url()`, intended to fire once per movie. In practice, rclone's
HTTP mount issues a fresh GET (and thus a fresh call to `get_redirect_url()`) for **every** Range/seek
request — so the probe fired on every chunk, not once per movie. For movies with only ONE relation,
there was no alternate to fall back to, so it just re-probed and re-redirected to the SAME flaky
stream_id repeatedly. Logs showed 4 rapid duplicate 302 redirects and Dispatcharr session attempts
within milliseconds for a single play, 3 of which failed before one happened to succeed. This
directly reproduced the exact "rapid session cycling causes provider blocks" failure mode the
project's CLAUDE.md warns about — the "fix" was actively worse than the original bug.

**Caught by**: user noticed Dispatcharr's Active Connections panel showing a stale "stuck at 100%
watched" progress card despite Plex still genuinely playing, and asked to stop doing head/tail
probing entirely — let Plex/Dispatcharr's own session handling manage it without any plugin-side
pre-flight HTTP calls.

**Fix (v0.1.10)**: fully reverted `_probe_stream()` and all per-request HTTP pre-flight logic.
`get_redirect_url()` now just picks `relations[0]` by default, or a previously cached `stream_pick`
if one was set by `mark_stream_bad()` (defined but not wired to anything yet — no automatic trigger,
no dashboard button). Also fixed the underlying tuple-arity bug: every branch of `get_redirect_url()`
now consistently returns a 3-tuple `(url, error, account_id)` (previously some early-return branches
returned only 2 values). Added a top-level `try/except` in `server.py`'s WSGI `app()` wrapper so
future unhandled exceptions log via `logger.exception()` and return a clean 500 instead of dying
silently in wsgiref. Added `/favicon.ico` and `/logo.png` routes (`_serve_logo()`) plus a `<link
rel="icon">` tag in `dashboard.html` — purely cosmetic, unrelated to the playback investigation.

## Single-connection provider accounts — root cause found, no fix needed (2026-07-04/05)

After v0.1.10 removed all retry/probe logic, a handful of movies still intermittently failed
(`[VOD-ERROR] No suitable M3U profile found`). Investigated via live Dispatcharr logs and the
account-profile API:

- The affected movies all mapped to M3U accounts whose *provider-reported* connection limit was
  just 1 concurrent stream — well below Dispatcharr's own configured `max_streams` for those
  profiles, which was already more generous than the real limit.
- Other accounts with more provider-side headroom (3-5 concurrent connections) never showed the
  same failure.
- rclone's HTTP mount (`--vfs-cache-mode off`, no local caching) issues an initial short-lived
  overlapping range request at playback start that self-resolves in ~2-3 seconds (confirmed via
  Dispatcharr's Active Connections panel: a second card appears for a few seconds, then disappears,
  while the real playback connection continues uninterrupted the whole time). On single-connection
  provider accounts, this brief overlap is enough to collide with itself and get rejected by the
  provider. On accounts with real headroom, the overlap is invisible.
- **Considered and rejected**: raising Dispatcharr's `max_streams` on the affected profiles to match
  higher-capacity accounts. The API call rejected the change outright, and investigation showed it
  would have been wrong anyway — the provider's real concurrent-connection limit is fixed at 1
  regardless of what Dispatcharr is configured to allow, so raising `max_streams` doesn't create
  real capacity, it just lets Dispatcharr believe it has room the provider will reject regardless.
- **Considered and rejected**: switching the plugin's rclone mount to `--vfs-cache-mode full` (like
  the standalone bridge's Plex mount) to eliminate the startup overlap entirely. Rejected as
  disproportionate — the overlap is brief (2-3s), self-resolving, and only actually matters for
  single-connection provider accounts. Full caching would be a much bigger, more invasive change to
  a mount that otherwise works fine, to fix a problem that only manifests as occasional first-play
  flakiness on a handful of movies.
- **Conclusion**: this is a genuine provider-side limitation (accounts that only permit one real
  concurrent connection), not a plugin or Dispatcharr bug. No code or config changes made. Movies on
  these specific accounts will occasionally fail to start on the first attempt and need a retry from
  the user — accepted as a provider constraint, not something to engineer around. Confirmed the same
  evening: one other, unrelated movie (on a different, higher-capacity account) had a one-off
  buffering report that resolved itself on retry with no code change — consistent with ordinary
  transient provider hiccups, not a systemic issue.
