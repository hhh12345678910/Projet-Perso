"""Les trois morceaux de `dRest`, nommés — et surtout SÉPARÉS.

CE QUI A COÛTÉ CINQ HYPOTHÈSES FAUSSES
--------------------------------------
`dRest` disait « le temps est dans le bloc des alertes » sans dire lequel des
trois morceaux le prend :

  * `tgIni` — la construction de l'alerter : trois lectures en base, une fois
    par appel (paris déjà joués, books en sourdine, canaux configurés) ;
  * `dedup` — DEUX requêtes SQL par pari ET PAR CANAL. Avec 76 paris et cinq
    canaux, cela fait 760 requêtes, chacune sur un `event_key LIKE` que
    SQLite ne peut pas servir par index ;
  * `envoi`  — la pause de `min_send_interval_s` plus le POST.

Trois causes concurrentes qui produisent toutes la même ligne. Le 04/09, j'ai
accusé la troisième sur la foi d'une arithmétique — 118,6 / 3,2 = 37,06 — et
la sonde m'a réfuté : zéro alerte délivrée, zéro non-200, et `dRest` toujours
à 96 s. Une coïncidence numérique n'est pas une mesure.

LE PIÈGE QUE CES TESTS VERROUILLENT
-----------------------------------
`_doit_notifier` a TROIS sorties, dont deux rendent False. Compter le temps
seulement sur le chemin nominal cacherait très exactement le cas cher : celui
où tout est dédoublonné, donc où rien ne part, donc où le compteur d'alertes
délivrées reste à zéro pendant que le cycle brûle deux minutes. C'est le cas
observé en production. D'où le `finally`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from src.alerter import TelegramAlerter, TelegramConfig, send_alerts
from src.models import Book, MarketType, Outcome, ValueBet

NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    TelegramAlerter._next_slot.clear()
    TelegramAlerter._cooldown_until.clear()
    yield
    TelegramAlerter._next_slot.clear()
    TelegramAlerter._cooldown_until.clear()


def _bet(ev_pct: float = 5.0) -> ValueBet:
    return ValueBet(
        event_key="209906010000::boise__vs__sarasota", book=Book.UNIBET_BE,
        market=MarketType.H2H, outcome=Outcome(label="home", line=None),
        odd_taken=1.86, fair_prob=0.565, fair_odd=1.77, ev_pct=ev_pct,
        kelly_stake_pct=1.5, detected_at=NOW)


def _client_ok() -> MagicMock:
    c = MagicMock(spec=httpx.Client)
    c.post.return_value.status_code = 200
    return c


# ── Les compteurs existent et sont remplis ───────────────────────────

def test_send_alerts_remplit_les_trois_compteurs():
    ch: dict = {}
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0,
                         min_send_interval_s=0.0)
    send_alerts([_bet()], cfg, chrono=ch)
    assert "tgIni" in ch, ch
    assert "envoi" in ch and ch["nEnvoi"] >= 1, ch


def test_le_chrono_survit_a_l_alerter():
    """L'alerter est reconstruit à chaque cycle ; le dict appartient à
    l'appelant, sinon la mesure disparaîtrait avec lui."""
    ch: dict = {}
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0,
                         min_send_interval_s=0.0)
    send_alerts([_bet()], cfg, chrono=ch)
    n1 = ch["nEnvoi"]
    send_alerts([_bet(ev_pct=6.0)], cfg, chrono=ch)
    assert ch["nEnvoi"] > n1, "le second appel n'a rien ajouté"


def test_sans_chrono_rien_ne_casse():
    """La signature reste compatible : l'appelant qui n'en veut pas
    n'en fournit pas."""
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0,
                         min_send_interval_s=0.0)
    assert send_alerts([_bet()], cfg) is not None


# ── LE PIÈGE : le temps du dédoublonnage qui n'aboutit pas ───────────

def test_le_dedoublonnage_est_compte_meme_quand_il_ecarte(tmp_path,
                                                          monkeypatch):
    """LE CAS QUI COMPTE. Un pari écarté par le dédoublonnage ne s'envoie
    pas — il coûte quand même ses deux requêtes SQL. Sans le `finally`, un
    cycle entièrement dédoublonné afficherait `dedup 0.0` alors que c'est
    précisément là que passe tout son temps."""
    from src.storage import Storage
    base = Storage(str(tmp_path / "v.db"))
    monkeypatch.setattr("src.alerter._PLAYS_DB", tmp_path / "v.db")

    ch: dict = {}
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0,
                         valuebet_max_alerts=0)   # tout est écarté d'emblée
    a = TelegramAlerter(cfg, client=_client_ok(), chrono=ch,
                        canaux=[], storage=base)
    b = _bet()
    assert a._doit_notifier(b, "c") is False
    a.close()
    assert ch["nDedup"] == 1, ch
    assert "dedup" in ch, ch


def test_le_dedoublonnage_est_compte_quand_il_laisse_passer(tmp_path,
                                                            monkeypatch):
    from src.storage import Storage
    base = Storage(str(tmp_path / "v.db"))
    monkeypatch.setattr("src.alerter._PLAYS_DB", tmp_path / "v.db")
    ch: dict = {}
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    a = TelegramAlerter(cfg, client=_client_ok(), chrono=ch,
                        canaux=[], storage=base)
    assert a._doit_notifier(_bet(), "c") is True
    a.close()
    assert ch["nDedup"] == 1, ch


def test_un_envoi_refuse_par_le_quota_est_quand_meme_compte():
    """`_reserve_slot` peut rendre None sans rien imprimer (file trop en
    avance). Cet envoi-là ne dort pas, mais il DOIT apparaître dans `nEnvoi` —
    sinon un canal saturé disparaîtrait de la mesure."""
    ch: dict = {}
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_send_interval_s=100.0,
                         max_defer_s=0.0)
    a = TelegramAlerter(cfg, client=_client_ok(), chrono=ch, canaux=[])
    TelegramAlerter._next_slot["c"] = __import__("time").time() + 500
    assert a._send("x", "c") is False
    a.close()
    assert ch["nEnvoi"] == 1, ch


# ── Les compteurs ne sont pas des durées ─────────────────────────────

def test_les_comptes_ne_sont_pas_des_phases():
    """`nDedup` et `nEnvoi` sont des COMPTES. S'ils entraient dans
    `par_phase`, `ligne_phases` les afficherait comme des secondes et `reste`
    les soustrairait — 152 dédoublonnages deviendraient 152 secondes."""
    import inspect

    from src import main
    src = inspect.getsource(main._daemon_scan_sport)
    # Aucune ligne ne doit à la fois écrire dans `par_phase` et nommer un
    # compteur. Découper le source à l'aveugle attraperait la ligne d'à côté,
    # qui a le droit de lire `nDedup` pour l'AFFICHER.
    fautives = [l for l in src.splitlines()
                if "par_phase" in l and ("nDedup" in l or "nEnvoi" in l)]
    assert not fautives, fautives
    # Et la liste des clés promues en phases est explicite, donc vérifiable.
    assert 'for _k in ("tgIni", "dedup", "envoi")' in src


def test_les_trois_phases_sont_soustraites_du_parent():
    """⚠️ LE BUG DU 03/09, QUI SERAIT REVENU. Une phase comptée dans
    `par_phase` sans être retirée de `detc_reste` fait additionner deux fois
    le même temps ; `reste` devient négatif, donc écrêté à zéro, donc muet —
    et c'est lui qui dit où chercher."""
    import inspect

    from src import main
    src = inspect.getsource(main._daemon_scan_sport)
    ligne = [l for l in src.splitlines() if "_enfants = sum(" in l]
    assert ligne, "le calcul des enfants a disparu"
    bloc = src.split("_enfants = sum(")[1][:300]
    for phase in ("tgIni", "dedup", "envoi"):
        assert phase in bloc, f"{phase} n'est pas soustraite de detc_reste"


def test_la_ligne_de_phases_tient_toujours_sous_80_colonnes():
    """Trois phases de plus, et `rich` coupe toujours à 80 hors terminal."""
    import io

    from rich.console import Console

    from src.orchestration import Chrono, ligne_phases
    ch = Chrono()
    ch.par_phase.update({
        "fetch": 20.9, "detc_reste": 3.0, "base": 1.2, "fair": 1.0,
        "retards": 0.6, "marques": 0.4, "clv_alertes": 0.3, "detection": 0.2,
        "surebets": 0.9, "middles": 0.1, "find": 0.5, "insVB": 0.4,
        "feat": 0.3, "seed": 0.2, "suivi": 0.1, "tgIni": 1.1,
        "dedup": 88.4, "envoi": 6.4,
    })
    for sport in ("volleyball", "basketball", "soccer", "tennis"):
        buf = io.StringIO()
        Console(file=buf, width=80).print(ligne_phases(sport, ch))
        rendu = buf.getvalue().rstrip("\n")
        assert "\n" not in rendu, f"{sport} : enveloppée — {rendu!r}"
        assert len(rendu) <= 80, f"{sport} : {len(rendu)} col — {rendu!r}"
        assert " tot " in rendu, f"{sport} : `tot` perdu — {rendu!r}"
