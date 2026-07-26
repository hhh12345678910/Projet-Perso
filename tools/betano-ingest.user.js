// ==UserScript==
// @name         Betano → Valuebet odds push
// @namespace    valuebet.local
// @version      2.0.0
// @description  Fetch Betano's live overview from a real browser session and push it to the Valuebet VM, automating the manual capture the README describes.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-idle
// @noframes
// ==/UserScript==

// Why the browser does the fetching
// --------------------------------
// Pushing the session cookie and letting the VM fetch does NOT work: DataDome
// scores the requesting IP, and the VM's datacenter address gets a 403 even
// with a valid, freshly-minted cookie. (cf_clearance isn't even in play here —
// it's never set on this session; the block is DataDome's, not Cloudflare's.)
//
// So the fetch has to happen from the browser that DataDome already trusts,
// which is exactly the manual capture the README documents. This just does it
// on a timer and ships the result, instead of by hand.

(function () {
  "use strict";

  // ─── CONFIG ──────────────────────────────────────────────────────────────
  const VPS_URL = "http://34.59.193.111:8787/ingest";
  const TOKEN = "PASTE_TOKEN_HERE";
  // 60 s to start. The payload is the full overview, so this is the knob to
  // turn if the upload can't keep up — the banner reports the size each cycle.
  const INTERVAL_MS = 60 * 1000;
  // ─────────────────────────────────────────────────────────────────────────

  const LOG = "[betano-odds]";
  const OVERVIEW_URL =
    "/fr/danae-webapi/api/live/overview/latest" +
    "?includeVirtuals=true&queryLanguageId=9&queryOperatorId=22";

  let el = null;
  function banner(text, bg) {
    if (!el) {
      el = document.createElement("div");
      el.style.cssText = [
        "position:fixed", "top:0", "left:0", "right:0", "z-index:2147483647",
        "font:bold 15px/1.5 system-ui,sans-serif", "padding:12px 16px",
        "color:#fff", "text-align:center", "white-space:pre-wrap",
        "box-shadow:0 2px 10px rgba(0,0,0,.4)",
      ].join(";");
      document.body.appendChild(el);
    }
    el.style.background = bg;
    el.textContent = text;
  }
  const ok = (t) => banner("✅ BETANO → VM : " + t, "#2e7d32");
  const err = (t) => banner("❌ BETANO → VM : " + t, "#c62828");
  const info = (t) => banner("⏳ BETANO → VM : " + t, "#455a64");

  const mb = (n) => (n / 1048576).toFixed(2) + " Mo";

  function send(text) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: VPS_URL,
        headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
        data: text,
        // Generous: a multi-MB upload on a home connection is slow, and a
        // premature timeout here looks identical to a server failure.
        timeout: 120000,
        onload: (r) =>
          r.status >= 200 && r.status < 300
            ? resolve(r)
            : reject(new Error("VM a répondu " + r.status + " — " + r.responseText)),
        onerror: () => reject(new Error("VM injoignable (service arrêté ?)")),
        ontimeout: () => reject(new Error("upload trop lent (>120 s) — augmente INTERVAL_MS")),
      });
    });
  }

  let running = false;
  async function tick() {
    if (running) return; // never let a slow upload overlap the next cycle
    running = true;
    try {
      info("récupération des cotes…");
      const res = await fetch(OVERVIEW_URL, {
        method: "GET",
        credentials: "include",
        headers: {
          Accept: "application/json, text/plain, */*",
          "x-language": "9",
          "x-operator": "22",
        },
      });
      if (!res.ok) {
        throw new Error(
          "Betano a répondu " + res.status +
          (res.status === 403 ? " — DataDome bloque, recharge la page" : "")
        );
      }
      const text = await res.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        throw new Error("Betano n'a pas renvoyé du JSON (page de blocage ?)");
      }
      const c = {
        events: Object.keys(data.events || {}).length,
        markets: Object.keys(data.markets || {}).length,
        selections: Object.keys(data.selections || {}).length,
      };
      if (!(c.events || c.markets || c.selections)) {
        throw new Error("overview vide (aucun match en direct ?)");
      }

      info("envoi de " + mb(text.length) + " vers la VM…");
      await send(text);
      ok(
        "envoyé à " + new Date().toLocaleTimeString() + " — " + mb(text.length) +
        "\n" + c.events + " matchs, " + c.selections + " cotes 🎉"
      );
      console.log(LOG, "pushed", c, text.length);
    } catch (e) {
      err(e.message);
      console.warn(LOG, e.message);
    } finally {
      running = false;
    }
  }

  if (TOKEN.indexOf("PASTE_TOKEN") !== -1 || TOKEN.length < 16) {
    err("token non configuré — édite le script.");
    return;
  }

  info("démarrage…");
  tick();
  setInterval(tick, INTERVAL_MS);
})();
