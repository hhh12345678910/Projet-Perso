"""Controle de production du routage par canaux — les huit points, une commande.

LECTURE SEULE. N'ecrit rien, n'envoie rien, ne supprime rien.

    .venv/bin/python -m scripts.controle_production
    .venv/bin/python -m scripts.controle_production --exemples 25

Enchaine les quatre controles de `verifier_canaux` (service, configuration,
routage, doublons) puis quatre mesures qui demandent du TRAFIC REEL et ne
pouvaient donc pas etre faites au redemarrage :

  5. une meme opportunite peut-elle atteindre plusieurs canaux ;
  6. le canal reellement utilise correspond-il a ce que le modele predit ;
  7. y a-t-il eu rafale ;
  8. quelques alertes concretes, avec leur canal.

Le controle 6 est le plus important des quatre. Les 32 960 cas compares
avant la bascule portaient sur la FONCTION de routage. Celui-ci porte sur
le DAEMON : ce qui est parti, ou c'est parti, et si le modele l'explique.

⚠️ Il ne peut pas etre exact au centieme. `notified_value_bets` garde l'EV
au moment de l'alerte, mais pas la cote ; `value_bets` garde la cote de la
PREMIERE detection et celle de la DERNIERE, jamais celle de l'instant de
l'alerte. Une alerte est donc declaree coherente si le canal utilise est
explique par l'une OU l'autre de ces deux cotes. Les cas restants sont
listes pour inspection, jamais comptes comme des fautes.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

import scripts.verifier_canaux as vc
from src.alerter import TelegramConfig
from src.channels import charger
from src.config import ScanConfig, load_env_file
from src.matcher import parse_event_key
from src.models import Book, MarketType, Outcome, ValueBet, is_half_time
from src.routing import canaux_pour
from src.storage import Storage


def _alertes_depuis(db: str, borne: str) -> list:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    lignes = con.execute(
        "SELECT n.event_key, n.book, n.market, n.outcome_label, n.line,"
        "       n.ev_pct, n.notified_at, n.chat_id,"
        "       v.odd_taken, v.last_odd, e.sport, e.league"
        "  FROM notified_value_bets n"
        "  LEFT JOIN value_bets v ON v.event_key = n.event_key"
        "       AND v.book = n.book AND v.market = n.market"
        "       AND v.outcome_label = n.outcome_label"
        "       AND (v.line IS n.line)"
        "  LEFT JOIN events e ON e.event_key = n.event_key"
        " WHERE n.notified_at >= ? ORDER BY n.notified_at", (borne,)).fetchall()
    con.close()
    return lignes


def _cibles(r, cote, canaux) -> list[str] | None:
    """Les canaux que le modele designe pour cette alerte, ou None si la
    ligne n'est pas reconstituable."""
    if cote is None:
        return None
    try:
        bet = ValueBet(
            event_key=r["event_key"], book=Book(r["book"]),
            market=MarketType(r["market"]),
            outcome=Outcome(r["outcome_label"], line=r["line"]),
            odd_taken=cote, fair_prob=0.5, fair_odd=1.0, ev_pct=r["ev_pct"],
            kelly_stake_pct=0.0, detected_at=datetime.now(timezone.utc),
            league=r["league"])
    except (ValueError, TypeError):
        return None
    quand = datetime.fromisoformat(r["notified_at"])
    p = parse_event_key(r["event_key"])
    live = False
    if p is not None:
        depart = p[0]
        if depart.tzinfo is None:
            depart = depart.replace(tzinfo=timezone.utc)
        live = depart <= quand
    return [c.chat_id for c in canaux_pour(bet, sport=r["sport"],
                                           league=r["league"], is_live=live,
                                           canaux=canaux)]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--exemples", type=int, default=15)
    p.add_argument("--depuis-minutes", type=int, default=60)
    a = p.parse_args()

    # ── 1 a 4 : le controle existant, tel quel ─────────────────────────
    import sys
    argv = sys.argv
    sys.argv = ["verifier_canaux", "--depuis-minutes", str(a.depuis_minutes)]
    try:
        code_base = vc.main()
    finally:
        sys.argv = argv

    load_env_file(".env")
    cfg = TelegramConfig.from_env()
    if cfg is None:
        return 2
    db = ScanConfig().db_path
    st = Storage(db)
    canaux = charger(st, print_fn=lambda _s: None)
    nom_de = {c.chat_id: c.nom for c in canaux}
    debut, origine = vc._depuis_le_redemarrage(a.depuis_minutes)
    lignes = _alertes_depuis(db, debut.isoformat())
    echecs: list[str] = []

    print("\n" + "═" * 60)
    print(f"TRAFIC REEL — {len(lignes)} alerte(s) depuis {origine}")
    if not lignes:
        # Surtout PAS 0 : « aucun doublon sur zero alerte » n'est pas une
        # validation, c'est une absence de mesure. Rendre GO ici ferait
        # exactement ce que ce script existe pour empecher.
        print("\n⚠️ Aucune alerte depuis le redemarrage : les controles 5 a 8")
        print("   ne mesurent RIEN. Relance quand le trafic sera arrive.")
        print("\nNO-GO — rien n'a pu etre verifie sur du trafic reel.")
        return 1

    # ── 5. multi-canal ─────────────────────────────────────────────────
    print("\n── 5. une opportunite sur plusieurs canaux ──")
    par_opp: dict[tuple, set] = defaultdict(set)
    for r in lignes:
        par_opp[(r["event_key"], r["book"], r["market"],
                 r["outcome_label"], r["line"])].add(r["chat_id"])
    multi = {k: v for k, v in par_opp.items() if len(v) > 1}
    print(f"   opportunites distinctes : {len(par_opp)}")
    print(f"   dont plusieurs canaux   : {len(multi)}")
    for k, v in list(multi.items())[:5]:
        print(f"      {k[0]} {k[1]} {k[2]} -> "
              f"{', '.join(sorted(nom_de.get(c, c or 'NULL') for c in v))}")
    if not multi:
        print("   ℹ 0 est le resultat ATTENDU avec cette configuration : le")
        print("     principal s'arrete a EV < 8, le premium commence a EV >= 8")
        print("     (disjoints), et le premium est exclusif au-dessus du")
        print("     critique. Aucun pari ne peut atteindre deux canaux tant")
        print("     qu'aucun canal chevauchant n'existe. Ce n'est donc PAS une")
        print("     preuve que le multi-canal fonctionne — seuls les tests le")
        print("     montrent (test_un_pari_dans_plusieurs_canaux).")

    # ── 6. le canal utilise vs le modele ───────────────────────────────
    print("\n── 6. le canal utilise correspond-il au modele ──")
    coherentes = inexplicables = irreconstituables = 0
    a_inspecter = []
    for r in lignes:
        if r["chat_id"] is None:
            continue                      # deja compte par le controle 4
        sets = [s for s in (_cibles(r, r["odd_taken"], canaux),
                            _cibles(r, r["last_odd"], canaux)) if s is not None]
        if not sets:
            irreconstituables += 1
            continue
        if any(r["chat_id"] in s for s in sets):
            coherentes += 1
        else:
            inexplicables += 1
            if len(a_inspecter) < 10:
                a_inspecter.append((r, sets))
    total6 = coherentes + inexplicables
    print(f"   coherentes        : {coherentes} / {total6}")
    print(f"   inexplicables     : {inexplicables}")
    print(f"   irreconstituables : {irreconstituables}  (pari absent de value_bets)")
    for r, sets in a_inspecter:
        attendus = sorted({nom_de.get(c, c) for s in sets for c in s}) or ["aucun"]
        print(f"      ⚠ {r['event_key']} {r['book']} {r['market']} "
              f"EV {r['ev_pct']:.1f} cote {r['odd_taken']}/{r['last_odd']}")
        print(f"        parti sur {nom_de.get(r['chat_id'], r['chat_id'])}, "
              f"modele : {', '.join(attendus)}")
    if inexplicables:
        echecs.append(f"{inexplicables} alerte(s) que le modele n'explique pas")

    # ── 7. rafale ──────────────────────────────────────────────────────
    print("\n── 7. rafale ──")
    # La reference est la DISTRIBUTION horaire d'AVANT la bascule, pas une
    # moyenne. Le trafic est naturellement en grappes — les matchs se
    # concentrent le soir — et comparer une heure chargee a la moyenne sur
    # 30 jours ferait crier au loup a chaque soiree normale. On compare donc
    # au maximum horaire deja observe : au-dessus, c'est du jamais-vu.
    con = sqlite3.connect(db)
    avant = [n for (n,) in con.execute(
        "SELECT COUNT(*) FROM notified_value_bets WHERE notified_at < ? "
        "GROUP BY substr(notified_at, 1, 13)", (debut.isoformat(),))]
    con.close()
    heures = Counter(r["notified_at"][:13] for r in lignes)
    minutes = Counter(r["notified_at"][:16] for r in lignes)
    pointe = heures.most_common(1)[0][1]
    duree_h = max((datetime.fromisoformat(lignes[-1]["notified_at"])
                   - datetime.fromisoformat(lignes[0]["notified_at"])
                   ).total_seconds() / 3600, 1 / 60)
    print(f"   duree observee : {duree_h:.2f} h   {len(lignes)} alertes "
          f"({len(lignes) / duree_h:.1f}/h)")
    print(f"   heure la plus chargee  : {pointe} alertes")
    print(f"   minute la plus chargee : {minutes.most_common(1)[0][1]} alertes")
    if len(avant) >= 24:
        tri = sorted(avant)
        p50 = tri[len(tri) // 2]
        p95 = tri[int(len(tri) * 0.95)]
        maxi = tri[-1]
        print(f"   reference AVANT la bascule, sur {len(avant)} heures pleines :")
        print(f"      mediane {p50}/h   p95 {p95}/h   maximum {maxi}/h")
        if pointe > maxi:
            echecs.append(f"heure de pointe a {pointe} alertes, au-dessus du "
                          f"maximum historique ({maxi}) — rafale possible")
        elif pointe > p95:
            print(f"   ⚠ pointe au-dessus du p95 mais sous le maximum connu — "
                  f"a surveiller, pas anormal")
        else:
            print("   ✓ pointe dans l'enveloppe historique")
    else:
        print(f"   ℹ pas assez d'historique ({len(avant)} heures) pour une "
              f"reference — controle non concluant")
    if duree_h < 0.5:
        print("   ℹ moins de 30 min observees : le taux n'est pas encore lisible")

    # ── 8. exemples ────────────────────────────────────────────────────
    print(f"\n── 8. les {min(a.exemples, len(lignes))} dernieres alertes ──")
    # `lignes[-0:]` rend TOUTE la liste, pas rien : la tranche negative doit
    # etre gardee pour ce cas.
    for r in (lignes[-a.exemples:] if a.exemples > 0 else []):
        canal = nom_de.get(r["chat_id"], r["chat_id"] or "NULL (global)")
        ligne = "" if r["line"] is None else f" {r['line']}"
        print(f"   {r['notified_at'][11:19]}  {canal:<10} "
              f"EV {r['ev_pct']:>5.1f}  cote {r['odd_taken'] or '?':>6}  "
              f"[{r['sport'] or 'sport ?'}]  {r['event_key']} "
              f"{r['book']} {r['market']}{ligne} {r['outcome_label']}")

    print("\n" + "═" * 60)
    if code_base or echecs:
        print("NO-GO :")
        if code_base:
            print("   • un ou plusieurs des controles 1 a 4 ont echoue (voir plus haut)")
        for e in echecs:
            print(f"   • {e}")
        return 1
    print("GO — les huit controles passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
