"""Moteur de détection LIVE — AsianOdds fait le prix juste, Unibet le prend.

INDÉPENDANT DE LA BOUCLE PRÉMATCH, et pas seulement « en pratique » : ce
module n'importe ni `main`, ni `detection`, ni `orchestration`. Il ne lit que
deux choses, et rien d'autre :

  - `market_state`, en LECTURE SEULE, pour les lignes AsianOdds ;
  - l'instantané EN MÉMOIRE du collecteur Unibet LIVE (commit 1).

Il n'écrit RIEN — ni base, ni Telegram. C'est vérifié par un test.

CE QUE CE MODULE REFUSE DE FAIRE, ET POURQUOI C'EST L'ESSENTIEL. Un moteur
LIVE qui se trompe ne perd pas de l'argent lentement : il alerte sur un prix
fabriqué avant un but, et le pari est perdu à l'instant où il est placé. Tous
les garde-fous ci-dessous vont donc dans le même sens — rater une occasion
plutôt qu'en inventer une. Chaque rejet est COMPTÉ et publié, pour que le prix
de cette prudence reste visible au lieu d'être supposé.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .detection import _overround_ok
from .devig import devig, overround
from .ev import ev_pct, fair_odd, kelly_fraction
from .matcher import parse_event_key
from .models import Book, MarketType, OddQuote, Outcome

#: +10 % d'espérance, STRICTEMENT au-dessus. Le seuil est une borne de
#: décision, pas un arrondi : à 10,00 % exactement on ne prend pas. C'est
#: arbitraire, et c'est justement pour ça que ça doit être écrit et testé —
#: sinon le comportement au seuil dépend du sens d'un `>=` que personne
#: n'a choisi.
SEUIL_EV_PCT = 10.0

#: Tolérance du comparateur de seuil. Sans elle, le seuil n'est PAS décidable :
#: une EV mathématiquement égale à 10 % ressort de `ev_pct` à
#: 10.000000000000002 ou 9.999999999999998 selon la cote qui l'a produite, et
#: le verdict dépend alors du dernier bit d'un flottant. Constaté sur le test
#: du seuil, pas supposé. On considère donc qu'une EV est AU seuil dès qu'elle
#: en est à moins d'un milliardième de point — et être au seuil, c'est ne pas
#: passer. L'écart avec la première valeur qui doit passer (10,1 %) est cent
#: millions de fois plus grand : aucune décision réelle n'en dépend.
EPSILON_EV = 1e-9

#: Âge maximal d'une ligne AsianOdds, sur `observed_at` (l'instant où LA
#: SOURCE a fabriqué le prix), jamais sur `fetched_at` (notre écriture).
#:
#: Mesuré : 95 % des sélections AsianOdds sont revues en moins de 40 s, la
#: médiane est à 28 s, et `upsert_live_state` écrit à CHAQUE message, sans
#: condition — un `observed_at` vieux veut donc bien dire « la source s'est
#: tue », et non « le prix n'a pas bougé ». 60 s laisse passer la quasi-
#: totalité du flux normal et coupe la queue, qui monte à 12 min 49.
AGE_MAX_FAIR_SEC = 60.0

#: Âge maximal de l'instantané Unibet. Le collecteur sonde à 1 s : au-delà de
#: 5 s, ce n'est plus de la latence, c'est un collecteur en panne dont on
#: garderait les derniers prix connus. `Instantane` conserve délibérément
#: l'instantané périmé plutôt que de l'effacer ; c'est ici qu'on refuse de
#: s'en servir.
AGE_MAX_PRENEUR_SEC = 5.0

#: De combien l'EV doit bouger pour re-signaler une occasion déjà vue. Sans
#: ce seuil, sonder à 1 s republierait la même occasion soixante fois par
#: minute pour un centième de point d'EV.
DELTA_EV_REEMISSION = 2.0

#: Au-dela, le match ne peut plus etre en cours. 90 minutes de jeu + 15 de
#: mi-temps + l'arret de jeu + une prolongation eventuelle et ses pauses
#: tiennent dans 150 minutes ; rien de ce qui se joue encore n'est dehors.
#:
#: ⚠️ Deux erreurs a ne pas refaire. J'ai d'abord cru que Rapid Vienna (coup
#: d'envoi 16:45, observe a 18:25) etait termine : c'est faux, j'avais OUBLIE
#: LA MI-TEMPS — 16:45 + 45 + 15 + 45 = 18:30, le match etait a la 88e. La
#: borne est donc large a dessein. Et elle ne pretend rien resoudre d'autre :
#: sur ce meme Rapid, AsianOdds cotait le nul favori a 1.71 a la 88e avec
#: 1:0 au tableau, ce qui est impossible — un `observed_at` frais a 5 s
#: portait un PRIX vieux. Aucune borne temporelle ne detecte ca.
MINUTES_MAX_LIVE = 150.0

#: Marge maximale du marche PRENEUR. Volontairement tres haut : ce controle
#: n'est pas la pour trier les occasions, il est la pour ecarter ce qui n'est
#: pas une offre. Un 1X2 Kambi en direct tourne entre 1,04 et 1,15.
#:
#: ⚠️ IL N'Y A PAS DE BORNE BASSE, ET C'EST DELIBERE. Une marge INFERIEURE a
#: 100 % sur un marche complet est exactement la signature d'un prix trop
#: genereux — c'est-a-dire la chose meme qu'on cherche. La couper reviendrait
#: a supprimer les occasions pour cause d'occasion. Le cas Petrojet du 26/08
#: (marge 0,032) n'etait pas un marche faux mais un marche INCOMPLET : il est
#: traite comme tel, signale et conserve, jamais rejete pour sa marge.
OVERROUND_PRENEUR_MAX = 1.50

#: h2h + totals, MATCH PLEIN. Le HANDICAP est absent par construction, pas
#: par filtrage tardif : la convention de ligne d'Unibet face à celle
#: d'AsianOdds n'a pas été vérifiée, et un handicap mal orienté produit une
#: EV énorme et fausse. Les mi-temps (`H2H_H1`, `TOTALS_H1`) sont dehors pour
#: la même raison : rien ne garantit qu'Unibet et AsianOdds parlent de la
#: même période.
MARCHES = (MarketType.H2H, MarketType.TOTALS)

#: Le seul preneur de cette étape. Betano est volontairement coupé.
BOOKS_PRENEURS = (Book.UNIBET_BE,)

#: Issues attendues par marché. Un groupe amputé n'est pas déviguable : le
#: devig normalise à 100 % QUOI QU'ON LUI DONNE, donc un 1X2 sans le nul
#: rendrait des probabilités d'apparence normale et une EV inventée.
ISSUES_REQUISES = {
    MarketType.H2H: ({"home", "away"}, 2),
    MarketType.TOTALS: ({"over", "under"}, 2),
}


class Statut(str, Enum):
    """Le verdict porté sur une EV au-dessus du seuil.

    Une seule valeur est exploitable. Toutes les autres sont conservées et
    affichées : c'est le compte des rejets qui dit ce que la prudence coûte.
    """
    RETENUE = "RETENUE"
    #: Détectée, mais on ne sait pas à quel score Unibet a fabriqué sa cote.
    #: NON exploitable — voir `_score_coherent`.
    OBSERVEE_SCORE_INCONNU = "OBSERVEE_SCORE_INCONNU"
    REJET_SCORE_INCOHERENT = "REJET_SCORE_INCOHERENT"
    REJET_FAIR_PERIMEE = "REJET_FAIR_PERIMEE"
    REJET_COTE_PERIMEE = "REJET_COTE_PERIMEE"
    #: Le match ne peut plus etre en cours (voir MINUTES_MAX_LIVE).
    REJET_MATCH_TERMINE = "REJET_MATCH_TERMINE"
    #: Le marche preneur n'est pas une offre : marge grotesque sur un marche
    #: COMPLET. Un marche partiel ne passe jamais par la.
    REJET_MARCHE_PRENEUR = "REJET_MARCHE_PRENEUR"
    DOUBLON = "DOUBLON"


def _na(x, fmt="{:.1f}") -> str:
    """Une valeur manquante s'écrit N/A. Elle ne s'invente pas, et elle ne
    vaut pas zéro — un âge inconnu affiché « 0.0 s » se lit comme frais."""
    return "N/A" if x is None else fmt.format(x)


def _cle_ligne(line) -> "float | None":
    return None if line is None else round(float(line), 3)


def _lire_horodatage(txt) -> "datetime | None":
    """Un horodatage ISO de la base, ou None. N'élève jamais : une colonne
    abîmée doit dégrader la détection, pas arrêter le moteur."""
    if not txt:
        return None
    try:
        d = datetime.fromisoformat(txt)
    except (TypeError, ValueError):
        return None
    return d


#: En deçà, un âge négatif est du jitter d'ordonnancement : `parse_listview`
#: estampille une cote quelques microsecondes après l'instant de référence, et
#: « -0,0 s » veut dire zéro. Au-delà, ce n'est plus du jitter — c'est que les
#: deux horloges divergent, et un prix « venu du futur » franchirait le
#: contrôle de fraîcheur quel que soit son âge réel. On refuse alors de dater.
TOLERANCE_HORLOGE_SEC = 1.0


def _age(depuis: "datetime | None", maintenant: datetime) -> "float | None":
    """Secondes écoulées, ou None quand la question n'a pas de réponse sûre."""
    if depuis is None:
        return None
    if depuis.tzinfo is None:
        depuis = depuis.replace(tzinfo=maintenant.tzinfo)
    ecart = (maintenant - depuis).total_seconds()
    if ecart < -TOLERANCE_HORLOGE_SEC:
        return None
    return max(0.0, ecart)


@dataclass(frozen=True)
class GroupeFair:
    """Une ligne juste AsianOdds, déviguée, avec sa provenance."""
    event_key: str
    market: MarketType
    line: "float | None"
    probs: dict
    #: L'observation la PLUS ANCIENNE du groupe. Le devig mêle toutes les
    #: issues : sa fraîcheur est celle de sa jambe la plus vieille, pas celle
    #: de la plus fraîche.
    observed_at: "datetime | None"
    feed_score: "str | None"
    source_event_id: "str | None"
    inverse: bool = False
    cotes: tuple = ()
    #: Non vide = groupe inutilisable. Le groupe est tout de même construit
    #: pour que le motif puisse être compté.
    rejet: "str | None" = None


def construire_fair(storage, maintenant: datetime, *,
                    methode: str = "shin") -> "tuple[dict, Counter]":
    """Les lignes justes AsianOdds du moment. LECTURE SEULE.

    Rend `{(event_key, market, line): GroupeFair}` et le compte des groupes
    écartés par motif. Un groupe écarté n'est PAS dans le dictionnaire : le
    reste du moteur ne peut donc pas s'en servir par inadvertance.
    """
    groupes: dict = {}
    lignes: dict = {}
    for r in storage.market_state(live_only=True):
        if r["book"] != Book.ASIANODDS.value:
            continue
        try:
            marche = MarketType(r["market"])
        except ValueError:
            continue
        if marche not in MARCHES:
            continue
        cle = (r["event_key"], marche, _cle_ligne(r["line"]))
        lignes.setdefault(cle, []).append(r)

    motifs: Counter = Counter()
    for cle, rows in lignes.items():
        g = _grouper(cle, rows, maintenant, methode)
        if g.rejet:
            motifs[g.rejet] += 1
            continue
        groupes[cle] = g
    return groupes, motifs


def _grouper(cle, rows, maintenant: datetime, methode: str) -> GroupeFair:
    event_key, marche, line = cle
    attendues, minimum = ISSUES_REQUISES[marche]

    # Le score du groupe AVANT toute autre chose. Deux jambes du même marché
    # estampillées de scores différents = un devig calculé à cheval sur deux
    # états du match. La probabilité qui en sort n'a jamais existé.
    scores = {r["feed_score"] for r in rows if r["feed_score"]}
    feed_score = next(iter(scores)) if len(scores) == 1 else None

    observations = [_lire_horodatage(r["observed_at"]) for r in rows]
    observed = min((o for o in observations if o is not None), default=None)
    sources = {r["source_event_id"] for r in rows if r["source_event_id"]}

    base = dict(event_key=event_key, market=marche, line=line, probs={},
                observed_at=observed, feed_score=feed_score,
                source_event_id=next(iter(sources)) if len(sources) == 1 else None,
                inverse=any(r["source_inverse"] for r in rows))

    if len(scores) > 1:
        return GroupeFair(**base, rejet="score incohérent dans le groupe fair")

    # Une seule mesure de complétude, et elle porte sur les PRIX RETENUS, pas
    # sur les lignes lues. Une jambe cotée 1.00 est une ligne présente en base
    # et une issue absente du marché : compter les lignes d'abord donnerait un
    # groupe « complet » que le devig ne pourrait pas honorer.
    cotes = tuple(
        OddQuote(event_key=event_key, book=Book.ASIANODDS, market=marche,
                 outcome=Outcome(label=r["outcome_label"], line=line),
                 decimal_odd=float(r["odd"]), fetched_at=maintenant,
                 source_event_id=r["source_event_id"] or "")
        for r in rows if float(r["odd"] or 0) > 1.0)
    prixes = {q.outcome.label for q in cotes}
    if len(cotes) < minimum or not attendues <= prixes:
        return GroupeFair(**base, rejet="groupe incomplet")
    # ET aucune jambe PRÉSENTE ne doit avoir été écartée faute de prix. Le
    # jeu d'issues requises ne peut pas dire à lui seul si un 1X2 est
    # complet : `{home, away}` est aussi le minimum d'un moneyline à deux
    # voies, donc l'exiger laisserait passer un 1X2 dont le NUL est coté 1.00
    # — et le devig normaliserait les deux jambes restantes à 100 %, soit un
    # 1X2 devigué comme un pile ou face. Comparer aux labels réellement
    # présents en base tranche sans avoir à connaître le sport : une issue
    # que la source annonce mais ne price plus est un marché en suspension.
    if {r["outcome_label"] for r in rows} - prixes:
        return GroupeFair(**base, rejet="groupe incomplet")

    # Même contrôle de marge que la référence prématch, et pour la même
    # raison : écarter AVANT de déviger. Nourri d'un marché à 144 % de marge,
    # le devig rend une probabilité plausible et l'aberration devient
    # indétectable en aval.
    if not _overround_ok(list(cotes)):
        return GroupeFair(**base, rejet="overround invalide")
    try:
        probs = devig([q.decimal_odd for q in cotes], method=methode)
    except Exception:                                            # noqa: BLE001
        return GroupeFair(**base, rejet="devig impossible")
    if any(p <= 0.0 or p >= 1.0 for p in probs):
        return GroupeFair(**base, rejet="devig impossible")

    base["probs"] = {q.outcome.label: p for q, p in zip(cotes, probs)}
    return GroupeFair(**base, cotes=cotes)


@dataclass(frozen=True)
class Opportunite:
    """Une EV au-dessus du seuil, avec TOUT ce qui a servi à la juger.

    Les cinq mesures de fraîcheur sont portées ici et non recalculées à
    l'affichage : une occasion qu'on relit dix minutes plus tard doit dire
    l'âge qu'avaient ses sources AU MOMENT DE LA DÉTECTION, pas maintenant.
    """
    detecte_a: datetime
    event_key: str
    home: str
    away: str
    market: MarketType
    line: "float | None"
    outcome: str
    book: Book
    cote_preneur: float
    fair_prob: float
    fair_cote: float
    ev_pct: float
    statut: Statut
    #: Pourquoi ce STATUT. Distinct de `motif_reemission` : l'un dit pourquoi
    #: l'occasion est (in)exploitable, l'autre pourquoi elle est signalee
    #: maintenant. Les entasser dans un seul champ faisait perdre le second
    #: des que le premier etait rempli.
    motif: str = ""
    motif_reemission: str = ""
    #: Kelly plein, en % de bankroll. INFORMATIF ET DE TRI UNIQUEMENT : il ne
    #: supprime jamais une occasion. Mesure le 26/08 : les cinq ecarts de prix
    #: les plus credibles se classaient a l'INVERSE par EV et par Kelly — une
    #: cote a 101 rendait +119 % d'EV pour 1,9 point de probabilite d'ecart,
    #: quand une cote a 3,25 rendait +15 % pour 8,1 points. L'EV en pourcent
    #: mesure la LONGUEUR DE LA COTE autant que l'erreur du book ; Kelly, non.
    kelly_pct: float = 0.0
    #: — les cinq mesures demandées, None quand la donnée manque —
    age_fair_sec: "float | None" = None
    age_preneur_sec: "float | None" = None
    delai_calcul_sec: "float | None" = None
    intervalle_maj_sec: "float | None" = None
    #: — etat du marche preneur —
    #: Une jambe absente n'invalide RIEN : l'EV d'une selection ne depend que
    #: de SA cote et de SA probabilite juste. On le signale, on ne le rejette
    #: pas, et on n'invente aucune cote manquante.
    partiel: bool = False
    issues_manquantes: tuple = ()
    overround_preneur: "float | None" = None
    #: Minutes ecoulees depuis le coup d'envoi annonce. Horloge murale, donc
    #: mi-temps comprise — ce n'est PAS la minute de jeu.
    minute_ecoulee: "float | None" = None
    #: — provenance —
    feed_score: "str | None" = None
    score_preneur: "str | None" = None
    source_event_id_fair: "str | None" = None
    source_event_id_preneur: "str | None" = None
    #: AsianOdds annonçait-il ce match à l'envers ? Vient de `source_inverse`
    #: en base. Le prix a alors DÉJÀ été remis dans notre sens par
    #: `normalise_evf` — c'est une trace, pas une correction à appliquer.
    fair_inverse: bool = False

    @property
    def exploitable(self) -> bool:
        return self.statut is Statut.RETENUE

    @property
    def cle(self) -> tuple:
        """La clé de déduplication : événement + marché + ligne + issue +
        book. Le score n'y entre PAS — après un but, ce n'est pas la même
        occasion mais ce n'en est pas une nouvelle non plus : c'est
        l'évolution de l'EV qui décide de re-signaler."""
        return (self.event_key, self.market.value, self.line,
                self.outcome, self.book.value)

    def ligne(self) -> str:
        """Le format d'observation locale. Une occasion, une ligne."""
        ligne = "" if self.line is None else f" {self.line:g}"
        return (f"[live] match={self.home}-{self.away}"
                f" market={self.market.value}{ligne}"
                f" outcome={self.outcome}"
                f" unibet={self.cote_preneur:.2f}"
                f" fair={self.fair_cote:.2f}"
                f" ev={self.ev_pct:+.1f}%"
                f" asianodds_age={_na(self.age_fair_sec)}s"
                f" unibet_age={_na(self.age_preneur_sec)}s"
                f" delai_calcul={_na(self.delai_calcul_sec, '{:.2f}')}s"
                f" maj_precedente={_na(self.intervalle_maj_sec)}s"
                f" kelly={self.kelly_pct:.2f}%"
                f" score={self.feed_score or 'N/A'}"
                f"/{self.score_preneur or 'N/A'}"
                f" min={_na(self.minute_ecoulee, '{:.0f}')}"
                f" status={self.statut.value}"
                + (f" ({self.motif})" if self.motif else "")
                + (f" [{self.motif_reemission}]"
                   if self.motif_reemission else "")
                + (f"  ⚠️ marché partiel — manque "
                   f"{', '.join(self.issues_manquantes)}" if self.partiel else ""))


@dataclass
class Memoire:
    """Ce que le moteur retient d'un passage à l'autre. Rien n'est persisté.

    Deux mémoires distinctes, parce qu'elles répondent à deux questions :
    `vues` sert la déduplication, `observations` sert à mesurer l'intervalle
    entre deux mises à jour d'une même sélection.
    """
    vues: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)

    def revoir(self, cle, ev: float, feed_score, delta: float) -> "str | None":
        """Faut-il re-signaler cette selection ? Le motif, ou None.

        LA REGLE, EN ENTIER. Une selection deja signalee est retenue une
        seconde fois si — et seulement si — l'une de ces trois choses est
        vraie :
          1. son EV a bouge d'au moins `delta` points ;
          2. le SCORE AsianOdds a change depuis la derniere emission — ce
             n'est alors plus la meme situation de jeu, meme a EV identique ;
          3. elle avait DISPARU d'un passage (gere par `evaluer`, qui oublie
             les cles absentes) et revient.
        Sinon elle est marquee DOUBLON. Rien d'autre ne declenche.
        """
        avant = self.vues.get(cle)
        if avant is None:
            self.vues[cle] = (ev, feed_score)
            return "premiere fois"
        ev0, score0 = avant
        if score0 != feed_score:
            self.vues[cle] = (ev, feed_score)
            return f"score {score0 or 'N/A'} -> {feed_score or 'N/A'}"
        if abs(ev - ev0) >= delta:
            self.vues[cle] = (ev, feed_score)
            return f"EV {ev0:+.1f} -> {ev:+.1f}"
        return None

    def intervalle(self, cle, observed: "datetime | None") -> "float | None":
        """Secondes depuis la mise à jour PRÉCÉDENTE de cette sélection.

        None tant qu'on n'a pas vu deux observations DIFFÉRENTES : entre deux
        passages qui lisent le même `observed_at`, la source n'a rien dit, et
        renvoyer 0 laisserait croire à une mise à jour à l'instant."""
        if observed is None:
            return None
        avant = self.observations.get(cle)
        self.observations[cle] = observed
        if avant is None or avant == observed:
            return None
        return (observed - avant).total_seconds()


@dataclass
class Analyse:
    """Le résultat d'un passage complet."""
    opportunites: list = field(default_factory=list)
    matchs_analyses: int = 0
    quotes_analysees: int = 0
    groupes_fair: int = 0
    groupes_rejetes: Counter = field(default_factory=Counter)
    #: Cotes preneuses écartées AVANT tout calcul d'EV.
    ecartees: Counter = field(default_factory=Counter)
    #: Occasions ≥ seuil par statut.
    par_statut: Counter = field(default_factory=Counter)
    sous_seuil: int = 0
    partiels: int = 0

    @property
    def retenues(self) -> list:
        return [o for o in self.opportunites if o.exploitable]

    @property
    def nouvelles(self) -> list:
        """Tout ce qui n'est pas un doublon : c'est ce qu'on afficherait."""
        return [o for o in self.opportunites if o.statut is not Statut.DOUBLON]


def _score_coherent(feed_score, score_preneur) -> "tuple[bool, bool]":
    """(les deux scores concordent, le score du preneur est connu).

    ⚠️ `feed_score` d'AsianOdds est déjà ramené à NOTRE orientation par
    `normalise_evf` (il permute le score quand le match est annoncé à
    l'envers). Comparer un score preneur exprimé dans la même convention est
    donc légitime — c'est la seule raison pour laquelle cette comparaison a
    un sens.
    """
    if not score_preneur or not feed_score:
        return (False, False)
    return (score_preneur.strip() == feed_score.strip(), True)


def evaluer(quotes_preneur, storage, maintenant: datetime, *,
            scores_preneur: "dict | None" = None,
            memoire: "Memoire | None" = None,
            preneur_pris_a: "datetime | None" = None,
            seuil_ev: float = SEUIL_EV_PCT,
            age_max_fair: float = AGE_MAX_FAIR_SEC,
            age_max_preneur: float = AGE_MAX_PRENEUR_SEC,
            delta_reemission: float = DELTA_EV_REEMISSION,
            methode: str = "shin") -> Analyse:
    """Un passage : les cotes Unibet LIVE contre les lignes justes AsianOdds.

    `quotes_preneur` sort d'`unibet_live.apparier` — déjà re-clées sur nos
    `event_key`, déjà orientées, et marquées `from_live_feed=True`. C'est
    cette marque, et non le nom du book, qui distingue une cote LIVE d'une
    cote prématch : les deux portent `Book.UNIBET_BE`.

    `scores_preneur` associe `source_event_id` → « H:A » dans NOTRE
    orientation. Vide aujourd'hui : le collecteur du commit 1 ne l'expose
    pas. Toute occasion sort alors en `OBSERVEE_SCORE_INCONNU` — détectée,
    affichée, NON exploitable.
    """
    scores_preneur = scores_preneur or {}
    memoire = memoire if memoire is not None else Memoire()
    a = Analyse()

    fair, a.groupes_rejetes = construire_fair(storage, maintenant, methode=methode)
    a.groupes_fair = len(fair)

    # DEUX mesures distinctes, et il faut les garder distinctes. `delai` est
    # le temps entre la fin du sondage Unibet et CE calcul — la latence que
    # NOUS ajoutons. L'âge d'une cote, lui, part de l'instant où
    # `parse_listview` l'a estampillée, et se calcule par cote : deux cotes
    # du même instantané peuvent venir de deux sondages si l'un a échoué.
    delai = _age(preneur_pris_a, maintenant)

    # Le marche PRENEUR complet, par (event_key, marche, ligne). Sert a deux
    # choses et a deux choses seulement : mesurer sa marge, et savoir quelles
    # jambes manquent par rapport a la fair. Jamais a rejeter une selection
    # dont la cote est la.
    lots_preneur: dict = {}
    for q in quotes_preneur:
        if q.book in BOOKS_PRENEURS and q.market in MARCHES and q.from_live_feed:
            lots_preneur.setdefault(
                (q.event_key, q.market, _cle_ligne(q.outcome.line)), []).append(q)

    vus: set = set()
    matchs: set = set()
    for q in quotes_preneur:
        if q.book not in BOOKS_PRENEURS:
            a.ecartees[f"book hors périmètre : {q.book.value}"] += 1
            continue
        if q.market not in MARCHES:
            a.ecartees[f"marché hors périmètre : {q.market.value}"] += 1
            continue
        # « Ne jamais utiliser une cote Unibet prématch comme cote LIVE. »
        # Le book est le même des deux côtés ; seul ce drapeau les sépare.
        if not q.from_live_feed:
            a.ecartees["cote prématch (from_live_feed faux)"] += 1
            continue
        a.quotes_analysees += 1
        matchs.add(q.event_key)

        g = fair.get((q.event_key, q.market, _cle_ligne(q.outcome.line)))
        if g is None:
            a.ecartees["aucune ligne juste AsianOdds"] += 1
            continue
        p = g.probs.get(q.outcome.label)
        if p is None:
            a.ecartees["issue absente de la ligne juste"] += 1
            continue

        ev = ev_pct(q.decimal_odd, p)
        if ev <= seuil_ev + EPSILON_EV:
            a.sous_seuil += 1
            continue

        cle_lot = (q.event_key, q.market, _cle_ligne(q.outcome.line))
        o = _juger(q, g, p, ev, maintenant, scores_preneur, memoire,
                   _age(q.fetched_at, maintenant), delai,
                   age_max_fair, age_max_preneur, delta_reemission,
                   lots_preneur.get(cle_lot, []))
        vus.add(o.cle)
        a.opportunites.append(o)
        a.par_statut[o.statut.value] += 1
        if o.partiel:
            a.partiels += 1

    a.matchs_analyses = len(matchs)
    # Une occasion qui DISPARAÎT est oubliée : si elle revient, elle sera
    # signalée à nouveau. C'est la moitié de la règle de déduplication —
    # sans cet oubli, une occasion vue une fois ne se re-signalerait plus
    # jamais tant que son EV ne saute pas de deux points.
    for cle in list(memoire.vues):
        if cle not in vus:
            del memoire.vues[cle]
    return a


def _juger(q, g: GroupeFair, p: float, ev: float, maintenant: datetime,
           scores_preneur: dict, memoire: Memoire, age_preneur, delai,
           age_max_fair, age_max_preneur, delta_reemission,
           lot_preneur: list) -> Opportunite:
    parsed = parse_event_key(q.event_key)
    home, away = (parsed[1], parsed[2]) if parsed else ("?", "?")
    minute = _age(parsed[0], maintenant) if parsed else None
    minute = None if minute is None else minute / 60.0
    age_fair = _age(g.observed_at, maintenant)
    score_preneur = scores_preneur.get(q.source_event_id)

    # Etat du marche preneur. `manquantes` compare aux issues que la FAIR
    # price : c'est le seul referentiel dont on dispose, et il est le bon —
    # une jambe que la reference cote et que le preneur ne cote plus est
    # exactement ce qu'on veut savoir.
    presentes = {x.outcome.label for x in lot_preneur}
    manquantes = tuple(sorted(set(g.probs) - presentes))
    cotes = [x.decimal_odd for x in lot_preneur if x.decimal_odd > 1.0]
    marge = 1.0 + overround(cotes) if len(cotes) >= 2 else None

    o = Opportunite(
        detecte_a=maintenant, event_key=q.event_key, home=home, away=away,
        market=q.market, line=_cle_ligne(q.outcome.line),
        outcome=q.outcome.label, book=q.book, cote_preneur=q.decimal_odd,
        fair_prob=p, fair_cote=fair_odd(p), ev_pct=ev,
        statut=Statut.RETENUE,
        kelly_pct=100.0 * kelly_fraction(q.decimal_odd, p),
        age_fair_sec=age_fair, age_preneur_sec=age_preneur,
        delai_calcul_sec=delai,
        intervalle_maj_sec=memoire.intervalle(
            (q.event_key, q.market.value, _cle_ligne(q.outcome.line),
             q.outcome.label),
            g.observed_at),
        partiel=bool(manquantes), issues_manquantes=manquantes,
        overround_preneur=marge, minute_ecoulee=minute,
        feed_score=g.feed_score, score_preneur=score_preneur,
        source_event_id_fair=g.source_event_id,
        source_event_id_preneur=q.source_event_id,
        fair_inverse=g.inverse)

    # L'ORDRE compte : on nomme le defaut le plus grave d'abord. Une cote
    # perimee ET un score incoherent se raconte « score incoherent », parce
    # que c'est celui-la qui fait perdre le pari.
    #
    # ⚠️ CE QUI N'EST PAS UN MOTIF DE REJET, ET NE DOIT PAS LE DEVENIR :
    #   - une EV enorme. Il n'existe AUCUN plafond ici. +500 % passe si le
    #     calcul est valide, parce que c'est precisement ce qu'on observe.
    #   - un Kelly faible. Il informe et il trie, il ne supprime pas.
    #   - un marche preneur PARTIEL. L'EV d'une selection ne depend que de SA
    #     cote et de SA probabilite juste ; les autres jambes n'entrent pas
    #     dans le calcul. Une jambe absente est signalee, jamais fatale.
    #   - une marge preneur INFERIEURE a 100 %. C'est la signature d'un prix
    #     trop genereux, donc de l'occasion elle-meme.
    statut, motif = Statut.RETENUE, ""
    concordent, connu = _score_coherent(g.feed_score, score_preneur)
    if connu and not concordent:
        statut = Statut.REJET_SCORE_INCOHERENT
        motif = f"fair {g.feed_score} != preneur {score_preneur}"
    elif minute is not None and minute > MINUTES_MAX_LIVE:
        statut = Statut.REJET_MATCH_TERMINE
        motif = f"{minute:.0f} min depuis le coup d'envoi"
    elif (not manquantes and marge is not None
            and marge > OVERROUND_PRENEUR_MAX):
        # Marche COMPLET dont la marge est grotesque : ce n'est pas une offre.
        statut = Statut.REJET_MARCHE_PRENEUR
        motif = f"marge {marge:.2f} > {OVERROUND_PRENEUR_MAX:.2f}"
    elif age_fair is None:
        statut, motif = Statut.REJET_FAIR_PERIMEE, "observed_at absent"
    elif age_fair > age_max_fair:
        statut = Statut.REJET_FAIR_PERIMEE
        motif = f"{age_fair:.0f}s > {age_max_fair:.0f}s"
    elif age_preneur is None:
        statut, motif = Statut.REJET_COTE_PERIMEE, "instantane sans horodatage"
    elif age_preneur > age_max_preneur:
        statut = Statut.REJET_COTE_PERIMEE
        motif = f"{age_preneur:.0f}s > {age_max_preneur:.0f}s"
    elif not connu:
        # Le seul statut « detecte mais pas exploitable ». On ne sait pas a
        # quel score Unibet a fabrique sa cote : la traiter comme valide
        # reviendrait a supposer ce qu'on n'a pas mesure, et c'est
        # exactement l'alerte-apres-un-but qu'on refuse.
        statut, motif = Statut.OBSERVEE_SCORE_INCONNU, "score preneur indisponible"

    o = _avec(o, statut=statut, motif=motif)

    revoir = memoire.revoir(o.cle, ev, g.feed_score, delta_reemission)
    if revoir is None:
        return _avec(o, statut=Statut.DOUBLON, motif="deja signalee")
    return _avec(o, motif_reemission=revoir)


def _avec(o: Opportunite, **kw) -> Opportunite:
    from dataclasses import replace
    return replace(o, **kw)


def resume(a: Analyse) -> str:
    """Le compte rendu d'un passage. Les rejets sont AUSSI le résultat."""
    lignes = [
        f"  matchs analysés {a.matchs_analyses}   "
        f"quotes analysées {a.quotes_analysees}   "
        f"lignes justes AsianOdds {a.groupes_fair}",
        f"  EV > {SEUIL_EV_PCT:.1f} % : {len(a.opportunites)}   "
        f"(sous le seuil : {a.sous_seuil})",
        f"    exploitables            {a.par_statut.get('RETENUE', 0)}",
        f"    score preneur inconnu   "
        f"{a.par_statut.get('OBSERVEE_SCORE_INCONNU', 0)}",
        f"    score incohérent        "
        f"{a.par_statut.get('REJET_SCORE_INCOHERENT', 0)}",
        f"    fair périmée            "
        f"{a.par_statut.get('REJET_FAIR_PERIMEE', 0)}",
        f"    cote périmée            "
        f"{a.par_statut.get('REJET_COTE_PERIMEE', 0)}",
        f"    match terminé           "
        f"{a.par_statut.get('REJET_MATCH_TERMINE', 0)}",
        f"    marché preneur invalide "
        f"{a.par_statut.get('REJET_MARCHE_PRENEUR', 0)}",
        f"    doublons                {a.par_statut.get('DOUBLON', 0)}",
        f"    dont marchés partiels   {a.partiels}  (signalés, JAMAIS rejetés)",
    ]
    # Les tranches d'EV, SANS plafond superieur. Chacune est un sur-ensemble
    # de la suivante ; une occasion a +500 % compte dans les quatre.
    vivantes = [o for o in a.opportunites if o.statut is not Statut.DOUBLON]
    if vivantes:
        lignes.append("  EV : " + "   ".join(
            f"≥{s_} % : {sum(1 for o in vivantes if o.ev_pct >= s_)}"
            for s_ in (10, 20, 50, 100)))
        haut = max(vivantes, key=lambda o: o.ev_pct)
        gros = max(vivantes, key=lambda o: o.kelly_pct)
        lignes.append(f"  top EV    {haut.ev_pct:+.1f} % (kelly "
                      f"{haut.kelly_pct:.2f} %)  {haut.home}-{haut.away} "
                      f"{haut.market.value} {haut.outcome}")
        lignes.append(f"  top Kelly {gros.kelly_pct:.2f} % (EV "
                      f"{gros.ev_pct:+.1f} %)  {gros.home}-{gros.away} "
                      f"{gros.market.value} {gros.outcome}")
    if a.groupes_rejetes:
        lignes.append("  groupes AsianOdds écartés : " + ", ".join(
            f"{m} ×{n}" for m, n in a.groupes_rejetes.most_common()))
    if a.ecartees:
        lignes.append("  cotes preneuses écartées : " + ", ".join(
            f"{m} ×{n}" for m, n in a.ecartees.most_common(6)))
    return "\n".join(lignes)
