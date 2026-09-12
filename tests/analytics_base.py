"""Fabrique de bases de test pour la couche Analytics.

⚠️ LE SCHÉMA VIENT DE LA PRODUCTION, PAS D'UN CREATE TABLE RECOPIÉ.

Les fixtures de ce projet écrivaient jusqu'ici leurs propres `CREATE TABLE`.
Ça a déjà coûté : le jour où une requête a gagné une jointure sur
`notified_value_bets`, trois fixtures sont tombées parce que la table n'y
existait pas — le test décrivait une base qui n'était plus celle du moteur.
Ici on instancie `Storage`, qui applique `SCHEMA`, `FEATURES_SCHEMA` et
`MIGRATIONS`. Une colonne ajoutée en production apparaît donc dans les tests
sans que personne n'ait à y penser.

Ce module n'est PAS un fichier de tests (son nom ne commence pas par `test_`)
et n'est donc jamais collecté par pytest.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from src.storage import Storage


@dataclass
class Opp:
    """Une opportunité de test, avec tout ce qui peut lui arriver."""
    id: int
    sport: str = "soccer"
    league: str = "Jupiler Pro League"
    home: str = "Anderlecht"
    away: str = "Genk"
    jour: str = "2026-08-15"
    heure: str = "18:00"
    book: str = "unibet_be"
    market: str = "h2h"
    outcome: str = "home"
    line: "float | None" = None
    odd: float = 2.00
    ev: float = 10.0
    #: Détection. Par défaut huit heures avant le coup d'envoi.
    detecte: "str | None" = None
    #: Clôture DÉVIGUÉE. None = aucune CLV mesurable, et c'est un cas à tester.
    cloture: "float | None" = None
    #: Résultat de l'événement. None = non réglé.
    gagnant: "str | None" = None
    score_dom: "float | None" = None
    score_ext: "float | None" = None
    #: Clic sur « Jouer ».
    joue: bool = False
    #: Instant de l'alerte Telegram. None = jamais envoyée.
    alerte: "str | None" = None
    #: Forcer une clé d'événement (pour tester le tennis multi-clés).
    event_key: "str | None" = None

    @property
    def coup_envoi(self) -> str:
        return f"{self.jour}T{self.heure}:00+00:00"

    @property
    def cle(self) -> str:
        if self.event_key:
            return self.event_key
        stamp = self.jour.replace("-", "") + self.heure.replace(":", "")
        return f"{stamp}::{self.home.lower()}__vs__{self.away.lower()}"

    @property
    def detecte_a(self) -> str:
        return self.detecte or f"{self.jour}T10:00:00+00:00"


def monter(tmp_path, opportunites, nom="v.db") -> Path:
    """Construit une base complète à partir d'une liste d'`Opp`."""
    chemin = Path(tmp_path) / nom
    Storage(str(chemin))              # ← le schéma RÉEL du moteur
    con = sqlite3.connect(str(chemin))

    for o in opportunites:
        con.execute(
            "INSERT OR IGNORE INTO events(event_key, sport, league, home, away,"
            " start_time) VALUES (?,?,?,?,?,?)",
            (o.cle, o.sport, o.league, o.home, o.away, o.coup_envoi))
        # fair_prob / fair_odd / kelly_pct sont NOT NULL : on les dérive de
        # l'EV plutôt que d'inventer des valeurs qui se contrediraient.
        fair_odd = o.odd / (1 + o.ev / 100.0)
        con.execute(
            "INSERT INTO value_bets(id, event_key, book, market, outcome_label,"
            " line, odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct,"
            " detected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.id, o.cle, o.book, o.market, o.outcome, o.line, o.odd,
             1.0 / fair_odd, fair_odd, o.ev, 1.0, o.detecte_a))
        if o.cloture is not None:
            con.execute(
                "INSERT INTO clv_snapshots(value_bet_id, snapshot_at, closing,"
                " pinnacle_odd, pinnacle_prob, fair_odd)"
                " VALUES (?,?,1,?,?,?)",
                # ⚠️ Une clôture nulle ou négative DOIT pouvoir être
                # fabriquée : c'est le cas dégénéré que la couche doit
                # ignorer plutôt que rendre une CLV absurde. On ne dérive donc
                # la probabilité que quand la cote le permet.
                (o.id, o.coup_envoi, o.cloture * 0.95,
                 (1.0 / o.cloture) if o.cloture > 0 else 0.0, o.cloture))
        if o.gagnant is not None or o.score_dom is not None:
            con.execute(
                "INSERT OR IGNORE INTO results(event_key, winner, home_score,"
                " away_score, source, settled_at) VALUES (?,?,?,?,?,?)",
                (o.cle, o.gagnant, o.score_dom, o.score_ext, "test",
                 o.coup_envoi))
        if o.joue:
            con.execute(
                "INSERT INTO played_bets(dedup_key, played_at, value_bet_id,"
                " event_key, sport, book, market, outcome_label, line,"
                " odd_taken, ev_pct, stake) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"k{o.id}", o.detecte_a, o.id, o.cle, o.sport,
                 # ⚠️ VOLONTAIREMENT UN LIBELLÉ D'AFFICHAGE, pas l'enum : c'est
                 # ce que `bot_listener` écrit réellement (76,6 % des lignes de
                 # production). Un test qui écrirait l'enum ici ne prouverait
                 # rien sur l'interdiction de lire cette colonne.
                 "Unibet / 711 / Bingoal / Scooore",
                 o.market, o.outcome, o.line, o.odd, o.ev, 25.0))
        if o.alerte:
            con.execute(
                "INSERT INTO notified_value_bets(event_key, book, market,"
                " outcome_label, line, ev_pct, notified_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (o.cle, o.book, o.market, o.outcome, o.line, o.ev, o.alerte))
    con.commit()
    con.close()
    return chemin


def ajouter_features(chemin, value_bet_id, detected_at, **kw):
    """Écrit une ligne `bet_features` avec un `detected_at` CHOISI.

    Sert au test qui prouve que la couche n'utilise jamais cette colonne comme
    source temporelle : en production elle porte la DERNIÈRE détection, pas la
    première."""
    con = sqlite3.connect(str(chemin))
    con.execute(
        "INSERT OR REPLACE INTO bet_features(value_bet_id, detected_at,"
        " event_key, book, market, outcome_label, odd_taken, fair_odd, ev_pct)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (value_bet_id, detected_at, kw.get("event_key", "x"),
         kw.get("book", "unibet_be"), kw.get("market", "h2h"),
         kw.get("outcome_label", "home"), kw.get("odd_taken", 2.0),
         kw.get("fair_odd", 1.8), kw.get("ev_pct", 10.0)))
    con.commit()
    con.close()


def ajouter_canal(chemin, nom="PREMIUM", *, ev_min=8.0, ev_max=None,
                  odd_min=1.5, odd_max=4.0, sports_exclus=()):
    """Crée un canal configuré, via l'API RÉELLE de `Storage`.

    ⚠️ Sans canal en base, `porte_de_canal` retombe sur `TelegramConfig` et
    lève une `SystemExit` faute de jeton Telegram. Un test qui dépendrait de
    l'environnement serait vert ou rouge selon la machine — ici la porte est
    posée explicitement, donc la population ELIGIBLE_FOR_ALERT est
    reproductible."""
    st = Storage(str(chemin))
    canal = st.create_channel(chat_id="-100", nom=nom)
    regle = st.add_channel_rule(canal, ev_min=ev_min, ev_max=ev_max,
                                odd_min=odd_min, odd_max=odd_max,
                                phase="prematch")
    for s in sports_exclus:
        st.add_rule_value(regle, "sport", s, inclut=False)
    return canal
