"""Single-user offline music studio; loopback HTTP and serialized local jobs."""
import json, math, os, re, secrets, subprocess, threading, time, traceback, uuid, wave
from pathlib import Path
from urllib.parse import urlsplit, unquote
from http.server import ThreadingHTTPServer
import server
from generation_controls import RELEASE, VERSION, CONTROL_VERSION, DEFAULT_STRENGTH, LEGACY_INSTRUCTIONS, validate_controls, text_field, build_caption, effective_controls

ROOT=Path(os.environ.get('SING_APP_ROOT',str(server.ROOT)))
TEST=Path(os.environ.get('SING_LOCAL_TEST',str(ROOT.parent/'local-test')))
DATA=ROOT/'data'
JOBS=DATA/'jobs'
TRACKS=DATA/'tracks'
TOKEN=secrets.token_urlsafe(32)
LOCK=threading.RLock()
WAKE=threading.Event()
PROCESS=None
RUNNING=None
PYTHON=TEST/'ace-env/Scripts/python.exe'
FLAGS=getattr(subprocess,'CREATE_NO_WINDOW',0)
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

def public_track(t):return {k:v for k,v in t.items() if k!='source'}

def public_job(directory):
    value=read(directory/'job.json')
    if not value:return None
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
    return value

def jobs():return sorted(filter(None,(public_job(p) for p in JOBS.iterdir() if p.is_dir())),key=lambda x:x.get('queue_order',x['created_at']),reverse=True)

def settings():
    value = {**{'profile':'quality','style_b':'Jazz','gender':'auto','tone':'natural',
        'style':'R&B','mode':'remix','format':'both','instructions':'','paused':False,
        'instructions_a':'','instructions_b':'','change_strength':DEFAULT_STRENGTH,'melody_mode':'reference'},
        **read(DATA/'settings.json',{})}
    # Only migrate the exact old built-in text, never arbitrary user writing.
    if not value.get('controls_version') and value['instructions'] == LEGACY_INSTRUCTIONS:
        value['instructions'] = ''
    value['controls_version'] = CONTROL_VERSION
    return value

def prepare_job(payload):
    t=track(payload.get('track_id'))
    from yue2_engine import available
    if not available():raise ValueError('YuE2 本地安装不完整。')
    mode=payload.get('mode','remix')
    if mode not in ('remix','preserve'):raise ValueError('请选择改编模式。')
    duration=float(t['duration'])
    if not math.isfinite(duration) or not 5<=duration<=600:raise ValueError('歌曲长度须为 5 秒至 10 分钟。')
    seed=int(payload.get('seed',secrets.randbelow(2147483647)))
    if not 0<=seed<=2147483647:raise ValueError('随机种子超出范围。')
    style=text_field(payload,'style',40) or '自定义'
    instructions=text_field(payload,'instructions')
    variant_instructions=text_field(payload,'variant_instructions')
    caption=build_caption(style,instructions,variant_instructions) if 'instructions' in payload or 'variant_instructions' in payload else text_field(payload,'caption',2000)
    lyrics=str(payload.get('lyrics',t.get('lyrics',''))).strip()
    if not 1<=len(caption)<=2000 or len(lyrics)>20000:raise ValueError('请填写风格描述；歌词最多 20000 字。')
    fmt=payload.get('format','both')
    if fmt not in ('both','wav','mp3'):raise ValueError('导出格式无效。')
    from yue2_engine import PROFILES, GENDERS, TONES
    profile=payload.get('profile','quality');gender=payload.get('gender','auto');tone=payload.get('tone','natural')
    if profile not in PROFILES or gender not in GENDERS or tone not in TONES:raise ValueError('速度或声音设置无效。')
    if mode=='preserve':gender='auto';tone='natural'
    strength,melody=validate_controls(payload)
    if mode=='preserve':melody='reference'
    config={'profile':profile,'gender':gender,'tone':tone,'engine':'yue2','track_id':t['id'],'source':t['source'],'start':0,'duration':duration,'seed':seed,
        'mode':mode,'caption':caption,'lyrics':lyrics,'auto_lyrics':True,'format':fmt,'style':style,
        'app_version':VERSION,'controls_version':CONTROL_VERSION,'change_strength':strength,'melody_mode':melody,
        'instructions':instructions,'variant_instructions':variant_instructions}
    config['effective_controls']=effective_controls(config)
    identifier=uuid.uuid4().hex
    return config,{'id':identifier,'engine':'yue2','track_id':t['id'],'title':t['title'],'style':config['style'],'mode':mode,
        'duration':duration,'start':0,'state':'queued','created_at':time.time(),'attempt':0,'max_attempts':2,
        'config':{k:v for k,v in config.items() if k!='source'}}

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
            variants=item.get('variants')
            if variants is None:expanded.append((item,None,None));continue
            if not isinstance(variants,list) or len(variants)!=2 or not all(isinstance(v,dict) for v in variants):raise ValueError('每首歌曲需要 A、B 两个版本。')
            if str(variants[0].get('style','')).strip().casefold()==str(variants[1].get('style','')).strip().casefold():raise ValueError('A、B 请选择不同风格。')
            pair=uuid.uuid4().hex
            base={k:v for k,v in item.items() if k!='variants'}
            seed=secrets.randbelow(2147483646)
            for index,variant in enumerate(variants):
                override={k:v for k,v in variant.items() if k in ('style','caption','gender','tone','variant_instructions','change_strength','melody_mode')}
                expanded.append(({**base,**override,'seed':seed+index},pair,'AB'[index]))
        if sum(j['state'] in ('queued','running') for j in jobs())+len(expanded)>500:raise ValueError('队列最多容纳 500 首，请等部分任务完成。')
        # Validate the whole batch before creating any visible job.
        prepared=[];first={}
        for item,pair,variant in expanded:
            config,meta=prepare_job(item)
            if pair:
                config.update(pair_id=pair,variant=variant)
                meta.update(pair_id=pair,variant=variant)
                if pair in first:config['reuse_from']=first[pair]
                else:first[pair]=meta['id']
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
    if not isinstance(identifiers,list) or not 1<=len(identifiers)<=500:raise ValueError('请选择任务。')
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
    else:
        error=read(directory/'error.json',{}).get('error','本地引擎退出，未返回完整作品。')
        meta.setdefault('errors',[]).append({'attempt':meta.get('attempt',1),'time':time.time(),'detail':error[-4000:]})
        retry=meta.get('attempt',1)<meta.get('max_attempts',2)
        meta.update(state='queued' if retry else 'failed',error='正在自动重试一次。' if retry else '重试后仍未完成，已继续下一首。查看详情了解原因。')
        if not retry:meta['finished_at']=time.time()
        save(directory/'progress.json',{'stage':'retry' if retry else 'failed','message':meta['error']})
    save(directory/'job.json',meta)
    return meta

def worker():
    global PROCESS,RUNNING
    while True:
        WAKE.wait(2);WAKE.clear()
        with LOCK:
            if settings().get('paused'):continue
            pending=[j for j in jobs() if j['state']=='queued']
            if not pending:continue
            meta=pending[-1];directory=JOBS/meta['id'];RUNNING=meta['id']
            meta['state']='running';meta['attempt']=meta.get('attempt',0)+1
            meta.setdefault('started_at',time.time());meta.setdefault('processing_seconds',0);meta['attempt_started_at']=time.time();meta['heartbeat_at']=time.time()
            save(directory/'job.json',meta)
            # A new attempt must never reuse stale success/error markers.
            for name in ('result.json','error.json'):(directory/name).unlink(missing_ok=True)
            log=(directory/f"worker-attempt-{meta['attempt']}.log").open('w',encoding='utf-8')
            try:
                PROCESS=subprocess.Popen([str(PYTHON),str(ROOT/'engine.py'),str(directory)],stdout=log,stderr=subprocess.STDOUT,
                    cwd=ROOT,env={**os.environ,'PYTHONUTF8':'1'},creationflags=FLAGS)
                process=PROCESS
            except Exception as error:
                save(directory/'error.json',{'error':str(error)})
                finish_attempt(directory,1);log.close();PROCESS=None;RUNNING=None;WAKE.set();continue
        while process.poll() is None:
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with LOCK:
                    current=read(directory/'job.json')
                    if current['state']=='running':current['heartbeat_at']=time.time();save(directory/'job.json',current)
        code=process.returncode;log.close()
        with LOCK:
            finish_attempt(directory,code);PROCESS=None;RUNNING=None
        WAKE.set()

def cancel(identifier):
    if not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError('任务不存在。')
    with LOCK:
        directory=JOBS/identifier;meta=read(directory/'job.json')
        if not meta:raise ValueError('任务不存在。')
        if meta['state'] not in ('queued','running'):return meta
        if RUNNING==identifier and PROCESS and PROCESS.poll() is None:
            stopped=subprocess.run(['taskkill','/PID',str(PROCESS.pid),'/T','/F'],capture_output=True,creationflags=FLAGS,timeout=20)
            if stopped.returncode and PROCESS.poll() is None:raise ValueError('暂时无法停止此任务，请稍后重试。')
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
                self.json({'token':TOKEN,'tracks':values,'jobs':jobs(),'running':RUNNING,'engines':{'ace':True,'yue2':available()},'settings':settings(),'release':RELEASE},head=head)
            return
        match=re.fullmatch(r'/media/(sample|[0-9a-f]{32})',route)
        if match:
            try:path=Path(track(match[1])['source'])
            except ValueError:self.send_error(404);return
            self.send_file(path,head);return
        match=re.fullmatch(r'/results/([0-9a-f]{32})/(result|original|vocals|instrumental|mp3)',route)
        if match:
            directory=JOBS/match[1];meta=read(directory/'job.json',{});result=read(directory/'result.json',{})
            filename=result.get('files',{}).get(match[2])
            if meta.get('state')!='completed' or filename not in ('result.wav','source.wav','vocals.wav','instrumental.wav','result.mp3'):self.send_error(404);return
            self.send_file(directory/filename,head);return
        super().do_GET(head)

    def send_file(self,path,head):
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
                    for key in ('style','style_b','mode','format','instructions','paused','profile','gender','tone','instructions_a','instructions_b','change_strength','melody_mode'):
                        if key in payload:current[key]=payload[key]
                    if current['mode'] not in ('remix','preserve') or current['format'] not in ('both','wav','mp3') or not isinstance(current['paused'],bool):raise ValueError('设置无效。')
                    if not 1<=len(str(current['style']))<=40 or len(str(current['instructions']))>900:raise ValueError('风格名称或描述太长。')
                    from yue2_engine import PROFILES,GENDERS,TONES
                    if current['profile'] not in PROFILES or current['gender'] not in GENDERS or current['tone'] not in TONES or not 1<=len(str(current['style_b']))<=40:raise ValueError('速度或声音设置无效。')
                    validate_controls(current)
                    for key in ('instructions','instructions_a','instructions_b'):text_field(current,key)
                    save(DATA/'settings.json',current);WAKE.set()
                self.json(current);return
            match=re.fullmatch(r'/api/tracks/([0-9a-f]{32})',route)
            if match:
                with LOCK:
                    value=track(match[1])
                    for key in ('lyrics','style','style_b','mode','hidden','gender','tone','change_strength','melody_mode','instructions'):
                        if key in payload:value[key]=payload[key]
                    if not isinstance(value.get('lyrics',''),str) or len(value.get('lyrics',''))>20000 or value.get('mode','') not in ('','remix','preserve') or len(str(value.get('style','')))>40 or not isinstance(value.get('hidden',False),bool):raise ValueError('歌曲设置无效。')
                    from yue2_engine import GENDERS,TONES
                    if value.get('gender','') not in ('',*GENDERS) or value.get('tone','') not in ('',*TONES) or len(str(value.get('style_b','')))>40:raise ValueError('声音或 B 版风格无效。')
                    validate_controls(value,inherit=True);text_field(value,'instructions')
                    save(TRACKS/match[1]/'track.json',value)
                self.json(public_track(value));return
            if route=='/api/batch':self.json(create_batch(payload),201);return
            if route=='/api/jobs':self.json(create_job(payload),201);return
            if route=='/api/queue/remove':self.json({'jobs':change_jobs(payload.get('ids'),'remove')});return
            match=re.fullmatch(r'/api/jobs/([0-9a-f]{32})/(cancel|retry|remove|restore|favorite)',route)
            if match:
                if match[2] in ('remove','restore','favorite'):self.json(change_jobs([match[1]],match[2])[0]);return
                if match[2]=='cancel':self.json(cancel(match[1]));return
                with LOCK:
                    directory=JOBS/match[1];meta=read(directory/'job.json')
                    if not meta or meta['state'] not in ('failed','cancelled'):raise ValueError('此任务无需重试。')
                    meta['processing_seconds']=processing_seconds(meta)
                    meta.update(state='queued',hidden=False,max_attempts=meta.get('attempt',0)+2,
                        queue_order=max([time.time()]+[j.get('queue_order',j['created_at']) for j in jobs()])+.001)
                    meta.pop('finished_at',None);meta.pop('error',None)
                    save(directory/'job.json',meta)
                    save(directory/'progress.json',{'stage':'queued','message':'等待重新处理'})
                    WAKE.set()
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
    if args.desktop:server.desktop()
    http.serve_forever()

if __name__=='__main__':main()
