"""Les critères d'une analyse — validés AVANT de toucher à la base.

POURQUOI LA VALIDATION EST ICI ET PAS DANS LE SQL
-------------------------------------------------
Une borne absurde (`cote 4 → 2`) rendrait zéro ligne, et zéro ligne se lit
comme « aucun pari n'est rentable » au lieu de « ta bande est à l'envers ».
Une date mal écrite (`01/08/2026`) filtrerait tout ou rien selon la comparaison
de chaînes, en silence. Les deux sont le mode de panne du projet (§11), et les
deux se refusent ici, avec un message qui dit le format attendu.

⚠️ AUCUNE VALEUR UTILISATEUR N'ENTRE JAMAIS DANS UNE CHAÎNE SQL. Ce module
normalise et valide ; `requete.py` ne produit que des marqueurs `?` dont le
NOMBRE dépend des filtres, jamais le contenu.

SÉRIALISABLE PAR CONSTRUCTION
-----------------------------
`en_dict` / `depuis_dict` font l'aller-retour complet. C'est ce qui permettra
d'enregistrer une analyse (« Football — cote 2-4 — EV 10 %+ — Unibet »)
sans rien réécrire : un `Filtres` est déjà un document JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ..models import Book, MarketType
from .perimetre import (BORNES_EV, GROUPES_JUMEAUX, MARCHES_ANALYTICS,
                        SPORTS_ANALYTICS, jumeaux_de)
from .populations import Population

#: Les books que la base peut contenir. Fermé : il vient de l'énumération du
#: moteur, jamais d'une liste recopiée.
BOOKS_CONNUS = frozenset(b.value for b in Book)

#: Les marchés que le moteur produit. Fermé, même raison.
MARCHES_CONNUS = frozenset(m.value for m in MarketType)

#: ⚠️ Le PÉRIMÈTRE de l'Analytics, plus étroit que ce que la base contient.
#: Demander un sport ou un marché hors périmètre est REFUSÉ, jamais ignoré en
#: silence : recevoir zéro ligne sous un en-tête normal est le mode de panne
#: que tout ce module existe pour empêcher.
SPORTS_AUTORISES = frozenset(SPORTS_ANALYTICS)
MARCHES_AUTORISES = frozenset(MARCHES_ANALYTICS)

#: Alias de commodité. `kambi` se déplie en Unibet + 711 + Bingoal + Scooore.
#: Le groupe vient de `perimetre.GROUPES_JUMEAUX`, qui le lit lui-même dans
#: `reference.KAMBI_BOOKS` — une seule dérivation, jamais une recopie.
#:
#: ⚠️ L'ALIAS N'EST PLUS LE SEUL CHEMIN. Cocher n'importe lequel des quatre
#: jumeaux déplie désormais le groupe entier (voir `valider`) : il fallait
#: taper « kambi » pour obtenir le lot complet, et cocher « Scooore » rendait
#: un sous-ensemble arbitraire du même prix.
ALIAS_BOOKS = {"kambi": GROUPES_JUMEAUX[0]}

JOUE_VALEURS = ("tous", "oui", "non")

FORMAT_JOUR = "%Y-%m-%d"


class FiltreInvalide(ValueError):
    """Un critère que l'utilisateur doit corriger — pas un bug du serveur.

    Distincte de `ValueError` pour que l'API puisse la rendre en 400 et non
    en 500 : une borne à l'envers est une faute de saisie, et la traiter comme
    une panne interne cacherait à l'utilisateur ce qu'il doit changer."""


def _jour(valeur, nom: str) -> "str | None":
    """Valide un `AAAA-MM-JJ` et le rend tel quel.

    On garde la CHAÎNE et non un datetime : `detected_at` est stocké en ISO
    et les comparaisons se font lexicographiquement en SQL, ce qui est exact
    pour de l'ISO et laisse l'index `idx_vb_detected` utilisable. Convertir
    en epoch obligerait à calculer sur chaque ligne et tuerait l'index."""
    if valeur in (None, ""):
        return None
    texte = str(valeur).strip()
    try:
        datetime.strptime(texte, FORMAT_JOUR)
    except ValueError:
        raise FiltreInvalide(
            f"{nom} doit s'écrire AAAA-MM-JJ (exemple : 2026-08-01) — "
            f"reçu : {texte!r}")
    return texte


def _nombre(valeur, nom: str) -> "float | None":
    if valeur in (None, ""):
        return None
    try:
        return float(valeur)
    except (TypeError, ValueError):
        raise FiltreInvalide(f"{nom} doit être un nombre — reçu : {valeur!r}")


def _liste(valeur) -> tuple:
    """Accepte une liste, un tuple, ou une chaîne séparée par des virgules.

    Les trois arrivent réellement : JSON rend des listes, une query string
    rend « a,b », et un appel Python direct rend un tuple."""
    if valeur in (None, "", (), []):
        return ()
    if isinstance(valeur, str):
        morceaux = valeur.split(",")
    else:
        morceaux = list(valeur)
    return tuple(m.strip() for m in (str(x) for x in morceaux) if m.strip())


def _bandes_ev(valeur, ou: str) -> tuple:
    """Valide une sélection de bandes d'EV et la rend dans l'ordre canonique.

    ⚠️ L'ORDRE EST NORMALISÉ pour que deux sélections équivalentes produisent
    le même `Filtres`, donc la même clé si l'analyse est un jour enregistrée
    ou mise en cache. « 15-35 %, 5-8 % » et « 5-8 %, 15-35 % » sont la même
    demande et doivent se sérialiser pareil."""
    from .perimetre import ordre_ev
    choisies = _liste(valeur)
    if not choisies:
        return ()
    connues = list(BORNES_EV)
    for b in choisies:
        if b not in BORNES_EV:
            raise FiltreInvalide(
                f"Bande d'EV inconnue dans {ou} : {b!r}. "
                f"Connues : {', '.join(connues)}.")
    vues = set(choisies)
    canonique = [b for b in ordre_ev() if b in vues]
    # Une bande connue de `BORNES_EV` mais absente de l'ordre du moteur
    # s'ajoute en queue plutôt que de disparaître — même règle que les
    # découpes du service : rien ne sort d'un total sans le dire.
    return tuple(canonique + [b for b in connues if b in vues
                              and b not in canonique])


def _bandes_cote(valeur, ou: str) -> tuple:
    """Valide une sélection de bandes de COTE et la rend dans l'ordre canonique.

    Jumelle de `_bandes_ev`, sur la table de `perimetre.bornes_cote()` — celle
    qui construit déjà les lignes de la matrice cote × EV."""
    from .perimetre import bornes_cote, ordre_cote
    table = bornes_cote()
    choisies = _liste(valeur)
    if not choisies:
        return ()
    connues = list(table)
    for b in choisies:
        if b not in table:
            raise FiltreInvalide(
                f"Bande de cote inconnue dans {ou} : {b!r}. "
                f"Connues : {', '.join(connues)}.")
    vues = set(choisies)
    canonique = [b for b in ordre_cote() if b in vues]
    return tuple(canonique + [b for b in connues if b in vues
                              and b not in canonique])


def _cle_sport(valeur, ou: str) -> str:
    """Un sport canonique, ou un refus. Le périmètre s'applique ici aussi."""
    cle = str(valeur).strip().lower()
    if cle not in SPORTS_AUTORISES:
        raise FiltreInvalide(
            f"{ou} : sport hors périmètre {cle!r}. Autorisés : "
            f"{', '.join(sorted(SPORTS_AUTORISES))}.")
    return cle


def _cle_bande_cote(valeur, ou: str) -> str:
    """Une bande de cote canonique, depuis son LIBELLÉ ou depuis son SLUG.

    Les deux écritures arrivent réellement : « 1.0-1.8 » dans un document
    JSON, « 1_0_1_8 » dans une query string, où le libellé devrait être
    encodé. Elles désignent la même tranche et doivent donc se normaliser
    vers la même clé — sinon la même règle, écrite des deux façons,
    compterait pour deux et se ferait refuser comme un doublon."""
    from .perimetre import bornes_cote, slug_cote, slugs_cote
    brut = str(valeur).strip()
    table = bornes_cote()
    if brut in table:
        return brut
    slugs = slugs_cote()
    cle = slug_cote(brut)
    if cle in slugs:
        return slugs[cle]
    raise FiltreInvalide(
        f"{ou} : bande de cote inconnue {brut!r}. Connues : "
        f"{', '.join(table)} (ou leurs formes d'URL : "
        f"{', '.join(slugs)}).")


def _table_valide(brut, nom: str, cle_valide, valideur, quoi: str) -> list:
    """Valide une table « clé -> règle », quelle que soit la nature de la clé.

    ⚠️ FACTORISÉ ENTRE L'EV, LA COTE ET LES TRANCHES DE COTE, ET PAS PAR GOÛT
    DE LA CONCISION. Des boucles jumelles finissent toujours par diverger —
    sur un message, sur le traitement d'une entrée vide, sur le refus d'un
    doublon — et cet écart ne se voit pas : les filtres marchent tous,
    simplement pas pareil.

    `cle_valide` est le SEUL point qui change d'une table à l'autre. `nom`
    porte le libellé attendu par l'appelant pour que les messages restent ceux
    que ses tests verrouillent, et `quoi` nomme la clé dans ces messages.
    """
    out, vus = [], set()
    brut = brut.items() if isinstance(brut, dict) else brut
    for paire in brut or ():
        try:
            cle_brute, valeurs = paire
        except (TypeError, ValueError):
            raise FiltreInvalide(
                f"{nom} attend des paires ({quoi}, règle) — "
                f"reçu : {paire!r}") from None
        cle = cle_valide(cle_brute, nom)
        if cle in vus:
            raise FiltreInvalide(
                f"{nom} : le {quoi} {cle!r} apparaît deux fois. Une "
                f"seule règle par {quoi}, sinon laquelle s'applique ?")
        vus.add(cle)
        choisies = valideur(valeurs, f"{nom}[{cle}]")
        # Une entrée VIDE est retirée plutôt que conservée : « aucune bande
        # cochée pour le tennis » veut dire « pas de règle propre au tennis »,
        # et non « aucune opportunité de tennis ». Conserver un tuple vide
        # ferait rendre zéro ligne de tennis en silence.
        if choisies:
            out.append((cle, choisies))
    return out


def _par_sport_valide(brut, nom: str, valideur) -> list:
    """Table « sport -> règle ». Corps partagé, voir `_table_valide`."""
    return _table_valide(brut, nom, _cle_sport, valideur, "sport")


def _par_cote_valide(brut, nom: str, valideur) -> list:
    """Table « tranche de cote -> règle ». Même corps que par sport, et c'est
    ce qui garantit que les deux refusent exactement les mêmes choses."""
    return _table_valide(brut, nom, _cle_bande_cote, valideur, "tranche")


def _bornes_ev(valeur, ou: str) -> tuple:
    """Valide un couple de bornes libres d'EV et le rend `(min, max)`.

    Rend `()` quand les deux sont absentes : une règle vide est retirée, pas
    conservée — même convention que les bandes. Un `(None, None)` conservé
    ferait exister une règle qui ne filtre rien, et le sport concerné
    n'hériterait plus de la règle globale sans que personne l'ait voulu."""
    if valeur in (None, (), []):
        return ()
    try:
        lo, hi = valeur
    except (TypeError, ValueError):
        raise FiltreInvalide(
            f"{ou} attend un couple (min, max) — reçu : {valeur!r}") from None
    lo = _nombre(lo, f"{ou}[min]")
    hi = _nombre(hi, f"{ou}[max]")
    if lo is None and hi is None:
        return ()
    if lo is not None and hi is not None and lo > hi:
        raise FiltreInvalide(
            f"{ou} : borne basse {lo} au-dessus de la haute {hi}.")
    return (lo, hi)


#: Préfixes des bornes libres d'EV par sport : `?ev_min_soccer=5&ev_max_soccer=15`.
PREFIXE_EV_MIN_SPORT = "ev_min_"
PREFIXE_EV_MAX_SPORT = "ev_max_"


def _ev_libre_par_sport_depuis(d: dict) -> tuple:
    """Les bornes libres d'EV par sport, dans leurs deux écritures.

    * `ev_free_by_sport` : un dict `{sport: [min, max]}` — ce que rend
      `en_dict`, donc ce qu'un aller-retour JSON doit savoir relire ;
    * `ev_min_<sport>` / `ev_max_<sport>` : les paramètres plats, seule forme
      qu'une query string porte sans encoder du JSON dans une URL.
    """
    out: dict = {}
    table = d.get("ev_free_by_sport") or d.get("ev_libre_par_sport") or {}
    if isinstance(table, dict):
        for sport, bornes in table.items():
            out[str(sport).strip().lower()] = tuple(bornes or (None, None))
    for cle, valeur in d.items():
        nom = str(cle)
        for prefixe, rang in ((PREFIXE_EV_MIN_SPORT, 0),
                              (PREFIXE_EV_MAX_SPORT, 1)):
            # ⚠️ `ev_min` et `ev_max` NUS ne doivent pas être lus ici : ce
            # sont les bornes GLOBALES, et les prendre pour un sport nommé ""
            # les ferait disparaître de la règle globale.
            if nom.startswith(prefixe) and len(nom) > len(prefixe):
                sport = nom[len(prefixe):].strip().lower()
                if sport:
                    courant = list(out.get(sport, (None, None)))
                    courant[rang] = valeur
                    out[sport] = tuple(courant)
    return tuple(sorted((s, b) for s, b in out.items()))


#: Préfixes des bornes libres d'EV par TRANCHE DE COTE :
#: `?ev_odds_min_1_0_1_8=3&ev_odds_max_1_0_1_8=12`.
#:
#: ⚠️ PAS `ev_min_<tranche>`. Ce nom commencerait par `PREFIXE_EV_MIN_SPORT`,
#: et le lecteur par sport y verrait un sport nommé « odds_1_0_1_8 » — refusé
#: comme hors périmètre, donc une requête légitime rejetée. Deux familles de
#: paramètres dont l'une préfixe l'autre ne cohabitent qu'avec une exception
#: écrite quelque part ; on évite l'exception plutôt que de l'écrire.
PREFIXE_EV_MIN_COTE = "ev_odds_min_"
PREFIXE_EV_MAX_COTE = "ev_odds_max_"


def _ev_libre_par_cote_depuis(d: dict) -> tuple:
    """Les bornes libres d'EV par tranche de cote, dans leurs deux écritures.

    * `ev_free_by_odds` : un dict `{tranche: [min, max]}` — ce que rend
      `en_dict`, donc ce qu'un aller-retour JSON doit savoir relire ;
    * `ev_odds_min_<slug>` / `ev_odds_max_<slug>` : les paramètres plats, la
      seule forme qu'une query string porte sans encoder du JSON dans une URL.

    Les clés ne sont PAS canonisées ici : `_cle_bande_cote` s'en charge à la
    validation. Écrire la même tranche sous ses deux formes produit donc deux
    entrées, que la validation refuse comme un doublon — c'est voulu, « qui
    l'emporte » n'a pas de réponse évidente."""
    out: dict = {}
    table = d.get("ev_free_by_odds") or d.get("ev_libre_par_cote") or {}
    if isinstance(table, dict):
        for bande, bornes in table.items():
            out[str(bande).strip()] = tuple(bornes or (None, None))
    for cle, valeur in d.items():
        nom = str(cle)
        for prefixe, rang in ((PREFIXE_EV_MIN_COTE, 0),
                              (PREFIXE_EV_MAX_COTE, 1)):
            if nom.startswith(prefixe) and len(nom) > len(prefixe):
                bande = nom[len(prefixe):].strip()
                if bande:
                    courant = list(out.get(bande, (None, None)))
                    courant[rang] = valeur
                    out[bande] = tuple(courant)
    return tuple(sorted((b, v) for b, v in out.items()))


#: Préfixe des paramètres d'EV par sport dans une query string :
#: `?ev_bands_soccer=5-8%&ev_bands_soccer=8-15%&ev_bands_tennis=15-35%`.
PREFIXE_EV_SPORT = "ev_bands_"

#: Idem pour les bandes de COTE : `?odds_bands_soccer=1.8-2.3`.
PREFIXE_COTE_SPORT = "odds_bands_"


def _par_sport_depuis_generique(d: dict, prefixe: str, cle_dict: str,
                                cle_alt: str) -> tuple:
    """Lit une table « sport -> bandes » sous ses DEUX écritures.

    Voir `_par_sport_depuis` pour le contrat ; ce corps est partagé entre
    l'EV et la cote, pour la même raison que `_par_sport_valide`."""
    out: dict = {}
    table = d.get(cle_dict) or d.get(cle_alt) or {}
    if isinstance(table, dict):
        for sport, bandes in table.items():
            out[str(sport).strip().lower()] = _liste(bandes)
    else:
        for paire in table:
            try:
                sport, bandes = paire
            except (TypeError, ValueError):
                raise FiltreInvalide(
                    f"{cle_dict} attend des paires (sport, bandes) — "
                    f"reçu : {paire!r}") from None
            out[str(sport).strip().lower()] = _liste(bandes)
    for cle, valeur in d.items():
        nom = str(cle)
        if nom.startswith(prefixe) and nom != cle_dict:
            sport = nom[len(prefixe):].strip().lower()
            if sport:
                out[sport] = _liste(valeur)
    return tuple(sorted((s, b) for s, b in out.items()))


def _cote_par_sport_depuis(d: dict) -> tuple:
    """Les bandes de COTE par sport, mêmes écritures que pour l'EV."""
    return _par_sport_depuis_generique(
        d, PREFIXE_COTE_SPORT, "odds_bands_by_sport", "cote_par_sport")


def _par_sport_depuis(d: dict) -> tuple:
    """Lit l'EV par sport sous ses DEUX écritures, sans en privilégier une.

    * `ev_bands_by_sport` : un dict — ce que rend `en_dict`, donc ce qu'un
      aller-retour JSON doit savoir relire.
    * `ev_bands_<sport>` : des paramètres plats — la seule forme qu'une query
      string sache porter sans encoder du JSON dans une URL.

    Les deux se cumulent, la forme plate l'emportant sur le dict pour un même
    sport : elle est la plus explicite des deux dans une requête écrite à la
    main."""
    return _par_sport_depuis_generique(
        d, PREFIXE_EV_SPORT, "ev_bands_by_sport", "ev_par_sport")


@dataclass(frozen=True)
class Filtres:
    """Les critères d'une analyse. Immuable : deux découpes d'une même analyse
    partent du même objet et ne peuvent pas se le modifier l'une l'autre."""

    sports: tuple = ()
    books: tuple = ()
    markets: tuple = ()
    leagues: tuple = ()
    cote_min: "float | None" = None
    cote_max: "float | None" = None
    ev_min: "float | None" = None
    ev_max: "float | None" = None
    #: Bandes d'EV cochées, en UNION (OR). Vide = aucune contrainte de bande.
    #: Se COMBINE avec `ev_min`/`ev_max`, qui restent des bornes globales : les
    #: deux se cumulent en ET, ce qui permet « les bandes 8-15 et 15-35, mais
    #: pas en dessous de 10 % » sans inventer une troisième syntaxe.
    ev_bandes: tuple = ()
    #: EV PAR SPORT — le cœur de la Phase 4. Tuple de paires
    #: `(sport, (bandes…))`, et non un dict : un `Filtres` est gelé, et un
    #: dict muté après validation contournerait silencieusement la
    #: vérification. Un sport absent de cette table retombe sur la sélection
    #: globale ci-dessus.
    ev_par_sport: tuple = ()
    #: Bandes de COTE cochées, en UNION (OR). Même contrat que `ev_bandes` :
    #: elles se cumulent en ET avec `cote_min`/`cote_max`, qui restent des
    #: bornes libres. « Les bandes 1.8-2.3 et 2.3-3.0, mais pas sous 2.00 »
    #: s'exprime donc sans inventer une troisième syntaxe.
    cote_bandes: tuple = ()
    #: Bandes de cote PAR SPORT, forme et règles identiques à `ev_par_sport` :
    #: tuple de paires `(sport, (bandes…))`, et un sport absent retombe sur la
    #: sélection globale plutôt que de disparaître.
    cote_par_sport: tuple = ()
    #: Bornes LIBRES d'EV par sport — tuple de paires `(sport, (min, max))`.
    #: Se cumulent en ET avec les bandes du même sport, exactement comme
    #: `ev_min`/`ev_max` se cumulent avec `ev_bandes` au niveau global.
    ev_libre_par_sport: tuple = ()
    #: Bornes LIBRES d'EV par TRANCHE DE COTE — tuple de paires
    #: `(tranche, (min, max))`.
    #:
    #: ⚠️ CELLE-CI PRIME, ELLE NE S'AJOUTE PAS. Un pari dont la cote tombe
    #: dans une tranche réglée ici obéit à CETTE borne et à aucune autre. Une
    #: règle qui s'ajouterait en ET serait un piège : demander « cote 1.0-1.8
    #: → EV ≥ 3 » alors que la borne globale vaut 5 ne rendrait rien de neuf,
    #: le 5 continuant de s'appliquer aux mêmes lignes. L'ordre complet est
    #: tranche de cote, puis sport, puis règle globale — et c'est exactement
    #: ce que `requete.clause_ev_libre` écrit en SQL.
    ev_libre_par_cote: tuple = ()
    date_from: "str | None" = None
    date_to: "str | None" = None
    delai_min_h: "float | None" = None
    delai_max_h: "float | None" = None
    population: Population = Population.DETECTED
    joue: str = "tous"
    #: Le canal dont la porte est rejouée pour ELIGIBLE_FOR_ALERT. None = le
    #: canal premium, comme toutes les analyses du projet depuis juillet.
    canal: "str | None" = None
    #: Minutes de fenêtre morte. None = LUE dans la configuration de
    #: production, jamais supposée.
    fenetre_morte_min: "float | None" = None
    #: Mise notionnelle par pari. Le ROI en dépend, donc elle voyage AVEC les
    #: filtres : un ROI dont on ignore la mise n'est pas reproductible.
    stake: float = 25.0

    # ── Validation ───────────────────────────────────────────────────

    def valider(self) -> "Filtres":
        """Rend un `Filtres` normalisé, ou lève `FiltreInvalide`.

        Normalise AUSSI : déplie l'alias `kambi`, met les books et marchés en
        minuscules, dédoublonne. Une validation qui ne normalise pas laisse
        passer « Unibet_BE » qui ne rapprochera jamais rien en base."""
        books = []
        for b in self.books:
            cle = b.strip().lower()
            if cle in ALIAS_BOOKS:
                books.extend(ALIAS_BOOKS[cle])
            elif cle in BOOKS_CONNUS:
                # ⚠️ UN JUMEAU EN DÉPLIE QUATRE, ET C'EST LE POINT.
                # Unibet, 711, Bingoal et Scooore servent le même flux Kambi.
                # Les garder séparés faisait dépendre le résultat du jumeau
                # coché, alors que le prix est identique — et éclatait les
                # effectifs en quatre lots trop petits pour conclure.
                # `jumeaux_de` rend `(cle,)` pour un book sans jumeau.
                books.extend(jumeaux_de(cle))
            else:
                # ⚠️ Un book inconnu ne doit PAS être ignoré en silence : la
                # requête rendrait un lot amputé sous un en-tête normal.
                raise FiltreInvalide(
                    f"Bookmaker inconnu : {b!r}. Connus : "
                    + ", ".join(sorted(BOOKS_CONNUS))
                    + f". Alias : {', '.join(sorted(ALIAS_BOOKS))}.")
        books = tuple(dict.fromkeys(books))

        marches = []
        for m in self.markets:
            cle = m.strip().lower()
            if cle not in MARCHES_CONNUS:
                raise FiltreInvalide(
                    f"Marché inconnu : {m!r}. Connus : "
                    + ", ".join(sorted(MARCHES_CONNUS)) + ".")
            if cle not in MARCHES_AUTORISES:
                # ⚠️ REFUSÉ, pas ignoré. `clv.settle` ne sait régler ni les
                # handicaps, ni le BTTS, ni la mi-temps : ces marchés sortent
                # toujours NON RÉGLÉS. Les accepter rendrait un lot dont le
                # ROI serait structurellement vide, sous un en-tête normal.
                raise FiltreInvalide(
                    f"Marché hors périmètre Analytics : {m!r}. L'Analytics ne "
                    f"traite que {', '.join(sorted(MARCHES_AUTORISES))} — les "
                    f"autres marchés ne sont jamais réglés par `clv.settle`, "
                    f"donc aucun ROI ne peut en sortir. Les données restent "
                    f"en base, seule l'analyse les écarte.")
            marches.append(cle)
        marches = tuple(dict.fromkeys(marches))

        # Les ligues sont un ensemble OUVERT : elles viennent des données, pas
        # d'une énumération. On normalise sans juger — mais une chaîne vide est
        # refusée, parce qu'elle ne rapprocherait rien tout en ayant l'air d'un
        # filtre. Les SPORTS, eux, sont bornés par le périmètre Analytics.
        sports = tuple(dict.fromkeys(s.strip().lower() for s in self.sports))
        for s in sports:
            if s not in SPORTS_AUTORISES:
                raise FiltreInvalide(
                    f"Sport hors périmètre Analytics : {s!r}. L'Analytics ne "
                    f"traite que {', '.join(sorted(SPORTS_AUTORISES))} — "
                    f"aucune source de résultats n'existe pour les autres "
                    f"(mesuré : 0 résultat sur 501 matchs de basket, 95 de "
                    f"volley, 21 de hockey). Les données restent en base.")
        leagues = tuple(dict.fromkeys(g.strip() for g in self.leagues))

        bandes = _bandes_ev(self.ev_bandes, "ev_bandes")
        # EV par sport : chaque entrée est validée comme une sélection à part
        # entière, et son sport doit lui aussi être dans le périmètre.
        par_sport = tuple(sorted(_par_sport_valide(
            self.ev_par_sport, "ev_par_sport", _bandes_ev)))

        ev_libre = tuple(sorted(_par_sport_valide(
            self.ev_libre_par_sport, "ev_libre_par_sport", _bornes_ev)))

        # Les tranches sont rangées dans l'ORDRE DE LA MATRICE, pas par ordre
        # alphabétique : « > 6.0 » se trierait avant « 1.0-1.8 » et l'export
        # PDF lirait les règles à l'envers de la table qu'il accompagne.
        from .perimetre import ordre_cote
        rang = {b: i for i, b in enumerate(ordre_cote())}
        ev_libre_cote = tuple(sorted(
            _par_cote_valide(self.ev_libre_par_cote, "ev_libre_par_cote",
                             _bornes_ev),
            key=lambda paire: rang.get(paire[0], len(rang))))

        # Bandes de COTE : mêmes règles, même aide, donc mêmes refus.
        bandes_cote = _bandes_cote(self.cote_bandes, "cote_bandes")
        cote_par_sport = tuple(sorted(_par_sport_valide(
            self.cote_par_sport, "cote_par_sport", _bandes_cote)))

        depuis = _jour(self.date_from, "date_from")
        jusqu = _jour(self.date_to, "date_to")
        if depuis and jusqu and depuis > jusqu:
            raise FiltreInvalide(
                f"Période vide par construction : date_from ({depuis}) est "
                f"APRÈS date_to ({jusqu}).")

        cote_min = _nombre(self.cote_min, "cote_min")
        cote_max = _nombre(self.cote_max, "cote_max")
        # Une cote décimale est strictement supérieure à 1 : « 1,00 » est un
        # pari sans gain, et une cote négative n'existe pas.
        for nom, v in (("cote_min", cote_min), ("cote_max", cote_max)):
            if v is not None and v <= 1.0:
                raise FiltreInvalide(
                    f"{nom} doit dépasser 1,00 — une cote décimale vaut "
                    f"toujours plus que 1. Reçu : {v}")
        if cote_min is not None and cote_max is not None and cote_min > cote_max:
            raise FiltreInvalide(
                f"Bande de cotes à l'envers : {cote_min} → {cote_max}.")

        ev_min = _nombre(self.ev_min, "ev_min")
        ev_max = _nombre(self.ev_max, "ev_max")
        if ev_min is not None and ev_max is not None and ev_min > ev_max:
            raise FiltreInvalide(
                f"Bande d'EV à l'envers : {ev_min} → {ev_max}.")

        d_min = _nombre(self.delai_min_h, "delai_min_h")
        d_max = _nombre(self.delai_max_h, "delai_max_h")
        if d_min is not None and d_max is not None and d_min > d_max:
            raise FiltreInvalide(
                f"Bande de délai à l'envers : {d_min} h → {d_max} h.")

        if self.joue not in JOUE_VALEURS:
            raise FiltreInvalide(
                f"joue doit valoir {' | '.join(JOUE_VALEURS)} — "
                f"reçu : {self.joue!r}")

        population = (self.population if isinstance(self.population, Population)
                      else Population(str(self.population).lower()))

        stake = _nombre(self.stake, "stake") or 0.0
        if stake <= 0:
            raise FiltreInvalide(f"stake doit être positive — reçu : {stake}")

        fm = _nombre(self.fenetre_morte_min, "fenetre_morte_min")
        if fm is not None and fm < 0:
            raise FiltreInvalide(
                f"fenetre_morte_min ne peut pas être négative — reçu : {fm}")

        return replace(
            self, sports=sports, books=books, markets=marches, leagues=leagues,
            date_from=depuis, date_to=jusqu, cote_min=cote_min,
            cote_max=cote_max, ev_min=ev_min, ev_max=ev_max,
            ev_bandes=bandes, ev_par_sport=par_sport,
            cote_bandes=bandes_cote, cote_par_sport=cote_par_sport,
            ev_libre_par_sport=ev_libre, ev_libre_par_cote=ev_libre_cote,
            delai_min_h=d_min, delai_max_h=d_max, population=population,
            stake=stake, fenetre_morte_min=fm)

    # ── Les règles d'EV, résolues ────────────────────────────────────

    def ev_effectif(self) -> dict:
        """Ce que l'EV filtre RÉELLEMENT, sport par sport.

        Rend `{sport: (bandes…)}` pour les sports qui ont une règle propre, et
        la clé `None` pour la règle globale qui s'applique à tous les autres.
        C'est cette table que le SQL traduit, et c'est elle qu'on affiche à
        l'utilisateur : une règle par sport qu'on ne peut pas RELIRE serait
        exactement le genre de filtre qu'on croit appliqué et qui ne l'est
        pas."""
        table = {None: self.ev_bandes}
        table.update(dict(self.ev_par_sport))
        return table

    def cote_effectif(self) -> dict:
        """Ce que la COTE filtre réellement, sport par sport.

        Même contrat que `ev_effectif` : la clé `None` porte la règle
        globale, celle qui s'applique à tout sport sans règle propre."""
        table = {None: self.cote_bandes}
        table.update(dict(self.cote_par_sport))
        return table

    def ev_libre_cote_effectif(self) -> dict:
        """Ce que l'EV filtre réellement, TRANCHE DE COTE par tranche.

        Rend `{tranche: (min, max)}` pour les seules tranches réglées. Une
        tranche absente n'est pas « sans contrainte » : elle retombe sur la
        règle du sport, puis sur `ev_min`/`ev_max`. Même contrat de lecture
        que `ev_effectif` — un filtre qu'on ne peut pas RELIRE est exactement
        celui qu'on croit appliqué et qui ne l'est pas."""
        return dict(self.ev_libre_par_cote)

    def sports_effectifs(self) -> tuple:
        """Les sports réellement analysés : la sélection, ou tout le périmètre.

        ⚠️ Rien de coché ne veut PAS dire « tous les sports de la base » : le
        périmètre Analytics s'applique quand même. C'est la seule lecture qui
        rende le total du tableau égal à la somme de ses tranches."""
        return self.sports or tuple(SPORTS_ANALYTICS)

    # ── Bornes dérivées ──────────────────────────────────────────────

    def borne_haute_exclusive(self) -> "str | None":
        """`date_to` + 1 jour, pour que la JOURNÉE ENTIÈRE soit comprise.

        ⚠️ Couper à `date_to` comparerait « 2026-09-08T23:59 » à
        « 2026-09-08 » et jetterait la dernière journée en silence. Personne
        ne compte les lignes qu'il ne voit pas."""
        if not self.date_to:
            return None
        veille = datetime.strptime(self.date_to, FORMAT_JOUR) + timedelta(days=1)
        return veille.strftime(FORMAT_JOUR)

    # ── Sérialisation ────────────────────────────────────────────────

    def en_dict(self) -> dict:
        """Un document JSON pur — listes, nombres, chaînes. Aucune valeur
        exotique : c'est ce qui rendra « enregistrer cette analyse » trivial."""
        return {
            "sports": list(self.sports), "bookmakers": list(self.books),
            "markets": list(self.markets), "leagues": list(self.leagues),
            "odds_min": self.cote_min, "odds_max": self.cote_max,
            "ev_min": self.ev_min, "ev_max": self.ev_max,
            "ev_bands": list(self.ev_bandes),
            "ev_bands_by_sport": {s: list(b) for s, b in self.ev_par_sport},
            "ev_free_by_sport": {s: list(b)
                                 for s, b in self.ev_libre_par_sport},
            "ev_free_by_odds": {b: list(v)
                                for b, v in self.ev_libre_par_cote},
            "odds_bands": list(self.cote_bandes),
            "odds_bands_by_sport": {s: list(b)
                                    for s, b in self.cote_par_sport},
            "date_from": self.date_from, "date_to": self.date_to,
            "delay_min": self.delai_min_h, "delay_max": self.delai_max_h,
            "population": self.population.value, "played": self.joue,
            "canal": self.canal, "dead_window_min": self.fenetre_morte_min,
            "stake": self.stake,
        }

    @classmethod
    def depuis_dict(cls, d: dict) -> "Filtres":
        """Construit depuis les noms de l'API. Accepte le singulier ET le
        pluriel (`sport` / `sports[]`) : les deux arrivent réellement d'une
        query string, et en refuser un serait un piège pour rien."""
        d = dict(d or {})

        def multi(*noms):
            out: list = []
            for n in noms:
                out.extend(_liste(d.get(n)))
            return tuple(dict.fromkeys(out))

        return cls(
            sports=multi("sport", "sports", "sports[]"),
            books=multi("bookmaker", "bookmakers", "bookmakers[]", "books"),
            markets=multi("market", "markets", "markets[]"),
            leagues=multi("league", "leagues", "leagues[]"),
            cote_min=d.get("odds_min", d.get("cote_min")),
            cote_max=d.get("odds_max", d.get("cote_max")),
            ev_min=d.get("ev_min"), ev_max=d.get("ev_max"),
            ev_bandes=multi("ev_band", "ev_bands", "ev_bands[]", "ev_bandes"),
            ev_par_sport=_par_sport_depuis(d),
            cote_bandes=multi("odds_band", "odds_bands", "odds_bands[]",
                              "cote_bandes"),
            cote_par_sport=_cote_par_sport_depuis(d),
            ev_libre_par_sport=_ev_libre_par_sport_depuis(d),
            ev_libre_par_cote=_ev_libre_par_cote_depuis(d),
            date_from=d.get("date_from"), date_to=d.get("date_to"),
            delai_min_h=d.get("delay_min", d.get("delai_min_h")),
            delai_max_h=d.get("delay_max", d.get("delai_max_h")),
            population=Population(str(d.get("population", "detected")).lower()),
            joue=str(d.get("played", d.get("joue", "tous"))).lower(),
            canal=d.get("canal") or None,
            fenetre_morte_min=d.get("dead_window_min"),
            stake=d.get("stake", 25.0),
        )
