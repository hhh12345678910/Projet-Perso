"""Collecteur LIVE AsianOdds : flux WebSocket → normalisation → `market_state`.

CE MODULE NE TOUCHE PAS AU PRÉMATCH. Il n'est importé par aucun chemin de
`main.py`, il n'écrit que dans `market_state` via `Storage.upsert_live_state`,
et il ne lit `events` qu'en lecture seule pour rapprocher ses matchs des
nôtres. Aucun calcul de fair odds, d'EV ou de CLV ici : ce module produit une
DONNÉE, pas une décision.

────────────────────────────────────────────────────────────────────────────
CE QUE LA SOURCE FOURNIT, ET CE QU'ELLE NE FOURNIT PAS

Mesuré sur deux captures de 20 min (52 000 messages de prix, 23/08) :

  latence source → client   médiane 76 ms, IQR 6 ms
  intervalle entre changements réels   médiane 28 s ; 2,7 s sur le marché
                                       le plus actif ; p90 39 s
  fraîcheur                 AUCUNE garantie : 95 % des lignes sont revues en
                            moins de 40 s, mais la queue monte à 12 min 49.
                            L'absence de message ne veut PAS dire « le prix
                            n'a pas bougé » — d'où `observed_at`.
  FID == base64("HS:AS")    52 000 / 52 000, sans exception

────────────────────────────────────────────────────────────────────────────
DEUX PIÈGES MESURÉS, ET CE QU'ILS IMPOSENT

1. `ST` / `SO` / `OKO` NE SONT PAS L'HEURE DE COUP D'ENVOI en LIVE. Mesuré :
   quantifiés sur ~10 valeurs pour 410 matchs, et décalés d'environ 3 h par
   rapport à `IGM`. On ne rapproche donc PAS les matchs sur l'horaire, mais
   sur les noms d'équipes, parmi les événements plausiblement en cours.

2. Les cotes arrivent en décimal (`OF="00"` = EU), MAIS les toutes premières
   trames d'un abonnement peuvent porter l'échelle Malay de l'abonnement
   précédent (valeurs dans [-1, +1]). Mesuré : 42 lignes sur 1955, toutes
   entre t+0,72 s et t+0,92 s. On les REJETTE au lieu de les convertir : une
   conversion silencieuse ferait entrer un prix faux si l'hypothèse est
   fausse, alors qu'un rejet ne coûte qu'une ligne pendant 200 ms.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Optional

from .matcher import team_similarity
from .models import Book, MarketType

WS_HOST = "app200.ao0188.com"
WS_PATH = "/WS/"
ORIGIN = "https://app.ao0188.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Constantes du protocole, relevées dans le bundle du client web.
MARKET_TYPE_LIVE = 0
SPORT_FOOTBALL = 1
ODDS_FORMAT_EU = "00"

# Une cote décimale valide est > 1. En dessous, c'est de l'échelle Malay
# résiduelle (voir piège 2) : on rejette.
MIN_DECIMAL_ODD = 1.0

# Seuils du rapprochement, repris à l'identique de `matcher.match_event` pour
# ne pas faire diverger deux rapprochements dans le même projet (§17.7).
MIN_MATCH_SCORE = 85.0
AMBIGUITY_MARGIN = 4.0

# Fenêtre des événements candidats : un match commencé il y a plus de 4 h
# n'est plus en cours, un match à plus de 15 min du coup d'envoi ne l'est pas
# encore. Bornes larges à dessein — le rapprochement se fait sur les noms.
LIVE_WINDOW_BEFORE = timedelta(hours=4)
LIVE_WINDOW_AFTER = timedelta(minutes=15)


# ══════════════════════════════════════════════════════════════════════════
# Client WebSocket minimal (RFC 6455)
# ══════════════════════════════════════════════════════════════════════════
# Écrit ici plutôt que tiré d'une bibliothèque : le projet n'a aucune
# dépendance WebSocket, et en ajouter une pour un collecteur expérimental
# ferait porter au prématch le risque d'une mise à jour de dépendance.
# `permessage-deflate` n'est pas implémenté : vérifié, le serveur ne le
# négocie pas (aucun Sec-WebSocket-Extensions dans sa réponse 101).
class _WS:
    def __init__(self, host: str, path: str, timeout: float = 30.0,
                 port: int = 443, tls: bool = True):
        raw = socket.create_connection((host, port), timeout=timeout)
        if tls:
            self.sock = ssl.create_default_context().wrap_socket(
                raw, server_hostname=host)
        else:
            self.sock = raw          # tests locaux uniquement
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nOrigin: {ORIGIN}\r\n"
            f"User-Agent: {UA}\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connexion fermée pendant le handshake")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        statut = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in statut:
            raise ConnectionError(f"handshake refusé : {statut}")

    def send_text(self, s: str) -> None:
        payload = s.encode()
        n = len(payload)
        h = bytearray([0x81])
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126); h += struct.pack(">H", n)
        else:
            h.append(0x80 | 127); h += struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        h += mask
        self.sock.sendall(bytes(h) + bytes(
            b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket fermée")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> tuple[int, bytes]:
        frames: list[bytes] = []
        premier: int | None = None
        while True:
            b0, b1 = self._read(2)
            fin, opcode, masque, ln = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masque else None
            data = self._read(ln) if ln else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:                       # ping serveur
                m = secrets.token_bytes(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + m + bytes(
                    b ^ m[i % 4] for i, b in enumerate(data)))
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode == 0x8:                       # close
                return 0x8, b""
            if premier is None and opcode != 0x0:
                premier = opcode
            frames.append(data)
            if fin:
                return premier or 0x1, b"".join(frames)

    def close(self) -> None:
        for action in (lambda: self.sock.sendall(
                bytes([0x88, 0x80]) + secrets.token_bytes(4)),
                self.sock.close):
            try:
                action()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════
# Décodage — fonctions pures, testables sans réseau
# ══════════════════════════════════════════════════════════════════════════
def decode_feed_score(fid: str | None) -> Optional[str]:
    """`FID` est le base64 de "HS:AS" : le score auquel le prix a été fabriqué.

    Vérifié conforme sur 52 000 messages. Renvoie None si le champ est absent
    ou indécodable — jamais une valeur inventée."""
    if not fid:
        return None
    try:
        brut = base64.b64decode(fid + "=" * ((4 - len(fid) % 4) % 4)).decode()
    except Exception:
        return None
    return brut if ":" in brut else None


def parse_line(valeur: str | None) -> Optional[float]:
    """Ligne asiatique : "2.5" → 2.5, "2.5-3" → 2.75 (ligne quart), "" → None.

    La ligne quart est un pari COUPÉ en deux : sa valeur représentative est la
    moyenne des deux bornes, comme le fait le client web
    (`getFormattedHdpOrGoal`)."""
    if not valeur:
        return None
    valeur = valeur.strip()
    if "-" in valeur:
        bornes = valeur.split("-")
        if len(bornes) != 2:
            return None
        try:
            return (float(bornes[0]) + float(bornes[1])) / 2
        except ValueError:
            return None
    try:
        return float(valeur)
    except ValueError:
        return None


def signed_handicap(ligne: float | None, favori: int) -> Optional[float]:
    """Handicap signé du point de vue du DOMICILE, convention Pinnacle.

    AsianOdds publie une ligne toujours positive et un drapeau `favoured`
    (1 = domicile favori, 2 = extérieur favori, 0 = aucun). Notre référence,
    elle, signe chaque camp : home -1.0 / away +1.0. On adopte sa convention
    pour que le LIVE se compare aux books souples exactement comme le
    prématch le fait."""
    if ligne is None:
        return None
    if favori == 1:
        return -abs(ligne)
    if favori == 2:
        return abs(ligne)
    return 0.0 if ligne == 0 else abs(ligne)


def _odd(valeur: str | None) -> Optional[float]:
    """Cote décimale, ou None. Rejette l'échelle Malay résiduelle."""
    if not valeur:
        return None
    try:
        v = float(valeur)
    except (TypeError, ValueError):
        return None
    return v if v > MIN_DECIMAL_ODD else None


@dataclass(frozen=True)
class LiveRow:
    """Une sélection normalisée, prête pour `Storage.upsert_live_state`."""
    event_key: str
    market: MarketType
    outcome_label: str
    line: Optional[float]
    odd: float
    observed_at: datetime
    home_score: int
    away_score: int
    feed_score: Optional[str]
    igm: Optional[int]
    league: Optional[str]

    def as_upsert_row(self, fetched_at: datetime,
                      book: Book = Book.ASIANODDS) -> tuple:
        return (self.event_key, book.value, self.market.value,
                self.outcome_label, self.line, self.odd,
                fetched_at.isoformat(), 1, self.league,
                self.observed_at.isoformat(), self.home_score,
                self.away_score, self.feed_score, self.igm)


def normalise_evf(evf: dict, event_key: str) -> list[LiveRow]:
    """Un message EVF → les sélections exploitables qu'il contient.

    Fonction PURE : aucun réseau, aucune base, aucune horloge. Tout ce qui
    est douteux est écarté silencieusement plutôt que deviné.

    Le handicap de MI-TEMPS est ignoré : `MarketType` n'a pas de
    `HANDICAP_H1`, et inventer un type ici le ferait exister sans qu'aucun
    devig ni aucun groupement ne sache le traiter."""
    try:
        observed = datetime.fromtimestamp(evf["S"] / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return []

    hs, aw = evf.get("HS"), evf.get("AS")
    if not isinstance(hs, int) or not isinstance(aw, int):
        return []
    igm = evf.get("IGM") if isinstance(evf.get("IGM"), int) else None
    feed = decode_feed_score(evf.get("FID"))
    ligue = evf.get("LN") or None

    def ligne(**kw) -> LiveRow:
        return LiveRow(event_key=event_key, observed_at=observed,
                       home_score=hs, away_score=aw, feed_score=feed,
                       igm=igm, league=ligue, **kw)

    out: list[LiveRow] = []

    # ── 1X2, match plein et mi-temps ──────────────────────────────────
    for prefixe, marche in (("FTX", MarketType.H2H), ("HTX", MarketType.H2H_H1)):
        trio = (_odd(evf.get(prefixe + "HODD")),
                _odd(evf.get(prefixe + "DODD")),
                _odd(evf.get(prefixe + "AODD")))
        # Les trois issues ou rien : un 1X2 amputé n'est pas déviguable, et
        # le laisser passer produirait une « référence » à deux voies.
        if all(trio):
            for label, cote in zip(("home", "draw", "away"), trio):
                out.append(ligne(market=marche, outcome_label=label,
                                 line=None, odd=cote))

    # ── Over/Under, match plein et mi-temps ───────────────────────────
    for pref_ligne, o_key, u_key, marche in (
            ("FTGOAL", "FTOODDS", "FTOUODDS", MarketType.TOTALS),
            ("HTGOAL", "HTOODD", "HTUODD", MarketType.TOTALS_H1)):
        total = parse_line(evf.get(pref_ligne))
        over, under = _odd(evf.get(o_key)), _odd(evf.get(u_key))
        if total is not None and over and under:
            out.append(ligne(market=marche, outcome_label="over",
                             line=total, odd=over))
            out.append(ligne(market=marche, outcome_label="under",
                             line=total, odd=under))

    # ── Handicap asiatique, match plein SEULEMENT ─────────────────────
    hdp = signed_handicap(parse_line(evf.get("FTHDP")), evf.get("FFT") or 0)
    dom, ext = _odd(evf.get("FTHHODD")), _odd(evf.get("FTHAODD"))
    if hdp is not None and dom and ext:
        out.append(ligne(market=MarketType.HANDICAP, outcome_label="home",
                         line=hdp, odd=dom))
        out.append(ligne(market=MarketType.HANDICAP, outcome_label="away",
                         line=-hdp, odd=ext))
    return out


# ══════════════════════════════════════════════════════════════════════════
# Rapprochement des événements
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Candidat:
    event_key: str
    home: str
    away: str


def match_live_event(home: str, away: str,
                     candidats: Iterable[Candidat]) -> Optional[Candidat]:
    """Rapprocher un match AsianOdds d'un de nos événements, SUR LES NOMS.

    `matcher.match_event` exige une concordance d'horaire à 10 min près. On ne
    peut pas s'en servir ici : mesuré, l'horaire annoncé par AsianOdds en LIVE
    est quantifié sur ~10 valeurs pour 410 matchs et décalé d'environ 3 h.

    Les seuils et le garde-fou d'ambiguïté sont ceux de `match_event`, à
    l'identique : deux candidats presque aussi bons → on refuse de deviner."""
    meilleur: Optional[Candidat] = None
    score_max = second = 0.0
    for c in candidats:
        direct = (team_similarity(home, c.home) + team_similarity(away, c.away)) / 2
        inverse = (team_similarity(home, c.away) + team_similarity(away, c.home)) / 2
        score = max(direct, inverse)
        if score > score_max:
            second, score_max, meilleur = score_max, score, c
        elif score > second:
            second = score
    if score_max < MIN_MATCH_SCORE:
        return None
    if second >= MIN_MATCH_SCORE and (score_max - second) < AMBIGUITY_MARGIN:
        return None
    return meilleur


def candidats_en_cours(storage, now: datetime) -> list[Candidat]:
    """Nos événements plausiblement en cours. LECTURE SEULE sur `events`."""
    debut = (now - LIVE_WINDOW_BEFORE).isoformat()
    fin = (now + LIVE_WINDOW_AFTER).isoformat()
    with storage._conn() as c:
        lignes = c.execute(
            "SELECT event_key, home, away FROM events "
            "WHERE start_time BETWEEN ? AND ?", (debut, fin)).fetchall()
    return [Candidat(r["event_key"], r["home"], r["away"]) for r in lignes]


# ══════════════════════════════════════════════════════════════════════════
# Session : LOGIN → REGISTER → SUBSCRIBE
# ══════════════════════════════════════════════════════════════════════════
class AsianOddsSession:
    """Une connexion authentifiée au flux. Ne fait RIEN d'autre que lire.

    Aucun ordre n'est jamais émis : ni PLACEBET, ni ODDSDATAREQUEST, ni
    SAVEUSERSETTINGS. Le seul message montant après l'abonnement est un PING
    de maintien (le serveur ferme au-delà de 30 s de silence)."""

    def __init__(self, username: str, password: str, *,
                 host: str = WS_HOST, port: int = 443, tls: bool = True):
        self.username = username
        self._pwd_md5 = hashlib.md5(password.encode()).hexdigest()
        self._host, self._port, self._tls = host, port, tls
        self.ws: _WS | None = None
        self.token: str | None = None
        self.base_bookie: str | None = None

    # `timezoneoffset` déclaré à 0 : la source décale ses horaires selon cette
    # valeur, et nous n'utilisons de toute façon pas son horaire (piège 1).
    # Le fixer rend les captures comparables entre machines.
    _TZ = "0"

    def _connect(self) -> _WS:
        return _WS(self._host, WS_PATH, port=self._port, tls=self._tls)

    def open(self, timeout: float = 25.0) -> None:
        # LOGIN sur une connexion jetable. Le serveur pousse ABS/PI AVANT le
        # LoginResponse : on boucle jusqu'à le trouver au lieu de prendre la
        # première trame venue.
        ws1 = self._connect()
        try:
            ws1.send_text(json.dumps({"Command": {"Action": "LOGIN", "Message": {
                "statickey": 0, "username": self.username,
                "password": self._pwd_md5, "timezoneoffset": self._TZ,
                "domain": "ao0188.com"}}}))
            lr = None
            ws1.sock.settimeout(5)
            fin = time.time() + timeout
            while lr is None and time.time() < fin:
                try:
                    op, data = ws1.recv()
                except socket.timeout:
                    continue
                if op == 0x8:
                    break
                try:
                    d = json.loads(data.decode(errors="replace"))
                except Exception:
                    continue
                if isinstance(d, dict) and "LoginResponse" in d:
                    lr = d["LoginResponse"]
        finally:
            ws1.close()
        if lr is None:
            raise ConnectionError("aucun LoginResponse")
        if not lr.get("SuccessfulLogin"):
            raise PermissionError(f"login refusé : {lr.get('TextMessage')}")
        self.token = lr["Token"]

        # REGISTER (< 60 s) sur la connexion de flux.
        ws = self._connect()
        ws.send_text(json.dumps({"Command": {"Action": "REGISTER", "Message": {
            "clientType": 3, "username": self.username, "token": self.token,
            "key": lr["Key"], "timezoneoffset": self._TZ,
            "domain": "ao0188.com", "webVersion": 4}}}))
        self.ws = ws

    def subscribe(self, sport: int = SPORT_FOOTBALL) -> None:
        if self.ws is None:
            raise RuntimeError("session non ouverte")
        # `displaystyle: "asian"` ramène EVF *et* AVF sur le même abonnement :
        # couverture complète par l'EVF, provenance par l'AVF quand elle
        # existe. `isBestOdds: 0` — mesuré, BEST ne gagne que 0,1 point
        # d'overround sur l'ensemble et vaut PIN dans 81 % des marchés.
        self.ws.send_text(json.dumps({"Command": {"Action": "SUBSCRIBE", "Message": {
            "token": self.token, "markettype": MARKET_TYPE_LIVE,
            "isBestOdds": 0, "isParlay": 0, "lname": "", "lsid": "",
            "matchid": "", "oddsformat": ODDS_FORMAT_EU, "sportstype": sport,
            "isOnwards": 0, "dateFilter": "", "issendleagues": 0,
            "isfeatured": 0, "customvalue": "", "displaystyle": "asian"}}}))

    def ping(self) -> None:
        if self.ws is not None:
            self.ws.send_text(json.dumps(
                {"Command": {"Action": "PING", "Message": {"token": self.token}}}))

    def messages(self, timeout: float = 5.0) -> Iterator[dict]:
        """Les messages décodés, jusqu'à fermeture. `None` sur inactivité."""
        if self.ws is None:
            raise RuntimeError("session non ouverte")
        self.ws.sock.settimeout(timeout)
        while True:
            try:
                op, data = self.ws.recv()
            except socket.timeout:
                yield {}                       # tick d'inactivité
                continue
            if op == 0x8:
                return
            try:
                d = json.loads(data.decode(errors="replace"))
            except Exception:
                continue
            if isinstance(d, dict):
                if "US" in d:
                    self.base_bookie = d["US"].get("BB")
                yield d

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None


# ══════════════════════════════════════════════════════════════════════════
# Boucle de collecte
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class Stats:
    """Ce qu'on veut savoir sans lire les logs : ce qui entre, ce qui sort,
    et surtout ce qui est PERDU et pourquoi."""
    messages: int = 0
    evf: int = 0
    normalises: int = 0
    ecrits: int = 0
    sans_event_key: int = 0          # match AsianOdds inconnu chez nous
    sans_selection: int = 0          # EVF sans une seule cote exploitable
    reconnexions: int = 0

    def resume(self) -> str:
        appariés = self.evf - self.sans_event_key
        taux = f"{100 * appariés / self.evf:.1f} %" if self.evf else "n/a"
        return (f"msg={self.messages} evf={self.evf} appariés={appariés} ({taux}) "
                f"sélections={self.normalises} écrites={self.ecrits} "
                f"sans_match={self.sans_event_key} vides={self.sans_selection} "
                f"reconnexions={self.reconnexions}")


# Cadence d'écriture. Le flux pousse ~30 messages/s ; écrire chaque message
# séparément ferait ~30 transactions/s sur la même base SQLite que le
# prématch, qui souffre déjà de « database is locked ». On accumule et on
# écrit par lots : une transaction toutes les FLUSH_SEC, quel que soit le
# débit. Les doublons du lot sont écrasés en mémoire avant écriture, donc un
# marché repricé trois fois dans l'intervalle ne coûte qu'une ligne.
FLUSH_SEC = 5.0
PING_SEC = 20.0
# Le rapprochement relit `events` : une fois par minute suffit, un match
# n'apparaît pas plus vite que ça.
REFRESH_CANDIDATS_SEC = 60.0


def collect(storage, username: str, password: str, *,
            duration_sec: float | None = None,
            sport: int = SPORT_FOOTBALL,
            dry_run: bool = False,
            session_factory=None,
            now_fn=None,
            log=print) -> Stats:
    """Collecter jusqu'à `duration_sec` (None = sans fin). Renvoie les stats.

    `dry_run=True` normalise et rapproche sans jamais écrire : c'est le mode
    qui permet de mesurer le taux d'appariement avant d'autoriser la moindre
    écriture en production.

    `session_factory` et `now_fn` existent pour les tests : ils permettent
    d'injecter une fausse session et une horloge figée, donc de tester la
    boucle entière sans réseau."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    stats = Stats()
    fabrique = session_factory or (
        lambda: AsianOddsSession(username, password))

    fin = None if duration_sec is None else time.monotonic() + duration_sec
    session = fabrique()
    session.open()
    session.subscribe(sport=sport)
    log(f"[ao] abonné (base bookie = {session.base_bookie})")

    candidats: list[Candidat] = []
    prochain_refresh = 0.0
    lot: dict[tuple, tuple] = {}
    dernier_flush = dernier_ping = time.monotonic()

    def vider() -> None:
        nonlocal lot
        if lot and not dry_run:
            stats.ecrits += storage.upsert_live_state(list(lot.values()))
        elif lot:
            stats.ecrits += len(lot)
        lot = {}

    try:
        for msg in session.messages():
            maintenant = time.monotonic()
            if fin is not None and maintenant > fin:
                break
            if msg:
                stats.messages += 1

            if maintenant >= prochain_refresh:
                candidats = candidats_en_cours(storage, now_fn())
                prochain_refresh = maintenant + REFRESH_CANDIDATS_SEC

            evf = msg.get("EVF") if msg else None
            if evf:
                stats.evf += 1
                cible = match_live_event(
                    evf.get("HN") or "", evf.get("AN") or "", candidats)
                if cible is None:
                    stats.sans_event_key += 1
                else:
                    lignes = normalise_evf(evf, cible.event_key)
                    if not lignes:
                        stats.sans_selection += 1
                    stats.normalises += len(lignes)
                    horodatage = now_fn()
                    for l in lignes:
                        # Clé naturelle : un même marché repricé plusieurs fois
                        # dans le lot ne produit qu'une écriture, la dernière.
                        lot[(l.event_key, l.market.value, l.outcome_label,
                             l.line)] = l.as_upsert_row(horodatage)

            if maintenant - dernier_flush >= FLUSH_SEC:
                vider()
                dernier_flush = maintenant
            if maintenant - dernier_ping >= PING_SEC:
                session.ping()
                dernier_ping = maintenant
    finally:
        vider()
        session.close()
    return stats
