"""Offline filesystem and removal workflow tests; no Plex/provider access."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('removal_bridge', os.environ.get('BRIDGE_SOURCE', str(Path(__file__).resolve().parents[1] / 'bridge.py')))
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests')}):
    spec.loader.exec_module(bridge)

class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.category = self.root / 'tv'
        self.folder = self.category / 'Show' / 'Season 01'
        self.folder.mkdir(parents=True)
        self.nfo = self.folder / 'Episode.nfo'
        self.nfo.write_text('test')
        (self.folder.parent / 'tvshow.nfo').write_text('show metadata')
        c = self.core = bridge.BridgeCore.__new__(bridge.BridgeCore)
        c._resolve_series_category = Mock(return_value={'strm_folder': 'tv'})
        c._series_category_path = lambda name: str(self.root / name)
        c._episodes_activated = {'1': {'category_id': 1, 'strm_folder': 'tv/Show/Season 01', 'strm_stem': 'Episode', 'series_name': 'Show', 'season_number': 1, 'episode_number': 1}}
        self.entry = dict(c._episodes_activated['1'])
        c._needs_attention = {}
        c._needs_attention_seq = 0
        c._needs_attention_lock = threading.Lock()
        for name in ['_save_state', '_append_history', '_log_event']:
            setattr(c, name, Mock())
        c._plex_delete_episodes = Mock(return_value=0)
        self.info = {'1': (1, 'tv/Show/Season 01', 'Episode')}

    def test_success_returns_true(self):
        self.assertTrue(self.core._remove_strm_for_episodes(self.info))
        self.assertFalse(self.nfo.exists())
        self.assertTrue(self.category.exists())

    def test_already_absent_is_success(self):
        self.core._remove_strm_for_episodes(self.info)
        self.assertTrue(self.core._remove_strm_for_episodes(self.info))

    def test_nonempty_season_preserves_other_episode(self):
        other = self.folder / 'Other.nfo'
        other.write_text('keep')
        self.assertTrue(self.core._remove_strm_for_episodes(self.info))
        self.assertTrue(other.exists())

    def test_permission_error_is_not_success(self):
        with patch.object(bridge.os, 'remove', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.core._remove_strm_for_episodes(self.info)

    def test_successful_deactivation_has_no_false_warning(self):
        self.core.deactivate_episodes({'episode_ids': ['1']})
        self.assertEqual(self.core._needs_attention, {})
        self.assertNotIn('1', self.core._episodes_activated)

    def test_retry_retries_filesystem_cleanup(self):
        with patch.object(bridge.os, 'remove', side_effect=PermissionError('denied')):
            self.core.deactivate_episodes({'episode_ids': ['1']})
        attn_id = next(iter(self.core._needs_attention))
        result = self.core.retry_needs_attention({'attn_ids': [attn_id]})
        self.assertFalse(self.nfo.exists())
        self.assertEqual(result['results'][0]['status'], 'ok')
        self.assertNotIn(attn_id, self.core._needs_attention)

    def test_missing_category_fails(self):
        self.core._resolve_series_category.return_value = None
        with self.assertRaises(ValueError):
            self.core._remove_strm_for_episodes(self.info)

    def test_prune_permission_error_fails(self):
        with patch.object(bridge.os, 'rmdir', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.core._remove_strm_for_episodes(self.info)

    def test_other_season_preserves_show_metadata(self):
        (self.folder.parent / 'Season 02').mkdir()
        self.assertTrue(self.core._remove_strm_for_episodes(self.info))
        self.assertTrue((self.folder.parent / 'tvshow.nfo').exists())

    def test_path_outside_category_is_rejected(self):
        outside = self.root / 'other'
        outside.mkdir()
        protected = outside / 'Episode.nfo'
        protected.write_text('keep')
        with self.assertRaises(ValueError):
            self.core._remove_strm_for_episodes({'1': (1, 'other', 'Episode')})
        self.assertTrue(protected.exists())

    def test_plex_success_count_is_preserved(self):
        self.core._plex_delete_episodes.return_value = 1
        result = self.core.deactivate_episodes({'episode_ids': ['1']})
        self.assertEqual(result['plex_removed'], 1)
        self.assertEqual(self.core._needs_attention, {})

    def test_plex_exception_is_reported_after_local_cleanup(self):
        self.core._plex_delete_episodes.side_effect = RuntimeError('Plex unavailable')
        self.core.deactivate_episodes({'episode_ids': ['1']})
        self.assertFalse(self.nfo.exists())
        warning = next(iter(self.core._needs_attention.values()))
        self.assertIn('Plex unavailable', warning['message'])
        self.core._plex_delete_episodes.side_effect = None
        attn_id = next(iter(self.core._needs_attention))
        result = self.core.retry_needs_attention({'attn_ids': [attn_id]})
        self.assertEqual(result['results'][0]['status'], 'ok')
        self.assertEqual(self.core._needs_attention, {})

    def test_repeated_filesystem_failure_preserves_retry_information(self):
        with patch.object(bridge.os, 'remove', side_effect=PermissionError('denied')):
            self.core.deactivate_episodes({'episode_ids': ['1']})
            attn_id = next(iter(self.core._needs_attention))
            result = self.core.retry_needs_attention({'attn_ids': [attn_id]})
        self.assertEqual(result['results'][0]['status'], 'error')
        self.assertTrue(self.nfo.exists())
        for warning in self.core._needs_attention.values():
            self.assertEqual(warning['retry_context']['plex_match_entry'], self.entry)

    def test_category_root_cannot_be_removed(self):
        with self.assertRaises(ValueError):
            self.core._remove_strm_for_episodes({'1': (1, 'tv', 'Episode')})
        self.assertTrue(self.category.exists())

    def test_symlink_outside_category_cannot_be_followed(self):
        outside = self.root / 'protected'
        outside.mkdir()
        target = outside / 'Episode.nfo'
        target.write_text('keep')
        (self.category / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.core._remove_strm_for_episodes({'1': (1, 'tv/link', 'Episode')})
        self.assertEqual(target.read_text(), 'keep')

    def test_empty_cleanup_is_success(self):
        self.assertTrue(self.core._remove_strm_for_episodes({}))

class PlexEpisodeRemovalTests(unittest.TestCase):
    def setUp(self):
        RemovalTests.setUp(self)
        # Exercise the real Plex matching method, with HTTP/deletion mocked.
        del self.core._plex_delete_episodes
        self.core.settings = {'plex_url': 'http://plex.invalid', 'plex_token': 'test-only'}
        self.core._resolve_series_category.return_value = {'strm_folder': 'tv', 'plex_library_section': 8}
        self.core._plex_delete_batch = Mock(return_value=1)
        self.item = {'ratingKey': '42', 'title': 'Episode title', 'grandparentTitle': 'Localized Show',
                     'parentIndex': 1, 'index': 1,
                     'Media': [{'Part': [{'file': '/mount/tv/Show/Season 01/Episode [1].mkv'}]}]}

    def delete(self, items=None, status=200):
        response = Mock(status_code=status)
        response.json.return_value = {'MediaContainer': {'Metadata': [self.item] if items is None else items}}
        with patch.object(bridge, 'requests', types.SimpleNamespace(get=Mock(return_value=response))):
            return self.core._plex_delete_episodes({'1': self.entry})

    def test_localized_show_removed_by_file_identity(self):
        self.assertEqual(self.delete(), 1)
        self.assertEqual(self.core._plex_delete_batch.call_args.args[2], [('42', 'Episode title')])

    def test_wrong_id_never_deleted(self):
        self.item['grandparentTitle'] = 'Show'
        self.item['Media'][0]['Part'][0]['file'] = '/mount/tv/Show/Season 01/Episode [2].mkv'
        self.delete()
        self.core._plex_delete_batch.assert_not_called()

    def test_wrong_category_never_deleted(self):
        self.item['grandparentTitle'] = 'Show'
        self.item['Media'][0]['Part'][0]['file'] = '/mount/kids/Show/Season 01/Episode [1].mkv'
        self.delete()
        self.core._plex_delete_batch.assert_not_called()

    def test_ambiguous_items_are_not_deleted(self):
        other = dict(self.item, ratingKey='43')
        with self.assertRaises(ValueError):
            self.delete([self.item, other])
        self.core._plex_delete_batch.assert_not_called()

    def test_mixed_versions_are_not_deleted(self):
        self.item['Media'].append({'Part': [{'file': '/local/Other.mkv'}]})
        with self.assertRaises(ValueError):
            self.delete()
        self.core._plex_delete_batch.assert_not_called()

    def test_legacy_path_and_title_match(self):
        self.item['grandparentTitle'] = 'Show'
        self.item['Media'][0]['Part'][0]['file'] = '/mount/tv/Show/Season 01/Episode.mkv'
        self.assertEqual(self.delete(), 1)

    def test_query_failure_is_propagated(self):
        with self.assertRaises(RuntimeError):
            self.delete(status=503)
        self.core._plex_delete_batch.assert_not_called()

    def test_failed_delete_is_propagated(self):
        self.core._plex_delete_batch.return_value = 0
        with self.assertRaises(RuntimeError):
            self.delete()

    def test_already_absent_in_plex_is_success(self):
        self.core._plex_delete_batch.return_value = 0
        self.assertEqual(self.delete([]), 0)

if __name__ == '__main__':
    unittest.main(verbosity=2)
