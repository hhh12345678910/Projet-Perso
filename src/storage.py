from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .config import SQLITE_BUSY_TIMEOUT_SEC
from .models import Book, MarketType, OddQuote, Outcome, ValueBet


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_key   TEXT PRIMARY KEY,
    sport       TEXT NOT NULL,
    league      TEXT NOT NULL,
    home        TEXT NOT NULL,
    away        TEXT NOT NULL,
    start_time  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    line            REAL,
    decimal_odd     REAL NOT NULL,
    fetched_at      TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    FOREIGN KEY (event_key) REFERENCES events(event_key)
);

CREATE INDEX IF NOT EXISTS idx_quotes_event ON quotes(event_key);
CREATE INDEX IF NOT EXISTS idx_quotes_fetched ON quotes(fetched_at);

CREATE TABLE IF NOT EXISTS value_bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    line            REAL,
    odd_taken       REAL NOT NULL,
    fair_prob       REAL NOT NULL,
    fair_odd        REAL NOT NULL,
    ev_pct          REAL NOT NULL,
    kelly_pct       REAL NOT NULL,
    stake           REAL,
    detected_at     TEXT NOT NULL,
    placed          INTEGER DEFAULT 0,
    result          TEXT,
    pnl             REAL
);

CREATE INDEX IF NOT EXISTS idx_vb_event ON value_bets(event_key);
CREATE INDEX IF NOT EXISTS idx_vb_detected ON value_bets(detected_at);

CREATE TABLE IF NOT EXISTS clv_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    value_bet_id    INTEGER NOT NULL,
    snapshot_at     TEXT NOT NULL,
    closing         INTEGER DEFAULT 0,
    pinnacle_odd    REAL NOT NULL,
    pinnacle_prob   REAL NOT NULL,
    -- The devigged closing line. pinnacle_odd is the price Pinnacle DISPLAYS,
    -- commission included; comparing a taken odd against it overstates CLV by
    -- the whole margin (~6-7% on this portfolio). fair_odd is that same closing
    -- price with the margin removed, i.e. the number EV is already measured
    -- against — so CLV and EV finally use the same ruler.
    fair_odd        REAL,
    fair_prob       REAL,
    overround       REAL,
    FOREIGN KEY (value_bet_id) REFERENCES value_bets(id)
);

CREATE TABLE IF NOT EXISTS notified_surebets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    market          TEXT NOT NULL,
    line            REAL,
    margin_pct      REAL NOT NULL,
    notified_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ns_lookup ON notified_surebets(event_key, market);

CREATE TABLE IF NOT EXISTS notified_middles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    low_line        REAL NOT NULL,
    high_line       REAL NOT NULL,
    ev_pct          REAL NOT NULL,
    notified_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nm_lookup ON notified_middles(event_key, low_line, high_line);

CREATE TABLE IF NOT EXISTS notified_value_bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    line            REAL,
    ev_pct          REAL NOT NULL,
    notified_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nvb_lookup ON notified_value_bets(event_key, book, market);

CREATE TABLE IF NOT EXISTS notified_clv_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    value_bet_id    INTEGER NOT NULL,
    clv_pct         REAL NOT NULL,
    current_pin_odd REAL NOT NULL,
    notified_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nca_vb ON notified_clv_alerts(value_bet_id);

CREATE TABLE IF NOT EXISTS teams (
    normalized_name  TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL
);

-- One row per tap on "Jouer". dedup_key stays the primary key because the
-- alert-suppression path keys on it; everything else is what turns a click
-- into a measurable bet (which value_bet it was, at what price, for what EV).
CREATE TABLE IF NOT EXISTS played_bets (
    dedup_key       TEXT PRIMARY KEY,
    played_at       TEXT,
    value_bet_id    INTEGER,
    event_key       TEXT,
    sport           TEXT,
    book            TEXT,
    market          TEXT,
    outcome_label   TEXT,
    line            REAL,
    odd_taken       REAL,
    fair_odd        REAL,
    ev_pct          REAL,
    stake           REAL
);

-- Settled outcomes, keyed by event. Populated by `settle`; kept apart from
-- value_bets so re-importing results never rewrites detection history.
CREATE TABLE IF NOT EXISTS results (
    event_key       TEXT PRIMARY KEY,
    winner          TEXT,
    home_score      REAL,
    away_score      REAL,
    source          TEXT,
    settled_at      TEXT NOT NULL
);
"""

# (table, column, type) added after the table shipped. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so each is attempted and its duplicate-column
# error swallowed — that keeps an existing production database upgradable
# without a dump/restore.
# Table permanente, JAMAIS purgée — c'est tout son intérêt.
#
# `quotes` pèse 20 Go pour deux jours et se purge chaque nuit : chaque cote de
# chaque book à chaque cycle, une information très diluée. Ici c'est l'inverse :
# une ligne par détection, ~300 octets, soit environ 200 Mo par an. Ce sont les
# variables calculées à la volée puis jetées à chaque cycle — ligue, overround
# de la référence, âge de la ligne, nombre de books offrant le marché, qualité
# de l'appariement — sans lesquelles aucune analyse par championnat, aucun
# diagnostic de faux positif et aucun modèle n'est possible, même rétroactivement.
#
# Le CLV et le résultat sont remplis plus tard, quand ils existent.
FEATURES_SCHEMA = """
CREATE TABLE IF NOT EXISTS bet_features (
    value_bet_id    INTEGER PRIMARY KEY,
    detected_at     TEXT NOT NULL,
    event_key       TEXT NOT NULL,
    sport           TEXT,
    league          TEXT,
    league_category TEXT,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    line            REAL,
    odd_taken       REAL NOT NULL,
    fair_odd        REAL NOT NULL,
    ev_pct          REAL NOT NULL,
    kelly_pct       REAL,
    delay_h         REAL,
    reference_book  TEXT,
    ref_overround   REAL,
    ref_n_outcomes  INTEGER,
    ref_age_sec     REAL,
    n_books_market  INTEGER,
    match_score     REAL,
    time_shift_min  REAL
);
CREATE INDEX IF NOT EXISTS idx_bf_detected ON bet_features(detected_at);
CREATE INDEX IF NOT EXISTS idx_bf_league ON bet_features(league_category);
CREATE INDEX IF NOT EXISTS idx_bf_event ON bet_features(event_key);

-- Combien de temps le book met-il à corriger sa cote ?
--
-- Mesure la fenêtre pendant laquelle une alerte reste réellement jouable, et
-- classe les books du plus lent au plus réactif. Elle ne se reconstitue pas
-- après coup : elle demande de suivre une cote de cycle en cycle, et les cotes
-- sont purgées à deux jours.
--
-- `corrected_at` NULL avec un `observed_until` tardif n'est PAS une absence de
-- donnée : c'est l'information « ce book n'avait toujours pas bougé après N
-- minutes », et c'est même la plus intéressante. Sans `observed_until` on ne
-- saurait pas distinguer un book lent d'un marché qu'on a cessé d'observer —
-- les deux donneraient une ligne vide.
CREATE TABLE IF NOT EXISTS bet_corrections (
    value_bet_id      INTEGER PRIMARY KEY,
    detected_at       TEXT NOT NULL,
    kickoff           TEXT,
    book              TEXT NOT NULL,
    event_key         TEXT NOT NULL,
    market            TEXT NOT NULL,
    outcome_label     TEXT NOT NULL,
    line              REAL,
    odd_taken         REAL NOT NULL,
    fair_odd          REAL,
    observed_until    TEXT,
    observations      INTEGER DEFAULT 0,
    min_odd_seen      REAL,
    -- Jalon 1 : le prix qu'on avait n'existe plus. Fin de la fenêtre jouable.
    corrected_at      TEXT,
    seconds_to_corr   REAL,
    odd_at_corr       REAL,
    -- Jalon 2 : le book a rejoint la ligne juste. La valeur a entièrement
    -- disparu. Distinct du premier : descendre d'un centime sous la cote prise
    -- ferme la fenêtre sans rien dire de la convergence.
    aligned_at        TEXT,
    seconds_to_align  REAL,
    odd_at_align      REAL
);
CREATE INDEX IF NOT EXISTS idx_bc_open
    ON bet_corrections(corrected_at, detected_at);
-- `detected_at` est la première condition de open_corrections, et la seule qui
-- soit sélective. Sans cet index, chaque cycle balaye toute la table — anodin
-- tant qu'elle est jeune, coûteux sur une table permanente qui grossit d'un an.
CREATE INDEX IF NOT EXISTS idx_bc_detected ON bet_corrections(detected_at);

-- Trajectoire complète d'une cote détectée, de la détection au coup d'envoi.
--
-- Table permanente, JAMAIS purgée. C'est la seule façon de tracer un graphe :
-- `quotes` porte déjà chaque cote de chaque cycle, mais elle pèse 20 Go pour
-- deux jours et se purge chaque nuit — l'historique y est détruit avant d'avoir
-- pu servir. `bet_corrections` survit, elle, mais ne garde que des jalons
-- (première correction, alignement) : deux points, pas une courbe.
--
-- UNE LIGNE PAR CHANGEMENT, jamais par cycle. Mesuré par tools/line_speed.py :
-- 97 à 99 % des cotes sont identiques d'un cycle à l'autre. Écrire chaque cycle
-- multiplierait le volume par cinquante pour répéter la même valeur. La série
-- complète se reconstruit en propageant la dernière valeur connue (forward
-- fill), sans aucune perte d'information.
--
-- `fair_odd` est la ligne de référence AU MÊME INSTANT, pas celle de la
-- détection. Sans elle on verrait le book bouger sans savoir s'il rejoint la
-- référence ou si c'est la référence qui est venue à lui — or c'est exactement
-- la question à laquelle une courbe doit répondre.
CREATE TABLE IF NOT EXISTS odds_history (
    value_bet_id  INTEGER NOT NULL,   -- identifie la SÉLECTION suivie
    book          TEXT NOT NULL,      -- de qui est cette cote
    seen_at       TEXT NOT NULL,
    odd           REAL NOT NULL,
    fair_odd      REAL,
    ev_pct        REAL
);
CREATE INDEX IF NOT EXISTS idx_oh_bet ON odds_history(value_bet_id, book, seen_at);

-- Books dont les alertes sont COUPÉES. Table d'exceptions, pas d'inscriptions :
-- un book absent d'ici alerte normalement, donc ajouter un scraper ne demande
-- rien et l'oubli d'une ligne ne rend jamais un book silencieux par accident.
--
-- Ne concerne QUE la notification. La collecte, le stockage, les courbes, le
-- CLV et tous les exports continuent pour tous les books sans exception —
-- c'est la condition posée par l'utilisateur : couper le bruit sans jamais
-- perdre de données.
-- ── market_state : le dernier prix connu, par sélection ───────────────────
--
-- `quotes` est un JOURNAL en ajout : « le dernier prix » s'y paie par un
-- GROUP BY sur des millions de lignes, purgées à 7 jours. Le futur moteur LIVE
-- raisonne sur les VARIATIONS et ne peut pas payer ça à chaque tick.
--
-- Cette table-ci est bornée par l'OFFRE COURANTE et non par le temps : une
-- ligne par sélection réellement proposée, mise à jour en place. Elle ne
-- duplique pas `quotes`, elle en est la projection « état courant ».
--
-- ⚠️ LE PIÈGE, mesuré avant d'écrire ce schéma : `line` vaut NULL sur tout
-- 1X2, et SQLite considère deux NULL comme DISTINCTS dans un index unique.
-- Une PRIMARY KEY (event_key, market, outcome_label, line, book) laisse donc
-- l'UPSERT ne jamais se déclencher — vérifié : trois UPSERT identiques sur une
-- ligne NULL produisent TROIS lignes, en silence. D'où l'index unique sur
-- EXPRESSION avec COALESCE ci-dessous, qui rend les NULL comparables tout en
-- gardant `line` lisible en NULL. Le sentinel -1e9 ne peut être une vraie
-- ligne de but ou de jeu.
--
-- `fetched_at` est l'instant de la dernière ÉCRITURE, pas de la dernière
-- observation : l'écriture creuse ne réécrit un marché que s'il a changé, ou
-- au battement de cœur. La péremption est donc bornée par QUOTES_HEARTBEAT_SEC
-- (1 800 s par défaut), et c'est ce que le LIVE devra savoir lire.
CREATE TABLE IF NOT EXISTS market_state (
    event_key     TEXT    NOT NULL,
    book          TEXT    NOT NULL,
    market        TEXT    NOT NULL,
    outcome_label TEXT    NOT NULL,
    line          REAL,
    odd           REAL    NOT NULL,
    fetched_at    TEXT    NOT NULL,
    is_live       INTEGER NOT NULL DEFAULT 0,
    league        TEXT
);
-- Clé naturelle. L'ordre des colonnes sert les lectures du LIVE, pas
-- l'esthétique : `event_key` d'abord (tous les prix d'un match), puis
-- `market`, `outcome_label`, `line` (tous les books d'une sélection donnée
-- sont alors CONTIGUS), et `book` en dernier.
CREATE UNIQUE INDEX IF NOT EXISTS ux_market_state
    ON market_state (event_key, market, outcome_label, COALESCE(line, -1e9), book);
-- « Les dernières mises à jour » : le LIVE lira par fenêtre de temps.
CREATE INDEX IF NOT EXISTS idx_ms_fetched ON market_state (fetched_at);
-- « Filtrer les marchés LIVE ». Index PARTIEL : les cotes live sont une
-- fraction du total, l'index ne porte donc que sur elles et reste petit.
CREATE INDEX IF NOT EXISTS idx_ms_live ON market_state (event_key) WHERE is_live = 1;

CREATE TABLE IF NOT EXISTS book_alerts_off (
    book        TEXT PRIMARY KEY,
    disabled_at TEXT NOT NULL
);

-- ── Canaux configurables ───────────────────────────────────────────────
-- Un canal = une destination Telegram + des regles. Un pari qui satisfait
-- trois canaux part dans les trois : ils sont INDEPENDANTS.
--
-- Ces trois tables vivent dans SCHEMA et non dans MIGRATIONS : le
-- `executescript(SCHEMA)` de chaque ouverture porte deja `IF NOT EXISTS`,
-- donc une base ancienne les gagne a la reouverture et une base recente ne
-- bouge pas. MIGRATIONS ne sert qu'aux colonnes ajoutees a une table qui
-- existe deja.
--
-- ⚠️ `REFERENCES` est ecrit pour dire l'intention, PAS pour agir : ce
-- projet n'active jamais `PRAGMA foreign_keys`, donc aucun ON DELETE
-- CASCADE ne se declencherait. `delete_channel` supprime ses enfants
-- explicitement, et un test l'exige.
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    nom         TEXT NOT NULL UNIQUE,
    actif       INTEGER NOT NULL DEFAULT 1,
    -- Ordonne la sortie du routage (petit = tot). Ne filtre rien.
    priorite    INTEGER NOT NULL DEFAULT 100,
    -- Quand un canal exclusif prend le pari, les canaux de priorite
    -- inferieure ne le recoivent pas. 0 par defaut : canaux independants.
    exclusif    INTEGER NOT NULL DEFAULT 0,
    -- Prevu pour le multi-utilisateur. Aucun code ne le lit encore.
    profile_id  INTEGER,
    cree_le     TEXT NOT NULL
);

-- Plusieurs regles pour un canal se combinent en OU.
-- Chaque borne porte sa strictesse : la configuration reelle en a besoin
-- des deux cotes (le canal principal s'arrete a EV < 8 tandis que sa bande
-- de cote inclut 4,00 ; la voie critique grosses cotes commence a cote > 4).
CREATE TABLE IF NOT EXISTS channel_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id),
    ev_min          REAL,
    ev_min_strict   INTEGER NOT NULL DEFAULT 0,
    ev_max          REAL,
    ev_max_strict   INTEGER NOT NULL DEFAULT 0,
    odd_min         REAL,
    odd_min_strict  INTEGER NOT NULL DEFAULT 0,
    odd_max         REAL,
    odd_max_strict  INTEGER NOT NULL DEFAULT 0,
    phase           TEXT              -- 'prematch' | 'live' | NULL = les deux
);
CREATE INDEX IF NOT EXISTS idx_chrules_canal ON channel_rules(channel_id);

-- Les dimensions multivaluees. Plusieurs valeurs d'une meme dimension se
-- combinent en OU ; deux dimensions differentes en ET.
-- `inclut=0` fait une exclusion (le tennis hors de la bande longue).
CREATE TABLE IF NOT EXISTS channel_rule_values (
    rule_id     INTEGER NOT NULL REFERENCES channel_rules(id),
    dimension   TEXT NOT NULL,        -- 'sport' | 'book' | 'market' | 'league'
    valeur      TEXT NOT NULL,
    inclut      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (rule_id, dimension, valeur)
);
"""


MIGRATIONS = [
    ("bet_corrections", "fair_odd", "REAL"),
    # Dernière cote enregistrée dans odds_history : permet de n'écrire que
    # les changements sans relire la table à chaque cycle.
    ("bet_corrections", "last_odd_seen", "REAL"),
    ("bet_corrections", "aligned_at", "TEXT"),
    ("bet_corrections", "seconds_to_align", "REAL"),
    ("bet_corrections", "odd_at_align", "REAL"),
    ("value_bets", "closing_lost", "INTEGER"),
    # Une opportunité n'a qu'UNE ligne, écrite à la première détection. Sans
    # ces colonnes, rien ne distingue un pari encore vivant d'un pari détecté
    # il y a trois heures et mort depuis : detected_at ne bouge jamais.
    # La référence sharp qui a produit la fair line de CE pari. Elle n'existait
    # que dans bet_features, donc invisible à close_lines et à export-history :
    # un pari valorisé sur un repli cherchait sa clôture chez Pinnacle, ne la
    # trouvait jamais (s'il la pricait, il n'y aurait pas eu de repli), et
    # disparaissait de toute mesure. L'alerte, elle, lit l'objet en mémoire et
    # affichait donc correctement sa référence — d'où un symptôme trompeur.
    ("value_bets", "reference_book", "TEXT"),
    ("value_bets", "last_seen_at", "TEXT"),
    ("value_bets", "last_odd", "REAL"),
    ("value_bets", "last_ev", "REAL"),
    ("clv_snapshots", "fair_odd", "REAL"),
    ("clv_snapshots", "fair_prob", "REAL"),
    ("clv_snapshots", "overround", "REAL"),
    ("played_bets", "value_bet_id", "INTEGER"),
    ("played_bets", "event_key", "TEXT"),
    ("played_bets", "sport", "TEXT"),
    ("played_bets", "book", "TEXT"),
    ("played_bets", "market", "TEXT"),
    ("played_bets", "outcome_label", "TEXT"),
    ("played_bets", "line", "REAL"),
    ("played_bets", "odd_taken", "REAL"),
    ("played_bets", "fair_odd", "REAL"),
    ("played_bets", "ev_pct", "REAL"),
    ("played_bets", "stake", "REAL"),
    # ── market_state : le contexte LIVE d'un prix ────────────────────────
    #
    # Ces cinq colonnes restent NULL pour tout ce qu'écrit le prématch : son
    # UPSERT ne les nomme pas. Elles n'existent que pour les sources LIVE, et
    # aucun calcul prématch ne les lit.
    #
    # `observed_at` ≠ `fetched_at`, et la distinction n'est pas cosmétique.
    # `fetched_at` est l'instant de NOTRE écriture ; `observed_at` est
    # l'instant où LA SOURCE a fabriqué le prix. Mesuré sur AsianOdds :
    # 76 ms d'écart médian, IQR 6 ms sur 52 000 messages. L'écart est petit
    # ici, mais c'est `observed_at` qui borne la péremption, pas notre horloge
    # d'écriture — un collecteur bloqué 30 s réécrit `fetched_at` sans que le
    # prix ait été revu.
    ("market_state", "observed_at", "TEXT"),
    # Score au moment de l'écriture, tel que NOUS le connaissons.
    ("market_state", "home_score", "INTEGER"),
    ("market_state", "away_score", "INTEGER"),
    # Score auquel LA SOURCE dit avoir fabriqué ce prix. Chez AsianOdds c'est
    # le champ FID, base64 de "HS:AS" — vérifié conforme sur 52 000 messages.
    # Stocké À CÔTÉ de home_score/away_score et non à leur place : quand les
    # deux viendront de sources différentes, leur DÉSACCORD est précisément le
    # signal « ce prix est périmé, un but est tombé depuis ». C'est la réponse
    # directe aux faux surebets live : un prix figé après un but devient
    # détectable au lieu d'être deviné.
    ("market_state", "feed_score", "TEXT"),
    # Minute de jeu annoncée par la source.
    ("market_state", "igm", "INTEGER"),
    # ── Traçabilité de l'appariement ─────────────────────────────────────
    #
    # Sans ces trois colonnes, une ligne LIVE ne dit pas D'OÙ elle vient, et
    # le diagnostic d'une collision du 24/08 est resté partiel faute de
    # pouvoir nommer les deux matchs sources : l'information n'avait jamais
    # quitté la mémoire du collecteur.
    #
    # L'identifiant du match CHEZ LA SOURCE (MTCHID chez AsianOdds). Deux
    # valeurs différentes sous une même event_key = au moins un rapprochement
    # faux, et c'est enfin constatable après coup.
    ("market_state", "source_event_id", "TEXT"),
    # La source annonçait-elle ce match dans l'autre sens (son domicile est
    # notre extérieur) ? 1 = les prix et les scores ont été permutés pour
    # revenir à NOTRE convention.
    ("market_state", "source_inverse", "INTEGER"),
    # L'instant de l'instantané `events` sur lequel le rapprochement a été
    # décidé — PAS celui de l'écriture, que `fetched_at` porte déjà. La liste
    # des candidats est relue une fois par minute : cette colonne dit si le
    # match a été décidé contre une liste fraîche ou vieille de 59 s.
    ("market_state", "matched_at", "TEXT"),
    # Le canal qui a reçu cette alerte. NULL = ligne écrite AVANT le routage
    # multi-canal, du temps où un pari n'avait qu'une seule destination.
    #
    # ⚠️ Ces NULL comptent pour TOUS les canaux (voir `_clause_canal`). Sans
    # cela, le jour du déploiement, chaque pari encore vivant paraîtrait
    # jamais notifié sur chaque canal et repartirait partout : une rafale de
    # doublons proportionnelle au nombre de canaux, au moment précis où
    # personne ne regarde.
    ("notified_value_bets", "chat_id", "TEXT"),
]


class Storage:
    def __init__(self, path: str | Path = "data/valuebet.db",
                 quote_heartbeat_sec: float | None = None):
        # Dernière signature écrite par marché : {clé: (signature, instant)}.
        # En mémoire seulement — un redémarrage repart d'un instantané complet,
        # ce qui est correct et sans conséquence.
        self._quote_sig: dict[tuple, tuple[tuple, datetime]] = {}
        # Chargé depuis `market_state` au premier appel de `insert_quotes_sparse`,
        # jamais dans __init__ : une instance qui n'écrit pas de cotes — la
        # plupart des commandes CLI — ne doit pas payer cette requête.
        self._quote_sig_amorce = False
        self._heartbeat = (
            quote_heartbeat_sec if quote_heartbeat_sec is not None
            else float(os.getenv("QUOTES_HEARTBEAT_SEC", "1800"))
        )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.executescript(FEATURES_SCHEMA)
            for table, column, coltype in MIGRATIONS:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                except sqlite3.OperationalError:
                    pass  # column already present
            # Après les migrations : l'index porte sur une colonne qu'elles
            # viennent d'ajouter, il ne peut pas vivre dans SCHEMA.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_vb_last_seen ON value_bets(last_seen_at)"
            )
            # Le dedoublonnage interroge cette table une fois par pari et par
            # canal a chaque cycle : le nombre de lectures est multiplie par
            # le nombre de canaux. idx_nvb_lookup porte deja (event_key, book,
            # market) ; chat_id en queue evite de toucher la ligne pour la
            # filtrer.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_nvb_canal ON notified_value_bets"
                "(event_key, book, market, chat_id)"
            )
            # « Ce qui a été VU récemment », par opposition à idx_ms_fetched
            # qui dit « ce qui a été ÉCRIT récemment ». Toute la raison d'être
            # d'observed_at est cette lecture-là ; sans index elle balaie la
            # table entière à chaque tick.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_ms_observed "
                "ON market_state(observed_at)"
            )
            # « Toutes les lignes venant de ce match source ». C'est la
            # requête de l'enquête : retrouver ce qu'un match AsianOdds
            # donné a écrit, et sous combien de clés. Index SIMPLE, jamais
            # dans `ux_market_state` : y faire entrer la source dupliquerait
            # chaque marché par source et détruirait l'état courant.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_ms_source "
                "ON market_state(source_event_id)"
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # Attente unique du projet (`config.SQLITE_BUSY_TIMEOUT_SEC`) : ce
        # chemin-ci portait 10 s quand la purge en portait 60. WAL activé au
        # premier appel (persisté dans le fichier) : les lectures restent
        # possibles pendant une écriture.
        conn = sqlite3.connect(str(self.path), timeout=SQLITE_BUSY_TIMEOUT_SEC)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_event(
        self, event_key: str, sport: str, league: str, home: str, away: str, start_time: datetime
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO events(event_key, sport, league, home, away, start_time) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_key, sport, league, home, away, start_time.isoformat()),
            )

    def insert_quote(self, q: OddQuote) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO quotes(event_key, book, market, outcome_label, line, decimal_odd, "
                "fetched_at, source_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    q.event_key,
                    q.book.value,
                    q.market.value,
                    q.outcome.label,
                    q.outcome.line,
                    q.decimal_odd,
                    q.fetched_at.isoformat(),
                    q.source_event_id,
                ),
            )

    # ------------------------------------------------ écriture parcimonieuse
    #
    # `quotes` pesait 34 Go pour deux jours de rétention, la purge nocturne
    # mettait plus d'une heure et n'arrivait plus à suivre le rythme
    # d'écriture. Or `line_speed` mesure 99,6 % de cotes Pinnacle IDENTIQUES
    # d'un cycle à l'autre : plus de 99 % de ce qu'on écrivait répétait ce qui
    # était déjà en base.
    #
    # On n'écrit donc plus qu'un marché qui a BOUGÉ. C'est le raisonnement qui
    # a rendu `odds_history` possible (§15.1) — entre deux points, une cote
    # n'est pas inconnue, elle est constante.
    #
    # ⚠️ Le marché est réécrit ENTIER dès qu'une seule de ses issues change.
    # C'est délibéré et c'est tout le design : `closing_group` exige que les
    # issues d'une clôture partagent le même `fetched_at`, parce que le devig
    # retire la marge en normalisant les issues les unes contre les autres.
    # Mélanger deux instants y laisserait une marge parasite, donc une CLV
    # fausse. On perd un peu de compression, on garde l'invariant dont dépend
    # toute la mesure.
    #
    # ⚠️ Le battement de cœur existe pour une raison de fond : un book dont les
    # cotes ne bougent pas n'écrirait plus rien et deviendrait indiscernable
    # d'un book en panne — le mode de défaillance dominant du projet (§11).
    # Réécrire périodiquement garantit qu'un book qui répond laisse une trace.

    def _market_key(self, q: "OddQuote") -> tuple:
        return (q.book.value, q.event_key, q.market.value, q.outcome.line)

    def _amorcer_quote_sig(self) -> int:
        """Reconstruire `_quote_sig` depuis `market_state`. Une requête, une fois.

        ⚠️ C'est CE point qui rend l'écriture creuse compatible avec plusieurs
        processus. Avant, l'état ne vivait qu'en mémoire d'instance : une
        seconde instance partait d'un dictionnaire vide et réécrivait tout
        l'instantané, y compris ce que la première venait d'écrire.

        La signature est reconstruite à l'IDENTIQUE — mêmes clés, même arrondi à
        quatre décimales, même `seen_at` (le maximum du groupe). C'est la preuve
        que `market_state` ne duplique pas cet état : il le contient, en plus
        durable. Si la reconstruction n'était pas exacte, un marché inchangé
        paraîtrait changé (écriture inutile) ou l'inverse (écriture perdue) —
        d'où le test qui compare les deux dictionnaires terme à terme.
        """
        groupes: dict[tuple, list[tuple[str, float, str]]] = {}
        with self._conn() as c:
            for r in c.execute(
                "SELECT event_key, book, market, outcome_label, line, odd, fetched_at "
                "FROM market_state"
            ):
                cle = (r["book"], r["event_key"], r["market"], r["line"])
                groupes.setdefault(cle, []).append(
                    (r["outcome_label"], r["odd"], r["fetched_at"]))
        for cle, lignes in groupes.items():
            sig = tuple(sorted((lab, round(odd, 4)) for lab, odd, _ in lignes))
            vu = max(datetime.fromisoformat(t) for _, _, t in lignes)
            self._quote_sig[cle] = (sig, vu)
        self._quote_sig_amorce = True
        return len(groupes)

    def insert_quotes_sparse(self, quotes: "Iterable[OddQuote]") -> int:
        """Comme `insert_quotes`, mais n'écrit que les marchés qui ont changé.

        Renvoie le nombre de lignes réellement écrites.

        L'état de comparaison vient de `market_state`, chargé une fois au
        premier appel puis tenu en mémoire. Deux conséquences, toutes deux
        voulues : un redémarrage ne réécrit plus l'instantané complet, et deux
        processus qui partagent la base partagent la décision. Le cache en
        mémoire reste devant, parce que la relire à chaque cycle coûterait une
        requête sur des dizaines de milliers de lignes pour une information
        qu'on vient d'écrire soi-même."""
        if not self._quote_sig_amorce:
            self._amorcer_quote_sig()
        by_market: dict[tuple, list[OddQuote]] = {}
        for q in quotes:
            by_market.setdefault(self._market_key(q), []).append(q)
        if not by_market:
            return 0

        to_write: list[OddQuote] = []
        for key, group in by_market.items():
            # Signature indépendante de l'ordre de collecte : deux cycles qui
            # rendent les mêmes issues dans un ordre différent sont identiques.
            sig = tuple(sorted((g.outcome.label, round(g.decimal_odd, 4))
                               for g in group))
            seen_at = max(g.fetched_at for g in group)
            prev = self._quote_sig.get(key)
            if prev is not None:
                prev_sig, prev_at = prev
                unchanged = prev_sig == sig
                fresh = (seen_at - prev_at).total_seconds() < self._heartbeat
                if unchanged and fresh:
                    continue
            self._quote_sig[key] = (sig, seen_at)
            to_write.extend(group)
        return self.insert_quotes(to_write)

    # UPSERT sur l'index UNIQUE À EXPRESSION : la cible du ON CONFLICT doit
    # reprendre l'expression telle quelle, COALESCE compris. Sans ça SQLite ne
    # reconnaît pas l'index et l'UPSERT devient un INSERT — donc un doublon par
    # cycle sur chaque 1X2, sans la moindre erreur.
    _UPSERT_MARKET_STATE = (
        "INSERT INTO market_state "
        "(event_key, book, market, outcome_label, line, odd, fetched_at, is_live, league) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (event_key, market, outcome_label, COALESCE(line, -1e9), book) "
        "DO UPDATE SET odd = excluded.odd, "
        "              fetched_at = excluded.fetched_at, "
        "              is_live = excluded.is_live, "
        "              league = COALESCE(excluded.league, market_state.league)"
    )

    @staticmethod
    def _market_state_rows(quotes: "list[OddQuote]") -> list[tuple]:
        return [
            (q.event_key, q.book.value, q.market.value, q.outcome.label,
             q.outcome.line, q.decimal_odd, q.fetched_at.isoformat(),
             1 if q.from_live_feed else 0, q.league)
            for q in quotes
        ]

    # UPSERT du LIVE. Distinct de _UPSERT_MARKET_STATE et non une extension de
    # celui-ci : le prématch ne doit PAS nommer les colonnes LIVE, sinon chacun
    # de ses cycles les écraserait à NULL. Deux écrivains, deux requêtes, aucun
    # champ partagé au-delà de la clé naturelle.
    _UPSERT_LIVE_STATE = (
        "INSERT INTO market_state "
        "(event_key, book, market, outcome_label, line, odd, fetched_at, "
        " is_live, league, observed_at, home_score, away_score, feed_score, igm, "
        " source_event_id, source_inverse, matched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (event_key, market, outcome_label, COALESCE(line, -1e9), book) "
        "DO UPDATE SET odd = excluded.odd, "
        "              fetched_at = excluded.fetched_at, "
        "              is_live = excluded.is_live, "
        "              league = COALESCE(excluded.league, market_state.league), "
        "              observed_at = excluded.observed_at, "
        "              home_score = excluded.home_score, "
        "              away_score = excluded.away_score, "
        "              feed_score = excluded.feed_score, "
        "              igm = excluded.igm, "
        "              source_event_id = excluded.source_event_id, "
        "              source_inverse = excluded.source_inverse, "
        "              matched_at = excluded.matched_at"
    )

    def upsert_live_state(self, rows: "Iterable[tuple]") -> int:
        """Écrire l'état LIVE d'un lot de sélections. Renvoie le nombre de lignes.

        Chaque ligne :
          (event_key, book, market, outcome_label, line, odd, fetched_at_iso,
           is_live, league, observed_at_iso, home_score, away_score,
           feed_score, igm)

        N'écrit QUE dans `market_state`. Rien dans `quotes` : le journal
        prématch est dimensionné pour ~1 écriture par marché et par cycle de
        plusieurs minutes, là où le LIVE reprice toutes les 28 s en médiane et
        toutes les 2,7 s sur les marchés actifs. L'y déverser gonflerait une
        table que rien ne lirait, et allongerait les purges que le prématch
        subit."""
        rows = list(rows)
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(self._UPSERT_LIVE_STATE, rows)
        return len(rows)

    def insert_quotes(self, quotes: "Iterable[OddQuote]") -> int:
        """Batch-insert quotes in a SINGLE transaction. The per-quote insert_quote
        opens a fresh connection and fsync-commits each row, which is catastrophic
        for the thousands of quotes a cycle produces — this does one connection,
        one executemany, one commit. Returns the number of rows inserted.

        Met aussi `market_state` à jour, DANS LA MÊME TRANSACTION. Deux
        transactions séparées laisseraient, sur une coupure entre les deux, un
        état courant qui affirme un prix que le journal n'a pas — et c'est
        précisément l'état courant que le futur LIVE croira sur parole."""
        quotes = list(quotes)
        rows = [
            (q.event_key, q.book.value, q.market.value, q.outcome.label,
             q.outcome.line, q.decimal_odd, q.fetched_at.isoformat(), q.source_event_id)
            for q in quotes
        ]
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                "INSERT INTO quotes(event_key, book, market, outcome_label, line, decimal_odd, "
                "fetched_at, source_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            c.executemany(self._UPSERT_MARKET_STATE, self._market_state_rows(quotes))
        return len(rows)

    def prune_market_state(self, max_age_days: float = 2.0,
                           now: "datetime | None" = None) -> int:
        """Oublier l'état des matchs finis. Renvoie le nombre de lignes ôtées.

        ⚠️ Sans ça, `market_state` est bornée par l'OFFRE COURANTE en théorie et
        par RIEN en pratique : un match joué hier y garde sa ligne pour
        toujours. Le §21.22 a relevé exactement ce défaut sur `_LATE_EDGES` —
        une fuite lente sur un processus qui tourne des semaines, et ici elle
        alourdirait en plus l'amorçage de `_quote_sig` à chaque démarrage.

        Le critère est le COUP D'ENVOI lu dans la clé, pas `fetched_at` : un
        marché qu'on ne cote plus n'est plus rafraîchi, donc son `fetched_at`
        se fige et le rendrait immortel au moment même où il devient inutile.
        Une clé qu'on ne sait pas dater part aussi — elle ne pourra plus jamais
        être appariée à un événement.
        """
        from .matcher import parse_event_key

        now = now or datetime.now(timezone.utc)
        limite = now - timedelta(days=max_age_days)
        with self._conn() as c:
            cles = [r[0] for r in c.execute(
                "SELECT DISTINCT event_key FROM market_state")]
            perimes = []
            for ek in cles:
                parsed = parse_event_key(ek)
                if parsed is None or parsed[0] < limite:
                    perimes.append((ek,))
            if perimes:
                c.executemany("DELETE FROM market_state WHERE event_key = ?", perimes)
        # Le cache en mémoire suivrait sinon un état que la base n'a plus.
        partis = {e[0] for e in perimes}
        for cle in [k for k in self._quote_sig if k[1] in partis]:
            del self._quote_sig[cle]
        return len(perimes)

    def market_state(self, event_key: str | None = None, *,
                     live_only: bool = False,
                     since: "datetime | None" = None,
                     observed_since: "datetime | None" = None) -> list[sqlite3.Row]:
        """Le dernier prix connu, filtré par les trois accès que le LIVE fera.

        Sans argument : tout l'état courant. Les trois filtres correspondent aux
        trois index posés sur la table, et à rien d'autre — un quatrième mode
        de lecture demanderait un quatrième index, et il faudra le justifier."""
        clauses, args = [], []
        if event_key is not None:
            clauses.append("event_key = ?")
            args.append(event_key)
        if live_only:
            clauses.append("is_live = 1")
        if since is not None:
            clauses.append("fetched_at > ?")
            args.append(since.isoformat())
        # `since` filtre sur fetched_at (dernière ÉCRITURE), `observed_since`
        # sur observed_at (dernière OBSERVATION à la source). Le second répond
        # à « ce prix est-il encore frais » : mesuré sur AsianOdds, 95 % des
        # lignes sont revues en moins de 40 s, mais la queue monte à 12 min 49.
        # L'absence de message ne veut donc PAS dire « le prix n'a pas bougé ».
        if observed_since is not None:
            clauses.append("observed_at > ?")
            args.append(observed_since.isoformat())
        sql = "SELECT * FROM market_state"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._conn() as c:
            return c.execute(sql, args).fetchall()

    def upsert_events(self, rows: "Iterable[tuple]") -> None:
        """Batch INSERT OR IGNORE events in one transaction.
        Each row = (event_key, sport, league, home, away, start_time_iso).

        La ligue est complétée après coup lorsqu'elle manque : un simple
        INSERT OR IGNORE laissait vide à jamais tout événement vu une première
        fois sans ligue — au premier cycle qui suit un redémarrage, ou quand
        Pinnacle ne la renvoie pas encore. Une ligue jamais remplie ne se
        rattrape pas, alors qu'une ligue déjà connue n'est jamais écrasée."""
        rows = list(rows)
        if not rows:
            return
        with self._conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO events(event_key, sport, league, home, away, start_time) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            c.executemany(
                "UPDATE events SET league = ? "
                "WHERE event_key = ? AND (league IS NULL OR league = '')",
                [(r[2], r[0]) for r in rows if r[2]],
            )

    def prune_quotes(
        self, retention_days: int = 7, *,
        batch_size: int = 200_000, pause_sec: float = 0.05,
        max_seconds: float | None = None,
        progress: "Callable[[int, float], None] | None" = None,
    ) -> int:
        """Delete raw quote rows older than retention_days and return how many
        were removed. The quotes table grows ~unbounded (every quote, every
        cycle); only recent rows matter — closing lines are captured into
        clv_snapshots within hours of kickoff, after which the raw history is
        dead weight.

        Supprime par lots, pour deux raisons distinctes qu'il ne faut pas
        confondre :

        1. **Borner le WAL.** Un seul DELETE massif sur une table de plusieurs
           gigaoctets fait gonfler le fichier -wal de plusieurs gigaoctets
           avant de pouvoir valider, et a rempli le disque en pratique. Une
           boucle par lots le maintient à la taille d'un lot.

        2. **Ne pas assommer le daemon.** C'est le point ajouté le 01/08 après
           l'avoir constaté en production : lancer cette purge pendant que le
           daemon tourne remplissait le log de « database is locked » sur
           TOUS les books, sports entiers compris, pendant toute sa durée.

        La cause n'était pas les DELETE mais le `wal_checkpoint(TRUNCATE)`
        exécuté après CHAQUE lot : TRUNCATE prend un verrou exclusif et attend
        la fin de tous les lecteurs. Des centaines de lots, donc des centaines
        de blocages, largement au-delà des 10 s de patience du daemon.

        PASSIVE fait le même travail de recyclage sans jamais bloquer : le
        fichier -wal n'est pas tronqué mais réutilisé, ce qui suffit puisque
        c'est le découpage en lots — et non TRUNCATE — qui borne sa taille. Le
        TRUNCATE ne sert plus qu'une fois, à la fin, pour rendre l'espace.

        Une courte pause entre les lots laisse en plus une fenêtre d'écriture
        au daemon : sans elle, la purge enchaîne les transactions sans jamais
        lui rendre la main.

        `max_seconds` borne la durée. Supprimer des dizaines de millions de
        lignes coûte surtout la mise à jour des deux index de `quotes`, et sur
        un gros retard cela peut durer des heures. Une purge nocturne qui
        déborde sur la journée est pire que le retard qu'elle rattrape : elle
        s'arrête donc au budget, et la nuit suivante reprend là où elle en
        était. Le retard se résorbe en quelques nuits au lieu d'une.

        `progress` est appelé après chaque lot avec (lignes supprimées,
        secondes écoulées). Sans lui, la commande reste muette pendant des
        minutes et donne toutes les raisons de croire qu'elle est bloquée —
        c'est ce qui a conduit à l'interrompre en production."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        removed = 0
        started = time.monotonic()
        conn = sqlite3.connect(str(self.path), timeout=SQLITE_BUSY_TIMEOUT_SEC)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SEC * 1000)}")
            while True:
                cur = conn.execute(
                    "DELETE FROM quotes WHERE rowid IN "
                    "(SELECT rowid FROM quotes WHERE fetched_at < ? LIMIT ?)",
                    (cutoff, batch_size),
                )
                conn.commit()
                n = cur.rowcount
                removed += n
                if n == 0:
                    break
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                elapsed = time.monotonic() - started
                if progress is not None:
                    progress(removed, elapsed)
                if max_seconds is not None and elapsed >= max_seconds:
                    break
                if pause_sec:
                    time.sleep(pause_sec)
            # Une seule fois, la purge terminée : plus rien n'est en attente,
            # donc le verrou exclusif ne coûte qu'un instant.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        return removed

    def insert_bet_features(self, rows: Iterable[tuple]) -> int:
        """Écrire les features d'un lot de détections. Voir FEATURES_SCHEMA.

        `INSERT OR REPLACE` sur value_bet_id : rejouer un cycle ne duplique
        rien. Un échec ici ne doit jamais faire tomber le scan — c'est de la
        collecte annexe, le pari lui-même est déjà en base."""
        rows = list(rows)
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO bet_features("
                "value_bet_id, detected_at, event_key, sport, league, league_category,"
                "book, market, outcome_label, line, odd_taken, fair_odd, ev_pct,"
                "kelly_pct, delay_h, reference_book, ref_overround, ref_n_outcomes,"
                "ref_age_sec, n_books_market, match_score, time_shift_min) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def seed_corrections(self, rows: Iterable[tuple]) -> int:
        """Ouvrir le suivi de correction pour un lot de détections.

        `INSERT OR IGNORE` : une détection re-signalée au cycle suivant ne doit
        pas remettre son chronomètre à zéro, sinon un book qui ne corrige
        jamais afficherait un délai toujours nul."""
        rows = list(rows)
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO bet_corrections("
                "value_bet_id, detected_at, kickoff, book, event_key, market,"
                "outcome_label, line, odd_taken, fair_odd) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def open_corrections(self, *, max_age_hours: float | None = None) -> list[sqlite3.Row]:
        """Suivis encore ouverts : **jusqu'au coup d'envoi**.

        La condition portait sur les jalons — un suivi se fermait dès que la
        fenêtre jouable ET l'alignement étaient franchis. C'était suffisant pour
        mesurer des délais, mais ça tronquait la courbe : un book qui rejoint la
        ligne juste en dix minutes cessait d'être observé pendant les six heures
        suivantes, alors que c'est là que le marché se forme.

        Les jalons ne s'en trouvent pas faussés : chaque `UPDATE` porte sa
        propre garde `... IS NULL`, donc un jalon déjà franchi n'est jamais
        réécrit.

        Un suivi sans `kickoff` connu retombe sur l'ancienne règle : sans heure
        de fin, il faut bien un critère d'arrêt. Et l'âge reste borné pour que
        la requête demeure petite à chaque cycle — un horaire faux ne doit pas
        faire traîner un suivi indéfiniment.

        La borne d'âge est le levier de coût de tout le suivi : elle décide
        combien de lignes sont relues ET réécrites à chaque cycle. 168 h couvre
        41 % de détections dont le coup d'envoi dépasse 48 h, au prix d'environ
        trois fois plus de suivis ouverts. Réglable par
        `CORRECTIONS_MAX_AGE_HOURS` sans déploiement, précisément parce que
        c'est le premier paramètre à baisser si un cycle s'allonge."""
        if max_age_hours is None:
            max_age_hours = float(os.getenv("CORRECTIONS_MAX_AGE_HOURS", "168"))
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM bet_corrections "
                "WHERE detected_at > ? AND ("
                "  (kickoff IS NOT NULL AND kickoff > ?)"
                "  OR (kickoff IS NULL AND (corrected_at IS NULL OR aligned_at IS NULL))"
                ")", (cutoff, now.isoformat())
            ))

    def update_corrections(
        self, observed: Iterable[tuple], corrected: Iterable[tuple],
        aligned: Iterable[tuple] = (), history: Iterable[tuple] = (),
    ) -> None:
        """`observed` = (instant, cote vue, id) pour les suivis encore ouverts ;
        `corrected` = (instant, secondes, cote, id) pour ceux dont la fenêtre
        jouable vient de se fermer ; `aligned` = idem pour ceux qui viennent de
        rejoindre la ligne juste ; `history` = (id, instant, cote, cote juste,
        ev) pour les seules cotes qui ont CHANGÉ depuis le dernier point
        enregistré.

        Le tout dans une seule transaction : l'historique n'a de sens que s'il
        est cohérent avec le `last_odd_seen` qui décide du prochain point."""
        observed, corrected, aligned = list(observed), list(corrected), list(aligned)
        history = list(history)
        with self._conn() as c:
            if history:
                c.executemany(
                    "INSERT INTO odds_history(value_bet_id, book, seen_at, odd, "
                    "fair_odd, ev_pct) VALUES (?, ?, ?, ?, ?, ?)", history,
                )
            if observed:
                c.executemany(
                    "UPDATE bet_corrections SET observed_until = ?, "
                    "observations = COALESCE(observations, 0) + 1, "
                    "min_odd_seen = MIN(COALESCE(min_odd_seen, ?), ?) "
                    "WHERE value_bet_id = ?",
                    [(ts, odd, odd, vid) for ts, odd, vid in observed],
                )
            if corrected:
                # Ne jamais réécrire un jalon déjà franchi : le premier passage
                # est le bon, les cycles suivants voient toujours la condition
                # vraie et repousseraient l'instant indéfiniment.
                c.executemany(
                    "UPDATE bet_corrections SET corrected_at = ?, "
                    "seconds_to_corr = ?, odd_at_corr = ? "
                    "WHERE value_bet_id = ? AND corrected_at IS NULL",
                    corrected,
                )
            if aligned:
                c.executemany(
                    "UPDATE bet_corrections SET aligned_at = ?, "
                    "seconds_to_align = ?, odd_at_align = ? "
                    "WHERE value_bet_id = ? AND aligned_at IS NULL",
                    aligned,
                )

    def corrections_report(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM bet_corrections"))

    def features_with_clv(self) -> list[sqlite3.Row]:
        """Features jointes à leur ligne de clôture dévigée, quand elle existe.

        LEFT JOIN volontaire : une détection sans clôture reste visible, ce qui
        permet de distinguer « pas encore mesurable » de « rien collecté » —
        deux situations que le même tableau vide confondrait."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT bf.*, cs.fair_odd AS closing_fair_odd, "
                "pb.dedup_key IS NOT NULL AS played "
                "FROM bet_features bf "
                "LEFT JOIN clv_snapshots cs "
                "  ON cs.value_bet_id = bf.value_bet_id AND cs.closing = 1 "
                "LEFT JOIN played_bets pb ON pb.value_bet_id = bf.value_bet_id "
                "ORDER BY bf.detected_at"
            ))

    def count_quotes_older_than(self, retention_days: int) -> int:
        """Combien de lignes la purge a encore à supprimer. Sert à dire si un
        arrêt sur budget de temps a laissé du retard — sans quoi une purge qui
        n'arrive jamais au bout ressemble à une purge qui a réussi."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM quotes WHERE fetched_at < ?", (cutoff,)
            ).fetchone()[0]

    def prune_notifications(self, retention_days: int = 30) -> int:
        """Trim old dedup bookkeeping rows (notified_*). Tiny vs quotes, but
        keeps the tables tidy. Returns total rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        removed = 0
        with self._conn() as c:
            for table in ("notified_value_bets", "notified_surebets", "notified_clv_alerts", "notified_middles"):
                cur = c.execute(f"DELETE FROM {table} WHERE notified_at < ?", (cutoff,))
                removed += cur.rowcount
        return removed

    def vacuum(self) -> None:
        """Rebuild the database file to reclaim space freed by deletes. Must run
        outside a transaction, so it uses its own autocommit connection.

        ⚠️ VACUUM écrit sa copie compactée dans un fichier TEMPORAIRE avant de
        la remettre en place, et SQLite choisit cet emplacement dans
        SQLITE_TMPDIR / TMPDIR / /var/tmp / /tmp — PAS à côté de la base. Sur
        cette VM /tmp est petit : la place vérifiée en amont était donc celle
        d'un disque, et la copie partait sur un autre. On force l'emplacement
        sur le répertoire de la base, celui-là même dont on a mesuré l'espace,
        pour que la garde porte sur le bon disque."""
        import os as _os

        _prev = _os.environ.get("SQLITE_TMPDIR")
        _os.environ["SQLITE_TMPDIR"] = str(self.path.parent)
        conn = sqlite3.connect(str(self.path), timeout=SQLITE_BUSY_TIMEOUT_SEC)
        conn.isolation_level = None  # autocommit — VACUUM can't run in a tx
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
            if _prev is None:
                _os.environ.pop("SQLITE_TMPDIR", None)
            else:
                _os.environ["SQLITE_TMPDIR"] = _prev

    def insert_value_bet(self, vb: ValueBet, stake: Optional[float] = None) -> int:
        """Persist a detected value bet. Returns the new row id, or the
        existing row id if the same (event, book, market, outcome, line) tuple
        is already on file — we want one tracking record per opportunity, not
        one per scan that re-surfaced it.

        Une ré-détection ne touche QUE last_seen_at / last_odd / last_ev.
        detected_at, odd_taken et ev_pct restent ceux de la première détection :
        toute la mesure de CLV compare la clôture à l'EV de départ, et les
        réécrire à chaque cycle la rendrait fausse — le CLV ne ferait plus que
        répéter l'EV, exactement l'erreur corrigée en §1.

        Ces trois colonnes sont en revanche la seule façon de savoir qu'un pari
        est encore vivant : sans elles, un pari détecté il y a trois heures et
        mort depuis est indiscernable d'un pari que le daemon vient de revoir.
        """
        existing = self.find_value_bet_id(
            vb.event_key, vb.book.value, vb.market.value, vb.outcome.label, vb.outcome.line
        )
        if existing is not None:
            with self._conn() as c:
                c.execute(
                    "UPDATE value_bets SET last_seen_at=?, last_odd=?, last_ev=? WHERE id=?",
                    (vb.detected_at.isoformat(), vb.odd_taken, vb.ev_pct, existing),
                )
            return existing
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO value_bets(event_key, book, market, outcome_label, line, odd_taken, "
                "fair_prob, fair_odd, ev_pct, kelly_pct, stake, detected_at, "
                "reference_book, last_seen_at, last_odd, last_ev) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    vb.event_key,
                    vb.book.value,
                    vb.market.value,
                    vb.outcome.label,
                    vb.outcome.line,
                    vb.odd_taken,
                    vb.fair_prob,
                    vb.fair_odd,
                    vb.ev_pct,
                    vb.kelly_stake_pct,
                    stake,
                    vb.detected_at.isoformat(),
                    # None quand la référence est Pinnacle : c'est le cas de
                    # l'écrasante majorité, et NULL se lit « pinnacle » partout
                    # en aval, ce qui garde l'historique antérieur cohérent.
                    (vb.reference_book.value
                     if getattr(vb, "reference_book", None) is not None
                     and vb.reference_book is not Book.PINNACLE else None),
                    vb.detected_at.isoformat(),
                    vb.odd_taken,
                    vb.ev_pct,
                ),
            )
            return int(cur.lastrowid or 0)

    def find_value_bet_id(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float],
    ) -> Optional[int]:
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT id FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line IS NULL",
                    (event_key, book, market, outcome_label),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line=?",
                    (event_key, book, market, outcome_label, line),
                ).fetchone()
            return int(row["id"]) if row else None

    def find_value_bet_ev(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float],
    ) -> Optional[tuple[int, float]]:
        """Return (id, ev_pct) for an existing bet, or None if not yet tracked."""
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT id, ev_pct FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line IS NULL",
                    (event_key, book, market, outcome_label),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id, ev_pct FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line=?",
                    (event_key, book, market, outcome_label, line),
                ).fetchone()
            return (int(row["id"]), float(row["ev_pct"])) if row else None

    def update_value_bet_ev(self, value_bet_id: int, ev_pct: float) -> None:
        """Refresh the stored EV% so the next scan delta-checks against the latest value."""
        with self._conn() as c:
            c.execute(
                "UPDATE value_bets SET ev_pct=? WHERE id=?",
                (ev_pct, value_bet_id),
            )

    def open_value_bets(self) -> list[sqlite3.Row]:
        """Bets that still might get a closing snapshot.

        `closing_lost` excludes the ones whose quotes were purged before the
        closing could be captured. Without it they pile up forever and every
        run reports the same failure — 146 of them at the time this was added,
        105 older than a week. A real outage would then look exactly like the
        permanent residue, which is how a broken capture stays unnoticed."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT vb.* FROM value_bets vb "
                "LEFT JOIN clv_snapshots cs ON cs.value_bet_id = vb.id AND cs.closing = 1 "
                "WHERE cs.id IS NULL AND COALESCE(vb.closing_lost, 0) = 0 "
                "ORDER BY vb.detected_at"
            ))

    def mark_closing_lost(self, value_bet_ids: Iterable[int]) -> int:
        """Retire définitivement des paris de la file de clôture.

        À n'appeler que sur des paris dont le coup d'envoi est passé depuis
        plus longtemps que la rétention : leurs cotes n'existent plus nulle
        part, la clôture ne peut plus être reconstituée."""
        ids = [(int(i),) for i in value_bet_ids]
        if not ids:
            return 0
        with self._conn() as c:
            c.executemany("UPDATE value_bets SET closing_lost = 1 WHERE id = ?", ids)
        return len(ids)

    def closing_snapshot(self, value_bet_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM clv_snapshots WHERE value_bet_id=? AND closing=1 "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (value_bet_id,),
            ).fetchone()

    @staticmethod
    def _event_key_like(event_key: str) -> str:
        """Build a LIKE pattern that matches any event_key with the same date
        and teams regardless of the exact kick-off minute.

        event_key format: "YYYYMMDDHHMM::home__vs__away"
        Pattern produced:  "YYYYMMDD%::home__vs__away"

        Pinnacle sometimes adjusts a match's start time by a few minutes
        between scans (DST corrections, late schedule changes). When that
        happens the full key changes but the date + teams stay identical, so
        exact-key dedup would miss the stored notification and fire again.
        Matching on date+teams makes dedup robust to minor time drifts."""
        if "::" not in event_key:
            return event_key  # malformed key — fall back to exact match
        date_prefix = event_key[:8]          # "YYYYMMDD"
        teams_part = event_key.split("::", 1)[1]  # "home__vs__away"
        return f"{date_prefix}%::{teams_part}"

    def surebet_already_notified(
        self, event_key: str, market: str, line: Optional[float],
        current_margin_pct: float = 0.0, roi_delta_pct: float = 0.5,
    ) -> bool:
        """Return True (skip) only when the surebet was already notified AND
        its margin hasn't moved by more than roi_delta_pct since the last alert.
        A ROI change >= roi_delta_pct triggers a fresh notification so the user
        sees the updated opportunity without needing to disable dedup entirely."""
        like_key = self._event_key_like(event_key)
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT margin_pct FROM notified_surebets WHERE event_key LIKE ? AND market=? "
                    "AND line IS NULL ORDER BY notified_at DESC LIMIT 1",
                    (like_key, market),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT margin_pct FROM notified_surebets WHERE event_key LIKE ? AND market=? "
                    "AND line=? ORDER BY notified_at DESC LIMIT 1",
                    (like_key, market, line),
                ).fetchone()
            if row is None:
                return False
            last_margin_pct = row[0]
            return abs(current_margin_pct - last_margin_pct) < roi_delta_pct

    def surebet_notify_count(
        self, event_key: str, market: str, line: Optional[float],
    ) -> int:
        """How many times this surebet has already been alerted, so the daemon
        can cap re-alerts at a fixed number per opportunity (same hard cap as
        value bets), jitter-proof regardless of the ROI-delta dedup."""
        like_key = self._event_key_like(event_key)
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT COUNT(*) FROM notified_surebets "
                    "WHERE event_key LIKE ? AND market=? AND line IS NULL",
                    (like_key, market),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) FROM notified_surebets "
                    "WHERE event_key LIKE ? AND market=? AND line=?",
                    (like_key, market, line),
                ).fetchone()
            return int(row[0]) if row else 0

    def mark_surebet_notified(
        self, event_key: str, market: str, line: Optional[float],
        margin_pct: float, notified_at: datetime,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO notified_surebets(event_key, market, line, margin_pct, notified_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_key, market, line, margin_pct, notified_at.isoformat()),
            )

    def middle_already_notified(
        self, event_key: str, low_line: float, high_line: float,
        current_ev_pct: float = 0.0, ev_delta_pct: float = 2.0,
    ) -> bool:
        """Return True (skip) when this middle was already notified AND its EV
        hasn't moved by ev_delta_pct since the last alert. Mirrors the surebet
        dedup so a meaningful EV shift re-alerts without disabling dedup."""
        like_key = self._event_key_like(event_key)
        with self._conn() as c:
            row = c.execute(
                "SELECT ev_pct FROM notified_middles WHERE event_key LIKE ? "
                "AND low_line=? AND high_line=? ORDER BY notified_at DESC LIMIT 1",
                (like_key, low_line, high_line),
            ).fetchone()
            if row is None:
                return False
            return abs(current_ev_pct - row[0]) < ev_delta_pct

    def middle_notify_count(
        self, event_key: str, low_line: float, high_line: float,
    ) -> int:
        """How many times this middle has already been alerted, for the hard
        per-opportunity re-alert cap (jitter-proof, same as surebets)."""
        like_key = self._event_key_like(event_key)
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM notified_middles "
                "WHERE event_key LIKE ? AND low_line=? AND high_line=?",
                (like_key, low_line, high_line),
            ).fetchone()
            return int(row[0]) if row else 0

    def mark_middle_notified(
        self, event_key: str, low_line: float, high_line: float,
        ev_pct: float, notified_at: datetime,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO notified_middles(event_key, low_line, high_line, ev_pct, notified_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_key, low_line, high_line, ev_pct, notified_at.isoformat()),
            )

    def clv_alert_already_notified(self, value_bet_id: int) -> bool:
        """True if a pre-kickoff CLV alert was already sent for this value bet row."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM notified_clv_alerts WHERE value_bet_id=? LIMIT 1",
                (value_bet_id,),
            ).fetchone()
            return row is not None

    def mark_clv_alert_notified(
        self, value_bet_id: int, clv_pct: float,
        current_pin_odd: float, notified_at: datetime,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO notified_clv_alerts(value_bet_id, clv_pct, current_pin_odd, notified_at) "
                "VALUES (?, ?, ?, ?)",
                (value_bet_id, clv_pct, current_pin_odd, notified_at.isoformat()),
            )

    @staticmethod
    def _clause_canal(chat_id: Optional[str]) -> tuple[str, tuple]:
        """Le fragment SQL qui restreint le dédoublonnage à un canal.

        `chat_id=None` — le défaut, et le SEUL usage de la production
        aujourd'hui — ne produit AUCUNE clause : la requête est alors mot pour
        mot celle d'avant le routage multi-canal. Un pari notifié compte une
        fois, quelle que soit sa destination.

        `chat_id="X"` compte les lignes de X **et celles laissées à NULL**.
        Ces NULL sont l'historique d'avant la bascule : un pari alerté hier
        l'a été sans qu'on sache où. Les ignorer ferait paraître chaque pari
        vivant « jamais notifié » sur chaque canal le jour du déploiement, et
        il repartirait partout à la fois."""
        if chat_id is None:
            return "", ()
        return " AND (chat_id=? OR chat_id IS NULL)", (chat_id,)

    def value_bet_already_notified(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float],
        current_ev_pct: float = 0.0, ev_delta_pct: float = 1.0,
        chat_id: Optional[str] = None,
    ) -> bool:
        """Return True (skip) when this value bet was already notified AND its
        EV hasn't moved by ev_delta_pct since the last alert.

        `chat_id` restreint la question à un canal — voir `_clause_canal`. Non
        fourni : comportement d'avant le multi-canal, inchangé."""
        like_key = self._event_key_like(event_key)
        canal, p_canal = self._clause_canal(chat_id)
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT ev_pct FROM notified_value_bets "
                    "WHERE event_key LIKE ? AND book=? AND market=? AND outcome_label=? AND line IS NULL"
                    + canal + " ORDER BY notified_at DESC LIMIT 1",
                    (like_key, book, market, outcome_label) + p_canal,
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT ev_pct FROM notified_value_bets "
                    "WHERE event_key LIKE ? AND book=? AND market=? AND outcome_label=? AND line=?"
                    + canal + " ORDER BY notified_at DESC LIMIT 1",
                    (like_key, book, market, outcome_label, line) + p_canal,
                ).fetchone()
            if row is None:
                return False
            return abs(current_ev_pct - row[0]) < ev_delta_pct

    def value_bet_notify_count(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float], chat_id: Optional[str] = None,
    ) -> int:
        """How many times this value bet has already been alerted. Used to cap
        re-alerts at a fixed number per bet, so a bet whose EV keeps jittering
        across the dedup delta can't notify forever.

        `chat_id` compte par canal — voir `_clause_canal`. Sans lui, le
        plafond reste global, comme avant."""
        like_key = self._event_key_like(event_key)
        canal, p_canal = self._clause_canal(chat_id)
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT COUNT(*) FROM notified_value_bets "
                    "WHERE event_key LIKE ? AND book=? AND market=? AND outcome_label=? AND line IS NULL"
                    + canal,
                    (like_key, book, market, outcome_label) + p_canal,
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) FROM notified_value_bets "
                    "WHERE event_key LIKE ? AND book=? AND market=? AND outcome_label=? AND line=?"
                    + canal,
                    (like_key, book, market, outcome_label, line) + p_canal,
                ).fetchone()
            return int(row[0]) if row else 0

    def mark_value_bet_notified(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float], ev_pct: float, notified_at: datetime,
        chat_id: Optional[str] = None,
    ) -> None:
        """`chat_id` non fourni écrit NULL : c'est ce que fait la production
        aujourd'hui, et une ligne NULL vaut « notifié partout »."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO notified_value_bets"
                "(event_key, book, market, outcome_label, line, ev_pct, notified_at, chat_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_key, book, market, outcome_label, line, ev_pct,
                 notified_at.isoformat(), chat_id),
            )

    # ══ Canaux configurables ═══════════════════════════════════════════
    # SQL uniquement : cette classe ne connait pas `routing`. La conversion
    # des lignes vers Canal/Regle/Critere vit dans `src/channels.py`, ce qui
    # garde la persistance ignorante du modele de decision — et le modele de
    # decision ignorant de SQLite.

    def create_channel(self, chat_id: str, nom: str, *, actif: bool = True,
                       priorite: int = 100, exclusif: bool = False,
                       profile_id: Optional[int] = None) -> int:
        """Cree un canal et rend son id. Le nom est UNIQUE : c'est par lui
        que les commandes Telegram le designeront."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO channels(chat_id, nom, actif, priorite, exclusif,"
                " profile_id, cree_le) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, nom, int(actif), int(priorite), int(exclusif),
                 profile_id, datetime.now(timezone.utc).isoformat()),
            )
            return int(cur.lastrowid)

    def add_channel_rule(
        self, channel_id: int, *,
        ev_min: Optional[float] = None, ev_min_strict: bool = False,
        ev_max: Optional[float] = None, ev_max_strict: bool = False,
        odd_min: Optional[float] = None, odd_min_strict: bool = False,
        odd_max: Optional[float] = None, odd_max_strict: bool = False,
        phase: Optional[str] = None,
    ) -> int:
        """Ajoute une regle. Les parametres refletent les colonnes une a une :
        une couche de persistance qui reinterprete ses propres colonnes est
        une couche ou l'on ne sait plus ce qui est stocke."""
        if phase is not None and phase not in ("prematch", "live"):
            raise ValueError(f"phase inconnue : {phase!r}")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO channel_rules(channel_id, ev_min, ev_min_strict,"
                " ev_max, ev_max_strict, odd_min, odd_min_strict, odd_max,"
                " odd_max_strict, phase) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (channel_id, ev_min, int(ev_min_strict), ev_max, int(ev_max_strict),
                 odd_min, int(odd_min_strict), odd_max, int(odd_max_strict), phase),
            )
            return int(cur.lastrowid)

    def add_rule_value(self, rule_id: int, dimension: str, valeur: str,
                       *, inclut: bool = True) -> None:
        """Une valeur pour une dimension. Rejouable : la meme valeur deux fois
        ne cree pas de doublon, mais met a jour son sens (inclut/exclut).

        La dimension est validee ICI, a l'ecriture. Une ligne invalide ecrite
        en base ne se manifesterait qu'au chargement, dans le cycle, loin de
        la commande qui l'a produite."""
        if dimension not in ("sport", "book", "market", "league"):
            raise ValueError(
                f"dimension inconnue : {dimension!r} "
                f"(attendu : sport, book, market, league)")
        v = str(valeur).strip().lower()
        if not v:
            raise ValueError("valeur vide")
        with self._conn() as c:
            c.execute(
                "INSERT INTO channel_rule_values(rule_id, dimension, valeur, inclut) "
                "VALUES (?,?,?,?) ON CONFLICT(rule_id, dimension, valeur) "
                "DO UPDATE SET inclut=excluded.inclut",
                (rule_id, dimension, v, int(inclut)),
            )

    def set_channel_active(self, channel_id: int, actif: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE channels SET actif=? WHERE id=?",
                      (int(actif), channel_id))

    def delete_channel(self, channel_id: int) -> None:
        """Supprime un canal ET ses regles.

        ⚠️ Les enfants sont supprimes A LA MAIN : `PRAGMA foreign_keys` n'est
        jamais active dans ce projet, donc aucun ON DELETE CASCADE ne se
        declenche. Sans ces deux DELETE, les regles survivraient au canal et
        seraient reattribuees au prochain canal recevant le meme id."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM channel_rule_values WHERE rule_id IN "
                "(SELECT id FROM channel_rules WHERE channel_id=?)", (channel_id,))
            c.execute("DELETE FROM channel_rules WHERE channel_id=?", (channel_id,))
            c.execute("DELETE FROM channels WHERE id=?", (channel_id,))

    def delete_channel_rule(self, rule_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM channel_rule_values WHERE rule_id=?", (rule_id,))
            c.execute("DELETE FROM channel_rules WHERE id=?", (rule_id,))

    def load_channel_rows(self) -> tuple[list, list, list]:
        """Les trois tables, en TROIS requetes — pas une par canal.

        Ce chargement tournera une fois par cycle et par sport. Un N+1 y
        coûterait un aller-retour SQLite par canal et par regle, sur le
        chemin le plus chaud du daemon."""
        with self._conn() as c:
            canaux = c.execute(
                "SELECT * FROM channels ORDER BY priorite, nom, id").fetchall()
            regles = c.execute(
                "SELECT * FROM channel_rules ORDER BY channel_id, id").fetchall()
            valeurs = c.execute(
                "SELECT * FROM channel_rule_values ORDER BY rule_id, dimension, valeur"
            ).fetchall()
        return list(canaux), list(regles), list(valeurs)

    def find_channel_by_name(self, nom: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM channels WHERE nom=?", (nom,)).fetchone()

    def record_team(self, normalized_name: str, display_name: str) -> None:
        """Persist (or refresh) the mapping from the matcher's space-stripped
        team key to the original human-readable name a scraper just saw.
        UPSERT semantics — the most recent scraper to see the team wins."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO teams(normalized_name, display_name, last_seen_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(normalized_name) DO UPDATE SET "
                "  display_name = excluded.display_name, "
                "  last_seen_at = excluded.last_seen_at",
                (normalized_name, display_name, datetime.utcnow().isoformat()),
            )

    def get_team(self, normalized_name: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM teams WHERE normalized_name=?",
                (normalized_name,),
            ).fetchone()

    def all_teams(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM teams"))

    def latest_pinnacle_quote_before(
        self, event_key: str, market: str, outcome_label: str,
        line: Optional[float], before: datetime,
    ) -> Optional[sqlite3.Row]:
        """Return the most recent Pinnacle quote stored before `before` for
        this exact (event, market, outcome, line) tuple. close-lines uses
        this to capture the closing price from our own historical capture
        instead of asking Pinnacle's live API — by kickoff the live market
        is already gone, the only place the real closing line still exists
        is in our quotes table."""
        with self._conn() as c:
            params: list = [event_key, market, outcome_label, before.isoformat()]
            line_clause = "line IS NULL" if line is None else "line = ?"
            if line is not None:
                # Splice the line value just after outcome_label in the params list.
                params = [event_key, market, outcome_label, line, before.isoformat()]
            sql = (
                f"SELECT * FROM quotes "
                f"WHERE book = 'pinnacle' "
                f"  AND event_key = ? "
                f"  AND market = ? "
                f"  AND outcome_label = ? "
                f"  AND {line_clause} "
                f"  AND fetched_at < ? "
                f"ORDER BY fetched_at DESC "
                f"LIMIT 1"
            )
            return c.execute(sql, params).fetchone()

    def odds_before(
        self, event_key: str, book: Book, before: datetime,
    ) -> dict[tuple[str, str, Optional[float]], float]:
        """Dernière cote de ce book sur cet événement AVANT `before`.

        Sert à distinguer un marché prématch oublié d'un marché repricé en
        direct. La plupart des books belges continuent d'exposer un match une
        fois commencé, sans rien dire de son état : « le book propose encore ce
        match » ne prouve donc aucune erreur. Ce qui la prouve, c'est que le
        PRIX n'a pas bougé depuis le coup d'envoi — un book qui price en direct
        déplace forcément ses cotes.

        Une cote par (marché, issue, ligne), et non le dernier instantané
        complet : un book ne republie pas tout son marché à chaque cycle, et
        prendre le dernier instantané en laisserait passer."""
        with self._conn() as c:
            rows = c.execute(
                # Colonnes nues avec MAX() : SQLite renvoie alors la ligne du
                # maximum de chaque groupe, ce qui évite une sous-requête par
                # issue. L'index idx_quotes_event rend la sélection directe.
                "SELECT market, outcome_label, line, decimal_odd, MAX(fetched_at) "
                "FROM quotes "
                "WHERE event_key = ? AND book = ? AND fetched_at < ? "
                "GROUP BY market, outcome_label, line",
                (event_key, book.value, before.isoformat()),
            ).fetchall()
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    def closing_group(
        self, event_key: str, market: str, line: Optional[float], before: datetime,
        book: str = "pinnacle",
    ) -> list[sqlite3.Row]:
        """Every competing outcome ONE book priced in a market, taken from the
        single most recent capture before kickoff.

        Deviging needs the whole market, not one side of it: the margin can only
        be removed by normalising the competing outcomes against each other. All
        rows come from the same fetched_at so the set is internally consistent —
        mixing two capture times would leave a spurious margin behind.

        `book` est paramétrable parce qu'un pari valorisé contre une référence
        de repli doit être mesuré contre la clôture de CETTE référence. Le
        mesurer contre Pinnacle serait impossible — s'il pricait ce marché, il
        n'y aurait pas eu de repli — et le laisser sans clôture le rendrait
        invisible à toute mesure, ce qui est pire."""
        with self._conn() as c:
            line_clause = "line IS NULL" if line is None else "line = ?"
            head: list = [book, event_key, market]
            tail: list = [] if line is None else [line]
            last = c.execute(
                f"SELECT MAX(fetched_at) FROM quotes "
                f"WHERE book = ? AND event_key = ? AND market = ? "
                f"  AND {line_clause} AND fetched_at < ?",
                (*head, *tail, before.isoformat()),
            ).fetchone()
            if last is None or last[0] is None:
                return []
            return list(c.execute(
                f"SELECT * FROM quotes "
                f"WHERE book = ? AND event_key = ? AND market = ? "
                f"  AND {line_clause} AND fetched_at = ?",
                (*head, *tail, last[0]),
            ))

    def pinnacle_closing_group(
        self, event_key: str, market: str, line: Optional[float], before: datetime,
    ) -> list[sqlite3.Row]:
        """Conservé : tout le code existant et ses tests passent par ce nom."""
        return self.closing_group(event_key, market, line, before, book="pinnacle")

    def books_alert_off(self) -> set[str]:
        """Books dont les alertes sont coupées."""
        with self._conn() as c:
            return {r[0] for r in c.execute("SELECT book FROM book_alerts_off")}

    def toggle_book_alert(self, book: str) -> bool:
        """Bascule un book. Renvoie True si les alertes sont désormais ACTIVES."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM book_alerts_off WHERE book = ?", (book,)
            ).fetchone()
            if row:
                c.execute("DELETE FROM book_alerts_off WHERE book = ?", (book,))
                return True
            c.execute(
                "INSERT INTO book_alerts_off(book, disabled_at) VALUES (?, ?)",
                (book, datetime.now(timezone.utc).isoformat()),
            )
            return False

    def books_seen(self, *, days: float = 7.0) -> list[str]:
        """Books ayant produit une détection récemment.

        Liste dynamique plutôt que codée en dur : un book ajouté apparaît seul
        dans /book, un book retiré en disparaît, et personne n'a à tenir un
        second inventaire à jour."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT book FROM value_bets WHERE detected_at >= ? "
                "ORDER BY book", (since,)
            )]

    def odds_curves(self, *, since: str, min_ev: float = 0.0) -> list[sqlite3.Row]:
        """Trajectoires complètes, jointes à l'identité du pari.

        Ordonné par (pari, instant) : l'appelant peut regrouper en un seul
        passage sans trier, ce qui compte quand la table aura un an de points.
        """
        with self._conn() as c:
            return list(c.execute(
                "SELECT h.value_bet_id, h.book, h.seen_at, h.odd, h.fair_odd, h.ev_pct, "
                "       v.event_key, v.market, v.outcome_label, v.line, "
                "       v.odd_taken, v.ev_pct AS ev_detect, v.detected_at, "
                "       v.book AS detect_book, "
                "       e.sport "
                "FROM odds_history h "
                "JOIN value_bets v ON v.id = h.value_bet_id "
                "LEFT JOIN events e ON e.event_key = v.event_key "
                "WHERE v.detected_at >= ? AND v.ev_pct >= ? "
                "ORDER BY h.value_bet_id, h.book, h.seen_at",
                (since, min_ev),
            ))

    def all_closed_bets(self) -> list[sqlite3.Row]:
        """Bets joined with their closing snapshot, ready for CLV aggregation.
        The events LEFT JOIN carries the sport so clv-report can break CLV down
        per sport (sport is NULL when the event row was never persisted)."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT vb.*, cs.pinnacle_odd AS closing_odd, "
                "cs.fair_odd AS closing_fair_odd, cs.overround AS closing_overround, "
                "cs.snapshot_at AS closed_at, e.sport AS sport, e.league AS league, "
                "pb.dedup_key IS NOT NULL AS played, r.winner AS winner "
                "FROM value_bets vb "
                "JOIN clv_snapshots cs ON cs.value_bet_id = vb.id AND cs.closing = 1 "
                "LEFT JOIN events e ON e.event_key = vb.event_key "
                "LEFT JOIN played_bets pb ON pb.value_bet_id = vb.id "
                "LEFT JOIN results r ON r.event_key = vb.event_key "
                "ORDER BY vb.detected_at DESC"
            ))

    def insert_clv_snapshot(
        self, value_bet_id: int, pinnacle_odd: float, pinnacle_prob: float,
        snapshot_at: datetime, closing: bool = False,
        fair_odd: Optional[float] = None, fair_prob: Optional[float] = None,
        overround: Optional[float] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO clv_snapshots(value_bet_id, snapshot_at, closing, pinnacle_odd, "
                "pinnacle_prob, fair_odd, fair_prob, overround) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (value_bet_id, snapshot_at.isoformat(), 1 if closing else 0, pinnacle_odd,
                 pinnacle_prob, fair_odd, fair_prob, overround),
            )

    # ------------------------------------------------------------ played ----
    def latest_value_bet_for(
        self, event_key: str, market: str, outcome_label: str, line: Optional[float],
    ) -> Optional[sqlite3.Row]:
        """The most recent detection of one selection. A tap on Jouer carries
        only the dedup_key, which is exactly (event, market, outcome, line) —
        this resolves it back to the value_bet row so the click inherits the
        bet's EV, fair line and, later, its closing line."""
        with self._conn() as c:
            line_clause = "line IS NULL" if line is None else "line = ?"
            params: list = [event_key, market, outcome_label]
            if line is not None:
                params.append(line)
            return c.execute(
                f"SELECT * FROM value_bets WHERE event_key = ? AND market = ? "
                f"  AND outcome_label = ? AND {line_clause} "
                f"ORDER BY detected_at DESC LIMIT 1",
                params,
            ).fetchone()

    def record_played_bet(
        self, dedup_key: str, played_at: datetime, stake: float,
        value_bet: Optional[sqlite3.Row] = None, sport: str = "",
        book: str = "", odd_taken: Optional[float] = None,
        ev_pct: Optional[float] = None,
    ) -> None:
        """Log a tap on Jouer.

        Upsert rather than plain insert: the alert-suppression path writes a
        bare (dedup_key, played_at) row first and must keep working on its own,
        so this has to be able to fill in the detail afterwards. Every column
        is COALESCEd, which makes a re-tap harmless — the first click's price
        and stake are what actually happened, and a later one must not rewrite
        them."""
        parts = dedup_key.split("|")
        event_key = parts[0] if parts else ""
        market = parts[1] if len(parts) > 1 else ""
        outcome_label = parts[2] if len(parts) > 2 else ""
        with self._conn() as c:
            c.execute(
                "INSERT INTO played_bets(dedup_key, played_at, value_bet_id, "
                "event_key, sport, book, market, outcome_label, line, odd_taken, "
                "fair_odd, ev_pct, stake) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(dedup_key) DO UPDATE SET "
                "value_bet_id=COALESCE(played_bets.value_bet_id, excluded.value_bet_id), "
                "event_key=COALESCE(played_bets.event_key, excluded.event_key), "
                "sport=COALESCE(NULLIF(played_bets.sport,''), excluded.sport), "
                "book=COALESCE(NULLIF(played_bets.book,''), excluded.book), "
                "market=COALESCE(played_bets.market, excluded.market), "
                "outcome_label=COALESCE(played_bets.outcome_label, excluded.outcome_label), "
                "line=COALESCE(played_bets.line, excluded.line), "
                "odd_taken=COALESCE(played_bets.odd_taken, excluded.odd_taken), "
                "fair_odd=COALESCE(played_bets.fair_odd, excluded.fair_odd), "
                "ev_pct=COALESCE(played_bets.ev_pct, excluded.ev_pct), "
                "stake=COALESCE(played_bets.stake, excluded.stake)",
                (
                    dedup_key, played_at.isoformat(),
                    int(value_bet["id"]) if value_bet is not None else None,
                    value_bet["event_key"] if value_bet is not None else event_key,
                    sport,
                    book or (value_bet["book"] if value_bet is not None else ""),
                    value_bet["market"] if value_bet is not None else market,
                    value_bet["outcome_label"] if value_bet is not None else outcome_label,
                    value_bet["line"] if value_bet is not None else None,
                    odd_taken if odd_taken is not None
                    else (value_bet["odd_taken"] if value_bet is not None else None),
                    value_bet["fair_odd"] if value_bet is not None else None,
                    ev_pct if ev_pct is not None
                    else (value_bet["ev_pct"] if value_bet is not None else None),
                    stake,
                ),
            )

    def closing_snapshots_missing_fair(self) -> list[sqlite3.Row]:
        """Closing snapshots taken before the devig fix, with the bet details
        needed to recompute them. Only those whose Pinnacle quotes survived the
        retention window can be recovered — the rest are gone for good."""
        with self._conn() as c:
            return list(c.execute(
                # reference_book fait partie des « bet details needed » :
                # `_closing_prices` cherche la clôture chez la référence qui a
                # valorisé le pari, et l'omettre ici faisait échouer le
                # backfill sur tout pari de repli.
                "SELECT cs.id AS snapshot_id, vb.id AS value_bet_id, vb.event_key, "
                "vb.market, vb.outcome_label, vb.line, vb.reference_book "
                "FROM clv_snapshots cs JOIN value_bets vb ON vb.id = cs.value_bet_id "
                "WHERE cs.closing = 1 AND cs.fair_odd IS NULL"
            ))

    def oldest_quote_at(self) -> Optional[str]:
        """Horodatage de la plus ancienne cote encore en base.

        La purge coupe à deux jours : toute clôture antérieure n'a plus de
        cotes Pinnacle à déviger, et l'interroger revient à balayer une table
        de 150 M de lignes pour un résultat connu d'avance. Indexé, donc
        instantané."""
        with self._conn() as c:
            row = c.execute("SELECT MIN(fetched_at) FROM quotes").fetchone()
            return row[0] if row else None

    def update_snapshot_fair(
        self, snapshot_id: int, fair_odd: float, fair_prob: float, overround: float,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE clv_snapshots SET fair_odd=?, fair_prob=?, overround=? WHERE id=?",
                (fair_odd, fair_prob, overround, snapshot_id),
            )

    def played_bets_unlinked(self) -> list[sqlite3.Row]:
        """Clicks logged before the tracker existed: a dedup_key and a date,
        nothing else. The key is (event, market, outcome, line), so each one can
        still be resolved back to the value_bet it came from."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT dedup_key, played_at FROM played_bets WHERE value_bet_id IS NULL"
            ))

    def link_played_bet(self, dedup_key: str, value_bet: sqlite3.Row, stake: float) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE played_bets SET value_bet_id=?, event_key=?, book=?, market=?, "
                "outcome_label=?, line=?, odd_taken=?, fair_odd=?, ev_pct=?, "
                "stake=COALESCE(stake, ?) WHERE dedup_key=?",
                (int(value_bet["id"]), value_bet["event_key"], value_bet["book"],
                 value_bet["market"], value_bet["outcome_label"], value_bet["line"],
                 value_bet["odd_taken"], value_bet["fair_odd"], value_bet["ev_pct"],
                 stake, dedup_key),
            )

    def played_bet(self, dedup_key: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM played_bets WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()

    def played_bets_with_clv(self) -> list[sqlite3.Row]:
        """Every played bet with its closing line and result when known. This is
        the population that actually matters — clv-report's headline number has
        always been computed over every detection, most of which were never
        backed."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT pb.*, cs.pinnacle_odd AS closing_odd, cs.fair_odd AS closing_fair_odd, "
                "cs.overround AS closing_overround, e.sport AS event_sport, "
                "e.start_time AS start_time, r.winner AS winner, "
                "r.home_score AS home_score, r.away_score AS away_score, "
                "r.source AS result_source "
                "FROM played_bets pb "
                "LEFT JOIN clv_snapshots cs ON cs.value_bet_id = pb.value_bet_id AND cs.closing = 1 "
                "LEFT JOIN events e ON e.event_key = pb.event_key "
                "LEFT JOIN results r ON r.event_key = pb.event_key "
                "ORDER BY pb.played_at"
            ))

    # ----------------------------------------------------------- results ----
    def record_result(
        self, event_key: str, winner: str, settled_at: datetime,
        home_score: Optional[float] = None, away_score: Optional[float] = None,
        source: str = "manual",
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO results(event_key, winner, home_score, away_score, source, settled_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET "
                "winner=excluded.winner, home_score=excluded.home_score, "
                "away_score=excluded.away_score, source=excluded.source, "
                "settled_at=excluded.settled_at",
                (event_key, winner, home_score, away_score, source, settled_at.isoformat()),
            )

    def events_awaiting_result(
        self, since: datetime, until: datetime,
    ) -> list[sqlite3.Row]:
        """Événements déjà joués sur lesquels on a parié, et sans résultat connu.

        Restreint aux événements portant au moins un value bet : un résultat
        n'a d'utilité que là où il y a quelque chose à noter, et `events`
        contient depuis le §19.7 tout le cadre de référence, pas seulement ce
        qu'on a détecté.

        La population volontairement retenue est celle des DÉTECTIONS, pas des
        paris joués. Le projet mesure depuis juillet toutes les opportunités
        éligibles, jouées ou non, précisément pour supprimer le biais de
        sélection manuelle — noter seulement les paris cliqués rétablirait ce
        biais dans le P&L.

        `until` borne le haut pour ne pas réclamer le résultat d'un match qui
        vient de commencer : il n'existe pas encore, et l'appel serait perdu.
        """
        with self._conn() as c:
            return list(c.execute(
                "SELECT e.event_key, e.sport, e.league, e.home, e.away, e.start_time "
                "FROM events e "
                "WHERE e.start_time >= ? AND e.start_time < ? "
                "  AND EXISTS (SELECT 1 FROM value_bets vb WHERE vb.event_key = e.event_key) "
                "  AND NOT EXISTS (SELECT 1 FROM results r WHERE r.event_key = e.event_key) "
                "ORDER BY e.start_time",
                (since.isoformat(), until.isoformat()),
            ))

    def recent_value_bets(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM value_bets ORDER BY detected_at DESC LIMIT ?", (limit,)
            ))
