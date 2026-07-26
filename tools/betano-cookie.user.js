// ==UserScript==
// @name         Betano → Valuebet cookie push
// @namespace    valuebet.local
// @version      2.0.0
// @description  Send the Betano session cookie (cf_clearance + datadome) to the Valuebet VM so the daemon can fetch odds itself. Replaces the manual cookie pasting.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-idle
// @noframes
// ==/UserScript==

// Why push the cookie instead of the odds:
//   - ~0.5 KB per push instead of a multi-MB overview dump.
//   - This script never calls Betano's API, so there is no extra traffic for
//     DataDome to score — it only reads cookies the browser already holds.
//   - The VM fetches through BetanoScraper, the path that already works when a
//     cookie is pasted by hand. We're only automating the pasting.
//   - Odds freshness becomes the daemon's cycle (~1 min), independent of how
//     often this pushes.

(function () {
  "use strict";

  // ─── CONFIG ──────────────────────────────────────────────────────────────
  const VPS_URL = "http://34.59.193.111:8787/ingest-cookie";
  const TOKEN = "PASTE_TOKEN_HERE";
  // The cookie lives for hours, so pushing every 5 min is ample and keeps a
  // fresh one landing well before the old expires.
  const INTERVAL_MS = 5 * 60 * 1000;
  // ─────────────────────────────────────────────────────────────────────────

  const LOG = "[betano-cookie]";
  // Cookies the danae-webapi actually gates on. Anything else the browser
  // holds (analytics, prefs) is noise we don't need to send.
  const WANTED = ["cf_clearance", "datadome"];

  // ── status banner ────────────────────────────────────────────────────────
  // Deliberately large and fixed to the top: the whole point is that the
  // status is readable at a glance, with no devtools console involved.
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

  // ── read cookies ─────────────────────────────────────────────────────────
  // document.cookie only — deliberately no GM_cookie. Requesting that grant
  // made Tampermonkey decline to run the script at all (silently: no banner,
  // no log, nothing reached the server), which is far worse than the one thing
  // it buys us. HttpOnly cookies are therefore invisible here; whether that
  // matters depends on which tokens the API actually gates on, and the server
  // logs which ones arrived so we can tell.
  //
  // Send every cookie rather than filtering to WANTED: the payload is still
  // ~1 KB, and replaying the browser's full jar is closer to what the real
  // page sends than a hand-picked subset.
  function readCookies() {
    return document.cookie
      .split(";")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  // ── push ─────────────────────────────────────────────────────────────────
  function send(body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: VPS_URL,
        headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
        data: JSON.stringify(body),
        timeout: 20000,
        onload: (r) =>
          r.status >= 200 && r.status < 300
            ? resolve(r)
            : reject(new Error("VM a répondu " + r.status + " — " + r.responseText)),
        onerror: () => reject(new Error("VM injoignable (service arrêté ? pare-feu ?)")),
        ontimeout: () => reject(new Error("VM ne répond pas (timeout 20s)")),
      });
    });
  }

  async function tick() {
    const pairs = readCookies();
    const names = pairs.map((p) => p.split("=")[0]);
    try {
      // Always POST, even with an empty jar: the server logs the note, so a
      // "script ran but found no cookies" state is visible on the VM instead
      // of looking identical to "script never ran".
      await send({
        cookie: pairs.join("; "),
        user_agent: navigator.userAgent,
        note: "v3 document.cookie, " + pairs.length + " cookies: " + names.join(","),
      });
      const missing = WANTED.filter((w) => names.indexOf(w) === -1);
      const when = new Date().toLocaleTimeString();
      if (missing.length) {
        banner(
          "⚠️ BETANO → VM : envoyé à " + when + " (" + pairs.length + " cookies)\n" +
          "manquant : " + missing.join(", "),
          "#ef6c00"
        );
      } else {
        ok("cookie envoyé à " + when + " (" + pairs.length + " cookies) — tout est bon 🎉");
      }
      console.log(LOG, "pushed", names);
    } catch (e) {
      err(e.message);
      console.warn(LOG, e.message);
    }
  }

  if (TOKEN.indexOf("PASTE_TOKEN") !== -1 || TOKEN.length < 16) {
    err("token non configuré — édite le script et colle BETANO_INGEST_TOKEN.");
    return;
  }

  info("démarrage…");
  tick();
  setInterval(tick, INTERVAL_MS);
})();
