#!/usr/bin/env python3
"""P&L sur TOUTES les détections, pas seulement sur celles qu'on a cliquées.

`track-update` mesure les paris joués. C'est utile, mais c'est une population
choisie à la main, et le projet a mesuré quatre fois que cette sélection
n'apporte rien : jouées +10,73 % contre non jouées +9,87 %, t = +0,90. Ce
qu'on gagne en la retirant, c'est un effectif bien plus grand et l'absence de
tout biais — un pari qu'on a « oublié » de jouer parce qu'on dormait n'est pas
un pari qu'on a écarté.

⚠️ La mise est NOTIONNELLE et constante. Une détection non jouée n'a pas de
mise ; en imposer une identique à toutes est le seul choix honnête, et il rend
le ROI directement comparable d'un segment à l'autre. Ce n'est donc pas de
l'argent, c'est un rendement par unité risquée.

⚠️ Déduplication sur `équipes + jour + marché + pari`, jamais sur `event_key` :
au tennis Pinnacle révise son horaire par pas de 15 minutes et fabrique jusqu'à
onze clés pour un seul match (§17.8). Sans ça les effectifs sont gonflés et les
significativités avec.

⚠️ `--premium` lit la porte RÉELLE : les canaux configurés en base (§24) si
elle en contient, sinon les seuils de `TelegramConfig`. Il ne recopie plus
aucun nombre — c'est ce qui l'avait fait diverger trois fois de la production
(§22.9, §23.5, §24). La porte retenue est imprimée avant le tableau : lis-la
avant de lire les chiffres.

Usage :
    .venv/bin/python -m scripts.pnl_detections
    .venv/bin/python -m scripts.pnl_detections --premium
    .venv/bin/python -m scripts.pnl_detections --canal "Premium"
    .venv/bin/python -m scripts.pnl_detections --stake 25
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.clv import pnl as clv_pnl  # noqa: E402
from src.clv import settle as clv_settle  # noqa: E402


class _Bet:
    """Le minimum que `routing.canaux_pour` lit sur un pari."""
    __slots__ = ("ev_pct", "odd_taken", "book", "market")

    def __init__(self, row) -> None:
        self.ev_pct = float(row["ev_pct"])
        self.odd_taken = float(row["odd_taken"])
        self.book = row["book"]
        self.market = row["market"]


def _est_live(row) -> bool:
    """Détectée après le coup d'envoi = live — la définition du §9 (délai
    négatif). `value_bets` ne porte aucun drapeau : il n'existe pas d'autre
    source. Un horaire manquant compte comme PRÉMATCH, parce qu'écarter par
    défaut supprimerait des paris qu'on n'a jamais voulu couper — c'est
    l'asymétrie du §24.3."""
    debut, vu = row["start_time"], row["detected_at"]
    return bool(debut and vu and vu >= debut)


def _canaux_en_base(db_path: str):
    """Les canaux configurés, chargés par le MÊME chemin que l'alerter.

    Liste vide = aucune configuration persistée, donc le routage historique
    s'applique — exactement la sémantique de `alerter._load_channels`."""
    class _Source:
        @staticmethod
        def load_channel_rows():
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            try:
                return (
                    con.execute("SELECT * FROM channels "
                                "ORDER BY priorite, nom, id").fetchall(),
                    con.execute("SELECT * FROM channel_rules "
                                "ORDER BY channel_id, id").fetchall(),
                    con.execute("SELECT * FROM channel_rule_values").fetchall(),
                )
            finally:
                con.close()

    try:
        from src.channels import charger
        return charger(_Source, print_fn=lambda s: print(f"  ⚠️ {s}"))
    except Exception:
        return []


def porte_de_canal(db_path: str, nom_voulu: str | None):
    """Rend « ce pari part-il sur ce canal ? », et la description de la porte.

    ⚠️ Ce module RECOPIAIT les seuils (`cote 1,5-4 dès EV 8`, `4-6 dès 20`).
    Ils ont divergé de la production trois fois sans que rien ne le signale :
    l'exclusion du tennis en cote 4-6 (§22.9), la voie critique grosses cotes
    (§23.5), puis les canaux configurables (§24). Une analyse « premium »
    décrivait donc un flux qui n'existait plus. On lit désormais la même
    configuration que l'alerter, par la même fonction — c'est le §17.7 : une
    sonde qui recalcule autre chose que la production ment.
    """
    from src.routing import canaux_pour

    canaux = _canaux_en_base(db_path)

    if canaux:
        if nom_voulu:
            vises = [c for c in canaux if c.nom.lower() == nom_voulu.lower()]
        else:
            vises = [c for c in canaux if "premium" in c.nom.lower()]
        if not vises:
            dispo = ", ".join(sorted(c.nom for c in canaux)) or "aucun"
            raise SystemExit(
                f"Aucun canal ne correspond à « {nom_voulu or 'premium'} ». "
                f"Canaux configurés : {dispo}. Nomme-le avec --canal.")
        if len(vises) > 1:
            raise SystemExit(
                "Plusieurs canaux correspondent : "
                + ", ".join(sorted(c.nom for c in vises))
                + ". Nomme-en un seul avec --canal.")
        cible = vises[0]

        def predicat(row) -> bool:
            # On passe la liste ENTIÈRE : un canal exclusif de priorité
            # supérieure peut préempter le pari, et c'est le comportement de
            # production qu'on reproduit — pas la seule éligibilité.
            return cible in canaux_pour(
                _Bet(row), sport=row["sport"], league=row["league"],
                is_live=_est_live(row), canaux=canaux)

        return predicat, (f"canal « {cible.nom} » lu en base, "
                          f"{len(cible.regles)} règle(s)")

    # Aucun canal en base : la porte historique, LUE dans TelegramConfig.
    from src.alerter import TelegramConfig
    tg = TelegramConfig.from_env()
    if tg is None:
        # ⚠️ Ne PAS retomber sur `TelegramConfig(bot_token="", chat_id="")`.
        # Le constructeur nu rend les défauts du CODE : `premium_hi_sports_exclus`
        # y vaut `()`, donc l'exclusion du tennis (§22.9) disparaît en silence et
        # le tableau annonce un premium qui n'est pas le tien. Mesuré en écrivant
        # ce correctif : le tennis en cote 5 à 25 % d'EV repassait la porte.
        # Un ROI plausible et faux coûte plus qu'une erreur franche (§18.3).
        raise SystemExit(
            "Seuils du premium INCONNUS : aucun canal en base, et "
            "TelegramConfig.from_env() ne rend rien (TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID absents). Les défauts du code donneraient un "
            "chiffre plausible et faux. Lance la commande depuis la racine du "
            "projet, sur une installation portant son .env — ou nomme un canal "
            "configuré avec --canal.")
    exclus = tuple(tg.premium_hi_sports_exclus)

    def predicat(row) -> bool:
        if _est_live(row):
            return False              # le premium est prématch (alerter.py)
        ev, odd = float(row["ev_pct"]), float(row["odd_taken"])
        standard = (ev >= tg.min_premium_ev_pct
                    and tg.premium_min_odd <= odd <= tg.premium_max_odd)
        longue = (ev >= tg.premium_hi_min_ev
                  and tg.premium_hi_min_odd <= odd <= tg.premium_hi_max_odd
                  and (row["sport"] or "").lower() not in exclus)
        return standard or longue

    desc = (f"porte historique — standard EV ≥ {tg.min_premium_ev_pct:g} % "
            f"cote {tg.premium_min_odd:g}–{tg.premium_max_odd:g} ; "
            f"longue EV ≥ {tg.premium_hi_min_ev:g} % "
            f"cote {tg.premium_hi_min_odd:g}–{tg.premium_hi_max_odd:g}"
            + (f" ; sports exclus de la longue : {', '.join(exclus)}"
               if exclus else ""))
    return predicat, desc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true",
                    help="Ne garder que ce qui part RÉELLEMENT sur le canal "
                         "premium — règles lues en base, sinon .env.")
    ap.add_argument("--canal", default=None, metavar="NOM",
                    help="Mesurer un autre canal, par son nom exact "
                         "(implique --premium).")
    ap.add_argument("--stake", type=float, default=25.0,
                    help="Mise notionnelle par pari.")
    a = ap.parse_args()
    if a.canal:
        a.premium = True

    # Les sondes de `scripts/` ne passent pas par le callback Typer de
    # `src.main` : sans ça, la porte historique se calculerait sur les
    # DÉFAUTS du code au lieu des seuils de cette installation (§20.11).
    from src.config import load_env_file
    load_env_file()

    porte = None
    if a.premium:
        porte, desc = porte_de_canal(a.db, a.canal)
        print(f"\nPORTE PREMIUM — {desc}")

    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row
    rows = list(c.execute("""
        SELECT vb.event_key, vb.book, vb.market, vb.outcome_label, vb.line,
               vb.odd_taken, vb.ev_pct, vb.detected_at,
               e.sport AS sport, e.home AS home, e.away AS away,
               e.league AS league, e.start_time AS start_time,
               r.winner, r.home_score, r.away_score
        FROM value_bets vb
        JOIN results r ON r.event_key = vb.event_key
        LEFT JOIN events e ON e.event_key = vb.event_key
    """))
    if not rows:
        print("Aucune détection avec résultat. Lance `results-update`.")
        return 1

    # Déduplication : une opportunité = équipes + jour + marché + pari.
    best: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        if porte is not None and not porte(r):
            continue
        key = ((r["home"] or "").lower(), (r["away"] or "").lower(),
               (r["start_time"] or "")[:10], r["market"], r["outcome_label"],
               r["line"])
        prev = best.get(key)
        if prev is None or float(r["odd_taken"]) > float(prev["odd_taken"]):
            best[key] = r
    opp = list(best.values())

    def report(label: str, sub: list, warn: int = 50) -> None:
        w = l = v = 0
        total = 0.0
        staked = 0.0
        odds = []
        for r in sub:
            status = clv_settle(r["market"], r["outcome_label"], r["line"],
                                r["winner"], r["home_score"], r["away_score"])
            p = clv_pnl(status, float(r["odd_taken"]), a.stake)
            if p is None:            # résultat insuffisant pour ce marché
                continue
            staked += a.stake
            total += p
            odds.append(float(r["odd_taken"]))
            if status == "won":
                w += 1
            elif status == "lost":
                l += 1
            else:
                v += 1
        n = w + l + v
        if not n:
            print(f"  {label:22} —")
            return
        roi = 100 * total / staked if staked else 0.0
        wr = 100 * w / (w + l) if (w + l) else 0.0
        # Écart-type du ROI : de quoi savoir si le chiffre veut dire quelque chose.
        per = [clv_pnl(clv_settle(r["market"], r["outcome_label"], r["line"],
                                  r["winner"], r["home_score"], r["away_score"]),
                       float(r["odd_taken"]), a.stake) for r in sub]
        per = [x for x in per if x is not None]
        sd = st.stdev(per) if len(per) > 1 else 0.0
        sigma = (total / (sd * math.sqrt(len(per)))) if sd > 0 else float("nan")
        flag = " ⚠️" if n < warn else ""
        print(f"  {label:22} n={n:5}  ROI {roi:+7.2f}%  {sigma:5.1f}σ  "
              f"gagnés {wr:5.1f}%  cote moy {st.mean(odds):.2f}  "
              f"P&L {total:+9.0f}€{flag}")

    scope = "canal premium" if a.premium else "toutes détections"
    print(f"\nP&L NOTIONNEL — {scope}, mise constante de {a.stake:.0f} €")
    print(f"{len(opp)} opportunités dédupliquées, sur {len(rows)} lignes\n")

    report("TOTAL", opp)
    print()

    # ⚠️ LA découpe qui teste la qualité de la référence.
    #
    # Un devig biaisé aux cotes hautes rend une « ligne juste » trop généreuse
    # pour les outsiders : ces paris affichent alors de l'EV et de la CLV sans
    # que l'argent suive. La signature est nette — edge positif sur les cotes
    # basses, négatif sur les hautes. Si le ROI décroît régulièrement quand la
    # cote monte, ce n'est pas la malchance, c'est la méthode de devig.
    print("  par tranche de cote  ⚠️ décroissance régulière = devig suspect")
    bands = [("1.0-1.8", 1.0, 1.8), ("1.8-2.3", 1.8, 2.3), ("2.3-3.0", 2.3, 3.0),
             ("3.0-4.0", 3.0, 4.0), ("4.0-6.0", 4.0, 6.0), ("> 6.0", 6.0, 1e9)]
    for lab, lo, hi in bands:
        report(f"    cote {lab}",
               [r for r in opp if lo <= float(r["odd_taken"]) < hi], warn=30)
    print()

    for field, title in (("sport", "par sport"), ("market", "par marché"),
                         ("book", "par book")):
        by = defaultdict(list)
        for r in opp:
            by[r[field] or "?"].append(r)
        print(f"  {title}")
        for k, sub in sorted(by.items(), key=lambda kv: -len(kv[1])):
            report(f"    {k}", sub, warn=30)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
