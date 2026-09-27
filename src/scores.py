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

import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Iterable, Protocol

from rapidfuzz import fuzz

from .matcher import (_strip_class_tag, class_marker_from_league, match_event,
                      normalize_team, team_similarity, tolerance_for,
                      wide_tolerance_for, with_class_marker)

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
    # ⚠️ La ligue n'est pas décorative : c'est elle qui porte la classe
    # (féminin, jeunes) que Pinnacle NE MET PAS sur le nom d'équipe, alors que
    # les sources de scores, elles, la mettent. Sans elle, la barrière de
    # classe rend tout le football féminin inappariable — voir
    # `class_marker_from_league`. Optionnelle pour ne pas casser les appels
    # existants, mais un appelant qui l'omet perd le féminin et les jeunes.
    league: str = ""


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
    """L'ANCIENNE décision d'orientation, gardée pour la sonde qui retrouve les
    résultats qu'elle a mal orientés (`scripts/verif_resultats.py`). La
    production ne l'appelle plus : voir `_orientation`."""
    direct = (team_similarity(ev.home, res.home) + team_similarity(ev.away, res.away))
    swap = (team_similarity(ev.home, res.away) + team_similarity(ev.away, res.home))
    return swap > direct


def _nom_entier(nom: str) -> str:
    return _strip_class_tag(normalize_team(nom))


#: Deux écritures d'une même chose, pour l'ÉGALITÉ EXACTE de `_orientation`
#: seulement : le reste du matcher, et les marges calibrées, n'en voient rien.
#: Trouvé par `verif_resultats` le 27/09 : « Dundee United v Dundee FC » contre
#: la source « Dundee Utd v Dundee », dans le même ordre, restait indécidable
#: — et c'était un pari joué.
_ABREVIATIONS = {"utd": "united", "wed": "wednesday"}
#: Les formes juridiques que `normalize_team` ne retire pas : « AD San Carlos »
#: est « San Carlos » chez API-Football, « RCD Mallorca » y est « Mallorca ».
_FORMES_JURIDIQUES = frozenset({"ad", "afc", "ca", "cs", "if", "ik", "rc", "rcd",
                                "sd", "ud"})


def _nom_canonique(nom: str) -> str:
    """`_nom_entier`, abréviations développées et formes juridiques retirées.

    Sans risque pour l'égalité EXACTE : pour qu'elle désigne le mauvais sens,
    il faudrait que NOTRE nom de chaque club soit, mot pour mot, celui que la
    source donne à L'AUTRE — et deux clubs au même nom canonique rendent les
    deux sens égaux, que `_orientation` refuse. Pas pour un score flou : un
    mot retiré change toutes les distances."""
    mots = [_ABREVIATIONS.get(m, m) for m in _nom_entier(nom).split()]
    return " ".join([m for m in mots if m not in _FORMES_JURIDIQUES] or mots)


#: Deux avis ne décident que s'ils sont NETS. Calibré le 27/09 sur 1,85 million
#: de cas appariés (350 clubs et leurs graphies, 190 joueurs sous 7 formats,
#: et un corpus hostile de noms mélangés) : sans marge, un écart de 1 point de
#: `fuzz.ratio` — qui ne mesure alors que des longueurs de chaîne — tranchait,
#: et se trompait (« Tsitsipas S v Tsitsipas P », « River v River Plate »).
TRANCHE_ENTIER = 8.0          # noms entiers seuls, quand l'appariement hésite
TRANCHE_APPARIEMENT = 15.0    # appariement seul, quand les noms entiers hésitent
CONFIRME_ENTIER = 4.0         # noms entiers qui confirment un appariement faible
#: Deux équipes de la MÊME famille (« Dundee » / « Dundee United », « Liège » /
#: « Standard Liège », « Gimnasia y Esgrima » qui nomme les DEUX clubs de La
#: Plata et de Mendoza) : les fragments y trompent l'appariement à coup sûr.
#: Une famille, c'est deux noms que le matcher juge proches (`SEUIL_FAMILLE`),
#: OU qui commencent par le même mot d'au moins `MOT_FAMILLE` lettres :
#: « Gimnasia La Plata » / « Gimnasia y Esgrima Mendoza » ne valent que 71,
#: et le fragment « y Esgrima » les inversait.
#: On n'y oriente que si les deux avis s'accordent nettement — ou, quand
#: l'appariement fait jeu égal (il le fait toujours entre noms emboîtés), si
#: les noms entiers tranchent de très loin : « Paris FC v Paris Saint Germain »
#: dans l'ordre de la source, c'est 100 d'écart.
SEUIL_FAMILLE = 85.0
MOT_FAMILLE = 4
FAMILLE_ENTIER = 30.0
FAMILLE_APPARIEMENT = 15.0
FAMILLE_ENTIER_SEUL = 50.0


def _famille(a: str, b: str) -> bool:
    """Deux équipes que les chaînes ne savent pas séparer : voir
    `SEUIL_FAMILLE`."""
    if team_similarity(a, b) >= SEUIL_FAMILLE:
        return True
    ma, mb = _nom_entier(a).split(), _nom_entier(b).split()
    return bool(ma and mb and ma[0] == mb[0] and len(ma[0]) >= MOT_FAMILLE)


def _orientation(ev: "OurEvent", res: MatchResult) -> str | None:
    """"direct", "inverse", ou None quand on ne peut pas le savoir.

    ⚠️ LE SCORE D'APPARIEMENT NE SUFFIT PAS À ORIENTER. `team_similarity`
    prend le meilleur FRAGMENT (`partial_ratio`) : « Dundee » vaut 100 contre
    « Dundee United », et « Inter » contre « Inter Miami ». Entre deux clubs
    aux noms emboîtés, les deux orientations font jeu égal — ou pire, la
    mauvaise l'emporte : « Dundee Utd v Dundee » contre la source « Dundee
    United v Dundee », DANS LE MÊME ORDRE, était retourné (200 contre 187).

    D'où deux avis indépendants :
    * le score d'appariement (celui de `match_event`) ;
    * la ressemblance des noms EN ENTIER (`fuzz.ratio`, classe retirée), qui
      ne se laisse pas prendre à un fragment.
    Ils ne décident qu'avec une marge (voir `TRANCHE_ENTIER`), et entre deux
    clubs de la même famille, qu'ensemble (voir `SEUIL_FAMILLE`). S'ils se
    contredisent, ou n'ont rien de net à dire : None, et le match reste sans
    résultat. Un pari non réglé est un trou visible ; un pari réglé à l'envers
    empoisonne le ROI sans jamais se signaler.

    Mesuré sur le corpus de calibration (27/09) : l'ancienne règle se trompait
    271 fois sur 147 648 matchs de football et 162 fois sur 1,7 million de
    tennis ; celle-ci, zéro fois — au prix de 0,40 % de matchs laissés sans
    résultat en football (des derbies de famille, presque tous) et 0,07 % au
    tennis. Sur un corpus HOSTILE de noms mélangés à dessein, 20 erreurs contre
    761 : toutes sur un nom réellement ambigu (« Gimnasia y Esgrima », le nom
    des deux clubs) ou mal étiqueté dans le corpus — une chaîne ne peut pas les
    trancher, seul un identifiant de club le pourrait."""
    appariement = ((team_similarity(ev.home, res.away) + team_similarity(ev.away, res.home))
                   - (team_similarity(ev.home, res.home) + team_similarity(ev.away, res.away)))
    h, a = _nom_entier(ev.home), _nom_entier(ev.away)
    rh, ra = _nom_entier(res.home), _nom_entier(res.away)
    entier = ((fuzz.ratio(h, ra) + fuzz.ratio(a, rh))
              - (fuzz.ratio(h, rh) + fuzz.ratio(a, ra)))
    # Des noms IDENTIQUES dans un sens et pas dans l'autre : rien à peser.
    # Sans ce court-circuit, « Svetlana Kuznetsova v Alina Kuznetsova » ou
    # « Feirense v Oliveirense », repris mot pour mot par la source, restaient
    # sans résultat : la famille exige des marges que deux noms si proches ne
    # donnent jamais. L'égalité exacte, elle, ne se trompe pas — y compris
    # sur les noms canoniques (« Utd » = « United », voir `_nom_canonique`).
    for x, y, rx, ry in ((h, a, rh, ra),
                         tuple(map(_nom_canonique, (ev.home, ev.away, res.home, res.away)))):
        if (x, y) == (rx, ry) and (x, y) != (ry, rx):
            return "direct"
        if (x, y) == (ry, rx) and (x, y) != (rx, ry):
            return "inverse"
    p = (appariement > 0) - (appariement < 0)
    e = (entier > 0) - (entier < 0)
    if p and e and p != e:
        return None
    if _famille(ev.home, ev.away):
        if p and e:
            sens = p if (abs(entier) >= FAMILLE_ENTIER
                         and abs(appariement) >= FAMILLE_APPARIEMENT) else 0
        elif not p:
            sens = e if abs(entier) >= FAMILLE_ENTIER_SEUL else 0
        else:
            sens = 0
    elif not p:
        sens = e if abs(entier) >= TRANCHE_ENTIER else 0
    elif not e:
        sens = p if abs(appariement) >= TRANCHE_APPARIEMENT else 0
    else:
        sens = p if (abs(entier) >= CONFIRME_ENTIER
                     or abs(appariement) >= TRANCHE_APPARIEMENT) else 0
    if not sens:
        return None
    return "inverse" if sens > 0 else "direct"


_DOUBLES = re.compile(r"\bdoubles?\b", re.IGNORECASE)
#: Un double compacté (« Sohyunparklanlantang », repli du registre sans nom)
#: fait environ deux fois la longueur d'un simple. Au-delà de ce rapport, on
#: ne rapproche pas : ce n'est pas le même match.
RAPPORT_DOUBLE = 1.5


def _est_double(ev: "OurEvent") -> bool:
    """Un match de DOUBLE : « A / B » dans un nom, ou « Doubles » dans la
    ligue."""
    return "/" in ev.home or "/" in ev.away or bool(_DOUBLES.search(ev.league or ""))


def _longueur(nom: str) -> int:
    return len(_nom_entier(nom).replace(" ", ""))


def _double_contre_simple(ev: "OurEvent", res: MatchResult) -> bool:
    """Nos noms font-ils deux fois ceux de la source ? Le repli compacté d'un
    double a perdu son « / » : c'est la longueur qui le trahit."""
    nous = _longueur(ev.home) + _longueur(ev.away)
    eux = _longueur(res.home) + _longueur(res.away)
    return eux > 0 and nous >= RAPPORT_DOUBLE * eux


def _sans_cote(res: MatchResult) -> bool:
    """Un résultat que l'orientation ne change pas : nul au score symétrique
    (1-1), ou nul sans score. Le retourner le laisse identique."""
    if res.winner != "draw":
        return False
    return res.home_score == res.away_score


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


#: Sports dont la SOURCE de résultats porte, elle aussi, la classe (féminin,
#: jeunes) sur ses noms. Le marquage de nos noms n'est correct QUE là.
#:
#: ⚠️ CETTE LISTE EXISTE PARCE QUE LE MARQUAGE EST À SENS UNIQUE AILLEURS.
#:
#: `bind_results` recopie la classe de la ligue sur NOS noms pour que la
#: barrière de classe de `team_similarity` ne sépare pas ce qui doit être
#: rapproché. Le report symétrique sur les noms de la SOURCE n'existe que dans
#: `parse_apifootball_results` (src/score_sources.py:130) : le parseur tennis
#: `parse_livetennis_results` rend les noms de joueurs bruts et n'appelle
#: jamais `with_class_marker`.
#:
#: Sans ce garde, au tennis le marqueur n'atterrit donc que d'un côté, et la
#: barrière renvoie un 0.0 DUR : deux noms pourtant identiques ne s'apparient
#: plus. Mesuré le 18/09 sur dix jours — 196 événements marqués, 196 échecs,
#: soit 85 % du trou tennis. Toutes les épreuves « ITF Women », « ITF Ladies »
#: et « ITF Juniors » étaient inappariables À 100 %, quelle que soit la
#: qualité des noms, et le compteur `classe_posee` annonçait un succès sur
#: exactement les événements qu'il condamnait.
#:
#: Pourquoi restreindre plutôt que marquer aussi la source : au tennis les
#: joueurs sont des INDIVIDUS. La collision que la barrière protège —
#: « Barcelona » contre « Barcelona Femení », deux équipes distinctes portant
#: le même nom — n'a pas d'équivalent ici : une joueuse et un joueur ne
#: partagent pas un nom, et l'horaire sépare déjà les deux épreuves d'un même
#: joueur dans la journée. La barrière n'y protège de rien et coûte tout.
#:
#: Le jour où une source tennis portera la classe, il suffira d'ajouter
#: « tennis » ici — et le test `test_le_tennis_feminin_sapparie_quand_meme`
#: devra alors être revu en même temps.
SPORTS_A_CLASSE_SYMETRIQUE = ("soccer",)


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
        # Apparié, mais impossible de dire dans quel sens (noms emboîtés qui
        # se contredisent) : laissé sans résultat plutôt que réglé à l'envers.
        "orientation_indecidable": 0,
        # Un double de tennis : la source n'en fournit aucun, et un simple des
        # mêmes joueurs n'est PAS son résultat.
        "double_sans_source": 0,
        "classe_posee": 0,           # féminin/jeunes : classe reprise de la ligue
        # Événements dont la ligue porte une classe que la source de CE sport
        # ne sait pas porter. Compté plutôt que tu : sans ce chiffre, « la
        # barrière ne s'applique pas ici » est une décision invisible, et on ne
        # saurait pas combien d'événements en dépendent (§13.12).
        "classe_non_portable": 0,
    }
    tol = tolerance_for_scores(sport)
    bindings: list[tuple[str, MatchResult]] = []

    for ev in events:
        # ⚠️ LE TENNIS N'A AUCUN RÉSULTAT DE DOUBLE : `parse_livetennis_results`
        # les écarte tous. Un double ne peut donc être rapproché que d'un
        # SIMPLE — et `team_similarity` le permet, un sous-ensemble de mots
        # valant 100 : « Sohyun Park / Lanlan Tang » contre « Sohyun Park ».
        # Si les mêmes joueuses ont disputé un simple dans les 12 h (courant en
        # ITF et en Challenger), son score était attribué au double. Constaté
        # par la revue du 27/09 ; aucun garde n'existait.
        if sport == "tennis" and _est_double(ev):
            counters["double_sans_source"] += 1
            continue
        # Remettre la classe sur NOS noms avant de comparer. Pinnacle la laisse
        # dans la ligue (« USA - National Womens Soccer League » / « Houston
        # Dash »), les sources de scores la posent sur l'équipe (« Houston Dash
        # W ») — et `team_similarity` renvoie 0.0 dès que les classes diffèrent.
        # Sans cette ligne, le féminin et les jeunes sont perdus EN ENTIER, sans
        # qu'aucune erreur ne soit levée.
        # ⚠️ ET SEULEMENT SI LA SOURCE DE CE SPORT PORTE LA CLASSE, ELLE AUSSI.
        # Marquer un seul côté ne relâche pas la barrière : il la DÉCLENCHE.
        # Voir `SPORTS_A_CLASSE_SYMETRIQUE` pour la mesure et la raison.
        marque = class_marker_from_league(ev.league)
        if marque and sport not in SPORTS_A_CLASSE_SYMETRIQUE:
            counters["classe_non_portable"] += 1
            marque = ""
        if marque:
            ev = replace(ev, home=with_class_marker(ev.home, marque),
                         away=with_class_marker(ev.away, marque))
            counters["classe_posee"] += 1
        best = match_event(
            ev, candidates, time_tolerance_minutes=tol, min_score=min_score,
        )
        if best is None:
            counters["sans_candidat"] += 1
            continue
        if not best.gradable:
            counters["resultat_inutilisable"] += 1
            continue
        if sport == "tennis" and _double_contre_simple(ev, best):
            counters["double_sans_source"] += 1
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
        # Voir `_orientation` : sur des noms emboîtés (« Dundee » / « Dundee
        # United »), le score d'appariement désignait parfois le mauvais sens.
        # Quand on ne peut pas savoir, on ne règle pas — sauf un nul
        # symétrique, que l'orientation ne change pas.
        sens = _orientation(ev, best)
        if sens is None and not _sans_cote(best):
            counters["orientation_indecidable"] += 1
            continue
        if sens == "inverse":
            best = _flip(best)
            counters["orientation_corrigee"] += 1

        bindings.append((ev.event_key, best))
        counters["lies"] += 1

    return bindings, counters
