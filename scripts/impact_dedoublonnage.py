"""Combien d'alertes EN PLUS le dedoublonnage par canal produirait-il ?

LECTURE SEULE. N'ecrit rien dans la base du projet, n'envoie rien, ne
modifie aucun canal. Le dedoublonnage simule tape dans une base temporaire
jetee a la fin.

    .venv/bin/python -m scripts.impact_dedoublonnage
    .venv/bin/python -m scripts.impact_dedoublonnage --exemples 20

La question
-----------
Aujourd'hui `main.py` dedoublonne UNE FOIS pour toutes les destinations :
un pari deja notifie est muet partout, meme s'il vient d'entrer dans la
bande premium. Apres bascule, chaque canal a sa propre memoire. Le routage
est identique (32 960 cas compares, 0 divergence) ; c'est le VOLUME qui
peut changer.

Ce que la base permet de voir, et ce qu'elle ne permet pas
---------------------------------------------------------
`insert_value_bet` garde UNE ligne par opportunite : `ev_pct`/`odd_taken`
figent la PREMIERE detection, `last_ev`/`last_odd` portent la DERNIERE.
On dispose donc de DEUX points par opportunite, jamais de la trajectoire.

⚠️ Le resultat est donc un PLANCHER, pas une estimation centree. Un pari
passe de 7,9 a 8,5 puis redescendu a 7,9 se lit ici comme immobile, alors
qu'il aurait franchi une frontiere de bande. Le vrai surplus est
superieur, d'un facteur qu'on ne peut pas mesurer avec ces colonnes.

Ce que la sonde reimplemente, et pourquoi
-----------------------------------------
Le ROUTAGE n'est pas reimplemente : `routing.canaux_pour` est appele, avec
les canaux que `channels.depuis_config` traduit de .env — et l'egalite de
ce couple avec le routage historique est deja etablie (`comparer_routage`,
32 960 cas synthetiques + 20 000 detections reelles, 0 divergence). Le
DEDOUBLONNAGE n'est pas reimplemente non plus : ce sont les vraies
fonctions de `Storage`, avec `chat_id=None` pour la portee globale et un
`chat_id` pour la portee par canal.

Deux garde-fous sont en revanche reassembles ici, faute d'exister comme
fonctions : la fenetre morte avant coup d'envoi et la suppression des
marches de mi-temps (`alerter.send_value_bet`). Ils s'appliquent des deux
cotes, donc ils ne peuvent pas gonfler l'ecart — au pire ils le retrecissent.

Ne sont PAS rejoues : la sourdine `/book` et les marches deja joues, qui
dependent d'un etat present et non de l'instant de la detection. Eux aussi
s'appliquent des deux cotes.
"""
from __future__ import annotations

import argparse
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.alerter import TelegramConfig
from src.channels import depuis_config
from src.config import ScanConfig, load_env_file
from src.models import Book, MarketType, Outcome, ValueBet, is_half_time
from src.matcher import parse_event_key
from src.routing import canaux_pour
from src.storage import Storage

# Au-dela de ce nombre de marques en attente, on vide la table temporaire a
# la prochaine frontiere de groupe. Assez bas pour que le balayage reste
# trivial, assez haut pour que la purge reste amortie.
PURGE_AU_DELA = 200

TRANCHES = ((1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 6.0), (6.0, 1e9))


def _tranche(cote: float) -> str:
    for bas, haut in TRANCHES:
        if bas <= cote < haut:
            return f"{bas:g}-{haut:g}" if haut < 1e9 else f"{bas:g}+"
    return "?"


def _bande_ev(ev: float, cfg) -> str:
    if ev >= cfg.min_critical_ev_pct:
        return "critique"
    if ev >= cfg.premium_hi_min_ev:
        return "hi"
    if ev >= cfg.min_premium_ev_pct:
        return "premium"
    if ev >= cfg.min_ev_pct:
        return "principal"
    return "sous-seuil"


def _instantanes(r) -> list[tuple[float, float, datetime]]:
    """Les points connus d'une opportunite : premier, et dernier s'il differe."""
    points = [(r["ev_pct"], r["odd_taken"], datetime.fromisoformat(r["detected_at"]))]
    if r["last_ev"] is not None and r["last_seen_at"]:
        dernier = (r["last_ev"], r["last_odd"] if r["last_odd"] is not None
                   else r["odd_taken"], datetime.fromisoformat(r["last_seen_at"]))
        if (dernier[0], dernier[1]) != (points[0][0], points[0][1]):
            points.append(dernier)
    return points


def _recevable(bet: ValueBet, quand: datetime, cfg) -> tuple[bool, bool]:
    """(recevable, is_live) — les deux garde-fous globaux calculables ici."""
    if is_half_time(bet.market):
        return False, False
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=timezone.utc)
    p = parse_event_key(bet.event_key)
    if p is None:
        return True, False
    depart = p[0]
    if depart.tzinfo is None:
        depart = depart.replace(tzinfo=timezone.utc)
    live = depart <= quand
    if not live and (depart - quand).total_seconds() / 60 < cfg.min_minutes_to_kickoff:
        return False, False
    return True, live


def analyser(db: str, cfg, *, limite: int | None = None) -> dict:
    canaux = depuis_config(cfg)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    sql = ("SELECT v.*, e.sport, e.league FROM value_bets v "
           "LEFT JOIN events e ON e.event_key = v.event_key ORDER BY v.id")
    if limite:
        sql += f" LIMIT {int(limite)}"
    lignes = con.execute(sql).fetchall()
    con.close()

    # Les opportunites de groupes DIFFERENTS n'interagissent jamais : le
    # dedoublonnage ne regarde que (date+equipes, book, marche, issue, ligne).
    # On les traite donc groupe par groupe, en vidant la table temporaire des
    # qu'elle grossit. Sans cela chaque verification balaie une table qui
    # grandit — `event_key LIKE ?` ne peut pas utiliser d'index, SQLite faisant
    # LIKE insensible a la casse par defaut — et le cout devient quadratique :
    # mesure a 27 minutes sur 36 000 opportunites, contre ~7 ainsi.
    def _groupe(r):
        return (Storage._event_key_like(r["event_key"]), r["book"], r["market"],
                r["outcome_label"], r["line"])

    lignes = sorted(lignes, key=lambda r: (_groupe(r), r["id"]))
    groupe_courant = None
    marques = 0

    envois_ancien: Counter = Counter()
    envois_nouveau: Counter = Counter()
    surplus: list[dict] = []
    multi = 0
    motifs: Counter = Counter()
    illisibles = 0
    horodatages: list[datetime] = []

    with tempfile.TemporaryDirectory() as d:
        st_a = Storage(str(Path(d) / "ancien.db"))
        st_n = Storage(str(Path(d) / "nouveau.db"))
        for r in lignes:
            g = _groupe(r)
            if g != groupe_courant:
                # Frontiere de groupe : c'est le SEUL endroit ou vider est sur.
                # Purger au milieu d'un groupe effacerait l'historique dont le
                # dedoublonnage a besoin, et fabriquerait de faux surplus.
                if marques > PURGE_AU_DELA:
                    st_a.prune_notifications(retention_days=0)
                    st_n.prune_notifications(retention_days=0)
                    marques = 0
                groupe_courant = g
            try:
                base = dict(
                    event_key=r["event_key"], book=Book(r["book"]),
                    market=MarketType(r["market"]),
                    outcome=Outcome(r["outcome_label"], line=r["line"]),
                    fair_prob=r["fair_prob"] or 0.5, fair_odd=r["fair_odd"] or 1.0,
                    kelly_stake_pct=r["kelly_pct"] or 0.0, league=r["league"])
            except (ValueError, TypeError):
                illisibles += 1
                continue
            cle = (r["event_key"], r["book"], r["market"], r["outcome_label"], r["line"])
            points = _instantanes(r)
            vus_ancien: set[str] = set()
            for ev, cote, quand in points:
                bet = ValueBet(**base, odd_taken=cote, ev_pct=ev, detected_at=quand)
                ok, live = _recevable(bet, quand, cfg)
                if not ok:
                    continue
                cibles = canaux_pour(bet, sport=r["sport"], league=r["league"],
                                     is_live=live, canaux=canaux)
                if not cibles:
                    continue
                horodatages.append(quand)
                if len(cibles) > 1:
                    multi += 1

                # ── ANCIEN : une seule porte, pour toutes les destinations
                passe = (st_a.value_bet_notify_count(*cle) < cfg.valuebet_max_alerts
                         and not (cfg.valuebet_dedup and st_a.value_bet_already_notified(
                             *cle, current_ev_pct=ev,
                             ev_delta_pct=cfg.valuebet_ev_delta_pct)))
                if passe:
                    for c in cibles:
                        envois_ancien[c.nom] += 1
                        vus_ancien.add(c.nom)
                    st_a.mark_value_bet_notified(*cle, ev, quand)
                    marques += 1

                # ── NOUVEAU : une porte PAR canal
                for c in cibles:
                    ouvert = (st_n.value_bet_notify_count(*cle, chat_id=c.chat_id)
                              < cfg.valuebet_max_alerts
                              and not (cfg.valuebet_dedup
                                       and st_n.value_bet_already_notified(
                                           *cle, current_ev_pct=ev,
                                           ev_delta_pct=cfg.valuebet_ev_delta_pct,
                                           chat_id=c.chat_id)))
                    if not ouvert:
                        continue
                    envois_nouveau[c.nom] += 1
                    st_n.mark_value_bet_notified(*cle, ev, quand, chat_id=c.chat_id)
                    marques += 1
                    if not passe:
                        motif = ("canal jamais atteint auparavant"
                                 if c.nom not in vus_ancien
                                 else "re-alerte sur un canal deja atteint")
                        motifs[motif] += 1
                        surplus.append({
                            "cle": f"{r['event_key']} {r['book']} {r['market']}"
                                   f"{'' if r['line'] is None else ' ' + str(r['line'])}"
                                   f" {r['outcome_label']}",
                            "canal": c.nom, "sport": r["sport"], "ev": ev, "cote": cote,
                            "depuis": f"EV {points[0][0]:.1f} ({_bande_ev(points[0][0], cfg)}, "
                                      f"cote {points[0][1]} [{_tranche(points[0][1])}])",
                            "vers": f"EV {ev:.1f} ({_bande_ev(ev, cfg)}, "
                                    f"cote {cote} [{_tranche(cote)}])",
                            "bande_changee": _bande_ev(points[0][0], cfg) != _bande_ev(ev, cfg),
                            "tranche_changee": _tranche(points[0][1]) != _tranche(cote),
                            "motif": motif})

    jours = 0.0
    if horodatages:
        jours = max((max(horodatages) - min(horodatages)).total_seconds() / 86400, 1e-9)
    return {"lignes": len(lignes), "illisibles": illisibles, "multi": multi,
            "ancien": envois_ancien, "nouveau": envois_nouveau,
            "surplus": surplus, "motifs": motifs, "jours": jours,
            "canaux": [c.nom for c in canaux],
            "deux_points": sum(1 for r in lignes if len(_instantanes(r)) > 1)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--exemples", type=int, default=12)
    p.add_argument("--limite", type=int, default=None)
    a = p.parse_args()

    print(f"env : {load_env_file('.env')} cles chargees depuis .env")
    cfg = TelegramConfig.from_env()
    if cfg is None:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents.")
        return 2
    db = ScanConfig().db_path
    print(f"source : {db}  (LECTURE SEULE)")
    r = analyser(db, cfg, limite=a.limite)

    print(f"\nopportunites lues        : {r['lignes']}  (illisibles : {r['illisibles']})")
    print(f"dont au moins deux points : {r['deux_points']}  "
          f"— les seules ou un surplus peut apparaitre")
    print(f"fenetre historique        : {r['jours']:.1f} jours")
    print(f"canaux traduits           : {', '.join(r['canaux'])}")

    print(f"\n{'canal':<12}{'ancien':>10}{'nouveau':>10}{'surplus':>10}{'  /jour':>10}")
    print("  " + "─" * 50)
    tot_a = tot_n = 0
    for nom in r["canaux"]:
        av, ap = r["ancien"][nom], r["nouveau"][nom]
        tot_a += av
        tot_n += ap
        par_jour = (ap - av) / r["jours"] if r["jours"] else 0.0
        print(f"{nom:<12}{av:>10}{ap:>10}{ap - av:>+10}{par_jour:>10.2f}")
    print("  " + "─" * 50)
    print(f"{'TOTAL':<12}{tot_a:>10}{tot_n:>10}{tot_n - tot_a:>+10}"
          f"{(tot_n - tot_a) / r['jours'] if r['jours'] else 0:>10.2f}")

    pct = 100 * (tot_n - tot_a) / tot_a if tot_a else 0.0
    print(f"\nsurplus relatif : {pct:+.1f} %")
    print(f"detections routees vers PLUSIEURS canaux a la fois : {r['multi']}")
    if r["motifs"]:
        print("\nrepartition du surplus :")
        for motif, n in r["motifs"].most_common():
            print(f"   {n:>6}  {motif}")

    chg_b = sum(1 for s in r["surplus"] if s["bande_changee"])
    chg_t = sum(1 for s in r["surplus"] if s["tranche_changee"])
    print(f"\n   dont bande d'EV changee      : {chg_b}")
    print(f"   dont tranche de cote changee : {chg_t}")

    if r["surplus"]:
        print(f"\nexemples ({min(a.exemples, len(r['surplus']))} sur {len(r['surplus'])}) :")
        for s in r["surplus"][:a.exemples]:
            print(f"\n   {s['cle']}  [{s['sport']}]")
            print(f"      {s['depuis']}  ->  {s['vers']}")
            print(f"      surplus sur {s['canal']} — {s['motif']}")
    else:
        print("\nAucun surplus detectable sur cet historique.")

    print("\n⚠️ PLANCHER, pas estimation centree : la base ne garde que le")
    print("   premier et le dernier etat d'une opportunite. Un aller-retour")
    print("   entre deux bandes est invisible ici et le vrai surplus est")
    print("   superieur. Rien n'a ete ecrit, rien n'a ete envoye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
