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
import { config, formData, list, loadMap, submitted } from '../release-helper.mjs';

const ID = 'a'.repeat(32);
let root, folder, server, calls, cfg;

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
  folder = join(root, 'Summer Wind');
  pack(folder, ID);
  pack(join(root, 'Old EP'), 'b'.repeat(32));
  calls = [];
  const releases = [
    { id: ID, status: 'ready', hidden: false, export_path: folder },
    { id: 'b'.repeat(32), status: 'draft', hidden: false, export_path: join(root, 'Old EP') },
  ];
  server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      if (req.method === 'GET' && req.url === '/api/state') {
        res.end(JSON.stringify({ token: 'T', releases, release_settings: { export_folder: root } }));
        return;
      }
      calls.push({ url: req.url, token: req.headers['x-sing-token'], body: JSON.parse(body || '{}') });
      res.end(JSON.stringify({ status: 'submitted' }));
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  cfg = { ...config(), sing_studio: `http://127.0.0.1:${server.address().port}` };
});

after(() => {
  server.close();
  rmSync(root, { recursive: true, force: true });
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
