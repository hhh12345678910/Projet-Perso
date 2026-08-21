"""La sonde de découverte des codes de mi-temps — §21.14.

Son seul travail est de PROPOSER des candidats à un humain. Elle échoue donc
de deux façons : en manquant un marché réel, et — plus grave — en ne trouvant
rien parce qu'elle lit le mauvais champ, ce qui ressemble trait pour trait à
« ce book n'expose pas de mi-temps ». C'est la panne silencieuse du §11.
"""
from __future__ import annotations

import json

from scripts.discover_half_time import _candidat, circus, betano, magicbetting


def test_reconnait_une_mi_temps_par_le_code_seul():
    """Les BetType de Circus sont descriptifs : le code suffit."""
    assert _candidat("1st-half-P1XP2", "")
    assert _candidat("1st-half-total-OverUnder", "")


def test_reconnait_une_mi_temps_par_le_libelle_seul():
    """Betano nomme en clair, son code est opaque."""
    assert _candidat("OUH1", "But en première mi-temps Plus de/Moins de")
    assert _candidat("XYZ", "Eerste helft - resultaat")


def test_ecarte_la_seconde_mi_temps():
    """Pinnacle ne remonte que la période 1 : une 2e mi-temps n'a pas de
    référence en face, et la proposer ferait perdre du temps."""
    assert not _candidat("2nd-half-P1XP2", "")
    assert not _candidat("X", "Résultat 2ème mi-temps")
    assert not _candidat("X", "Second half winner")


def test_ecarte_le_match_plein():
    for code, lib in (("total-OverUnder", "Plus/Moins"), ("MRES", "Résultat du match"),
                      ("P1XP2", ""), ("HCTG", "Total des buts")):
        assert not _candidat(code, lib), f"{code} ne doit pas être candidat"


def test_circus_repere_un_code_de_mi_temps_dans_un_vrai_dump(tmp_path, capsys):
    """Forme réelle : Leagues → Events → Markets → BetType."""
    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "soccer.json").write_text(json.dumps({"Leagues": [{"LeagueName": "L", "Events": [
        {"HomeName": "A", "AwayName": "B", "StartDate": "2030-01-01T20:00:00",
         "Markets": [{"BetType": "P1XP2"},
                     {"BetType": "1st-half-P1XP2"},
                     {"BetType": "1st-half-total-OverUnder"}]}]}]}))
    circus(tmp_path)
    out = capsys.readouterr().out
    assert "1st-half-P1XP2" in out
    assert "2 candidats" in out
    assert "★" in out


def test_betano_repere_ouh1_et_le_dit_deja_mappe(tmp_path, capsys):
    """OUH1 est mappé depuis le 20/08 : la sonde doit le signaler comme tel,
    sinon on le remapperait en double."""
    d = tmp_path / "data" / "prematch"
    d.mkdir(parents=True)
    (d / "soccer.json").write_text(json.dumps({"blocks": [{"events": [{"markets": [
        {"type": "MRES", "name": "Résultat du match"},
        {"type": "OUH1", "name": "But en première mi-temps"}]}]}]}))
    betano(tmp_path)
    out = capsys.readouterr().out
    assert "OUH1" in out and "DÉJÀ MAPPÉ" in out


def test_magicbetting_repere_un_stake_type_de_mi_temps(tmp_path, capsys):
    """Forme réelle : StakeTypes → {Id, N}."""
    d = tmp_path / "data" / "magicbetting"
    d.mkdir(parents=True)
    (d / "soccer.json").write_text(json.dumps({"E": [{"StakeTypes": [
        {"Id": 1, "N": "Résultat du match"},
        {"Id": 77, "N": "1ère mi-temps - Résultat"}]}]}))
    magicbetting(tmp_path)
    out = capsys.readouterr().out
    assert "77" in out and "1 candidats" in out


def test_signale_l_absence_totale_de_libelle(tmp_path, capsys):
    """Le cas dangereux : des marchés lus, aucun libellé, donc un repérage
    aveugle. La sonde doit le DIRE plutôt que de conclure à une absence."""
    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "s.json").write_text(json.dumps({"Leagues": [{"Events": [
        {"HomeName": "A", "AwayName": "B", "StartDate": "2030-01-01T20:00:00",
         "Markets": [{"BetType": "OPAQUE1"}, {"BetType": "OPAQUE2"}]}]}]}))
    circus(tmp_path)
    out = capsys.readouterr().out
    assert "AUCUN libellé" in out


def test_un_repertoire_absent_ne_fait_pas_echouer_la_sonde(tmp_path, capsys):
    circus(tmp_path)
    betano(tmp_path)
    magicbetting(tmp_path)
    out = capsys.readouterr().out
    # Compter la LIGNE de message, pas le mot : le chemin temporaire de
    # pytest contient lui-même « absent ».
    assert out.count("répertoire absent") == 3


def test_circus_lit_un_dump_enveloppe(tmp_path, capsys):
    """Le dump réel n'a pas `Leagues` à la racine.

    La première version supposait cette forme et n'a rien lu sur la VM, alors
    que le répertoire existait — « aucun marché lu » se lisant comme « ce book
    n'a pas de mi-temps ». La traversée doit trouver les marchés où qu'ils
    soient.
    """
    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "soccer.json").write_text(json.dumps(
        {"Result": {"Data": {"Leagues": [{"Events": [
            {"Markets": [{"BetType": "P1XP2"},
                         {"BetType": "1st-half-P1XP2"}]}]}]}}}))
    circus(tmp_path)
    out = capsys.readouterr().out
    assert "1st-half-P1XP2" in out
    assert "1 candidats" in out


def test_circus_signale_un_dump_de_forme_inconnue(tmp_path, capsys):
    """Des fichiers lus mais aucune clé `Markets` : le dire, ne pas conclure."""
    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "soccer.json").write_text(json.dumps({"autre": {"forme": [1, 2, 3]}}))
    circus(tmp_path)
    out = capsys.readouterr().out
    assert "AUCUNE clé `Markets`" in out


def test_un_code_long_n_est_jamais_tronque(tmp_path, capsys):
    """Le mappage se fait par égalité exacte : un code coupé ne matche rien.

    Relevé le 20/08 : `half-time-totals-over-under-...` faisait exactement la
    largeur de colonne et sortait amputé de sa fin. Un code illisible vaut un
    code absent, et coûte ici 444 marchés.
    """
    long_code = "half-time-totals-over-under-OverUnder-extra-long-suffix"
    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "s.json").write_text(json.dumps({"Leagues": [{"Events": [
        {"Markets": [{"BetType": long_code, "Name": "1ère mi-temps - Total de buts"}]}]}]}))
    circus(tmp_path)
    out = capsys.readouterr().out
    assert long_code in out, "le code doit apparaître en entier"


def test_detail_montre_les_noms_d_issues_intraduisibles(tmp_path, capsys):
    """Un marché correctement mappé peut rendre des groupes incomplets.

    `_h2h_label` traduit par ÉGALITÉ EXACTE avec les noms d'équipe : un nom
    suffixé renvoie None, la cote est jetée, et `find_value_bets` écarte le
    groupe entier au contrôle de structure. Rien n'est journalisé — d'où ce
    mode détail. Observé le 21/08 : 66 % des h2h_h1 de Circus écartés.
    """
    from scripts.discover_half_time import _detail

    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "s.json").write_text(json.dumps({"Leagues": [{"Events": [
        {"HomeName": "Anderlecht", "AwayName": "Genk",
         "Markets": [{"BetType": "1HalfP1XP2", "Outcomes": [
             {"Name": "Anderlecht"},        # traduit
             {"Name": "Nul"},               # traduit
             {"Name": "Genk (1ère MT)"},    # PAS traduit
         ]}]}]}]}))
    _detail(tmp_path, "1HalfP1XP2")
    out = capsys.readouterr().out
    assert "Genk (1ère MT)" in out
    assert "JETÉE" in out
    assert "1 cotes sur 3" in out


def test_detail_confirme_quand_tout_est_traduit(tmp_path, capsys):
    from scripts.discover_half_time import _detail

    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "s.json").write_text(json.dumps({"Leagues": [{"Events": [
        {"HomeName": "Anderlecht", "AwayName": "Genk",
         "Markets": [{"BetType": "1HalfP1XP2", "Outcomes": [
             {"Name": "Anderlecht"}, {"Name": "Nul"}, {"Name": "Genk"}]}]}]}]}))
    _detail(tmp_path, "1HalfP1XP2")
    out = capsys.readouterr().out
    assert "tous les noms d'issues sont traduits" in out


def test_detail_sur_un_code_absent_le_dit(tmp_path, capsys):
    from scripts.discover_half_time import _detail

    d = tmp_path / "data" / "circus"
    d.mkdir(parents=True)
    (d / "s.json").write_text(json.dumps({"Leagues": []}))
    _detail(tmp_path, "code-inexistant")
    out = capsys.readouterr().out
    assert "Aucun marché" in out
