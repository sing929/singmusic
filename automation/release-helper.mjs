// Sing Studio release helper for n8n (runs on the app PC, next to Sing Studio).
//   node release-helper.mjs list                 EP folders that are packed and not yet submitted
//   node release-helper.mjs login <distributor>  open the upload Chrome on the distributor site to sign in once
//   node release-helper.mjs fill <release id>    fill the distributor's upload form in that Chrome, never Submit
//   node release-helper.mjs submitted <release id> [UPC]   record that the person pressed Submit
// Every command prints one JSON object and exits 0, so n8n can branch on "ok" and "status".
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runMap } from './fill-engine.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const CREDIT_TEXT = { none: 'No AI', partly: 'Partly AI', fully: 'Fully AI' };
const RELEASE_AI = { none: 'No AI', partially: 'Partially AI', entirely: 'Entirely AI' };

export function config() {
  const file = join(HERE, 'config.json');
  const own = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : {};
  const local = process.env.LOCALAPPDATA || join(process.env.USERPROFILE || HERE, 'AppData', 'Local');
  return {
    sing_studio: 'http://127.0.0.1:18765',
    chrome: ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
      join(local, 'Google/Chrome/Application/chrome.exe'), 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync),
    profile: join(local, 'SingStudio', 'upload-chrome'),
    port: 9333,
    maps: join(HERE, 'maps'),
    ...own,
  };
}

const readJson = (file) => { try { return JSON.parse(readFileSync(file, 'utf8')); } catch { return null; } };

async function studio(cfg, path, body) {
  const response = await fetch(cfg.sing_studio + path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Sing-Token': (await studio(cfg, '/api/state')).token }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Sing Studio answered ${response.status}`);
  return data;
}

// Packed releases whose EP folder still holds this release's pack and has no submitted.json.
export async function list(cfg) {
  let state;
  try { state = await studio(cfg, '/api/state'); } catch (error) {
    return { ok: false, error: `Sing Studio 没有响应（${cfg.sing_studio}）：请先打开 Sing Studio。${error.message}` };
  }
  const eps = [];
  for (const r of state.releases || []) {
    if (r.hidden || r.status !== 'ready' || !r.export_path) continue;
    const meta = readJson(join(r.export_path, 'metadata.json'));
    if (!meta || meta.release_id !== r.id || existsSync(join(r.export_path, 'submitted.json'))) continue;
    eps.push({ id: r.id, title: meta.release.title, folder: r.export_path, distributor: meta.distributor.id, distributor_name: meta.distributor.name,
      tracks: meta.tracks.length, release_type: meta.release.release_type, release_date: meta.release.release_date,
      label: `${meta.release.title} · ${meta.distributor.name} · ${meta.tracks.length} 首 · ${meta.release.release_date}` });
  }
  return { ok: true, export_folder: state.release_settings?.export_folder || '', count: eps.length, eps };
}

async function find(cfg, id) {
  if (!/^[0-9a-f]{32}$/.test(id || '')) throw new Error('发行编号无效。');
  const found = (await list(cfg)).eps?.find((ep) => ep.id === id);
  if (!found) throw new Error('找不到这个待上传的 EP：可能已标记提交，或资料改动后还没重新生成发行包。');
  return found;
}

// The upload Chrome: its own profile (sign in to the distributors there once) with a local debugging
// port, started detached so it stays open for the person after this command ends.
async function chrome(cfg) {
  const { chromium } = await import('playwright-core');
  const url = `http://127.0.0.1:${cfg.port}`;
  const ready = async () => { try { return (await fetch(url + '/json/version')).ok; } catch { return false; } };
  if (!(await ready())) {
    if (!cfg.chrome) throw new Error('找不到 Chrome，请在 config.json 里填写 chrome 路径。');
    mkdirSync(cfg.profile, { recursive: true });
    // "headless" is only for tests: the person needs to see this Chrome to check the form and press Submit.
    spawn(cfg.chrome, [`--remote-debugging-port=${cfg.port}`, '--remote-debugging-address=127.0.0.1', `--user-data-dir=${cfg.profile}`, '--no-first-run', '--no-default-browser-check',
      ...(cfg.headless ? ['--headless=new'] : [])],
      { detached: true, stdio: 'ignore' }).unref();
    for (let i = 0; i < 40 && !(await ready()); i++) await new Promise((r) => setTimeout(r, 250));
    if (!(await ready())) throw new Error('上传用的 Chrome 没有启动。');
  }
  const browser = await chromium.connectOverCDP(url);
  connected.push(browser);
  return { browser, context: browser.contexts()[0] };
}

// Connections to the upload Chrome, closed before this command ends. Closing a connected (not launched)
// browser only disconnects, so Chrome and the filled form stay open.
const connected = [];

// A distributor's field map; config "maps" may point at another folder (the tests use a mock site).
export function loadMap(distributor, folder = config().maps) {
  if (!/^[a-z]+$/.test(distributor || '')) throw new Error('平台名称无效。');
  const map = readJson(join(folder, `${distributor}.json`));
  if (!map) throw new Error(`没有 ${distributor} 的字段映射（${join(folder, distributor + '.json')}）。`);
  return map;
}

export function formData(folder) {
  const meta = readJson(join(folder, 'metadata.json'));
  const tracks = meta.tracks.map((t) => ({
    ...t, explicit_text: t.explicit ? 'Yes' : 'No', lyrics: t.lyrics_file ? readFileSync(join(folder, t.lyrics_file), 'utf8').trim() : '',
    has_lyrics: t.lyrics_file ? 'true' : 'false', snippet_text: `${Math.floor(t.snippet_start / 60)}:${String(t.snippet_start % 60).padStart(2, '0')}`,
    ai: Object.fromEntries(Object.entries(t.ai_credits).map(([role, v]) => [role, v ? CREDIT_TEXT[v] : ''])),
    credit_name: meta.release.credit_name || meta.release.artist,
  }));
  const release = { ...meta.release, label: meta.release.label || meta.release.artist, ai_use_text: RELEASE_AI[meta.release.ai_use] || '' };
  return { release, tracks, track_files: tracks.map((t) => t.file), cover: 'cover.jpg', distributor: meta.distributor, release_id: meta.release_id };
}

async function login(cfg, distributor) {
  const map = loadMap(distributor, cfg.maps);
  const { context } = await chrome(cfg);
  const page = await context.newPage();
  await page.goto(map.login_url || map.start_url, { waitUntil: 'domcontentloaded' });
  await page.bringToFront();
  return { ok: true, status: 'opened', message: `已在上传用的 Chrome 打开 ${map.name}，请在那里登录一次（会保持登录）。` };
}

async function fill(cfg, id) {
  const ep = await find(cfg, id);
  const map = loadMap(ep.distributor, cfg.maps);
  const data = formData(ep.folder);
  const { context } = await chrome(cfg);
  const page = await context.newPage();
  await page.goto(map.start_url, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  await page.bringToFront();
  const signedOut = (map.login_patterns || []).some((p) => page.url().toLowerCase().includes(p))
    || await page.locator('input[type=password]').first().isVisible().catch(() => false);
  if (signedOut) {
    return { ok: true, status: 'login_required', ep, message: `请先在打开的 Chrome 里登录 ${map.name}，然后在 n8n 表单里重新开始。` };
  }
  if (!map.steps?.length) {
    return { ok: true, status: 'opened', ep, map_verified: false, done: [], skipped: [],
      message: `${map.name} 还没有字段映射：已在 Chrome 打开网站，请按 EP 文件夹里的《上传清单》填写，再由你本人点 Submit。` };
  }
  const lines = [];
  const result = await runMap(page, map, data, ep.folder, (line) => lines.push(line));
  const log = { at: new Date().toISOString(), map: map.name, map_verified: !!map.verified, ...result, lines };
  writeFileSync(join(ep.folder, 'upload-log.json'), JSON.stringify(log, null, 2));
  await page.bringToFront();
  const missed = result.skipped.length ? `，${result.skipped.length} 项没找到（${result.skipped.slice(0, 6).join('、')}${result.skipped.length > 6 ? ' …' : ''}），请按《上传清单》补上` : '';
  return { ok: true, ep, map_verified: !!map.verified, ...result,
    message: result.status === 'filled'
      ? `已在 Chrome 填好 ${map.name} 的 ${result.done.length} 项${missed}。请逐项核对，然后由你本人点 Submit。`
      : `填到“${result.stopped_at}”停下（${result.reason}）。已填 ${result.done.length} 项，其余请按 EP 文件夹里的《上传清单》在 Chrome 里手动完成，再由你本人点 Submit。` };
}

export async function submitted(cfg, id, upc) {
  const ep = await find(cfg, id);
  const code = String(upc || '').replace(/\D/g, '');
  if (code && !/^\d{12,14}$/.test(code)) throw new Error('UPC 须为 12–14 位数字，或留空。');
  // The record in the folder comes first: it is what keeps this EP from being offered again.
  writeFileSync(join(ep.folder, 'submitted.json'), JSON.stringify({ submitted_at: new Date().toISOString(), upc: code, distributor: ep.distributor, by: 'n8n' }, null, 2));
  try {
    await studio(cfg, `/api/releases/${id}/submitted`, code ? { upc: code } : {});
    return { ok: true, status: 'submitted', ep, message: `已记录《${ep.title}》在 ${ep.distributor_name} 提交。` };
  } catch (error) {
    return { ok: true, status: 'submitted', ep, warning: `EP 文件夹已标记提交，但 Sing Studio 未能更新：${error.message}`, message: `已记录《${ep.title}》在 ${ep.distributor_name} 提交。` };
  }
}

async function main([command, arg, extra]) {
  const cfg = config();
  if (command === 'list') return list(cfg);
  if (command === 'login') return login(cfg, arg || 'ditto');
  if (command === 'fill') return fill(cfg, arg);
  if (command === 'submitted') return submitted(cfg, arg, extra);
  return { ok: false, error: 'usage: list | login <distributor> | fill <id> | submitted <id> [UPC]' };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  // No process.exit(): on Windows it can abort Node while fetch handles close ("UV_HANDLE_CLOSING"),
  // which n8n reports as a failed command. Closing every connection lets Node end on its own.
  main(process.argv.slice(2))
    .catch((error) => ({ ok: false, error: error.message }))
    .then(async (result) => {
      await Promise.all(connected.map((browser) => browser.close().catch(() => {})));
      process.stdout.write(JSON.stringify(result) + '\n');
      process.exitCode = 0;
    });
}
