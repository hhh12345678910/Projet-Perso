"""Apres l'installation des canaux : quatre verifications, un verdict.

LECTURE SEULE. N'ecrit rien, n'envoie rien, ne modifie aucun canal.

    .venv/bin/python -m scripts.verifier_canaux
    .venv/bin/python -m scripts.verifier_canaux --depuis-minutes 180

Les quatre controles
--------------------
1. Le service tourne.
2. Les canaux en base correspondent EXACTEMENT a ce que .env decrit —
   nom, chat_id, priorite, exclusivite, activation, et chaque regle.
3. Le routage est inchange : les canaux CHARGES DEPUIS LA BASE rendent les
   memes destinations que la traduction de .env, sur toutes les valeurs
   pile aux bornes. C'est le controle qui compte : il porte sur ce que le
   daemon lit vraiment, pas sur ce qu'on croit avoir ecrit.
4. Aucun doublon depuis le redemarrage. Deux signatures cherchees :
     - une ligne de notification a chat_id NULL ecrite APRES le
       redemarrage : `main.py` marquerait encore globalement en plus du
       marquage par canal, et une telle ligne bloquerait ensuite TOUS les
       canaux (elle compte pour chacun) ;
     - un couple (opportunite, canal) au-dela de valuebet_max_alerts.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

from src.alerter import TelegramConfig
from src.channels import charger, depuis_config
from src.config import ScanConfig, load_env_file
from src.models import Book, MarketType, Outcome, ValueBet
from src.routing import canaux_pour
from src.storage import Storage

SERVICE = "valuebet-daemon"
_EV = (-5.0, 4.99, 5.0, 7.99, 8.0, 8.01, 19.99, 20.0, 34.99, 35.0, 35.01, 200.0)
_COTES = (1.01, 1.49, 1.5, 2.10, 3.99, 4.0, 4.01, 5.99, 6.0, 6.01, 12.0)
_SPORTS = ("soccer", "tennis", None)


def _systemctl(*args) -> str:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _depuis_le_redemarrage(defaut_minutes: int) -> tuple[datetime, str]:
    """Depuis quand le daemon tourne — par l'AGE du processus, pas par
    l'horodatage de systemd.

    `systemctl show -p ActiveEnterTimestamp` rend une date dans le fuseau du
    systeme et une abreviation locale (« CEST »), que strptime refuse : le
    controle serait retombe en silence sur une fenetre de 60 minutes, et
    aurait rate des doublons plus anciens. `ps -o etimes=` rend des secondes
    ecoulees — ni fuseau, ni locale, ni format a deviner."""
    pid = _systemctl("show", SERVICE, "-p", "MainPID", "--value")
    if pid and pid != "0":
        try:
            r = subprocess.run(["ps", "-o", "etimes=", "-p", pid],
                               capture_output=True, text=True, timeout=10)
            secondes = int(r.stdout.strip())
            return (datetime.now(timezone.utc) - timedelta(seconds=secondes),
                    f"demarrage du service, il y a {secondes // 60} min")
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    d = datetime.now(timezone.utc) - timedelta(minutes=defaut_minutes)
    return d, (f"les {defaut_minutes} dernieres minutes — l'age du processus "
               f"n'a pas pu etre lu")


def _pari(ev, cote, live):
    quand = datetime.now(timezone.utc) + timedelta(hours=-1 if live else 48)
    return ValueBet(
        event_key=f"{quand:%Y%m%d%H%M}::a__vs__b", book=Book.UNIBET_BE,
        market=MarketType.H2H, outcome=Outcome("home"), odd_taken=cote,
        fair_prob=0.5, fair_odd=2.0, ev_pct=ev, kelly_stake_pct=1.0,
        detected_at=datetime.now(timezone.utc))


def _signature(canal) -> tuple:
    return (canal.nom, canal.chat_id, canal.actif, canal.priorite,
            canal.exclusif, canal.regles)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--depuis-minutes", type=int, default=60)
    a = p.parse_args()

    print(f"env : {load_env_file('.env')} cles chargees depuis .env")
    cfg = TelegramConfig.from_env()
    if cfg is None:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents.")
        return 2
    db = ScanConfig().db_path
    st = Storage(db)
    echecs: list[str] = []
    indetermines: list[str] = []

    # ── 1. le service ──────────────────────────────────────────────────
    print("\n── 1. le service ──")
    etat = _systemctl("is-active", SERVICE)
    depuis = _systemctl("show", SERVICE, "-p", "ActiveEnterTimestamp", "--value")
    pid = _systemctl("show", SERVICE, "-p", "MainPID", "--value")
    if not etat:
        # Un controle qu'on ne PEUT PAS faire n'est pas un controle qui
        # echoue. Les confondre ferait passer un NO-GO pour un diagnostic.
        print("   ⚠ systemctl indisponible — etat du service INDETERMINE")
        indetermines.append("etat du service (systemctl indisponible)")
    else:
        print(f"   {SERVICE} : {etat}   PID {pid or '?'}")
        print(f"   actif depuis : {depuis or '?'}")
        if etat != "active":
            echecs.append(f"le service n'est pas actif ({etat})")

    # ── 2. la configuration installee ──────────────────────────────────
    print("\n── 2. les canaux en base vs .env ──")
    en_base = charger(st, print_fn=lambda s: echecs.append(f"chargement : {s}"))
    attendus = depuis_config(cfg)
    print(f"   en base : {len(en_base)}   attendus : {len(attendus)}")
    sig_b = {c.nom: _signature(c) for c in en_base}
    sig_a = {c.nom: _signature(c) for c in attendus}
    for nom in sorted(set(sig_a) | set(sig_b)):
        if nom not in sig_b:
            echecs.append(f"canal {nom} absent de la base")
            print(f"   ✗ {nom} : ABSENT de la base")
        elif nom not in sig_a:
            print(f"   ⚠ {nom} : en base mais pas dans .env (canal ajoute a la main ?)")
        elif sig_b[nom] != sig_a[nom]:
            echecs.append(f"canal {nom} : la base ne correspond pas a .env")
            print(f"   ✗ {nom} : DIFFERENT")
            for i, (x, y) in enumerate(zip(sig_a[nom], sig_b[nom])):
                if x != y:
                    champ = ("nom", "chat_id", "actif", "priorite", "exclusif",
                             "regles")[i]
                    print(f"        {champ} : .env={x!r}  base={y!r}")
        else:
            c = sig_b[nom]
            print(f"   ✓ {nom} : chat={c[1]} prio={c[3]} "
                  f"{'exclusif ' if c[4] else ''}{len(c[5])} regle(s)")

    # ── 3. le routage, depuis ce que le daemon lit vraiment ────────────
    print("\n── 3. le routage (canaux charges depuis la base) ──")
    ecarts = 0
    total = 0
    for live in (False, True):
        for ev in _EV:
            for cote in _COTES:
                for sport in _SPORTS:
                    bet = _pari(ev, cote, live)
                    x = [c.nom for c in canaux_pour(bet, sport=sport, league=None,
                                                    is_live=live, canaux=en_base)]
                    y = [c.nom for c in canaux_pour(bet, sport=sport, league=None,
                                                    is_live=live, canaux=attendus)]
                    total += 1
                    if x != y:
                        ecarts += 1
                        if ecarts <= 5:
                            print(f"   ✗ ev={ev} cote={cote} sport={sport} "
                                  f"live={live} : base={x} .env={y}")
    print(f"   {total - ecarts} / {total} identiques, {ecarts} divergence(s)")
    if ecarts:
        echecs.append(f"{ecarts} divergence(s) de routage entre la base et .env")

    # ── 4. les doublons depuis le redemarrage ──────────────────────────
    print("\n── 4. les doublons ──")
    debut, origine = _depuis_le_redemarrage(a.depuis_minutes)
    print(f"   fenetre : depuis {debut.isoformat()} — {origine}")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    borne = debut.isoformat()
    nuls = con.execute(
        "SELECT COUNT(*) FROM notified_value_bets "
        "WHERE chat_id IS NULL AND notified_at >= ?", (borne,)).fetchone()[0]
    par_canal = con.execute(
        "SELECT chat_id, COUNT(*) n FROM notified_value_bets "
        "WHERE notified_at >= ? GROUP BY chat_id ORDER BY n DESC", (borne,)).fetchall()
    trop = con.execute(
        "SELECT event_key, book, market, outcome_label, line, chat_id, COUNT(*) n "
        "FROM notified_value_bets WHERE notified_at >= ? AND chat_id IS NOT NULL "
        "GROUP BY event_key, book, market, outcome_label, line, chat_id "
        "HAVING n > ? ORDER BY n DESC LIMIT 10",
        (borne, cfg.valuebet_max_alerts)).fetchall()
    con.close()

    print(f"   alertes marquees depuis : {sum(r['n'] for r in par_canal)}")
    for r in par_canal:
        print(f"      {r['chat_id'] or 'NULL (global)':<20} {r['n']}")
    if nuls:
        echecs.append(f"{nuls} marquage(s) GLOBAUX apres le redemarrage — "
                      f"main.py marque encore en plus du marquage par canal")
        print(f"   ✗ {nuls} ligne(s) a chat_id NULL apres le redemarrage")
    else:
        print("   ✓ aucun marquage global : le dedoublonnage est bien par canal")
    if trop:
        echecs.append(f"{len(trop)} couple(s) (opportunite, canal) au-dela du plafond")
        print(f"   ✗ au-dela de valuebet_max_alerts={cfg.valuebet_max_alerts} :")
        for r in trop:
            print(f"      {r['event_key']} {r['book']} {r['market']} "
                  f"-> {r['chat_id']} : {r['n']}")
    else:
        print(f"   ✓ aucun couple (opportunite, canal) au-dela de "
              f"{cfg.valuebet_max_alerts}")

    print("\n" + "═" * 60)
    for i in indetermines:
        print(f"⚠ INDETERMINE : {i}")
    if echecs:
        print(f"NO-GO — {len(echecs)} probleme(s) :")
        for e in echecs:
            print(f"   • {e}")
        return 1
    faites = 4 - len(indetermines)
    print(f"GO — {faites} verification(s) sur 4 passent"
          + (f", {len(indetermines)} indeterminee(s)." if indetermines else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
