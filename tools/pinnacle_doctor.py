"""Pourquoi Pinnacle est-il sauté ? — analyse du journal du daemon.

    .venv/bin/python tools/pinnacle_doctor.py
    .venv/bin/python tools/pinnacle_doctor.py --hours 6 --sport soccer

Pourquoi cet outil plutôt qu'un grep
------------------------------------
Trois messages différents portent le mot « skipping », et ils n'ont RIEN à voir
entre eux :

  « Pinnacle en échec — skipping »            un appel a échoué (403, 429, 5xx,
                                              timeout…) — le seul vrai problème
  « Pinnacle sans événement — skipping »      Pinnacle a répondu correctement,
                                              avec zéro match. Normal la nuit.
  « Pinnacle skipped: <erreur> »              une exception a traversé
                                              _fetch_all_parallel — souvent un
                                              timeout ou une erreur de parsing,
                                              JAMAIS un 403 (celui-ci est
                                              intercepté avant)

Les confondre envoie chercher un blocage là où il n'y en a pas, et inversement.
Deux diagnostics de cette session sont partis dans le décor pour cette raison.

Les lignes du journal ne portent pas d'horodatage : seul l'en-tête de cycle en
a un. Chaque évènement est donc rattaché au cycle qui le contient, ce qu'un
grep ne peut pas faire. Et le compteur de cycles repart à 1 à chaque
redémarrage : les runs sont séparés, pour ne pas mélanger deux versions du code.
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict


CYCLE_RE = re.compile(r"══ CYCLE (\d+) — (\d\d:\d\d:\d\d) UTC")
DONE_RE = re.compile(r"done in (\d+)s")
# Le sport préfixe la plupart des lignes : « [soccer]   … ».
SPORT_RE = re.compile(r"\[(soccer|tennis|basketball|hockey|volleyball|esports)\]")
HTTP_RE = re.compile(r"Pinnacle (\w+) : HTTP (\d+), pause de (\d+)")
# rich coupe à 80 colonnes quand la sortie n'est pas un terminal : on cherche
# des fragments, jamais la ligne entière.
FAIL_RE = re.compile(r"Pinnacle en échec")
EMPTY_RE = re.compile(r"Pinnacle sans événement")
SKIPPED_RE = re.compile(r"(\w[\w ]*?) skipped: (.*)")
MUTE_RE = re.compile(r"Pinnacle muet")
BACK_RE = re.compile(r"Pinnacle rétabli")
IDLE_RE = re.compile(r"aucun événement depuis (\d+) cycles")
QUOTES_RE = re.compile(r"→\s+(\d+)\s+quotes\s+(\w+)")
VBETS_RE = re.compile(r"value bets: (\d+) total")
# Un seul motif, sur le fragment qui n'apparaît qu'une fois par message.
# La première version cherchait aussi « limite N » : rich coupant à 80
# colonnes, le message se répandait sur deux lignes dont chacune matchait,
# et le compteur doublait. Un diagnostic qui exagère se fait ignorer aussi
# vite qu'un diagnostic muet.
STALE_RE = re.compile(r"ignoré : dump vieux de|ignoré : flux périmé")


class Run:
    def __init__(self, first_ts: str):
        self.first_ts = first_ts
        self.last_ts = first_ts
        self.cycles = 0
        self.durations: list[int] = []
        self.fail: Counter = Counter()        # sport -> cycles « en échec »
        self.empty: Counter = Counter()       # sport -> cycles « sans événement »
        self.http: Counter = Counter()        # (sport, code) -> n
        self.backoffs: Counter = Counter()    # valeur de recul -> n
        self.skipped: Counter = Counter()     # (book, message) -> n
        self.quotes: Counter = Counter()      # book -> cycles ayant sorti des cotes
        self.mutes = 0
        self.recoveries = 0
        self.idle: Counter = Counter()
        self.events: list[tuple[str, str]] = []   # (horodatage, description)
        self.vbets_by_hour: Counter = Counter()   # heure -> détections
        self.cycles_by_hour: Counter = Counter()
        self.stale = 0
        self.sports: set[str] = set()      # sports réellement scannés dans ce run


def parse(path: str) -> list[Run]:
    runs: list[Run] = []
    cur: Run | None = None
    ts = "??:??:??"
    sport = "?"
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = CYCLE_RE.search(line)
            if m:
                n, ts = int(m.group(1)), m.group(2)
                # Compteur à 1 = redémarrage du daemon, donc nouveau run.
                if n == 1 or cur is None:
                    cur = Run(ts)
                    runs.append(cur)
                cur.cycles += 1
                cur.last_ts = ts
                cur.cycles_by_hour[ts[:2]] += 1
                continue
            if cur is None:
                continue
            sm = SPORT_RE.search(line)
            if sm:
                sport = sm.group(1)
                cur.sports.add(sport)
            d = DONE_RE.search(line)
            if d:
                cur.durations.append(int(d.group(1)))
            h = HTTP_RE.search(line)
            if h:
                cur.http[(h.group(1), h.group(2))] += 1
                cur.backoffs[int(h.group(3))] += 1
                cur.events.append((ts, f"HTTP {h.group(2)} sur {h.group(1)}, recul {h.group(3)}s"))
            if FAIL_RE.search(line):
                cur.fail[sport] += 1
            if EMPTY_RE.search(line):
                cur.empty[sport] += 1
            s = SKIPPED_RE.search(line)
            if s:
                book = s.group(1).strip().split()[-1]
                cur.skipped[(book, s.group(2).strip()[:60])] += 1
            q = QUOTES_RE.search(line)
            if q:
                cur.quotes[q.group(2)] += 1
            v = VBETS_RE.search(line)
            if v:
                cur.vbets_by_hour[ts[:2]] += int(v.group(1))
            if STALE_RE.search(line):
                cur.stale += 1
            if MUTE_RE.search(line):
                cur.mutes += 1
                cur.events.append((ts, "🚨 alerte « Pinnacle muet »"))
            if BACK_RE.search(line):
                cur.recoveries += 1
                cur.events.append((ts, "✅ alerte « Pinnacle rétabli »"))
            i = IDLE_RE.search(line)
            if i:
                cur.idle[sport] += 1
                cur.events.append((ts, f"{sport} passe en sondage espacé"))
    return runs


def report(run: Run, idx: int, total: int, sport_filter: str) -> None:
    print("=" * 74)
    print(f"RUN {idx}/{total} — {run.first_ts} → {run.last_ts}   ({run.cycles} cycles)")
    print("=" * 74)

    if run.durations:
        d = sorted(run.durations)
        print(f"cycle : médiane {d[len(d)//2]}s  p90 {d[int(len(d)*.9)]}s  max {d[-1]}s")

    print("\n— Pinnacle sauté, par cause (à ne pas confondre) —")
    sports = sorted(set(run.fail) | set(run.empty))
    if not sports:
        print("  aucun cycle sauté")
    for sp in sports:
        if sport_filter and sp != sport_filter:
            continue
        f, e = run.fail.get(sp, 0), run.empty.get(sp, 0)
        pct = 100 * (f + e) / run.cycles if run.cycles else 0
        print(f"  {sp:<12} échec {f:>5}   sans événement {e:>5}   "
              f"({pct:.0f} % des cycles)")

    print("\n— Codes HTTP —")
    if not run.http:
        print("  aucun refus : le problème n'est PAS un blocage réseau")
    for (sp, code), n in run.http.most_common():
        print(f"  {sp:<12} HTTP {code}  ×{n}")
    if run.backoffs:
        vals = ", ".join(f"{k}s×{v}" for k, v in sorted(run.backoffs.items()))
        print(f"  reculs : {vals}")
        if max(run.backoffs) > 120:
            print("  ⚠️  un recul dépasse 120 s : le plafond n'est pas appliqué "
                  "(ancien code ou variable d'environnement)")

    if run.skipped:
        print("\n— Exceptions traversantes (« <book> skipped: … ») —")
        print("  Ce ne sont JAMAIS des 403 : ceux-là sont interceptés avant.")
        for (book, msg), n in run.skipped.most_common(12):
            print(f"  {book:<14} ×{n:<4} {msg}")

    if run.quotes:
        # Chaque cycle interroge chaque book une fois par sport : le
        # dénominateur est cycles × sports RÉELLEMENT scannés, lu dans le
        # journal et non supposé — SPORT_LIST change, et un dénominateur
        # deviné donnerait des couvertures fausses sans que rien ne le dise.
        slots = run.cycles * max(1, len(run.sports))
        print("\n— Couverture par book —")
        print("  Un book sous 95 % est absent d'une partie des cycles : autant")
        print("  d'occasions jamais comparées, sans la moindre erreur au journal.")
        print(f"  {'book':<16} {'cycles':>8} {'couverture':>12}")
        for book, n in run.quotes.most_common():
            pct = 100 * n / slots if slots else 0
            flag = "" if pct >= 95 else ("  ⚠️" if pct >= 80 else "  ❌")
            print(f"  {book:<16} {n:>8} {pct:>11.1f} %{flag}")
        if run.stale:
            print(f"  ⚠️  {run.stale} rejets pour flux périmé — un onglet navigateur"
                  f" ne pousse plus (Betano/Circus)")

    if run.vbets_by_hour:
        print("\n— Détections par heure —")
        for h in sorted(run.cycles_by_hour):
            n = run.vbets_by_hour.get(h, 0)
            c = run.cycles_by_hour[h]
            bar = "█" * min(40, int(n / max(1, max(run.vbets_by_hour.values())) * 40))
            print(f"  {h}h  {n:>6} détections  ({c:>4} cycles)  {bar}")

    print(f"\n— Alertes — muet ×{run.mutes}, rétabli ×{run.recoveries}")
    if run.events:
        print("\n— Chronologie (30 derniers évènements) —")
        for ts, what in run.events[-30:]:
            print(f"  {ts}  {what}")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", default="valuebet.log")
    p.add_argument("--runs", type=int, default=2,
                   help="Nombre de runs récents à détailler (défaut 2).")
    p.add_argument("--sport", default="",
                   help="Restreindre la ventilation à un sport.")
    args = p.parse_args()

    runs = parse(args.log)
    if not runs:
        print(f"Aucun cycle trouvé dans {args.log}.")
        return
    print(f"{len(runs)} run(s) dans le journal — un run = un démarrage du daemon.\n")
    for i, r in enumerate(runs[-args.runs:], start=len(runs) - min(args.runs, len(runs)) + 1):
        report(r, i, len(runs), args.sport)


if __name__ == "__main__":
    main()
