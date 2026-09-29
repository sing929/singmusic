"""Local pipeline. Each GPU stage runs in a fresh process to release VRAM."""
import argparse
import json
import shutil
import re
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST = Path(os.environ.get('SING_LOCAL_TEST', str(ROOT.parent / 'local-test')))
SOURCE = TEST / 'ace-source' / 'ace-step-ACE-Step-1.5-ca1e85f'
PYTHON = TEST / 'ace-env/Scripts/python.exe'
FFMPEG = TEST / 'bin/ffmpeg.exe'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def write_json(path, data):
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(.05)
    temporary.replace(path)

def setup():
    for key, value in {
        'ACESTEP_CHECKPOINTS_DIR': str(TEST/'checkpoints'),
        'HF_HOME': str(TEST/'cache/huggingface'),
        'HF_HUB_OFFLINE':'1', 'TRANSFORMERS_OFFLINE':'1',
        'HF_HUB_DISABLE_TELEMETRY':'1', 'GRADIO_ANALYTICS_ENABLED':'False',
        'ACESTEP_INIT_LLM':'false', 'ACESTEP_SAVE_MEMORY':'1',
        'MPLCONFIGDIR':str(TEST/'cache/matplotlib'),
        'NUMBA_CACHE_DIR':str(TEST/'cache/numba'),
        'TORCHINDUCTOR_CACHE_DIR':str(TEST/'cache/inductor'),
        'TRITON_CACHE_DIR':str(TEST/'cache/triton'),
        'PYTHONUTF8':'1',
    }.items():
        os.environ[key]=value
    os.environ['PATH']=str(TEST/'bin')+os.pathsep+os.environ['PATH']
    sys.path.insert(0,str(SOURCE))

def separate(source, destination):
    import torch
    import onnxruntime as ort
    from audio_separator.separator import Separator
    original_session = ort.InferenceSession
    def gpu_session(*args, **kwargs):
        session=original_session(*args, **kwargs)
        if 'CUDAExecutionProvider' not in session.get_providers():
            raise RuntimeError('UVR 未能使用显卡。请检查本地运行库，未自动切换到 CPU。')
        return session
    ort.InferenceSession=gpu_session
    class OfflineSeparator(Separator):
        def download_file_if_not_exists(self, url, output_path):
            if not Path(output_path).is_file():
                raise RuntimeError('缺少本地分离模型或配置：'+Path(output_path).name)
    destination.mkdir(parents=True,exist_ok=True)
    separator=OfflineSeparator(model_file_dir=str(TEST/'uvr-models'),output_dir=str(destination),
        output_format='WAV',use_soundfile=True,
        mdx_params={'hop_length':1024,'segment_size':256,'overlap':.25,'batch_size':1,'enable_denoise':False})
    separator.load_model(model_filename='UVR-MDX-NET-Inst_HQ_3.onnx')
    files=separator.separate(str(source))
    stems={}
    for file in files:
        path=Path(file)
        if not path.is_absolute():path=destination/path
        if '(Vocals)' in file:stems['vocals']=str(path)
        if '(Instrumental)' in file:stems['instrumental']=str(path)
    if set(stems)!={'vocals','instrumental'}:raise RuntimeError('人声分离未返回完整分轨。')
    write_json(destination/'stems.json',stems)

def generate(jobdir, config):
    if config.get('engine')=='yue2':
        from yue2_engine import generate as generate_yue2
        return generate_yue2(jobdir,config)
    import torch
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationParams,GenerationConfig,generate_music
    class CompatibleHandler(AceStepHandler):
        def _validate_quantization_setup(self,**kwargs):
            self.dtype=torch.float32
            return super()._validate_quantization_setup(**kwargs)
        def _get_vae_dtype(self,*args,**kwargs):return torch.float32
    handler=CompatibleHandler()
    status,ok=handler.initialize_service(project_root=str(SOURCE),config_path='acestep-v15-turbo',
        device='cuda',use_flash_attention=False,compile_model=False,offload_to_cpu=True,
        offload_dit_to_cpu=True,quantization='int8_weight_only')
    if not ok:raise RuntimeError(status)
    preserve=config['mode']=='preserve'
    source=jobdir/'source.wav'
    if preserve:
        stems=json.loads((jobdir/'original-stems/stems.json').read_text(encoding='utf-8'))
        source=Path(stems['instrumental'])
    caption=config['caption']
    if preserve:caption+=' Instrumental arrangement only, no vocals, preserve the reference chord progression and timing.'
    params=GenerationParams(task_type='cover',src_audio=str(source),caption=caption,
        lyrics='[Instrumental]' if preserve else config['lyrics'],instrumental=preserve,
        vocal_language='unknown',duration=config['duration'],inference_steps=8,seed=config['seed'],
        thinking=False,use_cot_metas=False,use_cot_caption=False,use_cot_language=False,
        audio_cover_strength=(1-config['strength']*.45) if preserve else (1-config['strength']))
    result=generate_music(handler,None,params,GenerationConfig(batch_size=1,use_random_seed=False,
        seeds=[config['seed']],audio_format='wav'),save_dir=str(jobdir/'generated'))
    if not result.success or not result.audios:raise RuntimeError(result.error or result.status_message)
    output=Path(result.audios[0]['path'])
    output.replace(jobdir/'generated.wav')
    write_json(jobdir/'generation.json',{'settings':params.to_dict(),'peak_allocated_mib':torch.cuda.max_memory_allocated()/2**20})

def align_and_mix(jobdir):
    import numpy as np
    import soundfile as sf
    import librosa
    from scipy.ndimage import median_filter
    original=json.loads((jobdir/'original-stems/stems.json').read_text(encoding='utf-8'))
    generated=json.loads((jobdir/'generated-stems/stems.json').read_text(encoding='utf-8'))
    sr=48000
    def read(path):
        audio,rate=sf.read(path,always_2d=True,dtype='float32')
        if rate!=sr:audio=librosa.resample(audio.T,orig_sr=rate,target_sr=sr).T
        return audio
    reference=read(original['instrumental']);vocals=read(original['vocals']);new=read(generated['instrumental'])
    length=len(vocals);reference=librosa.util.fix_length(reference,size=length,axis=0)
    new=librosa.util.fix_length(new,size=length,axis=0)
    # Align a generated accompaniment to the original performance. Never warp vocals.
    low_sr=12000;hop=512
    ref_mono=librosa.resample(reference.mean(axis=1),orig_sr=sr,target_sr=low_sr)
    new_mono=librosa.resample(new.mean(axis=1),orig_sr=sr,target_sr=low_sr)
    ref_chroma=librosa.feature.chroma_stft(y=ref_mono,sr=low_sr,hop_length=hop)
    new_chroma=librosa.feature.chroma_stft(y=new_mono,sr=low_sr,hop_length=hop)
    ref_avg=ref_chroma.mean(axis=1);new_avg=new_chroma.mean(axis=1)
    def cosine(a,b):return float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-9))
    shift_scores=[cosine(ref_avg,np.roll(new_avg,s)) for s in range(12)]
    best=int(np.argmax(shift_scores));shift=best if best<=6 else best-12
    applied_shift=shift if shift_scores[best]-shift_scores[0]>.08 else 0
    if applied_shift:
        new=librosa.effects.pitch_shift(new.T,sr=sr,n_steps=applied_shift).T
        new_chroma=np.roll(new_chroma,applied_shift,axis=0)
    _,path=librosa.sequence.dtw(X=ref_chroma,Y=new_chroma,metric='cosine',global_constraints=True,band_rad=.12)
    path=path[::-1]
    rframes=np.unique(path[:,0]);gframes=np.array([np.median(path[path[:,0]==i,1]) for i in rframes])
    duration=length/sr
    rt=np.clip(rframes*hop/low_sr,0,duration);gt=np.clip(gframes*hop/low_sr,0,duration)
    gt=median_filter(gt,size=9)
    boundaries=np.linspace(0,duration,max(2,int(np.ceil(duration/2.0))+1))
    mapped=np.interp(boundaries,rt,gt)
    mapped[0]=0;mapped[-1]=duration
    # Limit local stretching to reduce audible artifacts in ambiguous matches.
    aligned=np.zeros_like(vocals);weights=np.zeros(length,dtype=np.float32);rates=[]
    margin=int(.07*sr)
    for i in range(len(boundaries)-1):
        ta=int(boundaries[i]*sr);tb=min(length,int(boundaries[i+1]*sr))
        sa=int(mapped[i]*sr);sb=int(mapped[i+1]*sr)
        raw_rate=(sb-sa)/max(1,tb-ta)
        rate=float(np.clip(raw_rate,.8,1.25));rates.append(rate)
        oa=max(0,ta-margin);ob=min(length,tb+margin)
        source_start=max(0,sa-int((ta-oa)*rate));source_end=min(len(new),source_start+int((ob-oa)*rate))
        chunk=new[source_start:source_end]
        if len(chunk)<2048:chunk=new[oa:ob];rate=1.
        warped=librosa.effects.time_stretch(chunk.T,rate=rate).T
        warped=librosa.util.fix_length(warped,size=ob-oa,axis=0)
        weight=np.ones(ob-oa,dtype=np.float32)
        if oa<ta:weight[:ta-oa]=np.linspace(0,1,ta-oa)
        if ob>tb:weight[-(ob-tb):]=np.linspace(1,0,ob-tb)
        aligned[oa:ob]+=warped*weight[:,None];weights[oa:ob]+=weight
    aligned/=np.maximum(weights[:,None],1e-5)
    mix=vocals+aligned*.8
    gain=min(1.,.94/max(float(np.max(np.abs(mix))),1e-6))
    sf.write(jobdir/'vocals.wav',vocals,sr,subtype='PCM_24')
    sf.write(jobdir/'instrumental.wav',aligned,sr,subtype='PCM_24')
    sf.write(jobdir/'result.wav',mix*gain,sr,subtype='PCM_24')
    write_json(jobdir/'alignment.json',{'method':'chroma DTW with bounded local stretch; original vocal timing unchanged',
        'pitch_shift_semitones':applied_shift,'min_stretch':min(rates),'max_stretch':max(rates),
        'mix_gain':gain,'vocal_timing_changed':False,
        'notice':'已保留原始分离人声，并对新伴奏做自动对齐。请试听复核节拍、和声及分离残留。'})

def validate(jobdir,config):
    import numpy as np
    import soundfile as sf
    files={'result':'result.wav','original':'source.wav'}
    if config['mode']=='preserve':files.update(vocals='vocals.wav',instrumental='instrumental.wav')
    checks={}
    for kind,name in files.items():
        data,sr=sf.read(jobdir/name,always_2d=True)
        duration=len(data)/sr;rms=float(np.sqrt(np.mean(data**2)));peak=float(np.max(np.abs(data)))
        duration_ok=abs(duration-config['duration'])<=.2
        if not np.isfinite(data).all() or (rms<1e-5 and kind!='vocals') or sr!=48000 or data.shape[1]!=2 or not duration_ok:
            raise RuntimeError('输出音频未通过完整性检查：'+kind)
        checks[kind]={'seconds':duration,'sample_rate':sr,'channels':data.shape[1],'rms':rms,'peak':peak}
    subprocess.run([str(FFMPEG),'-nostdin','-y','-v','error','-i',str(jobdir/'result.wav'),'-c:a','libmp3lame','-b:a','320k',str(jobdir/'result.mp3')],check=True,creationflags=CREATE_NO_WINDOW,timeout=180)
    files['mp3']='result.mp3'
    write_json(jobdir/'result.json',{'files':files,'checks':checks,
        'notice':'保留原唱模式已做自动对齐，仍需试听复核。' if config['mode']=='preserve' else ('YuE2 按歌词和目标要求自由重创；旋律、段落与声音可能变化，请试听复核。' if config.get('melody_mode')=='free' else 'YuE2 根据识别的旋律重新演绎；人声与歌词可能变化，请试听复核。')})

def reuse_analysis(jobdir,config):
    """Only reuse preprocessing of this pair's identical source; never generated audio or semantic tokens."""
    identifier=config.get('reuse_from','')
    if not re.fullmatch('[0-9a-f]{32}',identifier):return False
    prior=jobdir.parent/identifier
    try:
        old=json.loads((prior/'config.json').read_text(encoding='utf-8'))
        if any(old.get(k)!=config.get(k) for k in ('source','start','duration','lyrics','mode','pair_id')):return False
        if not (prior/'preprocessing.json').exists():return False
        for name in ('lyrics.json','melody.abc'):
            if (prior/name).exists():shutil.copy2(prior/name,jobdir/name)
        stems=prior/'original-stems'
        if (stems/'stems.json').exists():
            target=jobdir/'original-stems';shutil.copytree(stems,target,dirs_exist_ok=True)
            old_stems=json.loads((stems/'stems.json').read_text(encoding='utf-8'))
            write_json(target/'stems.json',{k:str(target/Path(v).name) for k,v in old_stems.items()})
        write_json(jobdir/'reused-analysis.json',{'from':identifier,'reused_at':time.time()})
        return True
    except (OSError,ValueError):return False

def run_pipeline(jobdir,config):
    def progress(stage,message):write_json(jobdir/'progress.json',{'stage':stage,'message':message,'updated_at':time.time()})
    def stage(name):
        subprocess.run([str(PYTHON),str(Path(__file__)),str(jobdir),'--stage',name],check=True,
            cwd=str(ROOT),creationflags=CREATE_NO_WINDOW,timeout=max(3600,int(config["duration"]*40)))
    progress('preparing','正在准备整首音频')
    subprocess.run([str(FFMPEG),'-nostdin','-y','-v','error','-ss',str(config['start']),'-i',config['source'],
        '-t',str(config['duration']),'-ac','2','-ar','48000','-c:a','pcm_s16le',str(jobdir/'source.wav')],
        check=True,creationflags=CREATE_NO_WINDOW,timeout=120)
    reused=reuse_analysis(jobdir,config)
    if config['mode']=='preserve' and not reused:
        progress('separating','正在分离原唱与伴奏');stage('separate-original')
    elif not reused and config.get('profile','quality')!='fast' and not config.get('lyrics','').strip() and config.get('auto_lyrics',True):
        progress('separating','正在提取人声以识别歌词')
        try:stage('separate-original')
        except subprocess.CalledProcessError:
            # Lyrics are optional. Use the original mix if vocal extraction fails.
            (jobdir/'error.json').unlink(missing_ok=True)
    if not reused and not config.get('lyrics','').strip() and config.get('auto_lyrics',True):
        progress('lyrics','正在本机识别歌词')
        stage('lyrics')
    write_json(jobdir/'preprocessing.json',{'completed_at':time.time()})
    progress('generating','正在本机生成新版本');stage('generate')
    if config['mode']=='preserve':
        progress('cleaning','正在清理新伴奏中的人声');stage('separate-generated')
        progress('aligning','正在对齐新伴奏并混入原唱');stage('align')
    else:
        (jobdir/'generated.wav').replace(jobdir/'result.wav')
    progress('validating','正在检查音频并保存作品');stage('validate')
    progress('complete','作品已完成')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('jobdir');parser.add_argument('--stage');args=parser.parse_args()
    directory=Path(args.jobdir).resolve();config=json.loads((directory/'config.json').read_text(encoding='utf-8'))
    setup()
    try:
        if args.stage=='lyrics':
            from lyrics import recognize
            recognize(directory,config,TEST)
        elif args.stage=='generate':generate(directory,config)
        elif args.stage=='separate-original':separate(directory/'source.wav',directory/'original-stems')
        elif args.stage=='separate-generated':separate(directory/'generated.wav',directory/'generated-stems')
        elif args.stage=='align':align_and_mix(directory)
        elif args.stage=='validate':validate(directory,config)
        else:run_pipeline(directory,config)
    except Exception:
        error=traceback.format_exc();print(error,flush=True)
        if args.stage or not (directory/'error.json').exists():
            write_json(directory/'error.json',{'error':error,'stage':args.stage or 'pipeline'})
        sys.exit(1)
