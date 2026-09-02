"""L'interface Telegram des canaux — et surtout, qui a le droit.

Le bot n'avait AUCUNE notion d'utilisateur avant ce commit : `_allowed_chats`
filtrait par CHAT, et `handle_callback` ne verifiait rien du tout. Creer un
canal depuis un groupe aurait donc ete a la portee de n'importe quel membre,
et un clic a la portee de quiconque atteint un clavier.

Les trois garde-fous qui portent le risque :

  * `test_un_inconnu_ne_peut_RIEN_ecrire` — le refus doit etre total, pas
    seulement affiche : rien ne doit changer en base ;
  * `test_sans_administrateur_deductible_tout_est_refuse` — si le chat
    principal est un groupe, aucun administrateur n'est deduit. Mieux vaut
    une interface inutilisable qu'une interface ouverte a tous ;
  * `test_le_mode_test_n_envoie_rien` — le module n'a aucun moyen de parler
    a Telegram, et ce test l'exige structurellement.
"""
from __future__ import annotations

import sqlite3

import pytest

import src.canaux_telegram as ct
from src.alerter import TelegramConfig
from src.channels import charger
from src.storage import Storage

ADMIN = "531952352"
INTRUS = "999999999"


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ADMIN_ID", raising=False)
    return TelegramConfig(bot_token="jeton", chat_id=ADMIN)


@pytest.fixture
def st(tmp_path) -> Storage:
    return Storage(str(tmp_path / "t.db"))


def _cmd(texte, st, cfg, qui=ADMIN):
    return ct.commande(texte, storage=st, cfg=cfg, id_utilisateur=qui)


def _clic(data, st, cfg, qui=ADMIN):
    return ct.bouton(data, storage=st, cfg=cfg, id_utilisateur=qui)


def _canal(st, nom="TENNIS"):
    _cmd(f"/nouveau {nom} -100123", st, TelegramConfig(bot_token="j", chat_id=ADMIN))
    c, ids, cid = ct._canal_et_regles(st, nom)
    return c, ids, cid


# ══ AUTORISATION ═══════════════════════════════════════════════════════
def test_un_inconnu_ne_peut_RIEN_ecrire(st, cfg):
    """Le refus doit etre TOTAL : affiche, et sans le moindre effet."""
    for texte in ("/canaux", "/nouveau PIRATE -100999", "/canal TENNIS",
                  "/test TENNIS"):
        rep = _cmd(texte, st, cfg, qui=INTRUS)
        assert rep is not None and rep.texte == ct.REFUS, texte
    assert st.load_channel_rows()[0] == [], "un intrus a ecrit en base"


def test_un_inconnu_ne_peut_actionner_aucun_bouton(st, cfg):
    _canal(st)
    _, ids, cid = ct._canal_et_regles(st, "TENNIS")
    for data in (f"cx:a:TENNIS", f"cx:r+:TENNIS", f"cx:x:TENNIS",
                 f"cx:xx:TENNIS", f"cx:v:TENNIS"):
        rep = _clic(data, st, cfg, qui=INTRUS)
        assert rep is not None and rep.texte == ct.REFUS, data
    canaux = charger(st)
    assert len(canaux) == 1 and canaux[0].actif is False
    assert canaux[0].regles == ()


def test_supprimer_un_canal_est_refuse_a_un_inconnu(st, cfg):
    """Le clic le plus destructeur, teste separement."""
    _canal(st)
    assert _clic("cx:xx:TENNIS", st, cfg, qui=INTRUS).texte == ct.REFUS
    assert len(charger(st)) == 1, "un intrus a supprime un canal"


def test_sans_auteur_tout_est_refuse(st, cfg):
    """Un post de CANAL Telegram n'a pas d'auteur. La gestion y est donc
    impossible — et c'est voulu."""
    assert _cmd("/canaux", st, cfg, qui=None).texte == ct.REFUS
    assert _clic("cx:a:TENNIS", st, cfg, qui=None).texte == ct.REFUS


def test_sans_administrateur_deductible_tout_est_refuse(st, monkeypatch):
    """Chat principal = un GROUPE (identifiant negatif) : aucun
    administrateur n'est deduit, personne ne peut rien faire."""
    monkeypatch.delenv("TELEGRAM_ADMIN_ID", raising=False)
    groupe = TelegramConfig(bot_token="j", chat_id="-1001234567890")
    assert ct.admin_id(groupe) is None
    assert _cmd("/canaux", st, groupe, qui="-1001234567890").texte == ct.REFUS
    assert _cmd("/canaux", st, groupe, qui=ADMIN).texte == ct.REFUS


def test_la_variable_d_environnement_prime(st, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_ID", "42")
    c = TelegramConfig(bot_token="j", chat_id=ADMIN)
    assert ct.admin_id(c) == "42"
    assert _cmd("/canaux", st, c, qui="42") is not None
    assert _cmd("/canaux", st, c, qui=ADMIN).texte == ct.REFUS


def test_l_administrateur_par_defaut_est_le_chat_prive(cfg):
    assert ct.admin_id(cfg) == ADMIN
    assert ct.est_admin(ADMIN, cfg) is True
    assert ct.est_admin(int(ADMIN), cfg) is True     # Telegram rend un entier
    assert ct.est_admin(INTRUS, cfg) is False


def test_une_commande_etrangere_n_est_pas_interceptee(st, cfg):
    """`/scan` et `/book` doivent continuer leur chemin, pas etre avales."""
    for texte in ("/scan", "/book", "/start", "bonjour"):
        assert _cmd(texte, st, cfg) is None, texte
    assert _clic("play:abc", st, cfg) is None
    assert _clic("bookalert:unibet_be", st, cfg) is None


# ══ CREATION ET LISTE ══════════════════════════════════════════════════
def test_creer_un_canal(st, cfg):
    rep = _cmd("/nouveau TENNIS -100123", st, cfg)
    canaux = charger(st)
    assert len(canaux) == 1
    assert (canaux[0].nom, canaux[0].chat_id) == ("TENNIS", "-100123")
    assert "TENNIS" in rep.texte and rep.clavier is not None


def test_un_canal_nait_COUPE_et_sans_regle(st, cfg):
    """Sinon poser la premiere regle le ferait emettre au milieu de la
    configuration."""
    _cmd("/nouveau TENNIS -100123", st, cfg)
    c = charger(st)[0]
    assert c.actif is False and c.regles == ()


def test_un_nom_deja_pris_est_refuse(st, cfg):
    _cmd("/nouveau TENNIS -100123", st, cfg)
    rep = _cmd("/nouveau TENNIS -100999", st, cfg)
    assert "existe déjà" in rep.texte
    assert len(charger(st)) == 1


def test_lister_les_canaux(st, cfg):
    _cmd("/nouveau TENNIS -100123", st, cfg)
    _cmd("/nouveau GROSSES -100456", st, cfg)
    rep = _cmd("/canaux", st, cfg)
    assert "TENNIS" in rep.texte and "GROSSES" in rep.texte


# ══ EDITION PAR BOUTONS ════════════════════════════════════════════════
def test_activer_et_couper(st, cfg):
    _canal(st)
    _clic("cx:a:TENNIS", st, cfg)
    assert charger(st)[0].actif is True
    _clic("cx:a:TENNIS", st, cfg)
    assert charger(st)[0].actif is False


def test_ajouter_et_supprimer_une_regle(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _clic("cx:r+:TENNIS", st, cfg)
    assert len(charger(st)[0].regles) == 2
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    _clic(f"cx:r-:{ids[0]}", st, cfg)
    assert len(charger(st)[0].regles) == 1


def test_le_cycle_absent_inclus_exclu(st, cfg):
    """Un seul bouton pour trois etats : deux boutons par valeur rendraient
    le clavier illisible sur un telephone."""
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rid = ids[0]

    _clic(f"cx:t:{rid}:sport:tennis", st, cfg)
    c = charger(st)[0].regles[0].criteres[0]
    assert (c.dimension, c.inclut, c.valeurs) == ("sport", True, frozenset({"tennis"}))

    _clic(f"cx:t:{rid}:sport:tennis", st, cfg)
    c = charger(st)[0].regles[0].criteres[0]
    assert c.inclut is False

    _clic(f"cx:t:{rid}:sport:tennis", st, cfg)
    assert charger(st)[0].regles[0].criteres == ()


def test_chaque_bouton_ne_touche_QUE_sa_borne(st, cfg):
    """Le defaut de la premiere version : les paliers etaient des couples
    figes, donc poser un maximum effacait le minimum et « EV entre 12 et 25 »
    etait impossible au clavier."""
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rid = ids[0]

    _clic(f"cx:evmin:{rid}:10", st, cfg)
    r = charger(st)[0].regles[0]
    assert r.ev_min.valeur == 10.0 and r.ev_max is None

    _clic(f"cx:evmax:{rid}:20", st, cfg)
    r = charger(st)[0].regles[0]
    assert (r.ev_min.valeur, r.ev_max.valeur) == (10.0, 20.0), \
        "poser le maximum a efface le minimum"

    _clic(f"cx:comin:{rid}:1.5", st, cfg)
    _clic(f"cx:comax:{rid}:4", st, cfg)
    r = charger(st)[0].regles[0]
    assert (r.odd_min.valeur, r.odd_max.valeur) == (1.5, 4.0)
    assert (r.ev_min.valeur, r.ev_max.valeur) == (10.0, 20.0), \
        "les bornes de cote ont touche celles d'EV"


def test_retirer_une_borne_sans_toucher_a_l_autre(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rid = ids[0]
    _clic(f"cx:evmin:{rid}:10", st, cfg)
    _clic(f"cx:evmax:{rid}:20", st, cfg)
    _clic(f"cx:evmax:{rid}:", st, cfg)           # « aucun »
    r = charger(st)[0].regles[0]
    assert r.ev_min.valeur == 10.0 and r.ev_max is None


def test_l_ecran_des_bornes_marque_la_valeur_posee(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rid = ids[0]
    _clic(f"cx:evmin:{rid}:10", st, cfg)
    rep = _clic(f"cx:ev:{rid}", st, cfg)
    assert "EV ≥ 10" in rep.texte
    libelles = [b["text"] for row in rep.clavier["inline_keyboard"] for b in row]
    assert "• 10" in libelles, libelles


def test_la_saisie_libre_donne_une_commande_prete(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rep = _clic(f"cx:evx:{ids[0]}", st, cfg)
    assert "/canal TENNIS 1 ev" in rep.texte
    assert rep.clavier is None


def test_cycler_la_phase(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rid = ids[0]
    for attendu in ("prematch", "live", None):
        _clic(f"cx:ph:{rid}", st, cfg)
        assert charger(st)[0].regles[0].phase == attendu


def test_supprimer_un_canal_demande_confirmation(st, cfg):
    _canal(st)
    rep = _clic("cx:x:TENNIS", st, cfg)
    assert "Supprimer" in rep.texte and rep.clavier is not None
    assert len(charger(st)) == 1, "la demande ne doit RIEN supprimer"
    _clic("cx:xx:TENNIS", st, cfg)
    assert charger(st) == []


def test_supprimer_un_canal_emporte_ses_regles(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    _clic(f"cx:t:{ids[0]}:sport:tennis", st, cfg)
    _clic("cx:xx:TENNIS", st, cfg)
    assert st.load_channel_rows() == ([], [], [])


def test_un_bouton_perime_le_dit_au_lieu_de_rester_muet(st, cfg):
    """Un clavier d'une version anterieure porte des verbes disparus. Ne rien
    renvoyer donnerait un bouton qui « ne marche pas » sans explication."""
    rep = _clic("cx:inconnu:x", st, cfg)
    assert rep is not None and rep.texte == ""
    assert "périmé" in rep.alerte


def test_le_retour_sur_une_regle_supprimee_ramene_quelque_part(st, cfg):
    """Telegram garde les anciens messages cliquables indefiniment : le
    retour doit aboutir meme si la regle a disparu entre-temps."""
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rid = ids[0]
    _clic(f"cx:r-:{rid}", st, cfg)
    rep = _clic(f"cx:vr:{rid}", st, cfg)
    assert "n'existe plus" in rep.texte and "TENNIS" in rep.texte
    rep = _clic(f"cx:ev:{rid}", st, cfg)
    assert "n'existe plus" in rep.texte


def test_une_dimension_inventee_est_refusee(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rep = _clic(f"cx:t:{ids[0]}:competition:x", st, cfg)
    assert "inconnue" in rep.texte
    assert st.load_channel_rows()[2] == []


# ══ VALEURS EXACTES PAR COMMANDE TEXTE ═════════════════════════════════
def test_bornes_exactes_par_commande(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _cmd("/canal TENNIS 1 ev 12.5 27.5", st, cfg)
    _cmd("/canal TENNIS 1 cote 1,85 3,95", st, cfg)
    r = charger(st)[0].regles[0]
    assert (r.ev_min.valeur, r.ev_max.valeur) == (12.5, 27.5)
    assert (r.odd_min.valeur, r.odd_max.valeur) == (1.85, 3.95)


def test_league_par_commande_texte(st, cfg):
    """Les ligues sont des chaines libres et nombreuses : un clavier serait
    illisible, et une correspondance partielle attraperait plus que ce qu'on
    a ecrit."""
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _cmd("/canal TENNIS 1 league Belgique - Pro League", st, cfg)
    c = charger(st)[0].regles[0].criteres[0]
    assert c.dimension == "league" and c.inclut is True
    assert c.valeurs == frozenset({"belgique - pro league"})
    _cmd("/canal TENNIS 1 -league Belgique - Pro League", st, cfg)
    assert charger(st)[0].regles[0].criteres[0].inclut is False


def test_un_numero_de_regle_hors_bornes_est_refuse(st, cfg):
    _canal(st)
    rep = _cmd("/canal TENNIS 7 ev 10", st, cfg)
    assert "règle" in rep.texte


# ══ LE MODE TEST : AUCUN ENVOI ═════════════════════════════════════════
def test_le_mode_test_n_envoie_rien():
    """Structurel : le module n'importe rien qui puisse parler a Telegram.
    Aucune boucle mal ecrite ne peut donc inonder un canal depuis ici."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "src" / "canaux_telegram.py").read_text(encoding="utf-8")
    arbre = ast.parse(src)
    noms = set()
    for n in ast.walk(arbre):
        if isinstance(n, ast.Import):
            noms.update(x.name for x in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            noms.add(n.module)
    for interdit in ("httpx", "requests", "urllib", "src.alerter", "telegram",
                     "socket", "http"):
        assert not any(x == interdit or x.startswith(interdit + ".") for x in noms), \
            f"canaux_telegram importe {interdit}"
    assert "sendMessage" not in src and "api.telegram.org" not in src


def test_tester_un_canal_compte_sans_envoyer(st, cfg):
    from datetime import datetime, timedelta, timezone
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    _clic(f"cx:evmin:{ids[0]}:20", st, cfg)

    c = sqlite3.connect(str(st.path))
    now = datetime.now(timezone.utc)
    for i, ev in enumerate((6.0, 25.0, 30.0)):
        depart = now + timedelta(hours=6)
        cle = f"{depart:%Y%m%d%H%M}::eq{i}a__vs__eq{i}b"
        c.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                  (cle, "tennis", "ATP", "a", "b", depart.isoformat()))
        c.execute("INSERT INTO value_bets(event_key, book, market, outcome_label,"
                  " line, odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct,"
                  " detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (cle, "unibet_be", "h2h", "home", None, 2.5, 0.5, 2.0, ev,
                   1.0, now.isoformat()))
    c.commit()
    c.close()

    rep = _cmd("/test TENNIS", st, cfg)
    assert "rejeu de 3" in rep.texte
    assert "retenues : <b>2</b>" in rep.texte
    assert "Aucun message n'a été envoyé" in rep.texte
    assert "coupé" in rep.texte     # le canal est inactif, il faut le dire


def test_tester_un_canal_inexistant(st, cfg):
    assert "introuvable" in _cmd("/test FANTOME", st, cfg).texte


# ══ CE QUI NE DOIT PAS BOUGER ══════════════════════════════════════════
def test_les_trois_canaux_traduits_de_env_sont_intacts(st, cfg):
    """L'interface ne doit modifier aucune regle des canaux existants tant
    que personne n'y touche."""
    from src.channels import depuis_config, installer
    complet = TelegramConfig(bot_token="j", chat_id=ADMIN,
                             premium_chat_id="P", critical_chat_id="C",
                             premium_hi_sports_exclus=("tennis",))
    attendus = depuis_config(complet)
    installer(st, attendus, print_fn=lambda _s: None)
    _cmd("/nouveau AUTRE -100777", st, cfg)
    _clic("cx:r+:AUTRE", st, cfg)
    obtenus = {c.nom: c for c in charger(st)}
    for a in attendus:
        assert obtenus[a.nom].regles == a.regles, a.nom
        assert obtenus[a.nom].actif == a.actif
        assert obtenus[a.nom].exclusif == a.exclusif


# ══ les gardes des trois ecritures ajoutees a Storage ══════════════════
def test_update_channel_rule_refuse_une_colonne_inconnue(st):
    """Ce chemin est atteint depuis Telegram. Une liste blanche est la seule
    facon d'etre certain qu'un nom de colonne ne vienne jamais du dehors —
    `UPDATE channel_rules SET <x>=?` n'est pas parametrable autrement."""
    cid = st.create_channel("C", "X")
    rid = st.add_channel_rule(cid)
    for interdit in ("id", "channel_id", "nom", "actif"):
        with pytest.raises(ValueError, match="colonnes inconnues"):
            st.update_channel_rule(rid, **{interdit: 1})


def test_update_channel_rule_refuse_une_phase_inconnue(st):
    cid = st.create_channel("C", "X")
    rid = st.add_channel_rule(cid)
    with pytest.raises(ValueError, match="phase inconnue"):
        st.update_channel_rule(rid, phase="mi-temps")


def test_delete_rule_value_normalise_comme_l_ecriture(st):
    """`add_rule_value` met en minuscules et retire les espaces. Si la
    suppression ne faisait pas de meme, une valeur posee par bouton
    deviendrait ineffacable."""
    cid = st.create_channel("C", "X")
    rid = st.add_channel_rule(cid)
    st.add_rule_value(rid, "sport", "  Tennis ")
    st.delete_rule_value(rid, "sport", "TENNIS")
    assert st.load_channel_rows()[2] == []


def test_sports_seen_est_dynamique(st):
    """Meme philosophie que `books_seen` : la liste vient des detections, pas
    d'un inventaire tenu a la main."""
    from datetime import datetime, timezone
    assert st.sports_seen() == []
    c = sqlite3.connect(str(st.path))
    for sport in ("tennis", "soccer"):
        cle = f"202609011800::{sport}a__vs__{sport}b"
        c.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                  (cle, sport, "L", "a", "b", "2026-09-01T18:00:00+00:00"))
        c.execute("INSERT INTO value_bets(event_key, book, market, outcome_label,"
                  " line, odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct,"
                  " detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (cle, "unibet_be", "h2h", "home", None, 2.0, 0.5, 2.0, 10.0,
                   1.0, datetime.now(timezone.utc).isoformat()))
    c.commit()
    c.close()
    assert st.sports_seen() == ["soccer", "tennis"]


# ══ lisibilite des libelles ════════════════════════════════════════════
def test_les_libelles_sont_lisibles():
    assert ct.joli("unibet_be") == "Unibet BE"
    assert ct.joli("starcasino_sport") == "Starcasino Sport"
    assert ct.joli("golden_palace") == "Golden Palace"
    assert ct.joli("h2h") == "H2H"
    assert ct.joli("btts") == "BTTS"
    assert ct.joli("soccer") == "Soccer"


def test_le_clavier_affiche_les_libelles_pas_les_cles(st, cfg):
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rep = _clic(f"cx:d:{ids[0]}:book", st, cfg)
    libelles = [b["text"] for row in rep.clavier["inline_keyboard"] for b in row]
    assert any("Unibet BE" in x for x in libelles), libelles
    assert not any("unibet_be" in x for x in libelles), libelles


def test_seuls_les_marches_REELLEMENT_detectes_sont_proposes(st, cfg):
    """L'enum porte BTTS, handicap et les mi-temps. Les proposer laisserait
    croire qu'un filtre les couvrira, alors qu'il n'attrapera jamais rien."""
    from datetime import datetime, timezone
    c = sqlite3.connect(str(st.path))
    for marche in ("h2h", "totals"):
        cle = f"202609011800::{marche}a__vs__{marche}b"
        c.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                  (cle, "soccer", "L", "a", "b", "2026-09-01T18:00:00+00:00"))
        c.execute("INSERT INTO value_bets(event_key, book, market, outcome_label,"
                  " line, odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct,"
                  " detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (cle, "unibet_be", marche, "home", None, 2.0, 0.5, 2.0, 10.0,
                   1.0, datetime.now(timezone.utc).isoformat()))
    c.commit()
    c.close()
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rep = _clic(f"cx:d:{ids[0]}:market", st, cfg)
    libelles = " ".join(b["text"] for row in rep.clavier["inline_keyboard"]
                        for b in row)
    assert "H2H" in libelles and "Totals" in libelles
    assert "BTTS" not in libelles and "Handicap" not in libelles, libelles


def test_sans_aucune_detection_un_repli_raisonnable(st, cfg):
    """Base neuve : plutot que zero bouton, les deux marches du projet."""
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rep = _clic(f"cx:d:{ids[0]}:market", st, cfg)
    libelles = " ".join(b["text"] for row in rep.clavier["inline_keyboard"]
                        for b in row)
    assert "H2H" in libelles and "Totals" in libelles


def test_les_dimensions_ont_un_nom_francais(st, cfg):
    """`market` et `league` sont des noms de colonnes, pas des mots
    d'interface."""
    _canal(st)
    _clic("cx:r+:TENNIS", st, cfg)
    _, ids, _ = ct._canal_et_regles(st, "TENNIS")
    rid = ids[0]
    assert _clic(f"cx:d:{rid}:market", st, cfg).texte.startswith("<b>Marché</b>")
    assert _clic(f"cx:d:{rid}:sport", st, cfg).texte.startswith("<b>Sport</b>")
    _clic(f"cx:t:{rid}:market:totals", st, cfg)
    assert "Marché ∈ {Totals}" in ct._vue(st, "TENNIS", edite=False).texte
