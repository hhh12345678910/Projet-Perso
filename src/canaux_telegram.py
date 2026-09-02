"""L'interface Telegram des canaux : ce qu'il faut REPONDRE, jamais l'envoi.

Ce module ne parle a personne. Il recoit une commande ou un clic, lit et
ecrit la base, et rend un couple (texte, clavier). C'est `bot_listener` qui
envoie. Deux raisons, et la seconde est la vraie :

  * tout se teste sans reseau, y compris l'autorisation ;
  * une rafale Telegram devient impossible depuis ici. Le module ne PEUT
    pas envoyer, donc aucune boucle mal ecrite ne peut inonder un canal.

⚠️ L'AUTORISATION EST DANS CE MODULE, pas seulement chez l'appelant.
`commande()` et `bouton()` verifient l'identite AVANT toute lecture ou
ecriture. Un appelant qui oublierait le controle n'ouvrirait rien : c'est
la seule facon d'etre sur que la garde ne peut pas etre contournee par
distraction.

Qui est administrateur
----------------------
Le projet n'avait AUCUNE notion d'utilisateur : `_allowed_chats` filtre par
CHAT, et les boutons n'etaient pas verifies du tout. Ici on lit
`from.id` — l'auteur reel du message ou du clic.

L'administrateur est `TELEGRAM_ADMIN_ID` s'il est defini, sinon
`TELEGRAM_CHAT_ID` quand c'est un chat PRIVE (identifiant positif) : dans
un chat prive, l'identifiant du chat EST celui de l'utilisateur. Si le chat
principal est un groupe (identifiant negatif), aucun administrateur n'est
deduit et TOUTE commande de gestion est refusee — mieux vaut une interface
inutilisable qu'une interface ouverte a tous les membres du groupe.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Optional

from src.channels import charger
from src.models import Book, MarketType
from src.routing import canaux_pour

DIMENSIONS = ("sport", "book", "market")     # league : par commande texte
PHASES = (None, "prematch", "live")
MARQUE = {None: "☐", True: "✅", False: "⛔"}

# Paliers proposes, MINIMUM et MAXIMUM separement. La premiere version
# n'offrait que des couples figes (« EV >= 10 », « cote 1,5-4 ») : impossible
# d'y composer « EV entre 12 et 25 » sans passer par une commande texte que
# personne ne devine. Chaque bouton ne touche desormais QUE sa borne et laisse
# l'autre en place.
EV_MIN = (None, 5.0, 8.0, 10.0, 15.0, 20.0, 35.0)
EV_MAX = (None, 8.0, 10.0, 15.0, 20.0, 35.0, 50.0)
COTE_MIN = (None, 1.3, 1.5, 1.8, 2.0, 2.5, 3.0, 4.0)
COTE_MAX = (None, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 10.0)


@dataclass(frozen=True)
class Reponse:
    """Ce qu'il faut afficher. `clavier=None` = message simple.
    `edite=True` = remplacer le message du bouton plutot qu'en poster un
    nouveau : sans ca, chaque clic empilerait un message de plus."""
    texte: str
    clavier: Optional[dict] = None
    edite: bool = False
    alerte: Optional[str] = None      # bulle de confirmation d'un clic


def admin_id(cfg: Any) -> Optional[str]:
    explicite = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
    if explicite:
        return explicite
    principal = str(getattr(cfg, "chat_id", "") or "").strip()
    # Un identifiant negatif est un groupe ou un canal : on n'en deduit
    # AUCUN administrateur.
    return principal if principal.lstrip("-").isdigit() and not principal.startswith("-") else None


def est_admin(id_utilisateur: Any, cfg: Any) -> bool:
    attendu = admin_id(cfg)
    return bool(attendu) and str(id_utilisateur or "") == attendu


# ══ rendu ══════════════════════════════════════════════════════════════
def _borne(b, signe: str) -> str:
    return "" if b is None else f"{signe}{'' if b.stricte else '='} {b.valeur:g}"


def decrire_regle(regle, numero: int) -> str:
    bouts = [x for x in (_borne(regle.ev_min, "EV >"), _borne(regle.ev_max, "EV <"),
                         _borne(regle.odd_min, "cote >"), _borne(regle.odd_max, "cote <"))
             if x]
    if regle.phase:
        bouts.append(regle.phase)
    for c in regle.criteres:
        bouts.append(f"{_NOM_DIM.get(c.dimension, c.dimension)} "
                     f"{'∈' if c.inclut else '∉'} "
                     f"{{{', '.join(joli(v) for v in sorted(c.valeurs))}}}")
    return f"  <b>{numero}.</b> " + (" ET ".join(bouts) or "<i>tout passe</i>")


def decrire_canal(canal, ids_regles) -> str:
    etat = "✅ actif" if canal.actif else "☐ coupé"
    lignes = [f"<b>{canal.nom}</b> — {etat}",
              f"chat <code>{canal.chat_id}</code> · priorité {canal.priorite}"
              + (" · exclusif" if canal.exclusif else "")]
    if not canal.regles:
        lignes.append("\n<i>Aucune règle — ce canal ne reçoit RIEN.</i>")
    else:
        lignes.append("\nRègles (l'une OU l'autre) :")
        lignes += [decrire_regle(r, i + 1) for i, r in enumerate(canal.regles)]
    return "\n".join(lignes)


# Sigles a laisser en capitales. Le reste est simplement capitalise :
# `unibet_be` -> « Unibet BE », `golden_palace` -> « Golden Palace ».
#
# Volontairement une REGLE et non une table de noms. `alerter` en tient
# deja une (_BOOK_NAMES) et la recopier ici la ferait diverger au premier
# book ajoute ; l'importer casserait l'isolement qui garantit que ce module
# ne peut pas envoyer de message.
_SIGLES = {"be", "h2h", "btts", "h1", "1h", "fr", "nl", "uk"}

# Les dimensions portent des noms techniques (`market`, `league`) qui n'ont
# aucune raison d'apparaitre a l'ecran.
_NOM_DIM = {"sport": "Sport", "book": "Book", "market": "Marché",
            "league": "Compétition"}


def joli(valeur: str) -> str:
    return " ".join(m.upper() if m.lower() in _SIGLES else m.capitalize()
                    for m in str(valeur).split("_"))


def _bouton(t, d):
    return {"text": t, "callback_data": d}


def clavier_canal(canal, ids_regles) -> dict:
    rows = [[_bouton("☐ Couper" if canal.actif else "✅ Activer", f"cx:a:{canal.nom}"),
             _bouton("🧪 Tester", f"cx:test:{canal.nom}")]]
    for i, rid in enumerate(ids_regles):
        rows.append([_bouton(f"— règle {i + 1} —", "cx:noop")])
        rows.append([_bouton("Sport", f"cx:d:{rid}:sport"),
                     _bouton("Book", f"cx:d:{rid}:book"),
                     _bouton("Marché", f"cx:d:{rid}:market")])
        rows.append([_bouton("EV", f"cx:ev:{rid}"), _bouton("Cote", f"cx:co:{rid}"),
                     _bouton("Phase", f"cx:ph:{rid}"),
                     _bouton("🗑", f"cx:r-:{rid}")])
    rows.append([_bouton("➕ Ajouter une règle", f"cx:r+:{canal.nom}")])
    rows.append([_bouton("🗑 Supprimer le canal", f"cx:x:{canal.nom}")])
    return {"inline_keyboard": rows}


def clavier_dimension(rid: int, dim: str, valeurs: list[str], etats: dict) -> dict:
    rows, cur = [], []
    for v in valeurs:
        cur.append(_bouton(f"{MARQUE[etats.get(v)]} {joli(v)[:18]}",
                           f"cx:t:{rid}:{dim}:{v}"))
        if len(cur) == 2:
            rows.append(cur); cur = []
    if cur:
        rows.append(cur)
    rows.append([_bouton("← retour", f"cx:vr:{rid}")])
    return {"inline_keyboard": rows}


def _libelle_borne(mini, maxi, unite: str) -> str:
    if mini is None and maxi is None:
        return "aucune borne"
    if maxi is None:
        return f"{unite} ≥ {mini:g}"
    if mini is None:
        return f"{unite} ≤ {maxi:g}"
    return f"{unite} {mini:g}–{maxi:g}"


def _rangee(rid: int, quoi: str, bord: str, valeurs, actuelle) -> list[list[dict]]:
    """Une ligne de paliers. La borne posee est marquee, pour qu'on voie
    d'un coup d'oeil ce qui est actif sans relire le texte."""
    rows, cur = [], []
    for v in valeurs:
        pose = (v is None and actuelle is None) or (
            actuelle is not None and v is not None and actuelle.valeur == v)
        libelle = "aucun" if v is None else f"{v:g}"
        cur.append(_bouton(("• " if pose else "") + libelle,
                           f"cx:{quoi}{bord}:{rid}:{'' if v is None else f'{v:g}'}"))
        if len(cur) == 4:
            rows.append(cur); cur = []
    if cur:
        rows.append(cur)
    return rows


def clavier_bornes(rid: int, quoi: str, regle=None) -> dict:
    """Deux rangees independantes : minimum, puis maximum."""
    if quoi == "ev":
        mini, maxi = (regle.ev_min if regle else None), (regle.ev_max if regle else None)
        vmin, vmax, unite = EV_MIN, EV_MAX, "EV"
    else:
        mini, maxi = (regle.odd_min if regle else None), (regle.odd_max if regle else None)
        vmin, vmax, unite = COTE_MIN, COTE_MAX, "cote"
    rows = [[_bouton(f"── {unite} minimum ──", "cx:noop")]]
    rows += _rangee(rid, quoi, "min", vmin, mini)
    rows.append([_bouton(f"── {unite} maximum ──", "cx:noop")])
    rows += _rangee(rid, quoi, "max", vmax, maxi)
    rows.append([_bouton("✏️ Valeur exacte", f"cx:{quoi}x:{rid}"),
                 _bouton("← retour", f"cx:vr:{rid}")])
    return {"inline_keyboard": rows}


# ══ lecture de l'etat ══════════════════════════════════════════════════
def _canal_et_regles(storage, nom: str):
    """Le canal charge, et les ID de ses regles dans le MEME ordre.

    `charger` rend des objets sans identifiant — c'est voulu, le modele de
    decision n'a que faire des cles primaires. Mais les boutons, eux, ont
    besoin de designer une regle. On relit donc les lignes brutes et on
    s'appuie sur l'ordre commun (`ORDER BY channel_id, id`)."""
    for c in charger(storage, print_fn=lambda _s: None):
        if c.nom.lower() == nom.lower():
            lignes_c, lignes_r, _ = storage.load_channel_rows()
            cid = next((int(x["id"]) for x in lignes_c if x["nom"] == c.nom), None)
            ids = [int(r["id"]) for r in lignes_r if int(r["channel_id"]) == cid]
            return c, ids, cid
    return None, [], None


def _canal_de_regle(storage, rid: int):
    lignes_c, lignes_r, _ = storage.load_channel_rows()
    cid = next((int(r["channel_id"]) for r in lignes_r if int(r["id"]) == rid), None)
    nom = next((x["nom"] for x in lignes_c if int(x["id"]) == cid), None)
    return nom


def _etats_dimension(storage, rid: int, dim: str) -> dict:
    _, _, valeurs = storage.load_channel_rows()
    return {v["valeur"]: bool(v["inclut"]) for v in valeurs
            if int(v["rule_id"]) == rid and v["dimension"] == dim}


def _valeurs_possibles(storage, dim: str) -> list[str]:
    if dim == "sport":
        return storage.sports_seen() or ["soccer", "tennis", "basketball"]
    if dim == "book":
        return storage.books_seen() or [b.value for b in Book if b != Book.PINNACLE]
    # L'enum porte des marches que le projet sait lire mais ne detecte pas
    # forcement ici. Proposer BTTS ou un handicap qui n'arrive jamais ferait
    # croire au filtre qu'on vient de poser.
    return storage.markets_seen() or [MarketType.H2H.value, MarketType.TOTALS.value]


def _vue(storage, nom: str, *, edite: bool, alerte=None) -> Reponse:
    canal, ids, _ = _canal_et_regles(storage, nom)
    if canal is None:
        return Reponse(f"Canal <b>{nom}</b> introuvable.")
    return Reponse(decrire_canal(canal, ids), clavier_canal(canal, ids),
                   edite=edite, alerte=alerte)


# ══ commandes texte ════════════════════════════════════════════════════
_AIDE = (
    "<b>Canaux</b>\n"
    "/canaux — la liste\n"
    "/nouveau &lt;nom&gt; &lt;chat_id&gt; — créer un canal (coupé, sans règle)\n"
    "/canal &lt;nom&gt; — voir et configurer\n"
    "/test &lt;nom&gt; — rejouer les dernières détections, <b>sans rien envoyer</b>\n"
    "\nValeurs exactes (le clavier ne propose que des paliers) :\n"
    "/canal &lt;nom&gt; &lt;n° règle&gt; ev &lt;min&gt; [max]\n"
    "/canal &lt;nom&gt; &lt;n° règle&gt; cote &lt;min&gt; [max]\n"
    "/canal &lt;nom&gt; &lt;n° règle&gt; league &lt;nom exact&gt;\n"
    "/canal &lt;nom&gt; &lt;n° règle&gt; -league &lt;nom exact&gt;")

REFUS = "Commande réservée à l'administrateur."


def _nombre(x) -> Optional[float]:
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


def commande(texte: str, *, storage, cfg, id_utilisateur) -> Optional[Reponse]:
    """Traite une commande texte. `None` = ce n'est pas pour nous.

    L'autorisation est verifiee ICI, avant toute lecture ou ecriture."""
    mots = (texte or "").strip().split()
    if not mots:
        return None
    cmd = mots[0].split("@", 1)[0].lower()
    if cmd not in ("/canaux", "/nouveau", "/canal", "/test"):
        return None
    if not est_admin(id_utilisateur, cfg):
        return Reponse(REFUS)

    if cmd == "/canaux":
        canaux = charger(storage, print_fn=lambda _s: None)
        if not canaux:
            return Reponse("Aucun canal configuré.\n\n" + _AIDE)
        lignes = []
        for c in canaux:
            lignes.append(f"{'✅' if c.actif else '☐'} <b>{c.nom}</b> — "
                          f"{len(c.regles)} règle(s), priorité {c.priorite}"
                          + (", exclusif" if c.exclusif else ""))
        return Reponse("\n".join(lignes) + "\n\n" + _AIDE)

    if cmd == "/nouveau":
        if len(mots) < 3:
            return Reponse("Usage : /nouveau &lt;nom&gt; &lt;chat_id&gt;\n\n" + _AIDE)
        nom, chat = mots[1], mots[2]
        if storage.find_channel_by_name(nom) is not None:
            return Reponse(f"Un canal <b>{nom}</b> existe déjà.")
        # Cree COUPE et SANS REGLE : un canal actif sans regle ne recevrait
        # rien de toute facon, mais un canal actif dont on pose la premiere
        # regle se mettrait a emettre au milieu de la configuration.
        storage.create_channel(chat, nom, actif=False, priorite=50)
        return _vue(storage, nom, edite=False)

    nom = mots[1] if len(mots) > 1 else ""
    if cmd == "/test":
        return _tester(storage, nom) if nom else Reponse("Usage : /test &lt;nom&gt;")
    if not nom:
        return Reponse(_AIDE)
    if len(mots) <= 2:
        return _vue(storage, nom, edite=False)
    return _sous_commande(storage, nom, mots[2:])


def _sous_commande(storage, nom: str, args: list[str]) -> Reponse:
    canal, ids, _ = _canal_et_regles(storage, nom)
    if canal is None:
        return Reponse(f"Canal <b>{nom}</b> introuvable.")
    try:
        numero = int(args[0])
    except (ValueError, IndexError):
        return Reponse("Il faut le numéro de la règle.\n\n" + _AIDE)
    if not 1 <= numero <= len(ids):
        return Reponse(f"Ce canal a {len(ids)} règle(s).")
    rid = ids[numero - 1]
    quoi = args[1].lower() if len(args) > 1 else ""

    if quoi in ("ev", "cote"):
        mini = _nombre(args[2]) if len(args) > 2 else None
        maxi = _nombre(args[3]) if len(args) > 3 else None
        if mini is None and maxi is None:
            return Reponse(f"Usage : /canal {nom} {numero} {quoi} &lt;min&gt; [max]")
        p = "ev" if quoi == "ev" else "odd"
        storage.update_channel_rule(rid, **{f"{p}_min": mini, f"{p}_max": maxi})
        return _vue(storage, nom, edite=False)

    if quoi in ("league", "-league"):
        valeur = " ".join(args[2:]).strip()
        if not valeur:
            return Reponse(f"Usage : /canal {nom} {numero} league &lt;nom exact&gt;")
        # Correspondance EXACTE, jamais partielle : un filtre qui attrape
        # plus que ce qu'on a ecrit est un filtre qu'on ne relit jamais.
        storage.add_rule_value(rid, "league", valeur, inclut=(quoi == "league"))
        return _vue(storage, nom, edite=False)

    return Reponse(_AIDE)


# ══ le mode test : AUCUN envoi, jamais ═════════════════════════════════
def _tester(storage, nom: str, *, limite: int = 500) -> Reponse:
    """Rejoue les dernieres detections dans les regles d'UN canal.

    N'envoie rien et ne peut rien envoyer : ce module n'a aucun moyen de
    parler a Telegram. C'est la reponse a « ce filtre va-t-il assecher le
    canal ou l'inonder ? », question qu'on ne pouvait trancher qu'apres
    coup — donc trop tard."""
    canal, _, _ = _canal_et_regles(storage, nom)
    if canal is None:
        return Reponse(f"Canal <b>{nom}</b> introuvable.")
    lignes = storage.dernieres_detections(limite=limite)
    if not lignes:
        return Reponse("Aucune détection en base : rien à rejouer.")
    # On rejoue le canal comme s'il etait ACTIF : `canaux_pour` ecarte les
    # canaux coupes, et un canal coupe est justement celui qu'on veut
    # eprouver AVANT de l'allumer. Sans ce forcage, /test rendrait toujours
    # zero sur un canal neuf — la seule reponse qui n'apprend rien.
    sonde = replace(canal, actif=True, exclusif=False)
    retenus = []
    for bet, sport, league, live in lignes:
        if canaux_pour(bet, sport=sport, league=league, is_live=live,
                       canaux=[sonde]):
            retenus.append((bet, sport))
    part = 100 * len(retenus) / len(lignes)
    txt = [f"🧪 <b>{canal.nom}</b> — rejeu de {len(lignes)} détections",
           f"retenues : <b>{len(retenus)}</b> ({part:.1f} %)",
           "<i>Aucun message n'a été envoyé.</i>"]
    if not canal.actif:
        txt.append("⚠️ Ce canal est <b>coupé</b> : il ne recevrait rien même ainsi.")
    if retenus:
        txt.append("\nExemples :")
        for bet, sport in retenus[:5]:
            txt.append(f"  EV {bet.ev_pct:.1f} cote {bet.odd_taken} "
                       f"[{sport or '?'}] {bet.book.value} {bet.market.value}")
    return Reponse("\n".join(txt))


# ══ boutons ════════════════════════════════════════════════════════════
def bouton(data: str, *, storage, cfg, id_utilisateur) -> Optional[Reponse]:
    """Traite un clic. `None` = ce n'est pas pour nous.

    Comme `commande`, l'autorisation est verifiee ICI. Les boutons etaient
    le trou du bot existant : `handle_callback` ne verifiait rien, donc
    quiconque pouvait atteindre un clavier agissait."""
    if not (data or "").startswith("cx:"):
        return None
    if not est_admin(id_utilisateur, cfg):
        return Reponse(REFUS, alerte=REFUS)
    bouts = data.split(":")
    verbe = bouts[1] if len(bouts) > 1 else ""
    arg = bouts[2] if len(bouts) > 2 else ""

    if verbe == "noop":
        return Reponse("", alerte=None)

    if verbe == "a":                                   # activer / couper
        canal, _, cid = _canal_et_regles(storage, arg)
        if canal is None:
            return Reponse(f"Canal <b>{arg}</b> introuvable.")
        storage.set_channel_active(cid, not canal.actif)
        return _vue(storage, arg, edite=True,
                    alerte="Canal activé" if not canal.actif else "Canal coupé")

    if verbe == "test":
        return _tester(storage, arg)

    if verbe == "r+":                                  # ajouter une regle
        canal, _, cid = _canal_et_regles(storage, arg)
        if canal is None:
            return Reponse(f"Canal <b>{arg}</b> introuvable.")
        storage.add_channel_rule(cid)
        return _vue(storage, arg, edite=True, alerte="Règle ajoutée")

    if verbe == "r-":                                  # supprimer une regle
        nom = _canal_de_regle(storage, int(arg))
        if nom is None:
            return Reponse("Règle introuvable.")
        storage.delete_channel_rule(int(arg))
        return _vue(storage, nom, edite=True, alerte="Règle supprimée")

    if verbe == "vr":                                  # retour depuis une regle
        # Une regle peut avoir disparu depuis que ce clavier a ete affiche —
        # Telegram garde les anciens messages cliquables indefiniment. Le
        # retour doit alors ramener QUELQUE PART, pas laisser l'ecran mort.
        nom = _canal_de_regle(storage, int(arg))
        if nom:
            return _vue(storage, nom, edite=True)
        return Reponse("Cette règle n'existe plus.\n\n"
                       + _liste(storage), edite=True,
                       alerte="Règle supprimée entre-temps")

    if verbe == "d":                                   # ouvrir une dimension
        dim = bouts[3] if len(bouts) > 3 else ""
        if dim not in DIMENSIONS:
            return Reponse("Dimension inconnue.")
        rid = int(arg)
        etats = _etats_dimension(storage, rid, dim)
        valeurs = sorted(set(_valeurs_possibles(storage, dim)) | set(etats))
        return Reponse(
            f"<b>{_NOM_DIM.get(dim, dim)}</b> — ☐ absent (tous), ✅ inclus, "
            f"⛔ exclu\n<i>Un clic fait tourner les trois états.</i>",
            clavier_dimension(rid, dim, valeurs, etats), edite=True)

    if verbe == "t":                                   # basculer une valeur
        dim, valeur = bouts[3], ":".join(bouts[4:])
        if dim not in DIMENSIONS:
            return Reponse("Dimension inconnue.")
        rid = int(arg)
        etats = _etats_dimension(storage, rid, dim)
        actuel = etats.get(valeur)
        # absent -> inclus -> exclu -> absent. Un seul bouton pour les trois
        # etats : deux boutons par valeur rendraient le clavier illisible.
        if actuel is None:
            storage.add_rule_value(rid, dim, valeur, inclut=True)
        elif actuel is True:
            storage.add_rule_value(rid, dim, valeur, inclut=False)
        else:
            storage.delete_rule_value(rid, dim, valeur)
        etats = _etats_dimension(storage, rid, dim)
        valeurs = sorted(set(_valeurs_possibles(storage, dim)) | set(etats))
        return Reponse(
            f"<b>{_NOM_DIM.get(dim, dim)}</b> — ☐ absent (tous), ✅ inclus, "
            f"⛔ exclu\n<i>Un clic fait tourner les trois états.</i>",
            clavier_dimension(rid, dim, valeurs, etats), edite=True,
            alerte=f"{joli(valeur)} : {MARQUE[etats.get(valeur)]}")

    if verbe in ("ev", "co"):                          # ouvrir les paliers
        return _ecran_bornes(storage, int(arg), verbe, edite=True)

    if verbe in ("evmin", "evmax", "comin", "comax"):  # poser UNE borne
        rid = int(arg)
        valeur = _nombre(bouts[3]) if len(bouts) > 3 else None
        quoi = "ev" if verbe.startswith("ev") else "co"
        colonne = ("ev" if quoi == "ev" else "odd") + \
                  ("_min" if verbe.endswith("min") else "_max")
        # UNE seule colonne : l'autre borne reste ou elle est. C'est tout
        # l'objet de la refonte — composer un intervalle en deux clics.
        storage.update_channel_rule(rid, **{colonne: valeur})
        return _ecran_bornes(storage, rid, quoi, edite=True,
                             alerte=f"{colonne} = "
                                    f"{'aucun' if valeur is None else f'{valeur:g}'}")

    if verbe in ("evx", "cox"):                        # saisie libre
        rid = int(arg)
        nom = _canal_de_regle(storage, rid)
        canal, ids, _ = _canal_et_regles(storage, nom or "")
        numero = ids.index(rid) + 1 if canal and rid in ids else 1
        quoi = "ev" if verbe == "evx" else "cote"
        return Reponse(
            f"Valeur exacte — copie la ligne, change les nombres, envoie-la :\n\n"
            f"<code>/canal {nom} {numero} {quoi} 12.5 27.5</code>\n\n"
            f"<i>Le second nombre est facultatif. « /canal {nom} {numero} "
            f"{quoi} 12.5 » ne pose que le minimum.</i>",
            alerte="Commande à copier")

    if verbe == "ph":                                  # cycler la phase
        rid = int(arg)
        canal, ids, _ = _canal_et_regles(storage, _canal_de_regle(storage, rid) or "")
        if canal is None or rid not in ids:
            return Reponse("Règle introuvable.")
        actuelle = canal.regles[ids.index(rid)].phase
        suivante = PHASES[(PHASES.index(actuelle) + 1) % len(PHASES)]
        storage.update_channel_rule(rid, phase=suivante)
        return _vue(storage, canal.nom, edite=True,
                    alerte=f"phase : {suivante or 'prématch ET live'}")

    if verbe == "x":                                   # demander suppression
        return Reponse(
            f"Supprimer le canal <b>{arg}</b> et toutes ses règles ?\n"
            f"<i>Irréversible.</i>",
            {"inline_keyboard": [[_bouton("🗑 Oui, supprimer", f"cx:xx:{arg}"),
                                  _bouton("← Annuler", f"cx:v:{arg}")]]}, edite=True)

    if verbe == "xx":                                  # confirmer
        canal, _, cid = _canal_et_regles(storage, arg)
        if canal is None:
            return Reponse(f"Canal <b>{arg}</b> introuvable.")
        storage.delete_channel(cid)
        return Reponse(f"Canal <b>{arg}</b> supprimé.", edite=True,
                       alerte="Supprimé")

    if verbe == "v":
        return _vue(storage, arg, edite=True)
    # Un clavier d'une version anterieure porte des verbes qui n'existent
    # plus. Ne rien renvoyer donnerait un bouton qui « ne marche pas » sans
    # explication — exactement le symptome qu'on cherche a supprimer.
    return Reponse("", alerte="Clavier périmé — relance /canaux")


def _liste(storage) -> str:
    """Le repli quand on ne sait plus quoi afficher."""
    canaux = charger(storage, print_fn=lambda _s: None)
    if not canaux:
        return "Aucun canal configuré."
    return "\n".join(f"{'✅' if c.actif else '☐'} <b>{c.nom}</b>" for c in canaux)


def _ecran_bornes(storage, rid: int, quoi: str, *, edite: bool,
                  alerte=None) -> Reponse:
    nom = _canal_de_regle(storage, rid)
    canal, ids, _ = _canal_et_regles(storage, nom or "")
    if canal is None or rid not in ids:
        return Reponse("Cette règle n'existe plus.\n\n" + _liste(storage),
                       edite=edite, alerte="Règle supprimée entre-temps")
    regle = canal.regles[ids.index(rid)]
    unite = "EV" if quoi == "ev" else "cote"
    mini = regle.ev_min if quoi == "ev" else regle.odd_min
    maxi = regle.ev_max if quoi == "ev" else regle.odd_max
    actuel = _libelle_borne(None if mini is None else mini.valeur,
                            None if maxi is None else maxi.valeur, unite)
    return Reponse(
        f"<b>{unite}</b> — actuellement : {actuel}\n"
        f"<i>Chaque bouton ne change que SA borne ; l'autre reste.</i>",
        clavier_bornes(rid, quoi, regle), edite=edite, alerte=alerte)
