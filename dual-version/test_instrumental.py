"""Instrumental workflow, real audio export, and backwards-compatible downloads."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from urllib.parse import unquote
from unittest.mock import patch

import engine
import generation_controls as controls
import service
import server


class InstrumentalTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sing-instrumental-')
        self.root=Path(self.temp.name)
        self.paths=patch.multiple(service,ROOT=self.root,DATA=self.root/'data',
            JOBS=self.root/'data/jobs',TRACKS=self.root/'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True);service.TRACKS.mkdir()
        self.tid='a'*32
        directory=service.TRACKS/self.tid;directory.mkdir()
        service.save(directory/'track.json',{'id':self.tid,'title':'Justin Bieber - Baby',
            'duration':5,'source':'unused.wav','lyrics':'Local preserved lyrics','gender':'female','mode':'preserve'})

    def tearDown(self):
        self.paths.stop();self.temp.cleanup()

    def config(self,**extra):
        return {'engine':'yue2','mode':'remix','caption':controls.build_caption('Lo-fi'),
            'style':'Lo-fi','duration':5,'seed':123,'controls_version':1,'change_strength':75,
            'melody_mode':'reference','gender':'female','tone':'husky','instrumental':True,
            'lyrics':'must not reach engine','auto_lyrics':True,'source':'unused.wav','start':0,**extra}

    def test_instrumental_request_ignores_lyrics_and_voice_but_keeps_melody_choice(self):
        for melody in ('reference','free'):
            request=controls.build_request(self.config(melody_mode=melody),'SCORE','cached lyrics',6)
            self.assertEqual(request['lyrics'],'[Instrumental]')
            self.assertIn('instrumental only, no singing or vocals',request['style'])
            self.assertNotIn('female',request['style']);self.assertNotIn('husky',request['style'])
            self.assertNotIn('fresh performance',request['style'])
            self.assertEqual(request['abc'],'SCORE' if melody=='reference' else '')
            self.assertEqual(request['cot'],'melody' if melody=='reference' else 'off')
            self.assertEqual(request['cfg_scale'],1.6)
        vocal=controls.build_request(self.config(instrumental=False),'SCORE','lyrics',6)
        self.assertIn('female vocals',vocal['style']);self.assertEqual(vocal['lyrics'],'lyrics')

    def test_pipeline_skips_asr_and_vocal_extraction_even_with_stale_flags(self):
        stages=[]
        def subprocess_run(args,**kwargs):
            if '--stage' in args:
                stage=args[-1];stages.append(stage)
                if stage=='generate':(self.root/'generated.wav').write_bytes(b'fixture')
        with patch.object(engine.subprocess,'run',side_effect=subprocess_run):
            engine.run_pipeline(self.root,self.config(lyrics='',profile='quality'))
        self.assertEqual(stages,['generate','validate'])
        self.assertEqual(service.read(self.root/'lyrics.json')['source'],'instrumental')

    def test_http_toggle_mixed_batch_and_immutable_job_settings(self):
        http=service.ThreadingHTTPServer(('127.0.0.1',0),service.Handler)
        previous=server.PORT;server.PORT=http.server_port
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def api(route,payload=None):
            req=urllib.request.Request(f'http://127.0.0.1:{server.PORT}'+route,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={'X-Sing-Token':service.TOKEN,'Content-Type':'application/json'})
            with client.open(req,timeout=10) as r:return json.load(r)
        try:
            saved=api('/api/tracks/'+self.tid,{'instrumental':True})
            self.assertEqual(saved['lyrics'],'Local preserved lyrics')
            self.assertEqual((saved['mode'],saved['gender']),('preserve','female'))
            for bad in ('true',1,None):
                with self.assertRaises(urllib.error.HTTPError):api('/api/tracks/'+self.tid,{'instrumental':bad})
                self.assertTrue(service.track(self.tid)['instrumental'])
            with patch('yue2_engine.available',return_value=True):
                jobs=api('/api/batch',{'request_id':'mixed-instrumental-1234','items':[
                    {'track_id':self.tid,'mode':'preserve','gender':'female','tone':'husky','melody_mode':'free',
                     'instructions':'','variants':[{'style':'Lo-fi'},{'style':'Jazz'}]},
                    {'track_id':self.tid,'instrumental':False,'style':'Acoustic','instructions':'','lyrics':'vocal test'}]})['jobs']
                for job in jobs[:2]:
                    c=job['config']
                    self.assertEqual((c['instrumental'],c['auto_lyrics'],c['lyrics']),(True,False,''))
                    self.assertEqual((c['mode'],c['gender'],c['tone'],c['melody_mode']),('remix','auto','natural','free'))
                self.assertEqual(jobs[0]['output_name'],'Lo-fi Justin Bieber - Baby')
                self.assertEqual(jobs[1]['output_name'],'Jazz Justin Bieber - Baby')
                self.assertFalse(jobs[2]['config']['instrumental']);self.assertTrue(jobs[2]['config']['auto_lyrics'])
            restored=api('/api/tracks/'+self.tid,{'instrumental':False})
            self.assertEqual(restored['lyrics'],saved['lyrics'])
            self.assertEqual(restored['gender'],'female')
            self.assertTrue(api('/api/state')['jobs'][0]['config']['instrumental'] is False)
            self.assertTrue(service.read(service.JOBS/jobs[0]['id']/'config.json')['instrumental'])
        finally:
            http.shutdown();http.server_close();thread.join();server.PORT=previous

    def test_cache_does_not_cross_track_type(self):
        prior=service.JOBS/('b'*32);prior.mkdir()
        destination=service.JOBS/('c'*32);destination.mkdir()
        config=self.config(reuse_from='b'*32,pair_id='pair')
        service.save(prior/'config.json',{**config,'instrumental':False})
        service.save(prior/'preprocessing.json',{})
        service.save(prior/'lyrics.json',{'text':'old vocals'})
        self.assertFalse(engine.reuse_analysis(destination,config))
        service.save(prior/'config.json',config)
        (prior/'melody.abc').write_text('SCORE')
        self.assertTrue(engine.reuse_analysis(destination,config))
        self.assertTrue((destination/'melody.abc').exists())
        self.assertFalse((destination/'lyrics.json').exists())

    def test_safe_names_and_real_wav_mp3_export(self):
        import numpy as np
        import soundfile as sf
        self.assertEqual(controls.output_name('Lo-fi','Justin Bieber - Baby'),'Lo-fi Justin Bieber - Baby')
        self.assertEqual(controls.output_name('Jazz/Lo-fi','中文:歌曲?'),'Jazz_Lo-fi 中文_歌曲_')
        self.assertNotIn('/',controls.output_name('../','../bad'))
        self.assertLessEqual(len(controls.output_name('Jazz','🎵'*300).encode('utf-16-le'))//2,200)
        wave=.1*np.sin(2*np.pi*220*np.arange(240000)/48000)
        audio=np.column_stack((wave,wave))
        for filename in ('source.wav','result.wav'):sf.write(self.root/filename,audio,48000)
        config=self.config(output_name='Lo-fi Justin Bieber - Baby')
        engine.validate(self.root,config)
        result=service.read(self.root/'result.json')
        self.assertEqual(result['files']['result'],'Lo-fi Justin Bieber - Baby.wav')
        self.assertEqual(result['files']['mp3'],'Lo-fi Justin Bieber - Baby.mp3')
        self.assertTrue((self.root/result['files']['mp3']).is_file())
        self.assertAlmostEqual(result['checks']['result']['seconds'],5)

    def test_old_and_new_download_names_ranges_and_path_protection(self):
        http=service.ThreadingHTTPServer(('127.0.0.1',0),service.Handler)
        previous=server.PORT;server.PORT=http.server_port
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        directory=service.JOBS/('d'*32);directory.mkdir()
        service.save(directory/'job.json',{'id':'d'*32,'title':'中文 歌曲','style':'Lo-fi','state':'completed','created_at':1})
        original=(directory/'job.json').read_bytes()
        url=f'http://127.0.0.1:{server.PORT}/results/'+('d'*32)+'/result'
        try:
            for filename in ('result.wav','Lo-fi 中文 歌曲.wav'):
                (directory/filename).write_bytes(b'RIFFfixtureaudio')
                service.save(directory/'result.json',{'files':{'result':filename}})
                with client.open(urllib.request.Request(url,headers={'Range':'bytes=0-3'})) as r:
                    self.assertEqual(r.status,206);self.assertEqual(r.read(),b'RIFF')
                    self.assertIn('Lo-fi 中文 歌曲.wav',unquote(r.headers['Content-Disposition']))
                with client.open(urllib.request.Request(url,method='HEAD')) as r:self.assertEqual(r.read(),b'')
            self.assertEqual(service.public_job(directory)['output_name'],'Lo-fi 中文 歌曲')
            self.assertEqual((directory/'job.json').read_bytes(),original)
            for bad in ('../outside.wav','C:\\outside.wav','result.wav:secret','.wav/../../outside.wav'):
                service.save(directory/'result.json',{'files':{'result':bad}})
                with self.assertRaises(urllib.error.HTTPError) as error:client.open(url)
                self.assertEqual(error.exception.code,404)
        finally:
            http.shutdown();http.server_close();thread.join();server.PORT=previous


if __name__=='__main__':unittest.main(verbosity=2)
