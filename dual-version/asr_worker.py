import json, sys, time
from pathlib import Path

def embedded(path):
    import mutagen
    audio=mutagen.File(path)
    tags=getattr(audio,'tags',None) or {}
    for key,value in tags.items():
        if 'lyric' in str(key).lower() or any(part.startswith(('USLT','SYLT')) for part in str(key).upper().split(':')) or key=='©lyr':
            text=getattr(value,'text',value)
            if isinstance(text,list):text='\n'.join(str(v[0] if isinstance(v,tuple) else v) for v in text)
            return str(text)[:20000]
    return ''

if sys.argv[1]=='--embedded':
    print(json.dumps({'text':embedded(sys.argv[2])},ensure_ascii=True));sys.exit()

from faster_whisper import WhisperModel
started=time.time()
model=WhisperModel(sys.argv[3],device='cpu',compute_type='int8',cpu_threads=8,local_files_only=True)
segments,info=model.transcribe(sys.argv[1],beam_size=5,condition_on_previous_text=False,vad_filter=False,
    language_detection_segments=5,language_detection_threshold=.9,hallucination_silence_threshold=2)
lines=[];timestamps=[]
for segment in segments:
    text=segment.text.strip()
    if text and segment.no_speech_prob<.6 and segment.avg_logprob>-1.2 and segment.compression_ratio<2.4:
        lines.append(text);timestamps.append({'start':segment.start,'end':segment.end,'text':text})
result={'text':'\n'.join(lines)[:20000],'segments':timestamps,'source':'whisper-large-v3-turbo',
    'language':info.language,'seconds':round(time.time()-started,2),
    'notice':'本机自动识别歌词，演唱内容可能识别有误。' if lines else '未识别到可靠歌词，已按原曲旋律继续。'}
Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
