# VOD To Plex

<p align="center">
  <img src="logo.png" alt="VOD To Plex logo" width="120">
</p>

<p align="center"><strong>Dispatcharr Plugin</strong></p>

VOD To Plex lets you choose movies and series from Dispatcharr and make them appear in Plex. It creates small STRM and NFO files, exposes them through an HTTP server, and sends playback to Dispatcharr's native VOD proxy.

## How it works

```text
Plex → rclone mount → VOD To Plex HTTP server → Dispatcharr VOD proxy → Provider
```

In plain language:

1. Dispatcharr runs the plugin.
2. You activate titles in the plugin dashboard.
3. The plugin creates library information for Plex.
4. rclone, running on the Plex machine, makes the online library look like a normal folder.
5. Plex scans that folder.
6. When you press Play, the plugin sends Plex to Dispatcharr's normal VOD stream.

The plugin does not replace Dispatcharr's streaming system. Dispatcharr continues to manage provider connections, range requests, sessions, and playback.

## Main features

- Movies and series browsing with selective activation.
- Provider, category, language, search, and duplicate filtering.
- STRM/NFO generation with TMDB-assisted metadata options.
- Plex library scans after activation.
- Plex deletion when content is deactivated.
- Provider capacity checks and fallback handling.
- Health, activity, now-playing, and maintenance tools.
- Manual review of orphaned STRM folders.

## Requirements

| Requirement | Purpose |
|---|---|
| Dispatcharr 0.24.0 or newer | Runs the plugin and supplies the VOD streams |
| Plex Media Server | Displays and plays the activated content |
| rclone on the Plex machine | Connects Plex to the plugin's HTTP library |
| Network access | Lets Plex reach the plugin's HTTP port |

## Start here

- [Deployment and Installation](INSTALL.md): install the plugin, configure settings, publish the port, and start the server.
- [Rclone Setup](RCLONE_SETUP.md): connect the Plex machine to the plugin in plain language.
- [Troubleshooting](TROUBLESHOOTING.md): diagnose setup, scanning, mounting, and playback problems.
- [Components](COMPONENTS.md): see how the plugin fits into the surrounding systems.

## Using the plugin

After installation:

1. Open the dashboard at `http://<dispatcharr-host-ip>:8888/`.
2. Choose **Movies** or **Series**.
3. Filter or search for the content you want.
4. Select a title and click **Activate**.
5. Wait for the Plex scan to finish.
6. Play the title from Plex.

For provider accounts with low connection limits, activate a small group first. Plex may inspect new files during a scan, and those inspections can briefly use provider connections.

## Architecture details

### Playback

```text
Plex requests a file
  → rclone forwards the request to the plugin
  → the plugin finds the selected Dispatcharr stream
  → the plugin returns a 302 redirect
  → Dispatcharr's VOD proxy streams the content
```

### File size handling

Plex and rclone ask for file information before playback. The plugin provides an estimate and later reconciles it with Plex's confirmed size so Plex does not repeatedly re-analyze a title because its reported size changed.

### Deactivation

When a title is deactivated, the plugin removes its generated library files and requests deletion of the matching Plex entry. Plex must have media deletion enabled for the Plex-side removal to succeed.

## Release packaging

Release ZIPs are imported by Dispatcharr's plugin manager. They must contain one top-level `vod_plex_bridge/` folder, and `templates/dashboard.html` must remain nested inside it:

```text
vod-to-plex-plugin-vX.Y.Z.zip
└── vod_plex_bridge/
    ├── plugin.json
    └── templates/
        └── dashboard.html
```

ZIP entry names must use forward slashes. See the full [release packaging instructions](INSTALL.md#release-packaging) before publishing a package.

## Known limitations

- Large series activations can cause Plex to inspect many episodes at once. Activate a season or smaller batch when provider limits are low.
- Provider fallback is based on capacity and playback health, not a permanent provider preference.
- Provider failures are reported through the dashboard and HTTP status codes rather than a custom video error screen.

## Acknowledgments

This project builds on ideas and patterns from the Dispatcharr community, [vod2strm](https://github.com/cmc0619/vod2strm), [VOD2MLIB](https://github.com/shedunraid/VOD2MLIB), and the [Dispatcharr project](https://github.com/Dispatcharr/Dispatcharr).

## License

MIT
