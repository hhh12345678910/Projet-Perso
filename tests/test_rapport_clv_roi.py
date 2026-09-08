"""Le rapport HTML dit-il la même chose que la commande texte ?

LA SEULE PROPRIÉTÉ QUI COMPTE VRAIMENT
--------------------------------------
Deux outils qui prétendent mesurer la même chose et qui divergent sont pires
qu'un seul : on croit vérifier un chiffre en le voyant deux fois. Le rapport
n'a donc PAS le droit de recalculer — il appelle `clv_roi_matrix.preparer` et
`_cellule`. Ces tests le vérifient en comparant les deux sorties sur la même
base, et le premier tombe si quelqu'un recopie la requête SQL ici (§17.7).

Le reste couvre le HTML lui-même : balises équilibrées, aucun `None` échappé
dans la page, et surtout AUCUNE COULEUR SEULE — un chiffre porte son signe, une
cellule sous effectif porte son icône. Un rapport s'imprime, souvent en noir et
blanc, et se lit par des gens qui ne distinguent pas le rouge du vert.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from scripts.rapport_clv_roi import _constats, _num, main


def _base(tmp_path, lignes):
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE value_bets (id INTEGER PRIMARY KEY, event_key TEXT,
            book TEXT, market TEXT, outcome_label TEXT, line REAL,
            odd_taken REAL, fair_odd REAL, ev_pct REAL, detected_at TEXT);
        CREATE TABLE clv_snapshots (id INTEGER PRIMARY KEY, value_bet_id INT,
            closing INT, fair_odd REAL);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            league TEXT, home TEXT, away TEXT, start_time TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score INT, away_score INT);
        CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, value_bet_id INT);
    """)
    for (i, sport, home, away, jour, cote, ev, clot, gagnant) in lignes:
        ek = f"{jour.replace('-', '')}1800::{home.lower()}__vs__{away.lower()}{i}"
        c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                  (ek, sport, "L1", home, away, f"{jour}T18:00:00+00:00"))
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, "unibet_be", "h2h", "home", None, cote,
                   cote / (1 + ev / 100), ev, f"{jour}T10:00:00+00:00"))
        if clot is not None:
            c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)", (i, i, 1, clot))
        if gagnant is not None:
            c.execute("INSERT INTO results VALUES (?,?,?,?)", (ek, gagnant, 1, 0))
    c.commit()
    c.close()
    return p


def _jeu(n=40, sport="soccer"):
    return [(i, sport, f"A{i}", f"B{i}", "2026-09-0%d" % (i % 8 + 1),
             2.00 + (i % 5) / 10, 5.0, 1.95,
             "home" if i % 2 else "away") for i in range(1, n + 1)]


def _ecrire(tmp_path, monkeypatch, lignes, extra=()):
    p = _base(tmp_path, lignes)
    out = tmp_path / "r.html"
    monkeypatch.setattr("sys.argv",
                        ["r", "--db", str(p), "--out", str(out), *extra])
    main()
    return out.read_text(encoding="utf-8")


# ── La propriété qui compte : les deux sorties concordent ────────────

def test_le_rapport_donne_le_meme_roi_que_la_commande_texte(
        tmp_path, capsys, monkeypatch):
    """⚠️ S'il tombe, quelqu'un a recopié le calcul au lieu de l'appeler."""
    from scripts.clv_roi_matrix import main as texte

    lignes = _jeu()
    p = _base(tmp_path, lignes)

    monkeypatch.setattr("sys.argv", ["m", "--db", str(p)])
    texte()
    sortie = capsys.readouterr().out
    roi_texte = re.search(r"TOUS\s+TOTAL.*?([+-]\d+\.\d+)%", sortie, re.S)
    assert roi_texte, sortie

    out = tmp_path / "r.html"
    monkeypatch.setattr("sys.argv", ["r", "--db", str(p), "--out", str(out)])
    main()
    page = out.read_text(encoding="utf-8")
    assert f"{roi_texte.group(1)} %" in page, (roi_texte.group(1), page[:400])


def test_le_nombre_d_opportunites_concorde(tmp_path, capsys, monkeypatch):
    from scripts.clv_roi_matrix import main as texte
    p = _base(tmp_path, _jeu())
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p)])
    texte()
    n = int(re.search(r"(\d+) opportunités dédupliquées",
                      capsys.readouterr().out).group(1))
    out = tmp_path / "r.html"
    monkeypatch.setattr("sys.argv", ["r", "--db", str(p), "--out", str(out)])
    main()
    assert f">{n}<" in out.read_text(encoding="utf-8")


# ── Le HTML ──────────────────────────────────────────────────────────

def test_les_balises_sont_equilibrees(tmp_path, monkeypatch):
    page = _ecrire(tmp_path, monkeypatch, _jeu(), ["--lister"])
    for t in ("table", "thead", "tbody", "tr", "th", "td", "div", "span",
              "html", "body"):
        o = len(re.findall(rf"<{t}(?=[\s>])", page))
        c = len(re.findall(rf"</{t}>", page))
        assert o == c, f"{t} : {o} ouverts, {c} fermés"


def test_aucun_none_ni_nan_dans_la_page(tmp_path, monkeypatch):
    """Une valeur manquante s'écrit « — ». « None » dans une page imprimée est
    une fuite de Python sous les yeux du lecteur."""
    lignes = _jeu()
    lignes[0] = (1, "soccer", "A", "B", "2026-09-01", 2.0, 5.0, None, None)
    page = _ecrire(tmp_path, monkeypatch, lignes, ["--lister"])
    assert ">None<" not in page
    assert not re.search(r"\b(nan|NaN|inf)\b", page)


def test_le_signe_accompagne_toujours_la_couleur():
    """⚠️ LA COULEUR NE PORTE JAMAIS SEULE. Le document s'imprime, souvent en
    noir et blanc, et se lit par des gens qui ne distinguent pas le rouge du
    vert. Toute valeur colorée doit porter son signe."""
    for v in (12.5, -3.2):
        rendu = _num(v, " %")
        assert 'class="' in rendu
        assert ("+" in rendu) or ("-" in rendu), rendu


def test_une_valeur_inconnue_n_est_ni_colorée_ni_nulle():
    rendu = _num(None, " %")
    assert "—" in rendu
    assert "bon" not in rendu and "mauvais" not in rendu


def test_les_cellules_sous_effectif_portent_une_icone(tmp_path, monkeypatch):
    """Icône + attribut `title`, pas une couleur : même règle."""
    page = _ecrire(tmp_path, monkeypatch, _jeu(n=5))
    assert "⚠️" in page
    assert "moins de 30 paris réglés" in page


def test_la_liste_a_un_en_tete_par_groupe(tmp_path, monkeypatch):
    """Deux colonnes de pourcentages se suivent (EV puis CLV). Sans libellés,
    une page qui tombe au milieu d'une liste devient indéchiffrable — et c'est
    au milieu d'une liste que les pages tombent."""
    page = _ecrire(tmp_path, monkeypatch, _jeu(), ["--lister"])
    bloc = page.split('class="liste"')[1]
    assert bloc.count("<thead>") >= 1
    assert ">EV<" in bloc and ">CLV<" in bloc


def test_sans_lister_pas_d_annexe(tmp_path, monkeypatch):
    page = _ecrire(tmp_path, monkeypatch, _jeu())
    assert "Les paris, un par un" not in page


def test_un_sport_sans_pari_regle_sort_du_tableau_principal(tmp_path,
                                                            monkeypatch):
    """Sinon il diluerait les moyennes avec des colonnes vides. Son absence
    doit rester VISIBLE, pas silencieuse (§11)."""
    lignes = _jeu() + [(900 + i, "basketball", f"C{i}", f"D{i}", "2026-09-01",
                        2.0, 5.0, 1.95, None) for i in range(5)]
    page = _ecrire(tmp_path, monkeypatch, lignes)
    assert "Sports hors périmètre de mesure" in page
    assert "basketball" in page


def test_la_commande_est_dans_le_pied_de_page(tmp_path, monkeypatch):
    """Un document sans sa commande n'est pas reproductible : le lecteur ne
    peut ni le refaire ni savoir quelle porte il regarde."""
    page = _ecrire(tmp_path, monkeypatch, _jeu(),
                   ["--depuis", "2026-09-01", "--axe", "semaine"])
    assert "scripts.rapport_clv_roi" in page
    assert "--depuis 2026-09-01" in page
    assert "--axe semaine" in page


def test_le_libelle_de_periode_est_du_francais(tmp_path, monkeypatch):
    """« du X au aujourd'hui » ne se dit pas, et un rapport qu'on imprime se
    relit. Le test déséchappe la page : l'apostrophe y est `&#x27;`, et c'est
    l'échappement qui est correct, pas le test qui doit l'ignorer."""
    import html as _h
    page = _h.unescape(_ecrire(tmp_path, monkeypatch, _jeu(),
                               ["--depuis", "2026-09-01"]))
    assert "à aujourd'hui" in page
    assert "au aujourd" not in page


# ── Les constats ─────────────────────────────────────────────────────

def test_les_constats_signalent_un_sport_peu_regle():
    """Un ROI mesuré sur moins d'un tiers des opportunités décrit un
    sous-ensemble, et il faut le dire avant qu'on s'en serve."""
    par_sport = {"tennis": {"n_opportunites": 520, "n_regles": 102,
                            "n_clv": 513}}
    tous = {"n_clv": 513, "n_regles": 102}
    txt = " ".join(_constats(par_sport, tous))
    assert "tennis" in txt
    assert "moins d'un tiers" in txt


def test_les_constats_ne_crient_pas_quand_tout_va_bien():
    par_sport = {"soccer": {"n_opportunites": 1000, "n_regles": 900,
                            "n_clv": 950}}
    tous = {"n_clv": 950, "n_regles": 900}
    txt = " ".join(_constats(par_sport, tous))
    assert "moins d'un tiers" not in txt
    assert "la comparaison a donc du sens" in txt


def test_une_base_vide_le_dit(tmp_path, monkeypatch):
    p = _base(tmp_path, [])
    out = tmp_path / "r.html"
    monkeypatch.setattr("sys.argv", ["r", "--db", str(p), "--out", str(out)])
    with pytest.raises(SystemExit):
        main()


def test_les_milliers_ne_se_coupent_pas_en_fin_de_ligne(tmp_path, monkeypatch):
    """⚠️ VU DANS LE PDF DU 8/09. Le sous-titre affichait « sur 25 » puis
    « 758 lignes » à la ligne suivante : le navigateur avait coupé le nombre
    sur son séparateur de milliers. Un nombre coupé en deux n'est plus un
    nombre, c'est deux nombres — et celui-là disait le volume de la mesure.

    L'espace fine insécable (U+202F) l'interdit."""
    from scripts.rapport_clv_roi import FINE, _entier

    assert _entier(25758) == f"25{FINE}758"
    assert " " not in _entier(1234567), "espace ordinaire dans un millier"
    page = _ecrire(tmp_path, monkeypatch, _jeu())
    # Aucun groupe de milliers séparé par une espace ordinaire dans la page.
    import re
    assert not re.search(r"\d \d{3}\b", page), \
        re.search(r".{40}\d \d{3}\b.{20}", page).group(0)
