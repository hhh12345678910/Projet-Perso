"""Vivatbet.be — marque blanche de la plateforme 1xBet, API JSON publique.

Relevé sur deux HAR réels du 28/09 (page d'accueil, puis Football → Premier
League → un match → Tennis → Challenger de Bari), puis vérifié depuis la VM :

  - **aucune authentification** : les appels de cotes partent sans cookie ;
  - **l'en-tête `x-hd` n'est pas exigé**. Le navigateur envoie un jeton de 276
    caractères fabriqué par le JavaScript de la page, mais la VM a reçu
    `HTTP 200` et la vraie liste de matchs sans lui — donc pas de pont
    navigateur, contrairement à Betano, Circus et MagicBetting ;
  - **l'IP de la VM est acceptée**.

⚠️ C'est une marque blanche 1xBet (partenaire `gr=704`, ressources servies par
`v3.traincdn.com`). Ses prix viennent très probablement du trading 1xBet —
marge relevée ≈ 3,1 % sur Belgique–France, en 1X2 comme en total 2,5. Aucun
autre book du portefeuille n'est sur cette plateforme : c'est une source
neuve. Une AUTRE marque blanche 1xBet ajoutée plus tard serait son JUMEAU, à
traiter comme les Kambi (`src/reference.py`).

LES TROIS APPELS
----------------
Tous sous `/service-api/main-line-feed/v3/`, avec les paramètres du site
(`fcountry=24`, `gr=704`, `lng=fr`, `ref=282`) :

  - `games1x2?…&selectedMs=2.<sport>` : les matchs d'un sport, cotes EN LIGNE
    (1X2 et total principal) — mais **plafonné à 50** : `count=50` passe,
    `count=100` répond 400 (mesuré le 28/09). Ce sont les « vedettes » ;
  - `leftMenuSports?…&selectedMs=2.<sport>` : la liste des compétitions, avec
    leur nombre de matchs — directes, ou regroupées par pays sous `subLigas` ;
  - `games1x2?…&selectedMs=2.<sport>.<compétition>` : tous les matchs d'une
    compétition (Premier League : 20 ; Challenger de Bari : 10).

L'offre complète coûte donc un appel par compétition (≈ 190 en football) : le
cycle ne lit que les vedettes, et le reste vient d'un balayage de fond — voir
`orchestration.fetch_vivatbet_quotes`.

LES DEUX PIÈGES DU FLUX
-----------------------
  - **de faux matchs** : « Home vs Away » (ou « Home (Special bets) ») est un
    pari sur les statistiques de TOUTE la journée — « 10 matchs », total de
    buts 26,5. Il arrive DANS une vraie compétition (Ligue des nations), porte
    `homeAwayFlag` et un `dopInfo`. S'il passait, son « total 26,5 » serait
    comparé à rien, ou pire à un vrai match nommé pareil ;
  - **les périodes** : les mi-temps arrivent dans `subGamesForMainGame`, avec
    le même `groupId 1` qu'un 1X2 de match. On ne lit QUE les `eventGroups` du
    match principal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..middle import is_half_line
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair

BASE = "https://vivatbet.be/service-api/main-line-feed/v3"
ORIGIN = "https://vivatbet.be"

# Les paramètres que le site envoie sur chaque appel du flux, relevés dans le
# HAR. `fcountry=24` est la Belgique, `gr=704` le partenaire Vivatbet.
PARAMS_SITE = {"fcountry": "24", "gr": "704", "lng": "fr", "ref": "282"}

# Le plafond du serveur : 50 accepté, 100 → `400 InvalidQueryParameters`.
MAX_COUNT = 50

# `selectedMs=2.<id>` : la section « TOP » du menu (2), puis le sport. Les
# identifiants de sport sont ceux de 1xBet, relevés dans `leftMenuSports`.
SECTION_MENU = 2
# Football et tennis seulement. Le hockey n'y est PAS : le `groupId 1` de 1xBet
# y est un 1X2 en temps RÉGLEMENTAIRE (trois issues), là où la référence
# Pinnacle price un vainqueur prolongations comprises — les comparer
# fabriquerait de la valeur qui n'existe pas.
SPORT_IDS = {
    "soccer": 1,
    "tennis": 4,
}

# `groupId` → marché, et `type` → issue. Nomenclature 1xBet, relevée sur les
# deux HAR : `groupId 1` porte 1 / X / 2 au football et 1 / 2 (types 1 et 3,
# sans nul) au tennis ; `groupId 17` porte le total, `parameter` = la ligne
# (en BUTS au football, en JEUX au tennis : 17,5 à 22,5 relevé).
GROUPE_VAINQUEUR = 1
GROUPE_TOTAL = 17
TYPES_VAINQUEUR = {1: "home", 2: "draw", 3: "away"}
TYPES_TOTAL = {9: "over", 10: "under"}

# Les compétitions qui ne sont pas des matchs : paris spéciaux, statistiques
# de journée, « équipe contre joueur ». Comparaison sur le nom ANGLAIS, stable
# d'une langue du site à l'autre.
_MOTS_LIGUE_SPECIALE = ("special", "statistics", "team vs player")
# Les « équipes » d'un pari de journée.
_NOMS_SPECIAUX = {"home", "away"}


def _nom(camp: dict) -> str:
    """Le nom ANGLAIS d'un camp, sinon le français.

    L'anglais d'abord : c'est la langue de la référence Pinnacle, donc celle
    qui se rapproche le mieux — « Belgium » et non « Belgique »."""
    return ((camp or {}).get("fullNameEng") or (camp or {}).get("fullName") or "").strip()


def _nom_affiche(camp: dict) -> str:
    """Le nom tel que le SITE l'affiche (français) : c'est celui qu'on
    cherche sur vivatbet.be en lisant l'alerte — « Îles Féroé », pas
    « Faroe Islands ». Le rapprochement, lui, reste sur l'anglais."""
    return ((camp or {}).get("fullName") or (camp or {}).get("fullNameEng") or "").strip()


def _ligue(jeu: dict) -> str:
    liga = jeu.get("liga") or {}
    return (liga.get("nameEng") or liga.get("name") or "").strip()


def ligue_speciale(nom: str) -> bool:
    bas = (nom or "").lower()
    return any(m in bas for m in _MOTS_LIGUE_SPECIALE)


def est_pari_special(jeu: dict) -> bool:
    """Ce « match » est-il un pari sur plusieurs matchs, ou une période ?"""
    # `homeAwayFlag` n'est porté QUE par ces paris de journée (8 sur 150 dans
    # les deux HAR). `dopInfo` les accompagne, mais n'est pas retenu comme
    # critère : rien ne garantit qu'un vrai match ne le porte jamais.
    if jeu.get("homeAwayFlag"):
        return True
    # Un sous-jeu (mi-temps, corners…) porte un nom de type ou de période ; le
    # match principal les a vides — vérifié sur les 150 matchs des deux HAR.
    if jeu.get("gameTypeName") or jeu.get("periodName"):
        return True
    for camp in ("opponent1", "opponent2"):
        nom = _nom(jeu.get(camp) or {}).lower()
        if "(special bets)" in nom or nom.split(" (")[0] in _NOMS_SPECIAUX:
            return True
    return ligue_speciale(_ligue(jeu))


def _debut(jeu: dict) -> datetime | None:
    ts = jeu.get("startTs")
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
    except (TypeError, ValueError, OSError):
        return None


def _cote(ev: dict) -> float | None:
    """La cote décimale d'une issue, ou None si elle n'est pas jouable.

    `blocked` est le verrou 1xBet : une issue suspendue reste dans le flux
    avec sa dernière cote. La publier fabriquerait une détection qu'on ne peut
    pas jouer."""
    if ev.get("blocked"):
        return None
    try:
        v = float(ev.get("cf"))
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


def compte_rejets(payload: list) -> dict[str, int]:
    """Pourquoi des matchs d'un lot n'ont produit aucune cote.

    Sans ces compteurs, « 50 annoncés, 46 retenus » laisse 4 matchs disparus
    sans dire s'ils sont légitimement écartés ou si le parseur en perd."""
    c = {"annonces": 0, "retenus": 0, "speciaux": 0, "equipes_manquantes": 0,
         "date_illisible": 0, "bruit": 0}
    for jeu in payload or []:
        c["annonces"] += 1
        if est_pari_special(jeu):
            c["speciaux"] += 1
            continue
        dom, ext = _nom(jeu.get("opponent1")), _nom(jeu.get("opponent2"))
        if not dom or not ext:
            c["equipes_manquantes"] += 1
            continue
        if _debut(jeu) is None:
            c["date_illisible"] += 1
            continue
        if is_noise_event(dom, ext, _ligue(jeu)):
            c["bruit"] += 1
            continue
        c["retenus"] += 1
    return c


def parse_games(payload: list, book: Book = Book.VIVATBET) -> Iterator[OddQuote]:
    """Une réponse `games1x2` → des `OddQuote` (vainqueur et totaux).

    Forme : une LISTE de matchs ; chacun porte `eventGroups`, une liste de
    marchés `{groupId, events}` où `events` est une liste de COLONNES, chaque
    colonne une liste d'issues `{type, cf, parameter?, blocked?}`. Le flux ne
    sert que la ligne de total centrale ; `gameEvents` (un appel par match)
    les servirait toutes, et n'est pas utilisé.
    """
    now = datetime.now(timezone.utc)
    for jeu in payload or []:
        if est_pari_special(jeu):
            continue
        dom, ext = _nom(jeu.get("opponent1")), _nom(jeu.get("opponent2"))
        debut = _debut(jeu)
        if not dom or not ext or debut is None:
            continue
        ligue = _ligue(jeu)
        if is_noise_event(dom, ext, ligue):
            continue
        record_pair(dom, ext, book, debut,
                    noms_affiches=(_nom_affiche(jeu.get("opponent1")),
                                   _nom_affiche(jeu.get("opponent2"))))
        ek = event_key(dom, ext, debut)
        source_id = str(jeu.get("id") or "")

        # ⚠️ `eventGroups` du match principal SEULEMENT, jamais
        # `subGamesForMainGame` : une mi-temps y porte le même `groupId 1`.
        for groupe in jeu.get("eventGroups") or []:
            gid = groupe.get("groupId")
            if gid not in (GROUPE_VAINQUEUR, GROUPE_TOTAL):
                continue
            for colonne in groupe.get("events") or []:
                for ev in colonne or []:
                    cote = _cote(ev)
                    if cote is None:
                        continue
                    if gid == GROUPE_VAINQUEUR:
                        label = TYPES_VAINQUEUR.get(ev.get("type"))
                        if label is None:
                            continue
                        marche, ligne = MarketType.H2H, None
                    else:
                        label = TYPES_TOTAL.get(ev.get("type"))
                        ligne = ev.get("parameter")
                        if label is None or ligne is None:
                            continue
                        ligne = float(ligne)
                        # Lignes en « ,5 » seulement : une ligne entière peut
                        # rembourser et un quart est un pari fractionné — la
                        # devig à deux issues ne sait pricer ni l'un ni l'autre
                        # (`middle.is_half_line`, même définition partout).
                        if not is_half_line(ligne):
                            continue
                        marche = MarketType.TOTALS
                    yield OddQuote(
                        event_key=ek,
                        book=book,
                        market=marche,
                        outcome=Outcome(label=label, line=ligne),
                        decimal_odd=cote,
                        fetched_at=now,
                        source_event_id=source_id,
                        league=ligue or None,
                    )


def leagues_from_menu(menu: list, sport: str) -> list[tuple[int, str, int]]:
    """Les compétitions d'un sport dans `leftMenuSports` : (id, nom, matchs).

    Deux formes coexistent : une compétition DIRECTE (`nameEng` présent), ou
    un PAYS qui regroupe ses compétitions sous `subLigas` (« Angleterre » : 19
    compétitions, 150 matchs). Oublier la seconde perdait 138 des 188
    compétitions de football, et TOUTES celles du tennis (ATP, WTA… sont des
    regroupements). Les compétitions vides et spéciales sont écartées.
    """
    sid = SPORT_IDS.get(sport)
    out: dict[int, tuple[int, str, int]] = {}
    for section in menu or []:
        for sp in section.get("sports") or []:
            if sp.get("id") != sid:
                continue
            for entree in sp.get("ligas") or []:
                for lg in (entree.get("subLigas") or [entree]):
                    lid, nom = lg.get("id"), (lg.get("nameEng") or lg.get("name") or "")
                    n = int(lg.get("gamesCount") or 0)
                    if not lid or n <= 0 or ligue_speciale(nom):
                        continue
                    out[int(lid)] = (int(lid), nom, n)
    return list(out.values())


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _headers() -> dict[str, str]:
    """Les en-têtes du site, relevés dans le HAR — SAUF `x-hd`, le jeton que
    le JavaScript fabrique : la VM a reçu 200 sans lui, et l'imiter voudrait
    dire rejouer un jeton de session d'un navigateur."""
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "x-svc-source": "__BETTING_APP__",
        "x-app-n": "__BETTING_APP__",
        "Referer": f"{ORIGIN}/fr/line",
    }


class VivatbetScraper:
    book = Book.VIVATBET

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VivatbetScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        # Sans `reraise`, l'échec final devient une `RetryError`, qui n'est pas
        # une `httpx.HTTPError` : les filets en aval la laisseraient passer.
        reraise=True,
    )
    def _get(self, route: str, params: dict) -> list:
        # ⚠️ L'ORDRE DES PARAMÈTRES COMPTE : le flux exige l'ordre ALPHABÉTIQUE,
        # celui de toutes les URL du HAR. Les mêmes valeurs dans un autre ordre
        # répondent `400 InvalidQueryParametersException` — vérifié depuis la
        # VM le 28/09 : `curl` trié → 200, scraper non trié → 400.
        tries = dict(sorted({**PARAMS_SITE, **params}.items()))
        r = self._client.get(f"{BASE}/{route}", params=tries)
        r.raise_for_status()
        return r.json()

    def _selection(self, sport: str, *suite: int) -> str:
        sid = SPORT_IDS.get(sport)
        if sid is None:
            raise ValueError(f"sport non couvert par Vivatbet : {sport}")
        return ".".join(str(x) for x in (SECTION_MENU, sid, *suite))

    def fetch_top(self, sport: str = "soccer") -> list:
        """Les 50 « vedettes » d'un sport — un seul appel, lu à chaque cycle."""
        return self._get("games1x2", {
            "cfView": "3", "count": str(MAX_COUNT), "grMode": "4",
            "selectedMs": self._selection(sport)})

    def fetch_leagues(self, sport: str = "soccer") -> list[tuple[int, str, int]]:
        menu = self._get("leftMenuSports", {"selectedMs": self._selection(sport)})
        return leagues_from_menu(menu, sport)

    def fetch_league(self, sport: str, league_id: int) -> list:
        """Tous les matchs d'une compétition, au plafond de 50."""
        return self._get("games1x2", {
            "cfView": "3", "count": str(MAX_COUNT), "grMode": "4",
            "selectedMs": self._selection(sport, league_id)})
