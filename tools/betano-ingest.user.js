// ==UserScript==
// @name         Betano → Valuebet odds push
// @namespace    valuebet.local
// @version      3.0.0
// @description  Push Betano's live overview and per-sport prematch offer to the Valuebet VM from a browser session DataDome trusts.
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
  // 15 s. The full overview measures ~430 KB (163 events / 1239 selections),
  // not the multi-MB first assumed, so this is a light upload and roughly the
  // cadence Betano's own page refreshes live odds at — fast enough that the
  // daemon's cycle, not this, is what bounds freshness. The banner reports the
  // size each cycle if it ever grows.
  const INTERVAL_MS = 15 * 1000;
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

  // ── endpoint discovery ───────────────────────────────────────────────────
  // Kept after it did its job (it's how the prematch API was found on /fr/api
  // rather than danae-webapi): Betano ships new endpoints periodically, and a
  // passive record of what the page calls is cheap. Note the deliberate use of
  // origFetch for our own requests below, so the script doesn't log itself.
  const seenUrls = new Set();
  let pendingUrls = [];
  function noteUrl(u) {
    try {
      const s = String(u);
      if (s.indexOf("danae-webapi") === -1) return;
      // Collapse the volatile parts so a polling endpoint doesn't flood the
      // log with one entry per contentVersion.
      const key = s.split("?")[0].replace(/\/\d{3,}(\/|$)/g, "/{id}$1");
      if (seenUrls.has(key)) return;
      seenUrls.add(key);
      pendingUrls.push(key);
    } catch (e) { /* never let instrumentation break the page */ }
  }

  const origFetch = window.fetch;
  window.fetch = function (input) {
    noteUrl(input && input.url ? input.url : input);
    return origFetch.apply(this, arguments);
  };
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    noteUrl(url);
    return origOpen.apply(this, arguments);
  };

  function flushUrls() {
    if (!pendingUrls.length) return;
    const batch = pendingUrls;
    pendingUrls = [];
    GM_xmlhttpRequest({
      method: "POST",
      url: VPS_URL.replace("/ingest", "/discover"),
      headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
      data: JSON.stringify({ urls: batch }),
      timeout: 15000,
      onload: () => {},
      // Discovery is a side quest; if it fails, put the URLs back and let the
      // next cycle retry rather than losing them or surfacing an error that
      // would be mistaken for an odds-push failure.
      onerror: () => { pendingUrls = batch.concat(pendingUrls); },
      ontimeout: () => { pendingUrls = batch.concat(pendingUrls); },
    });
  }

  let running = false;
  // Set by the prematch cycle; shown on the live banner so one glance covers
  // both feeds.
  let lastPrematch = "";

  async function tick() {
    if (running) return; // never let a slow upload overlap the next cycle
    running = true;
    try {
      info("récupération des cotes…");
      const res = await origFetch(OVERVIEW_URL, {
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
        "live envoyé à " + new Date().toLocaleTimeString() + " — " + mb(text.length) +
        "\n" + c.events + " matchs, " + c.selections + " cotes" +
        (lastPrematch ? "\nprématch : " + lastPrematch : "")
      );
      console.log(LOG, "pushed", c, text.length);
    } catch (e) {
      err(e.message);
      console.warn(LOG, e.message);
    } finally {
      running = false;
      flushUrls();
    }
  }

  // ── prematch offer ───────────────────────────────────────────────────────
  // A separate API from the live overview, one URL per sport. This is where
  // the fixtures the engine actually prices live — the live feed is in-play
  // only (measured: 165 of 172 events already started).
  //
  // The `req` parameter differs between sports in the captured traffic
  // (football omits the leading `la,`), and it's not clear which part is
  // load-bearing, so try the observed variant first and fall back.
  // Only sports the daemon actually scans. Volleyball and basketball are out:
  // Pinnacle prices 2 volleyball events, so no fair line exists for it however
  // many Betano offers.
  const PREMATCH = [
    ["soccer", "le-football"],
    ["tennis", "tennis"],
  ];
  const REQ_VARIANTS = ["la,s,stnf,c,mb", "s,stnf,c,mb"];
  // A full sweep takes ~40 s (one request per competition, spaced), and
  // prematch prices move slowly, so there's no reason to run it often.
  const PREMATCH_INTERVAL_MS = 5 * 60 * 1000;
  // matchs-a-venir only returns roughly the next 24 h — measured: Betano's
  // horizon was J+0 while every other book reached J+35 to J+84. Each of its
  // blocks carries the competition's own url, and fetching that returns the
  // competition's full calendar. Iterating them is what lifts Betano from a
  // one-day window to the coverage the other books already have.
  const MAX_COMPETITIONS = 150;   // guard against an unexpectedly huge list
  const COMPETITION_DELAY_MS = 300;

  function sendTo(url, text) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: url,
        headers: { "Content-Type": "application/json", "X-Ingest-Token": TOKEN },
        data: text,
        timeout: 120000,
        onload: (r) =>
          r.status >= 200 && r.status < 300
            ? resolve(r)
            : reject(new Error("VM " + r.status + " " + r.responseText)),
        onerror: () => reject(new Error("VM injoignable")),
        ontimeout: () => reject(new Error("timeout")),
      });
    });
  }

  async function getJson(url) {
    for (const req of REQ_VARIANTS) {
      try {
        const res = await origFetch(url + "?req=" + req, {
          method: "GET",
          credentials: "include",
          headers: { Accept: "application/json, text/plain, */*" },
        });
        if (res.ok) return await res.json();
      } catch (e) { /* try the next variant */ }
    }
    return null;
  }

  // Merge every response into one payload shaped like a single
  // matchs-a-venir: the parser already handles data.blocks[].events[], so
  // nothing server-side needs to know this came from many requests.
  // Competitions overlap (a fixture can appear in both the 24 h list and its
  // own competition), hence the dedup by event id.
  function mergeBlocks(payloads) {
    const byBlock = new Map();
    for (const p of payloads) {
      const blocks = (p && p.data && p.data.blocks) || [];
      for (const b of blocks) {
        if (!b) continue;
        const key = String(b.id || b.name || Math.random());
        let target = byBlock.get(key);
        if (!target) {
          target = Object.assign({}, b, { events: [], _seen: new Set() });
          byBlock.set(key, target);
        }
        for (const ev of (b.events || [])) {
          const id = String(ev && ev.id);
          if (!id || target._seen.has(id)) continue;
          target._seen.add(id);
          target.events.push(ev);
        }
      }
    }
    const blocks = [];
    let events = 0;
    for (const b of byBlock.values()) {
      delete b._seen;
      events += b.events.length;
      blocks.push(b);
    }
    return { payload: { data: { blocks: blocks } }, blocks: blocks.length, events: events };
  }

  let pmRunning = false;
  async function tickPrematch() {
    if (pmRunning) return;
    pmRunning = true;
    const done = [];
    try {
      for (const [sport, slug] of PREMATCH) {
        // The 24 h list doubles as the competition index: its blocks carry the
        // per-competition urls we then walk for the full calendar.
        const seed = await getJson("/fr/api/sport/" + slug + "/matchs-a-venir/");
        if (!seed) { done.push(sport + ":✗"); continue; }

        const urls = [];
        for (const b of ((seed.data && seed.data.blocks) || [])) {
          if (b && b.url && urls.indexOf(b.url) === -1) urls.push(b.url);
        }

        const payloads = [seed];
        const budget = urls.slice(0, MAX_COMPETITIONS);
        for (let i = 0; i < budget.length; i++) {
          info("prématch " + sport + " : compétition " + (i + 1) + "/" + budget.length + "…");
          const p = await getJson("/fr/api" + budget[i]);
          if (p) payloads.push(p);
          await new Promise((r) => setTimeout(r, COMPETITION_DELAY_MS));
        }

        const merged = mergeBlocks(payloads);
        const text = JSON.stringify(merged.payload);
        try {
          await sendTo(
            VPS_URL.replace("/ingest", "/ingest-prematch") + "?sport=" + sport,
            text
          );
          done.push(sport + ":" + merged.events + "ev/" + budget.length + "comp");
        } catch (e) {
          // 422 = Betano returned no fixtures at all for this sport. Normal
          // overnight or off-season, and the server deliberately keeps the
          // previous file rather than blanking it.
          done.push(sport + (/\b422\b/.test(e.message) ? ":vide" : ":✗"));
        }
      }
      console.log(LOG, "prematch", done.join(" "));
      lastPrematch = done.join("  ");
    } finally {
      pmRunning = false;
    }
  }

  if (TOKEN.indexOf("PASTE_TOKEN") !== -1 || TOKEN.length < 16) {
    err("token non configuré — édite le script.");
    return;
  }

  info("démarrage…");
  tick();
  tickPrematch();
  setInterval(tick, INTERVAL_MS);
  setInterval(tickPrematch, PREMATCH_INTERVAL_MS);
})();
