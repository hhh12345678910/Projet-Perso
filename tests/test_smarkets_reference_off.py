"""Smarkets n'est plus une référence de repli — décision prise sur mesure.

Sur 24 paris valorisés contre le repli Smarkets et jugés au consensus dévigé
des softbooks — règle indépendante de Smarkets, c'est tout son intérêt — la CLV
ressort à −20,6 % de moyenne et **0 % de positifs**. Zéro sur vingt-quatre.

Le mécanisme se lit sur un cas : radualbot — viktorfrydrych, juste Smarkets à
3,71 quand six books s'accordent sur 10,22. Une offre orpheline dans un carnet
vide — et le repli va la chercher précisément là où l'exchange est le plus
creux, puisqu'il ne se déclenche que sur ce que Pinnacle ignore.
"""
from __future__ import annotations

from datetime import datetime, timezone

import src.main as m
# Les deux drapeaux sont DÉFINIS dans la couche de collecte : c'est là qu'on les
# lit, pas sur la copie que `main` en importe.
import src.orchestration as orch
# Les deux drapeaux sont désormais DÉFINIS dans config.py — une seule source de
# vérité. `orchestration` et `main` en importent le même objet ; on vérifie ici
# celui d'origine, et l'identité des trois juste après.
from src import config as cfg
from src.models import Book, MarketType, OddQuote, Outcome

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def q(book, label, odd):
    return OddQuote(event_key="202608161500::a__vs__b", book=book,
                    market=MarketType.H2H, outcome=Outcome(label=label),
                    decimal_odd=odd, fetched_at=NOW, source_event_id="1")


def test_the_fallback_is_off_by_default():
    """Il faut poser SMARKETS_AS_REFERENCE=1 pour le rallumer, jamais l'inverse.

    Un réglage dangereux doit demander un geste explicite ; l'oubli doit
    retomber du côté sûr."""
    assert cfg.SMARKETS_AS_REFERENCE is False


def test_without_a_secondary_no_fallback_line_exists():
    """Sans source secondaire, aucune juste n'est fabriquée sur un marché que
    Pinnacle ne price pas — donc aucun value bet ne peut en naître."""
    fair = m.build_fair_lines([], "shin", secondary_quotes=None)
    assert fair == {}


def test_smarkets_is_off_entirely():
    """Éteint : plus aucun appel, aucun cache, aucune cote stockée.

    `fetch_smarkets_quotes` rend une liste vide sans lancer de fil de
    rafraîchissement, donc le cycle ne paie plus rien pour lui."""
    assert cfg.SMARKETS_ENABLED is False
    assert orch.fetch_smarkets_quotes("tennis") == []


def test_smarkets_stays_a_sharp_book():
    """Invariant de sécurité, à tenir même éteint.

    Le retirer de SHARP_BOOKS en ferait un book SOFT — donc un book où l'on
    chasse une erreur de prix. Si quelqu'un rallume la collecte un jour, on
    parierait sur l'exchange qu'on vient de juger inexploitable."""
    assert Book.SMARKETS in m.SHARP_BOOKS


def test_les_drapeaux_n_ont_qu_une_source_de_verite():
    """⚠️ Le défaut réparé ici : `SMARKETS_ENABLED` était lu dans DEUX espaces
    de noms — la copie de `main` et l'originale de la collecte. Un futur
    changement aurait dû penser aux deux et n'en aurait changé qu'un.

    L'identité (`is`) et non l'égalité : deux booléens égaux ne prouvent rien,
    c'est le même OBJET qu'on veut."""
    import src.main as m
    from src import config as c
    # ENABLED est lu par la collecte ET par la boucle du daemon.
    assert orch.SMARKETS_ENABLED is c.SMARKETS_ENABLED is m.SMARKETS_ENABLED
    # AS_REFERENCE n'est lu que par la boucle : la collecte ne l'importe même
    # pas, ce qui est la forme la plus sûre de « pas de seconde copie ».
    assert c.SMARKETS_AS_REFERENCE is m.SMARKETS_AS_REFERENCE
    assert not hasattr(orch, "SMARKETS_AS_REFERENCE")
