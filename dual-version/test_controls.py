"""Regression of actual production request construction and HTTP persistence."""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

import generation_controls as controls

class ControlsTest(unittest.TestCase):
    def config(self, **kw):
        return {'controls_version':1,'mode':'remix','caption':'Jazz','duration':30,'seed':123,
            'change_strength':75,'melody_mode':'reference','gender':'female','tone':'husky',**kw}

    def test_strength_changes_real_guidance_with_same_seed_and_score(self):
        requests=[controls.build_request(self.config(change_strength=n),'K:C\nCDEF|','local lyrics',6) for n in (0,50,100)]
        self.assertEqual([r['cfg_scale'] for r in requests],[1,1.4,1.8])
        for r in requests:
            self.assertEqual(r['seed'],123)
            self.assertEqual(r['abc'],'K:C\nCDEF|')
            self.assertIn('female vocals',r['style'])
            self.assertIn('husky, raspy',r['style'])
            self.assertEqual(r['semantic_sampling'],{'min_tokens':750,'max_tokens':750})
            self.assertNotIn('semantic_tokens',r)

    def test_free_mode_removes_cached_reference_and_preserve_forces_it(self):
        free=controls.build_request(self.config(melody_mode='free'),'CACHED SCORE','lyrics',6)
        self.assertEqual((free['cot'],free['abc']),('off',''))
        keep=controls.build_request(self.config(mode='preserve',melody_mode='free'),'SCORE','lyrics',6)
        self.assertEqual((keep['cot'],keep['abc'],keep['lyrics']),('melody','SCORE','[Instrumental]'))
        self.assertNotIn('female',keep['style'])
        self.assertNotIn('husky',keep['style'])
        with self.assertRaises(ValueError):controls.build_request(self.config(),'','lyrics',6)

    def test_style_defaults_and_user_override_do_not_conflict(self):
        jazz=controls.build_caption('Jazz')
        dance=controls.build_caption('Remix')
        self.assertIn('brushed drums',jazz)
        self.assertIn('synth',dance)
        self.assertNotIn('piano',dance)
        override=controls.build_caption('Jazz','guitar only','no drums')
        self.assertEqual(override,'Jazz, guitar only, no drums')

    def test_invalid_values_are_rejected_and_legacy_is_stable(self):
        for value in (-1,101,True,'75',None,float('nan'),float('inf')):
            with self.subTest(value=value),self.assertRaises(ValueError):controls.validate_controls({'change_strength':value})
        self.assertEqual(controls.validate_controls({'change_strength':None},inherit=True),(None,''))
        self.assertEqual(controls.effective_controls({'strength':.65,'mode':'remix'})['cfg_scale'],1)

    def test_http_settings_tracks_and_batch_use_same_controls(self):
        with tempfile.TemporaryDirectory(prefix='sing-controls-') as tmp:
            os.environ['SING_APP_ROOT']=tmp
            import service,server
            service.JOBS.mkdir(parents=True);service.TRACKS.mkdir()
            http=service.ThreadingHTTPServer(('127.0.0.1',0),service.Handler)
            server.PORT=http.server_port
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def api(route,payload=None):
                request=urllib.request.Request(f'http://127.0.0.1:{server.PORT}'+route,
                    data=json.dumps(payload).encode() if payload is not None else None,
                    headers={'X-Sing-Token':service.TOKEN,'Content-Type':'application/json'})
                with client.open(request,timeout=10) as response:return json.load(response)
            try:
                service.save(service.DATA/'settings.json',{'instructions':controls.LEGACY_INSTRUCTIONS})
                self.assertEqual(api('/api/state')['settings']['instructions'],'')
                saved=api('/api/settings',{'instructions':controls.LEGACY_INSTRUCTIONS,'instructions_b':'drums only','change_strength':100,'melody_mode':'free','paused':True})
                self.assertEqual(api('/api/state')['settings'],saved)
                self.assertEqual(saved['instructions'],controls.LEGACY_INSTRUCTIONS)
                for value in (-1,101,'75',True):
                    with self.assertRaises(urllib.error.HTTPError):api('/api/settings',{'change_strength':value})
                    self.assertEqual(api('/api/state')['settings'],saved)
                tid='a'*32
                directory=service.TRACKS/tid;directory.mkdir()
                service.save(directory/'track.json',{'id':tid,'title':'Local test','duration':30,'source':'unused.wav'})
                record=api('/api/tracks/'+tid,{'change_strength':0,'melody_mode':'free','instructions':'guitar only'})
                self.assertEqual(record['change_strength'],0)
                payload={'request_id':'control-http-batch-12345','items':[{'track_id':tid,'instructions':record['instructions'],
                    'change_strength':record['change_strength'],'melody_mode':record['melody_mode'],
                    'variants':[{'style':'Jazz','variant_instructions':'airy voice'},{'style':'Remix','variant_instructions':'powerful voice','change_strength':100}]}]}
                with patch('yue2_engine.available',return_value=True):
                    jobs=api('/api/batch',payload)['jobs']
                    a,b=[service.read(service.JOBS/j['id']/'config.json') for j in jobs]
                    self.assertEqual(a['app_version'],controls.VERSION)
                    self.assertEqual((a['change_strength'],b['change_strength']),(0,100))
                    self.assertEqual((a['effective_controls']['cfg_scale'],b['effective_controls']['cfg_scale']),(1,1.8))
                    self.assertIn('guitar only',a['caption']);self.assertIn('powerful voice',b['caption'])
                    self.assertNotIn('piano',a['caption'])
                    self.assertNotEqual(a['seed'],b['seed'])
                    payload['request_id']='invalid-http-batch-12345';payload['items'][0]['variants'][1]['change_strength']=101
                    with self.assertRaises(urllib.error.HTTPError):api('/api/batch',payload)
                    self.assertEqual(len(service.jobs()),2)
                    self.assertEqual(api('/api/state')['release']['version'],controls.VERSION)
            finally:
                http.shutdown();http.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
