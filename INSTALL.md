<p align="center">
  <img src="logo.png" alt="VOD To Plex logo" width="100">
</p>

<h1 align="center">Installation Guide</h1>
<p align="center">VOD To Plex — Dispatcharr Plugin</p>

Step-by-step setup: installing the plugin, configuring settings, exposing the port, setting up rclone on the Plex server, and creating your Plex library.

See the main [README](README.md) for features, usage, and architecture.

## 📋 Before You Start

| Requirement | Notes |
|---|---|
| 🖥️ **Dispatcharr** | v0.24.0 or later |
| 🎞️ **Plex Media Server** | Any recent version |
| 🔌 **rclone** | Installed on the machine running **Plex** (not Dispatcharr) |
| 🌐 **Network access** | The plugin's HTTP port must be reachable from the Plex server |

---

## Step 1 — Install the Plugin

Copy the plugin folder into Dispatcharr's plugins directory so it looks like this:

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

**If running Docker**, copy the files into the container:
```bash
docker cp vod_plex_bridge/ <dispatcharr-container>:/data/plugins/vod_plex_bridge/
docker exec <dispatcharr-container> chown -R 1000:1000 /data/plugins/vod_plex_bridge/
```

Restart the Dispatcharr container, then enable the plugin from the Dispatcharr UI.

> ⬆️ **Upgrading from v2.0.x or earlier?** If you see duplicate "VOD To Plex" cards after upgrading, uninstall the old card first: in Dispatcharr's **Plugins** panel, find the old card (may show as "UNMANAGED"), click **Uninstall**, then go to **Find Plugins**, search "VOD To Plex", and **Import Plugin** with the latest release zip. Releases from v2.2.0 onward package correctly, so this is a one-time fix.

---

## Step 2 — Configure the Plugin

Open the plugin's settings in Dispatcharr and fill these in:

| Setting | Description | Example |
|---|---|---|
| **Dispatcharr URL** | LAN URL of Dispatcharr, reachable from Plex | `http://192.168.1.100:9191` |
| **Dashboard Port** | HTTP port for the dashboard and VOD endpoint | `8888` |
| **Dashboard Host IP** | LAN IP of the Docker host | `192.168.1.100` |
| **Plex URL** | Full URL of your Plex server | `http://192.168.1.200:32400` |
| **Plex Token** | X-Plex-Token for Plex API access | *(your token)* |
| **Plex Library Section** | Library section ID for VOD movies | `7` |
| **STRM Output Dir** | Where STRM/NFO files are written | `/data/plugin-strm` |
| **TMDB API Key** | Optional — enables language detection | *(your key)* |
| **TMDB Read Token** | Optional — alternative to API key | *(your token)* |

After saving and starting the server, open the dashboard and go to **Health → rclone Endpoints** to see the exact URLs to use:

```text
Movies: http://<dashboard-host>:<dashboard-port>/vod/
Series: http://<dashboard-host>:<dashboard-port>/vod-series/
```

Use the Movies URL for a Plex **Movies** library and the Series URL for a separate Plex **TV Shows** library. Series categories are subfolders under `/vod-series/`, not separate endpoints.

---

## Step 3 — Expose the Port

The plugin's HTTP port needs a Docker port mapping. If Dispatcharr runs behind a VPN container (e.g. gluetun), add the mapping there instead:

```yaml
ports:
  - "8888:8888"  # VOD To Plex plugin
```

> 💡 Running multiple instances? Each one needs its own unique port — update both the plugin setting and the Docker mapping to match.

---

## Step 4 — Set Up rclone on the Plex Server

**4a. Install rclone** on the machine running Plex:

```bash
# Linux/macOS
curl https://rclone.org/install.sh | sudo bash
```

Windows: download from [rclone.org/downloads](https://rclone.org/downloads/) and add it to your PATH.

Verify:
```bash
rclone version
```

**4b. Create an rclone remote** pointing at the plugin:

```ini
# Add to /root/.config/rclone/rclone.conf on the Plex server:
[vodplugin]
type = http
url = http://<dispatcharr-host-ip>:8888/vod/
```

**4c. Mount it** as a FUSE filesystem:

```bash
mkdir -p /mnt/vod-plugin
rclone mount vodplugin: /mnt/vod-plugin \
  --allow-other \
  --vfs-cache-mode off \
  --dir-cache-time 30s \
  --poll-interval 0 \
  --read-only
```

**4d. (Recommended) Make it persistent** with a systemd service:

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

---

## Step 5 — Create a Plex Library

1. In Plex, add a new **Movies** library
2. Point it to the rclone mount path (e.g. `/mnt/vod-plugin`)
3. Set the agent to **Plex Movie** (or your preferred agent)
4. 🎯 **Recommended**: under Advanced, set library scan to **Manual** or disable automatic media analysis, to avoid unnecessary provider connections during scans
5. ✅ Enable **Allow media deletion** in Plex Settings → Troubleshooting — required for real-time removal on deactivation

**Finding your Plex Library Section ID** (needed for the plugin's settings, so it can trigger scans and delete items):

1. Get your Plex token (`X-Plex-Token`): open Plex Web, play any item, click **⋮** → **Get Info** → **View XML**, and copy the `X-Plex-Token=...` value from the resulting URL.
2. Visit this URL in a browser (fill in your own server IP and token):
   ```
   http://<plex-server-ip>:32400/library/sections?X-Plex-Token=<your-token>
   ```
   **Example**: server IP `192.168.1.20`, token `abc123XYZ` →
   ```
   http://192.168.1.20:32400/library/sections?X-Plex-Token=abc123XYZ
   ```
3. This returns XML listing every library. Find the `<Directory>` entry whose `title` matches your new library, and use its `key` attribute — that's the Section ID:
   ```xml
   <Directory key="7" type="movie" title="Stream-Movies-Bridge" .../>
   ```

---

## Step 6 — Enable the Plugin

In Dispatcharr's plugin panel, enable the plugin, then click **Start Server**. Open the dashboard at `http://<host-ip>:8888/`.

To confirm it's running, click **Status** — you should see:
```
✓ Server running on port 8888 | 11 activated | 38,534 in catalog
```

---

## ✅ Next Steps

- See [README.md](README.md#usage) for how to browse, activate, and manage your library from the dashboard.
- Having trouble? See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
