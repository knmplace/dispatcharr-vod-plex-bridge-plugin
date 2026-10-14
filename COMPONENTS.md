# Components

This document lists the systems that work together in a VOD To Plex installation.

For setup instructions, see [Deployment and Installation](INSTALL.md) and [Rclone Setup](RCLONE_SETUP.md). For faults between these components, see [Troubleshooting](TROUBLESHOOTING.md).

## Components

| Component | Tier | Purpose |
|-----------|------|---------|
| VOD To Plex plugin (`bridge.py` / `server.py` / `plugin.py`) | home-lab | Core plugin logic: Dispatcharr catalog access, HTTP server, STRM/NFO generation, and Plex API integration |
| Web dashboard (`templates/dashboard.html`) | home-lab | Browse and activate content, health checks, maintenance, and now-playing information |
| rclone HTTP mount (Plex machine) | home-lab | Exposes the plugin's `/vod/` and `/vod-series/` endpoints as folders for Plex to scan |
| Dispatcharr database | home-lab | Owned by Dispatcharr; the plugin reads its VOD catalog through Dispatcharr's Django environment |
| Plex Media Server | home-lab | Scans the generated library and plays content through the plugin redirect |

## Connection flow

```text
Plex → rclone mount → VOD To Plex HTTP server → Dispatcharr VOD proxy → provider
```

The plugin's private STRM output directory is inside the Dispatcharr environment. Plex normally does not access that directory directly; it sees the HTTP endpoints through rclone.
