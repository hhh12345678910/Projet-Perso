// ==UserScript==
// @name         MagicBetting DÉCOUVERTE -> Valuebet
// @namespace    valuebet
// @version      1.0
// @description  Recopie vers la VM les appels d'API que MagicBetting fait lui-même.
// @match        https://sport-ak.bldiframe.com/*
// @match        https://www.magicbetting.be/*
// @match        https://magicbetting.be/*
// @match        https://magicbettingsports.be/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @connect      *
// ==/UserScript==
//
// À QUOI ÇA SERT
// --------------
// Deux inconnues bloquent MagicBetting (§18.6) :
//
//   1. `gettopeventslist` ne rend que 27 matchs — la liste des événements
//      VEDETTES, pas l'offre complète. 612 cotes contre 7 478 pour StarCasino.
//   2. Le tennis demande trois identifiants : le sportId, l'Id du marché
//      vainqueur, l'Id des totaux de jeux.
//
// Le §10 interdit de les deviner. Napoleon utilise 547 en football et 521 en
// tennis : une supposition qui tombe à côté ne lève aucune erreur, elle rend
// simplement un book muet — la panne dominante du projet (§11).
//
// Alors on ne devine pas. Le site connaît ses propres endpoints et les appelle
// tout seul quand on clique. Ce script écoute `fetch` et `XMLHttpRequest`,
// et recopie chaque réponse d'API vers la VM, qui la déchiffre avec leur WASM.
//
// ⚠️ SCRIPT TEMPORAIRE. Il capture pendant une session de navigation, puis on
// le DÉSACTIVE. Ce n'est pas un pont de production : le pont, c'est
// magicbetting-ingest.user.js, et lui seul écrit les cotes que lit le daemon.
// Les sondes atterrissent dans data/magicbetting/probes/ — jamais dans le
// fichier de production.
//
// MODE D'EMPLOI
// -------------
//   1. Renseigner VM et TOKEN ci-dessous (le MÊME token que l'autre script).
//   2. Activer, ouvrir magicbetting.be, recharger.
//   3. Naviguer POSÉMENT : la page football d'accueil, puis « tous les
//      matchs » / la liste complète des compétitions, puis le TENNIS, puis un
//      match de tennis. Chaque écran déclenche ses propres appels.
//   4. Sur la VM : python3 scripts/magic_probe_report.py
//   5. DÉSACTIVER ce script.
(function () {
  'use strict';

  const VM     = 'http://34.59.193.111:8787';
  const TOKEN  = 'REMPLACE_PAR_BETANO_INGEST_TOKEN';

  const MAX_CAPTURES = 120;    // borne dure : on explore, on n'inonde pas
  const MIN_BYTES    = 200;    // en dessous, c'est un ping ou une erreur

  const seen = new Set();
  let sent = 0;
  const log = (...a) => console.log('[valuebet-decouverte]', ...a);

  // On ne recopie QUE les appels d'API du site lui-même. Sans ce filtre on
  // enverrait aussi la télémétrie Sentry et les traceurs publicitaires — du
  // bruit, et des données qui ne nous regardent pas.
  function interesting(url) {
    let u;
    try { u = new URL(url, location.href); } catch { return false; }
    if (u.origin !== location.origin) return false;
    if (/\.(js|css|png|jpe?g|svg|gif|woff2?|ico|mp4)(\?|$)/i.test(u.pathname)) return false;
    if (/sentry|analytics|telemetry|gtm|hotjar/i.test(u.href)) return false;
    return true;
  }

  function push(url, body) {
    if (sent >= MAX_CAPTURES) return;
    // Clé = URL complète : deux sports sur le même endpoint sont deux
    // découvertes différentes, et il faut les deux.
    if (seen.has(url)) return;
    if (!body || body.length < MIN_BYTES) return;
    const t = body.trimStart()[0];
    if (t !== '{' && t !== '[') return;      // pas du JSON : pas notre affaire

    seen.add(url);
    sent += 1;
    GM_xmlhttpRequest({
      method: 'POST',
      url: `${VM}/probe-magicbetting`,
      headers: { 'Content-Type': 'application/json', 'X-Ingest-Token': TOKEN },
      data: JSON.stringify({ url, body }),
      onload: (r) => log(`${sent}/${MAX_CAPTURES}`, new URL(url).pathname,
                         '->', r.status, r.responseText.slice(0, 140)),
      onerror: () => log('VM injoignable pour', url),
    });
  }

  // --- fetch ---------------------------------------------------------------
  // `res.clone()` est obligatoire : lire le corps de la réponse originale le
  // consommerait et le site n'aurait plus rien à parser. On casserait
  // exactement la page qu'on observe.
  const origFetch = window.fetch;
  window.fetch = function (...args) {
    const p = origFetch.apply(this, args);
    try {
      const url = (args[0] && args[0].url) || String(args[0]);
      if (interesting(url)) {
        p.then((res) => { res.clone().text().then((b) => push(url, b), () => {}); },
               () => {});
      }
    } catch (e) { /* jamais gêner la page */ }
    return p;
  };

  // --- XMLHttpRequest ------------------------------------------------------
  // Les deux sont branchés parce que rien ne garantit lequel leur couche
  // réseau utilise, et n'en brancher qu'un rendrait un silence indiscernable
  // d'une absence d'appel.
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__vb_url = url;
    return origOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    try {
      if (interesting(this.__vb_url)) {
        this.addEventListener('loadend', () => {
          try {
            if (this.status === 200 && typeof this.responseText === 'string') {
              push(new URL(this.__vb_url, location.href).href, this.responseText);
            }
          } catch (e) { /* ignore */ }
        });
      }
    } catch (e) { /* ignore */ }
    return origSend.apply(this, args);
  };

  log('écoute active sur', location.origin, '| navigue : football complet, puis tennis');
})();
