"""Résultats de matchs — la seule règle de jugement qui ne dépende d'aucune référence.

Tout le reste du projet mesure la CLV. Battre la ligne de clôture prouve l'edge,
mais ne dit pas un euro : la table `results` est restée à zéro ligne depuis
juillet, donc aucun P&L réel n'existe. Ce module est le chaînon manquant.

Il ne parle à AUCUNE API. Il reçoit des résultats déjà normalisés
(`MatchResult`) et les rattache aux événements du projet ; le fournisseur vit
derrière `ScoreProvider`. C'est le même parti que `build_fair_lines`, qui
accepte une source secondaire sans jamais nommer un book : la source de scores
doit pouvoir changer sans que le rapprochement bouge d'une ligne.

Deux pièges commandent tout ce fichier, et chacun est du type §11 — faux en
silence, sans lever la moindre erreur :

1. **Un match porte plusieurs `event_key`.** Pinnacle révise l'horaire d'un
   match de tennis par pas de 15 minutes, jusqu'à onze clés pour un seul match
   (§17.8, 13,5 % des matchs de tennis). `results` étant clée sur `event_key`,
   un résultat écrit sous une seule clé laisse tous les paris pris sous les
   autres sans P&L — et ils disparaissent de la mesure sans rien signaler. D'où
   le sens du rapprochement : on part de NOS événements et on cherche leur
   résultat, jamais l'inverse. Un résultat se lie ainsi naturellement à autant
   de clés qu'il en existe.

2. **Au tennis, le vainqueur ne se déduit PAS du score.** `home_score` y compte
   les JEUX, et on peut gagner plus de jeux que son adversaire en perdant le
   match — 6-7, 7-6, 7-6 en est l'exemple courant. Le vainqueur doit venir du
   fournisseur ; le déduire fabriquerait des paris notés à l'envers, sans
   qu'aucune exception ne soit levée.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Protocol

from .matcher import match_event, team_similarity, tolerance_for, wide_tolerance_for

# Les sports où le total d'un marché « totals » se déduit du score, donc où le
# vainqueur aussi. Le football en fait partie : plus de buts = victoire. Le
# tennis non, et c'est le piège n°2 de l'en-tête.
_SCORE_DECIDES_WINNER = {"soccer", "football"}


@dataclass(frozen=True)
class MatchResult:
    """Un match TERMINÉ, tel qu'une source de scores le rapporte.

    N'existe que pour un match effectivement joué jusqu'au bout : un match
    reporté, abandonné ou interrompu ne doit jamais produire de `MatchResult`.
    C'est à l'adaptateur du fournisseur de filtrer sur le statut — écrire un
    0-0 d'abandon dans `results` noterait des paris perdants qui ne le sont pas,
    et `settle()` n'a aucun moyen de s'en apercevoir.

    ⚠️ `home_score` / `away_score` portent **l'unité que compte le marché
    "totals" de ce sport**, et rien d'autre :

    | Sport    | unité      | lignes typiques |
    |----------|------------|-----------------|
    | football | buts       | 0,5 à 6,5       |
    | tennis   | **jeux**   | 16,5 à 28       |

    Ce n'est pas un détail de nommage. `settle()` compare `home + away` à la
    ligne du pari ; y mettre des SETS au tennis (2-0, 2-1) noterait chaque
    total de jeux contre un total de sets, donc « under » gagnant à tous les
    coups. C'est exactement la confusion jeux/sets du §19.2, qui n'aurait levé
    aucune erreur là non plus.
    """
    sport: str
    home: str
    away: str
    start_time: datetime
    winner: str | None                 # "home" | "draw" | "away"
    home_score: float | None
    away_score: float | None
    source: str
    source_id: str = ""

    @property
    def gradable(self) -> bool:
        """Ce résultat permet-il de noter au moins un pari ?

        Le vainqueur seul suffit pour un h2h ; les totaux exigent les deux
        scores. Un résultat qui n'a ni l'un ni l'autre n'a rien à faire en base
        — il occuperait la clé et empêcherait un meilleur relevé de la prendre.
        """
        return self.winner is not None or (
            self.home_score is not None and self.away_score is not None
        )


class ScoreProvider(Protocol):
    """Une source de scores, vue par le reste du projet.

    Volontairement minuscule : tout ce qui est propre à un fournisseur — clé
    d'API, pagination, noms de champs, filtrage des statuts — reste derrière
    cette frontière. Le jour où la source change, rien de ce fichier ne bouge.
    """

    name: str

    def fetch_results(self, sport: str, day: date) -> list[MatchResult]:
        """Les matchs TERMINÉS de ce sport ce jour-là. Jamais les autres."""
        ...


def winner_from_scores(sport: str, home_score: float | None,
                       away_score: float | None) -> str | None:
    """Déduit le vainqueur du score — uniquement là où c'est licite.

    Renvoie None au tennis même avec deux scores en main, et c'est le
    comportement voulu : `home_score` y compte les jeux, dont le total ne
    décide pas du match. Un fournisseur qui ne donne pas le vainqueur d'un match
    de tennis ne permet donc pas de noter ses h2h, et il vaut mieux le savoir
    que le deviner.
    """
    if sport.lower() not in _SCORE_DECIDES_WINNER:
        return None
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


@dataclass(frozen=True)
class OurEvent:
    """Un événement du projet, réduit à ce que le rapprochement lit.

    Porte sa `event_key` parce que c'est elle qu'on écrira dans `results`, et
    les noms BRUTS — pas normalisés : `team_similarity` normalise lui-même, et
    les noms compactés de la clé (`tallongriekspoor`) sont précisément ce qui
    avait fait perdre tout le tennis de deux books au §15.4.
    """
    event_key: str
    home: str
    away: str
    start_time: datetime


def tolerance_for_scores(sport: str | None) -> int:
    """Fenêtre d'horaire admise entre un de nos événements et un résultat.

    Reprend telles quelles les tolérances de `matcher` — 12 h au tennis, 10 min
    au football — plutôt que d'en inventer de nouvelles. Le tennis en a besoin
    pour la même raison qu'entre books : l'heure n'y est qu'une estimation. Le
    football garde des horaires fermes.

    Élargir reste sûr parce que **le nom est le juge** : dans une même journée,
    deux équipes ne se rencontrent qu'une fois. Et la garde d'ambiguïté de
    `match_event` se durcit à mesure que la fenêtre grandit, puisqu'elle voit
    plus de candidats concurrents.

    ⚠️ Si la sonde montre que le football rate des matchs pour quelques minutes
    d'écart, c'est ICI qu'il faut le corriger, avec le chiffre en main — et pas
    en relâchant `min_score`, qui lui ferait apparier des équipes différentes.
    """
    return wide_tolerance_for(sport) or tolerance_for(sport)


def _is_swapped(ev: "OurEvent", res: MatchResult) -> bool:
    """L'appariement a-t-il retenu l'orientation inverse ?

    On compare la ressemblance de NOTRE domicile avec chacun des deux camps du
    résultat. Si elle est meilleure avec le second, c'est que la source ordonne
    les joueurs dans l'autre sens.

    En cas d'égalité parfaite on répond False — ne rien changer est le choix
    sûr : inverser sur une hésitation créerait l'erreur qu'on cherche à éviter.
    """
    direct = (team_similarity(ev.home, res.home) + team_similarity(ev.away, res.away))
    swap = (team_similarity(ev.home, res.away) + team_similarity(ev.away, res.home))
    return swap > direct


def _flip(res: MatchResult) -> MatchResult:
    """Remettre un résultat dans NOTRE ordre : camps, scores et vainqueur.

    Les trois doivent bouger ensemble. N'en retourner que deux laisserait un
    résultat cohérent en apparence et faux en profondeur — exactement le genre
    d'erreur que rien ne signale ensuite.
    """
    from dataclasses import replace
    flipped_winner = {"home": "away", "away": "home"}.get(res.winner or "", res.winner)
    return replace(
        res, home=res.away, away=res.home,
        home_score=res.away_score, away_score=res.home_score,
        winner=flipped_winner,
    )


def bind_results(
    events: Iterable[OurEvent],
    results: Iterable[MatchResult],
    *,
    sport: str,
    min_score: float = 85.0,
) -> tuple[list[tuple[str, MatchResult]], dict[str, int]]:
    """Rattache un résultat à chacun de nos événements, quand il en existe un.

    Sens du parcours : **on part de nos événements**. C'est ce qui fait qu'un
    même résultat se lie à toutes les `event_key` d'un match dont l'horaire a
    été révisé (§17.8) — le piège n°1 de l'en-tête. L'inverse en lierait une
    seule, choisie arbitrairement, et perdrait les paris pris sous les autres.

    Renvoie les liens ET des compteurs de rejet. Les compteurs ne sont pas un
    confort : sans eux, « la source ne couvre pas ce match » et « le
    rapprochement échoue » donnent le même résultat visible — rien — et c'est
    le mode de défaillance dominant du projet (§13.12).
    """
    candidates = [r for r in results if r.sport == sport]
    counters = {
        "lies": 0,
        "sans_candidat": 0,      # aucun résultat proche : la source ne l'a pas
        "resultat_inutilisable": 0,  # apparié, mais ni vainqueur ni scores
        "orientation_corrigee": 0,   # apparié à l'envers, vainqueur remis d'aplomb
    }
    tol = tolerance_for_scores(sport)
    bindings: list[tuple[str, MatchResult]] = []

    for ev in events:
        best = match_event(
            ev, candidates, time_tolerance_minutes=tol, min_score=min_score,
        )
        if best is None:
            counters["sans_candidat"] += 1
            continue
        if not best.gradable:
            counters["resultat_inutilisable"] += 1
            continue
        # ⚠️ `match_event` apparie les deux orientations — « A vs B » et
        # « B vs A » — ce qui est indispensable au tennis, où la notion de
        # domicile n'existe pas et où chaque source ordonne les joueurs à sa
        # guise. Mais il ne DIT PAS laquelle il a retenue.
        #
        # Sans ce contrôle, un résultat apparié à l'envers inscrit le vainqueur
        # du mauvais côté : le pari `home` est noté sur le joueur que NOUS
        # appelons `away`. Rien ne le signale — le score et le vainqueur restent
        # cohérents entre eux, seul leur rattachement à nos noms est faux.
        if _is_swapped(ev, best):
            best = _flip(best)
            counters["orientation_corrigee"] += 1

        bindings.append((ev.event_key, best))
        counters["lies"] += 1

    return bindings, counters
