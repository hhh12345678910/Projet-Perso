/* Valuebet Analytics — interface.
 *
 * ⚠️ CE FICHIER NE CALCULE AUCUNE MÉTRIQUE. Pas de CLV, pas de ROI, pas de
 * P&L, pas de déduplication. Il appelle `/api/filters`, `/api/analyse`,
 * `/api/detail`, `/api/segments` et `/api/strategies`, et il met en forme ce
 * qu'on lui rend. Une
 * formule qui apparaîtrait ici serait une SECONDE définition — c'est le
 * §17.7, et ce projet l'a payé trois fois. Un test vérifie qu'aucune autre URL
 * n'est appelée.
 *
 * ⚠️ L'ARRONDI EST UNIQUEMENT DE PRÉSENTATION. `12.068542605875932` s'affiche
 * « +12,1 % » ; la valeur rendue par l'API n'est jamais modifiée, et rien
 * n'est renvoyé au serveur.
 *
 * ORGANISATION
 * ------------
 * Une application à pages (Vue d'ensemble, Performance, CLV, …) au-dessus
 * d'UNE analyse : un seul appel `/api/analyse` nourrit toutes les pages, qui
 * ne font que choisir quelles découpes montrer. Chaque page est dessinée à sa
 * première ouverture après une analyse, pas avant : vingt graphiques cachés
 * ne coûtent rien tant qu'on ne les regarde pas.
 *
 * Les filtres vivent dans le tiroir « Filtres avancés » ; la barre du haut
 * n'en montre que quatre, sous forme de raccourcis qui écrivent dans le même
 * formulaire. Une seule source de vérité : `parametres()`.
 */
'use strict';

/* Seuils d'AFFICHAGE. Ils reprennent ceux que l'API applique déjà pour ses
 * avertissements textuels ; ici ils ne servent qu'à marquer visuellement une
 * mesure fragile. Aucun chiffre n'est recalculé à partir d'eux. */
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
/* Un réglage saisi (8, 8.5) se relit sans décimales inutiles. */
const num = (v) => v === null || v === undefined ? '—'
  : Number(v).toLocaleString('fr-FR', { maximumFractionDigits: 2 });

const signe = (v) => v === null || v === undefined ? '' : v > 0 ? 'pos'
  : v < 0 ? 'neg' : '';

const el = (t, cls, txt) => {
  const n = document.createElement(t);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};
const $ = (id) => document.getElementById(id);

/* Dates : « 21 juin », « 21 juin 2026 ». Les dates d'API sont des jours
 * (AAAA-MM-JJ) : on les lit à midi pour qu'aucun fuseau ne les décale. */
function dateLongue(iso, avecAnnee) {
  if (!iso) return '—';
  const d = new Date(String(iso).slice(0, 10) + 'T12:00:00');
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString('fr-FR', avecAnnee
    ? { day: 'numeric', month: 'long', year: 'numeric' }
    : { day: 'numeric', month: 'long' });
}
function periodeTexte(de, a) {
  if (!de && !a) return 'Toute la période';
  const memeAnnee = de && a && String(de).slice(0, 4) === String(a).slice(0, 4);
  return `${dateLongue(de, !memeAnnee)} → ${dateLongue(a, true)}`;
}
/* L'étiquette d'une période de l'axe temporel. La semaine arrive sous la
 * forme « 2026-07-06 (S28) », le jour « 2026-07-06 », le mois « 2026-07 ». */
function libTemps(cle) {
  const s = String(cle);
  let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) {
    return new Date(`${m[1]}-${m[2]}-${m[3]}T12:00:00`)
      .toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
  }
  m = s.match(/^(\d{4})-(\d{2})$/);
  if (m) {
    return new Date(`${s}-15T12:00:00`)
      .toLocaleDateString('fr-FR', { month: 'short', year: 'numeric' });
  }
  return s;
}
/* Un instant ISO en heure LOCALE du navigateur : « 27/09 14:32 ». */
function dateHeure(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16).replace('T', ' ');
  return d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' })
    + ' ' + d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
}

/* ── Appels API — EXCLUSIVEMENT ces chemins ────────────────────────── */

const API_FILTERS = '/api/filters';
const API_ANALYSE = '/api/analyse';
const API_DETAIL = '/api/detail';
const API_SEGMENTS = '/api/segments';
const API_STRATEGIES = '/api/strategies';

async function appel(chemin, params) {
  const url = params ? chemin + '?' + params.toString() : chemin;
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  const corps = await r.json().catch(() => null);
  if (!r.ok) {
    // FastAPI rend un 422 sous forme de LISTE d'objets : sans ce dépliage,
    // l'utilisateur lisait « [object Object] ».
    const det = corps && corps.detail;
    const msg = Array.isArray(det)
      ? det.map((d) => `${(d.loc || []).slice(-1)[0]} : ${d.msg}`).join(' · ')
      : det;
    const e = new Error(msg || `HTTP ${r.status}`);
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
let SALE = false;     // filtres modifiés depuis la dernière analyse
let PAGE_ACTIVE = 'vue-ensemble';
let AXE_AA = null;    // axe choisi dans « Analyse avancée »
let MODE_EVOL = 'clv', MODE_FIN = 'roi';
let MODE_COTE = 'toutes', MODE_EV = 'minimum';
let JETON = null;     // la dernière analyse lancée — une réponse plus ancienne est ignorée
let JETON_DETAIL = null, JETON_DERNIERES = null, JETON_PARIS = null;
let LIMITE_CMP = 50;  // lignes affichées du tableau des compétitions
let PJ_MODE = 'oui';  // lentille de la page « Paris joués » : oui | non | tous
let PARIS = null;     // l'analyse de la page « Paris joués », pour sa lentille
let OUVREUR = null;   // l'élément qui a ouvert le tiroir, pour lui rendre le focus
const RENDUES = new Set();
const A_REDESSINER = new Set();

/* ⚠️ LA REQUÊTE DE L'ANALYSE AFFICHÉE, PAS LE FORMULAIRE.
 *
 * Les pages se dessinent à leur première ouverture, et l'utilisateur peut
 * toucher un filtre sans relancer l'analyse. Un détail, des « dernières
 * opportunités », des segments ou un CSV construits depuis le formulaire
 * vivant décriraient alors un AUTRE lot que les KPI juste au-dessus — sous une
 * étiquette qui les dit identiques. Tout ce qui complète une analyse part donc
 * de la requête réellement envoyée, mémorisée ici, plus ses seuls paramètres
 * de navigation (page, tri, cellule). */
let PARAMS_ANALYSE = null;   // URLSearchParams de la dernière analyse affichée
let CLE_ANALYSE = null;      // sa signature, granularité comprise

function parametresAnalyse(extra) {
  const p = new URLSearchParams(PARAMS_ANALYSE || parametres());
  Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
  return p;
}
const cleFormulaire = () => parametres({ granularite: $('f-gran').value }).toString();

/* Préférences locales. `localStorage` peut être bloqué (navigation privée,
 * politique d'entreprise) : l'interface doit alors fonctionner quand même. */
const stock = {
  lire(k, d) {
    try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (_) { return d; }
  },
  ecrire(k, v) {
    try { localStorage.setItem(k, v); } catch (_) { /* préférence non retenue */ }
  },
};

/* Libellés : ils viennent TOUS du serveur. Les écrire ici ferait vivre deux
 * vocabulaires, dont l'un finirait par ne plus correspondre au filtre. */
const nomSport = (s) => (REFS && REFS.sports_labels && REFS.sports_labels[s]) || s || '—';
const nomBook = (b) => (REFS && REFS.bookmakers_labels && REFS.bookmakers_labels[b]) || b || '—';
const nomMarche = (m) => (REFS && REFS.markets_labels && REFS.markets_labels[m]) || m || '—';
const libPari = (v) => ((((REFS && REFS.outcomes) || [])
  .find((o) => o.value === v)) || { libelle: v || '—' }).libelle;
const nomPopulation = (v) => (((REFS && REFS.populations) || [])
  .find((p) => p.value === v) || { libelle: v || '—' }).libelle;

/* ── Icônes (SVG écrits ici : aucune ressource externe) ───────────── */

const ICONES = {
  radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12l6-6"/>',
  coche: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 12l3 3 5-6"/>',
  euro: '<path d="M17 6.5A6.5 6.5 0 1 0 17 17.5"/><path d="M4 10h9M4 14h9"/>',
  courbe: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  pile: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
  balance: '<path d="M12 3v18M5 7h14M5 7l-3 7a4 4 0 0 0 6 0zM19 7l-3 7a4 4 0 0 0 6 0z"/>',
  cible: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  mediane: '<path d="M4 20V10M10 20V4M16 20v-8M22 20H2"/>',
  pourcent: '<path d="M19 5L5 19"/><circle cx="7" cy="7" r="2.5"/><circle cx="17" cy="17" r="2.5"/>',
  cote: '<path d="M4 18l6-6 4 4 6-8"/>',
  bouclier: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
};
function icone(nom) {
  const s = el('span', 'ic');
  s.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONES[nom] || ''}</svg>`;
  return s;
}

/* ── Groupes de cases à cocher ──────────────────────────────────────
 *
 * ⚠️ REMPLACE LES `select multiple`, ET CE N'EST PAS COSMÉTIQUE.
 * Un `select multiple` exige de savoir qu'il faut maintenir Ctrl — ce que
 * rien n'indique —, efface toute la sélection au premier clic simple, et
 * cache ce qui est coché dès que la liste dépasse sa hauteur visible. Les
 * trois sont des pertes SILENCIEUSES : on croit avoir filtré sur quatre
 * bookmakers et on en a un seul, sous un tableau parfaitement normal.
 *
 * ⚠️ « RIEN DE COCHÉ = TOUS » RESTE LE CONTRAT DE L'API, PAS L'INTERFACE.
 * Chaque groupe porte en tête une ligne « Tous » explicite, cochée tant
 * qu'aucune valeur ne l'est. Elle n'a pas de valeur et ne part jamais au
 * serveur (`coches()` l'écarte) : l'état se LIT au lieu de se deviner.
 *
 * La valeur rendue est TOUJOURS la valeur canonique (`unibet_be`), jamais le
 * libellé affiché (« Unibet BE ») : c'est la première qui repart vers l'API.
 */

function groupeCases(hote, valeurs, options) {
  const o = options || {};
  hote.innerHTML = '';
  hote.dataset.groupe = '1';
  if (!valeurs.length) {
    hote.appendChild(el('div', 'cases-vide', o.vide || 'Aucune valeur.'));
    return;
  }

  const liste = el('div', 'cases-liste' + (o.courte ? ' courte' : ''));
  const nomTous = o.tous || 'Tous';

  const lt = el('label', 'case case-tous');
  const tous = el('input');
  tous.type = 'checkbox';
  tous.dataset.tous = '1';
  lt.appendChild(tous);
  lt.appendChild(el('span', null, nomTous));
  const majTous = () => {
    tous.checked = !liste.querySelector('input:not([data-tous]):checked');
  };
  const changement = () => {
    majTous();
    if (o.onChange) o.onChange();
    signalerChangement();
  };
  tous.addEventListener('change', () => {
    // Cocher « Tous » vide la sélection ; le décocher seul n'a pas de sens —
    // on quitte « Tous » en choisissant une valeur.
    if (tous.checked) {
      liste.querySelectorAll('input:not([data-tous])').forEach((c) => { c.checked = false; });
    }
    changement();
  });

  // La barre n'apparaît que si elle sert : deux cases n'ont pas besoin d'un
  // « tout sélectionner », et une barre inutile vole de la place à l'écran.
  if (valeurs.length > 3) {
    const barre = el('div', 'cases-barre');
    const tout = el('button', null, 'Tout');
    const aucun = el('button', null, 'Aucun');
    tout.type = aucun.type = 'button';
    tout.title = 'Cocher chaque valeur visible';
    aucun.title = `Tout décocher — revient à « ${nomTous} »`;
    const basculer = (etat) => () => {
      liste.querySelectorAll('.case:not(.case-tous):not(.masquee) input')
        .forEach((c) => { c.checked = etat; });
      // Tout cocher SANS recherche, c'est ne rien restreindre : on revient à
      // l'état explicite « Tous » plutôt que d'envoyer la liste entière —
      // qui exclurait en silence toute valeur apparue depuis.
      const cases = liste.querySelectorAll('input:not([data-tous])');
      if (etat && cases.length > 1 && [...cases].every((c) => c.checked)) {
        cases.forEach((c) => { c.checked = false; });
      }
      changement();
    };
    tout.addEventListener('click', basculer(true));
    aucun.addEventListener('click', basculer(false));
    barre.appendChild(tout);
    barre.appendChild(aucun);

    if (valeurs.length > 8) {
      const rech = el('input');
      rech.type = 'search';
      rech.placeholder = 'chercher…';
      rech.setAttribute('aria-label', 'Chercher dans la liste');
      // La recherche MASQUE, elle ne décoche pas : filtrer une liste ne doit
      // jamais modifier la sélection qu'on a déjà faite.
      rech.addEventListener('input', () => {
        const q = rech.value.trim().toLowerCase();
        liste.querySelectorAll('.case:not(.case-tous)').forEach((c) => {
          c.classList.toggle('masquee',
            q !== '' && !c.textContent.toLowerCase().includes(q));
        });
      });
      barre.appendChild(rech);
    }
    hote.appendChild(barre);
  }

  liste.appendChild(lt);
  valeurs.forEach((v) => {
    const cle = typeof v === 'string' ? v : v.key;
    const texte = typeof v === 'string' ? (o.labels && o.labels[v]) || v : v.label;
    const l = el('label', 'case');
    const c = el('input');
    c.type = 'checkbox';
    c.value = cle;
    if (o.coches && o.coches.includes(cle)) c.checked = true;
    c.addEventListener('change', changement);
    l.appendChild(c);
    l.appendChild(el('span', null, texte));
    liste.appendChild(l);
  });
  hote.appendChild(liste);
  majTous();
  hote.majTous = majTous;
}

/** Les valeurs canoniques cochées d'un groupe — jamais la ligne « Tous ». */
function coches(id) {
  const h = $(id);
  if (!h) return [];
  return Array.from(h.querySelectorAll('input[type="checkbox"]:checked'))
    .filter((c) => !c.dataset.tous)
    .map((c) => c.value);
}

/** Pose une sélection par programme (menus, analyses sauvegardées). */
function cocher(id, valeurs) {
  const h = $(id);
  if (!h) return;
  h.querySelectorAll('input[type="checkbox"]:not([data-tous])')
    .forEach((c) => { c.checked = valeurs.includes(c.value); });
  if (h.majTous) h.majTous();
}

/* ── Construction des paramètres ───────────────────────────────────── */

function valOuNull(id) {
  const v = $(id).value.trim();
  return v === '' ? null : v;
}

function parametres(extra) {
  const p = new URLSearchParams();
  const sports = coches('f-sports');
  sports.forEach((v) => p.append('sports', v));
  coches('f-books').forEach((v) => p.append('bookmakers', v));
  coches('f-markets').forEach((v) => p.append('markets', v));
  coches('f-outcomes').forEach((v) => p.append('outcomes', v));
  // ⚠️ Une compétition peut contenir une virgule : elle part en paramètre
  // RÉPÉTÉ, jamais jointe — l'API ne découpe pas ce champ-là.
  coches('f-league').forEach((v) => p.append('leagues', v));

  /* ⚠️ L'EV PAR SPORT PART VERS LE SERVEUR, IL N'EST PAS APPLIQUÉ ICI.
   * C'est la seule façon que les KPI, les découpes, la matrice ET le détail
   * décrivent le même lot : ils viennent tous de la même requête. Filtrer
   * après coup dans le navigateur ne corrigerait aucun des quatre. */
  if ($('ev-split').checked) {
    (sports.length ? sports : (REFS ? REFS.sports : [])).forEach((s) => {
      coches('ev-bands-' + s).forEach((b) => p.append('ev_bands_' + s, b));
    });
  } else {
    coches('f-ev-bands').forEach((b) => p.append('ev_bands', b));
  }

  /* ⚠️ LES TRANCHES DE COTE PARTENT VERS LE SERVEUR, PAS FILTRÉES ICI.
   * Même raison que pour l'EV. */
  if ($('cote-split').checked) {
    (sports.length ? sports : (REFS ? REFS.sports : [])).forEach((s) => {
      coches('odds-bands-' + s).forEach((b) => p.append('odds_bands_' + s, b));
    });
  } else {
    coches('f-odds-bands').forEach((b) => p.append('odds_bands', b));
  }

  /* Bornes LIBRES d'EV par sport, lisibles seulement quand le panneau par
   * sport est ouvert — sinon ce sont `ev_min`/`ev_max` globaux qui valent. */
  if ($('ev-split').checked) {
    (sports.length ? sports : (REFS ? REFS.sports : [])).forEach((s) => {
      [['ev_min_', 'ev-min-'], ['ev_max_', 'ev-max-']].forEach(([par, id]) => {
        const n = $(id + s);
        const v = n && n.value.trim();
        if (v) p.set(par + s, v);
      });
    });
  }

  /* Bornes d'EV par TRANCHE DE COTE. Le slug vient du serveur (`b.slug`), il
   * n'est pas refabriqué ici : deux règles de fabrication finiraient par
   * produire un nom que le serveur ne reconnaît plus. */
  if ($('ev-cote-split').checked) {
    ((REFS && REFS.odds_bands) || []).forEach((b) => {
      [['ev_odds_min_', 'evcote-min-'], ['ev_odds_max_', 'evcote-max-']]
        .forEach(([par, id]) => {
          const n = $(id + b.slug);
          const v = n && n.value.trim();
          if (v) p.set(par + b.slug, v);
        });
    });
  }

  /* ⚠️ LE SERVEUR ATTEND DES HEURES, TOUJOURS. L'unité est un confort de
   * saisie : `0.25 h` et `15 min` désignent la même chose, et c'est l'heure
   * décimale qui part. Envoyer des minutes sous le nom `delay_min` ferait
   * lire « 15 heures » à une requête qui voulait dire quinze minutes. */
  const facteur = $('f-delay-unite').value === 'min' ? 1 / 60 : 1;
  [['delay_min', 'f-delay-min'], ['delay_max', 'f-delay-max']]
    .forEach(([nom, id]) => {
      const v = valOuNull(id);
      if (v !== null) p.set(nom, String(Number(v) * facteur));
    });

  [['odds_min', 'f-odds-min'], ['odds_max', 'f-odds-max'],
   ['ev_min', 'f-ev-min'], ['ev_max', 'f-ev-max'],
   ['date_from', 'f-date-from'], ['date_to', 'f-date-to'],
   ['stake', 'f-stake']].forEach(([nom, id]) => {
    const v = valOuNull(id);
    if (v !== null) p.set(nom, v);
  });
  p.set('population', $('f-population').value);
  p.set('played', $('f-played').value);
  // La mise Kelly ne part QUE si elle est choisie : en mise fixe, la requête
  // reste celle d'avant, octet pour octet.
  if ($('f-stake-mode').value === 'kelly') {
    p.set('stake_mode', 'kelly');
    [['kelly_fraction', 'f-kelly-fraction'], ['bankroll', 'f-bankroll'],
     ['kelly_cap_pct', 'f-kelly-cap']].forEach(([nom, id]) => {
      const v = valOuNull(id);
      if (v !== null) p.set(nom, v);
    });
  }
  Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
  return p;
}

/* Les filtres du FORMULAIRE, sous la forme où le serveur les renvoie dans
 * `filters`. Lus depuis `parametres()` — ce qui partira vraiment — et non
 * depuis les champs un à un : deux lectures finiraient par diverger. */
function filtresDuFormulaire() {
  const p = parametres();
  const n = (k) => (p.get(k) === null ? null : Number(p.get(k)));
  const f = {
    date_from: p.get('date_from'), date_to: p.get('date_to'),
    sports: p.getAll('sports'), bookmakers: p.getAll('bookmakers'),
    markets: p.getAll('markets'), outcomes: p.getAll('outcomes'),
    leagues: p.getAll('leagues'), ev_bands: p.getAll('ev_bands'),
    odds_bands: p.getAll('odds_bands'),
    ev_min: n('ev_min'), ev_max: n('ev_max'),
    odds_min: n('odds_min'), odds_max: n('odds_max'),
    delay_min: n('delay_min'), delay_max: n('delay_max'),
    population: p.get('population'), played: p.get('played'), stake: n('stake'),
    stake_mode: p.get('stake_mode') || 'flat', kelly_fraction: n('kelly_fraction'),
    bankroll: n('bankroll'), kelly_cap_pct: n('kelly_cap_pct'),
    ev_bands_by_sport: {}, odds_bands_by_sport: {},
    ev_free_by_sport: {}, ev_free_by_odds: {},
  };
  new Set(p.keys()).forEach((k) => {
    if (k.startsWith('ev_bands_')) f.ev_bands_by_sport[k.slice(9)] = p.getAll(k);
    else if (k.startsWith('odds_bands_')) f.odds_bands_by_sport[k.slice(11)] = p.getAll(k);
    else if (k.startsWith('ev_odds_')) f.ev_free_by_odds[k] = p.get(k);
    else if (/^ev_(min|max)_/.test(k)) f.ev_free_by_sport[k] = p.get(k);
  });
  return f;
}

/* La mise, en mots — relue dans des filtres (ceux du serveur ou du
 * formulaire), jamais recalculée. */
const FRACTIONS_KELLY = [[1, '1/1'], [0.5, '1/2'], [1 / 3, '1/3'], [0.25, '1/4'],
  [0.2, '1/5'], [0.125, '1/8'], [0.1, '1/10']];
function fractionKelly(v) {
  const x = Number(v);
  const connue = FRACTIONS_KELLY.find(([f]) => Math.abs(f - x) < 1e-6);
  return connue ? connue[1] : num(x);
}
const estKelly = (f) => !!f && f.stake_mode === 'kelly';
function texteMise(f) {
  if (!estKelly(f)) return `${num(f.stake)} € par pari`;
  return `Kelly ${fractionKelly(f.kelly_fraction)} · bankroll ${num(f.bankroll)} € · max ${num(f.kelly_cap_pct)} %`;
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
  // Au clavier, la bulle se place sous l'élément qui a le focus.
  n.addEventListener('focus', () => {
    const r = n.getBoundingClientRect();
    montrer({ clientX: r.left, clientY: r.bottom }, titre, lignes, alerte);
  });
  n.addEventListener('blur', cacher);
}

/* Le libellé d'affichage d'une tranche. L'API rend `label` à côté de `key` ;
 * `key` reste la valeur canonique qui repart en filtre. */
const lib = (t) => t.label || t.key;

/* ⚠️ BADGE DE VOLUME — ET LE MOT « SIGNIFICATIF » N'APPARAÎT NULLE PART.
 * Un effectif ne décide pas de la significativité : il faudrait la variance,
 * la taille de l'effet cherché et le nombre de comparaisons faites. Le badge
 * qualifie la TAILLE de l'échantillon, et rien d'autre. */
function badge(ech) {
  if (!ech) return null;
  const b = el('span', 'ech ' + ech.niveau, ech.libelle);
  b.title = `${ent(ech.n)} — indication de VOLUME, pas de significativité `
    + 'statistique.';
  return b;
}

/* Les lignes d'infobulle communes à toute tranche rendue par l'API. */
function lignesTranche(t) {
  // Un champ ABSENT de la tranche (une semaine du Strategy Finder ne porte ni
  // taux de règlement, ni couverture, ni badge de volume) n'est pas écrit :
  // « (—) » y ferait croire à une mesure manquante. Un champ rendu à `null`
  // garde son « — », comme avant.
  const si = (k, txt) => (t[k] === undefined ? '' : txt);
  return [
    ['opportunités', ent(t.opportunities) + si('sample', ` · ${(t.sample || {}).libelle || ''}`)],
    ['réglées', ent(t.settled) + si('settlement_rate', ` (${pctNu(t.settlement_rate)})`)],
    ['CLV', `${pct(t.clv)} sur ${ent(t.clv_n)}` + si('clv_coverage', ` (${pctNu(t.clv_coverage, 0)})`)],
    ['ROI', pct(t.roi) + si('sample_settled', ` · ${(t.sample_settled || {}).libelle || ''}`)],
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
const svgTexte = (cls, x, y, texte, ancre) => {
  const t = svgEl('text', { class: cls, x, y, 'text-anchor': ancre || 'start' });
  t.textContent = texte;
  return t;
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

/* Des graduations RONDES (pas de 1, 2, 2,5 ou 5 × 10^k) qui englobent zéro.
 * « +6,8 % / +5,0 % / +3,1 % » faisait amateur ; « 0 / 2 / 4 / 6 % » se lit
 * d'un coup d'œil. C'est de la présentation d'axe : aucune valeur n'en dépend. */
function graduations(valeurs, n) {
  const b = bornes(valeurs);
  if (!b) return null;
  let lo = Math.min(0, ...valeurs.filter((x) => x !== null && x !== undefined));
  let hi = Math.max(0, ...valeurs.filter((x) => x !== null && x !== undefined));
  if (lo === hi) { lo -= 1; hi += 1; }
  const brut = (hi - lo) / (n || 4);
  const p10 = Math.pow(10, Math.floor(Math.log10(brut)));
  const pas = [1, 2, 2.5, 5, 10].map((m) => m * p10).find((x) => x >= brut) || 10 * p10;
  // La tolérance RAPPROCHE du multiple exact : une borne déjà ronde (zéro,
  // 150) reste la dernière graduation, au lieu d'ajouter un cran vide —
  // « -50 » sous un axe de volumes qui ne descend jamais sous zéro.
  const bas = Math.floor(lo / pas + 1e-9) * pas;
  const haut = Math.ceil(hi / pas - 1e-9) * pas;
  const ticks = [];
  for (let v = bas; v <= haut + pas / 2; v += pas) ticks.push(Math.abs(v) < pas / 1e6 ? 0 : v);
  const dec = pas >= 1 ? 0 : pas >= 0.1 ? 1 : 2;
  return { lo: bas, hi: haut, ticks, dec };
}

/* La largeur RÉELLE du conteneur : un graphique dessiné à sa taille garde des
 * textes à 11 px, là où un dessin fixe mis à l'échelle les rendait
 * minuscules sur téléphone et énormes sur grand écran. */
function largeur(hote) {
  return Math.max(300, Math.min(1600, Math.round(hote.clientWidth || 560)));
}

/* ⚠️ LES ÉTIQUETTES SE CHOISISSENT PAR L'ESPACE DISPONIBLE, PAS PAR UN
 * MODULO. Un « un sur deux » laisse les deux dernières se toucher dès que le
 * nombre de points est impair. On réserve une largeur minimale par étiquette,
 * et on retire l'avant-dernière si la dernière vient la percuter. */
function etiquettesX(n, x, iw, largeurEtiq) {
  const pas = Math.max(1, Math.ceil((n * largeurEtiq) / iw));
  const garde = new Set();
  for (let i = 0; i < n; i += pas) garde.add(i);
  garde.add(n - 1);
  const rangs = [...garde].sort((a, b) => a - b);
  if (rangs.length >= 2) {
    const [avant, dernier] = rangs.slice(-2);
    if (x(dernier) - x(avant) < largeurEtiq) garde.delete(avant);
  }
  return garde;
}

/* ── Courbes temporelles ───────────────────────────────────────────── */

/* Une ou plusieurs séries sur UNE échelle. N'y mettre que des grandeurs de
 * même nature (CLV et EV, deux pourcentages d'edge) — jamais CLV et ROI. */
function courbes(hote, tranches, series, libelle, fmt) {
  // `fmt` formate l'AXE et les étiquettes. Par défaut un pourcentage ;
  // le P&L cumulé passe `eur`, sans quoi un axe en « % » décrirait des
  // euros — une erreur d'unité qu'aucun relecteur ne rattrape ensuite.
  hote.innerHTML = '';
  const aVal = (t, s) => t[s.champ] !== null && t[s.champ] !== undefined;
  const pts = (tranches || []).filter((t) => series.some((s) => aVal(t, s)));
  if (!pts.length) { vide(hote, 'Aucune valeur mesurable sur cette période.'); return; }

  const W = largeur(hote), H = W < 520 ? 200 : 236;
  const mG = 58, mD = 14, mH = 14, mB = 30;
  const g = graduations(pts.flatMap((t) => series.filter((s) => aVal(t, s))
    .map((s) => t[s.champ])));
  const b = [g.lo, g.hi];
  const F = fmt || ((v) => pct(v, g.dec));
  const iw = W - mG - mD, ih = H - mH - mB;
  const x = (i) => mG + (pts.length === 1 ? iw / 2 : (i / (pts.length - 1)) * iw);
  const y = (v) => mH + ih - ((v - b[0]) / (b[1] - b[0])) * ih;

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': libelle });
  g.ticks.forEach((v) => {
    svg.appendChild(svgEl('line', { class: 'grille-ligne', x1: mG, x2: W - mD, y1: y(v), y2: y(v) }));
    svg.appendChild(svgTexte('axe-txt', mG - 8, y(v) + 3.5, v === 0 ? '0' : F(v), 'end'));
  });
  if (b[0] < 0 && b[1] > 0) {
    svg.appendChild(svgEl('line', { class: 'ligne-zero', x1: mG, x2: W - mD, y1: y(0), y2: y(0) }));
  }

  series.forEach((s) => {
    let d = '', ouvert = false, continu = true;
    pts.forEach((t, i) => {
      if (!aVal(t, s)) { ouvert = false; continu = false; return; }
      d += `${ouvert ? 'L' : 'M'}${x(i)},${y(t[s.champ])} `;
      ouvert = true;
    });
    // L'aire n'accompagne qu'une série seule et continue : sous deux séries
    // elle brouillerait la lecture, sur une série trouée elle mentirait.
    if (series.length === 1 && continu && pts.length > 1) {
      svg.appendChild(svgEl('path', { class: 'aire',
        d: `${d}L${x(pts.length - 1)},${y(0)} L${x(0)},${y(0)} Z` }));
    }
    svg.appendChild(svgEl('path', { class: s.cls === '2' ? 'serie-2' : 'serie', d }));
  });

  const r = pts.length > 50 ? 2.2 : pts.length > 24 ? 3 : 3.8;
  series.forEach((s) => pts.forEach((t, i) => {
    if (!aVal(t, s)) return;
    const faible = s.champ === 'clv' && t.clv_coverage !== null
      && t.clv_coverage < SEUIL_COUVERTURE;
    const c = svgEl('circle', { cx: x(i), cy: y(t[s.champ]), r,
      class: (s.cls === '2' ? 'pt-2' : 'pt') + (faible ? ' faible' : '') });
    accrocher(c, libTemps(lib(t)),
      [[s.libelle || libelle, fmt ? fmt(t[s.champ]) : pct(t[s.champ], 2)]]
        .concat(lignesTranche(t)), alerteTranche(t));
    svg.appendChild(c);
  }));

  const aEtiqueter = etiquettesX(pts.length, x, iw, 62);
  pts.forEach((t, i) => {
    if (aEtiqueter.has(i)) svg.appendChild(svgTexte('axe-txt', x(i), H - mB + 18, libTemps(t.key), 'middle'));
  });
  hote.appendChild(svg);
}

function courbe(hote, tranches, champ, libelle, fmt) {
  courbes(hote, tranches, [{ champ, libelle, cls: '' }], libelle, fmt);
}

/* ── Colonnes de volume ────────────────────────────────────────────
 *
 * ⚠️ UN VOLUME N'EST PAS UNE MESURE SIGNÉE. Les opportunités par période se
 * lisent sur un axe qui part de zéro et ne descend jamais en dessous ; leur
 * donner la palette divergente du CLV ferait croire à un signe. Une seule
 * teinte, aucune arme positive/négative. */

function colonnes(hote, tranches, champ, libelle) {
  hote.innerHTML = '';
  if (!tranches.length) { vide(hote, 'Aucune période.'); return; }
  const vals = tranches.map((t) => t[champ] || 0);
  const g = graduations([1].concat(vals));
  const hi = g.hi;

  const W = largeur(hote), H = 220, mG = 50, mD = 12, mH = 12, mB = 30;
  const iw = W - mG - mD, ih = H - mH - mB;
  const larg = Math.max(2, Math.min(34, iw / tranches.length - 4));
  const x = (i) => mG + (i + 0.5) * (iw / tranches.length);
  const y = (v) => mH + ih - (v / hi) * ih;

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': libelle });
  g.ticks.forEach((v) => {
    svg.appendChild(svgEl('line', { class: 'grille-ligne', x1: mG, x2: W - mD, y1: y(v), y2: y(v) }));
    svg.appendChild(svgTexte('axe-txt', mG - 8, y(v) + 3.5, ent(v), 'end'));
  });
  tranches.forEach((t, i) => {
    const v = t[champ] || 0;
    const r = svgEl('rect', {
      x: x(i) - larg / 2, y: y(v), width: larg,
      height: Math.max(0, mH + ih - y(v)), rx: 3, class: 'barre',
      fill: 'var(--pos-2)', 'fill-opacity': 0.85 });
    accrocher(r, libTemps(lib(t)), lignesTranche(t), alerteTranche(t));
    svg.appendChild(r);
  });
  const aEtiqueter = etiquettesX(tranches.length, x, iw, 62);
  tranches.forEach((t, i) => {
    if (aEtiqueter.has(i)) svg.appendChild(svgTexte('axe-txt', x(i), H - mB + 18, libTemps(t.key), 'middle'));
  });
  hote.appendChild(svg);
}

/* ── Barres divergentes ────────────────────────────────────────────── */

function barres(hote, tranches, champ, libelle, options) {
  const o = options || {};
  hote.innerHTML = '';
  const lot = (tranches || []).filter((t) => t.opportunities > 0);
  if (!lot.length) { vide(hote, 'Aucune opportunité.'); return; }

  const W = largeur(hote);
  const hL = 30, mH = 6, mB = 24;
  const mG = Math.min(190, Math.max(96, Math.round(W * 0.24)));
  const mD = o.n ? 190 : 124;
  const H = mH + lot.length * hL + mB;
  const b = bornes(lot.map((t) => t[champ]));
  if (!b) { vide(hote, 'Aucune valeur mesurable.'); return; }
  const iw = W - mG - mD;
  const x = (v) => mG + ((v - b[0]) / (b[1] - b[0])) * iw;
  const x0 = x(0);
  const maxCar = Math.max(8, Math.floor((mG - 14) / 6.4));

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': libelle });

  lot.forEach((t, i) => {
    const cy = mH + i * hL + hL / 2;
    const nom = String(lib(t));
    const lab = svgTexte('axe-txt', mG - 10, cy + 3.5,
      nom.length > maxCar ? nom.slice(0, maxCar - 1) + '…' : nom, 'end');
    svg.appendChild(lab);
    const droite = champ === 'clv'
      ? `couv. ${pctNu(t.clv_coverage, 0)}` : champ === 'roi' ? `${ent(t.settled)} réglés` : '';
    const n = o.n ? `${ent(t.opportunities)} opp.` : '';
    svg.appendChild(svgTexte('n-txt', W - 2, cy + 3.5, [n, droite].filter(Boolean).join(' · '), 'end'));

    const v = t[champ];
    if (v === null || v === undefined) {
      const nd = svgTexte('axe-txt', x0 + 7, cy + 3.5, '— non mesurable');
      svg.appendChild(nd);
      accrocher(nd, nom, lignesTranche(t), alerteTranche(t));
      return;
    }
    const xv = x(v);
    const gauche = Math.min(x0, xv), larg = Math.max(1.5, Math.abs(xv - x0));
    const faible = (champ === 'clv' && t.clv_coverage < SEUIL_COUVERTURE)
      || (champ === 'roi' && t.settled < SEUIL_REGLES);
    const r = svgEl('rect', {
      x: gauche, y: cy - 8, width: larg, height: 16, rx: 4,
      fill: v >= 0 ? 'var(--pos)' : 'var(--neg)',
      class: 'barre' + (faible ? ' faible' : ''),
      'fill-opacity': faible ? 0.45 : 0.92,
    });
    accrocher(r, nom, lignesTranche(t), alerteTranche(t));
    svg.appendChild(r);

    // ⚠️ L'ÉTIQUETTE NE DOIT JAMAIS ENTRER DANS LA GOUTTIÈRE DES LIBELLÉS.
    // Quand la barre négative est assez longue pour que sa valeur touche la
    // gouttière, on bascule l'étiquette de l'autre côté du zéro, où la place
    // est libre par construction.
    let lx = v >= 0 ? xv + 6 : xv - 6;
    let ancre = v >= 0 ? 'start' : 'end';
    if (v < 0 && lx - 48 < mG) { lx = x0 + 6; ancre = 'start'; }
    svg.appendChild(svgTexte('val-txt', lx, cy + 3.5, pct(v, 1) + (faible ? ' ⚠' : ''), ancre));
  });

  svg.appendChild(svgEl('line', { class: 'ligne-zero', x1: x0, x2: x0, y1: mH - 2, y2: H - mB + 2 }));
  svg.appendChild(svgTexte('axe-txt', x0, H - mB + 16, '0', 'middle'));
  hote.appendChild(svg);
}

/* ── Matrice ───────────────────────────────────────────────────────── */

/* ⚠️ LE TEXTE DOIT SUIVRE LE FOND, PAS L'INVERSE.
 *
 * Sur les paliers 2 et 3 (bleu #184f95, rouge #98211f, clarté OKLab ≈ .43),
 * l'encre quasi noire devenait illisible. La couleur du texte est donc
 * choisie par palier : les paliers 2 et 3 portent de l'encre claire, le
 * palier 1 et le neutre gardent l'encre du thème. */
function styleCellule(v, max) {
  if (v === null || v === undefined || max === 0) {
    return { fond: 'var(--neutre)', encre: 'var(--encre)' };
  }
  const f = Math.min(1, Math.abs(v) / max);
  const arme = v >= 0 ? 'pos' : 'neg';
  const pas = f < 0.34 ? 1 : f < 0.67 ? 2 : 3;
  return { fond: `var(--${arme}-${pas})`,
           encre: pas === 1 ? '#0b0b0b' : '#ffffff' };
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
        const mesure = c[champ] !== null && c[champ] !== undefined;
        const faible = mesure && ((champ === 'clv' && c.clv_coverage < SEUIL_COUVERTURE)
          || (champ === 'roi' && c.settled < SEUIL_REGLES));
        // ⚠️ La couleur ne suffit JAMAIS : la valeur est écrite dans la
        // cellule, et une cellule fragile porte un repère visible.
        td.appendChild(el('span', 'v', pct(c[champ], 1) + (faible ? ' ⚠' : '')));
        td.appendChild(el('span', 'n', 'n = ' + ent(c.opportunities)));
        if (faible) td.style.outline = `1.5px dashed ${st.encre}`;
        accrocher(td, `${lig} · EV ${col}`, lignesTranche(c), alerteTranche(c));
        td.tabIndex = 0;
        td.setAttribute('role', 'button');
        td.setAttribute('aria-label', `Détail de la cellule cote ${lig}, EV ${col}`);
        // Le clic ouvre le DÉTAIL de la cellule, sur la page des paris : la
        // cellule RESTREINT les filtres, elle ne les remplace pas.
        const ouvrir = () => {
          CELLULE = (CELLULE && CELLULE.odds === lig && CELLULE.ev === col)
            ? null : { odds: lig, ev: col, cell: c };
          PAGE = 1;
          // Sélection OU désélection : la page des paris doit se recharger.
          RENDUES.delete('paris');
          matrice();
          if (CELLULE) {
            // La cellule compte TOUTES ses opportunités, jouées ou non.
            PJ_MODE = 'tous';
            allerA('paris');
          }
        };
        td.addEventListener('click', ouvrir);
        td.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ouvrir(); }
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

function compterCartes(h) { h.dataset.n = String(h.children.length); }

function carteKpi(o) {
  const k = el('div', 'kpi' + (o.cls ? ' ' + o.cls : ''));
  const t = el('div', 'titre');
  if (o.icone) t.appendChild(icone(o.icone));
  t.appendChild(el('span', null, o.titre));
  k.appendChild(t);
  k.appendChild(el('div', 'valeur ' + (o.signe || ''), o.valeur));
  if (o.sous) k.appendChild(el('div', 'sous', o.sous));
  if (o.ligne2) {
    const l = el('div', 'ligne2');
    l.appendChild(el('b', o.ligne2[2] || '', o.ligne2[0]));
    l.appendChild(el('span', null, o.ligne2[1]));
    k.appendChild(l);
  }
  const det = el('div', 'details');
  if (o.details) det.appendChild(el('span', o.fragile ? 'fragile' : '', o.details));
  if (o.badge) det.appendChild(o.badge);
  if (det.childNodes.length) k.appendChild(det);
  if (o.vers) {
    k.classList.add('kpi-lien');
    k.tabIndex = 0;
    k.setAttribute('role', 'link');
    k.title = o.versTitre || 'Voir le détail';
    const aller = () => { if (o.avant) o.avant(); allerA(o.vers); };
    k.addEventListener('click', aller);
    k.addEventListener('keydown', (e) => { if (e.key === 'Enter') aller(); });
  }
  return k;
}

function squelettesKpi(id, n) {
  const h = $(id);
  if (!h) return;
  h.innerHTML = '';
  for (let i = 0; i < n; i += 1) {
    const k = el('div', 'kpi chargement');
    k.appendChild(el('div', 'titre squelette', 'chargement'));
    k.appendChild(el('div', 'valeur squelette', '0'));
    k.appendChild(el('div', 'sous squelette', '0'));
    h.appendChild(k);
  }
}

/* Les quatre cartes de la vue d'ensemble : opportunités, paris joués, ROI,
 * CLV. ⚠️ ROI ET CLV NE SE MÉLANGENT JAMAIS : le ROI est le résultat
 * financier du lot, la CLV sa performance face à la clôture. Deux cartes,
 * deux icônes, deux accents — et chacune porte SON dénominateur. */
function kpis(s) {
  const h = $('kpis');
  h.innerHTML = '';
  const couvFaible = s.clv_coverage !== null && s.clv_coverage < SEUIL_COUVERTURE;
  const peuRegles = s.settled > 0 && s.settled < SEUIL_REGLES;
  const f = (ANALYSE && ANALYSE.filters) || {};
  const quoi = f.played === 'oui' ? 'paris joués' : 'opportunités';

  h.appendChild(carteKpi({
    icone: 'radar', titre: 'Opportunités détectées', valeur: ent(s.opportunities),
    sous: 'sur la période et les filtres affichés',
    ligne2: [ent(s.matches), 'matchs distincts'],
    details: 'volume de l\'échantillon', badge: badge(s.sample),
    vers: 'paris', versTitre: 'Voir les opportunités une par une',
    avant: () => choisirLentille('tous') }));

  h.appendChild(carteKpi({
    icone: 'coche', titre: 'Paris joués', valeur: ent(s.played),
    sous: 'cliqués sur « Jouer »',
    ligne2: [pctNu(s.played_rate), 'taux de paris joués'],
    details: `sur ${ent(s.opportunities)} opportunités détectées`,
    vers: 'paris', versTitre: 'Voir les paris joués',
    avant: () => choisirLentille('oui') }));

  // ⚠️ Le badge du ROI porte l'effectif des RÉGLÉS, pas des opportunités :
  // c'est le dénominateur réel du ROI. Afficher celui des opportunités ferait
  // passer pour solide un ROI calculé sur quarante paris.
  h.appendChild(carteKpi({
    icone: 'euro', titre: 'ROI', valeur: pct(s.roi, 1), signe: signe(s.roi),
    cls: 'kpi-roi' + (s.roi > 0 ? ' pos-bord' : s.roi < 0 ? ' neg-bord' : ''),
    sous: estKelly(f)
      ? `Kelly ${fractionKelly(f.kelly_fraction)} — mise selon l'EV et la cote, pondéré par la mise`
      : `notionnel — ${num(f.stake)} € misés sur chaque ${quoi === 'paris joués' ? 'pari joué' : 'opportunité'}`,
    ligne2: [eur(s.pnl, 0), 'P&L cumulé', signe(s.pnl)],
    details: peuRegles ? `${ent(s.settled)} réglés — indice, pas résultat`
      : `sur ${ent(s.settled)} ${quoi} réglé${quoi === 'paris joués' ? 's' : 'es'}`,
    fragile: peuRegles, badge: badge(s.sample_settled),
    vers: 'performance', versTitre: 'Voir la performance financière' }));

  // ⚠️ La CLV ne sort JAMAIS sans sa couverture. C'est la règle du projet :
  // +10,4 % sur 95 % du lot et sur 30 % ne sont pas la même phrase.
  h.appendChild(carteKpi({
    icone: 'courbe', titre: 'CLV moyenne', valeur: pct(s.clv, 1), signe: signe(s.clv),
    cls: 'kpi-clv', sous: 'Closing Line Value — face à la clôture Pinnacle déviguée',
    ligne2: [pct(s.clv_median, 1), 'CLV médiane', signe(s.clv_median)],
    details: `sur ${ent(s.clv_n)} ${quoi} mesuré${quoi === 'paris joués' ? 's' : 'es'} · couverture ${pctNu(s.clv_coverage, 0)}`,
    fragile: couvFaible, badge: badge(s.sample_clv),
    vers: 'clv', versTitre: 'Voir l\'analyse de la CLV' }));
}

function kpisPerf(s) {
  const h = $('kpis-perf');
  h.innerHTML = '';
  h.appendChild(carteKpi({ icone: 'euro', titre: 'P&L notionnel', valeur: eur(s.pnl, 0),
    signe: signe(s.pnl), sous: `mise totale ${eur(s.stake_total, 0).replace('+', '')}` }));
  h.appendChild(carteKpi({ icone: 'pourcent', titre: 'ROI', valeur: pct(s.roi, 1),
    signe: signe(s.roi), sous: `sur ${ent(s.settled)} paris réglés`,
    fragile: s.settled > 0 && s.settled < SEUIL_REGLES }));
  h.appendChild(carteKpi({ icone: 'coche', titre: 'Réglées', valeur: ent(s.settled),
    sous: `${pctNu(s.settlement_rate)} de settlement`
      + (s.unsettled ? ` · ${ent(s.unsettled)} non réglées` : ''),
    details: s.unsettled_result_known
      ? `dont ${ent(s.unsettled_result_known)} au résultat connu mais non tranchable` : '' }));
  h.appendChild(carteKpi({ icone: 'balance', titre: 'Paris gagnés', valeur: ent(s.won),
    sous: `${ent(s.lost)} perdus · ${ent(s.void)} annulés` }));
  h.appendChild(carteKpi({ icone: 'cible', titre: 'EV moyenne', valeur: pct(s.ev_mean, 1),
    sous: 'à la détection' }));
  h.appendChild(carteKpi({ icone: 'cote', titre: 'Cote moyenne', valeur: cote(s.odds_mean),
    sous: 'cote prise' }));
  compterCartes(h);
}

function kpisClv(s) {
  const h = $('kpis-clv');
  h.innerHTML = '';
  const couvFaible = s.clv_coverage !== null && s.clv_coverage < SEUIL_COUVERTURE;
  h.appendChild(carteKpi({ icone: 'courbe', cls: 'kpi-clv', titre: 'CLV moyenne',
    valeur: pct(s.clv, 1), signe: signe(s.clv),
    sous: `couverture ${pctNu(s.clv_coverage, 0)}`, fragile: couvFaible }));
  h.appendChild(carteKpi({ icone: 'mediane', titre: 'CLV médiane', valeur: pct(s.clv_median, 1),
    signe: signe(s.clv_median), sous: `la moitié des ${ent(s.clv_n)} paris mesurés font au moins autant` }));
  h.appendChild(carteKpi({ icone: 'pourcent', titre: 'CLV positives', valeur: pctNu(s.clv_positive_rate, 0),
    sous: 'des paris mesurés battent la clôture' }));
  h.appendChild(carteKpi({ icone: 'bouclier', titre: 'Couverture CLV', valeur: pctNu(s.clv_coverage, 0),
    sous: `${ent(s.clv_n)} paris mesurés sur ${ent(s.opportunities)}`,
    fragile: couvFaible, badge: badge(s.sample_clv) }));
  compterCartes(h);
}

function kpisParis(s) {
  const h = $('kpis-paris');
  h.innerHTML = '';
  h.appendChild(carteKpi({ icone: 'radar', titre: 'Opportunités', valeur: ent(s.opportunities),
    sous: `${ent(s.matches)} matchs distincts` }));
  h.appendChild(carteKpi({ icone: 'coche', titre: 'Paris joués', valeur: ent(s.played),
    sous: s.played_rate === null || s.played_rate === undefined ? 'cliqués sur « Jouer »'
      : `${pctNu(s.played_rate)} des opportunités` }));
  h.appendChild(carteKpi({ icone: 'pile', titre: 'Réglées', valeur: ent(s.settled),
    sous: `${pctNu(s.settlement_rate)} de settlement` }));
  h.appendChild(carteKpi({ icone: 'euro', titre: 'ROI', valeur: pct(s.roi, 1), signe: signe(s.roi),
    sous: `P&L ${eur(s.pnl, 0)}` }));
  h.appendChild(carteKpi({ icone: 'courbe', titre: 'CLV moyenne', valeur: pct(s.clv, 1),
    signe: signe(s.clv), sous: `couverture ${pctNu(s.clv_coverage, 0)}` }));
  compterCartes(h);
}

/* Ce que le PDF doit emporter avec ses chiffres : la période, le périmètre
 * et TOUTES les règles réellement appliquées — telles que le serveur les a
 * renvoyées, jamais telles que l'interface croit les avoir envoyées. La
 * différence entre les deux est précisément ce qu'un export doit révéler. */
function enteteExport(d) {
  const f = d.filters || {};
  const hote = $('entete-pdf');
  hote.innerHTML = '';
  hote.hidden = false;
  hote.appendChild(el('h2', null, 'Valuebet Analytics — export'));

  const lignes = [];
  // Les libellés lus à l'écran, pas les clés techniques (« soccer »).
  const joint = (v, nom) => (v && v.length ? v.map(nom || String).join(', ') : 'tous');
  const table = (t) => Object.entries(t || {})
    .map(([k, v]) => `${nomSport(k)} : ${Array.isArray(v) ? v.join(', ') : v}`).join(' · ');
  /* ⚠️ UN COUPLE DE BORNES N'EST PAS UNE LISTE. `table()` joint à la virgule
   * et rendait « 1.0-1.8 : 3, » quand la borne haute manque : sur le papier,
   * cette virgule orpheline ne dit ni que la borne est absente, ni laquelle
   * des deux vaut 3. Une flèche et un tiret le disent. */
  // Clé de sport → son libellé ; une tranche de cote n'en a pas et reste telle.
  const bornes = (t) => Object.entries(t || {})
    .map(([k, v]) => `${nomSport(k)} : ${v[0] ?? '—'} → ${v[1] ?? '—'}`).join(' · ');

  lignes.push(['Période', `${f.date_from || REFS.date_min || '—'} → ${f.date_to || REFS.date_max || '—'}`]);
  lignes.push(['Sports', joint(f.sports, nomSport)]);
  lignes.push(['Bookmakers', joint(f.bookmakers, nomBook)]);
  lignes.push(['Marchés', joint(f.markets, nomMarche)]);
  // Le pari retenu change le lot autant qu'un sport : le papier doit le
  // nommer, et sous le libellé lu à l'écran, pas sous « home ».
  const nomPari = (v) => ((REFS.outcomes || []).find((o) => o.value === v)
                          || { libelle: v }).libelle;
  lignes.push(['Pari', (f.outcomes && f.outcomes.length)
    ? f.outcomes.map(nomPari).join(', ') : 'tous']);
  if (f.leagues && f.leagues.length) lignes.push(['Compétition', joint(f.leagues)]);

  const ev = [];
  if (f.ev_bands && f.ev_bands.length) ev.push(`tranches ${f.ev_bands.join(', ')}`);
  if (f.ev_min != null || f.ev_max != null) ev.push(`bornes ${f.ev_min ?? '—'} → ${f.ev_max ?? '—'}`);
  const evs = table(f.ev_bands_by_sport);
  const evl = bornes(f.ev_free_by_sport);
  const evc = bornes(f.ev_free_by_odds);
  if (evs) ev.push(`par sport : ${evs}`);
  if (evl) ev.push(`bornes par sport : ${evl}`);
  // ⚠️ Cette ligne PRIME sur les deux précédentes pour les tranches qu'elle
  // nomme. Le papier doit le dire, sinon deux règles contradictoires se
  // lisent comme cumulatives.
  if (evc) ev.push(`bornes par tranche de cote (prioritaires) : ${evc}`);
  lignes.push(['EV', ev.length ? ev.join(' · ') : 'aucune contrainte']);

  const co = [];
  if (f.odds_bands && f.odds_bands.length) co.push(`tranches ${f.odds_bands.join(', ')}`);
  if (f.odds_min != null || f.odds_max != null) co.push(`bornes ${f.odds_min ?? '—'} → ${f.odds_max ?? '—'}`);
  const cos = table(f.odds_bands_by_sport);
  if (cos) co.push(`par sport : ${cos}`);
  lignes.push(['Cote', co.length ? co.join(' · ') : 'aucune contrainte']);

  if (f.delay_min != null || f.delay_max != null) {
    lignes.push(['Délai', `${f.delay_min ?? '—'} h → ${f.delay_max ?? '—'} h`]);
  }
  const pop = (REFS.populations || []).find((x) => x.value === f.population);
  lignes.push(['Population', pop ? pop.libelle : (f.population || '—')]);
  lignes.push(['Joué', f.played === 'oui' ? 'joués (cliqués sur « Jouer »)'
    : f.played === 'non' ? 'non joués' : 'tous']);
  lignes.push(['Mise', estKelly(f)
    ? `Kelly ${fractionKelly(f.kelly_fraction)} — bankroll ${f.bankroll} € (fixe), `
      + `plafond ${f.kelly_cap_pct} % par pari ; ROI pondéré par la mise`
    : `${f.stake} € par pari (fixe)`]);
  lignes.push(['Exporté le', new Date().toLocaleString('fr-BE')]);

  const dl = el('dl', 'export-filtres');
  lignes.forEach(([k, v]) => {
    dl.appendChild(el('dt', null, k));
    dl.appendChild(el('dd', null, String(v)));
  });
  hote.appendChild(dl);

  // La limite de la population va sur le papier AUSSI : c'est elle qui dit
  // ce que le chiffre ne couvre pas.
  if (pop && pop.limites && pop.limites.length) {
    const ul = el('ul', 'export-limites');
    pop.limites.forEach((l) => ul.appendChild(el('li', null, l)));
    hote.appendChild(ul);
  }
}


/* ⚠️ LA RÈGLE D'EV EST RELUE DEPUIS LA RÉPONSE DU SERVEUR, jamais depuis les
 * cases cochées. Afficher ce qu'on a coché prouverait seulement qu'on sait
 * lire son propre formulaire ; afficher ce que le serveur dit avoir appliqué
 * est la seule vérification qui vaille. */
function noteEv(regles) {
  const h = $('note-ev');
  if (!h) return;
  h.innerHTML = '';
  if (!regles) return;
  const f = (ANALYSE && ANALYSE.filters) || {};
  const parts = [];
  if (f.ev_min != null || f.ev_max != null) {
    parts.push(`bornes ${bornesTexte(f.ev_min, f.ev_max, ' %')}`);
  }
  const bySport = regles.by_sport || {};
  Object.keys(bySport).forEach((sp) => {
    parts.push(`${nomSport(sp)} : ${bySport[sp].join(' ou ') || 'toutes tranches'}`);
  });
  const restants = (regles.sports_analyses || []).filter((sp) => !bySport[sp]);
  if (restants.length && (regles.global || []).length) {
    parts.push(`${restants.map(nomSport).join(', ')} : ${regles.global.join(' ou ')}`);
  }
  Object.entries(f.ev_free_by_sport || {}).forEach(([sp, v]) => {
    parts.push(`${nomSport(sp)} : bornes ${bornesTexte(v[0], v[1], ' %')}`);
  });
  Object.entries(f.ev_free_by_odds || {}).forEach(([bande, v]) => {
    parts.push(`cote ${bande} : ${bornesTexte(v[0], v[1], ' %')} (prioritaire)`);
  });
  h.textContent = 'Règle d\'EV appliquée par le serveur — '
    + (parts.length ? parts.join(' · ') : 'aucune contrainte d\'EV');
}

/* ── Avertissements ────────────────────────────────────────────────── */

/* Un bandeau d'avertissement. Un avertissement long se lit en entier d'un
 * clic — ou d'Entrée au clavier ; il n'est jamais retiré de la page, donc
 * jamais absent du PDF. Partagé par l'analyse et le Strategy Finder. */
function blocAvert(m, grave) {
  const d = el('div', 'avert' + (grave ? ' grave' : ''));
  d.innerHTML = '<svg class="ic-av" viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/></svg>';
  d.appendChild(el('span', 'avert-texte', m));
  d.title = 'Cliquer pour lire en entier';
  d.tabIndex = 0;
  d.setAttribute('aria-expanded', 'false');
  const basculer = () => {
    d.setAttribute('aria-expanded', String(d.classList.toggle('deplie')));
  };
  d.addEventListener('click', basculer);
  d.addEventListener('keydown', (e) => {
    // Seulement sur le bandeau lui-même : un bouton qu'il contient
    // (« Réessayer maintenant ») garde son propre Entrée.
    if (e.target !== d || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    basculer();
  });
  return d;
}

function avertissements(liste) {
  const h = $('avertissements');
  h.innerHTML = '';
  (liste || []).forEach((m) => {
    const grave = /^(Erreur|Filtre refusé|Impossible)/.test(m);
    h.appendChild(blocAvert(m, grave));
  });
}

/* ── Détail ────────────────────────────────────────────────────────── */

const deuxLignes = (haut, bas) => {
  const f = document.createDocumentFragment();
  f.appendChild(document.createTextNode(haut));
  if (bas) f.appendChild(el('span', 'sub', bas));
  return f;
};
const COLONNES = [
  ['detected_at', 'Détecté', (i) => dateHeure(i.detected_at)],
  ['sport', 'Sport', (i) => nomSport(i.sport)],
  [null, 'Match', (i) => deuxLignes(i.event || '—', i.league || '')],
  [null, 'Pari', (i) => deuxLignes(libPari(i.selection),
    nomMarche(i.market) + (i.line !== null ? ' ' + i.line : ''))],
  ['book', 'Bookmaker', (i) => nomBook(i.bookmaker)],
  ['odd_taken', 'Cote', (i) => cote(i.odds), 'num'],
  ['ev_pct', 'EV', (i) => pct(i.ev_pct, 1), 'num'],
  ['clv', 'CLV', (i) => deuxLignes(pct(i.clv_pct, 1),
    i.closing_fair_odd ? `clôture ${cote(i.closing_fair_odd)}` : 'sans clôture'), 'num'],
  [null, 'Résultat', null],
  // En mise Kelly, chaque pari a SA mise : elle s'affiche sous le P&L (lue
  // dans la réponse du serveur, jamais recalculée ici).
  ['pnl', 'P&L', (i) => (estKelly(ANALYSE && ANALYSE.filters) && i.stake != null
    ? deuxLignes(eur(i.pnl, 2), `mise ${eur(i.stake, 2).replace('+', '')}`)
    : eur(i.pnl, 2)), 'num'],
  [null, 'Statut', null],
];

const RESULTAT_FR = { won: 'Gagné', lost: 'Perdu', void: 'Annulé',
  unsettled: 'Non réglé' };

function etiquetteStatut(i) {
  return el('span', 'etiq ' + (i.played ? 'joue' : 'nonjoue'), i.played ? 'Joué' : 'Non joué');
}

function tableauDetail(d) {
  const t = $('detail');
  t.innerHTML = '';
  const thead = el('thead'), tr = el('tr');
  COLONNES.forEach(([tri, titre, , cls]) => {
    const th = el('th', [cls || '', tri ? 'triable' : '', tri && tri === TRI ? 'actif' : '']
      .filter(Boolean).join(' '));
    th.textContent = titre + (tri && tri === TRI ? (ORDRE === 'desc' ? ' ▾' : ' ▴') : '');
    if (tri) {
      const trier = () => {
        if (TRI === tri) ORDRE = ORDRE === 'desc' ? 'asc' : 'desc';
        else { TRI = tri; ORDRE = 'desc'; }
        PAGE = 1;
        chargerDetail();
      };
      th.tabIndex = 0;
      th.setAttribute('aria-sort', tri === TRI ? (ORDRE === 'desc' ? 'descending' : 'ascending') : 'none');
      th.addEventListener('click', trier);
      th.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); trier(); }
      });
    }
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  t.appendChild(thead);

  const tb = el('tbody');
  if (!d.items.length) {
    const r = el('tr', 'vide-ligne'), td = el('td', null, 'Aucune opportunité.');
    td.colSpan = COLONNES.length;
    r.appendChild(td);
    tb.appendChild(r);
  }
  d.items.forEach((i) => {
    const r = el('tr');
    COLONNES.forEach(([, titre, rendu, cls]) => {
      const td = el('td', cls || '');
      if (titre === 'Résultat') {
        td.appendChild(el('span', 'etiq ' + i.result, RESULTAT_FR[i.result] || i.result));
      } else if (titre === 'Statut') {
        td.appendChild(etiquetteStatut(i));
      } else {
        const r0 = rendu(i);
        if (typeof r0 === 'string') td.textContent = r0; else td.appendChild(r0);
        // L'EV d'une value bet est positive par construction : la colorer ne
        // dirait rien. Seuls les RÉSULTATS (CLV, P&L) portent une couleur.
        if (titre === 'CLV' || titre === 'P&L') {
          const v = titre === 'CLV' ? i.clv_pct : i.pnl;
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
  if (!d.total) return;
  const prec = el('button', 'btn btn-secondaire btn-petit', '‹ Précédent');
  prec.type = 'button';
  prec.disabled = d.page <= 1;
  prec.addEventListener('click', () => { PAGE = d.page - 1; chargerDetail(); });
  const suiv = el('button', 'btn btn-secondaire btn-petit', 'Suivant ›');
  suiv.type = 'button';
  suiv.disabled = d.page >= d.pages;
  suiv.addEventListener('click', () => { PAGE = d.page + 1; chargerDetail(); });
  p.appendChild(el('span', null,
    `Page ${ent(d.page)} / ${ent(d.pages)} — ${ent(d.total)} opportunités`));
  p.appendChild(prec);
  p.appendChild(suiv);
}

/* ⚠️ UNE CELLULE DE MATRICE PART SOUS FORME DE TRANCHES, PAS DE BORNES.
 *
 * La version précédente traduisait la cellule en bornes (`odds_min`,
 * `ev_min`…), et elle mentait deux fois. Les bornes de l'API sont
 * INCLUSIVES alors que les tranches de la matrice sont semi-ouvertes : 12
 * cellules sur 30 rendaient plus de lignes que leur effectif. Et `set`
 * ÉCRASAIT les bornes de l'utilisateur au lieu de les restreindre : avec
 * « EV ≥ 6 % », la cellule 5-8 % en comptait 41, le détail 72. La tranche est
 * la clé même de la matrice ; l'envoyer telle quelle rend exactement la
 * cellule, et les bornes de l'utilisateur restent en place (ET).
 *
 * Les règles PAR SPORT (`ev_bands_<s>`, `odds_bands_<s>`) priment sur la
 * règle globale pour leur sport : on les remplace par la tranche de la
 * cellule si elles la contiennent — sinon ce sport ne peut rien avoir dans la
 * cellule et il est retiré de la liste des sports. */
function requeteCellule(p, cellule) {
  p.set('odds_bands', cellule.odds);
  p.set('ev_bands', cellule.ev);
  const exclus = new Set();
  [...new Set(p.keys())].forEach((k) => {
    const regle = k.startsWith('ev_bands_') ? ['ev_bands_', cellule.ev]
      : k.startsWith('odds_bands_') ? ['odds_bands_', cellule.odds] : null;
    if (!regle) return;
    if (p.getAll(k).includes(regle[1])) p.set(k, regle[1]);
    else exclus.add(k.slice(regle[0].length));
  });
  if (exclus.size) {
    const sports = p.getAll('sports').length ? p.getAll('sports') : REFS.sports;
    const restants = sports.filter((sp) => !exclus.has(sp));
    // ⚠️ Plus aucun sport possible : la cellule est vide. Une liste de sports
    // VIDE voudrait dire « tous les sports » pour l'API — l'inverse exact.
    if (!restants.length) return null;
    p.delete('sports');
    restants.forEach((sp) => p.append('sports', sp));
  }
  return p;
}

/* La lentille de la page « Paris joués » : joués, non joués ou tous. Elle
 * ne s'applique que si l'analyse elle-même ne filtre pas déjà sur « Joué » —
 * sinon c'est le filtre de l'analyse qui décide, et la page le dit. */
function lentilleParis() {
  const joue = (ANALYSE && ANALYSE.filters && ANALYSE.filters.played) || 'tous';
  return joue === 'tous' ? PJ_MODE : joue;
}

async function chargerDetail() {
  const jeton = {};
  JETON_DETAIL = jeton;
  const p = parametresAnalyse({ page: PAGE, per_page: PAR_PAGE, sort: TRI, order: ORDRE,
    played: lentilleParis() });
  // Une cellule de matrice RESTREINT les filtres, elle ne les remplace pas :
  // l'utilisateur doit retrouver exactement le sous-ensemble qu'il a cliqué.
  if (CELLULE && !requeteCellule(p, CELLULE)) {
    tableauDetail({ items: [], total: 0, page: 1, pages: 0 });
    return;
  }
  $('detail-filtre').textContent = CELLULE
    ? `Restreint à la cellule cote ${CELLULE.odds} × EV ${CELLULE.ev} de la matrice. `
    : 'Lot de l\'analyse affichée. Cliquez sur un en-tête pour trier.';
  if (CELLULE) {
    const retirer = el('button', 'avert-plus', 'Retirer la restriction');
    retirer.type = 'button';
    retirer.addEventListener('click', () => { CELLULE = null; PAGE = 1; chargerDetail(); matrice(); });
    $('detail-filtre').appendChild(retirer);
  }
  try {
    const d = await appel(API_DETAIL, p);
    // Une réponse plus ancienne (tri, page ou analyse précédents) ne doit
    // jamais écraser la plus récente.
    if (JETON_DETAIL !== jeton) return;
    tableauDetail(d);
  } catch (e) {
    if (JETON_DETAIL !== jeton) return;
    // ⚠️ NE PAS REMPLACER LE CONTENEUR DE LA TABLE : l'appel SUIVANT
    // planterait sur `null`, transformant une erreur passagère en panne
    // définitive. Le message va à côté.
    $('detail').innerHTML = '';
    $('pagination').innerHTML = '';
    $('detail-filtre').textContent = 'Détail indisponible : ' + e.message;
  }
}

/* Les dernières opportunités de la vue d'ensemble : la même requête que le
 * détail, triée par date de détection, huit lignes. */
async function chargerDernieres() {
  const t = $('t-dernieres');
  const entetes = [['Date / heure'], ['Sport'], ['Compétition'], ['Match'], ['Marché'],
    ['Pari'], ['Cote', 'num'], ['Bookmaker'], ['EV', 'num'], ['Statut']];
  const tete = () => {
    const thead = el('thead'), tr = el('tr');
    entetes.forEach(([n, c]) => tr.appendChild(el('th', c || '', n)));
    thead.appendChild(tr);
    return thead;
  };
  const jeton = {};
  JETON_DERNIERES = jeton;
  try {
    const r = await appel(API_DETAIL, parametresAnalyse({ page: 1, per_page: 8,
      sort: 'detected_at', order: 'desc' }));
    if (JETON_DERNIERES !== jeton) return;
    t.innerHTML = '';
    t.appendChild(tete());
    const tb = el('tbody');
    if (!r.items.length) {
      const l = el('tr', 'vide-ligne'), td = el('td', null, 'Aucune opportunité.');
      td.colSpan = entetes.length;
      l.appendChild(td);
      tb.appendChild(l);
    }
    r.items.forEach((i) => {
      const l = el('tr');
      [dateHeure(i.detected_at), nomSport(i.sport), i.league || '—', i.event || '—',
       nomMarche(i.market) + (i.line !== null ? ' ' + i.line : ''), libPari(i.selection)]
        .forEach((v) => l.appendChild(el('td', null, v)));
      l.appendChild(el('td', 'num', cote(i.odds)));
      l.appendChild(el('td', null, nomBook(i.bookmaker)));
      // L'EV d'une value bet est positive par construction : pas de couleur.
      l.appendChild(el('td', 'num', pct(i.ev_pct, 1)));
      const tdS = el('td');
      tdS.appendChild(etiquetteStatut(i));
      l.appendChild(tdS);
      tb.appendChild(l);
    });
    t.appendChild(tb);
  } catch (e) {
    if (JETON_DERNIERES !== jeton) return;
    t.innerHTML = '';
    t.appendChild(tete());
    const tb = el('tbody'), l = el('tr', 'vide-ligne');
    const td = el('td', null, 'Dernières opportunités indisponibles : ' + e.message);
    td.colSpan = entetes.length;
    l.appendChild(td);
    tb.appendChild(l);
    t.appendChild(tb);
  }
}


/* ── EV par sport ──────────────────────────────────────────────────
 *
 * ⚠️ UN PANNEAU PAR SPORT COCHÉ, ET LA SÉLECTION SURVIT AU REDESSIN.
 * Cocher « Tennis » après avoir réglé l'EV du football ne doit pas effacer ce
 * qui était réglé : le panneau est reconstruit, mais les cases déjà cochées
 * sont relues et réappliquées.
 */

/* Deux champs « min → max » pour une CLÉ donnée — un sport, ou une tranche
 * de cote. Les identifiants suivent la convention `<quoi>-min-<clé>`, celle
 * que `parametres()` relit pour construire `ev_min_<sport>` comme
 * `ev_odds_min_<slug>`.
 *
 * ⚠️ L'ATTRIBUT S'APPELLE `data-cle` ET PAS `data-sport`. Il porte un slug de
 * tranche aussi souvent qu'un sport, et un nom qui ment sur son contenu finit
 * par produire le filtre d'à côté. */
function bornesPaire(quoi, cle, valeurs, unite) {
  const ligne = el('div', 'paire bornes-sport');
  ['min', 'max'].forEach((bout, i) => {
    const n = el('input');
    n.type = 'number';
    n.id = `${quoi}-${bout}-${cle}`;
    n.dataset.cle = cle;
    n.placeholder = bout + (unite ? ' ' + unite : '');
    n.step = quoi === 'cote' ? '0.05' : '0.5';
    n.value = valeurs[i] || '';
    if (i) ligne.appendChild(el('span', null, '→'));
    ligne.appendChild(n);
  });
  return ligne;
}

/* La valeur courante des bornes d'un panneau, pour la restituer après un
 * redessin : perdre une saisie parce qu'on a coché un sport de plus serait
 * une petite trahison répétée à chaque clic. */
function bornesCourantes(hote, quoi) {
  const out = {};
  hote.querySelectorAll('input[type="number"][data-cle]').forEach((n) => {
    const cle = n.dataset.cle;
    out[cle] = out[cle] || ['', ''];
    out[cle][n.id.startsWith(quoi + '-min-') ? 0 : 1] = n.value;
  });
  return out;
}

function panneauxEvParSport() {
  const hote = $('ev-par-sport');
  const actif = $('ev-split').checked;
  const memoire = {};
  const bornes = bornesCourantes(hote, 'ev');
  hote.querySelectorAll('[data-sport].cases').forEach((n) => {
    memoire[n.dataset.sport] = coches(n.id);
  });
  hote.innerHTML = '';
  $('f-ev-bands').style.display = actif ? 'none' : '';
  $('ev-tranches-aide').textContent = actif
    ? 'Tranches réglées sport par sport — voir « Segmentation avancée ».'
    : 'Plusieurs tranches = union (OU).';
  if (!actif || !REFS) return;

  const sports = coches('f-sports');
  (sports.length ? sports : REFS.sports).forEach((sp) => {
    const bloc = el('div', 'ev-sport');
    bloc.appendChild(el('h4', null, nomSport(sp)));
    const boite = el('div', 'cases');
    boite.id = 'ev-bands-' + sp;
    boite.dataset.sport = sp;
    bloc.appendChild(boite);
    // Bornes LIBRES propres au sport. Elles se cumulent en ET avec les
    // tranches juste au-dessus, exactement comme `ev_min`/`ev_max` se
    // cumulent avec les tranches globales.
    bloc.appendChild(bornesPaire('ev', sp, bornes[sp] || ['', ''], '%'));
    hote.appendChild(bloc);
    groupeCases(boite, REFS.ev_bands, { courte: true, tous: 'Toutes les tranches',
      coches: memoire[sp] || [] });
  });
  if (!hote.children.length) {
    hote.appendChild(el('p', 'aide', 'Choisissez au moins un sport.'));
  }
}

/* Jumeau exact de `panneauxEvParSport`, pour les tranches de cote. */
function panneauxCoteParSport() {
  const hote = $('cote-par-sport');
  const actif = $('cote-split').checked;
  const memoire = {};
  hote.querySelectorAll('[data-sport].cases').forEach((n) => {
    memoire[n.dataset.sport] = coches(n.id);
  });
  hote.innerHTML = '';
  $('f-odds-bands').style.display = actif ? 'none' : '';
  if (!actif || !REFS) return;

  const sports = coches('f-sports');
  (sports.length ? sports : REFS.sports).forEach((sp) => {
    const bloc = el('div', 'ev-sport');
    bloc.appendChild(el('h4', null, nomSport(sp)));
    const boite = el('div', 'cases');
    boite.id = 'odds-bands-' + sp;
    boite.dataset.sport = sp;
    bloc.appendChild(boite);
    hote.appendChild(bloc);
    groupeCases(boite, bandesCote(), { courte: true, tous: 'Toutes les tranches',
      coches: memoire[sp] || [] });
  });
  if (!hote.children.length) {
    hote.appendChild(el('p', 'aide', 'Choisissez au moins un sport.'));
  }
}

/* ── EV par tranche de cote ────────────────────────────────────────
 *
 * ⚠️ TOUTES LES TRANCHES SONT AFFICHÉES, PAS SEULEMENT CELLES COCHÉES
 * PLUS HAUT. Une tranche laissée vide n'est pas « sans contrainte » : elle
 * garde la règle de son sport, puis la règle globale.
 */
function panneauxEvParCote() {
  const hote = $('ev-par-cote');
  const actif = $('ev-cote-split').checked;
  const bornes = bornesCourantes(hote, 'evcote');
  hote.innerHTML = '';
  if (!actif || !REFS) return;

  const grille = el('div', 'ev-cote-grille');
  (REFS.odds_bands || []).forEach((b) => {
    grille.appendChild(el('span', 'ev-cote-nom', b.key));
    grille.appendChild(bornesPaire('evcote', b.slug,
                                   bornes[b.slug] || ['', ''], '%'));
  });
  hote.appendChild(grille);
  hote.appendChild(el('p', 'aide',
    'Vide = cette tranche garde la règle générale.'));
}

/* `REFS.odds_bands` porte des objets `{key, slug, min, max}` ; `groupeCases` veut
 * `{key, label}`. La clé RESTE canonique — c'est elle qui repart en filtre. */
function bandesCote() {
  return (REFS && REFS.odds_bands ? REFS.odds_bands : [])
    .map((b) => ({ key: b.key, label: b.key }));
}

/* ── Meilleurs segments ────────────────────────────────────────────
 *
 * ⚠️ LA MISE EN GARDE EST RENDUE AVANT LE TABLEAU, TOUJOURS.
 * Un classement de segments lu sans le nombre de combinaisons testées n'est
 * pas une information : c'est une illusion d'optique. Le bandeau n'est donc
 * pas une note de bas de page qu'on peut ne pas atteindre — il est au-dessus,
 * dans le flux, et il porte le compte réel.
 */

function tableauSegments(d) {
  const garde = $('s-garde');
  garde.innerHTML = '';
  const g = el('div', 'mise-en-garde');
  (d.warnings || []).forEach((w, i) => {
    const pp = el('p');
    if (i === 0) pp.appendChild(el('b', null, '⚠️ '));
    pp.appendChild(el('span', null, w));
    g.appendChild(pp);
  });
  const compte = el('p');
  compte.appendChild(el('b', null, `${ent(d.combinaisons_testees)} combinaisons testées`));
  compte.appendChild(el('span', null,
    ` · ${ent(d.retenus)} au-dessus du plancher de ${ent(d.min_n)}`
    + ` · ${ent(d.ecartes_effectif)} écartées pour effectif`
    + (d.redondants_fusionnes
      ? ` · ${ent(d.redondants_fusionnes)} descriptions redondantes fusionnées` : '')
    + (d.tronque ? ` · ${ent(d.tronque)} non affichées` : '')));
  g.appendChild(compte);
  garde.appendChild(g);

  const t = $('segments');
  t.innerHTML = '';
  const cols = [['Segment', ''], ['n', 'num'], ['Échantillon', ''],
    ['Réglés', 'num'], ['CLV', 'num'], ['Couv.', 'num'], ['ROI', 'num'],
    ['P&L', 'num']];
  const thead = el('thead'), tr = el('tr');
  cols.forEach(([titre, cls]) => tr.appendChild(el('th', cls, titre)));
  thead.appendChild(tr);
  t.appendChild(thead);

  const tb = el('tbody');
  if (!d.segments.length) {
    const r = el('tr', 'vide-ligne'), td = el('td');
    td.colSpan = cols.length;
    td.textContent = 'Aucun segment ne passe le plancher d\'effectif. '
      + 'Baissez le plancher ou élargissez les filtres — ce n\'est pas un résultat '
      + 'nul, c\'est une absence de mesure.';
    r.appendChild(td);
    tb.appendChild(r);
  }
  d.segments.forEach((sg) => {
    const r = el('tr');
    const td0 = el('td');
    sg.criteres.forEach((c) => {
      const chip = el('span', 'crit');
      chip.appendChild(el('b', null, c.label + ' '));
      chip.appendChild(el('span', null, c.display));
      td0.appendChild(chip);
    });
    r.appendChild(td0);
    r.appendChild(el('td', 'num', ent(sg.opportunities)));
    const tdE = el('td');
    const b = badge(sg.sample);
    if (b) tdE.appendChild(b);
    r.appendChild(tdE);
    r.appendChild(el('td', 'num', ent(sg.settled)));
    r.appendChild(el('td', 'num ' + signe(sg.clv), pct(sg.clv, 1)));
    r.appendChild(el('td', 'num', pctNu(sg.clv_coverage, 0)));
    r.appendChild(el('td', 'num ' + signe(sg.roi), pct(sg.roi, 1)));
    r.appendChild(el('td', 'num ' + signe(sg.pnl), eur(sg.pnl, 0)));
    accrocher(r, sg.criteres.map((c) => c.display).join(' × '),
      lignesTranche(sg).concat([['vs lot entier',
        `CLV ${pct((d.overall || {}).clv, 1)} sur ${ent((d.overall || {}).clv_n)} `
        + `(couv. ${pctNu((d.overall || {}).clv_coverage, 0)})`]]),
      alerteTranche(sg));
    tb.appendChild(r);
  });
  t.appendChild(tb);
}

async function chercherSegments() {
  const b = $('s-lancer');
  b.disabled = true;
  $('s-etat').textContent = 'recherche en cours…';
  try {
    tableauSegments(await appel(API_SEGMENTS, parametresAnalyse({
      min_n: $('s-min').value || 100,
      sort: $('s-tri').value,
      depth: $('s-prof').value,
      limit: 25,
    })));
    $('s-etat').textContent = '';
  } catch (e) {
    $('segments').innerHTML = '';
    $('s-etat').textContent = 'Segments indisponibles : ' + e.message;
  } finally {
    b.disabled = false;
  }
}

/* ── Tableaux de découpe ───────────────────────────────────────────
 *
 * Une tranche de l'API par ligne, sans rien recalculer. Le TRI est un choix
 * d'affichage : par défaut le volume, jamais un classement « du meilleur au
 * pire » que rien ne justifierait. Les découpes ordonnées par nature (cote,
 * EV, délai, période) gardent l'ordre du serveur.
 */

function parVolume(tranches) {
  return (tranches || []).slice().sort((a, b) => b.opportunities - a.opportunities);
}

function tableDecoupe(hote, tranches, options) {
  const o = options || {};
  if (!hote.etatTri || hote.etatTri.cle !== o.cle) {
    hote.etatTri = { cle: o.cle, champ: o.ordre ? null : 'opportunities', sens: -1 };
  }
  const etat = hote.etatTri;
  let lignes = (tranches || []).filter((t) => t.opportunities > 0);
  /* ⚠️ TRIER PAR UNE MESURE NE DOIT PAS COURONNER UN PETIT ÉCHANTILLON.
   * Un ROI de +80 % sur 4 paris réglés en tête de colonne se lirait comme le
   * meilleur segment. Les lignes sous SEUIL_REGLES (réglés pour le ROI et le
   * P&L, mesurés pour la CLV) sont triées entre elles, APRÈS les autres — et
   * le tableau le dit. */
  const faibleEffectif = (t, champ) => {
    if (['roi', 'pnl'].includes(champ)) return (t.settled || 0) < SEUIL_REGLES;
    if (['clv', 'clv_median', 'clv_positive_rate'].includes(champ)) return (t.clv_n || 0) < SEUIL_REGLES;
    return false;
  };
  let relegues = 0;
  if (etat.champ) {
    const champ = etat.champ;
    lignes = lignes.slice().sort((a, b) => {
      const fa = faibleEffectif(a, champ), fb = faibleEffectif(b, champ);
      if (fa !== fb) return fa ? 1 : -1;
      const va = champ === 'label' ? String(lib(a)) : a[champ];
      const vb = champ === 'label' ? String(lib(b)) : b[champ];
      const absA = va === null || va === undefined, absB = vb === null || vb === undefined;
      // Une mesure absente reste EN BAS dans les deux sens : un « — » en tête
      // de colonne se lirait comme le meilleur ou le pire, il n'est ni l'un
      // ni l'autre.
      if (absA || absB) return absA === absB ? 0 : absA ? 1 : -1;
      if (typeof va === 'string') return va.localeCompare(vb, 'fr') * etat.sens;
      return (va - vb) * etat.sens;
    });
    relegues = lignes.filter((t) => faibleEffectif(t, champ)).length;
  }
  if (o.filtre) {
    const q = o.filtre.toLowerCase();
    lignes = lignes.filter((t) => String(lib(t)).toLowerCase().includes(q));
  }
  const total = lignes.length;
  if (o.limite && total > o.limite) lignes = lignes.slice(0, o.limite);

  const cols = [['label', o.nom || 'Segment', 'nom'], ['opportunities', 'Opportunités', 'num']];
  if (o.complet) cols.push(['settled', 'Réglés', 'num']);
  cols.push(['clv', 'CLV', 'num']);
  if (o.complet) cols.push(['clv_median', 'CLV médiane', 'num'], ['clv_positive_rate', 'CLV > 0', 'num']);
  cols.push(['roi', 'ROI', 'num']);
  if (o.complet) cols.push(['pnl', 'P&L', 'num'], ['ev_mean', 'EV moy.', 'num'], ['odds_mean', 'Cote moy.', 'num']);

  hote.innerHTML = '';
  if (relegues && lignes.length > relegues) {
    const base = ['roi', 'pnl'].includes(etat.champ) ? 'réglés' : 'mesurés';
    hote.appendChild(el('caption', 'note-tri',
      `${ent(relegues)} ligne${relegues > 1 ? 's' : ''} à moins de ${SEUIL_REGLES} ${base} `
      + 'classée' + (relegues > 1 ? 's' : '') + ' en bas du tri — indice, pas résultat.'));
  }
  const thead = el('thead'), tr = el('tr');
  cols.forEach(([champ, titre, cls]) => {
    const actif = etat.champ === champ;
    const th = el('th', `${cls} triable${actif ? ' actif' : ''}`,
      titre + (actif ? (etat.sens < 0 ? ' ▾' : ' ▴') : ''));
    th.tabIndex = 0;
    th.setAttribute('aria-sort', actif ? (etat.sens < 0 ? 'descending' : 'ascending') : 'none');
    const trier = () => {
      if (etat.champ === champ) etat.sens = -etat.sens;
      else { etat.champ = champ; etat.sens = champ === 'label' ? 1 : -1; }
      tableDecoupe(hote, tranches, o);
      const th2 = hote.querySelectorAll('thead th')[cols.findIndex(([c]) => c === champ)];
      if (th2) th2.focus();
    };
    th.addEventListener('click', trier);
    th.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); trier(); }
    });
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  hote.appendChild(thead);

  const tb = el('tbody');
  if (!lignes.length) {
    const r = el('tr', 'vide-ligne'), td = el('td', null, o.filtre ? 'Aucune ligne ne correspond.' : 'Aucune donnée.');
    td.colSpan = cols.length;
    r.appendChild(td);
    tb.appendChild(r);
  }
  const cellule = (valeur, cls, sous, sousFragile) => {
    const td = el('td', cls);
    td.appendChild(document.createTextNode(valeur));
    if (sous) td.appendChild(el('span', 'sub' + (sousFragile ? ' fragile' : ''), sous));
    return td;
  };
  lignes.forEach((t) => {
    const r = el('tr');
    cols.forEach(([champ]) => {
      if (champ === 'label') {
        const td = el('td', 'nom', String(lib(t)));
        td.title = String(lib(t));
        r.appendChild(td);
      } else if (champ === 'opportunities') {
        r.appendChild(cellule(ent(t.opportunities), 'num', o.complet ? `${ent(t.matches)} matchs` : ''));
      } else if (champ === 'settled') {
        r.appendChild(cellule(ent(t.settled), 'num', pctNu(t.settlement_rate)));
      } else if (champ === 'clv') {
        // ⚠️ La CLV d'une ligne porte sa couverture, comme partout ailleurs.
        const faible = t.clv_coverage !== null && t.clv_coverage < SEUIL_COUVERTURE;
        r.appendChild(cellule(pct(t.clv, 1), 'num ' + signe(t.clv),
          `couv. ${pctNu(t.clv_coverage, 0)}`, faible));
      } else if (champ === 'roi') {
        const peu = t.settled > 0 && t.settled < SEUIL_REGLES;
        r.appendChild(cellule(pct(t.roi, 1), 'num ' + signe(t.roi),
          `${ent(t.settled)} réglés`, peu));
      } else if (champ === 'clv_median') {
        r.appendChild(cellule(pct(t.clv_median, 1), 'num ' + signe(t.clv_median)));
      } else if (champ === 'clv_positive_rate') {
        r.appendChild(cellule(pctNu(t.clv_positive_rate, 0), 'num'));
      } else if (champ === 'pnl') {
        r.appendChild(cellule(eur(t.pnl, 0), 'num ' + signe(t.pnl)));
      } else if (champ === 'ev_mean') {
        r.appendChild(cellule(pct(t.ev_mean, 1), 'num'));
      } else if (champ === 'odds_mean') {
        r.appendChild(cellule(cote(t.odds_mean), 'num'));
      }
    });
    accrocher(r, String(lib(t)), lignesTranche(t), alerteTranche(t));
    tb.appendChild(r);
  });
  hote.appendChild(tb);
  return total;
}

/* ── Rendu des pages ───────────────────────────────────────────────── */

function grapheEvolution(d) {
  document.querySelectorAll('#evol-mode button')
    .forEach((b) => b.classList.toggle('actif', b.dataset.mode === MODE_EVOL));
  $('evol-titre').textContent = { clv: 'Évolution de la CLV', ev: 'Évolution de l\'EV',
    mix: 'Évolution de la CLV et de l\'EV' }[MODE_EVOL];
  // ⚠️ La CLV ne sort jamais sans sa couverture, courbe comprise.
  const s = d.summary;
  $('evol-couv').textContent = MODE_EVOL === 'ev' ? ''
    : `CLV mesurée sur ${ent(s.clv_n)} opportunités · couverture ${pctNu(s.clv_coverage, 0)} sur la période.`;
  $('evol-couv').classList.toggle('fragile',
    MODE_EVOL !== 'ev' && s.clv_coverage !== null && s.clv_coverage < SEUIL_COUVERTURE);
  const leg = $('evol-legende');
  leg.innerHTML = '';
  if (MODE_EVOL === 'clv') {
    $('evol-sous').textContent = `CLV moyenne par période, en % — point pointillé : couverture sous ${SEUIL_COUVERTURE} %.`;
    courbe($('g-clv-temps'), d.by_time, 'clv', 'CLV dans le temps');
    return;
  }
  const series = MODE_EVOL === 'ev'
    ? [{ champ: 'ev_mean', libelle: 'EV moyenne', cls: '2' }]
    : [{ champ: 'clv', libelle: 'CLV moyenne', cls: '' },
       { champ: 'ev_mean', libelle: 'EV moyenne', cls: '2' }];
  $('evol-sous').textContent = MODE_EVOL === 'ev'
    ? 'EV moyenne à la détection, par période — en %.'
    : 'CLV moyenne et EV moyenne à la détection — même échelle, en %.';
  courbes($('g-clv-temps'), d.by_time, series, 'Évolution dans le temps');
  series.forEach((s) => {
    const l = el('span', 'l');
    l.appendChild(el('span', 'trait' + (s.cls === '2' ? ' t2' : '')));
    l.appendChild(el('span', null, s.libelle));
    leg.appendChild(l);
  });
}

/* ⚠️ DEUX GRAPHIQUES, JAMAIS UN DOUBLE AXE. Le ROI par période et le P&L
 * cumulé ont chacun leur échelle et leur unité : on bascule de l'un à
 * l'autre, on ne les superpose pas. */
function grapheFinance(d) {
  const roi = MODE_FIN === 'roi';
  document.querySelectorAll('#fin-mode button')
    .forEach((b) => b.classList.toggle('actif', b.dataset.mode === MODE_FIN));
  $('g-roi-temps').hidden = !roi;
  $('g-pnl-cumul').hidden = roi;
  const f = d.filters || {};
  $('fin-sous').textContent = !roi ? 'P&L cumulé, en € — somme des périodes réglées.'
    : estKelly(f)
      ? `ROI par période, en % — mise Kelly ${fractionKelly(f.kelly_fraction)} sur ${num(f.bankroll)} €, pondéré par la mise.`
      : `ROI par période, en % — mise notionnelle de ${num(f.stake)} € par pari réglé.`;
  if (roi) {
    courbe($('g-roi-temps'), d.by_time, 'roi', 'ROI dans le temps');
  } else {
    courbe($('g-pnl-cumul'), d.by_time, 'pnl_cumul', 'P&L cumulé',
      (v) => eur(v, 0));
  }
}

function rendreVueEnsemble(d, graphesSeuls) {
  kpis(d.summary);
  grapheEvolution(d);
  grapheFinance(d);
  barres($('g-clv-ev'), d.by_ev, 'clv', 'CLV par tranche d\'EV', { n: true });
  tableDecoupe($('t-sport'), d.by_sport, { nom: 'Sport', cle: 'sport' });
  tableDecoupe($('t-market'), d.by_market, { nom: 'Marché', cle: 'market' });
  tableDecoupe($('t-book'), d.by_book, { nom: 'Bookmaker', cle: 'book' });
  return graphesSeuls ? null : chargerDernieres();
}

function rendrePerformance(d) {
  kpisPerf(d.summary);
  colonnes($('g-vol-temps'), d.by_time, 'opportunities', 'Opportunités');
  colonnes($('g-reg-temps'), d.by_time, 'settled', 'Réglées');
  barres($('g-roi-sport'), d.by_sport, 'roi', 'ROI par sport');
  barres($('g-roi-cote'), d.by_odds, 'roi', 'ROI par cote');
  barres($('g-roi-ev'), d.by_ev, 'roi', 'ROI par EV');
  barres($('g-roi-delay'), d.by_delay, 'roi', 'ROI par délai');
}

function rendreClv(d) {
  kpisClv(d.summary);
  courbe($('g-clv-cumul'), d.by_time, 'clv_cumul', 'CLV cumulée');
  barres($('g-clv-sport'), d.by_sport, 'clv', 'CLV par sport');
  barres($('g-clv-cote'), d.by_odds, 'clv', 'CLV par cote');
  barres($('g-clv-delay'), d.by_delay, 'clv', 'CLV par délai');
  matrice();
}

function rendreBookmakers(d) {
  tableDecoupe($('t-book-complet'), d.by_book, { nom: 'Bookmaker', complet: true, cle: 'book' });
  const lot = parVolume(d.by_book);
  barres($('g-clv-book'), lot, 'clv', 'CLV par book');
  barres($('g-roi-book'), lot, 'roi', 'ROI par book');
}

function rendreMarches(d) {
  tableDecoupe($('t-market-complet'), d.by_market, { nom: 'Marché', complet: true, cle: 'market' });
  barres($('g-clv-market'), d.by_market, 'clv', 'CLV par marché');
  barres($('g-roi-market'), d.by_market, 'roi', 'ROI par marché');
  tableDecoupe($('t-outcome'), d.by_outcome, { nom: 'Pari', complet: true, ordre: true, cle: 'outcome' });
  barres($('g-clv-outcome'), d.by_outcome, 'clv', 'CLV par pari');
  barres($('g-roi-outcome'), d.by_outcome, 'roi', 'ROI par pari');
}

function rendreCompetitions(d) {
  const toutes = (d.by_league || []).filter((t) => t.opportunities > 0);
  const tete = parVolume(toutes).slice(0, 12);
  barres($('g-clv-league'), tete, 'clv', 'CLV par compétition');
  barres($('g-roi-league'), tete, 'roi', 'ROI par compétition');
  tableCompetitions();
}

function tableCompetitions() {
  if (!ANALYSE) return;
  const toutes = (ANALYSE.by_league || []).filter((t) => t.opportunities > 0);
  const n = tableDecoupe($('t-league'), toutes, { nom: 'Compétition', complet: true,
    cle: 'league', filtre: $('cmp-recherche').value.trim(), limite: LIMITE_CMP });
  $('cmp-compte').textContent = `${ent(toutes.length)} compétitions avec au moins une opportunité`
    + ($('cmp-recherche').value.trim() ? ` · ${ent(n)} correspondent à la recherche` : '');
  const plus = $('cmp-plus');
  plus.innerHTML = '';
  if (n > LIMITE_CMP) {
    const b = el('button', 'btn btn-secondaire btn-petit', `Afficher plus (${ent(n - LIMITE_CMP)} restantes)`);
    b.type = 'button';
    b.addEventListener('click', () => { LIMITE_CMP += 100; tableCompetitions(); });
    plus.appendChild(b);
  }
}

/* ⚠️ LA LENTILLE N'EST PAS UN FILTRE DE L'ANALYSE. Les boutons « Joués /
 * Non joués / Toutes » ne changent QUE cette page : les autres pages restent
 * sur l'analyse affichée. Quand l'analyse filtre déjà sur « Joué » (tiroir),
 * c'est elle qui décide et les boutons le montrent, grisés. Les KPI de la
 * lentille viennent d'une analyse du serveur — jamais d'un calcul local. */
const AIDE_PJ = {
  oui: 'Opportunités sur lesquelles vous avez cliqué « Jouer ». Les autres pages restent sur l\'analyse complète.',
  non: 'Opportunités jamais cliquées sur « Jouer » — alertées ou non. Les autres pages restent sur l\'analyse complète.',
  tous: 'Toutes les opportunités de l\'analyse affichée, jouées ou non.',
};

function majPjFiltre() {
  const impose = ANALYSE && ANALYSE.filters && ANALYSE.filters.played !== 'tous'
    ? ANALYSE.filters.played : null;
  const mode = impose || PJ_MODE;
  document.querySelectorAll('#pj-filtre button').forEach((b) => {
    b.classList.toggle('actif', b.dataset.played === mode);
    b.setAttribute('aria-pressed', String(b.dataset.played === mode));
    b.disabled = !!impose && b.dataset.played !== impose;
  });
  $('pj-aide').textContent = impose
    ? `L'analyse filtre déjà sur « ${impose === 'oui' ? 'Joués' : 'Non joués'} » (filtres avancés) : `
      + 'la page suit ce filtre.'
    : AIDE_PJ[mode];
}

/* Choisir la lentille AVANT d'arriver sur la page : la carte « Paris
 * joués » mène aux joués, « Opportunités » et « Tout voir » à tout le lot. */
function choisirLentille(mode) {
  PJ_MODE = mode;
  PAGE = 1;
  CELLULE = null;
  RENDUES.delete('paris');
}

async function rendreParis(d, graphesSeuls) {
  majPjFiltre();
  const mode = lentilleParis();
  const joue = (d.filters && d.filters.played) || 'tous';
  if (graphesSeuls) {
    if (PARIS && PARIS.mode === mode) kpisParis(PARIS.d.summary);
    return;
  }
  const detail = chargerDetail();
  if (joue !== 'tous' || mode === 'tous') {
    PARIS = { mode, d };
    kpisParis(d.summary);
    await detail;
    return;
  }
  if (PARIS && PARIS.mode === mode) { kpisParis(PARIS.d.summary); await detail; return; }
  const jeton = {};
  JETON_PARIS = jeton;
  squelettesKpi('kpis-paris', 5);
  try {
    const r = await appel(API_ANALYSE, parametresAnalyse({ played: mode }));
    if (JETON_PARIS === jeton) {
      PARIS = { mode, d: r };
      kpisParis(r.summary);
    }
  } catch (e) {
    if (JETON_PARIS === jeton) {
      $('kpis-paris').innerHTML = '';
      $('kpis-paris').appendChild(el('p', 'aide', 'Indicateurs indisponibles : ' + e.message));
    }
  }
  await detail;
}

/* ── Analyse avancée ───────────────────────────────────────────────
 *
 * PROGRESSIVE DISCLOSURE : l'utilisateur choisit d'abord un axe, et seuls
 * ses graphiques et son tableau apparaissent. Chaque axe est une découpe que
 * l'analyse a DÉJÀ rendue — rien n'est redemandé au serveur, sauf les
 * segments croisés, qui ont leur propre bouton.
 */
const AXES = [
  { cle: 'sport', lib: 'Sport', champ: 'by_sport', nom: 'Sport', section: 'champ-sports',
    filtre: (f) => listeTexte(f.sports, nomSport, 'Tous les sports') },
  { cle: 'book', lib: 'Bookmaker', champ: 'by_book', nom: 'Bookmaker', volume: true,
    section: 'champ-books', filtre: (f) => listeTexte(f.bookmakers, nomBook, 'Tous les bookmakers') },
  { cle: 'market', lib: 'Marché', champ: 'by_market', nom: 'Marché', section: 'champ-markets',
    filtre: (f) => listeTexte(f.markets, nomMarche, 'Tous les marchés') },
  { cle: 'league', lib: 'Compétition', champ: 'by_league', nom: 'Compétition',
    volume: true, top: 15, recherche: true, section: 'champ-league',
    filtre: (f) => listeTexte(f.leagues, (x) => x, 'Toutes les compétitions') },
  { cle: 'outcome', lib: 'Pari', champ: 'by_outcome', nom: 'Pari', ordre: true,
    section: 'champ-pari', filtre: (f) => listeTexte(f.outcomes, libPari, 'Tous les paris') },
  { cle: 'odds', lib: 'Cote', champ: 'by_odds', nom: 'Tranche de cote', ordre: true,
    section: 'champ-cote', regles: true, filtre: (f) => texteCote(f) || 'Toutes les cotes' },
  { cle: 'ev', lib: 'EV', champ: 'by_ev', nom: 'Tranche d\'EV', ordre: true,
    section: 'champ-ev', regles: true, filtre: (f) => texteEv(f) || 'Toute EV' },
  { cle: 'delay', lib: 'Délai', champ: 'by_delay', nom: 'Délai avant coup d\'envoi', ordre: true,
    section: 'champ-delai',
    filtre: (f) => (f.delay_min != null || f.delay_max != null
      ? `Délai ${bornesTexte(f.delay_min, f.delay_max, ' h')}` : 'Aucun délai imposé') },
  { cle: 'time', lib: 'Période', champ: 'by_time', nom: 'Période', ordre: true, temps: true,
    section: 'champ-axe',
    filtre: (f) => `${periodeTexte(f.date_from || REFS.date_min, f.date_to || REFS.date_max)}`
      + ` · pas ${libGranularite()}` },
  { cle: 'segments', lib: 'Segments croisés' },
];

/* Le pas de temps de l'analyse AFFICHÉE, sous le libellé du menu. */
function libGranularite() {
  const g = CLE_ANALYSE ? new URLSearchParams(CLE_ANALYSE).get('granularite') : $('f-gran').value;
  const o = [...$('f-gran').options].find((x) => x.value === g);
  return (o ? o.textContent : g || '').toLowerCase();
}

function rendreAvancee(d) {
  const h = $('aa-axes');
  h.innerHTML = '';
  AXES.forEach((a) => {
    const b = el('button', 'puce' + (a.cle === AXE_AA ? ' actif' : ''), a.lib);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(a.cle === AXE_AA));
    b.addEventListener('click', () => { AXE_AA = a.cle; $('aa-recherche').value = ''; rendreAvancee(ANALYSE); });
    h.appendChild(b);
  });
  $('aa-regles').textContent = $('note-ev').textContent
    || 'Aucune règle segmentée : la même règle d\'EV vaut pour tous les sports et toutes les cotes.';

  const axe = AXES.find((a) => a.cle === AXE_AA);
  $('aa-panneau').hidden = !axe || axe.cle === 'segments';
  $('bloc-segments').hidden = !axe || axe.cle !== 'segments';
  // Les règles d'EV segmentées ne concernent que les axes EV et Cote.
  $('aa-bloc-regles').hidden = !axe || !axe.regles;
  if (!axe || axe.cle === 'segments') return;

  // Ce que l'analyse affichée filtre sur CET axe, et où le régler.
  $('aa-param-valeur').textContent = axe.filtre(d.filters || {});
  $('aa-param-regler').onclick = () => ouvrirTiroir(axe.section);

  const tranches = d[axe.champ] || [];
  // « EV », « CLV » : un sigle garde ses capitales.
  const nomAxe = /^[A-Z]{2,}$/.test(axe.lib) ? axe.lib : axe.lib.toLowerCase();
  $('aa-titre-clv').textContent = axe.temps ? 'CLV dans le temps' : `CLV par ${nomAxe}`;
  $('aa-titre-roi').textContent = axe.temps ? 'ROI dans le temps' : `ROI par ${nomAxe}`;
  $('aa-titre-table').textContent = `Tableau complet — par ${nomAxe}`;
  if (axe.temps) {
    $('aa-sous').textContent = 'Moyenne par période ; effectifs et couverture au survol.';
    courbe($('aa-clv'), tranches, 'clv', 'CLV par période');
    courbe($('aa-roi'), tranches, 'roi', 'ROI par période');
  } else {
    const lot = axe.volume ? parVolume(tranches) : tranches;
    const visibles = axe.top ? lot.filter((t) => t.opportunities > 0).slice(0, axe.top) : lot;
    const n = tranches.filter((t) => t.opportunities > 0).length;
    $('aa-sous').textContent = axe.top && n > axe.top
      ? `Les ${axe.top} plus gros volumes sur ${ent(n)} — le tableau les donne toutes.`
      : 'Effectifs et couverture au survol de chaque barre.';
    barres($('aa-clv'), visibles, 'clv', `CLV par ${nomAxe}`);
    barres($('aa-roi'), visibles, 'roi', `ROI par ${nomAxe}`);
  }
  $('aa-recherche').hidden = !axe.recherche;
  tableDecoupe($('aa-table'), tranches, { nom: axe.nom, complet: true, ordre: axe.ordre,
    cle: axe.cle, filtre: axe.recherche ? $('aa-recherche').value.trim() : '' });
}

function rendrePage(p, graphesSeuls) {
  if (!ANALYSE || !PAGES[p] || !PAGES[p].analyse) return;
  if (!ANALYSE.summary.opportunities) return;
  if (!graphesSeuls && RENDUES.has(p)) {
    // Déjà rendue, mais à une autre largeur : on ne redessine que les
    // graphiques, sans relancer ses requêtes.
    if (!A_REDESSINER.has(p)) return;
    graphesSeuls = true;
  }
  A_REDESSINER.delete(p);
  RENDUES.add(p);
  const d = ANALYSE;
  // Rend la promesse des requêtes de la page (dernières, détail) : le
  // rapport PDF les attend avant d'imprimer.
  if (p === 'vue-ensemble') return rendreVueEnsemble(d, graphesSeuls);
  if (p === 'performance') return rendrePerformance(d);
  if (p === 'clv') return rendreClv(d);
  if (p === 'bookmakers') return rendreBookmakers(d);
  if (p === 'marches') return rendreMarches(d);
  if (p === 'competitions') return rendreCompetitions(d);
  if (p === 'paris') return rendreParis(d, graphesSeuls);
  if (p === 'avancee') return rendreAvancee(d);
  return null;
}

/* ── Analyse ───────────────────────────────────────────────────────── */

async function analyser() {
  const bouton = $('analyser');
  const jeton = {};
  JETON = jeton;
  bouton.disabled = true;
  bouton.classList.add('charge');
  bouton.querySelector('.btn-lib').textContent = 'Analyse…';
  document.body.classList.add('en-analyse');
  $('etat').textContent = 'analyse en cours…';
  if (!ANALYSE) squelettesKpi('kpis', 4);
  const envoyes = parametres({ granularite: $('f-gran').value });
  try {
    const d = await appel(API_ANALYSE, envoyes);
    // Une réponse plus ancienne que la dernière demande ne doit JAMAIS
    // écraser la plus récente : l'écran décrirait d'autres filtres.
    if (JETON !== jeton) return;
    ANALYSE = d;
    // Tout ce qui suit l'analyse (détail, dernières, segments, exports)
    // repart de CES paramètres, pas du formulaire : un filtre retouché sans
    // relancer ne doit pas faire décrire au tableau autre chose que les KPI.
    PARAMS_ANALYSE = new URLSearchParams(envoyes);
    PARAMS_ANALYSE.delete('granularite');
    CLE_ANALYSE = envoyes.toString();
    CELLULE = null;
    PAGE = 1;
    PARIS = null;
    LIMITE_CMP = 50;
    SALE = cleFormulaire() !== CLE_ANALYSE;
    RENDUES.clear();
    A_REDESSINER.clear();

    avertissements(d.warnings);
    enteteExport(d);
    $('note-population').textContent =
      `Population : ${d.population.explication}` +
      (d.population.limites.length
        ? ' — Limite : ' + d.population.limites.join(' ') : '');
    noteEv(d.ev_rules);
    // Le tableau de segments précédent est vidé pour qu'il ne décrive jamais
    // d'autres filtres que ceux affichés au-dessus. La recherche n'est PAS
    // relancée d'office : trois dimensions croisées coûtent nettement plus
    // qu'une analyse.
    $('segments').innerHTML = '';
    $('s-garde').innerHTML = '';
    $('s-etat').textContent = 'Cliquez sur « Chercher » pour explorer les combinaisons.';

    const videLot = !d.summary.opportunities;
    document.body.classList.toggle('sans-donnees', videLot);
    $('vide').hidden = !(videLot && PAGES[PAGE_ACTIVE].analyse);
    $('sb-analyse').textContent = 'Analyse lancée à '
      + new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
    majSale();
    contexte();
    rendrePage(PAGE_ACTIVE);
    $('etat').textContent = '';
  } catch (e) {
    if (JETON !== jeton) return;
    // Une erreur de saisie doit se lire, pas disparaître dans la console.
    const message = e.statut === 400 || e.statut === 422
      ? 'Filtre refusé : ' + e.message
      : 'Erreur : ' + e.message;
    avertissements([message]);
    // Sur une page sans analyse (Paramètres, Mes analyses…), le bandeau
    // d'avertissement n'est pas sous les yeux : on le dit aussi en toast.
    if (!PAGES[PAGE_ACTIVE].analyse) toast(message, 6000);
    $('etat').textContent = '';
    if (!ANALYSE) $('kpis').innerHTML = '';
  } finally {
    if (JETON === jeton) {
      bouton.disabled = false;
      bouton.classList.remove('charge');
      bouton.querySelector('.btn-lib').textContent = 'Analyser';
      document.body.classList.remove('en-analyse');
    }
  }
}

/* ── Amorçage ──────────────────────────────────────────────────────── */

/* ⚠️ DIRE CE QUI PART, PAS CE QUI EST TAPÉ. Le serveur reçoit des heures
 * décimales ; quelqu'un qui saisit « 90 » en minutes doit pouvoir vérifier
 * d'un coup d'œil que c'est bien 1,5 h qui partira. */
function majAideDelai() {
  const unite = $('f-delay-unite').value;
  const f = unite === 'min' ? 1 / 60 : 1;
  const bout = (id) => {
    const v = valOuNull(id);
    return v === null ? '—' : `${(Number(v) * f).toFixed(2)} h`;
  };
  $('aide-delai').textContent = unite === 'min'
    ? `Envoyé au serveur : ${bout('f-delay-min')} → ${bout('f-delay-max')}`
    : 'Le serveur raisonne en heures décimales (0,25 = 15 min).';
}

/* Les bandes de délai en boutons : elles POSENT les bornes en heures, elles
 * n'ajoutent pas un filtre parallèle. Un second mécanisme de délai à côté du
 * premier finirait par en contredire l'autre sans que rien ne le signale. */
function boutonsDelai() {
  const h = $('delay-rapides');
  h.innerHTML = '';
  (REFS.delay_bands || []).forEach((b) => {
    const bt = el('button', null, b.key);
    bt.type = 'button';
    bt.addEventListener('click', () => {
      const deja = bt.classList.contains('actif');
      h.querySelectorAll('button').forEach((x) => x.classList.remove('actif'));
      // Ces bornes viennent du serveur, donc en HEURES. Laisser l'unité sur
      // « minutes » ferait lire « 1 min » là où la bande dit « 1 h ».
      $('f-delay-unite').value = 'h';
      $('f-delay-min').value = deja || b.min === null ? '' : b.min;
      $('f-delay-max').value = deja || b.max === null ? '' : b.max;
      if (!deja) bt.classList.add('actif');
      majAideDelai();
      signalerChangement();
    });
    h.appendChild(bt);
  });
}

/* Les sections du tiroir se replient : repliées par défaut, elles affichent
 * leur valeur courante dans l'en-tête — l'état se lit sans rien ouvrir.
 * ⚠️ Le gestionnaire est posé en JS : sans lui, l'en-tête serait un piège,
 * on cliquerait dessus et rien ne s'ouvrirait. */
function pliage() {
  document.querySelectorAll('.champ.pliable > .champ-tete').forEach((t) => {
    t.setAttribute('aria-expanded', 'false');
    t.addEventListener('click', () => {
      const ouvert = t.parentElement.classList.toggle('ouvert');
      t.setAttribute('aria-expanded', String(ouvert));
    });
  });
}

/* ── Changement de filtre : résumés, contexte, état « à relancer » ── */

function signalerChangement() {
  if (!REFS) return;
  // « Modifié » se DÉDUIT : revenir à la main aux filtres analysés efface
  // l'alerte, au lieu de la laisser allumée pour rien.
  SALE = !!CLE_ANALYSE && cleFormulaire() !== CLE_ANALYSE;
  majSale();
  majResumes();
  majPresets();
  contexte();
}

function majSale() {
  $('modifie').hidden = !(SALE && ANALYSE);
  $('analyser').classList.toggle('a-relancer', !!(SALE && ANALYSE));
}

function resumeGroupe(id, nom, tous) {
  const v = coches(id);
  if (!v.length) return [tous, false];
  if (v.length <= 2) return [v.map(nom).join(' + '), true];
  return [`${v.length} sélectionnés`, true];
}

function bornesTexte(a, b, unite, fmt) {
  const F = fmt || num;
  if (a != null && b != null) return `${F(a)} → ${F(b)}${unite}`;
  if (a != null) return `≥ ${F(a)}${unite}`;
  return `≤ ${F(b)}${unite}`;
}

function texteEv(f) {
  const parts = [];
  if (f.ev_min != null || f.ev_max != null) parts.push(`EV ${bornesTexte(f.ev_min, f.ev_max, ' %')}`);
  if (f.ev_bands && f.ev_bands.length) parts.push(`EV ${f.ev_bands.join(', ')}`);
  if (Object.keys(f.ev_bands_by_sport || {}).length || Object.keys(f.ev_free_by_sport || {}).length) {
    parts.push('EV par sport');
  }
  if (Object.keys(f.ev_free_by_odds || {}).length) parts.push('EV par tranche de cote');
  return parts.join(' + ');
}

function texteCote(f) {
  const parts = [];
  if (f.odds_bands && f.odds_bands.length) parts.push(`Cote ${f.odds_bands.join(', ')}`);
  if (f.odds_min != null || f.odds_max != null) parts.push(`Cote ${bornesTexte(f.odds_min, f.odds_max, '', cote)}`);
  if (Object.keys(f.odds_bands_by_sport || {}).length) parts.push('Cote par sport');
  return parts.join(' + ');
}

/* Les phrases d'un jeu de filtres — celui du serveur (`d.filters`) ou celui
 * du formulaire. Même vocabulaire que les menus, pour qu'une même contrainte
 * ne se lise pas de deux façons. */
function listeTexte(v, nom, tous) {
  if (!v || !v.length) return tous;
  return v.length <= 2 ? v.map(nom).join(' + ') : `${v.length} ${tous.split(' ').pop()}`;
}

function phrasesFiltres(f) {
  const out = [periodeTexte(f.date_from || REFS.date_min, f.date_to || REFS.date_max)];
  const liste = listeTexte;
  out.push(liste(f.sports, nomSport, 'Tous les sports'));
  out.push(liste(f.bookmakers, nomBook, 'Tous les bookmakers'));
  out.push(liste(f.markets, nomMarche, 'Tous les marchés'));
  if (f.leagues && f.leagues.length) out.push(liste(f.leagues, (x) => x, 'Toutes les compétitions'));
  if (f.outcomes && f.outcomes.length) out.push('Pari ' + f.outcomes.map(libPari).join(', '));
  out.push(texteEv(f) || 'Toute EV');
  const c = texteCote(f);
  if (c) out.push(c);
  if (f.delay_min != null || f.delay_max != null) {
    out.push(`Délai ${bornesTexte(f.delay_min, f.delay_max, ' h')}`);
  }
  out.push('Population : ' + nomPopulation(f.population));
  if (f.played && f.played !== 'tous') out.push(f.played === 'oui' ? 'Joués' : 'Non joués');
  // La mise Kelly change le ROI lui-même (pondéré par la mise) : elle se lit
  // dans la barre, comme un filtre. La mise fixe ne change que le P&L.
  if (estKelly(f)) out.push(`Mise Kelly ${fractionKelly(f.kelly_fraction)}`);
  return out;
}

/* ⚠️ LA BARRE DE CONTEXTE DÉCRIT CE QUE LES CHIFFRES AFFICHÉS DÉCRIVENT : les
 * filtres que le SERVEUR a appliqués à la dernière analyse. Tant qu'un
 * réglage n'est pas analysé, elle ne change pas — le badge « Filtres
 * modifiés » le signale à côté. */
function contexte() {
  const h = $('contexte');
  if (!REFS) return;
  const f = ANALYSE ? ANALYSE.filters : filtresDuFormulaire();
  h.innerHTML = '';
  phrasesFiltres(f).forEach((p, i) => {
    if (i) h.appendChild(el('span', 'sep', '·'));
    h.appendChild(el(i === 0 ? 'b' : 'span', null, p));
  });
  h.title = ANALYSE ? 'Filtres appliqués par le serveur à l\'analyse affichée' : '';
}

function compteAvances() {
  const f = filtresDuFormulaire();
  let n = 0;
  if (f.leagues.length) n += 1;
  if (f.outcomes.length) n += 1;
  if (texteCote(f)) n += 1;
  if ($('ev-split').checked || $('ev-cote-split').checked) n += 1;
  if (f.delay_min != null || f.delay_max != null) n += 1;
  if (f.population !== 'detected') n += 1;
  if (f.played !== 'tous') n += 1;
  if (estKelly(f)) n += 1;
  return n;
}

function majResumes() {
  const f = filtresDuFormulaire();
  const pose = (id, [texte, actif]) => {
    const n = $(id);
    n.textContent = texte;
    n.classList.toggle('actif', !!actif);
  };
  const tousBooks = `Tous (${REFS.bookmakers.length})`;
  pose('r-sports', resumeGroupe('f-sports', nomSport, 'Tous'));
  pose('r-books', resumeGroupe('f-books', nomBook, tousBooks));
  pose('r-markets', resumeGroupe('f-markets', nomMarche, 'Tous'));
  pose('r-league', resumeGroupe('f-league', (x) => x, 'Toutes'));
  pose('r-pari', resumeGroupe('f-outcomes', libPari, 'Tous'));
  const tc = texteCote(f);
  pose('r-cote', [tc ? tc.replace(/^Cote /, '') : 'Toutes', !!tc]);
  const evGlobal = texteEv({ ...f, ev_bands_by_sport: {}, ev_free_by_sport: {}, ev_free_by_odds: {} });
  pose('r-ev', [evGlobal ? evGlobal.replace(/EV /g, '') : 'Toute EV', !!evGlobal]);
  const seg = [$('ev-split').checked ? 'par sport' : '', $('ev-cote-split').checked ? 'par tranche de cote' : '']
    .filter(Boolean);
  pose('r-segmentation', [seg.length ? 'EV ' + seg.join(' + ') : 'Aucune', !!seg.length]);
  const delai = f.delay_min != null || f.delay_max != null;
  pose('r-delai', [delai ? bornesTexte(f.delay_min, f.delay_max, ' h') : 'Aucun', delai]);
  pose('r-population', [nomPopulation(f.population), f.population !== 'detected']);
  pose('r-joue', [$('f-played').selectedIndex >= 0
    ? $('f-played').options[$('f-played').selectedIndex].textContent : 'Tous', f.played !== 'tous']);
  pose('r-mise', [estKelly(f) ? `Kelly ${fractionKelly(f.kelly_fraction)}`
    : `${num(f.stake)} € par pari`, estKelly(f)]);
  pose('r-axe', [$('f-gran').options[$('f-gran').selectedIndex].textContent, false]);

  // Les quatre raccourcis de la barre.
  const bouton = (id, [texte, actif]) => {
    const b = $(id);
    b.querySelector('.menu-val').textContent = texte;
    b.classList.toggle('filtre-actif', !!actif);
  };
  bouton('pf-sport', resumeGroupe('f-sports', nomSport, 'Tous'));
  bouton('pf-books', resumeGroupe('f-books', nomBook, tousBooks));
  bouton('pf-market', resumeGroupe('f-markets', nomMarche, 'Tous'));
  const ev = texteEv(f);
  bouton('pf-ev', [ev ? ev.replace(/^EV /, '') : 'Toute EV', !!ev]);

  const n = compteAvances();
  $('nb-avances').hidden = !n;
  $('nb-avances').textContent = String(n);
  document.querySelectorAll('#gran-rapide button')
    .forEach((b) => b.classList.toggle('actif', b.dataset.gran === $('f-gran').value));
}

/* ── Modes « Cote » et « EV » du tiroir ────────────────────────────
 *
 * Le mode choisit ce qui est VISIBLE, et vide ce qui ne l'est plus : un
 * filtre caché qui continuerait de s'appliquer serait exactement la panne
 * silencieuse que cette interface existe pour empêcher. En mode « Tranches »,
 * les bornes restent proposées : elles se cumulent (ET) avec les cases. */

function segActif(id, attr, valeur) {
  document.querySelectorAll(`#${id} button`)
    .forEach((b) => b.classList.toggle('actif', b.dataset[attr] === valeur));
}

function appliquerModeCote(mode, vider) {
  MODE_COTE = mode;
  segActif('cote-mode', 'mode', mode);
  $('cote-bloc-tranches').hidden = mode !== 'tranches';
  $('cote-bloc-bornes').hidden = mode === 'toutes';
  $('cote-bornes-titre').textContent = mode === 'tranches'
    ? 'Bornes additionnelles (facultatif)' : 'Cote minimum → maximum';
  $('cote-bornes-aide').textContent = mode === 'tranches'
    ? 'Se cumulent (ET) avec les tranches cochées.' : 'Bornes incluses.';
  if (vider) {
    if (mode !== 'tranches') {
      cocher('f-odds-bands', []);
      if ($('cote-split').checked) { $('cote-split').checked = false; panneauxCoteParSport(); }
    }
    if (mode === 'toutes') { $('f-odds-min').value = ''; $('f-odds-max').value = ''; }
    signalerChangement();
  }
}

/* Fixe ou Kelly : le mode choisit le bloc VISIBLE ; le champ caché
 * `f-stake-mode` est ce qui part (`parametres`). Rien n'est vidé : les deux
 * réglages restent prêts, un seul est envoyé. */
function appliquerModeMise(mode, signaler) {
  const m = mode === 'kelly' ? 'kelly' : 'flat';
  $('f-stake-mode').value = m;
  segActif('mise-mode', 'mode', m);
  $('mise-bloc-fixe').hidden = m !== 'flat';
  $('mise-bloc-kelly').hidden = m !== 'kelly';
  if (signaler) signalerChangement();
}

function appliquerModeEv(mode, vider) {
  MODE_EV = mode;
  segActif('ev-mode', 'mode', mode);
  $('ev-bloc-tranches').hidden = mode !== 'tranches';
  document.querySelectorAll('.ev-max-part').forEach((n) => { n.hidden = mode === 'minimum'; });
  $('ev-bornes-titre').textContent = mode === 'minimum' ? 'EV minimum (%)'
    : mode === 'tranches' ? 'Bornes additionnelles (%) — facultatif' : 'EV minimum → maximum (%)';
  $('ev-bornes-aide').textContent = mode === 'tranches'
    ? 'Se cumulent (ET) avec les tranches cochées.'
    : mode === 'minimum' ? 'Laisser vide : aucune contrainte d\'EV.' : 'Bornes incluses.';
  if (vider) {
    if (mode !== 'tranches') cocher('f-ev-bands', []);
    if (mode === 'minimum') $('f-ev-max').value = '';
    signalerChangement();
  }
}

function deduireModes() {
  const coteTranches = coches('f-odds-bands').length || $('cote-split').checked;
  appliquerModeCote(coteTranches ? 'tranches'
    : (valOuNull('f-odds-min') || valOuNull('f-odds-max')) ? 'perso' : 'toutes', false);
  appliquerModeEv(coches('f-ev-bands').length ? 'tranches'
    : valOuNull('f-ev-max') ? 'perso' : 'minimum', false);
}

/* ── Menus déroulants ──────────────────────────────────────────────── */

function fermerMenus(rendreFocus) {
  document.querySelectorAll('.menu.ouvert').forEach((m) => {
    m.classList.remove('ouvert');
    m.querySelector('.menu-pop').hidden = true;
    const btn = m.querySelector(':scope > button');
    btn.setAttribute('aria-expanded', 'false');
    // Échap depuis un menu rend le focus à son bouton, pas au document.
    if (rendreFocus === true) btn.focus();
  });
}

function brancherMenu(idMenu, remplir) {
  const m = $(idMenu);
  const btn = m.querySelector(':scope > button');
  const pop = m.querySelector('.menu-pop');
  btn.setAttribute('aria-expanded', 'false');
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const deja = m.classList.contains('ouvert');
    fermerMenus();
    if (deja || !REFS) return;
    if (remplir) remplir(pop);
    m.classList.add('ouvert');
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  });
  pop.addEventListener('click', (e) => e.stopPropagation());
}

function itemMenu(texte, choisi, faire, compte) {
  const b = el('button', 'menu-item' + (choisi ? ' choisi' : ''));
  b.type = 'button';
  b.innerHTML = '<svg class="coche" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>';
  b.appendChild(el('span', null, texte));
  if (compte !== undefined) b.appendChild(el('span', 'compte', compte));
  b.addEventListener('click', faire);
  return b;
}

/* Sport et marché : un choix simple depuis la barre. Plusieurs valeurs à la
 * fois restent possibles dans le tiroir — et se relisent ici, cochées. */
function menuUnique(groupe, valeurs, nom, tous, apres) {
  return (pop) => {
    pop.innerHTML = '';
    const sel = coches(groupe);
    const choisir = (v) => () => {
      cocher(groupe, v === null ? [] : [v]);
      if (apres) apres();
      fermerMenus();
      signalerChangement();
    };
    pop.appendChild(itemMenu(tous, !sel.length, choisir(null)));
    valeurs.forEach((v) => pop.appendChild(itemMenu(nom(v), sel.includes(v), choisir(v))));
    pop.appendChild(el('div', 'menu-note', 'Plusieurs à la fois : Filtres avancés.'));
  };
}

function menuBooks(pop) {
  pop.innerHTML = '';
  const zone = el('div', 'menu-recherche');
  const rech = el('input');
  rech.type = 'search';
  rech.placeholder = 'Chercher un bookmaker…';
  rech.setAttribute('aria-label', 'Chercher un bookmaker');
  zone.appendChild(rech);
  pop.appendChild(zone);
  const sel = new Set(coches('f-books'));
  const tous = itemMenu('Tous les bookmakers', !sel.size, () => {
    cocher('f-books', []);
    fermerMenus();
    signalerChangement();
  }, ent(REFS.bookmakers.length));
  pop.appendChild(tous);
  pop.appendChild(el('div', 'menu-sep'));
  const liste = el('div');
  REFS.bookmakers.forEach((b) => {
    const l = el('label', 'case');
    const c = el('input');
    c.type = 'checkbox';
    c.checked = sel.has(b);
    c.addEventListener('change', () => {
      const cour = new Set(coches('f-books'));
      if (c.checked) cour.add(b); else cour.delete(b);
      cocher('f-books', [...cour]);
      tous.classList.toggle('choisi', !cour.size);
      signalerChangement();
    });
    l.appendChild(c);
    l.appendChild(el('span', null, nomBook(b)));
    liste.appendChild(l);
  });
  rech.addEventListener('input', () => {
    const q = rech.value.trim().toLowerCase();
    liste.querySelectorAll('.case').forEach((x) => {
      x.classList.toggle('masquee', q !== '' && !x.textContent.toLowerCase().includes(q));
    });
  });
  pop.appendChild(liste);
  setTimeout(() => rech.focus(), 0);
}

/* Les seuils proposés viennent des tranches d'EV du SERVEUR — leur borne
 * basse —, pas d'une liste écrite ici qui finirait par ne plus leur
 * correspondre. */
function seuilsEv() {
  const out = [];
  (REFS.ev_bands || []).forEach((b) => {
    const m = String(b).match(/^(\d+(?:\.\d+)?)/);
    if (m) {
      const v = Number(m[1]);
      if (v > 0 && !out.includes(v)) out.push(v);
    }
  });
  return out.sort((a, b) => a - b);
}

/* ⚠️ LA COCHE DIT CE QUI PART. « Toute EV » n'est cochée que si AUCUNE
 * contrainte d'EV ne part au serveur — règles par sport et par cote
 * comprises — et la choisir les retire toutes. Un seuil « ≥ X % » remplace
 * la règle globale (bornes, tranches) mais laisse la segmentation, qui
 * prime pour ses sports : le menu le rappelle. */
function menuEv(pop) {
  pop.innerHTML = '';
  const f = filtresDuFormulaire();
  const actuel = valOuNull('f-ev-min');
  const globalSimple = valOuNull('f-ev-max') === null && !coches('f-ev-bands').length;
  const segmentee = $('ev-split').checked || $('ev-cote-split').checked;
  const toute = () => {
    appliquerModeEv('minimum', false);
    $('f-ev-min').value = '';
    $('f-ev-max').value = '';
    cocher('f-ev-bands', []);
    if ($('ev-split').checked) { $('ev-split').checked = false; panneauxEvParSport(); }
    if ($('ev-cote-split').checked) { $('ev-cote-split').checked = false; panneauxEvParCote(); }
    fermerMenus();
    signalerChangement();
  };
  const seuil = (v) => () => {
    appliquerModeEv('minimum', false);
    cocher('f-ev-bands', []);
    $('f-ev-max').value = '';
    $('f-ev-min').value = String(v);
    fermerMenus();
    signalerChangement();
  };
  const texte = texteEv(f);
  if (texte && !(globalSimple && actuel !== null && seuilsEv().includes(Number(actuel)) && !segmentee)) {
    pop.appendChild(el('div', 'menu-note', `Réglage actuel : ${texte}`));
  }
  pop.appendChild(itemMenu('Toute EV', !texte, toute));
  seuilsEv().forEach((v) => pop.appendChild(
    itemMenu(`≥ ${num(v)} %`, globalSimple && actuel !== null && Number(actuel) === v, seuil(v))));
  if (segmentee) {
    pop.appendChild(el('div', 'menu-note',
      'Des règles d\'EV par sport ou par cote s\'appliquent aussi et priment pour leurs sports. « Toute EV » les retire.'));
  }
  pop.appendChild(el('div', 'menu-sep'));
  pop.appendChild(itemMenu('Tranches, bornes, par sport…', false, () => {
    fermerMenus();
    ouvrirTiroir('champ-ev');
  }));
}

/* ── Tiroir ────────────────────────────────────────────────────────── */

function ouvrirTiroir(section) {
  fermerMenus();
  const t = $('tiroir');
  if (!t.classList.contains('ouvert')) {
    const actif = document.activeElement;
    OUVREUR = actif && actif !== document.body && $('app').contains(actif) ? actif : $('ouvrir-filtres');
  }
  t.classList.add('ouvert');
  t.setAttribute('aria-hidden', 'false');
  $('voile-filtres').hidden = false;
  document.body.classList.add('fige');
  // Tant que le tiroir est ouvert, le reste de l'application sort du
  // parcours clavier et des lecteurs d'écran : c'est un dialogue modal.
  $('app').inert = true;
  if (section && $(section)) {
    const s = $(section);
    s.classList.add('ouvert');
    const tete = s.querySelector('.champ-tete');
    if (tete) tete.setAttribute('aria-expanded', 'true');
    setTimeout(() => s.scrollIntoView({ block: 'start', behavior: 'smooth' }), 60);
  }
  setTimeout(() => $('fermer-filtres').focus(), 60);
}

function fermerTiroir() {
  const t = $('tiroir');
  if (!t.classList.contains('ouvert')) return;
  t.classList.remove('ouvert');
  t.setAttribute('aria-hidden', 'true');
  $('voile-filtres').hidden = true;
  document.body.classList.remove('fige');
  $('app').inert = false;
  const cible = OUVREUR && document.contains(OUVREUR) && !OUVREUR.closest('[hidden]')
    ? OUVREUR : $('ouvrir-filtres');
  OUVREUR = null;
  cible.focus();
}

/* ── Analyses rapides ──────────────────────────────────────────────
 *
 * ⚠️ AUCUNE DÉFINITION MÉTIER N'EST INVENTÉE ICI. « Premium » est la
 * population « Alertables » du serveur, dont la porte rejouée est celle du
 * canal premium par défaut — sa définition vit dans `src.analytics`. Les
 * autres raccourcis ouvrent une page ou posent un filtre existant.
 */

function ouvrirAxe(cle) {
  AXE_AA = cle;
  RENDUES.delete('avancee');
  allerA('avancee');
}

function presets() {
  const h = $('presets');
  h.innerHTML = '';
  const premium = (REFS.populations || []).find((p) => p.value === 'eligible_for_alert');
  const liste = [
    { id: 'global', lib: 'Performance globale',
      titre: 'Toutes les détections, sur toute la période, sans aucun filtre',
      faire: () => { $('reinit').click(); analyser(); } },
    premium && { id: 'premium', lib: 'Premium',
      titre: `${premium.libelle} — ${premium.explication} Porte du canal premium, définition du serveur.`,
      faire: () => { $('f-population').value = premium.value; majAidePopulation(); signalerChangement(); analyser(); } },
    { id: 'joues', lib: 'Paris joués', titre: 'Seulement les opportunités cliquées sur « Jouer »',
      faire: () => { $('f-played').value = 'oui'; signalerChangement(); analyser(); } },
    { id: 'clv', lib: 'CLV', titre: 'La performance face à la ligne de clôture',
      faire: () => allerA('clv') },
    { id: 'book', lib: 'Par bookmaker', faire: () => allerA('bookmakers') },
    { id: 'sport', lib: 'Par sport', faire: () => ouvrirAxe('sport') },
    { id: 'delai', lib: 'Par délai', faire: () => ouvrirAxe('delay') },
    { id: 'cote', lib: 'Par cote', faire: () => ouvrirAxe('odds') },
  ].filter(Boolean);
  liste.forEach((p) => {
    const b = el('button', 'puce', p.lib);
    b.type = 'button';
    b.dataset.preset = p.id;
    if (p.titre) b.title = p.titre;
    b.addEventListener('click', p.faire);
    h.appendChild(b);
  });
  majPresets();
}

function majPresets() {
  const f = filtresDuFormulaire();
  const etat = {
    global: !compteAvances() && !f.sports.length && !f.bookmakers.length && !f.markets.length
      && !texteEv(f) && f.date_from === REFS.date_min && f.date_to === REFS.date_max,
    premium: f.population === 'eligible_for_alert',
    joues: f.played === 'oui',
  };
  document.querySelectorAll('#presets .puce').forEach((b) => {
    b.classList.toggle('actif', !!etat[b.dataset.preset]);
  });
}

/* ── Pages ─────────────────────────────────────────────────────────── */

const PAGES = {
  'vue-ensemble': { titre: 'Vue d\'ensemble', sous: 'Analysez les performances de votre système de détection', analyse: true },
  performance: { titre: 'Performance', sous: 'ROI, P&L et volume de la population sélectionnée', analyse: true },
  clv: { titre: 'CLV', sous: 'Le prix obtenu face à la ligne de clôture Pinnacle', analyse: true },
  bookmakers: { titre: 'Bookmakers', sous: 'Opportunités, CLV et ROI par bookmaker', analyse: true },
  marches: { titre: 'Marchés', sous: 'Par marché et par pari', analyse: true },
  competitions: { titre: 'Compétitions', sous: 'Opportunités, CLV et ROI par compétition', analyse: true },
  paris: { titre: 'Paris joués', sous: 'Le détail des opportunités, pari par pari', analyse: true },
  avancee: { titre: 'Analyse avancée', sous: 'Découpez la population selon l\'axe de votre choix', analyse: true },
  strategies: { titre: 'Strategy Finder', sous: 'Découvrez automatiquement les configurations de paris les plus performantes.', analyse: false },
  'mes-analyses': { titre: 'Mes analyses', sous: 'Vos configurations d\'analyse sauvegardées', analyse: false },
  exporter: { titre: 'Exporter', sous: 'PDF et CSV de l\'analyse courante', analyse: false },
  parametres: { titre: 'Paramètres', sous: 'Apparence, préférences et données', analyse: false },
};

function pageDuLien() {
  const p = (location.hash || '').replace(/^#\/?/, '');
  return PAGES[p] ? p : 'vue-ensemble';
}

function allerA(p) {
  if (location.hash === '#/' + p) afficherPage(p);
  else location.hash = '#/' + p;
}

function afficherPage(p) {
  PAGE_ACTIVE = p;
  const meta = PAGES[p];
  // Le retour arrière du navigateur peut quitter la page sous une modale
  // ouverte : elle ne doit pas rester posée sur une autre page.
  if (SF_MODALE && p !== 'strategies') sfFermerModale(false);
  // Lu par la feuille d'impression : le papier du Strategy Finder ne porte
  // pas l'en-tête des filtres de l'analyse, qui ne l'ont pas produit.
  document.body.dataset.page = p;
  document.querySelectorAll('.page').forEach((s) => { s.hidden = s.dataset.page !== p; });
  document.querySelectorAll('.sb-lien').forEach((a) => {
    const on = a.dataset.page === p;
    a.classList.toggle('actif', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  $('titre-page').textContent = meta.titre;
  $('sous-titre-page').textContent = meta.sous;
  document.title = `${meta.titre} — Valuebet Analytics`;
  $('bloc-filtres').hidden = !meta.analyse;
  $('avertissements').hidden = !meta.analyse;
  const videLot = ANALYSE && !ANALYSE.summary.opportunities;
  $('vide').hidden = !(videLot && meta.analyse);
  fermerNavMobile();
  fermerMenus();
  window.scrollTo(0, 0);
  if (meta.analyse) rendrePage(p);
  if (p === 'mes-analyses') rendreMesAnalyses();
  if (p === 'parametres') rendreParametres();
  if (p === 'strategies') sfPreparer();
}

/* ── Navigation : barre latérale et téléphone ─────────────────────── */

function replierSidebar(r) {
  document.documentElement.classList.toggle('sb-replie', r);
  stock.ecrire('vb-sidebar', r ? 'replie' : 'ouvert');
  $('pr-sidebar').checked = r;
  // Les graphiques sont dessinés à la largeur de leur conteneur : on les
  // redessine une fois la transition de la barre terminée.
  setTimeout(redessiner, 240);
}

function ouvrirNavMobile() {
  $('app').classList.add('nav-ouverte');
  $('voile-nav').hidden = false;
}
function fermerNavMobile() {
  $('app').classList.remove('nav-ouverte');
  $('voile-nav').hidden = true;
}

let LARGEUR_FENETRE = window.innerWidth;
function redessiner() {
  // Les courbes du détail d'une configuration suivent la largeur, elles aussi.
  if (SF_DETAIL && SF_MODALE === 'sf-detail') sfGraphes(SF_DETAIL);
  if (!ANALYSE) return;
  // Les pages déjà rendues mais cachées ont été dessinées à l'ancienne
  // largeur : elles seront redessinées quand on y reviendra.
  RENDUES.forEach((p) => { if (p !== PAGE_ACTIVE) A_REDESSINER.add(p); });
  rendrePage(PAGE_ACTIVE, true);
}

/* ── Thème ─────────────────────────────────────────────────────────
 *
 * Préférence enregistrée d'abord (« light » / « dark »), sinon le thème du
 * système — suivi en direct tant que l'utilisateur n'a rien imposé. Le
 * premier rendu est posé par le script de `<head>`, avant tout affichage. */

function choixTheme() {
  const t = stock.lire('vb-theme', 'system');
  return t === 'light' || t === 'dark' ? t : 'system';
}
function appliquerTheme(choix, anime) {
  const racine = document.documentElement;
  const effectif = choix === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : choix;
  if (anime) {
    racine.classList.add('theme-transition');
    setTimeout(() => racine.classList.remove('theme-transition'), 320);
  }
  racine.setAttribute('data-theme', effectif);
  segActif('pr-theme', 'themeChoix', choix);
}

/* ── Mes analyses ──────────────────────────────────────────────────
 *
 * Enregistrées dans CE navigateur (`localStorage`) : aucune route
 * d'écriture n'est ajoutée à une API qui se veut en lecture seule. On stocke
 * l'état du FORMULAIRE, pas une URL : restaurer, c'est remettre les cases et
 * les bornes où elles étaient, puis relancer l'analyse.
 */

const CLE_ANALYSES = 'vb-analyses';
const CHAMPS_ETAT = ['f-date-from', 'f-date-to', 'f-odds-min', 'f-odds-max', 'f-ev-min',
  'f-ev-max', 'f-delay-min', 'f-delay-max', 'f-delay-unite', 'f-population', 'f-played',
  'f-stake', 'f-stake-mode', 'f-kelly-fraction', 'f-bankroll', 'f-kelly-cap', 'f-gran'];

function lireAnalyses() {
  try { return JSON.parse(stock.lire(CLE_ANALYSES, '[]')) || []; } catch (_) { return []; }
}
function ecrireAnalyses(l) { stock.ecrire(CLE_ANALYSES, JSON.stringify(l)); }

function capturerEtat() {
  const e = { v: {}, c: {}, b: {}, t: {} };
  CHAMPS_ETAT.forEach((id) => { e.v[id] = $(id).value; });
  ['ev-split', 'cote-split', 'ev-cote-split'].forEach((id) => { e.t[id] = $(id).checked; });
  document.querySelectorAll('#form .cases[id]').forEach((h) => {
    const v = coches(h.id);
    if (v.length) e.c[h.id] = v;
  });
  document.querySelectorAll('#form input[type="number"][data-cle]').forEach((n) => {
    if (n.value) e.b[n.id] = n.value;
  });
  return e;
}

function restaurerEtat(e) {
  $('reinit').click();
  Object.entries(e.v || {}).forEach(([id, v]) => { if ($(id)) $(id).value = v; });
  cocher('f-sports', (e.c || {})['f-sports'] || []);
  Object.entries(e.t || {}).forEach(([id, v]) => { if ($(id)) $(id).checked = !!v; });
  panneauxEvParSport();
  panneauxCoteParSport();
  panneauxEvParCote();
  Object.entries(e.c || {}).forEach(([id, v]) => cocher(id, v));
  Object.entries(e.b || {}).forEach(([id, v]) => { if ($(id)) $(id).value = v; });
  deduireModes();
  appliquerModeMise($('f-stake-mode').value);
  majAideDelai();
  majAidePopulation();
  signalerChangement();
}

function rendreMesAnalyses() {
  if (!REFS) return;
  $('ma-apercu').textContent = 'Configuration actuelle : '
    + phrasesFiltres(filtresDuFormulaire()).join(' · ');
  const h = $('ma-liste');
  h.innerHTML = '';
  const liste = lireAnalyses();
  if (!liste.length) {
    const c = el('article', 'carte analyse-carte');
    c.appendChild(el('h3', null, 'Aucune analyse sauvegardée'));
    c.appendChild(el('p', 'desc', 'Réglez la période et les filtres, puis sauvegardez : '
      + 'l\'analyse se recharge ensuite en un clic, filtres avancés compris.'));
    h.appendChild(c);
    return;
  }
  liste.forEach((a, i) => {
    const c = el('article', 'carte analyse-carte');
    c.appendChild(el('h3', null, a.nom));
    c.appendChild(el('div', 'quand', 'Sauvegardée le ' + new Date(a.cree)
      .toLocaleString('fr-FR', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit' })));
    c.appendChild(el('div', 'desc', a.resume));
    const act = el('div', 'actions');
    const charger = el('button', 'btn btn-primaire btn-petit', 'Charger et analyser');
    charger.type = 'button';
    charger.addEventListener('click', () => {
      restaurerEtat(a.etat || {});
      allerA('vue-ensemble');
      analyser();
      toast(`Analyse « ${a.nom} » chargée.`);
    });
    const suppr = el('button', 'btn btn-fantome btn-petit', 'Supprimer');
    suppr.type = 'button';
    suppr.addEventListener('click', () => {
      const l = lireAnalyses();
      l.splice(i, 1);
      ecrireAnalyses(l);
      rendreMesAnalyses();
    });
    act.appendChild(charger);
    act.appendChild(suppr);
    c.appendChild(act);
    h.appendChild(c);
  });
}

function sauverAnalyse() {
  const nom = $('ma-nom').value.trim()
    || `Analyse du ${new Date().toLocaleDateString('fr-FR')}`;
  const l = lireAnalyses();
  l.unshift({ nom, cree: new Date().toISOString(),
    resume: phrasesFiltres(filtresDuFormulaire()).join(' · '), etat: capturerEtat() });
  ecrireAnalyses(l.slice(0, 50));
  $('ma-nom').value = '';
  rendreMesAnalyses();
  toast(`Analyse « ${nom} » sauvegardée dans ce navigateur.`);
}

/* ── Exports ───────────────────────────────────────────────────────
 *
 * ⚠️ AUCUNE BIBLIOTHÈQUE. Le PDF passe par l'impression du navigateur ; le
 * CSV est une simple sérialisation de ce que l'API a rendu — pleine
 * précision, séparateur « ; » et virgule décimale pour un tableur français,
 * BOM UTF-8 pour les accents. Aucune valeur n'y est recalculée.
 */

const MAX_CSV = 20000;

function toast(message, reste) {
  const t = $('toast');
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toast.minuteur);
  // reste : true = jusqu'au prochain message ; un nombre = sa durée en ms.
  if (reste !== true) {
    toast.minuteur = setTimeout(() => { t.hidden = true; }, typeof reste === 'number' ? reste : 3800);
  }
}

function csvCellule(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') return String(v).replace('.', ',');
  if (typeof v === 'boolean') return v ? 'oui' : 'non';
  const s = String(v);
  return /[;"\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function csvTexte(entetes, lignes) {
  return '﻿' + [entetes].concat(lignes)
    .map((l) => l.map(csvCellule).join(';')).join('\r\n');
}
function telecharger(nom, texte) {
  const blob = new Blob([texte], { type: 'text/csv;charset=utf-8' });
  const lien = URL.createObjectURL(blob);
  const a = el('a');
  a.href = lien;
  a.download = nom;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(lien), 2000);
}
function nomFichier(quoi) {
  const f = (ANALYSE && ANALYSE.filters) || {};
  return `valuebet-${quoi}-${f.date_from || REFS.date_min}_${f.date_to || REFS.date_max}.csv`;
}
/* Un export décrit l'analyse AFFICHÉE : il repart de ses paramètres
 * (PARAMS_ANALYSE), jamais du formulaire. Des filtres retouchés depuis ne
 * bloquent donc rien — on le signale simplement, le fichier reste vérifiable
 * contre l'écran. */
function exportPossible() {
  if (!ANALYSE) { toast('Lancez d\'abord une analyse.'); return false; }
  return true;
}
const noteSale = () => (SALE ? ' Filtres modifiés depuis : le fichier décrit l\'analyse affichée.' : '');

/* Le PDF de la page courante : l'impression telle quelle. */
function imprimerPage() {
  fermerMenus();
  if (!exportPossible()) return;
  if (!PAGES[PAGE_ACTIVE].analyse) { imprimerRapport(); return; }
  if (SALE) toast('Filtres modifiés depuis : le PDF décrit l\'analyse affichée.', 5000);
  window.print();
}

/* ⚠️ LE RAPPORT COMPLET IMPRIME DES PAGES RENDUES, PAS DES PAGES VIDES.
 * Les pages sont dessinées à la demande : avant d'imprimer, chacune est
 * rendue, visible, et ses requêtes (dernières opportunités, détail) sont
 * ATTENDUES — sans quoi le papier montrerait des squelettes. */
async function imprimerRapport() {
  fermerMenus();
  if (!exportPossible()) return;
  if (!ANALYSE.summary.opportunities) { toast('Aucune opportunité dans l\'analyse affichée.'); return; }
  const pages = Object.keys(PAGES).filter((p) => PAGES[p].analyse
    && (p !== 'avancee' || (AXE_AA && AXE_AA !== 'segments')));
  toast('Préparation du rapport…', true);
  document.body.classList.add('impression-rapport');
  // Les avertissements (couverture, période) partent sur le papier même si
  // le rapport est lancé depuis une page outil, où ils sont masqués.
  $('avertissements').hidden = false;
  pages.forEach((p) => { $('page-' + p).hidden = false; });
  try {
    await Promise.all(pages.map((p) => {
      RENDUES.delete(p);
      A_REDESSINER.delete(p);
      return Promise.resolve(rendrePage(p)).catch(() => null);
    }));
  } finally {
    $('toast').hidden = true;
  }
  const fin = () => {
    window.removeEventListener('afterprint', fin);
    if (!document.body.classList.contains('impression-rapport')) return;
    document.body.classList.remove('impression-rapport');
    document.querySelectorAll('.page').forEach((x) => { x.hidden = x.dataset.page !== PAGE_ACTIVE; });
    $('avertissements').hidden = !PAGES[PAGE_ACTIVE].analyse;
  };
  window.addEventListener('afterprint', fin);
  if (SALE) toast('Filtres modifiés depuis : le PDF décrit l\'analyse affichée.', 5000);
  window.print();
  // `print()` rend la main une fois la boîte fermée ; `afterprint` couvre
  // les navigateurs qui ne bloquent pas.
  setTimeout(fin, 500);
}

const CHAMPS_CSV = [['opportunities', 'opportunites'], ['matches', 'matchs'],
  ['settled', 'reglees'], ['settlement_rate', 'taux_reglement_pct'], ['played', 'jouees'],
  ['played_rate', 'taux_joue_pct'], ['clv', 'clv_pct'], ['clv_n', 'clv_n'],
  ['clv_coverage', 'couverture_clv_pct'], ['clv_median', 'clv_mediane_pct'],
  ['clv_positive_rate', 'clv_positives_pct'], ['roi', 'roi_pct'], ['pnl', 'pnl_eur'],
  ['stake_total', 'mise_totale_eur'], ['ev_mean', 'ev_moyenne_pct'],
  ['odds_mean', 'cote_moyenne'], ['won', 'gagnes'], ['lost', 'perdus'], ['void', 'annules']];

function exporterCsvDecoupes() {
  fermerMenus();
  if (!exportPossible()) return;
  const d = ANALYSE;
  const axes = [['sport', 'by_sport'], ['bookmaker', 'by_book'], ['marche', 'by_market'],
    ['pari', 'by_outcome'], ['competition', 'by_league'], ['cote', 'by_odds'],
    ['ev', 'by_ev'], ['delai', 'by_delay'], ['periode', 'by_time']];
  const lignes = [['ensemble', 'tout', 'Tout le lot'].concat(CHAMPS_CSV.map(([c]) => d.summary[c]))];
  axes.forEach(([nom, champ]) => (d[champ] || []).forEach((t) => {
    lignes.push([nom, t.key, lib(t)].concat(CHAMPS_CSV.map(([c]) => t[c])));
  }));
  telecharger(nomFichier('decoupes'),
    csvTexte(['decoupe', 'cle', 'libelle'].concat(CHAMPS_CSV.map(([, n]) => n)), lignes));
  toast(`${ent(lignes.length)} lignes exportées.` + noteSale(), 5000);
}

const COLONNES_CSV = [['detected_at', 'detecte_le'], ['start_time', 'coup_envoi'],
  ['sport', 'sport'], ['league', 'competition'], ['event', 'match'], ['market', 'marche'],
  ['selection', 'pari'], ['line', 'ligne'], ['bookmaker', 'bookmaker'], ['odds', 'cote'],
  ['fair_odd', 'cote_juste'], ['ev_pct', 'ev_pct'], ['closing_fair_odd', 'cloture_juste'],
  ['clv_pct', 'clv_pct'], ['delay_h', 'delai_h'], ['played', 'joue'],
  ['notified_at', 'alerte_le'], ['result', 'resultat'], ['stake', 'mise_eur'], ['pnl', 'pnl_eur']];

async function exporterCsvOpportunites() {
  fermerMenus();
  if (!exportPossible()) return;
  const items = [];
  let total = 0;
  toast('Export en cours…', true);
  try {
    for (let page = 1; ; page += 1) {
      const r = await appel(API_DETAIL, parametresAnalyse({ page, per_page: 500,
        sort: 'detected_at', order: 'desc' }));
      total = r.total;
      items.push(...r.items);
      toast(`Export en cours… ${ent(Math.min(items.length, total))} / ${ent(total)}`, true);
      if (page >= r.pages || items.length >= MAX_CSV) break;
    }
  } catch (e) {
    toast('Export impossible : ' + e.message);
    return;
  }
  const lot = items.slice(0, MAX_CSV);
  telecharger(nomFichier('opportunites'), csvTexte(COLONNES_CSV.map(([, n]) => n),
    lot.map((i) => COLONNES_CSV.map(([k]) => i[k]))));
  toast((lot.length < total
    ? `CSV limité aux ${ent(MAX_CSV)} opportunités les plus récentes sur ${ent(total)}.`
    : `${ent(lot.length)} opportunités exportées.`) + noteSale(), 5000);
}

/* ── Strategy Finder ───────────────────────────────────────────────
 *
 * ⚠️ UNE PAGE QUI MONTRE UN CLASSEMENT, PAS UNE PAGE QUI LE FAIT. Le serveur
 * explore les combinaisons, écarte les configurations trop peu fournies, les
 * valide hors échantillon et les ordonne ; ici, on met en forme sa réponse.
 * Trier le tableau est un choix d'affichage sur des valeurs rendues. Aucune
 * part, aucun écart, aucun compte de sous-périodes n'est refait dans le
 * navigateur : ce serait une seconde définition, sous une étiquette qui la
 * dirait identique à celle du serveur.
 *
 * ⚠️ LE VOCABULAIRE RESTE HISTORIQUE. Une configuration « a présenté » une
 * CLV sur une période passée ; le texte ne promet rien de plus que la mesure.
 */

/* Le texte de la modale « Comment ça marche », tant que le serveur n'a pas
 * rendu le sien (`method`) — c'est lui qui fait foi dès qu'il est là. */
const SF_METHODE = [
  'Valuebet analyse différentes combinaisons de bookmaker, marché, type de pari, EV, cote et délai.',
  'Les configurations sont filtrées par volume minimum puis comparées sur leur CLV, ROI et stabilité.',
  'Une partie de la période est conservée pour valider les configurations hors-échantillon.',
].join(' ');

/* Les dimensions du moteur, dans l'ordre des colonnes, avec leur en-tête de
 * tableau et leur nom de colonne CSV. Les libellés des CRITÈRES eux-mêmes
 * viennent de la réponse (`criteria[].label`, `criteria[].display`). */
const SF_DIMS = [['bookmaker', 'Bookmaker', 'bookmaker'], ['market', 'Marché', 'marche'],
  ['outcome', 'Pari', 'pari'], ['ev', 'EV', 'ev'], ['odds', 'Cote', 'cote'],
  ['delay', 'Délai', 'delai']];
/* Un ORDRE de tri pour la robustesse, pas une mesure : rien ne s'affiche. */
const SF_RANG_ROBUSTESSE = { strong: 3, medium: 2, weak: 1 };

let SF = null;            // la dernière réponse de /api/strategies affichée
let JETON_SF = null;      // la dernière recherche lancée — une plus ancienne est ignorée
let SF_PRET = false;      // formulaire rempli depuis /api/filters
let SF_TRI = { cle: 'rank', sens: 1 };
let SF_MODALE = null;     // l'id de la modale ouverte, ou null
let SF_OUVREUR = null;    // l'élément qui l'a ouverte, pour lui rendre le focus
let SF_DETAIL = null;     // la configuration affichée dans le détail

/* Un écart en POINTS : « −1,2 pts » entre deux CLV n'est pas « −1,2 % ». */
const pts = (v) => (v === null || v === undefined ? '—'
  : (v > 0 ? '+' : '') + nb(v, 1) + FINE + 'pts');
/* Une part rendue entre 0 et 1 par le serveur. Le format « pourcentage » de
 * la locale fait la conversion d'AFFICHAGE ; la valeur n'est pas touchée. */
const partPct = (v) => (v === null || v === undefined ? '—'
  : v.toLocaleString('fr-FR', { style: 'percent', maximumFractionDigits: 0 })
    .replace(/\s/g, FINE));

/** Le libellé affiché d'un critère de la configuration, ou null s'il est absent. */
function sfCritere(s, dim) {
  const c = (s.criteria || []).find((x) => x.dimension === dim);
  return c ? (c.display || c.value || null) : null;
}
/* Le niveau de robustesse, borné aux trois valeurs du contrat : il devient
 * une classe CSS, rien d'autre n'y entre. */
const sfNiveau = (r) => (r && ['strong', 'medium', 'weak'].includes(r.level) ? r.level : 'inconnu');

function sfRobustesse(r) {
  const s = el('span', 'sf-robustesse ' + sfNiveau(r));
  s.appendChild(el('span', 'sf-point'));
  s.appendChild(el('span', null, (r && r.label) || '—'));
  return s;
}

/* Le badge de volume n'apparaît qu'aux extrêmes (limité, large) : « standard »
 * n'apprend rien sur une carte. Comme partout, il dit une TAILLE. */
function sfEchantillon(sample) {
  if (!sample || !['limited', 'large'].includes(sample.level)) return null;
  const b = el('span', 'ech ' + (sample.level === 'limited' ? 'petit' : 'tres_bon'), sample.label || '');
  b.title = 'Indication de volume, pas de significativité statistique.';
  return b;
}

/* ── Formulaire ─────────────────────────────────────────────────── */

/* Rempli une seule fois, à la première ouverture après /api/filters : les
 * libellés viennent du serveur, la période des filtres globaux ; ensuite le
 * formulaire vit sa vie et garde ce que l'utilisateur y a réglé. */
function sfPreparer() {
  if (!REFS || SF_PRET) return;
  SF_PRET = true;
  const s = $('sf-sport');
  s.innerHTML = '';
  const tous = el('option', null, 'Tous les sports');
  tous.value = '';
  s.appendChild(tous);
  const sports = REFS.sports || [];
  sports.forEach((sp) => {
    const o = el('option', null, nomSport(sp));
    o.value = sp;
    s.appendChild(o);
  });
  // Un sport réel par défaut — le football s'il est là, sinon le premier.
  s.value = sports.includes('soccer') ? 'soccer' : (sports[0] || '');

  const pop = $('sf-population');
  pop.innerHTML = '';
  (REFS.populations || []).forEach((p) => {
    const o = el('option', null, p.libelle || p.value);
    o.value = p.value;
    if (p.explication) o.title = p.explication;
    pop.appendChild(o);
  });
  if ((REFS.populations || []).some((p) => p.value === 'settled')) pop.value = 'settled';

  ['sf-date-from', 'sf-date-to'].forEach((id) => {
    if (REFS.date_min) $(id).min = REFS.date_min;
    if (REFS.date_max) $(id).max = REFS.date_max;
  });
  $('sf-date-from').value = $('f-date-from').value || REFS.date_min || '';
  $('sf-date-to').value = $('f-date-to').value || REFS.date_max || '';
  sfPoserMin($('sf-min').value, false);
}

/* Préréglage cliqué : il ÉCRIT le champ. Saisie libre : le préréglage égal à
 * la valeur tapée reste allumé, les autres s'éteignent. */
function sfPoserMin(v, ecrire) {
  const val = String(v === null || v === undefined ? '' : v).trim();
  if (ecrire) $('sf-min').value = val;
  segActif('sf-min-presets', 'min', val);
  document.querySelectorAll('#sf-min-presets button')
    .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.min === val)));
  document.querySelectorAll('#sf-rapides .puce')
    .forEach((b) => b.classList.toggle('actif', b.dataset.min === val));
}

function sfChoisirObjectif(v) {
  segActif('sf-objectif', 'objectif', v);
  document.querySelectorAll('#sf-objectif button')
    .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.objectif === v)));
}

function sfObjectif() {
  const b = document.querySelector('#sf-objectif button.actif');
  return b ? b.dataset.objectif : 'balanced';
}

/* Ce qui part au serveur. Un champ vide n'est pas envoyé : le serveur
 * applique alors SON défaut, et l'en-tête des résultats le relit. */
function sfParametres() {
  const p = new URLSearchParams();
  const sport = $('sf-sport').value;
  if (sport) p.set('sport', sport);
  [['date_from', 'sf-date-from'], ['date_to', 'sf-date-to'],
   ['population', 'sf-population'], ['min_n', 'sf-min']].forEach(([nom, id]) => {
    const v = $(id).value.trim();
    if (v) p.set(nom, v);
  });
  p.set('objective', sfObjectif());
  return p;
}

/* ── Recherche ──────────────────────────────────────────────────── */

async function sfLancer() {
  if (!REFS) { toast('Les filtres ne sont pas encore chargés — réessayez dans un instant.'); return; }
  const b = $('sf-lancer');
  if (b.disabled) return;
  const jeton = {};
  JETON_SF = jeton;
  b.disabled = true;
  b.classList.add('charge');
  b.setAttribute('aria-busy', 'true');
  sfAfficherEtat('chargement');
  try {
    const d = await appel(API_STRATEGIES, sfParametres());
    // Une réponse plus ancienne que la dernière recherche ne doit JAMAIS
    // écraser la plus récente : l'écran décrirait d'autres paramètres.
    if (JETON_SF !== jeton) return;
    SF = d;
    SF_TRI = { cle: 'rank', sens: 1 };
    sfRendre(d);
  } catch (e) {
    if (JETON_SF !== jeton) return;
    SF = null;
    sfAfficherEtat('erreur', e);
  } finally {
    if (JETON_SF === jeton) {
      b.disabled = false;
      b.classList.remove('charge');
      b.removeAttribute('aria-busy');
    }
  }
}

/* Chargement ou erreur : un seul emplacement, et les résultats précédents
 * disparaissent — ils décriraient une autre recherche que celle en cours. */
function sfAfficherEtat(etat, erreur) {
  const h = $('sf-etat');
  h.innerHTML = '';
  h.hidden = !etat;
  if (!etat) return;
  $('sf-resultats').hidden = true;
  if (etat === 'chargement') {
    const c = el('div', 'carte sf-chargement');
    c.setAttribute('role', 'status');
    const roue = el('span', 'sf-roue');
    roue.setAttribute('aria-hidden', 'true');
    c.appendChild(roue);
    c.appendChild(el('span', null, 'Analyse des configurations…'));
    h.appendChild(c);
  } else {
    const refus = erreur && (erreur.statut === 400 || erreur.statut === 422);
    h.appendChild(blocAvert((refus ? 'Paramètres refusés : ' : 'Recherche impossible : ')
      + ((erreur && erreur.message) || 'erreur inconnue'), true));
  }
}

function sfRendre(d) {
  sfAfficherEtat(null);
  $('sf-resultats').hidden = false;
  const liste = d.strategies || [];
  const vide = !liste.length;
  sfSynthese(d);
  $('sf-vide').hidden = !vide;
  $('sf-bloc-top').hidden = vide;
  $('sf-bloc-table').hidden = vide;
  $('sf-csv').disabled = vide;
  // Moins de cinq : on montre celles-là, et on le DIT — jamais de carte
  // inventée pour remplir la rangée.
  // Les cartes montrent des pistes DISTINCTES : une variante d'une carte
  // mieux classée (`variant_of`, décidé par le serveur) reste au tableau.
  const cartes = liste.filter((s) => !s.variant_of).slice(0, 5);
  const variantes = liste.length - cartes.length;
  const n = $('sf-nombre');
  n.hidden = vide || cartes.length >= 5;
  n.textContent = (cartes.length > 1
    ? `${ent(cartes.length)} configurations distinctes répondent aux critères.`
    : `${ent(cartes.length)} configuration distincte répond aux critères.`)
    + (variantes > 0 ? ` Les autres lignes du tableau en sont des variantes ou des configurations moins bien classées.` : '');
  sfCartes(cartes);
  sfTableau();
  sfComparaisons(d);
  sfEntetePdf(d);
}

/* ── En-tête des résultats ──────────────────────────────────────── */

const sfNomSport = (p) => p.sport_label || (p.sport ? nomSport(p.sport) : 'Tous les sports');

function sfPeriodeTexte(p) {
  if (p.date_from && p.date_to) return `du ${dateLongue(p.date_from, true)} au ${dateLongue(p.date_to, true)}`;
  if (p.date_from) return `depuis le ${dateLongue(p.date_from, true)}`;
  if (p.date_to) return `jusqu'au ${dateLongue(p.date_to, true)}`;
  return 'sur toute la période disponible';
}

/* ⚠️ L'EN-TÊTE RELIT LES PARAMÈTRES DU SERVEUR (`params`), pas le
 * formulaire : c'est ce qu'il a réellement appliqué — défauts compris. */
function sfSynthese(d) {
  const p = d.params || {};
  $('sf-titre').textContent = `Top configurations — ${sfNomSport(p)}`;
  // Le libellé de population est un NOM (« Résultat connu et réglable ») :
  // cité tel quel, pas fondu dans la phrase en minuscules.
  const pop = p.population_label || nomPopulation(p.population);
  $('sf-sous').textContent = `Population « ${pop} », ${sfPeriodeTexte(p)}`
    + ` · minimum ${ent(p.min_n)} paris réglés · mode ${p.objective_label || p.objective || '—'}`;

  const c = d.counts || {};
  const h = $('sf-compteurs');
  h.innerHTML = '';
  [[c.tested, 'configurations analysées'], [c.eligible, 'ont atteint le minimum de volume'],
   [c.validated, 'retenues pour validation'], [c.shown, 'affichées']].forEach(([v, lib], i) => {
    const k = el('div', 'sf-compteur');
    k.appendChild(el('b', null, ent(v)));
    k.appendChild(el('span', null, lib));
    if (i === 1 && c.redundant) k.title = `${ent(c.redundant)} configurations redondantes écartées`;
    h.appendChild(k);
  });

  const sp = d.split || {};
  const ligne = $('sf-split');
  ligne.innerHTML = '';
  const morceau = (titre, texte) => {
    const m = el('span', 'sf-split-part');
    if (titre) m.appendChild(el('b', null, titre + ' : '));
    m.appendChild(document.createTextNode(texte));
    if (ligne.childNodes.length) ligne.appendChild(el('span', 'sep', '·'));
    ligne.appendChild(m);
  };
  if (sp.cutoff) {
    morceau('Entraînement', periodeTexte((sp.train || {}).from, (sp.train || {}).to));
    morceau('Validation', periodeTexte((sp.validation || {}).from, (sp.validation || {}).to));
  } else {
    morceau('Validation', 'aucune — trop peu de données pour réserver une période');
  }
  if (p.stake !== null && p.stake !== undefined) morceau(null, `mise notionnelle ${num(p.stake)} € par pari`);
  const lot = d.lot || {};
  if (lot.opportunities !== null && lot.opportunities !== undefined) {
    morceau('Lot', `${ent(lot.opportunities)} opportunités, ${ent(lot.settled)} paris réglés`);
  }
  if (d.cached) morceau(null, 'résultat servi depuis le cache du serveur');

  const av = $('sf-avert');
  av.innerHTML = '';
  (d.warnings || []).forEach((m) => av.appendChild(blocAvert(m, false)));
}

/* ── Cartes du top 5 ────────────────────────────────────────────── */

/* La validation d'une configuration, en une ligne : CLV et ROI de la
 * période réservée — ou l'aveu qu'elle n'en dit rien. */
function sfValidation(v, court) {
  const l = el('span', 'sf-valid-val');
  if (!v || !v.sufficient) {
    l.appendChild(el('span', 'sf-valid-insuf', court ? 'insuffisante' : 'validation insuffisante'));
    return l;
  }
  l.appendChild(document.createTextNode('CLV '));
  l.appendChild(el('b', signe(v.clv), pct(v.clv, 1)));
  l.appendChild(document.createTextNode(' · ROI '));
  l.appendChild(el('b', signe(v.roi), pct(v.roi, 1)));
  return l;
}

function sfCartes(liste) {
  const h = $('sf-cartes');
  h.innerHTML = '';
  liste.forEach((s) => h.appendChild(sfCarte(s)));
}

/* ⚠️ LA CARTE RESTE NEUTRE. Seuls les CHIFFRES portent le signe (vert,
 * rouge) : une carte entière en vert se lirait comme une recommandation. */
function sfCarte(s) {
  const sm = s.summary || {};
  const c = el('article', 'carte sf-carte');
  c.tabIndex = 0;
  c.setAttribute('role', 'button');
  c.setAttribute('aria-label', `Configuration n° ${s.rank} — ${s.title || ''} : ouvrir le détail`);

  const tete = el('div', 'sf-carte-tete');
  tete.appendChild(el('span', 'sf-rang', `#${s.rank}`));
  const ech = sfEchantillon(s.sample);
  if (ech) tete.appendChild(ech);
  c.appendChild(tete);
  c.appendChild(el('h3', 'sf-carte-titre', s.title || '—'));

  // Le titre porte le bookmaker et le marché ; les autres critères, s'ils
  // existent, sur une ligne chacun. Un critère absent n'est pas écrit
  // « Toutes » : il n'est simplement pas restreint. Ordre de lecture fixe
  // d'une carte à l'autre : EV, Cote, Pari, Délai.
  const ordre = ['ev', 'odds', 'outcome', 'delay'];
  const crit = (s.criteria || []).filter((x) => ordre.includes(x.dimension))
    .sort((a, b) => ordre.indexOf(a.dimension) - ordre.indexOf(b.dimension));
  if (crit.length) {
    const dl = el('dl', 'sf-crit');
    crit.forEach((x) => {
      dl.appendChild(el('dt', null, x.label || x.dimension));
      dl.appendChild(el('dd', null, x.display || x.value || '—'));
    });
    c.appendChild(dl);
  }

  const pied = el('div', 'sf-carte-pied');
  const m = el('div', 'sf-mesures');
  [['CLV', pct(sm.clv, 1), signe(sm.clv), 'CLV moyenne face à la clôture'],
   ['ROI', pct(sm.roi, 1), signe(sm.roi), 'Sur les paris réglés, mise notionnelle'],
   ['Paris', ent(sm.settled), '', 'Paris réglés'],
   ['P&L', eur(sm.pnl, 0), signe(sm.pnl), 'P&L notionnel']].forEach(([k, val, cls, titre]) => {
    const b = el('div', 'sf-mesure');
    b.title = titre;
    b.appendChild(el('span', null, k));
    b.appendChild(el('b', cls, val));
    m.appendChild(b);
  });
  pied.appendChild(m);

  const valid = el('div', 'sf-carte-ligne');
  valid.appendChild(el('span', 'sf-carte-lib', 'Validation'));
  valid.appendChild(sfValidation(s.validation, false));
  pied.appendChild(valid);
  const rob = el('div', 'sf-carte-ligne');
  rob.appendChild(el('span', 'sf-carte-lib', 'Robustesse'));
  rob.appendChild(sfRobustesse(s.robustness));
  pied.appendChild(rob);
  c.appendChild(pied);

  const ouvrir = () => sfOuvrirDetail(s.id);
  c.addEventListener('click', ouvrir);
  c.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ouvrir(); }
  });
  return c;
}

/* ── Tableau détaillé ───────────────────────────────────────────── */

/* `val` lit une valeur RENDUE par le serveur, pour le tri seulement. */
const SF_COLONNES = [
  { cle: 'rank', titre: 'Rang', num: true, sens: 1, val: (s) => s.rank },
].concat(SF_DIMS.map(([dim, titre]) => ({ cle: dim, titre, sens: 1, val: (s) => sfCritere(s, dim) })))
  .concat([
    { cle: 'settled', titre: 'Paris', num: true, sens: -1, val: (s) => (s.summary || {}).settled },
    { cle: 'clv', titre: 'CLV', num: true, sens: -1, val: (s) => (s.summary || {}).clv },
    { cle: 'roi', titre: 'ROI', num: true, sens: -1, val: (s) => (s.summary || {}).roi },
    { cle: 'validation', titre: 'Validation', num: true, sens: -1,
      aide: 'CLV puis ROI de la période de validation — tri sur la CLV',
      val: (s) => (s.validation && s.validation.sufficient ? s.validation.clv : null) },
    { cle: 'robustness', titre: 'Robustesse', sens: -1,
      val: (s) => SF_RANG_ROBUSTESSE[sfNiveau(s.robustness)] || null },
  ]);

function sfTableau() {
  const t = $('sf-table');
  t.innerHTML = '';
  if (!SF) return;
  const col = SF_COLONNES.find((c) => c.cle === SF_TRI.cle) || SF_COLONNES[0];
  const lignes = (SF.strategies || []).slice().sort((a, b) => {
    const va = col.val(a), vb = col.val(b);
    const absA = va === null || va === undefined, absB = vb === null || vb === undefined;
    // Une valeur absente reste EN BAS dans les deux sens : un « — » en tête
    // de colonne se lirait comme le meilleur ou le pire, il n'est ni l'un
    // ni l'autre.
    if (absA || absB) return absA === absB ? a.rank - b.rank : absA ? 1 : -1;
    const r = typeof va === 'string' ? va.localeCompare(vb, 'fr', { numeric: true }) : va - vb;
    return r ? r * SF_TRI.sens : a.rank - b.rank;
  });

  const thead = el('thead'), tr = el('tr');
  SF_COLONNES.forEach((c) => {
    const actif = c.cle === SF_TRI.cle;
    const th = el('th', [c.num ? 'num' : '', 'triable', actif ? 'actif' : ''].filter(Boolean).join(' '),
      c.titre + (actif ? (SF_TRI.sens < 0 ? ' ▾' : ' ▴') : ''));
    th.scope = 'col';
    th.tabIndex = 0;
    th.dataset.cle = c.cle;
    if (c.aide) th.title = c.aide;
    th.setAttribute('aria-sort', actif ? (SF_TRI.sens < 0 ? 'descending' : 'ascending') : 'none');
    const trier = () => {
      if (SF_TRI.cle === c.cle) SF_TRI.sens = -SF_TRI.sens;
      else SF_TRI = { cle: c.cle, sens: c.sens };
      sfTableau();
      const th2 = $('sf-table').querySelector(`th[data-cle="${c.cle}"]`);
      if (th2) th2.focus();
    };
    th.addEventListener('click', trier);
    th.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); trier(); }
    });
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  t.appendChild(thead);

  const tb = el('tbody');
  lignes.forEach((s) => {
    const sm = s.summary || {};
    const r = el('tr', 'sf-ligne');
    const td0 = el('td', 'num');
    const bt = el('button', 'sf-rang-btn', `#${s.rank}`);
    bt.type = 'button';
    bt.setAttribute('aria-label', `Ouvrir le détail de la configuration n° ${s.rank}, ${s.title || ''}`);
    td0.appendChild(bt);
    // Variante d'une carte mieux classée (plus de 60 % de paris communs) :
    // dit par le serveur, jamais recalculé ici.
    const parent = s.variant_of && (SF.strategies || []).find((x) => x.id === s.variant_of);
    if (parent) {
      const v = el('span', 'sub', `variante de #${parent.rank}`);
      v.title = 'La majorité de ses paris sont déjà dans cette configuration mieux classée.';
      td0.appendChild(v);
    }
    r.appendChild(td0);
    SF_DIMS.forEach(([dim]) => {
      const x = sfCritere(s, dim);
      const td = el('td', x ? 'sf-crit-cell' : 'sf-absent', x || '—');
      if (!x) td.title = 'Critère non restreint';
      r.appendChild(td);
    });
    r.appendChild(el('td', 'num', ent(sm.settled)));
    r.appendChild(el('td', 'num ' + signe(sm.clv), pct(sm.clv, 1)));
    r.appendChild(el('td', 'num ' + signe(sm.roi), pct(sm.roi, 1)));
    r.appendChild(sfCelluleValidation(s.validation));
    const tdR = el('td');
    tdR.appendChild(sfRobustesse(s.robustness));
    r.appendChild(tdR);
    // Le bouton du rang porte le clavier ; son clic remonte à la ligne.
    r.addEventListener('click', () => sfOuvrirDetail(s.id));
    tb.appendChild(r);
  });
  t.appendChild(tb);
}

/* Deux lignes, CLV puis ROI de la validation ; seuls les nombres sont colorés. */
function sfCelluleValidation(v, brut) {
  const td = el('td', 'num sf-valid-cell');
  const clv = brut ? brut.clv : v && v.clv;
  const roi = brut ? brut.roi : v && v.roi;
  if (!brut && (!v || !v.sufficient)) {
    td.appendChild(el('span', 'sf-valid-insuf', 'insuffisante'));
    return td;
  }
  [['CLV ', clv], ['ROI ', roi]].forEach(([k, x], i) => {
    const l = el('span', i ? 'sf-v sf-v2' : 'sf-v');
    l.appendChild(document.createTextNode(k));
    l.appendChild(el('b', signe(x), pct(x, 1)));
    td.appendChild(l);
  });
  return td;
}

/* ── Comparaisons par bookmaker et par marché ───────────────────── */

function sfComparaisons(d) {
  const book = d.by_bookmaker || [], marche = d.by_market || [];
  $('sf-comparaisons').hidden = !book.length && !marche.length;
  $('sf-comparaisons').classList.toggle('sf-une', !book.length || !marche.length);
  $('sf-bloc-par-book').hidden = !book.length;
  $('sf-bloc-par-marche').hidden = !marche.length;
  sfTableComparaison($('sf-par-book'), book, 'Bookmaker');
  sfTableComparaison($('sf-par-marche'), marche, 'Marché');
}

function sfTableComparaison(t, lignes, nom) {
  t.innerHTML = '';
  const cols = [[nom, ''], ['Configuration', ''], ['Paris', 'num'], ['CLV', 'num'], ['ROI', 'num'],
    ['Validation', 'num'], ['Robustesse', '']];
  const thead = el('thead'), tr = el('tr');
  cols.forEach(([titre, cls]) => {
    const th = el('th', cls, titre);
    th.scope = 'col';
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  t.appendChild(thead);
  const ids = new Set(((SF && SF.strategies) || []).map((s) => s.id));
  const tb = el('tbody');
  lignes.forEach((x) => {
    // Le détail n'existe que pour une configuration du classement final.
    const dispo = !!x.strategy_id && ids.has(x.strategy_id);
    const r = el('tr', dispo ? 'sf-ligne' : 'sf-ligne-hors');
    r.appendChild(el('td', 'nom', x.display || x.key || '—'));
    const tdC = el('td', 'sf-comp-config');
    if (dispo) {
      const bt = el('button', 'sf-rang-btn', x.rank !== null && x.rank !== undefined ? `#${x.rank}` : 'Détail');
      bt.type = 'button';
      bt.setAttribute('aria-label', `Ouvrir le détail de ${x.title || 'cette configuration'}`);
      tdC.appendChild(bt);
    } else {
      tdC.appendChild(el('span', 'sf-hors', 'hors classement'));
      r.title = 'Hors du classement final : détail non disponible.';
    }
    tdC.appendChild(el('span', 'sf-comp-titre', x.title || '—'));
    r.appendChild(tdC);
    r.appendChild(el('td', 'num', ent(x.settled)));
    r.appendChild(el('td', 'num ' + signe(x.clv), pct(x.clv, 1)));
    r.appendChild(el('td', 'num ' + signe(x.roi), pct(x.roi, 1)));
    r.appendChild(sfCelluleValidation(null, { clv: x.validation_clv, roi: x.validation_roi }));
    const tdR = el('td');
    tdR.appendChild(sfRobustesse(x.robustness));
    r.appendChild(tdR);
    if (dispo) r.addEventListener('click', () => sfOuvrirDetail(x.strategy_id));
    tb.appendChild(r);
  });
  t.appendChild(tb);
}

/* ── Modales ────────────────────────────────────────────────────── */

/* Même contrat que le tiroir : le reste de l'application sort du parcours
 * clavier et des lecteurs d'écran, Échap ferme, le focus revient à qui a
 * ouvert. */
function sfOuvrirModale(id) {
  if (SF_MODALE) sfFermerModale(false);
  const actif = document.activeElement;
  SF_OUVREUR = actif && actif !== document.body && $('app').contains(actif) ? actif : null;
  SF_MODALE = id;
  cacher();
  $('sf-voile').hidden = false;
  $(id).hidden = false;
  $(id).scrollTop = 0;
  document.body.classList.add('fige');
  $('app').inert = true;
  const fermer = $(id).querySelector('.sf-modale-fermer');
  setTimeout(() => { if (SF_MODALE === id && fermer) fermer.focus(); }, 30);
}

function sfFermerModale(rendreFocus) {
  if (!SF_MODALE) return;
  $(SF_MODALE).hidden = true;
  $('sf-voile').hidden = true;
  SF_MODALE = null;
  SF_DETAIL = null;
  cacher();
  document.body.classList.remove('fige');
  $('app').inert = false;
  const cible = SF_OUVREUR;
  SF_OUVREUR = null;
  if (rendreFocus !== false && cible && document.contains(cible) && !cible.closest('[hidden]')) {
    cible.focus();
  }
}

/* Le focus tourne dans la modale : Tab depuis le dernier élément revient au
 * premier, et inversement. */
function sfPiegeFocus(e) {
  if (e.key !== 'Tab' || !SF_MODALE) return;
  const f = [...$(SF_MODALE).querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter((n) => !n.disabled && n.getClientRects().length);
  if (!f.length) return;
  const premier = f[0], dernier = f[f.length - 1];
  if (e.shiftKey && document.activeElement === premier) { e.preventDefault(); dernier.focus(); }
  else if (!e.shiftKey && document.activeElement === dernier) { e.preventDefault(); premier.focus(); }
}

function sfOuvrirMethode() {
  $('sf-methode-texte').textContent = (SF && SF.method) || SF_METHODE;
  sfOuvrirModale('sf-methode');
}

/* ── Détail d'une configuration ─────────────────────────────────── */

function sfSection(titre, contenu, cls) {
  const s = el('section', 'sf-section' + (cls ? ' ' + cls : ''));
  s.appendChild(el('h3', null, titre));
  if (contenu) s.appendChild(contenu);
  return s;
}

/* Une liste « libellé → valeur ». `sous` : lignes d'appoint sous la valeur,
 * `{ t, fragile }` pour une mise en garde. */
function sfDl(lignes) {
  const dl = el('dl', 'sf-dl');
  lignes.forEach(([k, v, cls, sous]) => {
    dl.appendChild(el('dt', null, k));
    const dd = el('dd');
    dd.appendChild(el('span', 'sf-dd-val' + (cls ? ' ' + cls : ''), v));
    (sous || []).filter(Boolean).forEach((x) => {
      const o = typeof x === 'string' ? { t: x } : x;
      dd.appendChild(el('span', 'sf-dd-sous' + (o.fragile ? ' fragile' : ''), o.t));
    });
    dl.appendChild(dd);
  });
  return dl;
}

/* « IC 90 % : [+9,8 % ; +13,7 %] » — seulement si le serveur a rendu une borne. */
function sfIntervalle(ci, bornesIc) {
  if (!Array.isArray(bornesIc) || !bornesIc.some((x) => x !== null && x !== undefined)) return null;
  return `IC ${num(ci.level)} % : [${pct(bornesIc[0], 1)} ; ${pct(bornesIc[1], 1)}]`;
}

function sfOuvrirDetail(id) {
  if (!SF) return;
  const s = (SF.strategies || []).find((x) => x.id === id);
  if (!s) return;
  sfRemplirDetail(s);
  sfOuvrirModale('sf-detail');
  SF_DETAIL = s;
  // Dessinées une fois la modale visible : une courbe prend la largeur
  // réelle de son conteneur.
  sfGraphes(s);
}

function sfGraphes(s) {
  const a = $('sf-g-clv'), b = $('sf-g-pnl');
  if (!a || !b) return;
  courbe(a, s.series || [], 'clv', 'CLV dans le temps');
  courbe(b, s.series || [], 'pnl_cumul', 'P&L cumulé', (v) => eur(v, 0));
}

function sfRemplirDetail(s) {
  const p = (SF && SF.params) || {};
  const sp = (SF && SF.split) || {};
  const sm = s.summary || {}, tr = s.train || {}, va = s.validation || {};
  const ci = s.ci || {}, delta = s.delta || {}, st = s.stability || {};

  $('sf-detail-rang').textContent = `#${s.rank}`;
  $('sf-detail-titre').textContent = s.title || 'Configuration';
  const sous = $('sf-detail-sous');
  sous.innerHTML = '';
  sous.appendChild(el('span', null, sfNomSport(p)));
  sous.appendChild(el('span', 'sep', '·'));
  const rob = el('span');
  rob.appendChild(document.createTextNode('Robustesse '));
  rob.appendChild(sfRobustesse(s.robustness));
  sous.appendChild(rob);
  if (s.sample && s.sample.label) {
    sous.appendChild(el('span', 'sep', '·'));
    sous.appendChild(el('span', null, s.sample.label));
  }

  const corps = $('sf-detail-corps');
  corps.innerHTML = '';
  corps.appendChild(el('p', 'sf-prudence', 'Ce que cette configuration a présenté sur la période '
    + 'analysée. Des résultats historiques ne préjugent pas des résultats futurs.'));

  const g1 = el('div', 'sf-detail-grille sf-grille-3');
  g1.appendChild(sfSection('Configuration', sfDl(
    [['Sport', sfNomSport(p)]].concat((s.criteria || [])
      .map((c) => [c.label || c.dimension, c.display || c.value || '—'])))));
  g1.appendChild(sfSection('Échantillon', sfDl([
    ['Paris réglés', ent(sm.settled)],
    ['Opportunités', ent(sm.opportunities)],
    ['Mise totale', eur(sm.stake_total, 0).replace('+', '')],
    ['P&L', eur(sm.pnl, 0), signe(sm.pnl)],
    ['Gagnés · perdus · annulés', `${ent(sm.won)} · ${ent(sm.lost)} · ${ent(sm.void)}`],
    ['EV moyenne', pct(sm.ev_mean, 1), '', ['à la détection']],
    ['Cote moyenne', cote(sm.odds_mean)],
  ])));
  // ⚠️ La CLV ne sort jamais sans sa couverture, ici non plus.
  const couvFaible = sm.clv_coverage !== null && sm.clv_coverage !== undefined
    && sm.clv_coverage < SEUIL_COUVERTURE;
  g1.appendChild(sfSection('Performance', sfDl([
    ['CLV moyenne', pct(sm.clv, 2), signe(sm.clv), [
      { t: `sur ${ent(sm.clv_n)} paris mesurés · couverture ${pctNu(sm.clv_coverage, 0)}`, fragile: couvFaible },
      sfIntervalle(ci, ci.clv)]],
    ['CLV médiane', pct(sm.clv_median, 1), signe(sm.clv_median)],
    ['CLV positives', pctNu(sm.clv_positive_rate, 0), '', ['des paris mesurés ont battu la clôture']],
    ['ROI', pct(sm.roi, 2), signe(sm.roi), [`sur ${ent(sm.settled)} paris réglés`, sfIntervalle(ci, ci.roi)]],
  ])));
  corps.appendChild(g1);

  const g2 = el('div', 'sf-detail-grille sf-grille-2');
  const tv = el('div', 'sf-tv');
  [['Entraînement', tr, sp.train], ['Validation', va, sp.validation]].forEach(([nom, m, per]) => {
    const b = el('div', 'sf-tv-bloc');
    b.appendChild(el('h4', null, nom));
    if (per && (per.from || per.to)) b.appendChild(el('p', 'sf-tv-per', periodeTexte(per.from, per.to)));
    b.appendChild(sfDl([
      ['CLV', pct(m.clv, 1), signe(m.clv), [`sur ${ent(m.clv_n)} mesurés`]],
      ['ROI', pct(m.roi, 1), signe(m.roi)],
      ['Paris', ent(m.settled), '', ['réglés']],
    ]));
    if (m === va && !va.sufficient) {
      b.appendChild(el('p', 'sf-valid-insuf', 'Validation insuffisante : trop peu de paris réglés '
        + 'sur cette période pour la lire.'));
    }
    tv.appendChild(b);
  });
  const secTv = sfSection('Entraînement / Validation', tv);
  secTv.appendChild(sfDl([
    ['Variation CLV', pts(delta.clv), '', ['validation − entraînement']],
    ['Variation ROI', pts(delta.roi)],
  ]));
  g2.appendChild(secTv);

  const secSt = sfSection('Stabilité');
  const blocs = st.blocks || [];
  if (blocs.length) {
    const enrob = el('div', 'enrob');
    const t = el('table', 'tableau tableau-dense sf-blocs');
    const th = el('thead'), trh = el('tr');
    [['Période', ''], ['Paris', 'num'], ['CLV', 'num'], ['ROI', 'num']].forEach(([x, cls]) => {
      const c = el('th', cls, x);
      c.scope = 'col';
      trh.appendChild(c);
    });
    th.appendChild(trh);
    t.appendChild(th);
    const tb = el('tbody');
    blocs.forEach((b) => {
      const r = el('tr');
      r.appendChild(el('td', null, periodeTexte(b.from, b.to)));
      r.appendChild(el('td', 'num', ent(b.settled)));
      r.appendChild(el('td', 'num ' + signe(b.clv), pct(b.clv, 1)));
      r.appendChild(el('td', 'num ' + signe(b.roi), pct(b.roi, 1)));
      tb.appendChild(r);
    });
    t.appendChild(tb);
    enrob.appendChild(t);
    secSt.appendChild(enrob);
  } else {
    secSt.appendChild(el('p', 'aide', 'Aucune sous-période mesurable.'));
  }
  // Les parts viennent du serveur (0 à 1) : rien n'est recompté ici.
  secSt.appendChild(el('p', 'sf-part',
    `${partPct(st.clv_positive_share)} des sous-périodes à CLV positive · `
    + `${partPct(st.roi_positive_share)} à ROI positif — sur ${ent(st.measured_blocks)} `
    + (st.measured_blocks > 1 ? 'sous-périodes mesurées' : 'sous-période mesurée')));
  g2.appendChild(secSt);
  corps.appendChild(g2);

  const g3 = el('div', 'sf-detail-grille sf-grille-2');
  [['CLV dans le temps', 'sf-g-clv', 'CLV moyenne par période, en %.'],
   ['P&L cumulé', 'sf-g-pnl', `En €, mise notionnelle de ${num(p.stake)} € par pari.`]]
    .forEach(([titre, id, legende]) => {
      const sec = sfSection(titre, null, 'sf-section-graphe');
      sec.appendChild(el('p', 'carte-sous', legende));
      const g = el('div', 'graphe');
      g.id = id;
      sec.appendChild(g);
      g3.appendChild(sec);
    });
  corps.appendChild(g3);

  const g4 = el('div', 'sf-detail-grille sf-grille-2');
  const pourquoi = sfSection('Pourquoi cette configuration ?');
  if ((s.why || []).length) {
    const ul = el('ul', 'sf-liste');
    s.why.forEach((w) => ul.appendChild(el('li', null, w)));
    pourquoi.appendChild(ul);
  } else {
    pourquoi.appendChild(el('p', 'aide', 'Aucune explication rendue par le serveur.'));
  }
  g4.appendChild(pourquoi);
  const secRob = sfSection('Robustesse');
  const tete = el('p', 'sf-rob-tete');
  tete.appendChild(sfRobustesse(s.robustness));
  if (s.sample && s.sample.label) tete.appendChild(el('span', 'aide', s.sample.label));
  secRob.appendChild(tete);
  const raisons = (s.robustness && s.robustness.reasons) || [];
  if (raisons.length) {
    const ul = el('ul', 'sf-liste');
    raisons.forEach((w) => ul.appendChild(el('li', null, w)));
    secRob.appendChild(ul);
  }
  g4.appendChild(secRob);
  corps.appendChild(g4);
}

/* ⚠️ « OUVRIR DANS L'ANALYTICS » ÉCRIT DANS LE TIROIR, la seule source de
 * vérité des filtres — exactement comme une analyse sauvegardée qu'on
 * recharge. Les valeurs sont celles que le serveur a rendues
 * (`analytics_filters`), jamais reconstruites depuis les libellés. */
function sfOuvrirAnalytics(s) {
  const f = s.analytics_filters || {};
  const p = (SF && SF.params) || {};
  $('reinit').click();
  if (f.date_from) $('f-date-from').value = f.date_from;
  if (f.date_to) $('f-date-to').value = f.date_to;
  const manques = [];
  const poser = (groupe, valeurs) => {
    const v = valeurs || [];
    cocher(groupe, v);
    if (coches(groupe).length !== new Set(v).size) manques.push(groupe);
  };
  poser('f-sports', f.sports);
  poser('f-books', f.bookmakers);
  poser('f-markets', f.markets);
  poser('f-outcomes', f.outcomes);
  if ((f.ev_bands || []).length) {
    appliquerModeEv('tranches', false);
    poser('f-ev-bands', f.ev_bands);
  }
  if ((f.odds_bands || []).length) {
    appliquerModeCote('tranches', false);
    poser('f-odds-bands', f.odds_bands);
  }
  // Le serveur parle en HEURES : l'unité du tiroir est remise sur « heures ».
  $('f-delay-unite').value = 'h';
  $('f-delay-min').value = f.delay_min === null || f.delay_min === undefined ? '' : String(f.delay_min);
  $('f-delay-max').value = f.delay_max === null || f.delay_max === undefined ? '' : String(f.delay_max);
  majAideDelai();
  if (f.population && [...$('f-population').options].some((o) => o.value === f.population)) {
    $('f-population').value = f.population;
  }
  majAidePopulation();
  // La même mise notionnelle que la recherche : sans elle, le P&L affiché
  // par l'Analytics ne se comparerait pas à celui de la configuration.
  if (p.stake !== null && p.stake !== undefined) $('f-stake').value = String(p.stake);
  signalerChangement();
  sfFermerModale(false);
  allerA('vue-ensemble');
  analyser();
  // ⚠️ ÉCART DE BORD CONNU, DIT PLUTÔT QUE CORRIGÉ EN SILENCE : la tranche de
  // délai exclut sa borne haute (« 6-12 h » = [6 ; 12[), le filtre de délai de
  // l'Analytics l'inclut. Un pari détecté PILE à 12 h compte dans l'Analytics
  // et pas dans la configuration (relevé le 28/09 sur une base de test aux
  // heures rondes ; rare avec de vraies heures de détection).
  const bordDelai = f.delay_max !== null && f.delay_max !== undefined
    ? ` Le filtre de délai inclut sa borne haute (${num(f.delay_max)} h), la configuration non : un pari détecté pile à cette borne peut s'ajouter.`
    : '';
  toast((manques.length
    ? 'Une partie de la configuration n\'existe pas dans les filtres : l\'analyse peut être plus large.'
    : `Configuration « ${s.title || ''} » reportée dans les filtres de l'Analytics.`) + bordDelai, 8000);
}

/* ── Exports ────────────────────────────────────────────────────── */

/* Une ligne par configuration, valeurs BRUTES du serveur (pleine précision,
 * virgule décimale posée par `csvTexte`) ; les critères sous leur libellé. */
function sfExporterCsv() {
  if (!SF || !(SF.strategies || []).length) { toast('Aucune configuration à exporter.'); return; }
  const p = SF.params || {};
  const entetes = ['rank'].concat(SF_DIMS.map(([, , csv]) => csv), ['settled', 'clv', 'clv_n',
    'roi', 'pnl', 'stake_total', 'validation_clv', 'validation_roi', 'validation_settled',
    'robustesse', 'echantillon']);
  const lignes = SF.strategies.map((s) => {
    const sm = s.summary || {}, v = s.validation || {};
    return [s.rank].concat(SF_DIMS.map(([dim]) => sfCritere(s, dim)), [sm.settled, sm.clv,
      sm.clv_n, sm.roi, sm.pnl, sm.stake_total, v.clv, v.roi, v.settled,
      (s.robustness || {}).label, (s.sample || {}).label]);
  });
  const de = p.date_from || (REFS && REFS.date_min) || '';
  const a = p.date_to || (REFS && REFS.date_max) || '';
  telecharger(`valuebet-strategies-${p.sport || 'tous'}-${de}_${a}.csv`, csvTexte(entetes, lignes));
  toast(`${ent(lignes.length)} configurations exportées.`);
}

/* L'en-tête du papier : les paramètres que le SERVEUR a appliqués. */
function sfEntetePdf(d) {
  const p = d.params || {};
  const sp = d.split || {};
  const h = $('sf-entete-pdf');
  h.innerHTML = '';
  h.hidden = false;
  h.appendChild(el('h2', null, 'Valuebet Analytics — Strategy Finder'));
  const dl = el('dl', 'export-filtres');
  [['Sport', sfNomSport(p)],
   ['Période', p.date_from || p.date_to
     ? `${dateLongue(p.date_from, true)} → ${dateLongue(p.date_to, true)}` : 'toute la période disponible'],
   ['Population', p.population_label || nomPopulation(p.population)],
   ['Minimum', `${ent(p.min_n)} paris réglés par configuration`],
   ['Mode', p.objective_label || p.objective || '—'],
   ['Mise', `${num(p.stake)} € par pari (notionnelle)`],
   ['Validation', sp.cutoff
     ? `${periodeTexte((sp.validation || {}).from, (sp.validation || {}).to)} (hors échantillon)`
     : 'aucune période réservée'],
   ['Exporté le', new Date().toLocaleString('fr-BE')],
  ].forEach(([k, v]) => {
    dl.appendChild(el('dt', null, k));
    dl.appendChild(el('dd', null, String(v)));
  });
  h.appendChild(dl);
}

function sfImprimer() {
  if (!SF) { toast('Lancez d\'abord une recherche.'); return; }
  sfEntetePdf(SF);
  window.print();
}

function brancherStrategies() {
  $('sf-lancer').addEventListener('click', sfLancer);
  $('sf-methode-btn').addEventListener('click', sfOuvrirMethode);
  document.querySelectorAll('#sf-min-presets button, #sf-rapides .puce').forEach((b) =>
    b.addEventListener('click', () => sfPoserMin(b.dataset.min, true)));
  $('sf-min').addEventListener('input', () => sfPoserMin($('sf-min').value, false));
  $('sf-min').addEventListener('keydown', (e) => { if (e.key === 'Enter') sfLancer(); });
  document.querySelectorAll('#sf-objectif button').forEach((b) =>
    b.addEventListener('click', () => sfChoisirObjectif(b.dataset.objectif)));
  $('sf-csv').addEventListener('click', sfExporterCsv);
  $('sf-pdf').addEventListener('click', sfImprimer);
  $('sf-ouvrir-analytics').addEventListener('click', () => { if (SF_DETAIL) sfOuvrirAnalytics(SF_DETAIL); });
  ['sf-methode', 'sf-detail'].forEach((id) => {
    const m = $(id);
    m.addEventListener('keydown', sfPiegeFocus);
    // Un clic sur le fond, HORS de la boîte, ferme la modale.
    m.addEventListener('click', (e) => { if (e.target === m) sfFermerModale(); });
    m.querySelector('.sf-modale-fermer').addEventListener('click', () => sfFermerModale());
  });
  $('sf-voile').addEventListener('click', () => sfFermerModale());
}

/* ── Paramètres ────────────────────────────────────────────────────── */

function majAidePopulation() {
  const p = (REFS.populations || []).find((x) => x.value === $('f-population').value);
  $('aide-population').textContent = p
    ? p.explication + (p.limites && p.limites.length ? ' Limite : ' + p.limites.join(' ') : '')
    : '';
}

function rendreParametres() {
  segActif('pr-theme', 'themeChoix', choixTheme());
  $('pr-sidebar').checked = document.documentElement.classList.contains('sb-replie');
  $('pr-lignes').value = String(PAR_PAGE);
  if (!REFS) return;
  const dl = $('pr-donnees');
  dl.innerHTML = '';
  const per = REFS.perimetre || {};
  const ex = per.exclus || {};
  const lignes = [
    ['Période des données', `${dateLongue(REFS.date_min, true)} → ${dateLongue(REFS.date_max, true)}`],
    ['Sports analysés', (per.sports || REFS.sports).map(nomSport).join(', ')],
    ['Marchés analysés', (per.markets || REFS.markets).map(nomMarche).join(', ')],
    ['Bookmakers', ent(REFS.bookmakers.length)],
    ['Compétitions', ent(REFS.leagues.length)],
  ];
  if (ex.total_detections !== undefined) lignes.push(['Détections en base', ent(ex.total_detections)]);
  if (ex.sport_hors_perimetre !== undefined) lignes.push(['Hors périmètre — sport', ent(ex.sport_hors_perimetre)]);
  if (ex.marche_hors_perimetre !== undefined) lignes.push(['Hors périmètre — marché', ent(ex.marche_hors_perimetre)]);
  if (ex.sans_evenement !== undefined) lignes.push(['Sans événement', ent(ex.sans_evenement)]);
  lignes.forEach(([k, v]) => {
    dl.appendChild(el('dt', null, k));
    dl.appendChild(el('dd', null, v));
  });
  $('pr-perimetre').textContent = per.pourquoi || '';
}

/* ── Démarrage ─────────────────────────────────────────────────────── */

function brancherInterface() {
  // Navigation.
  window.addEventListener('hashchange', () => afficherPage(pageDuLien()));
  $('sb-replier').addEventListener('click', () =>
    replierSidebar(!document.documentElement.classList.contains('sb-replie')));
  $('menu-mobile').addEventListener('click', ouvrirNavMobile);
  $('voile-nav').addEventListener('click', fermerNavMobile);

  // Thème.
  appliquerTheme(choixTheme(), false);
  $('theme').addEventListener('click', () => {
    const sombre = document.documentElement.getAttribute('data-theme') === 'dark';
    stock.ecrire('vb-theme', sombre ? 'light' : 'dark');
    appliquerTheme(sombre ? 'light' : 'dark', true);
  });
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const suivreSysteme = () => { if (choixTheme() === 'system') appliquerTheme('system', true); };
  if (mq.addEventListener) mq.addEventListener('change', suivreSysteme);
  document.querySelectorAll('#pr-theme button').forEach((b) => {
    b.addEventListener('click', () => {
      stock.ecrire('vb-theme', b.dataset.themeChoix);
      appliquerTheme(b.dataset.themeChoix, true);
    });
  });
  $('pr-sidebar').addEventListener('change', () => replierSidebar($('pr-sidebar').checked));
  $('pr-lignes').addEventListener('change', () => {
    PAR_PAGE = Number($('pr-lignes').value) || 25;
    PAGE = 1;
    stock.ecrire('vb-lignes', String(PAR_PAGE));
    RENDUES.delete('paris');
  });

  // Fermetures globales.
  document.addEventListener('click', fermerMenus);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    // Une couche à la fois : le menu ouvert d'abord, puis une modale du
    // Strategy Finder, puis le tiroir.
    if (document.querySelector('.menu.ouvert')) { fermerMenus(true); return; }
    if (SF_MODALE) { sfFermerModale(); return; }
    if ($('tiroir').classList.contains('ouvert')) { fermerTiroir(); return; }
    if ($('app').classList.contains('nav-ouverte')) { fermerNavMobile(); $('menu-mobile').focus(); }
  });
  document.querySelectorAll('.sb-lien').forEach((a) => a.addEventListener('click', fermerNavMobile));
  brancherStrategies();
  window.addEventListener('resize', () => {
    clearTimeout(redessiner.minuteur);
    redessiner.minuteur = setTimeout(() => {
      if (Math.abs(window.innerWidth - LARGEUR_FENETRE) < 24) return;
      LARGEUR_FENETRE = window.innerWidth;
      redessiner();
    }, 180);
  });
}

/* ⚠️ UNE API INDISPONIBLE AU DÉMARRAGE N'EST PAS UNE PANNE DÉFINITIVE.
 * Le serveur redémarre, la base se reconstruit : on réessaie seul, à
 * intervalles croissants, et un bouton permet de le faire tout de suite. */
async function chargerRefs(essai) {
  clearTimeout(chargerRefs.minuteur);
  try {
    return await appel(API_FILTERS);
  } catch (e) {
    const attente = Math.min(30, 2 ** Math.min(essai + 1, 5));
    $('sb-point').className = 'point ko';
    $('sb-statut').textContent = 'API indisponible';
    $('contexte').textContent = 'Filtres indisponibles.';
    $('kpis').innerHTML = '';
    avertissements([`Impossible de lire les filtres : ${e.message} — nouvel essai dans ${attente} s.`]);
    const av = $('avertissements').lastElementChild;
    if (av) {
      const b = el('button', 'avert-plus', 'Réessayer maintenant');
      b.type = 'button';
      b.addEventListener('click', (ev) => { ev.stopPropagation(); chargerRefs.relancer(0); });
      av.appendChild(b);
    }
    return new Promise((ok) => {
      chargerRefs.relancer = (n) => { clearTimeout(chargerRefs.minuteur); ok(chargerRefs(n)); };
      chargerRefs.minuteur = setTimeout(() => chargerRefs.relancer(essai + 1), attente * 1000);
    });
  }
}

async function demarrer() {
  PAR_PAGE = Number(stock.lire('vb-lignes', '25')) || 25;
  brancherInterface();
  afficherPage(pageDuLien());
  squelettesKpi('kpis', 4);

  REFS = await chargerRefs(0);
  avertissements([]);
  $('sb-point').className = 'point ok';
  $('sb-statut').textContent = 'API connectée';
  $('sb-donnees').textContent = `Données jusqu'au ${dateLongue(REFS.date_max, true)}`;
  // ⚠️ LE PÉRIMÈTRE EST ANNONCÉ. Un total qui ne couvre pas tout ce que le
  // moteur détecte doit le DIRE : sans ça, « 27 053 opportunités » se lit
  // comme « tout ce que j'ai détecté », et l'écart avec les chiffres du
  // daemon passerait pour un bug.
  const per = REFS.perimetre || { sports: REFS.sports, markets: REFS.markets };
  $('sb-donnees').title = `${(per.sports || []).map(nomSport).join(' + ')} · `
    + `${(per.markets || []).map(nomMarche).join(' + ')}`
    + (per.pourquoi ? ' — ' + per.pourquoi : '');

  groupeCases($('f-sports'), REFS.sports,
    { labels: REFS.sports_labels, courte: true, tous: 'Tous les sports',
      onChange: () => { panneauxEvParSport(); panneauxCoteParSport(); } });
  groupeCases($('f-books'), REFS.bookmakers,
    { labels: REFS.bookmakers_labels, tous: 'Tous les bookmakers' });
  groupeCases($('f-markets'), REFS.markets,
    { labels: REFS.markets_labels, courte: true, tous: 'Tous les marchés' });
  groupeCases($('f-league'), REFS.leagues, { tous: 'Toutes les compétitions' });
  $('aide-ligue').textContent = `${ent(REFS.leagues.length)} compétitions — `
    + 'la recherche masque, elle ne décoche rien.';
  /* ⚠️ LES LIBELLÉS VIENNENT DU SERVEUR, y compris la notation « 1 X 2 ».
   * Les écrire ici ferait exister deux vocabulaires pour le même pari, et
   * l'un des deux finirait par ne plus correspondre à ce que le filtre
   * envoie au serveur. */
  groupeCases($('f-outcomes'),
    (REFS.outcomes || []).map((o) => ({ key: o.value, label: o.libelle })),
    { courte: true, tous: 'Tous les paris' });
  groupeCases($('f-ev-bands'), REFS.ev_bands, { courte: true, tous: 'Toutes les tranches' });
  $('ev-split').addEventListener('change', panneauxEvParSport);
  panneauxEvParSport();
  groupeCases($('f-odds-bands'), bandesCote(), { courte: true, tous: 'Toutes les tranches' });
  $('cote-split').addEventListener('change', panneauxCoteParSport);
  panneauxCoteParSport();
  $('ev-cote-split').addEventListener('change', panneauxEvParCote);
  panneauxEvParCote();
  $('f-delay-unite').addEventListener('change', majAideDelai);
  ['f-delay-min', 'f-delay-max'].forEach(
    (id) => $(id).addEventListener('input', majAideDelai));
  majAideDelai();
  boutonsDelai();
  pliage();

  const sp = $('f-population');
  REFS.populations.forEach((p) => {
    // Le LIBELLÉ vient du serveur ; `value` reste la valeur canonique, celle
    // qui repart en filtre. Les confondre enverrait « Toutes les détections »
    // à une API qui ne connaît que « detected ».
    const o = el('option', null, p.libelle || p.value.replace(/_/g, ' '));
    o.value = p.value;
    sp.appendChild(o);
  });
  sp.addEventListener('change', majAidePopulation);
  majAidePopulation();

  if (REFS.date_min) $('f-date-from').value = REFS.date_min;
  if (REFS.date_max) $('f-date-to').value = REFS.date_max;
  // Le calendrier ne propose que les jours où il y a des données.
  ['f-date-from', 'f-date-to'].forEach((id) => {
    if (REFS.date_min) $(id).min = REFS.date_min;
    if (REFS.date_max) $(id).max = REFS.date_max;
  });

  // Tout changement de filtre, où qu'il ait lieu, passe par le même chemin.
  ['form', 'periode'].forEach((id) => {
    $(id).addEventListener('input', signalerChangement);
    $(id).addEventListener('change', signalerChangement);
  });

  // Modes du tiroir.
  document.querySelectorAll('#cote-mode button').forEach((b) =>
    b.addEventListener('click', () => appliquerModeCote(b.dataset.mode, true)));
  document.querySelectorAll('#ev-mode button').forEach((b) =>
    b.addEventListener('click', () => appliquerModeEv(b.dataset.mode, true)));
  document.querySelectorAll('#mise-mode button').forEach((b) =>
    b.addEventListener('click', () => appliquerModeMise(b.dataset.mode, true)));
  deduireModes();

  // Menus de la barre.
  brancherMenu('m-sport', menuUnique('f-sports', REFS.sports, nomSport, 'Tous les sports',
    () => { panneauxEvParSport(); panneauxCoteParSport(); }));
  brancherMenu('m-books', menuBooks);
  brancherMenu('m-market', menuUnique('f-markets', REFS.markets, nomMarche, 'Tous les marchés'));
  brancherMenu('m-ev', menuEv);
  brancherMenu('m-export', null);

  // Tiroir.
  $('ouvrir-filtres').addEventListener('click', () => ouvrirTiroir());
  $('fermer-filtres').addEventListener('click', fermerTiroir);
  $('voile-filtres').addEventListener('click', fermerTiroir);
  $('appliquer').addEventListener('click', () => { fermerTiroir(); analyser(); });
  $('aa-ouvrir-segmentation').addEventListener('click', () => ouvrirTiroir('champ-segmentation'));

  // Analyse et graphiques.
  $('analyser').addEventListener('click', analyser);
  $('m-mesure').addEventListener('change', matrice);
  $('s-lancer').addEventListener('click', chercherSegments);
  document.querySelectorAll('#evol-mode button').forEach((b) => b.addEventListener('click', () => {
    MODE_EVOL = b.dataset.mode;
    if (ANALYSE && ANALYSE.summary.opportunities) grapheEvolution(ANALYSE);
  }));
  document.querySelectorAll('#fin-mode button').forEach((b) => b.addEventListener('click', () => {
    MODE_FIN = b.dataset.mode;
    if (ANALYSE && ANALYSE.summary.opportunities) grapheFinance(ANALYSE);
  }));
  document.querySelectorAll('#gran-rapide button').forEach((b) => b.addEventListener('click', () => {
    $('f-gran').value = b.dataset.gran;
    signalerChangement();
    analyser();
  }));
  document.querySelectorAll('#pj-filtre button').forEach((b) => b.addEventListener('click', () => {
    if (b.disabled || PJ_MODE === b.dataset.played) return;
    choisirLentille(b.dataset.played);
    rendrePage('paris');
  }));
  $('dernieres-tout').addEventListener('click', () => choisirLentille('tous'));
  $('cmp-recherche').addEventListener('input', () => { LIMITE_CMP = 50; tableCompetitions(); });
  $('aa-recherche').addEventListener('input', () => { if (ANALYSE) rendreAvancee(ANALYSE); });

  /* ⚠️ AUCUNE BIBLIOTHÈQUE. L'impression du navigateur produit déjà un PDF
   * fidèle, hors ligne, avec les polices et les graphiques rendus tels qu'ils
   * s'affichent. La mise en page papier vit dans `@media print`. */
  $('pdf').addEventListener('click', imprimerPage);
  $('pdf-rapport').addEventListener('click', imprimerRapport);
  $('csv-opps').addEventListener('click', exporterCsvOpportunites);
  $('csv-decoupes').addEventListener('click', exporterCsvDecoupes);
  $('ex-pdf').addEventListener('click', imprimerRapport);
  $('ex-csv-opps').addEventListener('click', exporterCsvOpportunites);
  $('ex-csv-decoupes').addEventListener('click', exporterCsvDecoupes);

  // Mes analyses.
  $('ma-sauver').addEventListener('click', sauverAnalyse);
  $('ma-nom').addEventListener('keydown', (e) => { if (e.key === 'Enter') sauverAnalyse(); });

  // État vide.
  $('vide-periode').addEventListener('click', () => {
    $('f-date-from').value = REFS.date_min || '';
    $('f-date-to').value = REFS.date_max || '';
    signalerChangement();
    analyser();
  });
  $('vide-reinit').addEventListener('click', () => { $('reinit').click(); analyser(); });
  $('pr-reinit').addEventListener('click', () => {
    $('reinit').click();
    toast('Filtres réinitialisés — cliquez « Analyser » pour les appliquer.');
  });

  $('reinit').addEventListener('click', () => {
    $('form').reset();
    document.querySelectorAll('#form .cases').forEach((h) => { if (h.id) cocher(h.id, []); });
    $('delay-rapides').querySelectorAll('button')
      .forEach((b) => b.classList.remove('actif'));
    panneauxEvParSport();
    panneauxCoteParSport();
    // Sans ça, `form.reset()` décocherait la case et laisserait les bornes
    // saisies à l'écran : un réglage visible qui ne part plus au serveur.
    panneauxEvParCote();
    appliquerModeCote('toutes', false);
    appliquerModeEv('minimum', false);
    appliquerModeMise('flat', false);
    majAideDelai();
    if (REFS.date_min) $('f-date-from').value = REFS.date_min;
    if (REFS.date_max) $('f-date-to').value = REFS.date_max;
    majAidePopulation();
    signalerChangement();
  });

  presets();
  majResumes();
  contexte();
  if (PAGE_ACTIVE === 'parametres') rendreParametres();
  if (PAGE_ACTIVE === 'mes-analyses') rendreMesAnalyses();
  if (PAGE_ACTIVE === 'strategies') sfPreparer();
  analyser();
}

demarrer();
