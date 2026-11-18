"""Run directly; mocks Plex HTTP and needs no Django or network access."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

source = Path(os.environ.get('BRIDGE_SOURCE', Path(__file__).resolve().parents[1] / 'bridge.py'))
spec = importlib.util.spec_from_file_location('confirmation_bridge', source)
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'requests': types.ModuleType('requests')}):
    spec.loader.exec_module(bridge)

class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.core = bridge.BridgeCore.__new__(bridge.BridgeCore)
        self.core.settings = {'plex_url': 'http://plex.invalid', 'plex_token': 'test-only'}
        self.entry = {'series_name': '┃DE┃ JAG – Im Auftrag der Ehre', 'season_number': 2,
                      'episode_number': 2, 'category_id': 1,
                      'strm_folder': 'tv/JAG (1995)/Season 02'}
        self.core._episodes_activated = {'216': self.entry}
        self.core._resolve_series_category = Mock(return_value={'plex_library_section': 8})
        self.core._log_diagnostic = Mock()
        self.part = {'file': '/custom/mount/tv/JAG (1995)/Season 02/Secrets [216].mkv', 'size': 705250000}
        self.item = {'grandparentTitle': 'JAG', 'parentIndex': 2, 'index': 2, 'updatedAt': 123,
                     'Media': [{'Part': [self.part]}]}

    def fetch(self, items=None):
        response = Mock(status_code=200)
        response.json.return_value = {'MediaContainer': {'Metadata': [self.item] if items is None else items}}
        with patch.object(bridge, 'requests', types.SimpleNamespace(get=Mock(return_value=response))):
            return self.core._fetch_plex_episode_sizes(['216'])

    def test_localized_show_name(self):
        self.assertEqual(self.fetch(), {'216': (705250000, 123)})

    def test_changed_plex_numbering(self):
        self.item['index'] = 3
        self.assertIn('216', self.fetch())

    def test_all_media_versions(self):
        self.item['Media'].insert(0, {'Part': [{'file': '/other [999].mkv', 'size': 999}]})
        self.assertEqual(self.fetch()['216'][0], 705250000)

    def test_wrong_id(self):
        self.item['grandparentTitle'] = self.entry['series_name']
        self.part['file'] = self.part['file'].replace('[216]', '[999]')
        self.assertEqual(self.fetch(), {})

    def test_wrong_category(self):
        self.part['file'] = self.part['file'].replace('/tv/', '/kids/')
        self.assertEqual(self.fetch(), {})

    def test_duplicate_identity(self):
        self.assertEqual(self.fetch([self.item, copy.deepcopy(self.item)]), {})

    def test_legacy_fallback(self):
        self.item['grandparentTitle'] = self.entry['series_name']
        self.part['file'] = self.part['file'].replace(' [216]', '')
        self.assertIn('216', self.fetch())

    def test_legacy_ambiguity(self):
        self.item['grandparentTitle'] = self.entry['series_name']
        self.part['file'] = self.part['file'].replace(' [216]', '')
        self.assertEqual(self.fetch([self.item, copy.deepcopy(self.item)]), {})

    def test_invalid_size(self):
        self.part['size'] = 'invalid'
        self.assertEqual(self.fetch(), {})

    def test_missing_size(self):
        self.part.pop('size')
        self.assertEqual(self.fetch(), {})

    def test_multipart(self):
        self.item['Media'][0]['Part'].append({'file': '/other/part2.mkv', 'size': 42})
        self.assertEqual(self.fetch(), {})

    def test_windows_path(self):
        self.part['file'] = self.part['file'].replace('/custom/mount/', 'Z:/').replace('/', '\\')
        self.assertIn('216', self.fetch())

if __name__ == '__main__':
    unittest.main(verbosity=2)
