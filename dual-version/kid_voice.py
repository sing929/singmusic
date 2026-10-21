"""Turn newly generated singing into a child's voice, on this computer.

YuE2 is asked for a child singer; afterwards UVR separates the new vocals. A take in an adult
male range moves up one octave, so it stays in key (Rubber Band in FFmpeg when the local build has
it, otherwise a resampling shift). A spectral-envelope warp then sets the formants (the resonances
that tell a big adult voice from a small child's) a few semitones above the original take while the
notes stay where they are. The new vocals are level-matched and remixed with the new accompaniment.
Only newly generated singing is changed: the original recording never is.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

LABEL = 'Kid Voice'
RATE = 48000
# Below A3 the take sits in an adult male range; children sing about an octave higher.
LOW_MIDI = 57
# A child's shorter vocal tract puts formants about 19% above an adult woman's (+3 semitones)
# and about 33% above an adult man's (+5 semitones).
FORMANT_FROM_HIGH = 3
FORMANT_FROM_LOW = 5
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def plan(source_pitch):
    """(octave shift, formant shift) in semitones for a take with this median pitch (MIDI note)."""
    if source_pitch is not None and source_pitch < LOW_MIDI:
        return 12, FORMANT_FROM_LOW
    return 0, FORMANT_FROM_HIGH


def rubberband_filter(octave):
    """Rubber Band raises the notes, and with them the formants, by `octave`. Its formant-keeping
    mode is not used: measured with FFmpeg 8.1 it holds only partly going up and not at all going
    down, so the spectral warp alone decides where the formants end up."""
    return f'rubberband=pitch={2 ** (octave / 12):.6f}:formant=shifted:pitchq=quality:channels=together'


def warp_formants(signal, factor, rate=RATE, n_fft=2048, hop=256):
    """Move the spectral envelope by `factor` and keep the harmonics, so only the formants change.
    Each frame's envelope comes from cepstral smoothing; the magnitude is reweighted by the warped
    envelope over the original one and the phase is kept."""
    import librosa
    import numpy as np
    spectrum = librosa.stft(signal.astype(np.float32), n_fft=n_fft, hop_length=hop)
    magnitude = np.abs(spectrum)
    cepstrum = np.fft.irfft(np.log(magnitude + 1e-7), axis=0)
    # About 0.8 ms of quefrency: smooth enough to ignore harmonics of pitches up to ~1 kHz.
    lifter = int(.0008 * rate)
    cepstrum[lifter:n_fft - lifter + 1] = 0
    envelope = np.fft.rfft(cepstrum, axis=0).real
    position = np.arange(envelope.shape[0]) / factor
    low = np.clip(np.floor(position).astype(int), 0, envelope.shape[0] - 1)
    high = np.clip(low + 1, 0, envelope.shape[0] - 1)
    weight = np.clip(position - low, 0, 1)[:, None]
    warped = envelope[low] * (1 - weight) + envelope[high] * weight
    gain = np.exp(np.clip(warped - envelope, -2.3, 2.3))
    return librosa.istft(spectrum * gain, hop_length=hop, length=len(signal))


def _rubberband(signal, octave, ffmpeg, work):
    import soundfile as sf
    source, output = work / 'kid-source.wav', work / 'kid-shifted.wav'
    sf.write(source, signal, RATE, subtype='FLOAT')
    try:
        subprocess.run([str(ffmpeg), '-nostdin', '-y', '-v', 'error', '-i', str(source), '-af', rubberband_filter(octave),
                        '-ac', '1', '-ar', str(RATE), '-c:a', 'pcm_f32le', str(output)], check=True, creationflags=FLAGS, timeout=1800)
        audio, _ = sf.read(output, dtype='float32')
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
    return audio


def change(signal, octave, formant, ffmpeg, work):
    """The vocal line with the octave and formant shifts, and the method used for the octave."""
    import librosa
    import audio_adjust
    if not octave:
        return warp_formants(signal, 2 ** (formant / 12)), 'spectral'
    if audio_adjust.has_rubberband(ffmpeg):
        shifted, method = _rubberband(signal, octave, ffmpeg, work), 'rubberband'
    else:
        shifted, method = librosa.effects.pitch_shift(signal, sr=RATE, n_steps=octave), 'spectral'
    # The octave shift doubled the formants too; the warp brings them back to +formant.
    return warp_formants(shifted, 2 ** ((formant - octave) / 12)), method


def convert(jobdir, config, progress, ffmpeg):
    """Generated stems -> child-like vocals remixed into result.wav; writes kid-voice.json."""
    import librosa
    import numpy as np
    import soundfile as sf
    import voice_swap
    from engine import write_json
    jobdir = Path(jobdir)
    work = jobdir / 'kid-voice'
    work.mkdir(exist_ok=True)
    stems = json.loads((jobdir / 'generated-stems/stems.json').read_text(encoding='utf-8'))
    vocals, instrumental = voice_swap._read(stems['vocals']), voice_swap._read(stems['instrumental'])
    length = len(instrumental)
    signal = librosa.util.fix_length(vocals, size=length, axis=0).mean(axis=1)
    regions = voice_swap.active_regions(signal)
    report = {'effect': 'kid voice', 'label': LABEL}
    if not regions:
        shutil.copy2(jobdir / 'generated.wav', jobdir / 'result.wav')
        report.update(skipped=True, notice='生成结果中检测不到人声，已保留生成的原声，未做童声处理。')
        write_json(jobdir / 'kid-voice.json', report)
        return report
    pitch = voice_swap.median_pitch(np.concatenate([signal[a:b] for a, b in regions]))
    octave, formant = plan(pitch)
    progress('kid', '正在把新歌声变成童声' + (' · 升高八度' if octave else '') + f' · 共振峰 +{formant} 半音')
    started = time.perf_counter()
    converted, method = change(signal, octave, formant, ffmpeg, work)
    converted = librosa.util.fix_length(np.asarray(converted, dtype=np.float32), size=length)
    if not np.isfinite(converted).all():
        raise RuntimeError('童声处理结果包含无效数值。')
    def level(x):
        return float(np.sqrt(np.mean(np.concatenate([x[a:b] for a, b in regions]) ** 2)))
    # Keep the generated vocal/accompaniment balance.
    gain = float(np.clip(level(signal) / max(level(converted), 1e-6), .25, 4.))
    voice_track = np.column_stack((converted, converted)) * gain
    mix = instrumental + voice_track
    peak = float(np.max(np.abs(mix)))
    mix_gain = min(1., .989 / peak) if peak > 0 else 1.
    sf.write(work / 'vocals-kid.wav', voice_track, RATE, subtype='PCM_24')
    output = jobdir / 'result.new.wav'
    sf.write(output, mix * mix_gain, RATE, subtype='PCM_24')
    output.replace(jobdir / 'result.wav')
    report.update(skipped=False, method=method, source_pitch_midi=pitch, octave_shift=octave, formant_semitones=formant,
                  vocal_gain=gain, mix_gain=mix_gain, seconds=round(time.perf_counter() - started, 2),
                  notice='已在本机把新歌声做成童声：' + (f'整体升高八度（{"Rubber Band" if method == "rubberband" else "重采样"}）、' if octave else '') +
                  f'共振峰提高 {formant} 个半音，旋律与歌词不变；请试听复核。')
    write_json(jobdir / 'kid-voice.json', report)
    return report


def summary(report):
    """The fields shown with a finished work."""
    if not report:
        return None
    return {k: report[k] for k in ('skipped', 'method', 'octave_shift', 'formant_semitones', 'source_pitch_midi', 'notice') if k in report}
