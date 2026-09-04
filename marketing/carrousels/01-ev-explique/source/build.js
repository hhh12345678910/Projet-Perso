// Renders carousel.html from a slide spec (copy.json). Usage: node build.js [copy.json] [out.html]
const fs = require('fs');
const { makeChart } = require('./chart.js');
const NB = ' ', NNB = ' ';
const fr = s => typeof s !== 'string' ? s : s
  .replace(/« /g, '«' + NB).replace(/ »/g, NB + '»')
  .replace(/ ([;:!?])/g, NNB + '$1')
  .replace(/(\d) %/g, '$1' + NB + '%').replace(/(\d) €/g, '$1' + NB + '€')
  .replace(/(\d) (\d{3})(?!\d)/g, '$1' + NB + '$2')
  .replace(/ ([—–]) /g, NB + '$1 ');
const deep = v => Array.isArray(v) ? v.map(deep) : (v && typeof v === 'object') ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, deep(x)])) : fr(v);
const spec = deep(JSON.parse(fs.readFileSync(process.argv[2] || 'copy.json', 'utf8')));
const total = spec.slides.length;
const pad = n => String(n).padStart(2, '0');
const esc = s => s; // copy is trusted authoring input (may contain <b>, <span>)
const icon = ok => `<div class="ic ${ok ? 'yes' : 'no'}"><svg><use href="#${ok ? 'check' : 'cross'}"/></svg></div>`;

const R = {
  cover: s => `<div class="kicker">${s.kicker}</div><div class="num">${s.num || '01'}</div><h1>${s.title}</h1><div class="accent"></div><p class="hook">${s.body}</p>`,
  quote: s => {
    let dots = ''; for (let i = 0; i < 100; i++) dots += `<i${i < (s.dotsOn ?? 50) ? ' class="on"' : ''}></i>`;
    return `<div class="card"><p class="quote">${s.quote}</p><div class="dots">${dots}</div><div class="dots-cap"><span>${s.capLeft}</span><span>${s.capRight}</span></div></div>`;
  },
  statement: s => `<div class="card"><p class="statement">${s.lines.map((l, i) => `<span class="${i % 2 ? 'alt' : ''}">${l}</span>`).join('<br>')}</p></div>`,
  formula: s => `<div class="card formula-card"><div class="formula">${s.terms.map(t => t.op ? `<div class="op">${t.op}</div>` : `<div class="t"><div class="v ${t.cls || ''}">${t.v}</div><div class="lab">${t.lab}</div></div>`).join('')}</div>${s.note ? `<div class="fnote">${s.note}</div>` : ''}</div>`,
  compare: s => `${s.head ? `<div class="coin-head"><div>${s.head.left}</div><span>${s.head.right}</span></div>` : ''}<div class="coin">${s.cards.map(c => `<div class="c${c.win ? ' win' : ''}"><div class="lbl">${c.lbl}</div><div class="odd mono">${c.odd}</div><div class="calc mono">${c.calc}</div><div class="res ${c.tone || ''} mono">${c.res}</div></div>`).join('')}</div>`,
  bars: s => `<div class="card vig">${s.rows.map(r => `<div class="row"><div class="who">${r.who}</div><div class="bar"><i style="width:${r.w}%"></i><b>${r.label}</b></div></div>`).join('')}
    <div class="row sum" style="margin-top:52px"><div class="who">${s.total.who}</div><div class="bar"><i style="width:100%;border-radius:14px 0 0 14px"></i><i class="over" style="left:auto;right:0;width:${s.total.over}%;border-radius:0 14px 14px 0"></i><div class="mark" style="left:${100 - s.total.over}%"><span>100${NB}%</span></div><b>${s.total.label}</b></div></div>
    <div class="legend"><span>${s.legend.text}</span><span class="neg">${s.legend.val}</span></div></div>`,
  steps: s => `<div class="steps">${s.steps.map((st, i) => `<div class="s${i === s.steps.length - 1 ? ' last' : ''}"><div class="n">${i + 1}</div><div class="txt"><div class="h">${st.h}</div>${st.d ? `<div class="d">${st.d}</div>` : ''}</div></div>`).join('')}</div>`,
  calc: s => `<div class="card calc">${(s.lines || []).map(l => l.k !== undefined
      ? `<div class="line"><div class="k">${l.k}</div><div class="v mono">${l.v}${l.small ? `<small>${l.small}</small>` : ''}</div></div>`
      : `<div class="line plain"><div class="v mono">${l.v}</div><div class="v mono r ${l.tone || ''}">${l.r}</div></div>`).join('')}
    ${s.total ? `<div class="line total"><div class="k">${s.total.k}</div><div class="row"><div class="v mono expr">${s.total.expr}</div><div class="v mono res ${s.total.tone || ''}">${s.total.res}</div></div></div>` : ''}</div>`,
  chart: s => {
    const c = makeChart({ odds: s.odds, p: s.p ?? 0.5, N: s.n ?? 1000 });
    const ay = c.H + 44;
    const paths = c.paths.map((p, i) => `<polyline points="${p}" fill="none" stroke="rgba(37,99,235,${i === 0 ? '.9' : '.45'})" stroke-width="${i === 0 ? 4 : 3.5}" stroke-linejoin="round"/>`).join('');
    return `<div class="card chart"><svg viewBox="-70 -30 1010 ${c.H + 80}">
      ${s.band ? `<polygon class="band" points="${c.band}"/>` : ''}
      <line x1="0" y1="${c.sy0}" x2="${c.W}" y2="${c.sy0}" stroke="rgba(11,31,58,.25)" stroke-width="2"/>
      <line x1="0" y1="${c.syExp}" x2="${c.W}" y2="${c.syExp}" stroke="rgba(11,31,58,.10)" stroke-width="2" stroke-dasharray="6 8"/>
      <text class="axis" x="-16" y="${c.sy0}" text-anchor="end" dominant-baseline="middle">0</text>
      <text class="axis" x="-16" y="${c.syExp}" text-anchor="end" dominant-baseline="middle">+${c.expY}</text>
      <text class="axis" x="0" y="${ay}" text-anchor="start">0${NB}pari</text>
      <text class="axis" x="${c.W / 2}" y="${ay}" text-anchor="middle">${Math.round((s.n ?? 1000) / 2)}${NB}paris</text>
      <text class="axis" x="${c.W}" y="${ay}" text-anchor="end">${(s.n ?? 1000).toLocaleString('fr-FR').replace(/\s/g, NB)}${NB}paris</text>
      ${paths}<polyline points="${c.expected}" fill="none" stroke="#22C55E" stroke-width="7" stroke-linecap="round"/></svg>
      <div class="lg"><span><i style="background:#22C55E"></i>${s.legend[0]}</span><span><i style="background:rgba(37,99,235,.55)"></i>${s.legend[1]}</span>${s.band && s.legend[2] ? `<span><i style="background:rgba(56,189,248,.35)"></i>${s.legend[2]}</span>` : ''}</div></div>`;
  },
  list: s => `<div class="list">${s.items.map(it => `<div class="it">${icon(it.ok)}<div class="t">${it.t}${it.s ? ` <span>${it.s}</span>` : ''}</div></div>`).join('')}</div>`,
  cta: s => `<svg class="mark"><use href="#eq-mark"/></svg><div class="bigword">EQUODDS</div><div class="tagline">${spec.tagline}</div>
    <h2 style="font-size:64px;margin-top:20px">${s.title}</h2><p class="follow">${s.body}</p>
    ${s.pills ? `<div class="pills">${s.pills.map(p => `<div class="pill ${p.state || ''}">${p.state === 'done' ? '<svg><use href="#check-w"/></svg>' : ''}${p.t}</div>`).join('')}</div>` : ''}
    ${s.next ? `<div class="next"><div class="k">${s.next.k}</div><div class="h">${s.next.h}</div></div>` : ''}`,
};

const slides = spec.slides.map((s, idx) => {
  const i = idx + 1;
  const isCover = s.kind === 'cover', isCta = s.kind === 'cta';
  const head = `<header class="top"><div class="lockup"><svg><use href="#eq-mark"/></svg><span class="word">EQUODDS</span></div><div class="counter"><b>${pad(i)}</b> / ${total}</div></header>`;
  const foot = isCta
    ? `<footer class="bottom"><span class="series">${spec.series}</span><span class="legal">${s.legal || spec.legal}</span></footer>`
    : `<footer class="bottom"><span class="series">${isCover ? spec.tagline : spec.series}</span><span class="swipe">${spec.swipe || 'Glisse'} <svg><use href="#arrow"/></svg></span></footer>`;
  let content;
  if (isCover || isCta) content = R[s.kind](s);
  else {
    const body = s.body ? `<p class="body">${s.body}</p>` : '';
    content = `<div class="kicker">${s.kicker}</div><h2>${s.title}</h2>${s.bodyFirst ? body : ''}${R[s.kind](s)}${s.bodyFirst ? '' : body}`;
  }
  return `<section class="slide k-${s.kind}" id="s${i}">${head}<div class="content">${content}</div>${foot}</section>`;
}).join('\n\n');

const html = `<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>EQUODDS — ${spec.series}</title>
<style>
${fs.readFileSync('fonts/fonts.css', 'utf8')}
${fs.readFileSync('base.css', 'utf8')}
${fs.readFileSync('slides.css', 'utf8')}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <linearGradient id="eq-g" gradientUnits="userSpaceOnUse" x1="175" y1="686" x2="862" y2="243"><stop offset="0" stop-color="#2563EB"/><stop offset="0.55" stop-color="#1E90F5"/><stop offset="1" stop-color="#38BDF8"/></linearGradient>
  <symbol id="eq-mark" viewBox="120 210 760 520"><g fill="url(#eq-g)" stroke="url(#eq-g)"><polyline points="175,518 268,518 318,419 378,628 558,279 800,279" fill="none" stroke-width="72" stroke-linecap="round" stroke-linejoin="round"/><polygon stroke="none" points="760,243 862,243 827,315 760,315"/><polygon stroke="none" points="527,430 794,430 759,502 492,502"/><polygon stroke="none" points="464,614 830,614 795,686 429,686"/></g></symbol>
  <symbol id="arrow" viewBox="0 0 24 24"><path d="M4 12h15M13 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="check" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="#22C55E" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="check-w" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="cross" viewBox="0 0 24 24"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11" fill="none" stroke="#EF4444" stroke-width="3" stroke-linecap="round"/></symbol>
</defs></svg>
<div class="deck">
${slides}
</div></body></html>`;
fs.writeFileSync(process.argv[3] || 'carousel.html', html);
console.log('built', process.argv[3] || 'carousel.html', html.length, 'bytes,', total, 'slides');
