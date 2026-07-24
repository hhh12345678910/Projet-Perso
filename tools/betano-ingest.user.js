// ==UserScript==
// @name         Betano → Valuebet ingest
// @namespace    valuebet.local
// @version      1.0.0
// @description  Fetch Betano's live overview from a real browser session (valid cf_clearance + DataDome cookies, home IP) and push it to the Valuebet VM. Keeps Betano fresh with zero manual cookie pasting.
// @match        https://www.betanosports.be/*
// @match        https://betanosports.be/*
// @grant        GM_xmlhttpRequest
// @connect      34.59.193.111
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  "use strict";

  // ─────────────────────────────────────────────────────────────────────────
  // CONFIG — edit these two, then save.
  // ─────────────────────────────────────────────────────────────────────────
  // Your VM's public IP + the ingest port you opened (8787).
  const VPS_URL = "http://34.59.193.111:8787/ingest";
  // MUST match BETANO_INGEST_TOKEN in the VM's .env. Generate with:
  //   openssl rand -hex 32
  const TOKEN = "REPLACE_WITH_YOUR_TOKEN";
  // How often to fetch + push, in milliseconds. 15 s ≈ how often Betano's own
  // page refreshes live odds; going much faster gains nothing and is noisier.
  const INTERVAL_MS = 15000;
  // Random +/- jitter so pushes don't land on a robotic fixed beat.
  const JITTER_MS = 3000;
  // Show a small status badge in the page corner.
  const SHOW_BADGE = true;
  // ─────────────────────────────────────────────────────────────────────────

  // Same endpoint the Betano frontend hits for the initial bulk load. It's
  // same-origin here, so the browser attaches the CF/DataDome cookies itself
  // and there is no CORS problem — this is ordinary site traffic.
  const OVERVIEW_URL =
    "/fr/danae-webapi/api/live/overview/latest" +
    "?includeVirtuals=true&queryLanguageId=9&queryOperatorId=22";

  const LOG = "[betano-ingest]";
  let inFlight = false;
  let lastOk = null;
  let lastErr = null;

  // ── tiny status badge ────────────────────────────────────────────────────
  let badge = null;
  function ensureBadge() {
    if (!SHOW_BADGE || badge) return;
    badge = document.createElement("div");
    badge.style.cssText = [
      "position:fixed", "bottom:10px", "right:10px", "z-index:2147483647",
      "font:12px/1.4 monospace", "padding:6px 9px", "border-radius:6px",
      "background:rgba(0,0,0,.8)", "color:#eee", "pointer-events:none",
      "max-width:280px", "white-space:pre",
    ].join(";");
    document.body.appendChild(badge);
  }
  function setBadge(text, color) {
    if (!SHOW_BADGE) return;
    ensureBadge();
    if (badge) {
      badge.textContent = "Betano→VM " + text;
      badge.style.borderLeft = "4px solid " + (color || "#888");
    }
  }

  // ── push to the VM via the privileged cross-origin request ───────────────
  function push(jsonText, counts) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: VPS_URL,
        headers: {
          "Content-Type": "application/json",
          "X-Ingest-Token": TOKEN,
        },
        data: jsonText,
        timeout: 20000,
        onload: (res) => {
          if (res.status >= 200 && res.status < 300) resolve(res);
          else reject(new Error("VM " + res.status + ": " + res.responseText));
        },
        onerror: () => reject(new Error("VM unreachable (network/CORS/port?)")),
        ontimeout: () => reject(new Error("VM timeout")),
      });
    });
  }

  // ── one fetch + push cycle ───────────────────────────────────────────────
  async function tick() {
    if (inFlight) return; // never overlap two cycles
    inFlight = true;
    try {
      const res = await fetch(OVERVIEW_URL, {
        method: "GET",
        credentials: "include",
        headers: {
          Accept: "application/json, text/plain, */*",
          "x-language": "9",
          "x-operator": "22",
        },
      });
      if (!res.ok) throw new Error("Betano fetch " + res.status +
        (res.status === 403 ? " (Cloudflare/DataDome — reload the page?)" : ""));
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); }
      catch (e) { throw new Error("Betano returned non-JSON (blocked?)"); }

      const counts = {
        events: Object.keys(data.events || {}).length,
        markets: Object.keys(data.markets || {}).length,
        selections: Object.keys(data.selections || {}).length,
      };
      if (!(counts.events || counts.markets || counts.selections))
        throw new Error("empty overview (not logged in / no live events?)");

      await push(text, counts);
      lastOk = new Date();
      lastErr = null;
      console.log(LOG, "pushed", counts, "at", lastOk.toLocaleTimeString());
      setBadge(
        "OK " + lastOk.toLocaleTimeString() + "\nev=" + counts.events +
        " mk=" + counts.markets + " sel=" + counts.selections,
        "#4caf50"
      );
    } catch (e) {
      lastErr = e;
      console.warn(LOG, "cycle failed:", e.message);
      setBadge("ERR " + new Date().toLocaleTimeString() + "\n" + e.message, "#e53935");
    } finally {
      inFlight = false;
    }
  }

  function scheduleNext() {
    const jitter = (Math.random() * 2 - 1) * JITTER_MS;
    setTimeout(async () => {
      await tick();
      scheduleNext();
    }, Math.max(2000, INTERVAL_MS + jitter));
  }

  // Guard against an unset token. Deliberately NOT an equality check against the
  // placeholder string — a find-and-replace of REPLACE_WITH_YOUR_TOKEN would
  // rewrite that literal too and defeat the guard. A real token is a long hex
  // string, so "still contains the placeholder word" / "too short" catches the
  // not-yet-configured case without matching any valid token.
  if (TOKEN.indexOf("REPLACE_WITH") !== -1 || TOKEN.length < 16) {
    console.error(LOG, "TOKEN not set — edit the userscript and paste your BETANO_INGEST_TOKEN.");
    setBadge("TOKEN not set — edit the script", "#ff9800");
    return;
  }

  console.log(LOG, "started, pushing every ~" + (INTERVAL_MS / 1000) + "s to", VPS_URL);
  setBadge("starting…", "#888");
  tick().then(scheduleNext);
})();
