"""1–20 versions per song: settings, per-song overrides, batch expansion, legacy A/B mirrors and
10-entry lists saved before v1.6.0."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import generation_controls as controls
import service
import server


class VersionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-versions-')
        root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=root, DATA=root / 'data', JOBS=root / 'data/jobs', TRACKS=root / 'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True)
        service.TRACKS.mkdir()
        self.tid = 'a' * 32
        (service.TRACKS / self.tid).mkdir()
        service.save(service.TRACKS / self.tid / 'track.json', {'id': self.tid, 'title': 'Song', 'duration': 30,
            'source': 'unused.wav', 'style': 'Phonk', 'style_b': ''})
        self.http = service.ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
        self.previous = server.PORT
        server.PORT = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        server.PORT = self.previous
        self.paths.stop()
        self.temp.cleanup()

    def api(self, route, payload=None):
        request = urllib.request.Request(f'http://127.0.0.1:{server.PORT}' + route,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'X-Sing-Token': service.TOKEN, 'Content-Type': 'application/json'})
        with self.client.open(request, timeout=10) as response:
            return json.load(response)

    def rejected(self, route, payload):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api(route, payload)
        self.assertEqual(error.exception.code, 400)
        return json.load(error.exception)['error']

    def test_defaults_are_twenty_distinct_styles_and_legacy_settings_migrate(self):
        self.assertEqual((controls.MAX_VERSIONS, controls.LETTERS), (20, 'ABCDEFGHIJKLMNOPQRST'))
        defaults = self.api('/api/state')['settings']
        self.assertEqual(defaults['version_count'], 2)
        self.assertEqual(defaults['styles'], controls.DEFAULT_STYLES)
        self.assertEqual(len({s.casefold() for s in defaults['styles']}), 20)
        self.assertEqual(defaults['version_instructions'], [''] * 20)
        service.save(service.DATA / 'settings.json', {'style': 'Lo-fi', 'style_b': 'My own', 'instructions_b': 'drums only'})
        legacy = self.api('/api/state')['settings']
        self.assertEqual(legacy['styles'][:2], ['Lo-fi', 'My own'])
        self.assertEqual(len({s.casefold() for s in legacy['styles']}), 20)
        self.assertEqual(legacy['version_instructions'][1], 'drums only')
        for style in ('Soul', 'Funk'):
            self.assertIn(controls.STYLE_TAGS[style].split(',')[0], controls.build_caption(style))

    def test_ten_entry_lists_from_v1_5_keep_their_versions(self):
        ten = ['Jazz', 'Soul', 'Funk', 'Phonk', 'Remix', 'Lo-fi', 'Acoustic', 'Hardstyle', 'Hardtekk', 'My custom']
        notes = ['airy'] + [''] * 8 + ['last one']
        service.save(service.DATA / 'settings.json', {'style': 'Jazz', 'style_b': 'Soul', 'styles': ten, 'instructions_a': 'airy',
            'version_instructions': notes, 'version_count': 10})
        settings = self.api('/api/state')['settings']
        self.assertEqual(settings['styles'][:10], ten)
        self.assertEqual(len({s.casefold() for s in settings['styles']}), 20)
        self.assertEqual(settings['version_instructions'], notes + [''] * 10)
        self.assertEqual(settings['version_count'], 10)
        track = service.read(service.TRACKS / self.tid / 'track.json')
        service.save(service.TRACKS / self.tid / 'track.json', {**track, 'styles': ['Phonk', 'Jazz'] + [''] * 8, 'style_b': 'Jazz'})
        mine = next(t for t in self.api('/api/state')['tracks'] if t['id'] == self.tid)
        self.assertEqual(mine['styles'], ['Phonk', 'Jazz'] + [''] * 18)
        # A stale interface still sending ten entries pads instead of being refused.
        saved = self.api('/api/settings', {'styles': ten, 'version_instructions': notes})
        self.assertEqual((saved['styles'][:10], saved['version_instructions'][10:]), (ten, [''] * 10))
        self.assertEqual(self.api('/api/tracks/' + self.tid, {'styles': [''] * 9 + ['Soul']})['styles'], [''] * 9 + ['Soul'] + [''] * 10)

    def test_settings_lists_mirror_the_a_b_fields(self):
        styles = controls.DEFAULT_STYLES[::-1]
        notes = ['airy', 'brass'] + [''] * 17 + ['last one']
        saved = self.api('/api/settings', {'version_count': 20, 'styles': styles, 'version_instructions': notes})
        self.assertEqual((saved['style'], saved['style_b'], saved['instructions_a'], saved['instructions_b']), (styles[0], styles[1], 'airy', 'brass'))
        self.assertEqual((saved['styles'], saved['version_instructions'], saved['version_count']), (styles, notes, 20))
        self.assertEqual(self.api('/api/state')['settings'], saved)
        older = self.api('/api/settings', {'style_b': 'Acoustic'})
        self.assertEqual(older['styles'][1], 'Acoustic')
        self.assertEqual(older['styles'][2:], styles[2:])
        for bad in ({'version_count': 0}, {'version_count': 21}, {'version_count': '3'}, {'version_count': True},
                    {'styles': styles[:19]}, {'styles': styles[:19] + ['']}, {'styles': styles[:19] + ['x' * 41]},
                    {'styles': styles[:19] + [5]}, {'version_instructions': ['x' * 901] + [''] * 19}):
            with self.subTest(bad=bad):
                self.rejected('/api/settings', bad)
        self.assertEqual(self.api('/api/state')['settings'], older)

    def test_tracks_keep_per_song_styles_and_count(self):
        track = next(t for t in self.api('/api/state')['tracks'] if t['id'] == self.tid)
        self.assertEqual(track['styles'], ['Phonk'] + [''] * 19)
        self.assertIsNone(track['version_count'])
        styles = [''] * 20
        styles[1], styles[16] = 'Jazz', 'My custom'
        saved = self.api('/api/tracks/' + self.tid, {'styles': styles, 'version_count': 17})
        self.assertEqual((saved['styles'], saved['style'], saved['style_b'], saved['version_count']), (styles, '', 'Jazz', 17))
        self.assertIsNone(self.api('/api/tracks/' + self.tid, {'version_count': None})['version_count'])
        for bad in ({'version_count': 21}, {'styles': [''] * 19}, {'styles': [''] * 19 + ['x' * 41]}):
            with self.subTest(bad=bad):
                self.rejected('/api/tracks/' + self.tid, bad)

    def test_batch_expands_one_to_twenty_versions_with_distinct_styles(self):
        with patch('yue2_engine.available', return_value=True), patch.object(service.secrets, 'randbelow', side_effect=lambda n: n - 1):
            twenty = self.api('/api/batch', {'request_id': 'twenty-versions-12345', 'items': [{'track_id': self.tid, 'instructions': '',
                'variants': [{'style': s, 'variant_instructions': f'note {i}'} for i, s in enumerate(controls.DEFAULT_STYLES)]}]})['jobs']
            self.assertEqual([j['variant'] for j in twenty], list(controls.LETTERS))
            self.assertEqual(len({j['pair_id'] for j in twenty}), 1)
            configs = [service.read(service.JOBS / j['id'] / 'config.json') for j in twenty]
            self.assertTrue(all(c['reuse_from'] == twenty[0]['id'] for c in configs[1:]))
            self.assertNotIn('reuse_from', configs[0])
            seeds = [c['seed'] for c in configs]
            self.assertEqual(len(set(seeds)), 20)
            self.assertLessEqual(max(seeds), 2147483647)
            self.assertEqual(twenty[19]['output_name'], 'Bossa Nova Song')
            self.assertIn('note 19', configs[19]['caption'])
            one = self.api('/api/batch', {'request_id': 'one-version-12345678', 'items': [{'track_id': self.tid, 'instructions': '',
                'variants': [{'style': 'Soul'}]}]})['jobs']
            self.assertEqual((len(one), one[0]['variant']), (1, 'A'))
            count = len(service.jobs())
            self.assertIn('不同风格', self.rejected('/api/batch', {'request_id': 'duplicate-styles-1234', 'items': [{'track_id': self.tid,
                'instructions': '', 'variants': [{'style': 'Jazz'}, {'style': 'Lo-fi'}, {'style': ' jazz '}]}]}))
            self.assertIn('1–20', self.rejected('/api/batch', {'request_id': 'twentyone-versions-12', 'items': [{'track_id': self.tid,
                'instructions': '', 'variants': [{'style': f'Style {i}'} for i in range(21)]}]}))
            self.assertIn('1–20', self.rejected('/api/batch', {'request_id': 'zero-versions-1234567', 'items': [{'track_id': self.tid,
                'instructions': '', 'variants': []}]}))
            self.assertIn('批次内容', self.rejected('/api/batch', {'request_id': 'not-a-song-123456789', 'items': ['song']}))
            self.assertEqual(len(service.jobs()), count)


if __name__ == '__main__':
    unittest.main(verbosity=2)
