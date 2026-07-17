# VOD To Plex — Plugin Summary

> Plugin running inside Dispatcharr container on .245.
> History: [2026-07-13 archive](PLUGIN_SUMMARY_ARCHIVE_20260713.md) (v0.1.24 detail, 2026-07-11
> Incident Log, Channel Maintenance/Architecture/Key Files/rclone reference) |
> [2026-07-09 archive](PLUGIN_SUMMARY_ARCHIVE_20260709.md) (v0.1.22 full feature
> detail, v0.1.21 Stop/Start Server fixes, v0.1.17-20) |
> [2026-07-08 archive](PLUGIN_SUMMARY_ARCHIVE_20260708.md) |
> [2026-06-30 archive](PLUGIN_SUMMARY_ARCHIVE_20260630.md) | [pre-v0.1.3 archive](PLUGIN_SUMMARY_ARCHIVE_20260629.md)

## Deploy Process — Follow Every Step, Every Time

0. **Auth: use the SSH key, never `.ssh.env` password auth.** `pscp -i "C:\Users\knmfl\.ssh\kid_rsa.ppk" -P 22 ...` /
   `plink -i "C:\Users\knmfl\.ssh\kid_rsa.ppk" -P 22 -batch root@192.168.1.245 "..."`.
   Exception: `.109` (Plex host) refuses the key — needs password auth from `.ssh.env`, but only
   use that on `.109` specifically, never elsewhere. See `feedback-deploy-auth-use-ssh-key` and
   `plex-host-109-ssh-access` in Claude's cross-session memory.
1. `pscp -i kid_rsa.ppk` each changed file to `.245:/tmp/` individually — one command per file.
2. `docker cp` each file from `/tmp/` into the container — one `plink -i kid_rsa.ppk` call per
   file, not batched. Chain `docker cp && chown 1000:1000 && echo COPY_OK` and check for `COPY_OK`.
3. Clear `__pycache__`: `docker exec dispatcharr-IPTV2-245 find /data/plugins/vod_plex_bridge/ -name __pycache__ -exec rm -rf {} +`
4. **Verify with md5sum** — container vs. local for every changed file. All hashes must match.
5. Tell the user a Portainer restart is required — do not restart the container yourself.
6. After the user confirms restart, verify the fix behaves as expected before considering it done.

## Release Zip Packaging — Must Preserve Folder Structure

**Never use PowerShell's `Compress-Archive`** — it flattens `templates/dashboard.html` and
writes backslash entry names, which `unzip` (most Linux hosts) warns on / can fail to extract
as a real subdirectory.

**Correct method**: build with .NET's `System.IO.Compression.ZipArchive` directly, forward
slashes in entry names:
```powershell
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$files = git ls-files
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
foreach ($f in $files) {
  $entryName = $f -replace '\\','/'
  [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Join-Path (Get-Location) $f), $entryName) | Out-Null
}
$zip.Dispose()
```
**Always verify**: `unzip -q <zip> -d /tmp/verify && find /tmp/verify -type f` — confirm
`templates/dashboard.html` exists nested, no backslash warnings.

## Current State (2026-07-16)

- **v0.1.27**: resume/seek reliability fix + Bug Report export feature.
  - **Resume-after-stop playback fix**: a user reported a movie that stopped playing
    partway through in Plex, and resuming just buffered indefinitely instead of picking
    back up — had to restart from the beginning. Root cause: `REDIRECT_COALESCE_SECS`
    (3s) could reuse a cached redirect for a provider connection that had already
    dropped, so the resumed Range request got routed to a dead connection instead of a
    fresh one. Fix: narrowed the coalesce window to 1s (still enough to dedupe rclone's
    millisecond-scale VFS read-ahead bursts, the original reason it existed — confirmed
    via `git log -S`), and `get_redirect_url()` now checks whether the cached account
    currently has free capacity; if it does (implying the prior connection already
    ended), it re-resolves a fresh redirect instead of reusing the stale cached one.
    Deliberately did NOT re-add session tracking/liveness probes — those were tried and
    reverted in v0.1.6-9 for causing worse connection-holding/provider-churn problems
    (see Pending #3 below); this fix stays within the existing "no plugin-side HTTP call
    on the hot path" architecture.
  - **Bug Report / "Bundle Logs" export**: new `/api/bug-report?hours=N` endpoint and
    dashboard panel (Health tab) that packages the plugin's own activity log into a
    downloadable zip for a configurable window (4h/24h/72h/7 days), so a user can send
    diagnostic info back without needing shell/SSH access. **Hard requirement**: the
    export never contains real feed URLs or provider names — `bridge.py` now sanitizes
    every log line before it's written into the zip (`_sanitize_log_text()` regex-redacts
    URLs to `http://example.com/redacted`, `_build_provider_scrub_map()` maps real M3U
    account names to deterministic placeholders `Provider 1`, `Provider 2`, ... ordered
    by account ID). This scrubbing happens automatically, by default, with no opt-out.
  - **Also in this pass**: corrected the Dispatcharr host's container name in
    docs — it's `dispatcharr-IPTV2-245` (renamed to match the 2026-07-16 host IP move
    from .94 to .245), not `dispatcharr-IPTV2-94` as briefly documented mid-move.
- **STRM orphan-folder cleanup + investigation (no code/version change)**: user reported 4
  movies (12 Rounds, Stolen, Gunner, Aftermath) with live playback issues; investigated request
  patterns, ruled out `_revalidate_activated_streams()` as a live cause (confirmed disabled via
  matching md5sum, and `/api/health`'s nonzero counters proven stale — predate the container's
  last restart). Found: Stolen shows a retry-storm-under-capacity pattern (real, matches bead
  `3vo`'s theory); Aftermath shows a client-side decode-stall pattern (identical byte-range
  requested 23+ times, unrelated to the plugin). Separately, discovered and fixed a **confirmed
  orphan-STRM-folder bug**: 20 of 48 `/data/plugin-strm/` folders were stale duplicates from
  provider-side name changes (e.g. `Gunner - 2024 (2024)` vs. current `Gunner (2024)`) —
  `bridge.py` computes folder names fresh from live `Movie.name`/`year` on every write but never
  cleans up a prior folder when the computed name changes for a still-activated movie (the only
  deletion path, `_reconcile_removed_movies()`, fires solely on explicit movie removal). Both old
  and new folders' STRM files always resolved to the identical movie-ID URL, so this was
  confirmed as filesystem hygiene only, no playback impact. Manually deleted the 20 stale
  folders (kept newest-mtime per movie ID; left 7 singleton-only titles and preserved the single
  correct "Black Snow" folder out of 3 variants untouched). Filed **bead `pjx`** to design
  automatic detection/cleanup for this going forward — deferred, not yet implemented. Full
  findings logged as a comment on bead `3vo`.
- **v0.1.26 is current and fully reconciled** — git (tagged `v0.1.26`), GitHub Release (zip
  asset, marked Latest via .NET ZipArchive method, verified clean), and `.245` all match.
  Connection-capacity reliability pass: fixed `is_bridge` session-detection (was matching a
  stale mount name so the stall watchdog never fired), fixed an ffprobe audio-probe connection
  leak (was costing 2 provider slots instead of 1), added redirect coalescing for
  concurrent/rapid same-movie requests, added burst-stagger for capacity contention (rclone's
  VFS read-ahead opening several connections within ms of each other). **Config note**:
  `dispatcharr_url` must include the port (`http://<host>:9191`) — used both for the redirect
  URL and for resolving Dispatcharr's proxy 301 during audio probing; a port-less URL silently
  breaks both.
- **v0.1.25**: `_revalidate_activated_streams()` (background stream-pick revalidation) disabled
  entirely via feature flag — deliberately does not even read `revalidation_interval_secs`, so a
  stray `configure_plugin` write can't silently re-enable it. Full original implementation
  detail in the 2026-07-13 archive.
- Older releases (v0.1.22-24) and the 2026-07-11 `configure_plugin` full-replace incident: see
  the 2026-07-13 archive.

## Architecture
```
Plex hits STRM → rclone GET /vod/{id}.mkv
  → plugin: GET → 302 to {dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={id}
  → Dispatcharr proxy handles everything natively
```
No pre-fetching, no caching, no session resolution in the plugin — by design.

## Key Files
```
plugin/vod-plex-bridge/
├── bridge.py       — ORM queries, activation, STRM gen, language detection (NO cache/session code)
├── server.py       — WSGI routing, all API endpoints
├── plugin.py       — Plugin lifecycle, server start/stop/status
├── plugin.json     — Manifest (fields, actions)
└── templates/dashboard.html — Browse/Streams/Health UI
```
- Repo: `https://github.com/knmplace/dispatcharr-vod-plex-bridge-plugin`
- Container: `dispatcharr-IPTV2-245` | Port: 8888 | Dashboard: `http://192.168.1.245:8888/`
- Server start is MANUAL — click "Start Server" after enabling the plugin

## Pending
1. **EPG brown channels on .245** — assignments exist in DB, sources healthy, program records may
   be stale. Next: check EPG program records for those epg_data_ids, or force re-import.
2. Series support (after movies stable) — **ON HOLD**: Plex probes during library scan trigger
   real provider connections for episodes same as movies, unresolved.
3. **DO NOT re-add** head/tail cache, session pre-resolution, or a per-request liveness probe —
   all three tried and reverted (v0.1.6-9), all caused connection-holding/provider-churn
   problems. No plugin-side HTTP call on the hot path (rclone calls `get_redirect_url()` fresh
   on every Range/seek request).
4. **STRM orphan-folder rename detection** (bead `pjx`) — design whether the nightly refresh or
   reactivation flow should auto-detect a computed-folder-name change and clean up the stale
   prior folder. Working view: movie identity/playback is driven by movie ID / STRM target URL,
   not display name, so this is pure hygiene, not urgent.
5. **Confirm TMDB Read Access Token was regenerated** after the 2026-07-11 `configure_plugin`
   settings wipe (see 2026-07-13 archive) — not recoverable, unconfirmed as of last check.
6. See `bd ready` for the full open bead list (connection-capacity contention `wh3`, Aftermath
   decode-failure `nk7`, redirect-coalescing follow-up `3vo`, plugin-repo submission `zpj`).

## rclone on .109 (plugin mount)
`rclone-vodplugin.service` → `/mnt/vod-plugin`, remote `vodplugin` →
`http://192.168.1.245:8888/vod/`. Flags: `--allow-other --read-only --vfs-cache-mode off
--dir-cache-time 1m --poll-interval 0`. **DO NOT add** `--vfs-read-chunk-size` or
`--vfs-cache-mode full` — these hold connections open.
