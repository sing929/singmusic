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

DERIVED_NOTICES={'adjust':'已在本机对原作品变调变速，未重新生成；请试听复核音质。',
    'dj':'已在本机按检测到的节拍给原作品加上 DJ 效果，未重新生成；请试听复核。',
    'mix':'已在本机把所选作品按节拍对齐，混成一首不间断的 DJ 串烧，未重新生成；请试听复核过渡。',
    'suite':'已在本机把三个管弦版本在安静处衔接成一首组曲，未重新生成；请试听复核衔接。'}

def audio_stats(path):
    """(seconds, rate, channels, rms, peak, finite) read in blocks, so hour-long mixes fit in memory."""
    import numpy as np
    import soundfile as sf
    info=sf.info(path);energy=0.;count=0;peak=0.;finite=True
    for block in sf.blocks(str(path),blocksize=1<<20,always_2d=True,dtype='float64'):
        finite=finite and bool(np.isfinite(block).all())
        energy+=float(np.square(block).sum());count+=block.size
        if block.size:peak=max(peak,float(np.max(np.abs(block))))
    return info.frames/info.samplerate,info.samplerate,info.channels,(energy/count)**.5 if count else 0.,peak,finite

def validate(jobdir,config,seconds=None):
    """Check every output file, write the MP3 and result.json. `seconds` replaces the queued length
    for works whose length is only known once made (DJ edits, mixes, suites)."""
    derived=config.get('kind') in DERIVED_NOTICES
    expected=config['duration'] if seconds is None else seconds
    files={'result':'result.wav'} if derived else {'result':'result.wav','original':'source.wav'}
    if config['mode']=='preserve' and not derived:files.update(vocals='vocals.wav',instrumental='instrumental.wav')
    checks={}
    for kind,name in files.items():
        duration,sr,channels,rms,peak,finite=audio_stats(jobdir/name)
        duration_ok=abs(duration-expected)<=.2
        if not finite or (rms<1e-5 and kind!='vocals') or sr!=48000 or channels!=2 or not duration_ok:
            raise RuntimeError('输出音频未通过完整性检查：'+kind)
        checks[kind]={'seconds':duration,'sample_rate':sr,'channels':channels,'rms':rms,'peak':peak}
    subprocess.run([str(FFMPEG),'-nostdin','-y','-v','error','-i',str(jobdir/'result.wav'),'-c:a','libmp3lame','-b:a','320k',str(jobdir/'result.mp3')],check=True,creationflags=CREATE_NO_WINDOW,timeout=max(180,int(expected/4)))
    files['mp3']='result.mp3'
    if config.get('output_name'):
        from generation_controls import output_name
        # Leave room for the complete Windows path and extension.
        limit=max(20,min(200,240-len(str(jobdir).encode('utf-16-le'))//2-5))
        stem=output_name('',config['output_name'],limit)
        for kind,ext in (('result','.wav'),('mp3','.mp3')):
            filename=stem+ext
            (jobdir/files[kind]).replace(jobdir/filename)
            files[kind]=filename
    notice=DERIVED_NOTICES[config['kind']] if derived else ('已按纯音乐要求生成，跳过歌词识别；请试听复核。'+('自由重创允许旋律及段落变化。' if config.get('melody_mode')=='free' else '')) if config.get('instrumental') else ('保留原唱模式已做自动对齐，仍需试听复核。' if config['mode']=='preserve' else ('YuE2 按歌词和目标要求自由重创；旋律、段落与声音可能变化，请试听复核。' if config.get('melody_mode')=='free' else 'YuE2 根据识别的旋律重新演绎；人声与歌词可能变化，请试听复核。'))
    swap=json.loads((jobdir/'voice-swap.json').read_text(encoding='utf-8')) if config.get('voice') and not derived and (jobdir/'voice-swap.json').exists() else None
    if swap:notice+=swap['notice']
    kid=json.loads((jobdir/'kid-voice.json').read_text(encoding='utf-8')) if config.get('kid') and not derived and (jobdir/'kid-voice.json').exists() else None
    if kid:notice+=kid['notice']
    write_json(jobdir/'result.json',{'files':files,'checks':checks,'notice':notice})

def reuse_analysis(jobdir,config):
    """Only reuse preprocessing of this pair's identical source; never generated audio or semantic tokens."""
    identifier=config.get('reuse_from','')
    if not re.fullmatch('[0-9a-f]{32}',identifier):return False
    prior=jobdir.parent/identifier
    try:
        old=json.loads((prior/'config.json').read_text(encoding='utf-8'))
        if any(old.get(k)!=config.get(k) for k in ('source','start','duration','lyrics','mode','pair_id','adjust')):return False
        if bool(old.get('instrumental'))!=bool(config.get('instrumental')):return False
        if not (prior/'preprocessing.json').exists():return False
        for name in ('lyrics.json','melody.abc'):
            if name=='lyrics.json' and config.get('instrumental'):continue
            if (prior/name).exists():shutil.copy2(prior/name,jobdir/name)
        # The voice-swap reference may come from the un-shifted original (reference-stems).
        for name in ('original-stems','reference-stems'):
            stems=prior/name
            if (stems/'stems.json').exists():
                target=jobdir/name;shutil.copytree(stems,target,dirs_exist_ok=True)
                old_stems=json.loads((stems/'stems.json').read_text(encoding='utf-8'))
                write_json(target/'stems.json',{k:str(target/Path(v).name) for k,v in old_stems.items()})
        write_json(jobdir/'reused-analysis.json',{'from':identifier,'reused_at':time.time()})
        return True
    except (OSError,ValueError):return False

def run_adjustment(jobdir,config):
    """A derived work: key/tempo/reverb applied to a finished result, never regenerated."""
    import audio_adjust
    def progress(stage,message):write_json(jobdir/'progress.json',{'stage':stage,'message':message,'updated_at':time.time()})
    source=Path(config['source'])
    if not source.is_file():raise RuntimeError('找不到原作品音频，无法变调变速。')
    progress('adjusting','正在本机变调变速：'+audio_adjust.label(config['adjust']))
    report=audio_adjust.render(source,jobdir/'result.wav',config['adjust'],FFMPEG,
        target_frames=round(config['duration']*audio_adjust.RATE))
    write_json(jobdir/'adjust.json',{'result':report})
    progress('validating','正在检查音频并保存作品');validate(jobdir,config)
    progress('complete','作品已完成')

def run_effect(jobdir,config):
    """A DJ edit, a non-stop DJ mix or an orchestral suite made from finished works, never regenerated."""
    def progress(stage,message):write_json(jobdir/'progress.json',{'stage':stage,'message':message,'updated_at':time.time()})
    kind=config['kind']
    sources=[Path(s['path']) for s in config.get('sources',[])] or [Path(config['source'])]
    for path in sources:
        if not path.is_file():raise RuntimeError('找不到原作品音频：'+path.name)
    if kind=='dj':
        import dj_tools
        report=dj_tools.render_effects(sources[0],jobdir/'result.wav',config['effects'],progress)
    elif kind=='mix':
        import dj_tools
        report=dj_tools.render_mix(config['sources'],jobdir/'result.wav',config['order'],config['transition_beats'],FFMPEG,progress)
    else:
        import suite
        report=suite.render(sources,jobdir/'result.wav',progress)
    write_json(jobdir/f'{kind}.json',report)
    progress('validating','正在检查音频并保存作品');validate(jobdir,config,seconds=report['output_seconds'])
    progress('complete','作品已完成')

def run_pipeline(jobdir,config):
    if config.get('kind')=='adjust':return run_adjustment(jobdir,config)
    if config.get('kind') in ('dj','mix','suite'):return run_effect(jobdir,config)
    import audio_adjust
    def progress(stage,message):write_json(jobdir/'progress.json',{'stage':stage,'message':message,'updated_at':time.time()})
    def stage(name):
        subprocess.run([str(PYTHON),str(Path(__file__)),str(jobdir),'--stage',name],check=True,
            cwd=str(ROOT),creationflags=CREATE_NO_WINDOW,timeout=max(3600,int(config["duration"]*40)))
    voice=config.get('voice')
    if voice:
        import voice_swap
        # Fail before any GPU time is spent when the separate engine is missing.
        if not voice_swap.available():raise RuntimeError('换声引擎（Seed-VC）未安装或未通过自检，无法换成原唱音色。')
    progress('preparing','正在准备整首音频')
    subprocess.run([str(FFMPEG),'-nostdin','-y','-v','error','-ss',str(config['start']),'-i',config['source'],
        '-t',str(config.get('source_duration',config['duration'])),'-ac','2','-ar','48000','-c:a','pcm_s16le',str(jobdir/'source.wav')],
        check=True,creationflags=CREATE_NO_WINDOW,timeout=120)
    adjust=config.get('adjust');reports={}
    # Shift the reference itself, so separation, melody and YuE2 all follow the new key and tempo.
    if audio_adjust.changes_source(adjust):
        # The target timbre is the singer's real voice, before any key or tempo change.
        if voice:shutil.copy2(jobdir/'source.wav',jobdir/'source-unshifted.wav')
        progress('adjusting','正在变调变速原曲：'+audio_adjust.label(adjust,reverb=False))
        reports['source']=audio_adjust.render(jobdir/'source.wav',jobdir/'source.wav',adjust,FFMPEG,
            target_frames=round(config['duration']*audio_adjust.RATE),include_reverb=False,subtype='PCM_16')
        write_json(jobdir/'adjust.json',reports)
    reused=reuse_analysis(jobdir,config)
    if config['mode']=='preserve' and not reused:
        progress('separating','正在分离原唱与伴奏');stage('separate-original')
    elif not reused and not config.get('instrumental') and config.get('profile','quality')!='fast' and not config.get('lyrics','').strip() and config.get('auto_lyrics',True):
        progress('separating','正在提取人声以识别歌词')
        try:stage('separate-original')
        except subprocess.CalledProcessError:
            # Lyrics are optional. Use the original mix if vocal extraction fails.
            (jobdir/'error.json').unlink(missing_ok=True)
    if config.get('instrumental'):
        write_json(jobdir/'lyrics.json',{'text':'','source':'instrumental','notice':'纯音乐：已跳过歌词读取与识别。'})
    elif not reused and not config.get('lyrics','').strip() and config.get('auto_lyrics',True):
        progress('lyrics','正在本机识别歌词')
        stage('lyrics')
    if voice:
        stems=jobdir/('reference-stems' if (jobdir/'source-unshifted.wav').exists() else 'original-stems')
        if not (stems/'stems.json').exists():
            progress('separating','正在分离原唱人声，作为换声的目标音色')
            stage('separate-reference' if stems.name=='reference-stems' else 'separate-original')
        progress('separating','正在挑选原唱音色参考片段');voice_swap.prepare_reference(jobdir,stems)
    write_json(jobdir/'preprocessing.json',{'completed_at':time.time()})
    progress('generating','正在本机生成新版本');stage('generate')
    if config['mode']=='preserve':
        progress('cleaning','正在清理新伴奏中的人声');stage('separate-generated')
        progress('aligning','正在对齐新伴奏并混入原唱');stage('align')
    elif voice:
        progress('cleaning','正在分离新生成的人声与伴奏');stage('separate-generated')
        progress('voice','正在换成原唱音色');voice_swap.convert(jobdir,config,progress)
    elif config.get('kid'):
        import kid_voice
        progress('cleaning','正在分离新生成的人声与伴奏');stage('separate-generated')
        progress('kid','正在把新歌声变成童声');kid_voice.convert(jobdir,config,progress,FFMPEG)
    else:
        (jobdir/'generated.wav').replace(jobdir/'result.wav')
    if adjust and adjust['reverb']:
        progress('effects','正在添加混响')
        reports['result']=audio_adjust.render(jobdir/'result.wav',jobdir/'result.wav',{**audio_adjust.DEFAULT,'reverb':adjust['reverb']},
            FFMPEG,target_frames=round(config['duration']*audio_adjust.RATE))
        write_json(jobdir/'adjust.json',reports)
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
        elif args.stage=='separate-reference':separate(directory/'source-unshifted.wav',directory/'reference-stems')
        elif args.stage=='align':align_and_mix(directory)
        elif args.stage=='validate':validate(directory,config)
        else:run_pipeline(directory,config)
    except Exception:
        error=traceback.format_exc();print(error,flush=True)
        if args.stage or not (directory/'error.json').exists():
            write_json(directory/'error.json',{'error':error,'stage':args.stage or 'pipeline'})
        sys.exit(1)
