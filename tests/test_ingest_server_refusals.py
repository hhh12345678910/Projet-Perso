"""Serveur d'ingestion — tout refus doit laisser une trace.

`log_message` est volontairement muet : le serveur journalise lui-même, en
concis. Mais `do_GET` ne journalisait NI ses 401 NI ses 404, alors que
`do_POST` journalisait déjà ses 401. Conséquence mesurée le 21/08 sur le pont
résultats : trois autres ponts écrivaient en continu pendant que
`/scores-plan` restait totalement invisible, et rien dans le journal ne
permettait de distinguer

  - « le serveur a refusé le jeton »  (401, le userscript est mal configuré)
  - « la requête n'est jamais arrivée » (réseau, pare-feu, Tampermonkey)

Deux causes opposées, un seul symptôme : rien. C'est la panne dominante du
projet (§11, §13.12), appliquée au diagnostic lui-même.

Ces tests lancent le VRAI serveur sur un port éphémère et lisent ce qu'il
écrit. Tester la fonction de routage seule ne prouverait rien : c'est
justement l'absence d'appel à `_log` qui était le défaut.
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "betano_ingest_server.py"
TOKEN = "jeton-de-test-0123456789"


def _load_server(tmp_path):
    """Charge le module avec un jeton connu.

    Le jeton est lu à l'IMPORT (`TOKEN = os.getenv(...)`), donc il faut le
    poser avant de charger — le même piège que `provider_for` au §19.11.
    """
    os.environ["BETANO_INGEST_TOKEN"] = TOKEN
    os.environ["BETANO_INGEST_FILE"] = str(tmp_path / "betano.json")
    os.environ["SCORES_INGEST_DIR"] = str(tmp_path / "scores")
    spec = importlib.util.spec_from_file_location("ingest_srv_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def srv(tmp_path):
    mod = _load_server(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield mod, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(base, path, token=None):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post(base, path, body=b"{}", token=None):
    req = urllib.request.Request(base + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_un_mauvais_jeton_sur_scores_plan_est_journalise(srv, capfd):
    """LE cas du 21/08. Sans cette ligne, un jeton faux dans Tampermonkey est
    indiscernable d'un pont qui n'appelle pas."""
    _mod, base = srv
    code, _ = _get(base, "/scores-plan", token="mauvais-jeton")
    assert code == 401

    out = capfd.readouterr().out
    assert "401 unauthorized /scores-plan" in out
    # Le jeton fourni ne doit JAMAIS entrer dans le journal : il part dans
    # systemd et se relit à plusieurs.
    assert "mauvais-jeton" not in out


def test_un_mauvais_jeton_sur_magic_plan_est_journalise_aussi(srv, capfd):
    """Les deux routes GET protégées partagent le même défaut, donc le même
    correctif — et le même test, sinon l'une des deux régresse seule."""
    _mod, base = srv
    code, _ = _get(base, "/magic-plan?sport=soccer", token="mauvais-jeton")
    assert code == 401
    assert "401 unauthorized /magic-plan" in capfd.readouterr().out


def test_une_route_get_inconnue_est_journalisee(srv, capfd):
    """La signature d'un userscript qui appelle une route mal orthographiée.
    Aujourd'hui parfaitement silencieuse, donc introuvable."""
    _mod, base = srv
    code, _ = _get(base, "/scores-plans", token=TOKEN)   # le « s » de trop
    assert code == 404
    assert "404 GET /scores-plans" in capfd.readouterr().out


def test_une_route_post_inconnue_est_journalisee(srv, capfd):
    _mod, base = srv
    code, _ = _post(base, "/ingest-score", token=TOKEN)  # singulier, faux
    assert code == 404
    assert "404 POST /ingest-score" in capfd.readouterr().out


def test_le_bon_jeton_sert_le_plan_et_le_journalise(srv, capfd):
    """Le chemin nominal doit rester lisible : c'est la ligne qu'on cherche
    dans `journalctl` pour confirmer qu'un pont est vivant."""
    _mod, base = srv
    code, body = _get(base, "/scores-plan", token=TOKEN)
    assert code == 200
    assert isinstance(json.loads(body).get("fetch"), list)
    assert "200 scores-plan" in capfd.readouterr().out


def test_health_reste_muet_et_sans_jeton(srv, capfd):
    """`/health` sert aux tests de connectivité et peut être appelé en boucle :
    il ne doit ni exiger de jeton ni polluer le journal."""
    _mod, base = srv
    code, body = _get(base, "/health")
    assert code == 200 and "ok" in body
    assert capfd.readouterr().out == ""
