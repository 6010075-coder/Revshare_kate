const { chromium } = require('/Users/kate/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport:{width:800,height:1100}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await page.goto('file:///private/tmp/revshare-bpmn-preview.html');
  const f=page.frameLocator('iframe'); await f.locator('#rb-diagram').waitFor();
  const checks=[];
  for(const width of [800,390]) {
    await page.setViewportSize({width,height:1100});
    for(const diagram of ['overview','intake','validation','reconcile','result','operations']) {
      await f.locator('#rb-diagram').selectOption(diagram);
      checks.push({width,diagram,nodes:await f.locator('svg [data-node]').count()});
    }
  }
  await page.setViewportSize({width:800,height:1100});
  await f.locator('#rb-diagram').selectOption('intake');
  await f.locator('#rb-node').selectOption('F01');
  if(!(await f.locator('#rb-detail').textContent()).includes('document'))throw new Error('Detail failed');
  await page.screenshot({path:'/private/tmp/revshare-bpmn-preview.png',fullPage:true});
  console.log(JSON.stringify({checks,errors,detail:true}));
  await browser.close();
  if(errors.length)process.exit(1);
})().catch(e=>{console.error(e);process.exit(1)});
