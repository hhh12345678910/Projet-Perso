#!/usr/bin/env python3
"""CLV **et** ROI dans la même table, par sport et par tranche de cote.

POURQUOI CET OUTIL
------------------
Le §21.17 a trouvé que la CLV et le P&L se contredisent sur les grosses cotes.
Le vérifier demandait jusqu'ici deux commandes, deux populations et deux
fichiers : `clv_split --by cote` d'un côté, `pnl_detections` de l'autre. Les
deux tables ne se superposent que si les bornes sont identiques — d'où
l'import de `BANDES_COTE` plutôt qu'une copie.

⚠️ LES DEUX MESURES NE PORTENT PAS SUR LA MÊME POPULATION, et c'est pour ça
que chaque colonne porte SON effectif :

* la **CLV** exige une clôture capturée (`clv_snapshots.closing = 1`). Un match
  dont la ligne de clôture a été manquée n'en a pas, définitivement — les
  cotes sont purgées à deux jours (§7) ;
* le **ROI** exige un résultat dans `results`, donc une source de scores.

Un `n_clv` très inférieur au `n_regles` (ou l'inverse) n'est pas une anomalie :
c'est ce que ces deux chaînes couvrent, et le voir vaut mieux que de comparer
deux moyennes calculées sur des matchs différents en croyant les opposer.

FILTRE DE BOOKS
---------------
`--books` accepte les noms de la base et l'alias **`kambi`**, qui se déplie en
Unibet + 711 + Bingoal + Scooore — le groupe est lu dans `reference.KAMBI_BOOKS`,
jamais recopié. Le filtre s'applique AVANT la déduplication : « le meilleur
prix parmi les books que je joue vraiment », et non le meilleur prix du marché.

PORTE D'ENVOI (`--porte-envoi`)
-------------------------------
`--premium` rejoue la porte du CANAL. Elle n'est pas la seule : `send_value_bet`
écarte des value bets AVANT tout routage — les marchés de mi-temps, et les
détections prématch à moins de `min_minutes_to_kickoff` du coup d'envoi. Ces
paris existent dans `value_bets`, leur clôture est capturée et leur CLV
mesurée, mais AUCUN message n'est jamais parti. Ils gonflent donc le lot
« alerté et non joué » de paris fantômes. `--porte-envoi` les retire, en
lisant le seuil dans la configuration de production plutôt qu'en le
recopiant (§17.7), et imprime combien de paris DÉJÀ CLIQUÉS le rejeu tue —
son propre taux d'erreur.

AXE DES LIGNES
--------------
`--axe cote` (défaut) découpe par tranche de cote prise. `--axe delai` découpe
par heures entre la détection et le coup d'envoi, et pousse le découpage au
delà des « > 48 h » où le §16.4 s'arrêtait : 48-72, 72-96, 96-120, 120-168,
> 168 h. Sur cet axe la table tous sports confondus est imprimée EN PREMIER,
parce que c'est la seule où le ROI garde un effectif lisible dans les bandes
lointaines.

Usage :
    .venv/bin/python -m scripts.clv_roi_matrix --premium
    .venv/bin/python -m scripts.clv_roi_matrix --premium --axe delai
    .venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be
    .venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be \\
        --out clv_roi.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import statistics as st
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.clv import clv_pct  # noqa: E402
from src.clv import pnl as clv_pnl  # noqa: E402
from src.clv import settle as clv_settle  # noqa: E402
from src.config import load_env_file  # noqa: E402
from src.alerter import TelegramConfig  # noqa: E402
from src.models import MarketType, is_half_time  # noqa: E402
from src.reference import KAMBI_BOOKS  # noqa: E402
from scripts.pnl_detections import BANDES_COTE, porte_de_canal  # noqa: E402
from src.main import _EV_BUCKET_ORDER, _ev_bucket  # noqa: E402

_ALIAS = {"kambi": tuple(b.value for b in KAMBI_BOOKS)}


class _VueFairOdd:
    """La même ligne, vue avec `fair_odd` là où la porte lit `odd_taken`.

    C'est le seul moyen de rejouer la porte EXACTE de production sur une autre
    variable sans dupliquer une seule de ses règles (§17.7) — qu'elle vienne
    des canaux en base ou de `TelegramConfig`, elle passe par `__getitem__`.

    ⚠️ Rappel utile pour lire le résultat : `ev = odd_taken / fair_odd - 1`,
    donc sur un value bet `fair_odd < odd_taken` TOUJOURS. Basculer la bande
    de cotes sur la fair odd décale donc chaque pari vers le BAS : une bande
    1,5–4 sur la fair accepte une cote prise allant jusqu'à 4,8 à 20 % d'EV,
    et rejette les cotes prises entre 1,5 et 1,5×(1+EV). Ce n'est pas un
    élargissement uniforme, c'est un glissement.
    """
    __slots__ = ("_r",)

    def __init__(self, r) -> None:
        self._r = r

    def __getitem__(self, k):
        return self._r["fair_odd"] if k == "odd_taken" else self._r[k]


# Le decoupage fin demande par l'utilisateur : le §16.4 s'arretait a « > 48 h »
# sur la CLV, sans jamais savoir ce qu'il y avait dedans. Les bornes suivent les
# journees de calendrier parce que c'est ainsi que les books ouvrent leurs
# marches, puis s'elargissent quand les effectifs fondent.
BANDES_DELAI = [("0-2 h", 0.0, 2.0), ("2-6 h", 2.0, 6.0), ("6-12 h", 6.0, 12.0),
                ("12-24 h", 12.0, 24.0), ("24-48 h", 24.0, 48.0),
                ("48-72 h", 48.0, 72.0), ("72-96 h", 72.0, 96.0),
                ("96-120 h", 96.0, 120.0), ("120-168 h", 120.0, 168.0),
                ("> 168 h", 168.0, 1e9)]


def _heures(brut) -> "float | None":
    if not brut:
        return None
    try:
        d = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _delai_h(row) -> "float | None":
    """Heures entre la DETECTION et le coup d'envoi.

    ⚠️ `detected_at` ne bouge JAMAIS : `insert_value_bet` ne cree qu'une ligne
    par opportunite et rend l'existante sans rien reecrire quand le daemon la
    redetecte (§14.5). Ce delai est donc celui de la PREMIERE detection, pas
    celui de l'instant ou tu aurais mise. Un pari vu a 60 h puis encore present
    a 3 h compte ici en 48-72 h.

    ⚠️ Un delai negatif est une detection LIVE comparee a une ligne prematch
    morte (§9, un tiers des detections a l'epoque). La porte premium est
    prematch, donc ils sont deja ecartes — mais la bande `< 0` existe pour que
    leur presence eventuelle SE VOIE au lieu d'etre repartie en silence."""
    a, b = _heures(row["detected_at"]), _heures(row["start_time"])
    return None if (a is None or b is None) else (b - a) / 3600.0


def _fenetre_morte_defaut() -> "tuple[int, str]":
    """Les minutes de fenêtre morte, LUES DANS LA PRODUCTION.

    Rend (minutes, provenance). La provenance est imprimée : une valeur par
    défaut prise parce que l'environnement n'a pas de jeton doit SE VOIR
    (§11), sans quoi le rejeu prétendrait reproduire un réglage qu'il n'a
    jamais lu.
    """
    cfg = TelegramConfig.from_env()
    if cfg is not None:
        return cfg.min_minutes_to_kickoff, "lu dans TelegramConfig.from_env()"
    champ = TelegramConfig.__dataclass_fields__["min_minutes_to_kickoff"]
    return champ.default, ("défaut du dataclass — AUCUN jeton Telegram en "
                           "environnement, la variable n'a PAS été lue")


MI_TEMPS, FENETRE_MORTE = "mi-temps", "fenêtre morte"


def _raison_non_alertable(row, minutes: float) -> "str | None":
    """Pourquoi `send_value_bet` aurait tu CETTE ligne, ou `None`.

    Les deux raisons ne disent PAS la même chose et ne doivent pas être
    additionnées en silence. La mi-temps est un choix assumé et permanent
    (§21.8) : ces marchés viennent d'être ouverts, leur CLV est inconnue, et
    leur clôture est rarement capturée. La fenêtre morte, elle, écarte des
    marchés parfaitement ordinaires sur le seul critère de l'heure. Un lot
    écarté à 90 % de mi-temps et un lot écarté à 90 % de fenêtre morte
    appellent des conclusions opposées — d'où la ventilation imprimée.
    """
    try:
        marche = MarketType(row["market"])
    except (ValueError, TypeError):
        marche = None
    if marche is not None and is_half_time(marche):
        return MI_TEMPS
    h = _delai_h(row)
    if h is None:        # sans horaire de coup d'envoi : production n'écarte rien
        return None
    if h < 0:            # LIVE — le garde prématch ne s'y applique pas
        return None
    return None if h * 60.0 >= minutes else FENETRE_MORTE


def _alertable(row, minutes: float) -> bool:
    """Vrai si `send_value_bet` aurait laissé passer CETTE ligne.

    Rejoue les deux suppressions qui tombent AVANT tout routage, et que la
    porte du canal ne peut donc pas voir :

    * la **mi-temps** — aucun canal, ni principal, ni premium, ni critique ;
    * la **fenêtre morte** — un value bet PRÉMATCH dont le coup d'envoi est à
      moins de `min_minutes_to_kickoff`. Une détection LIVE (coup d'envoi
      déjà passé) n'est PAS concernée : le garde de production est explicite
      là-dessus, et l'oublier écarterait des paris que la production envoie.

    ⚠️ CE QUE CE REJEU NE PEUT PAS FAIRE. La production compare le coup
    d'envoi à `now` AU MOMENT DE L'ENVOI ; on ne dispose ici que de
    `detected_at`, l'heure de la PREMIÈRE détection, qui ne bouge jamais
    (§14.5). Les deux coïncident quand l'alerte part au premier cycle qui
    voit le pari — le cas normal, la dédup n'en laissant qu'un — mais un pari
    détecté à 3 h du coup d'envoi et alerté seulement plus tard serait jugé
    alertable ici alors que la production l'a peut-être tu. Le compte de
    fantômes que ce filtre donne est donc une borne BASSE, jamais une borne
    haute : il ne peut pas surestimer le ménage.
    """
    return _raison_non_alertable(row, minutes) is None


def _bande(odd: float) -> str:
    for lab, lo, hi in BANDES_COTE:
        if lo <= odd < hi:
            return lab
    return "?"


def _bande_delai(row) -> str:
    h = _delai_h(row)
    if h is None:
        return "? (sans horaire)"
    if h < 0:
        return "< 0 (LIVE)"
    for lab, lo, hi in BANDES_DELAI:
        if lo <= h < hi:
            return lab
    return "?"


# Les deux bandes hors barème existent pour SE VOIR (§11 : le mode de panne
# du projet est le silence). « < 0 » est une detection live, « ? » une ligne
# sans horaire de coup d'envoi ou sans `detected_at` exploitable.
ORDRE_DELAI = (["< 0 (LIVE)"] + [lab for lab, _lo, _hi in BANDES_DELAI]
               + ["? (sans horaire)"])


BANDES_CLV = [("< 0 %", -1e9, 0.0), ("0-5 %", 0.0, 5.0), ("5-10 %", 5.0, 10.0),
              ("10-20 %", 10.0, 20.0), ("> 20 %", 20.0, 1e9)]
SANS_CLOTURE = "sans clôture"
ORDRE_CLV = [lab for lab, _lo, _hi in BANDES_CLV] + [SANS_CLOTURE]


def _bande_clv(row) -> str:
    """La tranche de CLV d'un pari, ou « sans clôture » s'il n'en a pas.

    ⚠️ CETTE BANDE-LÀ N'EST PAS UN DÉCHET, C'EST LA MOITIÉ DE LA QUESTION.
    Un tiers des paris n'a pas de clôture capturée : leur ROI compte, leur CLV
    est inconnue. Les jeter ferait lire « le ROI par tranche de CLV » sur la
    sous-population dont on a réussi à mesurer la CLV — une sélection, pas un
    échantillon. Elle est donc imprimée avec les autres."""
    v = row["closing_fair_odd"]
    if not v or float(v) <= 0:
        return SANS_CLOTURE
    clv = clv_pct(float(row["odd_taken"]), float(v)) * 100.0
    for lab, lo, hi in BANDES_CLV:
        if lo <= clv < hi:
            return lab
    return SANS_CLOTURE


#: ⚠️ L'HEURE LOCALE, PAS UTC. `notified_at` est stocké en UTC ; afficher ces
#: heures-là dirait « creux à 1 h du matin » pour un creux qui est à 3 h chez
#: le lecteur. Un axe horaire dont on décale les libellés de deux heures est
#: pire qu'absent : il désigne le mauvais moment de la journée, et c'est sur ce
#: moment-là qu'on agirait. `zoneinfo` gère le passage à l'heure d'hiver, ce
#: qu'un décalage fixe ne ferait pas sur une fenêtre qui traverse octobre.
FUSEAU = os.getenv("TZ_RAPPORT", "Europe/Brussels")
SANS_ENVOI = "non notifié"


def _bande_heure(row) -> str:
    """L'heure LOCALE d'envoi de l'alerte Telegram.

    ⚠️ Sur `notified_at`, pas sur `detected_at`. Les deux sont normalement
    séparés de quelques secondes — mais pas toujours : un envoi différé par la
    limitation de débit, ou la file empoisonnée du 04/09 (§26.1), les écarte
    d'autant. C'est l'heure d'ENVOI qui décide de ce que le lecteur peut faire.

    ⚠️ « non notifié » n'est PAS un déchet. Un pari détecté et jamais alerté
    (book en sourdine, mi-temps, canal saturé, ou jointure imparfaite — voir
    l'en-tête de la commande) a une CLV parfaitement mesurable. Le jeter ferait
    lire l'axe sur la seule sous-population qu'on a réussi à rapprocher."""
    brut = row["notified_at"] if "notified_at" in row.keys() else None
    t = _heures(brut)
    if t is None:
        return SANS_ENVOI
    from zoneinfo import ZoneInfo
    return f"{datetime.fromtimestamp(t, ZoneInfo(FUSEAU)).hour:02d} h"


ORDRE_HEURE = [f"{h:02d} h" for h in range(24)] + [SANS_ENVOI]


def _bande_semaine(row) -> str:
    """La semaine ISO de la DÉTECTION, étiquetée par son lundi.

    ⚠️ ÉTIQUETÉE PAR UNE DATE, PAS PAR UN NUMÉRO. « S28 » ne se trie pas d'une
    année sur l'autre et ne dit à personne de quand il parle ; « 2026-07-06 »
    fait les deux. Le numéro ISO suit entre parenthèses, pour ceux qui
    raisonnent en semaines.

    ⚠️ SUR `detected_at`, PAS SUR LE COUP D'ENVOI. C'est la semaine où le prix
    est apparu — donc où le système a travaillé. Un pari détecté le dimanche
    pour un match du mercredi appartient à la semaine du dimanche : c'est la
    seule lecture qui permette de juger une semaine de production."""
    t = _heures(row["detected_at"])
    if t is None:
        return "? (sans date)"
    d = datetime.fromtimestamp(t, timezone.utc).date()
    lundi = d - timedelta(days=d.weekday())
    return f"{lundi.isoformat()} (S{d.isocalendar()[1]:02d})"


def _axe(nom: str):
    """(libellé de colonne, fonction de bande, ordre d'affichage)."""
    if nom == "heure":
        return "heure d'envoi", _bande_heure, ORDRE_HEURE
    if nom == "semaine":
        # Ordre canonique VIDE, et c'est voulu : les semaines présentes
        # dépendent des données. L'appelant complète l'ordre par un tri des
        # libellés observés, et le libellé commence par une date ISO
        # précisément pour que ce tri soit chronologique.
        return "semaine (lundi)", _bande_semaine, []
    if nom == "delai":
        return "délai", _bande_delai, ORDRE_DELAI
    if nom == "ev":
        # `_ev_bucket` vient de `main.py`, celui-là même que `clv-report`
        # utilise : recopier ses bornes ici ferait diverger deux outils qui
        # prétendent découper la même chose (§17.7).
        return ("EV détectée", lambda r: _ev_bucket(float(r["ev_pct"] or 0.0)),
                list(_EV_BUCKET_ORDER))
    if nom == "clv":
        return "CLV réalisée", _bande_clv, ORDRE_CLV
    return ("tranche", lambda r: _bande(float(r["odd_taken"])),
            [lab for lab, _lo, _hi in BANDES_COTE])


def _jour_utc(brut: str, nom: str) -> float:
    """Un `AAAA-MM-JJ` en secondes epoch UTC, ou une erreur qui dit le format.

    ⚠️ UNE DATE MAL ÉCRITE NE DOIT PAS PASSER EN SILENCE. `2026-13-01` ou
    `01/08/2026` lèveraient une ValueError nue quelque part plus loin, ou pire,
    seraient acceptés par un `try/except` complaisant et filtreraient TOUT.
    Une fenêtre vide qu'on croit pleine est le mode de panne du projet."""
    try:
        d = datetime.strptime(brut, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(
            f"{nom} : « {brut} » n'est pas une date AAAA-MM-JJ. "
            f"Exemple : {nom} 2026-08-01") from None
    return d.replace(tzinfo=timezone.utc).timestamp()


def _appliquer_fenetre(rows: list, a) -> tuple:
    """Restreint les lignes à la période demandée, et DIT ce qu'elle contient.

    ⚠️ FENÊTRE APPLIQUÉE AVANT LA DÉDUPLICATION. La dédup garde la meilleure
    cote d'un même pari : filtrer après elle pourrait retenir un exemplaire
    hors fenêtre puis le jeter, alors qu'un exemplaire DANS la fenêtre
    existait — l'opportunité disparaîtrait sans raison.

    ⚠️ Le filtre porte sur `detected_at`, qui ne bouge JAMAIS (§14.5) : une
    opportunité vue il y a dix jours et encore affichée hier est HORS d'une
    fenêtre de sept jours. La fenêtre découpe QUAND LE PRIX EST APPARU.
    """
    depuis = getattr(a, "depuis", None)
    jusqu_a = getattr(a, "jusqu_a", None)
    jours = getattr(a, "jours", 0)
    if jours and (depuis or jusqu_a):
        raise SystemExit(
            "--jours compte depuis MAINTENANT, --depuis/--jusqu-a fixent des "
            "dates.\nLes combiner donnerait une fenêtre dont personne ne peut "
            "dire les bornes : choisir l'un ou l'autre.")
    if not (jours or depuis or jusqu_a):
        return rows, ""

    avant = len(rows)
    if jours:
        lo = datetime.now(timezone.utc).timestamp() - jours * 86400
        hi = float("inf")
        libelle = f"{jours:g} derniers jours"
    else:
        lo = _jour_utc(depuis, "--depuis") if depuis else float("-inf")
        # ⚠️ BORNE HAUTE INCLUSIVE. « --jusqu-a 2026-09-08 » doit contenir le
        # 8 septembre EN ENTIER. Prendre minuit du 8 jetterait silencieusement
        # une journée de détections — et personne ne compte les lignes qu'il
        # ne voit pas.
        hi = (_jour_utc(jusqu_a, "--jusqu-a") + 86400) if jusqu_a else float("inf")
        if lo > hi:
            raise SystemExit(
                f"--depuis {depuis} est APRÈS --jusqu-a {jusqu_a} : la fenêtre "
                f"est vide par construction.")
        libelle = " ".join(filter(None, [
            f"du {depuis}" if depuis else "depuis le début",
            f"au {jusqu_a} inclus" if jusqu_a else "à aujourd'hui"]))

    gardees = []
    sans_date = 0
    for r in rows:
        t = _heures(r["detected_at"])
        if t is None:
            # ⚠️ COMPTÉES, PAS JETÉES EN SILENCE. Une ligne sans `detected_at`
            # exploitable ne peut appartenir à aucune fenêtre ; le dire évite
            # de chercher plus tard pourquoi les totaux ne se recollent pas.
            sans_date += 1
            continue
        if lo <= t < hi:
            gardees.append(r)
    rows = gardees
    if not rows:
        raise SystemExit(
            f"Aucune détection sur la période ({libelle}), sur {avant} au "
            f"total.")

    fenetre = (f"Fenêtre : {libelle} — {len(rows)} lignes sur {avant} "
               f"({100 * len(rows) / avant:.0f} %)")
    if sans_date:
        fenetre += f"\n   {sans_date} ligne(s) sans date de détection exploitable, écartées."

    # ⚠️ UNE FENÊTRE COURTE EST PLEINE DE MATCHS PAS ENCORE JOUÉS.
    # La CLV exige une clôture (capturée après le coup d'envoi) et le ROI un
    # résultat : un pari détecté avant-hier pour un match de dimanche n'a ni
    # l'une ni l'autre. Ils ne manquent pas, ils n'existent PAS ENCORE — et
    # comme les paris à long délai sont mécaniquement plus souvent à venir, ils
    # disparaissent des colonnes CLV et ROI en proportion de leur délai. Les
    # colonnes `opp` et `n_clv`/`réglés` ne décrivent alors plus la même
    # population du tout.
    maintenant = datetime.now(timezone.utc).timestamp()
    n_avenir = sum(1 for r in rows
                   if (_heures(r["start_time"]) or 0.0) > maintenant)
    if n_avenir:
        fenetre += (
            f"\n⚠️ {n_avenir} lignes ({100 * n_avenir / len(rows):.0f} %) "
            f"portent sur des matchs PAS ENCORE JOUÉS : ni CLV ni\n"
            f"   résultat, et d'autant plus souvent que le délai est long. "
            f"Les colonnes CLV et ROI\n   d'une fenêtre courte décrivent "
            f"donc les matchs DÉJÀ joués, pas la fenêtre entière.")
    return rows, fenetre


def _lister(opp: list, stake: float, bande_de) -> None:
    """Chaque opportunité, NOMMÉE, groupée par bande de l'axe courant.

    ⚠️ POURQUOI CETTE SORTIE EXISTE. Une moyenne ne se vérifie pas. Un ROI de
    +12 % sur 2 574 paris peut venir d'un flux sain ou de trois coups de chance
    sur des cotes à 8,00 — et rien dans la table ne les distingue. La liste
    nommée est la seule sortie de ce projet où l'on peut reconnaître un match,
    se rappeler l'avoir vu passer, et vérifier que le résultat enregistré est
    bien celui qu'on a vu.

    ⚠️ ELLE LISTE LES OPPORTUNITÉS DÉDUPLIQUÉES, donc exactement les lignes qui
    ont produit les tableaux au-dessus — pas les détections brutes. Les deux
    diffèrent d'un facteur dix, et lister les brutes ferait des totaux qui ne
    recollent pas avec ce qui précède.
    """
    par_bande: dict = defaultdict(list)
    for r in opp:
        par_bande[bande_de(r)].append(r)

    print("\n\n══ LES PARIS, UN PAR UN ══")
    print("Statut : ✅ gagné · ❌ perdu · ➖ annulé · ⏳ pas encore réglé")
    for bande in sorted(par_bande):
        lot = par_bande[bande]
        # Le plus récent d'abord : c'est celui dont on se souvient.
        lot.sort(key=lambda r: str(r["detected_at"] or ""), reverse=True)
        gains = _gains(lot, stake)
        mise = stake * len(gains)
        entete = f"── {bande} — {len(lot)} paris"
        if gains:
            entete += (f", {len(gains)} réglés, ROI "
                       f"{100 * sum(gains) / mise:+.2f} %, "
                       f"P&L {sum(gains):+.0f} €")
        else:
            entete += ", aucun réglé"
        print(f"\n{entete}")
        for r in lot:
            statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                                r["winner"], r["home_score"], r["away_score"])
            pnl = clv_pnl(statut, float(r["odd_taken"]), stake)
            marque = {"won": "✅", "lost": "❌"}.get(
                statut, "⏳" if pnl is None else "➖")
            cl = r["closing_fair_odd"]
            # ⚠️ « — » ET PAS 0,00 %. Une CLV sans clôture est INCONNUE. Écrire
            # zéro la ferait entrer dans les moyennes de l'œil du lecteur.
            clv = (f"{clv_pct(float(r['odd_taken']), float(cl)) * 100:+6.2f}%"
                   if cl and float(cl) > 0 else "     —")
            pari = f"{r['outcome_label']}"
            if r["line"] is not None:
                pari += f" {r['line']:g}"
            match = f"{r['home'] or '?'} - {r['away'] or '?'}"
            print(f"  {marque} {(r['start_time'] or '')[:10]} "
                  f"{match[:34]:<34} {str(r['market'])[:10]:<10} "
                  f"{pari[:14]:<14} @{float(r['odd_taken']):5.2f} "
                  f"{(r['book'] or '')[:12]:<12} "
                  f"EV{float(r['ev_pct'] or 0):+6.2f}% CLV{clv} "
                  + (f"{pnl:+7.2f} €" if pnl is not None else "       —"))


def _books_demandes(brut: str | None) -> set[str] | None:
    if not brut:
        return None
    out: set[str] = set()
    for morceau in (m.strip().lower() for m in brut.split(",")):
        if not morceau:
            continue
        out.update(_ALIAS.get(morceau, (morceau,)))
    return out or None


def _gains(rows: list, stake: float) -> list:
    """Le P&L de chaque pari notable du lot, un par élément.

    Extrait pour que le t de la différence entre deux lots disjoints puisse
    être calculé : `_cellule` n'agrège que des moyennes, et la variance de
    l'écart demande les gains individuels."""
    out = []
    for r in rows:
        statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                            r["winner"], r["home_score"], r["away_score"])
        p = clv_pnl(statut, float(r["odd_taken"]), stake)
        if p is not None:
            out.append(p)
    return out


def _cellule(rows: list, stake: float) -> dict:
    """Les deux mesures d'un groupe, chacune avec SON effectif."""
    matchs = {(r["home"], r["away"], (r["start_time"] or "")[:10]) for r in rows}

    clvs = [clv_pct(float(r["odd_taken"]), float(r["closing_fair_odd"])) * 100.0
            for r in rows
            if r["closing_fair_odd"] and float(r["closing_fair_odd"]) > 0]

    gains, gagnes, perdus, nuls = [], 0, 0, 0
    for r in rows:
        statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                            r["winner"], r["home_score"], r["away_score"])
        p = clv_pnl(statut, float(r["odd_taken"]), stake)
        if p is None:
            continue
        gains.append(p)
        if statut == "won":
            gagnes += 1
        elif statut == "lost":
            perdus += 1
        else:
            nuls += 1

    mise = stake * len(gains)
    ecart = st.stdev(gains) if len(gains) > 1 else 0.0
    # La CLV avait son effectif mais PAS sa precision. C'est pourtant elle qui
    # decide : elle est ~8 fois moins bruitee par pari que le P&L, donc c'est
    # le seul des deux instruments qui separe deux bandes a cet effectif.
    ecart_clv = st.stdev(clvs) if len(clvs) > 1 else 0.0
    return {
        "n_opportunites": len(rows),
        "n_matchs": len(matchs),
        "n_joues": sum(1 for r in rows if r["played"]),
        "n_clv": len(clvs),
        "clv_moy_pct": round(st.mean(clvs), 2) if clvs else None,
        "clv_positives_pct": (round(100.0 * sum(1 for x in clvs if x > 0) / len(clvs), 1)
                              if clvs else None),
        "n_regles": len(gains),
        "gagnes": gagnes,
        "perdus": perdus,
        "annules": nuls,
        "roi_pct": round(100.0 * sum(gains) / mise, 2) if mise else None,
        "pnl_eur": round(sum(gains), 2) if gains else None,
        "sigma_roi": (round(sum(gains) / (ecart * len(gains) ** 0.5), 1)
                      if ecart > 0 and gains else None),
        "sigma_clv": (round(st.mean(clvs) * len(clvs) ** 0.5 / ecart_clv, 1)
                      if ecart_clv > 0 and clvs else None),
    }


def _vecteurs(rows: list, stake: float):
    """(les CLV en %, les P&L en €) du lot — chacune avec SON effectif.

    Les moyennes de `_cellule` ne suffisent pas pour tester deux lots l'un
    contre l'autre : il faut les observations."""
    clvs = [clv_pct(float(r["odd_taken"]), float(r["closing_fair_odd"])) * 100.0
            for r in rows
            if r["closing_fair_odd"] and float(r["closing_fair_odd"]) > 0]
    return clvs, _gains(rows, stake)


def _welch(a: list, b: list):
    """(écart des moyennes, t de Welch) — variances inégales, effectifs inégaux.

    Welch et non Student : les deux lots n'ont ni la même taille ni la même
    dispersion, et le lot « le reste » est toujours le plus gros."""
    if len(a) < 2 or len(b) < 2:
        return None, None
    d = st.mean(a) - st.mean(b)
    v = st.variance(a) / len(a) + st.variance(b) / len(b)
    return d, (d / v ** 0.5 if v > 0 else None)


def _bloc_contre_le_reste(opp: list, bande_de, ordre: list, stake: float,
                          col_axe: str) -> None:
    """Chaque bande contre TOUT LE RESTE, corrigé du nombre de comparaisons.

    ⚠️ Pourquoi ce bloc existe : lue seule, la table invite à comparer une
    cellule à la ligne TOTAL. Ce test-là est faux deux fois — le TOTAL
    CONTIENT la bande (les deux échantillons se chevauchent, donc l'écart-type
    de l'écart est sous-estimé), et on le refait dix fois de suite sans jamais
    corriger le seuil. À dix comparaisons, un |t| de 2,3 arrive par pur hasard
    sous une vérité parfaitement plate.

    ⚠️ Le test est fait TOUS SPORTS CONFONDUS, donc il ne sépare pas l'effet du
    délai de celui de la composition : au-delà de 48 h la population est
    quasi exclusivement du soccer. La dernière colonne imprime la part du sport
    dominant de chaque bande pour que ce mélange se VOIE."""
    presentes = [lab for lab in ordre if any(bande_de(r) == lab for r in opp)]
    if len(presentes) < 2:
        return
    seuil = st.NormalDist().inv_cdf(1 - 0.025 / len(presentes))

    print(f"\nCHAQUE BANDE CONTRE TOUT LE RESTE — le test qui répond à "
          f"« où suis-je le moins bon »")
    print(f"{len(presentes)} bandes testées, donc seuil de Bonferroni "
          f"|t| ≥ {seuil:.2f} pour 5 % d'erreur sur TOUT le tableau.")
    # 9 et non 8 : « +10.13 pt » fait 9 caracteres et decalait toute la ligne.
    ent = (f"{col_axe:16} {'n_clv':>5} {'Δ CLV':>9} {'t':>6}   "
           f"{'réglés':>6} {'Δ ROI':>9} {'t':>6}   {'sport dominant':>22}")
    print(ent)
    print("-" * len(ent))
    retenues = []
    for lab in presentes:
        dedans = [r for r in opp if bande_de(r) == lab]
        dehors = [r for r in opp if bande_de(r) != lab]
        c_in, g_in = _vecteurs(dedans, stake)
        c_out, g_out = _vecteurs(dehors, stake)
        dc, tc = _welch(c_in, c_out)
        dg, tg = _welch(g_in, g_out)
        # Le P&L de Welch est en euros par pari : le ramener en points de ROI,
        # sinon la colonne ne se compare pas à celle de la CLV.
        dr = None if dg is None else dg / stake * 100.0
        comptes: dict[str, int] = defaultdict(int)
        for r in dedans:
            comptes[(r["sport"] or "?")] += 1
        dom, n_dom = max(comptes.items(), key=lambda kv: kv[1])
        f = lambda v, u="": "—" if v is None else f"{v:+.2f}{u}"  # noqa: E731
        ft = lambda v: "—" if v is None else f"{v:+.2f}"  # noqa: E731
        marque = ""
        if tc is not None and abs(tc) >= seuil:
            marque += " CLV✔"
        if tg is not None and abs(tg) >= seuil:
            marque += " ROI✔"
        print(f"{lab:16} {len(c_in):5} {f(dc, ' pt'):>9} {ft(tc):>6}   "
              f"{len(g_in):6} {f(dr, ' pt'):>9} {ft(tg):>6}   "
              f"{dom[:14]:>14} {100.0 * n_dom / len(dedans):5.0f} %{marque}")
        retenues.append((lab, tc, tg))

    survivants = [(l, tc, tg) for l, tc, tg in retenues
                  if (tc is not None and abs(tc) >= seuil)
                  or (tg is not None and abs(tg) >= seuil)]
    print(f"\n✔ = franchit le seuil de Bonferroni. "
          f"{len(survivants)} bande(s) sur {len(presentes)} le franchissent"
          + (" : " + ", ".join(l for l, _, _ in survivants) if survivants
             else " — aucune."))
    if not survivants:
        print("   Aucune bande ne se distingue du reste une fois le nombre de "
              "comparaisons pris en\n   compte. Ce n'est pas « les bandes sont "
              "égales » : c'est « à cet effectif, ce\n   tableau ne peut pas "
              "les séparer ».")
    print("\n⚠️ Ce test est TOUS SPORTS CONFONDUS : un écart peut être un écart "
          "de composition\n   plutôt que de délai. La colonne « sport dominant "
          "» dit à quel point la bande est\n   homogène — une bande à 99 % "
          "soccer comparée à un reste mixte compare aussi\n   deux sports.")


def preparer(a):
    """Tout ce qui précède l'affichage : la porte, les books, les lignes, la
    fenêtre, et la fonction de sélection/déduplication.

    ⚠️ EXTRAIT POUR ÊTRE PARTAGÉ, PAS POUR FAIRE JOLI. Le rapport HTML doit
    montrer EXACTEMENT les chiffres que cette commande imprime. Recopier chez
    lui la requête SQL, la porte du canal et la clé de déduplication ferait
    deux outils qui prétendent mesurer la même chose et divergeraient au
    premier changement — c'est le §17.7, et c'est déjà arrivé deux fois dans
    ce projet.

    Rend (porte, porte_desc, books, rows, fenetre, selectionner).

    `selectionner.rejets` porte, après chaque appel, ce que le rejeu de la
    porte d'envoi a écarté (ou `None` s'il n'a pas été demandé). C'est un
    attribut de fonction plutôt qu'un septième élément du tuple pour ne pas
    casser `rapport_clv_roi`, qui dépaquette ce retour.
    """
    porte = None
    porte_desc = "aucune — toutes les détections"
    if a.premium:
        porte, porte_desc = porte_de_canal(a.db, a.canal)
        if a.porte_sur == "fair" and not a.comparer:
            brute = porte
            porte = lambda r: brute(_VueFairOdd(r))  # noqa: E731
            porte_desc += "  ·  bande de cotes évaluée sur la FAIR ODD"

    books = _books_demandes(a.books)

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("""
        SELECT vb.id, vb.event_key, vb.book, vb.market, vb.outcome_label,
               vb.line, vb.odd_taken, vb.fair_odd, vb.ev_pct, vb.detected_at,
               e.sport AS sport, e.league AS league,
               e.home AS home, e.away AS away, e.start_time AS start_time,
               cs.fair_odd AS closing_fair_odd,
               r.winner, r.home_score, r.away_score,
               (pb.value_bet_id IS NOT NULL) AS played,
               nv.notified_at AS notified_at
        FROM value_bets vb
        LEFT JOIN clv_snapshots cs
               ON cs.value_bet_id = vb.id AND cs.closing = 1
        LEFT JOIN events e   ON e.event_key = vb.event_key
        LEFT JOIN results r  ON r.event_key = vb.event_key
        LEFT JOIN played_bets pb ON pb.value_bet_id = vb.id
        LEFT JOIN (
            SELECT event_key, book, market, outcome_label, line,
                   MIN(notified_at) AS notified_at
            FROM notified_value_bets
            GROUP BY event_key, book, market, outcome_label, line
        ) nv ON nv.event_key = vb.event_key AND nv.book = vb.book
            AND nv.market = vb.market AND nv.outcome_label = vb.outcome_label
            AND (nv.line IS vb.line)
    """))
    if not rows:
        raise SystemExit("Aucune détection en base.")

    # ⚠️ FENÊTRE APPLIQUÉE AVANT LA DÉDUPLICATION. La dédup garde la meilleure
    # cote d'un même pari : filtrer après elle pourrait retenir un exemplaire
    # hors fenêtre puis le jeter, alors qu'un exemplaire DANS la fenêtre
    # existait — l'opportunité disparaîtrait sans raison.
    #
    # ⚠️ Le filtre porte sur `detected_at`, qui ne bouge JAMAIS (§14.5) : une
    # opportunité vue il y a dix jours et encore affichée hier est HORS d'une
    # fenêtre de sept jours. La fenêtre découpe QUAND LE PRIX EST APPARU.
    rows, fenetre = _appliquer_fenetre(rows, a)

    # Rejeu facultatif des suppressions d'ENVOI. `None` = pas de rejeu ; le
    # comportement par défaut reste celui de toutes les mesures précédentes,
    # pour qu'activer l'option soit un acte visible et non un changement
    # silencieux de population sous les mêmes chiffres.
    demande = getattr(a, "porte_envoi", None)
    if demande is None:
        minutes, minutes_src = None, None
    elif demande == "auto":
        minutes, minutes_src = _fenetre_morte_defaut()
    else:
        minutes, minutes_src = float(demande), "imposé en ligne de commande"

    def selectionner(predicat):
        """Les opportunités dédupliquées que cette porte laisserait passer.

        La déduplication reste sur la COTE PRISE dans les deux régimes : c'est
        elle qui paie, et changer aussi le critère de « meilleur prix » ferait
        varier deux choses à la fois."""
        gardees = [r for r in rows
                   if (books is None or (r["book"] or "").lower() in books)
                   and (predicat is None or predicat(r))]
        best = {}
        # ⚠️ « JOUÉ » EST UNE PROPRIÉTÉ DE L'OPPORTUNITÉ, PAS DE LA LIGNE.
        #
        # La déduplication garde la MEILLEURE COTE. Un pari cliqué chez Unibet à
        # 2,10 dont Ladbrokes proposait 2,15 est donc représenté par la ligne
        # Ladbrokes — qui, elle, n'est pas marquée jouée. Filtrer sur
        # `r["played"]` APRÈS la dédup classerait cette opportunité dans « non
        # jouée » alors qu'elle a été jouée : le lot « non joué » se remplirait
        # exactement des paris les mieux tarifés, et la comparaison dirait le
        # contraire de la vérité.
        #
        # On agrège donc le drapeau sur TOUT le groupe avant de trancher.
        #
        # ⚠️ `notified_at` A EXACTEMENT LE MÊME PIÈGE, et il a d'abord été
        # manqué. L'alerte part sur UN book ; la dédup garde le book à la
        # meilleure cote. Quand ce n'est pas le même, la jointure du
        # représentant ne trouve aucune notification et l'opportunité tombe en
        # « non notifié » alors qu'elle a bien été alertée. Mesuré le 12/09 :
        # 45 % de rapprochement seulement sur Kambi+Ladbrokes — et le biais
        # n'est pas neutre, il retient les opportunités alertées sur le book
        # qui se trouvait être le mieux tarifé.
        #
        # ⚠️ « ALERTABLE » EST LE TROISIÈME PIÈGE DE LA MÊME FAMILLE, et il
        # fallait s'y attendre après les deux précédents. La fenêtre morte se
        # juge sur `detected_at`, qui diffère d'une LIGNE à l'autre : Unibet
        # peut voir le pari à 6 h du coup d'envoi et Ladbrokes à 4 minutes.
        # L'opportunité est partie en alerte dès qu'UNE ligne a échappé au
        # garde — donc le drapeau s'agrège en OU sur le groupe, exactement
        # comme « joué ». Le juger sur le seul représentant écarterait des
        # paris réellement alertés, et l'écart mesuré serait celui du hasard
        # des horaires de détection.
        joue: dict = {}
        notif: dict = {}
        alertable: dict = {}
        raisons: dict = {}
        for r in gardees:
            cle = ((r["home"] or "").lower(), (r["away"] or "").lower(),
                   (r["start_time"] or "")[:10], r["market"],
                   r["outcome_label"], r["line"])
            joue[cle] = joue.get(cle, False) or bool(r["played"])
            if minutes is not None:
                pourquoi = _raison_non_alertable(r, minutes)
                alertable[cle] = alertable.get(cle, False) or pourquoi is None
                # La raison du GROUPE n'a de sens que si aucune ligne n'est
                # passée. On accumule, on tranchera après la boucle.
                raisons.setdefault(cle, set()).add(pourquoi)
            q = r["notified_at"] if "notified_at" in r.keys() else None
            if q and (notif.get(cle) is None or q < notif[cle]):
                notif[cle] = q          # la PREMIÈRE alerte du groupe
            prev = best.get(cle)
            if prev is None or float(r["odd_taken"]) > float(prev["odd_taken"]):
                best[cle] = r
        # Le représentant porte désormais l'heure d'alerte du GROUPE. Passer en
        # dict plutôt qu'en `sqlite3.Row` : tout le reste du fichier indexe par
        # nom et appelle `.keys()`, les deux marchent à l'identique.
        best = {k: {**dict(v), "notified_at": notif.get(k)}
                for k, v in best.items()}
        # ⚠️ LE REJEU SE FALSIFIE LUI-MÊME, ET C'EST TOUT L'INTÉRÊT.
        #
        # Une opportunité CLIQUÉE sur « Jouer » a nécessairement été alertée :
        # le clic vient du bouton d'un message Telegram. Si le filtre en
        # écarte, ce n'est pas la production qu'il reproduit, c'est autre
        # chose — et le nombre de paris joués qu'il tue mesure exactement son
        # taux d'erreur. On le compte AVANT de filtrer, et `main` l'imprime.
        if minutes is not None:
            morts = [k for k in best if not alertable.get(k)]
            par_raison: dict = {}
            for k in morts:
                # Un groupe dont toutes les lignes sont tues peut l'être pour
                # DEUX raisons à la fois (mi-temps ici, fenêtre morte là).
                # L'étiquette combinée existe pour que ces cas se voient au
                # lieu d'être attribués arbitrairement à l'une des deux.
                lib = " + ".join(sorted(r for r in raisons.get(k, ()) if r))
                par_raison.setdefault(lib or "?", []).append(k)
            selectionner.rejets = {
                "minutes": minutes, "source": minutes_src,
                "n_avant": len(best), "n_ecartes": len(morts),
                "n_ecartes_joues": sum(1 for k in morts if joue.get(k)),
                "n_joues_avant": sum(1 for k in best if joue.get(k)),
                # ⚠️ LA STRATE ÉCARTÉE ELLE-MÊME, et pas seulement son compte.
                # C'est le chiffre qui tranche : si ces paris ont une CLV haute
                # et un ROI mauvais, le mécanisme soupçonné est confirmé ; s'ils
                # ressemblent au reste, l'écart vient d'ailleurs. La déduire en
                # soustrayant deux invocations ne marche PAS — la base bouge
                # entre deux runs (clôtures capturées, résultats arrivés), et
                # la soustraction attribuerait la dérive au filtre.
                "ecartes": [best[k] for k in morts],
                "par_raison": {lib: [best[k] for k in ks]
                               for lib, ks in par_raison.items()},
            }
            best = {k: v for k, v in best.items() if alertable.get(k)}
        else:
            selectionner.rejets = None
        voulu = getattr(a, "joues", "tous")
        if voulu == "oui":
            best = {k: v for k, v in best.items() if joue.get(k)}
        elif voulu == "non":
            best = {k: v for k, v in best.items() if not joue.get(k)}
        return best

    return porte, porte_desc, books, rows, fenetre, selectionner


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true",
                    help="Filtrer par la porte RÉELLE du canal premium.")
    ap.add_argument("--depuis", default=None, metavar="AAAA-MM-JJ",
                    help="Ne garder que les détections À PARTIR de ce jour "
                         "inclus (UTC). Se combine avec --jusqu-a pour une "
                         "période exacte, et s'oppose à --jours qui compte "
                         "depuis maintenant.")
    ap.add_argument("--jusqu-a", default=None, metavar="AAAA-MM-JJ",
                    dest="jusqu_a",
                    help="Ne garder que les détections JUSQU'À ce jour "
                         "INCLUS (UTC) — la journée entière est comprise.")
    ap.add_argument("--jours", type=float, default=0, metavar="N",
                    help="Ne garder que les détections des N derniers jours. "
                         "Le filtre porte sur `detected_at`, qui ne bouge "
                         "jamais (§14.5) : une opportunité vue il y a 10 jours "
                         "et encore affichée hier est HORS d'une fenêtre de 7 "
                         "jours.")
    ap.add_argument("--canal", default=None, metavar="NOM",
                    help="Un autre canal, par son nom exact (implique --premium).")
    ap.add_argument("--books", default=None, metavar="LISTE",
                    help="Books séparés par des virgules. Alias : kambi.")
    ap.add_argument("--stake", type=float, default=25.0,
                    help="Mise notionnelle par pari (défaut 25).")
    ap.add_argument("--out", default=None, metavar="CSV",
                    help="Écrire la table dans un CSV.")
    ap.add_argument("--axe",
                    choices=("cote", "delai", "ev", "clv", "semaine", "heure"),
                    default="cote",
                    help="Axe des lignes : tranche de COTE (défaut), DÉLAI "
                         "avant le coup d'envoi, EV détectée, CLV réalisée, "
                         "ou SEMAINE de détection. Le délai découpe au-delà "
                         "de 48 h, là où le §16.4 s'arrêtait.")
    ap.add_argument("--joues", choices=("tous", "oui", "non"), default="tous",
                    help="Restreindre aux opportunités CLIQUÉES sur « Jouer » "
                         "(oui), à celles seulement alertées (non), ou tout "
                         "(défaut). Le drapeau est agrégé sur l'opportunité "
                         "ENTIÈRE, pas sur la ligne retenue par la dédup.")
    ap.add_argument("--porte-envoi", nargs="?", const="auto", default=None,
                    metavar="MINUTES", dest="porte_envoi",
                    help="Rejouer les suppressions que `send_value_bet` "
                         "applique AVANT tout routage — mi-temps, et fenêtre "
                         "morte avant le coup d'envoi — que la porte du canal "
                         "ne peut pas voir. Sans valeur, le seuil est LU dans "
                         "la configuration de production ; avec une valeur, "
                         "ce nombre de minutes. Sert à savoir ce que le lot "
                         "« non joué » contient de paris qui ne sont JAMAIS "
                         "partis en alerte.")
    ap.add_argument("--lister", action="store_true",
                    help="Après les tableaux, lister chaque opportunité "
                         "NOMMÉE : match, marché, pari, book, cote, EV, CLV, "
                         "résultat, P&L. Une moyenne ne se vérifie pas ; une "
                         "ligne, si.")
    ap.add_argument("--porte-sur", choices=("cote", "fair"), default="cote",
                    dest="porte_sur",
                    help="Variable sur laquelle la bande de COTES du canal "
                         "est évaluée : la cote prise (production) ou la fair "
                         "odd. ANALYSE SEULE — ne change aucun réglage.")
    ap.add_argument("--comparer", action="store_true",
                    help="Rejouer les DEUX portes et afficher leur "
                         "recouvrement. Implique --premium.")
    a = ap.parse_args()
    # Un drapeau ignoré en silence est exactement le mode de panne du projet.
    if a.porte_envoi not in (None, "auto"):
        try:
            if float(a.porte_envoi) < 0:
                raise ValueError
        except ValueError:
            ap.error("--porte-envoi attend un nombre de minutes positif, ou "
                     f"rien du tout pour lire la production : {a.porte_envoi!r}")
    if a.comparer and a.axe != "cote":
        ap.error("--axe n'a pas de sens avec --comparer : la comparaison "
                 "n'affiche que des totaux, sans découpage en bandes.")
    if a.canal or a.comparer:
        a.premium = True
    load_env_file()

    (porte, porte_desc, books, rows, fenetre,
     selectionner) = preparer(a)

    if a.comparer:
        sur_cote = selectionner(porte)
        sur_fair = selectionner(lambda r: porte(_VueFairOdd(r)))

        # ⚠️ Partition sur l'IDENTITÉ DU PARI (`vb.id`), JAMAIS sur la clé de
        # déduplication. `selectionner` rejoue la dédup « meilleure cote prise »
        # sur un vivier différent dans chaque régime : pour une même clé
        # (équipes+jour+marché+pari), le représentant retenu peut être un AUTRE
        # book, à un AUTRE prix. Partitionner sur la clé faisait tomber ces cas
        # dans « gardées par les DEUX » et les sortait des deux lots exclusifs,
        # alors que les deux colonnes de totaux les comptaient avec deux prix,
        # deux CLV et deux P&L différents.
        #
        # Le biais était systématique ET orienté : il touche exactement les
        # sélections dont la cote prise dépasse la bande mais dont la fair odd y
        # retombe — donc le régime fair y promeut la cote la PLUS LONGUE de la
        # même sélection, un pari de variance supérieure, invisible dans le
        # tableau. Détecté par la revue adverse, puis confirmé sur les données
        # réelles du 03/09 : les totaux ne se reconstituaient pas —
        # 7 284 + 1 530 = 8 814 pour un total affiché de 8 855, 41 € manquants.
        par_id_c = {r["id"]: r for r in sur_cote.values()}
        par_id_f = {r["id"]: r for r in sur_fair.values()}
        communs = par_id_c.keys() & par_id_f.keys()

        # Combien de sélections changent de prix retenu d'un régime à l'autre.
        # Tant que ce nombre n'est pas nul, les deux colonnes de totaux ne
        # portent PAS sur les mêmes paris, et il faut le dire.
        bascules = sum(1 for k in set(sur_cote) & set(sur_fair)
                       if sur_cote[k]["id"] != sur_fair[k]["id"])

        print(f"\nCOMPARAISON DES DEUX PORTES — {porte_desc}")
        print(f"Books : {', '.join(sorted(books)) if books else 'tous'}"
              f"   ·   mise notionnelle {a.stake:g} €")
        print("\nLa bande d'EV et toutes les autres règles sont IDENTIQUES. "
              "Seule change\nla variable sur laquelle la bande de COTES est "
              "évaluée.\n")

        entete = (f"{'':34}{'porte COTE PRISE':>18}{'porte FAIR ODD':>18}")
        print(entete)
        print("-" * len(entete))
        ca, fa = _cellule(list(sur_cote.values()), a.stake), \
            _cellule(list(sur_fair.values()), a.stake)
        for lib, cle, suf, dec in (
                ("opportunités", "n_opportunites", "", 0),
                ("matchs distincts", "n_matchs", "", 0),
                ("paris valorisés en CLV", "n_clv", "", 0),
                ("CLV moyenne", "clv_moy_pct", " %", 2),
                ("CLV positives", "clv_positives_pct", " %", 1),
                ("paris réglés", "n_regles", "", 0),
                ("ROI", "roi_pct", " %", 2),
                # ⚠️ Chaque σ teste « cette porte gagne-t-elle » contre zéro,
                # SÉPARÉMENT. Les deux échantillons partagent l'essentiel de
                # leurs paris : ces deux nombres NE SE SOUSTRAIENT PAS, et leur
                # écart ne porte aucune significativité. Le seul t qui réponde
                # à la question est celui de la différence, imprimé plus bas.
                ("σ vs 0 (chaque porte seule)", "sigma_roi", "", 1),
                ("P&L notionnel", "pnl_eur", " €", 0)):
            # Un signe n'a de sens que sur une grandeur qui peut être négative.
            # « CLV positives : +100,0 % » se lirait comme une variation.
            signe = not (cle.startswith("n_") or cle == "clv_positives_pct")
            f = (lambda v, s=signe: "—" if v is None else
                 (f"{v:+.{dec}f}{suf}" if s
                  else f"{v:,.{dec}f}{suf}".replace(",", " ")))
            print(f"{lib:34}{f(ca[cle]):>18}{f(fa[cle]):>18}")

        print("\nRECOUVREMENT — ce que chaque porte prend SEULE")
        print("-" * len(entete))
        lot_c = [par_id_c[i] for i in par_id_c.keys() - communs]
        lot_f = [par_id_f[i] for i in par_id_f.keys() - communs]
        blocs = [("gardés par les DEUX", [par_id_c[i] for i in communs]),
                 ("SEULEMENT par la cote prise", lot_c),
                 ("SEULEMENT par la fair odd", lot_f)]
        for lib, sous in blocs:
            c = _cellule(sous, a.stake)
            roi = "—" if c["roi_pct"] is None else f"{c['roi_pct']:+.2f} %"
            clv = "—" if c["clv_moy_pct"] is None else f"{c['clv_moy_pct']:+.2f} %"
            pnl = "—" if c["pnl_eur"] is None else f"{c['pnl_eur']:+.0f} €"
            # Même convention que `pnl_detections.report` : une ligne sous
            # seuil est marquée. C'est ici qu'elle manquait le plus — le lot
            # exclusif est par construction le plus petit du tableau, et c'est
            # celui sur lequel la decision repose.
            flag = " ⚠️" if c["n_regles"] < 30 else ""
            print(f"  {lib:30} n={c['n_opportunites']:5}  "
                  f"réglés={c['n_regles']:5}  CLV {clv:>9}  ROI {roi:>9}  "
                  f"P&L {pnl:>9}{flag}")

        # Le t de la DIFFÉRENCE, la seule quantité qui réponde à la question.
        # Les échantillons sont APPARIÉS : la part commune s'annule exactement,
        # donc la variance de l'écart ne depend QUE des deux lots disjoints.
        gc, gf = _gains(lot_c, a.stake), _gains(lot_f, a.stake)
        var = ((st.variance(gc) * len(gc) if len(gc) > 1 else 0.0)
               + (st.variance(gf) * len(gf) if len(gf) > 1 else 0.0))
        ecart = sum(gf) - sum(gc)
        t = ecart / var ** 0.5 if var > 0 else None
        print(f"\n  ÉCART NET de la bascule : {ecart:+.0f} € "
              f"({'t = %+.2f' % t if t is not None else 't incalculable'})"
              f"   — sous |t| = 2, c'est du bruit.")
        if bascules:
            print(f"  ⚠️ {bascules} sélection(s) changent de PRIX RETENU d'un "
                  f"régime à l'autre : sur\n     celles-là, les deux colonnes "
                  f"de totaux ne comparent pas le même pari.")

        print("\n⚠️ Ce sont les DEUX dernières lignes du recouvrement qui "
              "décident, pas les totaux.\n   La partition porte sur l'identité "
              "du pari, donc les totaux se reconstituent\n   exactement : "
              "commun + lot exclusif = total, de chaque côté.")
        print("\n⚠️ Sur un value bet, `fair_odd < odd_taken` toujours "
              "(ev = odd/fair − 1). La bascule\n   n'élargit donc pas la bande, "
              "elle la fait GLISSER vers le haut des cotes prises :\n   à 20 % "
              "d'EV, une bande 1,5–4 sur la fair accepte jusqu'à 4,8 de cote "
              "prise et\n   rejette tout ce qui est pris sous 1,8.")
        print("\nAnalyse seule — aucun réglage n'a été lu autrement ni modifié.")
        return 0

    # ⚠️ UNE SEULE DÉDUPLICATION DANS CE FICHIER, ET C'EST `selectionner`.
    #
    # Ce chemin en avait sa PROPRE copie — même filtre de books, même clé
    # §17.8, même « meilleure cote gagne » — recopiée à côté de celle que
    # `--comparer` utilise. Les deux ont divergé à la première évolution : le
    # filtre `--joues`, ajouté dans `selectionner`, n'avait aucun effet ici, et
    # la commande rendait le tableau complet en annonçant une population
    # restreinte. C'est exactement le défaut contre lequel l'en-tête de ce
    # fichier met en garde (§17.7), commis dans le fichier qui l'énonce.
    opp = list(selectionner(porte).values())
    # ⚠️ UNE POPULATION VIDE DOIT LE DIRE. Sans ce garde, la commande imprimait
    # un tableau sans aucune ligne sous un en-tête parfaitement normal — et
    # « aucun pari ne correspond » se lisait comme « aucun pari n'est rentable ».
    if not opp:
        criteres = [f"porte : {porte_desc}"]
        if books:
            criteres.append(f"books : {', '.join(sorted(books))}")
        if a.joues != "tous":
            criteres.append("cliqués sur « Jouer »" if a.joues == "oui"
                            else "alertés et NON cliqués")
        if getattr(selectionner, "rejets", None):
            criteres.append(
                f"porte d'envoi rejouée : {selectionner.rejets['n_ecartes']} "
                f"opportunités écartées (mi-temps / fenêtre morte)")
        raise SystemExit(
            "Aucune opportunité ne passe ces filtres — il n'y a rien à "
            "mesurer.\n  " + "\n  ".join(criteres)
            + f"\n  (sur {len(rows)} lignes dans la fenêtre)")

    print(f"\nCLV ET ROI — porte : {porte_desc}")
    print(f"Books : {', '.join(sorted(books)) if books else 'tous'}")
    if a.joues != "tous":
        print("Population : "
              + ("UNIQUEMENT les paris cliqués sur « Jouer »" if a.joues == "oui"
                 else "UNIQUEMENT les paris alertés et NON cliqués"))
    print(f"Mise notionnelle : {a.stake:g} €")
    rej = getattr(selectionner, "rejets", None)
    if rej:
        print(f"Porte d'ENVOI rejouée : mi-temps écartée, et fenêtre morte de "
              f"{rej['minutes']:g} min\n  ({rej['source']})")
        # ⚠️ Ces deux nombres portent sur la population AVANT le partage
        # joué / non joué : le rejeu s'applique aux deux lots, et donner son
        # compte après le partage laisserait croire qu'il ne touche que celui
        # qu'on regarde.
        print(f"  {rej['n_ecartes']} opportunités écartées sur "
              f"{rej['n_avant']} "
              f"({100 * rej['n_ecartes'] / rej['n_avant']:.1f} %) — elles "
              f"n'auraient jamais atteint Telegram.\n"
              f"  (comptées AVANT le partage joué / non joué)")
        # Le contrôle qui décide si ce rejeu vaut quelque chose. Un pari
        # cliqué est venu d'un message : le filtre ne devrait pas pouvoir en
        # tuer. Ce qu'il en tue est son taux d'erreur, imprimé qu'il soit nul
        # ou non — un contrôle qu'on ne montre que quand il passe n'est pas
        # un contrôle.
        if rej["n_joues_avant"]:
            part = 100 * rej["n_ecartes_joues"] / rej["n_joues_avant"]
            marque = "  ⚠️ le rejeu est trop large" if part > 2 else ""
            print(f"  CONTRÔLE — dont {rej['n_ecartes_joues']} déjà CLIQUÉS "
                  f"sur « Jouer » ({part:.1f} % des "
                  f"{rej['n_joues_avant']} joués) : un pari cliqué a forcément "
                  f"été alerté,\n  donc ce nombre est le taux d'erreur du "
                  f"rejeu, et non un résultat.{marque}")
        if rej["ecartes"]:
            # ⚠️ CE QU'ON VIENT DE JETER, MESURÉ DANS LA MÊME INVOCATION.
            #
            # Un filtre qui ne dit pas ce qu'il retire demande qu'on le croie
            # sur parole. Et le déduire en soustrayant deux commandes ne
            # marche pas : entre deux runs la base gagne des clôtures et des
            # résultats, et la soustraction met cette dérive sur le dos du
            # filtre. Ici les deux lots sortent du même instant.
            print("\n  LA STRATE ÉCARTÉE — ce que le rejeu vient de retirer :")
            entete = f"    {'raison':24}{'opp':>6}{'n CLV':>7}{'CLV':>9}" \
                     f"{'réglés':>8}{'ROI':>9}{'P&L':>9}"
            print(entete)
            print("    " + "-" * (len(entete) - 4))
            blocs = sorted(rej["par_raison"].items(),
                           key=lambda kv: -len(kv[1]))
            for lib, sous in blocs + [("TOTAL écarté", rej["ecartes"])]:
                c = _cellule(sous, a.stake)
                clv = "—" if c["clv_moy_pct"] is None else f"{c['clv_moy_pct']:+.2f}%"
                roi = "—" if c["roi_pct"] is None else f"{c['roi_pct']:+.2f}%"
                pnl = "—" if c["pnl_eur"] is None else f"{c['pnl_eur']:+.0f}€"
                print(f"    {lib:24}{c['n_opportunites']:>6}{c['n_clv']:>7}"
                      f"{clv:>9}{c['n_regles']:>8}{roi:>9}{pnl:>9}")
            # La capture de clôture de la strate est ce qui distingue les deux
            # mécanismes soupçonnés. Une strate SANS clôture n'a pas pu faire
            # dégénérer la CLV : elle n'en a pas.
            n_ec = len(rej["ecartes"])
            n_cl = sum(1 for r in rej["ecartes"] if r["closing_fair_odd"])
            n_g = sum(1 for r in opp if r["closing_fair_odd"])
            print(f"    Clôture capturée pour {n_cl} de ces {n_ec} "
                  f"({100 * n_cl / n_ec:.1f} %), contre {n_g} sur {len(opp)} "
                  f"({100 * n_g / len(opp):.1f} %) dans le lot GARDÉ.")
    if fenetre:
        print(fenetre)
    print(f"{len(opp)} opportunités dédupliquées, sur {len(rows)} lignes\n")
    if a.axe == "heure":
        # ⚠️ LA JOINTURE EST IMPARFAITE, ET IL FAUT LE CHIFFRER.
        # `notified_value_bets` n'a pas de `value_bet_id` : on rapproche sur
        # cinq colonnes dont `event_key`. Or le dédoublonnage de production
        # compare les clés avec un LIKE sur date+équipes, tolérant à une
        # révision d'horaire (§17.8, jusqu'à onze clés au tennis). Une alerte
        # partie sous une clé révisée ne se rapproche donc PAS ici. Taire ce
        # taux ferait lire la bande « non notifié » comme « jamais alerté ».
        n_ok = sum(1 for r in opp if r["notified_at"])
        print(f"Heure d'envoi retrouvée pour {n_ok} opportunités sur "
              f"{len(opp)} ({100 * n_ok / len(opp):.0f} %), fuseau {FUSEAU}.")
        print("⚠️ Le reste tombe en « non notifié » : soit le pari n'a jamais "
              "été alerté (book\n   en sourdine, mi-temps, canal saturé), soit "
              "la clé d'événement a été révisée\n   entre la détection et "
              "l'envoi. Les deux sont indiscernables ici.\n")

    col_axe, bande_de, ordre = _axe(a.axe)

    groupes: dict[tuple, list] = defaultdict(list)
    for r in opp:
        groupes[((r["sport"] or "?"), bande_de(r))].append(r)

    # Une bande absente de l'ordre canonique ne doit pas DISPARAÎTRE : elle
    # s'ajoute en queue. Sans ça, un libellé imprévu retirerait ses paris du
    # tableau sans rien dire, et les lignes ne sommeraient plus au TOTAL.
    ordre = list(ordre) + sorted({k[1] for k in groupes} - set(ordre))

    lignes = []
    # Sur l'axe du délai, les effectifs par sport fondent dans les bandes
    # lointaines : la table tous sports confondus passe DEVANT, parce que
    # c'est la seule où le ROI garde un effectif lisible au-delà de 48 h.
    if a.axe == "delai":
        par_bande: dict[str, list] = defaultdict(list)
        for (_s, lab), sub in groupes.items():
            par_bande[lab].extend(sub)
        for lab in ordre:
            if par_bande.get(lab):
                lignes.append({"sport": "TOUS", "tranche": lab,
                               **_cellule(par_bande[lab], a.stake)})
        lignes.append({"sport": "TOUS", "tranche": "TOTAL",
                       **_cellule(opp, a.stake)})

    for sport in sorted({k[0] for k in groupes}):
        for lab in ordre:
            sub = groupes.get((sport, lab))
            if sub:
                lignes.append({"sport": sport, "tranche": lab, **_cellule(sub, a.stake)})
        tout = [r for r in opp if (r["sport"] or "?") == sport]
        lignes.append({"sport": sport, "tranche": "TOTAL", **_cellule(tout, a.stake)})
    if a.axe != "delai":
        lignes.append({"sport": "TOUS", "tranche": "TOTAL", **_cellule(opp, a.stake)})

    # Les libellés de délai vont jusqu'à « ? (sans horaire) » : une largeur
    # figée à 8 les tronquerait ou décalerait toute la ligne.
    larg = max(len(col_axe), max(len(l["tranche"]) for l in lignes))
    # Deux σ, donc deux noms : un « σ » unique se lisait comme s'il portait sur
    # les deux mesures, alors qu'il ne portait que sur le ROI.
    entete = (f"{'sport':8} {col_axe:{larg}} {'opp':>5} {'matchs':>6} {'joués':>5} "
              f"{'n_clv':>5} {'CLV':>8} {'σCLV':>5} {'CLV+':>6} "
              f"{'réglés':>6} {'G/P/N':>12} {'ROI':>8} {'σROI':>5} {'P&L':>9}")
    print(entete)
    print("-" * len(entete))
    for l in lignes:
        if l["tranche"] == "TOTAL":
            print("-" * len(entete))
        clv = "—" if l["clv_moy_pct"] is None else f"{l['clv_moy_pct']:+.2f}%"
        clv_pos = "—" if l["clv_positives_pct"] is None else f"{l['clv_positives_pct']:.0f}%"
        gpn = f"{l['gagnes']}/{l['perdus']}/{l['annules']}"
        roi = "—" if l["roi_pct"] is None else f"{l['roi_pct']:+.2f}%"
        sig = "—" if l["sigma_roi"] is None else f"{l['sigma_roi']:.1f}"
        sig_c = "—" if l["sigma_clv"] is None else f"{l['sigma_clv']:.1f}"
        pnl = "—" if l["pnl_eur"] is None else f"{l['pnl_eur']:+.0f}€"
        # Un ROI sur 11 paris s'imprime comme un ROI sur 400. Sur l'axe du
        # delai les bandes lointaines fondent : sans marque, la ligne la plus
        # spectaculaire du tableau est aussi la moins fiable, et rien ne le dit.
        maigre = " ⚠️" if 0 < l["n_regles"] < 30 else ""
        print(f"{l['sport'][:8]:8} {l['tranche']:{larg}} "
              f"{l['n_opportunites']:5} {l['n_matchs']:6} {l['n_joues']:5} "
              f"{l['n_clv']:5} {clv:>8} {sig_c:>5} {clv_pos:>6} "
              f"{l['n_regles']:6} {gpn:>12} {roi:>8} {sig:>5} {pnl:>9}{maigre}")

    if any(0 < l["n_regles"] < 30 for l in lignes):
        print("\n⚠️ = moins de 30 paris réglés dans la cellule. À cet effectif, "
              "l'intervalle de\n   confiance du ROI dépasse largement l'écart "
              "qu'on cherche à lire : la ligne est\n   un indice, pas un "
              "résultat.")

    _bloc_contre_le_reste(opp, bande_de, ordre, a.stake, col_axe)

    print("\n⚠️ `n_clv` et `réglés` ne décrivent PAS la même population : la CLV "
          "exige une clôture\n   capturée, le ROI un résultat. Comparer leurs "
          "moyennes suppose de regarder d'abord\n   si les deux effectifs se "
          "ressemblent.")

    if a.axe == "delai":
        print("\n⚠️ LE DÉLAI EST CELUI DE LA PREMIÈRE DÉTECTION, pas de la mise. "
              "`detected_at` ne\n   bouge jamais (§14.5) : une opportunité vue à "
              "60 h et encore affichée à 3 h\n   compte ici en 48-72 h. Ces "
              "bandes mesurent QUAND LE PRIX EST APPARU, ce qui est\n   la "
              "question posée à la CLV, mais elles ne prouvent pas qu'un pari "
              "ait été\n   plaçable pendant toute la bande.")
        print("\n⚠️ Le délai n'est pas indépendant du reste : les marchés "
              "ouverts tôt ne sont pas\n   les mêmes ligues, ni les mêmes "
              "books, ni les mêmes cotes que ceux ouverts à\n   2 h du coup "
              "d'envoi. Un écart de ROI entre deux bandes peut donc être un "
              "écart\n   de composition — croiser avec `--axe cote` avant de "
              "conclure.")

    if a.lister:
        _lister(opp, a.stake, bande_de)

    if a.out:
        champs = ["sport", "tranche", "n_opportunites", "n_matchs", "n_joues",
                  "n_clv", "clv_moy_pct", "sigma_clv", "clv_positives_pct",
                  "n_regles", "gagnes", "perdus", "annules", "roi_pct",
                  "sigma_roi", "pnl_eur"]
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=champs)
            w.writeheader()
            w.writerows(lignes)
        print(f"\n✓ CSV écrit : {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
