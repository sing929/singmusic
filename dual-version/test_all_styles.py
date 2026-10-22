"""v1.7.0: the "all styles" switch (one version per catalog style, shared and per song) and the new
rap styles: catalog order and letters, saved settings, batch expansion over HTTP, voice swap,
speed versions, the orchestral suite, smart defaults and the queue limit."""
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
import voice_swap

SWAP_ALL = {'versions': [True] * 20, 'octave': 'down'}
SWAP_A = {'versions': [True] + [False] * 19, 'octave': 'auto'}
RAP = ['Rap', 'Hip-hop', 'Trap', 'Boom Bap', 'Melodic Rap', 'Drill', 'Emo Rap', 'Phonk']


class CatalogTest(unittest.TestCase):
    def test_rap_group_and_new_styles(self):
        rap = next(g for g in controls.CATALOG['groups'] if g['id'] == 'rap')
        self.assertEqual(rap['styles'], RAP)
        for name in ('Rap', 'Melodic Rap', 'Drill', 'Emo Rap'):
            with self.subTest(style=name):
                info = controls.CATALOG['styles'][name]
                self.assertIn('rap', info['tags'])
                self.assertEqual(info['defaults'], {'mode': 'remix', 'melody_mode': 'free'})
        self.assertTrue({'Rap', 'Melodic Rap', 'Drill'} <= set(controls.DEFAULT_STYLES))
        self.assertEqual(len(set(controls.DEFAULT_STYLES)), 20)

    def test_all_styles_follow_the_groups(self):
        self.assertEqual(controls.ALL_STYLES, [s for g in controls.CATALOG['groups'] for s in g['styles']])
        self.assertEqual(sorted(controls.ALL_STYLES), sorted(controls.CATALOG['styles']))
        self.assertEqual(len(controls.ALL_STYLES), 55)
        self.assertEqual(controls.MAX_SONG_JOBS, 58)

    def test_version_letters(self):
        self.assertEqual(''.join(controls.version_letter(i) for i in range(20)), controls.LETTERS)
        self.assertEqual([controls.version_letter(i) for i in (25, 26, 27, 51, 52, 57, 701, 702)],
                         ['Z', 'AA', 'AB', 'AZ', 'BA', 'BF', 'ZZ', 'AAA'])
        letters = [controls.version_letter(i) for i in range(controls.MAX_SONG_JOBS)]
        self.assertEqual(len(set(letters)), len(letters))

    def test_voice_swap_switches_all_versions_together(self):
        self.assertEqual(voice_swap.for_all_styles(SWAP_ALL), {'target': 'original', 'octave': 'down'})
        self.assertIsNone(voice_swap.for_all_styles(SWAP_A))
        self.assertIsNone(voice_swap.for_all_styles(None))


class AllStylesBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-all-styles-')
        root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=root, DATA=root / 'data', JOBS=root / 'data/jobs', TRACKS=root / 'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True)
        service.TRACKS.mkdir()
        self.tid = 'b' * 32
        (service.TRACKS / self.tid).mkdir()
        service.save(service.TRACKS / self.tid / 'track.json', {'id': self.tid, 'title': 'Song', 'duration': 30, 'source': 'unused.wav'})
        self.http = service.ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
        self.previous = server.PORT
        server.PORT = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.engines = [patch('yue2_engine.available', return_value=True), patch.object(service.voice_swap, 'available', return_value=True)]
        for engine in self.engines:
            engine.start()

    def tearDown(self):
        for engine in self.engines:
            engine.stop()
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
        with self.client.open(request, timeout=30) as response:
            return json.load(response)

    def rejected(self, route, payload):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api(route, payload)
        self.assertEqual(error.exception.code, 400)
        return json.load(error.exception)['error']

    def item(self, **fields):
        return {'track_id': self.tid, 'instructions': '', 'mode': 'remix', 'melody_mode': 'reference', 'speed_variants': True,
            'adjust': {'semitones': 0, 'tempo': 100, 'speed_mode': 'independent', 'reverb': 0}, **fields}

    def batch(self, key, **fields):
        return self.api('/api/batch', {'request_id': key, 'items': [self.item(**fields)]})['jobs']

    def config(self, job):
        return service.read(service.JOBS / job['id'] / 'config.json')

    def test_shared_and_per_song_switch_are_saved(self):
        state = self.api('/api/state')
        self.assertIs(state['settings']['all_styles'], False)
        self.assertIsNone(next(t for t in state['tracks'] if t['id'] == self.tid)['all_styles'])
        self.assertIs(self.api('/api/settings', {'all_styles': True})['all_styles'], True)
        self.assertIs(self.api('/api/state')['settings']['all_styles'], True)
        self.assertIn('无效', self.rejected('/api/settings', {'all_styles': 'yes'}))
        self.assertIs(self.api('/api/tracks/' + self.tid, {'all_styles': True})['all_styles'], True)
        self.assertIsNone(self.api('/api/tracks/' + self.tid, {'all_styles': None})['all_styles'])
        self.assertIn('全部风格', self.rejected('/api/tracks/' + self.tid, {'all_styles': 1}))
        # A broken stored value falls back instead of breaking the app.
        service.save(service.DATA / 'settings.json', {**service.read(service.DATA / 'settings.json'), 'all_styles': 'on'})
        self.assertIs(self.api('/api/state')['settings']['all_styles'], False)

    def test_one_version_per_style_without_speed_versions(self):
        jobs = self.batch('all-styles-12345678', all_styles=True, variants=[{'style': 'Jazz'}, {'style': 'Soul'}], voice_swap=SWAP_A)
        count = len(controls.ALL_STYLES)
        self.assertEqual([j['style'] for j in jobs], controls.ALL_STYLES)
        self.assertEqual([j['variant'] for j in jobs], [controls.version_letter(i) for i in range(count)])
        self.assertEqual(len({j['pair_id'] for j in jobs}), 1)
        self.assertTrue(all(j['speed_variants'] is False and self.config(j)['speed_variants'] is False for j in jobs))
        # One switch for all versions: version A alone is not "on".
        self.assertTrue(all(self.config(j)['voice'] is None for j in jobs))
        seeds = [self.config(j)['seed'] for j in jobs]
        self.assertEqual(len(set(seeds)), count)
        rap = next(j for j in jobs if j['style'] == 'Drill')
        self.assertEqual(rap['output_name'], 'Drill Song')
        # Analysis is shared by every version with the same source settings.
        self.assertTrue(all(self.config(j)['reuse_from'] == jobs[0]['id'] for j in jobs[1:]))

    def test_voice_swap_smart_defaults_and_suite(self):
        jobs = self.batch('all-styles-smart-1234', all_styles=True, smart_defaults=True, voice_swap=SWAP_ALL, suite={'styles': ['String Quartet', 'Baroque', 'Waltz']})
        count = len(controls.ALL_STYLES)
        self.assertEqual(len(jobs), count + 3)
        self.assertEqual([j['variant'] for j in jobs[count:]], ['BD', 'BE', 'BF'])
        self.assertEqual([j['suite_label'] for j in jobs[count:]], ['Suite I', 'Suite II', 'Suite III'])
        by_style = {j['style']: self.config(j) for j in jobs[:count]}
        for name in RAP[:-1]:
            with self.subTest(style=name):
                self.assertEqual((by_style[name]['mode'], by_style[name]['melody_mode']), ('remix', 'free'))
                self.assertEqual(by_style[name]['voice'], {'target': 'original', 'octave': 'down'})
        # Asian DJ styles keep the original singer, so they are not voice swapped.
        self.assertEqual((by_style['Vinahouse']['mode'], by_style['Vinahouse']['voice']), ('preserve', None))
        self.assertEqual(by_style['Jazz']['voice'], {'target': 'original', 'octave': 'down'})
        # Without the switch the per-version limit still applies.
        self.assertIn('20', self.rejected('/api/batch', {'request_id': 'all-styles-off-123456', 'items': [self.item(
            all_styles=False, variants=[{'style': s} for s in controls.ALL_STYLES[:21]])]}))

    def test_bad_switch_and_queue_limit(self):
        self.assertIn('全部风格', self.rejected('/api/batch', {'request_id': 'all-styles-bad-123456', 'items': [self.item(all_styles='yes', variants=[{'style': 'Jazz'}])]}))
        with patch.object(service, 'QUEUE_LIMIT', 50):
            self.assertIn('最多容纳 50 首', self.rejected('/api/batch', {'request_id': 'all-styles-full-12345', 'items': [self.item(all_styles=True)]}))
        self.assertEqual(list(service.JOBS.iterdir()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
