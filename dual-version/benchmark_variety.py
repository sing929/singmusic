"""Local-only v1.6.0 verification on the app PC with real YuE2: a style from each new group, the new
singers (the kid voice with its local step), an orchestral suite and the DJ tools.

Audio, requests, scores and logs stay in the output folder outside Git; only sanitized results are
recorded. Passing says the settings reach the engine and the audio is complete, not that it sounds good.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from benchmark_adjust import audio_checks, run_engine

STYLES = ('Vinahouse', 'Big Room', 'Trap', 'Cinematic Orchestra', '中国风', 'Rock', 'Reggaeton', 'Music Box')
SINGERS = ('kid', 'duet', 'choir', 'opera')
# Original placeholder words, so no song's lyrics are needed.
LYRICS = '[verse]\nMorning light across the town\nEvery window turning gold\n[chorus]\nSing it high and sing it low\nLet the little river flow'


def read(directory, name):
    return json.loads((directory / name).read_text(encoding='utf-8'))


def config_for(source, start, seconds, style, **changes):
    from generation_controls import VERSION, CONTROL_VERSION, build_caption, output_name
    config = {'engine': 'yue2', 'source': str(source), 'start': start, 'source_duration': seconds, 'duration': seconds, 'adjust': None,
              'seed': 20261021, 'mode': 'remix', 'instrumental': True, 'profile': 'fast', 'format': 'both', 'auto_lyrics': False,
              'lyrics': '', 'app_version': VERSION, 'controls_version': CONTROL_VERSION, 'gender': 'auto', 'tone': 'natural',
              'style': style, 'caption': build_caption(style), 'change_strength': 75, 'melody_mode': 'reference', 'voice': None,
              'kid': False, 'output_name': output_name(style, 'Variety Check'), **changes}
    return config


def generated(directory, config, run, seconds, extra=()):
    """Run one real generation and check its audio, name and request."""
    code, elapsed = run_engine(directory, config)
    item = {'run': run, 'exit_code': code, 'processing_seconds': elapsed, 'passed': False}
    if code:
        error = directory / 'error.json'
        if error.exists():
            item['error'] = read(directory, 'error.json').get('error', '')[-1500:]
        return item, None
    result, request = read(directory, 'result.json'), read(directory, 'yue2-request.json')
    item.update(audio_checks(directory / result['files']['result'], seconds))
    item.update(seed=request['seed'], cfg_scale=request['cfg_scale'], cot=request['cot'],
                score_sha256=hashlib.sha256(request['abc'].encode()).hexdigest() if request['abc'] else None,
                caption_in_request=request['style'].startswith(config['caption']),
                named=result['files']['result'] == config['output_name'] + '.wav' and (directory / result['files']['mp3']).is_file())
    item['passed'] = all(item[k] for k in ('audio_ok', 'caption_in_request', 'named', *extra))
    return item, directory / result['files']['result']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='Folder outside Git for audio, logs and the report')
    parser.add_argument('--source', type=Path, required=True, help='A local song or instrumental (WAV/MP3/FLAC) for the reference melody')
    parser.add_argument('--start', type=float, default=0., help='Excerpt start in seconds')
    parser.add_argument('--seconds', type=float, default=30.)
    parser.add_argument('--validation', type=Path, help='Record the outcome in this VALIDATION-v<version>.json')
    args = parser.parse_args()
    args.output, args.runtime, args.source = args.output.resolve(), args.runtime.resolve(), args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ['SING_LOCAL_TEST'] = str(args.runtime)
    os.environ['SING_YUE2_HOME'] = str(args.runtime / 'yue2')
    import audio_adjust
    from generation_controls import CATALOG, GENDERS, SUITE, VERSION, build_caption, output_name
    report = {'version': VERSION, 'real_local_inference': True, 'subjective_listening_verified': False,
              'ffmpeg_rubberband': audio_adjust.has_rubberband(args.runtime / 'bin/ffmpeg.exe'), 'runs': [], 'passed': False}

    def save(item):
        report['runs'].append(item)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps(item, ensure_ascii=False), flush=True)

    # One style from each new group, with the same source, seed, score and strength.
    results, score = {}, None
    for index, style in enumerate(STYLES):
        directory = args.output / f'style-{index}'
        directory.mkdir(exist_ok=True)
        if score:
            shutil.copy2(score, directory / 'melody.abc')
        item, path = generated(directory, config_for(args.source, args.start, args.seconds, style), 'style ' + style, args.seconds)
        item['group'] = next(g['id'] for g in CATALOG['groups'] if style in g['styles'])
        save(item)
        if path:
            results[style] = path
            score = score or directory / 'melody.abc'
    runs = report['runs']
    report['styles_consistent'] = len({(r.get('seed'), r.get('cfg_scale'), r.get('score_sha256')) for r in runs if r['passed']}) == 1
    # The new singers sing placeholder words in free recomposition; the kid voice adds its local step.
    for singer in SINGERS:
        directory = args.output / f'singer-{singer}'
        directory.mkdir(exist_ok=True)
        style = 'Classical Piano' if singer == 'opera' else 'Lo-fi'
        config = config_for(args.source, args.start, args.seconds, style, instrumental=False, lyrics=LYRICS, gender=singer,
                            melody_mode='free', kid=singer == 'kid',
                            output_name=output_name(style, 'Variety Check' + (' (Kid Voice)' if singer == 'kid' else '')))
        item, _ = generated(directory, config, 'singer ' + singer, args.seconds)
        if item['exit_code'] == 0:
            item['singer_in_request'] = GENDERS[singer] in read(directory, 'yue2-request.json')['style']
            if singer == 'kid':
                kid = read(directory, 'kid-voice.json')
                item.update(kid_skipped=kid.get('skipped'), kid_method=kid.get('method'), kid_octave=kid.get('octave_shift'),
                            kid_formant=kid.get('formant_semitones'), kid_seconds=kid.get('seconds'))
                item['passed'] = item['passed'] and kid.get('skipped') is False and (directory / 'generated.wav').is_file()
            item['passed'] = item['passed'] and item['singer_in_request']
        save(item)
    # Three suite parts on the shared score, then the suite made from them.
    parts = []
    for index, part in enumerate(SUITE):
        directory = args.output / f'suite-part-{index}'
        directory.mkdir(exist_ok=True)
        if score:
            shutil.copy2(score, directory / 'melody.abc')
        config = config_for(args.source, args.start, args.seconds, part['style'], caption=build_caption(part['style']) + ', ' + part['hint'],
                            output_name=output_name(part['style'], f'Variety Check ({part["label"]})'))
        item, path = generated(directory, config, 'suite part ' + part['label'], args.seconds)
        save(item)
        parts.append(path)
    if all(parts):
        directory = args.output / 'suite'
        directory.mkdir(exist_ok=True)
        config = {'kind': 'suite', 'engine': 'effects', 'sources': [{'path': str(p)} for p in parts], 'duration': args.seconds,
                  'mode': 'remix', 'format': 'both', 'output_name': output_name('Orchestral Suite', 'Variety Check')}
        code, elapsed = run_engine(directory, config)
        item = {'run': 'orchestral suite', 'exit_code': code, 'processing_seconds': elapsed, 'passed': False}
        if not code:
            suite = read(directory, 'suite.json')
            result = read(directory, 'result.json')
            item.update(audio_checks(directory / result['files']['result'], suite['output_seconds']))
            item.update(switch_seconds=suite['switch_seconds'], lags_ms=suite['lags_ms'],
                        switches_near_thirds=all(abs(s - args.seconds * k / 3) <= .13 * args.seconds for s, k in zip(suite['switch_seconds'], (1, 2))))
            item['passed'] = item['audio_ok'] and item['switches_near_thirds']
        save(item)
    # DJ tools on finished results: an edit with all three effects, and a two-song mix.
    if len(results) >= 2:
        first, second = list(results.values())[:2]
        for kind, config in (('dj', {'source': str(first), 'effects': ['buildup', 'filter', 'echo']}),
                             ('mix', {'sources': [{'path': str(first), 'title': 'first'}, {'path': str(second), 'title': 'second'}],
                                      'order': 'auto', 'transition_beats': 8})):
            directory = args.output / kind
            directory.mkdir(exist_ok=True)
            config = {'kind': kind, 'engine': 'effects', 'duration': args.seconds, 'mode': 'remix', 'format': 'both',
                      'output_name': output_name('DJ', 'Variety Check ' + kind), **config}
            code, elapsed = run_engine(directory, config)
            item = {'run': 'DJ ' + kind, 'exit_code': code, 'processing_seconds': elapsed, 'passed': False}
            if not code:
                made = read(directory, f'{kind}.json')
                item.update(audio_checks(directory / read(directory, 'result.json')['files']['result'], made['output_seconds']))
                item.update({k: made[k] for k in ('tempo', 'drop_seconds', 'echo_cut_seconds', 'target_tempo', 'order') if k in made})
                item['passed'] = item['audio_ok']
            save(item)
    expected = len(STYLES) + len(SINGERS) + len(SUITE) + 1 + 2
    report['passed'] = len(runs) == expected and all(r['passed'] for r in runs) and report['styles_consistent']
    (args.output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    if args.validation:
        # Shared with the other benchmarks: a release passes only when every required benchmark passed.
        from benchmark_voice import record
        report['recorded_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        record(args.validation, 'variety', report)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
