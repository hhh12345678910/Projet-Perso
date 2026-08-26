"""Observatoire LIVE — collecte longue, sans toucher au moteur. §PHASE 5

    # collecte, plusieurs heures, Telegram actif
    .venv/bin/python -m scripts.live_observatoire --heures 6 --telegram

    # rapport sur ce qui a ete collecte (n'importe quand, meme pendant)
    .venv/bin/python -m scripts.live_observatoire --rapport

    # export CSV pour analyse a la main
    .venv/bin/python -m scripts.live_observatoire --export /tmp/live

LE MOTEUR N'EST PAS TOUCHE. Ce fichier APPELLE `evaluer` et
`send_live_observation` exactement comme le lanceur : aucun filtre nouveau,
aucune EV supprimee, aucun seuil deplace. Il ajoute UNIQUEMENT de
l'enregistrement.

OU VONT LES DONNEES. Dans SA PROPRE base (`--journal`, par defaut
`data/live_observation.db`), jamais dans `data/valuebet.db`. Le moteur
continue de ne rien ecrire nulle part.

CE QU'IL MESURE, ET QUI N'EXISTAIT PAS :

  1. LE PRIX FIGE. A chaque changement de `feed_score` sur une selection
     AsianOdds, on note si la COTE a bouge. Le cas Avro — Leek Town du 26/08
     (score 2:3 -> 2:4, fair 1.76 inchangee, `observed_at` rafraichi) suggere
     qu'AsianOdds met son score a jour sans repricer. Une ligne ne prouve
     rien ; quelques heures trancheront.

  2. LA SUITE DE L'ALERTE. Chaque selection alertee est suivie 5 minutes :
     on enregistre la cote Unibet a chaque fois qu'elle CHANGE, et le moment
     ou elle DISPARAIT. Une cote qui s'effondre ou qui est retiree juste
     apres l'alerte est une erreur que le book a corrigee ; une cote qui
     tient etait peut-etre un vrai prix. C'est la seule facon de distinguer
     les deux sans parier.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.live_value import (AGE_MAX_FAIR_SEC, AGE_MAX_PRENEUR_SEC,
                            SEUIL_EV_PCT, Memoire, Statut, evaluer)
from src.models import Book
from src.storage import Storage
from src.unibet_live import PERIODE_SEC, UnibetLive, apparier

#: Duree de suivi d'une selection apres son alerte.
SUIVI_SEC = 300.0
#: Periode du balayage « score change / prix fige ». AsianOdds reprice toutes
#: les ~10 s en mediane : balayer chaque seconde couterait une lecture
#: complete de market_state pour rien.
BALAYAGE_SEC = 5.0
TRANCHES = (10, 20, 50, 100)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alertes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detecte_a TEXT NOT NULL, event_key TEXT NOT NULL,
  home TEXT, away TEXT, market TEXT, line REAL, outcome TEXT, book TEXT,
  cote_preneur REAL, fair_cote REAL, fair_prob REAL,
  ev_pct REAL, kelly_pct REAL,
  statut TEXT, motif TEXT, motif_reemission TEXT,
  age_fair_sec REAL, age_preneur_sec REAL, delai_calcul_sec REAL,
  intervalle_maj_sec REAL, minute_ecoulee REAL,
  feed_score TEXT, partiel INTEGER, issues_manquantes TEXT,
  overround_preneur REAL, fair_inverse INTEGER,
  source_event_id_fair TEXT, source_event_id_preneur TEXT,
  envoyee INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alertes_t ON alertes(detecte_a);

-- Un releve par CHANGEMENT de la cote suivie, plus le point de depart et la
-- disparition. `cote_unibet` NULL = la selection n'est plus offerte.
CREATE TABLE IF NOT EXISTS suivi (
  alerte_id INTEGER NOT NULL, t_sec REAL NOT NULL, releve_a TEXT NOT NULL,
  cote_unibet REAL, fair_cote REAL, feed_score TEXT, ev_pct REAL,
  FOREIGN KEY (alerte_id) REFERENCES alertes(id)
);
CREATE INDEX IF NOT EXISTS ix_suivi_a ON suivi(alerte_id, t_sec);

-- Le score AsianOdds a change : la cote a-t-elle bouge ?
CREATE TABLE IF NOT EXISTS score_prix (
  vu_a TEXT NOT NULL, event_key TEXT, market TEXT, line REAL, outcome TEXT,
  score_avant TEXT, score_apres TEXT, odd_avant REAL, odd_apres REAL,
  observed_avant TEXT, observed_apres TEXT,
  prix_fige INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS passages (
  t TEXT NOT NULL, matchs_unibet INTEGER, apparies INTEGER,
  fair_groupes INTEGER, quotes INTEGER, occasions INTEGER, erreur TEXT
);
"""


def _ouvrir(chemin: str) -> sqlite3.Connection:
    c = sqlite3.connect(chemin)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def _cle(o) -> tuple:
    return (o.event_key, o.market.value, o.line, o.outcome)


def _enregistrer(cx, o, envoyee: bool) -> int:
    cur = cx.execute(
        "INSERT INTO alertes (detecte_a, event_key, home, away, market, line,"
        " outcome, book, cote_preneur, fair_cote, fair_prob, ev_pct,"
        " kelly_pct, statut, motif, motif_reemission, age_fair_sec,"
        " age_preneur_sec, delai_calcul_sec, intervalle_maj_sec,"
        " minute_ecoulee, feed_score, partiel, issues_manquantes,"
        " overround_preneur, fair_inverse, source_event_id_fair,"
        " source_event_id_preneur, envoyee)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o.detecte_a.isoformat(), o.event_key, o.home, o.away,
         o.market.value, o.line, o.outcome, o.book.value, o.cote_preneur,
         o.fair_cote, o.fair_prob, o.ev_pct, o.kelly_pct, o.statut.value,
         o.motif, o.motif_reemission, o.age_fair_sec, o.age_preneur_sec,
         o.delai_calcul_sec, o.intervalle_maj_sec, o.minute_ecoulee,
         o.feed_score, int(o.partiel), ",".join(o.issues_manquantes),
         o.overround_preneur, int(o.fair_inverse), o.source_event_id_fair,
         o.source_event_id_preneur, int(envoyee)))
    return cur.lastrowid


def collecter(a) -> int:
    cfg = envoi = None
    if a.telegram:
        from src.alerter import TelegramConfig, send_live_observation
        cfg = TelegramConfig.from_env()
        if cfg is None:
            print("[obs] Telegram non configuré (TELEGRAM_BOT_TOKEN / "
                  "TELEGRAM_CHAT_ID absents). `set -a && . ./.env && set +a`")
            return 2
        envoi = send_live_observation
        print(f"[obs] Telegram : canal LIVE = "
              f"{cfg.live_surebet_chat_id or 'NON DÉFINI'}")

    cx = _ouvrir(a.journal)
    storage, live, memoire = Storage(a.db), UnibetLive(a.sport), Memoire()
    #: cle AsianOdds -> (feed_score, odd, observed_at) du dernier balayage
    vu_ao: dict = {}
    #: alerte_id -> (cle, expire_a, derniere_cote)
    suivis: dict = {}
    total = Counter()
    debut = datetime.now(timezone.utc)
    fin = time.monotonic() + a.heures * 3600
    prochain_balayage = 0.0
    prochain_battement = time.monotonic() + 600

    print(f"[obs] démarrage {debut.isoformat()} — {a.heures:g} h, "
          f"sondage {a.periode:g} s, EV > {a.ev:g} % SANS plafond")
    print(f"[obs] journal : {a.journal}   (la base de production n'est jamais "
          f"écrite)")
    try:
        while time.monotonic() < fin:
            c = live.sonder()
            maintenant = datetime.now(timezone.utc)
            if c.erreur:
                total["erreurs"] += 1
                cx.execute("INSERT INTO passages (t, erreur) VALUES (?, ?)",
                           (maintenant.isoformat(), c.erreur))
                cx.commit()
                time.sleep(a.periode)
                continue
            total["passages"] += 1
            app = apparier(live.instantane, storage, maintenant, a.sport)
            maintenant = datetime.now(timezone.utc)
            an = evaluer(app.quotes, storage, maintenant, memoire=memoire,
                         preneur_pris_a=live.instantane.pris_a,
                         seuil_ev=a.ev, age_max_fair=a.age_fair,
                         age_max_preneur=a.age_preneur)

            # ── index des cotes du moment, pour le suivi ──
            cotes = {(q.event_key, q.market.value,
                      None if q.outcome.line is None
                      else round(float(q.outcome.line), 3),
                      q.outcome.label): q.decimal_odd for q in app.quotes}

            # ── alertes ──
            partants = [o for o in an.nouvelles
                        if a.envoyer_rejets
                        or not o.statut.value.startswith("REJET_")]
            for o in an.nouvelles:
                # `envoyee` doit dire ce qui est PARTI, pas ce qui aurait pu
                # partir : sans --telegram, rien ne part, et marquer 1 ferait
                # lire un envoi qui n'a pas eu lieu.
                part = o in partants and envoi is not None
                aid = _enregistrer(cx, o, part)
                suivis[aid] = [_cle(o), maintenant + timedelta(seconds=SUIVI_SEC),
                               o.cote_preneur, maintenant]
                cx.execute(
                    "INSERT INTO suivi (alerte_id, t_sec, releve_a,"
                    " cote_unibet, fair_cote, feed_score, ev_pct)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (aid, 0.0, maintenant.isoformat(), o.cote_preneur,
                     o.fair_cote, o.feed_score, o.ev_pct))
                total[f"statut:{o.statut.value}"] += 1
                for t in TRANCHES:
                    if o.ev_pct >= t:
                        total[f"ev>={t}"] += 1
                print(f"[obs] #{aid} {o.ev_pct:+.1f}% kelly {o.kelly_pct:.2f}% "
                      f"{o.home}-{o.away} {o.market.value} {o.outcome} "
                      f"@{o.cote_preneur:.2f} fair {o.fair_cote:.2f} "
                      f"score {o.feed_score or 'N/A'} age {o.age_fair_sec:.0f}s"
                      f" [{o.statut.value}]{' ENVOYÉE' if part else ''}")
            if envoi is not None and partants:
                total["envoyees"] += envoi(partants, cfg)

            # ── suivi des alertes deja posees ──
            for aid in list(suivis):
                cle, expire, derniere, pose = suivis[aid]
                cote = cotes.get(cle)
                ecoule = (maintenant - pose).total_seconds()
                if cote != derniere:
                    cx.execute(
                        "INSERT INTO suivi (alerte_id, t_sec, releve_a,"
                        " cote_unibet, fair_cote, feed_score, ev_pct)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (aid, ecoule, maintenant.isoformat(), cote,
                         None, None, None))
                    suivis[aid][2] = cote
                if maintenant >= expire:
                    cx.execute(
                        "INSERT INTO suivi (alerte_id, t_sec, releve_a,"
                        " cote_unibet, fair_cote, feed_score, ev_pct)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (aid, ecoule, maintenant.isoformat(), cote,
                         None, None, None))
                    del suivis[aid]

            # ── balayage « score change / prix fige » ──
            if time.monotonic() >= prochain_balayage:
                prochain_balayage = time.monotonic() + BALAYAGE_SEC
                for r in storage.market_state(live_only=True):
                    if r["book"] != Book.ASIANODDS.value:
                        continue
                    k = (r["event_key"], r["market"], r["line"],
                         r["outcome_label"])
                    neuf = (r["feed_score"], r["odd"], r["observed_at"])
                    ancien = vu_ao.get(k)
                    vu_ao[k] = neuf
                    if ancien is None or ancien[0] == neuf[0]:
                        continue
                    fige = int(abs((ancien[1] or 0) - (neuf[1] or 0)) < 1e-9)
                    total["score_change"] += 1
                    total["prix_fige"] += fige
                    cx.execute(
                        "INSERT INTO score_prix (vu_a, event_key, market,"
                        " line, outcome, score_avant, score_apres, odd_avant,"
                        " odd_apres, observed_avant, observed_apres,"
                        " prix_fige) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (maintenant.isoformat(), k[0], k[1], k[2], k[3],
                         ancien[0], neuf[0], ancien[1], neuf[1],
                         ancien[2], neuf[2], fige))

            cx.execute(
                "INSERT INTO passages (t, matchs_unibet, apparies,"
                " fair_groupes, quotes, occasions) VALUES (?,?,?,?,?,?)",
                (maintenant.isoformat(), c.matchs, app.matchs_apparies,
                 an.groupes_fair, an.quotes_analysees, len(an.nouvelles)))
            cx.commit()

            if time.monotonic() >= prochain_battement:
                prochain_battement = time.monotonic() + 600
                h = (maintenant - debut).total_seconds() / 3600
                print(f"[obs] {h:.1f} h — {total['passages']} passages, "
                      f"{total['erreurs']} erreurs, "
                      f"{sum(v for k, v in total.items() if k.startswith('statut:'))}"
                      f" alertes, {total['envoyees']} envoyées, "
                      f"{total['score_change']} changements de score dont "
                      f"{total['prix_fige']} à prix figé")
            if time.monotonic() + a.periode > fin:
                break
            time.sleep(a.periode)
    except KeyboardInterrupt:
        print("\n[obs] interrompu — le journal est conservé")
    finally:
        live.close()
        cx.commit()
        cx.close()
    print(f"\n[obs] terminé. Rapport : "
          f".venv/bin/python -m scripts.live_observatoire --rapport "
          f"--journal {a.journal}")
    return 0


def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def _n(x, u="", f="{:.1f}"):
    return "N/A" if x is None else f.format(x) + u


def rapport(a) -> int:
    cx = _ouvrir(a.journal)
    p = cx.execute("SELECT COUNT(*) n, MIN(t) d, MAX(t) f,"
                   " SUM(erreur IS NOT NULL) e FROM passages").fetchone()
    if not p["n"]:
        print("journal vide — rien n'a encore été collecté")
        return 1
    d, f = datetime.fromisoformat(p["d"]), datetime.fromisoformat(p["f"])
    print(f"\n{'═' * 74}\nOBSERVATOIRE LIVE — {a.journal}\n{'═' * 74}")
    print(f"  du {d:%d/%m %H:%M} au {f:%d/%m %H:%M} UTC "
          f"({(f - d).total_seconds() / 3600:.1f} h)")
    print(f"  {p['n']} passages, {p['e']} en erreur")

    # ── 1. le prix fige ──
    s = cx.execute("SELECT COUNT(*) n, SUM(prix_fige) fige FROM score_prix"
                   ).fetchone()
    print(f"\n{'─' * 74}\n1. LE SCORE ASIANODDS CHANGE — LE PRIX SUIT-IL ?\n"
          f"{'─' * 74}")
    if not s["n"]:
        print("  aucun changement de score observé")
    else:
        fige = s["fige"] or 0
        print(f"  changements de score observés    {s['n']}")
        print(f"  dont la cote n'a PAS bougé       {fige}"
              f"   ({100 * fige / s['n']:.1f} %)")
        print(f"  dont la cote a bougé             {s['n'] - fige}")
        bouge = cx.execute(
            "SELECT AVG(ABS(odd_apres - odd_avant) / odd_avant) v"
            " FROM score_prix WHERE prix_fige = 0 AND odd_avant > 0"
        ).fetchone()["v"]
        if bouge:
            print(f"  quand elle bouge, de            {100 * bouge:.1f} % en moyenne")
        print("\n  exemples de prix figés :")
        for r in cx.execute(
                "SELECT * FROM score_prix WHERE prix_fige = 1"
                " ORDER BY vu_a DESC LIMIT 8"):
            ligne = "" if r["line"] is None else f" {r['line']:g}"
            print(f"    {r['event_key'][:38]:<38} {r['market']}{ligne} "
                  f"{r['outcome']:<5} {r['score_avant']}->{r['score_apres']} "
                  f"cote {r['odd_avant']:.2f} inchangée")

    # ── 2. les alertes ──
    print(f"\n{'─' * 74}\n2. ALERTES\n{'─' * 74}")
    tot = cx.execute("SELECT COUNT(*) n, SUM(envoyee) e FROM alertes"
                     ).fetchone()
    print(f"  enregistrées {tot['n']}   envoyées sur Telegram {tot['e'] or 0}")
    for t in TRANCHES:
        r = cx.execute("SELECT COUNT(*) n, SUM(envoyee) e FROM alertes"
                       " WHERE ev_pct >= ?", (t,)).fetchone()
        print(f"    EV ≥ {t:>3} %   {r['n']:>5}   (envoyées {r['e'] or 0})")
    for r in cx.execute("SELECT statut, COUNT(*) n FROM alertes"
                        " GROUP BY statut ORDER BY n DESC"):
        print(f"    {r['statut']:<26} {r['n']}")

    print(f"\n  fraîcheur au moment de l'alerte :")
    for t in TRANCHES:
        xs = [r["age_fair_sec"] for r in cx.execute(
            "SELECT age_fair_sec FROM alertes WHERE ev_pct >= ?", (t,))]
        ys = [r["age_preneur_sec"] for r in cx.execute(
            "SELECT age_preneur_sec FROM alertes WHERE ev_pct >= ?", (t,))]
        print(f"    EV ≥ {t:>3} %   fair médiane {_n(_med(xs), ' s'):>10}"
              f"   cote médiane {_n(_med(ys), ' s'):>10}")

    print(f"\n  score AsianOdds au moment de l'alerte :")
    for r in cx.execute(
            "SELECT COALESCE(feed_score,'N/A') s, COUNT(*) n FROM alertes"
            " GROUP BY s ORDER BY n DESC LIMIT 8"):
        print(f"    {r['s']:<8} {r['n']}")

    # ── 3. la suite de l'alerte ──
    print(f"\n{'─' * 74}\n3. QUE DEVIENT LA COTE UNIBET DANS LES 5 MINUTES ?\n"
          f"{'─' * 74}")
    print("  C'est ici que se joue la question « vraie EV ou erreur "
          "corrigée ».")
    print("  Une cote retirée ou effondrée juste après = le book s'est repris.")
    lignes = []
    for al in cx.execute("SELECT id, ev_pct, cote_preneur, home, away, market,"
                         " outcome, statut FROM alertes"):
        suite = cx.execute(
            "SELECT t_sec, cote_unibet FROM suivi WHERE alerte_id = ?"
            " ORDER BY t_sec", (al["id"],)).fetchall()
        if len(suite) < 2:
            continue
        fin_ = suite[-1]
        lignes.append((al, fin_["cote_unibet"], fin_["t_sec"], len(suite)))
    if not lignes:
        print("\n  aucune alerte n'a encore été suivie jusqu'au bout "
              "(il faut 5 min par alerte)")
    else:
        disparues = [x for x in lignes if x[1] is None]
        baissees = [x for x in lignes if x[1] is not None
                    and x[1] < x[0]["cote_preneur"] * 0.95]
        tenues = [x for x in lignes if x[1] is not None
                  and x[0]["cote_preneur"] * 0.95 <= x[1]
                  <= x[0]["cote_preneur"] * 1.05]
        montees = [x for x in lignes if x[1] is not None
                   and x[1] > x[0]["cote_preneur"] * 1.05]
        n = len(lignes)
        print(f"\n  {n} alerte(s) suivie(s) jusqu'à T+5 min :")
        print(f"    cote RETIRÉE par Unibet      {len(disparues):>5}"
              f"   ({100 * len(disparues) / n:.0f} %)")
        print(f"    cote BAISSÉE de plus de 5 %  {len(baissees):>5}"
              f"   ({100 * len(baissees) / n:.0f} %)")
        print(f"    cote TENUE (± 5 %)           {len(tenues):>5}"
              f"   ({100 * len(tenues) / n:.0f} %)")
        print(f"    cote MONTÉE de plus de 5 %   {len(montees):>5}"
              f"   ({100 * len(montees) / n:.0f} %)")
        print(f"\n  par tranche d'EV — part des cotes retirées ou effondrées :")
        for t in TRANCHES:
            g = [x for x in lignes if x[0]["ev_pct"] >= t]
            if not g:
                continue
            mauvais = sum(1 for x in g if x[1] is None
                          or x[1] < x[0]["cote_preneur"] * 0.95)
            print(f"    EV ≥ {t:>3} %   {mauvais}/{len(g)}"
                  f"   ({100 * mauvais / len(g):.0f} %)")
        print(f"\n  les 10 dernières, en détail :")
        for al, finale, t_sec, n_pts in lignes[-10:]:
            etat = ("RETIRÉE" if finale is None
                    else f"{finale:.2f} ({100 * (finale / al['cote_preneur'] - 1):+.0f} %)")
            print(f"    #{al['id']:<5} {al['ev_pct']:>+8.1f}%  "
                  f"{al['home'][:14]:<14}-{al['away'][:14]:<14} "
                  f"{al['market']:<7} {al['outcome']:<5} "
                  f"@{al['cote_preneur']:>6.2f} → {etat}")
    print(f"\n{'═' * 74}")
    cx.close()
    return 0


def export(a) -> int:
    import csv
    cx = _ouvrir(a.journal)
    base = Path(a.export)
    n = 0
    for table in ("alertes", "suivi", "score_prix"):
        rows = cx.execute(f"SELECT * FROM {table}").fetchall()
        chemin = base.with_name(f"{base.name}_{table}.csv")
        with open(chemin, "w", newline="", encoding="utf-8") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=rows[0].keys())
                w.writeheader()
                w.writerows(dict(r) for r in rows)
        print(f"  {chemin}  ({len(rows)} lignes)")
        n += len(rows)
    cx.close()
    return 0 if n else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--heures", type=float, default=6.0)
    p.add_argument("--periode", type=float, default=PERIODE_SEC)
    p.add_argument("--sport", default="soccer")
    p.add_argument("--db", default="data/valuebet.db",
                   help="base de PRODUCTION, lue seulement")
    p.add_argument("--journal", default="data/live_observation.db",
                   help="base d'observation, la seule qui soit écrite")
    p.add_argument("--ev", type=float, default=SEUIL_EV_PCT)
    p.add_argument("--age-fair", type=float, default=AGE_MAX_FAIR_SEC)
    p.add_argument("--age-preneur", type=float, default=AGE_MAX_PRENEUR_SEC)
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--envoyer-rejets", action="store_true")
    p.add_argument("--rapport", action="store_true",
                   help="lire le journal et rendre le rapport, sans collecter")
    p.add_argument("--export", metavar="PREFIXE",
                   help="exporter les tables en CSV et sortir")
    a = p.parse_args()
    if a.export:
        return export(a)
    if a.rapport:
        return rapport(a)
    return collecter(a)


if __name__ == "__main__":
    raise SystemExit(main())
