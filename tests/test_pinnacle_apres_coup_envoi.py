"""Une cote Pinnacle prématch cesse d'être exploitable au coup d'envoi.

Le garde d'origine portait sur `isLive`, et `isLive` ment sur ce flux. Mesuré
le 22/08 : sur **21 matchs de football commencés depuis 5 à 240 minutes,
`isLive` valait False sur 21**. 576 marchés de matchs en cours entraient donc
dans la chaîne prématch à chaque cycle.

Et ces prix sont MORTS. Mesuré dans la même réponse HTTP, avec témoin interne
— la seule forme de mesure que le cache Cloudflare ne peut pas fausser : sur
180 secondes, **15 % des prix prématch bougent (2 077 sur 13 785) et 0 sur 354
des prix de matchs commencés**. `markets/straight` est un catalogue prématch.

Le coût, rejoué sur les cotes réellement stockées : Pinnacle est une jambe dans
**218 des 277 surebets live reconstitués (79 %)**, marge médiane 4,60 %. Un
arbitrage qui n'existe pas, fabriqué par l'écart entre un prix gelé et un book
qui a repricé.

Le critère fiable est `start_time` : toujours présent (sans lui `_read_matchup`
rend None), toujours en UTC, et stable pendant les cinq minutes de cache du
calendrier — ce qu'`isLive` n'est pas.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.models import MarketType
from src.scrapers import pinnacle as P
from src.scrapers.pinnacle import PinnacleScraper, matchups_cache_clear

MAINTENANT = datetime(2026, 8, 22, 20, 0, 0, tzinfo=timezone.utc)
MATCHUP_ID = 4242


class _Horloge(datetime):
    """`datetime` dont `now()` est figé — pour tester l'égalité exacte du cas B."""
    @classmethod
    def now(cls, tz=None):
        return MAINTENANT


def _matchup(start_raw, *, is_live=False):
    m = {
        "id": MATCHUP_ID,
        "participants": [{"name": "Anderlecht", "alignment": "home"},
                         {"name": "Club Brugge", "alignment": "away"}],
        "league": {"name": "Jupiler Pro League"},
        "isLive": is_live,
    }
    if start_raw is not None:
        m["startTime"] = start_raw
    return m


_MARCHES = [
    {"status": "open", "matchupId": MATCHUP_ID, "type": "moneyline", "period": 0,
     "prices": [{"designation": "home", "price": -110},
                {"designation": "draw", "price": +240},
                {"designation": "away", "price": +300}]},
    {"status": "open", "matchupId": MATCHUP_ID, "type": "total", "period": 0,
     "prices": [{"designation": "over", "points": 2.5, "price": -105},
                {"designation": "under", "points": 2.5, "price": -115}]},
]


def _cotes(start_raw, *, is_live=False):
    """Les OddQuote produites pour un match dont le coup d'envoi est `start_raw`."""
    matchups = [_matchup(start_raw, is_live=is_live)]

    def faux_get(self, path, params=None):
        if path.endswith("/matchups"):
            return matchups
        if "markets/straight" in path:
            return _MARCHES
        return []

    matchups_cache_clear()
    try:
        with patch.object(PinnacleScraper, "_get", faux_get), \
             patch.object(P, "datetime", _Horloge):
            sc = PinnacleScraper(request_delay=0)
            return list(sc.fetch_market_quotes("soccer"))
    finally:
        matchups_cache_clear()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ── A. Match futur : rien ne change ──────────────────────────────────────

def test_A_un_match_futur_est_accepte():
    qs = _cotes(_iso(MAINTENANT + timedelta(hours=2)))
    assert len(qs) == 5, "3 issues de 1X2 + 2 issues de total"


def test_A_bis_le_prematch_produit_EXACTEMENT_les_memes_donnees():
    """Non-régression fine : marchés, issues, lignes et cotes, une par une.

    C'est ce test qui garantit que la correction n'ampute rien du prématch
    légitime — le reste ne fait qu'écarter des matchs commencés."""
    qs = _cotes(_iso(MAINTENANT + timedelta(hours=2)))
    obtenu = sorted((q.market.value, q.outcome.label, q.outcome.line,
                     round(q.decimal_odd, 6)) for q in qs)
    assert obtenu == sorted([
        ("h2h", "home", None, round(1.0 + 100 / 110, 6)),
        ("h2h", "draw", None, 3.40),
        ("h2h", "away", None, 4.00),
        ("totals", "over", 2.5, round(1.0 + 100 / 105, 6)),
        ("totals", "under", 2.5, round(1.0 + 100 / 115, 6)),
    ])
    assert {q.league for q in qs} == {"Jupiler Pro League"}


def test_A_ter_un_match_dans_une_seconde_passe_encore():
    """La borne ne mord que du bon côté : tant que le coup d'envoi est devant,
    la cote reste du prématch, même à une seconde près."""
    assert len(_cotes(_iso(MAINTENANT + timedelta(seconds=1)))) == 5


# ── B. Coup d'envoi exactement maintenant ────────────────────────────────

def test_B_le_coup_d_envoi_exact_est_REFUSE():
    """Choix explicite, et c'est la convention du projet : `find_value_bets`
    écrit `if start <= now: continue`, et `_kickoff` documente « le sens sûr de
    l'erreur ». À l'instant du coup d'envoi, le prix prématch est déjà périmé."""
    assert _cotes(_iso(MAINTENANT)) == []


# ── C / D. Match commencé ────────────────────────────────────────────────

def test_C_commence_depuis_une_seconde_est_refuse():
    assert _cotes(_iso(MAINTENANT - timedelta(seconds=1))) == []


def test_D_commence_depuis_dix_minutes_est_refuse():
    assert _cotes(_iso(MAINTENANT - timedelta(minutes=10))) == []


def test_D_bis_isLive_a_False_ne_sauve_plus_rien():
    """⚠️ LE défaut corrigé. C'est exactement l'état observé en production :
    21 matchs commencés, `isLive=False` sur 21. Avant, ces cotes passaient."""
    assert _cotes(_iso(MAINTENANT - timedelta(minutes=45)), is_live=False) == []


def test_D_ter_isLive_a_True_reste_refuse_lui_aussi():
    """`is_live` est conservé dans la condition : elle ne peut qu'écarter
    davantage, jamais moins."""
    assert _cotes(_iso(MAINTENANT - timedelta(minutes=45)), is_live=True) == []
    assert _cotes(_iso(MAINTENANT + timedelta(hours=2)), is_live=True) == []


# ── E / F. start_time absent ou illisible ────────────────────────────────

def test_E_start_time_absent_le_matchup_est_ignore():
    """Comportement ANTÉRIEUR à cette correction, inchangé et re-documenté :
    `_read_matchup` rend None, le matchup n'entre pas dans l'index, et le
    garde `matchup is None` écarte ses marchés. Sans date, on ne peut rien
    affirmer — on se tait."""
    assert _cotes(None) == []
    assert _cotes("") == []


def test_F_start_time_illisible_LEVE_et_ce_test_le_fige():
    """⚠️ Défaut PRÉEXISTANT, volontairement NON corrigé ici — la consigne
    était une correction minimale et ciblée.

    `datetime.fromisoformat` lève sur une date illisible, et rien ne rattrape
    dans `_read_matchup`. En production l'`except Exception` de
    `fetch_all_parallel` l'absorbe : Pinnacle devient muet pour ce cycle. C'est
    borné et sans plantage, mais dégradé — et `_PINNACLE_FAILED` n'est pas posé,
    donc l'alerte de santé peut ne pas se déclencher.

    Ce test ÉPINGLE ce comportement : le jour où quelqu'un le change, ce sera
    un choix, pas un effet de bord."""
    with pytest.raises(ValueError, match="isoformat"):
        _cotes("pas-une-date")


# ── G. Date incohérente / match reporté ──────────────────────────────────

def test_G_un_report_vers_le_futur_est_accepte():
    """Pinnacle décale `startTime` quand un match est reporté. La nouvelle
    heure étant devant, la cote redevient du prématch — ce qui est correct."""
    assert len(_cotes(_iso(MAINTENANT + timedelta(days=3)))) == 5


def test_G_bis_une_date_lointaine_dans_le_passe_est_refusee():
    """Un calendrier qui annonce une heure passée pour un match non joué :
    on refuse. Perdre une cote coûte une opportunité, valoriser contre une
    ligne périmée coûte un faux positif — le §21.24 en a compté 218."""
    assert _cotes(_iso(MAINTENANT - timedelta(days=3))) == []


def test_G_ter_le_fuseau_est_respecte_quelle_que_soit_l_ecriture():
    """La même instant écrit en +02:00 doit produire la même décision qu'en Z :
    `start_time` est ramené en UTC par `.astimezone(timezone.utc)`."""
    dans_2h = MAINTENANT + timedelta(hours=2)
    en_z = _iso(dans_2h)
    en_offset = dans_2h.astimezone(timezone(timedelta(hours=2))).isoformat()
    assert len(_cotes(en_z)) == len(_cotes(en_offset)) == 5

    passe = MAINTENANT - timedelta(hours=2)
    assert _cotes(_iso(passe)) == []
    assert _cotes(passe.astimezone(timezone(timedelta(hours=2))).isoformat()) == []
