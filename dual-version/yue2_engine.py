"""Local YuE2 GGUF cover adapter. One private, short-lived backend per job."""
import email.policy
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from email.parser import BytesParser
from pathlib import Path
from generation_controls import GENDERS, TONES, build_request, effective_controls

HOME = Path(os.environ.get('SING_YUE2_HOME', str(Path(__file__).resolve().parent.parent/'local-test/yue2')))
RUNTIME = HOME/'runtime'
MODELS = HOME/'models'
MODEL_NAMES = ('YuE2-3B-Q5_K_M.gguf', 'YuE2-Vae-F32.gguf', 'SheetSage2-Q5_K_M.gguf')
PROFILES={'fast':6,'balanced':16,'quality':32}

FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def available():
    return all(p.is_file() for p in [RUNTIME/'yue-server.exe', RUNTIME/'ggml-vulkan.dll', *[MODELS/n for n in MODEL_NAMES]])

class Backend:
    def __init__(self, directory, duration=30):
        self.directory = Path(directory)
        self.duration = duration
        self.process = None
        self.log = None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, route, body=None, content_type='application/json'):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(self.url+route, data=body, headers={'Content-Type':content_type})
        try:
            with self.http.open(req, timeout=30) as response:
                return response.headers.get('Content-Type', ''), response.read()
        except urllib.error.HTTPError as error:
            detail=error.read().decode('utf-8',errors='replace')[:1500]
            raise RuntimeError(f'YuE2 请求未被接受（{error.code}）：{detail}') from error

    def json(self, route, body=None):
        return json.loads(self.request(route, body)[1])

    def __enter__(self):
        if not available():
            raise RuntimeError('YuE2 本地模型或引擎不完整。')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.log = (self.directory/'yue2-backend.log').open('wb')
        env = {**os.environ, 'GGML_BACKEND':'Vulkan0'}
        env.pop('YUE_CUDA_BACKEND', None)
        torch_lib = Path(sys.prefix)/'Lib/site-packages/torch/lib'
        env['PATH'] = str(torch_lib)+os.pathsep+str(RUNTIME)+os.pathsep+env.get('PATH','')
        args = [str(RUNTIME/'yue-server.exe'), '--host','127.0.0.1','--port',str(port),
                '--model',str(MODELS/MODEL_NAMES[0]), '--vae',str(MODELS/MODEL_NAMES[1]),
                '--transcriber',str(MODELS/MODEL_NAMES[2]), '--max-seq',str(8192 if self.duration<=120 else 16384 if self.duration<=360 else 24576),
                '--max-batch','1', '--vae-core','128', '--clamp-fp16']
        try:
            self.process = subprocess.Popen(args, cwd=RUNTIME, env=env, stdout=self.log, stderr=subprocess.STDOUT, creationflags=FLAGS)
            deadline = time.monotonic()+90
            while time.monotonic()<deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('YuE2 Vulkan 后端启动失败，请查看 yue2-backend.log。')
                try:
                    if self.json('/health').get('status')=='ok':
                        return self
                except OSError:
                    pass
                time.sleep(.3)
            raise RuntimeError('YuE2 后端启动超时。')
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def __exit__(self, *args):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=10)
        if self.log:self.log.close()

    def wait(self, job, timeout=14400):
        identifier = job['id']
        deadline = time.monotonic()+timeout
        last_progress = ''
        while time.monotonic()<deadline:
            if self.process.poll() is not None:
                raise RuntimeError('YuE2 后端已退出，请查看 yue2-backend.log。')
            status = self.json('/job?id='+identifier)['status']
            if status=='done':return self.request('/job?id='+identifier+'&result=1')
            if status in ('failed','cancelled'):
                raise RuntimeError('YuE2 处理未完成：'+status+'；请查看 yue2-backend.log。')
            # Report real backend work, never a guessed completion percentage.
            try:
                with (self.directory/'yue2-backend.log').open('rb') as stream:
                    stream.seek(max(0,stream.seek(0,2)-3000))
                    tail=stream.read().decode('utf-8',errors='replace')
                matches=re.findall(r'\[(AR|NAR)\] (?:Semantic |Step )(\d+)/(\d+)',tail)
                if matches:
                    stage,current,total=matches[-1]
                    message={'AR':'音乐编码','NAR':'音频合成'}[stage]+f' · {current} / {total}'
                    if message!=last_progress:
                        from engine import write_json
                        write_json(self.directory/'progress.json',{'stage':'generating','message':'YuE2：'+message,'updated_at':time.time()})
                        last_progress=message
            except OSError:pass
            time.sleep(.5)
        self.json('/job?id='+identifier+'&cancel=1', {})
        raise RuntimeError('YuE2 处理超过时限。')

    def transcribe(self, path):
        boundary = 'SingStudio'+uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="melody_only"\r\n\r\n1\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="source.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode()+Path(path).read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
        job = json.loads(self.request('/transcribe',body,'multipart/form-data; boundary='+boundary)[1])
        score = json.loads(self.wait(job, timeout=1800)[1])['abc']
        if not score.strip():raise RuntimeError('YuE2 未能从原曲识别旋律。')
        return score

    def synthesize(self, request):
        content_type, data = self.wait(self.json('/synth', request))
        message = BytesParser(policy=email.policy.default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+data)
        replay, audio = None, None
        for part in message.iter_parts():
            if part.get_content_type()=='application/json':replay=json.loads(part.get_payload(decode=True))
            if part.get_content_type()=='audio/wav':audio=part.get_payload(decode=True)
        if not audio or replay is None:raise RuntimeError('YuE2 未返回音频与生成记录。')
        return replay, audio

def generate(directory, config):
    from engine import write_json
    directory = Path(directory)
    if not 5<=config['duration']<=600:
        raise ValueError('歌曲长度须为 5 秒至 10 分钟。')
    def progress(stage,message):write_json(directory/'progress.json',{'stage':stage,'message':message,'updated_at':time.time()})
    with Backend(directory, config['duration']) as backend:
        controls = effective_controls(config)
        score_path=directory/'melody.abc'
        score = ''
        if controls['melody_mode'] == 'reference':
            progress('transcribing','YuE2：正在识别原曲旋律')
            score=score_path.read_text(encoding='utf-8') if score_path.exists() else ''
            if not score.strip():score=backend.transcribe(directory/'source.wav')
            temporary=directory/'melody.new'
            temporary.write_text(score,encoding='utf-8');temporary.replace(score_path)
        lyric_info = json.loads((directory/'lyrics.json').read_text(encoding='utf-8')) if (directory/'lyrics.json').exists() else {}
        lyrics = config.get('lyrics','').strip() or lyric_info.get('text','')
        request = build_request(config, score, lyrics, PROFILES[config.get('profile','quality')])
        write_json(directory/'yue2-request.json',request)
        progress('generating','YuE2：'+('根据参考旋律改编' if controls['melody_mode']=='reference' else '按目标要求自由重创'))
        replay,audio = backend.synthesize(request)
        (directory/'generated.wav').write_bytes(audio)
        import soundfile as sf
        info=sf.info(directory/'generated.wav')
        if abs(info.duration-config['duration'])>.2:
            raise RuntimeError(f'YuE2 未生成完整长度：{info.duration:.2f} 秒 / {config["duration"]:.2f} 秒。不会用循环或静音填充。')
        write_json(directory/'yue2-replay.json',replay)
        write_json(directory/'generation.json',{'engine':'yue2','backend':'Vulkan','quantization':'Q5_K_M','settings':request,
            'app_version':config.get('app_version','legacy'),'effective_controls':controls,
            'notice':'风格、声音和歌词由模型生成；提高力度不保证完全遵从，请试听复核。'})
