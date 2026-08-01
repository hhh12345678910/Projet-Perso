"""Classer un nom de ligue Pinnacle en catégorie exploitable.

Pourquoi ce module
------------------
`events.league` était écrit vide et `value_bets` n'avait pas de colonne ligue :
le nom transitait de Pinnacle jusqu'à l'alerte, puis était jeté. Aucune analyse
par championnat n'était donc possible, même rétroactivement.

Le nom brut est désormais stocké tel quel, et cette catégorie n'est qu'une vue
dérivée. C'est délibéré : une catégorisation se révise, une donnée jetée ne se
récupère pas. Toute analyse future peut repartir du nom brut.

Précédence
----------
Une ligue peut relever de plusieurs catégories — « Sweden - Superettan » est à
la fois scandinave et de deuxième division. L'ordre ci-dessous tranche, du plus
spécifique au plus général :

    amical > féminin > jeunes > coupe > D2 > D3 > top 5 > région > autre

Le motif : les trois premiers décrivent une nature de match qui change tout
(effectifs, motivation, liquidité), la coupe un format, le reste un niveau ou
une géographie. Un amical féminin de D2 suédoise est d'abord un amical.

Les divisions inférieures passent AVANT le top 5, et pas l'inverse : « 2.
Bundesliga » contient « Bundesliga » et serait sinon comptée parmi les cinq
grands championnats — soit exactement le contraire de ce qu'elle est.

Les noms non reconnus tombent en « autre » et sont signalés une fois par le
daemon, pour être ajoutés au fil de l'eau plutôt que devinés d'avance.
"""
from __future__ import annotations

import re
import unicodedata


# Catégories, dans l'ordre de précédence. La première qui correspond gagne.
FRIENDLY = "amical"
WOMEN = "feminin"
YOUTH = "jeunes"
CUP = "coupe"
TOP5 = "top5"
D2 = "d2"
D3 = "d3"
SCANDINAVIA = "scandinavie"
EAST = "europe_est"
SOUTH_AMERICA = "amerique_sud"
OTHER = "autre"

CATEGORIES = (FRIENDLY, WOMEN, YOUTH, CUP, TOP5, D2, D3,
              SCANDINAVIA, EAST, SOUTH_AMERICA, OTHER)


def _norm(s: str) -> str:
    """Minuscules, sans accents, séparateurs unifiés — les noms Pinnacle
    mélangent « - », « – » et parenthèses selon les compétitions."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_FRIENDLY = re.compile(r"\b(friendly|friendlies|amical|club friendlies|testspiel)\b")
# « women » seul suffit ; « w » isolé est trop ambigu pour un nom de ligue et
# n'apparaît pas dans les libellés Pinnacle.
_WOMEN = re.compile(r"\b(women|womens|ladies|feminine|feminin|damen|dames|frauen)\b")
_YOUTH = re.compile(r"\b(u1[5-9]|u2[0-3]|youth|junior|juniors|primavera|jugend)\b")
_CUP = re.compile(
    r"\b(cup|coupe|copa|coppa|pokal|beker|taca|trophy|supercup|super cup|"
    r"champions league|europa league|conference league|libertadores|sudamericana|"
    r"playoff|play offs|qualification|qualifying)\b"
)

# Les cinq grands championnats européens de première division. Reconnus par le
# NOM de la compétition et non par le pays : « England - Championship » est
# anglais mais n'est pas un top 5.
_TOP5 = re.compile(
    r"\b(premier league|la liga|laliga|primera division|serie a|bundesliga|ligue 1)\b"
)
# Sauf que plusieurs pays réutilisent ces noms. On exige donc le bon pays quand
# le libellé en porte un.
_TOP5_COUNTRY = {
    "premier league": ("england",),
    "la liga": ("spain",),
    "laliga": ("spain",),
    "primera division": ("spain",),
    "serie a": ("italy",),
    "bundesliga": ("germany",),
    "ligue 1": ("france",),
}

_D2 = re.compile(
    r"\b(championship|2 bundesliga|2 liga|ligue 2|serie b|segunda division|"
    r"la liga 2|laliga 2|eerste divisie|superettan|obos ligaen|1 division|"
    r"challenge league|2 liga interregional|liga 2|division 2|second division|"
    r"b nationale)\b"
)
_D3 = re.compile(
    r"\b(league one|3 liga|3 bundesliga|national 1|serie c|primera federacion|"
    r"segunda federacion|tweede divisie|division 3|third division|liga 3|"
    r"ettan|2 divisjon|promotion league)\b"
)

# « superliga » est volontairement absent : le Danemark, la Serbie et la
# Slovaquie en ont chacun une. Le pays tranche, le nom de compétition non.
_SCANDINAVIA = re.compile(
    r"\b(sweden|swedish|norway|norwegian|denmark|danish|finland|finnish|iceland|"
    r"icelandic|allsvenskan|eliteserien|veikkausliiga|besta deild)\b"
)
_EAST = re.compile(
    r"\b(poland|polish|czech|slovakia|slovak|hungary|hungarian|romania|romanian|"
    r"bulgaria|bulgarian|serbia|serbian|croatia|croatian|slovenia|slovenian|"
    r"ukraine|ukrainian|russia|russian|belarus|estonia|latvia|lithuania|"
    r"bosnia|macedonia|montenegro|albania|kosovo|georgia|armenia|azerbaijan|"
    r"moldova|ekstraklasa|fortuna liga|superliga srbije)\b"
)
_SOUTH_AMERICA = re.compile(
    r"\b(brazil|brazilian|argentina|argentine|chile|chilean|colombia|colombian|"
    r"peru|peruvian|uruguay|uruguayan|paraguay|ecuador|bolivia|venezuela|"
    r"brasileiro|primera nacional|liga profesional)\b"
)


def _is_top5(text: str) -> bool:
    m = _TOP5.search(text)
    if not m:
        return False
    name = m.group(1)
    countries = _TOP5_COUNTRY.get(name)
    if not countries:
        return False
    # Un libellé sans pays reste accepté : Pinnacle écrit parfois la compétition
    # seule. Un libellé AVEC un autre pays est rejeté — « Brazil - Serie A »
    # n'est pas la Serie A italienne.
    country_like = re.search(
        r"\b(england|spain|italy|germany|france|brazil|portugal|netherlands|"
        r"austria|switzerland|belgium|greece|turkey|scotland|russia|china|"
        r"japan|korea|denmark|sweden|norway|finland|romania|bulgaria)\b", text)
    if country_like is None:
        return True
    return country_like.group(1) in countries


def categorize(league: str | None) -> str:
    """Catégorie d'un nom de ligue. Voir la précédence en tête de module."""
    text = _norm(league or "")
    if not text:
        return OTHER
    if _FRIENDLY.search(text):
        return FRIENDLY
    if _WOMEN.search(text):
        return WOMEN
    if _YOUTH.search(text):
        return YOUTH
    if _CUP.search(text):
        return CUP
    # Avant le top 5 : voir la précédence en tête de module.
    if _D2.search(text):
        return D2
    if _D3.search(text):
        return D3
    if _is_top5(text):
        return TOP5
    if _SCANDINAVIA.search(text):
        return SCANDINAVIA
    if _EAST.search(text):
        return EAST
    if _SOUTH_AMERICA.search(text):
        return SOUTH_AMERICA
    return OTHER
