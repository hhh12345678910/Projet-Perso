#!/usr/bin/env python3
"""Tiny HTTP receiver for Betano odds pushed from a browser userscript.

Why this exists
---------------
Betano's danae-webapi is gated by Cloudflare (cf_clearance) + DataDome cookies
that are bound to the *browser + IP* that minted them and expire every few
hours. The (datacenter) VM can't generate them, so the historical workaround
was to paste a fresh cookie by hand every few hours (see set-betano-cookie.sh).

Instead, a Tampermonkey userscript running in a real browser that's already
sitting on betanosports.be fetches the /live/overview/latest JSON from its own
IP with its own valid cookies — exactly the traffic the site makes itself — and
POSTs it here. This server just validates a shared token and writes the JSON to
disk, atomically, where the daemon reads it via `--betano-file`.

So Betano freshness == the userscript's push interval, with zero cookie
juggling on the VM.

Config (env vars)
-----------------
  BETANO_INGEST_TOKEN   required. Shared secret; the userscript sends it in the
                        X-Ingest-Token header. The server refuses to start
                        without it (an unauthenticated write endpoint would let
                        anyone poison the odds file).
  BETANO_INGEST_FILE    output path. Default: <project>/data/betano.json
  BETANO_INGEST_PORT    listen port. Default: 8787
  BETANO_INGEST_HOST    bind address. Default: 0.0.0.0 (all interfaces)
  BETANO_INGEST_MAX_MB  max accepted body size in MB. Default: 32

Endpoints
---------
  GET  /health   -> 200 "ok"        (no token; for connectivity tests)
  POST /ingest   -> 200 {...stats}  (requires X-Ingest-Token; writes the file)
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _project_dir() -> Path:
    # scripts/.. -> project root, so the default output path is portable.
    return Path(__file__).resolve().parent.parent


TOKEN = os.getenv("BETANO_INGEST_TOKEN", "")
OUT_FILE = Path(
    os.getenv("BETANO_INGEST_FILE", str(_project_dir() / "data" / "betano.json"))
)
HOST = os.getenv("BETANO_INGEST_HOST", "0.0.0.0")
PORT = int(os.getenv("BETANO_INGEST_PORT", "8787"))
MAX_BYTES = int(float(os.getenv("BETANO_INGEST_MAX_MB", "32")) * 1024 * 1024)


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write to a temp file in the same directory, then os.replace() it onto the
    target. replace() is atomic on POSIX, so the daemon reading `path` in a
    parallel thread never sees a half-written file — it gets either the old
    complete JSON or the new complete JSON, never a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


class Handler(BaseHTTPRequestHandler):
    # Quieter than the default (which logs every request line to stderr); we do
    # our own concise logging in the handlers instead.
    def log_message(self, *_args) -> None:  # noqa: D401
        pass

    def _send(self, code: int, body: dict | str) -> None:
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            ctype = "application/json"
        else:
            data = body.encode()
            ctype = "text/plain; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Permissive CORS: GM_xmlhttpRequest is privileged and doesn't enforce
        # CORS, but a plain fetch fallback (or a browser preflight) would — these
        # headers keep both paths working.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ingest-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Ingest-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[len("Bearer "):].strip()
        # Constant-time compare so a timing side-channel can't leak the token.
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, "")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            self._send(200, "ok")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/ingest":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            _log(f"401 unauthorized from {self.client_address[0]}")
            self._send(401, {"error": "bad or missing token"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return
        if length > MAX_BYTES:
            self._send(413, {"error": f"body too large (> {MAX_BYTES} bytes)"})
            return

        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except ValueError as e:
            _log(f"400 invalid JSON from {self.client_address[0]}: {e}")
            self._send(400, {"error": f"invalid JSON: {e}"})
            return
        if not isinstance(data, dict):
            self._send(400, {"error": "expected a JSON object"})
            return

        # Light sanity so a stray/empty push can't clobber a good file. The
        # overview payload is a Redux-style store with these three maps; if none
        # are present it's not an overview we can parse — reject without writing.
        n_events = len(data.get("events") or {})
        n_markets = len(data.get("markets") or {})
        n_selections = len(data.get("selections") or {})
        if not (n_events or n_markets or n_selections):
            _log(f"422 payload has no events/markets/selections from {self.client_address[0]}")
            self._send(422, {"error": "no events/markets/selections in payload"})
            return

        try:
            _atomic_write(OUT_FILE, raw)
        except OSError as e:
            _log(f"500 write failed: {e}")
            self._send(500, {"error": f"write failed: {e}"})
            return

        _log(
            f"200 wrote {len(raw)} B -> {OUT_FILE.name} "
            f"(events={n_events} markets={n_markets} selections={n_selections})"
        )
        self._send(200, {
            "ok": True,
            "bytes": len(raw),
            "events": n_events,
            "markets": n_markets,
            "selections": n_selections,
        })


def main() -> None:
    if not TOKEN:
        sys.exit(
            "BETANO_INGEST_TOKEN is not set. Generate one (`openssl rand -hex 32`), "
            "put it in .env, and use the same value in the userscript. Refusing to "
            "start an unauthenticated write endpoint."
        )
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _log(f"Betano ingest server listening on {HOST}:{PORT} -> {OUT_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _log("stopped")


if __name__ == "__main__":
    main()
