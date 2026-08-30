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
