"""Le 403 de Bet777 : reputation d'IP datacenter, ou geoblocage belge ?

La distinction commande la suite. Une IP datacenter bannie impose un pont
navigateur, avec un onglet allume en permanence de plus a maintenir. Un
geoblocage se leve avec n'importe quel relais situe en Belgique, pour quelques
euros par mois et sans machine a surveiller.

Trois mesures :
  - le corps complet de la page de blocage, qui porte le code Cloudflare
  - les autres books Gaming1 (Circus, Magic Betting), pour savoir si toute la
    plateforme est fermee ou seulement Bet777
  - un book qui fonctionne deja, comme temoin : il prouve que la VM n'est pas
    bloquee en general et isole le probleme chez Ardent
"""
from __future__ import annotations

import re

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-BE,fr;q=0.9",
           "Accept": "text/html,application/xhtml+xml"}

CIBLES = [
    ("Bet777  (Gaming1)", "https://www.bet777.be/fr"),
    ("Circus  (Gaming1)", "https://www.circus.be/fr"),
    ("MagicBetting (Gaming1?)", "https://www.magicbetting.be/fr"),
    ("Unibet  (temoin, Kambi)", "https://www.unibet.be/"),
    ("Pinnacle (temoin, reference)", "https://api.arcadia.pinnacle.com/0.1/sports"),
]

CODES = {
    "1020": "regle d'acces — l'IP, l'ASN ou le pays est refuse par une regle du site",
    "1015": "limitation de debit",
    "1010": "empreinte du client refusee",
    "1009": "PAYS BLOQUE — un relais en Belgique suffirait",
}


def texte(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


for nom, url in CIBLES:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        etat = "OK " if r.status_code == 200 else "BLOQUE"
        print(f"{etat} {r.status_code}  {nom}")
        if r.status_code != 200:
            corps = texte(r.text)
            code = re.search(r"error code:?\s*(\d+)", corps, re.I)
            if code:
                n = code.group(1)
                print(f"        error code {n} : {CODES.get(n, 'cause non repertoriee')}")
            for mot in ("country", "pays", "region", "not available", "indisponible",
                        "blocked", "Access denied", "Sorry"):
                m = re.search(r"[^.]{0,90}" + mot + r"[^.]{0,90}", corps, re.I)
                if m:
                    print(f"        indice : ...{m.group(0).strip()}...")
                    break
            else:
                print(f"        {corps[:180]}")
    except Exception as e:
        print(f"ERREUR    {nom} : {type(e).__name__}: {e}")

print("""
Lecture :
  Bet777 bloque, Unibet et Pinnacle OK  -> le blocage est propre a Ardent,
      pas a la VM ; reste a savoir si c'est l'IP ou le pays
  code 1009, ou un message parlant de pays/region  -> geoblocage :
      un petit relais en Belgique suffit, pas besoin de navigateur
  code 1020 sans mention de pays  -> regle d'acces, probablement l'ASN
      datacenter : un relais belge PEUT suffire, a tester avant d'investir
  Circus et Magic Betting bloques aussi -> toute la plateforme Gaming1 est
      derriere la meme regle ; un seul relais les ouvrirait tous les trois
""")
