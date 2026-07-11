# VOD To Plex — Dispatcharr Plugin

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that bridges VOD movies into Plex via rclone HTTP mount with 302 redirect streaming.

## Acknowledgments

This plugin wouldn't exist without the incredible work of the Dispatcharr community. A huge thank you to the developers whose plugins paved the way and served as the foundation for this project:

- **[vod2strm](https://github.com/cmc0619/vod2strm)** by [cmc0619](https://github.com/cmc0619) — A brilliant high-performance plugin that exports VOD libraries into .strm/.nfo files. Our file size estimation approach (`duration * bitrate` with 2 GiB fallback) and 302 redirect playback pattern were directly inspired by this plugin's elegant implementation. Thank you for showing the way.

- **[VOD2MLIB](https://github.com/shedunraid/VOD2MLIB)** by [shedunraid](https://github.com/shedunraid) — The original Dispatcharr VOD-to-media-library plugin that proved the concept of scanning VOD catalogs and generating STRM files for media server import. We referenced this extensively for STRM generation patterns and plugin architecture. Thank you for pioneering this approach.

- **[VOD2MLIB fork](https://github.com/OneHotTake/VOD2MLIB)** by [OneHotTake](https://github.com/OneHotTake) — An actively maintained fork of VOD2MLIB with continued improvements and contributions to the VOD plugin ecosystem. Thank you for keeping the momentum going.

- **[Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)** — The platform this plugin runs on. Its native `/proxy/vod/` endpoint handles all the heavy lifting for streaming — persistent connections, Range requests, Redis sessions, and stop detection. None of this would be possible without the Dispatcharr team's outstanding work.

We're grateful to everyone in the Dispatcharr community who shares their work openly — it makes projects like this possible.

## How It Works

```
Plex → rclone HTTP mount → Plugin HTTP server → 302 redirect → Dispatcharr VOD proxy → Provider
```

1. Plugin runs an HTTP server inside the Dispatcharr container
2. You activate movies from the web dashboard — plugin generates STRM/NFO files
3. rclone mounts the plugin's `/vod/` endpoint as a FUSE filesystem on your Plex server
4. Plex scans the mount and sees movies with metadata (from NFO) and posters (from TMDB)
5. On playback, the plugin issues a 302 redirect to Dispatcharr's native `/proxy/vod/` endpoint
6. Dispatcharr handles the streaming connection natively — persistent connections, Range requests, session management

**No streaming proxy needed** — Dispatcharr's VOD proxy already does everything. The plugin just bridges the metadata and redirects.

## Features

- **Web Dashboard** — Browse, search, filter, activate/deactivate movies with a dark-themed UI
- **Provider & Category Filtering** — Multi-select dropdowns filter movies by provider and category
- **Selective Activation** — Choose which movies appear in Plex, individually or in bulk
- **Select All / Clear All** — Bulk select all movies matching current search and filter state
- **Trailer Previews** — Watch YouTube trailers directly from movie cards (when available)
- **302 Redirect Playback** — Zero overhead, Dispatcharr handles streaming natively
- **NFO Metadata** — Title, year, rating, TMDB ID, genre, plot for Plex matching
- **TMDB Posters** — Movie artwork via TMDB poster URLs in NFO files
- **Language Detection** — Optional TMDB lookup to tag each movie's original language. Bulk detection with configurable limit (Activated Only / 500 / 1k / 2k / 5k / All). Runs at 2 req/sec — well within TMDB's rate limit.
- **Language Filter** — Multi-select language dropdown in Browse to filter by original language
- **Plex Real-Time Delete** — Deactivating a movie immediately removes it from Plex via API (no scan needed)
- **Auto Plex Scan** — Triggers library scan after activation
- **Plex Now Playing** — Monitor active Plex sessions from the dashboard (bridge vs local content)
- **Health Checks** — Dispatcharr DB and Plex connectivity status
- **Catalog Summary** — Category chips with movie counts, quick-filter on click

## Requirements

- Dispatcharr v0.24.0 or later
- Plex Media Server
- rclone installed on the Plex server (for the HTTP mount)
- The plugin's HTTP port must be accessible from the Plex server
- `requests` Python package (already present in Dispatcharr's container — no extra install needed for a standard Dispatcharr deployment)

## Installation

See **[INSTALL.md](INSTALL.md)** for full step-by-step setup: installing the plugin, configuring settings, exposing the port, setting up rclone on the Plex server, creating the Plex library, and finding your Plex Library Section ID.

## Usage

1. Open the dashboard at `http://<host-ip>:<port>/`
2. Optionally select a **provider** to narrow the catalog to one M3U account
3. Optionally select a **category** — the dropdown updates to show only categories from the selected provider
4. Browse, search, and filter movies. Click **Select All** to select every movie matching the current filters
5. Click the activate button (lightning bolt) on individual movies, or use **Activate Selected** for bulk
6. Watch trailers by clicking the play button on movie cards (when available)
7. The plugin generates STRM + NFO files and triggers a Plex library scan
8. Movies appear in Plex with posters and metadata
9. Hit Play in Plex — the plugin redirects to Dispatcharr for streaming
10. **Deactivating** a movie removes the STRM file and deletes it from Plex immediately via API

### Language Detection (Optional)

If you configure a TMDB API key or Read Access Token in plugin settings:

1. In the Browse tab, use the **Language Detection** bar above the movie grid
2. Choose a limit from the dropdown (default: 1,000 movies — about 8 minutes)
3. Click **Detect Now** — detection runs in the background at 2 req/sec (safe within TMDB's rate limit)
4. An amber status bar shows progress and ETA
5. When complete, a language badge appears on each movie card and the **All Languages** filter populates

## Architecture

### Playback Flow
```
Plex GET /vod/12345.mkv
  → rclone forwards to plugin HTTP server
  → Plugin looks up movie in Dispatcharr DB (Django ORM)
  → Gets movie UUID + stream_id from M3U relation
  → Returns 302 redirect to Dispatcharr's /proxy/vod/movie/{uuid}?stream_id={id}
  → Plex follows redirect
  → Dispatcharr streams natively (persistent connection, Range support)
```

### File Size Estimation
rclone uses HEAD requests to determine file sizes. The plugin estimates file size from the movie's duration:
- `duration_seconds * 250,000 bytes/sec` (assumes ~2 Mbps average bitrate)
- Falls back to 2 GiB if duration is unavailable
- This ensures Plex never sees 0-byte files (which it would skip entirely)

### Plex Delete on Deactivation
When a movie is deactivated, the plugin:
1. Removes the STRM/NFO folder from disk
2. Queries Plex's library JSON for matching entries by movie ID in the filename
3. Sends `DELETE /library/metadata/{ratingKey}` to remove it from Plex immediately

This requires **Allow media deletion** to be enabled in Plex Settings → Troubleshooting.

### Why 302 Redirect?
Dispatcharr's `/proxy/vod/` endpoint already provides persistent streaming connections, HTTP Range request support, Redis-based session management, and automatic stop detection. The 302 approach uses the same code path Dispatcharr uses for browser-based playback, which is proven stable for full-length movies.

## File Structure

```
vod_plex_bridge/
├── __init__.py         # Exports Plugin class
├── plugin.json         # Plugin manifest (fields, actions, metadata)
├── plugin.py           # Plugin lifecycle — auto-start, start/stop, status
├── server.py           # WSGI HTTP server (stdlib wsgiref, threaded)
├── bridge.py           # Django ORM queries, 302 URL builder, STRM/NFO gen, Plex API
├── logo.jpg            # Plugin logo for Dispatcharr UI
└── templates/
    └── dashboard.html  # Web dashboard (Browse, Streams, Health tabs)
```

## Known Limitations

- **No connection gating** — bulk activation + Plex scan can trigger many provider connections. Recommend setting Plex library analysis to Manual.
- **Movies only** — series support is planned
- **No provider fallback** — uses the first available stream per movie
- **No error screens** — provider errors return HTTP status codes, not user-friendly video messages

## Changelog

### v0.1.16 (2026-07-07)
- **Fixed Provider/Category/Language dropdowns showing only one entry on desktop** — the base `.panel` CSS rule had `overflow: hidden` (originally just to round the panel's corners), but the Provider/Category/Language multi-select dropdowns are absolutely-positioned popouts that live inside a `.panel` — so their ancestor's `overflow: hidden` clipped the dropdown to the panel's own box, hiding everything past the first row even though the panel-body's own `max-height: 300px; overflow-y: auto` was configured correctly. Data was never the problem (the `/api/providers` endpoint always returned the full list). Fixed by moving `overflow: hidden` out of the base `.panel` rule and into an opt-in `.panel.clip` class for the one panel (Proxy Logs) that actually still needs edge clipping — dropdowns now expand and scroll properly on desktop.

### v0.1.15 (2026-07-07)
- **Fixed stale version number in dashboard header** — the dashboard template had the plugin version hardcoded as static text, so it silently drifted from the real version on every release (e.g. still showed "v0.1.12" while the plugin card correctly showed v0.1.14). The header now reads the version from `plugin.json` at request time and substitutes it into the template, so it can never go stale again.

### v0.1.14 (2026-07-07)
- **Fixed "Stop Server" no-op bug** — Start/Stop/Status tracked the running server via `self._server_instance` on the `Plugin` object, but Dispatcharr's plugin runner isn't guaranteed to reuse the same `Plugin()` instance across action calls, so a click on Stop Server could hit a freshly-constructed instance with no memory of the server the earlier Start click had spun up — it always reported "Server was not running" without stopping anything. Server tracking moved to module-level state instead, so Start/Stop/Status always agree regardless of how many `Plugin` instances get created. Also: Start Server no longer falsely reports success if the port is already bound by something it isn't tracking, and Status now distinguishes "our tracked server is running" from "the port is bound but not by us."

### v0.1.13 (2026-07-07)
- **Fixed orphaned STRM folders on catalog removal** — deactivating a movie whose Dispatcharr `Movie` row was already gone previously silently skipped folder deletion, since the folder name was recomputed from the (now missing) DB row instead of being remembered. Activation now stores the folder name at generation time so removal always works regardless of catalog state.
- **New: automatic cleanup for movies removed from Dispatcharr** — a background check (piggybacked on the existing stall-watchdog loop, runs every ~5 minutes) now detects activated movies that have disappeared from Dispatcharr's VOD catalog (e.g. dropped by an M3U account refresh) and automatically removes their STRM folder, deletes them from Plex, and clears their activation state — previously these were never detected and lingered forever.

### v0.1.12 (2026-07-06)
- **Fixed Catalog Summary "Refresh" button** — failures in the refresh chain now surface a visible error instead of silently doing nothing; button shows a "Refreshing…" state while in flight
- **Poster load retry** — movie posters that fail to load (more common on mobile) now retry twice with backoff before falling back to "No Poster", instead of giving up on the first failure
- **Pinned Browse sub-header** — Catalog Summary and the Movies filter bar (search, provider/category/language filters, per-page, Select All/Clear/Activate/Deactivate) now stay fixed at the top while scrolling through the movie grid
- **Favicon / home-screen icon** — added `apple-touch-icon` and related meta tags so the plugin logo shows up correctly in browser tabs and when saved to a mobile home screen

### v0.1.5 (2026-06-30)
- **Auto-start on Dispatcharr restart** — `Plugin.__init__()` loads settings from the `PluginConfig` DB and starts the WSGI server automatically when Dispatcharr discovers the enabled plugin on boot. No manual "Start Server" click needed after container restart.
- **Richer status check** — "Status" button now shows `✓ Server running on port 8888 | N activated | N in catalog` when running, or a clear "not running" message with instruction when stopped.
- **Brighter text** — Dashboard `--text2` color raised from `#888` to `#b0b0b0` for consistent legibility across panel headers, metadata labels, tabs, and filter elements.

### v0.1.4 (2026-06-30)
- **TMDB language detection** — optional `tmdb_api_key` + `tmdb_read_token` plugin settings enable per-movie original language lookup via TMDB API
- **Configurable detection limit** — dropdown lets users choose scope: Activated Only / 500 / 1k / 2k / 5k / All. Default 1k ≈ 8 minutes. Safe rate: 2 req/sec vs TMDB's 40/10s limit.
- **Language filter** — multi-select "All Languages" dropdown in Browse filters the movie grid by original language
- **Language badges** — globe icon on movie cards showing detected language; amber status bar with ETA during detection; auto-refresh when complete
- **Compact Movies header** — language detection bar moved into panel body to reduce header crowding

### v0.1.3 (2026-06-29)
- **Multi-select provider + category dropdowns** — checkbox panel UI replacing single-select dropdowns
- **Per-page selector** — 300 / 800 / 1300 / 1800 / All
- **Plex real-time delete on deactivation** — `_plex_delete_movies()` queries Plex library JSON and sends `DELETE /library/metadata/{ratingKey}` immediately on deactivation, no scan wait
- **ORM filter bug fixed** — chaining `.filter()` on reverse multi-valued relation `m3u_relations` created independent JOINs allowing cross-provider movie contamination. Fixed by putting both conditions in a single `.filter()` call.

### v0.1.2 (2026-06-29)
- **Provider filtering** — dropdown filters movies and categories by M3U account
- **Select All / Clear All** — bulk select all movies matching current filters
- **Trailer previews** — YouTube trailer button on movie cards
- **Renamed** from "VOD Plex Bridge" to "VOD To Plex"
- **Plugin logo** added

### v0.1.1 (2026-06-29)
- Initial release — 302 redirect playback, web dashboard, STRM/NFO generation
- Plex Now Playing panel, health checks, category filtering
- File size estimation for rclone HEAD requests

## License

MIT
