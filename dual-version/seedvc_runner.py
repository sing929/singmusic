"""Runs inside the separate Seed-VC environment (local-test/seed-vc/env), never in the app's Python.

Loads the pinned singing model once, converts every vocal phrase listed in the plan with the
same reference clip, and measures the result with the engine's own models: CAM++ speaker
similarity to the reference before / after, and RMVPE pitch error against the source melody.
`--self-test` converts a synthetic sung tone to check the GPU install (used by seedvc-install.ps1).
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


def options(plan, source=None, output=None):
    # Same fields as Seed-VC's inference.py command line; checkpoint=None loads the pinned
    # singing model from the local Hugging Face cache (HF_HUB_OFFLINE=1 is set by the caller).
    return argparse.Namespace(source=source, target=plan['reference'], output=output,
        diffusion_steps=int(plan['diffusion_steps']), length_adjust=1.0,
        inference_cfg_rate=float(plan['inference_cfg_rate']), f0_condition=True, auto_f0_adjust=False,
        semi_tone_shift=int(plan['semi_tone_shift']), checkpoint=None, config=None, fp16=bool(plan['fp16']))


def gpu_report():
    torch = sys.modules.get('torch')
    if torch is None or not torch.cuda.is_available():
        return {}
    return {'torch': torch.__version__, 'cuda': torch.version.cuda, 'gpu': torch.cuda.get_device_name(0),
            'capability': list(torch.cuda.get_device_capability(0)),
            'peak_vram_mib': round(torch.cuda.max_memory_allocated() / 2 ** 20, 1)}


def measure(models, plan, device, limit=60.):
    """Objective checks only; they do not replace listening."""
    import librosa
    import numpy as np
    import torch
    import torchaudio
    campplus, f0_fn = models[4], models[2]
    sources, outputs, total = [], [], 0.
    for segment in plan['segments']:
        source = librosa.load(segment['source'], sr=16000)[0]
        output = librosa.util.fix_length(librosa.load(segment['output'], sr=16000)[0], size=len(source))
        sources.append(source)
        outputs.append(output)
        total += len(source) / 16000
        if total >= limit:
            break
    source, output = np.concatenate(sources), np.concatenate(outputs)
    reference = librosa.load(plan['reference'], sr=16000)[0]

    def embed(wave):
        tensor = torch.from_numpy(np.ascontiguousarray(wave[:16000 * 30], dtype=np.float32))[None].to(device)
        feat = torchaudio.compliance.kaldi.fbank(tensor, num_mel_bins=80, dither=0, sample_frequency=16000)
        feat = feat - feat.mean(dim=0, keepdim=True)
        with torch.no_grad():
            return campplus(feat.unsqueeze(0)).flatten().float()

    target = embed(reference)
    similarity = lambda wave: float(torch.nn.functional.cosine_similarity(embed(wave), target, dim=0))
    f_source = np.asarray(f0_fn(source, thred=0.03))
    f_output = np.asarray(f0_fn(output, thred=0.03))
    count = min(len(f_source), len(f_output))
    voiced = (f_source[:count] > 1) & (f_output[:count] > 1)
    cents = 1200 * np.log2(f_output[:count][voiced] / f_source[:count][voiced]) - 100 * int(plan['semi_tone_shift'])
    return {'measured_seconds': round(len(source) / 16000, 2),
            'speaker_similarity_before': similarity(source), 'speaker_similarity_after': similarity(output),
            'pitch_error_cents_median': float(np.median(np.abs(cents))) if voiced.any() else None,
            'voiced_frames_compared': int(voiced.sum())}


def self_test_plan(directory):
    """A 6 s sung-like tone (harmonics, vibrato, syllable envelope) as both source and reference."""
    import numpy as np
    import soundfile as sf
    rate = 44100
    t = np.arange(rate * 6) / rate
    f0 = 220 * 2 ** ((np.floor(t / 1.5) % 4) * 2 / 12) * (1 + .01 * np.sin(2 * np.pi * 5.5 * t))
    phase = 2 * np.pi * np.cumsum(f0) / rate
    wave = sum(np.sin(k * phase) / k for k in range(1, 12)) * (.55 + .45 * np.sin(np.pi * (t % 1.5) / 1.5))
    wave = (.3 * wave / np.max(np.abs(wave))).astype(np.float32)
    directory.mkdir(parents=True, exist_ok=True)
    sf.write(directory / 'source.wav', wave, rate)
    sf.write(directory / 'reference.wav', wave, rate)
    return {'reference': str(directory / 'reference.wav'), 'diffusion_steps': 10, 'inference_cfg_rate': .7,
            'semi_tone_shift': 0, 'fp16': True, 'allow_cpu': os.environ.get('SING_VOICE_ALLOW_CPU') == '1',
            'segments': [{'source': str(directory / 'source.wav'), 'output': str(directory / 'converted.wav')}]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seedvc', required=True, type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    report_path = args.report.resolve()
    plan = self_test_plan(report_path.parent / 'self-test') if args.self_test else \
        json.loads(args.plan.read_text(encoding='utf-8'))
    seedvc = args.seedvc.resolve()
    # The pinned checkpoints were saved for torch.load before PyTorch 2.6 made weights_only the
    # default; they come from the fixed Hugging Face files fetched by seedvc-download.ps1.
    os.environ.setdefault('TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD', '1')
    # inference.py resolves its checkpoints relative to its own folder.
    os.chdir(seedvc)
    sys.path.insert(0, str(seedvc))
    started = time.perf_counter()
    import inference
    device = str(inference.device)
    if device != 'cuda' and not plan.get('allow_cpu'):
        raise SystemExit('换声引擎未能使用 NVIDIA 显卡。RTX 50 系列需要 CUDA 12.8 版 PyTorch；请重新运行 seedvc-install.ps1。')
    models = inference.load_models(options(plan))
    # inference.main() reloads every model per call; keep the loaded ones for all phrases.
    inference.load_models = lambda _options: models
    loaded = time.perf_counter()
    total = len(plan['segments'])
    for index, segment in enumerate(plan['segments']):
        output = Path(segment['output'])
        work = output.with_name(output.stem + '.work')
        shutil.rmtree(work, ignore_errors=True)
        inference.main(options(plan, segment['source'], str(work)))
        produced = sorted(work.glob('*.wav'))
        if len(produced) != 1:
            raise RuntimeError('Seed-VC did not write exactly one converted file for ' + segment['source'])
        produced[0].replace(output)
        shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({'event': 'phrase', 'done': index + 1, 'total': total}), flush=True)
    converted = time.perf_counter()
    report = {'device': device, 'phrases': total, 'load_seconds': round(loaded - started, 2),
              'convert_seconds': round(converted - loaded, 2), **gpu_report()}
    try:
        report['metrics'] = measure(models, plan, inference.device)
    except Exception as error:  # metrics are informative; a failure must not discard the conversion
        report['metrics'] = {'error': f'{type(error).__name__}: {error}'}
    if args.self_test:
        import numpy as np
        import soundfile as sf
        audio = sf.read(plan['segments'][0]['output'])[0]
        report['self_test_passed'] = bool(len(audio) and np.isfinite(audio).all() and np.sqrt(np.mean(audio ** 2)) > 1e-4)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'event': 'done', **{k: report[k] for k in ('device', 'load_seconds', 'convert_seconds')}}), flush=True)
    return 0 if report.get('self_test_passed', True) else 1


if __name__ == '__main__':
    sys.exit(main())
