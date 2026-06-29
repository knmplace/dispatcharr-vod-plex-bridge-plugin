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
- **Provider & Category Filtering** — Filter movies by provider, then by category. Selecting a provider dynamically shows only that provider's categories
- **Selective Activation** — Choose which movies appear in Plex, individually or in bulk
- **Select All / Clear All** — Bulk select all movies matching current search, provider, and category filters
- **Trailer Previews** — Watch YouTube trailers directly from movie cards (when available from provider metadata)
- **302 Redirect Playback** — Zero overhead, Dispatcharr handles streaming natively
- **NFO Metadata** — Title, year, rating, TMDB ID, genre, plot for Plex matching
- **TMDB Posters** — Movie artwork via TMDB poster URLs in NFO files
- **Plex Now Playing** — Monitor active Plex sessions from the dashboard (bridge vs local content)
- **Health Checks** — Dispatcharr DB and Plex connectivity status
- **Catalog Summary** — Category chips with movie counts, quick-filter on click
- **Auto Plex Scan** — Triggers library scan after activation/deactivation
- **Zero Dependencies** — Uses Python stdlib only (no pip installs needed)

## Requirements

- Dispatcharr v0.24.0 or later
- Plex Media Server
- rclone installed on the Plex server (for the HTTP mount)
- The plugin's HTTP port must be accessible from the Plex server

## Installation

### 1. Install the Plugin

Copy the plugin folder into Dispatcharr's plugins directory:

```
/data/plugins/vod_plex_bridge/
├── __init__.py
├── plugin.json
├── plugin.py
├── server.py
├── bridge.py
├── streaming.py
└── templates/
    └── dashboard.html
```

If using Docker, copy files into the container:
```bash
docker cp vod_plex_bridge/ <dispatcharr-container>:/data/plugins/vod_plex_bridge/
docker exec <dispatcharr-container> chown -R 1000:1000 /data/plugins/vod_plex_bridge/
```

Restart the Dispatcharr container, then enable the plugin in the Dispatcharr UI.

### 2. Configure the Plugin

In Dispatcharr's plugin settings, configure:

| Setting | Description | Example |
|---------|-------------|---------|
| **Dispatcharr URL** | LAN URL of Dispatcharr reachable from Plex | `http://192.168.1.100:9191` |
| **Dashboard Port** | HTTP port for the dashboard and VOD endpoint | `8888` |
| **Dashboard Host IP** | LAN IP of the Docker host | `192.168.1.100` |
| **Plex URL** | Full URL of your Plex server | `http://192.168.1.200:32400` |
| **Plex Token** | X-Plex-Token for Plex API access | *(your token)* |
| **Plex Library Section** | Library section ID for VOD movies | `7` |
| **STRM Output Dir** | Where STRM/NFO files are written | `/data/plugin-strm` |

### 3. Expose the Port

The plugin's HTTP port must be exposed through Docker. If Dispatcharr runs behind a VPN container (e.g., gluetun), add the port mapping there:

```yaml
# In your gluetun or Dispatcharr docker-compose:
ports:
  - "8888:8888"  # VOD To Plex plugin
```

> **Running multiple instances?** Each instance needs a unique port. If you run this plugin alongside other HTTP-serving plugins (like VODFS on port 8888), change one of them to a different port (e.g., 8889, 8890). Configure both the plugin setting and the Docker port mapping to match.

### 4. Set Up rclone on the Plex Server

Create an rclone remote pointing to the plugin's `/vod/` endpoint:

```ini
# Add to /root/.config/rclone/rclone.conf on the Plex server:
[vodplugin]
type = http
url = http://<dispatcharr-host-ip>:8888/vod/
```

Mount it as a FUSE filesystem:

```bash
mkdir -p /mnt/vod-plugin
rclone mount vodplugin: /mnt/vod-plugin \
  --allow-other \
  --vfs-cache-mode off \
  --dir-cache-time 30s \
  --poll-interval 0 \
  --read-only
```

For persistent mounts, create a systemd service:

```ini
# /etc/systemd/system/rclone-vodplugin.service
[Unit]
Description=rclone VOD Plugin mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/rclone mount vodplugin: /mnt/vod-plugin \
  --allow-other \
  --vfs-cache-mode off \
  --dir-cache-time 30s \
  --poll-interval 0 \
  --read-only
ExecStop=/bin/fusermount -uz /mnt/vod-plugin
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now rclone-vodplugin
```

### 5. Create a Plex Library

1. In Plex, add a new **Movies** library
2. Point it to the rclone mount path (e.g., `/mnt/vod-plugin`)
3. Set the agent to **Plex Movie** (or your preferred agent)
4. **Recommended**: Under Advanced, set "Library scan" to **Manual** or disable automatic media analysis to avoid unnecessary provider connections during scans

### 6. Start the Server

In Dispatcharr's plugin panel, click **Start Server**. Open the dashboard at `http://<host-ip>:8888/`.

> **NOTE: On container restarts, the HTTP server must be manually restarted by clicking "Start Server" in the Dispatcharr plugin panel. Auto-start on reboot is planned for a future release.**

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

### Why 302 Redirect?
Dispatcharr's `/proxy/vod/` endpoint already provides:
- Persistent streaming connections
- HTTP Range request support
- Redis-based session management
- Automatic stop detection

There's no need to duplicate this with a streaming proxy. The 302 approach is the same code path Dispatcharr uses for browser-based playback, which is proven stable for full-length movies.

## File Structure

```
vod_plex_bridge/
├── __init__.py         # Exports Plugin class
├── plugin.json         # Plugin manifest (fields, actions, metadata)
├── plugin.py           # Plugin lifecycle — start/stop, server management
├── server.py           # WSGI HTTP server (stdlib wsgiref, threaded)
├── bridge.py           # Django ORM queries, 302 URL builder, STRM/NFO generation
├── streaming.py        # Stub (302 redirect replaces streaming proxy)
├── logo.jpg            # Plugin logo for Dispatcharr UI
└── templates/
    └── dashboard.html  # Web dashboard (Browse, Streams, Health tabs)
```

## Known Limitations

- **Manual server restart required** after Dispatcharr container restarts (auto-start planned)
- **No connection gating** — bulk activation + Plex scan can trigger many provider connections. Recommend setting Plex library analysis to Manual.
- **Movies only** — series support is planned
- **No provider fallback** — uses the first available stream per movie
- **No error screens** — provider errors return HTTP status codes, not user-friendly video messages

## Changelog

### v0.1.2 (2026-06-29)
- **Provider filtering** — new dropdown filters movies and categories by M3U account. Only providers with VOD movies are shown.
- **Select All / Clear All** — bulk select all movies matching current search, provider, and category filters
- **Trailer previews** — YouTube trailer button on movie cards (from provider custom_properties)
- **Renamed** from "VOD Plex Bridge" to "VOD To Plex"
- **Plugin logo** added for Dispatcharr UI

### v0.1.1 (2026-06-29)
- Initial release — 302 redirect playback, web dashboard, STRM/NFO generation
- Plex Now Playing panel, health checks, category filtering
- File size estimation for rclone HEAD requests

## License

MIT
