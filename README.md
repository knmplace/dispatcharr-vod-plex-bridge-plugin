<p align="center">
  <img src="logo.png" alt="VOD To Plex logo" width="120">
</p>

<h1 align="center">VOD To Plex</h1>
<p align="center"><strong>Dispatcharr Plugin</strong></p>

<p align="center">
A <a href="https://github.com/Dispatcharr/Dispatcharr">Dispatcharr</a> plugin that bridges VOD movies and series into Plex via rclone HTTP mount with 302 redirect streaming.
</p>

---

## 🚀 How It Works

```
Plex → rclone HTTP mount → Plugin HTTP server → 302 redirect → Dispatcharr VOD proxy → Provider
```

1. Plugin runs an HTTP server inside the Dispatcharr container
2. You activate movies and series from the web dashboard — the plugin generates STRM/NFO files
3. rclone mounts the plugin's HTTP endpoint as a FUSE filesystem on your Plex server
4. Plex scans the mount and sees your library with metadata (from NFO) and posters (from TMDB)
5. On playback, the plugin issues a 302 redirect straight to Dispatcharr's native VOD proxy
6. Dispatcharr handles the streaming connection natively — persistent connections, Range requests, session management

> ⚡ **No streaming proxy needed.** Dispatcharr's VOD proxy already does everything. The plugin just bridges the metadata and redirects.

## ✨ Features

**📚 Movies & Series**
- Full Movies and Series/Season/Episode browsing, side by side in one dashboard
- Selective activation — choose exactly what shows up in Plex, individually or in bulk
- Select All / Clear All with full filter awareness
- Reactivate — regenerate STRM/NFO in place without a destructive deactivate/reactivate cycle

**🔍 Browse & Filter**
- Multi-select provider and category filters
- Language filter, powered by optional TMDB language detection
- Search across your whole catalog
- Trailer previews (YouTube) right from the card

**🎬 Plex Integration**
- 302 redirect playback — zero overhead, Dispatcharr streams natively
- NFO metadata + TMDB posters for clean Plex matching
- Real-time Plex delete on deactivation — no waiting on a scan
- Automatic library scan trigger after activation
- Plex Now Playing panel — see active sessions from the dashboard

**🩺 Health & Reliability**
- Per-provider health dashboard with manual refresh
- Automatic cleanup of movies/episodes removed upstream by a provider
- Connection-capacity gating so bulk activation doesn't blow past your provider's concurrent-stream limit
- Catalog summary with clickable category chips

## 📋 Requirements

| Requirement | Notes |
|---|---|
| 🖥️ **Dispatcharr** | v0.24.0 or later |
| 🎞️ **Plex Media Server** | Any recent version |
| 🔌 **rclone** | Installed on the machine running Plex, for the HTTP mount |
| 🌐 **Network access** | The plugin's HTTP port must be reachable from the Plex server |
| 🐍 **`requests`** | Already bundled in Dispatcharr's container — nothing extra to install |

## 📦 Installation

👉 See **[INSTALL.md](INSTALL.md)** for the full step-by-step setup guide — installing the plugin, configuring settings, exposing the port, setting up rclone, and creating your Plex library.

## 🕹️ Usage

1. Open the dashboard at `http://<host-ip>:<port>/`
2. Switch between **Movies** and **Series** mode at the top of the dashboard
3. Optionally pick a **provider** to narrow the catalog to one M3U account, then a **category**
4. Browse, search, and filter — click **Select All** to grab everything matching your current filters
5. Click the activate icon on individual items, or use **Activate Selected** for bulk
6. The plugin generates STRM + NFO files and triggers a Plex library scan
7. Your content appears in Plex with posters and metadata — hit Play and the plugin redirects to Dispatcharr for streaming
8. **Deactivating** removes the STRM file(s) and deletes the item from Plex immediately via API

### 🌍 Language Detection (Optional)

Configure a TMDB API key or Read Access Token in plugin settings to unlock this:

1. In the Browse tab, use the **Language Detection** bar above the movie grid
2. Choose a scope from the dropdown (default: 1,000 movies, roughly 8 minutes)
3. Click **Detect Now** — runs in the background at a safe rate within TMDB's limits
4. A language badge appears on each card once detected, and the **All Languages** filter populates

## 🏗️ Architecture

### Playback Flow
```
Plex GET /vod/12345.mkv
  → rclone forwards to plugin HTTP server
  → Plugin looks up the item in Dispatcharr's DB (Django ORM)
  → Resolves the stream ID from the M3U relation
  → Returns a 302 redirect to Dispatcharr's native VOD proxy
  → Plex follows the redirect
  → Dispatcharr streams natively (persistent connection, Range support)
```

### File Size Estimation
rclone uses HEAD requests to determine file sizes. The plugin estimates size from duration, then reconciles against Plex's own confirmed size once known — so Plex never sees a mismatched or 0-byte file, which it would otherwise skip or endlessly re-analyze.

### Plex Delete on Deactivation
Deactivating an item removes its STRM/NFO files from disk, then queries Plex's library and sends a delete request for the matching entry — no scan wait required. This requires **Allow media deletion** to be enabled in Plex Settings → Troubleshooting.

### Why 302 Redirect?
Dispatcharr's VOD proxy already provides persistent streaming connections, HTTP Range support, session management, and stop detection. The 302 approach reuses that same proven code path instead of reinventing it.

## 📁 File Structure

```
vod_plex_bridge/
├── __init__.py         # Exports Plugin class
├── plugin.json         # Plugin manifest (fields, actions, metadata)
├── plugin.py           # Plugin lifecycle — auto-start, start/stop, status
├── server.py           # WSGI HTTP server (stdlib wsgiref, threaded)
├── bridge.py           # Django ORM queries, 302 URL builder, STRM/NFO gen, Plex API
├── logo.png             # Plugin logo
└── templates/
    └── dashboard.html  # Web dashboard (Browse, Series, Health tabs)
```

## ⚠️ Known Limitations

- **Connection gating is partial** — activation and playback check for free provider capacity before picking a stream, and stall detection auto-advances on repeated failures. A bulk activation plus a Plex library scan can still momentarily burst past comfortable levels before gating catches up — setting Plex library analysis to Manual is recommended for large batches.
- **Series/episode batch activation needs care** — activating a large batch of episodes at once means Plex will probe every new file on its next scan, each opening a real provider connection. Activate a season or a handful of episodes at a time for providers with low concurrent-connection limits, not an entire multi-season series in one shot.
- **Provider fallback is capacity-driven, not preference-ordered** — the plugin switches to another stream if the preferred provider has no room or stalls out, but doesn't rank providers by quality.
- **No error screens** — provider errors return HTTP status codes, not user-friendly video messages.

## 🙏 Acknowledgments

This plugin wouldn't exist without the incredible work of the Dispatcharr community. A huge thank you to the developers whose plugins paved the way and served as the foundation for this project:

- **[vod2strm](https://github.com/cmc0619/vod2strm)** by [cmc0619](https://github.com/cmc0619) — A brilliant high-performance plugin that exports VOD libraries into .strm/.nfo files. Our file size estimation approach (`duration * bitrate` with 2 GiB fallback) and 302 redirect playback pattern were directly inspired by this plugin's elegant implementation. Thank you for showing the way.
- **[VOD2MLIB](https://github.com/shedunraid/VOD2MLIB)** by [shedunraid](https://github.com/shedunraid) — The original Dispatcharr VOD-to-media-library plugin that proved the concept of scanning VOD catalogs and generating STRM files for media server import. We referenced this extensively for STRM generation patterns and plugin architecture. Thank you for pioneering this approach.
- **[VOD2MLIB fork](https://github.com/OneHotTake/VOD2MLIB)** by [OneHotTake](https://github.com/OneHotTake) — An actively maintained fork of VOD2MLIB with continued improvements and contributions to the VOD plugin ecosystem. Thank you for keeping the momentum going.
- **[Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)** — The platform this plugin runs on. Its native VOD proxy endpoint handles all the heavy lifting for streaming — persistent connections, Range requests, Redis sessions, and stop detection. None of this would be possible without the Dispatcharr team's outstanding work.
- **[PiratesIRC](https://github.com/PiratesIRC)** — Contributed the provider category-tag stripping fix (v2.4.4) that lets Plex correctly match titles that IPTV providers prefix with tags like `EN -` or `4K-EN -`.

We're grateful to everyone in the Dispatcharr community who shares their work openly — it makes projects like this possible.

## 📄 License

MIT
