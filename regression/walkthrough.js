// Walkthrough stage: every role, every menu and hub page, on a desktop and a phone.
//   node regression/walkthrough.js <base url> <sessions.json> <inventory.json> <shots dir> <checks.json>
//
// For each role session (regression/page_setup.py walk-sessions) and each width
// (desktop 1280x900, phone 390x844 with touch), it records the menu (sections,
// items, badges, hub tiles; on the phone after opening the menu), then visits
// every menu and hub page and every page type reached from them (numbers in the
// path are one page type, as in pages.js; GET only, no downloads, no sign-out).
// Per page it records the final URL, HTTP status, <h1>, breadcrumbs, section
// headings, buttons, links and forms (labels only) and a full-page screenshot in
// <shots dir>/<role>/<desktop|phone>/<slug>.png.
//
// Automated checks (stable IDs WALK-, regression/ids/walkthrough.yaml):
//   - a menu entry or hub tile has the same label as its page's <h1>;
//   - no sideways scroll; nothing (button, input, card, table cell...) wider
//     than its container or running off the screen;
//   - on a phone, buttons and links outside running text are at least 40x40 px;
//   - no duplicate labels in one role's menu (sidebar and hub tiles);
//   - every page a role can reach opens (HTTP 200) instead of only redirecting;
//   - no console or Content-Security-Policy errors.
// The inventory (role -> width -> menu + pages) is what the reviewers read.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [base, sessionsFile, inventoryFile, shotsDir, checksFile] = process.argv.slice(2);
const sessions = JSON.parse(fs.readFileSync(sessionsFile, 'utf8'));
const ROLE_ORDER = ['owner', 'authorized', 'view_only', 'tenant', 'former_owner', 'board', 'officer',
  'manager', 'arc_reviewer', 'auditor', 'sysadmin'];
const roles = [...ROLE_ORDER.filter((r) => sessions[r]), ...Object.keys(sessions).filter((r) => !ROLE_ORDER.includes(r))];
const WIDTHS = {
  desktop: { viewport: { width: 1280, height: 900 } },
  phone: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
};
// Downloads (with or without a query string) are not pages; same list as pages.js.
// /help/ask/ is the "Ask the board" shortcut: it forwards to a new owner request by design (D73).
const SKIP = [/logout/, /\/help\/ask\//, /\/ops\/status\/keys\/.+/, /\/ops\/status\/restart\//, /\.(pdf|ics|csv|xlsx|zip)(\?|$)/, /\/zip\/?$/, /format=(csv|pdf)/, /\/disbursements\/\d+\/register\//, /download/, /\/files?\//, /\/letters?\//, /statement/,
  /export/, /\/static\//, /\/2fa\/.+/, /reauthenticate/, /\/password\//, /\/email\//, /#/];
// Downloads checked as downloads (expect a file, not a page): the meeting
// packet .zip (WT-003, WT-042).
const DOWNLOADS = [{ re: /\/meetings\/\d+\/packets\/\d+\/zip\/?$/, type: 'application/zip' }];
const MAX_PAGES = Number(process.env.WALK_MAX_PAGES || 200);
const CONCURRENCY = Number(process.env.WALK_CONCURRENCY || 3);
const TAP = 40;

const pattern = (p) => {
  const [pth, q = ''] = p.split('?');
  const mine = /(^|&)mine=1(&|$)/.test(q) ? '?mine=1' : '';
  return pth.replace(/\/\d+(?=\/|$)/g, '/{id}') + mine;
};
const slug = (key) => (key === '/' ? 'home' : key.replace(/\{id\}/g, 'id').replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '').toLowerCase());
const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

// --- In-page readers (run in the browser) ---------------------------------------

function readMenu() {
  const txt = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const clean = (el) => {
    if (!el) return '';
    const c = el.cloneNode(true);
    c.querySelectorAll('.badge-count, .sr-only, svg').forEach((n) => n.remove());
    return txt(c);
  };
  const badge = (el) => {
    const b = el && el.querySelector('.badge-count');
    return b && b.firstChild ? Number(b.firstChild.textContent.trim()) || 0 : 0;
  };
  // The work sections sit inside one Board group (D64): .nav > .nav-work > .nav-section.
  const sections = [...document.querySelectorAll('#sidebar .nav > .nav-section, #sidebar .nav > .nav-work > .nav-section')].map((sec) => {
    const summary = sec.querySelector(':scope > summary');
    return {
      key: sec.getAttribute('data-nav-group') || '',
      work: !!sec.closest('.nav-work'),
      title: clean(summary),
      count: badge(summary),
      open: sec.tagName === 'DETAILS' ? sec.open : true,
      items: [...sec.querySelectorAll(':scope > a')].map((a) => ({
        label: clean(a), href: a.getAttribute('href'), count: badge(a), current: a.getAttribute('aria-current') === 'page',
      })),
    };
  });
  const foot = [...document.querySelectorAll('#sidebar .foot-links a')].map((a) => ({ label: clean(a), href: a.getAttribute('href') }));
  return { sections, foot, roles: txt(document.querySelector('#sidebar .who-sub')) };
}

function readPage() {
  const txt = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const clean = (el) => {
    if (!el) return '';
    const c = el.cloneNode(true);
    c.querySelectorAll('.badge-count, .sr-only, svg').forEach((n) => n.remove());
    return txt(c);
  };
  const main = document.querySelector('#main') || document.body;
  const uniq = (xs) => [...new Set(xs.filter(Boolean))];
  const labelFor = (f) => {
    if (f.id) { const l = document.querySelector(`label[for="${CSS.escape(f.id)}"]`); if (l) return clean(l); }
    const wrap = f.closest('label'); if (wrap) return clean(wrap);
    return f.getAttribute('aria-label') || f.getAttribute('name') || f.type || f.tagName.toLowerCase();
  };
  const forms = [...main.querySelectorAll('form')].map((f) => ({
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    fields: uniq([...f.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')].map(labelFor)).slice(0, 30),
    submit: uniq([...f.querySelectorAll('button, input[type=submit]')].map((b) => clean(b) || b.value)).slice(0, 10),
  }));
  const buttons = uniq([...main.querySelectorAll('.page-actions a, .page-actions button, a.btn, button, input[type=submit]')]
    .map((b) => clean(b) || b.value || b.getAttribute('aria-label'))).slice(0, 40);
  const links = [...main.querySelectorAll('a[href]')];
  // Hub tiles only: Welcome's tiles introduce pages that are also in the menu.
  const tiles = [...main.querySelectorAll('ul.tiles:not(.welcome-tiles) a.tile')].map((a) => ({
    label: clean(a.querySelector('strong')), href: a.getAttribute('href'), text: clean(a.querySelector('span')),
  }));
  return {
    title: document.title,
    h1: clean(main.querySelector('h1')),
    h1_count: main.querySelectorAll('h1').length,
    subtitle: txt(main.querySelector('.page-head .subtitle')),
    crumbs: uniq([...main.querySelectorAll('.crumbs')].map(clean)),
    headings: uniq([...main.querySelectorAll('h2, h3')].map(clean)).slice(0, 40),
    buttons,
    links: uniq(links.map(clean)).slice(0, 60),
    link_count: links.length,
    hrefs: uniq(links.map((a) => a.getAttribute('href'))),
    forms,
    tables: main.querySelectorAll('table').length,
    tiles,
    // D64: the page carries the Board (or Staff) badge from its menu entry.
    board_badge: !!document.querySelector('[data-board-badge]'),
    height: document.documentElement.scrollHeight,
  };
}

function layoutChecks(args) {
  const { phone, tap } = args;
  const vw = document.documentElement.clientWidth;
  const scroll = document.documentElement.scrollWidth - window.innerWidth;
  const main = document.querySelector('#main') || document.body;
  const scope = [main, document.querySelector('.topbar')].filter(Boolean);
  const all = (sel) => scope.flatMap((s) => [...s.querySelectorAll(sel)]);
  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    // Inside a closed <details> (other than its summary): not shown.
    const closed = el.closest('details:not([open])');
    if (closed && !el.closest('summary')) return false;
    if (el.checkVisibility && !el.checkVisibility({ visibilityProperty: true, contentVisibilityAuto: true })) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const scroller = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll') return p;
    }
    return null;
  };
  const describe = (el, extra) => {
    const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/)[0] : '';
    const t = (el.textContent || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 30);
    return `${el.tagName.toLowerCase()}${cls}${t ? ` "${t}"` : ''} ${extra}`;
  };
  const overflow = [];
  for (const el of all('button, input:not([type=hidden]), select, textarea, .card, .stat, .tile, td, th, a.btn, img, table, pre, .page-actions')) {
    if (!visible(el) || scroller(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 || r.left < -1) { overflow.push(describe(el, `runs off the screen (${Math.round(r.left)}..${Math.round(r.right)} of ${vw}px)`)); continue; }
    const parent = el.parentElement;
    if (!['TD', 'TH', 'TABLE'].includes(el.tagName) && parent && parent.clientWidth > 0) {
      const pr = parent.getBoundingClientRect();
      const pad = parseFloat(getComputedStyle(parent).paddingRight) || 0;
      if (r.right > pr.right - pad + 1 && getComputedStyle(parent).overflowX === 'visible') {
        overflow.push(describe(el, `wider than its container (${Math.round(r.width)}px in ${Math.round(pr.width)}px)`));
        continue;
      }
    }
    if ((el.tagName === 'TD' || el.tagName === 'TH' || el.classList.contains('card')) && el.scrollWidth > el.clientWidth + 1
      && getComputedStyle(el).overflowX === 'visible') {
      overflow.push(describe(el, `content spills out (${el.scrollWidth}px in ${el.clientWidth}px)`));
    }
  }
  const small = [];
  if (phone) {
    for (const el of all('a[href], button, input[type=submit], input[type=button], input[type=checkbox], input[type=radio], select, summary')) {
      if (!visible(el)) continue;
      if (el.tagName === 'A' && getComputedStyle(el).display === 'inline') {
        // A link inside running text (a sentence) is exempt.
        const p = el.parentElement;
        const around = p ? p.textContent.replace(/\s+/g, ' ').trim().length - el.textContent.replace(/\s+/g, ' ').trim().length : 0;
        if (around > 3) continue;
      }
      const target = (el.type === 'checkbox' || el.type === 'radio') && el.closest('label') ? el.closest('label') : el;
      const r = target.getBoundingClientRect();
      if (r.width < tap || r.height < tap) small.push(describe(el, `${Math.round(r.width)}x${Math.round(r.height)}px`));
    }
  }
  // Action rows that sit on, or touch, the boxes after them (D88: the
  // dashboard's Record payments received sat on the stat tiles), and stat
  // tiles or tiles that overlap a button. Cards in one column of the
  // dashboard have one width (WT-033).
  const touch = [];
  const meets = (a, b) => a.left < b.right && b.left < a.right;
  for (const row of all('.form-actions, .quick-row, .actions-row')) {
    if (!visible(row)) continue;
    const next = row.nextElementSibling;
    if (!next || !visible(next)) continue;
    const a = row.getBoundingClientRect();
    const b = next.getBoundingClientRect();
    if (meets(a, b) && b.top < a.bottom + 4) touch.push(describe(row, `touches the ${next.tagName.toLowerCase()}${typeof next.className === 'string' && next.className ? '.' + next.className.split(/\s+/)[0] : ''} below it (${Math.round(b.top - a.bottom)}px apart)`));
  }
  const boxes = all('.stat, .tiles .tile').filter(visible).map((el) => [el, el.getBoundingClientRect()]);
  for (const btn of all('a.btn, button').filter(visible)) {
    const r = btn.getBoundingClientRect();
    for (const [box, b] of boxes) {
      if (box.contains(btn) || btn.contains(box)) continue;
      if (r.left < b.right - 1 && b.left < r.right - 1 && r.top < b.bottom - 1 && b.top < r.bottom - 1) {
        touch.push(describe(btn, `overlaps ${describe(box, '')}`));
        break;
      }
    }
  }
  const dash = document.querySelector('.split.dash');
  if (phone && dash) {
    const widths = all('.split.dash .card').filter(visible).map((el) => Math.round(el.getBoundingClientRect().width));
    if (widths.length > 1 && Math.max(...widths) - Math.min(...widths) > 2) touch.push(`dashboard cards differ in width (${widths.join(', ')}px)`);
  }
  return { scroll, overflow, small, touch };
}

// --- Crawl ---------------------------------------------------------------------------

async function context(browser, role, width) {
  const ctx = await browser.newContext(WIDTHS[width]);
  await ctx.addCookies([{ name: 'sessionid', value: sessions[role], url: base }]);
  const page = await ctx.newPage();
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
  return { ctx, page, errors };
}

async function menuFor(view, role, width, add) {
  const { page } = view;
  await page.goto(base + '/', { waitUntil: 'load' });
  const dir = path.join(shotsDir, role, width);
  fs.mkdirSync(dir, { recursive: true });
  let opened = true;
  if (width === 'phone') {
    const toggle = page.locator('.topbar [data-nav-toggle]');
    if (await toggle.count()) {
      await toggle.click();
      await page.waitForTimeout(400);
    }
    opened = await page.evaluate(() => {
      const s = document.querySelector('#sidebar');
      return !!s && getComputedStyle(s).visibility === 'visible' && s.getBoundingClientRect().right > 0;
    });
    add(`${role} phone: the menu opens from the Menu button`, opened, opened ? '' : 'sidebar not visible after tapping Menu');
  }
  const menu = await page.evaluate(readMenu);
  // Open every group, then measure the entries (tap size on a phone).
  const sizes = await page.evaluate((tap) => {
    document.querySelectorAll('#sidebar details').forEach((d) => { d.open = true; });
    return [...document.querySelectorAll('#sidebar .nav a, #sidebar summary')].map((a) => {
      const r = a.getBoundingClientRect();
      return { label: a.textContent.replace(/\s+/g, ' ').trim().slice(0, 40), w: Math.round(r.width), h: Math.round(r.height), ok: r.width >= tap && r.height >= tap };
    });
  }, TAP);
  if (width === 'phone' && opened) {
    const bad = sizes.filter((s) => !s.ok);
    add(`${role} phone: menu entries are at least ${TAP}x${TAP} px`, bad.length === 0,
      bad.slice(0, 8).map((s) => `"${s.label}" ${s.w}x${s.h}`).join('; '));
  }
  // Screenshot of the whole menu with every group open.
  const shot = path.join(dir, 'menu.png');
  try {
    await page.evaluate(() => {
      const s = document.querySelector('#sidebar');
      if (s) Object.assign(s.style, { position: 'absolute', top: '0', left: '0', bottom: 'auto', height: 'auto', maxHeight: 'none', overflow: 'visible', transform: 'none', transition: 'none' });
    });
    await page.locator('#sidebar').screenshot({ path: shot });
    menu.screenshot = path.relative(process.cwd(), shot);
  } catch (e) { menu.screenshot = ''; }
  return menu;
}

async function visit(view, role, width, p) {
  const { page, errors } = view;
  errors.length = 0;
  let status = 0;
  let redirects = 0;
  try {
    const resp = await page.goto(base + p, { waitUntil: 'load' });
    status = resp ? resp.status() : 0;
    for (let r = resp && resp.request().redirectedFrom(); r; r = r.redirectedFrom()) redirects += 1;
  } catch (e) { errors.push(String(e).slice(0, 200)); }
  await page.waitForTimeout(50);
  const u = new URL(page.url());
  const final = u.pathname + u.search;
  let info = {};
  let layout = { scroll: 0, overflow: [], small: [], touch: [] };
  try {
    info = await page.evaluate(readPage);
    layout = await page.evaluate(layoutChecks, { phone: width === 'phone', tap: TAP });
  } catch (e) { errors.push(String(e).slice(0, 200)); }
  const key = pattern(p);
  const file = path.join(shotsDir, role, width, `${slug(key)}.png`);
  try { await page.screenshot({ path: file, fullPage: true }); } catch (e) { /* page gone: recorded by status */ }
  return {
    key, path: p, final, status, redirects, ...info, layout,
    console: [...errors], screenshot: path.relative(process.cwd(), file),
  };
}

async function walkRole(browser, role) {
  const results = [];
  const add = (check, ok, detail = '', evidence = '') => results.push({ check, status: ok ? 'pass' : 'fail', detail, evidence });
  const views = {};
  const inv = {};
  for (const width of Object.keys(WIDTHS)) {
    views[width] = await context(browser, role, width);
    inv[width] = { menu: await menuFor(views[width], role, width, add), pages: [], hubs: {} };
  }
  const via = new Map(); // pattern -> how it was reached
  const queue = [];
  const downloads = new Map(); // pattern -> href
  const push = (href, how, from) => {
    if (!href || !href.startsWith('/') || href.startsWith('//')) return;
    const key = pattern(href);
    if (DOWNLOADS.some((d) => d.re.test(href)) && !downloads.has(key)) downloads.set(key, href);
    if (via.has(key) || SKIP.some((re) => re.test(href))) return;
    via.set(key, { how, from });
    queue.push(href);
  };
  push('/', 'home', '');
  for (const width of Object.keys(WIDTHS)) {
    for (const sec of inv[width].menu.sections) for (const item of sec.items) push(item.href, 'menu', sec.title || '(top)');
    for (const item of inv[width].menu.foot) push(item.href, 'menu', 'footer');
  }
  const pages = {};
  let count = 0;
  while (queue.length && count < MAX_PAGES) {
    const p = queue.shift();
    count += 1;
    const key = pattern(p);
    for (const width of Object.keys(WIDTHS)) {
      const rec = await visit(views[width], role, width, p);
      rec.via = via.get(key).how;
      rec.from = via.get(key).from;
      inv[width].pages.push(rec);
      (pages[key] = pages[key] || {})[width] = rec;
      const ev = rec.screenshot;
      add(`${role} ${width}: ${key} has no sideways scroll`, rec.layout.scroll <= 1, `${rec.layout.scroll}px wider than the screen`, ev);
      add(`${role} ${width}: ${key} nothing runs off screen or out of its box`, rec.layout.overflow.length === 0,
        rec.layout.overflow.slice(0, 6).join('; '), ev);
      add(`${role} ${width}: ${key} buttons and boxes do not overlap or touch`, (rec.layout.touch || []).length === 0,
        (rec.layout.touch || []).slice(0, 6).join('; '), ev);
      if (width === 'phone') {
        add(`${role} phone: ${key} tap targets at least ${TAP}x${TAP} px`, rec.layout.small.length === 0,
          `${rec.layout.small.length} small: ${rec.layout.small.slice(0, 6).join('; ')}`, ev);
      }
      add(`${role} ${width}: ${key} has no console or CSP errors`, rec.console.length === 0, rec.console.join(' | '), ev);
      if (rec.tiles && rec.tiles.length) {
        inv[width].hubs[key] = { h1: rec.h1, tiles: rec.tiles };
        for (const t of rec.tiles) push(t.href, 'hub', rec.h1 || key);
      }
    }
    const d = pages[key].desktop;
    const redirected = p !== '/' && pattern(d.final) !== key;
    add(`${role}: ${key} opens a page (HTTP 200, not only a redirect)`, d.status === 200 && !redirected,
      `HTTP ${d.status}${redirected ? ` -> ${pattern(d.final)}` : ''}`, d.screenshot);
    for (const href of d.hrefs || []) push(href, 'link', key);
    for (const href of (pages[key].phone && pages[key].phone.hrefs) || []) push(href, 'link', key);
  }
  if (queue.length) add(`${role}: walkthrough reached every page type`, false, `stopped at ${MAX_PAGES} pages; ${queue.length} not visited`);
  for (const [key, href] of downloads) {
    const want = DOWNLOADS.find((d) => d.re.test(href));
    let status = 0;
    let type = '';
    try {
      const resp = await views.desktop.page.request.get(base + href, { maxRedirects: 0 });
      status = resp.status();
      type = (resp.headers()['content-type'] || '').split(';')[0];
      await resp.body();
    } catch (e) { type = String(e).slice(0, 120); }
    add(`${role}: ${key} downloads a file (HTTP 200, ${want.type})`, status === 200 && type === want.type,
      `HTTP ${status} ${type}`);
  }

  // Menu label = page <h1>; no duplicate labels in the menu.
  for (const width of Object.keys(WIDTHS)) {
    const { menu, hubs } = inv[width];
    const seen = new Map();
    const entries = [];
    for (const sec of menu.sections) for (const item of sec.items) entries.push({ kind: 'menu', label: item.label, href: item.href, where: sec.title || '(top)' });
    for (const [hubKey, hub] of Object.entries(hubs)) for (const t of hub.tiles) entries.push({ kind: 'hub tile', label: t.label, href: t.href, where: hub.h1 || hubKey });
    for (const e of entries) {
      const l = norm(e.label).toLowerCase();
      (seen.get(l) || seen.set(l, []).get(l)).push(`${e.kind} in ${e.where} -> ${e.href}`);
      const rec = pages[pattern(e.href)] && pages[pattern(e.href)][width];
      // The Dashboard's heading is a greeting (core/tests/test_ux_nav.py exempts it too).
      if (!rec || pattern(e.href) === '/') continue;
      const ok = norm(rec.h1).toLowerCase() === norm(e.label).toLowerCase();
      add(`${role} ${width}: ${e.kind} "${e.label}" (${pattern(e.href)}) matches its page heading`, ok,
        ok ? '' : `${e.kind} "${e.label}" (${e.where}) opens a page titled "${rec.h1 || '(no h1)'}"`, rec.screenshot);
    }
    const dups = [...seen.entries()].filter(([, v]) => v.length > 1);
    add(`${role} ${width}: no duplicate labels in the menu`, dups.length === 0,
      dups.map(([k, v]) => `"${k}": ${v.join(', ')}`).join('; '), menu.screenshot);
    inv[width].menu.item_count = menu.sections.reduce((n, s) => n + s.items.length, 0);
    inv[width].menu.tile_count = Object.values(hubs).reduce((n, h) => n + h.tiles.length, 0);
    for (const rec of inv[width].pages) delete rec.hrefs;
  }
  for (const v of Object.values(views)) await v.ctx.close();
  console.log(`${role}: ${count} page types, ${results.filter((r) => r.status === 'fail').length} failing checks`);
  return { role, inv, results };
}

(async () => {
  const started = Date.now();
  const browser = await chromium.launch({ args: ['--no-proxy-server'] });
  const done = {};
  const todo = [...roles];
  async function worker() {
    while (todo.length) {
      const role = todo.shift();
      try {
        done[role] = await walkRole(browser, role);
      } catch (e) {
        done[role] = { role, inv: {}, results: [{ check: `${role}: walkthrough ran`, status: 'fail', detail: String(e).slice(0, 400), evidence: '' }] };
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, roles.length) }, worker));
  await browser.close();
  // Results in a fixed role order so new IDs are numbered the same way every time.
  const results = roles.flatMap((r) => done[r].results);
  const inventory = {
    generated: new Date().toISOString(),
    seconds: Math.round((Date.now() - started) / 1000),
    widths: Object.fromEntries(Object.entries(WIDTHS).map(([k, v]) => [k, v.viewport])),
    tap_target_px: TAP,
    roles: Object.fromEntries(roles.map((r) => [r, done[r].inv])),
  };
  fs.writeFileSync(inventoryFile, JSON.stringify(inventory, null, 1));
  fs.writeFileSync(checksFile, JSON.stringify(results, null, 1));
  console.log(`${results.length} checks, ${results.filter((r) => r.status === 'fail').length} failing, ${inventory.seconds}s`);
})();
