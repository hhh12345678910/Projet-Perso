// ==UserScript==
// @name         MagicBetting -> Valuebet
// @namespace    valuebet
// @version      1.0
// @description  Relaie les réponses de l'API MagicBetting (Digitain) vers la VM.
// @match        https://sport-ak.bldiframe.com/*
// @match        https://www.magicbetting.be/*
// @match        https://magicbettingsports.be/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @connect      *
// ==/UserScript==
//
// POURQUOI CE PONT
// ----------------
// L'API Digitain est derrière Cloudflare, qui sert un défi « Just a moment… »
// à toute IP de datacenter : mesuré, la VM reçoit 403 là où ce navigateur
// reçoit 200, parce qu'il porte un cf_clearance lié à ton IP. Il n'existe pas
// de contournement côté serveur.
//
// CE QUE CE SCRIPT NE FAIT PAS
// ----------------------------
// Il ne déchiffre rien, ne parse rien, n'attribue rien. Il appelle l'API et
// poste la réponse CHIFFRÉE telle quelle. Tout le reste — déchiffrement par
// leur propre WebAssembly, mapping des marchés — se passe en Python sur la VM,
// où vivent les tests.
//
// C'est délibéré et c'est la leçon du §10 : le userscript de Circus devait
// attribuer les réponses en JavaScript et s'est cassé TROIS fois. Ici la
// partie fragile est testable.
//
// RÉGLAGES — à adapter avant usage
(function () {
  'use strict';

  const VM      = 'http://34.59.193.111:8787';  // ton serveur d'ingestion
  const TOKEN   = 'REMPLACE_PAR_BETANO_INGEST_TOKEN';
  const PERIOD  = 60_000;   // un appel par minute et par sport

  // Les sports à balayer — rien d'autre. Les SportId Digitain et les marchés
  // demandés vivent sur la VM (MAGIC_SPORT_IDS, MAGIC_STAKE_TYPES), parce
  // qu'ils diffèrent PAR SPORT et que s'y tromper ne lève aucune erreur : le
  // football écrit son 1X2 sous l'Id 1, le tennis son vainqueur sous 702, et
  // demander [1,3] au tennis rendrait des totaux sans aucun vainqueur — un
  // sport à moitié collecté, ce qui ne ressemble pas à une panne.
  //
  // Les tenir ici obligerait à recoller ce script dans Tampermonkey à chaque
  // ajustement, alors qu'un `git pull` suffit côté VM.
  const SPORTS = { soccer: 1, tennis: 1 };

  const log = (...a) => console.log('[valuebet-mb]', ...a);

  // ⚠️ UN SEUL contexte doit pousser. La partie sport est une IFRAME servie par
  // bldiframe.com, donc sans cette garde le script tourne DEUX fois — mesuré :
  // chaque envoi arrivait en double sur la VM. C'est la version « deux frames »
  // du piège du §7 (un seul onglet par book).
  //
  // On ne garde que le contexte dont l'URL porte l'identifiant de session de
  // 36 caractères : c'est celui qui peut construire l'URL de l'API, et il n'y
  // en a qu'un.
  if (!location.pathname.split('/').some((s) => s.length === 36)) {
    return;   // page enveloppe, ou sport pas encore ouvert
  }

  // Le chemin de l'API porte un identifiant de session de 36 caractères, pris
  // dans l'URL de la page — request.js fait exactement pareil. Le coder en dur
  // le ferait expirer sans prévenir.
  function pathPrefix() {
    // Garanti présent : la garde d'entrée l'a vérifié.
    return `/${location.pathname.split('/').find((s) => s.length === 36)}`;
  }

  // --- Le pont suit un plan, il n'en fabrique aucun ------------------------
  //
  // `gettopeventslist` rendait 27 matchs : la vitrine, pas l'offre. Le
  // catalogue du site en annonce 1173 en football — on en collectait 2 %.
  // L'offre entière se demande compétition par compétition, ce qui suppose de
  // savoir LESQUELLES et DANS QUEL ORDRE.
  //
  // Cette décision est prise sur la VM, en Python, là où vivent les tests. Ici
  // on se contente de demander « quoi appeler maintenant », d'appeler, et de
  // reposter le corps brut à l'adresse indiquée. Le contrat tient en une ligne :
  //
  //     {"fetch": [{"path": "/common/…", "post_to": "/ingest-…"}]}
  //
  // Si la réponse d'un `post_to` est elle-même un plan, on l'exécute aussi —
  // c'est ainsi que le catalogue se construit sans que ce script sache ce
  // qu'est un catalogue.
  //
  // C'est la leçon du §10 : le pont Circus devait attribuer ses réponses en
  // JavaScript et s'est cassé trois fois.
  const MAX_DEPTH = 3;

  function vm(method, route, data) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method, url: `${VM}${route}`, data,
        headers: { 'Content-Type': 'application/json', 'X-Ingest-Token': TOKEN },
        onload: (r) => {
          try { resolve(JSON.parse(r.responseText)); }
          catch { resolve(null); }
        },
        onerror: () => { log('VM injoignable :', route); resolve(null); },
      });
    });
  }

  async function fetchSite(path) {
    try {
      // Appel depuis la page : même origine, donc les cookies Cloudflare
      // partent tout seuls. C'est toute la raison d'être de ce pont.
      const r = await fetch(`${location.origin}${pathPrefix()}${path}`, {
        credentials: 'include', headers: { accept: '*/*' },
      });
      if (!r.ok) { log(`site -> HTTP ${r.status}`, path); return null; }
      const body = await r.text();
      return body && body.length >= 100 ? body : null;
    } catch (e) {
      log('site injoignable :', e.message);
      return null;
    }
  }

  async function follow(plan, depth) {
    if (!plan || !Array.isArray(plan.fetch) || depth > MAX_DEPTH) return 0;
    let done = 0;
    for (const item of plan.fetch) {
      if (!item || !item.path || !item.post_to) continue;
      const body = await fetchSite(item.path);
      if (!body) continue;
      const res = await vm('POST', item.post_to, body);
      done += 1;
      // La réponse peut être un plan à son tour (catalogue -> compétitions).
      done += await follow(res, depth + 1);
      // Une petite pause : un balayage qui part en rafale ressemble à un
      // robot, alors qu'étalé il ressemble à quelqu'un qui navigue.
      await new Promise((r) => setTimeout(r, 250));
    }
    return done;
  }

  async function tick() {
    for (const sport of Object.keys(SPORTS)) {
      const plan = await vm('GET', `/magic-plan?sport=${encodeURIComponent(sport)}`);
      const n = await follow(plan, 0);
      log(`${sport} : ${n} appels relayés`);
    }
  }

  // Cadencé par un Worker, pas par setInterval : Chrome ralentit fortement les
  // timers d'un onglet en arrière-plan, et le pont Circus croyait tenir 30 s
  // quand la mesure en disait 180 (§13.3).
  const worker = new Worker(URL.createObjectURL(new Blob([
    `let p=${PERIOD};setInterval(()=>postMessage(0),1000);`,
  ], { type: 'application/javascript' })));
  let last = 0;
  worker.onmessage = () => {
    const now = Date.now();
    if (now - last >= PERIOD) { last = now; tick(); }
  };

  log('pont actif, VM =', VM, '| période', PERIOD / 1000, 's');
  tick();
})();
