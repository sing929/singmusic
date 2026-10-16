"""Local-only voice swap verification on the app PC: real YuE2 generation, UVR and Seed-VC.

Uses an excerpt of a song with clear vocals that is already on this computer. Run 1 swaps the
generated voice for the original singer's; run 2 adds a key change, so the target timbre must
come from the un-shifted original. Objective checks only (GPU use, timbre moving toward the
original singer, melody kept, exact length, names); listening is still required.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def run_engine(directory, config):
    (directory/'config.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
    started=time.perf_counter()
    with (directory/'pipeline.log').open('wb') as log:
        process=subprocess.run([sys.executable,str(Path(__file__).with_name('engine.py')),str(directory)],
            stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return process.returncode,round(time.perf_counter()-started,2)


def audio_checks(path,seconds):
    import numpy as np
    import soundfile as sf
    audio,rate=sf.read(path,always_2d=True)
    checks={'seconds':len(audio)/rate,'sample_rate':rate,'channels':audio.shape[1],
        'finite':bool(np.isfinite(audio).all()),'rms':float(np.sqrt(np.mean(audio**2)))}
    checks['audio_ok']=checks['finite'] and rate==48000 and audio.shape[1]==2 and abs(checks['seconds']-seconds)<.2 and checks['rms']>1e-5
    return checks


def voice_run(directory,args,lyrics,adjust,label):
    import audio_adjust
    from generation_controls import VERSION,CONTROL_VERSION,build_caption,output_name
    adjust=audio_adjust.validate(adjust) if adjust else None
    voice={'target':'original','octave':'auto'}
    name=output_name('Lo-fi','Voice Check ('+', '.join(filter(None,['Original Voice',audio_adjust.label(adjust)]))+')')
    config={'engine':'yue2','source':str(args.source),'start':args.start,'source_duration':args.seconds,'duration':args.seconds,
        'adjust':adjust,'voice':voice,'seed':20261016,'mode':'remix','instrumental':False,'profile':args.profile,'format':'both',
        'auto_lyrics':not lyrics,'lyrics':lyrics,'app_version':VERSION,'controls_version':CONTROL_VERSION,'gender':'auto',
        'tone':'natural','style':'Lo-fi','caption':build_caption('Lo-fi'),'change_strength':75,'melody_mode':args.melody,'output_name':name}
    code,elapsed=run_engine(directory,config)
    item={'run':label,'melody_mode':args.melody,'profile':args.profile,'exit_code':code,'processing_seconds':elapsed,'passed':False}
    if code:
        error=directory/'error.json'
        if error.exists():item['error']=json.loads(error.read_text(encoding='utf-8')).get('error','')[-1500:]
        return item
    def read(name):return json.loads((directory/name).read_text(encoding='utf-8'))
    result,swap,reference=read('result.json'),read('voice-swap.json'),read('voice-reference.json')
    runner=swap.get('runner') or {}
    metrics=runner.get('metrics') or {}
    item.update(audio_checks(directory/result['files']['result'],args.seconds))
    generated=hashlib.sha256((directory/'generated.wav').read_bytes()).hexdigest()
    swapped=hashlib.sha256((directory/result['files']['result']).read_bytes()).hexdigest()
    before,after=metrics.get('speaker_similarity_before'),metrics.get('speaker_similarity_after')
    pitch=metrics.get('pitch_error_cents_median')
    item.update(phrases=swap.get('phrases'),octave_shift=swap.get('octave_shift'),diffusion_steps=swap.get('diffusion_steps'),
        reference_from=reference['source'],reference_seconds=reference['seconds'],voice_seconds=swap.get('seconds'),
        device=runner.get('device'),gpu=runner.get('gpu'),capability=runner.get('capability'),torch=runner.get('torch'),
        peak_vram_mib=runner.get('peak_vram_mib'),load_seconds=runner.get('load_seconds'),convert_seconds=runner.get('convert_seconds'),
        speaker_similarity_before=before,speaker_similarity_after=after,pitch_error_cents_median=pitch,metrics_error=metrics.get('error'))
    item.update(swapped=not swap.get('skipped') and (swap.get('phrases') or 0)>0 and swapped!=generated,
        on_gpu=runner.get('device')=='cuda',
        timbre_moved=isinstance(before,(int,float)) and isinstance(after,(int,float)) and after>before,
        melody_kept=isinstance(pitch,(int,float)) and pitch<=50,
        reference_unshifted=reference['source']==('reference-stems' if adjust else 'original-stems') and
            (directory/'source-unshifted.wav').exists()==bool(adjust),
        named=result['files']['result']==name+'.wav' and (directory/result['files']['mp3']).is_file())
    item['passed']=all(item[k] for k in ('audio_ok','swapped','on_gpu','timbre_moved','melody_kept','reference_unshifted','named'))
    return item


def record(path,name,report):
    """Write this benchmark into the release's validation file; passed needs every required benchmark."""
    validation=json.loads(path.read_text(encoding='utf-8'))
    benchmarks=validation.setdefault('benchmarks',{})
    benchmarks[name]={k:v for k,v in report.items() if k not in ('version','real_local_inference','subjective_listening_verified')}
    required=validation.get('required_benchmarks',[name])
    validation.update(real_local_inference=True,
        passed=bool(validation.get('dev_checks_passed')) and all(benchmarks.get(n,{}).get('passed') for n in required),
        pending=[p for p in validation.get('pending',[]) if f'benchmark_{name}' not in p] if report['passed'] else validation.get('pending',[]))
    path.write_text(json.dumps(validation,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='Folder outside Git for audio, logs and the report')
    parser.add_argument('--source',type=Path,required=True,help='A local song with clear vocals (WAV/MP3/FLAC)')
    parser.add_argument('--start',type=float,default=30.,help='Excerpt start in seconds (pick a sung part)')
    parser.add_argument('--seconds',type=float,default=30.)
    parser.add_argument('--lyrics-file',type=Path,help='Optional lyrics for the excerpt; otherwise Whisper recognizes them')
    parser.add_argument('--melody',choices=('free','reference'),default='free')
    parser.add_argument('--profile',choices=('fast','balanced','quality'),default='balanced')
    parser.add_argument('--skip-key-run',action='store_true',help='Only run the plain voice swap')
    parser.add_argument('--validation',type=Path,help='Record the outcome in this VALIDATION-v<version>.json')
    args=parser.parse_args();args.output=args.output.resolve();args.runtime=args.runtime.resolve();args.source=args.source.resolve()
    args.output.mkdir(parents=True,exist_ok=True)
    os.environ['SING_LOCAL_TEST']=str(args.runtime)
    os.environ['SING_YUE2_HOME']=str(args.runtime/'yue2')
    import voice_swap
    from generation_controls import VERSION
    if not voice_swap.available():
        print('Seed-VC is not installed under '+str(voice_swap.HOME)+'; run seedvc-install.ps1 first.',file=sys.stderr)
        return 2
    lyrics=args.lyrics_file.read_text(encoding='utf-8').strip() if args.lyrics_file else ''
    if not lyrics and not Path(__file__).with_name('asr-paths.json').is_file():
        print('Pass --lyrics-file, or copy asr-paths.json from the installed app folder next to this script for Whisper.',file=sys.stderr)
        return 2
    report={'version':VERSION,'real_local_inference':True,'subjective_listening_verified':False,
        'engine':voice_swap.install_info(),'reference':'local song excerpt with vocals','runs':[],'passed':False}
    def save():(args.output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    runs=[('voice swap to the original singer',None,'5'*32)]
    if not args.skip_key_run:runs.append(('voice swap with Key -2 (reference from the un-shifted original)',{'semitones':-2},'6'*32))
    for label,adjust,folder in runs:
        directory=args.output/folder;directory.mkdir(exist_ok=True)
        item=voice_run(directory,args,lyrics,adjust,label);report['runs'].append(item);save()
        print(json.dumps(item,ensure_ascii=False),flush=True)
        if not item['passed']:break
    report['passed']=len(report['runs'])==len(runs) and all(r['passed'] for r in report['runs'])
    report['recorded_at']=time.strftime('%Y-%m-%dT%H:%M:%S');save()
    if args.validation:record(args.validation,'voice',report)
    return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
