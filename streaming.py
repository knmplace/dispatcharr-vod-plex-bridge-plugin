"""
Streaming proxy module — placeholder for initial plugin testing.

The full implementation will port StreamPipe from the standalone bridge:
- Single persistent connection to Dispatcharr VOD proxy
- Adaptive bitrate throttling
- Head/tail caching
- Disk buffer with resume
- Circuit breaker

For now, this module provides stubs so the plugin can load and
serve the dashboard without streaming functionality.
"""

import logging
import threading

logger = logging.getLogger("vod_plex_bridge.streaming")


class StreamPipe:
    """Placeholder — will be ported from standalone bridge."""

    def __init__(self, movie_id, session_url, bitrate_bps):
        self.movie_id = movie_id
        self.session_url = session_url
        self.bitrate_bps = bitrate_bps
        self.bytes_downloaded = 0
        self.started_at = 0
        self._running = False

    def start(self):
        logger.info(f"StreamPipe stub: would start streaming {self.movie_id}")
        self._running = True

    def close(self):
        self._running = False

    def is_running(self):
        return self._running

    def read_range(self, start, end):
        return b""

    def get(self, key, default=None):
        return getattr(self, key, default)
