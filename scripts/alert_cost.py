#!/usr/bin/env python3
"""Ce que l'envoi des alertes coûte au cycle — et la preuve, ou la réfutation.

L'HYPOTHÈSE, ÉNONCÉE AVANT DE MESURER
-------------------------------------
`TelegramAlerter._send` réserve un créneau puis **dort dans le fil du scan**
(`_time.sleep(wait)`) pour respecter `min_send_interval_s` (3,2 s par défaut,
la limite de Telegram étant d'environ 20 messages/minute et par groupe). Rien
n'est asynchrone : le cycle est à l'arrêt pendant ces pauses. Un cycle qui
délivre N messages porte donc N × 3,2 s de sommeil pur.

LE MODÈLE EXACT — CE N'EST PAS UNE SOMME, C'EST UN MAXIMUM
----------------------------------------------------------
`_next_slot` est tenu PAR CHAT. Deux canaux ont deux budgets indépendants :
envoyer le même pari à deux canaux ne coûte pas deux pauses, la seconde part
pendant la pause de la première. Le coût d'un cycle est donc

    dRest ≈ min_send_interval_s × (messages sur le canal LE PLUS CHARGÉ)

et non × le nombre total de messages. C'est ce qui condamne le correctif
naïf : paralléliser les canaux ne rendrait rien, ils le sont déjà de fait.

⚠️ PREMIÈRE VERSION DE CETTE SONDE : FAUSSE. Elle exigeait une pente d'au
moins 0,8 × l'intervalle et aurait donc REJETÉ l'hypothèse sur le journal du
04/09 — 118,6 s pour 74 paris, soit 1,60 s par pari, sous le seuil. Le tort
était au seuil, pas aux données : avec un maximum par chat, la pente PAR PARI
DÉLIVRÉ est librement inférieure à l'intervalle dès qu'un pari sur deux est
dédoublonné sur le canal le plus chargé. Un test qui ne peut conclure que dans
un sens n'est pas un test.

CE QUI LA TUE VRAIMENT
----------------------
1. Des cycles SANS aucune alerte délivrée mais avec un `dRest` important. La
   sonde calcule cette moyenne-là EN PREMIER et l'affiche en premier : c'est
   le témoin, et il a le droit de dire non.
2. `dRest` qui ne suit pas le nombre d'alertes (corrélation faible).
3. Un `dRest` qui implique PLUS de messages sur un canal qu'il n'y a de paris
   délivrés. Un canal reçoit au plus un message par pari : au-delà de cette
   borne, les pauses ne peuvent pas expliquer le temps, quelque chose d'autre
   est dans `dRest`.

⚠️ `→ N value bet alert(s) sent` COMPTE DES PARIS, PAS DES MESSAGES. C'est la
raison d'être de la borne du point 3 : on n'observe pas les messages, mais on
connaît leur plafond.

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
RE_NON200 = re.compile(r"Telegram non-200 \((\d+)\) \[chat=([^\]]*)\]:?(.*)$")


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


def _quantification(durees: list[float], intervalle: float) -> tuple[float, float] | None:
    """`dRest` tombe-t-il sur des MULTIPLES ENTIERS de l'intervalle ?

    C'EST LE TEST QUI NE DÉPEND PAS DE LA LIVRAISON. Un envoi qui échoue dort
    quand même : `_send` réserve son créneau et fait sa pause AVANT le POST.
    Compter les alertes délivrées ne voit donc rien quand tout échoue — mais
    la signature arithmétique, elle, reste : n pauses de 3,2 s font 3,2 n
    secondes, plus une constante (le POST, les requêtes de dédoublonnage).

    Test de Rayleigh sur les restes modulo l'intervalle : R proche de 1 dit
    que tous les restes coïncident, donc que les durées sont espacées de
    multiples exacts. Un temps qui viendrait d'ailleurs les disperserait.

    Rend (R, p) ou None si l'échantillon est trop petit."""
    xs = [d for d in durees if d >= intervalle]
    n = len(xs)
    if n < 3 or intervalle <= 0:
        return None
    import cmath
    z = sum(cmath.exp(2j * cmath.pi * (d % intervalle) / intervalle) for d in xs)
    R = abs(z) / n
    return R, min(1.0, 2.718281828459045 ** (-n * R * R))


def lire(chemin: Path, sport: str | None) -> tuple[list, list, dict]:
    """Rend (lots, ordre_lots, incidents). Un lot = (clé, sport, dRest, tot,
    alertes, paris)."""
    dRest: dict[tuple, float] = {}
    tot: dict[tuple, float] = {}
    alertes: dict[tuple, int] = {}
    paris: dict[tuple, int] = {}
    incidents = {"429": 0, "cooldown": 0, "non200": 0, "pause_max": 0}
    # ⚠️ ATTRIBUÉS AU CYCLE. La première version comptait les incidents sur
    # TOUT le journal sans les rattacher : « 2086 non-200 » ne disait pas si
    # c'était hier ou maintenant, donc ne disait rien.
    par_cycle: dict[tuple, dict[str, int]] = defaultdict(
        lambda: {"429": 0, "cooldown": 0, "non200": 0})
    codes: dict[tuple, int] = defaultdict(int)
    exemples: list[str] = []
    ordre_lots: list[tuple] = []
    serie, dernier_num = 0, None
    cle = (0, 0)
    lignes_fichier = chemin.read_text(errors="replace").splitlines()
    for i, ligne in enumerate(lignes_fichier):
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
            par_cycle[cle]["429"] += 1
            incidents["pause_max"] = max(incidents["pause_max"], int(m.group(2)))
            continue
        if RE_COOLDOWN.search(ligne):
            incidents["cooldown"] += 1
            par_cycle[cle]["cooldown"] += 1
            continue
        m = RE_NON200.search(ligne)
        if m:
            incidents["non200"] += 1
            par_cycle[cle]["non200"] += 1
            codes[(m.group(1), m.group(2))] += 1
            # ⚠️ `rich` ENVELOPPE À 80 COLONNES HORS TERMINAL : le corps de la
            # réponse, qui est LA raison de l'échec, part sur les lignes
            # suivantes. Les recoller serait fragile — on les rend brutes.
            if len(exemples) < 3:
                exemples.append("\n".join(
                    lignes_fichier[i:i + 4]))
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
    incidents["par_cycle"] = dict(par_cycle)
    incidents["codes"] = dict(codes)
    incidents["exemples"] = exemples
    return lots, ordre_lots, incidents


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=LOG, help=f"Journal à lire (défaut : {LOG}).")
    ap.add_argument("--sport", default=None, help="Un seul sport.")
    ap.add_argument("--derniers", type=int, default=0, metavar="N",
                    help="Ne garder que les N derniers cycles (ordre du fichier).")
    ap.add_argument("--depuis-redemarrage", action="store_true",
                    dest="depuis_redemarrage",
                    help="Ne garder que la série en cours (depuis le dernier "
                         "redémarrage du daemon). C'est ce qu'il faut pour "
                         "mesurer un correctif : `--derniers N` enjambe le "
                         "redémarrage et mélange l'avant et l'après.")
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

    # ⚠️ TOUS LES LOTS, AVANT TOUT FILTRE. Le bloc AVANT/APRÈS compare deux
    # séries : le filtrer reviendrait à comparer une série à elle-même.
    tous = list(lots)

    if a.depuis_redemarrage:
        derniere = max(c[0] for c in ordre_lots) if ordre_lots else 0
        lots = [l for l in lots if l[0][0] == derniere]
        if not lots:
            raise SystemExit(
                "La série en cours ne contient aucun cycle mesuré.\n"
                "Le daemon vient peut-être de redémarrer : laisser tourner "
                "quelques minutes.")
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
    if len({c[0] for c in cycles}) > 1:
        print("\n⚠️ CETTE FENÊTRE ENJAMBE UN REDÉMARRAGE. Les moyennes "
              "ci-dessous mélangent\n   l'avant et l'après — c'est exactement "
              "ce qu'il ne faut pas pour juger un\n   correctif. Relancer avec "
              "`--depuis-redemarrage`, ou lire le bloc AVANT/APRÈS.")

    # ── LA QUANTIFICATION, EN PREMIER ────────────────────────────────
    # LE SEUL TEST QUI NE DÉPENDE PAS DE LA LIVRAISON. Un envoi qui échoue
    # dort quand même : `_send` réserve son créneau et fait sa pause AVANT le
    # POST. Compter les alertes délivrées ne voit donc RIEN quand tout échoue.
    # La signature arithmétique, elle, survit.
    q = _quantification([l[2] for l in lots], a.intervalle)
    print("\n── LA SIGNATURE : dRest tombe-t-il sur des multiples de "
          "l'intervalle ? ──")
    if q is None:
        print(f"  Moins de 3 lots au-dessus de {a.intervalle:.2f} s : "
              "indécidable.")
        quant_ok = False
    else:
        R, pval = q
        quant_ok = R >= 0.8
        print(f"  concentration des restes (Rayleigh) : R = {R:.3f}, "
              f"p ≈ {pval:.2g}")
        gros = sorted({round(l[2], 1) for l in lots if l[2] >= a.intervalle},
                      reverse=True)[:8]
        if gros:
            print("  les durées observées, décomposées :")
            for d in gros:
                n = round(d / a.intervalle)
                print(f"    {d:7.1f} s = {a.intervalle:.2f} × {n:3d} "
                      f"+ {d - a.intervalle * n:+.2f} s")
        print("  → " + ("des multiples ENTIERS : ce sont bien des pauses "
                        "comptées une par une."
                        if quant_ok else
                        "pas de multiples entiers : le temps ne vient pas "
                        "d'un compte de pauses."))

    # ── LE TÉMOIN ────────────────────────────────────────────────────
    # Il a le droit de dire non, et on le lit avant la corrélation.
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
        print(f"  soit {pente / a.intervalle:.2f} message(s) sur le canal le "
              f"plus chargé, par pari délivré.")
        print("  (< 1 est NORMAL : c'est la part des paris que le "
              "dédoublonnage écarte\n   sur ce canal-là. > 1 serait "
              "impossible — voir la borne ci-dessous.)")

    # ── LA BORNE ────────────────────────────────────────────────────
    # Un canal reçoit AU PLUS un message par pari délivré. Le nombre de
    # messages qu'implique dRest ne peut donc pas dépasser le nombre de paris.
    depassements = [l for l in avec
                    if l[2] / a.intervalle > l[4] * 1.15 + 1]
    print("\n── LA BORNE : pas plus d'un message par pari et par canal ──")
    print(f"  {len(depassements)} lot(s) sur {len(avec)} impliquent PLUS de "
          f"messages que de paris délivrés.")

    # ── LE VERDICT ───────────────────────────────────────────────────
    # TROIS ISSUES, PAS DEUX. Entre « le temps vient des pauses et les envois
    # arrivent » et « le temps vient d'ailleurs », il y a le cas qui a
    # réellement mordu le 04/09 : LES PAUSES ONT LIEU ET LES ENVOIS ÉCHOUENT.
    # Une sonde binaire l'aurait classé « écarté » et aurait envoyé chercher
    # le temps là où il n'est pas.
    print("\n── VERDICT ──")
    seuil_temoin = 2 * a.intervalle
    temoin_ok = moy_sans is None or moy_sans <= seuil_temoin
    correl_ok = r is not None and r >= 0.8
    # ⚠️ UNE BORNE SUPÉRIEURE, PAS UN PLANCHER. La pente peut légitimement
    # descendre bien sous l'intervalle (dédoublonnage) ; ce qu'elle ne peut
    # pas, c'est impliquer plus de messages sur un canal que de paris.
    borne_ok = not avec or len(depassements) <= len(avec) * 0.1
    gagne = _moy([l[2] for l in lots])
    tot_moy = _moy([l[3] for l in lots])
    # ⚠️ SUR LES LOTS QUI ONT VRAIMENT FAIT DES PAUSES. Moyenner soccer à
    # 118 s avec tennis à 0,1 s annoncerait « 18 pauses par lot » là où soccer
    # en fait 37 — un chiffre qui n'est vrai nulle part.
    _paues = [l[2] for l in lots if l[2] >= a.intervalle]
    n_msg = (_moy(_paues) / a.intervalle) if (_paues and a.intervalle > 0) else 0.0
    delivres = sum(l[4] for l in lots)
    if quant_ok and inc["non200"] and delivres * 4 < n_msg * max(1, len(_paues)):
        print("  LES PAUSES ONT LIEU — ET LES ENVOIS ÉCHOUENT.")
        print(f"  dRest porte la signature de {n_msg:.0f} pause(s) par lot "
              f"sur les {len(_paues)} qui en font,\n  mais {delivres} "
              f"alerte(s) seulement ont été délivrées sur {len(lots)} lots, "
              f"et le journal\n  compte {inc['non200']} réponses non-200.")
        print("  `_send` réserve son créneau et DORT AVANT le POST : un envoi "
              "qui échoue\n  coûte exactement le même temps qu'un envoi qui "
              "réussit. Le cycle paie\n  le plein tarif pour des messages que "
              "personne ne reçoit.")
        print("  ⚠️ LA PANNE N'EST PAS LA LENTEUR, C'EST LE SILENCE. Voir le "
              "bloc INCIDENTS.")
    elif temoin_ok and correl_ok and borne_ok:
        print("  HYPOTHÈSE CONFIRMÉE : le temps part dans les pauses de "
              "l'envoi Telegram.")
        print(f"  dRest moyen {gagne:.1f} s sur un cycle moyen de "
              f"{tot_moy:.1f} s, soit "
              + (f"{100 * gagne / tot_moy:.0f} %" if tot_moy else "N/A")
              + " du cycle.")
        print(f"  Sans l'envoi dans le fil du scan, le cycle vaudrait "
              f"{tot_moy - gagne:.1f} s.")
        if a.intervalle > 0:
            print(f"  Soit {gagne / a.intervalle:.0f} message(s) par cycle sur "
                  f"le canal le plus chargé,\n  à {a.intervalle:.2f} s de "
                  f"sommeil chacun.")
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
        if not borne_ok:
            print(f"    · {len(depassements)} lot(s) portent un dRest qui "
                  f"impliquerait plus de messages\n      sur un canal qu'il "
                  f"n'y a de paris délivrés — impossible pour des pauses "
                  f"seules.")

    # ── INCIDENTS ────────────────────────────────────────────────────
    print("\n── INCIDENTS TELEGRAM ──")
    gardes = {l[0] for l in lots}
    recents = {"429": 0, "cooldown": 0, "non200": 0}
    for c, d in inc["par_cycle"].items():
        if c in gardes:
            for k in recents:
                recents[k] += d[k]
    print(f"                     sur les cycles retenus / sur tout le journal")
    print(f"  429 (flood)      : {recents['429']:>7} / {inc['429']}"
          + (f"  (pause max demandée {inc['pause_max']} s)" if inc["429"] else ""))
    print(f"  envois reportés  : {recents['cooldown']:>7} / {inc['cooldown']}")
    print(f"  réponses non-200 : {recents['non200']:>7} / {inc['non200']}")
    if inc["429"] == 0 and inc["cooldown"] == 0 and not inc["non200"]:
        print("  Rien : ni 429 ni échec. Le rythme est nominal.")
    elif inc["429"] == 0 and inc["cooldown"] == 0:
        print("  Aucun 429 : ce n'est PAS une tempête de back-off. Mais les "
              "non-200 ci-dessus\n  sont des messages PERDUS — le cycle a payé "
              "leur pause pour rien.")
    if inc["codes"]:
        print("\n  par code et par canal :")
        for (code, chat), n in sorted(inc["codes"].items(), key=lambda kv: -kv[1]):
            print(f"    HTTP {code}  chat {chat:<18} {n:>6} fois")
    if inc["exemples"]:
        # ⚠️ BRUTES, SUR PLUSIEURS LIGNES. `rich` enveloppe à 80 colonnes hors
        # terminal : le corps de la réponse — LA raison de l'échec — part sur
        # les lignes suivantes. Les recoller serait fragile ; les rendre
        # telles quelles ne ment jamais.
        print("\n  les premières, telles quelles (le corps est enveloppé par "
              "rich à 80 col.) :")
        for ex in inc["exemples"]:
            print("    " + "\n    ".join(ex.splitlines()))
            print()

    # ── AVANT / APRÈS LE DERNIER REDÉMARRAGE ─────────────────────────
    # LA QUESTION QU'ON POSE VRAIMENT APRÈS UN CORRECTIF. Aucune moyenne
    # unique n'y répond : il faut deux fenêtres, et elles sont séparées par le
    # redémarrage, pas par un nombre de cycles.
    series = sorted({c[0] for c in ordre_lots})
    if len(series) >= 2:
        av_s, ap_s = series[-2], series[-1]
        av = [l for l in tous if l[0][0] == av_s]
        ap = [l for l in tous if l[0][0] == ap_s]
        print(f"\n── AVANT / APRÈS LE DERNIER REDÉMARRAGE ──")
        print(f"  série {av_s} = avant ({len(av)} lots), "
              f"série {ap_s} = après ({len(ap)} lots)")

        def _n200(ls):
            cs = {l[0] for l in ls}
            return sum(d["non200"] for c, d in inc["par_cycle"].items() if c in cs)

        rangs = [
            ("dRest moyen", lambda ls: _moy([l[2] for l in ls]), " s", False, 1),
            ("cycle moyen (tot)", lambda ls: _moy([l[3] for l in ls]), " s", False, 1),
            ("alertes délivrées", lambda ls: float(sum(l[4] for l in ls)), "", True, 0),
            ("réponses non-200", lambda ls: float(_n200(ls)), "", False, 0),
        ]
        print(f"  {'':<20} {'avant':>11} {'après':>11}   verdict")
        for nom, f, unite, haut_bon, dec in rangs:
            a_, b_ = f(av), f(ap)
            # ⚠️ LE SENS DÉPEND DE LA LIGNE. Moins de secondes est un progrès ;
            # moins d'alertes délivrées est une régression. Une flèche unique
            # pour les quatre dirait le contraire de la vérité une fois sur deux.
            if abs(b_ - a_) < 1e-9:
                v = "inchangé"
            elif (b_ > a_) == haut_bon:
                v = "MIEUX"
            else:
                v = "PIRE"
            print(f"  {nom:<20} {a_:>9.{dec}f}{unite:<2} "
                  f"{b_:>9.{dec}f}{unite:<2}   {v}")
        print("  ⚠️ Une série qui vient de démarrer a peu de cycles : laisser "
              "tourner\n     quelques minutes avant de conclure.")
    else:
        print("\n── AVANT / APRÈS LE DERNIER REDÉMARRAGE ──")
        print("  Une seule série dans ce journal : rien à comparer. Le bloc "
              "apparaîtra\n  au prochain redémarrage du daemon.")

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
