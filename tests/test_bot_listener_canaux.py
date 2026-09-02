"""Le branchement des canaux dans le bot — sans jamais parler a Telegram.

`tg()` est remplace par un espion : aucun test de ce fichier n'ouvre de
connexion. Ce qui est verifie ici, c'est ce que `bot_listener` ferait
PARTIR, pas ce que le module de canaux decide (couvert ailleurs).

⚠️ `test_les_trois_chemins_de_handle_message` existe pour une raison
precise : les imports de ce fichier sont LOCAUX aux fonctions, et un import
local rend le nom local a TOUTE la fonction, y compris sur les chemins qui
ne l'executent pas. Cette faute exacte a deja coute un plantage en
production (§22). Les trois chemins sont donc exerces.
"""
from __future__ import annotations

import pytest

import bot_listener as bl
from src.alerter import TelegramConfig
from src.storage import Storage

ADMIN = "531952352"
INTRUS = "999999999"


@pytest.fixture
def envois(monkeypatch, tmp_path):
    """Espionne `tg` et pointe le bot sur une base jetable."""
    appels: list[tuple] = []
    # `tg` rend TOUJOURS un dict (`{}` si la reponse est illisible). Une
    # sonde qui rend None ferait planter le code qui lit `r["ok"]` — et
    # masquerait le repli d'edition qu'on veut justement verifier.
    monkeypatch.setattr(bl, "tg",
                        lambda methode, **kw: (appels.append((methode, kw)),
                                               {"ok": True})[1])
    monkeypatch.setattr(bl, "DB_PATH", str(tmp_path / "t.db"))
    Storage(str(tmp_path / "t.db"))
    monkeypatch.delenv("TELEGRAM_ADMIN_ID", raising=False)
    monkeypatch.setattr(TelegramConfig, "from_env",
                        staticmethod(lambda: TelegramConfig(bot_token="j",
                                                            chat_id=ADMIN)))
    return appels


def _msg(texte, qui=ADMIN, chat=ADMIN):
    m = {"text": texte, "chat": {"id": chat}}
    if qui is not None:
        m["from"] = {"id": int(qui)}
    return m


def _cb(data, qui=ADMIN):
    return {"id": "cb1", "data": data, "from": {"id": int(qui)},
            "message": {"message_id": 7, "chat": {"id": ADMIN}}}


# ══ le chemin nominal ══════════════════════════════════════════════════
def test_une_commande_de_canaux_repond(envois):
    bl.handle_message(_msg("/canaux"))
    assert [m for m, _ in envois] == ["sendMessage"]
    assert "Aucun canal" in envois[0][1]["text"]


def test_creer_un_canal_depuis_telegram(envois, tmp_path):
    bl.handle_message(_msg("/nouveau TENNIS -100123"))
    from src.channels import charger
    canaux = charger(Storage(str(tmp_path / "t.db")))
    assert [c.nom for c in canaux] == ["TENNIS"]
    assert envois[0][1]["reply_markup"] is not None


def test_un_clic_edite_le_message_au_lieu_d_en_empiler_un(envois):
    bl.handle_message(_msg("/nouveau TENNIS -100123"))
    envois.clear()
    bl.handle_callback(_cb("cx:a:TENNIS"))
    methodes = [m for m, _ in envois]
    assert methodes == ["answerCallbackQuery", "editMessageText"]
    assert envois[1][1]["message_id"] == 7


# ══ AUTORISATION, cote bot ═════════════════════════════════════════════
def test_un_intrus_recoit_un_refus_et_n_ecrit_rien(envois, tmp_path):
    bl.handle_message(_msg("/nouveau PIRATE -100999", qui=INTRUS))
    assert "administrateur" in envois[0][1]["text"]
    st = Storage(str(tmp_path / "t.db"))
    assert st.load_channel_rows()[0] == []


def test_un_clic_d_intrus_ne_change_rien(envois, tmp_path):
    bl.handle_message(_msg("/nouveau TENNIS -100123"))
    envois.clear()
    bl.handle_callback(_cb("cx:xx:TENNIS", qui=INTRUS))
    from src.channels import charger
    assert len(charger(Storage(str(tmp_path / "t.db")))) == 1


def test_un_post_de_canal_sans_auteur_est_refuse(envois, tmp_path):
    """Dans un CANAL Telegram le message n'a pas d'auteur : la gestion y est
    impossible, et c'est voulu."""
    bl.handle_message(_msg("/nouveau PIRATE -100999", qui=None, chat="-100555"))
    assert "administrateur" in envois[0][1]["text"]
    assert Storage(str(tmp_path / "t.db")).load_channel_rows()[0] == []


# ══ ce qui ne doit pas etre avale ══════════════════════════════════════
def test_les_trois_chemins_de_handle_message(envois, monkeypatch):
    """Les imports sont LOCAUX : chaque chemin doit fonctionner seul.

    Chemin 1 : une commande de canaux. Chemin 2 : /scan, qui importe le
    MEME `TelegramConfig` plus bas dans la fonction. Chemin 3 : une commande
    inconnue, qui n'importe rien. Un `UnboundLocalError` sur l'un d'eux
    n'apparaitrait que la."""
    bl.handle_message(_msg("/canaux"))                       # 1
    assert envois, "la commande de canaux n'a rien produit"

    envois.clear()
    monkeypatch.setattr(bl, "_allowed_chats", lambda cfg: set())
    bl.handle_message(_msg("/scan"))                          # 2 — refuse, mais
    assert envois == [], "/scan ne doit rien envoyer a un chat non autorise"

    envois.clear()
    bl.handle_message(_msg("/inconnue"))                      # 3
    assert envois == []


def test_le_bouton_jouer_n_est_pas_intercepte(envois, monkeypatch):
    vus = []
    monkeypatch.setattr(bl, "already_logged", lambda t: vus.append(t) or True)
    monkeypatch.setattr(bl, "_mark_button_done", lambda cb, label="x": None)
    bl.handle_callback(_cb("play:jeton123"))
    assert vus == ["jeton123"], "le routage des boutons a avale play:"


def test_le_bouton_book_n_est_pas_intercepte(envois, monkeypatch):
    vus = []
    monkeypatch.setattr(bl, "handle_book_toggle", lambda cb: vus.append(cb))
    bl.handle_callback(_cb("bookalert:unibet_be"))
    assert len(vus) == 1


# ══ le bouton qui « ne marche pas » ════════════════════════════════════
def test_une_edition_refusee_produit_quand_meme_un_message(monkeypatch, tmp_path):
    """Telegram refuse l'edition pour des raisons banales — message trop
    ancien, ou contenu identique (« message is not modified »). Sans repli,
    le bouton paraissait mort : c'est le « retour qui ne marche pas
    toujours »."""
    appels: list[tuple] = []

    def _tg(methode, **kw):
        appels.append((methode, kw))
        if methode == "editMessageText":
            return {"ok": False, "description": "Bad Request: message is not modified"}
        return {"ok": True}

    monkeypatch.setattr(bl, "tg", _tg)
    monkeypatch.setattr(bl, "DB_PATH", str(tmp_path / "t.db"))
    Storage(str(tmp_path / "t.db"))
    monkeypatch.delenv("TELEGRAM_ADMIN_ID", raising=False)
    monkeypatch.setattr(TelegramConfig, "from_env",
                        staticmethod(lambda: TelegramConfig(bot_token="j",
                                                            chat_id=ADMIN)))
    bl.handle_message(_msg("/nouveau TENNIS -100123"))
    appels.clear()
    bl.handle_callback(_cb("cx:a:TENNIS"))
    methodes = [m for m, _ in appels]
    assert methodes == ["answerCallbackQuery", "editMessageText", "sendMessage"]
    assert "TENNIS" in appels[-1][1]["text"]


def test_un_callback_sans_message_ne_plante_pas(envois):
    """Au-dela de 48 h, Telegram n'attache plus le message au clic."""
    bl.handle_message(_msg("/nouveau TENNIS -100123"))
    envois.clear()
    bl.handle_callback({"id": "cb2", "data": "cx:a:TENNIS",
                        "from": {"id": int(ADMIN)}})
    assert [m for m, _ in envois] == ["answerCallbackQuery"]
