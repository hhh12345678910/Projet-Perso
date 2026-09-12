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
from ..reference import KAMBI_BOOKS
from .populations import Population

#: Les books que la base peut contenir. Fermé : il vient de l'énumération du
#: moteur, jamais d'une liste recopiée.
BOOKS_CONNUS = frozenset(b.value for b in Book)

#: Les marchés que le moteur produit. Fermé, même raison.
MARCHES_CONNUS = frozenset(m.value for m in MarketType)

#: Alias de commodité. `kambi` se déplie en Unibet + 711 + Bingoal + Scooore —
#: le groupe est LU dans `reference.KAMBI_BOOKS`, jamais recopié : ces books
#: servent des prix identiques et les séparer fausse les effectifs.
ALIAS_BOOKS = {"kambi": tuple(b.value for b in KAMBI_BOOKS)}

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
                books.append(cle)
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
            marches.append(cle)
        marches = tuple(dict.fromkeys(marches))

        # Les sports et les ligues sont un ensemble OUVERT : ils viennent des
        # données, pas d'une énumération. On normalise sans juger — mais une
        # chaîne vide est refusée, parce qu'elle ne rapprocherait rien tout en
        # ayant l'air d'un filtre.
        sports = tuple(dict.fromkeys(s.strip().lower() for s in self.sports))
        leagues = tuple(dict.fromkeys(g.strip() for g in self.leagues))

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
            delai_min_h=d_min, delai_max_h=d_max, population=population,
            stake=stake, fenetre_morte_min=fm)

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
            date_from=d.get("date_from"), date_to=d.get("date_to"),
            delai_min_h=d.get("delay_min", d.get("delai_min_h")),
            delai_max_h=d.get("delay_max", d.get("delai_max_h")),
            population=Population(str(d.get("population", "detected")).lower()),
            joue=str(d.get("played", d.get("joue", "tous"))).lower(),
            canal=d.get("canal") or None,
            fenetre_morte_min=d.get("dead_window_min"),
            stake=d.get("stake", 25.0),
        )
