"""Des lignes de base aux objets que `routing` sait lire.

Ce module est la seule couture entre la persistance et le modèle de
décision. `storage` reste ignorant de `routing` (il ne fait que du SQL) et
`routing` reste ignorant de SQLite (il n'importe rien du tout) : la
conversion doit donc vivre quelque part, et c'est ici.

Il ne touche pas la base non plus — il reçoit un objet qui sait rendre ses
trois tables (`load_channel_rows`), et rend des `Canal`. Ça le rend
testable sans fichier, et ça permettra à un futur `/test <canal>` de
router des lignes de base sans passer par le daemon.

⚠️ Un canal dont les règles sont invalides est SAUTÉ et SIGNALÉ, jamais
jeté en silence — et il ne fait jamais tomber le chargement des autres.
Ce chargement tournera dans le cycle du daemon : une ligne écrite à la
main dans SQLite ne doit pas pouvoir arrêter toutes les alertes. Le seul
chemin d'écriture normal (`Storage.add_rule_value`) valide déjà à la
source, donc une ligne invalide signifie que quelqu'un a édité la base.
"""
from __future__ import annotations

from typing import Any, Optional

from src.routing import Borne, Canal, Critere, Regle


def _borne(valeur: Any, stricte: Any) -> Optional[Borne]:
    """Une colonne NULL vaut « pas de borne ». 0.0 est une borne comme une
    autre — d'où le test sur None et non sur la fausseté."""
    if valeur is None:
        return None
    return Borne(float(valeur), stricte=bool(stricte))


def _criteres(lignes) -> tuple:
    """Regroupe les valeurs par (dimension, inclut).

    Le sens est porté par la LIGNE, donc rien n'empêche en base d'écrire
    « sport inclut tennis » et « sport exclut soccer » sous la même règle.
    Les deux deviennent deux critères sur la même dimension, ce que `Regle`
    refuse — et c'est voulu : le OU s'exprime par plusieurs valeurs, pas par
    deux critères qui se combineraient en ET et ne passeraient jamais."""
    groupes: dict[tuple, set] = {}
    for v in lignes:
        groupes.setdefault((v["dimension"], bool(v["inclut"])), set()).add(v["valeur"])
    return tuple(
        Critere(dimension=dim, valeurs=frozenset(vals), inclut=inclut)
        for (dim, inclut), vals in sorted(groupes.items())
    )


# ══ Traduction de la configuration .env en canaux ══════════════════════
# Les noms ci-dessous sont ceux des ROUTES, pas des canaux Telegram : la
# configuration ne porte que des chat_id, jamais de nom d'affichage. Les
# renommer plus tard est un UPDATE d'une ligne. Deduire « Citrique Alert BE »
# de la conversation plutot que de la configuration serait exactement le genre
# de deduction silencieuse qu'on s'interdit ici.
PRINCIPAL = "PRINCIPAL"
PREMIUM = "PREMIUM"
CRITIQUE = "CRITIQUE"

# La priorite ordonne l'evaluation. Elle importe pour UNE raison : le premium
# est exclusif au-dessus du critique (aujourd'hui `not _premium_takes`), et le
# principal doit etre evalue AVANT lui pour rester independant — c'est le cas
# actuel, ou le principal ne regarde ni le premium ni le critique.
_PRIORITES = {PRINCIPAL: 10, PREMIUM: 20, CRITIQUE: 30}


def depuis_config(cfg: Any) -> list[Canal]:
    """La configuration .env actuelle, exprimee en canaux.

    Fonction pure : ni base, ni reseau. Elle traduit, elle ne decide pas.

    Trois details valent la peine d'etre lus, parce que les rater ferait
    diverger l'ancien routage du nouveau sur des valeurs pile aux bornes :

      * le canal PRINCIPAL n'a AUCUNE contrainte de phase. Le code actuel ne
        teste `is_live` que pour le premium et le critique ; un pari dont le
        coup d'envoi est passe peut donc atteindre le chat principal, et cela
        doit rester vrai ;
      * sa borne haute d'EV est STRICTE (`ev < main_max_ev_pct`) alors que ses
        bornes de cote sont inclusives ;
      * la voie critique grosses cotes commence a `cote > critical_hi_min_odd`,
        strictement, pour ne pas empieter sur la bande premium qui inclut 4,00.
    """
    canaux: list[Canal] = []

    canaux.append(Canal(
        chat_id=cfg.chat_id, nom=PRINCIPAL, priorite=_PRIORITES[PRINCIPAL],
        regles=(Regle(
            ev_min=Borne(cfg.min_ev_pct),
            ev_max=Borne(cfg.main_max_ev_pct, stricte=True),
            odd_min=Borne(cfg.main_min_odd),
            odd_max=Borne(cfg.main_max_odd),
        ),),
    ))

    # Le premium n'existe que s'il est configure. Aujourd'hui `_premium_takes`
    # exige `cfg.effective_premium_chat_id` : sans canal premium, le pari n'est
    # pas « pris » et le critique peut le rattraper. Ne pas creer le canal
    # reproduit exactement cela.
    if cfg.effective_premium_chat_id:
        exclus = tuple(cfg.premium_hi_sports_exclus)
        hi_criteres = (
            (Critere("sport", frozenset(exclus), inclut=False),) if exclus else ()
        )
        canaux.append(Canal(
            chat_id=cfg.effective_premium_chat_id, nom=PREMIUM,
            priorite=_PRIORITES[PREMIUM],
            # Exclusif : c'est la traduction litterale de `not _premium_takes`
            # dans la condition du critique.
            exclusif=True,
            regles=(
                Regle(ev_min=Borne(cfg.min_premium_ev_pct),
                      odd_min=Borne(cfg.premium_min_odd),
                      odd_max=Borne(cfg.premium_max_odd),
                      phase="prematch"),
                Regle(ev_min=Borne(cfg.premium_hi_min_ev),
                      odd_min=Borne(cfg.premium_hi_min_odd),
                      odd_max=Borne(cfg.premium_hi_max_odd),
                      phase="prematch", criteres=hi_criteres),
            ),
        ))

    if cfg.effective_critical_chat_id:
        canaux.append(Canal(
            chat_id=cfg.effective_critical_chat_id, nom=CRITIQUE,
            priorite=_PRIORITES[CRITIQUE],
            regles=(
                Regle(ev_min=Borne(cfg.min_critical_ev_pct), phase="prematch"),
                Regle(ev_min=Borne(cfg.critical_hi_min_ev),
                      odd_min=Borne(cfg.critical_hi_min_odd, stricte=True),
                      phase="prematch"),
            ),
        ))
    return canaux


def installer(storage: Any, canaux: list[Canal], *, print_fn=print) -> list[str]:
    """Persiste des canaux. Rend les noms ecrits.

    ⚠️ Refuse d'ecraser : un nom deja present est SAUTE et signale. Aucune
    suppression implicite, jamais — cette fonction ne doit pas pouvoir effacer
    une configuration que quelqu'un a passe du temps a regler."""
    existants = {c["nom"] for c in storage.load_channel_rows()[0]}
    ecrits: list[str] = []
    for canal in canaux:
        if canal.nom in existants:
            print_fn(f"canal {canal.nom!r} deja present — laisse tel quel")
            continue
        cid = storage.create_channel(
            canal.chat_id, canal.nom, actif=canal.actif,
            priorite=canal.priorite, exclusif=canal.exclusif,
            profile_id=canal.profile_id)
        for regle in canal.regles:
            rid = storage.add_channel_rule(
                cid,
                ev_min=regle.ev_min.valeur if regle.ev_min else None,
                ev_min_strict=bool(regle.ev_min and regle.ev_min.stricte),
                ev_max=regle.ev_max.valeur if regle.ev_max else None,
                ev_max_strict=bool(regle.ev_max and regle.ev_max.stricte),
                odd_min=regle.odd_min.valeur if regle.odd_min else None,
                odd_min_strict=bool(regle.odd_min and regle.odd_min.stricte),
                odd_max=regle.odd_max.valeur if regle.odd_max else None,
                odd_max_strict=bool(regle.odd_max and regle.odd_max.stricte),
                phase=regle.phase,
            )
            for critere in regle.criteres:
                for valeur in sorted(critere.valeurs):
                    storage.add_rule_value(rid, critere.dimension, valeur,
                                           inclut=critere.inclut)
        ecrits.append(canal.nom)
    return ecrits


def charger(source: Any, *, print_fn=print) -> list[Canal]:
    """Les canaux de la base, prêts pour `routing.canaux_pour`.

    `source` est tout objet portant `load_channel_rows()` — un `Storage` en
    production, un double dans les tests."""
    lignes_canaux, lignes_regles, lignes_valeurs = source.load_channel_rows()

    valeurs_par_regle: dict[int, list] = {}
    for v in lignes_valeurs:
        valeurs_par_regle.setdefault(int(v["rule_id"]), []).append(v)

    regles_par_canal: dict[int, list] = {}
    for r in lignes_regles:
        regles_par_canal.setdefault(int(r["channel_id"]), []).append(r)

    canaux: list[Canal] = []
    for c in lignes_canaux:
        cid = int(c["id"])
        try:
            regles = tuple(
                Regle(
                    ev_min=_borne(r["ev_min"], r["ev_min_strict"]),
                    ev_max=_borne(r["ev_max"], r["ev_max_strict"]),
                    odd_min=_borne(r["odd_min"], r["odd_min_strict"]),
                    odd_max=_borne(r["odd_max"], r["odd_max_strict"]),
                    phase=r["phase"],
                    criteres=_criteres(valeurs_par_regle.get(int(r["id"]), [])),
                )
                for r in regles_par_canal.get(cid, [])
            )
        except ValueError as e:
            # Signalé, jamais jeté en silence : sans ce message, « ce canal
            # ne reçoit plus rien » et « ce canal n'a jamais rien reçu »
            # seraient indiscernables.
            print_fn(f"canal {c['nom']!r} (id {cid}) ignoré — règle invalide : {e}")
            continue
        canaux.append(Canal(
            chat_id=c["chat_id"],
            nom=c["nom"],
            regles=regles,
            actif=bool(c["actif"]),
            priorite=int(c["priorite"]),
            exclusif=bool(c["exclusif"]),
            profile_id=c["profile_id"],
        ))
    return canaux
