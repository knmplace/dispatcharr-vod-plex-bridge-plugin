# VOD To Plex — Dispatcharr Plugin

## What This Is
A Dispatcharr plugin that bridges VOD movies into Plex via rclone HTTP mount with 302 redirect streaming. Runs INSIDE the Dispatcharr container as a stdlib WSGI server (no async, no FastAPI).

## Session Start
Read `PLUGIN_SUMMARY.md` (root of vod-plex-bridge repo) for session-by-session history, bug list, and pending items. The main `CLAUDE.md` at repo root covers the standalone bridge — this file covers the plugin only.

## Architecture
```
Plex → rclone HTTP mount → Plugin WSGI server (port 8888) → 302 redirect → Dispatcharr /proxy/vod/
```
- Plugin uses Django ORM directly (same process as Dispatcharr)
- No streaming proxy — Dispatcharr handles all streaming natively
- stdlib `wsgiref` + `ThreadingMixIn` — zero async to avoid Django's SynchronousOnlyOperation
- `close_old_connections()` in WSGI wrapper prevents DB deadlocks

## Key Files
```
plugin/vod-plex-bridge/
├── bridge.py         — Django ORM queries, activation, STRM/NFO gen, Plex API
├── server.py         — WSGI server, URL routing, query parsing
├── plugin.py         — Plugin lifecycle, module-level globals for server state
├── plugin.json       — Manifest (fields, actions, version)
├── templates/dashboard.html — Single-page dashboard (Browse/Streams/Health)
└── __init__.py       — exports Plugin class
```

## Container & Deploy
- **Container**: `dispatcharr-IPTV2-94` on host 192.168.1.94
- **Plugin dir**: `/data/plugins/vod_plex_bridge/`
- **Deploy**: pscp to .94:/tmp/, docker cp into container, chown 1000:1000, clear __pycache__
- **Restart required**: sys.modules caches old code. Must restart container via Portainer after deploy.
- **Password**: In `b:\Claude_Apps\.ssh.env` (first entry)
- Read password into variable, never put literal in command: `PW=$(grep -m1 'password=' /b/Claude_Apps/.ssh.env | cut -d= -f2)`

## Django ORM Models (apps.vod)
- `Movie` — id, uuid, name, year, rating, genre, duration_secs, logo FK, tmdb_id, description, custom_properties
- `VODCategory` — id, name (NOT VodCategory)
- `M3UMovieRelation` — movie FK, category FK, stream_id, m3u_account FK, container_extension
- `M3UAccount` (apps.m3u) — id, name, is_active
- **Poster**: `movie.logo.url` (FK through VODLogo, use `select_related("logo")`)
- **Cross-app FK gotcha**: Can't use `M3UAccount.objects.annotate(Count("m3umovierelation"))` — query M3UMovieRelation directly and group by m3u_account_id

## Query Parsing
- `_parse_query()` uses `parse_qs()` which returns `{"key": ["val1", "val2"]}` for repeated params
- Multi-select filters pass `provider_id=1&provider_id=2` — backend reads full list with `query.get("provider_id", [])`

## Dashboard Features (v0.1.3)
- Multi-select provider and category dropdowns (checkbox panels, Set-based state)
- Per-page selector (300/800/1300/1800/All)
- Movie grid with TMDB posters, search, pagination
- Select All / Clear All with filter awareness
- Trailer previews (YouTube embed)
- Quick activate/deactivate per card
- Plex delete on deactivation (DELETE /library/metadata/{ratingKey})
- Plex Now Playing panel, Health checks
- Category chips in Catalog Summary (clickable to filter)

## Plex Integration
- **Activate**: generates STRM + NFO files, triggers library scan
- **Deactivate**: removes STRM files, deletes items from Plex via API
- `_plex_delete_movies()`: queries Plex library JSON, matches by filename pattern, DELETEs by ratingKey
- Filename patterns: `{id}.mkv` or `[{id}].mkv`

## Rules
- NEVER use FastAPI/uvicorn (causes Django SynchronousOnlyOperation)
- NEVER auto-change ports or Docker configs
- No IPs, keys, passwords in commits
- Never git push without asking
- No Co-Authored-By in commits
- Container lifecycle (stop/start/rm) through Portainer only — docker cp/exec is fine

## Series Support — ON HOLD
Plex makes HEAD + Range probe requests during library scan for series episodes (same as movies). This triggers provider connections. User explicitly said don't implement until this is resolved.

## Repo
- Local: `b:\Claude_Apps\vod-plex-bridge\plugin\vod-plex-bridge\`
- GitHub: `https://github.com/knmplace/dispatcharr-vod-plex-bridge-plugin`
- Has its own .git inside the plugin dir (separate from parent repo)
