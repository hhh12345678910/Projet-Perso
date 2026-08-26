"""Collecteur Unibet LIVE — sondage indépendant de la boucle prématch. §PHASE 5

POURQUOI UN COLLECTEUR SÉPARÉ. Le cycle du daemon dure ~54 s et ne récupère
Unibet qu'une fois par cycle : une cote y a donc 10 à 60 s. Face à AsianOdds,
frais à quelques secondes, l'écart rend toute comparaison douteuse — on
mesurerait surtout le retard de notre propre collecte. Sondé à part, Unibet
descend sous les 5 s et les deux sources deviennent comparables.

CE QUE CE MODULE NE FAIT PAS, ET C'EST VOULU : aucune écriture en base, aucun
calcul d'EV, aucune alerte. Il tient un instantané EN MÉMOIRE et le mesure.
Le moteur viendra le lire dans le même processus (commit 2) ; c'est justement
pour ça qu'il n'a pas besoin de passer par `market_state`.

Rien du prématch n'est touché : `scrapers/unibet.py` est utilisé tel quel,
`parse_listview` n'est pas modifié, et le daemon ignore l'existence de ce
module.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import httpx

from .asianodds_live import candidats_en_cours, evaluer_appariement
from .models import Book, MarketType, OddQuote, Outcome
from .scrapers.unibet import UnibetScraper, parse_listview

#: La vue in-play de Kambi : TOUS les matchs en cours d'un sport en un appel.
#: 92 matchs et 456 cotes en 219 ms, mesuré le 25/08 depuis la VM.
CHEMIN_IN_PLAY = "all/all/all/in-play"

#: 5 s. Mesuré : la réponse tient en 219 ms, donc le coût d'un sondage est
#: marginal — 720 requêtes/heure, une seule par sondage. Mais descendre plus
#: bas n'apporterait RIEN : le prix de référence AsianOdds ne change que
#: toutes les 28 s en médiane, et un book soft bouge plus lentement encore.
#: Sonder à 1 s multiplierait les requêtes par cinq pour observer cinq fois
#: le même prix. L'API est publique et sans authentification : la ménager
#: est une question de correction, pas une contrainte technique.
PERIODE_SEC = 5.0
#: Repli sur refus ou panne du serveur. Jamais d'accélération sous PERIODE_SEC.
REPLI_MAX_SEC = 60.0
#: Au-delà, on abandonne : s'acharner sur un serveur qui refuse n'aide pas.
ECHECS_MAX = 8

#: Seuls marchés retenus. Le HANDICAP est EXCLU : la convention de ligne
#: d'Unibet n'est pas vérifiée face à celle d'AsianOdds, et les apparier sans
#: l'avoir constaté rejouerait les value bets fantômes de `detection.py:242`.
MARCHES = (MarketType.H2H, MarketType.TOTALS)


def _evenements(data: dict) -> dict:
    """`source_event_id` → (home, away), tels qu'Unibet les nomme.

    `parse_listview` construit l'`event_key` puis JETTE les noms. Or le
    rapprochement avec nos `events` se fait sur les noms — l'horaire annoncé
    par un book n'est pas fiable pour un match déjà commencé. On relit donc
    le payload pour eux seuls, en lisant EXACTEMENT les mêmes champs que le
    parseur de production : `homeName`, `awayName`, `id`.
    """
    out = {}
    for entry in data.get("events") or []:
        ev = entry.get("event") or {}
        home, away, sid = ev.get("homeName"), ev.get("awayName"), ev.get("id")
        if home and away and sid is not None:
            out[str(sid)] = (home, away)
    return out


def _compter_betoffers(data: dict) -> int:
    return sum(len(e.get("betOffers") or []) for e in (data.get("events") or []))


@dataclass(frozen=True)
class CycleStats:
    """Ce qu'un sondage a coûté et rapporté. Un compteur par question posée."""
    debut: datetime
    fin: datetime
    duree_ms: float
    matchs: int = 0
    betoffers: int = 0
    quotes: int = 0
    h2h: int = 0
    totals: int = 0
    erreur: "str | None" = None
    #: Le contenu a-t-il VRAIMENT changé depuis le sondage précédent ? La
    #: leçon d'AsianOdds : un message reçu n'est pas un prix qui bouge.
    change: bool = False
    #: COMBIEN de sélections ont bougé. Le booléen ci-dessus ne pouvait pas
    #: arbitrer une cadence : mesuré le 26/08 à 5 s, il saturait à 98 % —
    #: sur 168 sélections vivantes, qu'au moins une bouge est presque
    #: certain, et il aurait saturé tout autant à 30 s. C'est la PART de
    #: sélections modifiées qui dit si sonder plus vite apporte quelque
    #: chose.
    selections_modifiees: int = 0
    selections_suivies: int = 0
    #: Âge de l'instantané au terme du sondage. Vaut ~0 après un succès ; ne
    #: grandit que si un sondage échoue et qu'on garde le précédent.
    fraicheur_sec: float = 0.0
    disparus: int = 0

    def resume(self) -> str:
        e = f"  ERREUR {self.erreur}" if self.erreur else ""
        bouge = (f"{self.selections_modifiees:>3}/{self.selections_suivies:<4}"
                 if self.selections_suivies else "  1er   ")
        return (f"{self.debut:%H:%M:%S}.{self.debut.microsecond // 1000:03d}"
                f" → {self.fin:%H:%M:%S}.{self.fin.microsecond // 1000:03d}"
                f"  {self.duree_ms:6.0f} ms  "
                f"matchs={self.matchs:>3} betOffers={self.betoffers:>4} "
                f"quotes={self.quotes:>4} (h2h={self.h2h:>3} "
                f"totals={self.totals:>3})  bougees {bouge}  "
                f"fraicheur={self.fraicheur_sec:4.1f}s "
                f"disparus={self.disparus:>2}{e}")


@dataclass
class Instantane:
    """L'état courant, EN MÉMOIRE. Remplacé en entier à chaque sondage réussi.

    Le remplacement intégral est le seul comportement sûr : une cote absente
    du dernier sondage n'est plus offerte, et la garder ferait calculer une
    value sur un marché suspendu. Un instantané périmé n'est pas effacé pour
    autant — il porte son âge, et l'appelant décide.
    """
    pris_a: "datetime | None" = None
    #: (source_event_id) → (home, away)
    noms: dict = field(default_factory=dict)
    #: liste d'OddQuote, clés Unibet, non encore rapprochées
    quotes: list = field(default_factory=list)

    def age_sec(self, maintenant: datetime) -> float:
        if self.pris_a is None:
            return float("inf")
        return (maintenant - self.pris_a).total_seconds()


def _signature(quotes: "list[OddQuote]") -> tuple:
    """De quoi dire si le contenu a changé, sans garder les messages."""
    return tuple(sorted(
        (q.source_event_id, q.market.value, q.outcome.label,
         q.outcome.line if q.outcome.line is not None else -1e9,
         round(q.decimal_odd, 4))
        for q in quotes))


class UnibetLive:
    """Sonde la vue in-play et tient l'instantané. Ne persiste rien."""

    def __init__(self, sport: str = "soccer", *, scraper=None,
                 horloge=None) -> None:
        self.sport = sport
        self._scraper = scraper or UnibetScraper()
        self._horloge = horloge or (lambda: datetime.now(timezone.utc))
        self.instantane = Instantane()
        self._signature = None
        self._cotes: dict = {}

    def close(self) -> None:
        fermer = getattr(self._scraper, "close", None)
        if fermer:
            fermer()

    def sonder(self) -> CycleStats:
        """Un sondage. N'élève JAMAIS : l'erreur est une mesure, pas un arrêt."""
        debut = self._horloge()
        t0 = time.perf_counter()
        try:
            data = self._scraper.fetch_listview(self.sport, CHEMIN_IN_PLAY)
        except Exception as e:                                  # noqa: BLE001
            fin = self._horloge()
            # L'instantané précédent est CONSERVÉ, avec son âge. L'effacer
            # ferait disparaître toute la couverture sur un simple hoquet
            # réseau ; le garder sans son âge ferait travailler sur des prix
            # morts. La seule réponse honnête est de le vieillir.
            return CycleStats(
                debut=debut, fin=fin,
                duree_ms=(time.perf_counter() - t0) * 1000,
                erreur=f"{type(e).__name__}: {e}",
                fraicheur_sec=self.instantane.age_sec(fin))

        quotes = [q for q in parse_listview(data) if q.market in MARCHES]
        noms = _evenements(data)
        fin = self._horloge()
        sig = _signature(quotes)
        # Comparaison SELECTION PAR SELECTION, et non du bloc entier. Un
        # marche qui apparait ou disparait n'est pas un prix qui bouge : on
        # ne compte que les selections presentes DES DEUX COTES.
        avant = self._cotes
        cotes = {t[:-1]: t[-1] for t in sig}
        communes = set(avant) & set(cotes)
        bougees = sum(1 for k in communes if avant[k] != cotes[k])
        change = self._signature is not None and sig != self._signature
        self._cotes = cotes
        avant = set(self.instantane.noms)
        self._signature = sig
        self.instantane = Instantane(pris_a=fin, noms=noms, quotes=quotes)
        types = Counter(q.market for q in quotes)
        return CycleStats(
            debut=debut, fin=fin,
            duree_ms=(time.perf_counter() - t0) * 1000,
            matchs=len(data.get("events") or []),
            betoffers=_compter_betoffers(data),
            quotes=len(quotes),
            h2h=types.get(MarketType.H2H, 0),
            totals=types.get(MarketType.TOTALS, 0),
            change=change,
            selections_modifiees=bougees,
            selections_suivies=len(communes),
            fraicheur_sec=0.0,
            disparus=len(avant - set(noms)))


def _permuter_h2h(q: "OddQuote") -> "OddQuote":
    """Notre domicile est leur extérieur : permuter home et away.

    Le NUL ne bouge pas, et les TOTAUX non plus — « plus de 2.5 buts » ne
    dépend pas de qui reçoit. Cette invariance est établie et testée côté
    AsianOdds ; la rejouer ici serait une régression.
    """
    if q.market != MarketType.H2H:
        return q
    autre = {"home": "away", "away": "home"}.get(q.outcome.label)
    if autre is None:
        return q
    return replace(q, outcome=Outcome(label=autre, line=q.outcome.line))


@dataclass
class Appariees:
    quotes: list = field(default_factory=list)
    matchs_vus: int = 0
    matchs_apparies: int = 0
    inversions: int = 0
    sans_event_key: int = 0
    motifs: Counter = field(default_factory=Counter)


def apparier(instantane: Instantane, storage, maintenant: datetime,
             sport: str = "soccer") -> Appariees:
    """Re-clé les cotes Unibet sur NOS `event_key`.

    Réutilise le rapprochement d'`asianodds_live`, éprouvé sur données
    réelles : seuils, garde-fou d'ambiguïté, orientation, doublons de
    `events` et collisions. Importé sans être déplacé — extraire un module
    partagé maintenant ferait courir un risque de régression à du code qu'on
    vient de valider.
    """
    out = Appariees()
    if not instantane.quotes:
        return out
    candidats = candidats_en_cours(storage, maintenant, _SPORT_AO.get(sport, 1))
    par_source: dict = {}
    for q in instantane.quotes:
        par_source.setdefault(q.source_event_id, []).append(q)
    out.matchs_vus = len(par_source)

    for sid, lot in par_source.items():
        noms = instantane.noms.get(sid)
        if not noms:
            out.sans_event_key += 1
            out.motifs["nom absent du payload"] += 1
            continue
        app = evaluer_appariement(noms[0], noms[1], candidats)
        if app.cible is None:
            out.sans_event_key += 1
            out.motifs[app.motif.split(" —")[0].split(" (")[0]] += 1
            continue
        out.matchs_apparies += 1
        for cible in app.toutes_les_cibles:
            inverse = app.inverse_pour(cible)
            if inverse:
                out.inversions += 1
            for q in lot:
                r = _permuter_h2h(q) if inverse else q
                out.quotes.append(replace(r, event_key=cible.event_key,
                                          book_event_key=q.event_key,
                                          from_live_feed=True))
    return out


#: Notre nom de sport → le `sportstype` d'AsianOdds, qu'attend
#: `candidats_en_cours`.
_SPORT_AO = {"soccer": 1, "basketball": 2, "tennis": 3}


def collecter(*, duree_sec: float, sport: str = "soccer",
              periode_sec: float = PERIODE_SEC, scraper=None,
              storage=None, dormir=time.sleep, horloge=None,
              log=print) -> "list[CycleStats]":
    """Sonder pendant `duree_sec`, en rendant la mesure de chaque cycle.

    Le repli sur refus ne descend JAMAIS sous `periode_sec` : accélérer
    quand un serveur refuse est le meilleur moyen de se faire bloquer.
    """
    live = UnibetLive(sport, scraper=scraper, horloge=horloge)
    cycles: list[CycleStats] = []
    fin = time.monotonic() + duree_sec
    attente = periode_sec
    echecs = 0
    try:
        while time.monotonic() < fin:
            c = live.sonder()
            cycles.append(c)
            if storage is not None and not c.erreur:
                a = apparier(live.instantane, storage,
                             live.instantane.pris_a or datetime.now(timezone.utc),
                             sport)
                log(f"[ub] {c.resume()}  apparies={a.matchs_apparies}"
                    f"/{a.matchs_vus} inversions={a.inversions}")
            else:
                log(f"[ub] {c.resume()}")
            if c.erreur:
                echecs += 1
                if echecs >= ECHECS_MAX:
                    log(f"[ub] abandon après {echecs} échecs consécutifs")
                    break
                attente = min(attente * 2, REPLI_MAX_SEC)
            else:
                echecs = 0
                attente = periode_sec
            if time.monotonic() + attente > fin:
                break
            dormir(attente)
    finally:
        live.close()
    return cycles


def resume_global(cycles: "list[CycleStats]") -> str:
    ok = [c for c in cycles if not c.erreur]
    err = [c for c in cycles if c.erreur]
    if not ok:
        return f"{len(cycles)} sondage(s), AUCUN réussi, {len(err)} erreur(s)"
    d = sorted(c.duree_ms for c in ok)
    n = len(d)
    changes = sum(1 for c in ok if c.change)
    suivis = [c for c in ok if c.selections_suivies]
    bougees = sum(c.selections_modifiees for c in suivis)
    total = sum(c.selections_suivies for c in suivis)
    # C'est CE taux qui arbitre la cadence, pas le booleen : mesure le 26/08
    # a 5 s, le booleen saturait a 98 % et aurait sature tout autant a 30 s.
    part = f"{100 * bougees / total:.2f} %" if total else "n/a"
    par_sondage = f"{bougees / len(suivis):.1f}" if suivis else "n/a"
    return (f"{len(cycles)} sondage(s) : {len(ok)} réussis, {len(err)} en erreur\n"
            f"  durée      p50 {d[n // 2]:.0f} ms  p95 "
            f"{d[min(n - 1, int(0.95 * n))]:.0f} ms  max {d[-1]:.0f} ms\n"
            f"  sondages avec au moins un changement : {changes}/{len(ok)}\n"
            f"  SÉLECTIONS bougées : {bougees}/{total} ({part}), "
            f"soit {par_sondage} par sondage\n"
            f"    → c'est ce taux qui dit si sonder plus vite sert : un taux "
            f"élevé\n      signifie que le marché bouge entre deux sondages, "
            f"un taux bas que\n      l'on regarde le même prix plusieurs fois.\n"
            f"  matchs {ok[-1].matchs}  quotes {ok[-1].quotes} "
            f"(h2h {ok[-1].h2h}, totals {ok[-1].totals})")
