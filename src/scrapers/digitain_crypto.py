"""Déchiffrement des réponses Digitain (MagicBetting), via leur propre WASM.

Le §15.6 avait buté sur des payloads chiffrés : `{"payload": "…", "timestamp": …}`,
entropie 7,98 bits/octet, ni gzip ni zlib ni deflate, pas un chiffrement par
blocs, et une clé qui ne dérive pas du timestamp. Les tentatives échouaient
parce qu'elles s'attaquaient au mauvais objet.

Ce que `request.js` a révélé
----------------------------
Le déchiffrement n'est ni en JavaScript ni un XOR : c'est un module
**WebAssembly** chargé par `window.createDecryptor()`, avec une interface C
minuscule. Ses codes d'erreur décrivent l'algorithme :

    -1  Data too short
    -2  Authentication failed (wrong key or corrupted)   -> AEAD, donc tag
    -3  Decompression failed                             -> clair compressé
    -4  Memory allocation failed

Et les chaînes du binaire contiennent les messages d'erreur d'inflate
(« invalid literal/lengths set », « incorrect header check »). La chaîne
complète est donc :

    base64  ->  AEAD (nonce 12 + tag 16)  ->  inflate  ->  JSON

D'où l'échec du §15.6 : gzip/zlib étaient testés sur le CHIFFRÉ, alors que la
compression est à l'intérieur.

Pourquoi on n'extrait pas la clé
--------------------------------
Inutile. Le module est autonome — mesuré : aucun de ses cinq imports n'est
appelé pendant un déchiffrement — donc on le charge tel quel sous `wasmtime`
et on appelle ses fonctions. On utilise leur déchiffreur comme une boîte
noire, ce qui évite à la fois la rétro-ingénierie de la clé ET un troisième
onglet de navigateur permanent (§7, contrainte 1).

Le binaire est minifié : ses exports s'appellent `f` à `q`. Le mapping est
déduit des signatures et de l'ordre des `cwrap` dans `request.js`, puis
CONFIRMÉ par les codes d'erreur — des octets aléatoires rendent « Data too
short » en dessous de 29 octets et « Authentication failed » au-dessus, ce
qu'un mauvais mapping ne produirait pas.

⚠️ Le WASM est servi sous un chemin versionné (`/version/{version}/…`, voir
`linuxLoader.js`). Une mise à jour de leur site peut donc changer le binaire,
et rien ne garantit que la clé survive. Le fichier est à re-télécharger si le
déchiffrement se met à rendre « Authentication failed » sur tout.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

# Emplacement par défaut du binaire, récupéré depuis l'onglet Network du
# navigateur (filtre Wasm) — il n'est pas téléchargeable depuis la VM tant que
# Cloudflare y refuse les IP de datacenter.
WASM_PATH = os.getenv("DIGITAIN_WASM", "data/digitain_decrypt.wasm")

_ERRORS = {
    -1: "Data too short",
    -2: "Authentication failed (wrong key or corrupted)",
    -3: "Decompression failed",
    -4: "Memory allocation failed",
}


class DigitainDecryptor:
    """Charge le WASM une fois et déchiffre autant de payloads qu'on veut.

    Non thread-safe : le module garde son résultat dans un tampon global
    (`wasm_get_result` / `wasm_free_result`), donc deux déchiffrements
    simultanés se marcheraient dessus. Un verrou suffit si besoin."""

    def __init__(self, wasm_path: str | Path = WASM_PATH):
        from wasmtime import Engine, FuncType, Func, Instance, Module, Store, ValType

        path = Path(wasm_path)
        if not path.exists():
            raise FileNotFoundError(
                f"WASM Digitain introuvable : {path}. Récupère-le dans l'onglet "
                "Network du navigateur (filtre Wasm) et dépose-le là, ou pointe "
                "DIGITAIN_WASM dessus."
            )
        engine = Engine()
        self._store = Store(engine)
        module = Module.from_file(engine, str(path))

        i32, f64 = ValType.i32(), ValType.f64()

        def _stub(params, results):
            # Aucun de ces imports n'est appelé en pratique. On les fournit
            # quand même, et `resize_heap` rend 1 : rendre 0 ferait échouer
            # toute allocation un peu grosse, en silence.
            def fn(*_a):
                return 1 if results else None
            return Func(self._store, FuncType(params, results), fn)

        imports = [
            _stub([i32, f64], [i32]),
            _stub([i32], []),
            _stub([], []),
            _stub([], []),
            _stub([i32], [i32]),
        ]
        exports = Instance(self._store, module, imports).exports(self._store)
        self._mem = exports["f"]
        exports["g"](self._store)               # __wasm_call_ctors

        self._alloc = exports["h"]              # wasm_alloc(size) -> ptr
        self._free = exports["i"]               # wasm_free(ptr)
        self._decrypt = exports["j"]            # wasm_decrypt(ptr, len) -> code
        self._result = exports["k"]             # wasm_get_result() -> ptr
        self._result_len = exports["l"]         # wasm_get_result_len() -> len
        self._free_result = exports["m"]        # wasm_free_result()

    def decrypt_bytes(self, raw: bytes) -> bytes:
        ptr = self._alloc(self._store, len(raw))
        if not ptr:
            raise MemoryError("wasm_alloc a refusé")
        try:
            self._mem.write(self._store, raw, ptr)
            code = self._decrypt(self._store, ptr, len(raw))
        finally:
            self._free(self._store, ptr)
        if code != 0:
            raise ValueError(_ERRORS.get(code, f"code inconnu {code}"))
        out_ptr = self._result(self._store)
        out_len = self._result_len(self._store)
        try:
            return bytes(self._mem.read(self._store, out_ptr, out_ptr + out_len))
        finally:
            self._free_result(self._store)

    def decrypt_payload(self, payload_b64: str) -> Any:
        """Déchiffre le champ `payload` d'une réponse et rend le JSON."""
        return json.loads(self.decrypt_bytes(base64.b64decode(payload_b64)))

    def decrypt_response(self, body: dict) -> Any:
        """Rend le corps utile d'une réponse, chiffrée ou non.

        Toutes les réponses ne le sont pas : `request.js` ne déchiffre que
        celles qui portent un champ `payload`."""
        if isinstance(body, dict) and "payload" in body:
            return self.decrypt_payload(body["payload"])
        return body
