// ==UserScript==
// @name         Pinnacle — test d'IP résidentielle
// @namespace    valuebet
// @version      1.0
// @description  Interroge l'API Pinnacle depuis le navigateur, avec EXACTEMENT les en-têtes du scraper, pour savoir si un 403 sur la VM vient de l'IP ou du quota.
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @connect      guest.api.arcadia.pinnacle.com
// @run-at       document-idle
// ==/UserScript==
//
// POURQUOI CE SCRIPT
// ------------------
// Le §12 pose une hypothèse jamais tranchée : un 403 répété sur une IP de
// datacenter ressemble à un filtrage d'ASN, comme DataDome pour Betano et le
// blocage Gaming1 pour Circus. Un 403 n'est d'ailleurs pas le code d'une
// limitation de débit — ce serait 429.
//
// Le test décisif est de rejouer la MÊME requête depuis une IP résidentielle
// pendant que la VM reçoit un 403.
//
//   VM 403 + maison 200  -> filtrage d'IP/ASN. Aucun espacement n'y changera
//                           rien ; il faudra un pont navigateur comme pour
//                           Betano, ou PINNACLE_PROXY / PINNACLE_LOCAL_IP.
//   VM 403 + maison 403  -> quota lié à la clé d'API publique. L'espacement
//                           est alors le bon levier.
//
// POURQUOI PAS SIMPLEMENT OUVRIR L'URL
// ------------------------------------
// Un navigateur qui navigue vers une URL d'API n'envoie ni X-API-Key, ni
// Origin, ni Referer. Cloudflare le classe robot sur ce seul critère, quelle
// que soit l'IP — le « Sorry, you have been blocked » obtenu ainsi ne prouve
// donc rien. Il faut rejouer les en-têtes du scraper, et GM_xmlhttpRequest est
// le seul moyen de le faire depuis un navigateur sans buter sur le CORS.

(function () {
  "use strict";

  const BASE = "https://guest.api.arcadia.pinnacle.com/0.1";
  // Clé publique du frontend Pinnacle, la même que src/scrapers/pinnacle.py.
  const API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R";
  const SPORTS = { soccer: 29, tennis: 33 };

  // Copie conforme de _headers() dans src/scrapers/pinnacle.py. Toute
  // différence invaliderait la comparaison avec la VM.
  const HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.pinnacle.com/",
    "Origin": "https://www.pinnacle.com",
    "X-API-Key": API_KEY,
    "Accept": "application/json",
  };

  function probe(sport, id) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      GM_xmlhttpRequest({
        method: "GET",
        url: `${BASE}/sports/${id}/matchups`,
        headers: HEADERS,
        timeout: 20000,
        onload: (r) => resolve({ sport, status: r.status, ms: Date.now() - t0,
                                 body: (r.responseText || "").slice(0, 120) }),
        onerror: () => resolve({ sport, status: "ERREUR RÉSEAU", ms: Date.now() - t0, body: "" }),
        ontimeout: () => resolve({ sport, status: "TIMEOUT", ms: Date.now() - t0, body: "" }),
      });
    });
  }

  async function run() {
    console.log("%c[Pinnacle] test depuis cette IP…", "font-weight:bold");
    const out = [];
    for (const [sport, id] of Object.entries(SPORTS)) {
      const r = await probe(sport, id);
      out.push(r);
      console.log(`[Pinnacle] ${sport.padEnd(8)} HTTP ${r.status}  ${r.ms} ms`);
      console.log(`           ${r.body}`);
    }
    const codes = out.map((r) => `${r.sport}=${r.status}`).join("  ");
    const ok = out.every((r) => r.status === 200);
    alert(
      "Pinnacle depuis CETTE IP :\n\n" + codes +
      (ok
        ? "\n\n=> 200. Si la VM recoit un 403 au meme moment,\n   c'est un filtrage d'IP/ASN."
        : "\n\n=> pas 200. Si la VM aussi, c'est le quota de la cle d'API.")
    );
  }

  GM_registerMenuCommand("🎯 Tester Pinnacle depuis cette IP", run);
})();
