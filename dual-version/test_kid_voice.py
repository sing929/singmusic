"""Kid voice: the octave / formant plan, both processing methods on a synthetic sung line, the remix
with the new accompaniment, the pipeline order and the result notice. No model is run."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

import audio_adjust
import engine
import kid_voice
import service
import voice_swap

SR = 48000


def vowel(seconds, frequency, formants=(700., 1200., 2600.), rate=SR):
    """A sung vowel: harmonics of `frequency` (with vibrato) weighted by three resonances."""
    t = np.arange(int(seconds * rate)) / rate
    phase = 2 * np.pi * np.cumsum(frequency * (1 + .004 * np.sin(2 * np.pi * 5 * t))) / rate
    wave = np.zeros_like(t)
    for k in range(1, int(8000 / frequency)):
        weight = sum(1 / (1 + ((k * frequency - f) / 150) ** 2) for f in formants)
        wave += weight * np.sin(k * phase)
    return (.3 * wave / np.max(np.abs(wave))).astype(np.float32)


def centroid(signal):
    middle = signal[len(signal) // 4:3 * len(signal) // 4]
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    frequencies = np.fft.rfftfreq(len(middle), 1 / SR)
    return float((spectrum * frequencies).sum() / spectrum.sum())


def stereo(signal):
    return np.column_stack((signal, signal))


class KidVoiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-kid-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def stems(self, jobdir, vocals, seconds=4.):
        accompaniment = .1 * np.sin(2 * np.pi * 110 * np.arange(int(seconds * SR)) / SR).astype(np.float32)
        folder = jobdir / 'generated-stems'
        folder.mkdir(parents=True)
        sf.write(folder / 'vocals.wav', stereo(vocals), SR, subtype='PCM_24')
        sf.write(folder / 'instrumental.wav', stereo(accompaniment), SR, subtype='PCM_24')
        sf.write(jobdir / 'generated.wav', stereo(vocals + accompaniment), SR, subtype='PCM_24')
        service.save(folder / 'stems.json', {'vocals': str(folder / 'vocals.wav'), 'instrumental': str(folder / 'instrumental.wav')})

    def test_plan_and_rubberband_octave(self):
        self.assertEqual(kid_voice.plan(50.), (12, 5))
        self.assertEqual(kid_voice.plan(62.), (0, 3))
        self.assertEqual(kid_voice.plan(None), (0, 3))
        self.assertEqual(kid_voice.rubberband_filter(12), 'rubberband=pitch=2.000000:formant=shifted:pitchq=quality:channels=together')

    def test_formants_move_while_the_notes_stay(self):
        line = vowel(3, 220)
        changed, method = kid_voice.change(line, 0, 3, engine.FFMPEG, self.root)
        self.assertEqual(method, 'spectral')
        self.assertAlmostEqual(voice_swap.median_pitch(changed[:len(line)]), voice_swap.median_pitch(line), delta=.35)
        self.assertGreater(centroid(changed) / centroid(line), 1.1)
        self.assertLess(centroid(changed) / centroid(line), 1.4)
        low = vowel(3, 130)
        installed = audio_adjust.has_rubberband(engine.FFMPEG)
        for name, rubberband in (('rubberband', True), ('spectral', False)):
            if rubberband and not installed:
                continue
            with self.subTest(method=name), patch.object(audio_adjust, 'has_rubberband', return_value=rubberband):
                octave, method = kid_voice.change(low, 12, 5, engine.FFMPEG, self.root)
                self.assertEqual(method, name)
                self.assertAlmostEqual(voice_swap.median_pitch(octave[:3 * SR]), voice_swap.median_pitch(low) + 12, delta=.5)
                # One octave up, with the formants ending 5 semitones (not an octave) higher.
                self.assertGreater(centroid(octave) / centroid(low), 1.15)
                self.assertLess(centroid(octave) / centroid(low), 1.6)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_convert_remixes_the_child_like_line(self):
        jobdir = self.root / 'low'
        line = np.concatenate((np.zeros(SR // 2, dtype=np.float32), vowel(3, 130), np.zeros(SR // 2, dtype=np.float32)))
        self.stems(jobdir, line)
        generated = (jobdir / 'generated.wav').read_bytes()
        messages = []
        report = kid_voice.convert(jobdir, {'kid': True}, lambda stage, message: messages.append(stage), engine.FFMPEG)
        self.assertEqual((report['skipped'], report['octave_shift'], report['formant_semitones']), (False, 12, 5))
        self.assertIn(report['method'], ('rubberband', 'spectral'))
        self.assertIn('童声', report['notice'])
        self.assertEqual(service.read(jobdir / 'kid-voice.json')['octave_shift'], 12)
        result, rate = sf.read(jobdir / 'result.wav', always_2d=True)
        self.assertEqual((rate, result.shape), (SR, (4 * SR, 2)))
        kid, _ = sf.read(jobdir / 'kid-voice/vocals-kid.wav', always_2d=True)
        sung = kid[SR // 2:SR * 7 // 2, 0]
        self.assertAlmostEqual(voice_swap.median_pitch(sung), voice_swap.median_pitch(line[SR // 2:SR * 7 // 2]) + 12, delta=.5)
        self.assertAlmostEqual(float(np.sqrt(np.mean(sung ** 2))), float(np.sqrt(np.mean(line[SR // 2:SR * 7 // 2] ** 2))), delta=.03)
        self.assertEqual((jobdir / 'generated.wav').read_bytes(), generated)
        self.assertEqual(messages, ['kid'])
        quiet = self.root / 'quiet'
        self.stems(quiet, np.zeros(4 * SR, dtype=np.float32))
        report = kid_voice.convert(quiet, {'kid': True}, lambda *args: None, engine.FFMPEG)
        self.assertTrue(report['skipped'])
        self.assertEqual((quiet / 'result.wav').read_bytes(), (quiet / 'generated.wav').read_bytes())
        self.assertEqual(kid_voice.summary(report)['skipped'], True)

    def test_pipeline_order_and_notice(self):
        source = self.root / 'song.wav'
        sf.write(source, stereo(vowel(6, 220)), SR, subtype='PCM_16')
        jobdir = self.root / 'job'
        jobdir.mkdir()
        config = {'engine': 'yue2', 'mode': 'remix', 'instrumental': False, 'profile': 'fast', 'lyrics': 'la la', 'auto_lyrics': True,
                  'source': str(source), 'start': 0, 'source_duration': 6, 'duration': 6, 'adjust': None, 'melody_mode': 'free',
                  'seed': 1, 'voice': None, 'kid': True, 'gender': 'kid'}
        stages, conversions, real_run = [], [], subprocess.run

        def run(args, **kwargs):
            if '--stage' not in args:
                return real_run(args, **kwargs)
            stages.append(args[-1])
            if args[-1] == 'generate':
                (jobdir / 'generated.wav').write_bytes((jobdir / 'source.wav').read_bytes())
        with patch.object(engine.subprocess, 'run', side_effect=run), \
                patch.object(kid_voice, 'convert', side_effect=lambda job, cfg, progress, ffmpeg: conversions.append(cfg['kid'])), \
                patch.object(voice_swap, 'convert') as swap:
            engine.run_pipeline(jobdir, config)
        self.assertEqual(stages, ['generate', 'separate-generated', 'validate'])
        self.assertEqual(conversions, [True])
        swap.assert_not_called()
        # The finished work's notice explains the kid voice.
        sf.write(jobdir / 'result.wav', stereo(vowel(6, 220)), SR, subtype='PCM_24')
        sf.write(jobdir / 'source.wav', stereo(vowel(6, 220)), SR, subtype='PCM_16')
        service.save(jobdir / 'kid-voice.json', {'skipped': False, 'notice': '已在本机把新歌声做成童声。'})
        engine.validate(jobdir, {**config, 'output_name': 'Lo-fi Song (Kid Voice)'})
        result = service.read(jobdir / 'result.json')
        self.assertTrue(result['notice'].endswith('已在本机把新歌声做成童声。'))
        self.assertEqual(result['files']['result'], 'Lo-fi Song (Kid Voice).wav')


if __name__ == '__main__':
    unittest.main(verbosity=2)
