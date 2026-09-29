"""Sonde hockey : quels marchés chaque book publie-t-il, et lesquels sont
« prolongation incluse » ? LECTURE SEULE — n'écrit rien en base.

Pourquoi : Pinnacle price le hockey prolongation et tirs au but inclus. Un
marché « temps réglementaire » comparé à sa ligne juste fabrique une EV et une
CLV fictives (voir `src/hockey.py`). La base ne garde pas l'identifiant du
marché d'origine : il faut donc savoir, AVANT de collecter, quel identifiant
de chaque book est le vainqueur et le total prolongation incluse.

Pour chaque book, la sonde interroge son API hockey (les mêmes appels que la
production), puis imprime chaque marché distinct : son identifiant, son nom,
ses issues, et le nombre de matchs qui le portent. La réponse brute est
gardée dans `data/sonde_hockey/<book>.json`.

    .venv/bin/python -m scripts.sonde_hockey                 # tous les books
    .venv/bin/python -m scripts.sonde_hockey --books unibet,vivatbet

Ce qu'on cherche dans la sortie : un vainqueur à DEUX issues (sans nul), et
des totaux dont le nom dit « prol. incl. », « incl. OT », « including
overtime »… Un 1X2 à trois issues est du temps réglementaire.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

SPORT = "hockey"
DOSSIER = Path("data/sonde_hockey")


class Tableau:
    """Marchés distincts d'un book : clé → (nom, issues, nombre de matchs)."""

    def __init__(self):
        self.noms: dict = {}
        self.issues: dict = defaultdict(set)
        self.matchs: dict = defaultdict(set)

    def ajouter(self, cle, nom, issues, match) -> None:
        cle = str(cle)
        if nom and cle not in self.noms:
            self.noms[cle] = str(nom)
        self.issues[cle].update(str(i) for i in issues if i not in (None, ""))
        self.matchs[cle].add(str(match))

    def imprimer(self, titre: str) -> None:
        print(f"\n=== {titre} — {len(self.noms or self.matchs)} marché(s) ===")
        if not self.matchs:
            print("  (aucun marché hockey dans la réponse)")
            return
        for cle in sorted(self.matchs, key=lambda k: -len(self.matchs[k])):
            issues = ", ".join(sorted(self.issues[cle]))[:90]
            print(f"  {cle:>14}  {len(self.matchs[cle]):>4} matchs  "
                  f"{self.noms.get(cle, '')[:48]:48}  [{issues}]")


def _garder(nom: str, brut) -> None:
    DOSSIER.mkdir(parents=True, exist_ok=True)
    (DOSSIER / f"{nom}.json").write_text(
        json.dumps(brut, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ── Un sondeur par plateforme ─────────────────────────────────────────

def pinnacle() -> None:
    from src.scrapers.pinnacle import SPORT_IDS, PinnacleScraper
    with PinnacleScraper() as p:
        brut = p._get(f"/sports/{SPORT_IDS[SPORT]}/markets/straight")
    _garder("pinnacle", brut)
    t = Tableau()
    for m in brut if isinstance(brut, list) else []:
        cle = f"p{m.get('period')}:{m.get('type')}"
        t.ajouter(cle, f"period {m.get('period')} · {m.get('type')}",
                  [x.get("designation") for x in m.get("prices") or []], m.get("matchupId"))
    t.imprimer("Pinnacle (référence) — période:type")


def unibet() -> None:
    from src.scrapers.unibet import UnibetScraper
    with UnibetScraper() as u:
        brut = u.fetch_all_events(SPORT)
    _garder("unibet", brut)
    t = Tableau()
    for e in brut.get("events") or []:
        eid = (e.get("event") or {}).get("id")
        for bo in e.get("betOffers") or []:
            cr = bo.get("criterion") or {}
            typ = (bo.get("betOfferType") or {}).get("id")
            t.ajouter(f"t{typ}/c{cr.get('id')}",
                      cr.get("englishLabel") or cr.get("label"),
                      [o.get("type") for o in bo.get("outcomes") or []], eid)
    t.imprimer("Unibet (Kambi) — betOfferType/criterion")


def _altenar(nom: str, classe) -> None:
    with classe() as s:
        brut = s.fetch_events(SPORT)
    _garder(nom, brut)
    marches = {m.get("id"): m for m in brut.get("markets") or []}
    odds = {o.get("id"): o for o in brut.get("odds") or []}
    t = Tableau()
    for ev in brut.get("events") or []:
        for mid in ev.get("marketIds") or []:
            m = marches.get(mid) or {}
            t.ajouter(f"typeId {m.get('typeId')}", m.get("name"),
                      [(odds.get(o) or {}).get("typeId") for o in m.get("oddIds") or []],
                      ev.get("id"))
    t.imprimer(f"{nom} (Altenar) — typeId [typeId des issues]")


def goldenpalace() -> None:
    from src.scrapers.goldenpalace import GoldenPalaceScraper
    _altenar("goldenpalace", GoldenPalaceScraper)


def starcasino() -> None:
    from src.scrapers.starcasinosport import StarCasinoSportScraper
    _altenar("starcasino", StarCasinoSportScraper)


def ladbrokes() -> None:
    from src.scrapers.ladbrokes import LadbrokesScraper
    with LadbrokesScraper() as lb:
        brut = lb.fetch_all_meetings(SPORT, max_meetings=15)
    _garder("ladbrokes", brut)
    t = Tableau()
    for ev in ((brut.get("result") or {}).get("events") or []):
        code = (ev.get("eventInfo") or {}).get("eventCode")
        for bg in ev.get("betGroupList") or []:
            for og in bg.get("oddGroupList") or []:
                t.ajouter(f"betId {og.get('betId')}",
                          f"{og.get('alternativeDescription') or ''} · {og.get('oddGroupDescription') or ''}",
                          [o.get("boxTitle") for o in og.get("oddList") or []], code)
    t.imprimer("Ladbrokes — betId [boxTitle]")


def meridianbet() -> None:
    from src.scrapers.meridianbet import MeridianScraper
    with MeridianScraper() as mb:
        brut = mb.fetch_all_events(SPORT, max_pages=5)
    _garder("meridianbet", brut)
    t = Tableau()
    for lg in ((brut.get("payload") or {}).get("leagues") or []):
        for ev in lg.get("events") or []:
            eid = (ev.get("header") or {}).get("eventId")
            for pos in ev.get("positions") or []:
                for g in pos.get("groups") or []:
                    cle = g.get("id") or g.get("groupId") or g.get("name")
                    t.ajouter(f"g{cle}", f"{g.get('name') or ''} ou={g.get('overUnder')}",
                              [s.get("name") for s in g.get("selections") or []], eid)
    t.imprimer("MeridianBet — groupe [sélections]")


def betfirst() -> None:
    from src.scrapers.betfirst import BetFirstScraper
    with BetFirstScraper() as bf:
        brut = bf.fetch_all_events(SPORT, days_ahead=3, max_market_count=30, max_pages=5)
    _garder("betfirst", brut)
    data = brut.get("data") or {}
    sel = defaultdict(list)
    for s in data.get("selections") or []:
        sel[str(s.get("marketId"))].append(s.get("name") or s.get("outcomeType"))
    t = Tableau()
    for m in data.get("markets") or []:
        t.ajouter(m.get("marketTemplateId"), m.get("name"), sel[str(m.get("id"))],
                  m.get("eventId"))
    t.imprimer("BetFirst — marketTemplateId [sélections]")


def napoleon() -> None:
    from src.scrapers.napoleon import NapoleonScraper
    with NapoleonScraper() as nap:
        brut = nap.fetch_by_date(SPORT)
    _garder("napoleon", brut)
    t = Tableau()
    for ev in brut.get("data") or []:
        for o in ev.get("odds") or []:
            t.ajouter(f"marketId {o.get('marketId')}", o.get("marketName") or o.get("name"),
                      [o.get("code")], ev.get("eventId"))
    t.imprimer("Napoleon — marketId [codes]")


def vivatbet() -> None:
    from src.scrapers.vivatbet import MAX_COUNT, SECTION_MENU, VivatbetScraper
    with VivatbetScraper() as v:
        menu = v._get("leftMenuSports", {"selectedMs": str(SECTION_MENU)})
        sports = [(sp.get("id"), sp.get("name") or sp.get("nameEng") or "")
                  for section in menu or [] for sp in section.get("sports") or []]
        print("\n=== Vivatbet — sports du menu ===")
        for sid, nom in sports:
            print(f"  {sid:>6}  {nom}")
        hockey = [sid for sid, nom in sports if "hockey" in nom.lower()
                  and not any(x in nom.lower() for x in ("gazon", "field", "roller", "salle"))]
        if not hockey:
            print("  (aucun sport « hockey » trouvé dans le menu)")
            _garder("vivatbet_menu", menu)
            return
        brut = v._get("games1x2", {"cfView": "3", "count": str(MAX_COUNT), "grMode": "4",
                                   "selectedMs": f"{SECTION_MENU}.{hockey[0]}"})
    _garder("vivatbet", {"sport_id": hockey[0], "menu": menu, "games": brut})
    t = Tableau()
    for jeu in brut or []:
        for g in jeu.get("eventGroups") or []:
            issues = [f"type{e.get('type')}" + (f"@{e.get('parameter')}" if e.get("parameter") else "")
                      for col in g.get("events") or [] for e in col or []]
            t.ajouter(f"groupId {g.get('groupId')}", g.get("name") or g.get("groupName"),
                      issues[:6], jeu.get("id"))
        for sous in jeu.get("subGamesForMainGame") or []:
            t.ajouter(f"sous-jeu {sous.get('periodName') or sous.get('gameTypeName') or sous.get('id')}",
                      sous.get("periodName") or sous.get("gameTypeName"), [], jeu.get("id"))
    t.imprimer(f"Vivatbet (sport {hockey[0]}) — groupId [type@ligne]")


SONDES = {
    "pinnacle": pinnacle, "unibet": unibet, "ladbrokes": ladbrokes,
    "goldenpalace": goldenpalace, "starcasino": starcasino,
    "meridianbet": meridianbet, "betfirst": betfirst, "napoleon": napoleon,
    "vivatbet": vivatbet,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--books", default=",".join(SONDES),
                    help=f"Books séparés par des virgules, parmi {', '.join(SONDES)}.")
    a = ap.parse_args()
    for nom in [b.strip() for b in a.books.split(",") if b.strip()]:
        fn = SONDES.get(nom)
        if fn is None:
            print(f"\n=== {nom} : inconnu (choix : {', '.join(SONDES)}) ===")
            continue
        try:
            fn()
        except Exception as e:  # une sonde qui tombe ne doit pas taire les autres
            print(f"\n=== {nom} : ÉCHEC — {type(e).__name__}: {e} ===")
    print(f"\nRéponses brutes : {DOSSIER}/  —  Betano, Circus et MagicBetting passent par le "
          "navigateur : leurs identifiants hockey se relèvent dans le navigateur.")


if __name__ == "__main__":
    main()
