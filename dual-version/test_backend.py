"""Isolated HTTP/queue regression. No user data writes or model invocation."""
import json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error, unittest, wave
from pathlib import Path
ROOT=Path(__file__).parent
TEST=Path(os.environ.get('SING_LOCAL_TEST',str(ROOT.parent/'local-test')))
import uuid
fixture=ROOT/('api-test-'+uuid.uuid4().hex)
fixture.mkdir()
os.environ['SING_APP_ROOT']=str(fixture)
os.environ['SING_LOCAL_TEST']=str(TEST)
os.environ['SING_YUE2_HOME']=str(TEST/'yue2')
import service,server
server.PORT=18767
service.JOBS.mkdir(parents=True);service.TRACKS.mkdir()
service.save(service.DATA/'settings.json',{**service.settings(),'paused':True})
http=service.ThreadingHTTPServer(('127.0.0.1',server.PORT),service.Handler)
threading.Thread(target=http.serve_forever,daemon=True).start()
client=urllib.request.build_opener(urllib.request.ProxyHandler({}))

def request(route,body=None,headers=None):
    raw=json.dumps(body).encode() if isinstance(body,dict) else body
    h={'X-Sing-Token':service.TOKEN,**(headers or {})}
    req=urllib.request.Request(f'http://127.0.0.1:{server.PORT}'+route,data=raw,headers=h)
    with client.open(req,timeout=120) as r:
        data=r.read()
        return r.status,json.loads(data) if 'json' in r.headers.get('Content-Type','') else data

class Tests(unittest.TestCase):
    def test_01_security_and_batch_import(self):
        with self.assertRaises(urllib.error.HTTPError) as e:request('/api/settings',{}, {'X-Sing-Token':'wrong'})
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):request('/data/settings.json')
        # Twenty-one separate uploads exercise actual file decoding and persistence.
        audio=(TEST/'samples/fishin-30s.wav').read_bytes()
        for n in range(21):
            status,t=request('/api/import',audio,{'X-File-Name':f'QA-{n+1}.wav'})
            self.assertEqual(status,201);self.assertAlmostEqual(t['duration'],30,places=1)
        self.assertEqual(len(list(service.TRACKS.glob('*/track.json'))),21)
        track=request('/api/state')[1]['tracks'][-1]
        request('/api/tracks/'+track['id'],{'lyrics':'[Verse]\nLocal lyrics test','style':'Jazz','mode':'preserve'})
        self.assertEqual(service.track(track['id'])['mode'],'preserve')
        with self.assertRaises(urllib.error.HTTPError):request('/api/import',b'not an audio file',{'X-File-Name':'bad.wav'})
        deadline=time.time()+2
        while time.time()<deadline and len(list(service.TRACKS.iterdir()))!=21:time.sleep(.05)
        self.assertEqual(len(list(service.TRACKS.iterdir())),21)

    def test_02_batch_atomic_validation_and_idempotency(self):
        tracks=[t for t in request('/api/state')[1]['tracks'] if t['id']!='sample']
        items=[{'track_id':t['id'],'caption':'R&B full song','mode':'remix'} for t in tracks]
        with self.assertRaises(urllib.error.HTTPError):request('/api/batch',{'request_id':'invalid-batch-12345','items':items+[{'track_id':'missing'}]})
        self.assertEqual(service.jobs(),[])
        body={'request_id':'test-batch-123456789','items':items}
        first=request('/api/batch',body)[1]['jobs'];second=request('/api/batch',body)[1]['jobs']
        self.assertEqual([j['id'] for j in first],[j['id'] for j in second]);self.assertEqual(len(service.jobs()),21)
        self.assertEqual([j['id'] for j in reversed(service.jobs())],[j['id'] for j in first])
        self.assertTrue(all(j['engine']=='yue2' and j['duration']==30 for j in first))
        for j in first:request('/api/jobs/'+j['id']+'/cancel',{})
        self.assertTrue(all(j['state']=='cancelled' for j in service.jobs()))

    def test_03_retry_then_next_and_restart_recovery(self):
        # Run the production queue with a deterministic isolated subprocess fixture.
        (fixture/'engine.py').write_text('''import json,sys,time
from pathlib import Path
p=Path(sys.argv[1]);m=json.loads((p/'job.json').read_text());c=json.loads((p/'config.json').read_text())
if c['caption']=='always fail' or (c['caption']=='fail once' and m['attempt']==1):
 (p/'error.json').write_text(json.dumps({'error':'controlled test failure'}));sys.exit(1)
if c['caption']=='hold':time.sleep(90)
(p/'result.json').write_text(json.dumps({'files':{},'checks':{}}))
''',encoding='utf-8')
        first=service.create_job({'track_id':'sample','caption':'fail once','mode':'remix'})
        second=service.create_job({'track_id':'sample','caption':'always fail','mode':'remix'})
        third=service.create_job({'track_id':'sample','caption':'success','mode':'preserve'})
        threading.Thread(target=service.worker,daemon=True).start()
        request('/api/settings',{'paused':False})
        deadline=time.time()+45
        while time.time()<deadline and service.public_job(service.JOBS/third['id'])['state']!='completed':time.sleep(.2)
        a=service.public_job(service.JOBS/first['id']);b=service.public_job(service.JOBS/second['id']);c=service.public_job(service.JOBS/third['id'])
        self.assertEqual((a['state'],a['attempt']),('completed',2))
        self.assertEqual((b['state'],b['attempt']),('failed',2))
        self.assertEqual(c['state'],'completed');self.assertEqual(len(b['errors']),2)
        held=service.create_job({'track_id':'sample','caption':'hold','mode':'remix'})
        deadline=time.time()+10
        while time.time()<deadline and service.RUNNING!=held['id']:time.sleep(.1)
        request('/api/jobs/'+held['id']+'/cancel',{})
        self.assertEqual(service.public_job(service.JOBS/held['id'])['state'],'cancelled')
        request('/api/settings',{'paused':True})
        pending=service.create_job({'track_id':'sample','caption':'persisted','mode':'remix'})
        self.assertEqual(service.read(service.JOBS/pending['id']/'config.json')['engine'],'yue2')

    def test_04_cold_restart_recovers_interrupted_and_queued(self):
        interrupted=service.create_job({'track_id':'sample','caption':'restart test','mode':'remix'})
        interrupted.update(state='running',attempt=1)
        service.save(service.JOBS/interrupted['id']/'job.json',interrupted)
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        command=f"import service,server;server.PORT={port};service.main()"
        process=subprocess.Popen([sys.executable,'-c',command],cwd=ROOT,env=os.environ.copy(),creationflags=service.FLAGS)
        try:
            deadline=time.time()+15;state=None
            while time.time()<deadline:
                try:
                    with client.open(f'http://127.0.0.1:{port}/api/state',timeout=1) as response:state=json.load(response)
                    break
                except OSError:time.sleep(.2)
            self.assertIsNotNone(state)
            restored=next(j for j in state['jobs'] if j['id']==interrupted['id'])
            self.assertEqual(restored['state'],'queued');self.assertEqual(restored['attempt'],1)
            self.assertTrue(state['settings']['paused'])
            self.assertTrue(any(j['config']['caption']=='persisted' and j['state']=='queued' for j in state['jobs']))
            self.assertTrue(any(j['state']=='completed' for j in state['jobs']))
        finally:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=service.FLAGS,timeout=15)

    def test_05_dual_versions_and_settings(self):
        request('/api/settings',{'paused':True,'profile':'fast','gender':'female','tone':'husky','style_b':'Acoustic'})
        persisted=request('/api/state')[1]['settings']
        self.assertEqual((persisted['profile'],persisted['tone']),('fast','husky'))
        payload={'request_id':'paired-regression-123456','items':[{'track_id':'sample','profile':'fast','gender':'male','tone':'warm','variants':[
            {'style':'R&B','caption':'R&B performance'},{'style':'Jazz','caption':'Jazz performance'}]}]}
        pair=request('/api/batch',payload)[1]['jobs']
        again=request('/api/batch',payload)[1]['jobs']
        self.assertEqual([j['id'] for j in pair],[j['id'] for j in again])
        a,b=[service.read(service.JOBS/j['id']/'config.json') for j in pair]
        self.assertEqual(a['pair_id'],b['pair_id']);self.assertEqual(b['reuse_from'],pair[0]['id'])
        self.assertNotEqual(a['seed'],b['seed']);self.assertNotEqual(a['style'],b['style'])
        self.assertEqual((a['profile'],b['gender'],b['tone']),('fast','male','warm'))
        count=len(service.jobs());bad=json.loads(json.dumps(payload));bad['request_id']='bad-pair-regression-123';bad['items'][0]['variants'][1]['style']='R&B'
        with self.assertRaises(urllib.error.HTTPError):request('/api/batch',bad)
        self.assertEqual(len(service.jobs()),count)
        with self.assertRaises(urllib.error.HTTPError):request('/api/jobs',{'track_id':'sample','caption':'x','profile':'unknown'})
        preserve=request('/api/jobs',{'track_id':'sample','caption':'instrumental','mode':'preserve','gender':'female','tone':'husky'})[1]
        self.assertEqual(preserve['config']['gender'],'auto');self.assertEqual(preserve['config']['tone'],'natural')

    def test_06_remove_restore_favorite_and_retry(self):
        pair=[j for j in service.jobs() if j.get('pair_id')]
        ids=[j['id'] for j in pair]
        request('/api/queue/remove',{'ids':ids})
        self.assertTrue(all(service.read(service.JOBS/i/'job.json')['hidden'] for i in ids))
        self.assertTrue(all(service.read(service.JOBS/i/'job.json')['state']=='cancelled' for i in ids))
        old=service.public_job(service.JOBS/ids[0]);request('/api/jobs/'+ids[0]+'/retry',{})
        new=service.public_job(service.JOBS/ids[0]);self.assertEqual(new['pair_id'],old['pair_id']);self.assertEqual(new['state'],'queued');self.assertFalse(new['hidden'])
        request('/api/jobs/'+ids[0]+'/remove',{})
        done=next(j for j in service.jobs() if j['state']=='completed')
        request('/api/jobs/'+done['id']+'/favorite',{})
        request('/api/jobs/'+done['id']+'/remove',{})
        deleted=service.public_job(service.JOBS/done['id']);self.assertTrue(deleted['hidden']);self.assertTrue(deleted['favorite'])
        self.assertTrue((service.JOBS/done['id']/'result.json').exists())
        request('/api/jobs/'+done['id']+'/restore',{})
        self.assertFalse(service.public_job(service.JOBS/done['id'])['hidden'])
        with self.assertRaises(urllib.error.HTTPError):request('/api/queue/remove',{'ids':[ids[0],'../../invalid']})

    def test_07_processing_time_excludes_wait_and_downtime(self):
        from unittest.mock import patch
        with patch.object(service.time,'time',return_value=500):
            meta={'state':'running','started_at':100,'created_at':1,'processing_seconds':20,'attempt_started_at':490}
            self.assertEqual(service.processing_seconds(meta),30)
            service.close_clock(meta);meta['state']='queued'
            self.assertEqual(service.processing_seconds(meta),30)
            meta.update(state='running',attempt_started_at=450)
            service.close_clock(meta,460);meta['state']='failed'
            self.assertEqual(service.processing_seconds(meta),40)
            self.assertEqual(service.processing_seconds({'state':'completed','started_at':100,'finished_at':160}),60)
            self.assertEqual(service.processing_seconds({'state':'queued','created_at':1}),0)

    def test_08_analysis_reuse_and_prompt_profile(self):
        import engine,yue2_engine
        from unittest.mock import patch
        pair=[j for j in service.jobs() if j.get('pair_id')]
        first=next(j for j in pair if j['variant']=='A');second=next(j for j in pair if j['variant']=='B')
        a=service.JOBS/first['id'];b=service.JOBS/second['id']
        cfg=service.read(b/'config.json')
        service.save(a/'preprocessing.json',{'completed_at':1})
        service.save(a/'lyrics.json',{'text':'test lyrics'})
        (a/'melody.abc').write_text('X:1\nK:C\nCDEF|')
        self.assertTrue(engine.reuse_analysis(b,cfg));self.assertTrue((b/'melody.abc').exists())
        self.assertFalse(engine.reuse_analysis(b,{**cfg,'duration':12}))
        captured=[]
        class Backend:
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def transcribe(self,*args):raise AssertionError('Cached score should be reused')
            def synthesize(self,req):captured.append(req);return {},b'test fixture, not audio'
        with patch.object(yue2_engine,'Backend',Backend),patch('soundfile.info') as info:
            info.return_value.duration=cfg['duration'];yue2_engine.generate(b,cfg)
        self.assertEqual(captured[0]['steps'],6)
        self.assertIn('male vocals',captured[0]['style']);self.assertIn('warm, mellow',captured[0]['style'])
        self.assertNotIn('semantic',captured[0]);self.assertEqual(captured[0]['duration'],30)

try:
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    (ROOT/'API-TESTS.json').write_text(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'fixture':str(fixture),'note':'HTTP/import/persistence/retry tests use controlled worker; audio synthesis verified separately.'},indent=2))
finally:http.shutdown()
sys.exit(0 if result.wasSuccessful() else 1)
