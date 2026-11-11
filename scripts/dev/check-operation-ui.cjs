// Synthetic directory and mocked worker responses; does not validate native worker or TLS.
const { chromium } = require(process.env.SMOLOFIS_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const assert = require('node:assert/strict');
const root = require('node:path').resolve(__dirname, '../..');
const origin = fs.readFileSync(process.env.SMOLOFIS_AUTH_BROWSER_ORIGIN_FILE || '/private/tmp/smolofis-operation-browser-origin','utf8').trim();
const out = root + '/.impeccable/review/operations';
fs.mkdirSync(out, { recursive:true });
const make = (id, phase='succeeded', outcome='identity_verified') => ({id:id.repeat(64),kind:'machine_identity_check',actor_uid:0,actor_guid:'07'.repeat(16),requester_uid:995,transitions:[{phase:'queued',timestamp_ms:1794380400000},{phase:'running',timestamp_ms:1794380400010},{phase,timestamp_ms:1794380400020}],outcome});
(async()=>{
 const browser = await chromium.launch({headless:true, executablePath:process.env.SMOLOFIS_BROWSER_EXECUTABLE});
 const checks=[];
 for (const [label,width,height,colorScheme] of [['desktop',1440,1000,'light'],['mobile',390,844,'light'],['mobile-dark',390,844,'dark'],['small',320,740,'light']]) {
  const context=await browser.newContext({viewport:{width,height},colorScheme});
  const page=await context.newPage(); const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  let rows=[],status=200,posts=0,uncertain=false,malformed=false;
  await page.route('**/api/state',async route=>{const response=await route.fetch(); const data=await response.json(); data.updated_at=Date.now()/1000; await route.fulfill({json:data});});
  await page.route('**/api/operations',route=>route.fulfill({status,json:malformed?{operations:[{id:'<img src=x onerror=alert(1)>'}]}:{operations:rows}}));
  await page.route('**/api/operations/identity-check',async route=>{posts++;assert.deepEqual(route.request().postDataJSON(),{csrf:await page.locator('[data-operations]').getAttribute('data-csrf')}); rows.push(make('c')); if(uncertain)return route.abort('failed');await new Promise(resolve=>setTimeout(resolve,500));await route.fulfill({status:201,json:rows.at(-1)});});
  await page.goto(origin+'/auth/login'); await page.locator('[name=username]').fill('admin');await page.locator('[name=password]').fill('correct');await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await page.goto(origin+'/console/services');
  const run=page.locator('[data-run-identity]'), refresh=page.locator('[data-refresh-operations]'),notice=page.locator('[data-operation-notice]');
  await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent.includes('No operations recorded'));
  assert.equal(await run.isEnabled(),true);checks.push(label+': empty history and action enabled');
  await run.click();await page.waitForFunction(()=>document.querySelector('[data-operations]').getAttribute('aria-busy')==='true');assert.equal(await run.isDisabled(),true);checks.push(label+': busy state disables actions');await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent==='The server matches its saved identity.');assert.equal(posts,1);
  assert.equal(await page.locator('.operation-row').count(),1);checks.push(label+': one POST with session CSRF and confirmed result');
  rows=[make('a','failed','identity_mismatch'),make('b')];await refresh.click();await page.waitForFunction(()=>document.querySelectorAll('.operation-row').length===2);
  await page.locator('.operation-row summary').first().click();
  assert.equal(await page.locator('.operation-row[open]').count(),1);
  await page.locator('.operation-row summary').first().focus();await page.keyboard.press('Enter');assert.equal(await page.locator('.operation-row[open]').count(),0);
  await page.keyboard.press('Enter');assert.equal(await page.locator('.operation-row[open]').count(),1);
  assert.equal(await page.locator('.operation-row .disclosure').count(),2);checks.push(label+': keyboard disclosure and explicit chevron');
  const contrasts=await page.evaluate(()=>{
    const rgb=color=>color.match(/[\d.]+/g).slice(0,3).map(Number).map(x=>{x/=255;return x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4;});
    const lum=color=>rgb(color).reduce((sum,x,i)=>sum+x*[0.2126,0.7152,0.0722][i],0);
    return [...document.querySelectorAll('[data-operation-notice], .operation-status, .operation-summary time, [data-run-identity]')].map(node=>{
      let ancestor=node,bg;
      do {bg=getComputedStyle(ancestor).backgroundColor;ancestor=ancestor.parentElement;}while(ancestor&&bg==='rgba(0, 0, 0, 0)');
      const fg=lum(getComputedStyle(node).color),back=lum(bg);return (Math.max(fg,back)+0.05)/(Math.min(fg,back)+0.05);
    });
  });assert.ok(contrasts.every(ratio=>ratio>=4.5),JSON.stringify(contrasts));checks.push(label+': operation text contrast >=4.5:1');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.evaluate(()=>{document.activeElement.blur();window.scrollTo({top:0,behavior:'instant'});});
  await page.screenshot({path:out+'/'+label+'.png',fullPage:true});
  const box=await run.boundingBox();assert.ok(box.height>=40);checks.push(label+': expanded history, long IDs wrap, no horizontal overflow, usable action');
  await run.click();await page.waitForFunction(()=>document.querySelectorAll('.operation-row').length===3);assert.equal(posts,2);checks.push(label+': new result preserves prior history');
  status=503;await refresh.click();await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent.includes('unavailable'));assert.equal(await run.isDisabled(),true);assert.equal(await page.locator('.operation-row').count(),3);
  checks.push(label+': outage keeps stale history and blocks submission');status=200;
  malformed=true;await refresh.click();await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent.includes('could not be read'));assert.equal(await page.locator('[data-operation-history] img').count(),0);malformed=false;
  rows=[make('d','unknown','interrupted')];await refresh.click();await page.waitForFunction(()=>document.querySelector('.operation-status').textContent==='Result unknown');assert.equal(await run.isEnabled(),true);
  rows=[{...make('e'),outcome:null,transitions:[{phase:'queued',timestamp_ms:1794380400000}]}];await refresh.click();await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent.includes('unfinished'));assert.equal(await run.isDisabled(),true);checks.push(label+': malformed, unknown and unfinished states stay explicit');
  rows=[];await refresh.click();await page.waitForFunction(()=>!document.querySelector('[data-run-identity]').disabled);
  uncertain=true;await run.click();await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent.includes('may have been recorded'));assert.equal(posts,3);assert.equal(await run.isDisabled(),true);
  await refresh.click();await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent.includes('History refreshed'));assert.equal(posts,3);assert.equal(await run.isDisabled(),true);
  if(label==='mobile'){await page.evaluate(()=>{document.activeElement.blur();window.scrollTo({top:0,behavior:'instant'});});await page.screenshot({path:out+'/uncertain-mobile.png',fullPage:true});}checks.push(label+': uncertain submission is never retried by refresh');
  status=401;await refresh.click();await page.waitForFunction(()=>document.querySelector('[data-operation-notice]').textContent.includes('Sign in again'));assert.equal(await run.isDisabled(),true);assert.deepEqual(errors,[]);checks.push(label+': expired session blocks actions; no browser errors');
  await context.close();
 }
 await browser.close();fs.writeFileSync(out+'/report.json',JSON.stringify({synthetic_directory:true,mocked_worker_api:true,real_tls:false,checks},null,2));console.log('PASS: '+checks.length+' browser checks; screenshots '+out);
})().catch(e=>{console.error(e);process.exit(1)});
