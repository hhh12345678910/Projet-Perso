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
# Sondes de découverte : réponses capturées au vol pendant qu'on navigue sur le
# site, déchiffrées et rangées ICI et pas dans MAGIC_DIR. La séparation est
# volontaire — une sonde ne doit JAMAIS pouvoir écraser le fichier que lit le
# daemon, sinon explorer le site en direct empoisonnerait les cotes de
# production sans qu'on s'en aperçoive.
MAGIC_PROBE_DIR = MAGIC_DIR / "probes"
# Un fichier par compétition balayée, réassemblés en <sport>.json.
#
# Pourquoi par morceaux plutôt qu'un seul fichier réécrit : un balayage
# interrompu — onglet fermé, réseau coupé, budget épuisé — ne doit pas vider
# l'offre. Chaque morceau porte sa propre date, donc une compétition qui cesse
# d'être rafraîchie disparaît toute seule de l'assemblage au lieu de servir des
# cotes mortes. C'est la garde de fraîcheur du §5, appliquée par morceau.
MAGIC_PARTS_DIR = MAGIC_DIR / "parts"
MAGIC_PART_MAX_AGE = float(os.getenv("MAGIC_PART_MAX_AGE_MIN", "20")) * 60
# L'assemblage coûte une réécriture du fichier entier. À trente morceaux par
# minute et plusieurs mégaoctets, réécrire à chaque morceau userait le disque
# pour rien — le daemon ne lit qu'une fois par cycle.
MAGIC_REBUILD_EVERY = float(os.getenv("MAGIC_REBUILD_SEC", "10"))
_MAGIC_LAST_REBUILD: dict = {}
# SportId Gaming1 attendus par sport, pour refuser un push mal routé. Doit
# rester aligné sur CIRCUS_SPORTS dans src/main.py. Un sport absent d'ici est
# accepté sans vérification, pour qu'ajouter un sport ne casse rien.
CIRCUS_SPORT_IDS = {"soccer": 844, "tennis": 848}


# SId Digitain attendus par sport, pour refuser un push mal routé. Doit rester
# aligné sur SPORT_IDS dans src/scrapers/magicbetting.py.
MAGIC_SPORT_IDS = {"soccer": 1, "tennis": 3}


def magic_sport_mismatch(events, sport: str) -> set | None:
    """Les SId présents ne correspondent pas au sport annoncé ?

    Même barrière que pour Circus, et pour la même raison : un onglet resté sur
    une ancienne version du userscript a déjà écrit le tennis dans
    `soccer.json`. Le daemon écarte maintenant les événements étrangers, mais
    trop tard — le bon fichier a été écrasé et le sport disparaît jusqu'au
    push suivant. La barrière utile est ici, avant l'écriture.

    Un sport inconnu passe sans vérification : en ajouter un ne doit rien
    casser, et l'oubli d'une ligne ici ne doit jamais rendre un book muet."""
    expect = MAGIC_SPORT_IDS.get(sport)
    if expect is None:
        return None
    seen = {
        ev.get("SId") for ev in events
        if isinstance(ev, dict) and ev.get("SId") is not None
    }
    if not seen or seen == {expect}:
        return None
    return seen


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


# --- Balayage MagicBetting ---------------------------------------------------
# Le pont demandait `gettopeventslist`, qui rend 27 matchs. Le catalogue en
# annonce 1173 en football : on collectait 2 % de l'offre. Le balayage consiste
# à demander les matchs COMPÉTITION PAR COMPÉTITION.
#
# Tout le raisonnement — quoi balayer, dans quel ordre, à quelle cadence — vit
# ICI, en Python. Le userscript reçoit une liste d'URL et les appelle, sans
# jamais rien interpréter. C'est la règle du §10 : le pont Circus devait
# attribuer ses réponses en JavaScript et s'est cassé trois fois.
MAGIC_STAKE_TYPES = {"soccer": [1, 3], "tennis": [702, 3]}
# Budget par sport et par tour : combien d'événements viser, et surtout combien
# d'appels réseau y consacrer. Le second plafond est le vrai garde-fou — la
# longue traîne des compétitions à un match coûte un appel chacune.
MAGIC_BUDGET_EVENTS = int(os.getenv("MAGIC_BUDGET_EVENTS", "500"))
MAGIC_MAX_CALLS = int(os.getenv("MAGIC_MAX_CALLS", "30"))
# Catalogue en mémoire : sport -> {"tournaments": [...], "at": timestamp,
# "cursor": int}. Volatile exprès — un redémarrage le reconstruit au premier
# tour, et le persister exposerait à servir un catalogue périmé sans le savoir.
MAGIC_CATALOG: dict = {}


MAGIC_MAX_COUNTRIES = int(os.getenv("MAGIC_MAX_COUNTRIES", "25"))
# Le catalogue se reconstruit à cet intervalle. Une compétition qui se termine
# ou qui commence n'apparaît pas plus vite que ça — c'est sans conséquence, les
# calendriers ne changent pas à la minute.
MAGIC_CATALOG_TTL = float(os.getenv("MAGIC_CATALOG_TTL_SEC", "1800"))


def _magic_params(extra: list[tuple[str, str]]) -> str:
    from urllib.parse import urlencode

    base = [("period", "0"), ("langId", "62"),
            ("partnerId", "3000270"), ("countryCode", "BE")]
    return urlencode(base + extra)


def magic_countries_url(sport_id: int) -> str:
    return ("/common/getmixedchampionshiplistbyperiod?" + _magic_params([
        ("sportId", str(sport_id)), ("isTournament", "false"),
        ("eventFilterType", "0"), ("includeLiveSports", "false"),
    ]))


def magic_tournaments_url(country_id: int) -> str:
    return ("/common/getmixedtournamentsbyperiod?" + _magic_params([
        ("championshipId", str(country_id)), ("isTournament", "false"),
        ("includeLiveTournaments", "false"),
    ]))


def magic_event_url(sport: str, tournament_id: int) -> str:
    """Le chemin RELATIF de la liste d'événements d'une compétition.

    Relatif, et c'est essentiel : l'URL réelle porte un identifiant de session
    de 36 caractères que seul le navigateur connaît, et qui expire. La VM n'a
    pas à le connaître — le userscript préfixe. Le coder ici le ferait périmer
    sans prévenir."""
    from urllib.parse import urlencode

    params = [
        ("period", "0"),
        ("tournamentId", str(tournament_id)),
        ("isTournament", "false"),
        ("eventFilterType", "false"),
        ("includeLiveEvents", "false"),
        ("langId", "62"),
        ("partnerId", "3000270"),
        ("countryCode", "BE"),
    ]
    params += [("stakeTypes", str(s)) for s in MAGIC_STAKE_TYPES.get(sport, [])]
    return f"/common/getmixedsportandeventslistwithoutright?{urlencode(params)}"


def magic_rebuild(sport: str, *, force: bool = False) -> tuple[int, int]:
    """Réassembler <sport>.json à partir des morceaux encore frais.

    Rend (événements, morceaux retenus). Les morceaux périmés sont ignorés ET
    supprimés au-delà du double de leur durée de vie : sans ça, une
    compétition terminée laisserait son fichier pour toujours.

    Dédoublonnage par Id d'événement : un même match peut apparaître dans deux
    compétitions (une phase de qualification listée sous deux libellés), et le
    compter deux fois gonflerait l'offre sans ajouter une cote."""
    import time as _time

    now = _time.time()
    if not force and now - _MAGIC_LAST_REBUILD.get(sport, 0) < MAGIC_REBUILD_EVERY:
        return (-1, -1)
    _MAGIC_LAST_REBUILD[sport] = now

    d = MAGIC_PARTS_DIR / sport
    if not d.is_dir():
        return (0, 0)
    merged: dict = {}
    kept = 0
    for f in sorted(d.glob("*.json")):
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if age > MAGIC_PART_MAX_AGE:
            if age > MAGIC_PART_MAX_AGE * 2:
                try:
                    f.unlink()
                except OSError:
                    pass
            continue
        try:
            evs = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(evs, list):
            continue
        kept += 1
        for ev in evs:
            if isinstance(ev, dict) and ev.get("Id") is not None:
                merged[ev["Id"]] = ev

    payload = json.dumps(list(merged.values()), ensure_ascii=False).encode()
    _atomic_write(MAGIC_DIR / f"{sport}.json", payload)
    return (len(merged), kept)


def _magic_catalog_fns():
    """Les parseurs de catalogue, importés à la demande.

    Ils vivent dans `src/scrapers/magicbetting.py` avec leurs tests, et non
    ici : le serveur d'ingestion n'a pas de suite de tests, donc tout ce qui
    peut se tromper doit être ailleurs."""
    if str(_project_dir()) not in sys.path:
        sys.path.insert(0, str(_project_dir()))
    from src.scrapers.magicbetting import parse_catalog, select_within_budget
    return parse_catalog, select_within_budget


def magic_plan(sport: str, now: float) -> list[dict]:
    """Les compétitions à rafraîchir MAINTENANT, sous budget.

    Deux rythmes, parce que les compétitions n'ont pas le même poids. Les plus
    grosses reviennent à chaque tour ; la longue traîne défile par tranches, un
    curseur avançant d'un tour à l'autre. Tout rafraîchir chaque minute
    coûterait des centaines d'appels pour des matchs dont la cote ne bouge
    pas ; ne jamais rafraîchir la traîne la laisserait périmer sans que rien ne
    le signale."""
    cat = MAGIC_CATALOG.get(sport)
    if not cat or not cat.get("tournaments"):
        return []
    tours = cat["tournaments"]
    head = tours[:MAGIC_MAX_CALLS // 2]
    tail = tours[len(head):]
    picked = list(head)
    if tail:
        room = MAGIC_MAX_CALLS - len(picked)
        cur = cat.get("cursor", 0) % len(tail)
        # Tranche circulaire : on repart du début quand on atteint la fin, donc
        # aucune compétition n'est oubliée indéfiniment.
        picked += (tail + tail)[cur:cur + room]
        cat["cursor"] = (cur + room) % len(tail)
    total = 0
    out = []
    for t in picked:
        if total >= MAGIC_BUDGET_EVENTS:
            break
        total += t["events"]
        out.append({"part": t["id"], "name": t["name"],
                    "path": magic_event_url(sport, t["id"])})
    return out


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
        route = self.path.split("?", 1)[0]
        if route == "/health":
            self._send(200, "ok")
            return
        if route == "/magic-plan":
            # Le plan dit quelles compétitions balayer : c'est de la
            # configuration de collecte, pas une donnée publique. Même jeton
            # que les écritures.
            if not self._authorized():
                self._send(401, {"error": "bad or missing token"})
                return
            self._handle_magic_plan()
            return
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
        # `part` = une compétition du balayage. Sans lui, c'est l'ancien push
        # d'un seul bloc (la vitrine), qu'on garde pour ne pas casser un pont
        # resté sur l'ancienne version.
        raw_part = (qs.get("part") or [""])[0]
        part = "".join(c for c in raw_part if c.isalnum() or c in "-_")[:24]
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

        wrong = magic_sport_mismatch(clear, sport)
        if wrong is not None:
            _log(f"422 magicbetting push annonce '{sport}' mais porte les "
                 f"SId {sorted(wrong)} — fichier non écrasé")
            self._send(422, {"error": f"sport mismatch: expected "
                                      f"{MAGIC_SPORT_IDS.get(sport)}, got {sorted(wrong)}"})
            return

        payload = json.dumps(clear, ensure_ascii=False).encode()
        try:
            if part:
                # Balayage : un morceau par compétition, puis réassemblage.
                _atomic_write(MAGIC_PARTS_DIR / sport / f"{part}.json", payload)
                n_ev, n_parts = magic_rebuild(sport)
            else:
                # Ancien push d'un bloc unique. Il écrase le fichier assemblé,
                # ce qui est voulu : les deux modes ne doivent pas coexister,
                # sinon l'offre dépendrait de qui a écrit en dernier.
                MAGIC_DIR.mkdir(parents=True, exist_ok=True)
                _atomic_write(MAGIC_DIR / f"{sport}.json", payload)
                n_ev, n_parts = len(clear), 0
        except OSError as e:
            _log(f"500 magicbetting write failed: {e}")
            self._send(500, {"error": f"write failed: {e}"})
            return

        if part:
            # n_ev == -1 : réassemblage sauté par la limitation de cadence, ce
            # qui est le cas NORMAL au milieu d'un balayage. Le dire autrement
            # ferait lire un échec là où il n'y en a pas.
            etat = (f"assemblé: {n_ev} événements / {n_parts} compétitions"
                    if n_ev >= 0 else "assemblage différé")
            _log(f"200 magicbetting {sport} part={part} "
                 f"({len(clear)} événements, {n_stakes} cotes) — {etat}")
            self._send(200, {"ok": True, "sport": sport, "part": part,
                             "events": len(clear), "stakes": n_stakes,
                             "merged_events": n_ev, "parts": n_parts})
            return
        _log(f"200 magicbetting {len(raw)} B chiffres -> {len(payload)} B clairs "
             f"-> magicbetting/{sport}.json (events={len(clear)} stakes={n_stakes})")
        self._send(200, {"ok": True, "events": len(clear), "stakes": n_stakes,
                         "sport": sport})

    def _handle_magic_plan(self) -> None:
        """Dire au pont quoi appeler MAINTENANT.

        Le contrat est volontairement minuscule et générique :

            {"fetch": [{"path": "/common/…", "post_to": "/ingest-…"}]}

        Le userscript préfixe `path` de l'origine et de l'identifiant de
        session — le seul élément qu'il connaît et que la VM ignore — appelle,
        et reposte le corps brut à `post_to`. Si CETTE réponse est elle-même un
        `{"fetch": …}`, il recommence. Il ne lit rien d'autre, ne décide rien.

        Deux étages en découlent sans que le pont le sache : tant que le
        catalogue manque, le plan demande le catalogue ; une fois qu'il est là,
        le plan demande les cotes."""
        from urllib.parse import parse_qs, urlparse
        import time as _time

        qs = parse_qs(urlparse(self.path).query)
        sport = (qs.get("sport") or [""])[0]
        sport_id = MAGIC_SPORT_IDS.get(sport)
        if sport_id is None:
            self._send(400, {"error": f"sport inconnu: {sport!r}"})
            return

        now = _time.time()
        cat = MAGIC_CATALOG.get(sport)
        if not cat or (now - cat.get("at", 0)) > MAGIC_CATALOG_TTL:
            # Le catalogue est absent ou périmé : on le redemande. On NE VIDE
            # PAS l'ancien — le balayage continue de tourner sur les
            # compétitions connues pendant la reconstruction, au lieu de
            # laisser un trou.
            MAGIC_CATALOG.setdefault(sport, {"tournaments": [], "cursor": 0})
            MAGIC_CATALOG[sport]["at"] = now
            self._send(200, {"fetch": [{
                "path": magic_countries_url(sport_id),
                "post_to": f"/magic-catalog?sport={sport}&level=countries",
            }]})
            return

        plan = magic_plan(sport, now)
        # Un plan VIDE est le symptôme d'un catalogue qui ne s'est pas rempli,
        # et il ne se voit nulle part ailleurs : le pont appellerait zéro URL
        # et se tairait, exactement comme s'il n'y avait rien à faire.
        if not plan:
            _log(f"200 magic-plan {sport} : plan VIDE — "
                 f"{len(MAGIC_CATALOG.get(sport, {}).get('tournaments', []))} "
                 f"compétitions au catalogue")
        self._send(200, {"fetch": [{
            "path": p["path"],
            "post_to": f"/ingest-magicbetting?sport={sport}&part={p['part']}",
        } for p in plan]})

    def _handle_magic_catalog(self, raw: bytes) -> None:
        """Ranger un étage du catalogue, et demander le suivant s'il y en a un."""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        sport = (qs.get("sport") or [""])[0]
        level = (qs.get("level") or [""])[0]
        if sport not in MAGIC_SPORT_IDS or level not in ("countries", "tournaments"):
            self._send(400, {"error": "sport ou level invalide"})
            return
        try:
            clear = _magic_decryptor().decrypt_response(json.loads(raw))
        except FileNotFoundError as e:
            self._send(503, {"error": str(e)})
            return
        except Exception as e:                                      # noqa: BLE001
            _log(f"422 magic-catalog {sport}/{level} : {e}")
            self._send(422, {"error": f"decrypt failed: {e}"})
            return

        parse_catalog, select_within_budget = _magic_catalog_fns()
        items = parse_catalog(clear)
        cat = MAGIC_CATALOG.setdefault(sport, {"tournaments": [], "cursor": 0})

        if level == "countries":
            # On ne balaie pas les 90 pays : le budget en appels ferait
            # exploser le trafic pour des championnats à un match. Les plus
            # fournis d'abord, le reste attend le tour suivant du catalogue.
            picked = select_within_budget(
                items, budget=MAGIC_BUDGET_EVENTS * 3, max_items=MAGIC_MAX_COUNTRIES)
            _log(f"200 magic-catalog {sport} : {len(items)} pays, "
                 f"{len(picked)} retenus ({sum(p['events'] for p in picked)} événements)")
            self._send(200, {"fetch": [{
                "path": magic_tournaments_url(p["id"]),
                "post_to": f"/magic-catalog?sport={sport}&level=tournaments",
            } for p in picked]})
            return

        # level == "tournaments" : fusionner, en gardant le compteur le plus
        # récent pour chaque compétition déjà connue.
        by_id = {t["id"]: t for t in cat["tournaments"]}
        for t in items:
            by_id[t["id"]] = t
        cat["tournaments"] = sorted(by_id.values(),
                                    key=lambda d: (-d["events"], d["id"]))
        # ⚠️ Cet étage se taisait. Entre « le pont ne l'a jamais appelé » et
        # « il l'a appelé et rien n'en est sorti », le journal ne montrait
        # aucune différence — alors que ce sont deux pannes opposées.
        _log(f"200 magic-catalog {sport} : +{len(items)} compétitions "
             f"({sum(t['events'] for t in items)} événements), "
             f"catalogue = {len(cat['tournaments'])}")
        self._send(200, {"ok": True, "tournois": len(cat["tournaments"])})

    def _handle_magic_probe(self, raw: bytes) -> None:
        """Ranger une réponse Digitain capturée au vol, pour DÉCOUVERTE.

        Deux inconnues bloquent MagicBetting (§18.6) : l'endpoint de l'offre
        complète — `gettopeventslist` ne rend que 27 matchs vedettes — et les
        trois identifiants du tennis. Le §10 interdit de les deviner : Napoleon
        utilise 547 en football et 521 en tennis, et une supposition qui tombe
        à côté fabrique des cotes muettes sans jamais lever d'erreur.

        Donc on ne devine pas : on laisse le SITE faire ses propres appels
        pendant qu'on navigue, et on les recopie. Le userscript de découverte
        n'interprète rien, il relaie `{url, body}` ; ici on déchiffre et on
        range. `scripts/magic_probe_report.py` lit ensuite le tout et énumère
        les sports et les marchés réellement présents.

        ⚠️ Écrit dans MAGIC_PROBE_DIR, jamais dans MAGIC_DIR : une exploration
        ne doit pas pouvoir toucher aux cotes que sert le daemon."""
        import hashlib
        from urllib.parse import urlparse as _urlparse

        try:
            env = json.loads(raw)
            url = str(env["url"])
            body_text = env["body"]
        except (ValueError, KeyError, TypeError) as e:
            self._send(400, {"error": f"expected {{url, body}}: {e}"})
            return

        parts = _urlparse(url)
        # Le nom de fichier vient de l'endpoint, plus une empreinte courte de la
        # requête : deux appels au même endpoint avec des paramètres différents
        # (deux sports, par exemple) sont deux découvertes distinctes.
        leaf = (parts.path.rstrip("/").rsplit("/", 1)[-1] or "root")[:48]
        leaf = "".join(c for c in leaf if c.isalnum() or c in "-_") or "root"
        sig = hashlib.sha1(f"{parts.path}?{parts.query}".encode()).hexdigest()[:8]

        note = None
        try:
            clear = _magic_decryptor().decrypt_response(json.loads(body_text))
        except FileNotFoundError as e:
            self._send(503, {"error": str(e)})
            return
        except Exception as e:                                      # noqa: BLE001
            # Une sonde qu'on ne sait pas lire reste une information : on la
            # garde telle quelle plutôt que de la jeter, parce qu'elle dit
            # qu'un endroit du site parle un autre dialecte.
            clear, note = None, f"{type(e).__name__}: {e}"

        record = {
            "url": url, "query": parts.query,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "bytes_in": len(raw),
            "clear": clear,
            "raw": None if clear is not None else body_text[:20000],
            "note": note,
        }
        try:
            MAGIC_PROBE_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write(MAGIC_PROBE_DIR / f"{leaf}-{sig}.json",
                          json.dumps(record, ensure_ascii=False).encode())
        except OSError as e:
            self._send(500, {"error": f"write failed: {e}"})
            return

        n = len(clear) if isinstance(clear, (list, dict)) else 0
        _log(f"200 sonde {leaf}-{sig} ({len(raw)} B) "
             f"{'illisible: ' + note if note else f'-> {n} entrees'}")
        self._send(200, {"ok": True, "file": f"{leaf}-{sig}.json",
                         "entries": n, "note": note})

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
                         "/ingest-magicbetting", "/probe-magicbetting",
                         "/magic-catalog"):
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
        if route == "/probe-magicbetting":
            self._handle_magic_probe(raw)
            return
        if route == "/magic-catalog":
            self._handle_magic_catalog(raw)
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
