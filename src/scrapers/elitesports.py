"""EliteSports.be — marque blanche FM Gaming, API REST publique.

Le book le plus simple du portefeuille, et c'est mesuré sur un HAR réel du
22/08 puis vérifié depuis la VM :

  - **aucune authentification** — 0 cookie sur 132 requêtes, aucun
    `Authorization`, même sur les routes `/users/me/` ;
  - **aucun anti-bot** — ni `cf-ray`, ni DataDome, ni `set-cookie`, ni
    en-tête de limitation ; 111 réponses sur 111 en HTTP 200 ;
  - **l'IP de datacenter est acceptée** — la VM reçoit 200. Contrairement à
    Betano (DataDome), MagicBetting (Cloudflare) et API-Sports, ce book ne
    demande AUCUN pont navigateur.

⚠️ C'est une marque blanche : le front est servi par `elit.fmgaming.dev`,
l'API par `api.lisaparyaj.com`, et l'en-tête `tenant-code` sélectionne
l'enseigne. Le même back-end sert donc probablement d'autres marques — et une
plateforme tierce change d'hôte ou de version sans prévenir. C'est le profil
type du book qui casse en silence : l'alerte « softbook muet » (§20.6) est la
garde qui compte ici.

LE POINT QUI REND CE BOOK BON MARCHÉ
------------------------------------
Les cotes sont DANS la liste des matchs — `markets → periods → lines → odds`.
Pas d'appel par événement, donc pas le piège du §18.6 où `gettopeventslist` ne
rendait que 27 vedettes et où l'offre complète coûtait un appel par
compétition. Mesuré : `size=500` est servi tel quel, donc **4 appels pour les
1 523 matchs de football**.
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

BASE = "https://api.lisaparyaj.com/api/v1/public"
TENANT = "elitebet"
ORIGIN = "https://elit.fmgaming.dev"

# UUID de sport, relevés dans `/public/sports` — jamais devinés. Le §10 est
# formel : un identifiant se confirme par égalité exacte, et une supposition
# qui tombe à côté ne lève aucune erreur, elle rend simplement un book muet.
SPORT_IDS = {
    "soccer": "d4fa6462-c2d1-4389-ac34-f32e6fc75425",   # « Football », 1 523 prématch
    "tennis": "c3986677-aa20-4232-bcf0-3b1cd434daac",   # 35 prématch seulement,
                                                        # et AUCUN total : le
                                                        # marché 7 y est vide
}

# `marketExternalId` → type de marché. Deux seulement nous intéressent, et ce
# sont exactement les deux que la liste sert en ligne.
# ⚠️ Les identifiants DIFFÈRENT d'un sport à l'autre, et la première version de
# ce fichier ne connaissait que ceux du football — le tennis rendait donc ZÉRO
# cote, en silence, exactement la panne que le §10 décrit. Relevés le 22/08 sur
# les deux payloads de la capture, football ET tennis.
MARKET_IDS = {
    1: MarketType.H2H,      # football — « Résultat du match » (1X2)
    5: MarketType.H2H,      # tennis   — « Vainqueur » (2 issues, pas de nul)
    7: MarketType.TOTALS,   # « Total de buts » au football
}

# `betTypeName` → notre libellé d'issue. Relevé exhaustivement sur la capture :
# le marché 1 ne produit que ces trois valeurs, le marché 7 que ces deux-là.
# Traduction par ÉGALITÉ EXACTE, jamais par ressemblance — les libellés
# arrivent en anglais alors que `marketName` est en français, donc ils ne
# suivent pas `x-locale` et pourraient changer sans prévenir.
# ⚠️ « Home wins » au football mais « Home **team** wins » au tennis. Un seul
# mot d'écart, et c'est la moitié d'un marché qui disparaît sans erreur. Les
# deux formes sont donc listées, relevées et non déduites.
BET_TYPES = {
    "Home wins": "home",          # football
    "Home team wins": "home",     # tennis
    "Draw": "draw",
    "Away team wins": "away",     # les deux sports emploient cette forme-ci
    "Away wins": "away",          # symétrie défensive, jamais observée
    "Total over": "over",
    "Total under": "under",
}

# Seul le temps réglementaire. `periodIdentifier` vaut 0 pour « Regular time ».
# Sans ce filtre, une mi-temps entrerait dans la même clé (event, marché,
# ligne) qu'un marché de match entier et serait comparée à la mauvaise ligne
# juste — c'est la famille de bug du §21.14, silencieuse par construction.
# Au tennis la période s'appelle « Match » et porte le même identifiant 0 : le
# filtre vaut donc pour les deux sports. Le seul `periodIdentifier` nul observé
# est celui du marché « Total » du tennis, qui est un PLACEHOLDER — `marketId`
# à zéro, `locked: true`, `lines: []`. Il n'y a rien à en tirer, et la garde
# `locked` l'écarte déjà. On garde donc l'égalité stricte : accepter `None`
# ouvrirait la porte à des périodes qu'on n'a pas relevées.
FULL_TIME_PERIOD = 0


def _is_playable_total(line: float | None) -> bool:
    """Cette ligne de total est-elle un over/under que le joueur peut cliquer ?

    ⚠️ Le marché 7 d'EliteSports n'est PAS un over/under européen : il sert
    l'échelle ASIATIQUE complète, de 0,25 à 5,5 par pas de 0,25. La preuve est
    dans le pas lui-même — un over/under classique n'a que des lignes en « ,5 ».
    Deux familles y sont donc à écarter, et pour deux raisons différentes :

    - **les quarts** (2,25 ; 0,75) sont des paris FRACTIONNÉS — miser « over
      2,25 » c'est miser moitié sur 2,0 et moitié sur 2,5 ;
    - **les entières** (2 ; 3) sont des lignes à REMBOURSEMENT — sur un total
      de 2 exact, « over 2 » rend la mise.

    Les entières sont celles qui ont coûté cher : le filtre d'origine ne
    coupait que les quarts, donc elles partaient en alerte. Relevé le 22/08 en
    base : **6 des 8** détections totals EliteSports étaient sur une ligne
    entière — et introuvables sur le site, donc injouables. Le même relevé a
    montré que MagicBetting, lui, sert et HONORE ses lignes entières : le
    filtre reste donc local à ce book, il ne monte pas dans `find_value_bets`.

    Conséquence secondaire, réelle : sur une ligne entière l'EV affichée est
    surévaluée. La devig ne price que deux issues alors qu'il y en a trois, et
    l'EV vraie vaut `(1 - p_remboursement) x EV_affichée`.

    Le prédicat vient de `middle.py` : une seule définition pour le projet,
    donc pas de dérive possible entre les deux endroits qui posent la question.
    """
    return is_half_line(line)


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _team_names(event: dict) -> tuple[str, str] | None:
    """Les deux équipes, prises sur `teams[]` et non sur `eventName`.

    `eventName` vaut « Fluminense RJ - Remo PA » : le séparateur est un tiret,
    et des noms d'équipes en contiennent. `teams[]` porte les noms séparés
    (« Fluminense/RJ »), donc aucun découpage à faire.
    """
    teams = event.get("teams") or []
    if len(teams) >= 2:
        h = (teams[0].get("teamName") or "").strip()
        a = (teams[1].get("teamName") or "").strip()
        if h and a:
            return h, a
    noms = [str(n).strip() for n in (event.get("teamNames") or []) if str(n).strip()]
    return (noms[0], noms[1]) if len(noms) >= 2 else None


def compte_rejets(payload: dict) -> dict[str, int]:
    """Pourquoi des événements de la page n'ont produit aucune cote.

    ⚠️ Sans ces compteurs, « 1 503 annoncés, 1 476 analysés » laisse 27
    événements disparus et aucun moyen de dire s'ils sont légitimement écartés
    ou si le parseur en perd. C'est le mode de défaillance dominant du projet
    (§13.12) : deux causes, un seul symptôme — rien.
    """
    c = {"annonces": 0, "retenus": 0, "pas_prematch": 0,
         "equipes_manquantes": 0, "date_illisible": 0, "bruit": 0}
    for league in payload.get("content") or []:
        nom = (league.get("leagueName") or "").strip()
        for event in league.get("events") or []:
            c["annonces"] += 1
            if event.get("status") != "PREMATCH":
                c["pas_prematch"] += 1
                continue
            noms = _team_names(event)
            if not noms:
                c["equipes_manquantes"] += 1
                continue
            if _parse_dt(event.get("dateTime")) is None:
                c["date_illisible"] += 1
                continue
            if is_noise_event(noms[0], noms[1], nom):
                c["bruit"] += 1
                continue
            c["retenus"] += 1
    return c


def parse_prematch(payload: dict, book: Book = Book.ELITESPORTS) -> Iterator[OddQuote]:
    """Une page de `/sports/{id}/events/prematch` → des `OddQuote`.

    Forme : `content` est une liste de LIGUES, chacune portant ses `events`, et
    chaque événement portant ses `markets` en ligne. Trois niveaux de `locked`
    existent (marché, période, cote) et chacun est respecté : une cote
    verrouillée n'est pas jouable, la publier fabriquerait des détections
    injouables.
    """
    now = datetime.now(timezone.utc)
    for league in payload.get("content") or []:
        league_name = (league.get("leagueName") or "").strip()
        for event in league.get("events") or []:
            if event.get("status") != "PREMATCH":
                continue
            noms = _team_names(event)
            start = _parse_dt(event.get("dateTime"))
            if not noms or start is None:
                continue
            home, away = noms
            if is_noise_event(home, away, league_name):
                continue
            record_pair(home, away)
            ek = event_key(home, away, start)
            source_id = str(event.get("eventId") or "")

            for market in event.get("markets") or []:
                market_type = MARKET_IDS.get(market.get("marketExternalId"))
                if market_type is None or market.get("locked"):
                    continue
                for period in market.get("periods") or []:
                    if period.get("periodIdentifier") != FULL_TIME_PERIOD:
                        continue
                    if period.get("locked"):
                        continue
                    for line in period.get("lines") or []:
                        ligne = line.get("coefficientValue")
                        ligne = float(ligne) if ligne is not None else None
                        if market_type is MarketType.TOTALS:
                            if not _is_playable_total(ligne):
                                continue
                        for odd in line.get("odds") or []:
                            if odd.get("locked"):
                                continue
                            label = BET_TYPES.get(odd.get("betTypeName"))
                            cote = odd.get("oddsValue")
                            if label is None or not cote:
                                continue
                            yield OddQuote(
                                event_key=ek,
                                book=book,
                                market=market_type,
                                outcome=Outcome(
                                    label=label,
                                    line=ligne if market_type is MarketType.TOTALS else None,
                                ),
                                decimal_odd=float(cote),
                                fetched_at=now,
                                source_event_id=source_id,
                                league=league_name or None,
                            )


def leagues_seen(payload: dict) -> dict[str, str]:
    """Les `leagueId` d'une page, avec leur nom. Sert au balayage profond."""
    return {lg["leagueId"]: (lg.get("leagueName") or "")
            for lg in payload.get("content") or [] if lg.get("leagueId")}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _headers() -> dict[str, str]:
    """Les en-têtes que l'API exige réellement, relevés sur la capture.

    `tenant-code` sélectionne l'enseigne : sans lui, le back-end ne sait pas
    quelle marque servir. `x-fingerprint` est un identifiant d'appareil — sa
    PRÉSENCE compte, sa valeur n'est pas vérifiée côté serveur (200 obtenu
    depuis la VM avec la valeur du navigateur). On en pose donc une stable
    plutôt que d'emprunter celle d'un vrai poste.
    """
    return {
        "Accept": "application/json",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "tenant-code": TENANT,
        "x-locale": "fr",
        "x-fingerprint": "0" * 32,
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
    }


class EliteSportsScraper:
    book = Book.ELITESPORTS

    # Mesuré : `size=500` est servi tel quel, donc 4 appels pour 1 523 matchs.
    # La borne dure sur les pages évite qu'une pagination qui ne se termine
    # jamais fasse tourner le cycle indéfiniment — le §score_sources a la même
    # garde pour la même raison.
    PAGE_SIZE = 500
    MAX_PAGES = 12

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EliteSportsScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(f"{BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def fetch_pages(self, sport: str = "soccer") -> Iterator[dict]:
        """Les pages brutes de l'offre prématch d'un sport.

        Rendues une par une plutôt qu'assemblées : une page qui échoue en
        cours de balayage ne doit pas emporter celles déjà obtenues.

        ⚠️ **L'arrêt se fait sur une page VIDE, jamais sur `totalPages`.**
        Mesuré le 22/08 : à `size=10` le tennis annonce `totalElements: 35,
        totalPages: 4`, cohérent ; à `size=500` il annonce **6** pour 35
        événements réellement servis. Le compteur de pages n'est donc pas
        fiable aux grandes tailles, et s'y fier tronque le balayage — c'est
        très probablement ce qui faisait rendre 1 476 événements de football
        sur 1 503 annoncés, pas un filtre.

        Une source qui se trompe sur sa propre taille n'est pas une raison de
        perdre des cotes en silence. Le coût est UN appel de plus par sport et
        par cycle — la page vide qui prouve la fin — et ce book n'a ni quota
        ni authentification.
        """
        sport_id = SPORT_IDS.get(sport)
        if sport_id is None:
            return
        for page in range(self.MAX_PAGES):
            payload = self._get(
                f"/sports/{sport_id}/events/prematch",
                {"page": page, "size": self.PAGE_SIZE, "sort": "dateTime,ASC,id,ASC"},
            )
            n = sum(len(l.get("events") or [])
                    for l in payload.get("content") or [])
            if not n:
                return                      # page vide : la fin, pour de bon
            yield payload
        # La borne dure est atteinte : on a peut-être tronqué. Le dire, plutôt
        # que de rendre un book silencieusement incomplet.
        import warnings
        warnings.warn(
            f"EliteSports {sport}: {self.MAX_PAGES} pages lues sans page vide — "
            "l'offre est peut-être tronquée, relever MAX_PAGES.",
            stacklevel=2,
        )

    def fetch_league_pages(self, sport: str, league_id: str) -> Iterator[dict]:
        """L'offre prématch d'UNE compétition.

        ⚠️ **Cette route ne pose pas la même fenêtre que la route globale**, et
        c'est tout son intérêt. Mesuré le 22/08 : la route globale s'arrête à
        J+2 (1 480 événements du 22 au 24/08), alors que la Coupe d'Allemagne
        interrogée ligue par ligue rend des matchs jusqu'au **2 septembre**.

        Balayer les 302 ligues du football coûte 302 appels et 55 s, et rend
        **255 événements exploitables de plus** (J+0 à J+8, au-delà Pinnacle ne
        price plus rien donc aucune ligne juste n'existe). Les deux tiers
        tombent au-delà de 48 h, la tranche que le §9 mesure à +6,38 % de CLV,
        significative — pas dans le creux 24-48 h.
        """
        sport_id = SPORT_IDS.get(sport)
        if sport_id is None:
            return
        for page in range(self.MAX_PAGES):
            payload = self._get(
                f"/sports/{sport_id}/leagues/{league_id}/events/prematch",
                {"page": page, "size": self.PAGE_SIZE},
            )
            if not sum(len(l.get("events") or []) for l in payload.get("content") or []):
                return
            yield payload

    def fetch_quotes(self, sport: str = "soccer") -> list[OddQuote]:
        out: list[OddQuote] = []
        for payload in self.fetch_pages(sport):
            out.extend(parse_prematch(payload, self.book))
        return out
