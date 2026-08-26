"""Collecteur Unibet LIVE : sondage, instantané, rapprochement. §PHASE 5

Tous ces tests tournent SANS RÉSEAU. Le jeu d'essai reprend EXACTEMENT les
champs que `parse_listview` lit en production — `homeName`, `awayName`,
`start`, `id`, `betOfferType.id`, `outcomes[].odds/type/line` — et non une
forme imaginée : un test bâti sur un format supposé ne prouve rien du format
réel.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.models import Book, MarketType
from src.storage import Storage
from src.unibet_live import (
    CycleStats, Instantane, UnibetLive, apparier, collecter, resume_global)

MAINTENANT = datetime(2026, 8, 25, 17, 30, tzinfo=timezone.utc)
KO = "2026-08-25T17:00:00Z"


def _evt(sid, home, away, offers):
    return {"event": {"id": sid, "homeName": home, "awayName": away,
                      "start": KO, "group": "Sweden"},
            "betOffers": offers}


def _h2h(h=2500, d=3200, a=2800):
    """betOfferType 2 = 1X2. Kambi met les cotes au millième."""
    return {"betOfferType": {"id": 2},
            "outcomes": [{"type": "OT_ONE", "odds": h},
                         {"type": "OT_CROSS", "odds": d},
                         {"type": "OT_TWO", "odds": a}]}


def _totals(ligne=2500, over=1900, under=1950):
    return {"betOfferType": {"id": 6},
            "outcomes": [{"type": "OT_OVER", "odds": over, "line": ligne},
                         {"type": "OT_UNDER", "odds": under, "line": ligne}]}


def _handicap(ligne=-1000, un=1850, deux=1950):
    """betOfferType 11 = handicap. Doit être ÉCARTÉ."""
    return {"betOfferType": {"id": 11},
            "outcomes": [{"type": "OT_ONE", "odds": un, "line": ligne},
                         {"type": "OT_TWO", "odds": deux, "line": ligne}]}


PAYLOAD = {"events": [
    _evt(1001, "Örebro SK", "Varbergs BoIS", [_h2h(), _totals(), _handicap()]),
    _evt(1002, "Östers IF", "GIF Sundsvall", [_h2h(1500, 4000, 6000)]),
]}


class _FauxScraper:
    """Rejoue des payloads, ou lève. Aucun réseau."""

    def __init__(self, suite):
        self._suite = list(suite)
        self.appels = 0
        self.fermé = False
        self.chemins = []

    def fetch_listview(self, sport="soccer", path_suffix=""):
        self.appels += 1
        self.chemins.append(path_suffix)
        p = self._suite.pop(0) if self._suite else PAYLOAD
        if isinstance(p, Exception):
            raise p
        return p

    def close(self):
        self.fermé = True


def _sonde(suite=None, horloge=None):
    return UnibetLive("soccer", scraper=_FauxScraper(suite or [PAYLOAD]),
                      horloge=horloge or (lambda: MAINTENANT))


# ── sondage ──────────────────────────────────────────────────────────────
def test_un_sondage_mesure_ce_qu_il_a_vu():
    live = _sonde()
    c = live.sonder()
    assert c.erreur is None
    assert c.matchs == 2
    assert c.betoffers == 4          # 3 sur le premier match, 1 sur le second
    assert c.h2h == 6                # 3 issues × 2 matchs
    assert c.totals == 2
    assert c.quotes == 8
    assert c.duree_ms >= 0
    assert c.fraicheur_sec == 0.0


def test_le_handicap_est_ecarte():
    """La convention de ligne d'Unibet n'est pas vérifiée face à AsianOdds.
    L'inclure rejouerait les value bets fantômes de `detection.py:242`."""
    live = _sonde()
    live.sonder()
    assert all(q.market in (MarketType.H2H, MarketType.TOTALS)
               for q in live.instantane.quotes)
    assert not [q for q in live.instantane.quotes
                if q.market == MarketType.HANDICAP]


def test_la_vue_in_play_est_bien_celle_demandee():
    live = _sonde()
    live.sonder()
    assert live._scraper.chemins == ["all/all/all/in-play"]


def test_les_cotes_sont_ramenees_a_l_echelle_decimale():
    """Kambi met tout au millième : 2500 vaut 2.50, pas 2500."""
    live = _sonde()
    live.sonder()
    h = {q.outcome.label: q.decimal_odd for q in live.instantane.quotes
         if q.market == MarketType.H2H and q.source_event_id == "1001"}
    assert h == {"home": 2.5, "draw": 3.2, "away": 2.8}
    t = [q for q in live.instantane.quotes if q.market == MarketType.TOTALS]
    assert {q.outcome.line for q in t} == {2.5}


def test_les_noms_sont_relus_du_payload():
    """`parse_listview` construit la clé puis JETTE les noms, or c'est sur eux
    que le rapprochement se fait."""
    live = _sonde()
    live.sonder()
    assert live.instantane.noms["1001"] == ("Örebro SK", "Varbergs BoIS")
    assert live.instantane.noms["1002"] == ("Östers IF", "GIF Sundsvall")


# ── changement réel ──────────────────────────────────────────────────────
def test_un_prix_identique_n_est_PAS_un_changement():
    """La leçon d'AsianOdds : un message reçu n'est pas un prix qui bouge.
    Sans ce compteur on prendrait la cadence de sondage pour celle du marché."""
    live = _sonde([PAYLOAD, PAYLOAD, PAYLOAD])
    assert live.sonder().change is False      # premier : rien à comparer
    assert live.sonder().change is False
    assert live.sonder().change is False


def test_un_prix_qui_bouge_est_un_changement():
    bouge = {"events": [_evt(1001, "Örebro SK", "Varbergs BoIS",
                             [_h2h(2600, 3200, 2800)])]}
    live = _sonde([PAYLOAD, bouge])
    live.sonder()
    assert live.sonder().change is True


# ── erreurs et instantané ────────────────────────────────────────────────
def test_un_sondage_en_erreur_ne_leve_pas_et_se_mesure():
    live = _sonde([RuntimeError("502 Bad Gateway")])
    c = live.sonder()
    assert c.erreur and "502" in c.erreur
    assert c.quotes == 0


def test_un_echec_CONSERVE_l_instantane_precedent_en_le_vieillissant():
    """L'effacer ferait disparaître toute la couverture sur un hoquet réseau ;
    le garder sans son âge ferait travailler sur des prix morts."""
    horloge = iter([MAINTENANT, MAINTENANT,
                    MAINTENANT + timedelta(seconds=7),
                    MAINTENANT + timedelta(seconds=7)])
    live = UnibetLive("soccer", scraper=_FauxScraper(
        [PAYLOAD, RuntimeError("timeout")]),
        horloge=lambda: next(horloge))
    live.sonder()
    avant = list(live.instantane.quotes)
    c = live.sonder()
    assert c.erreur is not None
    assert live.instantane.quotes == avant, "l'instantané a été effacé"
    assert c.fraicheur_sec == pytest.approx(7.0)


def test_un_sondage_reussi_REMPLACE_l_instantane_en_entier():
    """Une cote absente du dernier sondage n'est plus offerte. La garder
    ferait calculer une value sur un marché suspendu."""
    reduit = {"events": [_evt(1002, "Östers IF", "GIF Sundsvall", [_h2h()])]}
    live = _sonde([PAYLOAD, reduit])
    live.sonder()
    c = live.sonder()
    assert set(live.instantane.noms) == {"1002"}, "un match disparu a survécu"
    assert c.disparus == 1
    assert all(q.source_event_id == "1002" for q in live.instantane.quotes)


# ── boucle bornée ────────────────────────────────────────────────────────
def test_la_boucle_respecte_sa_duree():
    attentes = []
    cycles = collecter(duree_sec=0, scraper=_FauxScraper([PAYLOAD] * 10),
                       dormir=attentes.append, horloge=lambda: MAINTENANT,
                       log=lambda *a: None)
    assert cycles == [] or len(cycles) <= 1


class _Assez(Exception):
    """Sort de la boucle depuis `dormir`, sans dépendre d'une horloge."""


def test_le_repli_double_sur_erreur_et_ne_descend_jamais_sous_la_periode():
    """Accélérer quand un serveur refuse est le meilleur moyen d'être bloqué."""
    attentes = []

    def dormir(s):
        attentes.append(s)
        if len(attentes) >= 5:
            raise _Assez

    with pytest.raises(_Assez):
        collecter(duree_sec=999, periode_sec=5.0,
                  scraper=_FauxScraper([RuntimeError("429")] * 3 + [PAYLOAD] * 9),
                  dormir=dormir, horloge=lambda: MAINTENANT,
                  log=lambda *a: None)

    assert attentes[:3] == [10.0, 20.0, 40.0], attentes
    assert attentes[3] == 5.0, "le succès doit ramener à la période nominale"
    assert min(attentes) >= 5.0
    assert max(attentes) <= 60.0


def test_on_abandonne_apres_trop_d_echecs():
    from src.unibet_live import ECHECS_MAX
    faux = _FauxScraper([RuntimeError("503")] * 50)
    cycles = collecter(duree_sec=999, scraper=faux, dormir=lambda _: None,
                       horloge=lambda: MAINTENANT, log=lambda *a: None)
    assert len(cycles) == ECHECS_MAX
    assert all(c.erreur for c in cycles)


def test_le_scraper_est_toujours_referme():
    faux = _FauxScraper([PAYLOAD])
    collecter(duree_sec=0, scraper=faux, dormir=lambda _: None,
              horloge=lambda: MAINTENANT, log=lambda *a: None)
    assert faux.fermé


# ── rapprochement avec nos events ────────────────────────────────────────
def _db(tmp_path, home="Orebro SK", away="Varbergs BoIS", cle="k_orebro"):
    db = Storage(tmp_path / "t.db")
    db.upsert_events([(cle, "soccer", "Sweden - Superettan", home, away,
                       (MAINTENANT - timedelta(minutes=30)).isoformat())])
    return db


def test_les_cotes_sont_reclees_sur_NOS_event_key(tmp_path):
    db = _db(tmp_path)
    live = _sonde()
    live.sonder()
    a = apparier(live.instantane, db, MAINTENANT)
    assert a.matchs_vus == 2
    assert a.matchs_apparies == 1
    assert {q.event_key for q in a.quotes} == {"k_orebro"}
    assert all(q.from_live_feed for q in a.quotes)
    assert all(q.book == Book.UNIBET_BE for q in a.quotes)


def test_la_cle_unibet_est_conservee_pour_l_enquete(tmp_path):
    db = _db(tmp_path)
    live = _sonde()
    live.sonder()
    a = apparier(live.instantane, db, MAINTENANT)
    assert all(q.book_event_key and q.book_event_key != q.event_key
               for q in a.quotes)


def test_un_match_inconnu_est_compte_avec_son_motif(tmp_path):
    db = _db(tmp_path)
    live = _sonde()
    live.sonder()
    a = apparier(live.instantane, db, MAINTENANT)
    assert a.sans_event_key == 1
    assert "aucun candidat assez proche" in "".join(a.motifs)


def test_une_orientation_inversee_permute_le_h2h_ET_PAS_les_totaux(tmp_path):
    """Notre domicile est leur extérieur. Le nul ne bouge pas, et « plus de
    2.5 buts » ne dépend pas de qui reçoit — cette invariance est établie et
    testée côté AsianOdds, la rejouer ici serait une régression."""
    db = _db(tmp_path, home="Varbergs BoIS", away="Orebro SK", cle="k_envers")
    live = _sonde()
    live.sonder()
    a = apparier(live.instantane, db, MAINTENANT)

    assert a.inversions == 1
    h = {q.outcome.label: q.decimal_odd for q in a.quotes
         if q.market == MarketType.H2H}
    assert h["home"] == 2.8, "notre home doit porter la cote de LEUR away"
    assert h["away"] == 2.5
    assert h["draw"] == 3.2, "le nul ne se permute pas"
    t = {q.outcome.label: q.decimal_odd for q in a.quotes
         if q.market == MarketType.TOTALS}
    assert t == {"over": 1.9, "under": 1.95}, "les totaux ont été permutés"


def test_sans_candidat_rien_n_est_apparie(tmp_path):
    db = Storage(tmp_path / "vide.db")
    live = _sonde()
    live.sonder()
    a = apparier(live.instantane, db, MAINTENANT)
    assert a.quotes == []
    assert a.matchs_apparies == 0


def test_un_instantane_vide_ne_plante_pas(tmp_path):
    assert apparier(Instantane(), _db(tmp_path), MAINTENANT).quotes == []


# ── aucune écriture ──────────────────────────────────────────────────────
def test_le_collecteur_n_ecrit_RIEN_en_base(tmp_path):
    """Contrainte explicite de cette étape : mémoire seule."""
    db = _db(tmp_path)
    live = _sonde()
    live.sonder()
    apparier(live.instantane, db, MAINTENANT)
    assert db.market_state() == []
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0


# ── résumé ───────────────────────────────────────────────────────────────
def test_le_resume_ne_ment_pas_quand_tout_echoue():
    c = CycleStats(debut=MAINTENANT, fin=MAINTENANT, duree_ms=1.0,
                   erreur="boom")
    assert "AUCUN réussi" in resume_global([c])


def test_la_permutation_ne_touche_QUE_le_h2h():
    """Le garde-fou de marché de `_permuter_h2h` n'est pas décoratif.

    `HANDICAP` porte les mêmes labels `home`/`away` que le 1X2. Sans ce
    garde-fou, l'inclure un jour permuterait ses labels SANS inverser le signe
    de la ligne — deux erreurs qui ne s'annulent pas, et exactement le défaut
    corrigé côté AsianOdds. Les totaux, eux, sont protégés par la table de
    labels ; ce test fige le cas qui ne l'est pas.
    """
    from src.models import Outcome, OddQuote
    from src.unibet_live import _permuter_h2h

    hdp = OddQuote(event_key="k", book=Book.UNIBET_BE,
                   market=MarketType.HANDICAP,
                   outcome=Outcome("home", -1.0), decimal_odd=1.85,
                   fetched_at=MAINTENANT, source_event_id="1")
    apres = _permuter_h2h(hdp)
    assert apres.outcome.label == "home", "le handicap a été permuté"
    assert apres.outcome.line == -1.0, "la ligne aurait changé de camp"

    h2h = OddQuote(event_key="k", book=Book.UNIBET_BE, market=MarketType.H2H,
                   outcome=Outcome("home"), decimal_odd=2.5,
                   fetched_at=MAINTENANT, source_event_id="1")
    assert _permuter_h2h(h2h).outcome.label == "away"


def test_on_compte_COMBIEN_de_selections_bougent_pas_seulement_si():
    """Le booléen `change` ne peut pas arbitrer une cadence : mesuré le 26/08
    à 5 s, il saturait à 98 % — sur 168 sélections vivantes, qu'au moins une
    bouge est presque certain, et il aurait saturé tout autant à 30 s. Seule
    la PART de sélections modifiées dit si sonder plus vite sert."""
    une_bouge = {"events": [_evt(1001, "Örebro SK", "Varbergs BoIS",
                                 [_h2h(2600, 3200, 2800), _totals()]),
                            _evt(1002, "Östers IF", "GIF Sundsvall",
                                 [_h2h(1500, 4000, 6000)])]}
    live = _sonde([PAYLOAD, une_bouge])
    premier = live.sonder()
    assert premier.selections_suivies == 0, "rien à comparer au premier sondage"

    c = live.sonder()
    assert c.change is True
    assert c.selections_suivies == 8, "les 8 sélections communes"
    assert c.selections_modifiees == 1, "une seule cote a bougé, pas huit"


def test_un_marche_qui_apparait_n_est_pas_un_prix_qui_bouge():
    """On ne compare que les sélections présentes DES DEUX CÔTÉS : sinon
    l'ouverture d'un marché gonflerait le taux et ferait croire à une
    agitation qui n'existe pas."""
    plus = {"events": [_evt(1001, "Örebro SK", "Varbergs BoIS",
                            [_h2h(), _totals(), _totals(3500, 2100, 1750)]),
                       _evt(1002, "Östers IF", "GIF Sundsvall",
                            [_h2h(1500, 4000, 6000)])]}
    live = _sonde([PAYLOAD, plus])
    live.sonder()
    c = live.sonder()
    assert c.selections_modifiees == 0, "une ouverture comptée comme un mouvement"
    assert c.selections_suivies == 8


def test_le_resume_publie_la_part_de_selections_bougees():
    live = _sonde([PAYLOAD, PAYLOAD])
    r = resume_global([live.sonder(), live.sonder()])
    assert "SÉLECTIONS bougées" in r
    assert "0/8 (0.00 %)" in r


def _cycle(duree, bougees, suivies=8):
    return CycleStats(debut=MAINTENANT, fin=MAINTENANT, duree_ms=duree,
                      selections_suivies=suivies, selections_modifiees=bougees,
                      change=bool(bougees))


def test_le_resume_separe_la_duree_des_sondages_qui_rendent_du_neuf():
    """C'est cet écart qui a révélé le cache CDN de ~2 s côté Kambi, et donc
    ce qui borne la cadence utile. Un p50 global l'aurait noyé : ici 50 ms et
    200 ms se moyennent en 125 ms, un chiffre qui n'existe nulle part et qui
    ne dit rien. Le test échoue si les deux populations sont refondues."""
    r = resume_global([_cycle(200.0, 3), _cycle(50.0, 0),
                       _cycle(200.0, 5), _cycle(50.0, 0)])
    assert "rend du NEUF : 200 ms" in r
    assert "sans rien de neuf : 50 ms" in r
    assert "125 ms" not in r, "les deux populations ont été moyennées"


def test_une_population_vide_dit_n_a_et_n_emprunte_pas_l_autre_chiffre():
    """Si aucun sondage ne rend du neuf, le résumé doit le dire, pas recopier
    la durée des sondages servis du cache : ce serait inventer une mesure
    qu'on n'a pas faite, et masquer précisément le cas où sonder ne sert à
    rien du tout."""
    r = resume_global([_cycle(50.0, 0), _cycle(50.0, 0)])
    assert "rend du NEUF : n/a" in r
    assert "sans rien de neuf : 50 ms" in r
