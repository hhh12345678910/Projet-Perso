#!/usr/bin/env python3
"""Ce que l'envoi des alertes coûte au cycle — et la preuve, ou la réfutation.

L'HYPOTHÈSE, ÉNONCÉE AVANT DE MESURER
-------------------------------------
`TelegramAlerter._send` réserve un créneau puis **dort dans le fil du scan**
(`_time.sleep(wait)`) pour respecter `min_send_interval_s` (3,2 s par défaut,
la limite de Telegram étant d'environ 20 messages/minute et par groupe). Rien
n'est asynchrone : le cycle est à l'arrêt pendant ces pauses. Un cycle qui
délivre N messages porte donc N × 3,2 s de sommeil pur.

Si c'est vrai, `dRest` — la part du bloc des value bets qu'aucune sous-phase
(`find`, `insVB`, `feat`, `seed`, `suivi`) ne revendique, c'est-à-dire
essentiellement `send_alerts` — doit être PROPORTIONNELLE au nombre d'alertes
délivrées, avec un coefficient d'au moins `min_send_interval_s`.

CE QUI LA TUERAIT
-----------------
Des cycles SANS aucune alerte délivrée mais avec un `dRest` important. La
sonde calcule cette moyenne-là en premier et l'affiche en premier : c'est le
témoin, et il a le droit de dire non. Un coefficient très inférieur à
`min_send_interval_s` la tuerait aussi — le temps viendrait d'ailleurs.

⚠️ `→ N value bet alert(s) sent` COMPTE DES PARIS, PAS DES MESSAGES. Un pari
routé vers deux canaux vaut deux messages, donc deux pauses. Le coefficient
mesuré est un temps PAR PARI DÉLIVRÉ ; divisé par `min_send_interval_s`, il
donne le nombre moyen de canaux par pari. C'est une information, pas une
anomalie.

Usage :
    .venv/bin/python -m scripts.alert_cost
    .venv/bin/python -m scripts.alert_cost --derniers 20
    .venv/bin/python -m scripts.alert_cost --sport soccer --intervalle 3.2
"""
from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from pathlib import Path

from scripts.book_latency import RE_PAIRE, RE_PHASES

LOG = os.getenv("CYCLE_LOG", "valuebet.log")

RE_CYCLE = re.compile(r"══ CYCLE (\d+) —")
# ⚠️ IMPORTÉES, PAS RECOPIÉES. Les deux sondes lisent la MÊME ligne de la
# production ; en avoir deux copies, c'est laisser l'une se réparer sans
# l'autre. C'est exactement ce qui a caché le bug de `dRest` lu « est ».
RE_ENVOI = re.compile(r"^\[(\w+)\]\s+→ (\d+) value bet alert\(s\) sent\s*$")
RE_BETS = re.compile(r"^\[(\w+)\]\s+value bets: (\d+) total\s*$")
RE_429 = re.compile(r"Telegram 429 \[chat=([^\]]*)\] — pause (\d+)s")
RE_COOLDOWN = re.compile(r"Telegram cooldown (\d+)s restant \[chat=([^\]]*)\]")
RE_NON200 = re.compile(r"Telegram non-200 \((\d+)\)")


def _moy(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pente_origine(xy: list[tuple[float, float]]) -> float | None:
    """La pente d'une droite PASSANT PAR L'ORIGINE : zéro alerte doit coûter
    zéro seconde. Une régression libre absorberait le coût de l'envoi dans une
    ordonnée à l'origine et ferait disparaître exactement ce qu'on cherche."""
    sxx = sum(x * x for x, _ in xy)
    if sxx <= 0:
        return None
    return sum(x * y for x, y in xy) / sxx


def _pearson(xy: list[tuple[float, float]]) -> float | None:
    n = len(xy)
    if n < 3:
        return None
    mx, my = _moy([x for x, _ in xy]), _moy([y for _, y in xy])
    num = sum((x - mx) * (y - my) for x, y in xy)
    dx = sum((x - mx) ** 2 for x, _ in xy) ** 0.5
    dy = sum((y - my) ** 2 for _, y in xy) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def lire(chemin: Path, sport: str | None) -> tuple[list, list, dict]:
    """Rend (lots, ordre_lots, incidents). Un lot = (clé, sport, dRest, tot,
    alertes, paris)."""
    dRest: dict[tuple, float] = {}
    tot: dict[tuple, float] = {}
    alertes: dict[tuple, int] = {}
    paris: dict[tuple, int] = {}
    incidents = {"429": 0, "cooldown": 0, "non200": 0, "pause_max": 0}
    ordre_lots: list[tuple] = []
    serie, dernier_num = 0, None
    cle = (0, 0)
    for ligne in chemin.read_text(errors="replace").splitlines():
        m = RE_CYCLE.search(ligne)
        if m:
            num = int(m.group(1))
            # Même règle que book_latency : le compteur repart à 1 après un
            # redémarrage, indexer par numéro nu mélangerait deux régimes.
            if dernier_num is None or num <= dernier_num:
                serie += 1
            dernier_num = num
            cle = (serie, num)
            if cle not in ordre_lots:
                ordre_lots.append(cle)
            continue
        m = RE_429.search(ligne)
        if m:
            incidents["429"] += 1
            incidents["pause_max"] = max(incidents["pause_max"], int(m.group(2)))
            continue
        if RE_COOLDOWN.search(ligne):
            incidents["cooldown"] += 1
            continue
        if RE_NON200.search(ligne):
            incidents["non200"] += 1
            continue
        nu = ligne.strip()
        m = RE_PHASES.match(nu)
        if m:
            sp = m.group(1)
            if sport and sp != sport:
                continue
            ph = {k: float(v) for k, v in RE_PAIRE.findall(m.group(2))}
            # Une phase absente de la ligne est sous 0,05 s, donc nulle — pas
            # inconnue. `ligne_phases` les omet exprès.
            dRest[(cle, sp)] = ph.get("dRest", 0.0)
            tot[(cle, sp)] = float(m.group(3))
            continue
        m = RE_ENVOI.match(nu)
        if m:
            sp = m.group(1)
            if not (sport and sp != sport):
                alertes[(cle, sp)] = int(m.group(2))
            continue
        m = RE_BETS.match(nu)
        if m:
            sp = m.group(1)
            if not (sport and sp != sport):
                paris[(cle, sp)] = int(m.group(2))

    lots = []
    for k in sorted(dRest, key=lambda k: (ordre_lots.index(k[0]), k[1])):
        # ⚠️ ZÉRO PAR DÉFAUT, ET C'EST JUSTIFIÉ : la ligne `→ N alert(s) sent`
        # n'est imprimée QUE si N > 0. Son absence veut dire zéro envoi, pas
        # zéro mesure. Les paris, eux, sont toujours imprimés.
        lots.append((k[0], k[1], dRest[k], tot.get(k, 0.0),
                     alertes.get(k, 0), paris.get(k, 0)))
    return lots, ordre_lots, incidents


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=LOG, help=f"Journal à lire (défaut : {LOG}).")
    ap.add_argument("--sport", default=None, help="Un seul sport.")
    ap.add_argument("--derniers", type=int, default=0, metavar="N",
                    help="Ne garder que les N derniers cycles (ordre du fichier).")
    ap.add_argument("--intervalle", type=float,
                    default=float(os.getenv("TELEGRAM_MIN_SEND_INTERVAL", "3.2")),
                    help="min_send_interval_s attendu (défaut : la valeur de "
                         "l'environnement, sinon 3,2).")
    a = ap.parse_args()

    chemin = Path(a.log)
    if not chemin.exists():
        raise SystemExit(
            f"Journal introuvable : {chemin}\n"
            "La sortie du daemon va dans valuebet.log, PAS dans journalctl — "
            "c'est scan-daemon.sh qui redirige.")

    lots, ordre_lots, inc = lire(chemin, a.sport)
    if not lots:
        raise SystemExit(
            "Aucune ligne de phases dans ce journal.\n"
            "`dRest` est écrit depuis le 04/09/2026 : un journal antérieur, ou "
            "un daemon\npas encore redémarré sur cette version, n'en a pas.")

    if a.derniers:
        gardes = set(ordre_lots[-a.derniers:])
        lots = [l for l in lots if l[0] in gardes]
        if not lots:
            raise SystemExit(f"--derniers {a.derniers} ne garde aucun cycle mesuré.")

    print(f"\nCE QUE L'ENVOI DES ALERTES COÛTE AU CYCLE — {chemin}")
    cycles = sorted({l[0] for l in lots}, key=ordre_lots.index)
    print(f"{len(lots)} lots (cycle × sport), cycles {cycles[0][1]} à "
          f"{cycles[-1][1]} sur {len({c[0] for c in cycles})} série(s)"
          + (f", sport {a.sport}" if a.sport else ""))
    print(f"Intervalle attendu entre deux messages : {a.intervalle:.2f} s")

    # ── LE TÉMOIN, EN PREMIER ────────────────────────────────────────
    # Il a le droit de dire non, et on le lit avant tout le reste.
    sans = [l for l in lots if l[4] == 0]
    avec = [l for l in lots if l[4] > 0]
    print("\n── TÉMOIN : les cycles qui n'ont RIEN envoyé ──")
    if not sans:
        print("  Aucun. Tous les lots ont délivré au moins une alerte : le "
              "témoin est muet,\n  la proportionnalité ci-dessous est le seul "
              "argument.")
        moy_sans = None
    else:
        moy_sans = _moy([l[2] for l in sans])
        print(f"  {len(sans)} lot(s) sans alerte délivrée — dRest moyen "
              f"{moy_sans:6.1f} s")
        if avec:
            print(f"  {len(avec)} lot(s) avec alertes      — dRest moyen "
                  f"{_moy([l[2] for l in avec]):6.1f} s")
        else:
            print("  Aucun lot n'a délivré d'alerte.")

    # ── LA PROPORTIONNALITÉ ──────────────────────────────────────────
    xy = [(float(l[4]), l[2]) for l in lots]
    pente = _pente_origine(xy)
    r = _pearson(xy)
    print("\n── dRest EN FONCTION DU NOMBRE D'ALERTES DÉLIVRÉES ──")
    print(f"  pente (droite par l'origine) : "
          + (f"{pente:.2f} s par pari délivré" if pente is not None else "N/A"))
    print(f"  corrélation de Pearson       : "
          + (f"{r:+.3f}" if r is not None else "N/A (moins de 3 lots)"))
    if pente is not None and a.intervalle > 0:
        print(f"  soit {pente / a.intervalle:.2f} message(s) par pari délivré "
              f"— le nombre moyen de canaux touchés.")

    # ── LE VERDICT ───────────────────────────────────────────────────
    # ⚠️ SENS DU TEST. « Confirmée » exige les TROIS : un témoin muet ou
    # presque, une corrélation forte, et une pente au moins égale à
    # l'intervalle. Il manque n'importe laquelle et on ne conclut pas.
    print("\n── VERDICT ──")
    seuil_temoin = 2 * a.intervalle
    temoin_ok = moy_sans is None or moy_sans <= seuil_temoin
    correl_ok = r is not None and r >= 0.8
    pente_ok = pente is not None and pente >= a.intervalle * 0.8
    if temoin_ok and correl_ok and pente_ok:
        gagne = _moy([l[2] for l in lots])
        tot_moy = _moy([l[3] for l in lots])
        print("  HYPOTHÈSE CONFIRMÉE : le temps part dans les pauses de "
              "l'envoi Telegram.")
        print(f"  dRest moyen {gagne:.1f} s sur un cycle moyen de "
              f"{tot_moy:.1f} s, soit "
              + (f"{100 * gagne / tot_moy:.0f} %" if tot_moy else "N/A")
              + " du cycle.")
        print(f"  Sans l'envoi dans le fil du scan, le cycle vaudrait "
              f"{tot_moy - gagne:.1f} s.")
    else:
        print("  HYPOTHÈSE ÉCARTÉE — le temps de `dRest` ne vient pas (que) de "
              "l'envoi Telegram :")
        if not temoin_ok:
            print(f"    · des cycles SANS aucune alerte portent quand même "
                  f"{moy_sans:.1f} s de dRest\n      (au-dessus du seuil de "
                  f"{seuil_temoin:.1f} s) — quelque chose d'autre s'y cache ;")
        if not correl_ok:
            print("    · dRest ne suit pas le nombre d'alertes "
                  + (f"(r = {r:+.3f}, il faudrait ≥ +0,800)" if r is not None
                     else "(trop peu de lots pour le dire)") + " ;")
        if not pente_ok:
            print("    · la pente "
                  + (f"({pente:.2f} s/pari)" if pente is not None else "(N/A)")
                  + f" est sous {a.intervalle * 0.8:.2f} s — les pauses "
                    "n'expliquent pas ce volume.")

    # ── INCIDENTS ────────────────────────────────────────────────────
    print("\n── INCIDENTS TELEGRAM SUR TOUT LE JOURNAL ──")
    print(f"  429 (flood)      : {inc['429']}"
          + (f", pause la plus longue demandée {inc['pause_max']} s"
             if inc["429"] else ""))
    print(f"  envois reportés  : {inc['cooldown']} (cooldown actif)")
    print(f"  réponses non-200 : {inc['non200']}")
    if inc["429"] == 0 and inc["cooldown"] == 0:
        print("  Aucun 429 : ce n'est PAS une tempête de back-off, c'est le "
              "rythme nominal.")

    # ── LES PIRES LOTS ───────────────────────────────────────────────
    pires = sorted(lots, key=lambda l: -l[2])[:10]
    if pires and pires[0][2] > 0:
        print("\n── LES 10 LOTS LES PLUS CHERS ──")
        print("  cycle    sport      dRest      tot   alertes   paris   s/pari")
        for cle, sp, dr, tt, al, pa in pires:
            spp = f"{dr / al:6.2f}" if al else "     —"
            print(f"  {cle[1]:>5}  {sp:<10} {dr:6.1f} s {tt:6.1f} s "
                  f"{al:7}  {pa:6}  {spp}")

    print("\nLecture seule — aucune écriture, aucun réglage modifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
