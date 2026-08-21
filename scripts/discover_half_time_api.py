"""Les books en API directe exposent-ils la mi-temps ? — §21.14

Pourquoi cet outil existe
-------------------------
`discover_half_time` ne lit que les books à dump sur disque (Circus, Betano,
MagicBetting). Les autres — Unibet et la famille Kambi (711, Bingoal,
Scooore), GoldenPalace, Ladbrokes — tirent leurs cotes d'une API en direct et
restaient hors de portée.

Elle répond à deux questions, et la seconde est la plus importante :

1. **Quels marchés de mi-temps ces books exposent-ils ?** Unibet est le book le
   plus joué du projet et sa plateforme Kambi est partagée par trois autres :
   un seul relevé en ouvrirait quatre.

2. **En ingère-t-on DÉJÀ sans le savoir ?** `unibet.py` mappe sur le seul
   `betOfferType.id`, sans regarder ni `criterion` ni période. Si Kambi
   publie ses offres de mi-temps sous les mêmes identifiants que le match
   plein, elles sortent aujourd'hui en `TOTALS`/`H2H` et sont confrontées à
   l'échelle 90 minutes de Pinnacle — des value bets fantômes, silencieux.
   **C'est une hypothèse, pas un constat : cette sonde existe pour la
   trancher.**

Elle cherche par LIBELLÉ, pas par schéma. La première version de
`discover_half_time` supposait la forme du dump Circus et n'a rien trouvé là
où il y avait quatre marchés : une sonde qui suppose ce qu'elle découvre ne
découvre rien.

    .venv/bin/python -m scripts.discover_half_time_api
    .venv/bin/python -m scripts.discover_half_time_api --book unibet

⚠️ Elle fait de VRAIS appels aux books. À lancer ponctuellement, pas en boucle.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from scripts.discover_half_time import _INDICES, _CONTRE

# Champs qui, dans ces payloads, portent un identifiant de marché. Sert à
# montrer QUOI mapper une fois un libellé de mi-temps repéré.
_CHAMPS_ID = ("id", "typeId", "betOfferType", "criterion", "betId",
              "marketTypeId", "alternativeDescription", "name", "label",
              "englishLabel", "criterionId")


def _textes(d: dict) -> list[str]:
    """Les valeurs textuelles d'un dict, y compris un niveau d'imbrication.

    Kambi range le vrai nom du marché dans `criterion.label`, pas à la racine
    de l'offre : ne regarder que la racine manquerait tout.
    """
    out = []
    for v in d.values():
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            out.extend(x for x in v.values() if isinstance(x, str))
    return out


def _empreinte(d: dict) -> str:
    """Ce qu'il faut pour identifier le marché, en clair."""
    bouts = []
    for k in _CHAMPS_ID:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, dict):
            petit = {kk: vv for kk, vv in v.items()
                     if kk in ("id", "name", "label", "englishLabel")}
            if petit:
                bouts.append(f"{k}={petit}")
        elif isinstance(v, (str, int, float)):
            bouts.append(f"{k}={v!r}")
    return "  ".join(bouts) or "(aucun identifiant reconnaissable)"


def _explorer(payload, mappes: dict) -> tuple[Counter, Counter]:
    """(candidats mi-temps, contaminations) relevés dans un payload.

    Une « contamination » est un marché dont le libellé dit mi-temps ALORS QUE
    son identifiant est déjà mappé sur un type de match plein : ces cotes
    entrent donc dès aujourd'hui dans le pipeline sous la mauvaise échelle.
    """
    candidats: Counter = Counter()
    contamines: Counter = Counter()
    piles = [payload]
    while piles:
        n = piles.pop()
        if isinstance(n, dict):
            textes = _textes(n)
            if any(_INDICES.search(t) and not _CONTRE.search(t) for t in textes):
                # On s'arrête au dict le plus ENGLOBANT et on ne descend pas
                # dedans : chez Kambi le libellé vit dans `criterion`, imbriqué
                # dans l'offre, et compter les deux doublerait chaque marché.
                # Les champs d'un marché appartiennent au marché.
                emp = _empreinte(n)
                candidats[emp] += 1
                # L'identifiant est-il de ceux qu'on mappe déjà ?
                for k in ("typeId", "betId", "marketTypeId"):
                    if n.get(k) in mappes:
                        contamines[f"{k}={n[k]!r} → {mappes[n[k]].value}   {emp}"] += 1
                bot = n.get("betOfferType")
                if isinstance(bot, dict) and bot.get("id") in mappes:
                    contamines[f"betOfferType.id={bot['id']!r} → "
                               f"{mappes[bot['id']].value}   {emp}"] += 1
                continue
            piles.extend(v for v in n.values() if isinstance(v, (dict, list)))
        elif isinstance(n, list):
            piles.extend(x for x in n if isinstance(x, (dict, list)))
    return candidats, contamines


def _rapport(nom: str, payload, mappes: dict) -> None:
    candidats, contamines = _explorer(payload, mappes)
    print(f"\n--- {nom} ---")
    if not candidats:
        print("  Aucun libellé de mi-temps dans le payload.")
        print("  → Soit ce book n'en propose pas, soit l'endpoint interrogé n'en")
        print("    remonte pas (une vue « principale » n'expose souvent que les")
        print("    marchés phares). Ne pas conclure à une absence sans vérifier.")
        return
    print(f"  {len(candidats)} marché(s) de mi-temps distincts repérés :")
    for emp, n in candidats.most_common(12):
        print(f"    ★ ×{n:<5d} {emp}")
    if contamines:
        print(f"\n  🔴 CONTAMINATION — {len(contamines)} identifiant(s) déjà mappés")
        print("     sur un type de MATCH PLEIN portent un libellé de mi-temps.")
        print("     Ces cotes entrent donc dès aujourd'hui dans le pipeline sous")
        print("     la mauvaise échelle, et sont comparées au 90 minutes de")
        print("     Pinnacle. C'est la fabrique de value bets fantômes du §21.3.")
        for emp, n in contamines.most_common(8):
            print(f"       ×{n:<5d} {emp}")
    else:
        print("\n  OK : aucun identifiant déjà mappé ne porte de libellé de")
        print("  mi-temps. Les marchés ci-dessus sont donc écartés aujourd'hui,")
        print("  et peuvent être mappés proprement sur h2h_h1 / totals_h1.")


def unibet() -> None:
    from src.scrapers.unibet import UnibetScraper, _MARKET_BY_TYPE_ID
    sc = UnibetScraper()
    try:
        _rapport("Unibet (Kambi — vaut aussi pour 711, Bingoal, Scooore)",
                 sc.fetch_listview("soccer"), _MARKET_BY_TYPE_ID)
    finally:
        getattr(sc, "close", lambda: None)()


def goldenpalace() -> None:
    from src.scrapers.goldenpalace import GoldenPalaceScraper, _MARKET_BY_TYPE_ID
    sc = GoldenPalaceScraper()
    try:
        _rapport("GoldenPalace (Altenar)", sc.fetch_events("soccer"),
                 _MARKET_BY_TYPE_ID)
    finally:
        getattr(sc, "close", lambda: None)()


def ladbrokes() -> None:
    from src.scrapers.ladbrokes import LadbrokesScraper, _MARKET_BY_BET_ID
    sc = LadbrokesScraper()
    try:
        _rapport("Ladbrokes", sc.fetch_prematch_next(), _MARKET_BY_BET_ID)
    finally:
        getattr(sc, "close", lambda: None)()


_BOOKS = {"unibet": unibet, "goldenpalace": goldenpalace, "ladbrokes": ladbrokes}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", default="", help=f"Un seul book parmi {', '.join(_BOOKS)}.")
    args = ap.parse_args()

    choisis = {args.book: _BOOKS[args.book]} if args.book in _BOOKS else _BOOKS
    if args.book and args.book not in _BOOKS:
        print(f"Book inconnu : {args.book}. Connus : {', '.join(_BOOKS)}")
        return

    print("Appels RÉELS aux books — AUCUN mappage effectué, aucun code modifié.")
    for nom, f in choisis.items():
        try:
            f()
        except Exception as e:
            print(f"\n--- {nom} : échec ({type(e).__name__}: {e})")
            print("    Un book injoignable ne prouve rien sur ses marchés.")

    print("\n" + "=" * 64)
    print("StarCasino, Napoleon et Betcenter ne sont pas couverts : leurs")
    print("scrapers ne portent pas de table d'identifiants comparable. À")
    print("traiter séparément si les books ci-dessus rendent quelque chose.")


if __name__ == "__main__":
    main()
