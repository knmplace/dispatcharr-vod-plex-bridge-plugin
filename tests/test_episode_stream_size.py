"""Offline measured-size tests. No provider, Dispatcharr, or Plex requests."""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('size_bridge', os.environ.get('BRIDGE_SOURCE', str(Path(__file__).resolve().parents[1] / 'bridge.py')))
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests')}):
    spec.loader.exec_module(bridge)

class StreamSizeTests(unittest.TestCase):
    def setUp(self):
        c = self.core = bridge.BridgeCore.__new__(bridge.BridgeCore)
        c._episodes_activated = {'1': {'stream_pick': '10', 'confirmed_size': 2147483648, 'estimated_size': 2000000000}}
        c._get_episode_redirect_lock = Mock(return_value=threading.Lock())
        c._build_episode_proxy_url = Mock(return_value='http://dispatcharr.invalid/proxy/vod/episode/test')
        c._account_has_capacity = Mock(return_value=True)
        c._save_state = Mock()
        c._log_diagnostic = Mock()
        self.episode = types.SimpleNamespace(id=1, duration_secs=1700)
        self.relation = types.SimpleNamespace(stream_id=10, m3u_account_id=2)
        self.response = Mock(status_code=200, headers={'Content-Length': '888524811'})
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.head = Mock(return_value=self.response)
        self.patcher = patch.object(bridge, 'requests', types.SimpleNamespace(head=self.head))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def size(self):
        return self.core._get_episode_stream_size(self.episode, self.relation)

    def test_measured_length_overrides_plex_and_estimate(self):
        self.assertEqual(self.size(), 888524811)
        self.assertEqual(self.core._estimate_episode_size(self.episode), 888524811)
        self.core._save_state.assert_called_once()

    def test_uses_head_and_closes_response_without_reading_body(self):
        self.size()
        self.head.assert_called_once()
        self.response.__exit__.assert_called_once()
        self.assertEqual(self.head.call_args.kwargs['headers']['Accept-Encoding'], 'identity')

    def test_cache_avoids_repeat_provider_request(self):
        self.size()
        self.assertEqual(self.size(), 888524811)
        self.head.assert_called_once()

    def test_changed_stream_is_remeasured(self):
        self.size()
        self.relation.stream_id = 11
        self.response.headers = {'Content-Length': '900000000'}
        self.assertEqual(self.size(), 900000000)
        self.assertEqual(self.head.call_count, 2)

    def test_partial_response_uses_total_not_chunk_length(self):
        self.response.status_code = 206
        self.response.headers = {'Content-Length': '2', 'Content-Range': 'bytes 0-1/888524811'}
        self.assertEqual(self.size(), 888524811)

    def test_malformed_partial_headers_rejected(self):
        self.response.status_code = 206
        for value in ['', 'bytes 0-1/*', 'bytes 4-1/99', 'bytes 0-99/99']:
            with self.subTest(value=value):
                self.core._episodes_activated['1'].pop('size_probe_at', None)
                self.response.headers = {'Content-Length': '2', 'Content-Range': value}
                self.assertIsNone(self.size())

    def test_missing_negative_zero_or_malformed_length_rejected(self):
        for value in ['', '-1', '0', 'unknown']:
            with self.subTest(value=value):
                self.core._episodes_activated['1'].pop('size_probe_at', None)
                self.response.headers = {'Content-Length': value}
                self.assertIsNone(self.size())

    def test_http_failure_rejected_and_backed_off(self):
        self.response.status_code = 503
        self.assertIsNone(self.size())
        self.assertIsNone(self.size())
        self.head.assert_called_once()
        self.core._save_state.assert_not_called()

    def test_compressed_representation_rejected(self):
        self.response.headers['Content-Encoding'] = 'gzip'
        self.assertIsNone(self.size())

    def test_timeout_does_not_return_old_plex_size(self):
        self.head.side_effect = TimeoutError('test timeout')
        self.assertIsNone(self.size())
        self.assertIsNone(self.core._estimate_episode_size(self.episode))

    def test_no_capacity_makes_no_request(self):
        self.core._account_has_capacity.return_value = False
        self.assertIsNone(self.size())
        self.head.assert_not_called()

    def test_missing_url_makes_no_request(self):
        self.core._build_episode_proxy_url.return_value = None
        self.assertIsNone(self.size())
        self.head.assert_not_called()

    def test_head_405_uses_bounded_private_range_and_cleans_up(self):
        self.response.status_code = 405
        ranged = Mock(status_code=206, headers={'Content-Range': 'bytes 0-1/888524811', 'Content-Length': '2'})
        ranged.__enter__ = Mock(return_value=ranged)
        ranged.__exit__ = Mock(return_value=False)
        get = Mock(return_value=ranged)
        self.core._cleanup_episode_size_probe = Mock()
        with patch.object(bridge.requests, 'get', get, create=True):
            self.assertEqual(self.size(), 888524811)
        self.assertEqual(get.call_args.kwargs['headers']['Range'], 'bytes=0-1')
        self.assertTrue(get.call_args.kwargs['stream'])
        self.assertIn('/vod_size_', get.call_args.args[0])
        self.core._cleanup_episode_size_probe.assert_called_once()
        ranged.iter_content.assert_not_called()
        ranged.__exit__.assert_called_once()

    def test_range_failure_still_cleans_up_probe(self):
        self.response.status_code = 405
        self.core._cleanup_episode_size_probe = Mock()
        with patch.object(bridge.requests, 'get', Mock(side_effect=TimeoutError()), create=True):
            self.assertIsNone(self.size())
        self.core._cleanup_episode_size_probe.assert_called_once()

    def test_deactivated_during_probe_is_not_resurrected(self):
        def request(*args, **kwargs):
            self.core._episodes_activated.clear()
            return self.response
        self.head.side_effect = request
        self.assertIsNone(self.size())
        self.assertEqual(self.core._episodes_activated, {})

class EpisodeHeadRouteTests(unittest.TestCase):
    def test_routes_only_advertise_measured_length(self):
        server_path = source = Path(os.environ.get('SERVER_SOURCE', str(Path(__file__).resolve().parents[1] / 'server.py')))
        spec = importlib.util.spec_from_file_location('size_server', server_path)
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        for path in ['/vod-series/tv/Show/Season 01/Episode [1].mkv', '/vod/episode/Episode [1].mkv']:
            for size, status in [(None, '503 Service Unavailable'), (888524811, '200 OK')]:
                with self.subTest(path=path, size=size):
                    core = Mock()
                    core._head_request_semaphore = threading.Semaphore(1)
                    core.get_episode_info.return_value = {'file_size': size}
                    start_response = Mock()
                    result = server._dispatch({'PATH_INFO': path, 'REQUEST_METHOD': 'HEAD', 'QUERY_STRING': ''}, start_response, Mock(), core, {})
                    self.assertEqual(start_response.call_args.args[0], status)
                    headers = dict(start_response.call_args.args[1])
                    self.assertEqual(headers['Content-Length'], str(size) if size else '0')
                    self.assertEqual(result, [b''])

if __name__ == '__main__':
    unittest.main(verbosity=2)
