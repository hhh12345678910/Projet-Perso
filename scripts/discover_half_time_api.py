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


#: Nœuds d'HORLOGE de match, pas des marchés. Kambi publie l'état en direct
#: (« 1ère mi-temps », minute 45, running) sur chaque événement en cours : sans
#: ce filtre, 37 de ces nœuds noient les 2 vrais marchés relevés le 21/08.
#: Un signal qu'il faut chercher dans du bruit finit par être manqué.
_CLES_HORLOGE = ("minute", "second", "running", "periodId",
                 "minutesLeftInPeriod", "secondsLeftInMinute")


def _est_horloge(d: dict) -> bool:
    return sum(1 for k in _CLES_HORLOGE if k in d) >= 2


def _textes(d: dict) -> list[str]:
    """Les valeurs textuelles d'un dict, y compris un niveau d'imbrication.

    Kambi range le vrai nom du marché dans `criterion.label`, pas à la racine
    de l'offre : ne regarder que la racine manquerait tout.
    """
    out = []
    for v in d.values():
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict) and not _est_horloge(v):
            # Sans cette exclusion, le texte d'une horloge imbriquée remonte au
            # parent, qui est alors retenu comme « marché » avec une empreinte
            # vide. Écarter l'horloge elle-même ne suffit pas : il faut aussi
            # l'empêcher de contaminer ce qui la contient.
            out.extend(x for x in v.values() if isinstance(x, str))
    return out


def _declencheur(d: dict) -> str:
    """Le texte qui a fait retenir ce dict.

    C'est l'information la plus utile de toute la sonde : elle dit CE QU'EST le
    marché. Sans elle on lit « id=11928 » sans savoir si c'est un 1X2 ou un
    total, donc sans pouvoir mapper.
    """
    for t in _textes(d):
        if _INDICES.search(t) and not _CONTRE.search(t):
            return t
    return "?"


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
    if bouts:
        return "  ".join(bouts)
    # Aucun champ connu : montrer ce qu'il Y A, plutôt que d'avouer l'échec.
    # 25 marchés relevés sous « aucun identifiant reconnaissable » ne sont
    # exploitables par personne.
    apercu = {k: v for k, v in d.items()
              if isinstance(v, (str, int, float, bool))}
    return f"champs inconnus : {dict(list(apercu.items())[:5])}" if apercu else "(vide)"


def _matche(d) -> bool:
    """Ce nœud porte-t-il, à son propre niveau, un libellé de mi-temps ?"""
    if not isinstance(d, dict) or _est_horloge(d):
        return False
    return any(_INDICES.search(t) and not _CONTRE.search(t) for t in _textes(d))


def _descendant_matche(n) -> bool:
    """Un nœud PLUS PROFOND porte-t-il le même libellé ?

    C'est ce qui remplace toute hypothèse de schéma. Deux tentatives ont
    échoué avant celle-ci : s'arrêter au nœud le plus englobant retenait des
    `eventId` inexploitables, et exiger des clés de marché connues
    (`criterion`, `betOfferType`) ne retenait plus RIEN — le nœud qui portait
    le libellé ÉTAIT le `criterion`, qui ne contient pas une clé de ce nom.
    Un faux « ce book n'en propose pas », soit exactement le verdict que cette
    sonde existe pour éviter.

    On garde donc le match le plus PROFOND, sans rien supposer de la forme.
    """
    piles = [v for v in n.values() if isinstance(v, (dict, list))]
    while piles:
        x = piles.pop()
        if isinstance(x, dict):
            if _matche(x):
                return True
            piles.extend(v for v in x.values() if isinstance(v, (dict, list)))
        elif isinstance(x, list):
            piles.extend(y for y in x if isinstance(y, (dict, list)))
    return False


def _contexte(ancetres: tuple) -> str:
    """À quoi ce nœud est-il rattaché ?

    Un `criterion` relevé seul ne suffit pas à mapper : il faut savoir s'il
    porte une VRAIE offre — avec un `betOfferType` et des issues cotées — ou
    s'il n'est qu'une entrée de catalogue. Deux identifiants vus une seule fois
    chacun ressemblent à un catalogue, et mapper là-dessus serait deviner.
    """
    for a in ancetres[::-1]:
        bot = a.get("betOfferType")
        if isinstance(bot, dict) or "outcomes" in a:
            bits = []
            if isinstance(bot, dict):
                bits.append(f"betOfferType={bot.get('id')}/{bot.get('name')!r}")
            outs = a.get("outcomes")
            bits.append(f"{len(outs)} issues cotées" if isinstance(outs, list)
                        else "AUCUNE issue — catalogue, pas une offre")
            return "   ← " + "  ".join(bits)
    return "   ← ⚠️ rattaché à aucune offre : entrée de CATALOGUE, non mappable"


def _explorer(payload, mappes: dict, bruts: list | None = None) -> tuple[Counter, Counter]:
    """(candidats mi-temps, contaminations) relevés dans un payload.

    Une « contamination » est un marché dont le libellé dit mi-temps ALORS QUE
    son identifiant est déjà mappé sur un type de match plein : ces cotes
    entrent donc dès aujourd'hui dans le pipeline sous la mauvaise échelle.
    """
    candidats: Counter = Counter()
    contamines: Counter = Counter()
    # On porte la chaîne d'ancêtres : le libellé vit sur le nœud le plus
    # profond (`criterion`), mais l'identifiant qu'on mappe vit sur son PARENT
    # (`betOffer.betOfferType.id`). Chercher la contamination sur le seul nœud
    # retenu la manquerait entièrement.
    piles: list = [(payload, ())]
    while piles:
        n, ancetres = piles.pop()
        if isinstance(n, dict):
            textes = _textes(n)
            if _est_horloge(n):
                piles.extend((v, ancetres + (n,)) for v in n.values()
                             if isinstance(v, (dict, list)))
                continue
            if any(_INDICES.search(t) and not _CONTRE.search(t) for t in textes):
                if _descendant_matche(n):
                    # Un nœud plus profond dit la même chose : c'est lui le
                    # marché, pas ce conteneur. On descend.
                    piles.extend((v, ancetres + (n,)) for v in n.values()
                                 if isinstance(v, (dict, list)))
                    continue
                if bruts is not None and len(bruts) < 5:
                    bruts.append(n)
                emp = f"{_declencheur(n)!r}   {_empreinte(n)}{_contexte(ancetres)}"
                candidats[emp] += 1
                # L'identifiant mappé est-il sur ce nœud, ou sur l'un de ses
                # ancêtres ? Les deux comptent : c'est la même offre.
                for noeud in (n,) + ancetres:
                    for k in ("typeId", "betId", "marketTypeId"):
                        if noeud.get(k) in mappes:
                            contamines[f"{k}={noeud[k]!r} → "
                                       f"{mappes[noeud[k]].value}   {emp}"] += 1
                    bot = noeud.get("betOfferType")
                    if isinstance(bot, dict) and bot.get("id") in mappes:
                        contamines[f"betOfferType.id={bot['id']!r} → "
                                   f"{mappes[bot['id']].value}   {emp}"] += 1
                continue
            piles.extend((v, ancetres + (n,)) for v in n.values()
                         if isinstance(v, (dict, list)))
        elif isinstance(n, list):
            piles.extend((x, ancetres) for x in n if isinstance(x, (dict, list)))
    return candidats, contamines


def _rapport(nom: str, payload, mappes: dict, brut: bool = False) -> None:
    bruts: list = []
    candidats, contamines = _explorer(payload, mappes, bruts)
    print(f"\n--- {nom} ---")
    if not candidats:
        print("  Aucun libellé de mi-temps dans le payload.")
        print("  → Soit ce book n'en propose pas, soit l'endpoint interrogé n'en")
        print("    remonte pas (une vue « principale » n'expose souvent que les")
        print("    marchés phares). Ne pas conclure à une absence sans vérifier.")
        # Montrer ce QU'IL Y A : deux règles d'arrêt successives ont déjà
        # produit un faux « rien trouvé » sur ce book. Un aperçu des marchés
        # réellement présents dit tout de suite si le payload en contient.
        noms = _apercu_marches(payload)
        if noms:
            print(f"\n  Pour contrôle, {len(noms)} libellés de marché lus dans ce")
            print("  payload — si aucun n'évoque une mi-temps, l'endpoint n'en")
            print("  expose pas :")
            for t, k in noms.most_common(15):
                print(f"    ×{k:<5d} {t!r}")
        else:
            print("\n  ⚠️ Et AUCUN libellé de marché lu du tout : la sonde ne")
            print("     comprend pas la forme de ce payload. Relancer avec --brut.")
        if brut:
            _montrer_brut(payload)
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


def _apercu_marches(payload) -> Counter:
    """Les libellés qui ressemblent à des noms de marché, mi-temps ou non.

    Sert de témoin : « aucune mi-temps » ne vaut que si la sonde a bien lu des
    marchés par ailleurs.
    """
    noms: Counter = Counter()
    piles = [payload]
    while piles:
        n = piles.pop()
        if isinstance(n, dict):
            for cle in ("label", "name", "englishLabel", "alternativeDescription"):
                v = n.get(cle)
                if isinstance(v, str) and v.strip():
                    noms[v.strip()] += 1
            piles.extend(v for v in n.values() if isinstance(v, (dict, list)))
        elif isinstance(n, list):
            piles.extend(x for x in n if isinstance(x, (dict, list)))
    return noms


def _montrer_brut(payload) -> None:
    """Un échantillon de la structure, pour quand la sonde ne comprend rien."""
    import json
    print("\n  --brut : premiers nœuds du payload\n")
    piles = [(payload, 0)]
    vus = 0
    while piles and vus < 3:
        n, prof = piles.pop()
        if isinstance(n, dict) and prof >= 2:
            extrait = json.dumps(n, ensure_ascii=False)[:600]
            print(f"    {extrait}…\n")
            vus += 1
            continue
        if isinstance(n, dict):
            piles.extend((v, prof + 1) for v in n.values() if isinstance(v, (dict, list)))
        elif isinstance(n, list):
            piles.extend((x, prof + 1) for x in n if isinstance(x, (dict, list)))


def unibet(brut: bool = False) -> None:
    from src.scrapers.unibet import UnibetScraper, _MARKET_BY_TYPE_ID
    sc = UnibetScraper()
    try:
        _rapport("Unibet (Kambi — vaut aussi pour 711, Bingoal, Scooore)",
                 sc.fetch_listview("soccer"), _MARKET_BY_TYPE_ID, brut=brut)
    finally:
        getattr(sc, "close", lambda: None)()


def goldenpalace(brut: bool = False) -> None:
    from src.scrapers.goldenpalace import GoldenPalaceScraper, _MARKET_BY_TYPE_ID
    sc = GoldenPalaceScraper()
    try:
        _rapport("GoldenPalace (Altenar)", sc.fetch_events("soccer"),
                 _MARKET_BY_TYPE_ID, brut=brut)
    finally:
        getattr(sc, "close", lambda: None)()


def ladbrokes(brut: bool = False) -> None:
    from src.scrapers.ladbrokes import LadbrokesScraper, _MARKET_BY_BET_ID
    sc = LadbrokesScraper()
    try:
        _rapport("Ladbrokes", sc.fetch_prematch_next(), _MARKET_BY_BET_ID, brut=brut)
    finally:
        getattr(sc, "close", lambda: None)()


_BOOKS = {"unibet": unibet, "goldenpalace": goldenpalace, "ladbrokes": ladbrokes}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", default="", help=f"Un seul book parmi {', '.join(_BOOKS)}.")
    ap.add_argument("--brut", action="store_true",
                    help="Affiche un échantillon brut du payload quand rien "
                         "n'est trouvé — pour voir la forme réelle plutôt que "
                         "de conclure à une absence.")
    args = ap.parse_args()

    choisis = {args.book: _BOOKS[args.book]} if args.book in _BOOKS else _BOOKS
    if args.book and args.book not in _BOOKS:
        print(f"Book inconnu : {args.book}. Connus : {', '.join(_BOOKS)}")
        return

    print("Appels RÉELS aux books — AUCUN mappage effectué, aucun code modifié.")
    for nom, f in choisis.items():
        try:
            f(brut=args.brut)
        except Exception as e:
            print(f"\n--- {nom} : échec ({type(e).__name__}: {e})")
            print("    Un book injoignable ne prouve rien sur ses marchés.")

    print("\n" + "=" * 64)
    print("StarCasino, Napoleon et Betcenter ne sont pas couverts : leurs")
    print("scrapers ne portent pas de table d'identifiants comparable. À")
    print("traiter séparément si les books ci-dessus rendent quelque chose.")


if __name__ == "__main__":
    main()
