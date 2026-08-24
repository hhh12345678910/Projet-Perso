"""Le garde-fou de saisie des identifiants. §PHASE 4

Sujet minuscule, mais il a coûté quatre allers-retours : un exemple collé
tel quel remonte du serveur en « Invalid userid or password », qui accuse
les identifiants au lieu du copier-coller. La version précédente listait des
chaînes exactes et a laissé passer `ton_identifiant_asianodds` parce qu'elle
ne connaissait que `ton_identifiant`.
"""
import pytest

from scripts.live_asianodds import INVITE_SAISIE, est_un_exemple


@pytest.mark.parametrize("valeur", [
    # Ceux réellement collés au cours de la mise au point.
    "ton_identifiant_asianodds",
    "ton_mot_de_passe",
    "<ton mot de passe AsianOdds>",
    # Variantes que la liste fixe n'aurait pas vues.
    "ton_mot_de_passe_asianodds", "TON_IDENTIFIANT", "  ton_mdp  ",
    "votre_identifiant", "your_password", "mon_mot_de_passe",
    "ma_valeur", "mes_identifiants",
    "password", "identifiant", "mdp", "changeme", "...", "",
])
def test_un_placeholder_est_refuse(valeur):
    assert est_un_exemple(valeur)


@pytest.mark.parametrize("valeur", [
    "hubindylan98", "ao_client_4471", "Xk7#pQ2!zR9v",
    # Un vrai secret peut contenir ces mots sans être un exemple : seule la
    # valeur ENTIÈRE, ou son préfixe possessif, est un signal.
    "monkey42", "toner", "tapas", "Password1sNotMyPassword",
    "my-real-password-actually",   # « my-» n'est pas dans les préfixes
])
def test_une_vraie_valeur_passe(valeur):
    assert not est_un_exemple(valeur)


def test_l_invite_ne_contient_rien_a_remplacer():
    """C'est la cause racine : toute commande donnée à coller qui contient
    un mot à remplacer finira par être collée sans le remplacement."""
    assert "<" not in INVITE_SAISIE
    assert not any(est_un_exemple(mot) for mot in INVITE_SAISIE.split())
    assert "read -rsp" in INVITE_SAISIE, "le mot de passe doit rester masqué"
