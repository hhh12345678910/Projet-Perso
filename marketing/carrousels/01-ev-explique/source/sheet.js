// usage: node sheet.js <dir> -> <dir>/sheet.png (5x2 grid of the ev-*.png slides)
const { chromium } = require('playwright'); const path=require('path'); const fs=require('fs');
(async()=>{ const dir=process.argv[2]; const files=fs.readdirSync(dir).filter(f=>/^ev-\d+\.png$/.test(f)).sort();
 const html=`<html><body style="margin:0;background:#E5E7EB"><div style="display:grid;grid-template-columns:repeat(5,432px);gap:16px;padding:16px">${files.map(f=>`<img src="file://${path.resolve(dir,f)}" style="width:432px;height:540px;display:block">`).join('')}</div></body></html>`;
 fs.writeFileSync(path.join(dir,'sheet.html'),html);
 const b=await chromium.launch(); const p=await b.newPage({viewport:{width:2272,height:1128}});
 await p.goto('file://'+path.resolve(dir,'sheet.html')); await p.waitForTimeout(300);
 await p.screenshot({path:path.join(dir,'sheet.png'),fullPage:true}); await b.close(); console.log('sheet', files.length); })();
