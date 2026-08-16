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
from datetime import date, datetime, timezone
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

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


def parse_apifootball_results(payload: dict) -> tuple[list[MatchResult], dict[str, int]]:
    """`/fixtures?date=` d'API-Football -> résultats notables."""
    counters = {"retenus": 0, "non_termine": 0, "score_manquant": 0}
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
            "Le compte n'est probablement pas en cause : cette API refuse les IP "
            "de datacenter et le présente ainsi. Vérifie depuis une IP "
            "résidentielle avant de toucher au compte, et pose "
            "SCORES_FOOTBALL_RAPIDAPI_KEY pour passer par RapidAPI, qui relaie "
            "depuis sa propre infrastructure."
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
    depuis un conteneur cloud. Le message est trompeur — le compte est actif,
    c'est l'origine qui est refusée. C'est le quatrième anti-bot du projet
    après DataDome, Gaming1 et Cloudflare, et le premier à toucher la mesure
    plutôt que la collecte.

    D'où les deux routes. Si `SCORES_FOOTBALL_RAPIDAPI_KEY` est posée, l'appel
    passe par RapidAPI, qui relaie depuis SA propre infrastructure : API-Sports
    ne voit jamais l'IP de la VM. La réponse est identique au champ près, donc
    le parseur ne change pas. Sinon on garde la route directe, valable partout
    où l'IP est acceptée.
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
                "X-RapidAPI-Key": self.rapidapi_key,
                "X-RapidAPI-Host": API_FOOTBALL_RAPIDAPI_HOST,
                "Accept": "application/json",
            }
        else:
            base = API_FOOTBALL_BASE
            headers = {"x-apisports-key": self.api_key, "Accept": "application/json"}
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

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("SCORES_TENNIS_KEY", "")
        self._client = httpx.Client(
            base_url=LIVE_TENNIS_BASE, timeout=_TIMEOUT,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Accept": "application/json"},
        )

    def __enter__(self) -> "LiveTennisScores":
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
        if sport != "tennis":
            return [], {"sport_non_couvert": 1}

        results: list[MatchResult] = []
        counters: dict[str, int] = {}
        offset = 0
        # `meta.total` vaut toujours None : on ne peut pas savoir d'avance
        # combien de pages il y a, seulement suivre `has_more`. La borne dure
        # évite qu'une pagination qui ne se termine jamais vide le quota.
        for _ in range(10):
            payload = self._get("/history/matches", {
                "from": day.isoformat(), "to": day.isoformat(),
                "limit": self.page_size, "offset": offset,
            })
            page, page_counters = parse_livetennis_results(payload)
            results.extend(page)
            for k, v in page_counters.items():
                counters[k] = counters.get(k, 0) + v
            meta = payload.get("meta") or {}
            if not meta.get("has_more"):
                break
            offset += meta.get("limit") or self.page_size

        return results, counters


PROVIDERS = {"soccer": ApiFootballScores, "tennis": LiveTennisScores}
