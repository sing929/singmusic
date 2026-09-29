"""Local-only end-to-end instrumental verification with synthetic reference audio."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path,help='Optional existing local instrumental WAV')
    args=parser.parse_args();args.output=args.output.resolve();args.runtime=args.runtime.resolve()
    args.output.mkdir(parents=True,exist_ok=True)
    os.environ['SING_LOCAL_TEST']=str(args.runtime)
    os.environ['SING_YUE2_HOME']=str(args.runtime/'yue2')
    import numpy as np
    import soundfile as sf
    from generation_controls import VERSION,CONTROL_VERSION,build_caption,output_name
    duration=30;sr=48000
    audio=np.zeros(sr*duration)
    for index in range(60):
        notes=(60,64,67,72,69,65,67,62)
        frequency=440*2**((notes[index%8]-69)/12)
        t=np.arange(sr//2)/sr
        note=(np.sin(2*np.pi*frequency*t)+.3*np.sin(4*np.pi*frequency*t))*.16
        envelope=np.minimum(t/.015,1)*np.exp(-t*4)*np.minimum((.5-t)/.03,1)
        audio[index*sr//2:(index+1)*sr//2]+=note*envelope
    source=args.output/'synthetic-reference.wav'
    sf.write(source,np.column_stack((audio,audio)),sr)
    if args.source:
        source=args.source.resolve();duration=sf.info(source).duration
    report={'version':VERSION,'real_local_inference':True,'reference':'existing local instrumental sample' if args.source else 'synthetic instrumental melody',
        'subjective_listening_verified':False,'runs':[],'passed':False}
    first=None
    for index,(style,melody) in enumerate((('Lo-fi','reference'),('Jazz','reference'),('Phonk','free'))):
        identifier=str(index+1)*32;directory=args.output/identifier;directory.mkdir(exist_ok=True)
        config={'engine':'yue2','source':str(source.resolve()),'start':0,'duration':duration,'seed':20260929+index,
            'mode':'remix','instrumental':True,'profile':'fast','format':'both','auto_lyrics':False,'lyrics':'',
            'app_version':VERSION,'controls_version':CONTROL_VERSION,'gender':'auto','tone':'natural',
            'style':style,'caption':build_caption(style),'change_strength':75,'melody_mode':melody,
            'output_name':output_name(style,'Local Instrumental'),'pair_id':'local-instrumental-pair'}
        if index==1:config['reuse_from']=first
        if index==0:first=identifier
        (directory/'config.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
        started=time.perf_counter()
        with (directory/'pipeline.log').open('wb') as log:
            process=subprocess.run([sys.executable,str(Path(__file__).with_name('engine.py')),str(directory)],
                stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        item={'style':style,'melody_mode':melody,'exit_code':process.returncode,'processing_seconds':round(time.perf_counter()-started,2),'passed':False}
        if process.returncode==0:
            def read(name):return json.loads((directory/name).read_text(encoding='utf-8'))
            request=read('yue2-request.json');replay=read('yue2-replay.json');result=read('result.json')
            output,rate=sf.read(directory/result['files']['result'],always_2d=True)
            item.update(seconds=len(output)/rate,sample_rate=rate,channels=output.shape[1],
                finite=bool(np.isfinite(output).all()),rms=float(np.sqrt(np.mean(output**2))),
                instrumental_request=request['lyrics']=='[Instrumental]' and 'no singing or vocals' in request['style'],
                instrumental_replay=replay.get('lyrics')=='[Instrumental]' and 'no singing or vocals' in replay.get('style',''),
                skipped_lyrics=read('lyrics.json').get('source')=='instrumental',
                no_vocal_separation=not (directory/'original-stems').exists(),
                reference_correct=(request['cot']=='melody' and bool(request['abc'])) if melody=='reference' else request['cot']=='off' and request['abc']=='',
                named_wav=result['files']['result']==config['output_name']+'.wav',
                named_mp3=result['files']['mp3']==config['output_name']+'.mp3' and (directory/result['files']['mp3']).is_file(),
                reused_analysis=(directory/'reused-analysis.json').exists())
            item['passed']=all(item[k] for k in ('finite','instrumental_request','instrumental_replay','skipped_lyrics',
                'no_vocal_separation','reference_correct','named_wav','named_mp3')) and abs(item['seconds']-duration)<.2 and item['rms']>1e-5
        report['runs'].append(item);report['passed']=len(report['runs'])==3 and all(r['passed'] for r in report['runs'])
        (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(item),flush=True)
        if not item['passed']:return 1
    return 0


if __name__=='__main__':sys.exit(main())
