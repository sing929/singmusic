"""Real local inference validation. Keeps audio/lyrics out of Git reports."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

parser=argparse.ArgumentParser()
parser.add_argument('--runtime',type=Path,required=True)
parser.add_argument('--installed-app',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
root=Path(__file__).resolve().parent
os.environ['SING_LOCAL_TEST']=str(args.runtime)
os.environ['SING_YUE2_HOME']=str(args.runtime/'yue2')
import soundfile as sf
import numpy as np
from generation_controls import VERSION,build_caption

args.output.mkdir(parents=True,exist_ok=True)
report={'version':VERSION,'real_inference':True,'subjective_listening_verified':False,'runs':[]}
base={'engine':'yue2','start':0,'seed':20260929,'mode':'remix','profile':'fast','format':'both',
      'auto_lyrics':False,'app_version':VERSION,'controls_version':1,'gender':'female','tone':'husky',
      'style':'Jazz','caption':build_caption('Jazz'),'lyrics':'','melody_mode':'reference'}
tracks=[json.loads(p.read_text(encoding='utf-8')) for p in (args.installed_app/'data/tracks').glob('*/track.json')]
full=next(t for t in tracks if 160<float(t['duration'])<180)
specs=[('reference-low',args.runtime/'samples/fishin-30s.wav',30,{'change_strength':0}),
       ('reference-high',args.runtime/'samples/fishin-30s.wav',30,{'change_strength':100}),
       ('free-full',Path(full['source']),full['duration'],{'change_strength':100,'melody_mode':'free','style':'Remix',
           'caption':build_caption('Remix'),'tone':'powerful','lyrics':full.get('lyrics','')})]
for label,source,duration,overrides in specs:
    directory=args.output/label;directory.mkdir(exist_ok=True)
    config={**base,'source':str(source),'duration':duration,**overrides}
    if label=='reference-high':
        cached=args.output/'reference-low/melody.abc'
        if cached.exists():shutil.copy2(cached,directory/'melody.abc')
    if label=='free-full' and not config['lyrics']:
        # Reuse an already recognized lyric file for this exact source, locally.
        for path in (args.installed_app/'data/jobs').glob('*/config.json'):
            old=json.loads(path.read_text(encoding='utf-8'))
            if old.get('track_id')==full['id']:
                lyric=path.with_name('lyrics.json')
                config['lyrics']=old.get('lyrics','') or (json.loads(lyric.read_text(encoding='utf-8')).get('text','') if lyric.exists() else '')
                if config['lyrics']:break
    (directory/'config.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
    start=time.perf_counter()
    with (directory/'pipeline.log').open('wb') as log:
        result=subprocess.run([sys.executable,str(root/'engine.py'),str(directory)],env=os.environ.copy(),
            stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    item={'label':label,'exit_code':result.returncode,'seconds':round(time.perf_counter()-start,2),
          'requested_duration':duration,'strength':config['change_strength'],'melody_mode':config['melody_mode']}
    if result.returncode==0:
        audio,sr=sf.read(directory/'result.wav',always_2d=True)
        request=json.loads((directory/'yue2-request.json').read_text(encoding='utf-8'))
        replay=json.loads((directory/'yue2-replay.json').read_text(encoding='utf-8'))
        item.update(actual_duration=len(audio)/sr,sample_rate=sr,channels=audio.shape[1],finite=bool(np.isfinite(audio).all()),
            peak=float(np.abs(audio).max()),rms=float(np.sqrt(np.mean(audio**2))),
            sha256=hashlib.sha256((directory/'result.wav').read_bytes()).hexdigest(),
            cfg_scale=request['cfg_scale'],cot=request['cot'],abc_present=bool(request['abc']),replay_cfg=replay.get('cfg_scale'))
    else:
        item['error']=(directory/'error.json').read_text(encoding='utf-8') if (directory/'error.json').exists() else 'See local pipeline.log'
    report['runs'].append(item)
    report['passed']=len(report['runs'])==len(specs) and all(r['exit_code']==0 and r['finite'] and r['channels']==2 and abs(r['actual_duration']-r['requested_duration'])<.2 for r in report['runs'])
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(item,ensure_ascii=False),flush=True)
    if result.returncode:break
