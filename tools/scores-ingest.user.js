// ==UserScript==
// @name         Valuebet — pont résultats (API-Football)
// @namespace    valuebet
// @version      1.0
// @description  Relaie les résultats de football depuis une IP résidentielle vers la VM.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @match        *://*.circus-sport.be/*
// @match        https://www.magicbetting.be/*
// @match        https://magicbettingsports.be/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @connect      v3.football.api-sports.io
// @connect      34.59.193.111
// @run-at       document-idle
// ==/UserScript==

/*
 * POURQUOI CE PONT EXISTE
 *
 * API-Sports refuse les IP de datacenter et le déguise en « account
 * suspended ». Mesuré le 16/08 avec la MÊME clé : 200 depuis une IP
 * résidentielle, suspension depuis la VM comme depuis un conteneur cloud. Le
 * compte était alors actif ; c'était l'origine qui était rejetée.
 *
 * ⚠️ Le 21/08 le compte a été RÉELLEMENT suspendu, constaté sur le tableau de
 * bord. Ce pont ne contourne que le refus d'IP : contre une suspension de
 * compte il est inopérant, et le vérifier sur le tableau de bord évite de le
 * soupçonner à tort. C'est le quatrième
 * anti-bot du projet après DataDome, Gaming1 et Cloudflare — et le premier à
 * toucher la mesure plutôt que la collecte.
 *
 * CE SCRIPT NE COMPREND RIEN, ET C'EST VOULU
 *
 * Il demande à la VM quoi appeler, appelle, et repose la réponse BRUTE. Il
 * n'analyse aucun match, ne calcule aucune date, ne décide d'aucune fraîcheur.
 * Tout cela vit en Python, où c'est testé. C'est la leçon du §10 : le
 * JavaScript du pont Circus devait attribuer les réponses et s'est cassé trois
 * fois de suite. Le pont MagicBetting (§18.6), bête par construction, n'a
 * jamais cassé.
 *
 * PAS D'ONGLET DÉDIÉ
 *
 * Contrairement aux trois autres ponts, celui-ci se greffe sur les onglets
 * DÉJÀ ouverts (Betano, Circus, MagicBetting) : un résultat de match ne bouge
 * plus une fois le match fini, et le plan ne réclame que les journées
 * manquantes — quelques appels par jour. Un PC éteint 24 h ne perd rien, il
 * prend du retard, et `results-update` rattrape sur sa fenêtre.
 *
 * À RENSEIGNER CI-DESSOUS : la clé API-Football et le jeton du serveur.
 * La clé reste ICI, dans Tampermonkey — elle ne transite jamais par le réseau
 * et n'entre jamais dans le dépôt (§18.7 : un userscript ne vit pas dans git).
 */

(function () {
  'use strict';

  const API_KEY   = 'REMPLACE_PAR_TA_CLE_API_FOOTBALL';
  const VM        = 'http://34.59.193.111:8787';
  const TOKEN     = 'REMPLACE_PAR_TON_JETON_INGEST';
  const API_BASE  = 'https://v3.football.api-sports.io';
  const PERIOD_MS = 60 * 60 * 1000;   // un tour par heure ; le plan est presque toujours vide

  // Une seule instance, même si plusieurs onglets correspondent au @match.
  // Deux ponts qui tournent en parallèle doubleraient la consommation de quota
  // pour écrire exactement le même fichier — c'est le piège du §7, et sa
  // variante « deux frames » du §18.7.
  const LOCK = 'valuebet_scores_bridge_owner';
  const ME = String(Math.random()).slice(2);

  function claimLock() {
    try {
      const raw = localStorage.getItem(LOCK);
      const held = raw ? JSON.parse(raw) : null;
      if (held && held.who !== ME && Date.now() - held.at < 3 * PERIOD_MS) return false;
      localStorage.setItem(LOCK, JSON.stringify({ who: ME, at: Date.now() }));
      return true;
    } catch (e) {
      return true;   // pas de localStorage : on tourne, un doublon coûte moins qu'un silence
    }
  }

  function log(msg) { console.log('[valuebet-scores]', msg); }

  function request(opts) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: opts.method, url: opts.url, headers: opts.headers || {},
        data: opts.data, timeout: 120000,
        onload: (r) => resolve(r),
        onerror: (e) => reject(new Error('réseau: ' + JSON.stringify(e))),
        ontimeout: () => reject(new Error('timeout')),
      });
    });
  }

  async function tick() {
    if (!claimLock()) { log('un autre onglet tient le pont — rien à faire'); return; }

    let plan;
    try {
      const r = await request({
        method: 'GET', url: VM + '/scores-plan',
        headers: { 'Authorization': 'Bearer ' + TOKEN },
      });
      if (r.status !== 200) { log('plan refusé: HTTP ' + r.status + ' ' + r.responseText); return; }
      plan = JSON.parse(r.responseText).fetch || [];
    } catch (e) {
      log('plan injoignable: ' + e.message);
      return;
    }

    if (!plan.length) { log('rien à récupérer — tout est à jour'); return; }
    log(plan.length + ' journée(s) à récupérer');

    for (const item of plan) {
      try {
        const src = await request({
          method: 'GET', url: API_BASE + item.path,
          headers: { 'x-apisports-key': API_KEY, 'Accept': 'application/json' },
        });
        if (src.status !== 200) {
          log('source HTTP ' + src.status + ' sur ' + item.path);
          continue;
        }
        // On reposte le corps TEL QUEL. Ne rien reformater est la garantie que
        // la VM analyse exactement ce que la source a répondu — y compris ses
        // messages d'erreur, que le serveur sait reconnaître et refuser plutôt
        // que d'enregistrer une journée vide.
        const push = await request({
          method: 'POST', url: VM + item.post_to,
          headers: { 'Authorization': 'Bearer ' + TOKEN,
                     'Content-Type': 'application/json' },
          data: src.responseText,
        });
        log(item.post_to + ' -> HTTP ' + push.status + ' ' + push.responseText.slice(0, 160));
      } catch (e) {
        log('échec sur ' + item.path + ': ' + e.message);
      }
    }
  }

  // Déclencheur manuel dans le menu Tampermonkey. Sans lui, vérifier une
  // installation demande d'attendre le tour suivant, et un pont muet ressemble
  // exactement à un pont qui n'a rien à faire — c'est le mode de défaillance
  // dominant du projet. Ici on force le tour et on lit la console tout de
  // suite. Il ignore le verrou : c'est une demande explicite de l'utilisateur.
  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('⚽ Récupérer les résultats maintenant', () => {
      try { localStorage.removeItem(LOCK); } catch (e) { /* sans importance */ }
      log('déclenchement manuel');
      tick();
    });
  }

  tick();
  setInterval(tick, PERIOD_MS);
})();
