/* Valuebet Analytics — interface.
 *
 * ⚠️ CE FICHIER NE CALCULE AUCUNE MÉTRIQUE. Pas de CLV, pas de ROI, pas de
 * P&L, pas de déduplication. Il appelle `/api/filters`, `/api/analyse` et
 * `/api/detail`, et il met en forme ce qu'on lui rend. Une formule qui
 * apparaîtrait ici serait une SECONDE définition — c'est le §17.7, et ce
 * projet l'a payé trois fois. Un test vérifie qu'aucune autre URL n'est
 * appelée.
 *
 * ⚠️ L'ARRONDI EST UNIQUEMENT DE PRÉSENTATION. `12.068542605875932` s'affiche
 * « 12,07 % » ; la valeur rendue par l'API n'est jamais modifiée, et rien
 * n'est renvoyé au serveur.
 */
'use strict';

/* Seuils d'AFFICHAGE. Ils reprennent ceux que l'API applique déjà pour ses
 * avertissements textuels ; ici ils ne servent qu'à marquer visuellement une
 * marque fragile. Aucun chiffre n'est recalculé à partir d'eux. */
const SEUIL_COUVERTURE = 80;   // sous ce taux, la CLV porte sur une minorité
const SEUIL_REGLES = 30;       // sous cet effectif : indice, pas résultat

const FINE = ' ';         // espace fine insécable — « 7,67 % » ne se coupe pas

/* ── Mise en forme (présentation seulement) ────────────────────────── */

const nb = (v, dec) => v.toLocaleString('fr-FR', {
  minimumFractionDigits: dec, maximumFractionDigits: dec,
}).replace(/ | | /g, FINE);

const pct = (v, dec = 2) => v === null || v === undefined ? '—'
  : (v > 0 ? '+' : '') + nb(v, dec) + FINE + '%';
const pctNu = (v, dec = 1) => v === null || v === undefined ? '—'
  : nb(v, dec) + FINE + '%';
const eur = (v, dec = 2) => v === null || v === undefined ? '—'
  : (v > 0 ? '+' : '') + nb(v, dec) + FINE + '€';
const ent = (v) => v === null || v === undefined ? '—' : nb(v, 0);
const cote = (v) => v === null || v === undefined ? '—' : nb(v, 2);

const signe = (v) => v === null || v === undefined ? '' : v > 0 ? 'pos'
  : v < 0 ? 'neg' : '';

const el = (t, cls, txt) => {
  const n = document.createElement(t);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};
const $ = (id) => document.getElementById(id);

/* ── Appels API — EXCLUSIVEMENT ces trois chemins ──────────────────── */

const API_FILTERS = '/api/filters';
const API_ANALYSE = '/api/analyse';
const API_DETAIL = '/api/detail';

async function appel(chemin, params) {
  const url = params ? chemin + '?' + params.toString() : chemin;
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  const corps = await r.json().catch(() => null);
  if (!r.ok) {
    const e = new Error((corps && corps.detail) || `HTTP ${r.status}`);
    e.statut = r.status;
    throw e;
  }
  return corps;
}

/* ── État ──────────────────────────────────────────────────────────── */

let REFS = null;      // /api/filters
let ANALYSE = null;   // dernière /api/analyse
let PAGE = 1, PAR_PAGE = 25, TRI = 'detected_at', ORDRE = 'desc';
let CELLULE = null;   // cellule de matrice sélectionnée {odds, ev}

/* ── Construction des paramètres ───────────────────────────────────── */

function multi(id) {
  return Array.from($(id).selectedOptions).map((o) => o.value);
}
function valOuNull(id) {
  const v = $(id).value.trim();
  return v === '' ? null : v;
}

function parametres(extra) {
  const p = new URLSearchParams();
  multi('f-sports').forEach((v) => p.append('sports', v));
  multi('f-books').forEach((v) => p.append('bookmakers', v));
  multi('f-markets').forEach((v) => p.append('markets', v));
  const lig = valOuNull('f-league');
  if (lig) p.append('leagues', lig);
  [['odds_min', 'f-odds-min'], ['odds_max', 'f-odds-max'],
   ['ev_min', 'f-ev-min'], ['ev_max', 'f-ev-max'],
   ['date_from', 'f-date-from'], ['date_to', 'f-date-to'],
   ['delay_min', 'f-delay-min'], ['delay_max', 'f-delay-max'],
   ['stake', 'f-stake']].forEach(([nom, id]) => {
    const v = valOuNull(id);
    if (v !== null) p.set(nom, v);
  });
  p.set('population', $('f-population').value);
  p.set('played', $('f-played').value);
  Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
  return p;
}

/* ── Infobulle ─────────────────────────────────────────────────────── */

const bulle = $('bulle');
function montrer(ev, titre, lignes, alerte) {
  bulle.innerHTML = '';
  bulle.appendChild(el('div', 't', titre));
  lignes.forEach(([g, d]) => {
    const l = el('div', 'l');
    l.appendChild(el('span', null, g));
    l.appendChild(el('span', null, d));
    bulle.appendChild(l);
  });
  if (alerte) bulle.appendChild(el('div', 'w', alerte));
  bulle.classList.add('on');
  bulle.setAttribute('aria-hidden', 'false');
  placer(ev);
}
function placer(ev) {
  const r = bulle.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  bulle.style.left = Math.max(8, x) + 'px';
  bulle.style.top = Math.max(8, y) + 'px';
}
function cacher() {
  bulle.classList.remove('on');
  bulle.setAttribute('aria-hidden', 'true');
}
function accrocher(n, titre, lignes, alerte) {
  n.addEventListener('mouseenter', (e) => montrer(e, titre, lignes, alerte));
  n.addEventListener('mousemove', placer);
  n.addEventListener('mouseleave', cacher);
}

/* Les lignes d'infobulle communes à toute tranche rendue par l'API. */
function lignesTranche(t) {
  return [
    ['opportunités', ent(t.opportunities)],
    ['réglées', `${ent(t.settled)} (${pctNu(t.settlement_rate)})`],
    ['CLV', `${pct(t.clv)} sur ${ent(t.clv_n)} (${pctNu(t.clv_coverage, 0)})`],
    ['ROI', pct(t.roi)],
    ['P&L', eur(t.pnl, 0)],
  ];
}
function alerteTranche(t) {
  const a = [];
  if (t.clv_coverage !== null && t.clv_coverage < SEUIL_COUVERTURE) {
    a.push(`CLV sur ${pctNu(t.clv_coverage, 0)} du lot seulement`);
  }
  if (t.settled > 0 && t.settled < SEUIL_REGLES) {
    a.push(`${ent(t.settled)} paris réglés : indice, pas résultat`);
  }
  return a.join(' · ');
}

/* ── SVG ───────────────────────────────────────────────────────────── */

const NS = 'http://www.w3.org/2000/svg';
const svgEl = (t, attrs) => {
  const n = document.createElementNS(NS, t);
  Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
  return n;
};

function vide(hote, message) {
  hote.innerHTML = '';
  hote.appendChild(el('p', 'vide', message));
}

/* Bornes d'axe incluant TOUJOURS zéro : une série entièrement positive dont
 * l'axe commencerait à son minimum ferait paraître énorme un écart d'un
 * point. Le zéro est le repère, il ne se négocie pas. */
function bornes(valeurs) {
  const v = valeurs.filter((x) => x !== null && x !== undefined);
  if (!v.length) return null;
  let lo = Math.min(0, ...v), hi = Math.max(0, ...v);
  if (lo === hi) { lo -= 1; hi += 1; }
  const marge = (hi - lo) * 0.08;
  return [lo - marge, hi + marge];
}

/* ── Courbe temporelle ─────────────────────────────────────────────── */

function courbe(hote, tranches, champ, libelle) {
  hote.innerHTML = '';
  const pts = tranches.filter((t) => t[champ] !== null);
  if (pts.length < 1) { vide(hote, 'Aucune valeur mesurable sur cette période.'); return; }

  const W = 560, H = 220, mG = 46, mD = 12, mH = 12, mB = 46;
  const b = bornes(pts.map((t) => t[champ]));
  const iw = W - mG - mD, ih = H - mH - mB;
  const x = (i) => mG + (pts.length === 1 ? iw / 2
    : (i / (pts.length - 1)) * iw);
  const y = (v) => mH + ih - ((v - b[0]) / (b[1] - b[0])) * ih;

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': libelle });

  for (let k = 0; k <= 4; k++) {
    const v = b[0] + (k / 4) * (b[1] - b[0]);
    svg.appendChild(svgEl('line', { class: 'grille-ligne',
      x1: mG, x2: W - mD, y1: y(v), y2: y(v) }));
    const t = svgEl('text', { class: 'axe-txt', x: mG - 7, y: y(v) + 3.5,
      'text-anchor': 'end' });
    t.textContent = pct(v, 1);
    svg.appendChild(t);
  }
  if (b[0] < 0 && b[1] > 0) {
    svg.appendChild(svgEl('line', { class: 'ligne-zero',
      x1: mG, x2: W - mD, y1: y(0), y2: y(0) }));
  }

  const d = pts.map((t, i) => `${i ? 'L' : 'M'}${x(i)},${y(t[champ])}`).join(' ');
  svg.appendChild(svgEl('path', { class: 'serie', d }));

  // ⚠️ LES ÉTIQUETTES SE CHOISISSENT PAR L'ESPACE DISPONIBLE, PAS PAR UN
  // MODULO. Un « un sur deux » laisse les deux dernières se toucher dès que
  // le nombre de points est impair — observé au rendu : « 2026-09-21 » et
  // « 2026-09-28 » collées. On réserve une largeur minimale par étiquette, et
  // on retire l'avant-dernière si la dernière vient la percuter.
  const LARGEUR_ETIQ = 68;
  const pas = Math.max(1, Math.ceil((pts.length * LARGEUR_ETIQ) / iw));
  const aEtiqueter = new Set();
  for (let i = 0; i < pts.length; i += pas) aEtiqueter.add(i);
  aEtiqueter.add(pts.length - 1);
  const rangs = [...aEtiqueter].sort((a, b) => a - b);
  if (rangs.length >= 2) {
    const [avant, dernier] = rangs.slice(-2);
    if (x(dernier) - x(avant) < LARGEUR_ETIQ) aEtiqueter.delete(avant);
  }

  pts.forEach((t, i) => {
    const faible = t.clv_coverage !== null && t.clv_coverage < SEUIL_COUVERTURE;
    const c = svgEl('circle', { cx: x(i), cy: y(t[champ]), r: 4.5,
      class: 'pt' + (faible && champ === 'clv' ? ' faible' : '') });
    accrocher(c, t.key, lignesTranche(t), alerteTranche(t));
    svg.appendChild(c);
    if (aEtiqueter.has(i)) {
      const lab = svgEl('text', { class: 'axe-txt', x: x(i), y: H - mB + 16,
        'text-anchor': 'middle' });
      lab.textContent = String(t.key).slice(0, 10);
      svg.appendChild(lab);
    }
  });
  hote.appendChild(svg);
}

/* ── Barres divergentes ────────────────────────────────────────────── */

function barres(hote, tranches, champ, libelle) {
  hote.innerHTML = '';
  const lot = tranches.filter((t) => t.opportunities > 0);
  if (!lot.length) { vide(hote, 'Aucune opportunité.'); return; }

  const hL = 26, mH = 10, mB = 26, mG = 108, mD = 54;
  const W = 560, H = mH + lot.length * hL + mB;
  const b = bornes(lot.map((t) => t[champ]));
  if (!b) { vide(hote, 'Aucune valeur mesurable.'); return; }
  const iw = W - mG - mD;
  const x = (v) => mG + ((v - b[0]) / (b[1] - b[0])) * iw;
  const x0 = x(0);

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': libelle });

  lot.forEach((t, i) => {
    const cy = mH + i * hL + hL / 2;
    const lab = svgEl('text', { class: 'axe-txt', x: mG - 9, y: cy + 3.5,
      'text-anchor': 'end' });
    lab.textContent = String(t.key).slice(0, 17);
    svg.appendChild(lab);

    const v = t[champ];
    if (v === null || v === undefined) {
      const nd = svgEl('text', { class: 'axe-txt', x: x0 + 7, y: cy + 3.5 });
      nd.textContent = '— non mesurable';
      svg.appendChild(nd);
      accrocher(nd, t.key, lignesTranche(t), alerteTranche(t));
      return;
    }
    const xv = x(v);
    const gauche = Math.min(x0, xv), larg = Math.max(1.5, Math.abs(xv - x0));
    const faible = (champ === 'clv' && t.clv_coverage < SEUIL_COUVERTURE)
      || (champ === 'roi' && t.settled < SEUIL_REGLES);
    const r = svgEl('rect', {
      x: gauche, y: cy - 8, width: larg, height: 16, rx: 4,
      fill: v >= 0 ? 'var(--pos)' : 'var(--neg)',
      class: faible ? 'faible' : '',
      'fill-opacity': faible ? 0.45 : 1,
    });
    accrocher(r, t.key, lignesTranche(t), alerteTranche(t));
    svg.appendChild(r);

    // ⚠️ L'ÉTIQUETTE NE DOIT JAMAIS ENTRER DANS LA GOUTTIÈRE DES LIBELLÉS.
    // Observé au rendu : « 1.8-2.3-1,2 % » — le libellé de tranche et la
    // valeur se chevauchaient, illisibles. Quand la barre négative est assez
    // longue pour que sa valeur touche la gouttière, on bascule l'étiquette
    // de l'autre côté du zéro, où la place est libre par construction.
    let lx = v >= 0 ? xv + 6 : xv - 6;
    let ancre = v >= 0 ? 'start' : 'end';
    if (v < 0 && lx - 46 < mG) { lx = x0 + 6; ancre = 'start'; }
    const val = svgEl('text', { class: 'axe-txt', y: cy + 3.5, x: lx,
      'text-anchor': ancre });
    val.textContent = pct(v, 1) + (faible ? ' ⚠' : '');
    svg.appendChild(val);
  });

  svg.appendChild(svgEl('line', { class: 'ligne-zero',
    x1: x0, x2: x0, y1: mH - 2, y2: H - mB + 2 }));
  const z = svgEl('text', { class: 'axe-txt', x: x0, y: H - mB + 15,
    'text-anchor': 'middle' });
  z.textContent = '0';
  svg.appendChild(z);
  hote.appendChild(svg);
}

/* ── Matrice ───────────────────────────────────────────────────────── */

/* ⚠️ LE TEXTE DOIT SUIVRE LE FOND, PAS L'INVERSE.
 *
 * Observé au rendu : sur les paliers 2 et 3 (bleu #184f95, rouge #98211f,
 * clarté OKLab ≈ .43), l'encre quasi noire devenait illisible — « +13,8 % »
 * et « n = 3 » disparaissaient dans le fond. La couleur du texte est donc
 * choisie par palier, pas fixée une fois pour toutes. Les paliers 2 et 3
 * portent de l'encre claire, le palier 1 et le neutre gardent l'encre du
 * thème. */
function styleCellule(v, max) {
  if (v === null || v === undefined || max === 0) {
    return { fond: 'var(--neutre)', encre: 'var(--encre)' };
  }
  const f = Math.min(1, Math.abs(v) / max);
  const arme = v >= 0 ? 'pos' : 'neg';
  const pas = f < 0.34 ? 1 : f < 0.67 ? 2 : 3;
  return { fond: `var(--${arme}-${pas})`,
           encre: pas === 1 ? 'var(--encre)' : '#ffffff' };
}

function matrice() {
  const t = $('matrice');
  t.innerHTML = '';
  if (!ANALYSE) return;
  const champ = $('m-mesure').value;
  const m = ANALYSE.matrix;
  const par = {};
  m.cells.forEach((c) => { par[c.odds + '|' + c.ev] = c; });
  const vals = m.cells.map((c) => c[champ]).filter((v) => v !== null);
  const max = vals.length ? Math.max(...vals.map(Math.abs)) : 0;

  const thead = el('thead');
  const tr = el('tr');
  tr.appendChild(el('th', 'lig', 'cote ╲ EV'));
  m.cols.forEach((c) => tr.appendChild(el('th', null, c)));
  thead.appendChild(tr);
  t.appendChild(thead);

  const tb = el('tbody');
  m.rows.forEach((lig) => {
    const r = el('tr');
    r.appendChild(el('th', 'lig', lig));
    m.cols.forEach((col) => {
      const c = par[lig + '|' + col];
      const td = el('td');
      if (!c || !c.opportunities) {
        td.className = 'zero';
        td.textContent = '·';
      } else {
        td.className = 'cliquable';
        const st = styleCellule(c[champ], max);
        td.style.background = st.fond;
        td.style.color = st.encre;
        const faible = (champ === 'clv' && c.clv_coverage < SEUIL_COUVERTURE)
          || (champ === 'roi' && c.settled < SEUIL_REGLES);
        // ⚠️ La couleur ne suffit JAMAIS : la valeur est écrite dans la
        // cellule, et une cellule fragile porte un repère visible.
        td.appendChild(el('span', 'v', pct(c[champ], 1) + (faible ? ' ⚠' : '')));
        td.appendChild(el('span', 'n', 'n = ' + ent(c.opportunities)));
        if (faible) td.style.outline = `1.5px dashed ${st.encre}`;
        accrocher(td, `${lig} · EV ${col}`, lignesTranche(c), alerteTranche(c));
        td.addEventListener('click', () => {
          CELLULE = (CELLULE && CELLULE.odds === lig && CELLULE.ev === col)
            ? null : { odds: lig, ev: col, cell: c };
          PAGE = 1;
          chargerDetail();
          matrice();
        });
        if (CELLULE && CELLULE.odds === lig && CELLULE.ev === col) {
          td.style.borderColor = 'var(--encre)';
        }
      }
      r.appendChild(td);
    });
    tb.appendChild(r);
  });
  t.appendChild(tb);
}

/* ── KPI ───────────────────────────────────────────────────────────── */

function tuile(titre, valeur, note, cls, vedette) {
  const k = el('div', 'kpi' + (vedette ? ' vedette' : ''));
  k.appendChild(el('div', 'titre', titre));
  k.appendChild(el('div', 'valeur ' + (cls || ''), valeur));
  if (note) k.appendChild(el('div', 'note', note));
  return k;
}

function kpis(s) {
  const h = $('kpis');
  h.innerHTML = '';
  const couvFaible = s.clv_coverage !== null && s.clv_coverage < SEUIL_COUVERTURE;
  const peuRegles = s.settled > 0 && s.settled < SEUIL_REGLES;

  h.appendChild(tuile('Opportunités', ent(s.opportunities),
    `${ent(s.matches)} matchs distincts`, '', true));
  h.appendChild(tuile('Réglées', ent(s.settled),
    `${pctNu(s.settlement_rate)} de settlement` +
    (s.unsettled ? ` · ${ent(s.unsettled)} en attente` : '')));
  // ⚠️ La CLV ne sort JAMAIS sans sa couverture. C'est la règle du projet :
  // +10,4 % sur 95 % du lot et sur 30 % ne sont pas la même phrase.
  const kc = tuile('CLV moyenne', pct(s.clv),
    `sur ${ent(s.clv_n)} paris · couverture ${pctNu(s.clv_coverage, 0)}`,
    signe(s.clv), true);
  if (couvFaible) kc.querySelector('.note').classList.add('fragile');
  h.appendChild(kc);
  const kr = tuile('ROI', pct(s.roi),
    peuRegles ? `${ent(s.settled)} réglés — indice, pas résultat`
      : `sur ${ent(s.settled)} paris réglés`, signe(s.roi), true);
  if (peuRegles) kr.querySelector('.note').classList.add('fragile');
  h.appendChild(kr);
  h.appendChild(tuile('P&L notionnel', eur(s.pnl, 0),
    `mise totale ${eur(s.stake_total, 0).replace('+', '')}`, signe(s.pnl)));
  h.appendChild(tuile('CLV médiane', pct(s.clv_median),
    `${pctNu(s.clv_positive_rate)} de CLV positives`, signe(s.clv_median)));
  h.appendChild(tuile('EV moyen', pct(s.ev_mean), 'à la détection'));
  h.appendChild(tuile('Cote moyenne', cote(s.odds_mean),
    `${ent(s.won)} G · ${ent(s.lost)} P · ${ent(s.void)} A`));
}

/* ── Avertissements ────────────────────────────────────────────────── */

function avertissements(liste) {
  const h = $('avertissements');
  h.innerHTML = '';
  (liste || []).forEach((m) => {
    const grave = /indisponible|irrécupérable|lot vide/i.test(m);
    const d = el('div', 'avert' + (grave ? ' grave' : ''));
    d.appendChild(el('span', null, grave ? '🔴' : '⚠️'));
    d.appendChild(el('span', null, m));
    h.appendChild(d);
  });
}

/* ── Détail ────────────────────────────────────────────────────────── */

const COLONNES = [
  ['detected_at', 'Détecté', (i) => String(i.detected_at).slice(0, 16).replace('T', ' ')],
  ['sport', 'Sport', (i) => i.sport || '—'],
  [null, 'Compétition', (i) => i.league || '—'],
  [null, 'Match', (i) => i.event || '—'],
  [null, 'Marché', (i) => i.market + (i.line !== null ? ' ' + i.line : '')],
  [null, 'Pari', (i) => i.selection],
  ['book', 'Book', (i) => i.bookmaker || '—'],
  ['odd_taken', 'Cote', (i) => cote(i.odds), 'num'],
  ['ev_pct', 'EV', (i) => pct(i.ev_pct), 'num'],
  [null, 'Clôture', (i) => cote(i.closing_fair_odd), 'num'],
  ['clv', 'CLV', (i) => pct(i.clv_pct), 'num'],
  [null, 'Mise', (i) => i.stake === null ? '—' : eur(i.stake, 0).replace('+', ''), 'num'],
  [null, 'Résultat', null],
  ['pnl', 'P&L', (i) => eur(i.pnl, 2), 'num'],
];

const RESULTAT_FR = { won: 'Gagné', lost: 'Perdu', void: 'Annulé',
  unsettled: 'Non réglé' };

function tableauDetail(d) {
  const t = $('detail');
  t.innerHTML = '';
  const thead = el('thead'), tr = el('tr');
  COLONNES.forEach(([tri, titre, , cls]) => {
    const th = el('th', (cls || '') + (tri && tri === TRI ? ' actif' : ''));
    th.textContent = titre + (tri && tri === TRI ? (ORDRE === 'desc' ? ' ▾' : ' ▴') : '');
    if (tri) {
      th.addEventListener('click', () => {
        if (TRI === tri) ORDRE = ORDRE === 'desc' ? 'asc' : 'desc';
        else { TRI = tri; ORDRE = 'desc'; }
        PAGE = 1;
        chargerDetail();
      });
    } else { th.style.cursor = 'default'; }
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  t.appendChild(thead);

  const tb = el('tbody');
  d.items.forEach((i) => {
    const r = el('tr');
    COLONNES.forEach(([, titre, rendu, cls]) => {
      const td = el('td', cls || '');
      if (titre === 'Résultat') {
        const e = el('span', 'etiq ' + i.result, RESULTAT_FR[i.result] || i.result);
        td.appendChild(e);
      } else {
        td.textContent = rendu(i);
        if (titre === 'CLV' || titre === 'P&L' || titre === 'EV') {
          const v = titre === 'CLV' ? i.clv_pct : titre === 'P&L' ? i.pnl : i.ev_pct;
          // ⚠️ `classList.add('')` LÈVE une exception (DOMTokenList refuse le
          // jeton vide), et `signe()` rend '' pour zéro comme pour null. Le
          // symptôme observé : la table entière restait vide, sans erreur
          // visible ailleurs que dans la console.
          const c = signe(v);
          if (c) td.classList.add(c);
        }
      }
      r.appendChild(td);
    });
    tb.appendChild(r);
  });
  t.appendChild(tb);

  const p = $('pagination');
  p.innerHTML = '';
  if (!d.total) { p.appendChild(el('span', null, 'Aucune opportunité.')); return; }
  const prec = el('button', 'discret', '‹ Précédent');
  prec.disabled = d.page <= 1;
  prec.addEventListener('click', () => { PAGE = d.page - 1; chargerDetail(); });
  const suiv = el('button', 'discret', 'Suivant ›');
  suiv.disabled = d.page >= d.pages;
  suiv.addEventListener('click', () => { PAGE = d.page + 1; chargerDetail(); });
  p.appendChild(prec);
  p.appendChild(el('span', null,
    `Page ${ent(d.page)} / ${ent(d.pages)} — ${ent(d.total)} opportunités`));
  p.appendChild(suiv);
}

async function chargerDetail() {
  const extra = { page: PAGE, per_page: PAR_PAGE, sort: TRI, order: ORDRE };
  // Une cellule de matrice RESTREINT les filtres, elle ne les remplace pas :
  // l'utilisateur doit retrouver exactement le sous-ensemble qu'il a cliqué.
  if (CELLULE) {
    const [a, b] = CELLULE.odds.replace('> ', '').split('-').map(parseFloat);
    if (!Number.isNaN(a)) extra.odds_min = a;
    if (!Number.isNaN(b)) extra.odds_max = b;
    const ev = CELLULE.ev.replace(/%/g, '');
    const m = ev.match(/^(\d+)-(\d+)$/);
    if (m) { extra.ev_min = m[1]; extra.ev_max = m[2]; }
    else if (ev.startsWith('<')) extra.ev_max = parseFloat(ev.slice(1));
    else if (ev.endsWith('+')) extra.ev_min = parseFloat(ev);
  }
  $('detail-filtre').textContent = CELLULE
    ? `Restreint à la cellule cote ${CELLULE.odds} × EV ${CELLULE.ev} — reclique la cellule pour l'annuler.`
    : 'Toutes les opportunités de l\'analyse courante.';
  try {
    tableauDetail(await appel(API_DETAIL, parametres(extra)));
  } catch (e) {
    // ⚠️ NE PAS REMPLACER LE CONTENEUR DE LA TABLE. La première version
    // écrivait le message à la place de `.enrob`, donc `#detail` cessait
    // d'exister — et l'appel SUIVANT plantait sur `null`, transformant une
    // erreur passagère en panne définitive. Le message va à côté.
    $('detail').innerHTML = '';
    $('pagination').innerHTML = '';
    $('detail-filtre').textContent = 'Détail indisponible : ' + e.message;
  }
}

/* ── Analyse ───────────────────────────────────────────────────────── */

async function analyser() {
  const bouton = $('analyser');
  bouton.disabled = true;
  $('etat').textContent = 'analyse en cours…';
  try {
    const d = await appel(API_ANALYSE,
      parametres({ granularite: $('f-gran').value }));
    ANALYSE = d;
    CELLULE = null;
    PAGE = 1;

    avertissements(d.warnings);
    kpis(d.summary);
    ['bloc-kpi', 'bloc-temps', 'bloc-decoupes', 'bloc-matrice', 'bloc-detail']
      .forEach((id) => { $(id).hidden = false; });

    $('note-population').textContent =
      `Population : ${d.population.explication}` +
      (d.population.limites.length
        ? ' — Limite : ' + d.population.limites.join(' ') : '');

    courbe($('g-clv-temps'), d.by_time, 'clv', 'CLV dans le temps');
    courbe($('g-roi-temps'), d.by_time, 'roi', 'ROI dans le temps');
    barres($('g-clv-cote'), d.by_odds, 'clv', 'CLV par cote');
    barres($('g-roi-cote'), d.by_odds, 'roi', 'ROI par cote');
    barres($('g-clv-ev'), d.by_ev, 'clv', 'CLV par EV');
    barres($('g-roi-ev'), d.by_ev, 'roi', 'ROI par EV');
    barres($('g-clv-book'), d.by_book, 'clv', 'CLV par book');
    barres($('g-roi-book'), d.by_book, 'roi', 'ROI par book');
    barres($('g-clv-sport'), d.by_sport, 'clv', 'CLV par sport');
    barres($('g-roi-sport'), d.by_sport, 'roi', 'ROI par sport');
    matrice();
    await chargerDetail();
    $('etat').textContent = '';
  } catch (e) {
    // Une erreur de saisie doit se lire, pas disparaître dans la console.
    avertissements([e.statut === 400 || e.statut === 422
      ? 'Filtre refusé : ' + e.message
      : 'Erreur : ' + e.message]);
    $('etat').textContent = '';
  } finally {
    bouton.disabled = false;
  }
}

/* ── Amorçage ──────────────────────────────────────────────────────── */

function remplir(select, valeurs) {
  select.innerHTML = '';
  valeurs.forEach((v) => {
    const o = el('option', null, v);
    o.value = v;
    select.appendChild(o);
  });
}

async function demarrer() {
  try {
    REFS = await appel(API_FILTERS);
  } catch (e) {
    avertissements(['Impossible de lire les filtres : ' + e.message]);
    return;
  }
  remplir($('f-sports'), REFS.sports);
  remplir($('f-books'), REFS.bookmakers);
  remplir($('f-markets'), REFS.markets);
  const dl = $('ligues');
  REFS.leagues.forEach((l) => {
    const o = el('option');
    o.value = l;
    dl.appendChild(o);
  });
  const sp = $('f-population');
  REFS.populations.forEach((p) => {
    const o = el('option', null, p.value.replace(/_/g, ' '));
    o.value = p.value;
    sp.appendChild(o);
  });
  const majAide = () => {
    const p = REFS.populations.find((x) => x.value === sp.value);
    $('aide-population').textContent = p ? p.explication : '';
  };
  sp.addEventListener('change', majAide);
  majAide();

  $('perimetre').textContent =
    `${REFS.sports.length} sports · ${REFS.bookmakers.length} books · ` +
    `${ent(REFS.leagues.length)} compétitions · ${REFS.date_min} → ${REFS.date_max}`;
  if (REFS.date_min) $('f-date-from').value = REFS.date_min;
  if (REFS.date_max) $('f-date-to').value = REFS.date_max;

  $('analyser').addEventListener('click', analyser);
  $('m-mesure').addEventListener('change', matrice);
  $('reinit').addEventListener('click', () => {
    $('form').reset();
    if (REFS.date_min) $('f-date-from').value = REFS.date_min;
    if (REFS.date_max) $('f-date-to').value = REFS.date_max;
    majAide();
  });
  $('f-gran').addEventListener('change', () => { if (ANALYSE) analyser(); });

  const racine = document.documentElement;
  $('theme').addEventListener('click', () => {
    const sombre = racine.getAttribute('data-theme') === 'dark';
    racine.setAttribute('data-theme', sombre ? 'light' : 'dark');
    try { localStorage.setItem('vb-theme', sombre ? 'light' : 'dark'); } catch (_) {}
  });
  try {
    const t = localStorage.getItem('vb-theme');
    if (t) racine.setAttribute('data-theme', t);
    else if (matchMedia('(prefers-color-scheme: dark)').matches) {
      racine.setAttribute('data-theme', 'dark');
    }
  } catch (_) {}

  analyser();
}

demarrer();
