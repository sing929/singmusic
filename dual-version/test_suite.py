"""Orchestral suite: quiet switch points, beat alignment and the crossfaded render; the three suite
parts in a batch, the automatic suite job once all parts are done, and the shared / per-song setting."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import numpy as np
import soundfile as sf

import engine
import generation_controls as controls
import service
import server
import suite

SR = 48000


def part(seconds, pitch, delay=0., quiet=(), seed=0):
    """A clicking beat at 120 BPM with a held note of `pitch`; silent around the `quiet` times."""
    count = int(seconds * SR)
    t = np.arange(count) / SR
    audio = .15 * np.sin(2 * np.pi * pitch * t)
    click = np.random.default_rng(seed).standard_normal(int(.02 * SR)) * np.exp(-np.arange(int(.02 * SR)) / (.004 * SR)) * .6
    for n in range(int(seconds * 2)):
        a = int((n * .5 + delay) * SR)
        if a < count:
            audio[a:a + len(click)] += click[:count - a]
    for moment in quiet:
        audio[int((moment - .4) * SR):int((moment + .4) * SR)] *= .02
    return np.column_stack((audio, audio)).astype(np.float32)


def pitch_of(audio):
    mono = audio.mean(axis=1)
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    frequencies = np.fft.rfftfreq(len(mono), 1 / SR)
    band = (frequencies > 150) & (frequencies < 1000)
    return float(frequencies[band][np.argmax(spectrum[band])])


class SuiteRenderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-suite-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_lag_and_quiet_point(self):
        steady = part(12, 220)
        late = part(12, 330, delay=.2)
        self.assertAlmostEqual(suite.lag(steady, late, 6 * SR) / SR, .2, delta=.015)
        self.assertAlmostEqual(suite.lag(late, steady, 6 * SR) / SR, -.2, delta=.015)
        ramp = np.arange(10, dtype=np.float32)[:, None].repeat(2, axis=1)
        np.testing.assert_array_equal(suite.shift(ramp, 3)[:, 0], [3, 4, 5, 6, 7, 8, 9, 0, 0, 0])
        np.testing.assert_array_equal(suite.shift(ramp, -3)[:, 0], [0, 0, 0, 0, 1, 2, 3, 4, 5, 6])
        parts = [part(30, 220, quiet=(11.,)), part(30, 330, quiet=(11.,)), part(30, 440)]
        self.assertAlmostEqual(suite.quiet_point(parts, 10 * SR) / SR, 11., delta=.45)

    def test_render_plays_soft_fast_grand_in_turn(self):
        paths = []
        for index, (pitch, delay) in enumerate(((220., 0.), (330., .15), (440., 0.))):
            path = self.root / f'part-{index}.wav'
            sf.write(path, part(36, pitch, delay=delay, quiet=(13., 23.), seed=index), SR, subtype='PCM_24')
            paths.append(path)
        output = self.root / 'suite.wav'
        report = suite.render(paths, output)
        result, rate = sf.read(output, always_2d=True)
        self.assertEqual((rate, len(result)), (SR, 36 * SR))
        self.assertAlmostEqual(report['output_seconds'], 36)
        first, second = report['switch_seconds']
        self.assertAlmostEqual(first, 13., delta=.45)
        self.assertAlmostEqual(second, 23., delta=.45)
        # The fast part is 150 ms late and is moved; the grand part then lines up with it as is.
        self.assertAlmostEqual(report['lags_ms'][0], 150, delta=15)
        self.assertAlmostEqual(report['lags_ms'][1], 0, delta=15)
        self.assertAlmostEqual(pitch_of(result[2 * SR:10 * SR]), 220, delta=3)
        self.assertAlmostEqual(pitch_of(result[16 * SR:20 * SR]), 330, delta=3)
        self.assertAlmostEqual(pitch_of(result[27 * SR:34 * SR]), 440, delta=3)
        self.assertLessEqual(float(np.max(np.abs(result))), 1.)


class SuiteServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-suite-service-')
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=self.root, DATA=self.root / 'data', JOBS=self.root / 'data/jobs', TRACKS=self.root / 'data/tracks')
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

    def finish(self, job, pitch, seconds=30.):
        """Give a queued generated job a result and let the service complete it."""
        directory = service.JOBS / job['id']
        name = job['output_name'] + '.wav'
        sf.write(directory / name, part(seconds, pitch, quiet=(10., 20.)), SR, subtype='PCM_24')
        service.save(directory / 'result.json', {'files': {'result': name}, 'checks': {'result': {'seconds': seconds}}})
        with service.LOCK:
            return service.finish_attempt(directory, 0)

    def test_setting_and_per_song_switch(self):
        settings = self.api('/api/state')['settings']
        self.assertEqual(settings['suite'], {'enabled': False, 'styles': [p['style'] for p in controls.SUITE]})
        saved = self.api('/api/settings', {'suite': {'enabled': True, 'styles': ['Classical Piano', 'Waltz', 'Cinematic Orchestra']}})
        self.assertEqual(saved['suite'], {'enabled': True, 'styles': ['Classical Piano', 'Waltz', 'Cinematic Orchestra']})
        for bad in ({'enabled': 'yes'}, {'enabled': True, 'styles': ['A', 'B']}, {'enabled': True, 'styles': ['A', 'a', 'B']},
                    {'enabled': True, 'styles': ['A', 'B', '']}, {'on': True}, True):
            with self.subTest(bad=bad):
                self.rejected('/api/settings', {'suite': bad})
        self.assertEqual(self.api('/api/state')['settings']['suite'], saved['suite'])
        service.save(service.DATA / 'settings.json', {**service.read(service.DATA / 'settings.json'), 'suite': {'enabled': 1}})
        self.assertFalse(self.api('/api/state')['settings']['suite']['enabled'])
        self.assertIs(self.api('/api/tracks/' + self.tid, {'suite': True})['suite'], True)
        self.assertIsNone(self.api('/api/tracks/' + self.tid, {'suite': None})['suite'])
        self.assertIn('组曲', self.rejected('/api/tracks/' + self.tid, {'suite': 'on'}))

    def test_suite_parts_follow_the_versions_and_the_suite_is_queued_once(self):
        styles = ['String Quartet', 'Baroque', 'Cinematic Orchestra']
        with patch('yue2_engine.available', return_value=True), patch.object(service.voice_swap, 'available', return_value=True):
            jobs = self.api('/api/batch', {'request_id': 'suite-batch-123456789', 'items': [{'track_id': self.tid, 'instructions': '',
                'melody_mode': 'free', 'speed_variants': True, 'voice_swap': {'versions': [True] * 20, 'octave': 'auto'},
                'suite': {'styles': styles}, 'variants': [{'style': 'Jazz'}, {'style': 'Cinematic Orchestra'}]}]})['jobs']
            self.assertEqual([j['variant'] for j in jobs], list('ABCDE'))
            parts = jobs[2:]
            self.assertEqual([j['output_name'] for j in parts], ['String Quartet Song (Suite I)', 'Baroque Song (Suite II)', 'Cinematic Orchestra Song (Suite III)'])
            self.assertEqual(len({j['suite_id'] for j in parts}), 1)
            self.assertEqual([j['suite_part'] for j in parts], [0, 1, 2])
            for job, entry in zip(parts, controls.SUITE):
                config = service.read(service.JOBS / job['id'] / 'config.json')
                self.assertEqual((config['melody_mode'], config['speed_variants'], config['voice']), ('reference', False, None))
                self.assertTrue(config['caption'].endswith(entry['hint']))
                self.assertIn(controls.STYLE_TAGS[config['style']], config['caption'])
            self.assertEqual(service.read(service.JOBS / jobs[0]['id'] / 'config.json')['melody_mode'], 'free')
            self.assertNotIn('suite_id', jobs[0])
            self.assertIn('超过 20', self.rejected('/api/batch', {'request_id': 'suite-too-many-1234567', 'items': [{'track_id': self.tid,
                'instructions': '', 'suite': {'styles': styles}, 'variants': [{'style': s} for s in controls.DEFAULT_STYLES[:18]]}]}))
            for bad in ({'styles': styles[:2]}, {'styles': ['Waltz', 'waltz', 'Baroque']}, {'enabled': True, 'styles': styles}, 'on'):
                with self.subTest(bad=bad):
                    self.assertIn('组曲', self.rejected('/api/batch', {'request_id': 'suite-bad-12345678901', 'items': [{'track_id': self.tid,
                        'instructions': '', 'suite': bad, 'variants': [{'style': 'Jazz'}]}]}))
            # A request cannot make up its own suite part.
            forged = self.api('/api/batch', {'request_id': 'suite-forged-123456789', 'items': [{'track_id': self.tid, 'instructions': '',
                'suite_part': {'id': 'f' * 32, 'part': 0}, 'smart_applied': {'mode': 'preserve'}, 'variants': [{'style': 'Jazz'}]}]})['jobs']
            self.assertNotIn('suite_id', forged[0])
            self.assertEqual(forged[0]['smart_defaults'], {})
        self.finish(parts[0], 220)
        self.finish(parts[1], 330)
        self.assertEqual([j for j in service.pending_jobs('adjust') if j['kind'] == 'suite'], [])
        self.finish(parts[2], 440)
        queued = [j for j in service.pending_jobs('adjust') if j['kind'] == 'suite']
        self.assertEqual(len(queued), 1)
        made = queued[0]
        self.assertEqual((made['style'], made['output_name'], made['group_id'], made['parent_ids'], made['variant']),
                         ('Orchestral Suite', 'Orchestral Suite Song', parts[0]['pair_id'], [p['id'] for p in parts], None))
        self.assertEqual(made['suite_styles'], styles)
        # Finishing a part again (a restart) never queues a second suite.
        with service.LOCK:
            service.finish_attempt(service.JOBS / parts[2]['id'], 0)
        self.assertEqual(len([j for j in service.jobs() if j.get('kind') == 'suite']), 1)
        # The parts make no speed versions; the suite renders as a new work.
        self.assertEqual([j['kind'] for j in service.pending_jobs('adjust')], ['suite'])
        directory = service.JOBS / made['id']
        engine.run_pipeline(directory, service.read(directory / 'config.json'))
        result = service.read(directory / 'result.json')
        self.assertEqual(result['files']['result'], 'Orchestral Suite Song.wav')
        self.assertIn('组曲', result['notice'])
        report = service.read(directory / 'suite.json')
        self.assertAlmostEqual(report['switch_seconds'][0], 10., delta=.5)
        self.assertAlmostEqual(report['switch_seconds'][1], 20., delta=.5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
