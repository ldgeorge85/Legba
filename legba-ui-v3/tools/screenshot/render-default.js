// Legba UI screenshot harness — serves the built dist, proxies /api/v1 to the
// registry with the bearer, renders in Chromium, captures the default view +
// dumps the menu/interactive elements so we can drive panel navigation next.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('/work/node_modules/playwright-core');

const DIST = '/dist';
const REGISTRY = 'http://legba-registry:8090';
const TOKEN = process.env.LEGBA_TOKEN || '';
const PORT = 8080;

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.geojson': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
  '.woff': 'font/woff', '.ico': 'image/x-icon', '.map': 'application/json',
};

// Static server with SPA fallback to index.html.
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0]);
  let fp = path.join(DIST, p);
  if (!fp.startsWith(DIST)) { res.writeHead(403); return res.end(); }
  fs.stat(fp, (err, st) => {
    if (!err && st.isFile()) {
      res.writeHead(200, { 'content-type': MIME[path.extname(fp)] || 'application/octet-stream' });
      fs.createReadStream(fp).pipe(res);
    } else {
      res.writeHead(200, { 'content-type': 'text/html' });
      fs.createReadStream(path.join(DIST, 'index.html')).pipe(res);
    }
  });
});

(async () => {
  await new Promise((r) => server.listen(PORT, r));
  console.log('static server on ' + PORT);

  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 1 });

  // Seed the bearer the SPA reads from localStorage.
  await ctx.addInitScript((tok) => { try { localStorage.setItem('legba_token', tok); } catch (e) {} }, TOKEN);

  // Proxy every /api/v1 call to the registry with the bearer header.
  await ctx.route('**/api/v1/**', async (route) => {
    const req = route.request();
    const u = new URL(req.url());
    const target = REGISTRY + u.pathname + u.search;
    try {
      const headers = { ...req.headers(), authorization: 'Bearer ' + TOKEN };
      delete headers.host;
      const init = { method: req.method(), headers };
      if (!['GET', 'HEAD'].includes(req.method())) init.body = req.postData() || undefined;
      const resp = await fetch(target, init);
      const buf = Buffer.from(await resp.arrayBuffer());
      const h = {}; resp.headers.forEach((v, k) => { if (k !== 'content-encoding' && k !== 'transfer-encoding') h[k] = v; });
      await route.fulfill({ status: resp.status, headers: h, body: buf });
    } catch (e) {
      await route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ proxy_error: String(e) }) });
    }
  });

  const page = await ctx.newPage();
  const errs = [];
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text().slice(0, 200)); });
  page.on('pageerror', (e) => errs.push('PAGEERROR ' + String(e).slice(0, 200)));

  await page.goto('http://localhost:' + PORT + '/', { waitUntil: 'networkidle', timeout: 35000 }).catch((e) => console.log('goto:', String(e).slice(0, 120)));
  await page.waitForTimeout(3500);

  await page.screenshot({ path: '/work/shots/00-default-full.png', fullPage: true });
  await page.screenshot({ path: '/work/shots/00-default-view.png', fullPage: false });

  // Dump interactive elements (menu items, tabs, buttons) to plan navigation.
  const items = await page.$$eval('button, a, [role=menuitem], [role=tab], [role=button], nav li, [class*=menu] *, [class*=nav] *',
    (els) => Array.from(new Set(els.map((e) => (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ')).filter((t) => t && t.length < 40))).slice(0, 250));
  const title = await page.title();
  const bodyText = (await page.evaluate(() => document.body.innerText || '')).slice(0, 600);

  console.log('TITLE:', title);
  console.log('CONSOLE_ERRORS:', errs.length, JSON.stringify(errs.slice(0, 8)));
  console.log('INTERACTIVE_ELEMENTS(' + items.length + '):');
  console.log(JSON.stringify(items, null, 0));
  console.log('BODY_TEXT_HEAD:', JSON.stringify(bodyText));

  await browser.close();
  server.close();
  console.log('DONE');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
