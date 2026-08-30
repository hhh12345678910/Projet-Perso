"""ANCIEN ROUTAGE → NOUVEAU ROUTAGE. Identiques, ou on ne branche pas.

Pourquoi cet outil existe
-------------------------
Le commit 4 est une migration a comportement equivalent, pas une refonte.
La seule preuve acceptable est une comparaison, pari par pari, entre le
routage historique et le routage par canaux — sur la configuration
actuelle, avant d'avoir cree le moindre canal personnalise.

Les deux routages viennent de la MEME fonction de production,
`TelegramAlerter.send_value_bet` :

    canaux=()          -> chemin historique (les quatre `if` de .env)
    canaux=depuis_config(cfg) -> chemin par canaux

Aucune regle n'est reimplementee ici. Une sonde qui recalcule autre chose
que la production ment (§17.7), et c'est encore plus vrai d'une sonde
censee valider la production.

    .venv/bin/python -m scripts.comparer_routage
    .venv/bin/python -m scripts.comparer_routage --historique --limite 5000

⚠️ Rien n'est envoye : `_send` est remplace par un releve du chat_id vise,
et le dedoublonnage tape dans une base temporaire jetee a la fin.
"""
from __future__ import annotations

import argparse
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import src.alerter as al
from src.alerter import TelegramAlerter, TelegramConfig
from src.channels import depuis_config
from src.config import ScanConfig, load_env_file
from src.models import Book, MarketType, Outcome, ValueBet
from src.storage import Storage

# Balayage exhaustif AUTOUR des bornes : chaque seuil de la configuration est
# teste a la valeur pile, un cran en dessous et un cran au-dessus. C'est la
# que les deux routages peuvent differer sans que rien d'autre ne le montre.
_EV = (-5.0, 0.0, 4.99, 5.0, 5.01, 7.99, 8.0, 8.01, 12.0, 19.99, 20.0,
       20.01, 25.0, 34.99, 35.0, 35.01, 60.0, 250.0)
_COTES = (1.01, 1.49, 1.50, 1.51, 2.10, 3.99, 4.00, 4.01, 5.00, 5.99,
          6.00, 6.01, 8.00, 12.0, 101.0)
_SPORTS = ("soccer", "tennis", "basketball", None)
_BOOKS = (Book.UNIBET_BE, Book.BETANO_BE, Book.ELITESPORTS)
_MARCHES = (MarketType.H2H, MarketType.TOTALS)
_LIGUES = ("Belgique - Pro League", None)


def _pari(ev, cote, book, marche, quand, n=0) -> ValueBet:
    """`n` rend la cle de dedoublonnage UNIQUE.

    Sans lui, tous les cas synthetiques partagent (event_key, book, market,
    outcome, line) : le nouveau chemin, qui dedoublonne par canal, se
    plafonne des le second cas et paraît diverger de l'ancien — qui, lui, ne
    dedoublonne pas dans `send_value_bet` (c'est `main` qui le fait). La
    premiere execution de ce harnais a produit 3 840 fausses divergences pour
    cette seule raison. On compare des paris DISTINCTS, comme en production.
    """
    return ValueBet(
        event_key=f"{quand:%Y%m%d%H%M}::cas{n:06d}_a__vs__cas{n:06d}_b", book=book,
        market=marche, outcome=Outcome("home"), odd_taken=cote,
        fair_prob=0.5, fair_odd=round(cote / (1 + ev / 100), 4) or 1.0,
        ev_pct=ev, kelly_stake_pct=1.0,
        detected_at=datetime.now(timezone.utc))


class _Espion(TelegramAlerter):
    """Capture les chat_id vises au lieu de parler a Telegram."""

    def __init__(self, cfg, canaux, storage):
        super().__init__(cfg, canaux=canaux, storage=storage)
        self.vus: list[str] = []

    def _send(self, text, chat_id, reply_markup=None):
        self.vus.append(chat_id)
        return True

    def route(self, bet, sport):
        self.vus.clear()
        self.send_value_bet(bet, sport=sport)
        return list(self.vus)


def _bases(dossier: Path):
    """Une base NEUVE par routage : le dedoublonnage ne doit pas faire croire
    a une divergence parce que le premier passage a deja notifie."""
    return Storage(str(dossier / "ancien.db")), Storage(str(dossier / "nouveau.db"))


def comparer(cas, cfg, *, print_fn=print) -> dict:
    """`cas` : iterable de (libelle, bet, sport, league_ignoree).

    Rend le compte et la liste des divergences. Rien n'est masque : chaque
    ecart est rendu tel quel, avec ses entrees."""
    canaux = depuis_config(cfg)
    with tempfile.TemporaryDirectory() as d:
        st_a, st_n = _bases(Path(d))
        ancien = _Espion(cfg, (), st_a)
        nouveau = _Espion(cfg, canaux, st_n)
        total = identiques = 0
        divergences = []
        for libelle, bet, sport in cas:
            a = sorted(ancien.route(bet, sport))
            n = sorted(nouveau.route(bet, sport))
            total += 1
            if a == n:
                identiques += 1
            else:
                divergences.append({"cas": libelle, "ancien": a, "nouveau": n})
    return {"total": total, "identiques": identiques, "divergences": divergences,
            "canaux": [c.nom for c in canaux]}


def cas_synthetiques():
    """Le produit cartesien des bornes, en prematch ET en live."""
    maintenant = datetime.now(timezone.utc)
    futur = maintenant + timedelta(days=2)          # prematch, hors fenetre morte
    passe = maintenant - timedelta(hours=1)         # live
    n = 0
    for quand, phase in ((futur, "prematch"), (passe, "live")):
        for ev in _EV:
            for cote in _COTES:
                for sport in _SPORTS:
                    for book in _BOOKS:
                        for marche in _MARCHES:
                            n += 1
                            bet = _pari(ev, cote, book, marche, quand, n)
                            yield (f"{phase} ev={ev} cote={cote} sport={sport} "
                                   f"book={book.value} marche={marche.value}",
                                   bet, sport)


def cas_historiques(limite: int, db: str):
    """Les vraies detections, jointes a `events` pour le sport et la ligue.

    `value_bets` ne porte ni sport ni league (§A.3 de l'audit) : la jointure
    par event_key est le seul moyen de les retrouver. Une detection dont
    l'evenement a ete purge sort avec sport=None — c'est un cas interessant,
    pas un cas a jeter."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    lignes = con.execute(
        "SELECT v.event_key, v.book, v.market, v.outcome_label, v.line,"
        "       v.odd_taken, v.fair_prob, v.fair_odd, v.ev_pct, v.kelly_pct,"
        "       v.detected_at, e.sport, e.league"
        "  FROM value_bets v LEFT JOIN events e ON e.event_key = v.event_key"
        " ORDER BY v.id DESC LIMIT ?", (limite,)).fetchall()
    con.close()
    for r in lignes:
        try:
            bet = ValueBet(
                event_key=r["event_key"], book=Book(r["book"]),
                market=MarketType(r["market"]),
                outcome=Outcome(r["outcome_label"], line=r["line"]),
                odd_taken=r["odd_taken"], fair_prob=r["fair_prob"] or 0.5,
                fair_odd=r["fair_odd"] or 1.0, ev_pct=r["ev_pct"],
                kelly_stake_pct=r["kelly_pct"] or 0.0,
                detected_at=datetime.fromisoformat(r["detected_at"]),
                league=r["league"])
        except (ValueError, KeyError, TypeError):
            # Un book ou un marche disparu de l'enum : on le DIT plutot que
            # de le sauter en silence — un cas non compare n'est pas un cas
            # identique.
            yield (f"ILLISIBLE {r['event_key']} {r['book']} {r['market']}", None, None)
            continue
        yield (f"{r['event_key']} {r['book']} {r['market']} "
               f"ev={r['ev_pct']:.1f} cote={r['odd_taken']}", bet, r["sport"])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--historique", action="store_true",
                   help="rejoue les vraies detections de la base")
    p.add_argument("--limite", type=int, default=20000)
    a = p.parse_args()

    print(f"env : {load_env_file('.env')} cles chargees depuis .env")
    cfg = TelegramConfig.from_env()
    if cfg is None:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents — rien a comparer.")
        return 2

    if a.historique:
        db = ScanConfig().db_path
        brut = list(cas_historiques(a.limite, db))
        illisibles = [c for c, b, _ in brut if b is None]
        cas = [(c, b, s) for c, b, s in brut if b is not None]
        print(f"source : {db}")
        print(f"detections lues : {len(brut)}  (illisibles : {len(illisibles)})")
        for x in illisibles[:20]:
            print(f"   {x}")
    else:
        cas = list(cas_synthetiques())
        print(f"cas synthetiques : {len(cas)}")

    r = comparer(cas, cfg)
    print(f"canaux traduits : {', '.join(r['canaux'])}")
    print()
    print(f"  compares    : {r['total']}")
    print(f"  identiques  : {r['identiques']}")
    print(f"  divergences : {len(r['divergences'])}")
    for d in r["divergences"]:
        print(f"\n  ✗ {d['cas']}\n      ancien  : {d['ancien']}\n      nouveau : {d['nouveau']}")
    print()
    if r["divergences"]:
        print("NO-GO — l'ancien et le nouveau routage different.")
        return 1
    print("GO — routage identique sur tous les cas compares.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
