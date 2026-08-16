#!/usr/bin/env python3
"""Rejuger les paris référencés Smarkets contre la clôture de PINNACLE.

Pourquoi c'est nécessaire
-------------------------
Un pari valorisé sur la référence de repli est aussi CLÔTURÉ sur elle : on
mesure donc Smarkets contre lui-même. Si son carnet est bruité à l'ouverture,
il l'est aussi à la clôture — et battre une ligne bruitée ne prouve rien. La
CLV, fiable contre Pinnacle, perd ici sa propriété essentielle.

Mesuré le 16/08 sur 28 paris : moyenne +27,98 % mais médiane +8,95 %, et
57,1 % de positifs contre 70,9 % pour Pinnacle. L'écart moyenne/médiane trahit
quelques valeurs extrêmes ; 57,1 % sur 28 tirages est à 0,76 σ du hasard. Rien
ne se conclut de là.

L'idée
------
Pinnacle ne price pas ces marchés AU MOMENT DE LA DÉTECTION — c'est bien pour
ça qu'il y a eu repli. Mais beaucoup de matchs ignorés à J-2 finissent pricés à
l'approche du coup d'envoi. Quand cette ligne existe, elle donne une règle
fiable pour juger après coup si la détection valait quelque chose.

On ne modifie rien : on relit `quotes`, on dévigue la clôture Pinnacle, et on
compare les deux verdicts côte à côte.

⚠️ Ne porte que sur les paris dont les cotes sont encore en base. La rétention
est à 2 jours : au-delà, la clôture Pinnacle est purgée et le pari sort de
l'échantillon sans être un échec.

Usage :
    .venv/bin/python scripts/crossclose.py             # smarkets
    .venv/bin/python scripts/crossclose.py magicbetting
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os                                                # noqa: E402

# La même valeur que la purge nocturne : au-delà, les cotes n'existent plus et
# leur absence ne dit rien de ce que Pinnacle pricait.
RETENTION_DAYS = float(os.getenv("PRUNE_DAYS", "2"))

from src.config import ScanConfig                        # noqa: E402
from src.devig import devig                              # noqa: E402
from src.storage import Storage                          # noqa: E402


def fair_from_group(rows, label, line, method: str) -> float | None:
    """La cote juste d'une issue, dévigée sur tout le marché.

    Dévigue le GROUPE et pas la seule issue qui nous intéresse : la marge ne
    s'enlève qu'en normalisant les issues concurrentes les unes contre les
    autres. Rend None si le marché est trop mince pour ça — une issue seule
    normaliserait à 100 % et rendrait une « juste » égale à la cote affichée,
    c'est-à-dire une CLV de zéro présentée comme une mesure."""
    group = [r for r in rows if (r["line"] if line is None else r["line"]) == line] or list(rows)
    if len(group) < 2:
        return None
    try:
        probs = devig([float(r["decimal_odd"]) for r in group], method=method)
    except Exception:                                               # noqa: BLE001
        return None
    for r, p in zip(group, probs):
        if r["outcome_label"] == label and p > 0:
            return 1.0 / p
    return None


def stats(v: list[float]) -> str:
    if not v:
        return "     0        —        —        —"
    m = mean(v)
    pos = 100 * sum(1 for x in v if x > 0) / len(v)
    if len(v) > 1:
        ec = (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5
        sig = m / (ec / len(v) ** 0.5) if ec else 0.0
    else:
        sig = 0.0
    return f"{len(v):>6} {m:>+8.2f}% {median(v):>+8.2f}% {pos:>7.1f}% {sig:>6.1f}"


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "smarkets"
    cfg = ScanConfig()
    storage = Storage(cfg.db_path)

    import sqlite3
    c = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    bets = list(c.execute("""
        SELECT v.id, v.event_key, v.market, v.outcome_label, v.line,
               v.odd_taken, v.ev_pct, s.fair_odd AS ref_close,
               e.start_time, e.home, e.away
        FROM value_bets v
        JOIN clv_snapshots s ON s.value_bet_id = v.id AND s.closing = 1
        LEFT JOIN events e ON e.event_key = v.event_key
        WHERE v.reference_book = ? AND s.fair_odd IS NOT NULL AND s.fair_odd > 0
        ORDER BY v.detected_at
    """, (ref,)))
    if not bets:
        print(f"Aucun pari clôturé référencé {ref}.")
        return 1

    sur_ref, sur_pin, apparies = [], [], []
    # ⚠️ Trois causes d'absence, et il ne faut SURTOUT pas les additionner.
    # « Pinnacle ne price jamais ce marché » condamne le repli : il serait
    # structurellement invérifiable. « Les cotes sont purgées » ne condamne
    # rien du tout — il suffit d'allonger la rétention et de réessayer. Les
    # confondre ferait conclure au pire sur une limite d'outillage.
    jamais_price = purge = sans_match = 0
    limite = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    for b in bets:
        clv_ref = (b["odd_taken"] / b["ref_close"] - 1) * 100
        sur_ref.append(clv_ref)

        start = b["start_time"]
        if not start:
            sans_match += 1
            continue
        try:
            ko = datetime.fromisoformat(start)
        except ValueError:
            sans_match += 1
            continue
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)

        # Fenêtre identique à celle de close-lines : la dernière capture avant
        # le coup d'envoi. Prendre plus tard attraperait du live, dont les prix
        # ne sont pas comparables à une détection prématch.
        rows = storage.closing_group(b["event_key"], b["market"], b["line"],
                                     ko + timedelta(minutes=1), book="pinnacle")
        fair_pin = fair_from_group(rows, b["outcome_label"], b["line"],
                                   cfg.devig_method) if rows else None
        if not fair_pin:
            # Le coup d'envoi est-il antérieur à la rétention ? Alors les cotes
            # de ce match ont été supprimées, et leur absence ne dit rien de ce
            # que Pinnacle pricait.
            if ko < limite:
                purge += 1
            else:
                jamais_price += 1
            continue
        clv_pin = (b["odd_taken"] / fair_pin - 1) * 100
        sur_pin.append(clv_pin)
        apparies.append((b, clv_ref, clv_pin, fair_pin))

    print(f"=== {len(bets)} paris référencés {ref}, clôturés ===")
    print(f"    {len(apparies):>3} comparables à Pinnacle")
    print(f"    {jamais_price:>3} sans ligne Pinnacle même près du coup d'envoi "
          f"— marché réellement ignoré")
    print(f"    {purge:>3} coup d'envoi antérieur à {RETENTION_DAYS} j : cotes "
          f"purgées, indécidable")
    print(f"    {sans_match:>3} sans match rattaché dans `events`\n")
    print(f"{'règle de mesure':22} {'n':>6} {'moyenne':>9} {'médiane':>9} "
          f"{'positifs':>8} {'σ':>6}")
    print("-" * 66)
    print(f"{'clôture ' + ref:22} {stats(sur_ref)}")
    if apparies:
        print(f"{'  dont appariés':22} {stats([x[1] for x in apparies])}")
        print(f"{'clôture Pinnacle':22} {stats(sur_pin)}")

    if len(apparies) < 30:
        print(f"\n⚠️ {len(apparies)} paris comparables : trop peu pour conclure, "
              f"dans un sens comme dans l'autre.")
        if purge > jamais_price:
            print("   La cause dominante est la PURGE, pas l'absence de ligne "
                  "Pinnacle.\n   C'est une limite d'outillage, pas un verdict "
                  "sur la référence :\n   allonger PRUNE_DAYS après le VACUUM "
                  "et refaire tourner.")
        elif jamais_price:
            print("   La cause dominante est que Pinnacle ne price JAMAIS ces "
                  "marchés,\n   même près du coup d'envoi. Le repli est alors "
                  "structurellement\n   invérifiable — aucune règle fiable "
                  "n'existe pour le juger.")
    if not apparies:
        return 0

    print(f"\n{'écart':22} {mean([x[2] for x in apparies]) - mean([x[1] for x in apparies]):+.2f} "
          f"points de CLV (Pinnacle − {ref})")
    print("\nLecture : si la CLV chute nettement en passant à la règle "
          f"Pinnacle,\nla « juste » de {ref} était trop courte — elle "
          "fabriquait de la value\nqui n'existait pas. Si les deux se "
          "rejoignent, le repli tient.\n")

    print("--- détail, écart le plus défavorable d'abord ---")
    for b, cref, cpin, fpin in sorted(apparies, key=lambda x: x[2] - x[1])[:15]:
        ligne = f" {b['line']:+g}" if b["line"] is not None else ""
        print(f"{cref:+8.2f}% -> {cpin:+8.2f}%   {b['home']} — {b['away']}")
        print(f"     {b['market']}/{b['outcome_label']}{ligne} @ "
              f"{b['odd_taken']:.2f}   juste {ref} {b['ref_close']:.2f} "
              f"vs Pinnacle {fpin:.2f}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
