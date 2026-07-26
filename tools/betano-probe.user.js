// ==UserScript==
// @name         Betano → probe prematch endpoints
// @namespace    valuebet.local
// @version      1.0.0
// @description  One-shot: try candidate prematch API paths from the browser and report which ones answer, so fetch_prematch_overview() can be pointed at a confirmed path.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-idle
// @noframes
// ==/UserScript==

// Navigation capture only surfaces what the SPA happens to call, and it caches
// aggressively — so a prematch page may never re-request anything. Probing is
// the complement: ask the API directly, from the browser DataDome trusts, and
// let the VM log which paths answer.
//
// Run once, read the log, then disable this script.

(function () {
  "use strict";

  const REPORT_URL = "http://34.59.193.111:8787/discover";
  const TOKEN = "PASTE_TOKEN_HERE";

  const API = "/fr/danae-webapi/api";
  const Q = "?includeVirtuals=true&queryLanguageId=9&queryOperatorId=22";

  // `/live/overview/{v}` and `/layout/live` are the two confirmed shapes, so
  // the candidates are those two patterns with `live` swapped for every plausible
  // prematch synonym, plus a couple of standalone guesses.
  const SECTIONS = ["prematch", "sport", "sports", "upcoming", "sportmarket", "offer", "coupon"];
  const CANDIDATES = [];
  SECTIONS.forEach((s) => {
    CANDIDATES.push(API + "/" + s + "/overview/latest" + Q);
    CANDIDATES.push(API + "/layout/" + s);
  });
  CANDIDATES.push(API + "/overview/latest" + Q);
  CANDIDATES.push(API + "/layout/live");

  const results = [];

  function report(lines, done) {
    GM_xmlhttpRequest({
      method: "POST",
      url: REPORT_URL,
      headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
      data: JSON.stringify({ urls: lines }),
      timeout: 20000,
      onload: done,
      onerror: done,
      ontimeout: done,
    });
  }

  function banner(text, bg) {
    let el = document.getElementById("betano-probe-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "betano-probe-banner";
      el.style.cssText = [
        "position:fixed", "bottom:0", "left:0", "right:0", "z-index:2147483646",
        "font:bold 14px/1.5 system-ui,sans-serif", "padding:10px 14px",
        "color:#fff", "text-align:center", "white-space:pre-wrap",
      ].join(";");
      document.body.appendChild(el);
    }
    el.style.background = bg;
    el.textContent = text;
  }

  async function probe(url) {
    try {
      const res = await fetch(url, {
        method: "GET",
        credentials: "include",
        headers: {
          Accept: "application/json, text/plain, */*",
          "x-language": "9",
          "x-operator": "22",
        },
      });
      let detail = "";
      if (res.ok) {
        const text = await res.text();
        try {
          const d = JSON.parse(text);
          // Report the counts that decide whether a path is actually usable:
          // parse_overview() needs events+markets+selections to be present.
          detail =
            " events=" + Object.keys(d.events || {}).length +
            " markets=" + Object.keys(d.markets || {}).length +
            " selections=" + Object.keys(d.selections || {}).length +
            " bytes=" + text.length;
        } catch (e) {
          detail = " (non-JSON, " + text.length + " bytes)";
        }
      }
      return "PROBE " + res.status + " " + url.split("?")[0] + detail;
    } catch (e) {
      return "PROBE ERR " + url.split("?")[0] + " " + e.message;
    }
  }

  async function run() {
    if (TOKEN.indexOf("PASTE_TOKEN") !== -1) {
      banner("token non configuré dans le script de sondage", "#c62828");
      return;
    }
    for (let i = 0; i < CANDIDATES.length; i++) {
      banner("🔎 sondage " + (i + 1) + "/" + CANDIDATES.length + "…", "#455a64");
      const line = await probe(CANDIDATES[i]);
      results.push(line);
      console.log("[betano-probe]", line);
      // Space the requests out: a burst of unusual paths is exactly the shape
      // DataDome scores, and there's no hurry for a one-shot discovery run.
      await new Promise((r) => setTimeout(r, 1200));
    }
    banner("📤 envoi des résultats…", "#455a64");
    report(results, () =>
      banner("✅ sondage terminé — lis le log de la VM, puis désactive ce script", "#2e7d32")
    );
  }

  run();
})();
