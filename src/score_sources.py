"""Les deux sources de scores, et rien d'autre.

Tout ce qui est propre à un fournisseur vit ici — clé, pagination, noms de
champs, filtrage des statuts — pour que `src/scores.py` n'ait jamais à savoir
qui lui parle. Deux fournisseurs sont nécessaires parce qu'aucun ne fait les
deux sports : API-Sports couvre 1 200+ ligues de football mais **pas le
tennis**, mesuré le 16/08.

Chaque source est coupée en deux : une fonction de parsing pure, qui prend le
JSON et rend des `MatchResult`, et une classe qui fait l'appel HTTP. Le
parsing est ce qui peut se tromper, et c'est donc lui qui est testé contre des
échantillons réels — la classe HTTP ne décide de rien.

⚠️ Chaque parseur rend AUSSI des compteurs de rejet. Sans eux, « la source n'a
pas ce match » et « mon filtre est trop strict » donnent tous deux zéro
résultat, et c'est le mode de défaillance dominant du projet (§13.12).
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .matcher import normalize_team, team_class
from .scores import MatchResult, winner_from_scores

# --------------------------------------------------------------- commun ----

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


_RETRY = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)


def _require_key(value: str, env_name: str) -> str:
    """Refuser tôt une clé absente, et dire laquelle.

    Sans ce contrôle, une clé vide part dans l'en-tête et httpx lève
    « Illegal header value b'Bearer ' » — un message qui ne nomme ni le
    réglage, ni le fichier, ni le sport. Vu en production le 16/08 : la
    commande ne chargeait pas `.env`, et ce message était le seul indice.
    """
    if not value.strip():
        raise RuntimeError(
            f"{env_name} est vide ou absent. Pose-le dans .env "
            f"(et vérifie que la commande charge bien .env)."
        )
    return value.strip()


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------- football ----

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"

# ⚠️ Seul FT est accepté, et c'est une décision mesurée, pas de la prudence.
#
# Sur les 1 215 matchs du 15/08 : 1 177 FT, 14 PST, 10 NS, 8 PEN, 3 CANC,
# 2 AET, 1 ABD. Sur les 1 177 FT, `goals` et `score.fulltime` sont
# rigoureusement identiques, aucun n'est nul, et `teams.home.winner` ne
# contredit jamais le score — la donnée y est parfaitement cohérente.
#
# AET et PEN sont écartés parce que leur score à 90 minutes est AMBIGU dans
# cette API : un match du Schweizer Cup rend `fulltime` 3-4 avec `extratime`
# 0-1, donc `fulltime` porte déjà la prolongation et le vrai score
# réglementaire (3-3) n'apparaît nulle part. Or 1X2 et totaux se règlent sur
# 90 minutes. Les noter sur ce chiffre inverserait des paris sans lever
# d'erreur. Ils pèsent 10 matchs sur 1 215, soit 0,8 % — le prix du silence
# est dérisoire, celui de l'erreur ne l'est pas.
_FOOTBALL_PLAYED_TO_COMPLETION = {"FT"}


# Les mots qui, dans un nom de LIGUE, disent la classe des équipes qui y
# jouent. Volontairement la même famille que `_CLASS_RULES` du matcher, plus
# les formes propres aux noms de compétition (« femenina », « feminile »).
_LEAGUE_CLASS = (
    ("Women", re.compile(
        r"\b(women|womens|women'?s|ladies|feminin\w*|femenin\w*|feminil\w*|"
        r"femminil\w*|frauen|dames|damen)\b", re.IGNORECASE)),
    ("U19", re.compile(r"\b(u1[5-9]|u2[0-3]|youth|junior\w*|jugend)\b", re.IGNORECASE)),
)


def class_marker_from_league(league: str) -> str:
    """Le marqueur de classe porté par un nom de LIGUE, ou "".

    ⚠️ Pourquoi ça existe, et pourquoi ça vaut 6 % du flux football.

    Le matcher refuse — à raison — d'apparier une équipe féminine avec la
    section masculine du même club : `team_similarity` renvoie 0.0 dès que la
    classe diffère, sinon un « Portland Thorns » masculin noterait les paris
    pris sur le féminin. Mais la classe se lit dans le NOM D'ÉQUIPE, et les
    deux sources ne la mettent pas au même endroit :

    | | ligue | équipe |
    |---|---|---|
    | Pinnacle      | `Colombia - Liga Women`  | `Deportivo Cali (W)` |
    | API-Football  | `Colombia - Liga Femenina` | `Deportivo Cali`   |

    Côté API-Football l'équipe est un nom de club NU : elle est donc classée
    « main », la nôtre « xwomen », et la barrière les sépare. **Aucun match
    féminin n'était appariable**, en silence — mesuré le 21/08 sur le 20/08 :
    AFC Champions League Women 0/2, Colombia Liga Women 0/3, NWSL 0/1, alors
    que la source servait bien 12, 4 et 1 matchs de ces compétitions.

    On remet donc le marqueur là où le matcher le cherche. Le faire ICI plutôt
    que dans le matcher est délibéré : c'est une CONVENTION DE SOURCE, et le
    §score_sources dit que tout ce qui est propre à un fournisseur reste
    derrière cette frontière. Le matcher, lui, ne doit rien savoir d'API-Sports.
    """
    # Accents pliés d'abord : « Division 1 Féminine » et « Copa Femenina » se
    # côtoient dans le même catalogue, et `\bfeminin` ne voit pas le « é ».
    # C'est la même normalisation que `normalize_team`, pour la même raison.
    plat = unicodedata.normalize("NFKD", league or "").encode(
        "ascii", "ignore").decode("ascii")
    for marker, pat in _LEAGUE_CLASS:
        if pat.search(plat):
            return marker
    return ""


def _with_class(team: str, marker: str) -> str:
    """Ajouter le marqueur, sauf s'il y est déjà.

    Le doubler serait sans effet aujourd'hui — `_extract_class` retire TOUS les
    marqueurs trouvés — mais un nom propre reste plus lisible dans les
    journaux et les exports.
    """
    if not marker or not team:
        return team
    if team_class(normalize_team(team)) != "main":
        return team
    return f"{team} {marker}"


def parse_apifootball_results(payload: dict) -> tuple[list[MatchResult], dict[str, int]]:
    """`/fixtures?date=` d'API-Football -> résultats notables."""
    counters = {"retenus": 0, "non_termine": 0, "score_manquant": 0,
                "classe_reportee": 0}
    out: list[MatchResult] = []

    for f in payload.get("response") or []:
        fixture = f.get("fixture") or {}
        status = ((fixture.get("status") or {}).get("short") or "").upper()
        if status not in _FOOTBALL_PLAYED_TO_COMPLETION:
            counters["non_termine"] += 1
            continue

        start = _parse_dt(fixture.get("date"))
        teams = f.get("teams") or {}
        home = ((teams.get("home") or {}).get("name") or "").strip()
        away = ((teams.get("away") or {}).get("name") or "").strip()
        # La classe vit dans le nom de LIGUE chez cette source, dans le nom
        # d'ÉQUIPE chez nous. Sans ce report, la barrière de classe du matcher
        # rend tout le football féminin inappariable — voir
        # `class_marker_from_league`.
        marker = class_marker_from_league(((f.get("league") or {}).get("name") or ""))
        if marker:
            avant = (home, away)
            home, away = _with_class(home, marker), _with_class(away, marker)
            if (home, away) != avant:
                counters["classe_reportee"] += 1
        ft = (f.get("score") or {}).get("fulltime") or {}
        hs, as_ = ft.get("home"), ft.get("away")

        if not (start and home and away) or hs is None or as_ is None:
            counters["score_manquant"] += 1
            continue

        out.append(MatchResult(
            sport="soccer",
            home=home,
            away=away,
            start_time=start,
            # Jamais `teams.home.winner` : sur un AET/PEN il désigne le
            # vainqueur de la qualification, pas celui du 1X2. Le déduire du
            # score de 90 minutes est la seule règle qui reste juste partout.
            winner=winner_from_scores("soccer", hs, as_),
            home_score=float(hs),
            away_score=float(as_),
            source="api-football",
            source_id=str(fixture.get("id") or ""),
        ))
        counters["retenus"] += 1

    return out, counters


API_FOOTBALL_RAPIDAPI_BASE = "https://api-football-v1.p.rapidapi.com/v3"
API_FOOTBALL_RAPIDAPI_HOST = "api-football-v1.p.rapidapi.com"


def _football_error_message(errors: Any, route: str) -> str:
    """Traduire l'erreur d'API-Sports en quelque chose qui n'égare pas.

    « Your account is suspended » envoie chercher un problème de compte alors
    que le compte est actif : c'est l'IP appelante qui est refusée. Sans cette
    traduction, le message coûte un aller-retour sur le tableau de bord du
    fournisseur — il l'a déjà coûté une fois le 16/08.
    """
    text = str(errors)
    if "suspend" in text.lower() and route == "direct":
        return (
            "api-football: réponse « account suspended » sur la route DIRECTE. "
            "DEUX causes donnent ce message et seul le tableau de bord du "
            "fournisseur les sépare : (1) le compte est réellement suspendu, "
            "(2) l'IP de datacenter est refusée et présentée ainsi. "
            "Va voir le tableau de bord AVANT de conclure. Si le compte est "
            "sain, pose SCORES_FOOTBALL_BRIDGE=1 pour appeler depuis le "
            "navigateur (IP résidentielle). ⚠️ Ne cherche pas RapidAPI : le "
            "listing d'API-Sports n'existe plus (« API not found », vérifié le "
            "21/08 — §21.16)."
        )
    return f"api-football ({route}): {errors}"


class ApiFootballScores:
    """Résultats de football. 1 200+ ligues, une journée entière par requête.

    Mesuré le 15/08 : 1 215 matchs en une seule réponse de 1,1 Mo, sans
    pagination. Le palier gratuit (100 requêtes/jour) est donc très largement
    suffisant — le projet n'a besoin que des résultats finaux, une fois par
    jour, pas du direct.

    ⚠️ **API-Sports refuse les IP de datacenter et le déguise en suspension de
    compte.** Mesuré le 16/08 avec une seule et même clé : 200 depuis une IP
    résidentielle, `{"access": "Your account is suspended"}` depuis la VM ET
    depuis un conteneur cloud. Le message était alors trompeur — le compte
    était actif, c'est l'origine qui était refusée. C'est le quatrième anti-bot
    du projet après DataDome, Gaming1 et Cloudflare, et le premier à toucher la
    mesure plutôt que la collecte.

    🔴 **MAIS le 21/08, le compte est RÉELLEMENT suspendu** — constaté sur le
    tableau de bord du fournisseur, pas déduit d'un message d'API. Les deux
    causes existent donc, elles produisent le même message, et seul le tableau
    de bord les sépare. **Vérifier là AVANT de conclure quoi que ce soit d'une
    réponse d'API.** Le pont navigateur ne contourne que le refus d'IP : contre
    une suspension de compte, il ne sert à rien.

    D'où les deux routes. Si `SCORES_FOOTBALL_RAPIDAPI_KEY` est posée, l'appel
    passe par RapidAPI, qui relaie depuis SA propre infrastructure : API-Sports
    ne voit jamais l'IP de la VM. La réponse est identique au champ près, donc
    le parseur ne change pas. Sinon on garde la route directe, valable partout
    où l'IP est acceptée.

    🔴 **La route RapidAPI est du code MORT, et le restera sauf retour du
    listing.** `rapidapi.com/api-sports/api/api-football` répond « NOT_FOUND —
    API not found » (vérifié le 21/08, §21.16), après deux échecs antérieurs
    d'une autre forme (§20.5, §21.9). Le code reste ici parce qu'il est juste
    et coûte zéro s'il n'est pas activé — mais **ne repars pas chercher cette
    page**, elle a déjà coûté trois allers-retours au projet. La parade
    utilisable aujourd'hui est `SCORES_FOOTBALL_BRIDGE=1`, qui appelle depuis
    le navigateur, donc depuis une IP résidentielle.
    """

    name = "api-football"
    sports = ("soccer",)

    def __init__(self, api_key: str | None = None,
                 rapidapi_key: str | None = None) -> None:
        self.rapidapi_key = rapidapi_key or os.getenv("SCORES_FOOTBALL_RAPIDAPI_KEY", "")
        self.api_key = api_key or os.getenv("SCORES_FOOTBALL_KEY", "")
        if self.rapidapi_key:
            base = API_FOOTBALL_RAPIDAPI_BASE
            headers = {
                "X-RapidAPI-Key": _require_key(
                    self.rapidapi_key, "SCORES_FOOTBALL_RAPIDAPI_KEY"),
                "X-RapidAPI-Host": API_FOOTBALL_RAPIDAPI_HOST,
                "Accept": "application/json",
            }
        else:
            base = API_FOOTBALL_BASE
            headers = {
                "x-apisports-key": _require_key(self.api_key, "SCORES_FOOTBALL_KEY"),
                "Accept": "application/json",
            }
        self.route = "rapidapi" if self.rapidapi_key else "direct"
        self._client = httpx.Client(base_url=base, timeout=_TIMEOUT, headers=headers)

    def __enter__(self) -> "ApiFootballScores":
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    @_RETRY
    def _get(self, path: str, params: dict) -> dict:
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def fetch_results(self, sport: str, day: date) -> list[MatchResult]:
        results, _ = self.fetch_with_counters(sport, day)
        return results

    def fetch_with_counters(
        self, sport: str, day: date,
    ) -> tuple[list[MatchResult], dict[str, int]]:
        if sport != "soccer":
            return [], {"sport_non_couvert": 1}
        # timezone=UTC : sans ce paramètre l'API découpe la journée sur le
        # fuseau du compte, et les matchs de fin de soirée basculeraient au
        # lendemain — donc absents du jour demandé, sans que rien ne le dise.
        payload = self._get("/fixtures", {"date": day.isoformat(), "timezone": "UTC"})
        errors = payload.get("errors")
        if errors:
            raise RuntimeError(_football_error_message(errors, self.route))
        return parse_apifootball_results(payload)


# --------------------------------------------------------------- tennis ----

LIVE_TENNIS_BASE = "https://api.livetennisapi.com/api/public/v1"

# Nombre de sets qu'il faut GAGNER selon le format. Sert d'invariant de
# complétude : un match dont le vainqueur n'a pas ce compte n'est pas allé au
# bout, quoi qu'en dise son statut.
_SETS_TO_WIN = {"BO3": 2, "BO5": 3}


def _tennis_is_complete(m: dict) -> bool:
    """Ce match est-il réellement allé à son terme ?

    ⚠️ `status` vaut « completed » sur 100 % des lignes rendues, y compris sur
    des relevés manifestement partiels — mesuré le 15/08 : 149 matchs tous
    « completed », dont 17 sans vainqueur et 28 dont le vainqueur n'a pas le
    compte de sets requis (abandons, ou relevé figé en cours de match). Se fier
    au statut écrirait des résultats faux pour 30 % des matchs.

    L'invariant, lui, est vérifiable : le vainqueur doit avoir exactement le
    nombre de sets que son format exige, et personne ne peut en avoir plus. Un
    abandon échoue au test — ce qui est le comportement voulu, un abandon étant
    de toute façon remboursé par les books et non gagné ou perdu.
    """
    score = m.get("score") or {}
    sets = score.get("sets")
    winner = m.get("winner")
    needed = _SETS_TO_WIN.get(m.get("format") or "")
    if not needed or winner not in (1, 2) or not isinstance(sets, list) or len(sets) != 2:
        return False
    return sets[winner - 1] == needed and max(sets) == needed


def parse_livetennis_results(payload: dict) -> tuple[list[MatchResult], dict[str, int]]:
    """`/history/matches?from=&to=` de Live Tennis API -> résultats notables."""
    counters = {"retenus": 0, "double": 0, "incomplet": 0, "champ_manquant": 0}
    out: list[MatchResult] = []

    for m in payload.get("data") or []:
        # Les doubles sont écartés : leurs noms arrivent mutilés — « Mi /
        # Victoria Luiza Barros », « - Bohrer Martins / Garcia Vidal »,
        # « Robert Cash / Stevens ». Prénoms tronqués, tirets parasites,
        # parfois un seul patronyme. Les apparier à des noms complets côté
        # Pinnacle produirait des rapprochements faux, donc de faux résultats
        # écrits en base. 21 % des lignes, sur un marché peu liquide (§17.10).
        if m.get("is_doubles"):
            counters["double"] += 1
            continue
        if not _tennis_is_complete(m):
            counters["incomplet"] += 1
            continue

        players = m.get("players") or {}
        p1 = ((players.get("p1") or {}).get("name") or "").strip()
        p2 = ((players.get("p2") or {}).get("name") or "").strip()
        start = _parse_dt(m.get("scheduled_time"))
        games = (m.get("score") or {}).get("games")

        if not (p1 and p2 and start) or not isinstance(games, list) or len(games) != 2:
            counters["champ_manquant"] += 1
            continue

        try:
            g1 = float(sum(games[0]))
            g2 = float(sum(games[1]))
        except (TypeError, ValueError):
            counters["champ_manquant"] += 1
            continue

        out.append(MatchResult(
            sport="tennis",
            home=p1,
            away=p2,
            start_time=start,
            # Le vainqueur vient du fournisseur, JAMAIS des jeux : on peut en
            # gagner plus et perdre le match (6-0 6-7 6-7 = 18 jeux contre 14,
            # et deux sets à un contre soi).
            winner="home" if m["winner"] == 1 else "away",
            # …et les jeux sont bien l'unité du marché « totals » au tennis :
            # médiane 22, plage 14-39 sur les 104 matchs complets du 15/08, ce
            # qui recouvre exactement les lignes 16,5-28 du §19.2. Des SETS y
            # rendraient « under » gagnant partout.
            home_score=g1,
            away_score=g2,
            source="livetennisapi",
            source_id=str(m.get("id") or ""),
        ))
        counters["retenus"] += 1

    return out, counters


class LiveTennisScores:
    """Résultats de tennis. ATP, WTA, Challenger et ITF.

    ⚠️ Le filtre de date s'appelle `from`/`to`. Le paramètre `date`, plus
    évident, est accepté et **silencieusement ignoré** : une requête sur le
    15/08 rendait des matchs du 24/06 au 17/08. Vérifié le 16/08.
    """

    name = "livetennisapi"
    sports = ("tennis",)
    page_size = 200
    # ⚠️ Le palier Basic plafonne à 60 requêtes/minute. Un rattrapage de
    # soixante jours enchaîne soixante appels : sans cadence, il tombe très
    # exactement sur la limite et échoue en plein milieu, laissant la moitié
    # de l'historique sans résultat. 1,1 s d'espacement rend 54 req/min, ce
    # qui laisse une marge sans allonger sensiblement le rattrapage (~70 s
    # pour deux mois).
    #
    # Lu à la CONSTRUCTION et non ici : une valeur figée à l'import rendrait
    # le réglage de `.env` sans effet, et rien ne dirait pourquoi. Même piège
    # que `provider_for`, §19.11.
    DEFAULT_MIN_INTERVAL_SEC = 1.1

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("SCORES_TENNIS_KEY", "")
        self.min_interval_sec = float(os.getenv(
            "SCORES_TENNIS_MIN_INTERVAL_SEC", str(self.DEFAULT_MIN_INTERVAL_SEC)))
        self._last_call = 0.0
        self._client = httpx.Client(
            base_url=LIVE_TENNIS_BASE, timeout=_TIMEOUT,
            headers={
                "Authorization":
                    f"Bearer {_require_key(self.api_key, 'SCORES_TENNIS_KEY')}",
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> "LiveTennisScores":
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    @_RETRY
    def _get(self, path: str, params: dict) -> dict:
        self._pace()
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _pace(self) -> None:
        """Espacer les appels pour rester sous la limite du palier.

        Portée sur l'INSTANCE et non sur l'appel : `results-update` crée un
        seul fournisseur puis boucle sur les journées, donc c'est bien la
        succession des jours qu'il faut cadencer, pas les pages d'un jour.
        """
        import time as _t
        wait = self.min_interval_sec - (_t.monotonic() - self._last_call)
        if wait > 0:
            _t.sleep(wait)
        self._last_call = _t.monotonic()

    def fetch_results(self, sport: str, day: date) -> list[MatchResult]:
        results, _ = self.fetch_with_counters(sport, day)
        return results

    def fetch_with_counters(
        self, sport: str, day: date,
    ) -> tuple[list[MatchResult], dict[str, int]]:
        if sport != "tennis":
            return [], {"sport_non_couvert": 1}

        results: list[MatchResult] = []
        counters: dict[str, int] = {}
        offset = 0
        # `meta.total` vaut toujours None : on ne peut pas savoir d'avance
        # combien de pages il y a, seulement suivre `has_more`. La borne dure
        # évite qu'une pagination qui ne se termine jamais vide le quota.
        for page_no in range(10):
            try:
                payload = self._get("/history/matches", {
                    "from": day.isoformat(), "to": day.isoformat(),
                    "limit": self.page_size, "offset": offset,
                })
            except Exception:
                # ⚠️ Une page de SUITE qui échoue ne doit pas emporter celles
                # déjà obtenues. Le palier gratuit ne donne que 20 appels
                # d'historique par MOIS et répond 403 au-delà : la première
                # page de chaque journée arrivait, puis la seconde levait, et
                # l'exception jetait tout — le sport entier affichait « panne »
                # alors qu'on tenait déjà l'essentiel de la journée.
                #
                # La première page, elle, remonte : si même elle échoue, il n'y
                # a rien à sauver et le problème doit se voir franchement.
                if page_no == 0:
                    raise
                counters["pages_refusees"] = counters.get("pages_refusees", 0) + 1
                break

            page, page_counters = parse_livetennis_results(payload)
            results.extend(page)
            for k, v in page_counters.items():
                counters[k] = counters.get(k, 0) + v
            meta = payload.get("meta") or {}
            if not meta.get("has_more"):
                break
            offset += meta.get("limit") or self.page_size

        return results, counters


class BridgedFootballScores:
    """Résultats de football lus depuis le pont navigateur.

    Même parseur que l'appel direct — c'est tout l'intérêt : le userscript
    repose la réponse BRUTE d'API-Sports, donc `parse_apifootball_results`
    n'a pas à savoir par où elle est arrivée, et les mesures faites sur la
    route directe restent valables telles quelles.

    Existe parce qu'API-Sports refuse les IP de datacenter (voir
    `ApiFootballScores`). Le navigateur de l'utilisateur est sur une IP
    résidentielle ; il appelle, la VM range et analyse.
    """

    name = "api-football (pont)"
    sports = ("soccer",)

    def __init__(self, directory: str | None = None) -> None:
        from pathlib import Path
        self.dir = Path(directory or os.getenv("SCORES_INGEST_DIR", "data/scores"))

    def __enter__(self) -> "BridgedFootballScores":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def fetch_results(self, sport: str, day: date) -> list[MatchResult]:
        results, _ = self.fetch_with_counters(sport, day)
        return results

    def fetch_with_counters(
        self, sport: str, day: date,
    ) -> tuple[list[MatchResult], dict[str, int]]:
        if sport != "soccer":
            return [], {"sport_non_couvert": 1}
        path = self.dir / sport / f"{day.isoformat()}.json"
        if not path.exists():
            # Distinct d'une journée sans match : le pont n'a pas encore posé
            # ce fichier. Confondre les deux ferait conclure que ces matchs
            # n'ont pas de résultat, alors qu'ils n'ont pas été demandés.
            return [], {"journee_non_pontee": 1}
        import json
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(f"pont scores: {path} illisible — {e}") from e
        return parse_apifootball_results(payload)


def provider_for(sport: str) -> type | None:
    """La source à utiliser pour ce sport, décidée à l'APPEL et non à l'import.

    Lire l'environnement au chargement du module figerait la route au démarrage
    du process : poser `SCORES_FOOTBALL_BRIDGE=1` dans `.env` n'aurait alors
    aucun effet visible, et rien ne dirait pourquoi. C'est le §19.11 — un
    service actif qui sert du code, ou ici de la configuration, déjà périmée.

    Le choix reste explicite : pas de repli automatique du direct vers le pont,
    qui masquerait le refus d'IP qu'on veut précisément voir.
    """
    if sport == "soccer":
        if os.getenv("SCORES_FOOTBALL_BRIDGE", "").strip().lower() in ("1", "true", "yes"):
            return BridgedFootballScores
        return ApiFootballScores
    if sport == "tennis":
        return LiveTennisScores
    return None
