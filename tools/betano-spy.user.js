// ==UserScript==
// @name         Betano → spy (document-start)
// @namespace    valuebet.local
// @version      1.0.0
// @description  Record every danae-webapi call from the very first one, including the prematch fetches the page makes during initial load.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-start
// @noframes
// ==/UserScript==

// The odds script hooks at document-idle, which is too late: the homepage
// already lists prematch fixtures, so whatever endpoint serves them is called
// during initial load and the hook never sees it. Probing synonyms of
// `/live/overview` and `/layout/live` found nothing but 404s, so the prematch
// path doesn't follow that shape and has to be observed rather than guessed.
//
// document-start is the whole point of this script: install before the page's
// own bundles run, so the first request is captured too.

(function () {
  "use strict";

  const REPORT_URL = "http://34.59.193.111:8787/discover";
  const TOKEN = "PASTE_TOKEN_HERE";

  const seen = new Set();
  let pending = [];

  // Static assets only — everything else is reported. The previous version
  // filtered to URLs containing "danae-webapi", which would silently hide a
  // prematch API served from any other path or host; that assumption is
  // exactly what needs testing, so it can't be baked into the filter.
  const SKIP = /\.(js|mjs|css|png|jpe?g|gif|svg|webp|avif|woff2?|ttf|eot|ico|map)(\?|$)/i;

  function note(url, how) {
    try {
      const s = String(url);
      if (!s || SKIP.test(s)) return;
      // Keep the query: the parameters are what distinguish a prematch call
      // from a live one. Only volatile ids are collapsed so one polling
      // endpoint doesn't fill the log.
      const key = how + " " + s.replace(/\/\d{6,}(\/|$)/g, "/{v}$1");
      if (seen.has(key)) return;
      seen.add(key);
      pending.push(key);
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
      onload: function () { show(); },
      onerror: function () { pending = batch.concat(pending); },
      ontimeout: function () { pending = batch.concat(pending); },
    });
  }

  function show() {
    if (!document.body) return;
    let el = document.getElementById("betano-spy-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "betano-spy-banner";
      el.style.cssText = [
        "position:fixed", "bottom:0", "left:0", "right:0", "z-index:2147483645",
        "font:bold 14px/1.4 system-ui,sans-serif", "padding:8px 12px",
        "background:#4527a0", "color:#fff", "text-align:center",
      ].join(";");
      document.body.appendChild(el);
    }
    el.textContent = "🕵️ SPY : " + seen.size + " URL danae-webapi capturées — navigue pour en révéler d'autres";
  }

  if (TOKEN.indexOf("PASTE_TOKEN") === -1) {
    // Flush often at first (the initial-load burst is what we're after), then
    // keep a slower beat for whatever navigation turns up.
    setTimeout(flush, 3000);
    setTimeout(flush, 8000);
    setInterval(flush, 15000);
    document.addEventListener("DOMContentLoaded", show);
  }
})();
