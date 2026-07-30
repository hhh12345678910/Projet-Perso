# Valuebet — état du projet

Document de reprise. À lire en premier pour reprendre le travail sans
redécouvrir le contexte.

Dépôt : `hhh12345678910/Projet-Perso` — branche `claude/stoic-babbage-q16wi3`
VM : Google Cloud, `us-central1`, IP `34.59.193.111`, utilisateur `hubindylan98`

---

## 1. Objectif

Détecter des paris à espérance positive sur le marché belge en comparant les
books « soft » à une référence sharp (Pinnacle), et alerter sur Telegram.

Le principe : Pinnacle est le book le plus efficient du marché. On retire sa
marge (devig) pour obtenir la probabilité juste, puis on cherche les books qui
proposent une cote supérieure à cette probabilité.

**Le KPI est le CLV** (Closing Line Value), pas le profit à court terme. Battre
la ligne de clôture prouve l'edge ; gagner sur 50 paris peut n'être que de la
chance.

### Résultats réels, mesurés

P&L reconstitué depuis les historiques de paris exportés des books (HAR), sur
la période de ~5 semaines :

| Book | Paris réglés | Misé | P&L | ROI |
|---|---|---|---|---|
| 711 | 40 | 777 € | +546 € | +70,2 % |
| Scooore | 135 | 2 834 € | +1 035 € | +36,5 % |
| Unibet | 328 | 5 997 € | +270 € | +4,5 % |
| StarCasino (hors basket) | 264 | 4 408 € | +3 € | +0,1 % |
| **Total** | **767** | **14 016 €** | **+1 854 €** | **+13,2 %** |

Le basket, retiré depuis, avait coûté **−384 € sur 1 341 € misés** (−28,6 %).

**Le CLV réel est d'environ +8 %**, et non les +10 % du tableur ni les +21,7 %
qu'affichait `clv-report`. Deux mesures indépendantes concordent : +8,26 % par
appariement des HAR aux lignes de clôture, +8,34 % mesuré nativement par le
système sur les paris joués.

⚠️ **Pourquoi le CLV était faux.** Il était mesuré contre la cote de clôture
*affichée* par Pinnacle, commission comprise, alors que l'EV est mesurée contre
la ligne dévigée — deux règles différentes pour les deux bouts du même pari. La
commission médiane de Pinnacle vaut 6,6 % ici (mesurée sur 111 078 marchés 1X2
complets), donc un pari sans le moindre avantage affichait +6,6 % de CLV, et un
pari que le marché a démenti restait confortablement positif. Vérification sur
14 238 paris : EV moyenne 14,26 % + 6,6 % = 21,80 % attendu contre 21,71 %
affiché ; en médiane 15,31 % attendu contre 15,36 %. Le CLV ne faisait que
répéter l'EV de départ. Corrigé : `close-lines` dévige la clôture.

⚠️ **Le ROI par book ne mesure rien à cette échelle.** 711, Scooore et Unibet
sont les mêmes prix Kambi et affichent des CLV indiscernables (+5,9 / +9,5 /
+8,0 %) pour des ROI de +137 %, +26 % et +0,7 %. Même edge, 136 points d'écart.
Sur 767 paris, l'espérance était de +794 € : **environ 1 050 € des gains sont
de la chance**, pas de l'edge. Ne dimensionne rien sur le ROI observé.

---

## 2. Architecture

```
Pinnacle (référence sharp)  ─┐
Unibet / Ladbrokes /         ├─→ devig → ligne juste → EV% → alertes Telegram
StarCasino / Napoleon        │                              → surebets / middles
Betano (via navigateur)     ─┘                              → CLV
```

### Code source

```
src/
  models.py       OddQuote, ValueBet, FairLine, Book, MarketType
  devig.py        Shin / power / multiplicatif        (82 l.)
  ev.py           EV%, Kelly                          (28 l.)
  matcher.py      appariement flou des événements     (213 l.)
  surebet.py      arbitrages inter-books              (141 l.)
  middle.py       middles sur totaux                  (136 l.)
  clv.py          agrégation CLV                      (85 l.)
  storage.py      SQLite
  alerter.py      formatage + routage Telegram
  main.py         orchestration + CLI
  scrapers/       un module par book
tests/            244 tests, tous verts
```

Le cœur (devig, EV, matcher, surebet, middle, clv) fait **685 lignes totalement
indépendantes du pays** — ajouter un marché étranger, c'est ajouter des
scrapers, pas réécrire le moteur.

### Books

| Book | État | Note |
|---|---|---|
| Pinnacle | ✅ référence sharp | 403 occasionnels, sans gravité |
| Unibet | ✅ | plateforme Kambi |
| Ladbrokes | ✅ | plateforme Eurobet |
| StarCasino | ✅ | plateforme Altenar |
| Napoleon | ✅ | plateforme Superbet |
| **Betano** | ✅ via navigateur | voir §3 |
| BetFirst | ❌ désactivé | plus de compte ; fonctionnait en tennis |
| Betcenter | ❌ | cotes erronées |
| 711 / Bingoal / Scooore | ❌ | jumeaux Kambi d'Unibet, prix identiques |
| MeridianBet | ❌ | token anti-bot |
| Golden Palace | ❌ | compte limité |
| Smarkets | ❌ retiré | voir §5 |

**Sports scannés : soccer, tennis, hockey.** Basket retiré (pas d'alertes
souhaitées), volley retiré (Pinnacle ne price que 2 événements → aucune ligne de
référence possible).

---

## 3. Betano — le gros chantier de la session

### Le problème

Betano est protégé par **DataDome**, qui note l'**IP appelante**. Une IP
datacenter reçoit 403 même avec un cookie de session valide et frais. Ce n'est
pas Cloudflare : `cf_clearance` n'apparaît jamais sur la session.

Conséquence : **la VM ne peut pas interroger Betano**, quelle que soit la
gestion de cookie. Seul un navigateur sur IP résidentielle le peut.

### La solution retenue

```
Navigateur (onglet betanosports.be ouvert)
   │  userscript Tampermonkey
   ├─ live    : /danae-webapi/api/live/overview        toutes les 15 s
   └─ prématch: /fr/api/sport/{slug}/matchs-a-venir    toutes les 2 min
   │            + balayage des ~110 compétitions       toutes les 30 min
   ↓ HTTP POST (token partagé)
scripts/betano_ingest_server.py (VM, port 8787)
   ↓ écriture atomique
data/betano.json  +  data/prematch/{sport}.json
   ↓
daemon (lecture à chaque cycle)
```

**Point crucial découvert :** le prématch n'est **pas** sur `danae-webapi`. Il
vit sur une API totalement différente, `/fr/api/`, avec ses propres codes
marché. Le live (`/live/overview`) ne contient **que des matchs en cours**
(mesuré : 165 sur 172 déjà commencés).

`matchs-a-venir` ne couvre que ~24 h. Chaque bloc de sa réponse porte l'URL de
sa compétition, ce qui permet de balayer le calendrier complet.

### Codes marché prématch

| Code | Marché | Décision |
|---|---|---|
| `HTOH`, `HTHP`, `H2HT`, `MRES` | Vainqueur / 1X2 | → H2H |
| `MR12` | 1X2 **SuperOdds** (cotes boostées) | → H2H |
| `FTGO`, `HCTG` | Totaux | → TOTALS |
| `TGHC`, `HCAP`, `AHCP` | Handicap | → HANDICAP |
| `OUH1` | Buts 1ʳᵉ mi-temps | ❌ **exclu** — comparé aux totaux match complet de Pinnacle, fabriquerait de faux value bets |
| `DBLC`, `DNOB`, `BTSC` | Double chance, DNB, BTTS | ❌ exclus — aucune référence sharp |

Les codes inconnus sont **signalés dans le log** au lieu d'être jetés
silencieusement. C'est ce qui a permis de rattraper `H2HT` (volley).

### Outils créés (réutilisables pour n'importe quel book)

| Fichier | Rôle |
|---|---|
| `tools/betano-ingest.user.js` | Push live + prématch (le script de production) |
| `tools/betano-spy.user.js` | Espionne les appels API d'une page (`document-start`) |
| `tools/betano-probe.user.js` | Teste une liste d'URL candidates |
| `tools/betano-sample.user.js` | Capture des échantillons pour analyse |
| `tools/history-spy.user.js` | Trouve l'endpoint d'historique de paris (session connectée) |
| `tools/detect-platform.sh` | Identifie la plateforme d'un book (Kambi/Altenar/…) |

**C'est l'actif technique le plus important du projet** : une méthode pour
ingérer des books protégés par anti-bot sans proxy résidentiel.

---

## 4. Commandes

```bash
./doctor.sh                                  # état complet + cause de tout zéro
python -m src.main clv-report                # LA mesure de rentabilité
python -m src.main track-update              # suivi des paris joués -> CSV
python -m src.main settle --from <csv>       # injecte les scores, calcule le P&L
python -m src.main backfill-fair-lines       # rattrape les clôtures d'avant le correctif
python -m src.main backfill-played-bets      # rattache les anciens clics à leur détection
python -m src.main export-history --out <csv>   # détections + délai + overround + CLV
python -m src.main books-coverage --sport soccer
python -m src.main betano-value-test --min-ev 0.5   # dry-run, sans alerte
python -m src.main betano-coverage           # ce que contient le dump Betano
python -m src.main betano-prematch-shape data/prematch/soccer.json
python -m src.main inspect-json <fichier> --path a.b.0.c
python -m src.main prune --days 2            # purge (VACUUM refusé si disque insuffisant)
./tools/detect-platform.sh https://www.bet777.be/fr

grep "done in" valuebet.log | tail -5        # durée des cycles (~17 s normal)
tac valuebet.log | awk '/══ CYCLE/{c++} c<5' | tac | grep -oiP '\b\w[\w ]*(?= skipped:)' | sort | uniq -c
```

### Services systemd

| Unité | Rôle |
|---|---|
| `valuebet-daemon` | Boucle de scan continue |
| `betano-ingest` | Réception des push navigateur |
| `valuebet-prune.timer` | Purge nocturne (rétention 2 jours) |
| `valuebet-close-lines.timer` | **Capture horaire des lignes de clôture** |

⚠️ `close-lines` doit tourner **plus souvent que la rétention**. Le prix de
clôture n'existe que dans notre propre capture (Pinnacle retire les marchés
prématch au coup d'envoi) — une fois les lignes purgées, le CLV de ces paris
est **définitivement perdu**.

---

## 5. Changements de la session (43 commits)

### Betano — de zéro à couverture complète
- Push navigateur (live 15 s + prématch complet)
- Découverte de l'API prématch sur `/fr/api/` (mon filtre `danae-webapi` la
  masquait — c'est ce qui a coûté le plus de temps)
- Balayage des compétitions pour dépasser la fenêtre de 24 h
- Virtuels exclus (`includeVirtuals=false` + filtre de sécurité sur les noms de
  ligue type « 4x5 minutes »)
- Ligue affichée dans les alertes quand disponible

### Fiabilité
- **Garde de fraîcheur** : flux périmé (live > 5 min, prématch > 30 min) ignoré
  et signalé. Sans elle, un onglet fermé = cotes mortes traitées comme fraîches,
  en silence.
- **`doctor`** : diagnostic complet en une commande
- **Purge planifiée** + refus du VACUUM quand le disque ne peut pas l'absorber
  (la base avait atteint 29 Go avec 13 Go libres)
- `close-lines` planifié — **il ne l'était pas du tout**

### Alertes
- Suppression au niveau du **marché** : jouer `1` sur un 1X2 silence aussi `X`
  et `2`. La ligne reste dans la clé pour ne pas tuer les middles.
- Canal critique : seul canal **sans filtre de cote** (EV seul, prématch
  uniquement) — c'est là qu'atterrissent les EV extrêmes sur grosses cotes
- `reference_book` sur chaque value bet + avertissement dans l'alerte si la
  référence n'est pas Pinnacle

### Smarkets — testé puis retiré
Ajouté comme source sharp de repli, puis **retiré**. Motif : un rafraîchissement
prenait ~26 minutes (une requête par événement) et bloquait le cycle de scan,
silenciant un sport entier pendant ce temps. Deux cycles mesurés à 1571 s et
1595 s. Aucune preuve qu'il pricait quoi que ce soit que Pinnacle n'a pas.

Ce qui reste : `build_fair_lines` accepte toujours une source secondaire **en
repli strict** (jamais de mélange — Pinnacle seul quand il price), et lit la
référence dans les cotes au lieu de nommer un book. Une future source
(Matchbook répond 200 depuis la VM) ne demandera aucune modification ici.

---

## 6. Ce qui reste à faire

### Priorité 1 — mesurer
1. ✅ **CLV dévigé** — `close-lines` capture tout le marché de clôture et en
   retire la commission. `backfill-fair-lines` rattrape les clôtures d'avant le
   correctif dont les cotes n'ont pas encore été purgées.
2. ✅ **Suivi des paris joués** — chaque clic sur Jouer écrit une ligne dans
   `data/paris_track.csv` (EV de départ, CLV réel, mise fictive de 25 €).
   `track-update` régénère le fichier, `settle --from` y injecte les résultats.
   `clv-report` sépare désormais « paris joués » et « toutes détections ».
3. **Résultats automatiques** — il n'existe encore aucune source de scores. Les
   scores se saisissent à la main dans le fichier de suivi, ou s'importent
   depuis l'historique de paris du book. Piste la plus propre : capturer le
   score depuis le flux live de Pinnacle, qui couvre exactement notre univers.
4. ✅ **Segmentation** — faite, résultats en §9. Reste à refaire sur ~2 semaines
   de mesures propres, et sur les paris **joués** plutôt que les détections.

### Priorité 2 — bookmakers
- **Gaming1 (Circus, Bet777, Magic Betting)** : protocole entièrement
  rétro-conçu, voir §10. Ni Kambi ni Altenar — le raccourci « identifiant
  d'opérateur, 30 min » ne s'applique pas. Une seule source de prix
  **indépendante** des quatre books Kambi actuels, donc le meilleur candidat.
- **BetFirst** : fonctionnel, désactivé faute de compte. Mesuré depuis :
  c'est le book qui offre les **pires prix** (−3,20 points de CLV à sélection
  identique, voir §9). Sa perte n'en est pas une.
- Chaque book ajouté = **500-600 € de capacité neuve** (voir §7).

### Priorité 3 — gestion de mise et de capital

**Le constat central :** les comptes sont limités après ~500-600 € de gain, les
mises tombent à 17-22 €. La contrainte n'est donc **pas le temps mais la
capacité en euros par compte**.

Conséquence, et elle contredit le fonctionnement actuel : jouer 45 petits paris
à 20 € est la **pire** façon d'exploiter un plafond. À plafond identique,
15 paris à 50 € atteignent le même montant avec trois fois moins de signaux
envoyés au book — et beaucoup moins de fatigue.

**À faire :**
- Arrondir les mises (17,43 € est la signature d'un calculateur — c'est le
  signal de détection le plus fort et le plus simple à corriger)
- Une fois le CLV confirmé, monter les mises sur les meilleurs signaux
  uniquement, plutôt que de multiplier les petits paris
- Étudier le passage à des seuils d'EV plus élevés (8-10 %) : moins de paris,
  meilleur ROI unitaire, moins de fatigue — **à valider par la segmentation**
- Camouflage : quelques paris récréatifs, utilisation des promos, éviter de
  prendre la meilleure cote dans les secondes qui suivent son apparition

### Priorité 4 — pistes commerciales

Trois modèles envisagés :

| Modèle | Problème |
|---|---|
| Vendre des sélections | **S'autodétruit** : 200 abonnés sur la même ligne soft = le book bouge sa cote et limite tout le monde. Plus d'abonnés = produit moins bon. |
| SaaS multi-utilisateurs | Refonte lourde (comptes, isolation, facturation). Le daemon est mono-utilisateur de bout en bout. |
| **Licencier la techno d'ingestion** | Le plus prometteur : pas de problème de capacité, on vend l'outil et non les pronostics. Moins exposé juridiquement. |

**Obstacles à traiter avant d'investir :**
- **Juridique** : la Belgique n'autorise que les sites licenciés `.be`. Vendre
  des pronostics y est encadré par la Commission des Jeux de Hasard. Un outil
  dont la finalité est de contourner un anti-bot expose davantage que son usage
  personnel. À faire valider par un juriste **avant** de construire.
- **Passage à l'échelle** : un navigateur allumé par source. Pour 3 books c'est
  un mini-PC, pour 30 c'est une ferme à maintenir.
- **Preuve** : aucun acheteur n'acceptera un tableur comme track record.

**Marché français** envisagé (ANJ, autre VM) : attention, les books français ont
des marges de 7-12 % contre 5-6 % ailleurs — marge plus haute = **moins** de
value bets, pas plus. Tester un book avant d'investir dans une infrastructure.

### Exchanges
Écarté pour l'instant : aucun exchange n'est licencié en Belgique (Betfair
renvoie 403 depuis la VM, testé). Avec un CLV de +10 %, la stratégie back/lay
serait pourtant rentable (10 % − 2 % de commission ≈ 8 % verrouillés sans risque
de résultat). L'équivalent accessible existe déjà dans le système : les
**surebets** et **middles** inter-books, détectés à chaque cycle.

---

## 7. Contraintes permanentes à ne pas oublier

1. **L'onglet Betano doit rester ouvert en permanence** sur une machine allumée.
   DataDome bloque l'IP de la VM ; il n'existe aucun contournement côté serveur.
   La garde de fraîcheur signale le gel dans les logs mais ne peut pas le
   relancer.
2. **Les comptes sont limités après ~500-600 € de gain.** C'est structurel, pas
   évitable — seulement retardable.
3. **`close-lines` doit tourner** plus souvent que la rétention, sinon le CLV
   est perdu définitivement.
4. **La base grossit d'environ 80 M de lignes par jour.** Purge à 2 jours.

---

## 8. Point ouvert au moment de la rédaction

Baisse ressentie du nombre de value bets premium (4-5 au lieu de 10-15).
**Le cœur de détection est inchangé** (vérifié par diff : `find_value_bets` n'a
reçu que deux champs d'information, le routage premium est identique).

Trois causes cumulées, toutes issues de demandes explicites :
- basket + volley retirés → 5 sports devenus 3
- BetFirst désactivé → 5 books devenus 4 (il produisait 38 value bets / 15
  alertes par 24 h)
- suppression au niveau du marché → chaque pari joué silence 2-3 sélections au
  lieu d'une

Le troisième est mesurable et réversible (on peut rendre la granularité
configurable). Les deux premiers sont voulus.

---

## 9. Ce que mesure le CLV corrigé

Mesuré sur les détections **prématch**, cote 1,5-6, **dédupliquées par
opportunité** — 822 opportunités, une semaine de captures dévigées.

⚠️ **Méthode : toujours dédupliquer avant de conclure.** Une même sélection est
détectée sur plusieurs books ; ce sont des observations corrélées, pas
indépendantes, et tu n'en joues qu'une (la suppression au niveau du marché
silence les autres). Ne pas dédupliquer double artificiellement les effectifs et
gonfle toutes les significativités. 1 283 lignes = 822 opportunités = 644 matchs.

### Le délai avant le coup d'envoi — le facteur n°1

| Délai | n | CLV | Positifs |
|---|---|---|---|
| < 6 h | 558 | +6,35 % | 79 % |
| 6-12 h | 383 | +7,56 % | 77 % |
| 12-24 h | 148 | +5,81 % | 72 % |
| **> 24 h** | **194** | **+0,41 à +2,74 %** | **53-58 %** |

**Couper à 24 h, pas à 12 h.** La tranche 12-24 h est indiscernable des plus
courtes (0,8 σ) ; seul l'au-delà de 24 h s'effondre (3,2 σ après déduplication,
p = 0,00003) — pour seulement 15-16 % du volume sacrifié.

Le mécanisme : loin du match, la ligne de **référence** est elle-même bruitée.
On ne détecte alors pas une erreur du book soft mais une erreur temporaire de
Pinnacle, qui se corrige d'ici la clôture — et contre nous. La décomposition le
confirme : le côté gagnant est stable (+12 à +13 % partout), **seule la
fréquence des perdants change** (20 % à moins de 12 h, 42 % au-delà de 24 h).

### L'EV — le filtre le plus rentable, déjà en place

| EV | n | CLV | Positifs |
|---|---|---|---|
| 5-8 % | 409 | +3,69 % | 73 % |
| 8-10 % | 146 | +5,65 % | 74 % |
| 10-12 % | 71 | +8,89 % | 80 % |
| 12-15 % | 76 | +8,42 % | 76 % |
| 15-20 % | 63 | +11,79 % | 84 % |
| 20-30 % | 44 | +20,60 % | 93 % |

Croissance monotone, sans exception : **l'estimation d'EV est informative**.
Le seuil premium à 8 % fait passer le CLV de +7,7 % à +10,2 %. Monter à 10 %
gagnerait encore ~3 points, au prix de la moitié du volume.

### La cote — n'est pas un critère

+5,8 / +6,7 / +5,4 / +7,2 % sur les tranches 1-2, 2-2,5, 2,5-3, 3-4. Plat.
À edge égal, préférer les cotes basses **pour la variance**, pas pour l'edge.

### Les books — comparaison appariée

Sur 269 sélections proposées par 2+ books (même clôture, seul le prix diffère) :

| Book | Écart vs concurrents | Meilleur prix |
|---|---|---|
| **StarCasino** | **+1,01 % ± 0,34** | 49 % |
| Ladbrokes | +0,35 % | 27 % |
| Unibet | +0,06 % | 42 % |
| Betano | −0,57 % | 27 % |
| Napoleon | −0,58 % | 33 % |
| **BetFirst** | **−3,20 % ± 0,91** | 15 % |

StarCasino paraît médiocre en vue brute (CLV +5,54 %) mais donne le meilleur
prix une fois sur deux : son portefeuille de détections est moins bon, pas ses
prix. À sélection identique, le prendre en priorité.

### Ce qui NE marche pas — pistes fermées

- **L'overround de la référence ne prédit rien.** Corrélation +0,009 avec le
  CLV, aucun ordre entre les tranches. Hypothèse abandonnée.
- **Filtrer par championnat est hors de portée.** 141 ligues pour 375 paris,
  soit 2,7 chacune. Test de permutation : la meilleure ligue observée
  (+34,7 %) tombe dans ce que le pur hasard produit (p = 0,13). Il faudrait
  100 à 400 paris **par ligue**, soit des années. Règle générale : une
  hypothèse posée à l'avance sur beaucoup de données vaut quelque chose, cent
  hypothèses cherchées après coup ne valent rien.

### Le live — un tiers des détections, structurellement invalide

750 détections sur 2 274 ont un délai négatif : elles comparent une cote live à
une ligne Pinnacle **prématch**, périmée depuis le coup d'envoi. Elles affichent
+20,5 % de CLV, ce qui est absurde. Le scraper Pinnacle ignore délibérément les
matchs en cours (`isLive`), donc il n'existe aucune référence live.

Elles n'atteignent aucun canal (premium et critique sont prématch, le canal
principal est plafonné à 8 % d'EV) — donc elles ne sont pas jouées, mais elles
polluent toute statistique globale. **Nettoyage le plus rentable qui reste.**

---

## 10. Gaming1 / Ardent — protocole rétro-conçu

Circus, Bet777 et Magic Betting tournent sur la plateforme maison d'Ardent. Ni
Kambi ni Altenar. Un seul scraper ouvrirait les trois, et ce serait la première
source de prix **indépendante** des quatre books Kambi actuels.

### Accès

L'IP de la VM est refusée sur **tout le domaine**, pas seulement l'API — HTTPS
403 y compris sur la page d'accueil. Ce n'est pas un géoblocage : une IP Google
Cloud **belge** (`europe-west1`) est refusée aussi. C'est l'ASN datacenter.
Migrer la VM n'y changerait rien → **pont navigateur obligatoire**, comme Betano.

### Protocole

Tout passe par WebSocket, rien en HTTP — d'où des exports HAR vides de cotes
(Chrome n'y écrit pas les trames ; utiliser `tools/websocket-spy.user.js`).

| | Bet777 | Circus |
|---|---|---|
| Serveur | `wss://wss02.777.be` | `wss://wss02.circus-sport.be` |
| `RoomDomainName` | `777BE` | `CIRCUS` |
| `SportId` football | 844 | 844 |

Enveloppe : `{"Id":uuid, "Message":"<json sérialisé EN CHAÎNE>", "MessageType":N,
"TTL":10}`. MessageType 1 = handshake, 7 = accusé, 8 = métadonnées, 1000 =
requête/réponse. **Le handshake n'exige ni compte, ni token, ni captcha.**

Commandes utiles (Type 201) : `GetSports`, `GetSportNav`, `GetPrematchSport`,
`GetEventsForLeague`, `GetEvent`. Les réponses portent `Odd`, `Name` et
`ProviderProbabilities` (la probabilité déjà dévigée par le book).

### Compression

Circus compresse, Bet777 non. Préfixe `--##LZS2##--` suivi de lz-string en
UTF-16 (`decompressFromUTF16`). Piège : la compression s'applique à **deux
niveaux** — soit la trame entière, soit seulement le champ `Message` ou
`Content` d'une enveloppe JSON par ailleurs normale. Traiter les deux cas.

À vérifier : déclarer `SupportedCompressions: ""` dans le handshake pourrait
suffire à obtenir du clair, le client annonçant ses capacités.

### Reste à faire

`tools/circus-ingest.user.js` (WebSocket propre, boucle sur le prématch, push
vers la VM), un endpoint `/ingest-circus`, et `src/scrapers/circus.py`.
Filtrer les cotes ≤ 0 : elles signalent un marché fermé. La forme exacte de
`GetPrematchSport` n'a pas été capturée sur Circus, à déduire de Bet777.
