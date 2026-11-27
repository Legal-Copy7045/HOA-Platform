// Page stage: crawl the portal as each role, in a real browser.
//   node regression/pages.js <base url> <sessions.json> <out.json>
// For each role, follows same-origin links from the home page (GET only) and checks each
// kind of page once (numbers in the path are treated as one page type):
//   - it loads (HTTP 200) with no console errors and no Content-Security-Policy violations;
//   - at phone width (390 px) it does not scroll sideways.
// Then every page type the board reached is opened as an owner: it must be refused (403/404)
// unless the owner reached that page type themselves.
const { chromium } = require('playwright');
const fs = require('fs');

const [base, sessionsFile, outFile] = process.argv.slice(2);
const sessions = JSON.parse(fs.readFileSync(sessionsFile, 'utf8'));
// Downloads (PDF, CSV, spreadsheets, calendar files, packet zips) are not pages,
// with or without a query string (test.pdf?kind=letter).
// /help/ask/ is the "Ask the board" shortcut: it forwards to a new owner request by design (D73).
const SKIP = [/logout/, /\/help\/ask\//, /\.(pdf|ics|csv|xlsx|zip)(\?|$)/, /\/zip\/?$/, /format=(csv|pdf)/, /download/, /\/files?\//, /\/receipts\//, /\/letters?\//, /statement/,
  /export/, /\/static\//, /\/2fa\/.+/, /reauthenticate/, /\/password\//, /\/email\//, /#/];
const MAX_PAGES = 120;

// Query strings are dropped, except ?mine=1: the homeowner and board sides of
// the same list are different pages (a board member who owns a home sees both).
const pattern = (path) => {
  const [p, q = ''] = path.split('?');
  const mine = /(^|&)mine=1(&|$)/.test(q) ? '?mine=1' : '';
  return p.replace(/\/\d+(?=\/|$)/g, '/{id}') + mine;
};
const results = [];
const add = (check, ok, detail = '') => results.push({ check, status: ok ? 'pass' : 'fail', detail });

async function crawl(browser, role) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addCookies([{ name: 'sessionid', value: sessions[role], url: base }]);
  const page = await ctx.newPage();
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
  await mobile.addCookies([{ name: 'sessionid', value: sessions[role], url: base }]);
  const phone = await mobile.newPage();
  let errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

  const seen = new Map(); // pattern -> concrete path
  const queue = ['/'];
  while (queue.length && seen.size < MAX_PAGES) {
    const path = queue.shift();
    const key = pattern(path);
    if (seen.has(key) || SKIP.some((re) => re.test(path))) continue;
    seen.set(key, path);
    errors = [];
    let status = 0;
    try {
      const resp = await page.goto(base + path, { waitUntil: 'load' });
      status = resp ? resp.status() : 0;
    } catch (e) { errors.push(String(e).slice(0, 200)); }
    const finalUrl = new URL(page.url());
    const finalPath = finalUrl.pathname + finalUrl.search;
    const redirectedAway = pattern(finalPath) !== key && path !== '/';
    add(`${role}: ${key} loads`, status === 200 && !redirectedAway,
      `HTTP ${status}${redirectedAway ? ` -> ${finalPath}` : ''}`);
    add(`${role}: ${key} has no console or CSP errors`, errors.length === 0, errors.join(' | '));
    try {
      await phone.goto(base + path, { waitUntil: 'load' });
      const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      add(`${role}: ${key} fits a phone screen`, overflow <= 1, `${overflow}px wider than the screen`);
    } catch (e) { add(`${role}: ${key} fits a phone screen`, false, String(e).slice(0, 200)); }
    const links = await page.$$eval('a[href]', (as) => as.map((a) => a.getAttribute('href')));
    for (const href of links) {
      if (!href || !href.startsWith('/') || href.startsWith('//')) continue;
      if (!seen.has(pattern(href))) queue.push(href);
    }
  }
  await ctx.close();
  await mobile.close();
  return seen;
}

(async () => {
  const browser = await chromium.launch({ args: ['--no-proxy-server'] });
  const reached = {};
  for (const role of Object.keys(sessions)) reached[role] = await crawl(browser, role);
  // Cross-role: board pages the owner could not reach must be refused to the owner.
  if (reached.board && sessions.owner) {
    const ctx = await browser.newContext();
    await ctx.addCookies([{ name: 'sessionid', value: sessions.owner, url: base }]);
    const page = await ctx.newPage();
    for (const [key, path] of reached.board) {
      // A list the owner reached as "?mine=1" is the same page (shared
      // list, owner side) - not a board page.
      if (reached.owner.has(key) || reached.owner.has(`${key}?mine=1`) || key === '/') continue;
      const resp = await page.goto(base + path);
      const status = resp ? resp.status() : 0;
      add(`owner is refused board page ${key}`, status === 403 || status === 404, `HTTP ${status}`);
    }
    await ctx.close();
  }
  await browser.close();
  fs.writeFileSync(outFile, JSON.stringify(results, null, 1));
  console.log(`${results.length} checks, ${results.filter((r) => r.status === 'fail').length} failing`);
})();
