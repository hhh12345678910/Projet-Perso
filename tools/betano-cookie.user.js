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
  // cf_clearance is HttpOnly, so document.cookie can't see it — and the API
  // returns 403 without it, so GM_cookie (the only API that reads HttpOnly
  // cookies) is required rather than a nice-to-have.
  //
  // GM_cookie's result is merged over document.cookie rather than replacing
  // it: if the grant is unavailable or returns nothing, we still push the
  // visible jar and the banner names what's missing, instead of regressing to
  // sending nothing at all.
  function readCookies() {
    return new Promise((resolve) => {
      const visible = {};
      document.cookie.split(";").forEach((s) => {
        const t = s.trim();
        const i = t.indexOf("=");
        if (i > 0) visible[t.slice(0, i)] = t.slice(i + 1);
      });

      const done = (via) =>
        resolve({
          pairs: Object.keys(visible).map((k) => k + "=" + visible[k]),
          via: via,
        });

      if (typeof GM_cookie === "undefined" || !GM_cookie || !GM_cookie.list) {
        done("document.cookie (GM_cookie indisponible)");
        return;
      }
      let settled = false;
      // Some Tampermonkey builds never invoke the callback when the grant is
      // present but blocked; without this the script would hang silently and
      // look exactly like the failure mode we just spent hours on.
      const guard = setTimeout(() => {
        if (!settled) { settled = true; done("document.cookie (GM_cookie sans réponse)"); }
      }, 5000);
      try {
        GM_cookie.list({ domain: "betanosports.be" }, (cookies, error) => {
          if (settled) return;
          settled = true;
          clearTimeout(guard);
          if (error || !cookies || !cookies.length) {
            done("document.cookie (GM_cookie: " + (error || "vide") + ")");
            return;
          }
          cookies.forEach((c) => { visible[c.name] = c.value; });
          done("GM_cookie (" + cookies.length + " dont HttpOnly)");
        });
      } catch (e) {
        if (!settled) { settled = true; clearTimeout(guard); done("document.cookie (GM_cookie a levé: " + e.message + ")"); }
      }
    });
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
    const { pairs, via } = await readCookies();
    const names = pairs.map((p) => p.split("=")[0]);
    try {
      // Always POST, even with an empty jar: the server logs the note, so a
      // "script ran but found no cookies" state is visible on the VM instead
      // of looking identical to "script never ran".
      await send({
        cookie: pairs.join("; "),
        user_agent: navigator.userAgent,
        note: "v4 via " + via + ", " + pairs.length + " cookies: " + names.join(","),
      });
      const missing = WANTED.filter((w) => names.indexOf(w) === -1);
      const when = new Date().toLocaleTimeString();
      if (missing.length) {
        banner(
          "⚠️ BETANO → VM : envoyé à " + when + " (" + pairs.length + " cookies)\n" +
          "manquant : " + missing.join(", ") + "  —  source : " + via,
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
