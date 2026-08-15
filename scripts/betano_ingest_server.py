#!/usr/bin/env python3
"""Tiny HTTP receiver for Betano odds pushed from a browser userscript.

Why this exists
---------------
Betano's danae-webapi is gated by Cloudflare (cf_clearance) + DataDome cookies
that are bound to the *browser + IP* that minted them and expire every few
hours. The (datacenter) VM can't generate them, so the historical workaround
was to paste a fresh cookie by hand every few hours (see set-betano-cookie.sh).

Instead, a Tampermonkey userscript running in a real browser that's already
sitting on betanosports.be fetches the /live/overview/latest JSON from its own
IP with its own valid cookies — exactly the traffic the site makes itself — and
POSTs it here. This server just validates a shared token and writes the JSON to
disk, atomically, where the daemon reads it via `--betano-file`.

So Betano freshness == the userscript's push interval, with zero cookie
juggling on the VM.

Config (env vars)
-----------------
  BETANO_INGEST_TOKEN   required. Shared secret; the userscript sends it in the
                        X-Ingest-Token header. The server refuses to start
                        without it (an unauthenticated write endpoint would let
                        anyone poison the odds file).
  BETANO_INGEST_FILE    output path. Default: <project>/data/betano.json
  BETANO_INGEST_PORT    listen port. Default: 8787
  BETANO_INGEST_HOST    bind address. Default: 0.0.0.0 (all interfaces)
  BETANO_INGEST_MAX_MB  max accepted body size in MB. Default: 32

Endpoints
---------
  GET  /health         -> 200 "ok"   (no token; for connectivity tests)
  POST /ingest-cookie  -> 200 {...}  (preferred) stores the cookie + UA the
                         browser pushed; the daemon then fetches Betano itself.
  POST /ingest         -> 200 {...}  (fallback) stores a full odds dump the
                         browser fetched, for `--betano-file`.

Prefer /ingest-cookie: ~0.5 KB per push instead of a multi-MB dump, the
browser never touches Betano's API (nothing for DataDome to see), and the VM
reuses the already-working BetanoScraper fetch path.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _project_dir() -> Path:
    # scripts/.. -> project root, so the default output path is portable.
    return Path(__file__).resolve().parent.parent


TOKEN = os.getenv("BETANO_INGEST_TOKEN", "")
OUT_FILE = Path(
    os.getenv("BETANO_INGEST_FILE", str(_project_dir() / "data" / "betano.json"))
)
# Where /ingest-cookie stores the pushed credentials. Must match
# BETANO_COOKIE_FILE as read by src/scrapers/betano.py.
COOKIE_FILE = Path(
    os.getenv("BETANO_COOKIE_FILE", str(_project_dir() / "data" / "betano_cookie.json"))
)
# Captured payloads from endpoints whose shape isn't known yet.
SAMPLE_DIR = Path(os.getenv("BETANO_SAMPLE_DIR", str(_project_dir() / "data" / "samples")))
# Prematch offer, one file per sport (the API is per-sport, unlike the live
# overview which mixes them). Read by fetch_betano_quotes().
PREMATCH_DIR = Path(
    os.getenv("BETANO_PREMATCH_DIR", str(_project_dir() / "data" / "prematch"))
)
# Prématch Circus (plateforme Gaming1). Un seul fichier : le userscript pousse
# un cycle complet — un bloc par jour — en un envoi. Lu par
# src/scrapers/circus.load_pushed_quotes().
CIRCUS_DIR = Path(
    os.getenv("CIRCUS_INGEST_DIR", str(_project_dir() / "data" / "circus"))
)
# MagicBetting (plateforme Digitain). Le userscript pousse la réponse CHIFFRÉE
# telle quelle ; le déchiffrement se fait ici, côté serveur, avec leur propre
# module WebAssembly (src/scrapers/digitain_crypto).
#
# Pourquoi déchiffrer ici plutôt que dans le navigateur : le userscript reste
# alors totalement bête — il relaie, il ne comprend rien. Tout ce qui peut se
# tromper vit en Python, là où sont les tests. C'est la leçon du §10, où le
# JavaScript du pont Circus devait attribuer les réponses et s'est cassé trois
# fois de suite.
MAGIC_DIR = Path(
    os.getenv("MAGIC_INGEST_DIR", str(_project_dir() / "data" / "magicbetting"))
)
# SportId Gaming1 attendus par sport, pour refuser un push mal routé. Doit
# rester aligné sur CIRCUS_SPORTS dans src/main.py. Un sport absent d'ici est
# accepté sans vérification, pour qu'ajouter un sport ne casse rien.
CIRCUS_SPORT_IDS = {"soccer": 844, "tennis": 848}


def circus_sport_mismatch(blocks, sport: str) -> set | None:
    """Les SportId présents ne correspondent pas au sport annoncé ?

    Renvoie l'ensemble des SportId vus, ou None si le push est acceptable.
    Un sport inconnu ou des blocs sans SportId passent : mieux vaut accepter
    un push qu'on ne sait pas juger que couper le book sur une supposition."""
    expect = CIRCUS_SPORT_IDS.get(sport)
    if expect is None:
        return None
    seen = {
        lg.get("SportId")
        for b in blocks if isinstance(b, dict)
        for lg in (b.get("Leagues") or [])
        if isinstance(lg, dict) and lg.get("SportId") is not None
    }
    if not seen or seen == {expect}:
        return None
    return seen
HOST = os.getenv("BETANO_INGEST_HOST", "0.0.0.0")
PORT = int(os.getenv("BETANO_INGEST_PORT", "8787"))
MAX_BYTES = int(float(os.getenv("BETANO_INGEST_MAX_MB", "32")) * 1024 * 1024)


_MAGIC_DEC = None


def _magic_decryptor():
    """Le déchiffreur Digitain, instancié une seule fois.

    Le chargement du WASM coûte quelques dizaines de millisecondes ; le refaire
    à chaque push serait gâché. Import différé pour que le serveur démarre même
    si `wasmtime` n'est pas installé — seule cette route en dépend."""
    global _MAGIC_DEC
    if _MAGIC_DEC is None:
        sys.path.insert(0, str(_project_dir()))
        from src.scrapers.digitain_crypto import DigitainDecryptor
        _MAGIC_DEC = DigitainDecryptor()
    return _MAGIC_DEC


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write to a temp file in the same directory, then os.replace() it onto the
    target. replace() is atomic on POSIX, so the daemon reading `path` in a
    parallel thread never sees a half-written file — it gets either the old
    complete JSON or the new complete JSON, never a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


class Handler(BaseHTTPRequestHandler):
    # Quieter than the default (which logs every request line to stderr); we do
    # our own concise logging in the handlers instead.
    def log_message(self, *_args) -> None:  # noqa: D401
        pass

    def _send(self, code: int, body: dict | str) -> None:
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            ctype = "application/json"
        else:
            data = body.encode()
            ctype = "text/plain; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Permissive CORS: GM_xmlhttpRequest is privileged and doesn't enforce
        # CORS, but a plain fetch fallback (or a browser preflight) would — these
        # headers keep both paths working.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ingest-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Ingest-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[len("Bearer "):].strip()
        # Constant-time compare so a timing side-channel can't leak the token.
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, "")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            self._send(200, "ok")
        else:
            self._send(404, {"error": "not found"})

    def _handle_cookie(self, raw: bytes) -> None:
        """Store a cookie + User-Agent pushed by the userscript.

        This is the preferred feed: the browser sends ~0.5 KB of credentials
        instead of a multi-MB odds dump, makes no request to Betano's API at
        all (so nothing for DataDome to flag), and the VM does the fetching
        through the already-proven BetanoScraper path."""
        try:
            data = json.loads(raw)
        except ValueError as e:
            self._send(400, {"error": f"invalid JSON: {e}"})
            return
        if not isinstance(data, dict):
            self._send(400, {"error": "expected a JSON object"})
            return
        # Log the userscript's own account of what it found before validating.
        # When a push fails, this line is the only evidence that the script ran
        # at all — the browser-side banner may not render on an SPA, so the
        # server log has to be able to stand alone as the diagnostic.
        note = str(data.get("note") or "").strip()
        if note:
            _log(f"userscript note: {note}")

        cookie = str(data.get("cookie") or "").strip()
        if not cookie:
            _log(f"400 empty cookie from {self.client_address[0]} — script ran but found nothing")
            self._send(400, {"error": "missing 'cookie'"})
            return
        # datadome is the token that actually gates the API; warn (but still
        # store) if it's absent so a partial capture is visible in the log
        # rather than failing silently at scrape time.
        names = {p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p}
        payload = json.dumps({
            "cookie": cookie,
            "user_agent": str(data.get("user_agent") or "").strip(),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }).encode()
        try:
            _atomic_write(COOKIE_FILE, payload)
        except OSError as e:
            _log(f"500 cookie write failed: {e}")
            self._send(500, {"error": f"write failed: {e}"})
            return
        missing = [n for n in ("datadome", "cf_clearance") if n not in names]
        _log(
            f"200 cookie stored ({len(cookie)} chars, {len(names)} cookies)"
            + (f" — WARNING missing: {', '.join(missing)}" if missing else "")
        )
        self._send(200, {"ok": True, "cookies": sorted(names), "missing": missing})

    def _handle_discover(self, raw: bytes) -> None:
        """Log the danae-webapi URLs the Betano page actually calls.

        The prematch overview path was never confirmed — fetch_prematch_overview()
        still guesses among three candidates. Rather than have someone dig
        through DevTools, the userscript hooks fetch/XHR and reports the real
        URLs here; browsing to a prematch section then reveals the right path."""
        try:
            data = json.loads(raw)
            urls = data.get("urls") or []
        except ValueError:
            self._send(400, {"error": "invalid JSON"})
            return
        if not isinstance(urls, list):
            self._send(400, {"error": "'urls' must be a list"})
            return
        for u in urls[:100]:
            _log(f"DISCOVERED: {u}")
        self._send(200, {"ok": True, "logged": len(urls[:100])})

    def _handle_sample(self, raw: bytes) -> None:
        """Store an arbitrary JSON payload under data/samples/ for inspection.

        The prematch offer lives on a different API (/fr/api/...) with an
        unknown shape, so a sample has to be captured before a parser can be
        written for it."""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        raw_name = (qs.get("name") or ["sample"])[0]
        # Whitelist rather than blacklist: this value becomes a filename.
        name = "".join(c for c in raw_name if c.isalnum() or c in "-_")[:64] or "sample"
        try:
            json.loads(raw)
        except ValueError as e:
            self._send(400, {"error": f"invalid JSON: {e}"})
            return
        path = SAMPLE_DIR / f"{name}.json"
        try:
            _atomic_write(path, raw)
        except OSError as e:
            self._send(500, {"error": f"write failed: {e}"})
            return
        _log(f"200 sample '{name}' stored ({len(raw)} B) -> {path}")
        self._send(200, {"ok": True, "name": name, "bytes": len(raw)})

    def _handle_circus(self, raw: bytes) -> None:
        """Stocke le prématch Circus poussé par le navigateur.

        Gaming1 refuse l'IP de la VM sur tout le domaine — pas seulement sur
        l'API — donc contrairement à Betano il n'y a pas de cookie à repousser :
        le navigateur doit livrer les données elles-mêmes.

        Le userscript envoie un cycle complet ({"blocks": [...]}, un bloc par
        jour). Un cycle vide est refusé plutôt qu'écrit : écraser un bon fichier
        par du vide couperait Circus en silence, et la garde de fraîcheur côté
        daemon ne verrait rien puisque l'horodatage, lui, serait frais.

        Un fichier par sport : le daemon scanne sport par sport, et un fichier
        commun ferait porter les cotes tennis au scan football.

        Le contenu est vérifié contre le sport annoncé. Un onglet resté ouvert
        sur une ancienne version du userscript a déjà écrit le tennis dans
        soccer.json ; le refuser ici préserve le dernier bon fichier, là où
        l'écraser aurait fait disparaître le book jusqu'au cycle suivant."""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        raw_sport = (qs.get("sport") or [""])[0]
        sport = "".join(c for c in raw_sport if c.isalnum() or c in "-_")[:32]
        if not sport:
            self._send(400, {"error": "missing 'sport' query parameter"})
            return
        try:
            data = json.loads(raw)
        except ValueError as e:
            self._send(400, {"error": f"invalid JSON: {e}"})
            return
        blocks = data.get("blocks") if isinstance(data, dict) else None
        if not blocks:
            _log("422 circus push has no blocks — not overwriting")
            self._send(422, {"error": "no blocks in payload"})
            return
        n_events = sum(
            len(lg.get("Events") or [])
            for b in blocks if isinstance(b, dict)
            for lg in (b.get("Leagues") or [])
        )
        if not n_events:
            _log("422 circus push has no events — not overwriting")
            self._send(422, {"error": "no events in payload"})
            return
        wrong = circus_sport_mismatch(blocks, sport)
        if wrong is not None:
            expect = CIRCUS_SPORT_IDS[sport]
            _log(f"422 circus '{sport}' push carries SportId {sorted(wrong)} "
                 f"(expected {expect}) — not overwriting")
            self._send(422, {"error": "sport mismatch",
                             "expected": expect, "found": sorted(wrong)})
            return
        try:
            CIRCUS_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write(CIRCUS_DIR / f"{sport}.json", raw)
        except OSError as e:
            _log(f"500 circus write failed: {e}")
            self._send(500, {"error": f"write failed: {e}"})
            return
        _log(f"200 wrote {len(raw)} B -> circus/{sport}.json "
             f"(blocks={len(blocks)} events={n_events})")
        self._send(200, {"ok": True, "bytes": len(raw), "sport": sport,
                         "blocks": len(blocks), "events": n_events})

    def _handle_magicbetting(self, raw: bytes) -> None:
        """Déchiffrer une réponse Digitain et la stocker en clair.

        Le corps reçu est la réponse brute du site : `{"payload": "...",
        "timestamp": ...}`. On la déchiffre avec LEUR module WebAssembly, ce
        qui évite d'avoir à extraire leur clé — et permet au daemon de lire un
        simple fichier JSON, comme pour Circus et Betano."""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        raw_sport = (qs.get("sport") or [""])[0]
        sport = "".join(c for c in raw_sport if c.isalnum() or c in "-_")[:32]
        if not sport:
            self._send(400, {"error": "missing 'sport' query parameter"})
            return
        try:
            body = json.loads(raw)
        except ValueError as e:
            self._send(400, {"error": f"invalid JSON: {e}"})
            return

        try:
            clear = _magic_decryptor().decrypt_response(body)
        except FileNotFoundError as e:
            # Le .wasm n'est pas déposé sur la VM : dire quoi faire, plutôt
            # que de laisser une 500 opaque.
            _log(f"503 magicbetting: {e}")
            self._send(503, {"error": str(e)})
            return
        except Exception as e:                                      # noqa: BLE001
            # « Authentication failed » sur TOUT veut dire que le site a changé
            # de version et donc de binaire — c'est écrit dans digitain_crypto.
            _log(f"422 magicbetting decrypt failed: {e}")
            self._send(422, {"error": f"decrypt failed: {e}"})
            return

        if not isinstance(clear, list) or not clear:
            _log("422 magicbetting push has no events — not overwriting")
            self._send(422, {"error": "no events in decrypted payload"})
            return
        # Refuser un push vide PLUTÔT QUE d'écraser le dernier bon fichier :
        # sans ça, un appel qui échoue côté site ferait disparaître le book
        # jusqu'au cycle suivant, en silence.
        n_stakes = sum(
            len(st.get("Stakes") or [])
            for ev in clear if isinstance(ev, dict)
            for st in (ev.get("StakeTypes") or []) if isinstance(st, dict)
        )
        if not n_stakes:
            _log("422 magicbetting push has events but no stakes")
            self._send(422, {"error": "no stakes in decrypted payload"})
            return

        payload = json.dumps(clear, ensure_ascii=False).encode()
        try:
            MAGIC_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write(MAGIC_DIR / f"{sport}.json", payload)
        except OSError as e:
            _log(f"500 magicbetting write failed: {e}")
            self._send(500, {"error": f"write failed: {e}"})
            return
        _log(f"200 magicbetting {len(raw)} B chiffres -> {len(payload)} B clairs "
             f"-> magicbetting/{sport}.json (events={len(clear)} stakes={n_stakes})")
        self._send(200, {"ok": True, "events": len(clear), "stakes": n_stakes,
                         "sport": sport})

    def _handle_prematch(self, raw: bytes) -> None:
        """Store the prematch offer for one sport.

        The prematch API is per-sport (a different URL per sport slug), unlike
        the live overview which mixes every sport into one payload — hence one
        file per sport rather than a single dump."""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        raw_sport = (qs.get("sport") or [""])[0]
        sport = "".join(c for c in raw_sport if c.isalnum() or c in "-_")[:32]
        if not sport:
            self._send(400, {"error": "missing 'sport' query parameter"})
            return
        try:
            data = json.loads(raw)
        except ValueError as e:
            self._send(400, {"error": f"invalid JSON: {e}"})
            return
        blocks = ((data.get("data") or {}).get("blocks")) if isinstance(data, dict) else None
        if not blocks:
            # Betano serves an empty payload for a sport with no fixtures in
            # window; refuse it rather than overwrite a good file with nothing.
            _log(f"422 prematch '{sport}' has no blocks — not overwriting")
            self._send(422, {"error": "no data.blocks in payload"})
            return
        n_events = sum(len(b.get("events") or []) for b in blocks if isinstance(b, dict))
        try:
            _atomic_write(PREMATCH_DIR / f"{sport}.json", raw)
        except OSError as e:
            _log(f"500 prematch write failed: {e}")
            self._send(500, {"error": f"write failed: {e}"})
            return
        _log(
            f"200 prematch '{sport}' stored ({len(raw)} B, "
            f"{len(blocks)} leagues, {n_events} events)"
        )
        self._send(200, {"ok": True, "sport": sport, "leagues": len(blocks), "events": n_events})

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route not in ("/ingest", "/ingest-cookie", "/discover", "/sample",
                         "/ingest-prematch", "/ingest-circus",
                         "/ingest-magicbetting"):
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            _log(f"401 unauthorized from {self.client_address[0]}")
            self._send(401, {"error": "bad or missing token"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return
        if length > MAX_BYTES:
            self._send(413, {"error": f"body too large (> {MAX_BYTES} bytes)"})
            return

        raw = self.rfile.read(length)
        if route == "/ingest-cookie":
            self._handle_cookie(raw)
            return
        if route == "/discover":
            self._handle_discover(raw)
            return
        if route == "/sample":
            self._handle_sample(raw)
            return
        if route == "/ingest-prematch":
            self._handle_prematch(raw)
            return
        if route == "/ingest-magicbetting":
            self._handle_magicbetting(raw)
            return
        if route == "/ingest-circus":
            self._handle_circus(raw)
            return
        try:
            data = json.loads(raw)
        except ValueError as e:
            _log(f"400 invalid JSON from {self.client_address[0]}: {e}")
            self._send(400, {"error": f"invalid JSON: {e}"})
            return
        if not isinstance(data, dict):
            self._send(400, {"error": "expected a JSON object"})
            return

        # Light sanity so a stray/empty push can't clobber a good file. The
        # overview payload is a Redux-style store with these three maps; if none
        # are present it's not an overview we can parse — reject without writing.
        n_events = len(data.get("events") or {})
        n_markets = len(data.get("markets") or {})
        n_selections = len(data.get("selections") or {})
        if not (n_events or n_markets or n_selections):
            _log(f"422 payload has no events/markets/selections from {self.client_address[0]}")
            self._send(422, {"error": "no events/markets/selections in payload"})
            return

        try:
            _atomic_write(OUT_FILE, raw)
        except OSError as e:
            _log(f"500 write failed: {e}")
            self._send(500, {"error": f"write failed: {e}"})
            return

        _log(
            f"200 wrote {len(raw)} B -> {OUT_FILE.name} "
            f"(events={n_events} markets={n_markets} selections={n_selections})"
        )
        self._send(200, {
            "ok": True,
            "bytes": len(raw),
            "events": n_events,
            "markets": n_markets,
            "selections": n_selections,
        })


def main() -> None:
    if not TOKEN:
        sys.exit(
            "BETANO_INGEST_TOKEN is not set. Generate one (`openssl rand -hex 32`), "
            "put it in .env, and use the same value in the userscript. Refusing to "
            "start an unauthenticated write endpoint."
        )
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _log(f"Betano ingest server listening on {HOST}:{PORT} -> {OUT_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _log("stopped")


if __name__ == "__main__":
    main()
