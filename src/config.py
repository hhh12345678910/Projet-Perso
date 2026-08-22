from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env_file(path: str | Path | None = None) -> int:
    """Charge `.env` dans os.environ. Renvoie le nombre de clés posées.

    Le daemon reçoit sa config par `scan-daemon.sh`, qui source `.env` avant de
    lancer Python. Tout ce qui démarre autrement — une commande lancée à la
    main, `doctor`, ou `bot_listener` sous systemd — n'a rien dans son
    environnement et croit le projet non configuré. Ça s'est produit deux fois :
    `doctor` annonçait « Telegram non configuré » sur une installation qui
    marchait, et `/scan` se désactivait tout seul en n'acceptant aucun chat.

    L'environnement existant gagne (`setdefault`) : un override explicite passé
    au service reste prioritaire sur le fichier.
    """
    env_file = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return 0
    n = 0
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if v[:1] == v[-1:] and v[:1] in ("'", '"') and len(v) >= 2:
            v = v[1:-1]
        if k not in os.environ:
            n += 1
        os.environ.setdefault(k, v)
    return n


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "non")


# ── Attente maximale sur un verrou d'écriture SQLite ──────────────────────
#
# Une seule valeur pour TOUS les chemins qui ouvrent `data/valuebet.db`. Il y en
# avait cinq : 3 s et 5 s dans `alerter.py`, 10 s sur le chemin chaud de
# `Storage._conn`, 60 s pour la purge et pour VACUUM.
#
# Le choix de 60 s, et pas d'une moyenne :
#
# - **le timeout est INERTE hors contention.** SQLite ne l'attend que sur
#   SQLITE_BUSY ; en fonctionnement normal, avec WAL et des transactions
#   courtes, il ne coûte rien. L'élever ne ralentit donc pas le cycle ;
# - **les coûts ne sont pas symétriques.** Une écriture perdue est perdue pour
#   de bon — les lignes de clôture ne se rattrapent pas une fois la purge
#   passée (§21.22). Une attente ne coûte que de la latence, une fois ;
# - **695 collectes ont déjà été perdues en 11 h sur `database is locked`, à UN
#   seul processus.** Le chemin le plus court était celui du cycle ;
# - 60 s est la valeur déjà retenue pour les deux chemins qui avaient été
#   réfléchis (purge, VACUUM). On s'aligne sur la valeur pensée plutôt que d'en
#   inventer une sixième.
#
# ⚠️ Ce réglage est ce qui rendra deux processus viables sans changer de moteur
# de base. Le baisser rouvrirait exactement la panne du §21.22.
SQLITE_BUSY_TIMEOUT_SEC = float(os.getenv("SQLITE_BUSY_TIMEOUT_SEC", "60"))


# ── Smarkets : deux drapeaux globaux, une seule source de vérité ──────────
#
# Ils vivaient dans la couche de collecte, et `main.py` en importait une copie.
# `SMARKETS_ENABLED` était donc lu dans DEUX espaces de noms : un futur
# changement aurait dû penser aux deux, et n'en aurait probablement changé qu'un.
#
# Ce sont des constantes de MODULE, pas des champs de `ScanConfig` : elles sont
# évaluées à l'import, avant que la CLI n'appelle `load_env_file()`. Le daemon
# les reçoit par `scan-daemon.sh`, qui source `.env` avant de lancer Python.
# En faire des champs de `ScanConfig` changerait ce moment-là, donc le
# comportement.
#
# ⚠️ L'expression est reprise TELLE QUELLE, sans passer par `_env_flag`. Les
# deux ne sont pas équivalentes : `_env_flag` replie la casse, donc
# `SMARKETS_ENABLED=FALSE` y vaut faux, alors qu'ici il vaut VRAI (« FALSE »
# n'est pas dans la liste). C'est probablement un défaut, mais le corriger
# serait un changement de comportement — il se décide à part.
SMARKETS_ENABLED = os.getenv("SMARKETS_ENABLED", "0") not in ("0", "false", "False", "")
SMARKETS_AS_REFERENCE = os.getenv("SMARKETS_AS_REFERENCE", "0") not in (
    "0", "false", "False", "")


@dataclass
class ScanConfig:
    sport: str = "soccer"
    min_ev_pct: float = 2.0
    max_ev_pct: float = 1000.0   # detection cap: keep huge "error-looking" edges
                                 # (the premium/critical channels surface these on
                                 # purpose); above 1000% is almost certainly a
                                 # parsing/line bug and stays filtered out
    min_minutes_to_kickoff: int = 30
    devig_method: str = "shin"
    kelly_fraction: float = 0.25
    bankroll: float = 1000.0
    db_path: str = "data/valuebet.db"

    # Value bets on events that have already kicked off. Off by default: the
    # fair line comes from Pinnacle's PREMATCH feed (the scraper skips isLive
    # matchups), so once a match starts there is no live reference and the
    # comparison is against a price frozen at kick-off. Measured over a week,
    # those detections showed +20.5% CLV against +7.6% prematch — not an edge,
    # a stale denominator. They reached no alert channel anyway (premium and
    # critical are prematch-only, the main chat caps at 8% EV), so switching
    # this off changes no alert; it only stops them polluting the statistics.
    #
    # Scope: value bets ONLY. Surebets and middles compare soft books against
    # each other and need no sharp reference, so they keep running live — as
    # does quote storage, and therefore closing-line capture and CLV.
    #
    # Re-enable with VALUEBET_SCAN_LIVE=1 in .env, then restart the daemon.
    scan_live_value_bets: bool = field(
        default_factory=lambda: _env_flag("VALUEBET_SCAN_LIVE", default=False)
    )

    # Surebets — détection ET diffusion. Coupés le 21/08 sur demande.
    #
    # Coupe TOUT d'un coup, et c'est voulu : le calcul
    # (`canonicalize_for_surebets` + `find_surebets`), les deux canaux Telegram
    # dédiés (prématch et live), et la copie vers le canal critique au-delà de
    # TELEGRAM_MIN_CRITICAL_SUREBET. Laisser le calcul tourner pour n'en couper
    # que l'envoi garderait le coût sans le service.
    #
    # ⚠️ Ne PAS vider TELEGRAM_SUREBET_CHAT_ID pour obtenir le même effet :
    # `effective_surebet_chat_id` retombe sur le canal PRINCIPAL quand il est
    # vide, donc les surebets iraient polluer le chat principal au lieu de
    # disparaître. Ces identifiants servent aussi la liste blanche du listener
    # (`_allowed_chats`) : les effacer couperait ces canaux de /scan et /book.
    #
    # Coût mesuré du calcul, à l'échelle réelle (900 événements, 8 books,
    # ~44 000 cotes) : 0,89 s pour la canonicalisation et 0,12 s pour la
    # recherche, soit ~1,0 s par sport. Les sports tournant en PARALLÈLE dans
    # le daemon, le cycle ne raccourcit que du sport le plus lent — environ
    # 1 s sur ~54 s, soit ~2 %. Les surebets réutilisent des cotes déjà
    # téléchargées : il n'y a aucun gain réseau à en attendre.
    #
    # Rien n'est supprimé : `src/surebet.py`, la table `notified_surebets`, les
    # réglages TELEGRAM_* et la commande `scan-surebets` restent en place.
    # Réactiver avec SCAN_SUREBETS=1 dans .env, puis redémarrer le daemon.
    scan_surebets: bool = field(
        default_factory=lambda: _env_flag("SCAN_SUREBETS", default=False)
    )

    # Middles — détection ET diffusion. Coupés le 22/08 sur demande : ils ne
    # sont plus joués, donc le calcul ne sert plus à rien.
    #
    # ⚠️ Le canal n'est PAS touché, et il ne faut surtout pas le toucher : les
    # middles partent sur le canal CLV, partagé avec les alertes de CLV. Le
    # couper ferait taire une mesure qu'on veut garder. Ici on coupe le calcul,
    # et l'absence d'envoi en découle — le canal reste vivant pour la CLV.
    #
    # `find_middles` a besoin de `fair`, qui est de toute façon calculé pour
    # les value bets : le gain est celui de la recherche seule. MESURÉ à
    # l'échelle réelle (900 événements, 8 books, ~65 000 cotes de totaux,
    # 5 400 lignes justes) : **0,24 s par sport**.
    #
    # Avec les surebets (~1,00 s), le total coupé vaut ~1,24 s par sport, soit
    # environ 2 % d'un cycle de ~54 s — les sports tournant en parallèle. La
    # vraie raison de couper n'est pas là : un calcul dont personne ne lit le
    # résultat est du bruit, quel que soit son prix.
    #
    # Rien n'est supprimé : `src/middle.py`, la table `notified_middles`, les
    # réglages TELEGRAM_MIDDLE_* et le formatage restent en place.
    # Réactiver avec SCAN_MIDDLES=1 dans .env, puis redémarrer le daemon.
    scan_middles: bool = field(
        default_factory=lambda: _env_flag("SCAN_MIDDLES", default=False)
    )
