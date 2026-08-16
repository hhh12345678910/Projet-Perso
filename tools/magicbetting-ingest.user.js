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

  // Notre nom -> SportId Digitain et marchés demandés. Les stakeTypes sont
  // exactement ceux que src/scrapers/magicbetting.py sait lire ; en demander
  // d'autres ferait grossir la réponse pour rien.
  //
  // ⚠️ Les marchés diffèrent PAR SPORT, ce n'est pas une liste commune : le
  // football écrit son 1X2 sous l'Id 1, le tennis son vainqueur sous l'Id 702.
  // Demander [1,3] au tennis rendrait des totaux sans aucun vainqueur — un
  // sport à moitié collecté, ce qui ne ressemble pas à une panne.
  //
  // Confirmé sur capture du 16/08 (SId 3, ATP Cincinnati) : 702 rend 2 issues
  // par match, toutes des noms de joueur ; 3 rend des totaux de JEUX (lignes
  // 16,5 à 28 — un total de sets vaudrait 2,5).
  const SPORTS = {
    soccer: { id: 1, stakeTypes: [1, 3] },
    tennis: { id: 3, stakeTypes: [702, 3] },
  };

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

  function apiUrl(cfg) {
    const p = new URLSearchParams({
      sportId: String(cfg.id),
      langId: '62',
      partnerId: '3000270',
      countryCode: 'BE',
    });
    for (const st of cfg.stakeTypes) p.append('stakeTypes', String(st));
    return `${location.origin}${pathPrefix()}/prematch/gettopeventslist?${p}`;
  }

  async function pushOne(sport, cfg) {
    let body;
    try {
      // Appel depuis la page : même origine, donc les cookies Cloudflare
      // partent tout seuls. C'est toute la raison d'être de ce pont.
      const r = await fetch(apiUrl(cfg), {
        credentials: 'include',
        headers: { accept: '*/*' },
      });
      if (!r.ok) { log(`API ${sport} -> HTTP ${r.status}`); return; }
      body = await r.text();
    } catch (e) {
      log(`API ${sport} injoignable :`, e.message);
      return;
    }
    if (!body || body.length < 100) { log(`réponse ${sport} vide, ignorée`); return; }

    // GM_xmlhttpRequest et non fetch : la VM est sur une autre origine, et
    // seul GM_* franchit le CORS.
    GM_xmlhttpRequest({
      method: 'POST',
      url: `${VM}/ingest-magicbetting?sport=${encodeURIComponent(sport)}`,
      headers: { 'Content-Type': 'application/json', 'X-Ingest-Token': TOKEN },
      data: body,
      onload: (res) => log(`${sport} -> ${res.status} ${res.responseText.slice(0, 160)}`),
      onerror: () => log(`${sport} -> VM injoignable`),
    });
  }

  async function tick() {
    for (const [sport, cfg] of Object.entries(SPORTS)) await pushOne(sport, cfg);
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
