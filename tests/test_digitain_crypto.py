"""Le déchiffreur Digitain tourne-t-il vraiment hors navigateur ?

Ces tests n'ont pas besoin d'une vraie réponse chiffrée : les codes d'erreur
du module suffisent à prouver que le mapping des exports minifiés est correct.
Un mauvais mapping ne produirait pas « Data too short » en dessous de 29
octets et « Authentication failed » au-dessus.
"""
from __future__ import annotations

import os
import pytest

WASM = os.getenv("DIGITAIN_WASM_TEST", "/tmp/dg.wasm")
pytestmark = pytest.mark.skipif(
    not os.path.exists(WASM),
    reason="binaire Digitain absent — récupérable dans l'onglet Network (filtre Wasm)",
)


def _dec():
    from src.scrapers.digitain_crypto import DigitainDecryptor
    return DigitainDecryptor(WASM)


def test_module_loads_without_a_browser():
    """Le point qui décide de l'architecture : si le module tourne seul, le
    scraper vit sur la VM et aucun troisième onglet permanent n'est requis."""
    assert _dec() is not None


def test_short_input_is_rejected_as_too_short():
    d = _dec()
    for n in (1, 4, 16, 28):
        with pytest.raises(ValueError, match="too short"):
            d.decrypt_bytes(os.urandom(n))


def test_long_garbage_fails_authentication_not_length():
    """29 octets suffisent à tenter l'authentification : l'en-tête fait donc
    28 octets, soit nonce 12 + tag 16 — ChaCha20-Poly1305 ou AES-GCM."""
    d = _dec()
    for n in (29, 64, 256):
        with pytest.raises(ValueError, match="Authentication failed"):
            d.decrypt_bytes(os.urandom(n))


def test_a_plain_response_passes_through_untouched():
    """Toutes les réponses ne sont pas chiffrées ; seules celles qui portent
    un champ `payload` le sont."""
    assert _dec().decrypt_response({"Data": [1, 2]}) == {"Data": [1, 2]}
