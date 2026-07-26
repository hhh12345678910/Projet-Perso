// ==UserScript==
// @name         Bet history spy
// @namespace    valuebet.local
// @version      1.0.0
// @description  Record the API calls a bookmaker's "my bets" pages make, so the settlement history can be pulled into the tracker instead of retyped by hand.
// @match        https://*.unibet.be/*
// @match        https://*.ladbrokes.be/*
// @match        https://*.starcasino.be/*
// @match        https://*.napoleonsports.be/*
// @match        https://*.betanosports.be/*
// @match        https://*.betfirst.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-start
// @noframes
// ==/UserScript==

// Same technique that found Betano's prematch API, pointed at account pages
// instead. Browse to "Mes paris" / "Historique" and the endpoint serving the
// settled bets shows up in the VM log, ready to be pulled properly.
//
// This reads your own account data — the history you could copy by hand — and
// nothing else. It only records URLs; no response bodies, no credentials.
// Run it, read the log, then disable it.

(function () {
  "use strict";

  const REPORT_URL = "http://34.59.193.111:8787/discover";
  const TOKEN = "PASTE_TOKEN_HERE";

  // Static assets are noise. Everything else is reported, because the whole
  // point is that we don't know what the history endpoint is called.
  const SKIP = /\.(js|mjs|css|png|jpe?g|gif|svg|webp|avif|woff2?|ttf|eot|ico|map)(\?|$)/i;
  // Query strings are stripped here, unlike the odds spy: account URLs carry
  // session ids and account numbers, and those have no business leaving the
  // browser for a log file.
  const seen = new Set();
  let pending = [];

  function note(url, how) {
    try {
      const raw = String(url);
      if (!raw || SKIP.test(raw)) return;
      const key = how + " " + raw.split("?")[0].replace(/\/\d{4,}(\/|$)/g, "/{id}$1");
      if (seen.has(key)) return;
      seen.add(key);
      pending.push(location.hostname + "  " + key);
    } catch (e) { /* instrumentation must never break the page */ }
  }

  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input) {
      note(input && input.url ? input.url : input, "FETCH");
      return origFetch.apply(this, arguments);
    };
  }
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    note(url, "XHR:" + method);
    return origOpen.apply(this, arguments);
  };

  function flush() {
    if (!pending.length) return;
    const batch = pending;
    pending = [];
    GM_xmlhttpRequest({
      method: "POST",
      url: REPORT_URL,
      headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
      data: JSON.stringify({ urls: batch }),
      timeout: 20000,
      onload: show,
      onerror: function () { pending = batch.concat(pending); },
      ontimeout: function () { pending = batch.concat(pending); },
    });
  }

  function show() {
    if (!document.body) return;
    let el = document.getElementById("history-spy-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "history-spy-banner";
      el.style.cssText = [
        "position:fixed", "bottom:0", "left:0", "right:0", "z-index:2147483645",
        "font:bold 13px/1.4 system-ui,sans-serif", "padding:8px 12px",
        "background:#00695c", "color:#fff", "text-align:center",
      ].join(";");
      document.body.appendChild(el);
    }
    el.textContent = "🕵️ HISTORY SPY : " + seen.size +
      " URL capturées — ouvre « Mes paris » puis l'historique réglé";
  }

  if (TOKEN.indexOf("PASTE_TOKEN") === -1) {
    setTimeout(flush, 3000);
    setInterval(flush, 10000);
    document.addEventListener("DOMContentLoaded", show);
  }
})();
