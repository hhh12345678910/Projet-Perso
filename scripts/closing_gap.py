#!/usr/bin/env python3
"""Pourquoi la clôture manque, et est-ce que ça dépend du DÉLAI de détection ?

POURQUOI CETTE SONDE
--------------------
`clv_roi_matrix --axe delai` a mesuré que le taux de capture de la clôture
DÉPEND du délai : sur le football, canal premium, 69,6 % des opportunités
détectées à moins de 24 h ont une ligne de clôture, contre 63,2 % au-delà.
Six points d'écart. Si les clôtures manquantes ne sont pas un échantillon
aléatoire de leur bande, une partie du déficit de CLV des bandes lointaines
est un artefact de mesure et non un fait sur le marché.

L'HYPOTHÈSE, ET SON MÉCANISME
-----------------------------
`Storage.closing_group` (storage.py:1825) cherche la cote de clôture avec
`WHERE event_key = ?` — une égalité EXACTE. Or `event_key` vaut
`YYYYMMDDHHMM::home__vs__away` (matcher.py:190) : **la minute du coup d'envoi
est dans la clé**. La clé d'un pari est figée à sa détection ; celle des cotes
capturées près du coup d'envoi porte l'horaire révisé. Si Pinnacle a déplacé
l'heure entre les deux, l'égalité échoue et la clôture est perdue.

Ce n'est pas une hypothèse en l'air : `Storage._event_key_like`
(storage.py:1293) existe précisément pour ça — « Pinnacle sometimes adjusts a
match's start time by a few minutes » — et `matcher.py:213` admet **trois
heures** de tolérance parce qu'au tennis « un match commence quand le
précédent sur le court se termine ». Mais cette clé tolérante n'est utilisée
que dans les six fonctions de déduplication d'alertes. **Jamais dans la
capture de clôture.**

Et la probabilité qu'un horaire ait bougé CROÎT avec le délai : un match vu à
200 h a bien plus d'occasions d'être replacé qu'un match vu à 1 h. C'est
exactement la forme de l'écart observé.

CE QUE LA SONDE MESURE — ET SON TÉMOIN
--------------------------------------
Pour chaque opportunité : a-t-elle une clôture, et sa (date + équipes)
existe-t-elle dans `events` sous PLUSIEURS clés — la signature d'un horaire
déplacé ? Puis la comparaison qui décide :

    part des paris à horaire déplacé PARMI CEUX SANS clôture
    part des paris à horaire déplacé PARMI CEUX AVEC clôture

**Sans ce témoin la mesure ne vaut rien** : si les deux parts sont égales, le
déplacement d'horaire n'explique rien, quel que soit son niveau absolu.

⚠️ `events` n'est jamais purgé (contrairement à `quotes`, deux jours), donc ce
test porte sur TOUT l'historique. Mais il ne voit un déplacement que si le
daemon a bien créé une ligne `events` pour la nouvelle clé — un déplacement
survenu alors que l'événement n'était plus scanné reste invisible ici, et la
sonde SOUS-ESTIME donc le phénomène.

Usage :
    .venv/bin/python -m scripts.closing_gap --premium
    .venv/bin/python -m scripts.closing_gap --premium --sport soccer
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
from src.config import load_env_file  # noqa: E402
from scripts.clv_roi_matrix import BANDES_DELAI, _bande_delai  # noqa: E402
from scripts.pnl_detections import porte_de_canal  # noqa: E402

ORDRE = ["< 0 (LIVE)"] + [l for l, _, _ in BANDES_DELAI] + ["? (sans horaire)"]


def _groupe_tolerant(cle: str) -> tuple:
    """(jour, équipes) — la clé tolérante de `_event_key_like`, en Python.

    `YYYYMMDDHHMM::home__vs__away` → `("YYYYMMDD", "home__vs__away")`. Deux
    clés du même groupe ne diffèrent que par la MINUTE du coup d'envoi : c'est
    la signature d'un horaire déplacé, pas de deux matchs différents (les
    mêmes équipes ne se rencontrent pas deux fois le même jour)."""
    if "::" not in cle:
        return (cle, "")
    tete, reste = cle.split("::", 1)
    return (tete[:8], reste)


def _prop_diff(a_ok: int, a_n: int, b_ok: int, b_n: int):
    """(part A, part B, écart A−B, z) — différence de deux proportions.

    ⚠️ L'ORDRE DES ARGUMENTS PORTE LE SENS DE L'HYPOTHÈSE. Les appelants
    passent le lot SANS clôture en premier, parce que l'hypothèse est « ceux
    qui n'ont pas de clôture sont PLUS souvent déplacés » : un écart positif la
    soutient. La première version passait le lot AVEC en premier tout en
    gardant les verdicts écrits pour l'autre sens — les deux branches de
    conclusion étaient donc INVERSÉES, et la sonde aurait annoncé « hypothèse
    écartée » précisément quand elle est confirmée. Trouvé en construisant un
    jeu d'essai où l'association est plantée."""
    if a_n == 0 or b_n == 0:
        return None, None, None, None
    pa, pb = a_ok / a_n, b_ok / b_n
    p = (a_ok + b_ok) / (a_n + b_n)
    v = p * (1 - p) * (1 / a_n + 1 / b_n)
    return pa, pb, pa - pb, ((pa - pb) / math.sqrt(v) if v > 0 else None)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true",
                    help="Restreindre à la porte RÉELLE du canal premium.")
    ap.add_argument("--canal", default=None, metavar="NOM",
                    help="Un autre canal, par son nom exact (implique --premium).")
    ap.add_argument("--sport", default=None,
                    help="Un seul sport (le football porte 74 %% du volume).")
    a = ap.parse_args()
    if a.canal:
        a.premium = True
    load_env_file()

    porte = None
    desc = "aucune — toutes les détections"
    if a.premium:
        porte, desc = porte_de_canal(a.db, a.canal)

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # `closing_lost` est une colonne de migration (storage.py:403) : une base
    # ancienne ne l'a pas. Refuser de tourner serait absurde, l'inventer serait
    # pire — on la lit si elle existe et on le DIT sinon.
    colonnes = {r[1] for r in con.execute("PRAGMA table_info(value_bets)")}
    a_perdu = "closing_lost" in colonnes
    champ_perdu = ("COALESCE(vb.closing_lost, 0)" if a_perdu else "0")

    # Tous les groupes tolérants de `events`, avec leur nombre de clés
    # DISTINCTES. C'est la seule table qui garde l'historique complet.
    par_groupe: dict[tuple, set] = defaultdict(set)
    for (cle,) in con.execute("SELECT event_key FROM events"):
        par_groupe[_groupe_tolerant(cle)].add(cle)

    rows = list(con.execute(f"""
        SELECT vb.id, vb.event_key, vb.book, vb.market, vb.outcome_label,
               vb.line, vb.odd_taken, vb.fair_odd, vb.ev_pct, vb.detected_at,
               {champ_perdu} AS closing_lost,
               e.sport AS sport, e.league AS league, e.start_time AS start_time,
               (cs.id IS NOT NULL) AS a_cloture,
               (cs.fair_odd IS NOT NULL) AS a_fair
        FROM value_bets vb
        LEFT JOIN clv_snapshots cs
               ON cs.value_bet_id = vb.id AND cs.closing = 1
        LEFT JOIN events e ON e.event_key = vb.event_key
    """))
    if not rows:
        raise SystemExit("Aucune détection en base.")

    gardees = [r for r in rows
               if (a.sport is None or (r["sport"] or "") == a.sport)
               and (porte is None or porte(r))]
    if not gardees:
        raise SystemExit("Aucune détection ne passe ce filtre.")

    print(f"\nÉCART DE CAPTURE DE LA CLÔTURE — porte : {desc}")
    print(f"Sport : {a.sport or 'tous'}   ·   {len(gardees)} opportunités")
    if not a_perdu:
        print("⚠️ Colonne `closing_lost` absente de cette base : la colonne "
              "« perdue » lira 0 partout.")

    def deplace(r) -> bool:
        return len(par_groupe.get(_groupe_tolerant(r["event_key"]), ())) > 1

    par_bande: dict[str, list] = defaultdict(list)
    for r in gardees:
        par_bande[_bande_delai(r)].append(r)

    # « déplacé SANS » et « déplacé AVEC » : la part des horaires déplacés dans
    # chacun des deux lots. C'est leur ÉCART qui décide, pas leur niveau.
    # DEUX taux, pas un. Cette sonde comptait tout snapshot `closing = 1`,
    # alors que `clv_roi_matrix` exige en plus un `fair_odd` non nul — une
    # clôture non déviguée ne produit aucune CLV. Les deux outils annonçaient
    # donc deux « taux de capture » incomparables (96 % contre 68 % sur le
    # football) sans que rien ne le signale. `snapshot` est ce que la capture a
    # écrit ; `CLV util.` est ce que l'analyse peut réellement lire.
    ent = (f"{'délai':16} {'opp':>6} {'snapshot':>8} {'%':>6} "
           f"{'CLV util.':>9} {'%':>6}   {'perdue':>6}   "
           f"{'dépl.SANS':>9} {'dépl.AVEC':>9} {'z':>6}")
    print()
    print(ent)
    print("-" * len(ent))
    for lab in ORDRE + sorted(set(par_bande) - set(ORDRE)):
        sub = par_bande.get(lab)
        if not sub:
            continue
        n = len(sub)
        avec = [r for r in sub if r["a_cloture"]]
        sans = [r for r in sub if not r["a_cloture"]]
        util = sum(1 for r in sub if r["a_fair"])
        perdue = sum(1 for r in sub if r["closing_lost"])
        # SANS d'abord : voir `_prop_diff`.
        p_sans, p_avec, _d, z = _prop_diff(
            sum(1 for r in sans if deplace(r)), len(sans),
            sum(1 for r in avec if deplace(r)), len(avec))
        f = lambda v: "—" if v is None else f"{100 * v:5.1f}%"  # noqa: E731
        print(f"{lab:16} {n:6} {len(avec):8} {100 * len(avec) / n:5.1f}% "
              f"{util:9} {100 * util / n:5.1f}%   {perdue:6}   "
              f"{f(p_sans):>9} {f(p_avec):>9} "
              f"{('—' if z is None else f'{z:+.2f}'):>6}")

    # L'HYPOTHÈSE SUIVANTE, testée ici. `closing_group` (storage.py:1848)
    # apparie aussi sur `market` ET sur `line`. La ligne d'un handicap ou d'un
    # total DÉRIVE avec le temps : un pari pris à −0,5 se retrouve coté −1,0 au
    # coup d'envoi, et la recherche de clôture demande toujours −0,5. Plus le
    # pari est pris tôt, plus la ligne a eu le temps de bouger — exactement la
    # forme du déficit. Si l'hypothèse est juste, les marchés SANS ligne (h2h)
    # gardent leur taux de capture loin du match, et seuls ceux À ligne
    # s'effondrent. Si les deux s'effondrent pareil, elle est écartée aussi.
    print("\nLA CAPTURE PAR MARCHÉ — la ligne dérive-t-elle hors de portée ?")
    e2 = (f"{'marché':14} {'ligne':>6}   {'< 24 h':>8} {'%':>7}   "
          f"{'>= 24 h':>8} {'%':>7}   {'chute':>7}")
    print(e2)
    print("-" * len(e2))
    LOIN = {"24-48 h", "48-72 h", "72-96 h", "96-120 h", "120-168 h", "> 168 h"}
    par_marche: dict[str, list] = defaultdict(list)
    for r in gardees:
        par_marche[(r["market"] or "?")].append(r)
    for m in sorted(par_marche, key=lambda k: -len(par_marche[k])):
        sub = par_marche[m]
        if len(sub) < 30:
            continue
        avec_ligne = sum(1 for r in sub if r["line"] is not None)
        court = [r for r in sub if _bande_delai(r) not in LOIN]
        loin = [r for r in sub if _bande_delai(r) in LOIN]
        if not court or not loin:
            continue
        tc = sum(1 for r in court if r["a_cloture"]) / len(court)
        tl = sum(1 for r in loin if r["a_cloture"]) / len(loin)
        print(f"{m[:14]:14} {100 * avec_ligne / len(sub):5.0f}%   "
              f"{len(court):8} {100 * tc:6.1f}%   {len(loin):8} "
              f"{100 * tl:6.1f}%   {100 * (tl - tc):+6.1f} pt")
    print("\n  Une chute concentrée sur les marchés à ligne (handicaps, totaux) "
          "confirme la\n  dérive de ligne ; une chute égale partout, y compris "
          "sur un marché à 0 % de\n  ligne, l'écarte à son tour.")

    # LE TEST QUI RÉPOND VRAIMENT À LA QUESTION POSÉE.
    #
    # Deux mécanismes ont été proposés pour le déficit de capture et les deux
    # sont morts : la clé exacte (z = +1,3) et la dérive de ligne (h2h sans
    # ligne chute autant que les totaux avec ligne). Mais la question n'a
    # jamais été « pourquoi manquent-elles » — c'est « leur absence
    # fausse-t-elle la CLV mesurée ». Et ça se teste SANS connaître le
    # mécanisme : il suffit de comparer les paris qui gardent leur clôture à
    # ceux qui la perdent, sur ce qu'on observe des DEUX côtés.
    #
    # `ev_pct` est l'observable qui convient : c'est l'edge attendu à la
    # détection, et il vaut (odd_taken / fair_odd − 1) × 100, donc il porte
    # exactement ce que la CLV cherche à confirmer. La cote complète le
    # tableau, la CLV variant fortement par tranche de cote.
    #
    # ⚠️ Ce n'est PAS une preuve d'absence de biais : deux paris de même EV
    # peuvent avoir des CLV différentes, et la CLV des manquants est par
    # construction inobservable. Deux lots équilibrés sur l'EV et sur la cote
    # rendent seulement un gros biais improbable.
    print("\nLES CLÔTURES MANQUANTES SONT-ELLES UN ÉCHANTILLON NEUTRE ?")
    # La cote a SON t, comme l'EV. La première version imprimait son écart nu :
    # un écart de 0,20 sur une moyenne de 3,00 se lit « petit » alors qu'à
    # n = 1279 contre 484 il peut très bien être franc. Un écart sans son
    # effectif ne se juge pas à l'œil, et la cote est l'autre dimension qui
    # prédit la CLV — la laisser sans test laissait la moitié de la question
    # ouverte en donnant l'air d'y avoir répondu.
    e3 = (f"{'délai':16} {'avec':>6} {'EV avec':>7} {'EV sans':>7} "
          f"{'Δ EV':>6} {'t':>6}   {'ct avec':>7} {'ct sans':>7} "
          f"{'Δ ct':>6} {'t':>6}")
    print(e3)
    print("-" * len(e3))

    def _welch(a: list, b: list):
        if len(a) < 2 or len(b) < 2:
            return None, None
        d = st.mean(a) - st.mean(b)
        v = st.variance(a) / len(a) + st.variance(b) / len(b)
        return d, (d / v ** 0.5 if v > 0 else None)

    def _vals(sub, champ):
        return [float(r[champ]) for r in sub if r[champ] is not None]

    lignes_test = []
    for lab in ORDRE + sorted(set(par_bande) - set(ORDRE)):
        sub = par_bande.get(lab)
        if not sub:
            continue
        av = [r for r in sub if r["a_fair"]]
        sa = [r for r in sub if not r["a_fair"]]
        ev_a, ev_s = _vals(av, "ev_pct"), _vals(sa, "ev_pct")
        co_a, co_s = _vals(av, "odd_taken"), _vals(sa, "odd_taken")
        d_ev, t_ev = _welch(ev_a, ev_s)
        d_co, t_co = _welch(co_a, co_s)
        m = lambda v: "—" if not v else f"{st.mean(v):.2f}"  # noqa: E731
        g = lambda v: "—" if v is None else f"{v:+.2f}"  # noqa: E731
        print(f"{lab:16} {len(av):3}/{len(sa):<3} {m(ev_a):>7} {m(ev_s):>7} "
              f"{g(d_ev):>6} {g(t_ev):>6}   {m(co_a):>7} {m(co_s):>7} "
              f"{g(d_co):>6} {g(t_co):>6}")
        # LES DEUX tests comptent dans la famille : chercher un biais sur deux
        # dimensions puis corriger comme si on n'en avait cherché qu'une
        # reviendrait à s'accorder deux chances en n'en payant qu'une.
        for dim, t in (("EV", t_ev), ("cote", t_co)):
            if t is not None:
                lignes_test.append((lab, dim, t))

    av = [r for r in gardees if r["a_fair"]]
    sa = [r for r in gardees if not r["a_fair"]]
    d_ev, t_ev = _welch(_vals(av, "ev_pct"), _vals(sa, "ev_pct"))
    d_co, _ = _welch(_vals(av, "odd_taken"), _vals(sa, "odd_taken"))
    print("-" * len(e3))
    if d_ev is None:
        print("  Incalculable : un des deux lots est vide.")
    else:
        seuil = (st.NormalDist().inv_cdf(1 - 0.025 / len(lignes_test))
                 if lignes_test else 1.96)
        # LA MAGNITUDE, à côté de la significativité. À ces effectifs un
        # décalage de 0,20 sur une cote moyenne de 3,00 est parfaitement
        # détectable ET parfaitement négligeable : les deux sont vrais en même
        # temps, et une table qui n'imprime que le t fait lire le premier
        # constat comme s'il impliquait le contraire du second.
        #
        # La borne 1 pour 1 : l'EV vaut (cote / fair_odd_DÉTECTION − 1) × 100,
        # la CLV vaut (cote / fair_odd_CLÔTURE − 1) × 100. Même numérateur,
        # même forme — si la clôture ne bougeait pas de la détection, CLV = EV
        # exactement. Un déplacement d'EV de X points ne peut donc pas en
        # fabriquer plus de X en CLV. C'est un plafond, pas une estimation.
        def _biais(bandes, champ_ok, champ_ko):
            na = nt = 0
            sa = sv = 0.0
            for lab2, sub2 in par_bande.items():
                if lab2 not in bandes:
                    continue
                a2 = _vals([r for r in sub2 if r["a_fair"]], champ_ok)
                s2 = _vals([r for r in sub2 if not r["a_fair"]], champ_ko)
                na += len(a2); nt += len(a2) + len(s2)
                sa += sum(a2); sv += sum(a2) + sum(s2)
            if not na or not nt:
                return None
            return sa / na - sv / nt

        COURT = {l for l, _, _ in BANDES_DELAI if l not in LOIN}
        b_court = _biais(COURT, "ev_pct", "ev_pct")
        b_loin = _biais(LOIN, "ev_pct", "ev_pct")
        _d, t_co_tot = _welch(_vals(av, "odd_taken"), _vals(sa, "odd_taken"))
        print(f"{'TOUTES BANDES':16} {len(av):3}/{len(sa):<3} "
              f"{st.mean(_vals(av, 'ev_pct')):7.2f} "
              f"{st.mean(_vals(sa, 'ev_pct')):7.2f} {d_ev:+6.2f} "
              f"{('—' if t_ev is None else f'{t_ev:+.2f}'):>6}   "
              f"{st.mean(_vals(av, 'odd_taken')):7.2f} "
              f"{st.mean(_vals(sa, 'odd_taken')):7.2f} "
              f"{('—' if d_co is None else f'{d_co:+.2f}'):>6} "
              f"{('—' if t_co_tot is None else f'{t_co_tot:+.2f}'):>6}")
        # TOUTES celles qui franchissent, pas seulement la pire. N'annoncer que
        # le maximum masquait les autres : sur la base de production, deux
        # tests franchissaient et un seul était nommé.
        francs = sorted((x for x in lignes_test if abs(x[2]) >= seuil),
                        key=lambda x: -abs(x[2]))
        pire = max(lignes_test, key=lambda x: abs(x[2]), default=None)
        print(f"\n  Seuil de Bonferroni pour {len(lignes_test)} tests "
              f"(2 dimensions × {len(lignes_test) // 2} bandes) : "
              f"|t| ≥ {seuil:.2f}.")
        if francs:
            print(f"  {len(francs)} test(s) sur {len(lignes_test)} le "
                  f"franchissent :")
            for lab2, dim, t in francs:
                sens = ("les paris CONSERVÉS ont la valeur la plus haute"
                        if t > 0 else
                        "les paris PERDUS ont la valeur la plus haute")
                print(f"    · « {lab2} » sur {dim:4} : t = {t:+.2f} — {sens}")
            print("  Dans ces bandes le déficit de capture est SÉLECTIF. "
                  "Reste à savoir ce que ça PÈSE :\n  un t dit « distinguable "
                  "de zéro », jamais « ça compte ».")
        else:
            p = f"{abs(pire[2]):.2f} (« {pire[0]} » sur {pire[1]})" if pire else "—"
            print(f"  Aucun ne le franchit, le maximum atteint étant |t| = {p}. "
                  f"Les paris qui perdent\n  leur clôture ont le même EV et la "
                  f"même cote que ceux qui la gardent : sur les deux\n  "
                  f"dimensions observables qui prédisent la CLV, les manquants "
                  f"sont un échantillon\n  NEUTRE, et le déficit de capture ne "
                  f"fabrique pas l'écart de CLV entre bandes.")

    if b_court is not None and b_loin is not None:
        d = b_court - b_loin
        print("\n  CE QUE CETTE SÉLECTIVITÉ DÉPLACE RÉELLEMENT")
        print(f"    biais d'EV sur les bandes < 24 h : {b_court:+.3f} pt   "
              f"sur les bandes >= 24 h : {b_loin:+.3f} pt")
        print(f"    → biais induit sur le CONTRASTE court − lointain : "
              f"{d:+.3f} point d'EV")
        print(f"    La CLV ne peut pas réagir à l'EV plus que 1 pour 1 (même "
              f"numérateur, seule la\n    référence change), donc cette "
              f"sélectivité fabrique AU PLUS {abs(d):.3f} point de\n    "
              f"l'écart de CLV entre les deux zones.", end=" ")
        if d < 0:
            print("Et son signe FLATTE la zone lointaine :\n    l'écart vrai "
                  "est PLUS GRAND que l'écart mesuré, pas plus petit.")
        else:
            print("Son signe DÉSAVANTAGE la zone lointaine :\n    l'écart vrai "
                  "est plus petit que l'écart mesuré, de ce montant au plus.")

    print("\nLE TÉMOIN, TOUTES BANDES CONFONDUES")
    print("-" * len(ent))
    avec = [r for r in gardees if r["a_cloture"]]
    sans = [r for r in gardees if not r["a_cloture"]]
    p_sans, p_avec, d, z = _prop_diff(
        sum(1 for r in sans if deplace(r)), len(sans),
        sum(1 for r in avec if deplace(r)), len(avec))
    if p_sans is None:
        print("  Incalculable : un des deux lots est vide.")
    else:
        print(f"  horaire déplacé PARMI CEUX SANS clôture : {100 * p_sans:5.2f} % "
              f"(n={len(sans)})")
        print(f"  horaire déplacé PARMI CEUX AVEC clôture : {100 * p_avec:5.2f} % "
              f"(n={len(avec)})")
        print(f"  écart {100 * d:+.2f} points (SANS − AVEC)   z = "
              f"{'—' if z is None else f'{z:+.2f}'}")
        if z is None or abs(z) < 2:
            print("\n  → Les deux parts ne se distinguent PAS. Le déplacement "
                  "d'horaire n'explique\n    pas le déficit de capture : "
                  "l'hypothèse de la clé exacte est ÉCARTÉE, et le\n    déficit "
                  "reste à expliquer (marché trop mince pour être dévigué, "
                  "aucune cote\n    Pinnacle avant le coup d'envoi).")
        elif z > 0:
            print("\n  → Les paris SANS clôture sont PLUS souvent déplacés : "
                  "l'hypothèse de la clé\n    exacte de `closing_group` "
                  "(storage.py:1848) est SOUTENUE. Câbler la clé tolérante\n"
                  "    `_event_key_like` dans `closing_group` récupérerait ces "
                  "clôtures.")
        else:
            print("\n  → Les paris SANS clôture sont MOINS souvent déplacés. "
                  "C'est l'INVERSE de\n    l'hypothèse : elle est écartée, et "
                  "le déficit vient d'ailleurs.")

    print("\n⚠️ Cette sonde ne voit un déplacement que si le daemon a créé une "
          "ligne `events`\n   pour la nouvelle clé. Un horaire déplacé alors "
          "que l'événement n'était plus\n   scanné reste invisible : le "
          "phénomène est SOUS-ESTIMÉ, jamais surestimé.")
    print("\nLecture seule — aucune écriture, aucun réglage modifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
