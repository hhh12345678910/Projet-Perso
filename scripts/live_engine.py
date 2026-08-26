"""Moteur LIVE — observation. §PHASE 5, commit 4

AsianOdds fait le prix juste ET donne le score ; Unibet LIVE prend la cote.

    # lecture seule, AUCUN envoi — c'est le mode par defaut
    .venv/bin/python -m scripts.live_engine --minutes 10

    # meme chose, mais on IMPRIME les messages Telegram sans les envoyer
    .venv/bin/python -m scripts.live_engine --minutes 10 --telegram-blanc

    # envoi reel vers le canal LIVE
    .venv/bin/python -m scripts.live_engine --minutes 10 --telegram

N'ECRIT RIEN EN BASE, jamais. Aucun pari, aucun bouton, aucune action
bookmaker. `--telegram` exige TELEGRAM_LIVE_SUREBET_CHAT_ID : sans lui,
`send_live_observation` refuse d'envoyer plutot que de retomber sur le canal
prematch.

AUCUN PLAFOND D'EV. +10 %, +100 %, +500 % sortent tous si le calcul est valide.
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime, timezone

from src.live_value import (
    AGE_MAX_FAIR_SEC, AGE_MAX_PRENEUR_SEC, MINUTES_MAX_LIVE,
    OVERROUND_PRENEUR_MAX, SEUIL_EV_PCT, Memoire, Statut, evaluer, resume)
from src.storage import Storage
from src.unibet_live import PERIODE_SEC, UnibetLive, apparier

TRANCHES = (10, 20, 50, 100)


def _p(x, u="s"):
    return "N/A" if x is None else f"{x:.1f} {u}"


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--periode", type=float, default=PERIODE_SEC)
    p.add_argument("--sport", default="soccer")
    p.add_argument("--db", default="data/valuebet.db")
    p.add_argument("--ev", type=float, default=SEUIL_EV_PCT,
                   help=f"seuil BAS d'EV en %% (défaut : {SEUIL_EV_PCT:g}). "
                        f"Il n'existe aucun seuil haut.")
    p.add_argument("--age-fair", type=float, default=AGE_MAX_FAIR_SEC)
    p.add_argument("--age-preneur", type=float, default=AGE_MAX_PRENEUR_SEC)
    p.add_argument("--telegram", action="store_true",
                   help="ENVOYER vers le canal LIVE")
    p.add_argument("--telegram-blanc", action="store_true",
                   help="imprimer les messages sans les envoyer")
    p.add_argument("--tout", action="store_true", help="afficher les doublons")
    p.add_argument("--test-telegram", action="store_true",
                   help="envoyer UNE alerte de test au canal LIVE et sortir. "
                        "Ne sonde rien, ne lit pas la base.")
    p.add_argument("--envoyer-rejets", action="store_true",
                   help="envoyer AUSSI sur Telegram les occasions rejetées "
                        "(fair périmée, match terminé…). Elles restent de "
                        "toute façon affichées ici.")
    a = p.parse_args()

    cfg = alerte = None
    if a.telegram or a.telegram_blanc:
        from src.alerter import (TelegramConfig, format_live_observation,
                                 send_live_observation)
        cfg = TelegramConfig.from_env()
        if cfg is None:
            # `from_env` rend None des qu'il manque TELEGRAM_BOT_TOKEN ou
            # TELEGRAM_CHAT_ID. Le projet ne charge pas `.env` tout seul : un
            # lancement a la main n'a donc pas l'environnement du daemon.
            import os as _os
            absents = [v for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
                       if not _os.getenv(v)]
            print(f"[live] Telegram NON configuré — absent(s) de "
                  f"l'environnement : {', '.join(absents)}")
            print("[live] le projet ne lit pas `.env` tout seul. Charger :")
            print("       set -a && . ./.env && set +a")
            return 2
        alerte = (format_live_observation, send_live_observation)
        chat = cfg.live_surebet_chat_id
        print(f"[live] Telegram : canal LIVE = {chat or 'NON DÉFINI'}"
              f"   prématch = {cfg.chat_id}   surebet prématch = "
              f"{cfg.surebet_chat_id}")
        if a.telegram and not chat:
            print("[live] TELEGRAM_LIVE_SUREBET_CHAT_ID absent — AUCUN envoi "
                  "ne partira (le repli irait vers le prématch).")

    if a.test_telegram:
        # UNE alerte, tout de suite, sans reseau bookmaker ni base. Elle
        # passe par le VRAI formateur et le VRAI envoi : c'est ce qui la
        # rend concluante. Les valeurs sont inventees et le message le dit —
        # un message de test qui ressemblerait a une vraie occasion serait
        # pire qu'inutile.
        from datetime import timedelta
        from src.live_value import Opportunite, Statut
        from src.models import Book, MarketType
        maintenant = datetime.now(timezone.utc)
        faux = Opportunite(
            detecte_a=maintenant,
            event_key="TEST::ceci_est_un_test__vs__aucun_pari",
            home="CECI EST UN TEST", away="AUCUN PARI",
            market=MarketType.H2H, line=None, outcome="home",
            book=Book.UNIBET_BE, cote_preneur=2.00, fair_prob=0.60,
            fair_cote=1.67, ev_pct=20.0, statut=Statut.OBSERVEE_SCORE_INCONNU,
            motif="message de vérification du canal — données inventées",
            kelly_pct=20.0, age_fair_sec=1.0, age_preneur_sec=0.1,
            delai_calcul_sec=0.1, feed_score="0:0",
            source_event_id_fair="TEST", source_event_id_preneur="TEST",
            minute_ecoulee=1.0)
        n = alerte[1]([faux], cfg)
        print(f"[live] alerte de test : {n} message(s) accepté(s) par Telegram "
              f"vers {cfg.live_surebet_chat_id}")
        print("[live] vérifiez dans « Sure Bet live » : le message est arrivé, "
              "il n'a AUCUN bouton, et le canal prématch est resté muet.")
        return 0 if n else 1

    storage, live, memoire = Storage(a.db), UnibetLive(a.sport), Memoire()
    total, envoyes, passages, erreurs = Counter(), 0, 0, 0
    retenues, apparies_max, vus_max = {}, 0, 0

    debut = datetime.now(timezone.utc)
    print(f"[live] démarrage {debut.isoformat()} — {a.minutes:g} min, "
          f"sondage {a.periode:g} s, EV > {a.ev:g} % SANS plafond, "
          f"AUCUNE écriture")
    fin = time.monotonic() + a.minutes * 60
    try:
        while time.monotonic() < fin:
            c = live.sonder()
            if c.erreur:
                erreurs += 1
                print(f"[live] sondage en erreur : {c.erreur}")
                time.sleep(a.periode)
                continue
            passages += 1
            maintenant = datetime.now(timezone.utc)
            app = apparier(live.instantane, storage, maintenant, a.sport)
            apparies_max = max(apparies_max, app.matchs_apparies)
            vus_max = max(vus_max, app.matchs_vus)
            # Horloge RELUE ici. Prise avant `apparier`, elle rendait un
            # « temps de détection » de 0,00 s qui ne mesurait rien : tout le
            # coût du rapprochement tombait hors de la fenêtre mesurée.
            maintenant = datetime.now(timezone.utc)
            an = evaluer(app.quotes, storage, maintenant, memoire=memoire,
                         preneur_pris_a=live.instantane.pris_a,
                         seuil_ev=a.ev, age_max_fair=a.age_fair,
                         age_max_preneur=a.age_preneur)
            for o in (an.opportunites if a.tout else an.nouvelles):
                print(o.ligne())
            # Ce qui PART sur Telegram : les occasions vivantes, doublons
            # exclus. Le statut n'est pas un filtre ici — une occasion
            # rejetee part quand meme, marquee de son statut : c'est une
            # phase d'OBSERVATION, et ce qu'on ecarte est aussi une donnee.
            for o in an.nouvelles:
                v = retenues.get(o.cle)
                if v is None or o.ev_pct > v.ev_pct:
                    retenues[o.cle] = o
            # CE QUI PART sur Telegram. Ce n'est PAS un filtre d'EV — aucune
            # EV n'est jamais écartée pour sa taille, ni ici ni ailleurs.
            # C'est un filtre de VALIDITÉ : une occasion dont la fair a 57
            # minutes, ou dont le match est fini, a déjà été établie comme
            # non calculable. L'envoyer n'ajoute rien à l'observation et
            # noie ce qui mérite d'être regardé — mesuré le 26/08 : ~10
            # messages/minute, soit des dizaines de milliers sur quelques
            # jours. Tout reste affiché ici, et `--envoyer-rejets` rétablit
            # l'envoi complet.
            partants = [o for o in an.nouvelles
                        if a.envoyer_rejets
                        or not o.statut.value.startswith("REJET_")]
            if alerte is not None and partants:
                if a.telegram:
                    envoyes += alerte[1](partants, cfg)
                else:
                    for o in partants:
                        print("\n--- message Telegram (NON envoyé) ---")
                        print(alerte[0](o))
            total["retenus_envoi"] += len(partants)
            total["retenus_muets"] += len(an.nouvelles) - len(partants)
            total.update(an.par_statut)
            total["quotes"] += an.quotes_analysees
            total["sous_seuil"] += an.sous_seuil
            total["partiels"] += an.partiels
            total["matchs"] = max(total["matchs"], an.matchs_analyses)
            total["fair"] = max(total["fair"], an.groupes_fair)
            for m, n in an.groupes_rejetes.items():
                total[f"groupe:{m}"] += n
            for m, n in an.ecartees.items():
                total[f"ecart:{m}"] += n
            if time.monotonic() + a.periode > fin:
                break
            time.sleep(a.periode)
    except KeyboardInterrupt:
        print("\n[live] interrompu")
    finally:
        live.close()

    sel = list(retenues.values())
    duree = (datetime.now(timezone.utc) - debut).total_seconds()
    print(f"\n{'═' * 72}")
    print(f"OBSERVATION — {duree:.0f} s, {passages} passages, {erreurs} erreurs")
    print('═' * 72)

    # ── L'ENTONNOIR : combien restent apres chaque etape ──────────────
    print("\nENTONNOIR")
    print(f"  matchs Unibet vus                       {vus_max}")
    print(f"  … appariés à nos events                 {apparies_max}")
    print(f"  lignes justes AsianOdds retenues        {total['fair']}")
    for m in sorted(k for k in total if k.startswith("groupe:")):
        print(f"      écartées — {m[7:]:<28} {total[m]}")
    print(f"  cotes preneuses comparées               {total['quotes']}")
    for m in sorted(k for k in total if k.startswith("ecart:")):
        print(f"      écartées — {m[6:]:<28} {total[m]}")
    print(f"  … sous le seuil d'EV                    {total['sous_seuil']}")
    print(f"  OCCASIONS ≥ {a.ev:g} %                          {len(sel)}")
    for s in Statut:
        print(f"      {s.value:<32} {total.get(s.value, 0)}")
    # Distinct, comme les autres lignes de l'entonnoir. `an.partiels`
    # compte des OCCURRENCES et se cumule a chaque passage : l'afficher ici
    # ferait lire « 6 marches partiels » la ou il y en a 2.
    print(f"  dont marchés partiels (conservés)       "
          f"{sum(1 for o in sel if o.partiel)}"
          f"   ({total['partiels']} occurrences)")

    print("\nTRANCHES D'EV (aucun plafond)")
    for t in TRANCHES:
        n = sum(1 for o in sel if o.ev_pct >= t)
        print(f"  EV ≥ {t:>3} %   {n}")

    if sel:
        print("\nTOP EV")
        for o in sorted(sel, key=lambda x: -x.ev_pct)[:5]:
            print(f"  {o.ev_pct:>+8.1f} %  kelly {o.kelly_pct:>6.2f} %  "
                  f"{o.home}-{o.away} {o.market.value} {o.outcome} "
                  f"@{o.cote_preneur:.2f} score {o.feed_score or 'N/A'} "
                  f"[{o.statut.value}]")
        print("\nTOP KELLY")
        for o in sorted(sel, key=lambda x: -x.kelly_pct)[:5]:
            print(f"  kelly {o.kelly_pct:>6.2f} %  EV {o.ev_pct:>+8.1f} %  "
                  f"{o.home}-{o.away} {o.market.value} {o.outcome} "
                  f"@{o.cote_preneur:.2f} score {o.feed_score or 'N/A'} "
                  f"[{o.statut.value}]")
        partiels = [o for o in sel if o.partiel]
        if partiels:
            print("\nMARCHÉS PARTIELS (conservés, jamais rejetés)")
            for o in sorted(partiels, key=lambda x: -x.ev_pct)[:5]:
                print(f"  {o.ev_pct:>+8.1f} %  {o.home}-{o.away} "
                      f"{o.market.value} {o.outcome} — manque "
                      f"{', '.join(o.issues_manquantes)}  marge preneuse "
                      f"{_p(o.overround_preneur, '')}")
    if alerte is not None:
        quoi = ("envoyé(s)" if a.telegram else "qui SERAIENT partis")
        print(f"\nTELEGRAM : {total['retenus_envoi']} message(s) {quoi} vers "
              f"{cfg.live_surebet_chat_id or 'AUCUN CANAL'}")
        print(f"           {total['retenus_muets']} rejet(s) NON envoyé(s) "
              f"(affichés ci-dessus ; --envoyer-rejets pour les inclure)")
        if a.telegram:
            print(f"           {envoyes} accepté(s) par l'API Telegram")
    return 0 if passages else 1


if __name__ == "__main__":
    raise SystemExit(main())
