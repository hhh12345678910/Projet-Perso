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
  const MAX_WS_FRAMES = 12;    // par socket : les flux de cotes sont bavards

  const seen = new Set();
  let sent = 0;
  const rejected = [];         // pour diagnostiquer un silence, cf. __vb
  const log = (...a) => console.log('[valuebet-decouverte]', ...a);

  // ⚠️ PAS de filtre sur l'origine. La première version exigeait la même
  // origine que la page, et n'a rien attrapé de sportif : la partie sport de
  // magicbetting.be est une IFRAME servie par sport-ak.bldiframe.com, donc
  // depuis le contexte parent tous les appels utiles sont cross-origin. Le
  // filtre écartait précisément ce qu'on cherche.
  //
  // On exclut seulement ce qui n'est manifestement pas une API de cotes :
  // ressources statiques et télémétrie.
  function interesting(url) {
    let u;
    try { u = new URL(url, location.href); } catch { return false; }
    if (/\.(js|css|png|jpe?g|svg|gif|woff2?|ico|mp4|map)(\?|$)/i.test(u.pathname)) return false;
    if (/sentry|analytics|telemetry|gtm|hotjar|doubleclick|facebook/i.test(u.href)) return false;
    return true;
  }

  function push(url, body, key) {
    key = key || url;
    if (sent >= MAX_CAPTURES) return;
    // Clé = URL complète : deux sports sur le même endpoint sont deux
    // découvertes différentes, et il faut les deux.
    if (seen.has(key)) return;
    if (!body || body.length < MIN_BYTES) { rejected.push(['court', url]); return; }
    const t = body.trimStart()[0];
    if (t !== '{' && t !== '[') {            // pas du JSON : pas notre affaire
      rejected.push(['pas JSON', url]);
      return;
    }

    seen.add(key);
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

  // --- WebSocket -----------------------------------------------------------
  // Beaucoup de sportsbooks Digitain poussent les cotes par socket plutôt que
  // par requête. Si l'offre complète arrive par là, aucun `fetch` ne la
  // montrera jamais — et on chercherait un endpoint qui n'existe pas. Mieux
  // vaut regarder que supposer.
  const OrigWS = window.WebSocket;
  if (OrigWS) {
    window.WebSocket = function (url, protocols) {
      const ws = protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
      let n = 0;
      try {
        log('websocket ouvert :', String(url));
        ws.addEventListener('message', (ev) => {
          try {
            if (typeof ev.data !== 'string' || n >= MAX_WS_FRAMES) return;
            n += 1;
            push(String(url), ev.data, `ws:${url}#${n}`);
          } catch (e) { /* ignore */ }
        });
      } catch (e) { /* ignore */ }
      return ws;
    };
    window.WebSocket.prototype = OrigWS.prototype;
    Object.assign(window.WebSocket, OrigWS);
  }

  // Diagnostic à la demande. Un silence a plusieurs causes possibles — le
  // script pas injecté dans l'iframe, les appels filtrés, les cotes en
  // socket — et sans ça on ne peut pas les distinguer.
  window.__vb = () => ({
    contexte: window.top === window ? 'page principale' : 'IFRAME',
    origine: location.origin,
    chemin: location.pathname,
    envoyes: sent,
    urls: [...seen],
    ecartes: rejected.slice(-40),
  });

  log(window.top === window ? 'PAGE PRINCIPALE' : 'IFRAME', location.origin,
      '| navigue : football complet, puis tennis | bilan : __vb()');
})();
