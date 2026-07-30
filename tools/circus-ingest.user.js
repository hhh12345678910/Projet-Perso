// ==UserScript==
// @name         Circus ingest (Gaming1)
// @namespace    valuebet.local
// @version      1.0.0
// @description  Ouvre sa propre WebSocket vers Circus, réclame le prématch football et tennis et le pousse vers la VM.
// @match        *://*.circus-sport.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-idle
// @noframes
// ==/UserScript==

// Pourquoi un pont navigateur
// ---------------------------
// Gaming1 refuse l'IP de la VM sur TOUT le domaine, pas seulement sur l'API :
// même la page d'accueil répond 403. Ce n'est pas un géoblocage (une IP Google
// Cloud belge est refusée aussi), c'est l'ASN datacenter. Il faut donc un vrai
// navigateur sur IP résidentielle, comme pour Betano.
//
// Différence avec betano-ingest : ici on n'espionne pas la page. Le handshake
// Gaming1 n'exige ni compte, ni token, ni captcha, donc ce script ouvre sa
// PROPRE WebSocket et pilote ses requêtes. Tu n'as rien à cliquer — l'onglet
// peut rester sur n'importe quelle page du site.
//
// Réglage : colle le même jeton que betano-ingest dans TOKEN ci-dessous.

(function () {
  "use strict";

  const TOKEN = "PASTE_TOKEN_HERE";
  const INGEST = "http://34.59.193.111:8787/ingest-circus";

  const WSS = "wss://wss02.circus-sport.be";
  const ROOM = "CIRCUS";          // Bet777 : "777BE" — seule chose à changer
  const VERSION = "0.209.0";
  // SportId lus dans la réponse GetSports de Circus. Ajouter un sport ici
  // suppose de l'ajouter aussi dans CIRCUS_SPORTS côté src/main.py, sinon le
  // daemon ne lira jamais le fichier poussé.
  const SPORTS = [
    { key: "soccer", id: 844 },
    { key: "tennis", id: 848 },
  ];
  const DAYS = 4;                 // jours de prématch à balayer
  const EVERY_SEC = 30;           // fréquence d'un cycle complet
  const LZS = "--##LZS2##--";

  const uuid = () =>
    (crypto.randomUUID ? crypto.randomUUID()
      : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
          const r = (Math.random() * 16) | 0;
          return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
        }));

  const envelope = (msg, type) => JSON.stringify({
    Id: uuid(), Message: JSON.stringify(msg), MessageType: type, TTL: 10,
  });

  const request = (identifier, type, content) => envelope({
    Direction: 0, Id: uuid(), Groups: [],
    Requests: [{ Id: uuid(), Type: type, Identifier: identifier, Content: JSON.stringify(content) }],
  }, 1000);

  // Décompression lz-string (decompressFromUTF16), portage minimal. Circus
  // compresse ses grosses réponses ; Bet777 non. Le marqueur peut préfixer soit
  // la trame entière, soit seulement un champ à l'intérieur — les deux cas
  // arrivent, et n'en traiter qu'un fait échouer la moitié des trames.
  function lzDecompress(input) {
    if (!input) return null;
    const get = (i) => input.charCodeAt(i) - 32;
    const reset = 16384;
    const dict = { 0: 0, 1: 1, 2: 2 };
    let enlargeIn = 4, dictSize = 4, numBits = 3, val = get(0), pos = reset, index = 1;
    const result = [];

    function read(maxpower) {
      let bits = 0, power = 1;
      while (power !== maxpower) {
        const resb = val & pos;
        pos >>= 1;
        if (pos === 0) { pos = reset; val = get(index++); }
        bits |= (resb > 0 ? 1 : 0) * power;
        power <<= 1;
      }
      return bits;
    }

    let c;
    switch (read(4)) {
      case 0: c = String.fromCharCode(read(256)); break;
      case 1: c = String.fromCharCode(read(65536)); break;
      case 2: return "";
      default: return null;
    }
    dict[3] = c;
    let w = c;
    result.push(c);

    for (;;) {
      if (index > input.length) return "";
      let cc = read(1 << numBits);
      switch (cc) {
        case 0: dict[dictSize++] = String.fromCharCode(read(256)); cc = dictSize - 1; enlargeIn--; break;
        case 1: dict[dictSize++] = String.fromCharCode(read(65536)); cc = dictSize - 1; enlargeIn--; break;
        case 2: return result.join("");
      }
      if (enlargeIn === 0) { enlargeIn = 1 << numBits; numBits++; }

      let entry;
      if (dict[cc] !== undefined) entry = dict[cc];
      else if (cc === dictSize) entry = w + w.charAt(0);
      else return null;

      result.push(entry);
      dict[dictSize++] = w + entry.charAt(0);
      enlargeIn--;
      w = entry;
      if (enlargeIn === 0) { enlargeIn = 1 << numBits; numBits++; }
    }
  }

  const unwrap = (s) =>
    (typeof s === "string" && s.startsWith(LZS)) ? lzDecompress(s.slice(LZS.length)) : s;

  function decodeFrame(raw) {
    let body = unwrap(raw);
    let env;
    try { env = JSON.parse(body); } catch (e) { return null; }
    let msg = unwrap(env.Message);
    let inner;
    try { inner = (typeof msg === "string") ? JSON.parse(msg) : msg; } catch (e) { return null; }
    for (const r of (inner && inner.Requests) || []) r.Content = unwrap(r.Content);
    return inner;
  }

  const localDate = (offset) => {
    const d = new Date();
    d.setDate(d.getDate() + offset);
    return d.toISOString().slice(0, 10) + "T00:00:00Z";
  };

  function push(sport, blocks) {
    GM_xmlhttpRequest({
      method: "POST",
      url: INGEST + "?sport=" + encodeURIComponent(sport),
      headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
      data: JSON.stringify({ blocks: blocks }),
      timeout: 30000,
      onload: (r) => {
        state[sport] = (r.status === 200) ? blocks.length + "j" : "HTTP " + r.status;
        paint();
      },
      onerror: () => { state[sport] = "envoi KO"; paint("#b71c1c"); },
      ontimeout: () => { state[sport] = "délai VM"; paint("#b71c1c"); },
    });
  }

  const state = {};
  let banner;
  function paint(colour) {
    if (!document.body) return;
    if (!banner) {
      banner = document.createElement("div");
      banner.style.cssText = [
        "position:fixed", "bottom:12px", "right:12px", "z-index:2147483647",
        "color:#fff", "font:bold 13px system-ui,sans-serif",
        "padding:10px 14px", "border-radius:8px",
      ].join(";");
      document.body.appendChild(banner);
    }
    banner.style.background = colour || "#1b5e20";
    banner.textContent = "🎰 Circus  " +
      SPORTS.map((sp) => sp.key + " " + (state[sp.key] || "…")).join("  |  ");
  }

  // Une seule WebSocket, gardée ouverte. Rouvrir toutes les 30 s referait le
  // handshake à chaque cycle — plus lourd pour le serveur, et un bon moyen de
  // se faire remarquer. Une reconnexion n'a lieu que si la connexion tombe.
  let ws = null;
  let ready = false;
  let pending = {};       // sport -> blocs reçus en cours de cycle

  function connect() {
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    ready = false;
    try { ws = new WebSocket(WSS); } catch (e) { paint("#b71c1c"); return; }

    ws.onopen = () => {
      ws.send(envelope({
        NodeType: 1, Identity: uuid(), SupportedCompressions: "LZS2",
        ClientInformations: {
          AppName: "Front;Registration-Origin: empty", ClientType: "React",
          Version: VERSION, UserAgent: navigator.userAgent,
          LanguageCode: "fr", RoomDomainName: ROOM,
        },
      }, 1));
      ready = true;
      setTimeout(cycle, 500);
    };

    ws.onmessage = (e) => {
      const inner = decodeFrame(e.data);
      if (!inner) return;
      for (const r of (inner.Requests || [])) {
        if (r.Identifier !== "GetPrematchSport" || !r.Content) continue;
        let block;
        try { block = JSON.parse(r.Content); } catch (err) { continue; }
        // La réponse ne rappelle pas le sport demandé : on l'attribue au
        // premier sport encore en attente, les requêtes étant sérialisées.
        const sport = Object.keys(pending).find((k) => pending[k].left > 0);
        if (!sport) continue;
        pending[sport].blocks.push(block);
        if (--pending[sport].left <= 0) {
          push(sport, pending[sport].blocks);
          delete pending[sport];
        }
      }
    };

    ws.onclose = () => { ready = false; setTimeout(connect, 5000); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  function cycle() {
    if (!ready || !ws || ws.readyState !== 1) { connect(); return; }
    // Un cycle précédent encore en vol : on saute plutôt que d'empiler des
    // requêtes dont les réponses seraient attribuées au mauvais sport.
    if (Object.keys(pending).length) return;

    let delay = 0;
    for (const sp of SPORTS) {
      pending[sp.key] = { left: DAYS, blocks: [] };
      for (let d = 0; d < DAYS; d++) {
        setTimeout(() => {
          if (!ws || ws.readyState !== 1) return;
          ws.send(request("GetPrematchSport", 201, {
            locale: "fr", isUserLoggedIn: false, SportId: sp.id,
            DiffMinUtc: -new Date().getTimezoneOffset(),
            Filter: { Type: 2, LocalDate: localDate(d) },
          }));
        }, delay);
        delay += 250;
      }
    }
    // Filet : une réponse perdue ne doit pas figer les cycles suivants.
    setTimeout(() => { pending = {}; }, EVERY_SEC * 1000 - 2000);
  }

  if (TOKEN.indexOf("PASTE_TOKEN") !== -1) {
    document.addEventListener("DOMContentLoaded", () => paint("#b71c1c"));
    return;
  }
  paint("#f57c00");
  connect();
  setInterval(cycle, EVERY_SEC * 1000);
})();
