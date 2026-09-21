"""Le périmètre de l'Analytics, ses libellés, et ses bandes canoniques.

POURQUOI UN PÉRIMÈTRE, ET POURQUOI IL EST ICI
---------------------------------------------
La base contient six sports et six marchés. L'Analytics n'en analyse que
quatre valeurs : `soccer`, `tennis`, `h2h`, `totals`. Ce n'est pas un caprice
d'affichage, c'est une contrainte de MESURE :

* `clv.settle` ne sait régler que `h2h` et `totals`. Un `handicap` ou un
  `btts` ressort toujours NON RÉGLÉ, quel que soit le score connu. Les laisser
  entrer gonfle le dénominateur du taux de règlement sans jamais pouvoir
  produire un P&L — le taux baisse alors que rien n'a échoué.
* `provider_for` (`src/score_sources.py`) ne connaît aucune source pour le
  basket, le hockey, le volley ni `unknown`. Mesuré le 14/09 sur la base de
  production : **501 matchs de basket, 95 de volley, 21 de hockey, 81
  `unknown` — ZÉRO résultat, pour les quatre.** Ces lignes ne peuvent pas
  contribuer à un ROI ; elles ne font que diluer les effectifs et les taux.

⚠️ AUCUNE DONNÉE N'EST SUPPRIMÉE. Le périmètre est un FILTRE de lecture,
appliqué dans le SQL de l'Analytics. La base garde tout, le moteur continue de
tout détecter, et l'exclusion est réversible en changeant ce seul module.

LES LIBELLÉS SONT DE PRÉSENTATION, JAMAIS DE STOCKAGE
------------------------------------------------------
`unibet_be` reste `unibet_be` en base, dans le SQL, dans les filtres et dans
les paramètres d'API. « Unibet BE » n'existe que dans ce qui s'affiche. Un
libellé qui remonterait dans une requête deviendrait une seconde valeur
canonique — et l'Analytics a déjà payé ce piège avec `played_bets.book`, dont
76,6 % des lignes portent un libellé au lieu d'une valeur d'énumération.
"""
from __future__ import annotations

import re

from ..reference import KAMBI_BOOKS

# ── Le périmètre ─────────────────────────────────────────────────────

#: Les sports analysés. Fermé, et appliqué DANS LE SQL.
SPORTS_ANALYTICS = ("soccer", "tennis")

#: Les marchés analysés — exactement ceux que `clv.settle` sait régler.
MARCHES_ANALYTICS = ("h2h", "totals")

#: Les PARIS possibles, marché par marché. FERMÉ, et pris au vocabulaire du
#: moteur : `live_value` impose littéralement `{"over", "under"}` aux totals,
#: et `asianodds_live` produit `("home", "draw", "away")` pour le 1X2. Lire
#: la base à la place ferait apparaître dans l'interface toute saleté qu'une
#: ligne ancienne contiendrait, sous les mêmes atours qu'un vrai choix.
PARIS_PAR_MARCHE = {
    "h2h": ("home", "draw", "away"),
    "totals": ("over", "under"),
}

#: L'ordre d'affichage : celui du coupon, 1 X 2 puis Over/Under.
PARIS_ANALYTICS = tuple(pari for marche in MARCHES_ANALYTICS
                        for pari in PARIS_PAR_MARCHE[marche])

#: ⚠️ À QUEL MARCHÉ CHAQUE PARI APPARTIENT. L'interface en a besoin pour
#: DIRE que ne cocher que « 1 » et « X » écarte les Over/Under — un filtre
#: dont l'effet de bord n'est pas annoncé est un filtre mal compris.
MARCHE_DU_PARI = {pari: marche
                  for marche, paris in PARIS_PAR_MARCHE.items()
                  for pari in paris}


# ── Les libellés d'affichage ─────────────────────────────────────────

LIBELLE_SPORT = {
    "soccer": "Soccer",
    "tennis": "Tennis",
}

#: ⚠️ Couvre TOUTE l'énumération `models.Book`, y compris les books hors
#: périmètre courant : un book qui apparaîtrait demain dans les données ne
#: doit pas ressortir en identifiant brut au milieu de libellés propres.
LIBELLE_BOOK = {
    "pinnacle": "Pinnacle",
    "betano_be": "Betano BE",
    "unibet_be": "Unibet BE",
    "ladbrokes_be": "Ladbrokes",
    "circus_be": "Circus BE",
    "betfirst": "Betfirst",
    "golden_palace": "Golden Palace",
    "starcasino_sport": "StarCasino Sport",
    "seven_eleven_be": "711 BE",
    "bingoal_be": "Bingoal BE",
    "scooore_be": "Scooore BE",
    "meridian_be": "Meridian BE",
    "napoleon_be": "Napoleon BE",
    "betcenter": "Betcenter",
    "smarkets": "Smarkets",
    "magicbetting": "MagicBetting",
    "elitesports": "EliteSports",
    "asianodds": "AsianOdds",
}

#: ── Les books JUMEAUX, fusionnés pour l'analyse ─────────────────────
#:
#: ⚠️ LA LISTE VIENT DE `src/reference.py`, ELLE N'EST PAS RECOPIÉE ICI.
#: Unibet, 711, Bingoal et Scooore partagent un seul flux Kambi et cotent à
#: l'identique : le même pari sur les quatre est UNE opportunité, pas quatre.
#: Le moteur le sait depuis toujours — `merge_twin_book_value_bets` fusionne
#: leurs alertes — mais l'Analytics l'ignorait. Cocher « Scooore » y rendait
#: donc autre chose que cocher « Unibet » alors que c'est rigoureusement le
#: même prix, et les effectifs se retrouvaient éclatés en quatre.
#:
#: `reference.py` dit lui-même pourquoi il ne faut pas dupliquer ce groupe :
#: « Le recopier ailleurs le ferait diverger le jour où un cinquième jumeau
#: apparaît (§17.7). » On le LIT donc, on ne le réécrit pas.
GROUPES_JUMEAUX: tuple[tuple[str, ...], ...] = (
    tuple(b.value for b in KAMBI_BOOKS),
)

#: book -> tous les books de son groupe (lui compris). Absent = pas de jumeau.
_JUMEAUX_DE = {b: grp for grp in GROUPES_JUMEAUX for b in grp}
#: book -> le REPRÉSENTANT de son groupe. Le premier, comme `_TWIN_PRIMARY`
#: du moteur : c'est déjà la clé sous laquelle le moteur stocke et dédoublonne.
_REPRESENTANT = {b: grp[0] for grp in GROUPES_JUMEAUX for b in grp}

#: Le libellé du GROUPE, distinct de celui du book seul. `LIBELLE_BOOK` garde
#: « Unibet BE » parce qu'une ligne de DÉTAIL doit nommer le book où le prix a
#: réellement été vu ; c'est la DÉCOUPE qui parle du groupe.
LIBELLE_GROUPE_BOOK = {
    GROUPES_JUMEAUX[0][0]: "Unibet / Scooore / 711 / Bingoal",
}

LIBELLE_MARCHE = {
    "h2h": "H2H",
    "totals": "Totals",
    "handicap": "Handicap",
    "btts": "BTTS",
    "h2h_h1": "H2H mi-temps",
    "totals_h1": "Totals mi-temps",
}


def bornes_cote() -> dict:
    """Les bandes de COTE, `label -> (min inclus, max exclu)`.

    ⚠️ LUES dans `scripts.pnl_detections.BANDES_COTE`, jamais recopiées :
    c'est déjà la liste qui construit les lignes de la matrice cote × EV.
    Une seconde table ferait qu'une bande ajoutée à la matrice n'existerait
    pas comme filtre, ou l'inverse — et personne ne s'en apercevrait avant
    de comparer deux chiffres qui devraient être égaux.

    L'import est DIFFÉRÉ : `scripts` importe `src`, et un import au niveau
    du module créerait un cycle. Même dette assumée que `populations._delai_h`,
    et pour la même raison — une dépendance visible plutôt qu'un doublon
    invisible (§17.7).

    La borne haute « infinie » de la dernière bande (1e9) est rendue `None`,
    comme `BORNES_EV` le fait pour « 35 %+ » : les deux tables se lisent alors
    de la même façon.
    """
    from scripts.pnl_detections import BANDES_COTE
    return {lab: (lo, None if hi >= 1e9 else hi) for lab, lo, hi in BANDES_COTE}


def ordre_cote() -> tuple:
    """L'ordre canonique des bandes de cote, celui de la matrice."""
    from scripts.pnl_detections import BANDES_COTE
    return tuple(lab for lab, _, _ in BANDES_COTE)


def pari_de(outcome_label) -> str:
    """Le PARI canonique d'une sélection : « Over 2.5 » → « over ».

    ⚠️ C'EST LA RÈGLE DE `clv.settle`, ET PAS UNE AUTRE. Le règlement lit
    `outcome_label.split()[0].lower()` pour décider si un total est gagné, et
    compare le libellé entier pour le 1X2 — les deux coïncident, les paris
    1X2 n'ayant pas d'espace. Si le filtre lisait autrement, une même ligne
    pourrait être RÉGLÉE comme « over » et FILTRÉE comme autre chose : deux
    vérités sur le même pari, dont une invisible.

    `requete.EXPR_PARI` refait ce découpage en SQL, et un test vérifie que
    les deux rendent le même mot sur le même libellé."""
    texte = str(outcome_label or "").strip().lower()
    coupe = texte.find(" ")
    return texte if coupe < 0 else texte[:coupe]


def slug_cote(label) -> str:
    """Le nom d'URL d'une bande de cote : « 1.0-1.8 » → « 1_0_1_8 ».

    Une query string ne peut pas porter « > 6.0 » sans l'encoder, et un
    libellé encodé se relit mal dans un journal comme dans une URL partagée.

    ⚠️ DÉRIVÉ DU LIBELLÉ, JAMAIS DU RANG. Un `ev_odds_min_3` désignerait « la
    troisième bande » — donc une AUTRE bande le jour où `BANDES_COTE` en gagne
    une, et toute URL enregistrée changerait de sens sans rien dire. Le
    libellé, lui, ne peut pas glisser : s'il disparaît de la table, le filtre
    est REFUSÉ, ce qui se voit."""
    return re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")


def slugs_cote() -> dict:
    """`slug -> libellé`, collisions REFUSÉES.

    Deux bandes réduites au même slug rendraient un filtre indéchiffrable :
    « ev_odds_min_… » s'appliquerait à l'une des deux, et laquelle dépendrait
    de l'ordre d'itération. On préfère une panne franche à une tranche qui
    reçoit la règle de sa voisine."""
    out: dict = {}
    for label in ordre_cote():
        cle = slug_cote(label)
        if cle in out:
            raise ValueError(
                f"Deux bandes de cote partagent le slug {cle!r} : "
                f"{out[cle]!r} et {label!r}. Renomme l'une des deux dans "
                f"scripts.pnl_detections.BANDES_COTE.")
        out[cle] = label
    return out


#: ⚠️ LE LIBELLÉ PORTE LA NOTATION DU COUPON. « home » ne se lit pas sur une
#: grille ; « 1 » si. Au tennis, « 1 » désigne le joueur cité en premier —
#: l'interface le dit, parce que « domicile » n'y veut rien dire.
LIBELLE_PARI = {
    "home": "1 — Domicile",
    "draw": "X — Nul",
    "away": "2 — Extérieur",
    "over": "Over",
    "under": "Under",
}

#: Ce qu'un humain écrit vraiment dans une URL ou une case. Refuser « 1 »
#: obligerait à connaître le vocabulaire interne du moteur pour filtrer sur
#: une victoire à domicile.
ALIAS_PARI = {
    "1": "home", "domicile": "home",
    "x": "draw", "n": "draw", "nul": "draw",
    "2": "away", "exterieur": "away", "extérieur": "away",
}


def _libelle(table: dict, valeur) -> str:
    """Le libellé d'une valeur canonique, ou la valeur elle-même.

    ⚠️ Le repli rend la valeur BRUTE plutôt qu'un « ? » : une valeur inconnue
    doit rester identifiable pour être corrigée, pas disparaître derrière un
    point d'interrogation qui n'apprend rien."""
    if valeur in (None, ""):
        return "—"
    return table.get(str(valeur).lower(), str(valeur))


def libelle_pari(v) -> str:
    """« home » → « 1 — Domicile ». Une valeur inconnue reste BRUTE plutôt
    que de disparaître derrière un « ? » qui n'apprendrait rien."""
    return _libelle(LIBELLE_PARI, v)


def ordre_pari() -> tuple:
    """L'ordre canonique des paris : celui du coupon."""
    return PARIS_ANALYTICS


def libelle_sport(v) -> str:
    return _libelle(LIBELLE_SPORT, v)


def libelle_book(v) -> str:
    return _libelle(LIBELLE_BOOK, v)


def jumeaux_de(book) -> tuple[str, ...]:
    """Tous les books qui servent le MÊME prix que celui-ci, lui compris.

    Un book sans jumeau se rend seul — l'appelant n'a donc pas de cas
    particulier à écrire."""
    cle = str(book or "").lower()
    return _JUMEAUX_DE.get(cle, (cle,))


def canoniser_book(book) -> str:
    """Le représentant du groupe de ce book, ou le book lui-même.

    C'est ce qui fait qu'une découpe par bookmaker rend UNE ligne pour les
    quatre jumeaux Kambi au lieu de quatre lignes portant le même prix."""
    cle = str(book or "").lower()
    return _REPRESENTANT.get(cle, cle)


def libelle_groupe_book(v) -> str:
    """Le libellé à afficher pour une DÉCOUPE par bookmaker.

    Rend « Unibet / Scooore / 711 / Bingoal » pour le groupe Kambi, et le
    libellé ordinaire pour tous les autres."""
    cle = str(v or "").lower()
    if cle in LIBELLE_GROUPE_BOOK:
        return LIBELLE_GROUPE_BOOK[cle]
    return libelle_book(v)


def libelle_marche(v) -> str:
    return _libelle(LIBELLE_MARCHE, v)


# ── Les bandes d'EV ──────────────────────────────────────────────────
#
# ⚠️ CES BORNES NE SONT PAS UNE SECONDE DÉFINITION — elles sont VERROUILLÉES
# SUR `main._ev_bucket` PAR UN TEST qui balaie l'EV de −20 % à +200 % au
# centième et exige l'accord exact. Les libellés viennent de
# `main._EV_BUCKET_ORDER`, importé et jamais recopié.
#
# Pourquoi les bornes doivent exister ici : `_ev_bucket` classe une valeur, il
# ne rend pas d'intervalle. Or une sélection « 8-15 % ET 15-35 % » doit
# devenir du SQL, donc des BORNES. Les déduire en Python ligne par ligne
# reviendrait à charger toute la base pour la filtrer ensuite — précisément ce
# que l'architecture interdit.

#: label → (borne basse incluse, borne haute exclue). `None` = pas de borne.
BORNES_EV = {
    "<5%": (None, 5.0),
    "5-8%": (5.0, 8.0),
    "8-15%": (8.0, 15.0),
    "15-35%": (15.0, 35.0),
    "35%+": (35.0, None),
}


def ordre_ev() -> list:
    """L'ordre canonique des bandes d'EV, LU dans le moteur."""
    from src.main import _EV_BUCKET_ORDER
    return list(_EV_BUCKET_ORDER)


def bandes_cote() -> list:
    """Les bandes de cote, LUES dans `pnl_detections`. (label, lo, hi)."""
    from scripts.pnl_detections import BANDES_COTE
    return list(BANDES_COTE)


# ── Les bandes de délai avant coup d'envoi ───────────────────────────
#
# Introduites par la Phase 4. Elles n'existaient nulle part ailleurs dans le
# projet : c'est donc une définition NEUVE, et non un doublon. Les bornes sont
# en heures, basse incluse et haute exclue, et couvrent la droite réelle sans
# trou — un délai négatif (détection LIVE, coup d'envoi déjà passé) tombe dans
# la première bande, qui le nomme explicitement plutôt que de le perdre.
BANDES_DELAI = (
    ("LIVE / < 1 h", None, 1.0),
    ("1-3 h", 1.0, 3.0),
    ("3-6 h", 3.0, 6.0),
    ("6-12 h", 6.0, 12.0),
    ("12-24 h", 12.0, 24.0),
    ("> 24 h", 24.0, None),
)


def bande_delai(heures) -> str:
    """La bande d'un délai, ou « ? » quand le coup d'envoi est inconnu.

    ⚠️ « ? » est une bande À PART ENTIÈRE et non un rejet : une détection sans
    horaire d'événement doit rester comptée quelque part, sinon les tranches
    ne somment plus au total et personne ne voit ce qui manque."""
    if heures is None:
        return "?"
    h = float(heures)
    for label, lo, hi in BANDES_DELAI:
        if (lo is None or h >= lo) and (hi is None or h < hi):
            return label
    return "?"


def ordre_delai() -> list:
    return [label for label, _, _ in BANDES_DELAI]


# ── L'indicateur de taille d'échantillon ─────────────────────────────
#
# ⚠️ CE N'EST PAS UNE SIGNIFICATIVITÉ STATISTIQUE, ET LE NOM LE DIT.
#
# Ces paliers décrivent un VOLUME, rien d'autre. Un échantillon de 100 paris
# n'est pas « significatif » : la significativité dépend de la variance, de la
# taille de l'effet cherché et du nombre de comparaisons faites — trois choses
# qu'un compteur ignore. Le projet a déjà mesuré ce que coûte la confusion :
# sur 767 paris joués, l'espérance était de +794 € pour +1 854 € observés,
# soit environ 1 050 € de pure chance sur un effectif que ce barème appellerait
# « bon ». Le libellé qualifie donc la TAILLE, jamais la conclusion.

PALIERS_ECHANTILLON = (
    (1000, "tres_bon", "Très bon échantillon"),
    (300, "bon", "Bon échantillon"),
    (100, "moyen", "Échantillon moyen"),
    (0, "petit", "Petit échantillon"),
)

#: Effectif minimal pour qu'un segment soit seulement PROPOSÉ. En dessous, le
#: bruit domine tout écart qu'on chercherait à lire.
MIN_SEGMENT = 100


def taille_echantillon(n) -> dict:
    """Le palier de volume d'un effectif. Volume — pas significativité."""
    n = int(n or 0)
    for seuil, niveau, libelle in PALIERS_ECHANTILLON:
        if n >= seuil:
            return {"n": n, "niveau": niveau, "libelle": libelle,
                    "seuil": seuil}
    return {"n": n, "niveau": "petit", "libelle": "Petit échantillon",
            "seuil": 0}
