# AI-Assisted Setup — VOD To Plex

This file is a ready-made prompt for Claude (or another capable AI assistant). If you'd rather
have an AI walk you through installing this plugin and setting up rclone instead of following
[INSTALL.md](INSTALL.md) by hand, copy everything in the code block below into a new chat with
your assistant and follow along.

The assistant will ask you questions about your own setup first — it has no knowledge of your
network, your Plex server, or your Dispatcharr instance until you tell it. Don't paste real
credentials into the chat unless you're using a private/local assistant session you trust; where
possible, prefer having the assistant hand you the exact command and running it yourself, or
redacting tokens after confirming a command worked.

---

## Copy everything below this line into your AI assistant

```
You are helping me install and configure the "VOD To Plex" Dispatcharr plugin, and (if I want
your help) set up rclone so Plex can see the plugin's VOD catalog as a browsable library. You
have no prior knowledge of my network — do not assume any IP addresses, hostnames, ports, or
credentials. Ask me for anything you need before giving instructions.

## What this plugin does
It's a Dispatcharr plugin (runs inside the Dispatcharr container/process) that:
1. Reads VOD movies already in Dispatcharr's catalog
2. Generates a browsable HTTP directory listing plus 302-redirect streaming, so an rclone HTTP
   mount can expose it as a folder of "files" for Plex/Jellyfin
3. Provides a small web dashboard (default port 8888) for browsing the catalog and
   activating/deactivating which movies actually show up in Plex

## Step 1 — Learn my environment
Before giving me any commands, ask me these questions one at a time (or as a short batch if I
prefer) and wait for real answers — do not fill in example values and proceed as if they were
real:
1. Where does Dispatcharr run? (bare metal, Docker, Docker Compose, a VM/LXC, behind a VPN
   container like gluetun, etc.) What's its LAN IP or hostname, and what port is its web UI on?
2. Where does Plex run, relative to Dispatcharr? Same machine? Different machine on the same
   LAN? A different LAN/remote site? What's its LAN IP or hostname?
3. What OS is the Plex host running (Linux, Windows, macOS, a NAS OS like Synology/Unraid)? This
   determines how rclone gets installed and whether FUSE mounts are even available (Windows
   needs WinFsp instead of FUSE, Synology/Unraid often need a package or Docker sidecar).
4. Is Dispatcharr's container network reachable directly from the Plex host, or is there a
   firewall/VPN/VLAN boundary I need to know about before suggesting a port mapping?
5. Do I already have a Dispatcharr VOD catalog populated (movies visible in Dispatcharr's own VOD
   section), or does that still need to be set up first? This plugin does not create VOD content
   — it only bridges an existing Dispatcharr VOD catalog into Plex.
6. Am I comfortable running commands directly against my Plex/Dispatcharr hosts through you (if
   you have shell/SSH access in this session), or would I prefer you just hand me the exact
   commands to run myself?

Do not proceed past this point until you have real answers to at least 1–4. If I don't know an
answer, help me find it (e.g., "check your Docker host's `ip a` output" or "check your router's
DHCP client list") rather than guessing.

## Step 2 — Install the plugin
Once you know where Dispatcharr runs, walk me through:
1. Getting the plugin files onto the Dispatcharr host. The plugin repo is:
   https://github.com/knmplace/dispatcharr-vod-plex-bridge-plugin
   Latest release zip is under the repo's "Releases" — download it and unzip it.
2. Copying it into Dispatcharr's plugin directory. This path depends on how Dispatcharr is
   deployed:
   - Docker: the plugins directory is typically bind-mounted from the host to
     `/data/plugins/` inside the container — ask me to confirm my actual mount, don't assume.
   - The final layout inside `/data/plugins/` must be:
     ```
     vod_plex_bridge/
     ├── __init__.py
     ├── plugin.json
     ├── plugin.py
     ├── server.py
     ├── bridge.py
     └── templates/
         └── dashboard.html
     ```
   - **Important gotcha to warn me about**: if I unzip the release using a tool that flattens
     subfolders (this has happened before with Windows' `Compress-Archive`/some GUI zip tools
     writing backslash path separators), the `templates/` folder can come out as a flat file
     literally named `templates\dashboard.html` instead of a real subfolder, which breaks the
     plugin with a "Dashboard template not found" error. Verify after extracting that
     `templates/dashboard.html` is a real file inside a real `templates` directory, not a
     backslash-named flat file.
   - If Docker: `docker cp` the folder in, then
     `docker exec <container> chown -R 1000:1000 /data/plugins/vod_plex_bridge/`
   - Restart the Dispatcharr container/process afterward — Python module caching means a plain
     "reload plugins" is not enough after copying in new files.
3. Enabling the plugin in Dispatcharr's UI (Settings → Plugins, or wherever this Dispatcharr
   version exposes it).

## Step 3 — Configure the plugin
Help me fill in the plugin's settings fields, asking me for each value rather than assuming one:

| Setting | What it means | How to find it |
|---|---|---|
| Dispatcharr URL | LAN URL of Dispatcharr, reachable *from the Plex host* | Usually `http://<dispatcharr-lan-ip>:<port>` — confirm the port Dispatcharr's web UI actually listens on |
| Dashboard Port | Port the plugin's own small web server listens on | Default 8888; only change if that port is taken |
| Dashboard Host IP | LAN IP of the Docker host running Dispatcharr | Ask me, or help me find it via `ip a` / `ipconfig` |
| Plex URL | Full URL of the Plex server | `http://<plex-lan-ip>:32400` |
| Plex Token | Plex API auth token | Walk me through: open Plex Web, play any item, click the "..." menu → Get Info → View XML, and read the `X-Plex-Token=` value out of the resulting URL. Tell me to treat this like a password. |
| Plex Library Section | The numeric ID of the Plex library this plugin will manage | See Step 5 below — this doesn't exist until I've created the library |
| STRM Output Dir | Where the plugin writes .strm/.nfo files, if using STRM mode instead of the HTTP+rclone mode | Ask which mode I actually want before filling this in |
| TMDB API Key / Read Token | Optional, only needed for language-detection feature | Skip if I don't want this feature yet |

Ask me to confirm each value back to you before telling me to save the settings, since a wrong
IP or port here is the single most common setup failure.

## Step 4 — Expose the port
If Dispatcharr runs in Docker, the plugin's dashboard port (8888 by default) needs a Docker port
mapping to be reachable from the Plex host. If Dispatcharr runs behind a VPN sidecar container
(e.g. gluetun) using `network_mode: service:<vpn-container>`, the port mapping has to be added to
the VPN container's compose file, not Dispatcharr's own — ask me which is the case before giving
a docker-compose snippet, and only give me a real snippet after I've told you my actual container
names.

## Step 5 — Set up rclone on the Plex host (only if I want your help with this)
This step mounts the plugin's `/vod/` HTTP endpoint as a browsable folder on the Plex host, using
rclone's HTTP backend + FUSE (or WinFsp on Windows). Before giving me commands:
- Confirm the Plex host's OS again (Linux/macOS/Windows/NAS) — the install method differs
  completely.
- Confirm whether I already have rclone installed (`rclone version`).
- On Linux/macOS:
  ```
  curl https://rclone.org/install.sh | sudo bash
  ```
- On Windows: point me to https://rclone.org/downloads/ and WinFsp
  (https://github.com/winfsp/winfsp), since a native FUSE mount isn't available there.
- On a NAS (Synology/Unraid/etc.): ask which one specifically — package availability and mount
  permissions vary a lot, don't give generic Linux instructions and assume they'll work.

Once rclone itself is installed, help me create a remote. The config should look like this
(explain each placeholder, don't just dump it):
```ini
[vodplugin]
type = http
url = http://<dispatcharr-or-docker-host-lan-ip>:<dashboard-port>/vod/
```

Then help me mount it — ask whether I want a one-off test mount first (safer, easy to unmount) or
to go straight to a persistent systemd service:

Test mount (Linux):
```bash
mkdir -p /mnt/vod-plugin
rclone mount vodplugin: /mnt/vod-plugin \
  --allow-other \
  --vfs-cache-mode off \
  --dir-cache-time 30s \
  --poll-interval 0 \
  --read-only
```

Persistent systemd service (only after the test mount above is confirmed working):
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

**Validate the mount before moving on** — walk me through checking:
- `ls /mnt/vod-plugin` (or the mount path I chose) actually lists files, not an empty/error
  directory
- `mount | grep vod-plugin` shows it mounted
- If empty or erroring: check that the plugin dashboard's "Status" action shows the server
  actually running, that at least one movie is activated in the plugin dashboard (an inactive
  catalog will look like an empty mount), and that the Dispatcharr host's dashboard port is
  reachable from the Plex host at all (`curl -I http://<host>:<port>/vod/` from the Plex host).

## Step 6 — Create the Plex library and find its Section ID
1. In Plex, add a new Movies library pointed at the rclone mount path (e.g. `/mnt/vod-plugin`).
2. Recommend I set Library Scan to Manual (or disable automatic media analysis) under Advanced,
   to avoid the VOD provider getting hit by background scans.
3. Recommend I enable "Allow media deletion" under Plex Settings → Troubleshooting, since the
   plugin needs this to remove deactivated movies from Plex in real time.
4. Help me find the numeric Library Section ID (needed for the plugin's own setting) using my
   real Plex IP and the token from Step 3:
   ```
   http://<plex-lan-ip>:32400/library/sections?X-Plex-Token=<my-real-token>
   ```
   This returns XML. Help me find the `<Directory>` entry whose `title` matches the library I
   just created, and read its `key` attribute — that number goes into the plugin's "Plex Library
   Section" setting.

## Step 7 — Final validation
Before declaring this done, walk me through:
1. Confirming the plugin dashboard loads at `http://<dispatcharr-lan-ip>:<dashboard-port>/`
2. Activating at least one movie from the dashboard
3. Confirming that movie now appears as a file inside the rclone mount path
4. Triggering a Plex library scan (or waiting for the next automatic one, if I didn't set it to
   manual) and confirming the movie shows up in Plex
5. Playing the movie in Plex and confirming it actually streams

If any step fails, ask me what specifically happened (exact error text, what the dashboard
Status action shows, whether the mount is empty vs missing entirely) rather than guessing at a
fix — this setup has several places where a wrong IP, port, or missing port-mapping is the real
cause, and treating symptoms without confirming which layer failed just wastes time.

Throughout all of this: never invent an IP address, port, token, or file path on my behalf and
present it as if it were mine. If I haven't told you a real value yet, ask, or clearly mark
anything you write as a placeholder I need to replace.
```

---

## Notes for whoever pastes this in

- Replace nothing yourself before pasting — the whole point is that the assistant asks *you* for
  your real values instead of you having to know which placeholder means what up front.
- If your assistant has direct shell/SSH access to your hosts in that session, it can run the
  commands for you once you confirm the values; otherwise it will just hand you copy-pasteable
  commands.
- This is the same style of walkthrough used to build and validate this plugin's own install
  process — it isn't a generic rclone tutorial, it's specific to what this plugin actually needs.
