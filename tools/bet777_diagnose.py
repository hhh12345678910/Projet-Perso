"""Pourquoi Bet777 renvoie 403 a la VM : l'IP, l'endpoint, ou le client ?

Un 403 sur la poignee de main WebSocket ne dit pas QUI refuse. Trois causes
demandent trois reponses tres differentes :

  - l'IP datacenter est bannie          -> pont navigateur obligatoire
  - seul l'upgrade WebSocket est filtre -> chercher un en-tete/cookie manquant
  - Cloudflare refuse le client Python  -> l'empreinte TLS trahit le script

Ce script interroge les trois couches et lit le code d'erreur Cloudflare, qui
nomme la cause explicitement (1020 = regle d'acces, 1015 = debit, 1010 =
signature du client).
"""
from __future__ import annotations

import re
import socket
import ssl
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-BE,fr;q=0.9"}


def http_check(url: str) -> None:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        body = r.text[:200].replace("\n", " ")
        print(f"  {r.status_code}  {url}")
        if r.status_code != 200:
            print(f"        {body}")
    except Exception as e:
        print(f"  ERREUR  {url}  ({type(e).__name__}: {e})")


def ws_handshake(host: str) -> None:
    """Poignee de main WebSocket ecrite a la main, pour LIRE la reponse.

    La bibliotheque websockets leve une exception sur un 403 et jette le corps
    — or c'est precisement le corps qui porte le code d'erreur Cloudflare."""
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Origin: https://www.bet777.be\r\n"
        f"User-Agent: {UA}\r\n"
        f"Accept-Language: fr-BE,fr;q=0.9\r\n"
        f"\r\n"
    )
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=20) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                print(f"  TLS negocie : {tls.version()} / {tls.cipher()[0]}")
                tls.sendall(req.encode())
                data = b""
                while len(data) < 8192:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\r\n\r\n" in data and len(data) > 512:
                        break
        text = data.decode("utf-8", "replace")
        status = text.split("\r\n", 1)[0]
        print(f"  reponse : {status}")
        ray = re.search(r"Cloudflare Ray ID:.*?>([0-9a-f]+)<", text)
        if ray:
            print(f"  Cloudflare Ray ID : {ray.group(1)}")
        code = re.search(r"error code: (\d+)", text)
        if code:
            n = code.group(1)
            sens = {
                "1020": "regle d'acces — l'IP ou le pays est refuse",
                "1015": "limitation de debit",
                "1010": "empreinte du client refusee (le TLS Python trahit le script)",
                "1006": "connexion coupee par le navigateur/serveur",
            }.get(n, "voir la documentation Cloudflare")
            print(f"  >>> error code {n} : {sens}")
        elif "101" in status:
            print("  >>> upgrade ACCEPTE — la VM peut parler WebSocket")
        else:
            snippet = re.sub(r"<[^>]+>", " ", text)
            snippet = re.sub(r"\s+", " ", snippet)[:300]
            print(f"  corps : {snippet}")
    except Exception as e:
        print(f"  ERREUR reseau : {type(e).__name__}: {e}")


print("=== 1. HTTPS ordinaire vers le site et son API ===")
http_check("https://www.bet777.be/fr")
http_check("https://apifront.bet777.be/Ajax/GetShellParameters.ashx")

print("\n=== 2. IP publique de la VM ===")
try:
    print("  ", httpx.get("https://ipv4.icanhazip.com", timeout=15).text.strip())
except Exception as e:
    print("  indisponible :", e)

print("\n=== 3. Poignee de main WebSocket ===")
for host in ("wss02.777.be", "wss01.777.be"):
    print(f"-- {host}")
    ws_handshake(host)

print("""
Lecture :
  HTTPS 200 + WS 403 avec error code 1010 -> c'est le client Python, pas l'IP
  HTTPS 403 aussi                         -> l'IP de la VM est bannie
  HTTPS 200 + WS 403 sans code            -> en-tete ou cookie manquant
""")
