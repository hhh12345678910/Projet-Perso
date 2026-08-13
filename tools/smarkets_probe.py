#!/usr/bin/env python3
"""Sonde Smarkets — répond aux questions qui décident du design AVANT de coder.

Le §5 du handover a retiré Smarkets pour une seule raison : un rafraîchissement
prenait ~26 minutes et bloquait le cycle de scan. La cause est identifiée dans
`src/scrapers/smarkets.py` : `fetch_event_markets` fait une requête PAR
événement et `fetch_market_contracts` une requête PAR marché, alors que
`fetch_quotes` groupe déjà ses identifiants par virgule.

Cette sonde ne modifie rien. Elle mesure quatre choses :

  1. Les appels groupés fonctionnent-ils ? `/events/{id,id,...}/markets/` et
     `/markets/{id,id,...}/contracts/`. C'est le levier n°1 : s'il marche, les
     ~1 000 requêtes d'un cycle tombent à ~100.
  2. Quelle taille de lot l'API accepte-t-elle avant de refuser ?
  3. Combien de temps prend un rafraîchissement complet, avant et après ?
  4. Smarkets price-t-il des matchs que Pinnacle ignore ? C'est LA question
     ouverte du §5 — sans gain de couverture, une seconde référence ne sert
     qu'à débruiter la ligne juste, ce qui est un bénéfice bien plus mince.

Usage :
    python3 tools/smarkets_probe.py                 # football, 48 h
    python3 tools/smarkets_probe.py --sport tennis --hours 24
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://api.smarkets.com/v3"
DOMAINS = {"soccer": "football", "tennis": "tennis"}
MAIN_MARKET_TYPES = "WINNER_3_WAY,WINNER_2_WAY,OVER_UNDER"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _get(client: httpx.Client, path: str, params: dict | None = None):
    """Retourne (json, secondes, code). Ne lève pas : un échec est une réponse."""
    t0 = time.monotonic()
    try:
        r = client.get(f"{BASE}{path}", params=params, timeout=25.0)
        dt = time.monotonic() - t0
        if r.status_code != 200:
            return None, dt, r.status_code
        return r.json(), dt, 200
    except Exception as e:  # noqa: BLE001
        return None, time.monotonic() - t0, repr(e)[:60]


def fetch_events(client: httpx.Client, sport: str, hours: float) -> list[dict]:
    """Événements à venir dans la fenêtre demandée, en paginant."""
    domain = DOMAINS[sport]
    horizon = datetime.now(timezone.utc) + timedelta(hours=hours)
    out: list[dict] = []
    last_id = None
    for _ in range(20):  # garde-fou de pagination
        params = {
            "type_domain": domain,
            "state": "upcoming",
            "type_scope": "single_event",
            "limit": 200,
        }
        if last_id:
            params["pagination_last_id"] = last_id
        data, _dt, code = _get(client, "/events/", params)
        if data is None:
            _p(f"  !! /events/ a répondu {code}")
            break
        page = data.get("events") or []
        if not page:
            break
        out.extend(page)
        nxt = (data.get("pagination") or {}).get("next_page") or ""
        if "pagination_last_id=" not in nxt:
            break
        last_id = nxt.split("pagination_last_id=")[1].split("&")[0]

    kept = []
    for e in out:
        raw = e.get("start_datetime")
        if not raw:
            continue
        try:
            st = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if datetime.now(timezone.utc) < st <= horizon:
            kept.append(e)
    return kept


def probe_batch(client: httpx.Client, kind: str, ids: list[int]) -> tuple[int, str]:
    """Teste un appel groupé. Retourne (nb d'objets rendus, verdict)."""
    joined = ",".join(str(i) for i in ids)
    path = f"/events/{joined}/markets/" if kind == "markets" else f"/markets/{joined}/contracts/"
    params = {"market_types": MAIN_MARKET_TYPES} if kind == "markets" else None
    data, dt, code = _get(client, path, params)
    if data is None:
        return 0, f"REFUSÉ ({code})"
    items = data.get("markets" if kind == "markets" else "contracts") or []
    return len(items), f"OK — {len(items)} objets en {dt:.2f} s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="soccer", choices=sorted(DOMAINS))
    ap.add_argument("--hours", type=float, default=48.0)
    args = ap.parse_args()

    client = httpx.Client(headers=HEADERS, follow_redirects=True)

    _p("=" * 68)
    _p(f"SONDE SMARKETS — {args.sport}, fenêtre {args.hours:g} h")
    _p("=" * 68)

    # ---------------------------------------------------------------- 0. accès
    _p("\n[0] Accès à l'API depuis cette machine")
    data, dt, code = _get(client, "/events/", {"type_domain": DOMAINS[args.sport],
                                               "state": "upcoming", "limit": 1})
    if data is None:
        _p(f"  ÉCHEC — {code}")
        _p("  L'API est injoignable d'ici. Rien d'autre n'est mesurable.")
        return 1
    _p(f"  OK en {dt:.2f} s")

    # ------------------------------------------------------------ 1. événements
    _p(f"\n[1] Événements à venir dans les {args.hours:g} h")
    t0 = time.monotonic()
    events = fetch_events(client, args.sport, args.hours)
    _p(f"  {len(events)} événements en {time.monotonic() - t0:.1f} s")
    if not events:
        _p("  Aucun événement — impossible de mesurer la suite.")
        return 1
    for e in events[:3]:
        _p(f"    {e.get('id')}  {e.get('start_datetime')}  {e.get('name')}")

    ev_ids = [int(e["id"]) for e in events]

    # ------------------------------------------- 2. appel groupé sur /markets/
    _p("\n[2] LEVIER 1 — /events/{id,id,...}/markets/ groupé")
    _p("    (aujourd'hui : une requête PAR événement)")
    market_ids: list[int] = []
    best_ev_batch = 1
    for size in (2, 5, 10, 20, 50):
        if len(ev_ids) < size:
            break
        n, verdict = probe_batch(client, "markets", ev_ids[:size])
        _p(f"  lot de {size:>3} → {verdict}")
        if n > 0:
            best_ev_batch = size
        else:
            break
        time.sleep(0.3)

    # récupère de vrais market_id pour l'étape suivante
    joined = ",".join(str(i) for i in ev_ids[:best_ev_batch])
    data, _dt, _c = _get(client, f"/events/{joined}/markets/",
                         {"market_types": MAIN_MARKET_TYPES})
    for m in (data or {}).get("markets") or []:
        if m.get("state") == "open":
            market_ids.append(int(m["id"]))

    # ----------------------------------------- 3. appel groupé sur /contracts/
    _p("\n[3] LEVIER 2 — /markets/{id,id,...}/contracts/ groupé")
    _p("    (aujourd'hui : une requête PAR marché — c'est LE goulot des 26 min)")
    if not market_ids:
        _p("  Aucun marché ouvert récupéré, test impossible.")
    else:
        _p(f"  {len(market_ids)} marchés ouverts disponibles pour le test")
        for size in (2, 5, 10, 20, 50):
            if len(market_ids) < size:
                break
            _n, verdict = probe_batch(client, "contracts", market_ids[:size])
            _p(f"  lot de {size:>3} → {verdict}")
            if "REFUSÉ" in verdict:
                break
            time.sleep(0.3)

    # ------------------------------------------------- 4. coût par événement
    _p("\n[4] Coût réel de l'approche ACTUELLE (une requête par marché)")
    sample = ev_ids[: min(5, len(ev_ids))]
    t0 = time.monotonic()
    reqs = 0
    for eid in sample:
        d, _dt, _c = _get(client, f"/events/{eid}/markets/", {"market_types": MAIN_MARKET_TYPES})
        reqs += 1
        for m in (d or {}).get("markets") or []:
            if m.get("state") == "open":
                _get(client, f"/markets/{m['id']}/contracts/")
                reqs += 1
        time.sleep(0.1)
    dt = time.monotonic() - t0
    per_ev = dt / len(sample)
    _p(f"  {reqs} requêtes pour {len(sample)} événements en {dt:.1f} s")
    _p(f"  → {per_ev:.2f} s par événement")
    _p(f"  → projeté sur {len(events)} événements : "
       f"{per_ev * len(events) / 60:.1f} minutes")

    _p("\n" + "=" * 68)
    _p("À RETENIR")
    _p("=" * 68)
    _p("  • Si les lots passent en [2] et [3], le rafraîchissement complet")
    _p("    tombe de ~1 000 requêtes à ~100, soit de dizaines de minutes à")
    _p("    quelques dizaines de secondes.")
    _p("  • Ce chiffre décide si Smarkets peut redevenir une source de")
    _p("    référence utilisable (§5, §6 du handover).")
    _p("  • Étape suivante : `books-coverage` pour savoir si Smarkets price")
    _p("    des matchs que Pinnacle ignore — sans gain de couverture, le")
    _p("    bénéfice se limite au débruitage de la ligne juste.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
