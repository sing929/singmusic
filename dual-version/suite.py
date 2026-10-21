"""An orchestral suite from three finished versions of one song: a soft, a fast and a grand part.

All three follow the song's reference melody at the same length, so the suite plays the soft part
first, switches to the fast part and then to the grand part at the quietest moment near each third
of the song. Before each switch the next part's beats are lined up with the current part's (at most
half a second); the switch is a two-second equal-power crossfade. Nothing is regenerated.
"""
from pathlib import Path

RATE = 48000
FADE = 2.
# Part of the song searched around each third for a quiet switch point.
SEARCH = .12
MAX_LAG = .5


def frame_rms(audio, rate=RATE, frame=.05):
    import numpy as np
    hop = int(frame * rate)
    frames = len(audio) // hop
    mono = audio[:frames * hop].mean(axis=1) if audio.ndim == 2 else audio[:frames * hop]
    return np.sqrt((mono.reshape(frames, hop) ** 2).mean(axis=1)), hop


def quiet_point(parts, target, rate=RATE):
    """The sample near `target` where the three parts together are quietest."""
    import numpy as np
    levels = [frame_rms(p, rate) for p in parts]
    hop = levels[0][1]
    combined = sum(level[:min(len(l) for l, _ in levels)] for level, _ in levels)
    span = int(SEARCH * len(parts[0]) / hop)
    centre = int(target / hop)
    low, high = max(1, centre - span), min(len(combined) - 1, centre + span)
    if high <= low:
        return int(target)
    return (low + int(np.argmin(combined[low:high]))) * hop


def lag(current, following, at, rate=RATE, window=4.):
    """How many samples later than `current` the beats of `following` fall around `at`."""
    import librosa
    import numpy as np
    a, b = max(0, int(at - window * rate)), min(len(current), int(at + window * rate))
    sr = 22050
    def envelope(audio):
        mono = np.ascontiguousarray(audio[a:b].mean(axis=1), dtype=np.float32)
        onset = librosa.onset.onset_strength(y=librosa.resample(mono, orig_sr=rate, target_sr=sr), sr=sr, hop_length=128)
        return (onset - onset.mean()) / (onset.std() or 1.)
    x, y = envelope(current), envelope(following)
    limit = int(MAX_LAG * sr / 128)
    lags = range(-limit, limit + 1)
    scores = [float(np.mean(x[max(0, -k):len(x) - max(0, k)] * y[max(0, k):len(y) - max(0, -k)])) for k in lags]
    # Beats repeat, so a shift by whole beats matches almost as well: among the near-best peaks,
    # the smallest shift is the real one.
    top = max(scores)
    peaks = [i for i in range(len(scores)) if scores[i] >= top - .15 * abs(top)
             and scores[i] >= scores[max(0, i - 1)] and scores[i] >= scores[min(len(scores) - 1, i + 1)]]
    best = min(peaks or [int(np.argmax(scores))], key=lambda i: abs(lags[i]))
    return int(round(lags[best] * 128 * rate / sr))


def shift(audio, samples):
    """Move audio earlier by `samples` (later when negative), keeping its length."""
    import numpy as np
    if samples > 0:
        return np.vstack((audio[samples:], np.zeros((samples, audio.shape[1]), dtype=audio.dtype)))
    if samples < 0:
        return np.vstack((np.zeros((-samples, audio.shape[1]), dtype=audio.dtype), audio[:samples]))
    return audio


def render(paths, destination, progress=lambda stage, message: None):
    """Write the suite to `destination`; returns the report."""
    import numpy as np
    import soundfile as sf
    from dj_tools import read
    progress('analyzing', '正在读取三个管弦版本')
    parts = [read(path) for path in paths]
    length = max(len(p) for p in parts)
    parts = [np.vstack((p, np.zeros((length - len(p), 2), dtype=p.dtype))) for p in parts]
    progress('analyzing', '正在寻找安静的转换点')
    first = quiet_point(parts, length / 3)
    second = quiet_point(parts, 2 * length / 3)
    fade = int(FADE * RATE)
    first = min(max(first, fade), length - 3 * fade)
    second = min(max(second, first + 2 * fade), length - fade)
    lags = []
    for index, at in ((1, first), (2, second)):
        offset = lag(parts[index - 1], parts[index], at)
        parts[index] = shift(parts[index], offset)
        lags.append(round(offset / RATE * 1000, 1))
    progress('mixing', '正在衔接三个乐章')
    t = np.linspace(0, 1, 2 * fade)[:, None]
    result = parts[0].copy()
    for index, at in ((1, first), (2, second)):
        a, b = at - fade, at + fade
        result[a:b] = result[a:b] * np.cos(t * np.pi / 2) + parts[index][a:b] * np.sin(t * np.pi / 2)
        result[b:] = parts[index][b:]
    peak = float(np.max(np.abs(result))) if len(result) else 0.
    gain = min(1., .989 / peak) if peak > 0 else 1.
    output = Path(destination).with_name(Path(destination).stem + '.new.wav')
    sf.write(output, result * gain, RATE, subtype='PCM_24')
    output.replace(destination)
    return {'switch_seconds': [round(first / RATE, 3), round(second / RATE, 3)], 'lags_ms': lags,
            'crossfade_seconds': 2 * FADE, 'gain': gain, 'output_seconds': length / RATE}
