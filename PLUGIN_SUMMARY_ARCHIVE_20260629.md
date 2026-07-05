# VOD Plex Bridge — Plugin Summary

> Separate from main SESSION_SUMMARY.md. This tracks the Dispatcharr plugin experiment only.
> Session logs shared in `sessions/` directory.

---

## Current State (2026-06-29)

- **Plugin version**: v0.1.0 (experimental, not committed)
- **Files**: `plugin/vod-plex-bridge/` (7 files including template)
- **Deployed to**: `/data/plugins/vod_plex_bridge/` inside `dispatcharr-IPTV2-94`
- **Port**: 8888 (mapped through gluetun)
- **Dashboard**: `http://192.168.1.94:8888/`
- **Plugin key**: `vod_plex_bridge`

### What Works
- Dashboard loads — Browse/Streams/Health tabs, dark theme
- Catalog summary: 38,530 movies from Django ORM
- Movie grid: TMDB poster images, search, pagination, category filter
- Activate/deactivate movies (saves to JSON state, Active badge on cards)
- "Activated Only" filter checkbox
- Category filter dropdown (via M3UMovieRelation junction)
- Health checks: Dispatcharr DB + Plex both green (HTTP 200)
- Plugin buttons: Start/Stop Server, Status, Open Dashboard, Generate STRMs, Scan Plex
- Settings: dispatcharr_url, port, host IP, Plex config, STRM dir
- **Activate auto-generates STRM + triggers Plex scan** (no extra clicks needed)
- **Deactivate auto-removes STRM folder + triggers Plex scan**
- **`/vod/` directory listing** — HTML page with `<a>` links for activated movies (tested, works)
- **301 redirect from `/vod` to `/vod/`** — proper trailing slash handling

### What's Implemented — NEEDS TESTING (server.py rewrite not yet loaded)
- **server.py rewritten from `http.server` to FastAPI + uvicorn** (2026-06-29)
  - Copied VOD2MLIB's approach: FastAPI app with `uvicorn.Server` for proper start/stop
  - `RedirectResponse(url=proxy_url, status_code=302)` for movie file GET requests
  - `HTMLResponse` for directory listing
  - `Response` with proper headers for HEAD requests
  - FastAPI handles URL path decoding natively — no more `unquote()` hacks
  - `uvicorn.Server` with `should_exit = True` for clean shutdown from plugin thread
- **302 redirect playback**: GET `/vod/{filename}.mkv` → 302 to `{dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={stream_id}`
- **HEAD response**: HEAD `/vod/{filename}` returns Content-Type + Accept-Ranges + Content-Length

### CRITICAL: New Code Not Yet Loaded Into Running Server

**The FastAPI rewrite was deployed to disk but has NOT been loaded into the running Dispatcharr process.**

The problem: Dispatcharr's uwsgi process imports plugin modules once. Stop/Start Server only calls `serve()` again on the already-imported module. Even clearing `__pycache__` and doing Stop/Start doesn't force a re-import — Python's module cache (`sys.modules`) keeps the old bytecode in memory.

**What happened (2026-06-29):**
1. New FastAPI `server.py` deployed to disk (verified: `head -5` shows FastAPI docstring)
2. `__pycache__` cleared
3. Stop/Start Server clicked
4. Server thread crashed on `_create_app()` (new function doesn't exist in the in-memory old module)
5. Exception: `File "/data/plugins/vod_plex_bridge/server.py", line 30, in serve` + `socketserver.py line 457`
6. Some mechanism (possibly Dispatcharr's plugin loader error handling) fell back to the old `http.server` code
7. Result: "HTTP server listening on :8888" (old message) instead of "FastAPI server on :8888" (new message)
8. `/vod/{filename}` still returns `{"error": "Not found"}` (JSON = old `http.server` handler, FastAPI would return plain text)

**Fix needed (NEXT SESSION — do this first):**
1. `reload_plugins` via MCP (done — ran successfully, returned `count: 7`)
2. **Stop Server** in Dispatcharr plugin panel
3. **Start Server** in Dispatcharr plugin panel
4. Verify logs show `"VOD Plex Bridge FastAPI server on :8888"` (NOT `"HTTP server listening"`)
5. If still loading old code: **restart Dispatcharr container via Portainer** to force full module re-import
6. Test: browse `http://192.168.1.94:8888/vod/` → click movie link → should 302 redirect (not JSON error)

**How to tell which server is running:**
- Old (`http.server`): Log says `"VOD Plex Bridge HTTP server listening on :8888"`, errors return `{"error": "Not found"}` (JSON)
- New (FastAPI): Log says `"VOD Plex Bridge FastAPI server on :8888"`, errors return plain text `"Not found"`

### What's NOT Working / Known Issues
- **302 redirect returns "Not found"** — the `http.server` handler couldn't match URL-encoded paths (`%5B` vs `[`). This is WHY we rewrote to FastAPI. FastAPI decodes paths automatically. The fix is deployed but not loaded (see above).
- **rclone mount not set up on .109** — need new rclone remote pointing at `http://192.168.1.94:8888/vod/` and systemd FUSE mount
- **No provider filter** on dashboard Browse tab
- **No provider dropdown** on movie cards
- **MCP `run_plugin`/`enable_plugin` returns 400** for our plugin — must use UI buttons

### Plugin File Structure
```
plugin/vod-plex-bridge/
├── __init__.py          # exports Plugin class
├── plugin.json          # manifest: fields, actions, metadata
├── plugin.py            # Plugin class — lifecycle, action dispatch (module-level globals for server state)
├── server.py            # FastAPI + uvicorn server (REWRITTEN 2026-06-29, was http.server)
├── bridge.py            # Core logic — Django ORM queries, 302 URL builder, STRM gen, activation state
├── streaming.py         # Stub StreamPipe (UNUSED — 302 redirect replaces this)
└── templates/
    └── dashboard.html   # SPA dashboard (Browse/Streams/Health)
```

### Settings Fields (plugin.json)
| Field | Default | Purpose |
|-------|---------|---------|
| `dispatcharr_url` | `http://192.168.1.94:9191` | 302 redirect target — must be reachable by Plex |
| `http_port` | 8888 | Plugin HTTP server port |
| `dashboard_host` | `192.168.1.94` | LAN IP of Docker host (used in STRM URLs and dashboard link) |
| `plex_url` | `http://192.168.1.109:32400` | Plex server URL |
| `plex_token` | (password) | X-Plex-Token |
| `plex_library_section` | 7 | Plex library section ID |
| `strm_output_dir` | `/data/plugin-strm` | STRM file output (separate from standalone bridge's `/data/strm`) |

### Deploy Process (plugin)
1. Edit files locally in `plugin/vod-plex-bridge/`
2. `pscp` to `/tmp/plugin_update/` on .94
3. `docker cp` from host temp into container: `dispatcharr-IPTV2-94:/data/plugins/vod_plex_bridge/`
4. `docker exec dispatcharr-IPTV2-94 chown -R dispatch:dispatch /data/plugins/vod_plex_bridge/`
5. `docker exec dispatcharr-IPTV2-94 rm -rf /data/plugins/vod_plex_bridge/__pycache__`
6. **MCP `reload_plugins`** — CRITICAL: forces Dispatcharr to re-import the module from disk
7. Stop Server → Start Server in Dispatcharr plugin panel
8. If still loading old code: **restart container via Portainer** as last resort
9. Verify by checking logs: look for "FastAPI server" (new) vs "HTTP server listening" (old)

### 302 Redirect Architecture
```
Plex reads STRM → http://192.168.1.94:8888/vod/Movie (2009) [12345].mkv
  → FastAPI route: /vod/{filename:path} — path auto-decoded by FastAPI
  → _extract_movie_id() regex: \[(\d+)\]\.(mkv|mp4)$ → movie_id 12345
  → bridge.get_redirect_url(movie_id):
    → Check activated state (JSON file)
    → Django ORM: Movie.objects.get(id=12345) → uuid
    → movie.m3u_relations.first() → stream_id
    → Build URL: {dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={stream_id}
  → Returns RedirectResponse(url=proxy_url, status_code=302)
  → Plex follows redirect → Dispatcharr handles streaming natively
     (persistent connection, Range requests, Redis session management, stop detection)
```

**Why 302**: Dispatcharr's VOD proxy already handles everything — persistent connections, Range requests, session management via Redis, stop detection with 60s TTL, proper nginx config (`uwsgi_buffering off`, `uwsgi_read_timeout 300s`). Our standalone bridge's StreamPipe duplicated all of this. The 302 redirect lets Dispatcharr do what it already does well.

**What we keep**: Curated activation model (browse → select → activate), dashboard UI, health checks, Plex Now Playing, STRM/NFO generation with TMDB metadata. These are value-adds that VOD2MLIB doesn't have.

### server.py — Old vs New (for debugging reference)

**Old (http.server — REMOVED):**
- `from http.server import HTTPServer, BaseHTTPRequestHandler`
- `_make_handler(bridge, settings)` returned a Handler class
- `HTTPServer(("0.0.0.0", port), handler)` with `handle_request()` loop
- URL encoding issue: `self.path` stayed percent-encoded, regex for `[id]` didn't match `%5Bid%5D`
- Errors returned as JSON: `{"error": "Not found"}`
- Log message: `"VOD Plex Bridge HTTP server listening on :8888"`

**New (FastAPI + uvicorn — CURRENT on disk):**
- `import uvicorn` + `from fastapi import FastAPI, Request`
- `_create_app(bridge, settings)` returns FastAPI app with route decorators
- `uvicorn.Server(config).run()` — proper async server with `should_exit` for clean shutdown
- FastAPI `{filename:path}` parameter auto-decodes URL — no `unquote()` needed
- `RedirectResponse(url=..., status_code=302)` — exactly like VOD2MLIB's `httpfs.py`
- Errors returned as plain text: `Response(status_code=404, content="Not found")`
- Log message: `"VOD Plex Bridge FastAPI server on :8888"`

### VOD2MLIB Code We Copied From (reference paths inside container)
- `/data/plugins/vod2mlib/mountsrv/httpfs.py` — FastAPI GET/HEAD handlers, `RedirectResponse`, `HTMLResponse`, `_db_task()` wrapper
- `/data/plugins/vod2mlib/mountsrv/server.py` — `create_app()` function, `uvicorn.run()`, catch-all route `/{path:path}`
- `/data/plugins/vod2mlib/mountsrv/standalone_runner.py` — Django init, `_use_blocking_db_backend()`, subprocess launch
- `/data/plugins/vod2mlib/vodlib/playback.py` — `proxy_url()` builder function
- `/data/plugins/vod2mlib/httpfs_control.py` — `HttpfsControlMixin` with dependency bootstrap, PID management

**Key difference from VOD2MLIB**: They run uvicorn as a **separate subprocess** (`subprocess.Popen`) with its own Python interpreter (`/dispatcharrpy/bin/python`). We run uvicorn **in-thread** inside the Dispatcharr uwsgi process. This means:
- We don't need `standalone_runner.py` or Django re-init
- We don't need `_use_blocking_db_backend()` (we're already in Django's process)
- But we ARE affected by Python's module import cache (see "CRITICAL" section above)
- VOD2MLIB avoids this by starting a fresh Python process each time

**Possible fix if in-thread uvicorn keeps failing**: Switch to VOD2MLIB's subprocess approach — launch uvicorn as a child process like they do. This would:
- Guarantee fresh module imports on every start
- Isolate our async event loop from Dispatcharr's uwsgi
- Require copying their `standalone_runner.py` pattern (Django init + `_use_blocking_db_backend()`)

### Dispatcharr ORM Models (discovered)
| Model | Key Fields | Notes |
|-------|-----------|-------|
| `Movie` | id, uuid, name, year, rating, genre, tmdb_id, logo (FK), description, duration_secs, imdb_id, custom_properties (JSON) | Main VOD movie |
| `VODCategory` | id, name, category_type | NOT VodCategory |
| `VODLogo` | id, name, url | TMDB poster URL lives here |
| `M3UMovieRelation` | movie, category, stream_id, m3u_account | Junction: movie↔category |
| `M3UVODCategoryRelation` | category, m3u_account, enabled | Category↔account |
| `M3UAccount` | id, name, is_active | Providers |

**Full Movie fields**: id, uuid, name, description, year, rating, genre, duration_secs, logo (FK), tmdb_id, imdb_id, custom_properties (JSON), created_at, updated_at
**No container_type field** — can't determine mkv vs mp4 from Dispatcharr DB. Default to `.mkv`.
**Poster URL**: `movie.logo.url` (FK through VODLogo, not a direct text field)
**Category filter**: `Movie.objects.filter(m3u_relations__category_id=X).distinct()`
**Category count**: `VODCategory.objects.annotate(movie_count=Count("m3umovierelation"))`

### Architecture vs Standalone
| Aspect | Standalone Bridge | Plugin |
|--------|------------------|--------|
| Container | Own (`vod-plex-bridge`) | Inside Dispatcharr (gluetun VPN) |
| Data access | Scrapes API → own SQLite | Direct Django ORM |
| Network | Direct LAN (NOT behind VPN) | Behind VPN, needs `FIREWALL_OUTBOUND_SUBNETS` |
| Streaming | StreamPipe (proxies bytes, throttling, buffer) | 302 redirect to Dispatcharr proxy |
| Activation | SQLite + provider connections for head/tail | JSON file, no provider connections |
| Port | 8585 | 8888 (through gluetun) |
| STRM dir | `/data/strm` (own container) | `/data/plugin-strm` (Dispatcharr container) |

### Key Discoveries
1. Plugin inside gluetun sees VPN IP (10.100.0.2) — need `dashboard_host` setting
2. Plex reachable after adding `.109` to gluetun's `FIREWALL_OUTBOUND_SUBNETS`
3. Dispatcharr plugin API: actions return toast messages only, no URL opening support
4. Movie poster is `movie.logo.url` not `movie.poster` — `logo` is FK to `VODLogo`
5. VOD2MLIB's 302 redirect pattern proven working — every movie played without issues
6. Dispatcharr's `/proxy/vod/` handles persistent connections, Range requests, Redis sessions, stop detection natively
7. Our StreamPipe was duplicating what Dispatcharr already does
8. `custom_properties` JSONField stores hydration data (bitrate, codec info) — VOD2MLIB populates this via `refresh_movie_advanced_data()`
9. `duration_secs` available directly on Movie model — no need to fetch from TMDB
10. **`reload_plugins` alone may not force module re-import** — Python's `sys.modules` cache keeps old bytecode in memory. May need container restart via Portainer.
11. **VOD2MLIB runs uvicorn as subprocess** to avoid module cache issues — we run in-thread which IS affected
12. **uvicorn 0.49.0 and fastapi 0.138.2** are installed in both `/usr/bin/python3` and `/dispatcharrpy/bin/python`

### Bugs Found & Fixed

**Port leak on plugin disable (2026-06-29)** — when Dispatcharr disabled our plugin, port 8888 stayed bound:
- **Root cause 1**: `shutdown()` in server.py set `_running = False` but never called `self._httpd.server_close()` — the socket stayed open.
- **Root cause 2**: `_server_instance`/`_server_thread` were class-level attributes. Dispatcharr creates a new Plugin instance on reload, so the new instance had `None` refs and `_do_stop_server` thought nothing was running.
- **Fix**: (a) `shutdown()` now calls `self._httpd.server_close()` to release the socket. (b) Server state moved to module-level globals so it survives Plugin re-instantiation.

**http.server URL encoding mismatch (2026-06-29)** — clicking movie links in `/vod/` returned "Not found":
- **Root cause**: `http.server` keeps `self.path` URL-encoded (`%5B486697%5D`). Our regex looked for literal `[486697]`. Added `unquote()` but it STILL didn't work — deeper path routing issues with `http.server`.
- **Fix**: Rewrote server.py to use FastAPI + uvicorn (VOD2MLIB's approach). FastAPI's `{filename:path}` parameter auto-decodes URLs. **Not yet tested** — new code on disk but not loaded into running process.

**FastAPI code not loading after deploy (2026-06-29)** — server keeps running old `http.server` code:
- **Root cause**: Dispatcharr's uwsgi imports plugin modules once into `sys.modules`. Stop/Start only re-calls `serve()` on the already-imported old module. Clearing `__pycache__` and `reload_plugins` don't fully flush `sys.modules`.
- **Status**: `reload_plugins` was called (returned success). Need to Stop/Start again to test. If still fails, container restart via Portainer is the nuclear option.

---

## VOD2MLIB (third-party plugin)

- **URL**: https://github.com/OneHotTake/VOD2MLIB/releases/tag/v1.16.0
- **Author**: R3XCHRIS
- **Installed**: 2026-06-29 on .94 inside `dispatcharr-IPTV2-94`
- **Slug**: `vod2mlib`
- **Purpose**: Serves VOD as HTTP filesystem for rclone/Plex. Size hydration, 302→proxy streaming.
- **Requires**: uvicorn, fastapi, jinja2 (pip not included in Dispatcharr image — must bootstrap with `python3 -c 'import ensurepip; ensurepip.bootstrap()'` then `pip install uvicorn fastapi jinja2`)
- **Port**: 8889 (was 8888 — changed to avoid conflict with our plugin)
- **Settings configured**: dispatcharr_url=http://192.168.1.94:9191, httpfs_port=8889, timezone=America/New_York, generate_nfo=true, append_tmdb_id=true, hydrate_on_load=true, auth=false
- **Currently**: Disabled (our plugin using port 8888)

### VOD2MLIB Key Technical Details
- **302 redirect**: `httpfs.py` GET handler returns `RedirectResponse(url=node.metadata["stream_url"], status_code=302)`
- **Proxy URL format**: `http://{dispatcharr_url}/proxy/vod/movie/{uuid}?stream_id={stream_id}`
- **URL builder**: `vodlib/playback.py` → `proxy_url()` function — single source of truth for both STRM and HTTP mount
- **HEAD response**: Returns Content-Length (from hydration), Content-Type, Accept-Ranges
- **Hydration**: Calls `refresh_movie_advanced_data(rid)` which uses XC `get_vod_info` API — actual provider metadata calls (not stream connections). Gets bitrate/duration/codec info. Default 8 concurrent.
- **Size calculation**: stored bitrate × duration, or Range probe, or 250 KB/s estimate
- **`_REQUIRE_SIZE=true`**: Hides unsized movies from filesystem until hydrated
- **Subprocess architecture**: Runs uvicorn as child process (`subprocess.Popen`) with own Python interpreter — avoids module cache issues

### VOD2MLIB Install Issues (documented for Discord)
1. **No pip**: `ensurepip.bootstrap()` needed first, then `pip install uvicorn fastapi jinja2`
2. **Permission denied (Errno 13)**: Files installed via `docker cp` are owned by `root:root`. Fix: `docker exec <container> chown -R dispatch:dispatch /data/plugins/vod2mlib/`
3. **Port conflict**: Our plugin held port 8888 after disable. Required container restart via Portainer.
4. **Packages lost on rebuild**: uvicorn/fastapi installed in running container, not in image
5. **configure_plugin MCP wipes all settings**: API replaces, doesn't merge. Lost `dispatcharr_url` causing "Dispatcharr URL is empty" error.
6. **Permission denied creating /VODS**: `dispatch` user can't create root-level dirs. Fix: `docker exec <container> mkdir -p /VODS && chown -R dispatch:dispatch /VODS`
7. **Hydration uses provider connections**: Calls XC `get_vod_info` API at 8 concurrent — provider impact. Disable hydration on load, set concurrency to 1.

### VOD2MLIB rclone Setup on .109
- **rclone remote**: `vod2mlib` type=http url=`http://192.168.1.94:8888/` no_head=false
- **systemd service**: `/etc/systemd/system/rclone-vod2mlib.service` — mounts at `/mnt/vod2mlib`
- **Survives restarts**: Yes (systemd enabled)

---

## Strategic Direction

### Merge Vision
User wants to eventually merge our plugin with VOD2MLIB into one unified plugin:
- **Full catalog mode** (VOD2MLIB approach): Dump entire category → Plex
- **Curated mode** (our approach): Browse → select → activate into Plex
- User selects strategy in settings. Same 302 redirect backend either way.
- Reach out to VOD2MLIB developer (R3XCHRIS) to discuss collaboration.

### Why 302 Redirect Over StreamPipe
- Dispatcharr's `/proxy/vod/` already handles: persistent connections, Range requests, Redis session management, stop detection (60s TTL), nginx optimizations
- Our StreamPipe duplicated all of this AND introduced bugs (rapid session cycling, buffer management, throttle tuning)
- VOD2MLIB proved the 302 approach works — every movie played without issues
- Plugin runs inside Dispatcharr, so redirect target is localhost (fast, no network hop for the redirect resolution)

---

## Pending (Priority Order)

### Immediate (next session)
1. **Get FastAPI server loaded** — Stop/Start after `reload_plugins` (already called). If old code still loads, restart container via Portainer. Verify log message says "FastAPI server" not "HTTP server listening".
2. **Test 302 redirect** — browse `/vod/`, click a movie, confirm browser follows 302 to Dispatcharr proxy URL
3. **If in-thread uvicorn keeps failing**: Consider switching to VOD2MLIB's subprocess approach (launch uvicorn as child process like `httpfs_control.py` does)

### After playback works
4. **Set up rclone mount on .109** — new remote `vodplugin` type=http url=`http://192.168.1.94:8888/vod/` no_head=false, mount at `/mnt/vod-plugin/` (separate from standalone bridge's `/mnt/vod-bridge`)
5. **Add Plex library** — point at `/mnt/vod-plugin/` as Movies library
6. **Test full playback chain** — activate movie → appears in Plex → play → 302 → Dispatcharr proxy streams

### Enhancements
7. Add provider filter to dashboard Browse tab
8. Add activated/not-activated filter dropdown
9. Auto-start server on plugin enable
10. Commit plugin files to repo
11. Contact VOD2MLIB developer about merge collaboration
12. Provider badges on movie cards
