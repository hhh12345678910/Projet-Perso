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
import sqlite3
import ssl
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Optional

from .matcher import normalize_team, team_similarity
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

# Nos `events.sport` <- le `sportstype` de l'abonnement AsianOdds. Sans ce
# lien, on compare du football a TOUS nos sports confondus et le taux de
# couverture est mecaniquement sous-estime.
SPORT_VERS_NOTRE_NOM = {1: "soccer", 2: "basketball", 3: "tennis"}

# SpecialMatchType : "0" = vrai match. Tout le reste est un PSEUDO-EVENEMENT
# derive — "1" corners, "2" cartons, "3" to advance, "4" to win, "5"/"6"
# team totals domicile/exterieur, "7"/"8" team totals corners.
#
# ⚠️ PIEGE MESURE, ET IL CORROMPRAIT market_state : ces pseudo-evenements
# portent le NOM DE LA VRAIE EQUIPE suivi d'un suffixe. Verifie :
#   team_similarity("Stjarnan Team Totals Home Team", "Stjarnan") == 100.0
# Sans ce filtre, un « Team Totals over 2.5 » s'ecrit sous la cle du vrai
# match comme s'il en etait le total. Le rapprochement sur les noms ne peut
# PAS les distinguer : on s'appuie donc sur le champ que la source fournit.
SPMT_VRAI_MATCH = "0"


def _meme_sport(stp, sport: int) -> bool:
    """L'EVF appartient-il au sport demandé par l'abonnement ?

    `STP` (SportsType) porte le meme codage que le `sportstype` envoye au
    SUBSCRIBE. Un champ absent ou illisible est CONSERVE : on ne jette pas un
    message sur une donnee qu'on n'a pas su lire, le rapprochement tranchera.
    """
    if stp is None or stp == "":
        return True
    try:
        return int(stp) == sport
    except (TypeError, ValueError):
        return True


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
                # Le payload porte le code (2 octets) et le motif. Il etait
                # jete : une fermeture serveur devenait alors indiscernable
                # d'une fin normale, ce qui a fait passer un run vide de
                # 34 s pour un run reussi de 5 min.
                return 0x8, data
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
    #: D'OU vient cette ligne. Sans ces trois champs, une collision reste
    #: indiagnosticable apres coup : c'est ce qui a bloque l'enquete du 24/08.
    source_event_id: Optional[str] = None
    source_inverse: bool = False
    matched_at: Optional[datetime] = None

    def as_upsert_row(self, fetched_at: datetime,
                      book: Book = Book.ASIANODDS) -> tuple:
        return (self.event_key, book.value, self.market.value,
                self.outcome_label, self.line, self.odd,
                fetched_at.isoformat(), 1, self.league,
                self.observed_at.isoformat(), self.home_score,
                self.away_score, self.feed_score, self.igm,
                self.source_event_id, int(self.source_inverse),
                self.matched_at.isoformat() if self.matched_at else None)


def _inverser_score(txt: "str | None") -> "str | None":
    """« 1:0 » → « 0:1 ». None reste None."""
    if not txt or ":" not in txt:
        return txt
    h, _, a = txt.partition(":")
    return f"{a}:{h}"


def normalise_evf(evf: dict, event_key: str, *,
                  inverse: bool = False,
                  matched_at: "datetime | None" = None) -> list[LiveRow]:
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

    # ── Orientation ───────────────────────────────────────────────────
    # La source peut annoncer le match DANS L'AUTRE SENS : son domicile est
    # notre exterieur. Le rapprochement l'accepte (il compare aussi les noms
    # croises) mais les prix, eux, arrivent dans SON ordre. Sans permutation,
    # la cote du domicile adverse s'ecrit sous notre `home` — mesure : 1.30
    # ecrit la ou il fallait 9.00, et le score a l'envers avec.
    #
    # `feed_score` DOIT suivre : il sert a reperer un prix perime en le
    # comparant a home_score/away_score. Le laisser dans l'orientation de la
    # source les ferait diverger EN PERMANENCE, et le detecteur de peremption
    # crierait sur tous les matchs inverses.
    if inverse:
        hs, aw = aw, hs
        feed = _inverser_score(feed)

    # `MTCHID` est l'identifiant du match CHEZ ASIANODDS : il voyage avec le
    # message, il n'a pas a etre passe par l'appelant.
    source = evf.get("MTCHID")
    source = str(source) if source is not None else None

    def ligne(**kw) -> LiveRow:
        return LiveRow(event_key=event_key, observed_at=observed,
                       home_score=hs, away_score=aw, feed_score=feed,
                       igm=igm, league=ligue, source_event_id=source,
                       source_inverse=inverse, matched_at=matched_at, **kw)

    out: list[LiveRow] = []

    # ── 1X2, match plein et mi-temps ──────────────────────────────────
    for prefixe, marche in (("FTX", MarketType.H2H), ("HTX", MarketType.H2H_H1)):
        trio = (_odd(evf.get(prefixe + "HODD")),
                _odd(evf.get(prefixe + "DODD")),
                _odd(evf.get(prefixe + "AODD")))
        if inverse:                      # notre domicile est leur exterieur
            trio = (trio[2], trio[1], trio[0])
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
    if inverse and hdp is not None:
        # La ligne est SIGNEE selon le favori annonce par la source. En
        # renversant les cotes il faut renverser le signe avec, sinon le
        # handicap decrit l'avantage de l'equipe adverse.
        hdp, dom, ext = -hdp, ext, dom
    elif inverse:
        dom, ext = ext, dom
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
    # Les deux champs suivants sont KEYWORD-ONLY a dessein. Inserer `league`
    # devant `start_time` a silencieusement fait absorber des datetime par
    # `league` chez tous les appelants positionnels — attrape par les tests,
    # mais rien ne garantissait qu'ils couvrent tout. En keyword-only, un
    # appel positionnel echoue bruyamment au lieu de mal ranger la valeur.
    #
    #: La competition, telle que NOTRE reference la nomme. Sert a refuser un
    #: faux doublon : des noms d'equipes identiques ne suffisent pas a faire
    #: une meme rencontre (voir `_meme_rencontre`).
    league: Optional[str] = field(default=None, kw_only=True)
    #: L'heure annoncee par NOTRE reference. Sert uniquement au diagnostic :
    #: un « orphelin » qui a debute il y a 3 h est fini depuis longtemps et
    #: gonfle le denominateur sans qu'AsianOdds y soit pour quelque chose.
    start_time: Optional[datetime] = field(default=None, kw_only=True)


@dataclass(frozen=True)
class Appariement:
    """Le résultat d'un rapprochement, AVEC son motif.

    « Aucun candidat assez proche » et « deux candidats trop proches »
    produisent le même silence mais appellent des travaux opposés : le
    premier est un trou de couverture chez nous, le second un doublon dans
    `events`. Les confondre, c'est chercher au mauvais endroit."""
    cible: Optional[Candidat]
    score: float
    second: float
    motif: str
    #: Les AUTRES clés qui désignent la même rencontre. Voir
    #: `evaluer_appariement` : mesuré en base, un même match existe jusqu'à
    #: cinq fois sous cinq horaires différents.
    doublons: tuple = ()
    #: L'orientation retenue POUR CHAQUE CLE, et non une seule pour toutes.
    #:
    #: Deux clés de la même rencontre peuvent être stockées dans des sens
    #: OPPOSES — mesuré en base le 24/08 : `cerrolargo__vs__centralespanol`
    #: coexiste avec `centralespanol__vs__cerrolargo`. Un drapeau unique
    #: serait donc juste pour l'une et faux pour l'autre, et permuterait les
    #: prix du mauvais côté sur la seconde.
    inverse_par_cle: dict = field(default_factory=dict)

    @property
    def reussi(self) -> bool:
        return self.cible is not None

    @property
    def inverse(self) -> bool:
        """La source annonçait-elle la cible principale à l'envers ?"""
        return bool(self.cible is not None
                    and self.inverse_par_cle.get(self.cible.event_key))

    @property
    def toutes_les_cibles(self) -> tuple:
        return () if self.cible is None else (self.cible,) + self.doublons

    def inverse_pour(self, candidat) -> bool:
        return bool(self.inverse_par_cle.get(candidat.event_key))


#: Au-dela, deux rencontres. Un doublon d'horaire observe en base va de
#: 30 min (torpedozhodino 17:00/17:30) a quelques heures ; un match du
#: lendemain, lui, n'est pas le meme match.
ECART_MEME_RENCONTRE = timedelta(hours=3)


def _ligue_normalisee(nom: "str | None") -> str:
    return " ".join((nom or "").lower().split())


def _meme_rencontre(a: Candidat, b: Candidat) -> bool:
    """Deux de NOS candidats désignent-ils LE MÊME match ?

    Trois conditions, toutes nécessaires. En cas de doute : NON. Un doublon
    non reconnu coûte une couverture partielle ; un faux doublon écrit le
    prix d'un match sous la clé d'un autre.

    1. ÉGALITÉ EXACTE des noms normalisés, pas une ressemblance. Une
       ressemblance au seuil de rapprochement juge « Sporting CP » et
       « Sporting Gijon » identiques, et produirait précisément la corruption
       que le garde-fou d'ambiguïté existe pour empêcher.

    2. MÊME COMPÉTITION. Les noms d'équipes ne portent pas la catégorie :
       mesuré en base le 24/08, `kocaelispor — amedspor` existe en
       « Turkey - Super League » ET en « Turkey - Super Lig U19 », et
       `nacionalasuncion — sportivoluqueno` en « Division Profesional » ET
       en « Reserve League ». Ce sont QUATRE rencontres, pas deux. Le
       helper `class_marker_from_league` du projet attrape le U19 mais PAS
       les équipes réserves — vérifié : il rend "" sur « Reserve League ».
       L'égalité de la ligue couvre les deux, sans rien ajouter à
       `matcher.py`, dont dépend tout le prématch.
       Une ligue absente est un DOUTE, donc un refus : la base en contient.

    3. HORAIRES COMPATIBLES. Le doublon vient de l'horaire qui entre dans
       `event_key` ; passé quelques heures, ce n'est plus le même match.
    """
    x = (normalize_team(a.home), normalize_team(a.away))
    y = (normalize_team(b.home), normalize_team(b.away))
    if x != y and x != y[::-1]:
        return False
    la, lb = _ligue_normalisee(a.league), _ligue_normalisee(b.league)
    if not la or not lb or la != lb:
        return False
    if a.start_time is None or b.start_time is None:
        return False
    da, db = a.start_time, b.start_time
    if da.tzinfo is None:
        da = da.replace(tzinfo=timezone.utc)
    if db.tzinfo is None:
        db = db.replace(tzinfo=timezone.utc)
    return abs(da - db) <= ECART_MEME_RENCONTRE


def evaluer_appariement(home: str, away: str,
                        candidats: Iterable[Candidat]) -> Appariement:
    """Rapprocher un match AsianOdds d'un de nos événements, SUR LES NOMS.

    `matcher.match_event` exige une concordance d'horaire à 10 min près. On ne
    peut pas s'en servir ici : mesuré, l'horaire annoncé par AsianOdds en LIVE
    est quantifié sur ~10 valeurs pour 410 matchs et décalé d'environ 3 h.

    Les seuils et le garde-fou d'ambiguïté sont ceux de `match_event`, à
    l'identique : deux candidats presque aussi bons → on refuse de deviner."""
    notes = []
    sens = {}
    for c in candidats:
        direct = (team_similarity(home, c.home) + team_similarity(away, c.away)) / 2
        renverse = (team_similarity(home, c.away) + team_similarity(away, c.home)) / 2
        # `>` et non `>=` : a egalite parfaite — deux noms identiques des deux
        # cotes — on garde l'orientation annoncee plutot que de la retourner
        # sans raison.
        sens[c.event_key] = renverse > direct
        notes.append((max(direct, renverse), c))
    if not notes:
        return Appariement(None, 0.0, 0.0, "aucun candidat en cours")
    notes.sort(key=lambda t: t[0], reverse=True)
    score_max, meilleur = notes[0]
    second = notes[1][0] if len(notes) > 1 else 0.0

    if score_max < MIN_MATCH_SCORE:
        return Appariement(None, score_max, second,
                           f"aucun candidat assez proche (meilleur {score_max:.0f} "
                           f"< {MIN_MATCH_SCORE:.0f})")

    proches = [c for note, c in notes[1:]
               if note >= MIN_MATCH_SCORE and (score_max - note) < AMBIGUITY_MARGIN]
    if not proches:
        return Appariement(meilleur, score_max, second, "apparié",
                           inverse_par_cle={meilleur.event_key:
                                            sens[meilleur.event_key]})

    # Ex aequo. Deux cas OPPOSÉS derrière le même symptôme :
    #
    #   1. Nos candidats sont des rencontres DIFFÉRENTES qui se ressemblent
    #      (deux équipes réserves, deux homonymes). Deviner écrirait un prix
    #      sous la clé d'un autre match : on refuse, comme avant.
    #   2. Nos candidats sont LA MÊME rencontre, dupliquée dans `events`
    #      parce que deux books annoncent deux coups d'envoi et que l'heure
    #      entre dans la clé. Mesuré en base : jusqu'à CINQ clés pour un
    #      match (blackpool—lincolncity, 14:00/15:00/16:00/18:45). Refuser
    #      ici perdait un match correctement identifié à 96/100 pour un
    #      défaut qui est chez nous, pas dans la source.
    #
    # Corriger `events` releve du prematch, hors perimetre. On ecrit donc
    # sous TOUTES les cles de la meme rencontre : le prix est le meme, et
    # rien ne dit laquelle des cles le moteur consultera.
    if all(_meme_rencontre(meilleur, c) for c in proches):
        retenus = [meilleur] + proches
        return Appariement(meilleur, score_max, second,
                           f"apparié (+{len(proches)} doublon(s) dans events)",
                           tuple(proches),
                           {c.event_key: sens[c.event_key] for c in retenus})
    autre = next(c for c in proches if not _meme_rencontre(meilleur, c))
    return Appariement(
        None, score_max, second,
        f"ambiguïté {score_max:.0f} vs {second:.0f} entre DEUX rencontres — "
        f"« {meilleur.home} — {meilleur.away} » et "
        f"« {autre.home} — {autre.away} »")


def match_live_event(home: str, away: str,
                     candidats: Iterable[Candidat]) -> Optional[Candidat]:
    """La cible seule, quand le motif n'intéresse pas l'appelant."""
    return evaluer_appariement(home, away, candidats).cible


def candidats_en_cours(storage, now: datetime,
                       sport: int = SPORT_FOOTBALL) -> list[Candidat]:
    """Nos événements plausiblement en cours, DU SPORT ABONNÉ.

    LECTURE SEULE sur `events`. Le filtre de sport n'est pas cosmétique :
    sans lui, un abonnement football se compare aussi à nos matchs de tennis
    et de basket, qu'AsianOdds ne peut par construction pas couvrir — le taux
    de couverture s'effondre pour une raison qui n'a rien à voir avec la
    source."""
    debut = (now - LIVE_WINDOW_BEFORE).isoformat()
    fin = (now + LIVE_WINDOW_AFTER).isoformat()
    notre_sport = SPORT_VERS_NOTRE_NOM.get(sport)
    sql = ("SELECT event_key, home, away, league, start_time FROM events "
           "WHERE start_time BETWEEN ? AND ?")
    args: list = [debut, fin]
    if notre_sport:
        sql += " AND sport = ?"
        args.append(notre_sport)
    with storage._conn() as c:
        lignes = c.execute(sql, args).fetchall()
    return [Candidat(r["event_key"], r["home"], r["away"],
                     league=r["league"],
                     start_time=_horaire(r["start_time"])) for r in lignes]


def _horaire(brut) -> Optional[datetime]:
    """Un horaire illisible n'empêche pas le rapprochement : il ne sert qu'au
    diagnostic. On renvoie None plutôt que de lever."""
    try:
        return datetime.fromisoformat(brut)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════
# Session : LOGIN → REGISTER → SUBSCRIBE
# ══════════════════════════════════════════════════════════════════════════
#: Codes RFC 6455 et codes applicatifs vus sur ce flux. Un code seul ne dit
#: rien a la lecture ; ce sont ces libelles qui distinguent « le serveur nous
#: a jetes » de « le reseau a laché ».
_CODES_FERMETURE = {
    1000: "fermeture normale", 1001: "serveur en arret",
    1002: "erreur de protocole", 1003: "type de donnee refuse",
    1006: "fermeture anormale (aucune trame recue)",
    1008: "regle violee — souvent une session ouverte ailleurs",
    1011: "erreur interne du serveur", 1012: "redemarrage du serveur",
    1013: "reessayer plus tard (surcharge)",
}


def decrire_fermeture(payload: bytes) -> str:
    """Le code et le motif d'une trame de fermeture, en clair."""
    if not payload or len(payload) < 2:
        return "fermeture sans code"
    code = struct.unpack(">H", payload[:2])[0]
    motif = payload[2:].decode("utf-8", errors="replace").strip()
    libelle = _CODES_FERMETURE.get(code, "code inconnu")
    return f"fermeture {code} ({libelle})" + (f" : {motif}" if motif else "")


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
        #: Renseigne quand le serveur ferme, ou que la connexion se perd.
        #: `None` tant que le flux vit.
        self.fermeture: str | None = None

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
            except (ConnectionError, ssl.SSLError, OSError) as e:
                self.fermeture = f"connexion perdue : {e!r}"
                return
            if op == 0x8:
                self.fermeture = decrire_fermeture(data)
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


#: Durée au-delà de laquelle un match de football est presque sûrement fini
#: (2 × 45 min + mi-temps + arrêts de jeu, avec de la marge). Sert UNIQUEMENT
#: au diagnostic : rien n'est filtré sur cette base, on ne connaît pas l'état
#: réel du match, seulement l'heure annoncée par notre référence.
EN_JEU_MAX_MIN = 135


def _minutes_depuis_coup_denvoi(c, maintenant: datetime) -> "int | None":
    if c.start_time is None:
        return None
    debut = c.start_time
    if debut.tzinfo is None:
        debut = debut.replace(tzinfo=timezone.utc)
    return int((maintenant - debut).total_seconds() // 60)


def plausiblement_en_jeu(candidats, maintenant: datetime) -> list:
    """Ceux de nos candidats qui peuvent réellement être en cours.

    `candidats_en_cours` remonte jusqu'à LIVE_WINDOW_BEFORE = 4 h alors qu'un
    match en dure 2 : le dénominateur contient des matchs TERMINÉS et des
    matchs pas encore commencés. Mesuré le 24/08 : 21 de nos 46 « orphelins »
    étaient finis. Le taux brut accuse alors AsianOdds d'un trou qui n'est
    pas le sien. Un horaire absent est CONSERVÉ : on ne retire pas un
    candidat sur une donnée manquante.
    """
    gardes = []
    for c in candidats:
        m = _minutes_depuis_coup_denvoi(c, maintenant)
        if m is None or 0 <= m <= EN_JEU_MAX_MIN:
            gardes.append(c)
    return gardes


def _anciennete(c, maintenant: datetime) -> str:
    """« débuté il y a 3 h 12 » : un match de football dure environ 2 h. Nos
    candidats vont jusqu'à LIVE_WINDOW_BEFORE en arrière, donc une partie des
    « orphelins » est simplement TERMINÉE et gonfle le dénominateur sans
    qu'AsianOdds y soit pour quoi que ce soit."""
    m = _minutes_depuis_coup_denvoi(c, maintenant)
    if m is None:
        return ""
    if m < 0:
        return f"   (débute dans {-m} min)"
    fini = "  ⟵ probablement TERMINÉ" if m > EN_JEU_MAX_MIN else ""
    return f"   (débuté il y a {m // 60} h {m % 60:02d}){fini}"


def diagnostic_appariement(stats, limite: "int | None" = 15) -> str:
    """Les deux listes cote a cote, pour trancher a l'oeil.

    Si la MEME rencontre figure dans les deux colonnes sous deux
    orthographes, c'est le rapprochement qui echoue et il faut le durcir.
    Si les listes n'ont rien de commun, AsianOdds ne couvre simplement pas
    ces matchs, et aucun travail sur le rapprochement n'y changera rien."""
    couverts = stats.evenements_couverts
    orphelins = [c for c in stats.derniers_candidats
                 if c.event_key not in couverts]
    maintenant = datetime.now(timezone.utc)
    lignes = [
        "",
        "─" * 74,
        "DIAGNOSTIC — la même rencontre apparaît-elle des deux côtés ?",
        "─" * 74,
        f"NOS matchs en cours SANS référence AsianOdds ({len(orphelins)}) :",
    ]
    for c in (orphelins if limite is None else orphelins[:limite]):
        lignes.append(f"    {c.home} — {c.away}{_anciennete(c, maintenant)}")
    if limite is not None and len(orphelins) > limite:
        lignes.append(f"    … et {len(orphelins) - limite} autres")
    lignes += ["",
               f"Matchs AsianOdds NON rapprochés "
               f"({len(stats.asianodds_sans_match)}) :"]
    if stats.motifs_echec:
        lignes += ["", "Motifs d'échec du rapprochement :"]
        lignes += [f"    {n:>5} × {m}"
                   for m, n in stats.motifs_echec.most_common()]
    inconnus = list(stats.asianodds_sans_match.values())
    for nom in (inconnus if limite is None else inconnus[:limite]):
        lignes.append(f"    {nom}")
    if limite is not None and len(inconnus) > limite:
        lignes.append(f"    … et {len(inconnus) - limite} autres")
    return "\n".join(lignes)


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
    derives: int = 0                 # pseudo-evenements (team totals, corners)
    hors_sport: int = 0              # EVF d'un autre sport que l'abonnement
    sans_event_key: int = 0          # match AsianOdds inconnu chez nous
    sans_selection: int = 0          # EVF sans une seule cote exploitable
    reconnexions: int = 0
    #: Le TYPE des messages recus, pas seulement leur nombre. « msg=31
    #: evf=0 » ne dit pas si le serveur a envoye 31 battements de coeur ou
    #: 31 refus ; ce compteur le dit.
    types_messages: Counter = field(default_factory=Counter)
    #: Pourquoi la boucle s'est arretee. Une fin prematuree ressemblait a
    #: une fin normale, avec un taux de couverture de 0 % qui accusait la
    #: couverture d'AsianOdds au lieu de la connexion.
    fin_raison: str = "boucle jamais entree"
    #: Pourquoi les rapprochements ont echoue, par motif. Un echec de seuil
    #: et une ambiguite se corrigent a des endroits opposes.
    motifs_echec: Counter = field(default_factory=Counter)
    #: Nos evenements pour lesquels `events` porte plusieurs cles. Le prix
    #: est ecrit sous chacune : le defaut est chez nous, pas dans la source.
    doublons_events: set = field(default_factory=set)
    #: Mesure de l'ECRITURE. Le collecteur partage sa base SQLite avec le
    #: daemon prematch, qui souffre deja de « database is locked » : ces
    #: chiffres sont la seule facon de savoir si on aggrave son sort.
    transactions: int = 0
    sqlite_busy: int = 0
    sqlite_echecs: int = 0
    ecritures_ms: list = field(default_factory=list)

    def ecriture_resume(self) -> str:
        if not self.ecritures_ms:
            return "aucune transaction"
        v = sorted(self.ecritures_ms)
        n = len(v)
        p = lambda q: v[min(n - 1, int(q * n))]  # noqa: E731
        return (f"transactions={self.transactions} "
                f"lignes={self.ecrits} "
                f"p50={p(0.50):.1f} ms p95={p(0.95):.1f} ms "
                f"max={v[-1]:.1f} ms total={sum(v) / 1000:.2f} s "
                f"busy={self.sqlite_busy} echecs={self.sqlite_echecs}")
    # Comptes PAR MATCH, et non par message. Un match liquide reprice 200
    # fois quand un match calme reprice 3 fois : pondere par message, le
    # taux d'appariement decrit surtout les gros matchs. Ce sont ces
    # ensembles-la qui repondent a « combien de matchs sait-on lire ».
    matchs_vus: set = field(default_factory=set)
    matchs_apparies: set = field(default_factory=set)
    evenements_couverts: set = field(default_factory=set)
    candidats_connus: int = 0
    # event_key revendique par PLUSIEURS matchs AsianOdds : signe qu'au moins
    # un rapprochement est faux.
    origine_par_event: dict = field(default_factory=dict)
    collisions: set = field(default_factory=set)
    # Pour le diagnostic : « AsianOdds ne couvre pas ce match » et « le
    # rapprochement a echoue sur ce match » produisent le MEME chiffre mais
    # appellent des travaux opposes. Seule la confrontation des deux listes
    # permet de trancher, a l'oeil.
    asianodds_sans_match: dict = field(default_factory=dict)
    derniers_candidats: list = field(default_factory=list)

    def resume(self) -> str:
        # Taux calcule sur les VRAIS matchs : inclure les derives le
        # gonflerait d'evenements qu'on ne veut de toute facon pas.
        reels = self.evf - self.derives - self.hors_sport
        appariés = reels - self.sans_event_key
        taux = f"{100 * appariés / reels:.1f} %" if reels else "n/a"
        return (f"msg={self.messages} evf={self.evf} dérivés={self.derives} "
                f"hors_sport={self.hors_sport} "
                f"appariés={appariés} ({taux}) "
                f"sélections={self.normalises} écrites={self.ecrits} "
                f"sans_match={self.sans_event_key} vides={self.sans_selection} "
                f"reconnexions={self.reconnexions}\n"
                f"       fin : {self.fin_raison}\n"
                f"       types recus : {self.types_recus()}")

    def types_recus(self) -> str:
        if not self.types_messages:
            return "aucun"
        return " ".join(f"{k}={n}" for k, n in
                        self.types_messages.most_common())

    def couverture(self) -> str:
        """Les deux taux qui decident, comptes par MATCH.

        Le second est le plus important : un match qu'AsianOdds cote et que
        nous ignorons n'est pas une perte, puisque nos books belges ne le
        proposent pas non plus et qu'aucune value n'y est jouable. La vraie
        question est l'inverse : de NOS matchs en cours, combien AsianOdds
        nous en donne-t-il un prix ?"""
        # Sans un seul EVF, « 0,0 % de couverture » accuse AsianOdds de ne
        # pas coter nos matchs alors que le flux n'a rien envoye du tout.
        # Deux causes opposees, un seul chiffre : il faut le dire.
        if self.evf == 0:
            return (f"AUCUN EVF RECU — le taux de couverture ne veut rien "
                    f"dire ici.\n       {self.candidats_connus} de nos "
                    f"evenements etaient en cours ; le flux n'a rien cote.")
        vus, app = len(self.matchs_vus), len(self.matchs_apparies)
        t1 = f"{100 * app / vus:.1f} %" if vus else "n/a"
        couv, cand = len(self.evenements_couverts), self.candidats_connus
        t2 = f"{100 * couv / cand:.1f} %" if cand else "n/a"
        # Le taux brut compte des matchs finis et des matchs pas commences.
        # On donne les deux : le brut reste comparable d'un run a l'autre, le
        # corrige est celui qui decrit la source.
        en_jeu = plausiblement_en_jeu(self.derniers_candidats,
                                      datetime.now(timezone.utc))
        cles = {c.event_key for c in en_jeu}
        n_jeu = len(cles)
        couv_jeu = len(cles & self.evenements_couverts)
        t3 = f"{100 * couv_jeu / n_jeu:.1f} %" if n_jeu else "n/a"
        corrige = (f"\n       dont plausiblement EN JEU={n_jeu} "
                   f"(≤ {EN_JEU_MAX_MIN} min de jeu) couverts={couv_jeu} "
                   f"({t3})   <<< le taux honnête"
                   if self.derniers_candidats else "")
        dbl = (f"\n       ({len(self.doublons_events)} rencontre(s) portant "
               f"plusieurs clés dans events : prix écrit sous chacune)"
               if self.doublons_events else "")
        col = (f"\n       /!\\ {len(self.collisions)} de nos evenements "
               f"revendiques par PLUSIEURS matchs AsianOdds : au moins un "
               f"rapprochement est faux" if self.collisions else "")
        return (f"matchs AsianOdds reels={vus} apparies={app} ({t1})\n"
                f"       NOS evenements en cours={cand} couverts par "
                f"AsianOdds={couv} ({t2})" + corrige + dbl + col)


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


# Reconnexion. Le flux se fait couper : mesure le 24/08, une session de
# 5 min s'est arretee a 34 s. Sans reprise, le collecteur s'arrete au premier
# incident et le silence qui suit ressemble a « AsianOdds ne cote plus rien ».
#: Ecriture SQLite. La base est partagee avec le daemon prematch : un
#: `database is locked` est attendu, pas exceptionnel.
SQLITE_TENTATIVES = 4
SQLITE_ATTENTE_SEC = 0.25

RECONNEXION_DELAI_INITIAL = 2.0
RECONNEXION_DELAI_MAX = 30.0
#: Au-dela, on abandonne : s'acharner sur un serveur qui refuse n'aide pas et
#: peut faire verrouiller le compte.
RECONNEXION_ECHECS_MAX = 5
#: Une session qui a tenu ce temps-la est consideree saine : le delai
#: d'attente repart de zero. Sans cela, une coupure toutes les 10 min finirait
#: par imposer 30 s d'attente a chaque fois.
SESSION_STABLE_SEC = 60.0


def collect(storage, username: str, password: str, *,
            duration_sec: float | None = None,
            sport: int = SPORT_FOOTBALL,
            dry_run: bool = False,
            session_factory=None,
            now_fn=None,
            dormir=time.sleep,
            reconnecter: bool = False,
            log=print) -> Stats:
    """Collecter jusqu'à `duration_sec` (None = sans fin). Renvoie les stats.

    `dry_run=True` normalise et rapproche sans jamais écrire : c'est le mode
    qui permet de mesurer le taux d'appariement avant d'autoriser la moindre
    écriture en production.

    Reprend automatiquement quand le serveur coupe, tant qu'il reste du temps.
    Un refus d'identifiants n'est JAMAIS réessayé : s'acharner ne corrigerait
    rien et peut faire verrouiller le compte.

    La reprise demande une ÉCHÉANCE : sans `duration_sec`, il n'existe pas de
    « temps restant », donc rien qui borne les tentatives — une session qui se
    termine rendrait la main immédiatement, en boucle. Un appelant qui veut un
    flux sans fin doit le dire avec `reconnecter=True` ; il accepte alors une
    boucle qui ne s'arrête que sur `RECONNEXION_ECHECS_MAX` échecs d'affilée.

    `session_factory`, `now_fn` et `dormir` existent pour les tests : ils
    permettent d'injecter une fausse session, une horloge figée et une attente
    instantanée, donc de tester la boucle entière sans réseau ni délai."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    stats = Stats()
    fabrique = session_factory or (
        lambda: AsianOddsSession(username, password))

    fin = None if duration_sec is None else time.monotonic() + duration_sec
    candidats: list[Candidat] = []
    # Quand la liste des candidats a-t-elle ete relue ? C'est CET instant que
    # `matched_at` porte : il dit si le rapprochement a ete decide contre une
    # liste fraiche ou vieille de 59 s. `fetched_at` repond deja a « quand
    # avons-nous ecrit », une deuxieme colonne pour la meme chose ne
    # servirait a rien.
    candidats_datant_de: "datetime | None" = None
    prochain_refresh = 0.0
    lot: dict[tuple, tuple] = {}
    bookie_annonce = False
    delai = RECONNEXION_DELAI_INITIAL
    echecs = 0

    def vider() -> None:
        nonlocal lot
        if not lot:
            return
        rows = list(lot.values())
        lot = {}
        if dry_run:
            stats.ecrits += len(rows)
            return
        # `database is locked` n'est pas une erreur du collecteur : c'est le
        # daemon prematch qui tient la base. On reessaie brievement plutot
        # que de perdre le lot, et on COMPTE — c'est ce chiffre qui dira si
        # le collecteur peut cohabiter avec le prematch.
        for tentative in range(SQLITE_TENTATIVES):
            t0 = time.perf_counter()
            try:
                n = storage.upsert_live_state(rows)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e).lower():
                    stats.sqlite_echecs += 1
                    log(f"[ao] écriture refusée : {e!r}")
                    return
                stats.sqlite_busy += 1
                if tentative == SQLITE_TENTATIVES - 1:
                    stats.sqlite_echecs += 1
                    log(f"[ao] lot de {len(rows)} lignes PERDU après "
                        f"{SQLITE_TENTATIVES} tentatives : {e!r}")
                    return
                dormir(SQLITE_ATTENTE_SEC * (tentative + 1))
                continue
            stats.ecritures_ms.append((time.perf_counter() - t0) * 1000)
            stats.transactions += 1
            stats.ecrits += n
            return

    def temps_restant() -> bool:
        if fin is None:
            return reconnecter
        return time.monotonic() < fin

    def _une_session(session) -> str:
        """Consomme une session jusqu'à sa fin. Renvoie le motif d'arrêt."""
        nonlocal candidats, candidats_datant_de, prochain_refresh
        nonlocal bookie_annonce
        dernier_flush = dernier_ping = time.monotonic()
        for msg in session.messages():
            maintenant = time.monotonic()
            if fin is not None and maintenant > fin:
                return "durée demandée atteinte"
            if msg:
                stats.messages += 1
                stats.types_messages.update(msg.keys())
            else:
                stats.types_messages["(silence)"] += 1
            # `base_bookie` n'est renseigne qu'a la lecture du message US, donc
            # apres subscribe() : l'annoncer avant affichait toujours None.
            if not bookie_annonce and session.base_bookie:
                log(f"[ao] base bookie = {session.base_bookie}")
                bookie_annonce = True

            if maintenant >= prochain_refresh:
                candidats_datant_de = now_fn()
                candidats = candidats_en_cours(storage, candidats_datant_de,
                                               sport)
                stats.candidats_connus = max(stats.candidats_connus,
                                             len(candidats))
                stats.derniers_candidats = candidats
                prochain_refresh = maintenant + REFRESH_CANDIDATS_SEC

            evf = msg.get("EVF") if msg else None
            if evf:
                stats.evf += 1
                # Filtres AVANT le rapprochement. L'abonnement demande UN
                # sport (`sportstype`) mais le serveur en pousse plusieurs :
                # la capture du 24/08 rendait du tennis et de l'e-sport sur un
                # abonnement football, qui ne peuvent structurellement pas
                # correspondre. Et les noms ne permettent pas de distinguer un
                # derive de son match parent (voir SPMT_VRAI_MATCH), donc le
                # rapprochement l'accepterait.
                if not _meme_sport(evf.get("STP"), sport):
                    stats.hors_sport += 1
                    continue
                if str(evf.get("SPMT", "")) != SPMT_VRAI_MATCH:
                    stats.derives += 1
                    continue
                stats.matchs_vus.add(evf.get("MTCHID"))
                app = evaluer_appariement(
                    evf.get("HN") or "", evf.get("AN") or "", candidats)
                if app.cible is None:
                    stats.sans_event_key += 1
                    stats.motifs_echec[
                        app.motif.split(" —")[0].split(" (")[0]] += 1
                    stats.asianodds_sans_match[evf.get("MTCHID")] = (
                        f"{evf.get('HN')} — {evf.get('AN')}"
                        f"   [{evf.get('LN')}]\n        → {app.motif}")
                else:
                    # Deux matchs AsianOdds DIFFERENTS qui tombent sur le meme
                    # event_key : au moins l'un des deux est un mauvais
                    # rapprochement. Compte, pas devine.
                    for c in app.toutes_les_cibles:
                        deja = stats.origine_par_event.setdefault(
                            c.event_key, evf.get("MTCHID"))
                        if deja != evf.get("MTCHID"):
                            stats.collisions.add(c.event_key)
                    stats.matchs_apparies.add(evf.get("MTCHID"))
                    horodatage = now_fn()
                    if app.doublons:
                        stats.doublons_events.add(app.cible.event_key)
                    for c in app.toutes_les_cibles:
                        stats.evenements_couverts.add(c.event_key)
                        lignes = normalise_evf(
                            evf, c.event_key, inverse=app.inverse_pour(c),
                            matched_at=candidats_datant_de)
                        if not lignes:
                            stats.sans_selection += 1
                        stats.normalises += len(lignes)
                        for l in lignes:
                            # Clé naturelle : un même marché repricé plusieurs
                            # fois dans le lot ne produit qu'une écriture, la
                            # dernière.
                            lot[(l.event_key, l.market.value, l.outcome_label,
                                 l.line)] = l.as_upsert_row(horodatage)

            if maintenant - dernier_flush >= FLUSH_SEC:
                vider()
                dernier_flush = maintenant
            if maintenant - dernier_ping >= PING_SEC:
                session.ping()
                dernier_ping = maintenant
        # Le generateur s'est epuise : c'est le SERVEUR qui a mis fin au flux,
        # pas nous. Sans cette distinction, une fermeture a 34 s d'un run de
        # 5 min sortait exactement comme un run complet.
        return (getattr(session, "fermeture", None)
                or "flux fermé par le serveur (motif inconnu)")

    while True:
        session = fabrique()
        ouverte_a = time.monotonic()
        try:
            session.open()
            session.subscribe(sport=sport)
            log("[ao] abonné")
            stats.fin_raison = _une_session(session)
        except PermissionError:
            # Identifiants refuses : reessayer ne corrigerait rien et peut
            # faire verrouiller le compte. On remonte tel quel.
            raise
        except (ConnectionError, ssl.SSLError, OSError) as e:
            stats.fin_raison = f"connexion perdue : {e!r}"
        finally:
            vider()
            try:
                session.close()
            except Exception:
                pass

        if stats.fin_raison == "durée demandée atteinte" or not temps_restant():
            break
        # Une session qui a tenu repart avec le delai minimal : sinon une
        # coupure reguliere finirait par imposer l'attente maximale a vie.
        if time.monotonic() - ouverte_a >= SESSION_STABLE_SEC:
            delai, echecs = RECONNEXION_DELAI_INITIAL, 0
        echecs += 1
        if echecs > RECONNEXION_ECHECS_MAX:
            stats.fin_raison = (f"abandon après {RECONNEXION_ECHECS_MAX} "
                                f"reconnexions infructueuses — "
                                f"{stats.fin_raison}")
            break
        log(f"[ao] {stats.fin_raison} — reprise dans {delai:.0f} s "
            f"(tentative {echecs}/{RECONNEXION_ECHECS_MAX})")
        dormir(delai)
        if not temps_restant():
            break
        stats.reconnexions += 1
        delai = min(delai * 2, RECONNEXION_DELAI_MAX)
    return stats
