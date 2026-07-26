// ==UserScript==
// @name         Betano → Valuebet cookie push
// @namespace    valuebet.local
// @version      2.0.0
// @description  Send the Betano session cookie (cf_clearance + datadome) to the Valuebet VM so the daemon can fetch odds itself. Replaces the manual cookie pasting.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @grant        GM_xmlhttpRequest
// @grant        GM_cookie
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
  // cf_clearance is HttpOnly, so document.cookie cannot see it — GM_cookie is
  // the only way to read it. We fall back to document.cookie anyway so the
  // script degrades to "datadome only" instead of dying, and report which
  // names were actually captured.
  function readCookies() {
    return new Promise((resolve) => {
      if (typeof GM_cookie === "undefined" || !GM_cookie || !GM_cookie.list) {
        resolve({ pairs: fromDocument(), via: "document.cookie" });
        return;
      }
      GM_cookie.list({ domain: "betanosports.be" }, (cookies, error) => {
        if (error || !cookies || !cookies.length) {
          resolve({ pairs: fromDocument(), via: "document.cookie (GM_cookie: " + (error || "vide") + ")" });
          return;
        }
        const pairs = cookies
          .filter((c) => WANTED.indexOf(c.name) !== -1)
          .map((c) => c.name + "=" + c.value);
        resolve({ pairs: pairs, via: "GM_cookie" });
      });
    });
  }

  function fromDocument() {
    return document.cookie
      .split(";")
      .map((s) => s.trim())
      .filter((s) => WANTED.some((w) => s.indexOf(w + "=") === 0));
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
    try {
      const { pairs, via } = await readCookies();
      if (!pairs.length) {
        err("aucun cookie trouvé.\nRecharge la page, puis réessaie. (source: " + via + ")");
        return;
      }
      const names = pairs.map((p) => p.split("=")[0]);
      await send({ cookie: pairs.join("; "), user_agent: navigator.userAgent });
      const missing = WANTED.filter((w) => names.indexOf(w) === -1);
      const when = new Date().toLocaleTimeString();
      if (missing.length) {
        banner(
          "⚠️ BETANO → VM : envoyé à " + when + " mais il manque : " + missing.join(", ") +
          "\n(via " + via + " — active GM_cookie dans Tampermonkey pour lire les cookies HttpOnly)",
          "#ef6c00"
        );
      } else {
        ok("cookie envoyé à " + when + " (" + names.join(" + ") + ") — tout est bon 🎉");
      }
      console.log(LOG, "pushed", names, via);
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
