"""Le rendu PDF du HANDOVER — et le seul défaut qui compte vraiment.

UN NOMBRE COUPÉ EN DEUX EST UNE ERREUR DE LECTURE, PAS D'ESTHÉTIQUE
-------------------------------------------------------------------
La version PDF du 03/09 affichait « 25 758 » avec le 25 en fin de ligne et le
758 au début de la suivante. Sur un document de décision qui compare des
effectifs, ça se lit comme deux nombres. Le correctif vit dans le rendu et non
dans le Markdown, parce que le Markdown doit rester lisible en texte brut —
d'où ces tests, qui tiennent la frontière.

ET LA FRONTIÈRE INVERSE, QUI A DÉJÀ COÛTÉ
------------------------------------------
Une espace fine insécable copiée-collée dans un terminal donne une commande
qui échoue avec un message incompréhensible. C'est arrivé dans ce projet avec
un bloc `.env` recopié depuis une réponse. Les blocs de code ne doivent donc
JAMAIS être touchés — et c'est le test le plus important du fichier.
"""
from __future__ import annotations

import re

from scripts.handover_pdf import (FINE, INSEC, _insecables,
                                  _proteger_hors_code, _titre_propre)


# ── Les milliers ─────────────────────────────────────────────────────

def test_un_groupe_de_milliers_ne_se_coupe_plus():
    assert _insecables("25 758 détections") == f"25{FINE}758 détections"


def test_les_milliers_en_chaine_sont_tous_traites():
    assert _insecables("1 234 567") == f"1{FINE}234{FINE}567"


def test_un_chiffre_suivi_d_un_mot_garde_son_espace():
    """« 5 sports » n'est pas un nombre de cinq mille et quelques."""
    assert _insecables("5 sports") == "5 sports"
    assert _insecables("3 books belges") == "3 books belges"


def test_un_groupe_de_deux_chiffres_n_est_pas_un_millier():
    """« 12 34 » n'existe pas ; seul un groupe de TROIS chiffres l'est."""
    assert _insecables("12 34") == "12 34"


# ── Les unités et les relations ──────────────────────────────────────

def test_une_valeur_ne_se_separe_pas_de_son_unite():
    for brut, unite in (("25 €", "€"), ("2,11 pt", "pt"), ("15 min", "min"),
                        ("11,7 s", "s"), ("24 h", "h"), ("8,3 %", "%")):
        assert _insecables(brut) == brut.replace(" ", INSEC), brut


def test_une_relation_reste_d_un_seul_tenant():
    assert _insecables("n = 951") == f"n{INSEC}={INSEC}951"
    assert _insecables("t = +4,12") == f"t{INSEC}={INSEC}+4,12"
    assert _insecables("z = +11,1") == f"z{INSEC}={INSEC}+11,1"


def test_aucune_espace_ordinaire_ne_subsiste_dans_un_groupe_de_chiffres():
    """Le contrôle global, celui qui attrape ce que les motifs ont manqué."""
    texte = ("Mesuré : 42 122 lignes, 1 180 opportunités, 2 823 alertes, "
             "et 4 001 au total sur 45 585.")
    assert not re.search(r"\d \d{3}\b", _insecables(texte))


# ── ⚠️ LA FRONTIÈRE INVERSE : le code n'est JAMAIS touché ────────────

def test_un_bloc_de_code_garde_ses_espaces_ordinaires():
    md = ("Voici la commande :\n\n"
          "```\nsed -i 's/X/1 000/' .env   # 25 758\n```\n\n"
          "et 25 758 dans le texte.\n")
    out = _proteger_hors_code(md)
    assert "sed -i 's/X/1 000/' .env   # 25 758" in out, \
        "une espace fine dans un shell donne une commande qui échoue"
    assert f"25{FINE}758 dans le texte" in out


def test_le_code_en_ligne_garde_ses_espaces():
    md = "Lancer `--depuis 2026-07-01` sur 1 180 paris."
    out = _proteger_hors_code(md)
    assert "`--depuis 2026-07-01`" in out
    assert f"1{FINE}180 paris" in out


def test_plusieurs_blocs_alternent_correctement():
    """Le découpage par capture alterne texte / code : une erreur d'indice
    inverserait les deux et poserait des insécables DANS le code."""
    md = "a 1 000 `x 1 000` b 2 000\n```\nc 3 000\n```\nd 4 000"
    out = _proteger_hors_code(md)
    assert "`x 1 000`" in out and "c 3 000" in out
    assert f"a 1{FINE}000" in out and f"b 2{FINE}000" in out
    assert f"d 4{FINE}000" in out


def test_un_accent_grave_CITE_ne_ferme_pas_un_bloc():
    """⚠️ Le HANDOVER cite dix fois trois accents graves AU MILIEU d'une
    ligne. Un motif qui les apparie tous ferait fermer un vrai bloc par un
    accent cité — et tout le texte jusqu'au bloc suivant ressortirait intact,
    nombres coupés compris."""
    md = ("Une clôture s'écrit ``` en début de ligne.\n"
          "Et 1 000 ici doit être traité.\n"
          "```\nvrai code 2 000\n```\n"
          "Puis 3 000 encore.\n")
    out = _proteger_hors_code(md)
    assert "vrai code 2 000" in out, "le vrai bloc doit rester intact"
    assert f"Et 1{FINE}000 ici" in out, "du texte a été pris pour du code"
    assert f"Puis 3{FINE}000 encore" in out


def test_le_sommaire_porte_aussi_des_insecables():
    """⚠️ `markdown.extensions.toc` NORMALISE LES BLANCS du nom qu'il rend :
    l'espace fine posée en amont y redevenait une espace ordinaire, et
    « 2 612 » repartait coupable dans le sommaire pendant que le corps du
    texte, lui, était correct."""
    assert _titre_propre("14.1 La mesure — 2 612 opportunités") == \
        f"14.1 La mesure — 2{FINE}612 opportunités"


# ── Le sommaire ──────────────────────────────────────────────────────

def test_un_titre_ne_se_fait_pas_echapper_deux_fois():
    """⚠️ « P&L » arrivait dans le sommaire écrit « P&amp;L » EN TOUTES
    LETTRES — sur un document dont la moitié des sections parlent de P&L."""
    assert _titre_propre("le P&amp;L réel") == "le P&amp;L réel"
    assert "&amp;amp;" not in _titre_propre("le P&amp;L réel")


def test_les_balises_d_un_titre_sont_retirees():
    assert _titre_propre("Le trou de <code>fetch_all</code>") == \
        "Le trou de fetch_all"


# ── Le document entier ───────────────────────────────────────────────

def test_le_handover_reel_se_construit(tmp_path):
    """Un rendu qui casse sur le vrai document ne sert à rien. On ne rend pas
    le PDF ici (pas de navigateur garanti en CI), on construit le HTML."""
    from pathlib import Path
    from scripts.handover_pdf import RACINE, construire_html

    md = (RACINE / "HANDOVER.md").read_text(encoding="utf-8")
    html = construire_html(md, "HANDOVER", "test")
    assert "<h2" in html and "<table>" in html
    assert "Sommaire" in html
    # Aucun nombre coupé n'a survécu hors des blocs de code.
    sans_code = re.sub(r"<pre>.*?</pre>|<code>.*?</code>", "", html, flags=re.S)
    assert not re.search(r"\d \d{3}\b", sans_code)
