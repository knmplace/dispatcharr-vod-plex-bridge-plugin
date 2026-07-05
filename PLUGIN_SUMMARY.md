# VOD To Plex — Plugin Summary

> Plugin running inside Dispatcharr container on .94.
> History: [2026-06-30 archive](PLUGIN_SUMMARY_ARCHIVE_20260630.md) (now includes v0.1.9 probe
> revert + single-connection-provider investigation, 2026-07-04/05) | [pre-v0.1.3 archive](PLUGIN_SUMMARY_ARCHIVE_20260629.md)

## Current State (2026-07-04/05)

- **v0.1.10 — simplified `get_redirect_url()`, no probe/retry**: after a failed experiment (v0.1.9)
  that added a per-request liveness probe and made provider churn worse (see archive), the plugin
  was reverted to the simplest possible design: pick `movie.m3u_relations.first()` (or a cached
  `stream_pick` if `mark_stream_bad()` was ever called — it isn't, from anywhere, yet), return a
  bare Dispatcharr 302 URL, and let Dispatcharr/rclone handle all session/retry logic natively. No
  plugin-side HTTP calls to Dispatcharr happen outside of the one redirect per request.
- **Root-caused (not fixed, doesn't need to be)**: intermittent `[VOD-ERROR] No suitable M3U
  profile found` on a handful of movies traced to specific M3U accounts having a real provider-side
  limit of only 1 concurrent connection. A brief (~2-3s) self-resolving overlapping connection at
  playback start (rclone's HTTP mount has no local cache, `--vfs-cache-mode off`) is enough to
  collide on single-connection accounts; higher-capacity accounts never notice it. Confirmed via
  live Dispatcharr logs and the account-profile API. Raising Dispatcharr's `max_streams` was
  considered and rejected — the provider's real limit is 1 regardless of what Dispatcharr is
  configured to allow, and the API call rejected the change (400) anyway. Switching the rclone mount
  to `--vfs-cache-mode full` was also considered and rejected as disproportionate to a brief,
  self-resolving overlap. No code or config changes made — this is an accepted provider limitation,
  not a bug. Full writeup in the 2026-06-30 archive (bottom two sections).
- **Consistent 3-tuple return** from every `get_redirect_url()` branch (previously some early
  returns had only 2 values — latent bug, never actually hit in practice but fixed while in there).
- **`server.py` WSGI wrapper hardened**: top-level `try/except` around `app()` now logs unhandled
  exceptions via `logger.exception()` and returns a clean 500, instead of the exception propagating
  silently into wsgiref/stderr.
- **Favicon added**: `/favicon.ico` and `/logo.png` routes (`_serve_logo()`) + `<link rel="icon">`
  in `dashboard.html`. Cosmetic only.

## Previous State (2026-07-02)

- **`/api/headcache/status` mystery 404s — SOLVED (2026-07-02, self-inflicted, no code issue)**:
  Investigated continuous `GET /api/headcache/status` 404s in Dispatcharr's logs from
  `192.168.1.9` (the user's own PC) while the plugin's WSGI server was running. Ruled out (in
  order): vod2mlib port-squatting (our plugin genuinely owns the LISTEN socket on 8888, confirmed
  via `/proc/net/tcp`), gluetun IP mislabeling, Brave/Tor (real but unrelated — Brave was
  separately confirmed making its own outbound port-8888 connections to random public IPs, a Tor
  circuit-building artifact, coincidentally the same port number but a totally different
  destination), MCP client processes. Root cause found via Sysinternals Process Monitor process
  tree: three of Claude's own orphaned background bash loops from earlier in the session
  (`until curl http://192.168.1.94:8888/api/headcache/status | grep -q '"running":false'; do
  sleep N; done`), used to check if the plugin/bridge server had stopped, never killed after their
  check was no longer needed. Endpoint doesn't exist on either server so the loops retried forever.
  Killed all six leftover processes (three loop pairs); confirmed silent in logs afterward. No
  plugin/bridge code changes were needed — nothing to fix here, purely a leftover-process issue.
  Debug logging (`UNMATCHED_ROUTE` warning for any unmatched path, in `server.py`'s final 404
  fallthrough, logs method/path/query/User-Agent/Referer/Accept/X-Forwarded-For) was added to
  `server.py` during the investigation and **intentionally left in place** (user said keep it) —
  harmless, useful if similar mystery traffic shows up again.
  - **Reminder confirmed during this investigation**: the plugin's "Stop Server" dashboard button
    is broken/no-ops (`_server_instance` tracking bug in `plugin.py` — `_port_in_use()` returning
    true on start never sets the instance reference, so a later stop has nothing to stop). Still
    unfixed, see Pending. Only a full Dispatcharr container restart actually kills the running WSGI
    thread and picks up server.py code changes.
- **Confirmed healthy (2026-07-02)**: checked `get_vod_proxy_stats` (0 active connections, no
  leaks) and `get_system_events` on .94 — today's `vod_start`/`vod_stop` pairs (several test movies
  played from Plex at .109) are all cleanly paired, zero `vod_error` events found.
  Playback stable, no held connections. Satisfies Pending item #1 below (v0.1.8 test).
- **VOD2MLIB investigation (2026-07-02) — concluded, no merge**: considered merging with
  R3XCHRIS/VOD2MLIB (another Dispatcharr VOD plugin, no Plex support) to add Plex compatibility.
  Prototyped a patch (`_build_proxy_url` override) in a throwaway clone, then concluded it's the
  wrong approach: VOD2MLIB bulk-generates .strm for the entire catalog by default, which is safe
  for Jellyfin/Emby/Kodi (their scanners never open the URL) but would trigger real provider
  connections during Plex library scan (Plex HEAD/Range-probes files during scan) — same root
  cause as the shelved series-support issue below. Decision: keep plugins fully independent, two
  separate rclone mounts/folders, no shared write path. Our plugin's existing activation-gated
  STRM generation is the correct model for Plex; VOD2MLIB stays untouched for other servers.
  Duplication (2 small files per movie for users running both) accepted as the cost.
- **Version (2026-07-02 snapshot)**: v0.1.8 — stripped head/tail cache + session pre-resolution
  (connection hold fix). Superseded by v0.1.10, see Current State above.
- **Repo**: `https://github.com/knmplace/dispatcharr-vod-plex-bridge-plugin`
- **Container**: `dispatcharr-IPTV2-94` — Portainer restart required after code changes
- **Port**: 8888 | **Dashboard**: `http://192.168.1.94:8888/`
- **Server start is MANUAL** — click "Start Server" after enabling the plugin

## What's in v0.1.8

**Problem fixed**: plugin was holding provider connections open after playback stopped.
1. `rclone-vodplugin.service` had `--vfs-read-chunk-size` flags causing sequential range requests
   that kept connections open. Removed.
2. `bridge.py` had head/tail cache + session pre-resolution machinery (added v0.1.7, mistakenly
   mirroring the standalone bridge). Stripped entirely — Dispatcharr's 302 redirect handles
   persistent connections, Range requests, and session management natively; the plugin doesn't
   need to.

## Architecture
```
Plex hits STRM → rclone GET /vod/{id}.mkv
  → plugin: HEAD → fake Content-Length (no provider connection)
  → plugin: GET → 302 to {dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={id}
  → Dispatcharr proxy handles everything natively
```
No pre-fetching, no caching, no session resolution in the plugin — by design.

## Deploy Process
1. Edit locally in `plugin/vod-plex-bridge/`
2. `pscp` to `.94:/tmp/` → `docker cp` into `dispatcharr-IPTV2-94:/data/plugins/vod_plex_bridge/`
3. `chown 1000:1000`, clear `__pycache__`
4. **Portainer restart** required (sys.modules cache)
5. Enable plugin + Start Server in Dispatcharr UI
- Password: `b:\Claude_Apps\.ssh.env` — read into variable, never literal in commands
- `docker exec`/`docker cp` allowed ONLY for plugin container. NOT standalone bridge.

## Pending
1. ~~Test v0.1.8~~ — confirmed 2026-07-02 (see Current State)
2. **Connection gating** — UNBLOCKED (playback confirmed stable). `M3UAccount.max_streams` field
   confirmed to exist (int, 0=unlimited). `account_id` already resolved per-movie in bridge.py.
   Also now a prerequisite for safely shipping Plex-mode bulk activation.
3. **Error screens** — MP4 error videos (dead/busy/removed), standalone bridge has this shipped
   (v0.35.0) as a reference implementation — DEFER
4. **Provider fallback** — `mark_stream_bad()` exists in `bridge.py` (advances a movie's cached
   `stream_pick` to the next relation) but is not wired to anything — no dashboard button, no
   automatic trigger. Not yet requested by the user; don't build the trigger speculatively.
5. **Active Streams panel** — show connection usage per provider — DEFER
6. **EPG brown channels on .94** — assignments exist in DB, sources healthy, program records may
   be stale. Next: check EPG program records for those epg_data_ids, or force re-import.
7. Submit to official Dispatcharr plugin repo when stable
10. **Fix "Stop Server" button no-op bug** — `plugin.py`'s `_start_server`/`_stop_server`:
    `_port_in_use()` returning true on start never sets `self._server_instance`, so a later stop
    attempt has nothing to stop and silently reports "Server was not running." Confirmed broken
    live 2026-07-02. Only workaround today is a full Dispatcharr container restart.
8. Series support (after movies stable) — ON HOLD, Plex probes during scan trigger provider
   connections (same class of issue as the VOD2MLIB bulk-dump concern above)
9. **DO NOT re-add head/tail cache, session pre-resolution, or a per-request liveness probe** —
   all three tried and reverted (v0.1.6, v0.1.7, v0.1.9 respectively), all caused connection-holding
   or provider-churn problems. Root issue each time: rclone calls `get_redirect_url()` fresh on
   every Range/seek request, so any per-request HTTP pre-flight against Dispatcharr multiplies
   instead of firing once per movie. If fast-start or failover is needed later, it must not add a
   plugin-side HTTP call on the hot path — see archive for full detail on all three attempts.

## Key Files
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
