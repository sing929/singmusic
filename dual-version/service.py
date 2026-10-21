"""Single-user offline music studio; loopback HTTP and serialized local jobs."""
import json, math, os, re, secrets, subprocess, threading, time, traceback, uuid, wave
from pathlib import Path
from urllib.parse import urlsplit, unquote, quote
from http.server import ThreadingHTTPServer
import server
import audio_adjust
import voice_swap
import kid_voice
import dj_tools
from generation_controls import RELEASE, VERSION, CONTROL_VERSION, DEFAULT_STRENGTH, LEGACY_INSTRUCTIONS, validate_controls, text_field, build_caption, effective_controls, output_name
from generation_controls import MAX_VERSIONS, LEGACY_VERSIONS, LETTERS, version_count, text_list, default_styles
from generation_controls import SUITE, SMART_FIELDS, apply_smart_defaults, validate_suite

ROOT=Path(os.environ.get('SING_APP_ROOT',str(server.ROOT)))
TEST=Path(os.environ.get('SING_LOCAL_TEST',str(ROOT.parent/'local-test')))
DATA=ROOT/'data'
JOBS=DATA/'jobs'
TRACKS=DATA/'tracks'
TOKEN=secrets.token_urlsafe(32)
LOCK=threading.RLock()
WAKE=threading.Event()
# Key/tempo jobs on finished works are CPU-only and short; they never wait behind YuE2.
EFFECTS_WAKE=threading.Event()
ACTIVE={}
RUNNING=None
PYTHON=TEST/'ace-env/Scripts/python.exe'
FLAGS=getattr(subprocess,'CREATE_NO_WINDOW',0)
# Up to 20 versions per song, each with 3 speed versions, fill a queue quickly.
QUEUE_LIMIT=2000
# Works made on this computer from finished works; they share the effects queue and never use YuE2.
EFFECT_KINDS=('adjust','dj','mix','suite')
# Singers whose character is the point; swapping them to the original singer's voice would undo it.
OWN_VOICE_SINGERS=('kid','duet','choir')
SAMPLE={'id':'sample','title':'Let’s Go Fishin’','duration':30,'source':str(TEST/'samples/fishin-30s.wav')}

def read(path,default=None):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return default

def save(path,value):
    from engine import write_json
    write_json(path,value)

def track(identifier):
    if identifier=='sample':return SAMPLE.copy()
    if not re.fullmatch('[0-9a-f]{32}',str(identifier)):raise ValueError('找不到这首歌曲。')
    value=read(TRACKS/identifier/'track.json')
    if not value:raise ValueError('找不到这首歌曲。')
    return value

def stored_voice(value,inherit=False):
    """A saved voice swap setting, or the default / None when the stored shape is no longer valid."""
    try:return voice_swap.validate(value,inherit=inherit)
    except ValueError:return None if inherit else voice_swap.validate({})

def versioned_track(t):
    """Per-song version styles ('' follows the shared style); style / style_b stay their A and B entries.
    Ten styles saved before v1.6.0 gain K–T following the shared styles."""
    styles=t.get('styles')
    if isinstance(styles,list) and len(styles)==LEGACY_VERSIONS:styles=styles+['']*(MAX_VERSIONS-LEGACY_VERSIONS)
    if not (isinstance(styles,list) and len(styles)==MAX_VERSIONS):styles=['']*MAX_VERSIONS
    return {**t,'styles':[t.get('style',''),t.get('style_b',''),*styles[2:]],'version_count':t.get('version_count'),'voice_swap':stored_voice(t.get('voice_swap'),True),
        'speed_variants':t.get('speed_variants') if isinstance(t.get('speed_variants'),bool) else None,
        'suite':t.get('suite') if isinstance(t.get('suite'),bool) else None}

def public_track(t):return {k:v for k,v in versioned_track(t).items() if k!='source'}

def wake_workers():WAKE.set();EFFECTS_WAKE.set()

def adjust_history(meta):
    return meta.get('adjust_history') or ([meta['adjust']] if meta.get('adjust') else [])

def job_output_name(meta):
    labels=([voice_swap.LABEL] if meta.get('voice') else [])+([kid_voice.LABEL] if meta.get('kid') else [])+([meta['suite_label']] if meta.get('suite_label') else [])
    changes=', '.join(filter(None,labels+[audio_adjust.label(a) for a in adjust_history(meta)]+[dj_tools.label(e) for e in meta.get('dj_history',[])]))
    return output_name(meta.get('style',''),meta['title']+(f' ({changes})' if changes else ''))

def result_file(directory,result,kind,suffixes=('.wav','.mp3')):
    """Only a declared audio file directly inside this job may be served or reused."""
    filename=result.get('files',{}).get(kind)
    if (not isinstance(filename,str) or '/' in filename or '\\' in filename or ':' in filename or
        Path(filename).suffix.lower() not in suffixes or
        (directory/filename).resolve().parent!=directory.resolve()):return None
    return directory/filename

def public_job(directory):
    value=read(directory/'job.json')
    if not value:return None
    value['output_name']=job_output_name(value)
    value['timing_source']='active_time' if 'processing_seconds' in value else 'legacy_wall_time'
    value['processing_seconds']=processing_seconds(value)
    value['progress']=read(directory/'progress.json',{})
    lyrics=read(directory/'lyrics.json',{})
    if lyrics:value['lyrics_info']=lyrics
    if value['state']=='completed':
        result=read(directory/'result.json',{})
        value['urls']={k:f'/results/{value["id"]}/{k}' for k in result.get('files',{})}
        value['checks']=result.get('checks',{})
        value['notice']=result.get('notice','')
        if value.get('voice') and value.get('kind')!='adjust':value['voice_info']=voice_swap.summary(read(directory/'voice-swap.json'))
        if value.get('kid') and not value.get('kind'):value['kid_info']=kid_voice.summary(read(directory/'kid-voice.json'))
        if value.get('kind') in ('dj','mix','suite'):value['effect_info']=read(directory/f'{value["kind"]}.json',{})
    return value

def jobs():return sorted(filter(None,(public_job(p) for p in JOBS.iterdir() if p.is_dir())),key=lambda x:x.get('queue_order',x['created_at']),reverse=True)

def settings():
    value = {**{'profile':'quality','style_b':'Jazz','gender':'auto','tone':'natural',
        'style':'R&B','mode':'remix','format':'both','instructions':'','paused':False,
        'instructions_a':'','instructions_b':'','change_strength':DEFAULT_STRENGTH,'melody_mode':'reference',
        'adjust':dict(audio_adjust.DEFAULT),'version_count':2,'voice_swap':voice_swap.validate({}),'speed_variants':True,
        'smart_defaults':True,'suite':validate_suite({})},
        **read(DATA/'settings.json',{})}
    # Only migrate the exact old built-in text, never arbitrary user writing.
    if not value.get('controls_version') and value['instructions'] == LEGACY_INSTRUCTIONS:
        value['instructions'] = ''
    value['controls_version'] = CONTROL_VERSION
    value['voice_swap'] = stored_voice(value['voice_swap'])
    if not isinstance(value.get('speed_variants'),bool):value['speed_variants']=True
    if not isinstance(value.get('smart_defaults'),bool):value['smart_defaults']=True
    try:value['suite']=validate_suite(value.get('suite'))
    except ValueError:value['suite']=validate_suite({})
    styles=value.get('styles')
    # Ten versions saved before v1.6.0 keep their styles; K–T start on unused presets.
    if isinstance(styles,list) and len(styles)==LEGACY_VERSIONS and all(isinstance(s,str) for s in styles):styles=default_styles(*styles)
    if not (isinstance(styles,list) and len(styles)==MAX_VERSIONS):styles=default_styles(str(value['style']),str(value['style_b']))
    notes=value.get('version_instructions')
    if isinstance(notes,list) and len(notes)==LEGACY_VERSIONS:notes=notes+['']*(MAX_VERSIONS-LEGACY_VERSIONS)
    if not (isinstance(notes,list) and len(notes)==MAX_VERSIONS):notes=['']*MAX_VERSIONS
    value['styles']=[value['style'],value['style_b'],*styles[2:]]
    value['version_instructions']=[value['instructions_a'],value['instructions_b'],*notes[2:]]
    return value

def prepare_job(payload):
    t=track(payload.get('track_id'))
    from yue2_engine import available
    if not available():raise ValueError('YuE2 本地安装不完整。')
    mode=payload.get('mode','remix')
    if mode not in ('remix','preserve'):raise ValueError('请选择改编模式。')
    instrumental=payload.get('instrumental',t.get('instrumental',False))
    if not isinstance(instrumental,bool):raise ValueError('歌曲类型无效。')
    if instrumental:mode='remix'
    duration=float(t['duration'])
    if not math.isfinite(duration) or not 5<=duration<=600:raise ValueError('歌曲长度须为 5 秒至 10 分钟。')
    seed=int(payload.get('seed',secrets.randbelow(2147483647)))
    if not 0<=seed<=2147483647:raise ValueError('随机种子超出范围。')
    style=text_field(payload,'style',40) or '自定义'
    instructions=text_field(payload,'instructions')
    variant_instructions=text_field(payload,'variant_instructions')
    caption=build_caption(style,instructions,variant_instructions) if 'instructions' in payload or 'variant_instructions' in payload else text_field(payload,'caption',2000)
    part=payload.get('suite_part')
    if part is not None and (not isinstance(part,dict) or set(part)!={'id','part'} or not re.fullmatch('[0-9a-f]{32}',str(part['id'])) or part['part'] not in range(len(SUITE))):raise ValueError('管弦组曲设置无效。')
    # Each suite part also asks for its own movement character (soft, fast, grand).
    if part:caption+=', '+SUITE[part['part']]['hint']
    lyrics=str(payload.get('lyrics',t.get('lyrics',''))).strip()
    if not 1<=len(caption)<=2000 or len(lyrics)>20000:raise ValueError('请填写风格描述；歌词最多 20000 字。')
    fmt=payload.get('format','both')
    if fmt not in ('both','wav','mp3'):raise ValueError('导出格式无效。')
    from yue2_engine import PROFILES, GENDERS, TONES
    profile=payload.get('profile','quality');gender=payload.get('gender','auto');tone=payload.get('tone','natural')
    if profile not in PROFILES or gender not in GENDERS or tone not in TONES:raise ValueError('速度或声音设置无效。')
    if mode=='preserve' or instrumental:gender='auto';tone='natural'
    strength,melody=validate_controls(payload)
    if mode=='preserve':melody='reference'
    # Voice swap replaces generated singing only; preserve keeps the recording, instrumental has none,
    # and a kid, duet or choir singer keeps the character that was asked for.
    voice=None if mode=='preserve' or instrumental or gender in OWN_VOICE_SINGERS else voice_swap.validate_job(payload.get('voice'))
    kid=gender=='kid'
    smart=payload.get('smart_applied') or {}
    if voice and not voice_swap.available():raise ValueError(f'《{t["title"]}》要换成原唱音色，但本机还没有安装换声引擎（Seed-VC）。请先按说明安装，或关闭“换成原唱音色”。')
    adjust=audio_adjust.validate(payload.get('adjust') or audio_adjust.DEFAULT)
    adjust=adjust if audio_adjust.changes_anything(adjust) else None
    speed_variants=payload.get('speed_variants',True)
    if not isinstance(speed_variants,bool):raise ValueError('变速版本设置无效。')
    source_duration=duration
    if audio_adjust.changes_source(adjust):
        duration=audio_adjust.output_seconds(source_duration,adjust)
        if not 5<=duration<=600:raise ValueError(f'《{t["title"]}》变速后为 {duration/60:.1f} 分钟，须在 5 秒至 10 分钟之间。')
    config={'profile':profile,'gender':gender,'tone':tone,'engine':'yue2','track_id':t['id'],'source':t['source'],'start':0,'duration':duration,'seed':seed,
        'source_duration':source_duration,'adjust':adjust,'voice':voice,'speed_variants':speed_variants,'kid':kid,'smart_defaults':smart,
        'mode':mode,'caption':caption,'lyrics':'' if instrumental else lyrics,'auto_lyrics':not instrumental,'instrumental':instrumental,'format':fmt,'style':style,
        'app_version':VERSION,'controls_version':CONTROL_VERSION,'change_strength':strength,'melody_mode':melody,
        'instructions':instructions,'variant_instructions':variant_instructions}
    config['effective_controls']=effective_controls(config)
    identifier=uuid.uuid4().hex
    meta={'id':identifier,'engine':'yue2','track_id':t['id'],'title':t['title'],'style':config['style'],'mode':mode,'adjust':adjust,'voice':voice,
        'speed_variants':speed_variants,'kid':kid,'smart_defaults':smart,'duration':duration,'start':0,'state':'queued','created_at':time.time(),'attempt':0,'max_attempts':2}
    if part:
        suite={'suite_id':part['id'],'suite_part':part['part'],'suite_label':SUITE[part['part']]['label']}
        config.update(suite);meta.update(suite)
    config['output_name']=meta['output_name']=job_output_name(meta)
    meta['config']={k:v for k,v in config.items() if k!='source'}
    return config,meta

def expand_item(item):
    """One song's jobs as (payload, pair id, version letter): its versions, then the three suite parts."""
    variants=item.get('variants')
    if variants is None:return [(item,None,None)]
    if not isinstance(variants,list) or not 1<=len(variants)<=MAX_VERSIONS or not all(isinstance(v,dict) for v in variants):raise ValueError(f'每首歌曲需要 1–{MAX_VERSIONS} 个版本。')
    names=[str(v.get('style','')).strip().casefold() for v in variants]
    if len(set(names))!=len(names):raise ValueError('同一首歌的各版本请选择不同风格。')
    smart=item.get('smart_defaults',False);explicit=item.get('explicit',[])
    if not isinstance(smart,bool) or not isinstance(explicit,list) or not set(map(str,explicit))<=set(SMART_FIELDS):raise ValueError('智能默认设置无效。')
    suite=item.get('suite')
    if suite is not None:
        suite=validate_suite({'enabled':True,'styles':suite.get('styles')} if isinstance(suite,dict) and set(suite)=={'styles'} else None)['styles']
        if len(variants)+len(SUITE)>MAX_VERSIONS:raise ValueError(f'加上管弦组曲的 {len(SUITE)} 个版本后超过 {MAX_VERSIONS} 个，请减少这首歌的版本数。')
    pair=uuid.uuid4().hex
    base={k:v for k,v in item.items() if k not in ('variants','voice_swap','voice','smart_defaults','explicit','suite')}
    seed=secrets.randbelow(2147483648-MAX_VERSIONS)
    swap=voice_swap.validate(item.get('voice_swap') if item.get('voice_swap') is not None else {})
    expanded=[]
    for index,variant in enumerate(variants):
        override={k:v for k,v in variant.items() if k in ('style','caption','gender','tone','variant_instructions','change_strength','melody_mode')}
        job={**base,**override,'seed':seed+index,'voice':voice_swap.for_variant(swap,LETTERS[index])}
        if smart:
            job,applied=apply_smart_defaults(job,job.get('style',''),explicit)
            job['smart_applied']=applied
        expanded.append((job,pair,LETTERS[index]))
    if suite:
        identifier=uuid.uuid4().hex
        # The parts share every setting and the reference melody, so the suite can switch between them
        # on one time line; they get no speed versions or voice swap of their own.
        for part,style in enumerate(suite):
            index=len(variants)+part
            expanded.append(({**base,'style':style,'melody_mode':'reference','seed':seed+index,'voice':None,'speed_variants':False,
                'suite_part':{'id':identifier,'part':part}},pair,LETTERS[index]))
    return expanded

def create_batch(payload):
    items=payload.get('items')
    key=str(payload.get('request_id',''))
    if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('每批请选择 1–100 首歌曲。')
    if not re.fullmatch('[a-zA-Z0-9-]{16,64}',key):raise ValueError('批次编号无效。')
    with LOCK:
        journal=DATA/'batches'/f'{key}.json'
        existing=read(journal)
        if existing:
            commit_batch(journal);WAKE.set()
            return {'jobs':[public_job(JOBS/i) for i in existing['ids']]}
        expanded=[]
        for item in items:
            if not isinstance(item,dict):raise ValueError('批次内容无效。')
            # Smart-default records and suite parts are only ever set here, never taken from a request.
            expanded+=expand_item({k:v for k,v in item.items() if k not in ('smart_applied','suite_part')})
        if sum(j['state'] in ('queued','running') for j in jobs())+len(expanded)>QUEUE_LIMIT:raise ValueError(f'队列最多容纳 {QUEUE_LIMIT} 首，请等部分任务完成。')
        # Validate the whole batch before creating any visible job.
        prepared=[];first={}
        for item,pair,variant in expanded:
            config,meta=prepare_job(item)
            if pair:
                config.update(pair_id=pair,variant=variant)
                meta.update(pair_id=pair,variant=variant)
                # Smart defaults can give versions their own mode or key/tempo; analysis is only reused
                # from the first version with the same source settings.
                same=(pair,config['mode'],json.dumps(config['adjust'],sort_keys=True))
                if same in first:config['reuse_from']=first[same]
                else:first[same]=meta['id']
                meta['config']={k:v for k,v in config.items() if k!='source'}
            prepared.append((config,meta))
        last_order=max([time.time()]+[j.get('queue_order',j['created_at']) for j in jobs()])
        for index,(_,meta) in enumerate(prepared):meta['queue_order']=last_order+(index+1)*.001
        journal.parent.mkdir(exist_ok=True)
        save(journal,{'ids':[m['id'] for _,m in prepared],'prepared':prepared})
        commit_batch(journal)
        WAKE.set()
        return {'jobs':[m for _,m in prepared]}

def commit_batch(journal):
    batch=read(journal,{})
    for config,meta in batch.get('prepared',[]):
        directory=JOBS/meta['id'];directory.mkdir(exist_ok=True)
        if not (directory/'job.json').exists():
            save(directory/'config.json',config);save(directory/'job.json',meta)
    if 'prepared' in batch:save(journal,{'ids':batch['ids']})

def create_job(payload):return create_batch({'items':[payload],'request_id':uuid.uuid4().hex})['jobs'][0]

def create_adjustment(identifier,payload):
    """Queue a new work derived from a finished one; the original files stay untouched."""
    adjust=audio_adjust.validate(payload.get('adjust'))
    if not audio_adjust.changes_anything(adjust):raise ValueError('请至少调整变调、速度或混响中的一项。')
    with LOCK:target=queue_adjustment(identifier,adjust)
    EFFECTS_WAKE.set()
    return public_job(target)

def finished_work(identifier,purpose):
    """(job, its WAV, seconds) of a completed work that a derived work may read."""
    if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError('任务不存在。')
    directory=JOBS/identifier;parent=read(directory/'job.json')
    if not parent or parent.get('state')!='completed':raise ValueError(f'完成的作品才能{purpose}。')
    result=read(directory/'result.json',{})
    source=result_file(directory,result,'result',('.wav',))
    if source is None or not source.is_file():raise ValueError('找不到这首作品的 WAV 音频。')
    return parent,source,float(result.get('checks',{}).get('result',{}).get('seconds') or parent['duration'])

def queue_room(count=1):
    if sum(j['state'] in ('queued','running') for j in jobs())+count>QUEUE_LIMIT:raise ValueError(f'队列最多容纳 {QUEUE_LIMIT} 首，请等部分任务完成。')

def queue_derived(kind,parent,duration,inputs,**fields):
    """One queued effects job made from finished work (call with LOCK held); returns its directory.
    The name keeps the parent's labels: original voice, kid voice, suite part, key/tempo and DJ edits."""
    parent_config=parent.get('config',{})
    meta={'id':uuid.uuid4().hex,'kind':kind,'engine':'effects','parent_id':parent['id'],
        'group_id':parent.get('group_id') or parent.get('pair_id') or parent['id'],
        'track_id':parent.get('track_id'),'title':parent['title'],'style':parent.get('style',''),'variant':parent.get('variant'),
        'mode':parent.get('mode','remix'),'adjust_history':adjust_history(parent),'dj_history':parent.get('dj_history',[]),'voice':parent.get('voice'),
        'kid':bool(parent.get('kid')),'suite_label':parent.get('suite_label'),
        'duration':duration,'start':0,'state':'queued','created_at':time.time(),'attempt':0,'max_attempts':2,
        'queue_order':max([time.time()]+[j.get('queue_order',j['created_at']) for j in jobs()])+.001,**fields}
    meta['output_name']=job_output_name(meta)
    config={'kind':kind,'engine':'effects','duration':duration,'parent_id':meta['parent_id'],'output_name':meta['output_name'],
        'format':parent_config.get('format','both'),'mode':meta['mode'],'instrumental':bool(parent_config.get('instrumental')),
        'style':meta['style'],'app_version':VERSION,**inputs}
    meta['config']={k:v for k,v in config.items() if k not in ('source','sources')}
    target=JOBS/meta['id'];target.mkdir()
    save(target/'config.json',config);save(target/'job.json',meta)
    return target

def queue_adjustment(identifier,adjust,auto=False):
    """One queued key/tempo job on a completed work (call with LOCK held); returns its directory."""
    parent,source,seconds=finished_work(identifier,'变调变速')
    duration=audio_adjust.output_seconds(seconds,adjust)
    if not 5<=duration<=1200:raise ValueError(f'变速后为 {duration/60:.1f} 分钟，须在 5 秒至 20 分钟之间。')
    queue_room()
    history=adjust_history(parent)+[adjust]
    return queue_derived('adjust',parent,duration,{'source':str(source),'source_duration':seconds,'adjust':adjust,'adjust_history':history},
        auto=auto,adjust=adjust,adjust_history=history)

def create_dj(identifier,payload):
    """Queue a DJ edit of a finished work (build-up & drop, filter intro, echo-out); the original stays untouched."""
    effects=dj_tools.validate(payload.get('effects'))
    with LOCK:
        parent,source,seconds=finished_work(identifier,'添加 DJ 效果')
        queue_room()
        target=queue_derived('dj',parent,seconds,{'source':str(source),'source_duration':seconds,'effects':effects},
            dj=effects,dj_history=parent.get('dj_history',[])+[effects])
    EFFECTS_WAKE.set()
    return public_job(target)

def create_mix(payload):
    """Queue one non-stop DJ mix of 2–20 finished works, in the given or an automatic order."""
    ids,order,beats,name=dj_tools.validate_mix(payload)
    with LOCK:
        works=[finished_work(identifier,'做成 DJ 串烧') for identifier in ids]
        total=sum(seconds for _,_,seconds in works)
        if total>dj_tools.MAX_MIX_SECONDS:raise ValueError(f'所选作品共 {total/60:.0f} 分钟，DJ 串烧最多 {dj_tools.MAX_MIX_SECONDS//60} 分钟。')
        queue_room()
        first=works[0][0]
        parent={'id':first['id'],'title':name or f'{len(ids)} 首串烧','style':'DJ Mix','group_id':uuid.uuid4().hex,'config':first.get('config',{})}
        sources=[{'id':job['id'],'path':str(source),'title':job_output_name(job),'seconds':seconds} for job,source,seconds in works]
        target=queue_derived('mix',parent,max(5.,total-(len(ids)-1)*beats*.5),{'sources':sources,'order':order,'transition_beats':beats},
            parent_ids=ids,order=order,transition_beats=beats,variant=None,track_id=None,adjust_history=[],dj_history=[],voice=None,kid=False,suite_label=None)
    EFFECTS_WAKE.set()
    return public_job(target)

def queue_suite(meta):
    """When the last of a song's three suite parts completes, queue its orchestral suite once (LOCK held).
    Problems are recorded on that part, never raised into the worker."""
    suite=meta.get('suite_id')
    if not suite or meta.get('kind'):return
    marker=DATA/'suites'/f'{suite}.json'
    if marker.exists():return
    parts=sorted((j for j in jobs() if j.get('suite_id')==suite and not j.get('kind')),key=lambda j:j.get('suite_part',0))
    if len(parts)!=len(SUITE) or any(j['state']!='completed' for j in parts):return
    try:
        works=[finished_work(j['id'],'组成管弦组曲') for j in parts]
        queue_room()
        target=queue_derived('suite',works[0][0],max(seconds for _,_,seconds in works),
            {'sources':[{'id':job['id'],'path':str(source),'style':job.get('style','')} for job,source,_ in works]},
            style='Orchestral Suite',parent_ids=[j['id'] for j in parts],suite_styles=[j.get('style','') for j in parts],suite_label=None,variant=None)
    except (ValueError,OSError) as error:
        meta['suite_error']=str(error);return
    marker.parent.mkdir(exist_ok=True);save(marker,{'job':target.name,'parts':[j['id'] for j in parts]})
    EFFECTS_WAKE.set()

def queue_speed_variants(meta):
    """After a generated version completes, queue its Speed Up / Slowed / Super Slowed versions once.
    The parent must already be saved as completed. Problems are recorded, never raised into the worker."""
    if meta.get('kind') in EFFECT_KINDS or not meta.get('speed_variants') or 'speed_variant_ids' in meta:return
    created=[];errors=[]
    for name in audio_adjust.PRESETS:
        try:created.append(queue_adjustment(meta['id'],audio_adjust.preset(name),auto=True).name)
        except (ValueError,OSError) as error:errors.append({'preset':name,'error':str(error)})
    meta['speed_variant_ids']=created
    if errors:meta['speed_variant_errors']=errors
    if created:EFFECTS_WAKE.set()

def processing_seconds(meta):
    stored=meta.get('processing_seconds')
    if stored is None:
        return max(0,(meta.get('finished_at') or time.time())-meta['started_at']) if meta.get('started_at') else 0
    active=meta.get('attempt_started_at')
    return max(0,float(stored)+(max(0,time.time()-active) if active and meta['state']=='running' else 0))

def close_clock(meta, end=None):
    start=meta.pop('attempt_started_at',None)
    if start:meta['processing_seconds']=meta.get('processing_seconds',0)+max(0,(end if end is not None else time.time())-start)

def change_jobs(identifiers, action):
    if not isinstance(identifiers,list) or not 1<=len(identifiers)<=QUEUE_LIMIT:raise ValueError('请选择任务。')
    with LOCK:
        for identifier in identifiers:
            if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier) or not read(JOBS/identifier/'job.json'):raise ValueError('任务不存在。')
        result=[]
        for identifier in dict.fromkeys(identifiers):
            directory=JOBS/identifier
            meta=cancel(identifier) if action=='remove' else read(directory/'job.json')
            if action=='remove':meta['hidden']=True
            elif action=='restore':meta['hidden']=False
            elif action=='favorite':
                if meta['state']!='completed':raise ValueError('完成的作品才能收藏。')
                meta['favorite']=not meta.get('favorite',False)
            save(directory/'job.json',meta);result.append(meta)
        return result

def finish_attempt(directory,code):
    meta=read(directory/'job.json')
    if meta['state']=='cancelled':return meta
    close_clock(meta)
    succeeded=code==0 and (directory/'result.json').exists()
    if succeeded:
        meta.update(state='completed',finished_at=time.time());meta.pop('error',None)
        save(directory/'job.json',meta);queue_speed_variants(meta);queue_suite(meta)
    else:
        error=read(directory/'error.json',{}).get('error','本地引擎退出，未返回完整作品。')
        meta.setdefault('errors',[]).append({'attempt':meta.get('attempt',1),'time':time.time(),'detail':error[-4000:]})
        retry=meta.get('attempt',1)<meta.get('max_attempts',2)
        meta.update(state='queued' if retry else 'failed',error='正在自动重试一次。' if retry else '重试后仍未完成，已继续下一首。查看详情了解原因。')
        if not retry:meta['finished_at']=time.time()
        save(directory/'progress.json',{'stage':'retry' if retry else 'failed','message':meta['error']})
    save(directory/'job.json',meta)
    return meta

def pending_jobs(kind):return [j for j in jobs() if j['state']=='queued' and (j.get('kind') in EFFECT_KINDS)==(kind=='adjust')]

def worker(kind='generate'):
    """One serialized queue per kind: YuE2 generation, or effects made from finished works
    (key/tempo, DJ edits, non-stop mixes, orchestral suites)."""
    global RUNNING
    effects=kind=='adjust';wake=EFFECTS_WAKE if effects else WAKE
    while True:
        wake.wait(2);wake.clear()
        with LOCK:
            if settings().get('paused'):continue
            pending=pending_jobs(kind)
            if not pending:continue
            meta=pending[-1];directory=JOBS/meta['id']
            if not effects:RUNNING=meta['id']
            meta['state']='running';meta['attempt']=meta.get('attempt',0)+1
            meta.setdefault('started_at',time.time());meta.setdefault('processing_seconds',0);meta['attempt_started_at']=time.time();meta['heartbeat_at']=time.time()
            save(directory/'job.json',meta)
            # A new attempt must never reuse stale success/error markers.
            for name in ('result.json','error.json'):(directory/name).unlink(missing_ok=True)
            log=(directory/f"worker-attempt-{meta['attempt']}.log").open('w',encoding='utf-8')
            try:
                process=subprocess.Popen([str(PYTHON),str(ROOT/'engine.py'),str(directory)],stdout=log,stderr=subprocess.STDOUT,
                    cwd=ROOT,env={**os.environ,'PYTHONUTF8':'1'},creationflags=FLAGS)
                ACTIVE[meta['id']]=process
            except Exception as error:
                save(directory/'error.json',{'error':str(error)})
                finish_attempt(directory,1);log.close()
                if not effects:RUNNING=None
                wake.set();continue
        while process.poll() is None:
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with LOCK:
                    current=read(directory/'job.json')
                    if current['state']=='running':current['heartbeat_at']=time.time();save(directory/'job.json',current)
        code=process.returncode;log.close()
        with LOCK:
            finish_attempt(directory,code);ACTIVE.pop(meta['id'],None)
            if not effects:RUNNING=None
        wake.set()

def cancel(identifier):
    if not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError('任务不存在。')
    with LOCK:
        directory=JOBS/identifier;meta=read(directory/'job.json')
        if not meta:raise ValueError('任务不存在。')
        if meta['state'] not in ('queued','running'):return meta
        process=ACTIVE.get(identifier)
        if process and process.poll() is None:
            stopped=subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=FLAGS,timeout=20)
            if stopped.returncode and process.poll() is None:raise ValueError('暂时无法停止此任务，请稍后重试。')
        close_clock(meta)
        meta['state']='cancelled';meta['finished_at']=time.time();save(directory/'job.json',meta)
        return meta

class Handler(server.Handler):
    def json(self,value,status=200,head=False):
        payload=json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers()
        if not head:self.wfile.write(payload)

    def valid_host(self):return self.headers.get('Host') in (f'127.0.0.1:{server.PORT}',f'localhost:{server.PORT}')

    def do_GET(self,head=False):
        if not self.valid_host():self.send_error(403);return
        route=urlsplit(self.path).path
        if route=='/health':self.json({'app':server.APP_ID,'mode':'local-app','generation_enabled':True},head=head);return
        if route=='/api/state':
            with LOCK:
                values=[public_track(SAMPLE)]+[public_track(t) for p in TRACKS.iterdir() if (t:=read(p/'track.json'))]
                from yue2_engine import available
                self.json({'token':TOKEN,'tracks':values,'jobs':jobs(),'running':RUNNING,'engines':{'ace':True,'yue2':available(),'voice':voice_swap.available()},'settings':settings(),'release':RELEASE},head=head)
            return
        match=re.fullmatch(r'/media/(sample|[0-9a-f]{32})',route)
        if match:
            try:path=Path(track(match[1])['source'])
            except ValueError:self.send_error(404);return
            self.send_file(path,head);return
        match=re.fullmatch(r'/results/([0-9a-f]{32})/(result|original|vocals|instrumental|mp3)',route)
        if match:
            directory=JOBS/match[1];meta=read(directory/'job.json',{});result=read(directory/'result.json',{})
            path=result_file(directory,result,match[2])
            if meta.get('state')!='completed' or path is None:self.send_error(404);return
            name=job_output_name(meta)+path.suffix if match[2] in ('result','mp3') else None
            self.send_file(path,head,name);return
        super().do_GET(head)

    def send_file(self,path,head,download_name=None):
        if not path.is_file():self.send_error(404);return
        size=path.stat().st_size;start=0;end=size-1;status=200
        if self.headers.get('Range'):
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers['Range'])
            if not match:self.send_error(416);return
            start=int(match[1]);end=min(end,int(match[2]) if match[2] else end)
            if start>end:self.send_error(416);return
            status=206
        self.send_response(status);self.send_header('Content-Type','audio/mpeg' if path.suffix=='.mp3' else 'audio/wav');self.send_header('Accept-Ranges','bytes')
        self.send_header('Content-Length',str(end-start+1));self.send_header('Cache-Control','no-cache')
        if download_name:self.send_header('Content-Disposition',"inline; filename*=UTF-8''"+quote(download_name,safe=''))
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if head:return
        try:
            with path.open('rb') as stream:
                stream.seek(start);remaining=end-start+1
                while remaining:
                    chunk=stream.read(min(262144,remaining))
                    if not chunk:break
                    self.wfile.write(chunk);remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass

    def do_POST(self):
        origin=self.headers.get('Origin')
        if not self.valid_host() or self.headers.get('X-Sing-Token')!=TOKEN or (origin and origin not in (f'http://127.0.0.1:{server.PORT}',f'http://localhost:{server.PORT}')):
            self.json({'error':'请刷新应用后重试。'},403);return
        route=urlsplit(self.path).path
        import_directory=None
        try:
            size=int(self.headers.get('Content-Length','0'))
            if route=='/api/import':
                if not 0<size<=250*1024**2:raise ValueError('文件大小须在 250 MB 以内。')
                identifier=uuid.uuid4().hex;directory=TRACKS/identifier;directory.mkdir();import_directory=directory
                upload=directory/'upload.bin';self.connection.settimeout(120)
                with upload.open('wb') as stream:
                    remaining=size
                    while remaining:
                        chunk=self.rfile.read(min(1024**2,remaining))
                        if not chunk:raise ValueError('导入中断，请重试。')
                        stream.write(chunk);remaining-=len(chunk)
                output=directory/'source.wav'
                result=subprocess.run([str(TEST/'bin/ffmpeg.exe'),'-nostdin','-y','-v','error','-i',str(upload),'-map','0:a:0','-t','601','-ac','2','-ar','48000','-c:a','pcm_s16le',str(output)],capture_output=True,creationflags=FLAGS,timeout=120)
                from lyrics import embedded
                embedded_lyrics=embedded(upload) if result.returncode==0 else ''
                upload.unlink(missing_ok=True)
                if result.returncode:raise ValueError('无法读取音频，请尝试 WAV、MP3 或 FLAC。')
                with wave.open(str(output)) as wav:duration=wav.getnframes()/wav.getframerate()
                if not 5<=duration<=600:raise ValueError('歌曲长度须为 5 秒至 10 分钟。')
                name=unquote(self.headers.get('X-File-Name','我的歌曲'))
                value={'id':identifier,'title':Path(name).stem[:160],'duration':duration,'source':str(output),'lyrics':embedded_lyrics,'lyrics_source':'embedded' if embedded_lyrics else 'auto','filename':name,'created_at':time.time()}
                save(directory/'track.json',value);import_directory=None;self.json(public_track(value),201);return
            if not 0<=size<=12*1024**2:raise ValueError('请求太大。')
            payload=json.loads(self.rfile.read(size) or b'{}')
            if route=='/api/settings':
                with LOCK:
                    current=settings()
                    for key in ('style','style_b','mode','format','instructions','paused','profile','gender','tone','instructions_a','instructions_b','change_strength','melody_mode','version_count','speed_variants','smart_defaults'):
                        if key in payload:current[key]=payload[key]
                    if 'adjust' in payload:current['adjust']=audio_adjust.validate(payload['adjust'])
                    if 'voice_swap' in payload:current['voice_swap']=voice_swap.validate(payload['voice_swap'])
                    if 'suite' in payload:current['suite']=validate_suite(payload['suite'])
                    version_count(current['version_count'])
                    if 'styles' in payload:
                        current['styles']=text_list(payload['styles'],40,allow_empty=False,message='每个版本都需要 1–40 字的风格名称。',pad=lambda v:default_styles(*v))
                        current['style'],current['style_b']=current['styles'][:2]
                    if 'version_instructions' in payload:
                        current['version_instructions']=text_list(payload['version_instructions'],900,allow_empty=True,message='各版本补充要求须为不超过 900 字的文字。')
                        current['instructions_a'],current['instructions_b']=current['version_instructions'][:2]
                    current['styles'][:2]=[current['style'],current['style_b']]
                    current['version_instructions'][:2]=[current['instructions_a'],current['instructions_b']]
                    text_list(current['styles'],40,allow_empty=False,message='每个版本都需要 1–40 字的风格名称。')
                    text_list(current['version_instructions'],900,allow_empty=True,message='各版本补充要求须为不超过 900 字的文字。')
                    if current['mode'] not in ('remix','preserve') or current['format'] not in ('both','wav','mp3') or not isinstance(current['paused'],bool) or not isinstance(current['speed_variants'],bool) or not isinstance(current['smart_defaults'],bool):raise ValueError('设置无效。')
                    if not 1<=len(str(current['style']))<=40 or len(str(current['instructions']))>900:raise ValueError('风格名称或描述太长。')
                    from yue2_engine import PROFILES,GENDERS,TONES
                    if current['profile'] not in PROFILES or current['gender'] not in GENDERS or current['tone'] not in TONES or not 1<=len(str(current['style_b']))<=40:raise ValueError('速度或声音设置无效。')
                    validate_controls(current)
                    for key in ('instructions','instructions_a','instructions_b'):text_field(current,key)
                    save(DATA/'settings.json',current);wake_workers()
                self.json(current);return
            match=re.fullmatch(r'/api/tracks/([0-9a-f]{32})',route)
            if match:
                with LOCK:
                    value=track(match[1])
                    for key in ('lyrics','style','style_b','mode','hidden','gender','tone','change_strength','melody_mode','instructions','instrumental','version_count','speed_variants','suite'):
                        if key in payload:value[key]=payload[key]
                    if not isinstance(value.get('speed_variants'),(bool,type(None))):raise ValueError('变速版本设置无效。')
                    validate_suite(value.get('suite'),inherit=True)
                    if 'adjust' in payload:value['adjust']=audio_adjust.validate(payload['adjust'],inherit=True)
                    if 'voice_swap' in payload:value['voice_swap']=voice_swap.validate(payload['voice_swap'],inherit=True)
                    version_count(value.get('version_count'),inherit=True)
                    if 'styles' in payload:
                        value['styles']=text_list(payload['styles'],40,allow_empty=True,message='逐首版本风格无效。')
                        value['style'],value['style_b']=value['styles'][:2]
                    value=versioned_track(value)
                    text_list(value['styles'],40,allow_empty=True,message='逐首版本风格无效。')
                    if not isinstance(value.get('lyrics',''),str) or len(value.get('lyrics',''))>20000 or value.get('mode','') not in ('','remix','preserve') or len(str(value.get('style','')))>40 or not isinstance(value.get('hidden',False),bool):raise ValueError('歌曲设置无效。')
                    if not isinstance(value.get('instrumental',False),bool):raise ValueError('歌曲类型无效。')
                    from yue2_engine import GENDERS,TONES
                    if value.get('gender','') not in ('',*GENDERS) or value.get('tone','') not in ('',*TONES) or len(str(value.get('style_b','')))>40:raise ValueError('声音或 B 版风格无效。')
                    validate_controls(value,inherit=True);text_field(value,'instructions')
                    save(TRACKS/match[1]/'track.json',value)
                self.json(public_track(value));return
            if route=='/api/batch':self.json(create_batch(payload),201);return
            if route=='/api/jobs':self.json(create_job(payload),201);return
            if route=='/api/mix':self.json(create_mix(payload),201);return
            if route=='/api/queue/remove':self.json({'jobs':change_jobs(payload.get('ids'),'remove')});return
            match=re.fullmatch(r'/api/jobs/([0-9a-f]{32})/(cancel|retry|remove|restore|favorite|adjust|dj)',route)
            if match:
                if match[2] in ('remove','restore','favorite'):self.json(change_jobs([match[1]],match[2])[0]);return
                if match[2]=='cancel':self.json(cancel(match[1]));return
                if match[2]=='adjust':self.json(create_adjustment(match[1],payload),201);return
                if match[2]=='dj':self.json(create_dj(match[1],payload),201);return
                with LOCK:
                    directory=JOBS/match[1];meta=read(directory/'job.json')
                    if not meta or meta['state'] not in ('failed','cancelled'):raise ValueError('此任务无需重试。')
                    meta['processing_seconds']=processing_seconds(meta)
                    meta.update(state='queued',hidden=False,max_attempts=meta.get('attempt',0)+2,
                        queue_order=max([time.time()]+[j.get('queue_order',j['created_at']) for j in jobs()])+.001)
                    meta.pop('finished_at',None);meta.pop('error',None)
                    save(directory/'job.json',meta)
                    save(directory/'progress.json',{'stage':'queued','message':'等待重新处理'})
                    wake_workers()
                self.json(public_job(directory),201);return
            self.send_error(404)
        except (ValueError,TypeError,KeyError) as error:self.json({'error':str(error)},400)
        except Exception:
            with (DATA/'server-errors.log').open('a',encoding='utf-8') as log:log.write(traceback.format_exc())
            self.json({'error':'本地处理未完成，请重试。'},500)
        finally:
            if import_directory is not None and not (import_directory/'track.json').exists():
                for name in ('upload.bin','source.wav'):
                    try:(import_directory/name).unlink(missing_ok=True)
                    except OSError:pass
                try:import_directory.rmdir()
                except OSError:pass

def main():
    import argparse,urllib.request
    parser=argparse.ArgumentParser();parser.add_argument('--desktop',action='store_true');args=parser.parse_args()
    JOBS.mkdir(parents=True,exist_ok=True);TRACKS.mkdir(parents=True,exist_ok=True)
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.PORT}/health',timeout=1) as response:existing=json.load(response)
    except OSError:existing=None
    if existing:
        if existing.get('mode')!='local-app':raise RuntimeError('请先关闭旧版界面预览服务。')
        if args.desktop:server.desktop()
        return
    http=ThreadingHTTPServer(('127.0.0.1',server.PORT),Handler)
    (DATA/'server.pid').write_text(str(os.getpid()))
    for journal in (DATA/'batches').glob('*.json'):commit_batch(journal)
    for job in jobs():
        if job['state']=='running':
            directory=JOBS/job['id']
            job=read(directory/'job.json')
            close_clock(job,job.get('heartbeat_at',job.get('attempt_started_at',time.time())))
            save(directory/'job.json',job)
            save(directory/'error.json',{'error':'上次本机运行中断。'})
            finish_attempt(directory,1)
    threading.Thread(target=worker,daemon=True).start()
    threading.Thread(target=worker,args=('adjust',),daemon=True).start()
    if args.desktop:server.desktop()
    http.serve_forever()

if __name__=='__main__':main()
