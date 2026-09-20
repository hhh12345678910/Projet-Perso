"""Les six populations d'une analyse — et ce que chacune vaut RÉELLEMENT.

POURQUOI UN MODULE ENTIER POUR ÇA
---------------------------------
Une analyse qui ne sait pas sur quelle population elle porte ne mesure rien.
Les six étages du parcours d'une opportunité ne sont PAS interchangeables, et
trois d'entre eux ne sont pas stockés :

    DETECTED → ELIGIBLE_FOR_ALERT → SENT → CLICKED → BET → SETTLED

Ce module dit, pour chacun, d'où il vient et ce qui lui manque. Les limites
sont dans le code et non dans une note à côté : une population dont on oublie
la limite se met à passer pour une vérité.

⚠️ CE MODULE NE REDÉFINIT RIEN. `ELIGIBLE_FOR_ALERT` rejoue la PRODUCTION
(`routing.canaux_pour` et les gardes de `alerter.send_value_bet`), jamais une
copie de ses règles. Ces règles ont divergé trois fois dans ce projet quand
elles étaient recopiées (§17.7), et à chaque fois une sonde a décrit pendant
des semaines un flux qui n'existait plus.
"""
from __future__ import annotations

from enum import Enum

from ..models import MarketType, is_half_time


class Population(str, Enum):
    """Les six étages. `str` pour être sérialisable tel quel en JSON."""
    DETECTED = "detected"
    ELIGIBLE_FOR_ALERT = "eligible_for_alert"
    SENT = "sent"
    CLICKED = "clicked"
    BET = "bet"
    SETTLED = "settled"


#: Le nom AFFICHÉ de chaque population. En français, et pas par coquetterie :
#: « eligible_for_alert » dans une liste déroulante n'apprend rien à qui n'a
#: pas lu ce module, et une population qu'on choisit sans la comprendre produit
#: un chiffre qu'on croit sans savoir sur quoi il porte.
LIBELLE = {
    Population.DETECTED: "Toutes les détections",
    Population.ELIGIBLE_FOR_ALERT: "Alertables — reconstitué",
    Population.SENT: "Alertes envoyées",
    Population.CLICKED: "Cliquées sur « Jouer »",
    Population.BET: "Pariées",
    Population.SETTLED: "Résultat connu et réglable",
}

#: Les populations PROPOSÉES à l'utilisateur — cinq, pas six.
#:
#: ⚠️ `BET` EN EST RETIRÉE PARCE QU'ELLE N'EST PAS UN CHOIX. `alias_de` la
#: renvoie sur `CLICKED` : les deux entrées rendaient rigoureusement le même
#: lot. Proposer deux options qui donnent le même résultat laisse croire à une
#: distinction — « cliquées » contre « réellement misées » — que le système ne
#: sait pas faire, faute de confirmation de mise. Le jour où cette
#: confirmation existera, `alias_de` cessera de les confondre et `BET`
#: reviendra ici ; d'ici là elle reste ACCEPTÉE par l'API, pour qu'aucune URL
#: ni aucun filtre enregistré ne casse.
EXPOSEES = tuple(p for p in Population if p is not Population.BET)


#: Ce que chaque population mesure, en une phrase destinée à l'utilisateur.
EXPLICATION = {
    Population.DETECTED:
        "Toutes les opportunités détectées, sans aucune porte.",
    Population.ELIGIBLE_FOR_ALERT:
        "Celles qu'une alerte aurait pu franchir : porte du canal, plus les "
        "suppressions que `send_value_bet` applique avant tout routage.",
    Population.SENT:
        "Celles pour lesquelles un message Telegram est réellement parti.",
    Population.CLICKED:
        "Celles dont le bouton « Jouer » a été pressé.",
    Population.BET:
        "Identique à CLICKED — voir la limite.",
    Population.SETTLED:
        "Celles dont le résultat est connu ET que le moteur sait régler.",
}

#: ⚠️ LA LIMITE DE CHAQUE POPULATION. Affichée à l'utilisateur, pas cachée
#: dans un commentaire : c'est la différence entre un chiffre et un chiffre
#: qu'on peut croire.
LIMITES = {
    Population.DETECTED: (),
    Population.ELIGIBLE_FOR_ALERT: (
        "RECONSTITUÉE, non stockée : la porte est rejouée après coup sur la "
        "configuration ACTUELLE des canaux. Une opportunité de juillet est "
        "donc jugée par les règles d'aujourd'hui, pas par celles du jour où "
        "elle est apparue.",
        "La fenêtre morte est jugée sur `detected_at`, l'heure de la PREMIÈRE "
        "détection, alors que la production compare le coup d'envoi à `now` au "
        "moment de l'envoi. Le compte d'écartés est donc une borne BASSE.",
    ),
    Population.SENT: (
        "`notified_value_bets` n'a pas de `value_bet_id` : le rapprochement se "
        "fait sur cinq colonnes dont `event_key`. Une alerte partie sous une "
        "clé d'événement révisée depuis ne se retrouve pas — au tennis, où un "
        "match porte en moyenne 3,1 clés, la perte n'est pas marginale.",
    ),
    Population.CLICKED: (),
    Population.BET: (
        "Le système ne distingue PAS un clic d'un pari réellement placé : il "
        "n'existe aucune confirmation de mise. BET est donc, à ce jour, un "
        "autre nom pour CLICKED — et non une population plus étroite.",
    ),
    Population.SETTLED: (
        "`clv.settle` ne sait régler que `h2h` et `totals`. Les marchés de "
        "mi-temps n'y entrent jamais, quel que soit le résultat connu.",
    ),
}


#: Les populations dont le filtre ne peut PAS descendre dans le SQL, et
#: pourquoi. Le service les applique en Python sur l'ensemble déjà réduit par
#: la base — jamais sur la base entière.
HORS_SQL = {
    Population.ELIGIBLE_FOR_ALERT:
        "rejoue `routing.canaux_pour` et les gardes de `send_value_bet`, du "
        "code Python de production qu'on refuse de traduire en SQL",
    Population.SETTLED:
        "appelle `clv.settle`, seule définition du règlement du projet",
}


def alias_de(population: "Population") -> "Population":
    """BET et CLICKED sont le même filtre. Le dire ici plutôt que dans un
    `if` perdu au milieu du service : quand une confirmation de mise
    existera, c'est cette fonction-là qui cessera de les confondre."""
    return Population.CLICKED if population is Population.BET else population


def _delai_h(row):
    """Adaptateur vers l'unique implémentation du projet.

    ⚠️ L'IMPORT EST DIFFÉRÉ, ET CE N'EST PAS UN DÉTAIL. `clv_roi_matrix`
    importe ce module ; un import au niveau du module créerait un cycle et
    ferait échouer le démarrage. Différé, il est résolu à l'appel, quand les
    deux modules sont chargés.

    C'est une DETTE, et elle est assumée le temps de la V1 : `src/` ne devrait
    pas dépendre de `scripts/`. L'alternative — réécrire ces trois lignes ici —
    serait une seconde définition du délai, et c'est exactement ce que le
    §17.7 interdit. On préfère une dépendance visible à un doublon invisible.
    """
    from scripts.clv_roi_matrix import _delai_h as _impl
    return _impl(row)


MI_TEMPS, FENETRE_MORTE = "mi-temps", "fenêtre morte"


def _raison_non_alertable(row, minutes: float) -> "str | None":
    """Pourquoi `send_value_bet` aurait tu CETTE ligne, ou `None`.

    Les deux raisons ne disent PAS la même chose et ne doivent pas être
    additionnées en silence. La mi-temps est un choix assumé et permanent
    (§21.8) : ces marchés viennent d'être ouverts, leur CLV est inconnue, et
    leur clôture est rarement capturée. La fenêtre morte, elle, écarte des
    marchés parfaitement ordinaires sur le seul critère de l'heure. Un lot
    écarté à 90 % de mi-temps et un lot écarté à 90 % de fenêtre morte
    appellent des conclusions opposées — d'où la ventilation imprimée.
    """
    try:
        marche = MarketType(row["market"])
    except (ValueError, TypeError):
        marche = None
    if marche is not None and is_half_time(marche):
        return MI_TEMPS
    h = _delai_h(row)
    if h is None:        # sans horaire de coup d'envoi : production n'écarte rien
        return None
    if h < 0:            # LIVE — le garde prématch ne s'y applique pas
        return None
    return None if h * 60.0 >= minutes else FENETRE_MORTE


def _alertable(row, minutes: float) -> bool:
    """Vrai si `send_value_bet` aurait laissé passer CETTE ligne.

    Rejoue les deux suppressions qui tombent AVANT tout routage, et que la
    porte du canal ne peut donc pas voir :

    * la **mi-temps** — aucun canal, ni principal, ni premium, ni critique ;
    * la **fenêtre morte** — un value bet PRÉMATCH dont le coup d'envoi est à
      moins de `min_minutes_to_kickoff`. Une détection LIVE (coup d'envoi
      déjà passé) n'est PAS concernée : le garde de production est explicite
      là-dessus, et l'oublier écarterait des paris que la production envoie.

    ⚠️ CE QUE CE REJEU NE PEUT PAS FAIRE. La production compare le coup
    d'envoi à `now` AU MOMENT DE L'ENVOI ; on ne dispose ici que de
    `detected_at`, l'heure de la PREMIÈRE détection, qui ne bouge jamais
    (§14.5). Les deux coïncident quand l'alerte part au premier cycle qui
    voit le pari — le cas normal, la dédup n'en laissant qu'un — mais un pari
    détecté à 3 h du coup d'envoi et alerté seulement plus tard serait jugé
    alertable ici alors que la production l'a peut-être tu. Le compte de
    fantômes que ce filtre donne est donc une borne BASSE, jamais une borne
    haute : il ne peut pas surestimer le ménage.
    """
    return _raison_non_alertable(row, minutes) is None

