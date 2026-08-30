"""Le routage par canaux, branché — et la preuve qu'il ne change rien.

Ce commit est une migration à comportement équivalent. Deux familles de
tests, qui ne disent pas la même chose :

  * `test_ancien_egale_nouveau_*` rejoue la MÊME fonction de production dans
    ses deux modes et exige l'égalité. C'est le critère GO/NO-GO ;
  * le reste couvre ce que le nouveau chemin ajoute — le dédoublonnage par
    canal, sans lequel un pari parti sur un canal ne pourrait plus atteindre
    les autres.

Trois garde-fous portent le vrai risque :

  * `test_sans_canal_persiste_le_chemin_historique_s_applique` — la bascule
    doit être un acte explicite. Une base vide route comme avant ;
  * `test_une_erreur_sur_un_canal_n_empeche_pas_les_suivants` — un chat_id
    devenu invalide ne doit pas faire taire toute la liste ;
  * `test_le_bouton_jouer_reste_sur_le_premium` — le bouton est dans la
    liste de ce qu'on ne touche pas.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import src.alerter as al
from src.alerter import TelegramAlerter, TelegramConfig
from src.channels import CRITIQUE, PREMIUM, PRINCIPAL, depuis_config, installer
from src.models import Book, MarketType, Outcome, ValueBet
from src.routing import Borne, Canal, Critere, Regle
from src.storage import Storage


@pytest.fixture(autouse=True)
def _sans_disque(monkeypatch):
    monkeypatch.setattr(al, "_load_played_keys", lambda: (set(), set()))
    monkeypatch.setattr(al, "_load_books_alert_off", lambda: set())


@pytest.fixture
def st(tmp_path) -> Storage:
    return Storage(str(tmp_path / "t.db"))


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="jeton", chat_id="PRINCIPAL", premium_chat_id="PREMIUM",
                critical_chat_id="CRITIQUE", min_minutes_to_kickoff=0)
    base.update(kw)
    return TelegramConfig(**base)


_N = [0]


def _pari(ev=10.0, cote=2.0, *, book=Book.UNIBET_BE, marche=MarketType.H2H,
          live=False, league=None) -> ValueBet:
    _N[0] += 1
    quand = datetime.now(timezone.utc) + timedelta(hours=-1 if live else 48)
    return ValueBet(
        event_key=f"{quand:%Y%m%d%H%M}::x{_N[0]}__vs__y{_N[0]}", book=book,
        market=marche, outcome=Outcome("home"), odd_taken=cote, fair_prob=0.5,
        fair_odd=2.0, ev_pct=ev, kelly_stake_pct=1.0,
        detected_at=datetime.now(timezone.utc), league=league)


class _Espion(TelegramAlerter):
    def __init__(self, cfg, canaux, storage, *, echouer_sur=()):
        super().__init__(cfg, canaux=canaux, storage=storage)
        self.envois: list[tuple] = []
        self.messages: list[str] = []
        self._echouer_sur = set(echouer_sur)
        self._print = self.messages.append

    def _send(self, text, chat_id, reply_markup=None):
        if chat_id in self._echouer_sur:
            raise RuntimeError(f"chat {chat_id} injoignable")
        self.envois.append((chat_id, text, reply_markup))
        return True

    def route(self, bet, sport="soccer"):
        self.envois.clear()
        self.send_value_bet(bet, sport=sport)
        return [c for c, _, _ in self.envois]


def _canal(nom, chat, **kw) -> Canal:
    kw.setdefault("regles", (Regle(),))
    return Canal(chat_id=chat, nom=nom, **kw)


# ══ ANCIEN = NOUVEAU ═══════════════════════════════════════════════════
_EV = (-5.0, 4.99, 5.0, 7.99, 8.0, 8.01, 19.99, 20.0, 34.99, 35.0, 35.01, 200.0)
_COTES = (1.01, 1.49, 1.5, 3.99, 4.0, 4.01, 5.99, 6.0, 6.01, 12.0)
_SPORTS = ("soccer", "tennis", None)


def _compare(cfg, st_a, st_n, sports=_SPORTS) -> list[dict]:
    ancien = _Espion(cfg, (), st_a)
    nouveau = _Espion(cfg, depuis_config(cfg), st_n)
    ecarts = []
    for live in (False, True):
        for ev in _EV:
            for cote in _COTES:
                for sport in sports:
                    b = _pari(ev, cote, live=live)
                    a = sorted(ancien.route(b, sport))
                    n = sorted(nouveau.route(b, sport))
                    if a != n:
                        ecarts.append({"ev": ev, "cote": cote, "sport": sport,
                                       "live": live, "ancien": a, "nouveau": n})
    return ecarts


def test_ancien_egale_nouveau_config_complete(tmp_path):
    """Le critère GO/NO-GO, sur la configuration réelle (exclusion tennis
    comprise) et sur toutes les valeurs pile aux bornes."""
    cfg = _cfg(premium_hi_sports_exclus=("tennis",))
    ecarts = _compare(cfg, Storage(str(tmp_path / "a.db")),
                      Storage(str(tmp_path / "n.db")))
    assert ecarts == [], f"{len(ecarts)} divergence(s) : {ecarts[:5]}"


def test_ancien_egale_nouveau_sans_canal_premium(tmp_path):
    """Sans premium, `_premium_takes` est faux et le critique rattrape. Ne pas
    créer le canal doit reproduire exactement ça."""
    cfg = _cfg(premium_chat_id=None)
    assert _compare(cfg, Storage(str(tmp_path / "a.db")),
                    Storage(str(tmp_path / "n.db"))) == []


def test_ancien_egale_nouveau_sans_canal_critique(tmp_path):
    cfg = _cfg(critical_chat_id=None)
    assert _compare(cfg, Storage(str(tmp_path / "a.db")),
                    Storage(str(tmp_path / "n.db"))) == []


def test_ancien_egale_nouveau_sans_exclusion_de_sport(tmp_path):
    cfg = _cfg(premium_hi_sports_exclus=())
    assert _compare(cfg, Storage(str(tmp_path / "a.db")),
                    Storage(str(tmp_path / "n.db"))) == []


def test_ancien_egale_nouveau_avec_des_seuils_deplaces(tmp_path):
    """Des seuils inhabituels font se chevaucher principal et premium, cas
    que la configuration par défaut ne produit jamais."""
    cfg = _cfg(main_max_ev_pct=25.0, min_premium_ev_pct=8.0, min_ev_pct=2.0)
    assert _compare(cfg, Storage(str(tmp_path / "a.db")),
                    Storage(str(tmp_path / "n.db"))) == []


def test_le_principal_n_a_pas_de_contrainte_de_phase(st):
    """Le code actuel ne teste `is_live` QUE pour le premium et le critique.
    Un pari live doit donc continuer d'atteindre le chat principal."""
    cfg = _cfg()
    e = _Espion(cfg, depuis_config(cfg), st)
    assert e.route(_pari(6.0, 2.0, live=True)) == ["PRINCIPAL"]


def test_le_premium_et_le_critique_restent_prematch(st):
    cfg = _cfg()
    e = _Espion(cfg, depuis_config(cfg), st)
    assert e.route(_pari(50.0, 2.0, live=True)) == []
    assert e.route(_pari(50.0, 12.0, live=True)) == []


# ══ zero, un, plusieurs canaux ═════════════════════════════════════════
def test_un_pari_sans_aucun_canal_correspondant(st):
    c = [_canal("A", "a", regles=(Regle(ev_min=99.0),))]
    assert _Espion(_cfg(), c, st).route(_pari(10.0)) == []


def test_un_pari_dans_un_seul_canal(st):
    c = [_canal("A", "a", regles=(Regle(ev_min=5.0),)),
         _canal("B", "b", regles=(Regle(ev_min=99.0),))]
    assert _Espion(_cfg(), c, st).route(_pari(10.0)) == ["a"]


def test_un_pari_dans_plusieurs_canaux(st):
    """Le besoin central : une opportunité, trois notifications."""
    c = [_canal("TENNIS", "t", priorite=10, regles=(Regle(
            ev_min=10.0, odd_max=4.0,
            criteres=(Critere("sport", frozenset({"tennis"})),)),)),
         _canal("GROSSES", "g", priorite=20, regles=(Regle(ev_min=20.0),)),
         _canal("UNIBET", "u", priorite=30, regles=(Regle(
            ev_min=10.0,
            criteres=(Critere("book", frozenset({"unibet_be"})),)),))]
    e = _Espion(_cfg(), c, st)
    assert e.route(_pari(25.0, 3.0), sport="tennis") == ["t", "g", "u"]


# ══ dedoublonnage par canal ════════════════════════════════════════════
def test_le_dedoublonnage_est_independant_par_chat_id(st):
    """Un pari parti sur Tennis doit pouvoir partir sur Grosses Cotes."""
    c = [_canal("TENNIS", "t", priorite=10), _canal("GROSSES", "g", priorite=20)]
    e = _Espion(_cfg(), c, st)
    bet = _pari(25.0)
    assert e.route(bet) == ["t", "g"]
    # Deuxieme passage : l'EV n'a pas bouge, les DEUX se taisent.
    assert e.route(bet) == []


def test_un_canal_ajoute_apres_coup_recoit_quand_meme(st):
    """Le blocage que le commit 1 a levé : sans `chat_id` au dédoublonnage,
    ce second canal n'aurait jamais rien reçu."""
    e1 = _Espion(_cfg(), [_canal("TENNIS", "t")], st)
    bet = _pari(25.0)
    assert e1.route(bet) == ["t"]
    e2 = _Espion(_cfg(), [_canal("TENNIS", "t", priorite=10),
                          _canal("GROSSES", "g", priorite=20)], st)
    assert e2.route(bet) == ["g"]


def test_le_plafond_est_independant_par_canal(st):
    c = [_canal("A", "a", priorite=10), _canal("B", "b", priorite=20)]
    cfg = _cfg(valuebet_max_alerts=2, valuebet_dedup=False)
    e = _Espion(cfg, c, st)
    bet = _pari(25.0)
    assert e.route(bet) == ["a", "b"]
    assert e.route(bet) == ["a", "b"]
    assert e.route(bet) == []          # les deux plafonnent ensemble
    lignes = sqlite3.connect(str(st.path)).execute(
        "SELECT chat_id, COUNT(*) FROM notified_value_bets GROUP BY chat_id").fetchall()
    assert dict(lignes) == {"a": 2, "b": 2}


def test_le_marquage_porte_le_chat_id(st):
    e = _Espion(_cfg(), [_canal("A", "a")], st)
    e.route(_pari(25.0))
    with sqlite3.connect(str(st.path)) as c:
        assert c.execute("SELECT chat_id FROM notified_value_bets").fetchone()[0] == "a"


def test_un_envoi_refuse_n_est_pas_marque(st):
    """Un message non parti doit être retenté au cycle suivant."""
    class _Refus(_Espion):
        def _send(self, text, chat_id, reply_markup=None):
            return False
    e = _Refus(_cfg(), [_canal("A", "a")], st)
    e.send_value_bet(_pari(25.0), sport="soccer")
    with sqlite3.connect(str(st.path)) as c:
        assert c.execute("SELECT COUNT(*) FROM notified_value_bets").fetchone()[0] == 0


# ══ etat des canaux ════════════════════════════════════════════════════
def test_un_canal_inactif_ne_recoit_rien(st):
    c = [_canal("A", "a", actif=False), _canal("B", "b")]
    assert _Espion(_cfg(), c, st).route(_pari(10.0)) == ["b"]


def test_plusieurs_regles_dans_un_canal(st):
    c = [_canal("A", "a", regles=(Regle(ev_min=35.0),
                                  Regle(ev_min=20.0, odd_min=Borne(4.0, stricte=True))))]
    e = _Espion(_cfg(), c, st)
    assert e.route(_pari(40.0, 2.0)) == ["a"]
    assert e.route(_pari(25.0, 5.0)) == ["a"]
    assert e.route(_pari(25.0, 2.0)) == []


def test_la_priorite_ordonne_les_envois(st):
    c = [_canal("TARD", "tard", priorite=90), _canal("TOT", "tot", priorite=10)]
    assert _Espion(_cfg(), c, st).route(_pari(10.0)) == ["tot", "tard"]


def test_l_exclusivite_arrete_les_canaux_suivants(st):
    c = [_canal("P", "p", priorite=10, exclusif=True), _canal("C", "c", priorite=20)]
    assert _Espion(_cfg(), c, st).route(_pari(10.0)) == ["p"]


# ══ donnees absentes ═══════════════════════════════════════════════════
def test_sport_absent_une_inclusion_ne_passe_pas(st):
    c = [_canal("A", "a", regles=(Regle(
        criteres=(Critere("sport", frozenset({"tennis"})),)),))]
    assert _Espion(_cfg(), c, st).route(_pari(10.0), sport=None) == []


def test_sport_absent_une_exclusion_n_exclut_pas(st):
    """Même règle que `premium_hi_sports_exclus` aujourd'hui : `(sport or "")`
    ne figure dans aucune liste d'exclusion, donc le pari passe."""
    c = [_canal("A", "a", regles=(Regle(
        criteres=(Critere("sport", frozenset({"tennis"}), inclut=False),)),))]
    assert _Espion(_cfg(), c, st).route(_pari(10.0), sport=None) == ["a"]


def test_league_absente(st):
    c = [_canal("A", "a", regles=(Regle(
        criteres=(Critere("league", frozenset({"Pro League"})),)),))]
    e = _Espion(_cfg(), c, st)
    assert e.route(_pari(10.0, league=None)) == []
    assert e.route(_pari(10.0, league="Pro League")) == ["a"]


# ══ securites globales : intactes ══════════════════════════════════════
def test_la_fenetre_morte_s_applique_avant_les_canaux(st, monkeypatch):
    cfg = _cfg(min_minutes_to_kickoff=15)
    bet = _pari(25.0)
    proche = ValueBet(**{**bet.__dict__,
                         "event_key": (datetime.now(timezone.utc)
                                       + timedelta(minutes=5)).strftime("%Y%m%d%H%M")
                                      + "::p__vs__q"})
    assert _Espion(cfg, [_canal("A", "a")], st).route(proche) == []


def test_un_book_en_sourdine_s_applique_avant_les_canaux(st, monkeypatch):
    monkeypatch.setattr(al, "_load_books_alert_off", lambda: {"unibet_be"})
    assert _Espion(_cfg(), [_canal("A", "a")], st).route(_pari(25.0)) == []


def test_un_marche_deja_joue_s_applique_avant_les_canaux(st, monkeypatch):
    bet = _pari(25.0)
    cle = f"{bet.event_key}|{bet.market.value}|{bet.outcome.line}"
    monkeypatch.setattr(al, "_load_played_keys", lambda: (set(), {cle}))
    assert _Espion(_cfg(), [_canal("A", "a")], st).route(bet) == []


def test_la_mi_temps_s_applique_avant_les_canaux(st):
    bet = _pari(25.0, marche=MarketType.H2H_H1)
    assert _Espion(_cfg(), [_canal("A", "a")], st).route(bet) == []


# ══ robustesse ═════════════════════════════════════════════════════════
def test_une_erreur_sur_un_canal_n_empeche_pas_les_suivants(st):
    """Un chat_id devenu invalide ne doit pas faire taire toute la liste."""
    c = [_canal("KO", "ko", priorite=10), _canal("OK", "ok", priorite=20)]
    e = _Espion(_cfg(), c, st, echouer_sur={"ko"})
    assert e.route(_pari(25.0)) == ["ok"]
    assert any("KO" in m for m in e.messages)


def test_sans_canal_persiste_le_chemin_historique_s_applique(st):
    """La bascule est un acte explicite : `canaux=()` route comme avant."""
    cfg = _cfg()
    assert _Espion(cfg, (), st).route(_pari(6.0, 2.0)) == ["PRINCIPAL"]
    assert _Espion(cfg, (), st).route(_pari(50.0, 2.0)) == ["PREMIUM"]


def test_une_base_sans_les_tables_ne_bloque_pas_le_daemon(monkeypatch, tmp_path):
    """`_load_channels` doit rendre [] plutôt que lever : une base ancienne
    ne doit pas arrêter les alertes."""
    vide = tmp_path / "sansTables.db"
    sqlite3.connect(str(vide)).close()
    monkeypatch.setattr(al, "_PLAYS_DB", vide)
    assert al._load_channels(print_fn=lambda _s: None) == []
    assert al.routage_par_canaux_actif() is False


# ══ presentation : ce qui ne doit pas bouger ═══════════════════════════
def test_le_bouton_jouer_reste_sur_le_premium(st):
    cfg = _cfg()
    e = _Espion(cfg, depuis_config(cfg), st)
    e.route(_pari(25.0, 2.0))
    chat, texte, bouton = e.envois[0]
    assert chat == "PREMIUM"
    assert bouton is not None and "Jouer" in str(bouton)
    assert texte.startswith("💎 <b>VALUE PREMIUM</b>")


def test_le_critique_garde_son_entete_et_n_a_pas_de_bouton(st):
    cfg = _cfg()
    e = _Espion(cfg, depuis_config(cfg), st)
    e.route(_pari(50.0, 12.0))
    chat, texte, bouton = e.envois[0]
    assert chat == "CRITIQUE" and bouton is None
    assert texte.startswith("🚨 <b>VALUE BET EXCEPTIONNEL</b>")


def test_le_principal_n_a_ni_entete_ni_bouton(st):
    cfg = _cfg()
    e = _Espion(cfg, depuis_config(cfg), st)
    e.route(_pari(6.0, 2.0))
    _, texte, bouton = e.envois[0]
    assert bouton is None
    assert not texte.startswith("💎") and not texte.startswith("🚨")


# ══ installation en base ═══════════════════════════════════════════════
def test_installer_puis_charger_rend_les_memes_canaux(st):
    from src.channels import charger
    cfg = _cfg(premium_hi_sports_exclus=("tennis",))
    attendus = depuis_config(cfg)
    installer(st, attendus, print_fn=lambda _s: None)
    obtenus = charger(st)
    assert [c.nom for c in obtenus] == [c.nom for c in attendus]
    for a, b in zip(attendus, obtenus):
        assert (a.chat_id, a.priorite, a.exclusif, a.actif) == \
               (b.chat_id, b.priorite, b.exclusif, b.actif)
        assert a.regles == b.regles, a.nom


def test_installer_deux_fois_n_ecrase_rien(st):
    cfg = _cfg()
    msgs: list[str] = []
    assert len(installer(st, depuis_config(cfg), print_fn=msgs.append)) == 3
    assert installer(st, depuis_config(cfg), print_fn=msgs.append) == []
    assert len(st.load_channel_rows()[0]) == 3
    assert sum("deja present" in m for m in msgs) == 3


def test_les_trois_routes_sont_traduites(st):
    noms = [c.nom for c in depuis_config(_cfg())]
    assert noms == [PRINCIPAL, PREMIUM, CRITIQUE]


# ══ le harnais de comparaison lui-meme ═════════════════════════════════
def test_le_harnais_neutralise_le_dedoublonnage():
    """LE garde-fou du harnais, et il a fallu deux echecs pour l'ecrire.

    Le dedoublonnage ne vit pas au meme endroit dans les deux chemins : dans
    l'ancien il est dans `main.py`, AVANT `send_alerts`, donc un harnais qui
    appelle `send_value_bet` le contourne ; dans le nouveau il est DANS
    `send_value_bet`. Sans neutralisation, la comparaison oppose un chemin
    non dedoublonne a un chemin dedoublonne.

    Premier echec : 3 840 fausses divergences sur les cas synthetiques. J'ai
    corrige en rendant les cles uniques — ce qui MASQUAIT le defaut au lieu
    de le traiter. Second echec : 31 fausses divergences sur les detections
    reelles de la VM, qui contiennent de vraies repetitions.

    Le cas ci-dessous est celui de la VM : meme date, memes equipes, minute
    differente — `_event_key_like` les regroupe."""
    from scripts.comparer_routage import comparer
    cfg = _cfg()
    cle = "%s::pedrovives__vs__bernardomunk"

    def _repete(minute, ev):
        return ValueBet(
            event_key=cle % f"20260826{minute}", book=Book.UNIBET_BE,
            market=MarketType.H2H, outcome=Outcome("home"), odd_taken=3.05,
            fair_prob=0.5, fair_odd=2.0, ev_pct=ev, kelly_stake_pct=1.0,
            detected_at=datetime.now(timezone.utc))

    cas = [("1104", _repete("1104", 7.5), "tennis"),
           ("1046", _repete("1046", 7.5), "tennis"),
           ("1022", _repete("1022", 5.3), "tennis")]
    r = comparer(cas, cfg)
    assert r["divergences"] == [], (
        "le harnais mesure le dedoublonnage au lieu du routage : "
        f"{r['divergences']}")
    assert r["identiques"] == 3


def test_la_neutralisation_du_dedoublonnage_est_effective():
    """Falsification permanente : la config passee aux deux espions doit
    vraiment desactiver le dedoublonnage, sinon le test precedent passerait
    pour de mauvaises raisons."""
    from scripts.comparer_routage import _sans_dedoublonnage
    nu = _sans_dedoublonnage(_cfg(valuebet_dedup=True, valuebet_max_alerts=2))
    assert nu.valuebet_dedup is False
    assert nu.valuebet_max_alerts >= 10 ** 6
    # Et le reste de la configuration est intact : neutraliser le
    # dedoublonnage ne doit pas deplacer un seuil de routage.
    origine = _cfg()
    for champ in ("min_ev_pct", "main_max_ev_pct", "main_min_odd", "main_max_odd",
                  "min_premium_ev_pct", "premium_min_odd", "premium_max_odd",
                  "premium_hi_min_ev", "min_critical_ev_pct",
                  "critical_hi_min_ev", "critical_hi_min_odd"):
        assert getattr(_sans_dedoublonnage(origine), champ) == getattr(origine, champ), champ


def test_les_cas_synthetiques_ont_des_cles_de_dedoublonnage_distinctes():
    """Ceinture et bretelles depuis que le harnais neutralise le
    dedoublonnage : des cas distincts restent plus proches de la
    production que N fois le meme pari."""
    from scripts.comparer_routage import cas_synthetiques
    cles = [(b.event_key, b.book.value, b.market.value, b.outcome.label,
             b.outcome.line) for _, b, _ in cas_synthetiques()]
    assert len(cles) == len(set(cles)), (
        f"{len(cles) - len(set(cles))} cas partagent une cle de dedoublonnage")


def test_le_harnais_verrait_vraiment_une_divergence(tmp_path):
    """Falsification permanente : si on fausse la traduction, la comparaison
    doit le dire. Sans ce test, rien ne prouve qu'elle ne rend pas 0
    divergence quoi qu'il arrive."""
    import scripts.comparer_routage as cr
    cfg = _cfg()
    cas = [("temoin", _pari(6.0, 2.0), "soccer")]
    assert cr.comparer(cas, cfg)["divergences"] == []

    # Le harnais a importe `depuis_config` PAR SON NOM : c'est le symbole du
    # module appelant qu'il faut remplacer, pas celui de `src.channels`.
    vrai = cr.depuis_config
    try:
        cr.depuis_config = lambda c: [_canal("FAUX", "ailleurs")]
        r = cr.comparer(cas, cfg)
    finally:
        cr.depuis_config = vrai
    assert len(r["divergences"]) == 1
    assert r["divergences"][0]["ancien"] == ["PRINCIPAL"]
    assert r["divergences"][0]["nouveau"] == ["ailleurs"]
