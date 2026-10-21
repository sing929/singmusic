"""Swap the generated singing voice for the original singer's timbre, on this computer.

Seed-VC's singing model (44.1 kHz, F0-conditioned) runs as a separate program in its own
environment (`local-test/seed-vc`). This module picks a clean reference clip from the
original song's separated vocals, cuts the generated vocals into phrases, and puts the
converted phrases back at their exact positions before remixing them with the generated
accompaniment. Melody, lyrics and timing come from the generated vocals; only the timbre
changes. Nothing is uploaded: the engine loads its models from the local cache offline.
"""
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from generation_controls import LETTERS, MAX_VERSIONS, LEGACY_VERSIONS

ROOT = Path(__file__).resolve().parent
TEST = Path(os.environ.get('SING_LOCAL_TEST', str(ROOT.parent / 'local-test')))
HOME = Path(os.environ.get('SING_VOICE_HOME', str(TEST / 'seed-vc')))
PYTHON = HOME / 'env/Scripts/python.exe'
SOURCE = HOME / 'seed-vc'
RUNNER = ROOT / 'seedvc_runner.py'
# Pinned upstream code and the singing checkpoint it loads (see seedvc-download.ps1).
SEEDVC_COMMIT = '51383efd921027683c89e5348211d93ff12ac2a8'
MODEL = 'DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth'
RATE = 48000
DEFAULT = {'versions': [False] * MAX_VERSIONS, 'octave': 'auto'}
OCTAVES = ('auto', 'none', 'up', 'down')
STEPS = {'fast': 15, 'balanced': 30, 'quality': 50}
CFG_RATE = 0.7
LABEL = 'Original Voice'
REFERENCE_SECONDS = 20.
MINIMUM_REFERENCE = 3.
LONGEST_PHRASE = 30.
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def install_info():
    try:
        info = json.loads((HOME / 'install.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def available():
    """True once seedvc-install.ps1 finished its GPU self-test on this computer."""
    info = install_info()
    return bool(info and info.get('complete') and PYTHON.is_file() and (SOURCE / 'inference.py').is_file()
                and RUNNER.is_file())


def validate(value, *, inherit=False):
    """Shared or per-song setting: one switch per version letter A-T, and the octave rule.
    None means a song follows the shared setting. Ten switches (before v1.6.0) gain K-T switched off."""
    if value is None and inherit:
        return None
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('换声设置无效。')
    versions, octave = value.get('versions', DEFAULT['versions']), value.get('octave', DEFAULT['octave'])
    if isinstance(versions, list) and len(versions) == LEGACY_VERSIONS:
        versions = versions + [False] * (MAX_VERSIONS - LEGACY_VERSIONS)
    if (not isinstance(versions, list) or len(versions) != MAX_VERSIONS or not all(isinstance(v, bool) for v in versions)
            or octave not in OCTAVES):
        raise ValueError('换声设置无效。')
    return {'versions': list(versions), 'octave': octave}


def for_variant(setting, letter):
    """The job request for one version letter, or None when that version keeps its generated voice."""
    setting = validate(setting if setting is not None else {})
    return {'target': 'original', 'octave': setting['octave']} if setting['versions'][LETTERS.index(letter)] else None


def validate_job(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'target', 'octave'} or value['target'] != 'original'
            or value['octave'] not in OCTAVES):
        raise ValueError('换声设置无效。')
    return dict(value)


def octave_shift(mode, source_pitch, reference_pitch):
    """Whole-octave shift only, so the converted voice stays in the song's key."""
    if mode == 'up':
        return 12
    if mode == 'down':
        return -12
    if mode == 'none' or source_pitch is None or reference_pitch is None:
        return 0
    difference = reference_pitch - source_pitch
    # Within a fifth the singer can take the melody as written.
    return max(-12, min(12, 12 * round(difference / 12))) if abs(difference) >= 7 else 0


def _read(path):
    import librosa
    import soundfile as sf
    audio, rate = sf.read(path, always_2d=True, dtype='float32')
    if rate != RATE:
        audio = librosa.resample(audio.T, orig_sr=rate, target_sr=RATE).T
    return audio


def active_regions(signal, rate=RATE, *, gap=.6, pad=.15, shortest=.25, floor_db=-50., relative_db=-35.):
    """Sample ranges where someone sings: 10 ms RMS above a level set by the loud parts."""
    import numpy as np
    hop = rate // 100
    frames = len(signal) // hop
    if not frames:
        return []
    rms = np.sqrt((signal[:frames * hop].reshape(frames, hop) ** 2).mean(axis=1) + 1e-12)
    threshold = max(10 ** (floor_db / 20), float(np.percentile(rms, 95)) * 10 ** (relative_db / 20))
    active = np.flatnonzero(rms > threshold)
    if not len(active):
        return []
    regions = []
    start = previous = int(active[0])
    for frame in active[1:]:
        frame = int(frame)
        if (frame - previous - 1) * hop > gap * rate:
            regions.append((start, previous + 1))
            start = frame
        previous = frame
    regions.append((start, previous + 1))
    margin = int(pad * rate)
    result = []
    for begin, end in regions:
        begin, end = max(0, begin * hop - margin), min(len(signal), end * hop + margin)
        if end - begin >= shortest * rate:
            if result and begin <= result[-1][1]:
                result[-1] = (result[-1][0], end)
            else:
                result.append((begin, end))
    return result


def phrases(signal, rate=RATE, longest=LONGEST_PHRASE):
    """Sung regions, with any region longer than `longest` split at its quietest 10 ms."""
    import numpy as np
    result = []
    hop = rate // 100
    for begin, end in active_regions(signal, rate):
        while end - begin > longest * rate:
            low, high = begin + int(longest * .6 * rate), begin + int(longest * rate)
            window = signal[low:high]
            frames = len(window) // hop
            rms = np.sqrt((window[:frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
            cut = low + int(np.argmin(rms)) * hop
            result.append((begin, cut))
            begin = cut
        result.append((begin, end))
    return result


def choose_reference(vocals, rate=RATE, seconds=REFERENCE_SECONDS, minimum=MINIMUM_REFERENCE):
    """A voice-only clip: the sung regions joined with short fades, then the steadiest loud stretch."""
    import numpy as np
    signal = vocals.mean(axis=1) if vocals.ndim == 2 else vocals
    pieces = []
    fade = int(.01 * rate)
    # Tighter than the phrase cut: pauses carry no timbre, so they are left out of the clip.
    for begin, end in active_regions(signal, rate, gap=.15, pad=.02):
        piece = signal[begin:end].copy()
        ramp = np.linspace(0, 1, min(fade, len(piece) // 2))
        if len(ramp):
            piece[:len(ramp)] *= ramp
            piece[len(piece) - len(ramp):] *= ramp[::-1]
        pieces.append(piece)
    voice = np.concatenate(pieces) if pieces else np.zeros(0, dtype=signal.dtype)
    if len(voice) < minimum * rate:
        raise RuntimeError(f'原曲分离出的演唱不足 {minimum:g} 秒，无法作为换声的目标音色。请确认这首歌有清楚的人声，或关闭换声。')
    length = int(seconds * rate)
    start = 0
    if len(voice) > length:
        hop = rate // 10
        frames = len(voice) // hop
        rms = np.sqrt((voice[:frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
        window = length // hop
        # Highest median level avoids clips dominated by breaths or quiet bleed.
        scores = [np.median(rms[i:i + window]) for i in range(0, frames - window + 1, 5)]
        start = int(np.argmax(scores)) * 5 * hop
    clip = voice[start:start + length]
    peak = float(np.max(np.abs(clip)))
    return clip * (.9 / peak if peak > 0 else 1.), {'seconds': len(clip) / rate, 'voiced_seconds': len(voice) / rate}


def median_pitch(signal, rate=RATE, limit=45.):
    """Median sung pitch in MIDI notes over at most `limit` seconds, or None."""
    import librosa
    import numpy as np
    clip = librosa.resample(signal[:int(limit * rate)].astype('float32'), orig_sr=rate, target_sr=16000)
    if len(clip) < 16000:
        return None
    f0, voiced, _ = librosa.pyin(clip, fmin=65., fmax=1100., sr=16000, frame_length=1024, hop_length=320)
    f0 = f0[voiced & np.isfinite(f0)]
    return float(np.median(librosa.hz_to_midi(f0))) if len(f0) >= 25 else None


def prepare_reference(jobdir, stems_dir):
    """Write voice-swap/reference.wav from the original song's separated vocals."""
    import soundfile as sf
    from engine import write_json
    stems = json.loads((Path(stems_dir) / 'stems.json').read_text(encoding='utf-8'))
    clip, info = choose_reference(_read(stems['vocals']))
    work = Path(jobdir) / 'voice-swap'
    work.mkdir(exist_ok=True)
    sf.write(work / 'reference.wav', clip, RATE, subtype='PCM_16')
    info.update(source=Path(stems_dir).name, reference_pitch_midi=median_pitch(clip))
    write_json(Path(jobdir) / 'voice-reference.json', info)
    return info


def _tail(path, size=3000):
    try:
        with open(path, 'rb') as stream:
            stream.seek(max(0, stream.seek(0, 2) - size))
            return stream.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def run_engine(plan_path, report_path, log_path, progress, timeout):
    """Run the Seed-VC runner in its own environment; report real phrase progress from its log."""
    env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
           'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1'}
    for key in ('PYTHONPATH', 'PYTHONHOME', 'HF_HOME', 'HF_HUB_CACHE'):
        env.pop(key, None)
    command = [str(PYTHON), str(RUNNER), '--seedvc', str(SOURCE), '--plan', str(plan_path), '--report', str(report_path)]
    deadline = time.monotonic() + timeout
    last = None
    with open(log_path, 'wb') as log:
        process = subprocess.Popen(command, cwd=str(SOURCE), env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=FLAGS)
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError('换声处理超过时限。')
                for line in reversed(_tail(log_path).splitlines()):
                    if line.startswith('{"event": "phrase"'):
                        try:
                            event = json.loads(line)
                        except ValueError:
                            break
                        if event['done'] != last:
                            last = event['done']
                            progress('voice', f"换成原唱音色 · 乐句 {event['done']} / {event['total']}")
                        break
                time.sleep(.5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
    if process.returncode:
        raise RuntimeError('换声引擎未完成（Seed-VC）：' + _tail(log_path, 1500).strip()[-1500:])


def convert(jobdir, config, progress):
    """Generated stems + reference clip -> converted vocals remixed into result.wav."""
    import librosa
    import numpy as np
    import soundfile as sf
    from engine import write_json
    jobdir = Path(jobdir)
    voice = config['voice']
    work = jobdir / 'voice-swap'
    work.mkdir(exist_ok=True)
    stems = json.loads((jobdir / 'generated-stems/stems.json').read_text(encoding='utf-8'))
    vocals, instrumental = _read(stems['vocals']), _read(stems['instrumental'])
    length = len(instrumental)
    vocals = librosa.util.fix_length(vocals, size=length, axis=0)
    signal = vocals.mean(axis=1)
    regions = phrases(signal)
    reference = json.loads((jobdir / 'voice-reference.json').read_text(encoding='utf-8'))
    report = {'engine': 'Seed-VC', 'seedvc_commit': SEEDVC_COMMIT, 'model': MODEL,
              'target': 'original singer, separated from the uploaded song (' + reference['source'] + ')',
              'reference_seconds': reference['seconds'], 'octave_mode': voice['octave']}
    if not regions:
        shutil.copy2(jobdir / 'generated.wav', jobdir / 'result.wav')
        report.update(skipped=True, phrases=0, notice='生成结果中检测不到人声，已保留生成的原声，未换声。')
        write_json(jobdir / 'voice-swap.json', report)
        return report
    sung = np.concatenate([signal[a:b] for a, b in regions])
    source_pitch = median_pitch(sung)
    shift = octave_shift(voice['octave'], source_pitch, reference.get('reference_pitch_midi'))
    segments = []
    for index, (begin, end) in enumerate(regions):
        source = work / f'phrase-{index:03d}.wav'
        sf.write(source, signal[begin:end], RATE, subtype='FLOAT')
        segments.append({'source': str(source), 'output': str(work / f'converted-{index:03d}.wav')})
    steps = STEPS.get(config.get('profile', 'quality'), STEPS['quality'])
    plan = {'reference': str(work / 'reference.wav'), 'segments': segments, 'diffusion_steps': steps,
            'inference_cfg_rate': CFG_RATE, 'semi_tone_shift': shift, 'fp16': True,
            'allow_cpu': os.environ.get('SING_VOICE_ALLOW_CPU') == '1'}
    write_json(work / 'plan.json', plan)
    progress('voice', f'换成原唱音色 · 共 {len(segments)} 个乐句')
    started = time.perf_counter()
    run_engine(work / 'plan.json', work / 'runner.json', work / 'runner.log', progress,
               timeout=max(1800, int(config['duration'] * 30)))
    runner = json.loads((work / 'runner.json').read_text(encoding='utf-8'))
    converted = np.zeros(length, dtype=np.float32)
    fade = int(.01 * RATE)
    for (begin, end), segment in zip(regions, segments):
        audio, rate = sf.read(segment['output'], dtype='float32', always_2d=True)
        audio = audio.mean(axis=1)
        if rate != RATE:
            audio = librosa.resample(audio, orig_sr=rate, target_sr=RATE)
        audio = librosa.util.fix_length(audio, size=end - begin)
        ramp = np.linspace(0, 1, min(fade, len(audio) // 2))
        if len(ramp):
            audio[:len(ramp)] *= ramp
            audio[len(audio) - len(ramp):] *= ramp[::-1]
        converted[begin:end] = audio
    if not np.isfinite(converted).all():
        raise RuntimeError('换声结果包含无效数值。')
    def level(x):
        return float(np.sqrt(np.mean(np.concatenate([x[a:b] for a, b in regions]) ** 2)))
    # Keep the generated vocal/accompaniment balance.
    gain = float(np.clip(level(signal) / max(level(converted), 1e-6), .25, 4.))
    voice_track = np.column_stack((converted, converted)) * gain
    mix = instrumental + voice_track
    peak = float(np.max(np.abs(mix)))
    mix_gain = min(1., .989 / peak) if peak > 0 else 1.
    sf.write(work / 'vocals-converted.wav', voice_track, RATE, subtype='PCM_24')
    output = jobdir / 'result.new.wav'
    sf.write(output, mix * mix_gain, RATE, subtype='PCM_24')
    output.replace(jobdir / 'result.wav')
    for segment in segments:
        Path(segment['source']).unlink(missing_ok=True)
    report.update(skipped=False, phrases=len(regions), sung_seconds=round(len(sung) / RATE, 3),
                  source_pitch_midi=source_pitch, reference_pitch_midi=reference.get('reference_pitch_midi'),
                  octave_shift=shift, diffusion_steps=steps, inference_cfg_rate=CFG_RATE,
                  vocal_gain=gain, mix_gain=mix_gain, seconds=round(time.perf_counter() - started, 2),
                  runner=runner, notice='已用本机 Seed-VC 把新歌声换成原曲演唱者的音色，旋律、歌词与节奏来自新生成的演唱；请试听复核咬字、音准与分离残留。')
    write_json(jobdir / 'voice-swap.json', report)
    return report


def summary(report):
    """The fields shown with a finished work."""
    if not report:
        return None
    keys = ('skipped', 'phrases', 'octave_shift', 'octave_mode', 'diffusion_steps', 'reference_seconds', 'seconds', 'notice')
    value = {k: report[k] for k in keys if k in report}
    metrics = (report.get('runner') or {}).get('metrics') or {}
    for key in ('speaker_similarity_before', 'speaker_similarity_after', 'pitch_error_cents_median'):
        if isinstance(metrics.get(key), (int, float)) and math.isfinite(metrics[key]):
            value[key] = round(metrics[key], 3)
    return value
