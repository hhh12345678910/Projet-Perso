const { chromium } = require('playwright');
const path = require('path');
(async () => {
  // usage: node render_all.js <carousel.html> <outDir> [scale]
  const html = process.argv[2] || 'carousel.html';
  const outDir = process.argv[3] || 'out';
  const scale = +(process.argv[4] || 1);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1200, height: 1500 }, deviceScaleFactor: scale });
  await p.goto('file://' + path.resolve(html));
  await p.evaluate(() => document.fonts.ready);
  await p.waitForTimeout(200);
  const slides = await p.$$('.slide');
  const overflow = await p.evaluate(() => [...document.querySelectorAll('.slide')].map(s => {
    const r = s.getBoundingClientRect(); const c = s.querySelector('.content');
    const bad = [...s.querySelectorAll('*')].filter(e => { const er = e.getBoundingClientRect(); return er.width && (er.right > r.right + 0.5 || er.bottom > r.bottom + 0.5 || er.left < r.left - 0.5); }).map(e => e.className || e.tagName);
    return { id: s.id, contentH: Math.round(c.scrollHeight), contentBox: Math.round(c.getBoundingClientRect().height), overflowing: bad.slice(0, 5) };
  }));
  console.log(JSON.stringify(overflow));
  for (let i = 0; i < slides.length; i++) {
    const f = path.join(outDir, `ev-${String(i + 1).padStart(2, '0')}.png`);
    await slides[i].screenshot({ path: f });
  }
  await b.close();
  console.log('rendered', slides.length, 'slides to', outDir);
})();
