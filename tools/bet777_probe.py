"""Bet777 / Gaming1 : le vrai test de faisabilite depuis la VM.

Rejoue le handshake observe dans le navigateur, puis demande la liste des
sports et le prematch football. Si ca repond, le book est scrapable sans pont
navigateur — contrairement a Betano.
"""
import asyncio, json, uuid, datetime
import websockets

WSS = "wss://wss02.777.be"          # wss01 dans la config, wss02 en usage reel
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
FOOTBALL = 844


def envelope(message: dict, message_type: int) -> str:
    """Le protocole enveloppe chaque message dans une trame, avec la charge
    utile serialisee en JSON *dans une chaine* — pas en objet imbrique."""
    return json.dumps({
        "Id": str(uuid.uuid4()),
        "Message": json.dumps(message),
        "MessageType": message_type,
        "TTL": 10,
    })


def request(identifier: str, req_type: int, content: dict) -> str:
    return envelope({
        "Direction": 0,
        "Id": str(uuid.uuid4()),
        "Requests": [{
            "Id": str(uuid.uuid4()),
            "Type": req_type,
            "Identifier": identifier,
            "Content": json.dumps(content),
        }],
        "Groups": [],
    }, 1000)


async def main():
    try:
        ws = await websockets.connect(
            WSS,
            origin="https://www.bet777.be",
            user_agent_header=UA,
            additional_headers={
                "Accept-Language": "fr-BE,fr;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            open_timeout=20, max_size=None,
        )
    except Exception as e:
        print("CONNEXION REFUSEE :", type(e).__name__, e)
        print("-> l'IP de la VM est filtree ; il faudra passer par le navigateur")
        return

    print("CONNEXION OK sur", WSS)
    async with ws:
        await ws.send(envelope({
            "NodeType": 1,
            "Identity": str(uuid.uuid4()),
            "SupportedCompressions": "LZS2",
            "ClientInformations": {
                "AppName": "Front;Registration-Origin: empty",
                "ClientType": "React", "Version": "0.207.0",
                "UserAgent": UA, "LanguageCode": "fr",
                "RoomDomainName": "777BE",
            },
        }, 1))

        await ws.send(request("GetSports", 201, {}))
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        await ws.send(request("GetPrematchSport", 201, {
            "locale": "fr", "isUserLoggedIn": False, "SportId": FOOTBALL,
            "DiffMinUtc": 120, "Filter": {"Type": 2, "LocalDate": today},
        }))

        for _ in range(40):
            try:
                raw = await asyncio.wait_for(ws.recv(), 12)
            except asyncio.TimeoutError:
                print("(plus de messages)")
                break
            try:
                frame = json.loads(raw)
                inner = json.loads(frame.get("Message") or "{}")
            except Exception:
                print("  trame non-JSON :", str(raw)[:120]); continue

            if frame.get("MessageType") == 7:
                print("  handshake accepte :", inner)
            for r in inner.get("Requests", []):
                content = r.get("Content") or ""
                print(f"  <- {r['Identifier']} : {len(content)} octets")
                if r["Identifier"] == "GetSports":
                    try:
                        sports = json.loads(content)
                        items = sports if isinstance(sports, list) else sports.get("Sports", [])
                        print("     sports :", [(s.get("Id"), s.get("Name")) for s in items][:10])
                    except Exception:
                        print("     ", content[:200])
                if r["Identifier"] == "GetPrematchSport":
                    print("     extrait :", content[:400])
                    if '"Odd"' in content:
                        print("     >>> DES COTES SONT PRESENTES : le book est scrapable depuis la VM")
                        return


asyncio.run(main())
