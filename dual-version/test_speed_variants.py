"""Speed Up / Slowed / Super Slowed versions of every finished version: presets, the shared and
per-batch switch over HTTP, automatic queueing when a version completes, and a real FFmpeg render."""
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

import audio_adjust as adjust
import engine
import service
import server
import voice_swap

SR = 48000
NAMES = ['Speed Up', 'Slowed', 'Super Slowed']


def tone(seconds, frequency=220., amplitude=.3):
    t = np.arange(int(seconds * SR)) / SR
    wave = amplitude * np.sin(2 * np.pi * frequency * t)
    return np.column_stack((wave, wave))


def dominant(audio):
    middle = audio[len(audio) // 4:3 * len(audio) // 4].mean(axis=1)
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    return np.fft.rfftfreq(len(middle), 1 / SR)[np.argmax(spectrum)]


class SpeedVariantsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-speed-')
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=self.root, DATA=self.root / 'data',
            JOBS=self.root / 'data/jobs', TRACKS=self.root / 'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True)
        service.TRACKS.mkdir()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def http(self):
        http = service.ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
        previous = server.PORT
        server.PORT = http.server_port
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def api(route, payload=None):
            request = urllib.request.Request(f'http://127.0.0.1:{server.PORT}' + route,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={'X-Sing-Token': service.TOKEN, 'Content-Type': 'application/json'})
            with client.open(request, timeout=10) as response:
                return json.load(response)

        def rejected(route, payload):
            with self.assertRaises(urllib.error.HTTPError) as error:
                api(route, payload)
            self.assertEqual(error.exception.code, 400)
            return json.load(error.exception)['error']

        def stop():
            http.shutdown()
            http.server_close()
            thread.join()
            server.PORT = previous
        return api, rejected, stop

    def finished_version(self, identifier='e' * 32, seconds=6., filename='Lo-fi Song.wav', **meta):
        """A generated version whose engine run just ended successfully but whose job.json is still 'running'."""
        directory = service.JOBS / identifier
        directory.mkdir()
        sf.write(directory / filename, tone(seconds), SR, subtype='PCM_24')
        service.save(directory / 'result.json', {'files': {'result': filename},
            'checks': {'result': {'seconds': seconds}}})
        service.save(directory / 'job.json', {'id': identifier, 'title': 'Song', 'style': 'Lo-fi', 'state': 'running',
            'mode': 'remix', 'variant': 'A', 'pair_id': 'p' * 32, 'duration': seconds, 'created_at': 1, 'attempt': 1,
            'speed_variants': True, 'config': {'format': 'both', 'instrumental': False}, **meta})
        return directory

    def test_presets_are_exact_named_independent_adjustments(self):
        self.assertEqual(list(adjust.PRESETS), NAMES)
        for name, (tempo, semitones) in zip(NAMES, ((112, 1), (85, -2), (75, -3))):
            value = adjust.validate(adjust.preset(name))
            self.assertEqual(value, {'semitones': semitones, 'tempo': tempo, 'speed_mode': 'independent', 'reverb': 0, 'preset': name})
            self.assertEqual((adjust.label(value), adjust.speed(value), adjust.semitones(value)), (name, tempo / 100, float(semitones)))
            self.assertTrue(adjust.changes_source(value))
        self.assertAlmostEqual(adjust.output_seconds(30, adjust.preset('Slowed')), 30 / .85, places=4)
        # Plain adjustments never gain a preset name, and a name must match its preset exactly.
        self.assertNotIn('preset', adjust.validate({**adjust.DEFAULT, 'tempo': 112, 'semitones': 1}))
        for bad in ({**adjust.PRESETS['Slowed'], 'preset': 'Speed Up'}, {'preset': 'Nope'}, {**adjust.PRESETS['Speed Up'], 'preset': 'Speed Up', 'reverb': 20}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                adjust.validate(bad)

    def test_shared_switch_and_batch_items_persist_the_setting(self):
        tid = 'a' * 32
        (service.TRACKS / tid).mkdir()
        service.save(service.TRACKS / tid / 'track.json', {'id': tid, 'title': 'Song', 'duration': 30, 'source': 'unused.wav'})
        api, rejected, stop = self.http()
        try:
            self.assertIs(api('/api/state')['settings']['speed_variants'], True)
            self.assertIs(api('/api/settings', {'speed_variants': False})['speed_variants'], False)
            self.assertIs(api('/api/state')['settings']['speed_variants'], False)
            self.assertIn('无效', rejected('/api/settings', {'speed_variants': 'yes'}))
            service.save(service.DATA / 'settings.json', {**service.read(service.DATA / 'settings.json'), 'speed_variants': 'old'})
            self.assertIs(api('/api/state')['settings']['speed_variants'], True)
            with patch('yue2_engine.available', return_value=True):
                off, on = api('/api/batch', {'request_id': 'speed-batch-1234567', 'items': [
                    {'track_id': tid, 'instructions': '', 'speed_variants': False, 'variants': [{'style': 'Lo-fi'}]},
                    {'track_id': tid, 'instructions': '', 'variants': [{'style': 'Jazz'}]}]})['jobs']
                self.assertIs(off['speed_variants'], False)
                self.assertIs(on['speed_variants'], True)
                self.assertIs(service.read(service.JOBS / off['id'] / 'config.json')['speed_variants'], False)
                self.assertIn('变速版本', rejected('/api/batch', {'request_id': 'speed-batch-7654321', 'items': [
                    {'track_id': tid, 'instructions': '', 'speed_variants': 1, 'variants': [{'style': 'Lo-fi'}]}]}))
            self.assertEqual(len(service.jobs()), 2)
        finally:
            stop()

    def test_completed_version_queues_three_named_variants_once(self):
        parent = self.finished_version(voice={'target': 'original', 'octave': 'auto'})
        meta = service.finish_attempt(parent, 0)
        self.assertEqual(meta['state'], 'completed')
        self.assertEqual(len(meta['speed_variant_ids']), 3)
        self.assertNotIn('speed_variant_errors', meta)
        self.assertEqual(service.read(parent / 'job.json')['speed_variant_ids'], meta['speed_variant_ids'])
        pending = service.pending_jobs('adjust')
        self.assertEqual(service.pending_jobs('generate'), [])
        # Oldest first in the worker: Speed Up, then Slowed, then Super Slowed.
        self.assertEqual([j['id'] for j in reversed(pending)], meta['speed_variant_ids'])
        for job, name in zip(reversed(pending), NAMES):
            with self.subTest(name=name):
                self.assertEqual((job['kind'], job['auto'], job['parent_id'], job['group_id'], job['variant']),
                    ('adjust', True, parent.name, 'p' * 32, 'A'))
                self.assertEqual(job['adjust'], adjust.preset(name))
                self.assertEqual(job['output_name'], f'Lo-fi Song ({voice_swap.LABEL}, {name})')
                self.assertAlmostEqual(job['duration'], adjust.output_seconds(6., adjust.preset(name)))
                config = service.read(service.JOBS / job['id'] / 'config.json')
                self.assertEqual((Path(config['source']), config['adjust'], config['output_name']),
                    (parent / 'Lo-fi Song.wav', adjust.preset(name), job['output_name']))
        # Finishing again (for example after a restart) never duplicates the variants.
        service.finish_attempt(parent, 0)
        self.assertEqual(len(service.pending_jobs('adjust')), 3)
        self.assertEqual(len(list(service.JOBS.iterdir())), 4)

    def test_nothing_is_queued_when_off_failed_or_derived(self):
        off = self.finished_version('1' * 32, speed_variants=False)
        self.assertEqual(service.finish_attempt(off, 0)['state'], 'completed')
        derived = self.finished_version('2' * 32, kind='adjust', parent_id='1' * 32)
        self.assertEqual(service.finish_attempt(derived, 0)['state'], 'completed')
        failed = self.finished_version('3' * 32)
        (failed / 'result.json').unlink()
        self.assertEqual(service.finish_attempt(failed, 1)['state'], 'queued')
        for directory in (off, derived, failed):
            self.assertNotIn('speed_variant_ids', service.read(directory / 'job.json'))
        self.assertEqual(service.pending_jobs('adjust'), [])
        self.assertEqual(sorted(p.name for p in service.JOBS.iterdir()), sorted([off.name, derived.name, failed.name]))

    def test_variant_problems_are_recorded_not_raised(self):
        parent = self.finished_version()
        with patch.object(service, 'queue_adjustment', side_effect=ValueError('队列已满')):
            meta = service.finish_attempt(parent, 0)
        self.assertEqual((meta['state'], meta['speed_variant_ids']), ('completed', []))
        self.assertEqual([e['preset'] for e in meta['speed_variant_errors']], NAMES)
        self.assertEqual(service.pending_jobs('adjust'), [])

    def test_speed_up_renders_faster_and_one_semitone_higher(self):
        parent = self.finished_version()
        original = (parent / 'Lo-fi Song.wav').read_bytes()
        meta = service.finish_attempt(parent, 0)
        directory = service.JOBS / meta['speed_variant_ids'][0]
        config = service.read(directory / 'config.json')
        engine.run_pipeline(directory, config)
        result = service.read(directory / 'result.json')
        self.assertEqual(result['files'], {'result': 'Lo-fi Song (Speed Up).wav', 'mp3': 'Lo-fi Song (Speed Up).mp3'})
        self.assertAlmostEqual(result['checks']['result']['seconds'], 6 / 1.12, places=3)
        self.assertIn('未重新生成', result['notice'])
        audio, _ = sf.read(directory / result['files']['result'], always_2d=True)
        self.assertAlmostEqual(dominant(audio), 220 * 2 ** (1 / 12), delta=5)
        self.assertEqual(service.read(directory / 'adjust.json')['result']['speed'], 1.12)
        self.assertEqual((parent / 'Lo-fi Song.wav').read_bytes(), original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
