"""Quels canaux reçoivent ce pari — et rien d'autre.

Ce module est une FONCTION, pas un service. Il ne lit aucune base, ne parle
à aucun réseau, n'écrit nulle part, et n'importe **rien du projet** — pas
même `models`. Ses seules dépendances sont `dataclasses` et `typing`.

Cette dernière contrainte n'est pas de la coquetterie. Le routage décide où
part un pari ; s'il pouvait atteindre `detection` ou `reference`, plus rien
ne garantirait qu'un filtre n'influence pas une EV. En n'important rien, la
garantie est structurelle et vérifiable en une ligne
(`test_routing_import_graph.py`), au lieu de reposer sur une relecture.

Le pari est pris en canard : le module lit `ev_pct`, `odd_taken`, `book` et
`market`, sans exiger un `ValueBet`. Une ligne de base ou une opportunité
LIVE se route donc sans conversion — c'est ce qui rendra possible un
`/test <canal>` rejouant les détections passées.

La forme des règles
-------------------
    ET  entre les critères d'une même règle
    OU  entre les valeurs d'une même dimension
    OU  entre les règles d'un même canal

Une dimension non renseignée vaut « toutes ». Une règle sans aucun critère
vaut donc « tout passe ». Un canal SANS AUCUNE RÈGLE, en revanche, ne prend
rien : un canal qu'on vient de créer et qu'on n'a pas encore configuré doit
rester muet, pas déverser la totalité du flux.

⚠️ L'asymétrie sur les données manquantes
-----------------------------------------
Une INCLUSION sur une dimension inconnue échoue : un « canal tennis » ne
doit pas accepter un pari dont on ignore le sport.

Une EXCLUSION sur une dimension inconnue n'exclut pas : écarter par défaut
supprimerait des paris d'un sport qu'on n'a jamais voulu couper. C'est déjà
la règle appliquée à `premium_hi_sports_exclus` dans l'alerter, et elle suit
le principe retenu partout dans ce projet — un faux positif observable vaut
mieux qu'une suppression silencieuse.

Les deux se testent (`test_une_inclusion_ne_passe_pas_sur_une_dimension_inconnue`
et `test_une_exclusion_n_exclut_pas_sur_une_dimension_inconnue`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

DIMENSIONS = ("sport", "book", "market", "league")
PHASES = ("prematch", "live")


def _norm(valeur: Any) -> Optional[str]:
    """Ramène une valeur comparable à une chaîne normalisée, ou None.

    Un Enum `str` (Book, MarketType) rend son `.value` — obtenu par
    `getattr`, pour ne pas avoir à importer `models` et rouvrir une porte
    vers le moteur."""
    if valeur is None:
        return None
    brut = getattr(valeur, "value", valeur)
    texte = str(brut).strip().lower()
    return texte or None


@dataclass(frozen=True)
class Borne:
    """Une borne numérique, et si elle est stricte.

    Les deux existent dans la configuration réelle : le canal principal
    s'arrête à `EV < main_max_ev_pct` (stricte) tandis que ses bandes de
    cote sont inclusives, et la voie critique grosses cotes commence à
    `cote > 4,0` (stricte) pour ne pas empiéter sur la bande premium qui
    inclut 4,0. Sans ce drapeau, la configuration actuelle ne serait pas
    reproductible à l'identique — et la simulation de non-régression du
    commit 4 échouerait sur les valeurs pile aux bornes."""
    valeur: float
    stricte: bool = False

    @classmethod
    def coerce(cls, x: "float | Borne | None") -> "Borne | None":
        if x is None or isinstance(x, Borne):
            return x
        return cls(float(x))

    def au_moins(self, x: float) -> bool:
        return x > self.valeur if self.stricte else x >= self.valeur

    def au_plus(self, x: float) -> bool:
        return x < self.valeur if self.stricte else x <= self.valeur


@dataclass(frozen=True)
class Critere:
    """Une dimension et ses valeurs. `inclut=False` en fait une exclusion."""
    dimension: str
    valeurs: frozenset
    inclut: bool = True

    def __post_init__(self) -> None:
        if self.dimension not in DIMENSIONS:
            raise ValueError(
                f"dimension inconnue : {self.dimension!r} "
                f"(attendu : {', '.join(DIMENSIONS)})")
        if not self.valeurs:
            # Un critère vide se lirait « aucune valeur n'est acceptée » ou
            # « toutes le sont » selon qui le lit. Les deux lectures sont
            # defendables, donc la structure est ambigue : on la refuse.
            raise ValueError(
                f"critère {self.dimension!r} sans valeur — pour « toutes », "
                f"n'écris pas le critère du tout")
        object.__setattr__(self, "valeurs",
                           frozenset(v for v in (_norm(x) for x in self.valeurs) if v))

    def accepte(self, valeur: Any) -> bool:
        v = _norm(valeur)
        if v is None:
            # Voir l'asymétrie documentée en tête de module.
            return not self.inclut
        return (v in self.valeurs) if self.inclut else (v not in self.valeurs)


@dataclass(frozen=True)
class Regle:
    """Un jeu de conditions, toutes obligatoires (ET)."""
    ev_min: Any = None
    ev_max: Any = None
    odd_min: Any = None
    odd_max: Any = None
    phase: Optional[str] = None          # 'prematch' | 'live' | None = les deux
    criteres: tuple = ()

    def __post_init__(self) -> None:
        for nom in ("ev_min", "ev_max", "odd_min", "odd_max"):
            object.__setattr__(self, nom, Borne.coerce(getattr(self, nom)))
        if self.phase is not None and self.phase not in PHASES:
            raise ValueError(
                f"phase inconnue : {self.phase!r} (attendu : {', '.join(PHASES)} ou None)")
        object.__setattr__(self, "criteres", tuple(self.criteres))
        vues = [c.dimension for c in self.criteres]
        if len(vues) != len(set(vues)):
            # Deux critères sur la même dimension se combineraient en ET, ce
            # qui donne presque toujours l'ensemble vide (« sport=tennis ET
            # sport=soccer ») alors que l'auteur voulait un OU. Le modèle
            # exprime le OU par plusieurs VALEURS dans un seul critère.
            raise ValueError(
                f"deux critères sur la même dimension : {sorted(vues)} — "
                f"un seul critère, plusieurs valeurs")

    def accepte(self, *, ev_pct: float, odd: float, sport, book, market,
                league, is_live: bool) -> bool:
        if self.ev_min is not None and not self.ev_min.au_moins(ev_pct):
            return False
        if self.ev_max is not None and not self.ev_max.au_plus(ev_pct):
            return False
        if self.odd_min is not None and not self.odd_min.au_moins(odd):
            return False
        if self.odd_max is not None and not self.odd_max.au_plus(odd):
            return False
        if self.phase is not None and self.phase != ("live" if is_live else "prematch"):
            return False
        valeurs = {"sport": sport, "book": book, "market": market, "league": league}
        return all(c.accepte(valeurs[c.dimension]) for c in self.criteres)


@dataclass(frozen=True)
class Canal:
    """Une destination, et les règles qui décident ce qu'elle reçoit.

    `priorite` ne sert qu'à ORDONNER la sortie (petit = tôt). Elle ne filtre
    rien.

    `exclusif` : quand un canal exclusif prend le pari, les canaux de
    priorité inférieure ne le reçoivent pas. Le défaut est `False` — canaux
    INDÉPENDANTS, un même pari peut partir dans plusieurs. Le drapeau existe
    parce que la configuration actuelle en dépend : le canal critique ne
    reçoit aujourd'hui que ce qu'aucune bande premium n'a pris, et sans lui
    la simulation de non-régression du commit 4 ne pourrait pas rendre le
    même résultat.

    `profile_id` est prévu pour le multi-utilisateur et n'est lu par
    personne. Il ne participe à aucune décision de ce module."""
    chat_id: str
    nom: str
    regles: tuple = ()
    actif: bool = True
    priorite: int = 100
    exclusif: bool = False
    profile_id: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "regles", tuple(self.regles))

    def accepte(self, **faits) -> bool:
        # Un canal sans règle ne prend RIEN : voir l'en-tête de module.
        return any(r.accepte(**faits) for r in self.regles)


def _ordre(c: Canal) -> tuple:
    """Tri total, indépendant de l'ordre d'entrée. Sans le départage par nom
    puis chat_id, deux canaux de même priorité rendraient un résultat qui
    dépend de la façon dont l'appelant a construit sa liste."""
    return (c.priorite, c.nom, c.chat_id)


def canaux_pour(bet: Any, *, sport: Any = None, league: Any = None,
                is_live: bool = False,
                canaux: Iterable[Canal] = ()) -> list[Canal]:
    """Les canaux qui doivent recevoir ce pari, dans l'ordre de priorité.

    Fonction pure : mêmes entrées, même sortie ; rien n'est modifié, ni le
    pari, ni les canaux, ni la liste reçue.

    Renvoie 0, 1 ou plusieurs canaux. Un pari qui satisfait trois canaux
    part dans les trois — c'est le comportement voulu, pas un doublon."""
    faits = {
        "ev_pct": float(bet.ev_pct),
        "odd": float(bet.odd_taken),
        "sport": sport,
        "book": getattr(bet, "book", None),
        "market": getattr(bet, "market", None),
        "league": league,
        "is_live": bool(is_live),
    }
    retenus: list[Canal] = []
    for canal in sorted(canaux, key=_ordre):
        if not canal.actif:
            continue
        if not canal.accepte(**faits):
            continue
        retenus.append(canal)
        if canal.exclusif:
            break
    return retenus
