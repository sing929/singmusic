"""The v1.6.0 style catalog, singers and smart defaults: catalog integrity, engine requests, the
smart-default rules, batches over HTTP, and which singers keep their own voice."""
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

SWAP_ALL = {'versions': [True] * 20, 'octave': 'auto'}


class CatalogTest(unittest.TestCase):
    def test_every_style_is_grouped_once_and_reaches_the_engine(self):
        grouped = [s for group in controls.CATALOG['groups'] for s in group['styles']]
        self.assertEqual(sorted(grouped), sorted(controls.CATALOG['styles']))
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertGreaterEqual(len(grouped), 50)
        self.assertEqual({g['id'] for g in controls.CATALOG['groups']},
                         {'classic', 'asian-dj', 'edm', 'rap', 'orchestra', 'asian', 'band', 'world', 'chill'})
        for name, info in controls.CATALOG['styles'].items():
            with self.subTest(style=name):
                self.assertLessEqual(len(name), 40)
                self.assertTrue(info['tags'] and info['zh'])
                self.assertEqual(controls.build_caption(name), info['tags'])
                self.assertEqual(controls.build_caption(name.upper()), info['tags'])
                self.assertEqual(controls.build_caption(name, 'guitar only'), name + ', guitar only')
                request = controls.build_request({'controls_version': 1, 'mode': 'remix', 'caption': controls.build_caption(name), 'duration': 30,
                    'seed': 1, 'change_strength': 75, 'melody_mode': 'reference'}, 'SCORE', 'la', 6)
                self.assertTrue(request['style'].startswith(info['tags']))
                self.assertTrue(set(info.get('defaults', {})) <= {'mode', 'melody_mode', 'semitones'})
        self.assertEqual(len(controls.DEFAULT_STYLES), 20)
        self.assertEqual(controls.DEFAULT_STYLES[:10], ['R&B', 'Jazz', 'Lo-fi', 'Acoustic', 'Remix', 'Phonk', 'Hardstyle', 'Hardtekk', 'Soul', 'Funk'])
        self.assertTrue(all(s in controls.CATALOG['styles'] for s in controls.DEFAULT_STYLES))
        self.assertTrue(all(p['style'] in controls.CATALOG['styles'] and p['hint'] for p in controls.SUITE))

    def test_new_singers_reach_the_engine_request(self):
        self.assertEqual(list(controls.GENDERS), ['auto', 'male', 'female', 'kid', 'duet', 'choir', 'opera'])
        for singer, marker in (('kid', 'child vocals'), ('duet', 'duet'), ('choir', 'choir'), ('opera', 'bel canto'), ('female', 'female vocals')):
            with self.subTest(singer=singer):
                request = controls.build_request({'controls_version': 1, 'mode': 'remix', 'caption': 'Jazz', 'duration': 30, 'seed': 1,
                    'change_strength': 75, 'melody_mode': 'reference', 'gender': singer, 'tone': 'warm'}, 'SCORE', 'la', 6)
                self.assertIn(marker, request['style'])
                self.assertIn('warm, mellow', request['style'])

    def test_smart_default_rules(self):
        plain = {'mode': 'remix', 'melody_mode': 'reference', 'adjust': {'semitones': 0, 'tempo': 100, 'speed_mode': 'independent', 'reverb': 0}, 'gender': 'auto'}
        smart = controls.apply_smart_defaults
        result, applied = smart(plain, 'Vinahouse')
        # Smart defaults never change the tempo (v1.8.2): a DJ style only keeps the original singer.
        self.assertEqual(applied, {'mode': 'preserve'})
        self.assertEqual((result['mode'], result['adjust']), ('preserve', plain['adjust']))
        self.assertEqual(smart(plain, 'vinahouse')[1], applied)
        self.assertEqual(smart(plain, 'Vinahouse', ['mode'])[1], {})
        self.assertEqual(smart(plain, 'Vinahouse', ['adjust'])[1], {'mode': 'preserve'})
        self.assertEqual(smart({**plain, 'adjust': {**plain['adjust'], 'tempo': 90}}, 'Vinahouse')[0]['adjust']['tempo'], 90)
        self.assertEqual(smart({**plain, 'instrumental': True}, 'Vinahouse')[1], {})
        self.assertIsNone(smart({**plain, 'adjust': None}, '抖音 DJ')[0]['adjust'])
        for name, info in controls.CATALOG['styles'].items():
            self.assertNotIn('tempo', info.get('defaults', {}), name)
        self.assertEqual(smart({**plain, 'mode': 'preserve'}, 'Trap')[1], {'mode': 'remix', 'melody_mode': 'free'})
        self.assertEqual(smart(plain, 'Hip-hop', ['melody_mode'])[1], {})
        self.assertEqual(smart({**plain, 'mode': 'preserve'}, 'Boom Bap', ['mode'])[1], {})
        self.assertEqual(smart({**plain, 'instrumental': True}, 'Boom Bap')[1], {'melody_mode': 'free'})
        self.assertEqual(smart(plain, 'Jazz'), (plain, {}))
        self.assertEqual(smart(plain, 'My own style')[1], {})
        kid = {**plain, 'gender': 'kid'}
        self.assertEqual(smart(kid, 'Lo-fi')[1], {'semitones': 2})
        self.assertEqual(smart(kid, 'Trap')[1], {'melody_mode': 'free', 'semitones': 2})
        # A DJ remix keeps the original singer, so the kid voice and its key do not apply.
        self.assertEqual(smart(kid, 'Funkot')[1], {'mode': 'preserve'})
        self.assertEqual(smart({**kid, 'adjust': {**plain['adjust'], 'semitones': -1}}, 'Lo-fi')[1], {})
        self.assertEqual(smart({**kid, 'instrumental': True}, 'Lo-fi')[1], {})


class SmartBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-styles-')
        root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=root, DATA=root / 'data', JOBS=root / 'data/jobs', TRACKS=root / 'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True)
        service.TRACKS.mkdir()
        self.tid = 'a' * 32
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
        with self.client.open(request, timeout=10) as response:
            return json.load(response)

    def rejected(self, route, payload):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api(route, payload)
        self.assertEqual(error.exception.code, 400)
        return json.load(error.exception)['error']

    def batch(self, key, styles, **item):
        return self.api('/api/batch', {'request_id': key, 'items': [{'track_id': self.tid, 'instructions': '', 'mode': 'remix',
            'melody_mode': 'reference', 'adjust': {'semitones': 0, 'tempo': 100, 'speed_mode': 'independent', 'reverb': 0},
            'variants': [{'style': s} for s in styles], **item}]})['jobs']

    def config(self, job):
        return service.read(service.JOBS / job['id'] / 'config.json')

    def test_shared_switch_and_kid_singer_are_saved(self):
        self.assertIs(self.api('/api/state')['settings']['smart_defaults'], True)
        self.assertIs(self.api('/api/settings', {'smart_defaults': False})['smart_defaults'], False)
        self.assertIn('无效', self.rejected('/api/settings', {'smart_defaults': 'on'}))
        self.assertEqual(self.api('/api/settings', {'gender': 'kid'})['gender'], 'kid')
        self.assertEqual(self.api('/api/tracks/' + self.tid, {'gender': 'choir'})['gender'], 'choir')
        self.assertIn('声音', self.rejected('/api/tracks/' + self.tid, {'gender': 'robot'}))

    def test_versions_get_their_style_defaults(self):
        jazz, vina, trap = self.batch('smart-batch-12345678', ['Jazz', 'Vinahouse', 'Trap'], smart_defaults=True)
        configs = [self.config(j) for j in (jazz, vina, trap)]
        self.assertEqual([(c['mode'], c['melody_mode']) for c in configs], [('remix', 'reference'), ('preserve', 'reference'), ('remix', 'free')])
        self.assertEqual([c['smart_defaults'] for c in configs], [{}, {'mode': 'preserve'}, {'melody_mode': 'free'}])
        self.assertEqual(vina['smart_defaults'], {'mode': 'preserve'})
        self.assertIsNone(configs[1]['adjust'])
        self.assertAlmostEqual(configs[1]['duration'], 30, places=3)
        self.assertEqual(vina['output_name'], 'Vinahouse Song')
        # Analysis is shared only between versions with the same source settings.
        self.assertNotIn('reuse_from', configs[0])
        self.assertNotIn('reuse_from', configs[1])
        self.assertEqual(configs[2]['reuse_from'], jazz['id'])
        # What the song sets for itself wins; without the switch nothing changes.
        kept = self.batch('smart-explicit-123456', ['Trap'], smart_defaults=True, mode='preserve', explicit=['mode'])
        self.assertEqual((self.config(kept[0])['mode'], self.config(kept[0])['melody_mode'], kept[0]['smart_defaults']), ('preserve', 'reference', {}))
        off = self.batch('smart-off-1234567890', ['Vinahouse', 'Trap'])
        self.assertEqual([(self.config(j)['mode'], self.config(j)['melody_mode'], j['smart_defaults']) for j in off],
                         [('remix', 'reference', {}), ('remix', 'reference', {})])
        for bad in ({'smart_defaults': 'yes'}, {'smart_defaults': True, 'explicit': ['tempo']}, {'smart_defaults': True, 'explicit': 'mode'}):
            with self.subTest(bad=bad):
                self.assertIn('智能默认', self.rejected('/api/batch', {'request_id': 'smart-bad-1234567890', 'items': [{'track_id': self.tid,
                    'instructions': '', 'variants': [{'style': 'Jazz'}], **bad}]}))

    def test_kid_duet_and_choir_keep_their_own_voice(self):
        for singer in ('kid', 'duet', 'choir', 'opera', 'female'):
            with self.subTest(singer=singer):
                job = self.batch(f'singer-{singer}-1234567890', ['Lo-fi'], gender=singer, voice_swap=SWAP_ALL)[0]
                config = self.config(job)
                own = singer in ('kid', 'duet', 'choir')
                self.assertEqual(config['voice'] is None, own)
                self.assertEqual((config['kid'], job['kid']), (singer == 'kid', singer == 'kid'))
                self.assertEqual(job['output_name'], 'Lo-fi Song (Kid Voice)' if singer == 'kid' else 'Lo-fi Song' if own else 'Lo-fi Song (Original Voice)')
        higher = self.batch('singer-kid-smart-123456', ['Lo-fi'], gender='kid', smart_defaults=True)[0]
        self.assertEqual((higher['smart_defaults'], higher['output_name']), ({'semitones': 2}, 'Lo-fi Song (Kid Voice, Key +2)'))
        for mode, instrumental in (('preserve', False), ('remix', True)):
            with self.subTest(mode=mode, instrumental=instrumental):
                job = self.batch(f'singer-kid-{mode}-{instrumental}-12', ['Lo-fi'], gender='kid', mode=mode, instrumental=instrumental, smart_defaults=True)[0]
                self.assertEqual((self.config(job)['gender'], job['kid'], job['smart_defaults']), ('auto', False, {}))

    def test_queue_room_follows_the_limit(self):
        with patch.object(service, 'QUEUE_LIMIT', 3):
            self.batch('queue-room-1234567890', ['Jazz', 'Soul'])
            self.assertIn('最多容纳 3 首', self.rejected('/api/batch', {'request_id': 'queue-full-1234567890', 'items': [{'track_id': self.tid,
                'instructions': '', 'variants': [{'style': 'Jazz'}, {'style': 'Soul'}]}]}))
        self.assertEqual(service.QUEUE_LIMIT, 2000)


if __name__ == '__main__':
    unittest.main(verbosity=2)
