"""Release packs: store names, Ditto lyric format, AI-use answers, review rules, the cover, the 44.1 kHz /
16-bit export with the real FFmpeg, and the release routes from a draft to a submitted EP."""
import datetime
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import numpy as np
import soundfile as sf

import release_pack
import server
import service

SR = 48000
TODAY = datetime.date(2026, 10, 24)


def tone(seconds, pitch=220.):
    t = np.arange(int(seconds * SR)) / SR
    audio = .3 * np.sin(2 * np.pi * pitch * t) * (1 + .5 * np.sin(2 * np.pi * .25 * t))
    return np.stack([audio, audio * .9], axis=1)


def release(**fields):
    return {'id': 'r' * 32, 'distributor': 'ditto', 'title': 'Summer Wind', 'artist': 'Boss Sing', 'genre': 'Pop', 'language': 'zh',
        'copyright_holder': 'Boss Sing', 'copyright_year': 2026, 'release_date': '2026-11-20', 'tracks': [], 'created_at': 2, **fields}


class NamingTest(unittest.TestCase):
    def test_titles_versions_and_lyrics(self):
        self.assertEqual(release_pack.title_case('the fishing song of the summer'), 'The Fishing Song of the Summer')
        self.assertEqual(release_pack.title_case('LET IT GO'), 'Let It Go')
        self.assertEqual(release_pack.title_case('journey: the best of me'), 'Journey: The Best of Me')
        self.assertEqual(release_pack.title_case('DJ 夏天 iPhone'), 'DJ 夏天 iPhone')
        self.assertEqual(release_pack.song_title('01 - my old friend'), 'My Old Friend')
        self.assertEqual([release_pack.style_version(s) for s in ('House', 'Jazz', '慢摇 Manyao', 'Remix', 'Cinematic Orchestra')],
            ['House Remix', 'Jazz Version', 'Manyao Remix', 'Remix', 'Cinematic Orchestra Version'])
        self.assertEqual(release_pack.version_text({'style': 'Lo-fi', 'kid': True, 'adjust_history': [{'preset': 'Speed Up'}], 'dj_history': [['echo']]}),
            'Lo-fi Version, Kid Vocal, Sped Up, DJ Edit')
        self.assertEqual(release_pack.version_text({'kind': 'mix', 'style': 'DJ Mix'}), 'DJ Mix')
        text, notes = release_pack.format_lyrics('[Verse 1]\n[00:12.10]hello world,\nI LOVE YOU SO MUCH.\n\n\n(Chorus x2)\nsing it again (x2)\n(oh oh)\n副歌：\n我爱你。\n')
        self.assertEqual(text, 'Hello world\nI love you so much\n\nSing it again\nSing it again\n(Oh oh)\n\n我爱你')
        self.assertEqual(len(notes), 1)
        self.assertEqual(release_pack.display_title({'title': 'Wind', 'version': 'Jazz Version'}), 'Wind (Jazz Version)')
        self.assertEqual(release_pack.safe_name('a/b: c?'), 'a_b_ c_')

    def test_ai_answers_follow_how_each_track_was_made(self):
        credits = release_pack.credits
        self.assertEqual(credits([{'mode': 'preserve'}]), {'composer': 'none', 'lyricist': 'none', 'performer': 'none', 'producer': 'fully'})
        self.assertEqual(credits([{'mode': 'remix', 'voice': {'octave': 'auto'}}])['performer'], 'partly')
        self.assertEqual(credits([{'mode': 'remix', 'melody_mode': 'free'}]), {'composer': 'partly', 'lyricist': 'none', 'performer': 'fully', 'producer': 'fully'})
        self.assertEqual(credits([{'mode': 'remix', 'instrumental': True}]), {'composer': 'none', 'lyricist': None, 'performer': None, 'producer': 'fully'})
        # A mix takes the most AI answer of its songs.
        self.assertEqual(credits([{'mode': 'preserve'}, {'mode': 'remix'}])['performer'], 'fully')
        self.assertEqual(release_pack.release_ai([credits([{'mode': 'preserve'}])]), 'partially')
        self.assertEqual(release_pack.release_type(3, 600), 'Single')
        self.assertEqual(release_pack.release_type(5, 1200), 'EP')
        self.assertEqual(release_pack.release_type(5, 2000), 'Album')
        self.assertEqual(release_pack.release_type(7, 1200), 'Album')

    def test_field_validation(self):
        base = release()
        for payload, message in (({'distributor': 'spotify'}, '平台'), ({'release_date': '2026-13-01'}, '日期'), ({'upc': '12ab'}, 'UPC'),
                ({'copyright_year': '2026'}, '年份'), ({'language': 'xx'}, '语言'), ({'title': 'x' * 151}, '发行名称'),
                ({'tracks': [{'job_id': 'a' * 32, 'title': 'A'}, {'job_id': 'a' * 32, 'title': 'B'}]}, '只能放一次'),
                ({'tracks': [{'job_id': 'a' * 32, 'title': 'A', 'isrc': 'bad'}]}, 'ISRC')):
            with self.subTest(payload=payload), self.assertRaises(ValueError) as error:
                release_pack.apply(base, payload)
            self.assertIn(message, str(error.exception))
        value = release_pack.apply(base, {'tracks': [{'job_id': 'a' * 32, 'title': '  Wind  ', 'version': 'Jazz Version', 'isrc': 'gb-lfp-26-00001', 'snippet': 41.6}]})
        self.assertEqual(value['tracks'][0], {'job_id': 'a' * 32, 'title': 'Wind', 'version': 'Jazz Version', 'explicit': False, 'isrc': 'GBLFP2600001', 'snippet': 42})


class ReviewTest(unittest.TestCase):
    def entry(self, job_id, **config):
        track = {'job_id': job_id, 'title': 'Wind', 'version': config.pop('version', 'Jazz Version'), 'explicit': False, 'isrc': '', 'snippet': None}
        job = {'id': job_id, 'track_id': config.pop('song', 's'), 'title': 'Wind'}
        return {'track': track, 'job': job, 'roots': [{'mode': 'preserve', **config}], 'seconds': 180., 'lyrics': {'text': 'la', 'source': 'provided'}, 'parents': config.pop('parents', [])}

    def test_problems_and_warnings(self):
        value = release(title='', release_date='2026-10-24', tracks=[{'job_id': 'a' * 32}])
        facts = release_pack.review(value, [self.entry('a' * 32)], [], TODAY)
        self.assertIn('请填写发行名称。', facts['problems'])
        self.assertIn('发行日期须在今天之后。', facts['problems'])
        soon = release(release_date='2026-10-30', tracks=[{'job_id': 'a' * 32}, {'job_id': 'b' * 32}])
        entries = [self.entry('a' * 32), self.entry('b' * 32, mode='remix')]
        other = release(id='o' * 32, distributor='offstep', title='First EP', tracks=[{'job_id': 'b' * 32}], created_at=1)
        facts = release_pack.review(soon, entries, [other], TODAY)
        self.assertTrue(any('First EP' in p and 'OFFstep' in p for p in facts['problems']))
        self.assertTrue(any('不足 14 天' in w for w in facts['warnings']))
        self.assertTrue(any('YuE2 生成' in w for w in facts['warnings']))
        self.assertEqual((facts['release_type'], facts['ai_use']), ('Single', 'partially'))
        same = release(tracks=[{'job_id': 'a' * 32}, {'job_id': 'b' * 32}])
        facts = release_pack.review(same, [self.entry('a' * 32), self.entry('b' * 32)], [], TODAY)
        self.assertTrue(any('名称与版本完全相同' in p for p in facts['problems']))
        many = [self.entry('%032x' % i, version=f'V{i}') for i in range(8)]
        facts = release_pack.review(release(tracks=[e['track'] for e in many]), many, [], TODAY)
        self.assertEqual(facts['problems'], [])
        self.assertTrue(any('8 个版本' in w for w in facts['warnings']))
        self.assertEqual(facts['release_type'], 'Album')
        # The earlier release keeps its work; a later draft with the same work does not block it.
        later = release(id='l' * 32, title='Later', tracks=[{'job_id': 'b' * 32}], created_at=3)
        self.assertEqual(release_pack.review(other, [self.entry('b' * 32)], [later], TODAY)['problems'], [])
        # A speed version of a work that another distributor carries is a Content ID risk, not a block.
        derived = self.entry('c' * 32, parents=['b' * 32])
        facts = release_pack.review(release(tracks=[derived['track']]), [derived], [other], TODAY)
        self.assertEqual(facts['problems'], [])
        self.assertTrue(any('Content ID' in w for w in facts['warnings']))


class PackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-release-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cover_and_store_export(self):
        cover = release_pack.make_cover(self.root / 'cover.jpg', '夏天的风 Summer Wind', 'Boss Sing', 7)
        from PIL import Image
        with Image.open(cover) as image:
            self.assertEqual((image.size, image.mode, image.format), ((3000, 3000), 'RGB', 'JPEG'))
        self.assertLess(cover.stat().st_size, 10 * 1024 ** 2)
        source = self.root / 'source.wav'
        audio = tone(40.)
        audio[int(20 * SR):int(32 * SR)] = 0
        sf.write(source, audio, SR, subtype='PCM_24')
        stats = release_pack.export_wav(service.TEST / 'bin/ffmpeg.exe', source, self.root / 'store.wav', 40.)
        info = sf.info(str(self.root / 'store.wav'))
        self.assertEqual((info.samplerate, info.channels, info.subtype), (44100, 2, 'PCM_16'))
        self.assertAlmostEqual(stats['seconds'], 40., delta=.05)
        self.assertGreaterEqual(stats['longest_silence'], 11)
        self.assertLess(stats['peak_dbfs'], 0)


class ReleaseServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sing-release-service-')
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(service, ROOT=self.root, DATA=self.root / 'data', JOBS=self.root / 'data/jobs', TRACKS=self.root / 'data/tracks')
        self.paths.start()
        service.JOBS.mkdir(parents=True)
        service.TRACKS.mkdir()
        self.http = service.ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
        self.previous = server.PORT
        server.PORT = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        server.PORT = self.previous
        self.paths.stop()
        self.temp.cleanup()

    def api(self, route, payload=None):
        request = urllib.request.Request(f'http://127.0.0.1:{server.PORT}' + route,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'X-Sing-Token': service.TOKEN, 'Content-Type': 'application/json'})
        try:
            with self.client.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            error.body = error.read().decode('utf-8', 'replace')
            error.msg = f'{error.msg}: {error.body}'
            raise

    def rejected(self, route, payload):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api(route, payload)
        self.assertEqual(error.exception.code, 400)
        return json.loads(error.exception.body)['error']

    def work(self, identifier, style, seconds=12., config=None, **meta):
        directory = service.JOBS / identifier
        directory.mkdir()
        sf.write(directory / 'result.wav', tone(seconds, 200 + len(style) * 10), SR, subtype='PCM_24')
        service.save(directory / 'result.json', {'files': {'result': 'result.wav'}, 'checks': {'result': {'seconds': seconds}}})
        service.save(directory / 'job.json', {'id': identifier, 'title': '01 - summer wind', 'style': style, 'state': 'completed', 'mode': 'remix',
            'track_id': 't' * 32, 'variant': 'A', 'pair_id': 'p' * 32, 'duration': seconds, 'created_at': 1, 'attempt': 1,
            'config': {'format': 'both', 'mode': 'remix', 'melody_mode': 'reference', 'lyrics': '', **(config or {})}, **meta})
        return identifier

    def wait_built(self, identifier):
        for _ in range(200):
            value = next(r for r in self.api('/api/state')['releases'] if r['id'] == identifier)
            if value.get('build', {}).get('state') != 'building':
                return value
            time.sleep(.1)
        self.fail('pack was not made')

    def test_release_from_draft_to_submitted(self):
        first = self.work('1' * 32, 'Jazz', config={'mode': 'preserve', 'lyrics': '[Chorus]\nhello summer wind,\nHELLO AGAIN.'})
        second = self.work('2' * 32, 'House', config={'voice': {'octave': 'auto'}}, voice={'octave': 'auto'})
        service.save(service.JOBS / second / 'lyrics.json', {'text': 'recognized line', 'source': 'whisper-large-v3-turbo'})
        speed = self.work('3' * 32, 'House', kind='adjust', parent_id=second, adjust_history=[{'preset': 'Slowed'}], config={})
        made = self.api('/api/releases', {'job_ids': [first, second]})
        self.assertEqual((made['distributor'], made['status'], made['title']), ('ditto', 'draft', 'Summer Wind'))
        self.assertEqual([(t['title'], t['version']) for t in made['tracks']], [('Summer Wind', 'Jazz Version'), ('Summer Wind', 'House Remix')])
        self.assertEqual([e['credits']['performer'] for e in made['entries']], ['none', 'partly'])
        self.assertEqual([e['lyrics_source'] for e in made['entries']], ['provided', 'whisper-large-v3-turbo'])
        self.assertIn('请填写艺人名。', made['review']['problems'])
        self.assertIn('请先', self.rejected(f'/api/releases/{made["id"]}/submitted', {}))
        self.assertIn('艺人名', self.rejected(f'/api/releases/{made["id"]}/build', {}))
        self.assertIn('加入作品', self.rejected(f'/api/releases/{made["id"]}', {'tracks': [{'job_id': speed, 'title': 'X'}]}))
        date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        edited = self.api(f'/api/releases/{made["id"]}', {'artist': 'Boss Sing', 'genre': 'Pop', 'copyright_holder': 'Boss Sing', 'release_date': date,
            'tracks': [{**made['tracks'][1], 'snippet': 3}, made['tracks'][0]]})
        self.assertEqual(edited['review']['problems'], [])
        self.assertEqual([t['version'] for t in edited['tracks']], ['House Remix', 'Jazz Version'])
        self.assertTrue(any('自动识别' in w for w in edited['review']['warnings']))
        # The next release starts on the other distributor and cannot take a work that is already released.
        other = self.api('/api/releases', {})
        self.assertEqual((other['distributor'], other['artist']), ('offstep', 'Boss Sing'))
        taken = self.api(f'/api/releases/{other["id"]}/tracks', {'job_ids': [first, speed]})
        self.assertEqual(taken['tracks'][1]['version'], 'House Remix, Slowed')
        self.assertTrue(any('同一录音只能经一个平台' in p for p in taken['review']['problems']))
        self.assertTrue(any('Content ID' in w for w in taken['review']['warnings']))
        cover = self.client.open(f'http://127.0.0.1:{server.PORT}/releases/{made["id"]}/cover.jpg', timeout=20)
        self.assertEqual(cover.headers['Content-Type'], 'image/jpeg')
        self.assertGreater(len(cover.read()), 10000)
        self.assertEqual(self.api(f'/api/releases/{made["id"]}/build', {})['build']['state'], 'building')
        built = self.wait_built(made['id'])
        self.assertEqual((built['status'], built['build']['state']), ('ready', 'done'), built.get('build'))
        pack = Path(built['pack_path'])
        meta = json.loads((pack / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['release']['ai_use'], 'partially')
        self.assertEqual([t['file'] for t in meta['tracks']], ['01 - Summer Wind (House Remix).wav', '02 - Summer Wind (Jazz Version).wav'])
        self.assertEqual(meta['tracks'][0]['snippet_start'], 3)
        for t in meta['tracks']:
            info = sf.info(str(pack / t['file']))
            self.assertEqual((info.samplerate, info.subtype), (44100, 'PCM_16'))
        self.assertEqual((pack / meta['tracks'][1]['lyrics_file']).read_text(encoding='utf-8'), 'Hello summer wind\nHello again\n')
        checklist = (pack / '上传清单 Upload checklist.md').read_text(encoding='utf-8')
        self.assertIn('Partially AI', checklist)
        self.assertIn('Track mix version: House Remix', checklist)
        self.assertTrue((pack / 'cover.jpg').is_file())
        # Editing a packed release returns it to a draft until the pack is made again.
        stale = self.api(f'/api/releases/{made["id"]}', {'label': 'Sing Records'})
        self.assertEqual((stale['status'], stale['build']['state']), ('draft', 'stale'))
        self.api(f'/api/releases/{made["id"]}/build', {})
        self.assertEqual(self.wait_built(made['id'])['status'], 'ready')
        submitted = self.api(f'/api/releases/{made["id"]}/submitted', {'upc': '123456789012'})
        self.assertEqual((submitted['status'], submitted['upc']), ('submitted', '123456789012'))
        self.assertIn('已提交', self.rejected(f'/api/releases/{made["id"]}', {'title': 'Other'}))
        self.assertEqual(self.api(f'/api/releases/{made["id"]}', {'notes': 'live on Spotify'})['status'], 'submitted')
        self.assertEqual(self.api(f'/api/releases/{made["id"]}/reopen', {})['status'], 'draft')
        # A deleted release no longer holds its works.
        self.api(f'/api/releases/{made["id"]}/remove', {})
        freed = next(r for r in self.api('/api/state')['releases'] if r['id'] == other['id'])
        self.assertFalse(any('同一录音' in p for p in freed['review']['problems']))
        self.assertTrue(self.api(f'/api/releases/{made["id"]}/restore', {})['hidden'] is False)

    def test_export_folder_for_automation(self):
        outbox = self.root / 'EP outbox'
        self.assertIn('完整路径', self.rejected('/api/release-settings', {'export_folder': 'relative\\path'}))
        self.assertEqual(self.api('/api/release-settings', {'export_folder': str(outbox)})['export_folder'], str(outbox))
        self.assertEqual(self.api('/api/state')['release_settings']['export_folder'], str(outbox))
        date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        details = {'artist': 'Boss Sing', 'genre': 'Pop', 'copyright_holder': 'Boss Sing', 'release_date': date}
        first = self.api('/api/releases', {'job_ids': [self.work('6' * 32, 'Jazz')]})
        self.api(f'/api/releases/{first["id"]}', {**details, 'title': 'Night: Drive?'})
        # Another release's folder with the same name is never replaced; this one gets a numbered name.
        (outbox / 'Night_ Drive_').mkdir(parents=True)
        (outbox / 'Night_ Drive_' / 'metadata.json').write_text(json.dumps({'release_id': 'z' * 32}), encoding='utf-8')
        self.api(f'/api/releases/{first["id"]}/build', {})
        built = self.wait_built(first['id'])
        exported = Path(built['build']['export_path'])
        self.assertEqual((exported.name, built['export_path']), ('Night_ Drive_ (2)', str(exported)))
        self.assertEqual(json.loads((exported / 'metadata.json').read_text(encoding='utf-8'))['release_id'], first['id'])
        self.assertTrue((exported / 'cover.jpg').is_file() and not list(outbox.glob('.*.partial')))
        # Renamed and packed again: the EP folder follows the name and the old copy of this release goes away.
        self.api(f'/api/releases/{first["id"]}', {'title': 'Night Drive'})
        self.api(f'/api/releases/{first["id"]}/build', {})
        rebuilt = self.wait_built(first['id'])
        self.assertEqual(Path(rebuilt['export_path']).name, 'Night Drive')
        self.assertFalse(exported.exists())
        self.assertTrue((outbox / 'Night_ Drive_').is_dir())
        self.assertEqual(self.api('/api/release-settings', {'export_folder': ''})['export_folder'], '')

    def test_interrupted_build_and_missing_work(self):
        work = self.work('4' * 32, 'Jazz')
        made = self.api('/api/releases', {'job_ids': [work]})
        service.change_jobs([work], 'remove')
        shown = next(r for r in self.api('/api/state')['releases'] if r['id'] == made['id'])
        self.assertTrue(shown['entries'][0]['missing'])
        self.assertTrue(any('已不存在或已删除' in p for p in shown['review']['problems']))
        self.assertIn('已删除', self.rejected(f'/api/releases/{made["id"]}/tracks', {'job_ids': [self.work('5' * 32, 'Rock', hidden=True)]}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
