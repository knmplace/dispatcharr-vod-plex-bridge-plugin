# VOD To Plex: Deployment and Installation

This guide explains how to install the VOD To Plex plugin in Dispatcharr, configure it, and connect it to Plex.

If you are new to this setup, follow the guides in this order:

1. This deployment guide: install the plugin and configure its settings.
2. [Rclone Setup](RCLONE_SETUP.md): make the plugin's library visible to Plex.
3. [Troubleshooting](TROUBLESHOOTING.md): fix common connection and playback problems.

For a short overview of the project, see the [README](README.md).

## What you need

| Item | Why it is needed |
|---|---|
| Dispatcharr 0.24.0 or newer | Runs the plugin and provides the VOD streams |
| Plex Media Server | Displays the activated movies and series |
| rclone on the Plex machine | Lets Plex see the plugin's online library as a normal folder |
| Network access between Plex and Dispatcharr | Plex must be able to reach the plugin's port |

You do not need to install Python packages for this plugin. Dispatcharr supplies the Python environment and the plugin uses Dispatcharr's own VOD proxy for playback.

## Step 1: Prepare the release ZIP

When importing through the Dispatcharr GUI, use the release ZIP. The ZIP must contain one plugin folder at its top level:

```text
vod-to-plex-plugin-vX.Y.Z.zip
└── vod_plex_bridge/
    ├── plugin.json
    ├── plugin.py
    ├── bridge.py
    ├── server.py
    ├── streaming.py
    ├── logo.png
    └── templates/
        └── dashboard.html
```

The important details are:

- The top-level folder must be named `vod_plex_bridge`.
- `templates/dashboard.html` must remain inside the `templates` folder.
- ZIP paths must use forward slashes (`/`), including on Windows.
- Do not upload a ZIP containing the repository folder itself as the extra outer folder.

For maintainers packaging a release, see the [release packaging notes](README.md#release-packaging).

## Step 2: Import the plugin in Dispatcharr

1. Open Dispatcharr in your browser.
2. Open the **Plugins** page.
3. Choose **Find Plugins** or **Import Plugin**.
4. Select the release ZIP.
5. After the import finishes, find **VOD To Plex** in the plugin list.
6. Enable the plugin.

If you are upgrading from a very old version and see two VOD To Plex cards, remove the old unmanaged card first, then import the current ZIP again.

The normal GUI import installs the plugin under:

```text
/data/plugins/vod_plex_bridge/
```

Manual copying into the container is an administrator recovery method, not the normal installation path.

## Step 3: Configure the plugin

Open the VOD To Plex settings in Dispatcharr. Save the settings before starting the server.

### Required connection settings

| Setting | What to enter | Example |
|---|---|---|
| **Dispatcharr URL** | The Dispatcharr address that the Plex machine can reach. Include the port. Do not use `127.0.0.1` unless Plex and Dispatcharr are on the same machine. | `http://192.168.1.100:9191` |
| **Dashboard / Streaming Port** | The port used by the plugin's dashboard and library endpoints. Keep `8888` unless it is already in use. | `8888` |
| **Dashboard Host IP** | The LAN address of the machine running Dispatcharr. This address is written into the STRM files so Plex knows where to connect. | `192.168.1.100` |
| **STRM Output Directory** | Where Dispatcharr stores the generated STRM and NFO files. The default is recommended. Plex does not need direct filesystem access to this directory. | `/data/plugin-strm` |

### Plex settings

| Setting | What to enter |
|---|---|
| **Plex URL** | The address of Plex as seen from the Dispatcharr container, such as `http://192.168.1.200:32400`. |
| **Plex Token** | Your Plex authentication token. Keep this private. |
| **Plex Library Section ID** | The number belonging to the VOD Movies library. The [Plex library section instructions](RCLONE_SETUP.md#find-the-plex-library-section-id) explain how to find it. |

Plex settings are needed for automatic library scans, Plex confirmation, now-playing information, and deletion when content is deactivated.

### Optional settings

| Setting | Recommended starting point |
|---|---|
| **Auto-Start Server** | Enable this if the plugin should start after Dispatcharr reloads or restarts. |
| **Debug: Log Connections & Server Errors** | Leave off normally. Enable only while troubleshooting. |
| **Max Concurrent HEAD Probes** | Leave at `5` unless your provider allows fewer or more simultaneous connections. |
| **Max Concurrent Connections Per Title** | Leave at `2`, or lower it when a provider has a strict connection limit. |
| **Removed-Movie Check Interval** | The default `300` seconds is suitable for most installations. |
| **Auto Stream Refresh Interval** | The default `7` days is suitable for most installations. Set to `0` to disable it. |
| **Untracked Orphan Sweep: Dry Run Only** | Keep enabled until you have reviewed the dry-run log. Disable only when you want automatic cleanup of confirmed untracked content. |
| **Hide Adult Categories** | Enabled by default. Disable only if those categories should appear in the dashboard. |
| **TMDB API Key / Read Access Token** | Optional. Configure one if you want language detection and TMDB-assisted metadata. The Read Access Token takes priority if both are entered. |

Series destination folders are configured from the dashboard under the series settings. Each category can have its own folder and Plex TV library section.

## Step 4: Make the plugin port reachable

The plugin listens on port `8888` by default. The Plex machine must be able to reach that port on the Dispatcharr host.

If Dispatcharr runs in Docker, publish the same port on the host:

```yaml
ports:
  - "8888:8888"
```

If Dispatcharr uses a VPN container such as gluetun, publish the port on the container that actually owns Dispatcharr's network connection.

If you choose another plugin port, use that same number in all three places:

1. The plugin's **Dashboard / Streaming Port** setting.
2. The Docker port mapping.
3. The rclone URL in [Rclone Setup](RCLONE_SETUP.md).

## Step 5: Start the plugin server

1. In the Dispatcharr plugin card, click **Start Server**.
2. Click **Status** and confirm that the server is running.
3. Open the dashboard using:

```text
http://<dispatcharr-host-ip>:8888/
```

The dashboard also shows the exact Movies and Series endpoint URLs under **Health → rclone Endpoints**.

## Step 6: Connect Plex through rclone

Complete the separate [Rclone Setup guide](RCLONE_SETUP.md). It explains the connection in plain language and gives instructions for Linux, Windows, and Docker-based Plex installations.

Do not add the plugin's `/data/plugin-strm` directory directly to Plex unless you have deliberately created a separate shared filesystem. The normal setup is:

```text
Plex → rclone mount → plugin HTTP server → Dispatcharr VOD proxy
```

## Step 7: Activate content

1. Open the plugin dashboard.
2. Browse the **Movies** or **Series** tab.
3. Select one or more items.
4. Click **Activate**.
5. Wait for the STRM/NFO files and Plex scan to complete.
6. Confirm that the title appears in the matching Plex library.

For providers with strict connection limits, activate one season or a small group first. Plex may inspect new files during a scan, and each inspection can briefly use a provider connection.

## Updating the plugin

1. Download the newer release ZIP.
2. Confirm it has the same `vod_plex_bridge/` wrapper described above.
3. Import it through Dispatcharr's plugin interface.
4. If Dispatcharr does not reload the new code, restart the Dispatcharr container through your normal container-management tool.
5. Confirm the displayed plugin version and click **Status**.

For problems after an update, use the [Troubleshooting guide](TROUBLESHOOTING.md).

## Release packaging

This section is for the person creating release ZIPs.

The release must be built from a staging folder containing the plugin files under `vod_plex_bridge/`. Do not use PowerShell `Compress-Archive` for this project: it can create backslash-separated ZIP paths, which Linux-based Dispatcharr imports may not treat as real folders.

After creating the ZIP, verify that:

- `vod_plex_bridge/plugin.json` exists;
- `vod_plex_bridge/templates/dashboard.html` exists as a nested file;
- every ZIP path uses `/` separators; and
- extracting the ZIP produces exactly one top-level `vod_plex_bridge` folder.

## Next steps

- [Rclone Setup](RCLONE_SETUP.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [README](README.md)
