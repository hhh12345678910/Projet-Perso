"""Découverte côté API — §21.14.

Le résultat qui compte n'est pas la liste des candidats mais l'alerte de
CONTAMINATION : un identifiant déjà mappé sur un type de match plein qui
porte un libellé de mi-temps. Ces cotes-là entrent aujourd'hui dans le
pipeline sous la mauvaise échelle, et personne ne le voit.
"""
from __future__ import annotations

from src.models import MarketType
from scripts.discover_half_time_api import _explorer, _rapport


def test_kambi_repere_la_mi_temps_dans_criterion():
    """Kambi range le vrai nom du marché dans `criterion.label`, pas à la
    racine de l'offre : ne lire que la racine manquerait tout."""
    payload = {"events": [{"betOffers": [
        {"betOfferType": {"id": 6, "name": "Over/Under"},
         "criterion": {"id": 1001, "label": "Nombre de buts - 1ère mi-temps"}},
        {"betOfferType": {"id": 6, "name": "Over/Under"},
         "criterion": {"id": 1002, "label": "Nombre de buts"}},
    ]}]}
    candidats, _ = _explorer(payload, {})
    assert len(candidats) == 1
    assert "1ère mi-temps" in next(iter(candidats))


def test_la_contamination_est_signalee():
    """Le cas grave : betOfferType 6 est mappé sur TOTALS, et cette offre-là
    est une mi-temps. Elle est donc comparée au 90 minutes de Pinnacle."""
    payload = {"events": [{"betOffers": [
        {"betOfferType": {"id": 6, "name": "Over/Under"},
         "criterion": {"label": "Nombre de buts - 1ère mi-temps"}},
    ]}]}
    _, contamines = _explorer(payload, {6: MarketType.TOTALS})
    assert contamines, "un identifiant mappé + un libellé mi-temps = contamination"
    assert "totals" in next(iter(contamines))


def test_pas_de_contamination_quand_l_identifiant_n_est_pas_mappe():
    """Un marché de mi-temps sous un identifiant qu'on ignore est sans danger :
    il n'entre pas dans le pipeline. C'est un candidat, pas une fuite."""
    payload = {"events": [{"betOffers": [
        {"betOfferType": {"id": 99, "name": "1st Half Over/Under"},
         "criterion": {"label": "Nombre de buts - 1ère mi-temps"}},
    ]}]}
    candidats, contamines = _explorer(payload, {6: MarketType.TOTALS})
    assert candidats and not contamines


def test_altenar_par_type_id():
    """GoldenPalace marque ses marchés d'un `typeId` avec un nom à la racine."""
    payload = {"Value": [{"markets": [
        {"typeId": 18, "name": "Total de buts"},
        {"typeId": 555, "name": "1ère mi-temps - Total de buts"},
    ]}]}
    candidats, contamines = _explorer(payload, {18: MarketType.TOTALS})
    assert len(candidats) == 1
    assert "555" in next(iter(candidats))
    assert not contamines


def test_la_seconde_mi_temps_n_est_pas_un_candidat():
    payload = {"m": [{"typeId": 7, "name": "2ème mi-temps - Total de buts"}]}
    candidats, _ = _explorer(payload, {})
    assert not candidats


def test_un_payload_muet_le_dit_sans_conclure(capsys):
    """Ne pas confondre « ce book n'en propose pas » et « cet endpoint n'en
    remonte pas » — une vue principale n'expose que les marchés phares."""
    _rapport("Test", {"events": [{"betOffers": [
        {"betOfferType": {"id": 2, "name": "Match"}}]}]}, {})
    out = capsys.readouterr().out
    assert "Aucun libellé de mi-temps" in out
    assert "Ne pas conclure" in out
