// #90 KEYSTONE verify — boot the built dist, drive to the Entity Graph panel and
// the Why room (lineage + entity graphs), screenshot each, and PROVE the fix:
//   - cytoscape <canvas> elements have NON-ZERO height
//   - NO "reading 'h'" (or any cytoscape layout) console error
//   - nodes/edges actually drawn (cy.nodes().length etc. via a DOM probe)
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('/work/node_modules/playwright-core');

const DIST = '/dist';
const REGISTRY = 'http://legba-registry:8090';
const TOKEN = process.env.LEGBA_TOKEN || '';
const PORT = 8080;
const OUT = '/work/shots';
fs.mkdirSync(OUT, { recursive: true });

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.geojson': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
  '.woff': 'font/woff', '.ico': 'image/x-icon', '.map': 'application/json',
};

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

// Probe every cytoscape canvas on the page: bounding-box size of each <canvas>.
async function canvasProbe(page) {
  return page.evaluate(() => {
    const cs = Array.from(document.querySelectorAll('canvas'));
    return cs.map((c) => {
      const r = c.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height), attrH: c.height, attrW: c.width };
    });
  });
}

(async () => {
  await new Promise((r) => server.listen(PORT, r));
  console.log('static server on ' + PORT);

  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 1 });

  // Seed bearer + clear any saved layout so we always boot the default grid.
  await ctx.addInitScript((tok) => {
    try {
      localStorage.setItem('legba_token', tok);
      localStorage.removeItem('legba_dockview_personal');
      localStorage.removeItem('legba_dockview_cis');
      localStorage.removeItem('legba_nav_collapsed');
    } catch (e) {}
  }, TOKEN);

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
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text().slice(0, 240)); });
  page.on('pageerror', (e) => errs.push('PAGEERROR ' + String(e).slice(0, 240)));

  await page.goto('http://localhost:' + PORT + '/', { waitUntil: 'networkidle', timeout: 35000 }).catch((e) => console.log('goto:', String(e).slice(0, 120)));
  await page.waitForTimeout(3500);
  await page.screenshot({ path: OUT + '/00-boot.png', fullPage: false });
  const hAt = () => errs.filter((e) => /reading '?h'?/.test(e)).length;
  console.log('STEP0_BOOT h-errs=', hAt());

  // Helper: open a singleton panel by its sidebar/command-palette title.
  async function openViaPalette(title) {
    await page.keyboard.press('Control+k');
    await page.waitForTimeout(700);
    await page.keyboard.type(title, { delay: 25 });
    await page.waitForTimeout(900);
    // Enter selects the top result.
    await page.keyboard.press('Enter');
    await page.waitForTimeout(2500);
  }

  // Helper: bring the Why tab forward. Dockview tab titles render in `.dv-tab`;
  // the v4.why panel's title is "Why · Provenance".
  async function activateWhyTab() {
    const tabs = await page.$$('.dv-tab');
    for (const t of tabs) {
      const txt = ((await t.innerText().catch(() => '')) || '').trim();
      if (/why/i.test(txt)) {
        await t.click({ timeout: 3000 }).catch(() => {});
        return true;
      }
    }
    try { await page.getByText('Why · Provenance', { exact: false }).first().click({ timeout: 3000 }); return true; } catch (e) {}
    return false;
  }
  async function dumpTabs(label) {
    const titles = await page.$$eval('.dv-tab', (els) => els.map((e) => (e.textContent || '').trim()));
    console.log('TABS@' + label + ':', JSON.stringify(titles));
  }
  // Probe the canvases inside the Why panel (the v4.why DOM subtree).
  async function whyCanvasProbe() {
    return page.evaluate(() => {
      // The Why room renders ProvenanceTrail + (LineageGraph | EntityGraph).
      const roots = [
        document.querySelector('[data-testid="why-entity-graph"]'),
        ...Array.from(document.querySelectorAll('[data-testid="why-provenance-trail"]')).map((e) => e.parentElement),
      ].filter(Boolean);
      // Fall back to ANY canvas that is not inside the system entity-graph panel.
      const sys = document.querySelector('[data-testid="entity-graph-canvas"]');
      const all = Array.from(document.querySelectorAll('canvas')).filter((c) => !(sys && sys.contains(c)));
      return all.map((c) => {
        const r = c.getBoundingClientRect();
        return { h: Math.round(r.height), w: Math.round(r.width) };
      });
    });
  }

  // ---- 1) Why room — LineageGraph (click a finding in the Live Feed FIRST,
  // while the feed is the visible boot tab). The feed cards are
  // <button data-testid="finding-<id>"> whose onClick fires selectRow('finding'..).
  let clickedFinding = null;
  const findingBtns = await page.$$('button[data-testid^="finding-"]');
  for (const b of findingBtns) {
    const tid = await b.getAttribute('data-testid');
    if (!tid || /finding-(live|superseded)-/.test(tid)) continue;
    if (!await b.isVisible().catch(() => false)) continue;
    await b.click({ timeout: 4000 }).catch(() => {});
    clickedFinding = tid;
    break;
  }
  await page.waitForTimeout(1500);
  await activateWhyTab();
  await page.waitForTimeout(3800);
  await page.screenshot({ path: OUT + '/02-why-lineage.png', fullPage: false });
  const lineageProbe = await whyCanvasProbe();
  console.log('CLICKED_FINDING:', JSON.stringify(clickedFinding));
  console.log('WHY_LINEAGE_CANVASES:', JSON.stringify(lineageProbe));
  console.log('STEP1_LINEAGE h-errs=', hAt());

  // ---- 2) System Entity Graph panel (renders the top subgraph by default) ----
  await openViaPalette('Entity Graph');
  await page.waitForTimeout(3800); // let cose (run from the resize observer) settle
  await page.screenshot({ path: OUT + '/01-entity-graph.png', fullPage: false });
  const errsAfterEntity = errs.length;
  const entityCanvasContainer = await page.$('[data-testid="entity-graph-canvas"]');
  const entityCanvases = entityCanvasContainer
    ? await entityCanvasContainer.evaluate((el) =>
        Array.from(el.querySelectorAll('canvas')).map((c) => {
          const r = c.getBoundingClientRect();
          return { h: Math.round(r.height), w: Math.round(r.width) };
        }))
    : [];
  console.log('ENTITY_GRAPH_CANVASES:', JSON.stringify(entityCanvases));
  console.log('STEP2_SYS_ENTITY_GRAPH h-errs=', hAt());

  // ---- 3) Why room — EntityGraph (select an entity via the Entities panel) ----
  // The palette only indexes target/analyst/source, NOT entities. In the Entities
  // panel a row click EXPANDS it (setOpen); the global entity selection is fired
  // by the "open in graph →" button (data-testid="entities-open-graph-<id>"),
  // which calls select({kind:'entity'..}) → the Why room renders the EntityGraph.
  // Open the Entities panel via the sidebar item (exact text "Entities"), which
  // is more deterministic than the fuzzy palette (which also matches "Entity Graph").
  try {
    const sideEntities = await page.$$('button, a, [role="button"]');
    let opened = false;
    for (const el of sideEntities) {
      const box = await el.boundingBox().catch(() => null);
      if (!box || box.x > 300) continue;                  // sidebar only
      const t = ((await el.innerText().catch(() => '')) || '').trim();
      if (t === 'Entities') { await el.click({ timeout: 4000 }).catch(() => {}); opened = true; break; }
    }
    if (!opened) await openViaPalette('Entities');
  } catch (e) { await openViaPalette('Entities'); }
  await page.waitForTimeout(3000);
  const entRowCount = (await page.$$('button[data-testid^="entities-row-"]')).length;
  console.log('ENTITIES_ROW_COUNT:', entRowCount);
  // Make sure the Entities panel is the active/visible tab before reading rows.
  let clickedEntity = null;
  let openedInGraph = null;
  const entityRows = await page.$$('button[data-testid^="entities-row-"]');
  for (const r of entityRows) {
    if (!await r.isVisible().catch(() => false)) continue;
    await r.click({ timeout: 4000 }).catch(() => {});       // expand the row
    clickedEntity = (await r.getAttribute('data-testid')) || 'row';
    break;
  }
  console.log('CLICKED_ENTITY_ROW:', JSON.stringify(clickedEntity));
  await page.waitForTimeout(1200);
  const openGraphBtns = await page.$$('button[data-testid^="entities-open-graph-"]');
  for (const b of openGraphBtns) {
    if (!await b.isVisible().catch(() => false)) continue;
    await b.click({ timeout: 4000 }).catch(() => {});       // → global entity selection
    openedInGraph = (await b.getAttribute('data-testid')) || 'open-graph';
    break;
  }
  console.log('CLICKED_OPEN_IN_GRAPH:', JSON.stringify(openedInGraph));
  await page.waitForTimeout(1500);
  // The Why EntityGraph mounts cytoscape only once its tab is visible+sized
  // (useVisibleSize). Re-activate the Why tab each poll iteration (other panels
  // opened in the same group may steal foreground) until the canvas appears.
  await dumpTabs('step3');
  let whyMounted = 0;
  for (let i = 0; i < 14; i++) {
    await activateWhyTab();
    await page.waitForTimeout(900);
    whyMounted = await page.evaluate(() => {
      const c = document.querySelector('[data-testid="why-entity-graph"]');
      return c ? c.querySelectorAll('canvas').length : 0;
    });
    if (whyMounted > 0) break;
  }
  console.log('WHY_ENTITY_MOUNT_POLL_CANVASES:', whyMounted);
  const whyDiag = await page.evaluate(() => {
    const c = document.querySelector('[data-testid="why-entity-graph"]');
    if (!c) return 'no-why-entity-graph-div';
    const r = c.getBoundingClientRect();
    return { offParentNull: c.offsetParent === null, boxW: Math.round(r.width), boxH: Math.round(r.height),
      canvases: c.querySelectorAll('canvas').length };
  });
  console.log('WHY_ENTITY_DIAG:', JSON.stringify(whyDiag));
  const whyInner = await page.evaluate(() => {
    const c = document.querySelector('[data-testid="why-entity-graph"]');
    if (!c) return 'absent';
    return { childCount: c.childElementCount, html: c.innerHTML.slice(0, 200) };
  });
  console.log('WHY_ENTITY_INNER:', JSON.stringify(whyInner));
  await page.waitForTimeout(2500);
  await page.screenshot({ path: OUT + '/03-why-entity.png', fullPage: false });
  const whyEntityEl = await page.$('[data-testid="why-entity-graph"]');
  const whyEntityCanvases = whyEntityEl
    ? await whyEntityEl.evaluate((el) =>
        Array.from(el.querySelectorAll('canvas')).map((c) => {
          const r = c.getBoundingClientRect();
          return { h: Math.round(r.height), w: Math.round(r.width) };
        }))
    : [];
  console.log('WHY_ENTITY_GRAPH_FOUND:', !!whyEntityEl);
  console.log('WHY_ENTITY_GRAPH_CANVASES:', JSON.stringify(whyEntityCanvases));
  console.log('STEP3_WHY_ENTITY h-errs=', hAt());

  // ---- 4) Re-activate the system Entity Graph tab — it was hidden during the
  // entity re-center; confirm it RE-MOUNTS cleanly (no crash, canvas sized). ----
  await openViaPalette('Entity Graph');
  // The system entity graph centered on the selected entity (United States);
  // wait for the re-mounted cose to settle, then probe.
  for (let i = 0; i < 12; i++) {
    const n = await page.evaluate(() => {
      const c = document.querySelector('[data-testid="entity-graph-canvas"]');
      return c ? c.querySelectorAll('canvas').length : 0;
    });
    if (n > 0) break;
    await page.waitForTimeout(800);
  }
  await page.waitForTimeout(3000);
  await page.screenshot({ path: OUT + '/05-sys-entity-recenter.png', fullPage: false });
  const sysBox = await page.evaluate(() => {
    const c = document.querySelector('[data-testid="entity-graph-canvas"]');
    if (!c) return 'absent';
    const r = c.getBoundingClientRect();
    return { boxW: Math.round(r.width), boxH: Math.round(r.height),
      canvases: Array.from(c.querySelectorAll('canvas')).map((cv) => Math.round(cv.getBoundingClientRect().height)) };
  });
  console.log('SYS_ENTITY_GRAPH_AFTER_RECENTER:', JSON.stringify(sysBox));
  console.log('STEP4_SYS_ENTITY_REMOUNT h-errs=', hAt());

  await page.screenshot({ path: OUT + '/04-full.png', fullPage: true });

  // ---- Verdict ----
  const hReadErrs = errs.filter((e) => /reading '?h'?/.test(e) || /Cannot read propert.*'h'/.test(e));
  const layoutErrs = errs.filter((e) => /layout|cose|breadthfirst|concentric|bounding/i.test(e));
  console.log('TOTAL_CONSOLE_ERRORS:', errs.length);
  console.log('H_READ_ERRORS:', JSON.stringify(hReadErrs));
  console.log('LAYOUT_RELATED_ERRORS:', JSON.stringify(layoutErrs));
  console.log('ALL_ERRORS_SAMPLE:', JSON.stringify(errs.filter((e) => !/WebSocket/.test(e)).slice(0, 12)));
  console.log('ERRORS_DURING_ENTITY_PANEL:', errsAfterEntity, '(count at entity screenshot)');

  await browser.close();
  server.close();
  console.log('DONE');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
