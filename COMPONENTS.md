# Components

This project's deployment-tier inventory. See `_shared/deployment-tier.md` for tier definitions.

## Components

| Component | Tier | Purpose |
|-----------|------|---------|
| VOD To Plex plugin (bridge.py / server.py / plugin.py) | home-lab | Core plugin logic — Django ORM queries, WSGI HTTP server, STRM/NFO generation, Plex API integration |
| Web dashboard (templates/dashboard.html) | home-lab | Single-page UI for browsing/activating movies, health checks, now-playing |
| rclone HTTP mount (host .109) | home-lab | Exposes plugin's /vod/ endpoint as a FUSE filesystem for Plex to scan |
| Dispatcharr Postgres DB | home-lab | Owned by the host Dispatcharr application, not this plugin — read-only ORM consumer |
| Plex Media Server | home-lab | Personal media server that plays the bridged VOD library |

## Notes

Confirmed by the user 2026-07-11 during `/onboard`. All 10 assessed personas (Security
Engineer, IT Architect, Project Manager, Project Engineer, UX Designer, Code Reviewer,
Database Engineer, SRE, QA Engineer, Technical Writer) independently proposed home-lab
tier with no disagreement — single author, single household user, single container on
a home LAN host, no CI/CD, no customers, no compliance scope, no revenue impact if it
breaks (inconvenience for the operator only).

Overall onboarding result: 7 GREEN, 3 YELLOW, 0 RED. YELLOWs were: Security Engineer
(attack surface/secrets posture — normal for home-lab, no exploitable finding), Code
Reviewer (a since-fixed duplicate `activate_movies` method in bridge.py), and QA
Engineer (zero automated tests — below even the home-lab "smoke test" baseline, flagged
as a cheap future win on `server.py`'s pure-Python `_parse_query()` logic, not urgent).
