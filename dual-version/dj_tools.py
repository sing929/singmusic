"""DJ edits and non-stop DJ mixes made from finished works on this computer.

The beat grid comes from librosa. A DJ edit adds a build-up and drop at the song's biggest energy
jump, opens the intro with a filter sweep, or ends on an echo-out. A non-stop mix beat-matches every
song to one tempo (Rubber Band or atempo in FFmpeg, pitch unchanged), blends neighbours over a set
number of beats with a bass swap and writes one long work. Finished works are only read.
"""
import math
import subprocess
from pathlib import Path

RATE = 48000
EFFECTS = {'buildup': 'Drop', 'filter': 'Filter Intro', 'echo': 'Echo Out'}
TRANSITIONS = (8, 16, 32)
MAX_MIX_SONGS = 20
MAX_MIX_SECONDS = 3600
ECHO_BEATS = 8
BUILD_BEATS = 8
# Beyond about ±16% a tempo match is more audible than the jump it avoids; such songs blend unmatched.
MATCH_RANGE = (.86, 1.16)
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def validate(value):
    """The chosen DJ effects in a fixed order; at least one."""
    if (not isinstance(value, list) or not value or len(set(map(str, value))) != len(value)
            or not all(isinstance(v, str) and v in EFFECTS for v in value)):
        raise ValueError('请至少选择一种 DJ 效果。')
    return [name for name in EFFECTS if name in value]


def label(effects):
    """Work-name label such as 'DJ Drop + Echo Out'."""
    return 'DJ ' + ' + '.join(EFFECTS[name] for name in effects) if effects else ''


def validate_mix(payload):
    """(work ids, order, beats per transition, name) of a non-stop mix request."""
    ids = payload.get('ids')
    if (not isinstance(ids, list) or not 2 <= len(ids) <= MAX_MIX_SONGS or len(set(map(str, ids))) != len(ids)
            or not all(isinstance(i, str) and len(i) == 32 and all(c in '0123456789abcdef' for c in i) for i in ids)):
        raise ValueError(f'DJ 串烧需要 2–{MAX_MIX_SONGS} 首不同的作品。')
    order = payload.get('order', 'manual')
    beats = payload.get('transition_beats', 16)
    name = payload.get('name', '')
    if order not in ('manual', 'auto') or beats not in TRANSITIONS or isinstance(beats, bool):
        raise ValueError('串烧排序或过渡长度无效。')
    if not isinstance(name, str) or len(name.strip()) > 60:
        raise ValueError('串烧名称最多 60 字。')
    return ids, order, beats, name.strip()


def read(path):
    import librosa
    import numpy as np
    import soundfile as sf
    audio, rate = sf.read(path, always_2d=True, dtype='float32')
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    if rate != RATE:
        audio = librosa.resample(audio.T, orig_sr=rate, target_sr=RATE).T
    return np.ascontiguousarray(audio[:, :2])


def beats(audio, rate=RATE):
    """(tempo in BPM, beat times in seconds); a steady 120 BPM grid when no beat is found."""
    import librosa
    import numpy as np
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    low = librosa.resample(np.ascontiguousarray(mono, dtype=np.float32), orig_sr=rate, target_sr=22050)
    tempo, frames = librosa.beat.beat_track(y=low, sr=22050, units='frames')
    tempo = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.
    times = librosa.frames_to_time(frames, sr=22050)
    if not 60 <= tempo <= 200 or len(times) < 8:
        tempo = 120.
        times = np.arange(0, len(mono) / rate, .5)
    return tempo, np.asarray(times, dtype=float)


def key_of(audio, rate=RATE):
    """(tonic 0-11, 'major' / 'minor', Camelot code) from the Krumhansl key profiles."""
    import librosa
    import numpy as np
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    low = librosa.resample(np.ascontiguousarray(mono[:int(rate * 120)], dtype=np.float32), orig_sr=rate, target_sr=22050)
    chroma = librosa.feature.chroma_cqt(y=low, sr=22050).mean(axis=1)
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    scores = [(np.corrcoef(chroma, np.roll(profile, tonic))[0, 1], tonic, name)
              for name, profile in (('major', major), ('minor', minor)) for tonic in range(12)]
    _, tonic, mode = max(scores, key=lambda s: -1 if math.isnan(s[0]) else s[0])
    number = (7 * tonic + (7 if mode == 'major' else 4)) % 12 + 1
    return tonic, mode, f'{number}{"B" if mode == "major" else "A"}'


def camelot_distance(first, second):
    """Steps around the Camelot wheel; switching major/minor on the same number counts one."""
    a, b = int(first[:-1]), int(second[:-1])
    steps = min(abs(a - b), 12 - abs(a - b))
    return steps + (first[-1] != second[-1])


def auto_order(songs):
    """Slowest song first, then always the nearest next song by tempo and key."""
    remaining = list(range(len(songs)))
    order = [min(remaining, key=lambda i: songs[i]['tempo'])]
    remaining.remove(order[0])
    while remaining:
        last = songs[order[-1]]
        nearest = min(remaining, key=lambda i: abs(songs[i]['tempo'] - last['tempo']) / 4 + camelot_distance(songs[i]['camelot'], last['camelot']))
        order.append(nearest)
        remaining.remove(nearest)
    return order


def target_tempo(tempos):
    """One mix tempo: the median, after folding double- and half-time readings onto it."""
    import numpy as np
    middle = float(np.median(tempos))
    folded = [min((t / 2, t, t * 2), key=lambda x: abs(math.log(x / middle))) for t in tempos]
    return float(np.median(folded)), folded


def music_span(audio, rate=RATE, floor_db=-45.):
    """First and last sample above the silence floor."""
    import numpy as np
    level = np.abs(audio).max(axis=1)
    loud = np.flatnonzero(level > 10 ** (floor_db / 20))
    return (int(loud[0]), int(loud[-1]) + 1) if len(loud) else (0, len(audio))


def sweep(segment, start_hz, end_hz, kind, rate=RATE, n_fft=2048, hop=512, order=4):
    """A low- or high-pass whose cutoff glides exponentially from start_hz to end_hz."""
    import librosa
    import numpy as np
    result = np.empty_like(segment)
    frequencies = librosa.fft_frequencies(sr=rate, n_fft=n_fft)[:, None]
    for channel in range(segment.shape[1]):
        spectrum = librosa.stft(np.ascontiguousarray(segment[:, channel]), n_fft=n_fft, hop_length=hop)
        cutoff = start_hz * (end_hz / start_hz) ** np.linspace(0, 1, spectrum.shape[1])[None, :]
        ratio = frequencies / cutoff if kind == 'lowpass' else cutoff / np.maximum(frequencies, 1.)
        result[:, channel] = librosa.istft(spectrum / np.sqrt(1 + ratio ** (2 * order)), hop_length=hop, length=len(segment))
    return result


def filter_intro(audio, tempo, times, rate=RATE):
    """The first 16 beats open up from a muffled low-pass to the full sound."""
    beat = 60 / tempo
    first = float(times[0]) if len(times) else 0.
    end = int(min(len(audio) / rate * .3, max(4., first + 16 * beat)) * rate)
    audio[:end] = sweep(audio[:end], 250, 20000, 'lowpass')
    return end / rate


def drop_point(audio, times, rate=RATE, after=0.):
    """The beat where the energy of the next 8 beats rises most above the previous 8."""
    import numpy as np
    duration = len(audio) / rate
    edges = [int(t * rate) for t in times] + [len(audio)]
    energy = np.array([float(np.sqrt(np.mean(audio[a:b] ** 2))) if b > a else 0. for a, b in zip(edges, edges[1:])])
    best, best_rise = None, -np.inf
    for index in range(BUILD_BEATS, len(times) - BUILD_BEATS):
        if not max(.2 * duration, after) <= times[index] <= .85 * duration:
            continue
        rise = energy[index:index + BUILD_BEATS].mean() - energy[index - BUILD_BEATS:index].mean()
        if rise > best_rise:
            best, best_rise = index, rise
    return float(times[best]) if best is not None else None


def buildup(audio, tempo, times, rate=RATE, after=0., seed=20261021):
    """Bass drains out under a rising noise riser and an accelerating snare roll for 8 beats,
    half a beat of silence, then the full song drops back in at its biggest energy jump."""
    import numpy as np
    drop = drop_point(audio, times, rate, after)
    beat = 60 / tempo
    if drop is None or drop - BUILD_BEATS * beat < after:
        return None
    start, end = int((drop - BUILD_BEATS * beat) * rate), int(drop * rate)
    section = sweep(audio[start:end], 20, 1500, 'highpass')
    count = end - start
    rng = np.random.default_rng(seed)
    progress = np.linspace(0, 1, count)
    level = float(np.sqrt(np.mean(audio[start:end] ** 2))) or .05
    riser = sweep(rng.standard_normal((count, 1)).astype(np.float32), 300, 8000, 'highpass')[:, 0]
    riser *= progress ** 2 / (float(np.sqrt(np.mean(riser ** 2))) or 1.)
    roll = np.zeros(count, dtype=np.float32)
    hit = np.exp(-np.arange(int(.09 * rate)) / (.025 * rate)) * rng.standard_normal(int(.09 * rate))
    # Quarter notes for four beats, eighths for two, sixteenths for the last two.
    for position in [i * beat for i in range(4)] + [4 * beat + i * beat / 2 for i in range(4)] + [6 * beat + i * beat / 4 for i in range(8)]:
        a = int(position * rate)
        b = min(count, a + len(hit))
        roll[a:b] += hit[:b - a] * (.35 + .65 * position / (BUILD_BEATS * beat))
    roll /= float(np.sqrt(np.mean(roll ** 2))) or 1.
    section += (riser * .3 + roll * .25)[:, None] * level
    gap = int(beat / 2 * rate)
    fade = int(.01 * rate)
    section[count - gap:] = 0
    section[count - gap - fade:count - gap] *= np.linspace(1, 0, fade)[:, None]
    audio[start:end] = section
    return drop


def echo_out(audio, tempo, times, rate=RATE):
    """The dry song stops on a beat 8 beats before its end; its last beat rings out in
    dotted-eighth echoes over 8 more beats. Returns the shorter or longer audio."""
    import numpy as np
    beat = 60 / tempo
    _, end = music_span(audio, rate)
    wanted = end / rate - ECHO_BEATS * beat
    candidates = [t for t in times if .25 * len(audio) / rate <= t <= wanted] or [max(.25 * len(audio) / rate, wanted)]
    cut = int(min(candidates, key=lambda t: abs(t - wanted)) * rate)
    tail = int(ECHO_BEATS * beat * rate)
    result = np.zeros((cut + tail, 2), dtype=np.float32)
    result[:cut] = audio[:cut]
    fade = int(.02 * rate)
    result[cut - fade:cut] *= np.linspace(1, 0, fade)[:, None]
    send = audio[max(0, cut - int(beat * rate)):cut].copy()
    delay = int(.75 * beat * rate)
    for repeat in range(1, 64):
        gain = .55 ** repeat
        position = cut - len(send) + repeat * delay
        if gain < .01 or position >= len(result):
            break
        # Each echo is a little darker, like a tape delay.
        send[1:] = .6 * send[1:] + .4 * send[:-1]
        piece = send[:len(result) - position] * gain
        result[position:position + len(piece)] += piece
    result[cut:] *= np.linspace(1, 0, tail)[:, None] ** 1.5
    return result, cut / rate


def render_effects(source, destination, effects, progress=lambda stage, message: None):
    """A DJ edit of a finished work as a new 48 kHz stereo WAV; returns the report."""
    import numpy as np
    import soundfile as sf
    audio = read(source)
    report = {'effects': effects, 'input_seconds': len(audio) / RATE}
    progress('analyzing', '正在分析节拍')
    tempo, times = beats(audio)
    report['tempo'] = round(tempo, 2)
    intro_end = 0.
    if 'filter' in effects:
        progress('effects', '正在制作滤波渐入')
        intro_end = report['filter_intro_seconds'] = round(filter_intro(audio, tempo, times), 3)
    if 'buildup' in effects:
        progress('effects', '正在制作 Build-up 与 Drop')
        drop = buildup(audio, tempo, times, after=intro_end)
        report['drop_seconds'] = round(drop, 3) if drop is not None else None
        if drop is None:
            report['buildup_skipped'] = '歌曲太短，找不到放置 Build-up 的位置。'
    if 'echo' in effects:
        progress('effects', '正在制作回声结尾')
        audio, cut = echo_out(audio, tempo, times)
        report['echo_cut_seconds'] = round(cut, 3)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.
    gain = min(1., .989 / peak) if peak > 0 else 1.
    output = Path(destination).with_name(Path(destination).stem + '.new.wav')
    sf.write(output, audio * gain, RATE, subtype='PCM_24')
    output.replace(destination)
    report.update(gain=gain, output_seconds=len(audio) / RATE)
    return report


def stretch(audio, ratio, ffmpeg, work):
    """Tempo times `ratio` with the pitch unchanged (Rubber Band, else atempo)."""
    import soundfile as sf
    import audio_adjust
    if abs(ratio - 1) < 1e-3:
        return audio, 'none'
    rubberband = audio_adjust.has_rubberband(ffmpeg)
    text = (f'rubberband=tempo={ratio:.6f}:pitchq=quality:channels=together' if rubberband else
            ','.join(f'atempo={step:.6f}' for step in audio_adjust._tempo_steps(ratio)))
    source, output = Path(work) / 'mix-source.wav', Path(work) / 'mix-stretched.wav'
    sf.write(source, audio, RATE, subtype='FLOAT')
    try:
        subprocess.run([str(ffmpeg), '-nostdin', '-y', '-v', 'error', '-i', str(source), '-af', text, '-ac', '2', '-ar', str(RATE),
                        '-c:a', 'pcm_f32le', str(output)], check=True, creationflags=FLAGS, timeout=1800)
        result, _ = sf.read(output, always_2d=True, dtype='float32')
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
    return result, 'rubberband' if rubberband else 'atempo'


def split_bass(audio, rate=RATE, crossover=150.):
    from scipy.signal import butter, sosfiltfilt
    low = sosfiltfilt(butter(4, crossover, btype='lowpass', fs=rate, output='sos'), audio, axis=0)
    return low, audio - low


def blend(outgoing, incoming):
    """Equal-power crossfade of the highs; the bass hands over at the midpoint (bass swap)."""
    import numpy as np
    count = min(len(outgoing), len(incoming))
    t = np.linspace(0, 1, count)[:, None]
    low_out, high_out = split_bass(outgoing[:count])
    low_in, high_in = split_bass(incoming[:count])
    swap = np.clip((t - .45) / .1, 0, 1)
    return high_out * np.cos(t * np.pi / 2) + high_in * np.sin(t * np.pi / 2) + low_out * (1 - swap) + low_in * swap


def limit(audio, threshold=.9):
    """Soft knee above the threshold so summed transitions never clip; quieter samples are untouched."""
    import numpy as np
    over = np.abs(audio) > threshold
    room = .999 - threshold
    audio[over] = np.sign(audio[over]) * (threshold + room * np.tanh((np.abs(audio[over]) - threshold) / room))
    return audio


def render_mix(sources, destination, order, transition_beats, ffmpeg, progress=lambda stage, message: None):
    """One continuous mix of the sources (dicts with 'path' and 'title'); returns the report."""
    import numpy as np
    import soundfile as sf
    songs = []
    for index, source in enumerate(sources):
        progress('analyzing', f'正在分析第 {index + 1} / {len(sources)} 首的速度与调性')
        audio = read(source['path'])
        tempo, _ = beats(audio)
        _, _, camelot = key_of(audio)
        start, end = music_span(audio)
        songs.append({'title': source.get('title', ''), 'tempo': tempo, 'camelot': camelot,
                      'rms': float(np.sqrt(np.mean(audio[start:end] ** 2))) or 1e-3})
    target, folded = target_tempo([s['tempo'] for s in songs])
    # Half- and double-time readings sort by the tempo they will play at.
    sequence = auto_order([{**s, 'tempo': t} for s, t in zip(songs, folded)]) if order == 'auto' else list(range(len(songs)))
    loudness = float(np.median([s['rms'] for s in songs]))
    work = Path(destination).parent
    temporary = Path(destination).with_name(Path(destination).stem + '.new.wav')
    report = {'order': [songs[i]['title'] for i in sequence], 'order_mode': order, 'target_tempo': round(target, 2),
              'transition_beats': transition_beats, 'songs': [], 'transitions': []}
    written = 0
    tail = None
    with sf.SoundFile(temporary, 'w', RATE, 2, subtype='PCM_24') as stream:
        for position, index in enumerate(sequence):
            song = songs[index]
            progress('mixing', f'正在混音第 {position + 1} / {len(sequence)} 首：{song["title"]}')
            ratio = target / folded[index]
            matched = MATCH_RANGE[0] <= ratio <= MATCH_RANGE[1]
            audio, method = stretch(read(sources[index]['path']), ratio if matched else 1., ffmpeg, work)
            audio *= float(np.clip(loudness / song['rms'], .25, 4.))
            _, times = beats(audio)
            start, end = music_span(audio)
            length = int(transition_beats * 60 / target * RATE)
            # Very short songs get shorter blends, so every song is still heard on its own.
            length = max(int(RATE), min(length, (end - start) // 3))
            entry = {'title': song['title'], 'tempo': round(song['tempo'], 2), 'camelot': song['camelot'],
                     'stretch': round(ratio if matched else 1., 4), 'stretch_method': method, 'beat_matched': matched}
            beat_samples = [int(t * RATE) for t in times]
            come_in = 0
            if tail is not None:
                come_in = next((b for b in beat_samples if b >= start - int(.05 * RATE)), start)
                piece = blend(tail, audio[come_in:come_in + len(tail)])
                stream.write(limit(np.asarray(piece, dtype=np.float32)))
                report['transitions'].append({'at_seconds': round(written / RATE, 3), 'seconds': round(len(piece) / RATE, 3)})
                written += len(piece)
                come_in += len(piece)
            leave = len(audio)
            if position < len(sequence) - 1:
                latest = end - length
                leave = max([b for b in beat_samples if come_in + RATE <= b <= latest] or [max(come_in + RATE, latest)])
                tail = audio[leave:leave + length]
            body = audio[come_in:leave]
            stream.write(limit(np.asarray(body, dtype=np.float32)))
            written += len(body)
            entry['plays_seconds'] = round(len(body) / RATE, 3)
            report['songs'].append(entry)
    temporary.replace(destination)
    report['output_seconds'] = written / RATE
    return report
