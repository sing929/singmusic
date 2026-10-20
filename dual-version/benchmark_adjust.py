"""Local-only key/tempo/reverb verification: one real YuE2 generation and one derived work."""
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


def generation_run(directory,source,melody):
    """Pre-generation key/tempo on the reference, YuE2 at the new length, reverb on the result."""
    import audio_adjust
    from generation_controls import VERSION,CONTROL_VERSION,build_caption,output_name
    adjust=audio_adjust.validate({'semitones':-2,'tempo':90,'reverb':30})
    duration=30.;target=audio_adjust.output_seconds(duration,adjust)
    name=output_name('Lo-fi','Local Adjust ('+audio_adjust.label(adjust)+')')
    config={'engine':'yue2','source':str(source),'start':0,'source_duration':duration,'duration':target,'adjust':adjust,
        'seed':20261016,'mode':'remix','instrumental':True,'profile':'fast','format':'both','auto_lyrics':False,'lyrics':'',
        'app_version':VERSION,'controls_version':CONTROL_VERSION,'gender':'auto','tone':'natural','style':'Lo-fi',
        'caption':build_caption('Lo-fi'),'change_strength':75,'melody_mode':melody,'output_name':name}
    code,elapsed=run_engine(directory,config)
    item={'run':'generation with Key -2, Tempo 90%, Reverb 30','melody_mode':melody,'exit_code':code,'processing_seconds':elapsed,'passed':False}
    if code:return item,None
    def read(name):return json.loads((directory/name).read_text(encoding='utf-8'))
    result=read('result.json');report=read('adjust.json');request=read('yue2-request.json')
    item.update(audio_checks(directory/result['files']['result'],target))
    item.update(target_seconds=target,shift_method=report['source']['method'],
        source_shifted=abs(report['source']['output_seconds']-target)<1e-6 and report['source']['semitones']==-2,
        yue2_duration_matches=abs(request['duration']-target)<1e-6,reverb_on_result=report.get('result',{}).get('reverb')==30,
        named=result['files']['result']==name+'.wav' and result['files']['mp3']==name+'.mp3' and (directory/result['files']['mp3']).is_file())
    item['passed']=all(item[k] for k in ('audio_ok','source_shifted','yue2_duration_matches','reverb_on_result','named'))
    return item,directory/result['files']['result']


def derived_run(directory,parent):
    """Slowed + reverb applied to a finished work without regenerating it."""
    import audio_adjust
    import soundfile as sf
    adjust=audio_adjust.validate({'speed_mode':'linked','tempo':80,'reverb':40})
    seconds=sf.info(parent).duration;target=audio_adjust.output_seconds(seconds,adjust)
    before=hashlib.sha256(Path(parent).read_bytes()).hexdigest()
    name='Lo-fi Local Adjust ('+audio_adjust.label(adjust)+')'
    config={'kind':'adjust','engine':'effects','source':str(parent),'source_duration':seconds,'duration':target,'adjust':adjust,
        'adjust_history':[adjust],'format':'both','mode':'remix','instrumental':True,'style':'Lo-fi','output_name':name}
    code,elapsed=run_engine(directory,config)
    item={'run':'derived work with Slowed 80%, Reverb 40','exit_code':code,'processing_seconds':elapsed,'passed':False}
    if code:return item
    result=json.loads((directory/'result.json').read_text(encoding='utf-8'))
    item.update(audio_checks(directory/result['files']['result'],target))
    item.update(target_seconds=target,original_unchanged=hashlib.sha256(Path(parent).read_bytes()).hexdigest()==before,
        named=result['files']['result']==name+'.wav' and (directory/result['files']['mp3']).is_file())
    item['passed']=all(item[k] for k in ('audio_ok','original_unchanged','named'))
    return item


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path,help='Optional existing local instrumental WAV; the first 30 seconds are used')
    parser.add_argument('--validation',type=Path,help='Record the outcome in this VALIDATION-v<version>.json')
    args=parser.parse_args();args.output=args.output.resolve();args.runtime=args.runtime.resolve()
    args.output.mkdir(parents=True,exist_ok=True)
    os.environ['SING_LOCAL_TEST']=str(args.runtime)
    os.environ['SING_YUE2_HOME']=str(args.runtime/'yue2')
    import numpy as np
    import soundfile as sf
    import audio_adjust
    from generation_controls import VERSION
    sr=48000
    if args.source:
        source=args.source.resolve()
    else:
        audio=np.zeros(sr*30)
        for index in range(60):
            notes=(60,64,67,72,69,65,67,62)
            frequency=440*2**((notes[index%8]-69)/12);t=np.arange(sr//2)/sr
            envelope=np.minimum(t/.015,1)*np.exp(-t*4)*np.minimum((.5-t)/.03,1)
            audio[index*sr//2:(index+1)*sr//2]+=(np.sin(2*np.pi*frequency*t)+.3*np.sin(4*np.pi*frequency*t))*.16*envelope
        source=args.output/'synthetic-reference.wav';sf.write(source,np.column_stack((audio,audio)),sr)
    # The synthetic melody can trip the SheetSage beat grid, so it uses free recomposition.
    melody='reference' if args.source else 'free'
    report={'version':VERSION,'real_local_inference':True,'subjective_listening_verified':False,
        'ffmpeg_rubberband':audio_adjust.has_rubberband(args.runtime/'bin/ffmpeg.exe'),
        'reference':'existing local instrumental sample' if args.source else 'synthetic instrumental melody','runs':[],'passed':False}
    def save():(args.output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    first=args.output/('7'*32);first.mkdir(exist_ok=True)
    item,result=generation_run(first,source,melody);report['runs'].append(item);save();print(json.dumps(item,ensure_ascii=False),flush=True)
    if item['passed']:
        second=args.output/('8'*32);second.mkdir(exist_ok=True)
        item=derived_run(second,result);report['runs'].append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
    report['passed']=len(report['runs'])==2 and all(r['passed'] for r in report['runs']);save()
    if args.validation:
        validation=json.loads(args.validation.read_text(encoding='utf-8'))
        validation.update(real_local_inference=True,real_inference_runs=report['runs'],ffmpeg_rubberband=report['ffmpeg_rubberband'],
            real_inference_reference=report['reference'],real_inference_passed=report['passed'],
            passed=bool(validation.get('dev_checks_passed')) and report['passed'],
            pending=[p for p in validation.get('pending',[]) if 'benchmark_adjust' not in p] if report['passed'] else validation.get('pending',[]))
        args.validation.write_text(json.dumps(validation,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
