"""DJ edits and non-stop DJ mixes: effect choices, beat-synced build-up / filter intro / echo-out on
synthetic beat tracks, a beat-matched mix with the real FFmpeg, and the HTTP routes and pipeline."""
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

import dj_tools
import engine
import service
import server

SR = 48000


def beat_track(seconds, bpm, loud_from=None, pitch=220., seed=0, kick_hz=55.):
    """Kick on every beat, hi-hat on the off-beats and a held chord; louder after `loud_from`."""
    count = int(seconds * SR)
    t = np.arange(count) / SR
    audio = .06 * (np.sin(2 * np.pi * pitch * t) + np.sin(2 * np.pi * pitch * 1.25 * t) + np.sin(2 * np.pi * pitch * 1.5 * t))
    beat = 60 / bpm
    kick = np.sin(2 * np.pi * kick_hz * np.arange(int(.15 * SR)) / SR) * np.exp(-np.arange(int(.15 * SR)) / (.05 * SR)) * .5
    rng = np.random.default_rng(seed)
    hat = rng.standard_normal(int(.03 * SR)) * np.exp(-np.arange(int(.03 * SR)) / (.008 * SR)) * .12
    for n in range(int(seconds / beat) + 1):
        for offset, sound in ((0, kick), (.5, hat)):
            a = int((n + offset) * beat * SR)
            b = min(count, a + len(sound))
            if a < count:
                audio[a:b] += sound[:b - a]
    if loud_from is not None:
        audio[int(loud_from * SR):] *= 2.2
    audio *= .8 / np.max(np.abs(audio))
    return np.column_stack((audio, audio)).astype(np.float32)


def band_energy(audio, low, high):
    mono = audio.mean(axis=1)
    spectrum = np.abs(np.fft.rfft(mono)) ** 2
    frequencies = np.fft.rfftfreq(len(mono), 1 / SR)
    return float(spectrum[(frequencies >= low) & (frequencies < high)].sum())


class DjEffectsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-dj-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_effect_choices_and_labels(self):
        self.assertEqual(dj_tools.validate(['echo', 'buildup']), ['buildup', 'echo'])
        self.assertEqual(dj_tools.label(['buildup', 'filter', 'echo']), 'DJ Drop + Filter Intro + Echo Out')
        for bad in ([], ['loud'], ['echo', 'echo'], 'echo', None, [1]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dj_tools.validate(bad)
        ids = ['a' * 32, 'b' * 32]
        self.assertEqual(dj_tools.validate_mix({'ids': ids}), (ids, 'manual', 16, ''))
        for bad in ({'ids': ids[:1]}, {'ids': ids * 2}, {'ids': ['x'] * 2}, {'ids': ids, 'order': 'random'},
                    {'ids': ids, 'transition_beats': 12}, {'ids': ids, 'transition_beats': True}, {'ids': ids, 'name': 'x' * 61},
                    {'ids': ['%032x' % i for i in range(21)]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dj_tools.validate_mix(bad)

    def test_beats_key_and_order(self):
        tempo, times = dj_tools.beats(beat_track(16, 120))
        self.assertAlmostEqual(tempo, 120, delta=3)
        self.assertGreater(len(times), 24)
        self.assertAlmostEqual(float(np.median(np.diff(times))), .5, delta=.03)
        # A C major melody that dwells on each scale note as long as the major key profile weighs it.
        notes = [(0, 6.35), (2, 3.48), (4, 4.38), (5, 4.09), (7, 5.19), (9, 3.66), (11, 2.88)]
        melody = []
        for semitone, weight in notes * 2:
            t = np.arange(int(weight * .25 * SR)) / SR
            frequency = 261.63 * 2 ** (semitone / 12)
            melody.append(sum(np.sin(2 * np.pi * k * frequency * t) / k for k in (1, 2, 3)) * .2)
        melody = np.concatenate(melody)
        self.assertEqual(dj_tools.key_of(np.column_stack((melody, melody))), (0, 'major', '8B'))
        a_minor = [(9, 6.33), (11, 2.68), (0, 5.38), (2, 3.53), (4, 4.75), (5, 3.98), (7, 3.34)]
        melody = np.concatenate([sum(np.sin(2 * np.pi * k * 261.63 * 2 ** (s / 12) * np.arange(int(w * .25 * SR)) / SR) / k for k in (1, 2, 3)) * .2
                                 for s, w in a_minor * 2])
        self.assertEqual(dj_tools.key_of(np.column_stack((melody, melody)))[1:], ('minor', '8A'))
        self.assertEqual(dj_tools.camelot_distance('8B', '9B'), 1)
        self.assertEqual(dj_tools.camelot_distance('12A', '1A'), 1)
        self.assertEqual(dj_tools.camelot_distance('8B', '8A'), 1)
        songs = [{'tempo': 128, 'camelot': '9B'}, {'tempo': 100, 'camelot': '8B'}, {'tempo': 101, 'camelot': '3A'}, {'tempo': 102, 'camelot': '8B'}]
        self.assertEqual(dj_tools.auto_order(songs), [1, 3, 2, 0])
        target, folded = dj_tools.target_tempo([120, 62, 126])
        self.assertEqual(folded, [120, 124, 126])
        self.assertEqual(target, 124)

    def test_filter_intro_buildup_and_echo_follow_the_beat(self):
        original = beat_track(32, 120, loud_from=16.)
        source = self.root / 'song.wav'
        sf.write(source, original, SR, subtype='PCM_24')
        before = source.read_bytes()
        # Filter intro alone: muffled start, untouched rest.
        report = dj_tools.render_effects(source, self.root / 'filter.wav', ['filter'])
        filtered, _ = sf.read(self.root / 'filter.wav', always_2d=True)
        self.assertEqual(len(filtered), len(original))
        self.assertLess(band_energy(filtered[:SR], 3000, 20000), .1 * band_energy(original[:SR], 3000, 20000))
        end = int(report['filter_intro_seconds'] * SR)
        self.assertLessEqual(report['filter_intro_seconds'], 32 * .3 + .01)
        np.testing.assert_allclose(filtered[end + SR:], original[end + SR:] * report['gain'], atol=2e-4)
        # Build-up and drop: the drop lands on the energy jump, half a beat of silence before it.
        report = dj_tools.render_effects(source, self.root / 'drop.wav', ['buildup'])
        dropped, _ = sf.read(self.root / 'drop.wav', always_2d=True)
        self.assertEqual(len(dropped), len(original))
        self.assertAlmostEqual(report['drop_seconds'], 16., delta=.6)
        drop = int(report['drop_seconds'] * SR)
        self.assertLess(np.max(np.abs(dropped[drop - int(.2 * SR):drop - 10])), 1e-4)
        # The report rounds to the millisecond, so compare from just after the drop.
        np.testing.assert_allclose(dropped[drop + 100:], original[drop + 100:] * report['gain'], atol=2e-4)
        # Over the last beats of the build the bass is gone and the riser fills the highs.
        late = slice(drop - int(1.5 * SR), drop - int(.3 * SR))
        self.assertLess(band_energy(dropped[late], 20, 120), .1 * band_energy(original[late], 20, 120))
        self.assertGreater(band_energy(dropped[late], 3000, 20000), band_energy(original[late], 3000, 20000))
        # Echo-out: stops on a beat 8 beats before the end and rings out for 8 more beats.
        report = dj_tools.render_effects(source, self.root / 'echo.wav', ['echo'])
        echoed, _ = sf.read(self.root / 'echo.wav', always_2d=True)
        self.assertAlmostEqual(report['echo_cut_seconds'], 28., delta=.3)
        self.assertAlmostEqual(len(echoed) / SR, report['echo_cut_seconds'] + 8 * 60 / report['tempo'], delta=.01)
        self.assertAlmostEqual(report['output_seconds'], len(echoed) / SR, places=6)
        cut = int(report['echo_cut_seconds'] * SR)
        after = float(np.sqrt(np.mean(echoed[cut + int(.2 * SR):cut + SR] ** 2)))
        self.assertGreater(after, .01)
        self.assertLess(float(np.sqrt(np.mean(echoed[-SR // 2:] ** 2))), .25 * after)
        # All three together; the finished work itself is only read.
        report = dj_tools.render_effects(source, self.root / 'all.wav', ['buildup', 'filter', 'echo'])
        self.assertEqual(report['effects'], ['buildup', 'filter', 'echo'])
        self.assertGreater(report['drop_seconds'], report['filter_intro_seconds'])
        self.assertEqual(source.read_bytes(), before)

    def test_non_stop_mix_matches_tempo_and_blends_with_a_bass_swap(self):
        paths = []
        for index, (bpm, gain) in enumerate(((120, 1.), (126, .5), (62, 1.))):
            path = self.root / f'song-{index}.wav'
            sf.write(path, beat_track(20, bpm, pitch=220 * 1.12 ** index, seed=index) * gain, SR, subtype='PCM_24')
            paths.append({'path': str(path), 'title': f'Song {index}'})
        output = self.root / 'mix.wav'
        report = dj_tools.render_mix(paths, output, 'auto', 8, engine.FFMPEG)
        mixed, rate = sf.read(output, always_2d=True)
        self.assertEqual(rate, SR)
        self.assertAlmostEqual(report['target_tempo'], 124, delta=4)
        self.assertEqual(report['order'][0], 'Song 0')
        self.assertEqual(len(report['songs']), 3)
        self.assertEqual(len(report['transitions']), 2)
        self.assertTrue(all(song['beat_matched'] for song in report['songs']))
        self.assertTrue(all(song['stretch_method'] in ('rubberband', 'atempo', 'none') for song in report['songs']))
        for transition in report['transitions']:
            self.assertAlmostEqual(transition['seconds'], 8 * 60 / report['target_tempo'], delta=.05)
        total = sum(s['plays_seconds'] for s in report['songs']) + sum(t['seconds'] for t in report['transitions'])
        self.assertAlmostEqual(report['output_seconds'], total, delta=.01)
        self.assertAlmostEqual(len(mixed) / SR, report['output_seconds'], delta=.01)
        self.assertLess(report['output_seconds'], 60)
        self.assertGreater(report['output_seconds'], 40)
        self.assertLessEqual(float(np.max(np.abs(mixed))), 1.)
        # Loudness is evened out: the quiet song plays about as loud as the others.
        levels = []
        position = 0.
        for song, transition in zip(report['songs'], report['transitions'] + [{'seconds': 0}]):
            a, b = int((position + 1) * SR), int((position + song['plays_seconds'] - 1) * SR)
            levels.append(float(np.sqrt(np.mean(mixed[a:b] ** 2))))
            position += song['plays_seconds'] + transition['seconds']
        self.assertLess(max(levels) / min(levels), 1.5)


class DjServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-dj-service-')
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=self.root, DATA=self.root / 'data', JOBS=self.root / 'data/jobs', TRACKS=self.root / 'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True)
        service.TRACKS.mkdir()
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

    def work(self, identifier, style, bpm, seconds=20., **meta):
        directory = service.JOBS / identifier
        directory.mkdir()
        name = f'{style} Song.wav'
        sf.write(directory / name, beat_track(seconds, bpm), SR, subtype='PCM_24')
        service.save(directory / 'result.json', {'files': {'result': name}, 'checks': {'result': {'seconds': seconds}}})
        service.save(directory / 'job.json', {'id': identifier, 'title': 'Song', 'style': style, 'state': 'completed', 'mode': 'remix',
            'variant': 'A', 'pair_id': 'p' * 32, 'duration': seconds, 'created_at': 1, 'attempt': 1, 'config': {'format': 'both'}, **meta})
        return identifier

    def test_dj_edit_and_mix_are_queued_and_rendered_as_new_works(self):
        first = self.work('1' * 32, 'Vinahouse', 120, voice={'target': 'original', 'octave': 'auto'}, adjust_history=[{'semitones': 0, 'tempo': 112, 'speed_mode': 'independent', 'reverb': 0, 'preset': 'Speed Up'}])
        second = self.work('2' * 32, 'House', 124)
        edit = self.api(f'/api/jobs/{first}/dj', {'effects': ['echo', 'buildup']})
        self.assertEqual((edit['kind'], edit['dj'], edit['parent_id'], edit['group_id'], edit['variant']), ('dj', ['buildup', 'echo'], first, 'p' * 32, 'A'))
        self.assertEqual(edit['output_name'], 'Vinahouse Song (Original Voice, Speed Up, DJ Drop + Echo Out)')
        self.assertIn('DJ 效果', self.rejected(f'/api/jobs/{first}/dj', {'effects': []}))
        self.assertIn('完成的作品', self.rejected(f'/api/jobs/{"9" * 32}/dj', {'effects': ['echo']}))
        mix = self.api('/api/mix', {'ids': [second, first], 'order': 'manual', 'transition_beats': 8, 'name': 'Party'})
        self.assertEqual((mix['kind'], mix['parent_ids'], mix['style'], mix['output_name'], mix['variant']), ('mix', [second, first], 'DJ Mix', 'DJ Mix Party', None))
        self.assertNotIn(mix['group_id'], ('p' * 32, first, second))
        self.assertNotIn('sources', mix['config'])
        self.assertIn('2–20', self.rejected('/api/mix', {'ids': [first]}))
        self.assertIn('完成的作品', self.rejected('/api/mix', {'ids': [first, '9' * 32]}))
        pending = service.pending_jobs('adjust')
        self.assertEqual({j['kind'] for j in pending}, {'dj', 'mix'})
        self.assertEqual(service.pending_jobs('generate'), [])
        for job in (edit, mix):
            with self.subTest(kind=job['kind']):
                directory = service.JOBS / job['id']
                engine.run_pipeline(directory, service.read(directory / 'config.json'))
                result = service.read(directory / 'result.json')
                self.assertEqual(result['files']['result'], job['output_name'] + '.wav')
                self.assertTrue((directory / result['files']['mp3']).is_file())
                report = service.read(directory / f'{job["kind"]}.json')
                self.assertAlmostEqual(result['checks']['result']['seconds'], report['output_seconds'], delta=.01)
                self.assertIn('未重新生成', result['notice'])
        # The finished derived works expose their reports and never spawn speed versions.
        meta = service.finish_attempt(service.JOBS / mix['id'], 0)
        self.assertNotIn('speed_variant_ids', meta)
        shown = service.public_job(service.JOBS / mix['id'])
        self.assertEqual(shown['effect_info']['order'], [service.job_output_name(service.read(service.JOBS / second / 'job.json')), service.job_output_name(service.read(service.JOBS / first / 'job.json'))])

    def test_mix_length_limit(self):
        ids = [self.work('%032x' % (i + 1), 'House', 120, seconds=30.) for i in range(2)]
        with patch.object(dj_tools, 'MAX_MIX_SECONDS', 50):
            self.assertIn('最多', self.rejected('/api/mix', {'ids': ids}))
        self.assertEqual(len(service.pending_jobs('adjust')), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
