# Rclone Setup: Connect Plex to VOD To Plex

This guide explains rclone without assuming you already know how it works.

## What rclone does

The plugin creates an online library. Plex expects to see a normal folder on disk. rclone is the connector between those two things:

```text
Plex library folder
        ↓
rclone mount on the Plex machine
        ↓
VOD To Plex HTTP server in Dispatcharr
        ↓
Dispatcharr's VOD proxy
```

rclone does not copy your movies to the Plex machine. It presents the plugin's online folder to Plex and passes requests back to Dispatcharr.

Install rclone on the machine where Plex runs, not inside the Dispatcharr container.

For plugin installation and settings, see [Deployment and Installation](INSTALL.md). For common failures, see [Troubleshooting](TROUBLESHOOTING.md).

## The two addresses you need

The plugin has one HTTP port, normally `8888`. The same port serves the dashboard and both library paths.

| Library | Address used by rclone |
|---|---|
| Movies | `http://<dispatcharr-host-ip>:8888/vod/` |
| TV Shows | `http://<dispatcharr-host-ip>:8888/vod-series/` |

Replace `<dispatcharr-host-ip>` with the LAN address of the machine running Dispatcharr. Do not use `localhost` or `127.0.0.1` unless Plex and Dispatcharr are on the same machine.

If you changed the plugin port, replace `8888` everywhere with your chosen port. The number must match the plugin setting and the Docker port mapping.

You can copy the exact URLs from the plugin dashboard under **Health → rclone Endpoints**.

## Before setting up rclone

Confirm these items first:

1. The plugin is enabled in Dispatcharr.
2. The plugin server is running.
3. The Docker port is published.
4. The Plex machine can reach the Dispatcharr host over the network.

From the Plex machine, open the Movies URL in a browser or use a simple request. A working endpoint should return a directory listing or an HTTP response from the plugin, not a connection-refused error.

Linux example:

```bash
curl -I http://<dispatcharr-host-ip>:8888/vod/
```

If this fails, fix the plugin, Docker port, or network connection before configuring rclone. See [Troubleshooting](TROUBLESHOOTING.md#rclone-cannot-connect).

## Install rclone

### Linux

On the Plex machine, run:

```bash
curl https://rclone.org/install.sh | sudo bash
```

Then check that it is installed:

```bash
rclone version
```

### Windows

1. Download rclone from [rclone.org/downloads](https://rclone.org/downloads/).
2. Extract `rclone.exe` to a permanent folder, such as `C:\rclone`.
3. Add that folder to the Windows PATH, or use the full path to `rclone.exe` in the commands below.
4. Install [WinFsp](https://winfsp.dev/), which allows rclone to show an online folder as a Windows drive.

Windows mounting is optional if Plex itself runs in Docker or Linux. In those cases, configure rclone in the same environment that Plex can access.

## Create the rclone connection

Run:

```bash
rclone config
```

Choose:

1. `n` for **New remote**.
2. Name it `vodplugin`.
3. Choose storage type **HTTP**.
4. Enter the Movies URL:

```text
http://<dispatcharr-host-ip>:8888/vod/
```

5. Accept the remaining defaults and save the remote.

This creates a connection named `vodplugin`. The name is only a local label; it does not need to match the plugin name.

If you want the Series library too, create a second remote, such as `vodplugin-series`, using:

```text
http://<dispatcharr-host-ip>:8888/vod-series/
```

The two remotes are separate views of the plugin. Series categories appear as folders under the Series remote.

## Test the connection before mounting

Run:

```bash
rclone lsd vodplugin:
rclone lsjson vodplugin: --max-depth 1
```

You should see folders or an empty successful listing. An empty listing can simply mean that nothing has been activated yet; an error means the connection needs attention.

## Mount on Linux

Create a folder for Plex to use:

```bash
sudo mkdir -p /mnt/vod-plugin
```

Mount the Movies remote:

```bash
sudo rclone mount vodplugin: /mnt/vod-plugin \
  --allow-other \
  --vfs-cache-mode off \
  --dir-cache-time 30s \
  --poll-interval 0 \
  --read-only
```

The folder `/mnt/vod-plugin` is now the path you give Plex for the Movies library.

For Series, create another mount folder and use the Series remote:

```bash
sudo mkdir -p /mnt/vod-plugin-series
sudo rclone mount vodplugin-series: /mnt/vod-plugin-series \
  --allow-other \
  --vfs-cache-mode off \
  --dir-cache-time 30s \
  --poll-interval 0 \
  --read-only
```

### Make the Linux mount start automatically

Create `/etc/systemd/system/rclone-vodplugin.service`:

```ini
[Unit]
Description=VOD To Plex rclone mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/rclone mount vodplugin: /mnt/vod-plugin --allow-other --vfs-cache-mode off --dir-cache-time 30s --poll-interval 0 --read-only
ExecStop=/bin/fusermount -uz /mnt/vod-plugin
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rclone-vodplugin
```

If you also mount Series, create a second service with `vodplugin-series` and `/mnt/vod-plugin-series` substituted in the two relevant lines.

## Mount on Windows

Open PowerShell as Administrator and use a drive letter that is not already in use:

```powershell
rclone mount vodplugin: X: --network-mode --read-only
```

Keep that window open while the mount is in use. For a permanent Windows service, use a service manager such as WinSW or NSSM and configure it to run the same command after the network starts.

The Plex library path is then `X:\`. If Plex runs as a Windows service, make sure the service account can see the mounted drive; mapped drives created only in your user session may not be visible to the Plex service.

## Create Plex libraries

### Movies

1. In Plex, choose **Add Library**.
2. Choose **Movies**.
3. Add `/mnt/vod-plugin` on Linux or `X:\` on Windows.
4. Save the library.

### TV Shows

1. In Plex, choose **Add Library**.
2. Choose **TV Shows**.
3. Add `/mnt/vod-plugin-series` or the Windows Series drive.
4. Save the library.

Do not add `/data/plugin-strm` from the Dispatcharr container to Plex. That is the plugin's private storage location. The normal Plex path is the rclone mount.

For large activations, consider setting Plex library scanning and automatic analysis to manual. Plex may inspect every new STRM file, and each inspection can briefly use a provider connection.

## Find the Plex library section ID

The plugin uses the library section ID to request scans and remove titles from Plex.

1. Obtain your Plex token. In Plex Web, open an item's menu, choose **Get Info**, then **View XML**. The token appears in the URL as `X-Plex-Token=...`.
2. Open this URL in a browser, replacing the placeholders:

```text
http://<plex-server-ip>:32400/library/sections?X-Plex-Token=<your-token>
```

3. Find the `<Directory>` entry for the library you created.
4. Copy its `key` value. For example, `key="7"` means the section ID is `7`.
5. Enter that number in the plugin's **Plex Library Section ID** setting.

Keep the token private and do not commit it to a file or repository.

## Confirm everything is connected

Use this order when testing:

1. Open `http://<dispatcharr-host-ip>:8888/` and confirm the dashboard loads.
2. Open the plugin's **Health → rclone Endpoints** panel.
3. Run `rclone lsd vodplugin:` on the Plex machine.
4. Confirm the mounted folder is visible to the Plex service.
5. Activate one movie in the plugin.
6. Scan the Plex Movies library.
7. Confirm the movie appears and starts playing.

For Series, repeat the test with the Series remote and TV Shows library.

If any step fails, start with [Troubleshooting](TROUBLESHOOTING.md).
