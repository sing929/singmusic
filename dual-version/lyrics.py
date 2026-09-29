"""Optional offline lyrics, isolated from the music engine's dependencies."""
import json, os, subprocess, sys, time
from pathlib import Path

def recognize(directory, config, test):
    from engine import write_json
    settings=json.loads((Path(__file__).parent/'asr-paths.json').read_text(encoding='utf-8'))
    destination=directory/'lyrics.json'
    if destination.exists():return
    original=directory/'original-stems/stems.json'
    source=json.loads(original.read_text(encoding='utf-8'))['vocals'] if original.exists() else str(directory/'source.wav')
    try:
        subprocess.run([settings['python'],str(Path(__file__).parent/'asr_worker.py'),source,str(destination),settings['model']],
            check=True,timeout=max(3600,config['duration']*15),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
            env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1'})
    except Exception as error:
        write_json(destination,{'text':'','source':'unavailable','notice':'自动歌词识别未完成，已继续使用原曲旋律。','detail':str(error)})

def embedded(path):
    settings=json.loads((Path(__file__).parent/'asr-paths.json').read_text(encoding='utf-8'))
    try:
        result=subprocess.run([settings['python'],str(Path(__file__).parent/'asr_worker.py'),'--embedded',str(path)],
            capture_output=True,text=True,encoding='utf-8',check=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return json.loads(result.stdout).get('text','')[:20000]
    except Exception:return ''
