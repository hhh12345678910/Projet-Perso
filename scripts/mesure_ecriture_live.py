"""Run d'écriture CONTRÔLÉ du collecteur AsianOdds. §PHASE 4

Écrit réellement dans `market_state` pendant une durée bornée, avec le daemon
prématch qui tourne en parallèle, et rend le rapport avant/après :
lignes, taille, UPSERT, transactions, erreurs SQLite, latence, impact
prématch, integrity_check, échantillons.

    read -rp  'Identifiant AsianOdds : ' AO_USER && export AO_USER
    read -rsp 'Mot de passe AsianOdds : ' AO_PASS && export AO_PASS && echo
    .venv/bin/python -m scripts.mesure_ecriture_live --minutes 30

CE SCRIPT N'INSTALLE RIEN. Il ne touche ni à systemd, ni au daemon, ni au
prématch : il ouvre la même base en écriture le temps du run, puis rend la
main. Rien ne subsiste après lui qu'un flux LIVE dans `market_state`.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime, timezone

from src.asianodds_live import SPORT_FOOTBALL, collect
from scripts.live_asianodds import INVITE_SAISIE, est_un_exemple
from src.models import Book
from src.storage import Storage

#: Les tables du prématch. Aucune ne doit bouger AUTREMENT que par ajout du
#: daemon : ce qui existait avant le run doit être bit pour bit identique
#: après. C'est la vérification qui compte le plus.
TABLES_PREMATCH = ("events", "quotes", "value_bets", "clv_snapshots")


def _un(c, sql, args=()):
    r = c.execute(sql, args).fetchone()
    return r[0] if r else None


def empreinte_prematch(c, t0: str) -> dict:
    """Compte + empreinte des lignes ANTÉRIEURES au run, table par table.

    Se limiter au compte ne prouverait rien : une ligne modifiée en place ne
    change pas le total. On hache donc le contenu, en excluant ce que le
    daemon ajoute PENDANT le run — sa croissance est normale, sa réécriture
    ne le serait pas.
    """
    out = {}
    for t in TABLES_PREMATCH:
        colonnes = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        if not colonnes:
            continue
        horodatage = next((x for x in ("fetched_at", "detected_at",
                                       "snapshot_at", "start_time")
                           if x in colonnes), None)
        sql = f"SELECT * FROM {t}"
        args: tuple = ()
        if horodatage:
            sql += f" WHERE {horodatage} < ?"
            args = (t0,)
        sql += " ORDER BY rowid"
        h = hashlib.sha256()
        n = 0
        for ligne in c.execute(sql, args):
            h.update(repr(tuple(ligne)).encode())
            n += 1
        out[t] = (n, h.hexdigest()[:16], horodatage)
    return out


def tailles(chemin: str) -> dict:
    out = {}
    for suffixe in ("", "-wal", "-shm"):
        p = chemin + suffixe
        out[suffixe or "db"] = os.path.getsize(p) if os.path.exists(p) else 0
    return out


def octets(n: float) -> str:
    """Une décimale dès le kilo-octet : arrondir 1,5 Ko à « 2 Ko » ferait
    disparaître la moitié d'une croissance qu'on cherche justement à voir."""
    unite = "o"
    for u in ("o", "Ko", "Mo", "Go"):
        unite = u
        if abs(n) < 1024 or u == "Go":
            break
        n /= 1024.0
    if unite == "o":
        return f"{n:,.0f} o".replace(",", " ")
    return f"{n:,.1f} {unite}".replace(",", " ")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=30.0)
    p.add_argument("--db", default="data/valuebet.db")
    p.add_argument("--sport", type=int, default=SPORT_FOOTBALL)
    a = p.parse_args()

    user, pwd = os.environ.get("AO_USER"), os.environ.get("AO_PASS")
    if not user or not pwd or est_un_exemple(user) or est_un_exemple(pwd):
        print(f"ERREUR : AO_USER / AO_PASS manquants ou d'exemple.\n"
              f"{INVITE_SAISIE}", file=sys.stderr)
        return 2

    db = Storage(a.db)
    t0 = datetime.now(timezone.utc)
    t0_iso = t0.isoformat()

    with db._conn() as c:
        ms_avant = _un(c, "SELECT COUNT(*) FROM market_state")
        ms_ao_avant = _un(c, "SELECT COUNT(*) FROM market_state WHERE book = ?",
                          (Book.ASIANODDS.value,))
        prematch_avant = empreinte_prematch(c, t0_iso)
        journal = _un(c, "PRAGMA journal_mode")
    taille_avant = tailles(a.db)

    print(f"═══ AVANT — {t0_iso} ═══")
    print(f"  journal_mode        : {journal}")
    print(f"  market_state        : {ms_avant:,} lignes "
          f"(dont asianodds : {ms_ao_avant:,})".replace(",", " "))
    for t, (n, h, col) in prematch_avant.items():
        print(f"  {t:<20}: {n:,} lignes antérieures, empreinte {h}"
              .replace(",", " "))
    print(f"  taille              : db {octets(taille_avant['db'])} | "
          f"wal {octets(taille_avant['-wal'])}")
    print(f"\n═══ COLLECTE — {a.minutes:.0f} min, ÉCRITURE RÉELLE ═══", flush=True)

    depart = time.monotonic()
    stats = collect(db, user, pwd, duration_sec=a.minutes * 60,
                    sport=a.sport, dry_run=False)
    duree = time.monotonic() - depart

    t1 = datetime.now(timezone.utc)
    with db._conn() as c:
        ms_apres = _un(c, "SELECT COUNT(*) FROM market_state")
        ms_ao_apres = _un(c, "SELECT COUNT(*) FROM market_state WHERE book = ?",
                          (Book.ASIANODDS.value,))
        prematch_apres = empreinte_prematch(c, t0_iso)
        complet = _un(c, "PRAGMA integrity_check")
        cles = _un(c, "PRAGMA foreign_key_check") or "ok"
        journal_apres = _un(c, "PRAGMA journal_mode")
        # Ce que le daemon a AJOUTÉ pendant le run : preuve qu'il a continué
        # de tourner, et non qu'il s'est tu sous le verrou.
        quotes_pendant = _un(
            c, "SELECT COUNT(*) FROM quotes WHERE fetched_at >= ?", (t0_iso,))
        prematch_ecrases = _un(
            c, "SELECT COUNT(*) FROM market_state "
               "WHERE book != ? AND observed_at IS NOT NULL",
            (Book.ASIANODDS.value,))
        # Filtre sur `fetched_at` — l'heure a laquelle NOUS avons ecrit — et
        # non sur `observed_at`, qui est l'heure de la SOURCE : on veut les
        # lignes produites par ce run, pas celles que la source date d'apres
        # son propre horodatage.
        exemples = c.execute(
            "SELECT event_key, market, outcome_label, line, odd, league, "
            "       observed_at, home_score, away_score, feed_score, igm "
            "FROM market_state WHERE book = ? AND fetched_at >= ? "
            "ORDER BY fetched_at DESC LIMIT 8",
            (Book.ASIANODDS.value, t0_iso)).fetchall()
        incomplets = _un(
            c, "SELECT COUNT(*) FROM market_state WHERE book = ? "
               "AND (observed_at IS NULL OR feed_score IS NULL "
               "     OR home_score IS NULL OR igm IS NULL)",
            (Book.ASIANODDS.value,))
    taille_apres = tailles(a.db)

    print(f"\n═══ APRÈS — {t1.isoformat()} ═══")
    print(f"[ao] {stats.resume()}")
    print(f"[ao] {stats.couverture()}")

    print(f"\n─── 1. lignes market_state ───")
    print(f"  total     : {ms_avant:,} → {ms_apres:,} "
          f"({ms_apres - ms_avant:+,})".replace(",", " "))
    print(f"  asianodds : {ms_ao_avant:,} → {ms_ao_apres:,} "
          f"({ms_ao_apres - ms_ao_avant:+,})".replace(",", " "))

    print(f"\n─── 2. taille de la base ───")
    for k in ("db", "-wal", "-shm"):
        print(f"  {k:<5}: {octets(taille_avant[k])} → {octets(taille_apres[k])} "
              f"({octets(taille_apres[k] - taille_avant[k])})")
    croissance = (taille_apres["db"] - taille_avant["db"]) / max(duree / 3600, 1e-9)
    print(f"  rythme : {octets(croissance)}/h (base seule, WAL exclu)")

    print(f"\n─── 3-4-6. écriture ───")
    print(f"  {stats.ecriture_resume()}")
    if stats.transactions:
        print(f"  cadence : {stats.transactions / (duree / 60):.1f} "
              f"transactions/min, "
              f"{stats.ecrits / stats.transactions:.0f} lignes/transaction")
        print(f"  part du temps passée à écrire : "
              f"{100 * sum(stats.ecritures_ms) / 1000 / duree:.2f} %")

    print(f"\n─── 5. erreurs SQLite ───")
    print(f"  database is locked réessayés : {stats.sqlite_busy}")
    print(f"  lots définitivement perdus   : {stats.sqlite_echecs}")

    print(f"\n─── 7. impact sur le prématch ───")
    print(f"  journal_mode : {journal} → {journal_apres}")
    print(f"  quotes ajoutées par le daemon pendant le run : {quotes_pendant:,}"
          .replace(",", " "))
    if not quotes_pendant:
        print("  ⚠ AUCUNE quote ajoutée : le daemon n'a peut-être pas tourné, "
              "ou son cycle est plus long que le run.")
    intact = True
    for t, (n, h, col) in prematch_avant.items():
        n2, h2, _ = prematch_apres.get(t, (None, None, None))
        ok = (n, h) == (n2, h2)
        intact &= ok
        print(f"  {t:<15}: {'INTACT' if ok else 'MODIFIÉ'} "
              f"({n} → {n2} lignes antérieures, {h} → {h2})")
    print(f"  lignes prématch de market_state polluées par du contexte LIVE : "
          f"{prematch_ecrases}")

    print(f"\n─── 8. intégrité ───")
    print(f"  integrity_check    : {complet}")
    print(f"  foreign_key_check  : {cles}")

    print(f"\n─── 9. échantillons réels écrits ───")
    for r in exemples:
        ligne = "" if r["line"] is None else f" {r['line']:+g}"
        print(f"  {r['market']:<10} {r['outcome_label']:<6}{ligne:<7} "
              f"@ {r['odd']:<6} | {r['feed_score']} à {r['igm']}' | "
              f"{r['observed_at']} | {r['event_key']}")
    print(f"  lignes asianodds sans contexte LIVE complet : {incomplets}")

    propre = (intact and complet == "ok" and stats.sqlite_echecs == 0
              and incomplets == 0 and prematch_ecrases == 0)
    print(f"\n═══ VERDICT : {'PROPRE' if propre else 'À EXAMINER'} ═══")
    return 0 if propre else 1


if __name__ == "__main__":
    raise SystemExit(main())
