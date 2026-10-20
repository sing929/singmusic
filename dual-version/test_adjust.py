"""Key / tempo / reverb: real FFmpeg rendering, pipeline order and derived works over HTTP."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import unquote
from unittest.mock import patch

import numpy as np
import soundfile as sf

import audio_adjust as adjust
import engine
import service
import server

SR = 48000


def tone(seconds, frequency=220., amplitude=.3):
    t = np.arange(int(seconds * SR)) / SR
    wave = amplitude * np.sin(2 * np.pi * frequency * t)
    return np.column_stack((wave, wave))


def dominant(audio):
    middle = audio[len(audio) // 4:3 * len(audio) // 4].mean(axis=1)
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    return np.fft.rfftfreq(len(middle), 1 / SR)[np.argmax(spectrum)]


def setting(**values):
    return {**adjust.DEFAULT, **values}


class AdjustTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-adjust-')
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

        def stop():
            http.shutdown()
            http.server_close()
            thread.join()
            server.PORT = previous
        return api, client, stop

    def finished_work(self, identifier='e' * 32, seconds=6., filename='Lo-fi Song.wav', **meta):
        directory = service.JOBS / identifier
        directory.mkdir()
        sf.write(directory / filename, tone(seconds), SR, subtype='PCM_24')
        service.save(directory / 'result.json', {'files': {'result': filename},
            'checks': {'result': {'seconds': seconds}}})
        service.save(directory / 'job.json', {'id': identifier, 'title': 'Song', 'style': 'Lo-fi', 'state': 'completed',
            'mode': 'remix', 'variant': 'A', 'pair_id': 'p' * 32, 'duration': seconds, 'created_at': 1,
            'config': {'format': 'both', 'instrumental': False}, **meta})
        return directory

    def test_validation_labels_and_lengths(self):
        self.assertEqual(adjust.validate({}), adjust.DEFAULT)
        self.assertIsNone(adjust.validate(None, inherit=True))
        self.assertEqual(adjust.validate({'semitones': 2.0})['semitones'], 2)
        for bad in ({'semitones': 13}, {'semitones': -13}, {'semitones': 1.5}, {'semitones': True}, {'semitones': '2'},
                    {'tempo': 49}, {'tempo': 151}, {'tempo': float('nan')}, {'reverb': 101}, {'reverb': -1},
                    {'speed_mode': 'vinyl'}, {'volume': 3}, None, [], 'fast'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                adjust.validate(bad)
        self.assertEqual(adjust.label(setting(semitones=-2, tempo=90)), 'Key -2, Tempo 90%')
        self.assertEqual(adjust.label(setting(semitones=3)), 'Key +3')
        self.assertEqual(adjust.label(setting(speed_mode='linked', tempo=80, reverb=40)), 'Slowed 80%, Reverb 40')
        self.assertEqual(adjust.label(setting(speed_mode='linked', tempo=120, semitones=5)), 'Sped up 120%')
        self.assertEqual(adjust.label(setting(speed_mode='linked', tempo=80, reverb=40), reverb=False), 'Slowed 80%')
        self.assertFalse(adjust.changes_source(setting(speed_mode='linked', semitones=4)))
        self.assertFalse(adjust.changes_source(setting(reverb=50)))
        self.assertTrue(adjust.changes_anything(setting(reverb=50)))
        self.assertAlmostEqual(adjust.semitones(setting(speed_mode='linked', tempo=50)), -12)
        self.assertEqual(adjust.output_seconds(30, setting(tempo=80)), 37.5)
        self.assertEqual(adjust.output_seconds(30, None), 30)

    def test_filters_keep_total_speed_for_both_methods(self):
        for semitones, tempo in ((12, 50), (-12, 150), (5, 100), (0, 75), (-7, 130)):
            value = setting(semitones=semitones, tempo=tempo)
            rubberband, method = adjust.audio_filter(value, True)
            self.assertEqual(method, 'rubberband')
            self.assertIn(f'tempo={tempo / 100:.6f}', rubberband)
            fallback, method = adjust.audio_filter(value, False)
            self.assertEqual(method, 'atempo')
            factor = 1.
            for part in fallback.split(','):
                name, number = part.split('=')
                if name == 'asetrate':
                    factor *= int(number) / SR
                if name == 'atempo':
                    self.assertTrue(.5 - 1e-9 <= float(number) <= 2 + 1e-9, part)
                    factor *= float(number)
            self.assertAlmostEqual(factor, tempo / 100, places=5)
        self.assertEqual(adjust.audio_filter(setting(speed_mode='linked', tempo=80), True), ('asetrate=38400,aresample=48000', 'resample'))
        self.assertEqual(adjust.audio_filter(setting(reverb=60), True), (None, None))

    def test_real_render_changes_pitch_length_and_adds_reverb(self):
        source = self.root / 'tone.wav'
        sf.write(source, tone(4), SR, subtype='PCM_16')
        methods = [False] + ([True] if adjust.has_rubberband(engine.FFMPEG) else [])
        for rubberband in methods:
            with patch.object(adjust, 'has_rubberband', return_value=rubberband):
                for value, seconds, frequency in ((setting(semitones=12), 4, 440), (setting(tempo=50), 8, 220),
                        (setting(semitones=-5, tempo=125), 3.2, 220 * 2 ** (-5 / 12)), (setting(speed_mode='linked', tempo=80), 5, 176)):
                    with self.subTest(rubberband=rubberband, value=value):
                        target = self.root / 'out.wav'
                        report = adjust.render(source, target, value, engine.FFMPEG)
                        audio, rate = sf.read(target, always_2d=True)
                        self.assertEqual((rate, audio.shape[1], sf.info(target).subtype), (SR, 2, 'PCM_24'))
                        self.assertEqual(len(audio), round(seconds * SR))
                        self.assertAlmostEqual(dominant(audio), frequency, delta=frequency * .02)
                        self.assertEqual(report['method'], 'resample' if value['speed_mode'] == 'linked' else 'rubberband' if rubberband else 'atempo')
                        self.assertLessEqual(np.max(np.abs(audio)), .99)
        burst = np.zeros((SR * 3, 2))
        burst[:SR // 4] = tone(.25)
        sf.write(source, burst, SR, subtype='PCM_16')
        dry = self.root / 'dry.wav'
        wet = self.root / 'wet.wav'
        adjust.render(source, dry, setting(), engine.FFMPEG)
        report = adjust.render(source, wet, setting(reverb=60), engine.FFMPEG)
        tail = slice(int(.5 * SR), int(1.5 * SR))
        dry_audio, wet_audio = sf.read(dry, always_2d=True)[0], sf.read(wet, always_2d=True)[0]
        self.assertLess(np.sqrt(np.mean(dry_audio[tail] ** 2)), 1e-4)
        self.assertGreater(np.sqrt(np.mean(wet_audio[tail] ** 2)), 5e-3)
        self.assertGreater(np.abs(wet_audio[tail, 0] - wet_audio[tail, 1]).max(), 1e-3)
        self.assertEqual((report['reverb'], len(wet_audio)), (60, SR * 3))

    def test_pipeline_shifts_source_before_generation_and_reverb_after(self):
        source = self.root / 'song.wav'
        sf.write(source, tone(6), SR, subtype='PCM_16')
        value = setting(semitones=2, tempo=80, reverb=30)
        config = {'engine': 'yue2', 'mode': 'remix', 'instrumental': True, 'profile': 'fast', 'lyrics': '', 'auto_lyrics': False,
            'source': str(source), 'start': 0, 'source_duration': 6, 'duration': adjust.output_seconds(6, value), 'adjust': value,
            'melody_mode': 'free', 'seed': 1}
        stages, real_run = [], subprocess.run
        def run(args, **kwargs):
            if '--stage' not in args:
                return real_run(args, **kwargs)
            stages.append(args[-1])
            if args[-1] == 'generate':
                generated, _ = sf.read(self.root / 'source.wav', always_2d=True)
                self.assertEqual(len(generated), round(7.5 * SR))
                sf.write(self.root / 'generated.wav', generated, SR, subtype='PCM_24')
        with patch.object(engine.subprocess, 'run', side_effect=run):
            engine.run_pipeline(self.root, config)
        self.assertEqual(stages, ['generate', 'validate'])
        report = service.read(self.root / 'adjust.json')
        self.assertEqual(report['source']['reverb'], 0)
        self.assertAlmostEqual(report['source']['semitones'], 2)
        self.assertEqual(sf.info(self.root / 'source.wav').subtype, 'PCM_16')
        self.assertEqual((report['result']['reverb'], report['result']['output_seconds']), (30, 7.5))
        shifted, _ = sf.read(self.root / 'source.wav', always_2d=True)
        self.assertAlmostEqual(dominant(shifted), 220 * 2 ** (2 / 12), delta=5)

    def test_batch_jobs_carry_adjustment_and_reuse_requires_the_same_one(self):
        tid = 'a' * 32
        directory = service.TRACKS / tid
        directory.mkdir()
        service.save(directory / 'track.json', {'id': tid, 'title': 'Song', 'duration': 360, 'source': 'unused.wav'})
        api, _, stop = self.http()
        try:
            with patch('yue2_engine.available', return_value=True):
                jobs = api('/api/batch', {'request_id': 'adjust-batch-123456', 'items': [{'track_id': tid, 'instructions': '',
                    'adjust': setting(semitones=-2, tempo=90, reverb=20), 'variants': [{'style': 'Lo-fi'}, {'style': 'Jazz'}]}]})['jobs']
                a, b = [service.read(service.JOBS / j['id'] / 'config.json') for j in jobs]
                self.assertEqual((a['source_duration'], a['duration']), (360, 400))
                self.assertEqual(a['adjust'], b['adjust'])
                self.assertEqual(jobs[0]['output_name'], 'Lo-fi Song (Key -2, Tempo 90%, Reverb 20)')
                self.assertEqual(a['output_name'], jobs[0]['output_name'])
                plain = api('/api/jobs', {'track_id': tid, 'style': 'Jazz', 'instructions': ''})
                self.assertIsNone(plain['config']['adjust'])
                self.assertEqual((plain['duration'], plain['output_name']), (360, 'Jazz Song'))
                count = len(service.jobs())
                with self.assertRaises(urllib.error.HTTPError) as error:
                    api('/api/batch', {'request_id': 'too-long-batch-123456', 'items': [{'track_id': tid, 'instructions': '', 'adjust': setting(tempo=55),
                        'variants': [{'style': 'Lo-fi'}, {'style': 'Jazz'}]}]})
                self.assertIn('10 分钟', json.load(error.exception)['error'])
                self.assertEqual(len(service.jobs()), count)
            first, second = service.JOBS / jobs[0]['id'], service.JOBS / jobs[1]['id']
            service.save(first / 'preprocessing.json', {})
            (first / 'melody.abc').write_text('SCORE')
            self.assertTrue(engine.reuse_analysis(second, b))
            self.assertFalse(engine.reuse_analysis(second, {**b, 'adjust': setting(semitones=-3, tempo=90, reverb=20)}))
        finally:
            stop()

    def test_settings_and_tracks_persist_validated_adjustments(self):
        tid = 'b' * 32
        (service.TRACKS / tid).mkdir()
        service.save(service.TRACKS / tid / 'track.json', {'id': tid, 'title': 'Song', 'duration': 30, 'source': 'unused.wav'})
        api, _, stop = self.http()
        try:
            self.assertEqual(api('/api/state')['settings']['adjust'], adjust.DEFAULT)
            saved = api('/api/settings', {'adjust': {'speed_mode': 'linked', 'tempo': 80, 'reverb': 40}})
            self.assertEqual(saved['adjust'], setting(speed_mode='linked', tempo=80, reverb=40))
            self.assertEqual(api('/api/state')['settings'], saved)
            for bad in ({'tempo': 200}, {'semitones': '1'}, 'slow', None):
                with self.subTest(bad=bad), self.assertRaises(urllib.error.HTTPError):
                    api('/api/settings', {'adjust': bad})
            self.assertEqual(api('/api/state')['settings'], saved)
            self.assertEqual(api('/api/tracks/' + tid, {'adjust': {'semitones': 3}})['adjust'], setting(semitones=3))
            self.assertIsNone(api('/api/tracks/' + tid, {'adjust': None})['adjust'])
            with self.assertRaises(urllib.error.HTTPError):
                api('/api/tracks/' + tid, {'adjust': {'reverb': 101}})
        finally:
            stop()

    def test_finished_work_gets_a_new_adjusted_work_with_its_own_name(self):
        parent = self.finished_work()
        original = (parent / 'Lo-fi Song.wav').read_bytes()
        api, client, stop = self.http()
        try:
            work = api('/api/jobs/' + parent.name + '/adjust', {'adjust': setting(speed_mode='linked', tempo=80, reverb=40)})
            self.assertEqual((work['kind'], work['state'], work['parent_id'], work['group_id']), ('adjust', 'queued', parent.name, 'p' * 32))
            self.assertEqual((work['duration'], work['variant']), (7.5, 'A'))
            self.assertEqual(work['output_name'], 'Lo-fi Song (Slowed 80%, Reverb 40)')
            self.assertNotIn('source', work['config'])
            self.assertEqual([j['id'] for j in service.pending_jobs('adjust')], [work['id']])
            self.assertEqual(service.pending_jobs('generate'), [])
            directory = service.JOBS / work['id']
            config = service.read(directory / 'config.json')
            self.assertEqual(Path(config['source']), parent / 'Lo-fi Song.wav')
            engine.run_pipeline(directory, config)
            service.save(directory / 'job.json', {**service.read(directory / 'job.json'), 'state': 'completed'})
            result = service.read(directory / 'result.json')
            self.assertEqual(result['files'], {'result': 'Lo-fi Song (Slowed 80%, Reverb 40).wav', 'mp3': 'Lo-fi Song (Slowed 80%, Reverb 40).mp3'})
            self.assertAlmostEqual(result['checks']['result']['seconds'], 7.5)
            self.assertNotIn('original', result['files'])
            audio, _ = sf.read(directory / result['files']['result'], always_2d=True)
            self.assertAlmostEqual(dominant(audio), 176, delta=4)
            self.assertEqual((parent / 'Lo-fi Song.wav').read_bytes(), original)
            with client.open(f'http://127.0.0.1:{server.PORT}/results/{work["id"]}/mp3') as response:
                self.assertIn('Lo-fi Song (Slowed 80%, Reverb 40).mp3', unquote(response.headers['Content-Disposition']))
            again = api('/api/jobs/' + work['id'] + '/adjust', {'adjust': setting(semitones=-1)})
            self.assertEqual(again['output_name'], 'Lo-fi Song (Slowed 80%, Reverb 40, Key -1)')
            self.assertEqual((again['group_id'], again['duration']), ('p' * 32, 7.5))
        finally:
            stop()

    def test_adjust_requests_are_rejected_without_side_effects(self):
        parent = self.finished_work()
        pending = self.finished_work('f' * 32, state='running')
        traversal = self.finished_work('d' * 32)
        service.save(traversal / 'result.json', {'files': {'result': '../outside.wav'}})
        api, _, stop = self.http()
        try:
            for identifier, body, message in ((parent.name, {'adjust': setting()}, '至少调整'),
                    (parent.name, {'adjust': setting(tempo=151)}, '速度'), (parent.name, {}, '无效'),
                    (pending.name, {'adjust': setting(tempo=90)}, '完成的作品'),
                    (traversal.name, {'adjust': setting(tempo=90)}, 'WAV'),
                    ('c' * 32, {'adjust': setting(tempo=90)}, '完成的作品')):
                with self.subTest(identifier=identifier, body=body), self.assertRaises(urllib.error.HTTPError) as error:
                    api('/api/jobs/' + identifier + '/adjust', body)
                self.assertEqual(error.exception.code, 400)
                self.assertIn(message, json.load(error.exception)['error'])
            self.assertEqual(sorted(p.name for p in service.JOBS.iterdir()), sorted([parent.name, pending.name, traversal.name]))
        finally:
            stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
