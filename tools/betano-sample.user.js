// ==UserScript==
// @name         Betano → capture prematch samples
// @namespace    valuebet.local
// @version      1.0.0
// @description  One-shot: fetch the /fr/api prematch endpoints and push them to the VM so their shape can be inspected and a parser written.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-idle
// @noframes
// ==/UserScript==

// The spy found the prematch offer on a completely different API from the live
// feed: /fr/api/sport/{slug}/matchs-a-venir, not /fr/danae-webapi. Its payload
// shape is unknown and unrelated to the danae-webapi overview, so capture one
// sample per sport before writing any parser.
//
// Run once, then disable.

(function () {
  "use strict";

  const BASE = "http://34.59.193.111:8787/sample";
  const TOKEN = "PASTE_TOKEN_HERE";

  // Slugs are Betano's own localised URL segments, taken verbatim from the
  // captured calls — they are not derivable from the sport name.
  const TARGETS = [
    ["foot-prematch",   "/fr/api/sport/le-football/matchs-a-venir/?req=la,s,stnf,c,mb"],
    ["tennis-prematch", "/fr/api/sport/tennis/matchs-a-venir/?req=la,s,stnf,c,mb"],
    ["basket-prematch", "/fr/api/sport/basketball/matchs-a-venir/?req=la,s,stnf,c,mb"],
    ["volley-prematch", "/fr/api/sport/volleyball/matchs-a-venir/?req=la,s,stnf,c,mb"],
    ["foot-landing",    "/fr/api/sport/le-football/?req=la,s,stnf,c,mb"],
  ];

  function banner(text, bg) {
    let el = document.getElementById("betano-sample-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "betano-sample-banner";
      el.style.cssText = [
        "position:fixed", "top:0", "left:0", "right:0", "z-index:2147483647",
        "font:bold 15px/1.5 system-ui,sans-serif", "padding:12px 16px",
        "color:#fff", "text-align:center", "white-space:pre-wrap",
      ].join(";");
      document.body.appendChild(el);
    }
    el.style.background = bg;
    el.textContent = text;
  }

  function push(name, text) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: BASE + "?name=" + encodeURIComponent(name),
        headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
        data: text,
        timeout: 60000,
        onload: (r) => resolve(r.status + ""),
        onerror: () => resolve("ERR"),
        ontimeout: () => resolve("TIMEOUT"),
      });
    });
  }

  async function run() {
    const done = [];
    for (let i = 0; i < TARGETS.length; i++) {
      const name = TARGETS[i][0];
      const url = TARGETS[i][1];
      banner("📥 capture " + (i + 1) + "/" + TARGETS.length + " : " + name, "#455a64");
      try {
        const res = await fetch(url, {
          method: "GET",
          credentials: "include",
          headers: { Accept: "application/json, text/plain, */*" },
        });
        const text = await res.text();
        if (!res.ok) {
          done.push(name + ": HTTP " + res.status);
        } else {
          const status = await push(name, text);
          done.push(name + ": " + Math.round(text.length / 1024) + " Ko → VM " + status);
        }
      } catch (e) {
        done.push(name + ": " + e.message);
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
    banner("✅ capture terminée\n" + done.join("\n"), "#2e7d32");
    console.log("[betano-sample]", done);
  }

  run();
})();
