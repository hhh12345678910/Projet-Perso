"""Collecteur LIVE AsianOdds : décodage, rapprochement, boucle.

Tous ces tests tournent SANS RÉSEAU. Les messages sont ceux réellement
capturés le 23/08 sur la VM — pas des inventions : un test bâti sur un format
imaginé ne prouve rien du format réel.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.asianodds_live import (
    Candidat, Stats, candidats_en_cours, collect, decode_feed_score,
    match_live_event, normalise_evf, parse_line, signed_handicap)
from src.models import Book, MarketType
from src.storage import Storage

# Message EVF réel (Crvena Zvezda — Cukaricki, capture du 23/08).
EVF_REEL = {
    "LID": -406813183, "GID": "163460123421", "HN": "Crvena Zvezda",
    "AN": "Cukaricki", "LGID": -1145077806, "LN": "SERBIA SUPER LIGA",
    "MTID": 0, "MTCHID": "1634601234", "MT": "Live",
    "SO": "08/23/2026 07:00:00.000 PM", "ST": 1787511600000,
    "HS": 0, "AS": 0, "EL": 45, "IGM": 67, "RCA": 0, "RCH": 0, "P": 0,
    "F": 1, "FFT": 1, "FHT": 1, "S": 1787505194297, "PID": "", "SPMT": "0",
    "IPL": False, "FTHDIFF": "1.699", "FTOUDIFF": "1.075",
    "FTID": "1577081", "FTHDP": "1.5", "FTGOAL": "2.5-3",
    "FTHDPID": "1577081_FT_1.51", "FTOUID": "1634601234_FT_2.5-3OU",
    "HTID": "1634601234_x", "HTHDP": "0.5", "HTGOAL": "0.5-1",
    "FTXHODD": "1.261", "FTXAODD": "8.741", "FTXDODD": "5.93",
    "FTHHODD": "0.763", "FTHAODD": "-0.936",
    "FTOODDS": "0.442", "FTOUODDS": "-0.633",
    "HTXHODD": "1.662", "HTXAODD": "8.26", "HTXDODD": "2.70",
    "HTHHODD": "0.662", "HTHAODD": "-0.855",
    "HTOODD": "0.344", "HTUODD": "-0.491",
    "PC": "0", "STP": 1, "OF": "00", "LSID": 1150698481, "FID": "MDow",
    "ISUPC": 0, "CVAL": "", "OSTP": 1, "OLN": "SERBIA - SUPER LIGA",
    "OHN": "Crvena Zvezda", "OAN": "Cukaricki",
    "OKO": "8/24/2026 1:00:00 AM", "MIML": False,
}

# Le même, en décimal (état normal du flux) : ce sont les valeurs décimales
# observées sur les mêmes marchés quelques secondes plus tard.
EVF_DECIMAL = dict(EVF_REEL, **{
    "FTXHODD": "1.261", "FTXAODD": "8.741", "FTXDODD": "5.93",
    "FTHHODD": "1.763", "FTHAODD": "2.040",
    "FTOODDS": "1.442", "FTOUODDS": "2.633",
    "HTXHODD": "1.662", "HTXAODD": "8.26", "HTXDODD": "2.70",
    "HTHHODD": "1.662", "HTHAODD": "1.855",
    "HTOODD": "1.344", "HTUODD": "2.491",
})

KEY = "2026-08-23T17:00|crvena|cukaricki"


# ── décodage élémentaire ─────────────────────────────────────────────────
def test_decode_feed_score():
    assert decode_feed_score("MDow") == "0:0"
    assert decode_feed_score("MToy") == "1:2"
    assert decode_feed_score("MToxMQ==") == "1:11"


@pytest.mark.parametrize("brut", [None, "", "pas du base64!!", "YWJj"])
def test_decode_feed_score_refuse_au_lieu_d_inventer(brut):
    assert decode_feed_score(brut) is None


def test_fid_correspond_bien_au_score_du_message():
    """La propriété qui fait tout l'intérêt du champ, sur un message réel."""
    assert decode_feed_score(EVF_REEL["FID"]) == \
        f"{EVF_REEL['HS']}:{EVF_REEL['AS']}"


@pytest.mark.parametrize("brut,attendu", [
    ("2.5", 2.5), ("0.0", 0.0), ("1.5-2", 1.75), ("2.5-3", 2.75),
    ("0-0.5", 0.25), ("3.5-4", 3.75), ("", None), (None, None), ("x", None),
])
def test_parse_line(brut, attendu):
    assert parse_line(brut) == attendu


@pytest.mark.parametrize("ligne,favori,attendu", [
    (1.5, 1, -1.5),      # domicile favori => handicap négatif pour lui
    (1.5, 2, 1.5),       # extérieur favori
    (0.0, 0, 0.0),       # pick'em
    (0.25, 1, -0.25),    # ligne quart
    (None, 1, None),
])
def test_signed_handicap_suit_la_convention_pinnacle(ligne, favori, attendu):
    assert signed_handicap(ligne, favori) == attendu


# ── normalisation ────────────────────────────────────────────────────────
def test_rejette_l_echelle_malay_au_lieu_de_la_convertir():
    """Piège mesuré : 42 lignes sur 1955 arrivent en Malay au tout début d'un
    abonnement. Une conversion silencieuse ferait entrer un prix faux."""
    lignes = normalise_evf(EVF_REEL, KEY)
    marches = {l.market for l in lignes}
    # Le 1X2 est en décimal dans ce message et doit passer...
    assert MarketType.H2H in marches
    # ...mais le handicap et l'O/U sont en Malay et doivent être écartés.
    assert MarketType.HANDICAP not in marches
    assert MarketType.TOTALS not in marches


def test_normalise_un_message_decimal_complet():
    lignes = {(l.market, l.outcome_label): l for l in normalise_evf(EVF_DECIMAL, KEY)}

    assert lignes[(MarketType.H2H, "home")].odd == 1.261
    assert lignes[(MarketType.H2H, "draw")].odd == 5.93
    assert lignes[(MarketType.H2H, "away")].odd == 8.741
    assert lignes[(MarketType.H2H, "home")].line is None

    # FTGOAL "2.5-3" => ligne quart à 2.75, même ligne des deux côtés.
    assert lignes[(MarketType.TOTALS, "over")].line == 2.75
    assert lignes[(MarketType.TOTALS, "under")].line == 2.75
    assert lignes[(MarketType.TOTALS, "over")].odd == 1.442

    # FTHDP "1.5" + FFT=1 (domicile favori) => home -1.5 / away +1.5
    assert lignes[(MarketType.HANDICAP, "home")].line == -1.5
    assert lignes[(MarketType.HANDICAP, "away")].line == 1.5
    assert lignes[(MarketType.HANDICAP, "home")].odd == 1.763


def test_le_handicap_mi_temps_est_ignore_et_non_invente():
    """`MarketType` n'a pas de HANDICAP_H1. Le message en porte pourtant un
    (HTHDP/HTHHODD/HTHAODD) : il ne doit produire AUCUNE ligne."""
    lignes = normalise_evf(EVF_DECIMAL, KEY)
    assert all(l.market != MarketType.HANDICAP or l.line in (-1.5, 1.5)
               for l in lignes)
    assert {l.market for l in lignes} <= {
        MarketType.H2H, MarketType.H2H_H1, MarketType.TOTALS,
        MarketType.TOTALS_H1, MarketType.HANDICAP}


def test_le_contexte_live_est_porte_par_chaque_ligne():
    l = normalise_evf(EVF_DECIMAL, KEY)[0]
    assert l.feed_score == "0:0"
    assert l.home_score == 0 and l.away_score == 0
    assert l.igm == 67
    assert l.league == "SERBIA SUPER LIGA"
    assert l.observed_at == datetime.fromtimestamp(
        EVF_DECIMAL["S"] / 1000, tz=timezone.utc)


def test_un_1x2_ampute_est_rejete_en_entier():
    """Deux issues sur trois ne sont pas déviguables : mieux vaut rien."""
    ampute = dict(EVF_DECIMAL, FTXDODD="")
    assert not [l for l in normalise_evf(ampute, KEY)
                if l.market == MarketType.H2H]


@pytest.mark.parametrize("champ", ["S", "HS", "AS"])
def test_message_sans_contexte_indispensable_ne_produit_rien(champ):
    casse = dict(EVF_DECIMAL)
    casse.pop(champ)
    assert normalise_evf(casse, KEY) == []


def test_as_upsert_row_a_la_forme_attendue_par_storage(tmp_path):
    """Le contrat entre les deux modules, vérifié en écrivant vraiment."""
    db = Storage(tmp_path / "t.db")
    t = datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc)
    rows = [l.as_upsert_row(t) for l in normalise_evf(EVF_DECIMAL, KEY)]
    assert db.upsert_live_state(rows) == len(rows)

    etat = db.market_state(event_key=KEY)
    assert len(etat) == len(rows)
    assert {r["book"] for r in etat} == {Book.ASIANODDS.value}
    assert all(r["is_live"] == 1 for r in etat)
    assert all(r["feed_score"] == "0:0" for r in etat)


# ── rapprochement ────────────────────────────────────────────────────────
def test_rapproche_sur_les_noms_pas_sur_l_horaire():
    cands = [Candidat("k1", "Crvena Zvezda", "Cukaricki"),
             Candidat("k2", "Partizan", "Vojvodina")]
    assert match_live_event("Crvena Zvezda", "Cukaricki", cands).event_key == "k1"


def test_rapprochement_tolere_l_inversion_domicile_exterieur():
    cands = [Candidat("k1", "Cukaricki", "Crvena Zvezda")]
    assert match_live_event("Crvena Zvezda", "Cukaricki", cands).event_key == "k1"


def test_rapprochement_refuse_un_inconnu():
    cands = [Candidat("k1", "Partizan", "Vojvodina")]
    assert match_live_event("Crvena Zvezda", "Cukaricki", cands) is None


def test_rapprochement_refuse_de_deviner_entre_deux_rencontres():
    """Deux candidats presque aussi bons ET RÉELLEMENT DIFFÉRENTS : on ne
    tranche pas. « Dinamo » désigne aussi bien Minsk que Moscou ; deviner
    écrirait le prix d'un match sous la clé de l'autre.

    L'exemple d'origine utilisait deux candidats IDENTIQUES, qui sont un
    doublon de notre base et non une ambiguïté — voir
    test_un_doublon_de_notre_base_ne_fait_plus_perdre_le_match."""
    cands = [Candidat("k1", "Dinamo Minsk", "Zenit"),
             Candidat("k2", "Dinamo Moscow", "Zenit")]
    assert match_live_event("Dinamo", "Zenit", cands) is None


def test_une_ressemblance_ne_suffit_pas_a_declarer_un_doublon():
    """Garde-fou de la correction elle-même. « Sporting CP » et « Sporting
    Gijon » se ressemblent au-dessus du seuil de rapprochement : les traiter
    comme un doublon écrirait le prix du Sporting portugais sous la clé du
    Sporting espagnol. Le doublon interne se reconnaît à l'ÉGALITÉ des noms,
    il ne se devine pas."""
    from src.asianodds_live import _meme_rencontre, evaluer_appariement
    a = Candidat("k1", "Sporting CP", "Benfica")
    b = Candidat("k2", "Sporting Gijon", "Benfica")
    assert not _meme_rencontre(a, b)
    r = evaluer_appariement("Sporting", "Benfica", [a, b])
    assert not r.reussi
    assert "DEUX rencontres" in r.motif


def test_un_doublon_de_notre_base_ne_fait_plus_perdre_le_match():
    """Le cas mesuré le 24/08 : « Torpedo Zhodino — Dnepr Mogilev » identifié
    à 96/100, mais perdu parce que `events` le porte deux fois. Mesuré en
    base : jusqu'à CINQ clés pour une rencontre, l'horaire seul changeant
    (blackpool—lincolncity 14:00/15:00/16:00/18:45). Le défaut est chez nous ;
    la source ne doit pas en payer le prix."""
    from src.asianodds_live import evaluer_appariement
    cands = [Candidat("202608241700::a", "torpedozhodino", "dneprmogilev"),
             Candidat("202608241800::a", "torpedozhodino", "dneprmogilev")]
    r = evaluer_appariement("Torpedo Zhodino", "Dnepr Mogilev", cands)
    assert r.reussi
    assert len(r.toutes_les_cibles) == 2, "le prix doit aller sous les DEUX clés"
    assert "doublon" in r.motif


def test_un_doublon_inverse_domicile_exterieur_est_reconnu():
    from src.asianodds_live import _meme_rencontre
    a = Candidat("k1", "Anderlecht", "Club Brugge")
    b = Candidat("k2", "Club Brugge", "Anderlecht")
    assert _meme_rencontre(a, b)


def test_candidats_en_cours_ne_lit_que_la_fenetre(tmp_path):
    db = Storage(tmp_path / "t.db")
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db.upsert_events([
        ("k_encours", "soccer", "L", "A", "B", (now - timedelta(hours=1)).isoformat()),
        ("k_vieux", "soccer", "L", "C", "D", (now - timedelta(hours=9)).isoformat()),
        ("k_demain", "soccer", "L", "E", "F", (now + timedelta(hours=5)).isoformat()),
    ])
    cles = {c.event_key for c in candidats_en_cours(db, now)}
    assert cles == {"k_encours"}


# ── boucle de collecte, sans réseau ──────────────────────────────────────
class _FausseSession:
    """Rejoue une liste de messages, puis se termine. Compte les PING."""

    def __init__(self, messages):
        self._messages = messages
        self.base_bookie = "PIN"
        self.ouverte = self.abonnee = self.fermee = False
        self.pings = 0

    def open(self):
        self.ouverte = True

    def subscribe(self, sport=1):
        self.abonnee = True

    def ping(self):
        self.pings += 1

    def messages(self, timeout=5.0):
        yield from self._messages

    def close(self):
        self.fermee = True


def _db_avec_match(tmp_path, now):
    db = Storage(tmp_path / "t.db")
    db.upsert_events([(KEY, "soccer", "SERBIA SUPER LIGA", "Crvena Zvezda",
                       "Cukaricki", (now - timedelta(hours=1)).isoformat())])
    return db


def test_collect_ecrit_les_selections_appariees(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    session = _FausseSession([{"EVF": EVF_DECIMAL}, {"EVF": EVF_DECIMAL}])

    stats = collect(db, "u", "p", session_factory=lambda: session,
                    now_fn=lambda: now, log=lambda *a: None)

    assert session.ouverte and session.abonnee and session.fermee
    assert stats.evf == 2
    assert stats.sans_event_key == 0
    etat = db.market_state(event_key=KEY)
    assert len(etat) == 12, "1X2 FT 3 + 1X2 HT 3 + O/U FT 2 + O/U HT 2 + AH FT 2"
    assert all(r["book"] == Book.ASIANODDS.value for r in etat)


def test_collect_compte_les_matchs_non_apparies_sans_ecrire(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")            # aucun événement connu
    session = _FausseSession([{"EVF": EVF_DECIMAL}])

    stats = collect(db, "u", "p", session_factory=lambda: session,
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.evf == 1 and stats.sans_event_key == 1
    assert db.market_state() == [], "une ligne non appariée a été écrite"


def test_dry_run_n_ecrit_rien(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    session = _FausseSession([{"EVF": EVF_DECIMAL}])

    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: session,
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.normalises == 12
    assert db.market_state() == [], "dry_run a écrit en base"


def test_collect_dedoublonne_le_lot_avant_ecriture(tmp_path):
    """Un marché repricé N fois dans l'intervalle ne coûte qu'une écriture."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    variantes = [{"EVF": dict(EVF_DECIMAL, FTXHODD=str(1.20 + i / 100),
                              S=EVF_DECIMAL["S"] + i * 1000)}
                 for i in range(5)]
    session = _FausseSession(variantes)

    stats = collect(db, "u", "p", session_factory=lambda: session,
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.evf == 5
    assert stats.ecrits == 12, "le lot n'a pas été dédoublonné"
    (home,) = [r for r in db.market_state(event_key=KEY)
               if r["market"] == "h2h" and r["outcome_label"] == "home"]
    assert home["odd"] == 1.24, "la dernière valeur du lot doit gagner"


def test_collect_n_ecrit_jamais_dans_quotes(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    collect(db, "u", "p", session_factory=lambda: _FausseSession(
        [{"EVF": EVF_DECIMAL}]), now_fn=lambda: now, log=lambda *a: None)
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0


def test_stats_resume_est_lisible():
    s = Stats(messages=10, evf=8, normalises=40, ecrits=40, sans_event_key=2)
    assert "appariés=6" in s.resume() and "75.0 %" in s.resume()


# ── pseudo-événements dérivés ────────────────────────────────────────────
# Team Totals, Corners, Bookings : AsianOdds les publie comme des matchs à
# part entière, portant le NOM DE LA VRAIE ÉQUIPE suivi d'un suffixe. Le
# rapprochement sur les noms ne peut pas les distinguer — vérifié :
# team_similarity("Stjarnan Team Totals Home Team", "Stjarnan") == 100.0.
# Sans filtre, leurs prix s'écriraient sous la clé du vrai match.
TEAM_TOTALS = dict(
    EVF_DECIMAL,
    HN="Crvena Zvezda Team Totals Home Team",
    AN="Cukaricki Team Totals Home Team",
    MTCHID="1634601234TTH", SPMT="5",
)
CORNERS = dict(EVF_DECIMAL,
               HN="Crvena Zvezda - No. of Corners",
               AN="Cukaricki - No. of Corners", SPMT="1")


def test_le_rapprochement_seul_ne_protege_PAS_des_derives():
    """Constat qui justifie le filtre : sans lui, le rapprochement accepte.

    Ce test documente une FAIBLESSE, pas une garantie. Si un jour il échoue
    parce que le rapprochement s'est durci, le filtre SPMT reste correct —
    mais on saura que la raison d'être a changé."""
    cands = [Candidat("vrai", "Crvena Zvezda", "Cukaricki")]
    assert match_live_event(TEAM_TOTALS["HN"], TEAM_TOTALS["AN"], cands) is not None


@pytest.mark.parametrize("message,etiquette", [
    (TEAM_TOTALS, "team totals"), (CORNERS, "corners")])
def test_les_derives_ne_sont_jamais_ecrits(tmp_path, message, etiquette):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    session = _FausseSession([{"EVF": message}])

    stats = collect(db, "u", "p", session_factory=lambda: session,
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.derives == 1, f"{etiquette} non filtré"
    assert stats.sans_event_key == 0, "le dérivé a été soumis au rapprochement"
    assert db.market_state() == [], f"un {etiquette} a corrompu market_state"


def test_le_vrai_match_passe_toujours(tmp_path):
    """Le filtre ne doit pas jeter le bébé avec l'eau du bain."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    stats = collect(db, "u", "p",
                    session_factory=lambda: _FausseSession([{"EVF": EVF_DECIMAL}]),
                    now_fn=lambda: now, log=lambda *a: None)
    assert stats.derives == 0
    assert len(db.market_state()) == 12


def test_le_taux_ignore_les_derives():
    """Compter les dérivés dans le dénominateur masquerait le vrai taux."""
    s = Stats(evf=100, derives=60, sans_event_key=10)
    assert "dérivés=60" in s.resume()
    assert "appariés=30 (75.0 %)" in s.resume(), \
        "le taux doit porter sur les 40 vrais matchs, pas sur les 100"


# ── couverture comptée par match, pas par message ────────────────────────
def test_la_couverture_compte_les_matchs_pas_les_messages(tmp_path):
    """Un match liquide reprice 200 fois, un calme 3 fois. Pondéré par
    message, le taux décrirait surtout les gros matchs."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    # 50 messages pour le MÊME match : un seul match, pas cinquante.
    session = _FausseSession([{"EVF": EVF_DECIMAL} for _ in range(50)])

    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: session,
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.evf == 50
    assert len(stats.matchs_vus) == 1
    assert len(stats.matchs_apparies) == 1
    assert len(stats.evenements_couverts) == 1
    assert "matchs AsianOdds reels=1 apparies=1 (100.0 %)" in stats.couverture()


def test_la_couverture_de_nos_evenements_est_le_taux_annonce(tmp_path):
    """Trois de nos matchs en cours, AsianOdds n'en cote qu'un : 33 %."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    db.upsert_events([
        (KEY, "soccer", "L", "Crvena Zvezda", "Cukaricki",
         (now - timedelta(hours=1)).isoformat()),
        ("k2", "soccer", "L", "Partizan", "Vojvodina",
         (now - timedelta(hours=1)).isoformat()),
        ("k3", "soccer", "L", "Radnicki", "Novi Pazar",
         (now - timedelta(hours=1)).isoformat()),
    ])
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession([{"EVF": EVF_DECIMAL}]),
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.candidats_connus == 3
    assert len(stats.evenements_couverts) == 1
    assert "couverts par AsianOdds=1 (33.3 %)" in stats.couverture()


def test_les_derives_ne_comptent_dans_aucune_couverture(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession([{"EVF": TEAM_TOTALS}]),
                    now_fn=lambda: now, log=lambda *a: None)
    assert stats.matchs_vus == set() and stats.evenements_couverts == set()


# ── filtre de sport et collisions ────────────────────────────────────────
def test_les_candidats_sont_filtres_par_sport(tmp_path):
    """Sans ce filtre, un abonnement football se compare aussi à nos matchs
    de tennis, qu'AsianOdds ne peut par construction pas couvrir : le taux
    s'effondre pour une raison étrangère à la source."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    debut = (now - timedelta(hours=1)).isoformat()
    db.upsert_events([
        ("foot", "soccer", "L", "A", "B", debut),
        ("tennis", "tennis", "ATP", "C", "D", debut),
        ("basket", "basketball", "NBA", "E", "F", debut),
    ])
    from src.asianodds_live import SPORT_FOOTBALL
    cles = {c.event_key for c in candidats_en_cours(db, now, SPORT_FOOTBALL)}
    assert cles == {"foot"}, "le tennis et le basket polluent le dénominateur"
    assert {c.event_key for c in candidats_en_cours(db, now, 3)} == {"tennis"}


def test_deux_matchs_asianodds_sur_un_seul_de_nos_evenements(tmp_path):
    """Collision : au moins un des deux rapprochements est faux. On la
    compte au lieu de la laisser passer sous un taux flatteur."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    autre = dict(EVF_DECIMAL, MTCHID="9999999")   # autre match, mêmes noms
    session = _FausseSession([{"EVF": EVF_DECIMAL}, {"EVF": autre}])

    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: session,
                    now_fn=lambda: now, log=lambda *a: None)

    assert len(stats.matchs_apparies) == 2
    assert len(stats.evenements_couverts) == 1
    assert stats.collisions == {KEY}
    assert "au moins un rapprochement est faux" in stats.couverture()


def test_pas_de_collision_signalee_quand_tout_va_bien(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession(
                        [{"EVF": EVF_DECIMAL}] * 3),
                    now_fn=lambda: now, log=lambda *a: None)
    assert stats.collisions == set()
    assert "rapprochement est faux" not in stats.couverture()


# ── diagnostic d'appariement ─────────────────────────────────────────────
def test_le_diagnostic_montre_les_deux_cotes(tmp_path):
    """« AsianOdds ne couvre pas ce match » et « le rapprochement a échoué »
    donnent le même chiffre mais appellent des travaux opposés. Le
    diagnostic doit rendre les deux listes pour qu'on tranche à l'œil."""
    from src.asianodds_live import diagnostic_appariement
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    debut = (now - timedelta(hours=1)).isoformat()
    db.upsert_events([
        (KEY, "soccer", "L", "Crvena Zvezda", "Cukaricki", debut),
        ("orphelin", "soccer", "L", "Anderlecht", "Club Brugge", debut),
    ])
    # Un match qu'aucun de nos événements ne peut approcher. Attention au
    # choix : une première version prenait « RSC Anderlecht » contre notre
    # « Anderlecht », que le rapprochement apparie CORRECTEMENT — les deux
    # listes ressortaient vides et le test ne testait rien.
    inconnu = dict(EVF_DECIMAL, MTCHID="777", HN="Kawasaki Frontale",
                   AN="Urawa Reds", LN="JAPAN J1 LEAGUE")
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession(
                        [{"EVF": EVF_DECIMAL}, {"EVF": inconnu}]),
                    now_fn=lambda: now, log=lambda *a: None)

    rapport = diagnostic_appariement(stats)
    assert "Anderlecht — Club Brugge" in rapport, "notre orphelin absent"
    assert "Kawasaki Frontale — Urawa Reds" in rapport, "le leur absent"
    assert "Crvena Zvezda" not in rapport, "un match apparié ne doit pas figurer"


def test_le_diagnostic_ne_plante_pas_sur_des_stats_vides():
    from src.asianodds_live import diagnostic_appariement
    assert "DIAGNOSTIC" in diagnostic_appariement(Stats())


def test_le_diagnostic_complet_ne_tronque_rien(tmp_path):
    """Le résumé console est tronqué à 15 lignes : c'est assez pour trancher
    « couverture ou rapprochement », pas pour vérifier un match précis. Sur
    la capture du 24/08, « Milan — Torino » figurait parmi les 19 orphelins
    NON affichés, donc invérifiable."""
    from src.asianodds_live import diagnostic_appariement
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    debut = (now - timedelta(hours=1)).isoformat()
    db.upsert_events([(f"k{i}", "soccer", "L", f"Equipe{i}", f"Adverse{i}",
                       debut) for i in range(40)])

    # Un message quelconque : c'est la boucle qui déclenche la relecture des
    # candidats, donc sans lui les deux listes sortent vides.
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession(
                        [{"EVF": EVF_DECIMAL}]),
                    now_fn=lambda: now, log=lambda *a: None)

    tronque = diagnostic_appariement(stats)
    complet = diagnostic_appariement(stats, limite=None)
    assert "et 25 autres" in tronque
    assert "autres" not in complet
    for i in range(40):
        assert f"Equipe{i} — Adverse{i}" in complet


# ── filtre de sport SUR LE FLUX (STP) ────────────────────────────────────
TENNIS = dict(EVF_DECIMAL, MTCHID="555", STP=3,
              HN="Radka Zelnickova (Sets)", AN="Federica Sacco (Sets)",
              LN="ITF WOMEN TRIESTE")


def test_un_evf_d_un_autre_sport_est_ecarte_avant_le_rapprochement(tmp_path):
    """L'abonnement demande `sportstype=1`, le serveur pousse quand même du
    tennis et de l'e-sport — mesuré le 24/08 : 55 matchs non rapprochés,
    majoritairement du tennis. Les compter comme des échecs de
    rapprochement fausse le taux et fait travailler le matcher pour rien."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession(
                        [{"EVF": EVF_DECIMAL}, {"EVF": TENNIS}]),
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.hors_sport == 1
    assert stats.sans_event_key == 0, "le tennis compté comme échec"
    assert stats.matchs_vus == {EVF_DECIMAL["MTCHID"]}
    assert "555" not in stats.asianodds_sans_match
    assert "hors_sport=1" in stats.resume()
    assert "(100.0 %)" in stats.resume(), "le taux doit ignorer les hors-sport"


def test_le_filtre_de_flux_suit_le_sport_demande(tmp_path):
    """Sur un abonnement tennis, c'est le football qui doit sortir."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    stats = collect(db, "u", "p", dry_run=True, sport=3,
                    session_factory=lambda: _FausseSession(
                        [{"EVF": EVF_DECIMAL}, {"EVF": TENNIS}]),
                    now_fn=lambda: now, log=lambda *a: None)
    assert stats.hors_sport == 1
    assert stats.matchs_vus == {"555"}


@pytest.mark.parametrize("stp", [None, "", "?", {}])
def test_un_stp_illisible_ne_fait_pas_jeter_le_message(tmp_path, stp):
    """On ne jette pas une cote sur un champ qu'on n'a pas su lire : le
    rapprochement par les noms reste seul juge."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    muet = dict(EVF_DECIMAL)
    if stp is None:
        muet.pop("STP")
    else:
        muet["STP"] = stp
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession([{"EVF": muet}]),
                    now_fn=lambda: now, log=lambda *a: None)
    assert stats.hors_sport == 0
    assert len(stats.evenements_couverts) == 1


def test_stp_texte_est_accepte_comme_nombre(tmp_path):
    """Le flux mélange `"0"` et `0` sur SPMT ; rien ne garantit que STP soit
    toujours un entier."""
    from src.asianodds_live import _meme_sport
    assert _meme_sport("1", 1) and _meme_sport(1, 1)
    assert not _meme_sport("3", 1) and not _meme_sport(3, 1)


# ── pourquoi le flux s'est arrêté ────────────────────────────────────────
def test_decrire_fermeture_rend_le_code_et_le_motif():
    from src.asianodds_live import decrire_fermeture
    import struct
    assert "1000" in decrire_fermeture(struct.pack(">H", 1000))
    assert "fermeture normale" in decrire_fermeture(struct.pack(">H", 1000))
    avec_motif = struct.pack(">H", 1008) + "Session ailleurs".encode()
    d = decrire_fermeture(avec_motif)
    assert "1008" in d and "Session ailleurs" in d
    assert "session ouverte ailleurs" in d, "le libellé du code manque"


def test_decrire_fermeture_ne_plante_pas_sur_un_payload_absurde():
    from src.asianodds_live import decrire_fermeture
    import struct
    assert decrire_fermeture(b"") == "fermeture sans code"
    assert decrire_fermeture(b"\x03") == "fermeture sans code"
    # Octets non-UTF8 dans le motif : on remplace, on ne lève pas.
    assert "9999" in decrire_fermeture(struct.pack(">H", 9999) + b"\xff\xfe")


def test_le_ws_ne_jette_plus_le_payload_de_fermeture():
    """Il était remplacé par b"" : le motif de la fermeture, seule
    information utile quand un run de 5 min s'arrête à 34 s, était perdu."""
    import struct
    from src.asianodds_live import _WS
    ws = _WS.__new__(_WS)
    payload = struct.pack(">H", 1008) + b"go away"
    ws._buf = bytes([0x88, len(payload)]) + payload
    ws.sock = None                      # tout est déjà dans le tampon
    op, data = ws.recv()
    assert op == 0x8
    assert data == payload


class _SessionQuiFerme(_FausseSession):
    """Le serveur coupe après ses messages, comme le 24/08 à 34 s."""

    def __init__(self, messages, motif="fermeture 1008 (règle violée)"):
        super().__init__(messages)
        self.fermeture = motif


def test_une_fermeture_serveur_est_nommee_et_non_confondue_avec_la_fin(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    stats = collect(db, "u", "p", dry_run=True, duration_sec=300,
                    session_factory=_SessionsSuccessives(
                        [[{"EVF": EVF_DECIMAL}]] * 20, plafond=20),
                    now_fn=lambda: now, dormir=lambda _: None,
                    log=lambda *a: None)
    assert "fermeture 1008 (règle violée)" in stats.fin_raison
    assert "fermeture 1008" in stats.resume()


def test_une_fin_par_duree_est_nommee_comme_telle(tmp_path):
    """Sinon l'utilisateur ne peut pas distinguer les deux, et c'est
    exactement ce qui a fait prendre un run vide pour un run complet."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    # duration_sec=0 : la garde de durée tombe dès le premier message.
    stats = collect(db, "u", "p", dry_run=True, duration_sec=0,
                    session_factory=lambda: _FausseSession(
                        [{"EVF": EVF_DECIMAL}] * 5),
                    now_fn=lambda: now, log=lambda *a: None)
    assert stats.fin_raison == "durée demandée atteinte"


def test_les_types_de_messages_sont_comptes(tmp_path):
    """« msg=31 evf=0 » ne dit pas si le serveur a envoyé 31 battements de
    cœur ou 31 refus."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession(
                        [{"PI": {}}, {"PI": {}}, {"ABS": {}},
                         {"EVF": EVF_DECIMAL}, {}]),
                    now_fn=lambda: now, log=lambda *a: None)
    assert stats.types_messages["PI"] == 2
    assert stats.types_messages["ABS"] == 1
    assert stats.types_messages["EVF"] == 1
    assert stats.types_messages["(silence)"] == 1
    assert "PI=2" in stats.types_recus()


def test_sans_aucun_evf_la_couverture_refuse_d_annoncer_un_taux(tmp_path):
    """0,0 % accuse AsianOdds de ne pas coter nos matchs ; la vraie cause
    peut être que le flux n'a rien envoyé. Deux causes opposées, un seul
    chiffre — donc pas de chiffre."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    db.upsert_events([(f"k{i}", "soccer", "L", f"A{i}", f"B{i}",
                       (now - timedelta(hours=1)).isoformat())
                      for i in range(85)])
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession([{"PI": {}}]),
                    now_fn=lambda: now, log=lambda *a: None)
    texte = stats.couverture()
    assert "0.0 %" not in texte, "un taux trompeur est annoncé"
    assert "AUCUN EVF" in texte
    assert "85" in texte, "le nombre de candidats reste utile"


# ── POURQUOI un rapprochement échoue ─────────────────────────────────────
def test_l_ambiguite_est_distinguee_d_un_score_trop_bas():
    """Les deux produisaient le même silence. « Torpedo Zhodino — Dnepr
    Mogilev » figurait dans NOS orphelins ET dans les non-rapprochés
    d'AsianOdds : isolé il s'apparie à 96, donc l'échec venait d'un second
    candidat trop proche — un doublon chez nous, pas un trou de couverture."""
    from src.asianodds_live import evaluer_appariement
    vrai = Candidat("k1", "torpedozhodino", "dneprmogilev")
    doublon = Candidat("k2", "torpedozhodino", "dneprmogilev")

    seul = evaluer_appariement("Torpedo Zhodino", "Dnepr Mogilev", [vrai])
    assert seul.reussi and seul.cible is vrai and seul.motif == "apparié"
    assert seul.score > 90

    # Deux clés pour la MÊME rencontre : ce n'est pas une ambiguïté, c'est
    # un doublon de notre base, et il ne doit plus coûter le match.
    ambigu = evaluer_appariement("Torpedo Zhodino", "Dnepr Mogilev",
                                 [vrai, doublon])
    assert ambigu.reussi
    assert "doublon" in ambigu.motif
    assert set(ambigu.toutes_les_cibles) == {vrai, doublon}

    loin = evaluer_appariement("Kawasaki Frontale", "Urawa Reds", [vrai])
    assert not loin.reussi
    assert "aucun candidat assez proche" in loin.motif


def test_le_motif_d_ambiguite_nomme_les_deux_rivaux():
    """Sans les noms, le motif ne dit pas OÙ chercher."""
    from src.asianodds_live import evaluer_appariement
    a = Candidat("a", "Dinamo Minsk", "Zenit")
    b = Candidat("b", "Dinamo Moscow", "Zenit")
    m = evaluer_appariement("Dinamo", "Zenit", [a, b]).motif
    assert "Dinamo Minsk" in m and "Dinamo Moscow" in m


def test_match_live_event_garde_son_contrat():
    """L'ancienne fonction reste la porte d'entrée quand le motif n'importe
    pas : aucun appelant existant ne doit avoir à changer."""
    c = Candidat("k", "Crvena Zvezda", "Cukaricki")
    assert match_live_event("Crvena Zvezda", "Cukaricki", [c]) is c
    assert match_live_event("Kawasaki", "Urawa", [c]) is None


def test_les_motifs_d_echec_sont_comptes_dans_le_diagnostic(tmp_path):
    from src.asianodds_live import diagnostic_appariement
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    debut = (now - timedelta(hours=1)).isoformat()
    # DEUX rencontres différentes que le nom d'AsianOdds ne départage pas.
    db.upsert_events([
        ("k1", "soccer", "L", "Dinamo Minsk", "Zenit", debut),
        ("k2", "soccer", "L", "Dinamo Moscow", "Zenit", debut),
    ])
    ambigu = dict(EVF_DECIMAL, HN="Dinamo", AN="Zenit")
    stats = collect(db, "u", "p", dry_run=True,
                    session_factory=lambda: _FausseSession([{"EVF": ambigu}]),
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.sans_event_key == 1
    assert sum(stats.motifs_echec.values()) == 1
    assert "ambiguïté" in "".join(stats.motifs_echec)
    rapport = diagnostic_appariement(stats)
    assert "Motifs d'échec" in rapport
    assert "ambiguïté" in rapport


def test_un_doublon_fait_ecrire_le_prix_sous_chaque_cle(tmp_path):
    """Bout en bout : le même prix doit atterrir sous les deux clés, parce
    que rien ne dit laquelle le moteur consultera."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = Storage(tmp_path / "t.db")
    debut = (now - timedelta(hours=1)).isoformat()
    db.upsert_events([
        ("202608231700::z", "soccer", "L", "Crvena Zvezda", "Cukaricki", debut),
        ("202608231800::z", "soccer", "L", "Crvena Zvezda", "Cukaricki",
         (now - timedelta(minutes=30)).isoformat()),
    ])
    stats = collect(db, "u", "p",
                    session_factory=lambda: _FausseSession([{"EVF": EVF_DECIMAL}]),
                    now_fn=lambda: now, log=lambda *a: None)

    assert stats.sans_event_key == 0
    assert len(stats.evenements_couverts) == 2
    assert stats.doublons_events == {"202608231700::z"} or \
        stats.doublons_events == {"202608231800::z"}
    for cle in ("202608231700::z", "202608231800::z"):
        assert len(db.market_state(event_key=cle)) == 12, f"rien sous {cle}"
    assert stats.collisions == set(), "un doublon n'est pas une collision"
    assert "plusieurs clés dans events" in stats.couverture()


# ── ancienneté du coup d'envoi ───────────────────────────────────────────
def test_un_orphelin_termine_depuis_longtemps_est_signale(tmp_path, monkeypatch):
    """LIVE_WINDOW_BEFORE vaut 4 h alors qu'un match de football en dure 2 :
    une partie de nos « événements en cours » est finie et gonfle le
    dénominateur sans qu'AsianOdds y soit pour quelque chose."""
    from src.asianodds_live import diagnostic_appariement, Stats
    maintenant = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    stats = Stats()
    stats.evf = 1
    stats.derniers_candidats = [
        Candidat("vieux", "A", "B", maintenant - timedelta(hours=3, minutes=30)),
        Candidat("encours", "C", "D", maintenant - timedelta(minutes=40)),
        Candidat("apres", "E", "F", maintenant + timedelta(minutes=10)),
        Candidat("sans", "G", "H", None),
    ]
    monkeypatch.setattr("src.asianodds_live.datetime",
                        _Horloge(maintenant))
    rapport = diagnostic_appariement(stats)
    assert "A — B   (débuté il y a 3 h 30)  ⟵ probablement TERMINÉ" in rapport
    assert "C — D   (débuté il y a 0 h 40)" in rapport
    assert "TERMINÉ" not in rapport.split("C — D")[1].split("\n")[0]
    assert "E — F   (débute dans 10 min)" in rapport
    assert "G — H" in rapport, "un horaire absent ne doit pas faire disparaître"


class _Horloge(datetime):
    """`datetime` figé : le diagnostic lit l'heure réelle, pas `now_fn`."""

    def __new__(cls, fige):
        obj = super().__new__(cls, fige.year, fige.month, fige.day,
                              fige.hour, fige.minute, tzinfo=fige.tzinfo)
        return obj

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 23, 19, 0, tzinfo=tz or timezone.utc)


def test_un_horaire_illisible_ne_fait_pas_tomber_le_diagnostic():
    from src.asianodds_live import _horaire
    assert _horaire(None) is None
    assert _horaire("pas une date") is None
    assert _horaire("2026-08-23T19:00:00+00:00") is not None


# ── dénominateur honnête ─────────────────────────────────────────────────
def test_plausiblement_en_jeu_ecarte_le_fini_et_le_pas_commence():
    """La fenêtre de candidats remonte à 4 h alors qu'un match en dure 2 :
    mesuré le 24/08, 21 de nos 46 « orphelins » étaient terminés. Les
    compter accuse AsianOdds d'un trou qui n'est pas le sien."""
    from src.asianodds_live import plausiblement_en_jeu, EN_JEU_MAX_MIN
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    fini = Candidat("fini", "A", "B", now - timedelta(hours=3, minutes=30))
    encours = Candidat("encours", "C", "D", now - timedelta(minutes=40))
    limite = Candidat("limite", "I", "J",
                      now - timedelta(minutes=EN_JEU_MAX_MIN))
    futur = Candidat("futur", "E", "F", now + timedelta(minutes=10))
    sans = Candidat("sans", "G", "H", None)

    gardes = plausiblement_en_jeu([fini, encours, limite, futur, sans], now)

    assert {c.event_key for c in gardes} == {"encours", "limite", "sans"}, \
        "un horaire absent doit être conservé, pas écarté sur une donnée manquante"


def test_la_couverture_publie_le_taux_corrige(tmp_path, monkeypatch):
    """Les deux taux, et pas seulement le brut : le brut reste comparable
    d'un run à l'autre, le corrigé décrit la source."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("src.asianodds_live.datetime", _Horloge(now))
    stats = Stats()
    stats.evf = 1
    stats.candidats_connus = 4
    stats.derniers_candidats = [
        Candidat("couvert", "A", "B", now - timedelta(minutes=40)),
        Candidat("orphelin", "C", "D", now - timedelta(minutes=50)),
        Candidat("fini1", "E", "F", now - timedelta(hours=3)),
        Candidat("fini2", "G", "H", now - timedelta(hours=3, minutes=30)),
    ]
    stats.evenements_couverts = {"couvert"}
    stats.matchs_vus = {"m1"}
    stats.matchs_apparies = {"m1"}

    texte = stats.couverture()
    assert "en cours=4 couverts par AsianOdds=1 (25.0 %)" in texte
    assert "EN JEU=2" in texte and "couverts=1 (50.0 %)" in texte, \
        "le taux corrigé manque : les deux matchs finis gonflent encore"


# ── reconnexion ──────────────────────────────────────────────────────────
class _SessionsSuccessives:
    """Une fabrique qui rend une session neuve à chaque appel, en piochant
    dans une liste de scénarios. Reproduit ce que fait le vrai serveur : il
    coupe, on rouvre, ça remarche."""

    def __init__(self, scenarios, plafond=200):
        self._scenarios = list(scenarios)
        # Sans ce plafond, un collecteur qui ne s'arrête plus fait PENDRE la
        # suite au lieu de la faire échouer : un test qui pend ne signale
        # rien en intégration continue.
        self._plafond = plafond
        self.ouvertures = 0
        self.sessions = []

    def __call__(self):
        scenario = self._scenarios.pop(0) if self._scenarios else []
        self.ouvertures += 1
        if self.ouvertures > self._plafond:
            raise AssertionError(
                f"{self.ouvertures} ouvertures : le collecteur ne s'arrête plus")
        if isinstance(scenario, Exception):
            s = _SessionQuiLeve(scenario)
        else:
            s = _SessionQuiFerme(scenario)
        self.sessions.append(s)
        return s


class _SessionQuiLeve(_FausseSession):
    def __init__(self, erreur):
        super().__init__([])
        self._erreur = erreur

    def open(self):
        raise self._erreur


def test_le_collecteur_reprend_apres_une_coupure(tmp_path):
    """Mesuré le 24/08 : une session de 5 min coupée à 34 s. Sans reprise, le
    collecteur s'arrête au premier incident et le silence qui suit ressemble
    à « AsianOdds ne cote plus rien »."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    fabrique = _SessionsSuccessives([
        [{"EVF": EVF_DECIMAL}],          # coupé
        [{"EVF": EVF_DECIMAL}],          # coupé
        [{"EVF": EVF_DECIMAL}],          # coupé
    ])
    attentes = []
    stats = collect(db, "u", "p", dry_run=True, duration_sec=300,
                    session_factory=fabrique, now_fn=lambda: now,
                    dormir=attentes.append, log=lambda *a: None)

    assert stats.reconnexions >= 2, "aucune reprise après coupure"
    assert fabrique.ouvertures >= 3, "la session doit être RECRÉÉE, pas rouverte"
    assert stats.evf >= 3, "les messages d'après la coupure sont perdus"
    assert all(s.fermee for s in fabrique.sessions), "session non refermée"
    assert attentes == sorted(attentes), "le délai doit croître"
    assert max(attentes) <= 30.0


def test_le_delai_de_reprise_croit_puis_plafonne(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    attentes = []
    collect(db, "u", "p", dry_run=True, duration_sec=300,
            session_factory=_SessionsSuccessives([[]] * 10),
            now_fn=lambda: now, dormir=attentes.append, log=lambda *a: None)
    assert attentes[:4] == [2.0, 4.0, 8.0, 16.0]


def test_on_abandonne_au_lieu_de_s_acharner(tmp_path):
    """S'acharner sur un serveur qui refuse n'aide pas et peut faire
    verrouiller le compte."""
    from src.asianodds_live import RECONNEXION_ECHECS_MAX
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    fabrique = _SessionsSuccessives([[]] * 50, plafond=20)
    stats = collect(db, "u", "p", dry_run=True, duration_sec=100000,
                    session_factory=fabrique, now_fn=lambda: now,
                    dormir=lambda _: None, log=lambda *a: None)
    assert fabrique.ouvertures == RECONNEXION_ECHECS_MAX + 1
    assert "abandon" in stats.fin_raison


def test_un_refus_d_identifiants_n_est_jamais_reessaye(tmp_path):
    """Rejouer un mot de passe faux cinq fois est le meilleur moyen de faire
    verrouiller le compte."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    fabrique = _SessionsSuccessives([PermissionError("login refusé")] * 5)
    with pytest.raises(PermissionError):
        collect(db, "u", "p", dry_run=True, duration_sec=300,
                session_factory=fabrique, now_fn=lambda: now,
                dormir=lambda _: None, log=lambda *a: None)
    assert fabrique.ouvertures == 1, "le refus a été rejoué"


def test_une_panne_reseau_est_reprise(tmp_path):
    """Une coupure réseau n'est pas un refus : elle se réessaie."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    fabrique = _SessionsSuccessives([
        ConnectionError("socket fermée"),
        [{"EVF": EVF_DECIMAL}],
    ])
    stats = collect(db, "u", "p", dry_run=True, duration_sec=300,
                    session_factory=fabrique, now_fn=lambda: now,
                    dormir=lambda _: None, log=lambda *a: None)
    assert fabrique.ouvertures >= 2
    assert stats.evf >= 1, "le flux n'a pas repris après la panne"


def test_sans_echeance_le_collecteur_ne_boucle_pas(tmp_path):
    """Sans `duration_sec`, il n'existe pas de « temps restant » : reprendre
    en boucle ferait tourner le collecteur indéfiniment sur une fabrique
    épuisée. L'appelant qui veut un flux sans fin doit le demander."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    fabrique = _SessionsSuccessives([[{"EVF": EVF_DECIMAL}]] * 20)
    collect(db, "u", "p", dry_run=True,
            session_factory=fabrique, now_fn=lambda: now,
            dormir=lambda _: None, log=lambda *a: None)
    assert fabrique.ouvertures == 1


def test_reconnecter_force_la_reprise_sans_echeance(tmp_path):
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    fabrique = _SessionsSuccessives([[{"EVF": EVF_DECIMAL}]] * 20)
    collect(db, "u", "p", dry_run=True, reconnecter=True,
            session_factory=fabrique, now_fn=lambda: now,
            dormir=lambda _: None, log=lambda *a: None)
    assert fabrique.ouvertures > 1


def test_le_lot_est_ecrit_avant_de_reconnecter(tmp_path):
    """Une coupure ne doit pas jeter les cotes déjà normalisées."""
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    db = _db_avec_match(tmp_path, now)
    collect(db, "u", "p", duration_sec=300,
            session_factory=_SessionsSuccessives([[{"EVF": EVF_DECIMAL}], []]),
            now_fn=lambda: now, dormir=lambda _: None, log=lambda *a: None)
    assert len(db.market_state(event_key=KEY)) == 12
