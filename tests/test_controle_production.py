"""Le controle de production doit detecter, pas rassurer.

Il tourne sur du trafic reel apres la bascule. S'il rendait GO quoi qu'il
arrive, il transformerait une panne en certitude — et personne ne s'en
apercevrait avant longtemps, puisque son role est justement d'etre la
seule chose qu'on regarde.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import scripts.controle_production as cp
import scripts.verifier_canaux as vc
from src.alerter import TelegramConfig
from src.channels import charger, depuis_config, installer
from src.models import Book, MarketType, Outcome, ValueBet
from src.routing import canaux_pour
from src.storage import Storage

CFG = dict(bot_token="jeton", chat_id="PRINCIPAL", premium_chat_id="PREMIUM",
           critical_chat_id="CRITIQUE", premium_hi_sports_exclus=("tennis",))


@pytest.fixture
def prod(tmp_path, monkeypatch):
    """Une base avec les canaux installes et du trafic route par le VRAI
    modele — donc coherent par construction."""
    chemin = str(tmp_path / "t.db")
    st = Storage(chemin)
    cfg = TelegramConfig(**CFG)
    installer(st, depuis_config(cfg), print_fn=lambda _s: None)
    canaux = charger(st)
    now = datetime.now(timezone.utc)
    c = sqlite3.connect(chemin)
    for i, (ev, cote, sport) in enumerate(
            ((6.0, 2.10, "soccer"), (12.0, 2.50, "soccer"),
             (40.0, 12.0, "soccer"), (25.0, 5.00, "soccer"),
             (25.0, 5.00, "tennis"))):
        quand = now - timedelta(minutes=30 - i)
        depart = quand + timedelta(hours=20)
        cle = f"{depart:%Y%m%d%H%M}::eq{i}a__vs__eq{i}b"
        c.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                  (cle, sport, "L", "a", "b", depart.isoformat()))
        c.execute("INSERT INTO value_bets(event_key, book, market, outcome_label,"
                  " line, odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct,"
                  " detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (cle, "unibet_be", "h2h", "home", None, cote, 0.5, 2.0, ev,
                   1.0, quand.isoformat()))
        bet = ValueBet(event_key=cle, book=Book.UNIBET_BE, market=MarketType.H2H,
                       outcome=Outcome("home"), odd_taken=cote, fair_prob=0.5,
                       fair_odd=2.0, ev_pct=ev, kelly_stake_pct=1.0,
                       detected_at=quand)
        for canal in canaux_pour(bet, sport=sport, league="L", is_live=False,
                                 canaux=canaux):
            c.execute("INSERT INTO notified_value_bets(event_key, book, market,"
                      " outcome_label, line, ev_pct, notified_at, chat_id)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (cle, "unibet_be", "h2h", "home", None, ev,
                       quand.isoformat(), canal.chat_id))
    c.commit()
    c.close()

    faux = lambda: type("C", (), {"db_path": chemin})()
    for mod in (cp, vc):
        monkeypatch.setattr(mod, "ScanConfig", faux)
        monkeypatch.setattr(mod, "load_env_file", lambda p: 0)
        monkeypatch.setattr(mod, "_systemctl", lambda *a: "", raising=False)
    monkeypatch.setattr(TelegramConfig, "from_env", staticmethod(lambda: cfg))
    monkeypatch.setattr("sys.argv", ["controle_production"])
    return chemin


def _lancer(capsys, **kw):
    import sys
    sys.argv = ["controle_production"] + [str(x) for kv in kw.items() for x in kv]
    code = cp.main()
    return code, capsys.readouterr().out


def _historique(chemin, heures: int, par_heure: int):
    """Du trafic AVANT la bascule, pour donner une reference au controle 7."""
    c = sqlite3.connect(chemin)
    base = datetime.now(timezone.utc) - timedelta(days=10)
    for h in range(heures):
        for k in range(par_heure):
            c.execute("INSERT INTO notified_value_bets(event_key, book, market,"
                      " outcome_label, line, ev_pct, notified_at, chat_id)"
                      " VALUES (?,?,?,?,?,?,?,NULL)",
                      ("vieux", "unibet_be", "h2h", "home", None, 6.0,
                       (base + timedelta(hours=h, minutes=k)).isoformat()))
    c.commit()
    c.close()


# ══ le cas sain ════════════════════════════════════════════════════════
def test_un_trafic_coherent_rend_GO(prod, capsys):
    code, sortie = _lancer(capsys)
    assert code == 0, sortie
    assert "GO — les huit controles passent." in sortie
    assert "inexplicables     : 0" in sortie


def test_le_zero_multi_canal_est_explique_et_non_compte_comme_faute(prod, capsys):
    """Avec cette configuration aucun pari ne PEUT atteindre deux canaux. Le
    controle doit le dire, pas le presenter comme un echec."""
    code, sortie = _lancer(capsys)
    assert code == 0
    assert "dont plusieurs canaux   : 0" in sortie
    assert "resultat ATTENDU" in sortie


# ══ ce qu'il doit attraper ═════════════════════════════════════════════
def test_une_alerte_partie_sur_le_mauvais_canal(prod, capsys):
    """LE controle qui compte : il porte sur le DAEMON, pas sur la fonction
    de routage. Une alerte que le modele n'explique pas doit se voir."""
    c = sqlite3.connect(prod)
    r = c.execute("SELECT rowid FROM notified_value_bets WHERE ev_pct < 8 "
                  "AND chat_id='PRINCIPAL'").fetchone()
    c.execute("UPDATE notified_value_bets SET chat_id='CRITIQUE' WHERE rowid=?",
              (r[0],))
    c.commit()
    c.close()
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "inexplicables     : 1" in sortie
    assert "parti sur CRITIQUE" in sortie and "modele : PRINCIPAL" in sortie


def test_une_rafale_au_dessus_du_maximum_historique(prod, capsys):
    _historique(prod, heures=200, par_heure=1)
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "au-dessus du maximum historique" in sortie


def test_une_pointe_dans_l_enveloppe_historique_ne_declenche_rien(prod, capsys):
    """Le trafic est en grappes le soir. Comparer a une MOYENNE ferait crier
    au loup a chaque soiree normale ; on compare au maximum deja observe."""
    _historique(prod, heures=200, par_heure=40)
    code, sortie = _lancer(capsys)
    assert code == 0, sortie
    assert "pointe dans l'enveloppe historique" in sortie


def test_sans_trafic_le_controle_le_dit_et_ne_rend_pas_GO(prod, capsys):
    c = sqlite3.connect(prod)
    c.execute("DELETE FROM notified_value_bets")
    c.commit()
    c.close()
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "ne mesurent RIEN" in sortie


def test_exemples_zero_n_affiche_rien(prod, capsys):
    """`lignes[-0:]` rend TOUTE la liste, pas rien — le piege classique."""
    code, sortie = _lancer(capsys, **{"--exemples": 0})
    assert code == 0
    bloc = sortie.split("── 8.")[1].split("═")[0]
    assert bloc.strip().endswith("dernieres alertes ──"), bloc
