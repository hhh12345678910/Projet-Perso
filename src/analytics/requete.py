"""Le SQL d'une analyse — paramétré, dédupliqué, exécuté DANS la base.

DEUX RÈGLES QUI NE SE NÉGOCIENT PAS
-----------------------------------
1. **Aucune valeur utilisateur n'entre dans la chaîne SQL.** Ce qui varie,
   c'est le NOMBRE de marqueurs `?`, jamais leur contenu. Une liste de books
   produit `IN (?,?,?)` et trois paramètres — pas `IN ('a','b','c')`.

2. **`quotes` n'est JAMAIS lue.** 7,6 millions de lignes, 20 Go pour deux
   jours. La clôture est déjà matérialisée dans `clv_snapshots` ; y retourner
   ferait balayer la table la plus lourde du projet pour une information déjà
   rangée ailleurs.

LA DÉDUPLICATION, ET POURQUOI ELLE EST OBLIGATOIRE
--------------------------------------------------
`event_key` contient l'HEURE du coup d'envoi (`matcher.event_key`). Une
révision d'horaire crée donc une clé neuve pour le même match. Mesuré sur la
base de production le 12/09 :

    tennis      17 380 matchs → 53 600 clés   (3,084 par match, jusqu'à 43)
    basketball     986 matchs →  1 146 clés
    soccer      30 138 matchs → 31 023 clés

Sans déduplication, un match de tennis peut compter QUARANTE-TROIS FOIS. La
clé analytique est donc (§17.8) :

    équipes + jour + marché + pari + ligne        — jamais `event_key`

⚠️ ET LE PIÈGE QUI A MORDU TROIS FOIS DANS CE PROJET. La dédup garde la
MEILLEURE COTE. Toute propriété qui vit en dehors de `value_bets` — jouée,
notifiée — doit donc être AGRÉGÉE SUR LE GROUPE ENTIER avant qu'on choisisse
le représentant. Un pari cliqué chez Unibet à 2,10 dont Ladbrokes proposait
2,15 est représenté par la ligne Ladbrokes, qui n'est pas marquée jouée :
lire le drapeau sur le représentant classerait cette opportunité en « non
jouée », et le lot « non joué » se remplirait exactement des paris les mieux
tarifés. C'est `MAX(played) OVER (PARTITION BY cle)` qui l'empêche, et rien
d'autre.
"""
from __future__ import annotations

from .perimetre import BORNES_EV, MARCHES_ANALYTICS, SPORTS_ANALYTICS
from .populations import Population, alias_de

#: Les colonnes rendues. Nommées explicitement plutôt qu'en `SELECT *` : le
#: bloc `metriques._cellule` lit `r["home"]`, `r["closing_fair_odd"]`,
#: `r["played"]`… et un renommage silencieux en amont le ferait échouer
#: bruyamment plutôt que rendre des chiffres faux — mais autant que le
#: contrat soit écrit.
COLONNES = (
    "id", "event_key", "book", "market", "outcome_label", "line",
    "odd_taken", "fair_odd", "ev_pct", "detected_at",
    "sport", "league", "home", "away", "start_time",
    "closing_fair_odd", "winner", "home_score", "away_score",
    "played", "notified_at", "delai_h", "cle",
)

#: Délai entre la détection et le coup d'envoi, en heures.
#:
#: ⚠️ CALCULÉ, PAS LU. `bet_features.delay_h` existe mais ne couvre ni juin ni
#: juillet, et son `detected_at` est celui de la DERNIÈRE détection
#: (`insert_bet_features` fait un INSERT OR REPLACE avec l'objet en mémoire)
#: alors que `value_bets.detected_at` est celui de la PREMIÈRE et ne bouge
#: jamais. Deux colonnes du même nom, deux significations : on calcule depuis
#: `value_bets`, la seule source temporelle autorisée.
EXPR_DELAI = "(julianday(e.start_time) - julianday(vb.detected_at)) * 24.0"

#: La clé de déduplication. Repli sur `event_key` quand l'événement manque :
#: sans lui, toutes les lignes sans équipes tomberaient dans un même groupe
#: et se fondraient en une seule opportunité.
EXPR_CLE = """
    CASE WHEN e.home IS NULL OR e.away IS NULL OR e.start_time IS NULL
         THEN 'EK:' || vb.event_key
         ELSE lower(e.home) || '|' || lower(e.away) || '|'
              || substr(e.start_time, 1, 10)
    END
    || '|' || vb.market || '|' || vb.outcome_label
    || '|' || COALESCE(CAST(vb.line AS TEXT), '~')
"""

_BASE = """
WITH base AS (
    SELECT
        vb.id                AS id,
        vb.event_key         AS event_key,
        vb.book              AS book,
        vb.market            AS market,
        vb.outcome_label     AS outcome_label,
        vb.line              AS line,
        vb.odd_taken         AS odd_taken,
        vb.fair_odd          AS fair_odd,
        vb.ev_pct            AS ev_pct,
        vb.detected_at       AS detected_at,
        e.sport              AS sport,
        e.league             AS league,
        e.home               AS home,
        e.away               AS away,
        e.start_time         AS start_time,
        cs.fair_odd          AS closing_fair_odd,
        r.winner             AS winner,
        r.home_score         AS home_score,
        r.away_score         AS away_score,
        CASE WHEN pb.value_bet_id IS NOT NULL THEN 1 ELSE 0 END AS played_ligne,
        nv.notified_at       AS notified_ligne,
        {delai}              AS delai_h,
        {cle}                AS cle
    FROM value_bets vb
    -- ⚠️ LEFT JOIN et non JOIN : une détection dont l'événement manque doit
    -- rester visible. Un filtre de sport l'écartera de lui-même, mais elle ne
    -- disparaîtra pas d'un total qu'on croit complet.
    LEFT JOIN events        e  ON e.event_key = vb.event_key
    -- La CLÔTURE DÉVIGUÉE, jamais `pinnacle_odd`.
    LEFT JOIN clv_snapshots cs ON cs.value_bet_id = vb.id AND cs.closing = 1
    LEFT JOIN results       r  ON r.event_key = vb.event_key
    -- ⚠️ Le book analytique vient de `value_bets`. `played_bets.book` est
    -- pollué : 76,6 % de ses lignes portent un LIBELLÉ d'affichage
    -- (« StarCasino », « Unibet / 711 / Bingoal / Scooore ») et non une
    -- valeur d'énumération. On ne joint donc que sur `value_bet_id`, et on
    -- ne lit jamais sa colonne `book`.
    LEFT JOIN played_bets   pb ON pb.value_bet_id = vb.id
    -- `notified_value_bets` n'a PAS de `value_bet_id` : cinq colonnes, et le
    -- MIN pour prendre la PREMIÈRE alerte d'une sélection ré-alertée.
    LEFT JOIN (
        SELECT event_key, book, market, outcome_label, line,
               MIN(notified_at) AS notified_at
        FROM notified_value_bets
        GROUP BY event_key, book, market, outcome_label, line
    ) nv ON  nv.event_key     = vb.event_key
        AND  nv.book          = vb.book
        AND  nv.market        = vb.market
        AND  nv.outcome_label = vb.outcome_label
        AND  nv.line IS vb.line
    WHERE 1 = 1
{clauses}
),
groupee AS (
    SELECT base.*,
           -- ⚠️ LES PROPRIÉTÉS DU GROUPE, calculées AVANT le choix du
           -- représentant. C'est tout l'enjeu de la déduplication.
           MAX(played_ligne)   OVER (PARTITION BY cle) AS played,
           MIN(notified_ligne) OVER (PARTITION BY cle) AS notified_at,
           -- Le représentant est la MEILLEURE COTE — c'est elle qui paie.
           -- `id` départage à cote égale pour que le résultat soit stable
           -- d'une exécution à l'autre.
           ROW_NUMBER() OVER (PARTITION BY cle
                              ORDER BY odd_taken DESC, id ASC) AS rang
    FROM base
)
SELECT {colonnes}
FROM groupee
WHERE rang = 1
{apres}
"""


def _sql_bande_ev(label: str) -> "tuple[str, list]":
    """Une bande d'EV en SQL. Bornes basses INCLUSES, hautes EXCLUES.

    C'est la convention de `main._ev_bucket` (`if ev < 5: return "<5%"`), et
    un test la verrouille en balayant l'EV au centième : une borne haute
    incluse ferait compter deux fois un pari exactement à 8 %."""
    lo, hi = BORNES_EV[label]
    bouts, params = [], []
    if lo is not None:
        bouts.append("vb.ev_pct >= ?")
        params.append(lo)
    if hi is not None:
        bouts.append("vb.ev_pct < ?")
        params.append(hi)
    # Une bande sans aucune borne n'existe pas dans `BORNES_EV`, mais si elle
    # y entrait un jour elle doit rendre « vrai » et non une chaîne vide, qui
    # produirait un SQL invalide plutôt qu'un filtre neutre.
    return ("(" + " AND ".join(bouts) + ")" if bouts else "1 = 1"), params


def _sql_union_ev(bandes) -> "tuple[str, list]":
    """L'UNION (OR) d'une sélection de bandes. Vide = aucune contrainte."""
    if not bandes:
        return "1 = 1", []
    bouts, params = [], []
    for b in bandes:
        sql, p = _sql_bande_ev(b)
        bouts.append(sql)
        params.extend(p)
    return "(" + " OR ".join(bouts) + ")", params


def clause_ev(filtres) -> "tuple[str, list]":
    """La contrainte d'EV complète, règles PAR SPORT comprises.

    ⚠️ C'EST ICI QUE SE JOUE « L'EV PAR SPORT », ET C'EST DU VRAI SQL. Une
    règle par sport appliquée côté navigateur serait un mensonge : les KPI,
    les découpes et la matrice viennent tous du même lot, et filtrer après
    coup n'en corrigerait aucun. La forme produite est une disjonction de
    conjonctions :

        (   (sport = 'soccer' AND (EV dans les bandes du foot))
         OR (sport = 'tennis' AND (EV dans les bandes du tennis))
         OR (sport NOT IN (…sports réglés…) AND (bandes globales))  )

    La dernière branche est ce qui empêche une fuite : un sport SANS règle
    propre retombe sur la sélection globale, il ne disparaît pas et n'hérite
    pas de la règle d'un autre sport.
    """
    globales, params_g = _sql_union_ev(filtres.ev_bandes)
    par_sport = [(s, b) for s, b in filtres.ev_par_sport if b]
    if not par_sport:
        return globales, params_g

    branches, params = [], []
    for sport, bandes in par_sport:
        union, p = _sql_union_ev(bandes)
        branches.append(f"(lower(e.sport) = ? AND {union})")
        params.append(sport)
        params.extend(p)

    marqueurs = ", ".join("?" * len(par_sport))
    branches.append(
        f"(COALESCE(lower(e.sport), '') NOT IN ({marqueurs}) AND {globales})")
    params.extend(s for s, _ in par_sport)
    params.extend(params_g)
    return "(" + " OR ".join(branches) + ")", params


def construire(filtres) -> "tuple[str, list]":
    """Rend (sql, parametres). `filtres` doit avoir été validé.

    Les filtres de LIGNE (période, cote, EV, sport, book, marché, ligue,
    délai) s'appliquent AVANT la déduplication : garder un exemplaire hors
    bande puis le jeter ferait disparaître une opportunité dont un autre
    exemplaire était dans la bande.

    Les filtres de GROUPE (joué, envoyé) s'appliquent APRÈS, sur les
    propriétés agrégées — c'est la seule place correcte pour eux.
    """
    clauses: list = []
    params: list = []

    def borne(expr: str, valeur, op: str) -> None:
        if valeur is not None:
            clauses.append(f"      AND {expr} {op} ?")
            params.append(valeur)

    def dans(expr: str, valeurs) -> None:
        """`IN` à N marqueurs. Le nombre de `?` vient de la LONGUEUR de la
        liste ; aucune valeur ne touche la chaîne SQL."""
        if valeurs:
            marqueurs = ", ".join("?" * len(valeurs))
            clauses.append(f"      AND {expr} IN ({marqueurs})")
            params.extend(valeurs)

    borne("vb.detected_at", filtres.date_from, ">=")
    # Borne haute EXCLUSIVE au lendemain : la journée demandée est comprise
    # en entier, y compris une détection à 23 h 59.
    borne("vb.detected_at", filtres.borne_haute_exclusive(), "<")
    borne("vb.odd_taken", filtres.cote_min, ">=")
    borne("vb.odd_taken", filtres.cote_max, "<=")
    borne("vb.ev_pct", filtres.ev_min, ">=")
    borne("vb.ev_pct", filtres.ev_max, "<=")
    # L'expression est RÉPÉTÉE et non référencée par son alias : un alias de
    # SELECT n'est pas garanti visible dans le WHERE du même niveau.
    borne(EXPR_DELAI, filtres.delai_min_h, ">=")
    borne(EXPR_DELAI, filtres.delai_max_h, "<=")

    # ⚠️ LE PÉRIMÈTRE EST APPLIQUÉ DANS LE SQL, TOUJOURS, MÊME SANS SÉLECTION.
    # « Aucun sport coché » veut dire « tout le périmètre Analytics », jamais
    # « tout ce que la base contient ». Sans cette clause, un total affiché
    # comprendrait 501 matchs de basket qui n'ont aucun résultat, donc aucun
    # ROI possible : le taux de règlement baisserait sans qu'aucun pari n'ait
    # échoué. Une détection sans événement rattaché (`e.sport` NULL) sort du
    # périmètre par construction — son sport est inconnu, on ne peut pas
    # affirmer qu'elle est dans la portée.
    dans("lower(e.sport)",
         [s.lower() for s in (filtres.sports or SPORTS_ANALYTICS)])
    dans("vb.market", list(filtres.markets or MARCHES_ANALYTICS))

    dans("vb.book", list(filtres.books))
    dans("e.league", list(filtres.leagues))

    sql_ev, params_ev = clause_ev(filtres)
    if sql_ev != "1 = 1":
        clauses.append(f"      AND {sql_ev}")
        params.extend(params_ev)

    apres: list = []
    if filtres.joue == "oui":
        apres.append("  AND played = 1")
    elif filtres.joue == "non":
        apres.append("  AND played = 0")

    # SENT est le seul étage dont le filtre tienne en SQL : il ne dépend que
    # d'une colonne agrégée. ELIGIBLE_FOR_ALERT et SETTLED rejouent du code
    # Python de production et sont appliqués par le service — voir
    # `populations.HORS_SQL`.
    if alias_de(filtres.population) is Population.SENT:
        apres.append("  AND notified_at IS NOT NULL")
    elif alias_de(filtres.population) is Population.CLICKED:
        apres.append("  AND played = 1")
    elif alias_de(filtres.population) is Population.SETTLED:
        # Pré-filtre bon marché, adossé à la clé primaire de `results`. Le
        # verdict final revient à `clv.settle`, appelée par le service : ce
        # SQL ne sait pas régler un pari, et ne doit pas prétendre le savoir.
        apres.append("  AND winner IS NOT NULL")

    sql = _BASE.format(
        delai=EXPR_DELAI.strip(), cle=EXPR_CLE.strip(),
        clauses="\n".join(clauses),
        colonnes=", ".join(COLONNES),
        apres="\n".join(apres),
    )
    return sql, params
