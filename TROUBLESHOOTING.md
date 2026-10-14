# Troubleshooting: VOD To Plex

Start with [Deployment and Installation](INSTALL.md) for setup, or [Rclone Setup](RCLONE_SETUP.md) for the Plex connection. The [README](README.md) gives the short project overview.

## Activated a movie, STRM files exist, but Plex does not show it

1. **Check Plex's library path is the rclone mount, not the plugin's raw output directory.** The plugin writes STRM/NFO files inside its own container/host filesystem (`strm_output_dir`). Plex normally sees them through the rclone mount (`/mnt/vod-plugin` or similar) described in [Rclone Setup](RCLONE_SETUP.md). Do not point Plex directly at the plugin's private output directory.
2. **Confirm the rclone mount itself sees the file** before blaming Plex: run `ls /mnt/vod-plugin` (or your mount path) on the Plex server. If the movie's folder is not there, the problem is the rclone mount or URL, not Plex. Check the remote URL and confirm that the mount service is running (`systemctl status rclone-vodplugin` on Linux).
3. **Trigger a manual scan.** New STRM files are not visible until Plex scans the library. Use the plugin's **Scan Plex** action, or manually choose **Scan Library Files** from the Plex library menu.
4. **Verify `plex_url` and `plex_token` are set correctly** in the plugin settings. If either is blank, the automatic post-activation scan cannot run, so scan the library manually.
5. **Double-check the Plex Library Section ID** if using the Scan Plex action. An incorrect ID scans the wrong library or no library. See [Rclone Setup](RCLONE_SETUP.md#find-the-plex-library-section-id) for how to find it.

## Rclone cannot connect

1. Confirm the plugin server is running in Dispatcharr and that the dashboard opens at `http://<dispatcharr-host-ip>:8888/`.
2. From the Plex machine, test `http://<dispatcharr-host-ip>:8888/vod/` in a browser or with `curl -I`.
3. Confirm the Docker port mapping publishes the same port configured in the plugin.
4. Confirm the rclone remote URL uses the Dispatcharr machine's LAN address, not `localhost`.
5. Run `rclone lsd vodplugin:` and read the exact error. A connection-refused error usually means the plugin is stopped or the port is not published. A timeout usually means the machines cannot reach each other.

See the complete [Rclone Setup guide](RCLONE_SETUP.md) for Linux, Windows, and Plex library instructions.
