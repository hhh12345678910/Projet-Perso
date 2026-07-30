// Rejoue circus-ingest.user.js dans un faux navigateur pour vérifier
// l'attribution des réponses quand elles reviennent dans le désordre.
//
//   node tools/circus-ingest.selftest.js tools/circus-ingest.user.js
//
// Pourquoi ce fichier existe : l'attribution des réponses s'est trompée deux
// fois en production — fichiers croisés, puis fichiers mélangés — et aucun
// test ne la couvrait, le reste du projet étant en Python. Les scénarios
// ci-dessous reproduisent exactement ce qui a été observé. À rejouer après
// toute modification du userscript.
const fs = require("fs");


const SRC = process.argv[2];
const SPORT_ID = { soccer: 844, tennis: 848 };

let src = fs.readFileSync(SRC, "utf8");
src = src.replace('const TOKEN = "PASTE_TOKEN_HERE";', 'const TOKEN = "t";');
// Temps compressé, sinon le harness attendrait 30 s entre deux scénarios et
// 20 s de plus pour qu'un cycle laissé sans réponse se clôture.
src = src.replace(/const EVERY_SEC = [\d.]+;/, "const EVERY_SEC = 0.3;");
src = src.replace(/const CYCLE_TIMEOUT_MS = \d+;/, "const CYCLE_TIMEOUT_MS = 250;");
src = src.replace(/delay \+= \d+;/, "delay += 2;");

const pushes = [];      // {sport, blocks}
let sock = null;

global.crypto = { randomUUID: () => "id-" + Math.random().toString(36).slice(2) };
global.navigator = { userAgent: "harness" };
global.document = {
  body: { appendChild() {} },
  createElement: () => ({ style: { cssText: "", background: "" }, textContent: "" }),
  addEventListener() {},
};
global.GM_xmlhttpRequest = (o) => {
  const sport = decodeURIComponent(o.url.split("sport=")[1]);
  pushes.push({ sport, blocks: JSON.parse(o.data).blocks });
  o.onload({ status: 200 });
};
global.WebSocket = class {
  constructor() { this.readyState = 1; sock = this; this.outbox = []; setTimeout(() => this.onopen(), 0); }
  send(frame) { this.outbox.push(frame); }
  close() {}
};

new Function(src)();

// Un bloc de réponse tel que le renvoie Gaming1 : les ligues portent le SportId.
const blockFor = (sport, day) => ({
  Leagues: [{ SportId: SPORT_ID[sport], LeagueName: sport + "-" + day, Events: [] }],
});

function drain() {
  const out = sock.outbox.splice(0).map((f) => {
    const env = JSON.parse(f);
    if (env.MessageType !== 1000) return null;
    const inner = JSON.parse(env.Message);
    const r = inner.Requests[0];
    const c = JSON.parse(r.Content);
    return { reqId: r.Id, sportId: c.SportId, date: c.Filter.LocalDate };
  }).filter(Boolean);
  return out;
}

function reply(req, { echoId = true, sport = null, empty = false } = {}) {
  const key = sport || Object.keys(SPORT_ID).find((k) => SPORT_ID[k] === req.sportId);
  const content = empty ? { Leagues: [] } : blockFor(key, req.date);
  sock.onmessage({
    data: JSON.stringify({
      Id: "x", MessageType: 1000, TTL: 10,
      Message: JSON.stringify({
        Direction: 1,
        Requests: [{
          Id: echoId ? req.reqId : "unrelated-" + Math.random(),
          Type: 201, Identifier: "GetPrematchSport",
          Content: JSON.stringify(content),
        }],
      }),
    }),
  });
}

const results = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Attend qu'un nouveau cycle ait émis ses 8 requêtes.
async function nextCycle() {
  const got = [];
  for (let i = 0; i < 400; i++) {
    got.push(...drain());
    if (got.length >= 8) return got;
    await sleep(5);
  }
  throw new Error("cycle incomplet : " + got.length + " requêtes");
}

async function check(name, fn) {
  pushes.length = 0;
  try { await fn(await nextCycle()); results.push([true, name]); }
  catch (e) { results.push([false, name + " — " + e.message]); }
  await sleep(400);   // laisse le cycle en cours se clore
}

const owners = () => {
  const m = {};
  for (const p of pushes) {
    m[p.sport] = p.blocks.map((b) => (b.Leagues[0] || {}).LeagueName.split("-")[0]);
  }
  return m;
};
const assertClean = () => {
  const m = owners();
  for (const sport of Object.keys(m)) {
    if (m[sport].some((s) => s !== sport)) {
      throw new Error(sport + ".json a reçu " + JSON.stringify(m[sport]));
    }
    if (m[sport].length !== 4) throw new Error(sport + " a " + m[sport].length + " blocs");
  }
  if (Object.keys(m).length !== 2) throw new Error("sports poussés : " + Object.keys(m));
};

(async () => {
  await sleep(300);

  await check("réponses dans l'ordre", (reqs) => {
    reqs.forEach((r) => reply(r));
    assertClean();
  });

  await check("réponses en ordre inverse (le cas qui a cassé)", (reqs) => {
    reqs.reverse().forEach((r) => reply(r));
    assertClean();
  });

  await check("tennis répond avant que le football ait fini", (reqs) => {
    const t = reqs.filter((r) => r.sportId === 848);
    const s = reqs.filter((r) => r.sportId === 844);
    [t[0], t[1], s[0], t[2], s[1], t[3], s[2], s[3]].forEach((r) => reply(r));
    assertClean();
  });

  await check("sans écho de l'Id, le SportId suffit", (reqs) => {
    reqs.reverse().forEach((r) => reply(r, { echoId: false }));
    assertClean();
  });

  await check("ni Id ni SportId : la journée vide n'est pas mal rangée", (reqs) => {
    // Journée sans aucun match : le bloc ne porte aucun SportId. Il ne doit
    // surtout pas atterrir dans l'autre sport.
    reqs.forEach((r, i) => reply(r, { echoId: false, empty: i === 3 }));
    const m = owners();
    for (const sport of Object.keys(m)) {
      if (m[sport].some((s) => s !== sport)) {
        throw new Error(sport + ".json a reçu " + JSON.stringify(m[sport]));
      }
    }
  });

  await check("un cycle incomplet ne pousse rien", (reqs) => {
    reqs.slice(0, 5).forEach((r) => reply(r));
    if (pushes.length) throw new Error("a poussé " + JSON.stringify(owners()));
  });

  const ok = results.filter((r) => r[0]).length;
  for (const [pass, name] of results) console.log((pass ? "  ok  " : "FAIL  ") + name);
  console.log(`\n${ok}/${results.length}`);
  process.exit(ok === results.length ? 0 : 1);
})();
