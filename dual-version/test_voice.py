"""Voice swap: settings, phrase/reference analysis, the Seed-VC runner with a stand-in engine,
pipeline order, A/B batches over HTTP and derived works. No model is downloaded or run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import numpy as np
import soundfile as sf

import audio_adjust
import engine
import server
import service
import voice_swap

SR = 48000

# Behaves like Seed-VC's inference.py at the pinned commit: module-level `device`,
# load_models(args) and main(args), which calls load_models again and writes one
# vc_*.wav at 44.1 kHz into args.output. The "conversion" halves the level.
STUB = textwrap.dedent('''
    import json, os
    import numpy as np, soundfile as sf
    device = 'cpu'
    def log(entry):
        with open(os.environ['STUB_SEEDVC_LOG'], 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry) + '\\n')
    def load_models(args):
        log({'call': 'load', 'cwd': os.getcwd()})
        return ('model', 'semantic', 'f0', 'vocoder', 'campplus', 'mel', {})
    def main(args):
        load_models(args)
        if os.environ.get('STUB_SEEDVC_FAIL'):
            raise RuntimeError('stub conversion failed on purpose')
        log({'call': 'main', 'source': args.source, 'target': args.target, 'steps': args.diffusion_steps,
             'cfg': args.inference_cfg_rate, 'shift': args.semi_tone_shift, 'f0': args.f0_condition,
             'auto_f0': args.auto_f0_adjust, 'checkpoint': args.checkpoint, 'fp16': args.fp16,
             'length_adjust': args.length_adjust})
        audio, rate = sf.read(args.source, dtype='float32')
        count = int(round(len(audio) * 44100 / rate))
        converted = .5 * np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio)
        os.makedirs(args.output, exist_ok=True)
        name = os.path.basename(args.source).split('.')[0]
        sf.write(os.path.join(args.output, f'vc_{name}_reference_1.0_{args.diffusion_steps}_0.7.wav'), converted, 44100)
''')


def sung(seconds, frequency=220., amplitude=.3, rate=SR):
    """A harmonic tone with vibrato, which pYIN tracks like a held sung note."""
    t = np.arange(int(seconds * rate)) / rate
    phase = 2 * np.pi * np.cumsum(frequency * (1 + .004 * np.sin(2 * np.pi * 5 * t))) / rate
    wave = sum(np.sin(k * phase) / k for k in range(1, 6))
    return (amplitude * wave / np.max(np.abs(wave))).astype(np.float32)


def swap(*letters, octave='auto'):
    """The shared / per-song setting with the given version letters switched on."""
    return {'versions': [letter in letters for letter in voice_swap.LETTERS], 'octave': octave}


def silence(seconds, rate=SR):
    return np.zeros(int(seconds * rate), dtype=np.float32)


def stereo(signal):
    return np.column_stack((signal, signal))


def rms(signal):
    return float(np.sqrt(np.mean(np.square(signal)))) if len(signal) else 0.


def dominant(audio):
    middle = audio[len(audio) // 4:3 * len(audio) // 4]
    middle = middle.mean(axis=1) if middle.ndim == 2 else middle
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    return np.fft.rfftfreq(len(middle), 1 / SR)[np.argmax(spectrum)]


def write_stems(directory, vocals, instrumental):
    directory.mkdir(parents=True, exist_ok=True)
    sf.write(directory / 'vocals.wav', stereo(vocals), SR, subtype='PCM_24')
    sf.write(directory / 'instrumental.wav', stereo(instrumental), SR, subtype='PCM_24')
    service.save(directory / 'stems.json', {'vocals': str(directory / 'vocals.wav'),
                                            'instrumental': str(directory / 'instrumental.wav')})


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-voice-')
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=self.root, DATA=self.root / 'data',
                                    JOBS=self.root / 'data/jobs', TRACKS=self.root / 'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True)
        service.TRACKS.mkdir()
        # A stand-in Seed-VC install: the real runner, the system Python, a stub inference.py.
        home = self.root / 'seed-vc-home'
        (home / 'seed-vc').mkdir(parents=True)
        (home / 'seed-vc/inference.py').write_text(STUB, encoding='utf-8')
        service.save(home / 'install.json', {'complete': True, 'seedvc_commit': voice_swap.SEEDVC_COMMIT})
        self.log = self.root / 'stub-calls.jsonl'
        self.engine = patch.multiple(voice_swap, HOME=home, SOURCE=home / 'seed-vc', PYTHON=Path(sys.executable))
        self.engine.start()
        self.env = patch.dict(os.environ, {'STUB_SEEDVC_LOG': str(self.log), 'SING_VOICE_ALLOW_CPU': '1'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.engine.stop()
        self.paths.stop()
        self.temp.cleanup()

    def calls(self, kind):
        if not self.log.exists():
            return []
        return [c for c in map(json.loads, self.log.read_text(encoding='utf-8').splitlines()) if c['call'] == kind]

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
        return api, stop

    def track(self, identifier='a' * 32, **values):
        (service.TRACKS / identifier).mkdir()
        service.save(service.TRACKS / identifier / 'track.json',
                     {'id': identifier, 'title': 'Song', 'duration': 30, 'source': 'unused.wav', **values})
        return identifier

    def job_with_stems(self, vocals, instrumental, reference):
        jobdir = self.root / 'job'
        write_stems(jobdir / 'generated-stems', vocals, instrumental)
        sf.write(jobdir / 'generated.wav', stereo(vocals + instrumental), SR, subtype='PCM_24')
        write_stems(jobdir / 'original-stems', reference, silence(len(reference) / SR))
        return jobdir

    def test_settings_validation_names_and_octave_rule(self):
        self.assertEqual(voice_swap.validate({}), voice_swap.DEFAULT)
        self.assertIsNone(voice_swap.validate(None, inherit=True))
        self.assertEqual(voice_swap.validate({'octave': 'up'}), swap(octave='up'))
        self.assertEqual(voice_swap.validate(swap('B', 'J')), swap('B', 'J'))
        for bad in ({'a': True}, {'octave': 'sideways'}, {'versions': [True] * 9}, {'versions': [1] + [False] * 9},
                    {'versions': 'AB'}, None, [], 'on'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                voice_swap.validate(bad)
        setting = swap('A', 'J', octave='down')
        self.assertEqual(voice_swap.for_variant(setting, 'A'), {'target': 'original', 'octave': 'down'})
        self.assertEqual(voice_swap.for_variant(setting, 'J'), {'target': 'original', 'octave': 'down'})
        self.assertIsNone(voice_swap.for_variant(setting, 'B'))
        self.assertIsNone(voice_swap.for_variant(None, 'A'))
        self.assertIsNone(voice_swap.validate_job(None))
        for bad in ({'target': 'someone', 'octave': 'auto'}, {'target': 'original'}, {'target': 'original', 'octave': 'auto', 'x': 1}, 'original'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                voice_swap.validate_job(bad)
        # Only whole octaves, so the converted voice stays in the song's key.
        for args, shift in ((('auto', 69, 57), -12), (('auto', 57, 69), 12), (('auto', 60, 64), 0), (('auto', 60, 53), -12),
                            (('auto', 60, 54), 0), (('auto', 40, 80), 12), (('auto', None, 60), 0), (('none', 69, 50), 0),
                            (('up', 60, 60), 12), (('down', 60, 60), -12)):
            with self.subTest(args=args):
                self.assertEqual(voice_swap.octave_shift(*args), shift)
        self.assertEqual(service.job_output_name({'title': 'Song', 'style': 'Lo-fi', 'voice': {'target': 'original', 'octave': 'auto'},
                                                  'adjust': audio_adjust.validate({'semitones': -2})}), 'Lo-fi Song (Original Voice, Key -2)')

    def test_phrases_reference_and_pitch_follow_the_singing(self):
        signal = np.concatenate((silence(2), sung(5), silence(1), sung(4), silence(.3), sung(3), silence(2)))
        regions = voice_swap.phrases(signal)
        self.assertEqual(len(regions), 2)
        (a, b), (c, d) = regions
        for got, expected in ((a, 2), (b, 7), (c, 8), (d, 15.3)):
            self.assertAlmostEqual(got / SR, expected, delta=.17)
        long = np.concatenate([np.concatenate((sung(9.9), silence(.1) + 1e-3)) for _ in range(7)])
        pieces = voice_swap.phrases(long)
        self.assertGreater(len(pieces), 2)
        self.assertTrue(all(end - start <= voice_swap.LONGEST_PHRASE * SR for start, end in pieces))
        self.assertTrue(all(pieces[i][1] == pieces[i + 1][0] for i in range(len(pieces) - 1)))
        clip, info = voice_swap.choose_reference(stereo(signal))
        self.assertAlmostEqual(info['seconds'], len(clip) / SR)
        self.assertGreater(info['seconds'], 11.5)
        self.assertLessEqual(info['seconds'], voice_swap.REFERENCE_SECONDS)
        self.assertAlmostEqual(float(np.max(np.abs(clip))), .9, places=3)
        hop = SR // 20
        quiet = [rms(clip[i:i + hop]) for i in range(0, len(clip) - hop, hop)]
        self.assertGreater(min(quiet), 1e-3, 'reference keeps no silent gaps')
        with self.assertRaises(RuntimeError) as error:
            voice_swap.choose_reference(stereo(np.concatenate((silence(5), sung(2), silence(5)))))
        self.assertIn('不足', str(error.exception))
        self.assertAlmostEqual(voice_swap.median_pitch(sung(4, 220)), 57, delta=.5)
        self.assertAlmostEqual(voice_swap.median_pitch(sung(4, 440)), 69, delta=.5)
        self.assertIsNone(voice_swap.median_pitch(silence(4)))

    def test_convert_places_converted_phrases_and_remixes(self):
        vocals = np.concatenate((silence(1), sung(3, 440), silence(1.5), sung(2.5, 440), silence(1)))
        instrumental = .1 * np.sin(2 * np.pi * 110 * np.arange(len(vocals)) / SR).astype(np.float32)
        jobdir = self.job_with_stems(vocals, instrumental, np.concatenate((sung(8, 220), silence(1))))
        generated = hashlib.sha256((jobdir / 'generated.wav').read_bytes()).hexdigest()
        reference = voice_swap.prepare_reference(jobdir, jobdir / 'original-stems')
        self.assertAlmostEqual(reference['reference_pitch_midi'], 57, delta=.5)
        messages = []
        config = {'voice': {'target': 'original', 'octave': 'auto'}, 'profile': 'balanced', 'duration': 9}
        report = voice_swap.convert(jobdir, config, lambda stage, message: messages.append((stage, message)))
        loads, mains = self.calls('load'), self.calls('main')
        self.assertEqual(len(loads), 1, 'models load once for all phrases')
        self.assertTrue(os.path.samefile(loads[0]['cwd'], voice_swap.SOURCE))
        self.assertEqual(len(mains), 2)
        for call in mains:
            self.assertEqual((call['shift'], call['steps'], call['cfg'], call['f0'], call['auto_f0'], call['checkpoint'], call['fp16'], call['length_adjust']),
                             (-12, 30, voice_swap.CFG_RATE, True, False, None, True, 1.0))
            self.assertEqual(Path(call['target']), jobdir / 'voice-swap/reference.wav')
        self.assertIn(('voice', '换成原唱音色 · 共 2 个乐句'), messages)
        self.assertEqual((report['phrases'], report['octave_shift'], report['skipped']), (2, -12, False))
        self.assertAlmostEqual(report['vocal_gain'], 2, delta=.1)
        self.assertEqual(report['runner']['device'], 'cpu')
        self.assertIn('error', report['runner']['metrics'])
        self.assertEqual(service.read(jobdir / 'voice-swap.json'), report)
        result, rate = sf.read(jobdir / 'result.wav', always_2d=True)
        converted, _ = sf.read(jobdir / 'voice-swap/vocals-converted.wav', always_2d=True)
        self.assertEqual((rate, result.shape), (SR, (len(vocals), 2)))
        self.assertLess(rms(converted[:int(.8 * SR)]), 1e-4)
        self.assertLess(rms(converted[int(4.3 * SR):int(5.3 * SR)]), 1e-4)
        for start, end in ((1.2, 3.8), (5.7, 7.8)):
            span = slice(int(start * SR), int(end * SR))
            self.assertAlmostEqual(rms(converted[span, 0]) / rms(vocals[span]), 1, delta=.08)
            self.assertAlmostEqual(dominant(converted[span]), 440, delta=3)
        self.assertLess(np.max(np.abs(result - (stereo(instrumental) + converted))), 1e-3)
        self.assertEqual(hashlib.sha256((jobdir / 'generated.wav').read_bytes()).hexdigest(), generated)
        self.assertEqual(sorted(p.name for p in (jobdir / 'voice-swap').glob('phrase-*')), [])
        self.assertEqual(len(list((jobdir / 'voice-swap').glob('converted-*.wav'))), 2)
        info = voice_swap.summary(report)
        self.assertEqual((info['phrases'], info['octave_shift'], info['diffusion_steps']), (2, -12, 30))

    def test_convert_options_silence_and_engine_failure(self):
        vocals = np.concatenate((silence(1), sung(3, 220), silence(1)))
        instrumental = .1 * np.sin(2 * np.pi * 110 * np.arange(len(vocals)) / SR).astype(np.float32)
        jobdir = self.job_with_stems(vocals, instrumental, np.concatenate((sung(6, 220), silence(1))))
        voice_swap.prepare_reference(jobdir, jobdir / 'original-stems')
        for octave, shift in (('none', 0), ('up', 12), ('auto', 0)):
            with self.subTest(octave=octave):
                self.log.unlink(missing_ok=True)
                report = voice_swap.convert(jobdir, {'voice': {'target': 'original', 'octave': octave}, 'profile': 'fast', 'duration': 5}, lambda *a: None)
                self.assertEqual(report['octave_shift'], shift)
                self.assertEqual({(c['shift'], c['steps']) for c in self.calls('main')}, {(shift, voice_swap.STEPS['fast'])})
        (jobdir / 'result.wav').unlink()
        with patch.dict(os.environ, {'STUB_SEEDVC_FAIL': '1'}), self.assertRaises(RuntimeError) as error:
            voice_swap.convert(jobdir, {'voice': {'target': 'original', 'octave': 'auto'}, 'duration': 5}, lambda *a: None)
        self.assertIn('Seed-VC', str(error.exception))
        self.assertIn('stub conversion failed on purpose', str(error.exception))
        self.assertFalse((jobdir / 'result.wav').exists())
        # Nothing sung in the generated take: keep it, say so, never call the engine.
        write_stems(jobdir / 'generated-stems', silence(5), instrumental)
        self.log.unlink(missing_ok=True)
        report = voice_swap.convert(jobdir, {'voice': {'target': 'original', 'octave': 'auto'}, 'duration': 5}, lambda *a: None)
        self.assertTrue(report['skipped'])
        self.assertEqual((jobdir / 'result.wav').read_bytes(), (jobdir / 'generated.wav').read_bytes())
        self.assertEqual(self.calls('load'), [])

    def test_runner_self_test_with_stand_in_engine(self):
        report = self.root / 'self-test.json'
        result = subprocess.run([sys.executable, str(voice_swap.RUNNER), '--seedvc', str(voice_swap.SOURCE), '--self-test', '--report', str(report)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(report.read_text(encoding='utf-8'))['self_test_passed'])
        refused = subprocess.run([sys.executable, str(voice_swap.RUNNER), '--seedvc', str(voice_swap.SOURCE), '--self-test', '--report', str(report)],
                                 capture_output=True, text=True, timeout=120, env={**os.environ, 'SING_VOICE_ALLOW_CPU': ''})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('CUDA 12.8', refused.stderr)

    def test_pipeline_order_reference_source_and_preflight(self):
        source = self.root / 'song.wav'
        sf.write(source, stereo(sung(6, 220)), SR, subtype='PCM_16')
        base = {'engine': 'yue2', 'mode': 'remix', 'instrumental': False, 'profile': 'fast', 'lyrics': '[Verse]\nla la',
                'auto_lyrics': True, 'source': str(source), 'start': 0, 'source_duration': 6, 'duration': 6, 'adjust': None,
                'melody_mode': 'free', 'seed': 1, 'voice': {'target': 'original', 'octave': 'auto'}}
        for name, adjust, expected_stage, stems in (('plain', None, 'separate-original', 'original-stems'),
                                                     ('shifted', audio_adjust.validate({'semitones': 2}), 'separate-reference', 'reference-stems')):
            with self.subTest(name=name):
                jobdir = self.root / name
                jobdir.mkdir()
                stages, references, conversions, real_run = [], [], [], subprocess.run

                def run(args, **kwargs):
                    if '--stage' not in args:
                        return real_run(args, **kwargs)
                    stages.append(args[-1])
                    if args[-1] in ('separate-original', 'separate-reference', 'separate-generated'):
                        target = {'separate-original': 'original-stems', 'separate-reference': 'reference-stems'}.get(args[-1], 'generated-stems')
                        service.save((jobdir / target).mkdir() or jobdir / target / 'stems.json', {})
                    if args[-1] == 'generate':
                        (jobdir / 'generated.wav').write_bytes((jobdir / 'source.wav').read_bytes())
                with patch.object(engine.subprocess, 'run', side_effect=run), \
                        patch.object(voice_swap, 'prepare_reference', side_effect=lambda job, folder: references.append(Path(folder).name)), \
                        patch.object(voice_swap, 'convert', side_effect=lambda job, config, progress: conversions.append(config['voice'])):
                    engine.run_pipeline(jobdir, {**base, 'adjust': adjust, 'duration': 6})
                self.assertEqual(stages, [expected_stage, 'generate', 'separate-generated', 'validate'])
                self.assertEqual((references, conversions), ([stems], [base['voice']]))
                unshifted = jobdir / 'source-unshifted.wav'
                self.assertEqual(unshifted.exists(), adjust is not None)
                if adjust:
                    self.assertAlmostEqual(dominant(sf.read(unshifted)[0]), 220, delta=3)
                    self.assertAlmostEqual(dominant(sf.read(jobdir / 'source.wav')[0]), 220 * 2 ** (2 / 12), delta=4)
        missing = self.root / 'missing'
        missing.mkdir()
        with patch.object(voice_swap, 'available', return_value=False), \
                patch.object(engine.subprocess, 'run') as run, self.assertRaises(RuntimeError) as error:
            engine.run_pipeline(missing, base)
        self.assertIn('Seed-VC', str(error.exception))
        run.assert_not_called()
        # Version B reuses A's un-shifted reference separation.
        prior = self.root / ('1' * 32)
        write_stems(prior / 'reference-stems', sung(4), silence(4))
        service.save(prior / 'config.json', {**base, 'pair_id': 'p'})
        service.save(prior / 'preprocessing.json', {})
        second = self.root / ('2' * 32)
        second.mkdir()
        self.assertTrue(engine.reuse_analysis(second, {**base, 'pair_id': 'p', 'reuse_from': '1' * 32}))
        stems = service.read(second / 'reference-stems/stems.json')
        self.assertEqual(Path(stems['vocals']), second / 'reference-stems/vocals.wav')
        self.assertTrue(Path(stems['vocals']).is_file())

    def test_batches_settings_and_tracks_over_http(self):
        tid = self.track()
        api, stop = self.http()
        try:
            state = api('/api/state')
            self.assertEqual(state['settings']['voice_swap'], voice_swap.DEFAULT)
            self.assertTrue(state['engines']['voice'])
            saved = api('/api/settings', {'voice_swap': swap('A', octave='down')})
            self.assertEqual(saved['voice_swap'], swap('A', octave='down'))
            for bad in ({'versions': ['yes'] * 10}, {'octave': 'high'}, {'a': True}, None, 'on'):
                with self.subTest(bad=bad), self.assertRaises(urllib.error.HTTPError):
                    api('/api/settings', {'voice_swap': bad})
            self.assertEqual(api('/api/state')['settings'], saved)
            self.assertEqual(api('/api/tracks/' + tid, {'voice_swap': swap('B')})['voice_swap'], swap('B'))
            self.assertIsNone(api('/api/tracks/' + tid, {'voice_swap': None})['voice_swap'])
            # An unusable stored shape falls back instead of blocking later saves and batches.
            service.save(service.DATA / 'settings.json', {**service.read(service.DATA / 'settings.json'), 'voice_swap': {'a': True}})
            track = service.read(service.TRACKS / tid / 'track.json')
            service.save(service.TRACKS / tid / 'track.json', {**track, 'voice_swap': {'b': True}})
            state = api('/api/state')
            self.assertEqual(state['settings']['voice_swap'], voice_swap.DEFAULT)
            self.assertIsNone(next(t for t in state['tracks'] if t['id'] == tid)['voice_swap'])
            self.assertEqual(api('/api/settings', {'voice_swap': swap('A', octave='down')})['voice_swap'], swap('A', octave='down'))
            service.save(service.TRACKS / tid / 'track.json', track)
            def batch(key, **item):
                return api('/api/batch', {'request_id': key, 'items': [{'track_id': tid, 'instructions': '',
                    'variants': [{'style': 'Lo-fi'}, {'style': 'Jazz'}], **item}]})['jobs']
            with patch('yue2_engine.available', return_value=True):
                a, b = batch('voice-batch-1234567', voice_swap=swap('A'), adjust={'semitones': -2})
                ca, cb = (service.read(service.JOBS / j['id'] / 'config.json') for j in (a, b))
                self.assertEqual((ca['voice'], cb['voice']), ({'target': 'original', 'octave': 'auto'}, None))
                self.assertEqual((a['voice'], a['output_name'], b['output_name']), (ca['voice'], 'Lo-fi Song (Original Voice, Key -2)', 'Jazz Song (Key -2)'))
                self.assertEqual(ca['output_name'], a['output_name'])
                both = batch('voice-batch-both-1234', voice_swap=swap('A', 'B', octave='up'))
                self.assertEqual([j['config']['voice'] for j in both], [{'target': 'original', 'octave': 'up'}] * 2)
                three = api('/api/batch', {'request_id': 'voice-batch-three-123', 'items': [{'track_id': tid, 'instructions': '',
                    'voice_swap': swap('C', 'D'), 'variants': [{'style': 'Lo-fi'}, {'style': 'Jazz'}, {'style': 'Soul'}]}]})['jobs']
                self.assertEqual([(j['variant'], j['config']['voice'] is not None, j['output_name']) for j in three],
                                 [('A', False, 'Lo-fi Song'), ('B', False, 'Jazz Song'), ('C', True, 'Soul Song (Original Voice)')])
                for mode, instrumental in (('preserve', False), ('remix', True)):
                    with self.subTest(mode=mode, instrumental=instrumental):
                        kept = batch(f'voice-off-{mode}-{instrumental}-1234', mode=mode, instrumental=instrumental, voice_swap=swap('A', 'B'))
                        self.assertEqual([j['config']['voice'] for j in kept], [None, None])
                        self.assertNotIn('Original Voice', kept[0]['output_name'])
                single = api('/api/jobs', {'track_id': tid, 'style': 'R&B', 'instructions': '', 'voice': {'target': 'original', 'octave': 'none'}})
                self.assertEqual((single['config']['voice'], single['output_name']), ({'target': 'original', 'octave': 'none'}, 'R&B Song (Original Voice)'))
                count = len(service.jobs())
                for key, item in (('voice-bad-setting-1234', {'voice_swap': {'a': True}}), ('voice-bad-job-12345678', {'voice': 'original'})):
                    with self.subTest(item=item), self.assertRaises(urllib.error.HTTPError):
                        if 'voice' in item:
                            api('/api/jobs', {'track_id': tid, 'style': 'R&B', 'instructions': '', **item})
                        else:
                            batch(key, **item)
                with patch.object(voice_swap, 'available', return_value=False):
                    self.assertFalse(api('/api/state')['engines']['voice'])
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        batch('voice-missing-engine-12', voice_swap=swap('B'))
                    self.assertIn('Seed-VC', json.load(error.exception)['error'])
                    plain = batch('voice-missing-but-off-1', voice_swap=swap())
                self.assertEqual(len(service.jobs()), count + 2)
                self.assertEqual([j['config']['voice'] for j in plain], [None, None])
        finally:
            stop()

    def test_release_passes_only_with_every_required_benchmark(self):
        import benchmark_voice
        path = self.root / 'VALIDATION.json'
        path.write_text(json.dumps({'version': '1.4.0', 'dev_checks_passed': True, 'passed': False, 'required_benchmarks': ['adjust', 'voice'],
            'pending': ['Run benchmark_adjust.py', 'Run benchmark_voice.py', 'Listen']}), encoding='utf-8')
        def state():
            return json.loads(path.read_text(encoding='utf-8'))
        benchmark_voice.record(path, 'voice', {'version': '1.4.0', 'real_local_inference': True, 'passed': True, 'runs': [{'passed': True}]})
        self.assertEqual((state()['passed'], state()['pending']), (False, ['Run benchmark_adjust.py', 'Listen']))
        self.assertEqual(state()['benchmarks']['voice'], {'passed': True, 'runs': [{'passed': True}]})
        benchmark_voice.record(path, 'adjust', {'passed': False, 'runs': []})
        self.assertEqual((state()['passed'], state()['pending']), (False, ['Run benchmark_adjust.py', 'Listen']))
        benchmark_voice.record(path, 'adjust', {'passed': True, 'runs': [], 'ffmpeg_rubberband': True})
        self.assertEqual((state()['passed'], state()['pending']), (True, ['Listen']))
        self.assertTrue(state()['benchmarks']['adjust']['ffmpeg_rubberband'])

    def test_finished_voice_work_details_notice_and_derived_name(self):
        identifier = 'e' * 32
        directory = service.JOBS / identifier
        directory.mkdir()
        sf.write(directory / 'Lo-fi Song (Original Voice).wav', stereo(sung(6)), SR, subtype='PCM_24')
        sf.write(directory / 'source.wav', stereo(sung(6)), SR, subtype='PCM_16')
        voice = {'target': 'original', 'octave': 'auto'}
        service.save(directory / 'voice-swap.json', {'skipped': False, 'phrases': 3, 'octave_shift': -12, 'octave_mode': 'auto',
            'diffusion_steps': 30, 'reference_seconds': 20, 'seconds': 41.2, 'notice': '已换声。', 'model': 'x',
            'runner': {'metrics': {'speaker_similarity_before': .31234, 'speaker_similarity_after': .70111, 'pitch_error_cents_median': 18.4}}})
        service.save(directory / 'result.json', {'files': {'result': 'Lo-fi Song (Original Voice).wav'}, 'checks': {'result': {'seconds': 6.}}})
        service.save(directory / 'job.json', {'id': identifier, 'title': 'Song', 'style': 'Lo-fi', 'state': 'completed', 'mode': 'remix',
            'variant': 'A', 'pair_id': 'p' * 32, 'duration': 6., 'created_at': 1, 'voice': voice,
            'config': {'format': 'both', 'instrumental': False, 'voice': voice}})
        api, stop = self.http()
        try:
            work = next(j for j in api('/api/state')['jobs'] if j['id'] == identifier)
            self.assertEqual(work['output_name'], 'Lo-fi Song (Original Voice)')
            self.assertEqual(work['voice_info'], {'skipped': False, 'phrases': 3, 'octave_shift': -12, 'octave_mode': 'auto', 'diffusion_steps': 30,
                'reference_seconds': 20, 'seconds': 41.2, 'notice': '已换声。', 'speaker_similarity_before': .312,
                'speaker_similarity_after': .701, 'pitch_error_cents_median': 18.4})
            derived = api('/api/jobs/' + identifier + '/adjust', {'adjust': {'speed_mode': 'linked', 'tempo': 80}})
            self.assertEqual((derived['output_name'], derived['voice']), ('Lo-fi Song (Original Voice, Slowed 80%)', voice))
            self.assertNotIn('voice', derived['config'])
        finally:
            stop()
        config = {'mode': 'remix', 'duration': 6., 'voice': voice, 'melody_mode': 'reference', 'format': 'both'}
        (directory / 'Lo-fi Song (Original Voice).wav').replace(directory / 'result.wav')
        engine.validate(directory, config)
        result = service.read(directory / 'result.json')
        self.assertTrue(result['notice'].endswith('已换声。'))
        self.assertEqual(set(result['files']), {'result', 'original', 'mp3'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
