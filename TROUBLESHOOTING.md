# Troubleshooting — VOD To Plex

See [INSTALL.md](INSTALL.md) for setup, or the main [README](README.md) for features and usage.

## Activated a movie, STRM files exist, but Plex doesn't show it

1. **Check Plex's library path is the rclone mount, not the plugin's raw output directory.** The plugin writes STRM/NFO files inside its own container/host filesystem (`strm_output_dir`). Plex can only see them through the rclone HTTP mount (`/mnt/vod-plugin` or similar) set up in [INSTALL.md](INSTALL.md#4-set-up-rclone-on-the-plex-server) — pointing a Plex library directly at the plugin's raw output path will not work unless that exact path is actually shared/mounted into wherever Plex runs.
2. **Confirm the rclone mount itself sees the file** before blaming Plex: run `ls /mnt/vod-plugin` (or your mount path) on the Plex server. If the movie's folder isn't there, the problem is the rclone mount/URL, not Plex — check the remote's `url` matches the plugin's actual host:port, and that the mount service is actually running (`systemctl status rclone-vodplugin`).
3. **Trigger a manual scan.** New STRM files aren't visible until Plex scans the library — use the plugin's **Scan Plex** action, or manually trigger one from Plex's library settings (⋮ → Scan Library Files).
4. **Verify `plex_url` and `plex_token` are set correctly** in the plugin settings — if either is blank, the plugin's automatic post-activation scan silently no-ops (it only logs an error, no UI warning), so nothing triggers until you scan manually.
5. **Double check the Plex Library Section ID** if using the Scan Plex action — an incorrect ID scans the wrong (or no) library. See [INSTALL.md](INSTALL.md#5-create-a-plex-library) for how to find it.
