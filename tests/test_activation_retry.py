"""Offline regression tests against real bridge methods; no providers or Plex contacted."""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('bridge_under_test', os.environ['BRIDGE_SOURCE'])
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests')}):
    spec.loader.exec_module(bridge)

class ActivationRetryTests(unittest.TestCase):
    def setUp(self):
        self.core = c = bridge.BridgeCore.__new__(bridge.BridgeCore)
        c._activated = {}
        c._maint_stats = {'audio_checked_total': 0, 'audio_missing_total': 0}
        c._needs_attention = {}
        c._needs_attention_seq = 0
        c._needs_attention_lock = threading.Lock()
        c._movie_job_lock = threading.Lock()
        c._movie_job_counter = 0
        c._movie_jobs = {}
        c._movie_job_queue = []
        c._movie_job_wake = Mock()
        for name in ['_save_state', '_log_event', '_log_diagnostic', '_append_history', '_size_reconcile_fast_path']:
            setattr(c, name, Mock())
        c._account_has_capacity = Mock(return_value=True)
        c._resolve_estimated_size = Mock(return_value=100)
        c._generate_strm_for_movies = Mock(return_value=1)
        c._fetch_plex_movie_sizes = Mock(return_value={'1': 100})
        c._trigger_plex_scan = Mock(return_value=True)
        c._probe_audio_for_relation = Mock(return_value={'status': 'probe_failed', 'message': 'Dispatcharr URL not configured'})
        relation = types.SimpleNamespace(stream_id=10, m3u_account_id=2)
        movie = types.SimpleNamespace(id=1, name='Test Movie', m3u_relations=types.SimpleNamespace(all=lambda: [relation]))
        models = types.ModuleType('apps.vod.models')
        models.Movie = types.SimpleNamespace(objects=types.SimpleNamespace(get=lambda **kw: movie))
        self.modules = patch.dict(sys.modules, {'apps': types.ModuleType('apps'), 'apps.vod': types.ModuleType('apps.vod'), 'apps.vod.models': models})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.job = {'job_id': 'test', 'activated': [], 'activated_names': [], 'movie_names': [], 'strm_generated': 0, 'failed': [], 'failed_names': []}

    def succeed(self):
        self.core._probe_audio_for_relation.return_value = {'status': 'ok', 'audio_stream_count': 1, 'audio_codecs': ['ac3']}

    def test_failed_sync_probe_does_not_activate(self):
        self.core.activate_movies({'movie_ids': [1]})
        self.assertNotIn('1', self.core._activated)
        self.core._generate_strm_for_movies.assert_not_called()
        self.assertEqual(self.core._maint_stats['audio_checked_total'], 1)

    def test_failed_async_probe_does_not_activate(self):
        self.core._activate_movie_batch(['1'], self.job)
        self.assertNotIn('1', self.core._activated)
        self.assertEqual(len(self.job['failed']), 1)

    def test_retry_after_correcting_configuration_succeeds(self):
        self.core.activate_movies({'movie_ids': [1]})
        attn_id = next(iter(self.core._needs_attention))
        self.succeed()
        result = self.core.retry_needs_attention({'attn_ids': [attn_id]})
        self.assertEqual(result['results'][0]['status'], 'ok')
        self.assertNotIn(attn_id, self.core._needs_attention)
        self.assertEqual(self.core._activated['1']['stream_pick'], 10)
        self.core._generate_strm_for_movies.assert_called_once_with(['1'])

    def test_sync_recovers_saved_partial_entry(self):
        self.core._activated['1'] = {'audio_checks': {}, 'stream_pick': 10}
        self.succeed()
        result = self.core.activate_movies({'movie_ids': [1]})
        self.assertEqual(result['activated'], 1)
        self.assertTrue(self.core._activated['1']['activated_at'])

    def test_async_queue_accepts_saved_partial_entry(self):
        self.core._activated['1'] = {'audio_checks': {}}
        result = self.core.activate_movies_async({'movie_ids': [1]})
        self.assertEqual(result['status'], 'queued')

    def test_async_batch_recovers_saved_partial_entry(self):
        self.core._activated['1'] = {'audio_checks': {}}
        self.succeed()
        self.core._activate_movie_batch(['1'], self.job)
        self.assertEqual(self.job['activated'], ['1'])

    def test_successful_activation_is_not_repeated(self):
        self.core._activated['1'] = {'activated_at': 123, 'stream_pick': 10}
        self.core.activate_movies({'movie_ids': [1]})
        self.core._activate_movie_batch(['1'], self.job)
        self.core._probe_audio_for_relation.assert_not_called()

    def test_existing_active_movie_keeps_audio_check(self):
        self.core._activated['1'] = {'activated_at': 123, 'stream_pick': 10}
        self.core._store_audio_check('1', 10, {'status': 'ok'})
        self.assertEqual(self.core._activated['1']['audio_checks']['10']['status'], 'ok')

if __name__ == '__main__':
    unittest.main(verbosity=2)
