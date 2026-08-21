"""Les handicaps : quelle convention de ligne chaque book utilise-t-il ? §21.13

Pourquoi cet outil existe
-------------------------
Les handicaps représentent **26,9 % du payload de Pinnacle au football** et
36,7 % au tennis, et ils sont jetés à la source (`main.py`, `_fetch_all_parallel`).
Le motif est réel : Pinnacle SIGNE chaque côté (home −1.0 / away +1.0) là où
les books soft portent souvent les deux côtés au même |ligne|. Apparier ces
deux conventions ne lève aucune erreur — le groupe a ses deux côtés, la marge
est plausible, seule la ligne juste est fausse. C'est la panne silencieuse du
§21.3, appliquée à un quart du volume.

**La question n'est donc pas « peut-on les ajouter » mais « la convention de
chaque book est-elle déterminable sans ambiguïté ».** Un book dont on ne sait
pas lire le signe doit rester dehors : mieux vaut zéro pari qu'un pari inventé.

Cette sonde ne modifie RIEN. Pour chaque book, elle regarde les couples de
handicaps d'un même événement et classe la convention :

  SIGNÉE      les deux côtés portent des lignes opposées (−1,0 / +1,0)
              → alignable sur Pinnacle par simple lecture du signe
  MIROIR      les deux côtés portent la MÊME ligne (1,0 / 1,0)
              → le signe doit être déduit du côté (home/away), à valider

Un book qui MÊLE les deux est refusé : lire son signe serait juste une fois sur
deux, et l'erreur ne lèverait rien. Un book qui ne rend qu'un côté par ligne
est inutilisable de toute façon — sans les deux côtés, pas de devig.

    .venv/bin/python -m scripts.handicap_conventions --sport soccer

⚠️ Elle déclenche une collecte réelle sur tous les books. À lancer
ponctuellement.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from src.models import Book, MarketType


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="soccer")
    args = ap.parse_args()

    from src.main import _fetch_all_parallel

    tout = _fetch_all_parallel(args.sport, keep_handicaps=True)
    quotes = [q for q in tout if q.market == MarketType.HANDICAP]

    # Qui produit des handicaps, et qui n'en produit pas ? Sans ce compte,
    # l'absence d'un book se lit comme « pas de convention » alors qu'elle
    # signifie « pas de cotes du tout » — deux diagnostics opposés.
    par_book_total: Counter = Counter()
    sans_ligne: Counter = Counter()
    for q in tout:
        if q.market == MarketType.HANDICAP:
            par_book_total[q.book.value] += 1
            if q.outcome.line is None:
                sans_ligne[q.book.value] += 1
    tous_books = {q.book.value for q in tout}

    print("\n=== A. Qui produit des handicaps ? ===")
    for b in sorted(tous_books):
        n, nul = par_book_total.get(b, 0), sans_ligne.get(b, 0)
        if n == 0:
            print(f"  {b:<20s} AUCUN handicap — son scraper n'en mappe pas")
        elif nul == n:
            print(f"  {b:<20s} {n:6d} handicaps, TOUS sans ligne — inexploitables")
        elif nul:
            print(f"  {b:<20s} {n:6d} handicaps, dont {nul} sans ligne")
        else:
            print(f"  {b:<20s} {n:6d} handicaps")

    if not quotes:
        print("Aucun handicap collecté. Soit les books n'en rendent pas sur ce\n"
              "sport, soit la collecte a échoué — vérifier ./doctor.sh.")
        return

    # Groupes : un même événement, un même book, un même |ligne|. C'est à ce
    # niveau que les deux côtés d'un handicap se rencontrent.
    groupes: dict[tuple, list] = defaultdict(list)
    zero: Counter = Counter()
    for q in quotes:
        if q.outcome.line is None:
            continue
        if q.outcome.line == 0:
            # Un handicap 0 n'a PAS de signe : c'est le même marché des deux
            # côtés. Le compter comme « miroir » faisait déclarer Pinnacle
            # « deux conventions mêlées » sur 941 groupes — un faux verdict sur
            # la référence même du projet.
            zero[q.book.value] += 1
            continue
        groupes[(q.book, q.event_key, abs(q.outcome.line))].append(q)

    par_book: dict[Book, Counter] = defaultdict(Counter)
    exemples: dict[tuple, list] = defaultdict(list)
    for (book, ek, _abs), lot in groupes.items():
        lignes = sorted({q.outcome.line for q in lot})
        if len(lot) < 2:
            par_book[book]["un seul côté"] += 1
            continue
        # Le groupe étant formé sur |ligne|, les valeurs y sont soit toutes
        # égales, soit opposées deux à deux : il n'y a pas de troisième cas.
        # Deux lignes sans rapport (−1,0 et +2,5) sont deux marchés de handicap
        # DIFFÉRENTS, ce qui est normal et ne dit rien d'une convention.
        if len(lignes) == 2 and lignes[0] != 0:
            genre = "SIGNÉE"
        else:
            genre = "MIROIR"
        par_book[book][genre] += 1
        if len(exemples[(book, genre)]) < 2:
            exemples[(book, genre)].append(
                f"{ek[:44]}  " + ", ".join(f"{q.outcome.label}@{q.outcome.line:+g}"
                                           for q in sorted(lot, key=lambda x: x.outcome.label)))

    print(f"\n=== B. Convention de ligne — {args.sport} ===")
    if zero:
        print("  (handicaps de ligne 0 écartés, sans signe par nature : "
              + ", ".join(f"{b} {n}" for b, n in zero.most_common()) + ")")
    print()
    print(f"{'book':<20s} {'groupes':>8s} {'SIGNÉE':>8s} {'MIROIR':>8s} "
          f"{'1 côté':>7s}   verdict")
    print("-" * 86)
    verdicts: dict[str, str] = {}
    for book in sorted(par_book, key=lambda b: -sum(par_book[b].values())):
        c = par_book[book]
        total = c["SIGNÉE"] + c["MIROIR"]
        if total == 0:
            verdict = "aucun couple complet — inutilisable, pas de devig possible"
        elif c["SIGNÉE"] and c["MIROIR"]:
            verdict = "🔴 DEHORS — deux conventions mêlées"
        elif c["SIGNÉE"] == total:
            verdict = "✅ signée, alignable directement"
        else:
            verdict = "🟡 miroir — signe à déduire du côté"
        verdicts[book.value] = verdict
        print(f"{book.value:<20s} {sum(c.values()):8d} {c['SIGNÉE']:8d} "
              f"{c['MIROIR']:8d} {c['un seul côté']:7d}   {verdict}")

    print("\nExemples, un par book et par convention :")
    for (book, genre), lignes in sorted(exemples.items(), key=lambda kv: kv[0][0].value):
        for ex in lignes[:1]:
            print(f"  {book.value:<18s} {genre:<8s} {ex}")

    print("\n" + "=" * 72)
    ok = [b for b, v in verdicts.items() if v.startswith("✅")]
    jaune = [b for b, v in verdicts.items() if v.startswith("🟡")]
    dehors = [b for b, v in verdicts.items() if v.startswith("🔴")]
    print(f"Directement alignables : {', '.join(ok) or 'aucun'}")
    print(f"À déduire du côté      : {', '.join(jaune) or 'aucun'}")
    print(f"À laisser dehors       : {', '.join(dehors) or 'aucun'}")
    print("\n🔴 MAIS le blocage principal n'est PAS la convention des softs.")
    print("   `_group_quotes` groupe sur la ligne SIGNÉE (event, market, line).")
    print("   Pinnacle publiant home@-1 et away@+1, les deux côtés d'un MÊME")
    print("   marché tombent dans DEUX groupes séparés, d'une issue chacun —")
    print("   et `_devig_group` en exige au moins deux. Aucun handicap n'est")
    print("   donc devigable aujourd'hui, y compris chez la référence.")
    print("   Exploiter les handicaps demande d'abord une clé de groupe")
    print("   canonique (|ligne| + côté normalisé), AVANT toute question de")
    print("   convention soft.")
    print("\n⚠️ Un book « MIROIR » n'est pas perdu : il porte le même |ligne| des")
    print("deux côtés, et le signe se déduit alors de home/away. Mais cette")
    print("déduction est une HYPOTHÈSE sur la sémantique du book, pas une")
    print("lecture — à valider contre une cote connue avant d'y toucher.")
    print("\n⚠️ Et un book « DEHORS » doit le rester. Un handicap mal signé ne")
    print("lève aucune erreur : il fabrique des paris de valeur inventés (§21.3).")


if __name__ == "__main__":
    main()
