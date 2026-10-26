// Runs a distributor field map (maps/*.json) against an open page: fills text, picks options, ticks
// boxes, attaches files and presses "next"-style buttons. It never presses a button whose name looks like
// submitting, paying or publishing; the final Submit always stays with the person.
import { resolve as resolvePath } from 'node:path';

export const FORBIDDEN = /\b(submit|distribute|release now|publish|pay|payment|checkout|purchase|buy|place order|confirm order|agree)\b|提交|付款|支付|购买|发布|同意/i;

const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const pattern = (text) => new RegExp(escape(text), 'i');

// "{{release.title}}" or "{{track.version}}" from the data; a missing value is ''.
export function fillIn(template, data) {
  if (typeof template !== 'string') return template;
  return template.replace(/\{\{\s*([\w.]+)\s*\}\}/g, (_, path) => {
    const value = path.split('.').reduce((v, key) => (v == null ? v : v[key]), data);
    return value == null ? '' : String(value);
  });
}

// The nth (1-based, default 1) element matching a label, role + name, placeholder, text or CSS selector.
export function locate(page, target) {
  const all = target.label ? page.getByLabel(pattern(target.label))
    : target.role ? page.getByRole(target.role, target.name ? { name: pattern(target.name) } : {})
    : target.placeholder ? page.getByPlaceholder(pattern(target.placeholder))
    : target.text ? page.getByText(pattern(target.text))
    : target.css ? page.locator(target.css) : null;
  if (!all) throw new Error('step target needs label, role, placeholder, text or css');
  return all.nth(Math.max(0, Number(target.nth || 1) - 1));
}

// Hands Chrome the file paths itself (DevTools DOM.setFileInputFiles): Chrome runs on this PC and reads
// the files from disk, so whole albums are not limited by Playwright's 50 MB transfer cap.
async function attach(page, locator, paths) {
  const mark = 'sing-' + Math.random().toString(36).slice(2);
  await locator.evaluate((el, m) => {
    el.setAttribute('data-sing-upload', m);
    el.singChanged = false;
    el.addEventListener('change', () => { el.singChanged = true; }, { once: true });
  }, mark);
  const session = await page.context().newCDPSession(page);
  try {
    const { root } = await session.send('DOM.getDocument', { depth: 0 });
    const { nodeId } = await session.send('DOM.querySelector', { nodeId: root.nodeId, selector: `[data-sing-upload="${mark}"]` });
    if (!nodeId) throw new Error('file input is not in the main page');
    await session.send('DOM.setFileInputFiles', { nodeId, files: paths });
  } finally {
    await session.detach().catch(() => {});
  }
  // The site must notice the files: send the events only if Chrome did not.
  await locator.evaluate((el) => {
    el.removeAttribute('data-sing-upload');
    if (!el.singChanged) {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
}

async function nameOf(locator) {
  return locator.evaluate((el) => [el.getAttribute('aria-label'), el.innerText, el.value, el.getAttribute('title')].filter(Boolean).join(' ')).catch(() => '');
}

async function runStep(page, step, data, folder) {
  // Optional fields give up sooner, so a page without them is not slowed down by long waits.
  const timeout = step.timeout || (step.optional ? 3000 : 8000);
  if (step.action === 'goto') return page.goto(fillIn(step.url, data), { waitUntil: 'domcontentloaded' });
  if (step.action === 'wait') return page.waitForTimeout(Math.min(step.ms || 1000, 15000));
  const locator = locate(page, Object.fromEntries(Object.entries(step.target || {}).map(([k, v]) => [k, fillIn(v, data)])));
  await locator.waitFor({ state: step.action === 'upload' ? 'attached' : 'visible', timeout });
  const value = fillIn(step.value, data);
  if (step.action === 'fill') return locator.fill(value, { timeout });
  if (step.action === 'select') return locator.selectOption({ label: value }, { timeout });
  if (step.action === 'check') return value === 'false' ? locator.uncheck({ timeout }) : locator.check({ timeout });
  if (step.action === 'upload') {
    const files = (step.files_from ? data[step.files_from] || [] : Array.isArray(step.files) ? step.files : [step.files]).map((f) => fillIn(f, data)).filter(Boolean);
    return attach(page, locator, files.map((f) => resolvePath(folder, f)));
  }
  if (step.action === 'click') {
    const name = await nameOf(locator);
    if (FORBIDDEN.test(name) || FORBIDDEN.test(step.name || '')) throw new Error(`refused to press "${name.trim().slice(0, 60)}": submitting stays with you`);
    return locator.click({ timeout });
  }
  throw new Error(`unknown action ${step.action}`);
}

// Runs every step in order; steps with "for_each": "tracks" repeat their own steps per track.
// Stops at the first required step that fails and reports where, so the person can carry on by hand.
export async function runMap(page, map, data, folder, log = () => {}) {
  const done = [];
  const skipped = [];
  const walk = async (steps, scope) => {
    for (const step of steps) {
      if (step.for_each) {
        for (const [index, item] of (scope[step.for_each] || []).entries()) {
          await walk(step.steps, { ...scope, track: item, index: index + 1 });
        }
        continue;
      }
      const label = fillIn(step.name || step.action, scope);
      try {
        await runStep(page, step, scope, folder);
        done.push(label);
        log(`ok   ${label}`);
      } catch (error) {
        if (step.optional && !/refused/.test(error.message)) {
          skipped.push(label);
          log(`skip ${label}: ${error.message.split('\n')[0]}`);
          continue;
        }
        throw Object.assign(new Error(error.message.split('\n')[0]), { step: label });
      }
    }
  };
  try {
    await walk(map.steps, data);
    return { status: 'filled', done, skipped };
  } catch (error) {
    return { status: 'stopped', done, skipped, stopped_at: error.step, reason: error.message };
  }
}
