"""Key, tempo and reverb changes rendered on this computer with FFmpeg and NumPy.

Rubber Band (when the local FFmpeg build includes it) keeps key and tempo independent
with the least smearing; otherwise asetrate + atempo is used. Linked speed is a plain
resample, so pitch follows speed exactly like a slowed / sped-up record.
"""
import math
import subprocess
from pathlib import Path

DEFAULT = {'semitones': 0, 'tempo': 100, 'speed_mode': 'independent', 'reverb': 0}
SPEED_MODES = ('independent', 'linked')
RATE = 48000
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_RUBBERBAND = {}


def _whole(value, low, high, message):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value != int(value) or not low <= value <= high):
        raise ValueError(message)
    return int(value)


def validate(value, *, inherit=False):
    """A normalized adjustment; None means a song follows the shared setting."""
    if value is None and inherit:
        return None
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('变调变速设置无效。')
    value = {**DEFAULT, **value}
    if value['speed_mode'] not in SPEED_MODES:
        raise ValueError('变速方式无效。')
    return {'semitones': _whole(value['semitones'], -12, 12, '变调须为 -12 至 +12 的整数半音。'),
            'tempo': _whole(value['tempo'], 50, 150, '速度须为 50%–150% 的整数。'),
            'speed_mode': value['speed_mode'],
            'reverb': _whole(value['reverb'], 0, 100, '混响须为 0–100 的整数。')}


def speed(adjust):
    return adjust['tempo'] / 100 if adjust else 1.0


def semitones(adjust):
    """Effective pitch change. Linked speed moves pitch with the playback rate."""
    if not adjust:
        return 0.0
    if adjust['speed_mode'] == 'linked':
        return 12 * math.log2(speed(adjust))
    return float(adjust['semitones'])


def changes_source(adjust):
    """True when key or tempo change; reverb alone leaves timing and pitch untouched."""
    return bool(adjust) and (adjust['tempo'] != 100 or
        (adjust['speed_mode'] == 'independent' and adjust['semitones'] != 0))


def changes_anything(adjust):
    return changes_source(adjust) or bool(adjust and adjust['reverb'])


def output_seconds(seconds, adjust):
    """Exact 48 kHz length after the tempo change."""
    return round(seconds * RATE / speed(adjust)) / RATE


def label(adjust, reverb=True):
    """Short portable description, also used in work names: Key -2, Tempo 90%, Reverb 40."""
    if not adjust:
        return ''
    parts = []
    if adjust['speed_mode'] == 'linked':
        if adjust['tempo'] != 100:
            parts.append(('Slowed' if adjust['tempo'] < 100 else 'Sped up') + f" {adjust['tempo']}%")
    else:
        if adjust['semitones']:
            parts.append(f"Key {adjust['semitones']:+d}")
        if adjust['tempo'] != 100:
            parts.append(f"Tempo {adjust['tempo']}%")
    if reverb and adjust['reverb']:
        parts.append(f"Reverb {adjust['reverb']}")
    return ', '.join(parts)


def has_rubberband(ffmpeg):
    key = str(ffmpeg)
    if key not in _RUBBERBAND:
        try:
            listing = subprocess.run([key, '-hide_banner', '-filters'], capture_output=True, text=True,
                timeout=30, creationflags=FLAGS).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ''
        _RUBBERBAND[key] = any(line.split()[1:2] == ['rubberband'] for line in listing.splitlines())
    return _RUBBERBAND[key]


def _tempo_steps(factor):
    # atempo only accepts 0.5–100 per instance and sounds best within 0.5–2.
    steps = []
    while factor < .5 - 1e-9:
        steps.append(.5)
        factor /= .5
    while factor > 2 + 1e-9:
        steps.append(2.)
        factor /= 2
    return steps + [factor]


def audio_filter(adjust, rubberband):
    """(FFmpeg filter, method) for the key/tempo part, or (None, None) when unchanged."""
    if not changes_source(adjust):
        return None, None
    rate = speed(adjust)
    if adjust['speed_mode'] == 'linked':
        return f'asetrate={round(RATE * rate)},aresample={RATE}', 'resample'
    pitch = 2 ** (adjust['semitones'] / 12)
    if rubberband:
        return f'rubberband=tempo={rate:.6f}:pitch={pitch:.6f}:pitchq=quality:channels=together', 'rubberband'
    stages = []
    if adjust['semitones']:
        shifted = round(RATE * pitch)
        stages += [f'asetrate={shifted}', f'aresample={RATE}']
        rate /= shifted / RATE
    if abs(rate - 1) > 1e-9:
        stages += [f'atempo={step:.6f}' for step in _tempo_steps(rate)]
    return ','.join(stages), 'atempo'


def reverb(audio, amount, rate=RATE, seed=20261016):
    """Stereo convolution reverb built from a seeded, band-limited decaying noise tail."""
    if not amount:
        return audio
    import numpy as np
    from scipy.signal import butter, oaconvolve, sosfilt
    mix = amount / 100
    decay = 1.2 + 2.3 * mix
    length = int(rate * decay)
    t = np.arange(length) / rate
    tail = np.random.default_rng(seed).standard_normal((length, 2)) * np.exp(-6.9078 * t / decay)[:, None]
    tail = sosfilt(butter(2, (180, 7500), btype='bandpass', fs=rate, output='sos'), tail, axis=0)
    impulse = np.vstack((np.zeros((int(.025 * rate), 2)), tail))
    impulse /= np.sqrt((impulse ** 2).sum(axis=0))
    wet = np.column_stack([oaconvolve(audio[:, c], impulse[:, c])[:len(audio)] for c in range(2)])
    # The work keeps its length, so the tail is faded instead of cut off abruptly.
    fade = min(len(audio), rate)
    wet[len(audio) - fade:] *= np.linspace(1, 0, fade)[:, None]
    return audio * (1 - .35 * mix) + wet * (.6 * mix)


def render(source, destination, adjust, ffmpeg, *, target_frames=None, include_reverb=True, subtype='PCM_24'):
    """Render a 48 kHz stereo WAV with the adjustment; source may equal destination."""
    import numpy as np
    import soundfile as sf
    source, destination = Path(source), Path(destination)
    info = sf.info(source)
    if target_frames is None:
        target_frames = round(output_seconds(info.frames / info.samplerate, adjust) * RATE)
    filter_text, method = audio_filter(adjust, has_rubberband(ffmpeg))
    temporary = destination.with_name(destination.stem + '.adjusting.wav')
    command = [str(ffmpeg), '-nostdin', '-y', '-v', 'error', '-i', str(source)]
    if filter_text:
        command += ['-af', filter_text]
    command += ['-ac', '2', '-ar', str(RATE), '-c:a', 'pcm_f32le', str(temporary)]
    try:
        subprocess.run(command, check=True, creationflags=FLAGS, timeout=1800)
        audio, _ = sf.read(temporary, always_2d=True, dtype='float32')
    finally:
        temporary.unlink(missing_ok=True)
    # Filters differ from the exact target by a few milliseconds; trim or pad the end.
    if len(audio) >= target_frames:
        audio = audio[:target_frames]
    else:
        audio = np.vstack((audio, np.zeros((target_frames - len(audio), 2), dtype=audio.dtype)))
    amount = adjust['reverb'] if include_reverb and adjust else 0
    audio = reverb(audio, amount)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.
    gain = min(1., .989 / peak) if peak > 0 else 1.
    output = destination.with_name(destination.stem + '.new.wav')
    sf.write(output, audio * gain, RATE, subtype=subtype)
    output.replace(destination)
    return {'method': method or 'none', 'filter': filter_text, 'input_seconds': info.frames / info.samplerate,
            'output_seconds': target_frames / RATE, 'semitones': round(semitones(adjust), 3),
            'speed': speed(adjust), 'reverb': amount, 'gain': gain}
