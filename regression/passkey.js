// Page stage, part 2: passkey sign-in end to end with Chromium's virtual
// authenticator (password + app code, add a passkey, sign out, sign in with it).
//   node regression/passkey.js <base url on localhost> <user.json> <totp command>
// Prints one JSON result line per check.
const { chromium } = require('playwright');
const { execSync } = require('child_process');
const fs = require('fs');
const [base, userFile, totpCmd] = process.argv.slice(2);
const u = JSON.parse(fs.readFileSync(userFile, 'utf8'));
const code = () => execSync(totpCmd + ' ' + u.secret).toString().trim().split('\n').pop();
const report = (check, ok, detail = '') =>
  console.log(JSON.stringify({ check: `passkey: ${check}`, status: ok ? 'pass' : 'fail', detail }));
(async () => {
  const browser = await chromium.launch({ args: ['--no-proxy-server'] });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const auth = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: {
    protocol: 'ctap2', transport: 'internal', hasResidentKey: true,
    hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
  // 1. Password + authenticator app.
  await page.goto(base + '/accounts/login/');
  await page.fill('input[name=login]', u.email);
  await page.fill('input[name=password]', u.password);
  await page.click('form[action*="login"] button[type=submit]');
  await page.waitForURL(/2fa\/authenticate/);
  await page.fill('input[name=code]', code());
  await page.click('button[type=submit]');
  await page.waitForURL(base + '/');
  report('signs in with password and authenticator app', true);
  // 2. Add a passkey.
  await page.goto(base + '/accounts/2fa/webauthn/add/?passwordless=1');
  if (page.url().includes('reauthenticate')) {
    await page.fill('input[name=password]', u.password);
    await Promise.all([page.waitForNavigation(), page.click('main button[type=submit]')]);
  }
  const before = (await cdp.send('WebAuthn.getCredentials', { authenticatorId: auth.authenticatorId })).credentials.length;
  const name = await page.$('input[name=name]');
  if (name) await name.fill('E2E key');
  const pw = await page.$('input[name=passwordless]');
  if (pw && !(await pw.isChecked())) await pw.check();
  await Promise.all([page.waitForNavigation({ timeout: 15000 }), page.click('#mfa_webauthn_add')]);
  const after = (await cdp.send('WebAuthn.getCredentials', { authenticatorId: auth.authenticatorId })).credentials.length;
  report('adds a passkey', after === before + 1, `credentials ${before} -> ${after}`);
  // 3. Sign out, then sign in with the passkey only.
  await page.goto(base + '/accounts/logout/');
  await Promise.all([page.waitForNavigation(), page.click('main button[type=submit]')]);
  await page.goto(base + '/accounts/login/');
  await Promise.all([page.waitForNavigation({ timeout: 15000 }), page.click('#passkey_login')]);
  const final = new URL(page.url()).pathname;
  const greeting = await page.textContent('h1').catch(() => '');
  report('signs in with the passkey alone', final === '/' && /Passkey Tester/.test(greeting), final);
  report('no console errors during passkey flows', errors.length === 0, errors.join(' | '));
  await browser.close();
})().catch((e) => { report('flow completed', false, e.message.split('\n')[0]); process.exit(1); });
