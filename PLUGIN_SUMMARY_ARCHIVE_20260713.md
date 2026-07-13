# Archive: 2026-07-13

Split out of PLUGIN_SUMMARY.md to keep it under the ~100-line cap. Covers v0.1.24 Current
State detail, the 2026-07-11 Incident Log, Dispatcharr Channel Maintenance, Architecture,
Key Files, and rclone-on-.109 reference sections.

## Current State (2026-07-11, latest release: v0.1.24)

- **INCIDENT (2026-07-11): `configure_plugin` wiped all plugin settings — see full writeup below
  under "Incident Log".** Root cause: full-replace MCP tool behavior, not a code bug. Recovered.
  User must still confirm whether TMDB token was re-entered (see Pending #6).
- **Background stream revalidation (same version, no bump, local commit only — not yet
  pushed)**: new `_revalidate_activated_streams()` (bridge.py), a third timed gate in
  the existing stall-watchdog loop alongside removed-check and the 7-day refresh.
  Default every 14400s / 4 hours (`revalidation_interval_secs` setting, 0=off), it re-probes each
  activated movie's **current** cached stream pick (ffprobe audio check, same as
  activation/manual check — does NOT clear/re-pick like the 7-day refresh) and
  auto-advances via `mark_stream_bad` on failure. Before each probe it checks
  `_account_has_capacity_fail_closed()` — reads Dispatcharr's real-time Redis
  connection-pool count for that account (read-only, same source `_account_has_capacity`
  already used, but fails CLOSED/skip on any lookup error since this is a background
  pass, not the hot redirect path) — and skips the movie this cycle if the account has
  no free slot, or if the movie is currently playing in Plex, or if another check is
  already in flight against the same account (`self._revalidating_accounts` guard).
  Checks run strictly serially (single watchdog thread, `REVALIDATION_DELAY_SECS=5`
  between movies). This directly answers the user's periodic-liveness-check request
  from the 2026-07-11 spike (see below) — implemented per SRE/Project
  Engineer/IT Architect spike synthesis, confirmed against live `.94` data
  (`M3UAccount.max_streams` + Redis `profile_connections:{profile_id}` are real and
  already read by existing code). Deployed to `.94`, md5-verified.
  **NOTE (2026-07-13): this feature is now fully disabled** — see current PLUGIN_SUMMARY.md
  v0.1.25 entry. Kept here for historical implementation detail only.
  **Known limitation, not solved here**: user has Amber Baby 1 + Amber Baby 2 as two
  independent M3U accounts each with `max_streams=2` (deliberately headroom above
  their real 1-connection cap to tolerate probes) — fine as-is. But Warp TV 1 + Warp TV
  2 are two Dispatcharr M3UAccounts that share the **same underlying provider login**
  (3 connections total across BOTH, not 3 each). Dispatcharr's connection-pool capacity
  check is per-account, so it cannot see this shared cap — if this revalidation pass
  (or real playback) lands on both Warp TV accounts in the same tick, the capacity
  check could read "free" on each independently while the real shared login is at its
  limit. Not a risk today (Warp TV's per-account cap of 3 gives real headroom), but
  worth remembering if Warp TV's real shared limit is ever tightened.
  **Health tab + log labeling (same round)**: added `last_revalidation` to
  `self._maint_stats` (mirrors the shape of the other 3 scheduled jobs — ts,
  due/checked/skipped counts, names, advanced_names) plus
  `revalidation_checked_total`/`revalidation_advanced_total` counters, and a
  new "Background Revalidation" row on the dashboard's Health tab showing
  last-run counts and any auto-advanced titles. Activity-log lines for this
  job now carry a `[Background Revalidation]` prefix so they stand out from
  the other maintenance jobs' log lines. Deployed to `.94`, md5-verified.
- **Post-spike reliability fixes (same version, no bump, local commit only — not yet
  pushed)**: from the 2026-07-11 `/spike` (see Pending #5), implemented the 4 low-risk
  findings: (1) STRM/NFO batch generation now isolates per-movie failures in its own
  try/except instead of one loop-wide catch, so one bad title no longer aborts the rest
  of the batch, and failures now route to `_log_event` (dashboard-visible) not just the
  container log; (2) `log_play_request` now dedupes repeated "redirect OK" lines for the
  same (movie_id, stream_id) within 60s — previously every rclone Range/seek re-hit
  produced its own near-identical log line; (3) play-request log lines now include
  `(id=NNN)` for correlation with Dispatcharr's own logs; (4) bare excepts in
  `_resolve_relation`, `mark_stream_bad`, and `get_movie_info` now log a warning with the
  movie id and exception instead of silently swallowing. `get_redirect_url()` return
  signature changed from a 3-tuple to a 4-tuple (added `stream_id`) — only caller
  (server.py) updated to match. Deployed to `.94`, md5-verified, user confirmed working
  via live Plex playback test post-restart (log showed `id=` tag + no duplicate spam
  across an in-progress play).
- **v0.1.24 is current and fully reconciled** — git (tagged `v0.1.24`), GitHub Release
  (zip asset, marked Latest), and `.94` all match. Adds a director filter, stale-stream
  auto-recovery, and search UX fixes over v0.1.22. Deployed, user-tested, confirmed
  working.
- **v0.1.23** added audio detection for activation and manual checks (deployed same day
  as v0.1.24, folded into the same public release per the versioning convention —
  intermediate same-day iterations don't get their own tag).
- **Post-v0.1.24 cleanup (same version, no bump)**: removed the "Generate STRM Files"
  and "Scan Plex Library" manual actions from both `plugin.json` and the dashboard
  (redundant — STRM generation already happens automatically on activation); fixed a
  duplicate `activate_movies` method in `bridge.py` (an earlier, simpler definition at
  the old line ~1217 was dead code, fully shadowed by the real one); consolidated 6
  scattered local `import requests` calls into a single top-level import; corrected
  README's "Zero Dependencies" claim (the plugin does use `requests`, just not via pip
  — it's already present in Dispatcharr's container); un-gitignored `TROUBLESHOOTING.md`
  so INSTALL.md's link to it actually resolves for GitHub visitors.
- **v0.1.22** bundled: connection gating (falls through to another provider account if
  the preferred one is at capacity), auto stream refresh + on-demand Refresh/Reactivate
  actions, named activity logging (movie titles + provider account in log lines and
  Maintenance Activity panel), and toast/spinner feedback on dashboard action buttons.
  Full detail in the 2026-07-09 archive.

## Incident Log

### 2026-07-11: `configure_plugin` full-replace wiped Plex/TMDB settings

**What happened**: Called `mcp__dispatcharr-94__configure_plugin(key="vod_plex_bridge",
settings={"revalidation_interval_secs": 30})` to live-test a lowered revalidation interval.
This MCP tool performs a **full replace** of the plugin's entire settings object in
Dispatcharr's DB, not a merge/patch. Sending only one field wiped every other stored field
— `plex_url`, `plex_token`, `dashboard_host`, `tmdb_api_key`/`tmdb_read_token`, and
`plex_library_section` — to blank. This broke live Plex integration (`/api/health` showed
`"plex": {"status": "unconfigured"}`) and TMDB language detection. Discovered by the user via
a dashboard Settings screenshot showing all fields blank.

**Why it happened**: Assumed `configure_plugin` merges like most PATCH-style config APIs.
It does not — confirmed by testing a corrected full-payload call afterward, which still
returned `{"success": true, "settings": {}}` with nothing actually persisted (verified via
`list_plugins` still showing `"settings": {}` and the user's own screenshot still showing
blank fields). The tool's `success: true` response is not reliable evidence of a DB write for
this plugin — `list_plugins` also appears to only return the field *schema*, never actual
saved values. The Dispatcharr web UI itself is the only reliable ground truth for this
plugin's settings.

**Recovery**:
- Plex URL (`http://192.168.1.109:32400`) — reconstructed from the Plex host's known IP.
- Plex Token — recovered by SSHing into the Plex host (192.168.1.109) as root and reading
  `PlexOnlineToken` directly out of `Preferences.xml`
  (`/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml`),
  then verified working via a live Plex API call.
- Plex Library Section ID — I initially assumed `7` (`plugin.json`'s stale default,
  `Stream-Movies-Bridge`, mount path `/mnt/vod-bridge`). **User corrected this: the actually
  correct ID is `9`** (`PlugIn-Stream-Movies`, mount path `/mnt/vod-plugin` — matches the
  plugin's real rclone mount per this file's own "rclone on .109" section). Always use `9`
  going forward.
- TMDB API key / TMDB Read Access Token — **not recoverable**. Unlike Plex, TMDB does not
  store its key in any locally-accessible config file. User must regenerate a fresh token
  from themoviedb.org and re-enter it manually.
- Because `configure_plugin` itself proved unreliable for actually writing these values back,
  the user was given the recovered values and performed the actual save themselves via the
  Dispatcharr dashboard Settings UI directly, followed by a Stop/Start Server cycle (plugin
  settings are snapshotted at server-start time — see plugin.py `_start_server()`). User
  confirmed this worked ("thats been done"); `/api/health` subsequently showed
  `"plex": {"status": "ok", "http_status": 200}`. Plex integration fully restored.

**Permanent fix**: no code fix applies — this is external Dispatcharr MCP tool behavior, not
a bug in this plugin. Saved as a standing rule in Claude's cross-session memory
(`feedback-configure-plugin-full-replace.md`): never call `configure_plugin` on any
Dispatcharr instance (`.94`, `.251`, or others) with a partial settings payload — always
fetch the full current settings first and send the complete object back with only the
intended field(s) changed.

### 2026-07-11: dashboard.html Logs-tab polling interval — changed then reverted (net: no change)

**What happened**: User asked to reduce Dispatcharr container log noise, reporting
`GET /api/activity-log` firing every ~5 seconds. Changed the Logs-tab auto-refresh interval
in `templates/dashboard.html` from a hardcoded `5000` (5s) to `LOG_AUTO_REFRESH_MS = 30000`
(30s). Deployed to `.94`, md5-verified.

Minutes later the user posted a screenshot of Dispatcharr's own native "Active Connections"
panel (a completely separate UI element, showing 6 VOD movies stuck at near-zero playback
progress) with a "Refreshing every 5s" label visible on it, and demanded an immediate revert.

**Why it was undone**: Complied immediately per explicit, urgent user instruction, even
though the "Refreshing every 5s" label almost certainly belonged to Dispatcharr's own native
panel, architecturally unrelated to this plugin's Logs tab.

**What the revert was**: Reverted exactly back to hardcoded `5000` in all 3 call sites,
removing the `LOG_AUTO_REFRESH_MS` constant entirely. Deployed, md5-verified. Net effect:
zero diff from pre-session state.

**Actual root cause (confirmed after the revert)**: The user's next message confirmed the
real driver of "log noise" was actual live Plex/Dispatcharr VOD streaming traffic (6 real
movies mid-seek-probe activity), not any dashboard polling interval at all. See
`feedback-comply-first-explain-alongside.md` in cross-session memory — this incident is
the origin of that standing rule.

**Follow-up investigation (same incident)**: The 6 stuck "Active Connections" in
Dispatcharr's native panel were investigated and ruled out as: an active Plex library scan,
real Plex playback sessions, or a hung rclone mount. Confirmed via `get_vod_proxy_stats`
they had already self-cleared. Conclusion: transient state inside Dispatcharr's own internal
VOD proxy connection accounting (a `DECR-AS-CHECK failed: could not acquire lock` warning
was seen in raw container logs) — no plugin-side fix applies; would need reporting upstream
to Dispatcharr if it recurs.

## Dispatcharr Channel Maintenance (not plugin code — tracked here since it's Dispatcharr-adjacent)
- **"Channel Fix"** — a global Claude Code skill (`~/.claude/skills/channel-fix/SKILL.md`) that
  alphabetizes and renumbers the user's 4 real Dispatcharr channel groups (Niko TV, 24/7, Live TV,
  Movies — ~157 channels total) and strips noisy `USA `/`UK: `/`US: ` name prefixes from Live TV
  and Movies (24/7's `24/7 ` prefix is meaningful, left alone). Invoke by saying "channel fix" in
  any session — not project-scoped, works everywhere.
- Manual/on-demand only via the `ecm` + `dispatcharr-94` MCP servers — ECM has no scheduler hook
  for arbitrary maintenance tasks. First run: 2026-07-08/09. See skill file for full procedure.

## Architecture (reference — unchanged, kept in main file too)
```
Plex hits STRM → rclone GET /vod/{id}.mkv
  → plugin: GET → 302 to {dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={id}
  → Dispatcharr proxy handles everything natively
```
No pre-fetching, no caching, no session resolution in the plugin — by design.

## Key Files (reference — unchanged, kept in main file too)
```
plugin/vod-plex-bridge/
├── bridge.py       — ORM queries, activation, STRM gen, language detection (NO cache/session code)
├── server.py       — WSGI routing, all API endpoints
├── plugin.py       — Plugin lifecycle, server start/stop/status
├── plugin.json     — Manifest (fields, actions)
└── templates/dashboard.html — Browse/Streams/Health UI
```

## rclone on .109 (plugin mount)
- **Service**: `rclone-vodplugin.service` → `/mnt/vod-plugin`
- **Remote**: `vodplugin` → `http://192.168.1.94:8888/vod/`
- **Flags**: `--allow-other --read-only --vfs-cache-mode off --dir-cache-time 1m --poll-interval 0`
- **DO NOT add** `--vfs-read-chunk-size` or `--vfs-cache-mode full` — these hold connections open
