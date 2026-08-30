"""Quel canal reçoit quoi — sans rien envoyer.

Pourquoi cet outil existe
-------------------------
Le routage est réparti sur quatre conditions indépendantes dans
`send_value_bet` (§21) et une cascade dans `send_system_alert`. La seule
façon de savoir où part réellement une alerte était de la provoquer, donc
d'attendre qu'un vrai pari tombe — et de le lire dans un canal, ce qui ne
dit rien des canaux où il n'est PAS parti.

Deux erreurs de configuration silencieuses ont motivé ce script : un
`TELEGRAM_MAINTENANCE_CHAT_ID` avec un tiret en trop (`--100…`), qui a fait
disparaître une alerte de panne sans un mot, et l'exclusion tennis de la
bande longue, dont personne ne pouvait vérifier l'effet avant qu'un pari
tennis à grosse cote ne se présente.

    .venv/bin/python -m scripts.routage_telegram
    .venv/bin/python -m scripts.routage_telegram --envoyer

Sans `--envoyer`, AUCUN message ne part : l'envoi est remplacé par un
enregistrement du chat_id visé. C'est le défaut, et c'est délibéré — un
diagnostic ne doit pas pouvoir polluer les canaux qu'il inspecte.

⚠️ Ce script rejoue le routage RÉEL en appelant `send_value_bet`. Il ne
réimplémente aucune règle : une sonde qui recalcule autre chose que la
production ment (§17.7).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import src.alerter as al
from src.alerter import TelegramConfig, send_system_alert
from src.config import load_env_file
from src.models import Book, MarketType, Outcome, ValueBet

# Noms d'équipes impossibles à confondre avec un vrai match, au cas où un
# `--envoyer` partirait dans un canal réel.
EQUIPES = "ceci_est_un_test__vs__aucun_pari"

# (cote, EV %, sport, ce qu'on cherche à montrer)
CAS = (
    (12.00, 60.0, "soccer", "grosse cote hors bandes premium"),
    (1.20, 40.0, "soccer", "cote basse hors bandes premium"),
    (2.10, 50.0, "soccer", "bande premium standard"),
    (2.10, 12.0, "soccer", "EV moyenne, bande standard"),
    (5.00, 25.0, "soccer", "bande premium longue"),
    (5.00, 25.0, "tennis", "bande longue exclue -> voie grosses cotes"),
    (5.00, 40.0, "tennis", "bande longue exclue, EV critique"),
    (5.00, 15.0, "tennis", "bande longue exclue, EV sous 20 %"),
    (8.00, 25.0, "soccer", "au-dessus de toute bande premium"),
    (2.10, 25.0, "soccer", "grosse EV mais cote <= 4 : pas de voie critique"),
    (2.10, 3.0, "soccer", "EV sous tous les seuils"),
)


def canaux(cfg: TelegramConfig) -> str:
    return (
        f"principal   : {cfg.chat_id}\n"
        f"premium     : {cfg.effective_premium_chat_id}\n"
        f"critique    : {cfg.effective_critical_chat_id}\n"
        f"maintenance : {cfg.effective_maintenance_chat_id}\n"
        f"surebet     : {cfg.effective_surebet_chat_id}\n"
        f"clv         : {cfg.effective_clv_chat_id}"
    )


def regles(cfg: TelegramConfig) -> str:
    exclus = ", ".join(cfg.premium_hi_sports_exclus) or "aucun"
    return (
        f"principal  : {cfg.min_ev_pct} <= EV < {cfg.main_max_ev_pct} % "
        f"et cote {cfg.main_min_odd}-{cfg.main_max_odd}\n"
        f"premium    : EV >= {cfg.min_premium_ev_pct} % en cote "
        f"{cfg.premium_min_odd}-{cfg.premium_max_odd}, ou EV >= "
        f"{cfg.premium_hi_min_ev} % en cote {cfg.premium_hi_min_odd}-"
        f"{cfg.premium_hi_max_odd} (sports exclus de la bande longue : {exclus})\n"
        f"critique   : EV >= {cfg.min_critical_ev_pct} %, sans limite de cote, "
        f"mais SEULEMENT si le premium n'a pas déjà pris le pari"
    )


def _pari(odd: float, ev: float) -> ValueBet:
    depart = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y%m%d%H%M")
    return ValueBet(
        event_key=f"{depart}::{EQUIPES}", book=Book.UNIBET_BE,
        market=MarketType.H2H, outcome=Outcome("home"), odd_taken=odd,
        fair_prob=1 / (odd / (1 + ev / 100)), fair_odd=round(odd / (1 + ev / 100), 3),
        ev_pct=ev, kelly_stake_pct=1.0, detected_at=datetime.now(timezone.utc))


@contextmanager
def espionner(vus: list[str], *, envoyer: bool):
    """Remplace l'envoi par un enregistrement du chat_id visé.

    `envoyer` ne change QUE le fait de parler à Telegram : le chat_id est
    releve dans les deux cas, sinon le mode reel ne dirait plus rien. La
    restauration est dans un `finally` — un `_send` laisse en place
    transformerait tout envoi ulterieur du processus en silence."""
    reel = al.TelegramAlerter._send

    def _espion(self, text, chat_id, reply_markup=None):
        vus.append(chat_id)
        return reel(self, text, chat_id, reply_markup) if envoyer else True

    al.TelegramAlerter._send = _espion
    try:
        yield
    finally:
        al.TelegramAlerter._send = reel


def tableau(cfg: TelegramConfig, *, envoyer: bool, print_fn=print) -> list[list[str]]:
    """Rejoue le routage pour chaque cas. Renvoie les canaux atteints."""
    vus: list[str] = []
    atteints: list[list[str]] = []
    with espionner(vus, envoyer=envoyer):
        for odd, ev, sport, quoi in CAS:
            vus.clear()
            with al.TelegramAlerter(cfg) as a:
                a.send_value_bet(_pari(odd, ev), sport=sport)
            atteints.append(list(vus))
            print_fn(f"  cote {odd:>6.2f} | EV {ev:>5.1f} % | {sport:<7} | "
                     f"{', '.join(vus) or 'AUCUN CANAL':<28} | {quoi}")
    return atteints


def panne(cfg: TelegramConfig, *, envoyer: bool) -> tuple[list[str], bool]:
    """Rejoue une alerte d'exploitation par son chemin reel."""
    vus: list[str] = []
    with espionner(vus, envoyer=envoyer):
        ok = send_system_alert(cfg, "🔧 Test de routage — aucune panne réelle")
    return vus, ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--envoyer", action="store_true",
                   help="envoie VRAIMENT les messages de test dans les canaux")
    a = p.parse_args()

    print(f"env : {load_env_file('.env')} clés chargées depuis .env")
    cfg = TelegramConfig.from_env()
    if cfg is None:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents — rien à router.")
        return 2

    print("\n── canaux configurés ──")
    print(canaux(cfg))
    print("\n── règles appliquées ──")
    print(regles(cfg))
    print(f"\n── routage d'un value bet {'(ENVOI RÉEL)' if a.envoyer else '(simulation)'} ──")
    tableau(cfg, envoyer=a.envoyer)

    print("\n── alerte de panne (book muet, Pinnacle muet) ──")
    vus, ok = panne(cfg, envoyer=a.envoyer)
    print(f"  -> {', '.join(vus) or 'AUCUN CANAL'} (accepté : {ok})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
