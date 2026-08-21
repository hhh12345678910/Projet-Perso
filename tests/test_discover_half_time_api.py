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


def test_on_descend_jusqu_au_marche_sans_s_arreter_sur_l_evenement():
    """Relevé le 21/08 : la sonde retenait 51 « marchés » dont la plupart
    n'étaient que des `eventId`.

    Un dict d'événement contient, quelque part dans ses champs imbriqués, le
    mot « mi-temps » — il déclenchait donc la détection et bloquait la descente
    vers l'offre. Un `eventId` n'est mappable par personne.
    """
    payload = {"events": [{
        "event": {"eventId": 1028840142, "name": "A - B, 1ère mi-temps"},
        "betOffers": [
            {"betOfferType": {"id": 6}, "criterion": {"id": 11928,
                                                      "label": "Nombre de buts - 1ère mi-temps"}},
        ]}]}
    candidats, _ = _explorer(payload, {})
    rendu = " ".join(candidats)
    assert "11928" in rendu, "on doit atteindre l'offre"
    assert "1028840142" not in rendu, "et pas s'arrêter sur l'événement"


def test_le_texte_declencheur_est_affiche():
    """« id=11928 » seul ne dit pas si c'est un 1X2 ou un total, donc ne permet
    pas de mapper. Le libellé qui a déclenché la détection est l'information
    utile de toute la sonde."""
    payload = {"o": [{"betOfferType": {"id": 6},
                      "criterion": {"id": 11928,
                                    "label": "Nombre de buts - 1ère mi-temps"}}]}
    candidats, _ = _explorer(payload, {})
    assert "Nombre de buts - 1ère mi-temps" in next(iter(candidats))


def test_un_conteneur_sans_identifiant_de_marche_ne_masque_pas_ses_enfants():
    """Une ligue ou un bloc intermédiaire ne doit rien retenir pour lui-même."""
    payload = {"ligue": {"nom": "Championnat - 1ère mi-temps", "markets": [
        {"typeId": 555, "name": "1ère mi-temps - Total de buts"}]}}
    candidats, _ = _explorer(payload, {})
    assert len(candidats) == 1
    assert "555" in next(iter(candidats))


def test_rien_trouve_affiche_les_marches_reellement_lus(capsys):
    """Deux règles d'arrêt successives ont produit un faux « rien trouvé » sur
    Unibet — d'abord des `eventId` inexploitables, puis le vide.

    Quand la sonde ne trouve pas de mi-temps, elle doit montrer les marchés
    qu'elle a bien lus : c'est ce qui distingue « ce book n'en propose pas » de
    « la sonde ne comprend pas ce payload ».
    """
    from scripts.discover_half_time_api import _rapport

    _rapport("Test", {"events": [{"betOffers": [
        {"criterion": {"label": "Résultat du match"}},
        {"criterion": {"label": "Nombre de buts"}}]}]}, {})
    out = capsys.readouterr().out
    assert "Aucun libellé de mi-temps" in out
    assert "Résultat du match" in out, "le témoin doit lister ce qui a été lu"
    assert "l'endpoint n'en" in out


def test_rien_lu_du_tout_est_distingue_de_rien_trouve(capsys):
    """Le cas dangereux : la sonde ne comprend pas la forme du payload. Elle
    doit le dire, pas laisser croire à une absence de marchés."""
    from scripts.discover_half_time_api import _rapport

    _rapport("Test", {"donnees": [{"x": 1}, {"y": 2}]}, {})
    out = capsys.readouterr().out
    assert "AUCUN libellé de marché lu du tout" in out
    assert "--brut" in out


def test_le_match_le_plus_profond_garde_l_identifiant_de_l_ancetre():
    """La contamination se juge sur l'offre, le libellé vit sur le criterion :
    les deux doivent être rapprochés."""
    payload = {"events": [{"betOffers": [
        {"betOfferType": {"id": 6},
         "criterion": {"id": 11928, "label": "Nombre de buts - 1ère mi-temps"}}]}]}
    candidats, contamines = _explorer(payload, {6: MarketType.TOTALS})
    assert "11928" in next(iter(candidats)), "le libellé vient du criterion"
    assert contamines, "et l'identifiant mappé vient de l'offre parente"


def test_les_horloges_de_match_ne_sont_pas_des_marches():
    """Kambi publie l'état en direct sur chaque événement en cours :
    « 1ère mi-temps », minute 45, running.

    Relevé le 21/08 : 37 de ces nœuds noyaient les 2 vrais marchés. Un signal
    qu'il faut chercher dans du bruit finit par être manqué.
    """
    payload = {"events": [
        {"score": {"minute": 45, "second": 0, "period": "1ère mi-temps",
                   "running": False, "periodId": "FIRST_HALF"}},
        {"betOffers": [{"betOfferType": {"id": 6, "name": "Over/Under"},
                        "outcomes": [{"odds": 1900}, {"odds": 1900}],
                        "criterion": {"id": 11928,
                                      "label": "Total Goals - 1st Half"}}]},
    ]}
    candidats, _ = _explorer(payload, {})
    rendu = " ".join(candidats)
    assert "11928" in rendu
    assert "minute" not in rendu, "l'horloge de match n'est pas un marché"
    assert len(candidats) == 1


def test_une_entree_de_catalogue_est_signalee_comme_non_mappable():
    """Un criterion sans offre en face ne suffit pas à mapper : deux
    identifiants vus une fois chacun ressemblent à un catalogue."""
    payload = {"criteria": [{"id": 11927, "label": "Half Time"}]}
    candidats, _ = _explorer(payload, {})
    assert "CATALOGUE" in next(iter(candidats))


def test_une_vraie_offre_montre_son_type_et_ses_issues():
    """Ce qui permet de décider : le criterion porte-t-il des cotes ?"""
    payload = {"betOffers": [{
        "betOfferType": {"id": 2, "name": "Match"},
        "outcomes": [{"odds": 2400}, {"odds": 2050}, {"odds": 3600}],
        "criterion": {"id": 11927, "label": "Half Time"}}]}
    candidats, _ = _explorer(payload, {})
    rendu = next(iter(candidats))
    assert "betOfferType=2" in rendu
    assert "3 issues cotées" in rendu
