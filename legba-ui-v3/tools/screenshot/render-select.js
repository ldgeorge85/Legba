const http=require('http'),fs=require('fs'),path=require('path');
const {chromium}=require('/work/node_modules/playwright-core');
const DIST='/dist',REGISTRY='http://legba-registry:8090',TOKEN=process.env.LEGBA_TOKEN||'',PORT=8080;
const MIME={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.geojson':'application/json','.svg':'image/svg+xml','.png':'image/png','.woff2':'font/woff2','.woff':'font/woff','.ico':'image/x-icon','.map':'application/json'};
const server=http.createServer((req,res)=>{let p=decodeURIComponent(req.url.split('?')[0]),fp=path.join(DIST,p);if(!fp.startsWith(DIST)){res.writeHead(403);return res.end();}fs.stat(fp,(e,st)=>{if(!e&&st.isFile()){res.writeHead(200,{'content-type':MIME[path.extname(fp)]||'application/octet-stream'});fs.createReadStream(fp).pipe(res);}else{res.writeHead(200,{'content-type':'text/html'});fs.createReadStream(path.join(DIST,'index.html')).pipe(res);}});});
(async()=>{
  await new Promise(r=>server.listen(PORT,r));
  const b=await chromium.launch({args:['--no-sandbox']});
  const ctx=await b.newContext({viewport:{width:1680,height:1050}});
  await ctx.addInitScript((t)=>{try{localStorage.setItem('legba_token',t);}catch(e){}},TOKEN);
  await ctx.route('**/api/v1/**',async(route)=>{const req=route.request(),u=new URL(req.url());try{const h={...req.headers(),authorization:'Bearer '+TOKEN};delete h.host;const init={method:req.method(),headers:h};if(!['GET','HEAD'].includes(req.method()))init.body=req.postData()||undefined;const resp=await fetch(REGISTRY+u.pathname+u.search,init);const buf=Buffer.from(await resp.arrayBuffer());const hh={};resp.headers.forEach((v,k)=>{if(k!=='content-encoding'&&k!=='transfer-encoding')hh[k]=v;});await route.fulfill({status:resp.status,headers:hh,body:buf});}catch(e){await route.fulfill({status:502,body:'{}'});}});
  const page=await ctx.newPage();
  const errs=[];page.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,160));});page.on('pageerror',e=>errs.push('PAGEERR '+String(e).slice(0,160)));
  await page.goto('http://localhost:'+PORT+'/',{waitUntil:'networkidle',timeout:35000}).catch(()=>{});
  await page.waitForTimeout(3000);
  // open Live Feed to focus it
  try{ await page.getByText('Live Feed',{exact:true}).first().click({timeout:5000}); await page.waitForTimeout(1500);}catch(e){}
  // click the first feed-row-shaped element to the RIGHT of the sidebar (x>320), excluding the menu
  let clicked=null;
  const cands=await page.$$('[role="button"], li, [class*="row"], [class*="Row"], tr, a');
  for(const c of cands){
    const box=await c.boundingBox().catch(()=>null); if(!box) continue;
    if(box.x<320) continue;                 // skip the left sidebar menu
    if(box.width<160||box.width>760) continue;
    if(box.height<14||box.height>90) continue;
    const t=((await c.innerText().catch(()=> ''))||'').trim();
    if(t.length<18||t.length>240) continue;
    if(!/[a-z]{5}/i.test(t)) continue;
    if(!await c.isVisible().catch(()=>false)) continue;
    await c.click({timeout:4000}).catch(()=>{}); clicked=t.replace(/\s+/g,' ').slice(0,90); break;
  }
  await page.waitForTimeout(2500);
  await page.screenshot({path:'/work/shots/k2-feed-select.png'});
  console.log('CLICKED:',JSON.stringify(clicked));
  console.log('ERRORS('+errs.length+'):',JSON.stringify(errs.filter(e=>!/WebSocket/.test(e)).slice(0,8)));
  await b.close();server.close();console.log('DONE');
})().catch(e=>{console.error('FATAL',e);process.exit(1);});
