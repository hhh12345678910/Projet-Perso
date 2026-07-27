// ==UserScript==
// @name         WebSocket spy
// @namespace    valuebet.local
// @version      1.0.0
// @description  Capture les trames WebSocket d'un book, pour les books qui ne servent pas leurs cotes en HTTP.
// @match        https://www.bet777.be/*
// @match        https://www.magicbetting.be/*
// @match        https://www.circus.be/*
// @grant        none
// @run-at       document-start
// @noframes
// ==/UserScript==

// Le pendant de betano-spy pour les books temps reel. Certaines plateformes
// (Gaming1 / Ardent par exemple) ne font transiter AUCUNE cote en HTTP : tout
// passe par une WebSocket, ce qui rend un export HAR vide de donnees utiles —
// Chrome n'y ecrit pas les trames.
//
// Colle-le, ouvre une page de cotes, laisse tourner quelques secondes, puis
// clique le bandeau pour telecharger. Le fichier contient le protocole complet
// (handshake, abonnements, mises a jour) : de quoi ecrire le scraper.
//
// Lecture seule : rien n'est envoye nulle part, le fichier reste sur ta machine.

(function () {
  "use strict";

  const MAX = 4000;              // garde-fou memoire : un flux live est bavard
  const frames = [];
  const Native = window.WebSocket;
  if (!Native) return;

  function log(dir, url, data) {
    if (frames.length >= MAX) return;
    let body = data;
    if (body instanceof ArrayBuffer) body = "<binary " + body.byteLength + " octets>";
    else if (body && body.byteLength !== undefined) body = "<binary " + body.byteLength + " octets>";
    else body = String(body);
    frames.push({ t: Date.now(), dir: dir, url: url, len: body.length, body: body.slice(0, 20000) });
    paint();
  }

  window.WebSocket = function (url, protocols) {
    const ws = protocols === undefined ? new Native(url) : new Native(url, protocols);
    log("OPEN", url, "");
    ws.addEventListener("message", function (ev) { log("RECV", url, ev.data); });
    const send = ws.send;
    ws.send = function (data) { log("SEND", url, data); return send.apply(ws, arguments); };
    return ws;
  };
  window.WebSocket.prototype = Native.prototype;
  ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k, i) {
    window.WebSocket[k] = i;
  });

  let el;
  function paint() {
    if (!document.body) return;
    if (!el) {
      el = document.createElement("div");
      el.style.cssText = [
        "position:fixed", "bottom:12px", "right:12px", "z-index:2147483647",
        "background:#4a148c", "color:#fff", "font:bold 13px system-ui,sans-serif",
        "padding:10px 14px", "border-radius:8px", "cursor:pointer",
      ].join(";");
      el.onclick = function () {
        const blob = new Blob([JSON.stringify(frames, null, 1)], { type: "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = location.hostname + "-websocket.json";
        a.click();
      };
      document.body.appendChild(el);
    }
    el.textContent = "🔌 " + frames.length + " trames — cliquer pour télécharger";
  }

  document.addEventListener("DOMContentLoaded", paint);
})();
