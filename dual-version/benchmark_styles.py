"""Validate the new genre requests with local inference and sanitized results.

Same reference audio, seed, score, strength and synthesis profile for each genre.
Private requests, score, audio and logs stay in the ignored output directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    os.environ['SING_LOCAL_TEST']=str(args.runtime)
    os.environ['SING_YUE2_HOME']=str(args.runtime/'yue2')
    import numpy as np
    import soundfile as sf
    from generation_controls import VERSION, CONTROL_VERSION, build_caption
    source=args.runtime/'samples/fishin-30s.wav'
    duration=sf.info(source).duration
    report={'version':VERSION,'real_inference':True,'subjective_listening_verified':False,'runs':[],'passed':False}
    for style in ('Phonk','Hardstyle','Hardtekk'):
        directory=args.output/style.lower()
        directory.mkdir(exist_ok=True)
        if style!='Phonk':
            shutil.copy2(args.output/'phonk/melody.abc',directory/'melody.abc')
        config={'engine':'yue2','source':str(source),'start':0,'duration':duration,'seed':20260929,
            'mode':'remix','profile':'fast','format':'both','auto_lyrics':False,'lyrics':'',
            'app_version':VERSION,'controls_version':CONTROL_VERSION,'gender':'auto','tone':'natural',
            'style':style,'caption':build_caption(style),'change_strength':75,'melody_mode':'reference'}
        (directory/'config.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
        start=time.perf_counter()
        with (directory/'pipeline.log').open('wb') as log:
            result=subprocess.run([sys.executable,str(Path(__file__).with_name('engine.py')),str(directory)],
                env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        item={'style':style,'exit_code':result.returncode,'seconds':round(time.perf_counter()-start,2),'requested_duration':duration}
        if result.returncode==0:
            audio,sr=sf.read(directory/'result.wav',always_2d=True)
            request=json.loads((directory/'yue2-request.json').read_text(encoding='utf-8'))
            replay=json.loads((directory/'yue2-replay.json').read_text(encoding='utf-8'))
            item.update(actual_duration=len(audio)/sr,sample_rate=sr,channels=audio.shape[1],
                finite=bool(np.isfinite(audio).all()),rms=float(np.sqrt(np.mean(audio**2))),
                preset_in_request=build_caption(style) in request['style'],
                preset_in_replay=build_caption(style) in replay.get('style',''),
                seed=request['seed'],cfg_scale=request['cfg_scale'],cot=request['cot'],
                score_sha256=hashlib.sha256(request['abc'].encode()).hexdigest(),
                mp3_exists=(directory/'result.mp3').is_file())
            item['passed']=all((item['finite'],item['rms']>0,item['channels']==2,item['sample_rate']==48000,
                abs(item['actual_duration']-duration)<.2,item['preset_in_request'],item['preset_in_replay'],item['mp3_exists']))
        else:item['passed']=False
        report['runs'].append(item)
        runs=report['runs']
        report['passed']=len(runs)==3 and all(r['passed'] for r in runs) and all(
            len({r[key] for r in runs})==1 for key in ('seed','cfg_scale','score_sha256'))
        (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(item),flush=True)
        if not item['passed']:break
    return 0 if report['passed'] else 1

if __name__=='__main__':sys.exit(main())
