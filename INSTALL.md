# Installation Guide — VOD To Plex

Full step-by-step setup: installing the plugin, configuring settings, exposing the port, setting up rclone on the Plex server, and creating the Plex library.

See the main [README](README.md) for features, usage, and architecture.

## Requirements

- Dispatcharr v0.24.0 or later
- Plex Media Server
- rclone installed on the Plex server (for the HTTP mount)
- The plugin's HTTP port must be accessible from the Plex server

## 1. Install the Plugin

Copy the plugin folder into Dispatcharr's plugins directory:

```
/data/plugins/vod_plex_bridge/
├── __init__.py
├── plugin.json
├── plugin.py
├── server.py
├── bridge.py
└── templates/
    └── dashboard.html
```

If using Docker, copy files into the container:
```bash
docker cp vod_plex_bridge/ <dispatcharr-container>:/data/plugins/vod_plex_bridge/
docker exec <dispatcharr-container> chown -R 1000:1000 /data/plugins/vod_plex_bridge/
```

Restart the Dispatcharr container, then enable the plugin in the Dispatcharr UI.

## 2. Configure the Plugin

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
| **TMDB API Key** | Optional — enables language detection | *(your key)* |
| **TMDB Read Token** | Optional — alternative to API key (Bearer token) | *(your token)* |

## 3. Expose the Port

The plugin's HTTP port must be exposed through Docker. If Dispatcharr runs behind a VPN container (e.g., gluetun), add the port mapping there:

```yaml
# In your gluetun or Dispatcharr docker-compose:
ports:
  - "8888:8888"  # VOD To Plex plugin
```

> **Running multiple instances?** Each instance needs a unique port. Configure both the plugin setting and the Docker port mapping to match.

## 4. Set Up rclone on the Plex Server

**Install rclone** (on the machine running Plex, not the Dispatcharr host):

```bash
# Linux/macOS
curl https://rclone.org/install.sh | sudo bash
```

For Windows, download the installer from [rclone.org/downloads](https://rclone.org/downloads/) and add it to your PATH.

Verify it installed correctly:

```bash
rclone version
```

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

## 5. Create a Plex Library

1. In Plex, add a new **Movies** library
2. Point it to the rclone mount path (e.g., `/mnt/vod-plugin`)
3. Set the agent to **Plex Movie** (or your preferred agent)
4. **Recommended**: Under Advanced, set "Library scan" to **Manual** or disable automatic media analysis to avoid unnecessary provider connections during scans
5. Enable **Allow media deletion** in Plex Settings → Troubleshooting — required for real-time Plex removal on deactivation

**Finding your Plex Library Section ID** (needed for the plugin's "Plex Library Section ID" setting, so it can trigger scans and delete items via the Plex API):

1. Get your Plex auth token (`X-Plex-Token`): open Plex Web, play any item, click **⋮** → **Get Info** → **View XML**, and copy the `X-Plex-Token=...` value from the resulting URL.
2. Visit this URL in a browser (with your token and Plex server address filled in):
   ```
   http://<plex-server-ip>:32400/library/sections?X-Plex-Token=<your-token>
   ```

   **Worked example** — if your Plex server's IP is `192.168.1.20` and your token is `abc123XYZ`, the actual URL you'd type into your browser's address bar is:
   ```
   http://192.168.1.20:32400/library/sections?X-Plex-Token=abc123XYZ
   ```
3. This returns XML listing every library. Find the `<Directory>` entry whose `title` matches the library you just created (e.g. "Stream-Movies-Bridge"), and use its `key` attribute — that number is the Plex Library Section ID. For example, in this snippet the Library Section ID is **7**:
   ```xml
   <Directory key="7" type="movie" title="Stream-Movies-Bridge" .../>
   ```

## 6. Enable the Plugin

In Dispatcharr's plugin panel, enable the plugin, then click **Start Server** in the plugin's actions panel. Open the dashboard at `http://<host-ip>:8888/`.

To verify the server is running, click **Status** in the plugin actions panel — it will show:
`✓ Server running on port 8888 | 11 activated | 38,534 in catalog`

## Next Steps

- See [README.md](README.md#usage) for how to browse, activate, and manage movies from the dashboard.
- Having trouble after setup? See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
