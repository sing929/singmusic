// The helper n8n runs: listing packed EPs from Sing Studio, recording a submission, and the form filler
// on a mock release builder in a real (headless) Chrome — including that it never presses Submit.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { after, before, test } from 'node:test';
import { FORBIDDEN, fillIn, runMap } from '../fill-engine.mjs';
import { build, config, formData, list, loadMap, releases, resolveId, submitted, upload } from '../release-helper.mjs';

const ID = 'a'.repeat(32);
const NEW = 'c'.repeat(32);
let root, folder, server, calls, cfg, exportFolder;

function pack(dir, id) {
  mkdirSync(join(dir, 'lyrics'), { recursive: true });
  for (const f of ['01 - Wind (Jazz Version).wav', '02 - Wind (House Remix).wav']) writeFileSync(join(dir, f), 'RIFF');
  writeFileSync(join(dir, 'cover.jpg'), 'JPG');
  writeFileSync(join(dir, 'lyrics', '01 - Wind (Jazz Version).txt'), 'Hello wind\n');
  const track = (number, version, lyrics) => ({ number, title: 'Wind', version, display_title: `Wind (${version})`, file: `0${number} - Wind (${version}).wav`, seconds: 180,
    explicit: false, isrc: '', snippet_start: 65, lyrics_file: lyrics, ai_credits: { composer: 'none', lyricist: 'none', performer: 'partly', producer: 'fully' } });
  writeFileSync(join(dir, 'metadata.json'), JSON.stringify({ release_id: id, distributor: { id: 'ditto', name: 'Ditto Music' },
    release: { title: 'Summer Wind', artist: 'Boss Sing', credit_name: '', label: '', genre: 'Pop', subgenre: '', language: 'zh', language_name: 'Chinese (Mandarin) · 中文',
      copyright_holder: 'Boss Sing', copyright_year: 2026, release_date: '2026-12-01', release_type: 'Single', ai_use: 'partially', upc: '' },
    tracks: [track(1, 'Jazz Version', 'lyrics/01 - Wind (Jazz Version).txt'), track(2, 'House Remix', null)] }));
}

before(async () => {
  root = mkdtempSync(join(tmpdir(), 'sing-helper-'));
  exportFolder = root;
  folder = join(root, 'Summer Wind');
  pack(folder, ID);
  pack(join(root, 'Old EP'), 'b'.repeat(32));
  calls = [];
  const draft = (id, title, problems = []) => ({ id, title, status: 'draft', hidden: false, tracks: [{}, {}], release_date: '2026-12-01', review: { problems, warnings: [] } });
  const releases = [
    { ...draft(ID, 'Summer Wind'), status: 'ready', export_path: folder },
    { ...draft('b'.repeat(32), 'Old EP'), export_path: join(root, 'Old EP') },
    draft(NEW, 'Night Drive'),
    draft('d'.repeat(32), 'Unfinished', ['请填写艺人名。']),
    { ...draft('ab' + 'c'.repeat(30), 'Twin'), hidden: true },
  ];
  // Sing Studio stand-in: a build takes a moment, then the pack appears in the EP export folder.
  server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      if (req.method === 'GET' && req.url === '/api/state') {
        res.end(JSON.stringify({ token: 'T', releases, release_settings: { export_folder: exportFolder } }));
        return;
      }
      calls.push({ url: req.url, token: req.headers['x-sing-token'], body: JSON.parse(body || '{}') });
      const building = releases.find((r) => req.url === `/api/releases/${r.id}/build`);
      if (building) {
        building.build = { state: 'building' };
        setTimeout(() => {
          const out = join(root, building.title);
          pack(out, building.id);
          Object.assign(building, { status: 'ready', export_path: out, build: { state: 'done', export_path: out, tracks: [{}, {}], warnings: [] } });
        }, 400);
      }
      res.end(JSON.stringify({ status: 'ok' }));
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  cfg = { ...config(), sing_studio: `http://127.0.0.1:${server.address().port}` };
});

after(() => {
  server.close();
  // Chrome may still be releasing its profile folder for a moment after it closes.
  rmSync(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
});

test('templates and the submit guard', () => {
  assert.equal(fillIn('{{release.title}} / {{track.version}} / {{x.y}}', { release: { title: 'A' }, track: { version: 'B' } }), 'A / B / ');
  for (const name of ['Submit release', 'Distribute', 'Pay now', 'Checkout', '提交', 'Publish']) assert.ok(FORBIDDEN.test(name), name);
  for (const name of ['Create Release', 'Next', 'Save and continue', 'Edit', 'Upload audio']) assert.ok(!FORBIDDEN.test(name), name);
});

test('lists only packed, unsubmitted EPs and records a submission', async () => {
  const found = await list(cfg);
  assert.equal(found.ok, true);
  assert.deepEqual(found.eps.map((ep) => ep.id), [ID]);
  assert.equal(found.eps[0].label, 'Summer Wind · Ditto Music · 2 首 · 2026-12-01');
  const data = formData(folder);
  assert.deepEqual(data.track_files, ['01 - Wind (Jazz Version).wav', '02 - Wind (House Remix).wav']);
  assert.equal(data.tracks[0].lyrics, 'Hello wind');
  assert.equal(data.tracks[0].snippet_text, '1:05');
  assert.equal(data.tracks[0].ai.performer, 'Partly AI');
  assert.equal(data.release.label, 'Boss Sing');
  await assert.rejects(submitted(cfg, ID, '12ab'), /UPC/);
  const done = await submitted(cfg, ID, '123456789012');
  assert.equal(done.status, 'submitted');
  assert.deepEqual(calls.at(-1), { url: `/api/releases/${ID}/submitted`, token: 'T', body: { upc: '123456789012' } });
  assert.equal(JSON.parse(readFileSync(join(folder, 'submitted.json'), 'utf8')).upc, '123456789012');
  assert.equal((await list(cfg)).count, 0);
  rmSync(join(folder, 'submitted.json'));
});

test('fills the mock builder with the Ditto map and never submits', { timeout: 120000 }, async (t) => {
  if (!cfg.chrome) return t.skip('Chrome not installed');
  const { chromium } = await import('playwright-core');
  const browser = await chromium.launch({ executablePath: cfg.chrome, headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(pathToFileURL(join(import.meta.dirname, 'mock-builder.html')).href);
    const result = await runMap(page, loadMap('ditto'), formData(folder), folder);
    assert.equal(result.status, 'filled', JSON.stringify(result));
    assert.ok(result.skipped.includes('Create New') && result.skipped.includes('Copyright year'));
    const value = (id) => page.locator('#' + id).inputValue();
    assert.equal(await value('title'), 'Summer Wind');
    assert.equal(await value('artist'), 'Boss Sing');
    assert.equal(await value('label'), 'Boss Sing');
    assert.equal(await value('language'), 'Chinese (Mandarin) · 中文');
    assert.equal(await value('date'), '2026-12-01');
    assert.equal(await page.locator('#audio').evaluate((el) => el.files.length), 2);
    assert.equal(await page.locator('#art').evaluate((el) => el.files[0].name), 'cover.jpg');
    assert.equal(await page.locator('input[value=partially]').isChecked(), true);
    const rows = await page.locator('#tracks > div[data-t]').evaluateAll((els) => els.map((e) => [e.dataset.t, e.dataset.v, e.dataset.l || '']));
    assert.deepEqual(rows, [['Wind', 'Jazz Version', 'Hello wind'], ['Wind', 'House Remix', '']]);
    assert.equal(await page.locator('#state').textContent(), 'draft');
    // A map that tries to press Submit is refused and stops there.
    const refused = await runMap(page, { steps: [{ name: 'Submit', action: 'click', target: { role: 'button', name: 'Submit release' } }] }, {}, folder);
    assert.equal(refused.status, 'stopped');
    assert.match(refused.reason, /refused/);
    const sneaky = await runMap(page, { steps: [{ name: 'Next', action: 'click', target: { css: '#submit' }, optional: true }] }, {}, folder);
    assert.equal(sneaky.status, 'stopped');
    assert.equal(await page.locator('#state').textContent(), 'draft');
  } finally {
    await browser.close();
  }
  assert.ok(existsSync(join(folder, 'metadata.json')));
});

test('release ids, the releases list and build', { timeout: 60000 }, async () => {
  const all = (await releases(cfg)).releases;
  assert.deepEqual(all.map((r) => r.id), [ID, 'b'.repeat(32), NEW, 'd'.repeat(32)]);
  assert.equal(all[3].problems[0], '请填写艺人名。');
  assert.equal(resolveId(all, 'AAAAAA'), ID);
  assert.throws(() => resolveId(all, 'aaa'), /至少前 6 位/);
  assert.throws(() => resolveId(all, 'abcccc'), /找不到/);
  assert.throws(() => resolveId([...all, { id: 'a'.repeat(31) + 'b' }], 'aaaaaa'), /多个/);
  const blocked = await build(cfg, 'dddddd', { poll: 50 });
  assert.equal(blocked.ok, false);
  assert.match(blocked.error, /艺人名/);
  exportFolder = '';
  await assert.rejects(build(cfg, 'bbbbbb', { poll: 50 }), /导出文件夹/);
  exportFolder = root;
  const made = await build(cfg, 'bbbbbb', { poll: 50 });
  assert.equal(made.status, 'built', JSON.stringify(made));
  assert.equal(made.export_path, join(root, 'Old EP'));
  assert.equal(calls.at(-1).url, `/api/releases/${'b'.repeat(32)}/build`);
});

test('upload builds when needed, then fills the form without submitting', { timeout: 180000 }, async (t) => {
  if (!cfg.chrome) return t.skip('Chrome not installed');
  const maps = join(root, 'maps');
  mkdirSync(maps, { recursive: true });
  writeFileSync(join(maps, 'ditto.json'), JSON.stringify({ ...loadMap('ditto'), start_url: pathToFileURL(join(import.meta.dirname, 'mock-builder.html')).href }));
  // The Chrome profile lives outside the test folder: Chrome lets go of it a little after it exits.
  const profile = mkdtempSync(join(tmpdir(), 'sing-helper-chrome-'));
  const own = { ...cfg, maps, port: 9341, profile, headless: true };
  try {
    const first = await upload(own, 'cccccc', { poll: 50 });
    assert.equal(first.status, 'filled', JSON.stringify(first));
    assert.equal(first.built.export_path, join(root, 'Night Drive'));
    assert.match(first.message, /^已生成发行包/);
    assert.ok(calls.some((c) => c.url === `/api/releases/${NEW}/build`));
    const builds = calls.filter((c) => c.url.endsWith('/build')).length;
    const again = await upload(own, 'cccccc', { poll: 50 });
    assert.equal(again.built, 'reused');
    assert.equal(calls.filter((c) => c.url.endsWith('/build')).length, builds);
    const { chromium } = await import('playwright-core');
    const browser = await chromium.connectOverCDP(`http://127.0.0.1:${own.port}`);
    const page = browser.contexts()[0].pages().filter((p) => p.url().includes('mock-builder')).at(-1);
    assert.equal(await page.locator('#state').textContent(), 'draft');
    assert.equal(await page.locator('#audio').evaluate((el) => el.files.length), 2);
    await browser.close();
  } finally {
    // The upload Chrome was started by the helper and outlives it; close it for the test.
    const session = await fetch(`http://127.0.0.1:9341/json/version`).then((r) => r.json()).catch(() => null);
    if (session) {
      const { chromium } = await import('playwright-core');
      const browser = await chromium.connectOverCDP('http://127.0.0.1:9341');
      await (await browser.newBrowserCDPSession()).send('Browser.close').catch(() => {});
      for (let i = 0; i < 60 && (await fetch('http://127.0.0.1:9341/json/version').then(() => true, () => false)); i++) await new Promise((r) => setTimeout(r, 250));
    }
    await new Promise((r) => setTimeout(r, 2000));
    try { rmSync(profile, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 }); } catch { /* left in the temp folder */ }
  }
});
