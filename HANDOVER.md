# Valuebet — état du projet

Document de reprise. À lire en premier pour reprendre le travail sans
redécouvrir le contexte. Dernière mise à jour : 04/08/2026.

**Si tu ne lis que trois choses :** §1 pour la mesure qui fait autorité
(+8,86 % de CLV, 10,1 σ), §11 pour le mode de défaillance dominant du projet
(la panne silencieuse), §13 pour la session du 04/08 — état actuel, ce qui
tourne, et ce qui reste ouvert.

Dépôt : `hhh12345678910/Projet-Perso` — branche **`claude/project-summary-2cz4sk`**
VM : Google Cloud, `us-central1`, IP `34.59.193.111`, utilisateur `hubindylan98`
Répertoire sur la VM : **`~/Projet-Perso`** (`valuebet` est le nom d'hôte, pas
un dossier — l'erreur a coûté un aller-retour)

Déploiement :
```bash
cd ~/Projet-Perso && git pull && sudo systemctl restart valuebet-daemon
```

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

**Le CLV réel est de +8,9 %**, et non les +10 % du tableur ni les +21,7 %
qu'affichait `clv-report`. Quatre mesures indépendantes concordent :

| Mesure | Population | n | CLV | Significativité |
|---|---|---|---|---|
| Appariement HAR ↔ clôtures | paris réglés | — | +8,26 % | — |
| Natif, paris joués | opportunités dédupliquées | 186 | +9,29 % | 5,1 σ |
| **Natif, canal Premium complet** | **opportunités dédupliquées** | **423** | **+8,86 %** | **10,1 σ** |

La troisième ligne fait autorité : elle porte sur **toutes** les détections
éligibles au Premium, jouées ou non, ce qui supprime le biais de sélection
manuelle. IC 95 % : [+7,14 % ; +10,59 %]. 78,3 % d'opportunités positives.

⚠️ **La sélection manuelle n'apporte rien de mesurable.** Opportunités jouées
+8,23 % (n=132) contre non jouées +9,15 % (n=291). Trier à la main parmi les
alertes ne fait pas mieux que le filtre automatique — donc jouer plus
systématiquement ne dégraderait rien, et le temps d'arbitrage est mieux investi
ailleurs.

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
| Napoleon | ✅ | plateforme Superbet — 1X2 en football, vainqueur en tennis |
| **Betano** | ✅ via navigateur | voir §3 |
| **Circus** | ✅ via navigateur | plateforme Gaming1, voir §10 — football + tennis |
| BetFirst | ❌ désactivé | plus de compte ; fonctionnait en tennis |
| Betcenter | ❌ | cotes erronées |
| 711 / Bingoal / Scooore | ❌ | jumeaux Kambi d'Unibet, prix identiques |
| MeridianBet | ❌ | token anti-bot |
| Golden Palace | ❌ | compte limité |
| Smarkets | ❌ retiré | voir §5 |

**Sports scannés : soccer, tennis** (`SPORT_LIST` dans `.env`). Basket retiré
(pas d'alertes souhaitées), volley retiré (Pinnacle ne price que 2 événements →
aucune ligne de référence possible).

⚠️ **Le hockey a été coupé le 30/07 et ne reviendra pas tout seul.** Motif :
hors-saison en août, Pinnacle n'en price aucun match, et les huit requêtes par
cycle que hockey et volley consommaient — dont quatre vers Pinnacle — faisaient
partie du quota qui manquait au football, bloqué 20 à 70 cycles par heure.
**À remettre dans `SPORT_LIST` à la reprise de la saison NHL, en octobre.**

Un repli automatique existe par ailleurs pour un sport encore scanné mais sans
calendrier : après dix réponses vides, Pinnacle ne le sonde plus que toutes les
dix minutes, et il revient de lui-même. Il ne s'applique pas à un sport retiré
de `SPORT_LIST`.

### Couverture mesurée (30/07, fenêtre 24 h, après rapprochement flou)

| | Pinnacle price | couvert par ≥ 1 book |
|---|---|---|
| Football | 126 matchs | **112 (89 %)** |
| Tennis | 67 matchs | **60 (90 %)** |

Sur les 14 matchs de football non couverts, 9 n'ont aucun candidat proche —
sélections d'Amérique centrale et championnats équatoriens qu'aucun book belge
n'offre. Rien à corriger là.

⚠️ **Le facteur limitant en tennis est Pinnacle, pas les books.** Pinnacle ne
price que ~71 matchs de tennis prématch quand les books en offrent 107 à 201.
Ajouter un bookmaker de tennis n'élargit pas le gisement ; seule une seconde
référence sharp le ferait (voir §6, Smarkets).

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
python -m src.main books-coverage --sport soccer,tennis   # events, horizon, ∩ Pinnacle
python -m src.main betano-value-test --min-ev 0.5   # dry-run, sans alerte
python -m src.main betano-coverage           # ce que contient le dump Betano
python -m src.main betano-prematch-shape data/prematch/soccer.json
python -m src.main inspect-json <fichier> --path a.b.0.c
python -m src.main prune --days 2            # purge (VACUUM refusé si disque insuffisant)
./tools/detect-platform.sh https://www.bet777.be/fr

# Ponts navigateur — fraîcheur et cadence (les deux doivent être < 60 s)
ls -l data/circus/ data/prematch/ data/betano.json && date -u
sudo journalctl -u betano-ingest --since "5 min ago" --no-pager \
  | grep -oP "(?<=-> )\S+|(?<=prematch ')[a-z]+" | sort | uniq -c
sudo journalctl -u betano-ingest --since "30 min ago" --no-pager | grep -P "\] [45]\d\d "

# Détections par book sur une fenêtre donnée — pour juger un book récent, il
# FAUT restreindre la fenêtre : comparer 3 h à 7 jours donne 13 contre 1813.
sqlite3 data/valuebet.db "SELECT book, COUNT(*) FROM value_bets \
  WHERE detected_at > '2026-07-30T16:30:00' GROUP BY book ORDER BY 2 DESC;"

node tools/circus-ingest.selftest.js tools/circus-ingest.user.js  # 7 scénarios

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

Les services s'appellent `valuebet-daemon` et `betano-ingest`, **pas**
`valuebet` ni `valuebet-ingest`. `betano-ingest` sert aussi Circus, malgré son
nom. Ses logs vont au journal systemd, il n'y a pas de fichier
`betano-ingest.log`.

⚠️ `close-lines` doit tourner **plus souvent que la rétention**. Le prix de
clôture n'existe que dans notre propre capture (Pinnacle retire les marchés
prématch au coup d'envoi) — une fois les lignes purgées, le CLV de ces paris
est **définitivement perdu**.

---

## 5. Changements de la session Betano (43 commits)

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

⚠️ **À reconsidérer — c'est devenu la priorité n°1 (voir §6).** Le retrait
était motivé par la lenteur, pas par la juridiction : l'API Smarkets est
**publique et sans authentification**, donc utilisable depuis la Belgique comme
source de données (on ne parie pas dessus). Les 26 minutes viennent d'une
requête **par marché** pour les contrats, avec 0,5 s d'attente. Trois leviers
non tentés : grouper les identifiants par virgule comme le code le fait déjà
pour les cotes, se limiter aux 24-48 prochaines heures, et sortir le
rafraîchissement du cycle de scan comme pour Circus et Betano.

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

### Priorité 1 bis — une seconde référence sharp (le plus gros levier)

Tout le système dépend de **Pinnacle seul**. Trois conséquences, toutes
observées :

1. **Point de défaillance unique.** Le 30/07, Pinnacle a cessé de répondre et
   tout s'est arrêté. L'alerte de panne existe depuis (voir §11) mais ne
   remplace pas la mesure perdue.
2. **Le tennis est plafonné.** 71 matchs pricés par Pinnacle contre 107 à 201
   chez les books. Aucun bookmaker supplémentaire ne débloquera ça.
3. **La fair line est bruitée.** Une moyenne de sources sharp est plus juste
   qu'une source unique, et réduire ce bruit accélère mécaniquement toute
   segmentation future.

**Smarkets est le bon candidat** : exchange londonien, API REST **publique et
sans authentification** — pas de compte, pas de déclaration de résidence, c'est
une source de données et non un lieu de pari. Ses prix sont structurellement
sans marge (appariement pair-à-pair), donc directement utilisables comme
probabilité juste, sans devig. Le scraper existe déjà dans
`src/scrapers/smarkets.py`. Voir §5 pour les trois leviers de performance.

⚠️ **Betfair est à écarter.** Non licencié en Belgique ; y ouvrir un compte
depuis une VM étrangère suppose de déclarer une fausse résidence, ce qui expose
les fonds à un gel. Si les prix Betfair deviennent nécessaires, la voie propre
est un fournisseur de données commercial qui les revend sous licence.

### Priorité 2 — bookmakers
- ✅ **Circus** : en production depuis le 30/07, football et tennis, cycle de
  30 s. Voir §10 et §11.
- **Bet777 et Magic Betting** : même plateforme Gaming1, seul `ROOM` change dans
  le userscript. ⚠️ **Mesurer avant de construire** : ce sont des books du même
  opérateur (Ardent), donc probablement le même flux de prix — exactement la
  situation d'Unibet / 711 / Bingoal / Scooore, dont les CLV sont indiscernables.
  Trois books jumeaux ne valent pas mieux qu'un.
- **BetFirst** : fonctionnel, désactivé faute de compte. Mesuré depuis :
  c'est le book qui offre les **pires prix** (−3,20 points de CLV à sélection
  identique, voir §9). Sa perte n'en est pas une.
- Chaque book ajouté = **500-600 € de capacité neuve** (voir §7).

⚠️ **Ce qu'un book supplémentaire apporte vraiment.** Mesuré le 30/07 : les six
books tiennent dans une fourchette de 2,7 points de CLV (Betano +10,8 % à Unibet
+8,1 %). L'edge vient de la détection, pas du choix du bookmaker. Un book de
plus n'apporte donc pas un meilleur prix mais **du volume** — des paires
book × match supplémentaires sur des matchs déjà couverts. C'est ce nombre de
paires qu'il faut suivre, pas le nombre de books.

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

1. **Deux onglets doivent rester ouverts en permanence** sur une machine
   allumée : Betano et Circus. DataDome bloque l'IP de la VM pour le premier,
   l'ASN datacenter pour le second ; il n'existe aucun contournement côté
   serveur. La garde de fraîcheur signale le gel dans les logs mais ne peut pas
   relancer les onglets. **Un seul onglet par book** — deux versions d'un même
   userscript qui tournent en parallèle écrivent des fichiers incohérents
   (constaté le 30/07).
   Après toute modification d'un userscript, **recharger l'onglet** :
   Tampermonkey n'applique rien avant.
2. **Les comptes sont limités après ~500-600 € de gain.** C'est structurel, pas
   évitable — seulement retardable.
3. **`close-lines` doit tourner** plus souvent que la rétention, sinon le CLV
   est perdu définitivement.
4. **La base grossit d'environ 80 M de lignes par jour.** Purge à 2 jours.
5. **Le mode de panne dominant est silencieux, pas bruyant.** Voir §11 : cinq
   pannes trouvées le 30/07, toutes avec un book interrogé, répondant
   correctement, dont on jetait les données sans la moindre erreur dans les
   logs. Ne jamais conclure « ça marche » d'une absence d'erreur ; toujours
   compter ce qui sort.

---

## 8. Points ouverts au moment de la rédaction

1. **Vérifier au matin du 31/07 que le tennis Circus n'a pas décroché la
   nuit.** `ls -l data/circus/ && date -u` — les deux fichiers doivent avoir
   moins d'une minute. La nuit est le moment où une journée lointaine revient
   sans aucun match, ce qui avait figé le pont (corrigé, mais non encore
   éprouvé sur un programme creux).
2. **Le `clv-report` de dimanche 02/08** sera le premier à porter sur des paris
   détectés avec toutes les corrections du 30/07 en place. C'est lui qui dira
   si le tennis tient sa promesse : +13,5 % de CLV mesurés **avant** les
   corrections, sur 92 opportunités.
3. **Deux paris à EV aberrante ont été joués** (311 % et 65 %). À ce niveau une
   cote n'est pas bonne, elle est fausse — erreur d'appariement. Un plafond de
   sécurité vers 40-50 % d'EV reste à poser.
4. **Les résultats de matchs ne sont toujours pas automatisés**, donc aucun P&L
   réel dans `data/paris_track.csv`. Ce n'est pas bloquant : sur cette durée le
   P&L mesure la chance, le CLV mesure l'edge.

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

⚠️ **Révisé le 30/07 — ne pas poser de filtre.** Mesure plus récente sur le
canal Premium, 423 opportunités dévigées :

| Délai | n | CLV | σ |
|---|---|---|---|
| < 2 h | 100 | +10,98 % | 7,6 |
| 2-6 h | 59 | +10,56 % | 4,1 |
| 6-12 h | 130 | +10,35 % | 5,3 |
| 12-24 h | 52 | +9,18 % | 5,2 |
| **24-48 h** | 35 | **−2,70 %** | −1,1 |
| **> 48 h** | 47 | **+6,38 %** | **+3,0** |

L'agrégat ≤24 h contre >24 h est bien significatif (4,0 σ, p < 0,0001), mais le
détail **ne décrit pas un effondrement** : c'est un creux dans la fenêtre
24-48 h, entouré de valeurs positives des deux côtés, et le >48 h est
significativement **positif**. Un couperet à 24 h supprimerait donc aussi une
zone rentable. Le creux n'est pas significativement différent de zéro (n=35).

**Conclusion : laisser Kelly réduire la mise quand l'edge est plus faible,
plutôt que couper.** Un filtre ne se justifierait que si la contrainte était le
capital ou le temps, pas l'espérance.

Mécanisme proposé pour l'ancienne lecture, à reprendre avec prudence : loin du
match, la ligne de **référence** est elle-même bruitée.
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

⚠️ **Sauf au-delà de 4.** Mesure du 30/07 sur le canal Premium :

| Cote | n | CLV |
|---|---|---|
| 1,5-2 | 67 | +9,35 % |
| 2-2,5 | 85 | +7,60 % |
| 2,5-3 | 64 | +6,01 % |
| 3-4 | 166 | +7,45 % |
| **4-6** | **39** | **+21,22 %** |

La voie « cotes hautes » du Premium — cote 4 à 6 avec EV ≥ 20 % — est de loin le
meilleur segment du système. Même chose par EV : la tranche 30 %+ donne +52 %
sur 9 opportunités. Ces deux filtres se recouvrent largement ; c'est
probablement le **même** effet vu deux fois, pas deux effets indépendants.

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

Vue non appariée du 30/07, canal Premium (423 opportunités) : Betano +10,77 %,
Napoleon +8,93 %, StarCasino +8,58 %, Ladbrokes +8,29 %, Unibet +8,05 %. **Une
fourchette de 2,7 points sur cinq books** — l'edge est dans la détection, pas
dans le book.

### Le sport

| Sport | n | CLV |
|---|---|---|
| **Tennis** | 92 | **+13,50 %** |
| Football | 331 | +7,57 % |

Le tennis rapporte près du double, et ces données sont **antérieures** aux
corrections du 30/07 qui ont porté sa couverture de 72 % à 90 %. À reconfirmer
au rapport du 02/08 — si l'écart tient, c'est l'argument le plus fort pour la
seconde référence sharp, seule capable de lever le plafond des 71 matchs
Pinnacle.

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

### ✅ Implémenté le 30/07

`tools/circus-ingest.user.js`, l'endpoint `/ingest-circus?sport=`, et
`src/scrapers/circus.py`. Football (SportId 844) et tennis (848), 4 jours de
prématch, cycle de 30 s.

**Codes de marché retenus** — égalité exacte, jamais de correspondance par
ressemblance :

| Sport | Code | Marché |
|---|---|---|
| Football | `P1XP2` | 1X2 |
| Football | `total-goals-OverUnder`, `total-OverUnder` | totaux buts |
| Tennis | `P1P2` | vainqueur |
| Tennis | `total-games-OverUnder`, `total-games-over-under` | totaux jeux |

⚠️ **Circus écrit le même marché de deux façons.** Au tennis : 43 marchés en
`over-under` contre 24 en `OverUnder`, dans le **même** dump. N'en reconnaître
qu'une faisait perdre 64 % des totaux. Le football a la même duplication
(`total-OverUnder` / `total-goals-OverUnder`), déjà couverte. **Toujours
vérifier les BetType réellement présents** avant de conclure qu'un marché est
complet — le daemon liste les codes inconnus une fois par (sport, code) dans son
log.

⚠️ **Ne jamais reconnaître un marché à sa structure.** `draw-no-bet` porte
exactement les deux mêmes noms d'équipe qu'un vainqueur ;
`first-set-total-games-over-under-OverUnder` contient le code du match complet
en sous-chaîne tout en ne mesurant qu'un set.

Exclusions volontaires : mi-temps, set numéroté, premier set, double chance,
both-teams-to-score, draw-no-bet, handicaps, qualification, vainqueur du
trophée. Aucun n'a de contrepartie exploitable chez Pinnacle.

### Attribution des réponses — trois fois cassée, à ne pas refaire

Une réponse `GetPrematchSport` **ne rappelle pas quel sport a été demandé**.
C'est le piège central du pont, qui a produit trois pannes successives :

1. Attribuer « au premier sport encore en attente » → fichiers **croisés** (le
   tennis écrit dans `soccer.json`), parce qu'une réponse tennis de 500 Ko
   double une réponse football de 3 Mo.
2. Sérialiser strictement pour corriger → 8 requêtes à la file dépassent 30 s,
   donc **un cycle sur deux sauté**.
3. Exiger les 4 journées avant de pousser → une journée sans aucun match renvoie
   un bloc sans `SportId`, inattribuable ; le sport restait incomplet **pour
   toujours** et son fichier vieillissait jusqu'au rejet par la garde de
   fraîcheur.

**Solution en place :** requêtes parallèles, attribution par (1) l'Id de requête
si le serveur le renvoie, (2) le `SportId` porté par chaque `League`, (3) le
sport le plus en retard. Un cycle incomplet est poussé quand même — une journée
manquante ne coûte que les matchs les plus lointains, refuser de pousser coûtait
le sport entier.

`node tools/circus-ingest.selftest.js tools/circus-ingest.user.js` rejoue le
userscript dans un faux navigateur et vérifie ces sept scénarios. **À lancer
après toute modification du userscript** — le reste du projet est en Python,
rien d'autre ne couvre ce fichier.

Défense en profondeur : le serveur d'ingestion refuse (422) un push dont les
`SportId` démentent le `?sport=`, sans toucher au fichier ; et le parseur écarte
puis signale les ligues d'un autre sport.

---

## 11. Session du 30/07 — ce qui a changé

### Les trois demandes initiales

1. ✅ **Pause du live, réversible et sans effet de bord.** `VALUEBET_SCAN_LIVE=0`
   par défaut (`src/config.py`). Le filtre agit **uniquement** là où une
   détection devient une alerte : les cotes live continuent d'être collectées et
   stockées, donc les clôtures Pinnacle, le CLV et les surebets ne perdent rien.
   Rallumer : `VALUEBET_SCAN_LIVE=1` puis redémarrer le daemon.
   Les **surebets live continuent** — c'est voulu. Un value bet compare à une
   ligne de référence prématch, figée au coup d'envoi ; un surebet compare deux
   cotes vivantes entre elles et reste valide.
2. ✅ **Alerte panne Pinnacle.** Après 5 cycles consécutifs sans cotes sur un
   sport, un message part sur le canal critique, plus un message de
   rétablissement. Une seule alerte par panne. Seuil :
   `PINNACLE_ALERT_AFTER_CYCLES`.
3. ✅ **Circus.** Voir §10.

### Les cinq pannes silencieuses trouvées

Toutes du même type : **un book interrogé, qui répond correctement, et dont on
jetait les données — sans la moindre erreur dans les logs.**

| Panne | Effet | Cause |
|---|---|---|
| Circus tennis, totaux | 64 % des marchés perdus | deux orthographes du même `BetType` |
| Napoleon tennis | book entièrement absent | seul le marché 547 (1X2) était lu ; le tennis utilise 521 |
| Circus, fichiers croisés | football lisant du tennis | réponses attribuées par ordre d'arrivée |
| Tennis, rapprochement | 28 % des matchs non appariés | tolérance d'horaire de 10 min |
| Circus tennis, journée vide | book disparu 40 min | cycle jugé incomplet pour toujours |

**C'est le mode de défaillance dominant de ce système.** Un scraper qui plante
se voit ; un scraper qui filtre trop ne se voit pas. Le réflexe à garder :
compter ce qui **sort** de chaque book, pas vérifier qu'il n'y a pas d'erreur.
`books-coverage` est l'outil pour ça.

### Le rapprochement d'événements

**Tolérance d'horaire par sport** (`src/matcher.py`, `TIME_TOLERANCE_BY_SPORT`) :
tennis 180 min, football 10 min (inchangé).

Motif : au tennis un match commence quand le précédent libère le court, et
chaque book publie sa propre estimation. Mesuré — 10 des 19 matchs Pinnacle non
couverts avaient un candidat aux noms **identiques à 100 %**, écarté pour un
décalage de 20 à 130 minutes. Ce ne sont pas les traductions qui posaient
problème, ce sont les horaires.

Résultat : couverture tennis 72 % → **90 %**, et surtout **169 → 247 paires
book × match (+46 %)** — c'est ce nombre qui détermine le volume de détections,
pas la couverture.

Sûr parce que le nom reste le juge : deux joueurs ne se rencontrent qu'une fois
par tournoi, aucun autre match ne peut porter les deux mêmes noms le même jour.
La garde d'ambiguïté devient même plus stricte, voyant plus de références
concurrentes.

⚠️ **Effet de bord corrigé le soir même.** Une cote rapprochée adopte la clé de
la référence, donc **l'heure de Pinnacle**. Quand celle-ci est postérieure à
celle du book, un match déjà commencé passait pour à venir et la pause live ne
le filtrait pas — des alertes tennis en direct sont sorties. `OddQuote` et
`ValueBet` conservent désormais `book_event_key`, et les deux gardes retiennent
la **plus précoce** des heures connues.

### Cadences

| Flux | Avant | Après |
|---|---|---|
| Circus football + tennis | — | 30 s |
| Betano, liste 24 h | 2 min | 30 s |
| Betano, balayage compétitions | 30 min | 30 min (inchangé) |
| Betano live | 15 s | 15 s |

Le balayage des compétitions reste à 30 min : ce sont ses ~110 requêtes qui se
remarqueraient, et il ne sert qu'à atteindre des matchs à plusieurs jours dont
les prix ne dérivent presque pas. Il occupe 2-3 minutes pendant lesquelles les
fichiers prématch vieillissent — normal, sans conséquence (garde à 30 min).

### Tests

312 tests Python + 7 scénarios JavaScript. Nouveaux fichiers :
`tests/test_circus_ingest.py`, et l'ajout de `tools/circus-ingest.selftest.js`.

---

## 12. Pinnacle — le plafond du système (soirée du 30/07)

### Le symptôme

Le football est bloqué **20 à 70 cycles par heure**, soit 12 à 40 minutes sur
60. Pendant une coupure : aucune détection, et aucune capture de clôture — ce
CLV-là est perdu définitivement.

Le 403 **saute d'un sport à l'autre** (football muet pendant que le tennis
répond, puis l'inverse), donc ce n'est pas un blocage d'IP permanent.

### Ce qui a été corrigé

| Correctif | Effet |
|---|---|
| Recul exponentiel après 403 (60 s → 10 min) | ne plus prolonger la limitation en redemandant toutes les 20 s |
| Sérialisation des appels entre sports | supprime la rafale de 6-8 requêtes simultanées par cycle |
| Réponse vide ≠ panne | le hockey hors-saison alertait 24 h/24 |
| Sondage espacé des sports sans calendrier | après 10 réponses vides, une tentative toutes les 10 min |
| Hockey et volley retirés de `SPORT_LIST` | 8 requêtes Pinnacle par cycle → 4 |
| Alerte au bout de 20 min, plus 5 cycles | le canal critique redevient lisible |

**Un 403 persiste malgré tout ça** (alerte de 20 min reçue après la coupe des
sports hors-saison).

### Le levier restant, et l'arbitrage

`PINNACLE_MIN_INTERVAL_SEC=60` dans `.env` — espacement des appels, désactivé
par défaut à la demande de l'utilisateur.

⚠️ **L'espacement ne retarde aucune détection.** Les books soft restent
interrogés à chaque cycle ; seule la ligne de référence vieillit. Le coût n'est
donc pas une occasion manquée mais une EV légèrement fausse, égale à la dérive
de la ligne Pinnacle sur la durée d'espacement.

`tools/line_speed.py --hours 6` mesure cette dérive à partir de la table
`quotes`. **Mesure à faire avant de trancher** : si la colonne « inchangé »
montre 90 %+ de cotes identiques d'un cycle à l'autre, l'espacement est quasi
gratuit face à 20 minutes de coupure totale.

### L'hypothèse non testée

Un `403` n'est pas le code d'une limitation de débit (`429` l'est). Un 403
répété sur une IP de datacenter peut signaler un filtrage d'ASN, comme pour
Gaming1 et Betano. **Test décisif** : la même requête depuis une IP
résidentielle pendant que la VM reçoit un 403.

- Maison 200 / VM 403 → filtrage d'IP, aucun espacement n'y changera rien. Le
  code prévoit déjà `PINNACLE_PROXY` et `PINNACLE_LOCAL_IP` pour ça.
- Les deux en 403 → quota lié à la clé d'API publique, l'espacement est le bon
  levier.

### Ce que tout cela démontre

Le système entier est plafonné par le quota d'une source unique. C'est le
troisième argument, et le plus concret, en faveur de la seconde référence
sharp (§6) : **Smarkets**, API publique sans authentification, prix sans marge
par construction, scraper déjà écrit.

---

## 13. Session du 04/08 — état actuel

Une trentaine de commits, de `e431b5d` à `7f3d5fd`. Cinq chantiers : le
plafond Pinnacle, l'outillage de diagnostic, la collecte de données pour un
futur filtre ML, un détecteur de marchés en retard, et une panne totale
diagnostiquée en fin de session (§13.11).

### Ce qui tourne en production

Branche `claude/project-summary-2cz4sk`, commit `7f3d5fd`. Sept books couverts
à 100 % sur les derniers runs mesurés, cycle à 14-23 s de médiane.

⚠️ **La VM était restée sur `claude/stoic-babbage-q16wi3`** pendant une partie
de la session — un `git pull` répondait « Already up to date » sur la mauvaise
branche pendant que le travail partait ailleurs. Vérifier `git branch
--show-current` avant de conclure qu'un correctif ne marche pas.

### 13.1 Pinnacle — la cause réelle des 403

**Ce n'était pas le nombre de requêtes, c'était le volume.** Le football
téléchargeait ~5 Mo par cycle, soit ~12 Go/jour, et **les deux tiers étaient le
calendrier** (`/sports/{id}/matchups`), qui ne porte aucune cote.

Correctif : cache mémoire du calendrier (`PINNACLE_MATCHUPS_TTL_SEC`, 300 s par
défaut) dans `src/scrapers/pinnacle.py`. Trafic passé de 0,25 à ~0,028 Mo/s.

`PINNACLE_MIN_INTERVAL_SEC=60` est **justifié par la mesure** : `line_speed.py`
montre 99,5 % de cotes Pinnacle inchangées à 60 s d'intervalle. L'espacement ne
retarde aucune détection (les books soft restent interrogés à chaque cycle) ;
il ne fait vieillir que la ligne de référence.

**Non appliqué, offert** : `PINNACLE_MAX_REUSE_SEC=240`. Avec `60 + 120 ≤ 240`,
un 403 isolé ne créerait jamais de trou de détection. Coût mesuré ≈ 0,02 point
d'EV. À trancher.

### 13.2 `database is locked` — 695 fetches perdus en 11 h

Cause : `teams.record()` écrivait en base **pendant le parsing des scrapers**,
et entrait en collision avec `close-lines` et la purge. Une exception y faisait
tomber tout le scrape.

Correctif : `src/teams.py` avale désormais les échecs de stockage — un registre
d'affichage n'a aucune raison de faire échouer une collecte.

### 13.3 Circus — 180 s de latence, pas 30

Chrome throttle les timers des onglets en arrière-plan. Le userscript croyait
tenir 30 s, la mesure disait 180 s. Correctif : cadencement piloté par un
Worker, et découplage tick/période. **Mesuré après déploiement : 6-19 s.**

Le harnais de selftest était lui-même faux (5/7 même sur la version d'origine) :
`nextCycle()` mélangeait les requêtes de deux cycles. Corrigé en se
synchronisant d'abord sur le silence ; 7/7 sur les deux versions, et 2/7 en
réintroduisant volontairement le bug d'attribution.

### 13.4 Outils de diagnostic

| Outil | Ce qu'il répond |
|---|---|
| `tools/pinnacle_doctor.py` | Pourquoi Pinnacle est sauté. **Un tableau par run** (403/429, cycles sautés, verrous, couverture du book le plus faible) — `--runs 0` pour n'avoir que le tableau |
| `tools/line_speed.py` | Vitesse de correction des books, dérive de la ligne Pinnacle |
| `tools/clv_delay.py` | CLV par délai avant coup d'envoi, avec déduplication et tests de Welch |
| `export-tracking` | Sort l'historique de la VM (4,2 Mo) |

**Pourquoi le doctor existe** : trois messages différents portent le mot
« skipping » et n'ont rien à voir entre eux (échec d'appel / zéro événement /
exception traversante). Les confondre a envoyé deux diagnostics de cette
session dans le décor.

⚠️ **Un `grep -c` sur `valuebet.log` ne veut rien dire.** Le journal couvre
plusieurs jours et plusieurs versions du code : 750 « database is locked » et
314 « HTTP 403 » cumulés, alors que les runs récents n'en ont aucun. Toujours
raisonner par run.

`push-backups.sh` **ne peut pas fonctionner** : la base fait 31 Go, la limite
GitHub est 100 Mo. D'où `export-tracking`.

### 13.5 Ce que dit le CLV sur un mois complet

**Correction importante d'une conclusion antérieure.** Sur 9 jours, la tranche
24-48 h avant coup d'envoi paraissait être un trou (−0,82 %, n=51). Sur le mois
complet elle est à **+2,92 % (σ=2,1)**, significativement positive. La
recommandation de couper cette tranche était fausse et a été retirée.

**Sur les paris joués vs non joués** — correction de l'utilisateur : « Je ne
fais pas du triage, c'est juste que je n'ai pas le temps des fois de le jouer. »
L'écart observé n'est donc pas une compétence de sélection, et jouer plus
systématiquement ne dégraderait rien.

**Non appliqué, offert** : faire porter la bande de cotes du canal premium sur
`fair_odd` plutôt que sur `odd_taken`. Mesuré : +8,6 % de volume, +0,21 point
de CLV, 44 % des paris changent de tranche. En attente de décision.

### 13.6 Collecte pour un futur filtre ML

L'objectif énoncé : « récolter un max de données sur tous les matchs que je
peux suivre », pour entraîner plus tard un modèle qui filtre les mauvais paris.

- `src/storage.py` : tables `bet_features` et `bet_corrections`
- `src/leagues.py` : catégorisation des championnats, précédence
  `amical > féminin > jeunes > coupe > D2 > D3 > top5 > région > autre`
- `OddQuote.match_score` : le score du rapprochement flou était calculé puis
  jeté. Un appariement à 86 et un à 100 n'inspirent pas la même confiance, et
  « mauvais matching » est une cause soupçonnée de faux positifs.
- La ligue est désormais persistée dans `events` (elle était écrite vide, donc
  aucune analyse par championnat n'était possible, même a posteriori)
- Commandes : `features-report`, `corrections-report`, `export-tracking`

### 13.7 Détecteur de marchés en retard — et son flood

L'occasion : un match commencé depuis 19 min, 1-1, et Circus proposait encore
son marché « les deux équipes marquent » en prématch. Le pari était déjà gagné
au moment de le prendre.

**Mis en service, il a immédiatement noyé le canal critique.** Deux défauts
indépendants, corrigés dans `b26793d` :

1. **La prémisse était fausse.** « Le book expose encore ce match, donc il a
   oublié de suspendre » ne tient pas : Circus, Unibet et ses clones Kambi,
   Napoleon et StarCasino continuent d'exposer un match commencé et se
   contentent de le repricer en direct, sans qu'aucun champ ne le signale.
   Seuls **Betano** (drapeau live) et **Ladbrokes** (`live: 0`) permettent la
   distinction. Le détecteur signalait le fonctionnement normal de 4 books sur 7.

   Le vrai discriminant est le **prix** : un marché oublié a gardé sa cote
   d'avant le coup d'envoi, un book qui price en direct l'a forcément déplacée.
   Comparaison **par issue** et non par match — un book peut repricer son 1X2
   tout en oubliant son BTTS, et c'est exactement l'occasion recherchée.
   Sans historique on se tait : preuve positive d'immobilité exigée.

2. **Pinnacle muet valait « tout a disparu ».** Sans réponse, `live_now` est
   vide et le veto « Pinnacle le price encore » sautait sans bruit. Un recul
   après 403 suffisait : au moment où le système était le moins sûr de lui, il
   devenait le plus bavard.

**Alerte instantanée sur but** : le flux live Betano est la **seule** source de
score du projet (Pinnacle ignore les matchs en cours). Football uniquement — au
tennis le champ `score` porte les points du jeu et change à chaque échange. Un
événement vu pour la première fois n'est jamais un but, sinon le canal partirait
en rafale à chaque redémarrage.

Réglages `.env` : `LATE_MARKET_ENABLED` (coupe-circuit sans déploiement),
`LATE_MARKET_MIN_MINUTES=10`, `LATE_MARKET_MAX_MINUTES=75`,
`LATE_MARKET_COOLDOWN_SEC=300`.

**À vérifier au prochain démarrage** — les compteurs par cycle :
```bash
grep "marchés en retard —" valuebet.log | tail -20
```
Format : `retenue N, cote bougée N, sans historique N, pinnacle muet N`. Ils
existent parce qu'un filtre trop strict et un book sans erreur donnent le même
résultat — rien — et qu'il faut pouvoir distinguer les deux.

### 13.8 Demandes retirées par l'utilisateur

- **Mise à jour / suppression automatique des messages Telegram** (EV qui
  s'actualise, retrait sous 8 %). Retiré : « Je préfère peut-être un jour
  prochain plus loin donc créer un SaaS. » Les trois fichiers ont été remis en
  état, 391 tests + 7/7 JS vérifiés à ce moment-là.
- **Purge de la base** : « je ne veux pas purger, je veux laisser comme c'était
  avant, ça roulait bien » — précisé ensuite : « juste par rapport à la purge,
  mais pas par rapport aux autres modifs ». Les correctifs de purge (verrou,
  progression, `--max-seconds`) restent dans le code ; c'est l'exécution d'une
  purge agressive qui est écartée.

### 13.9 À faire au prochain démarrage

0. **Vérifier que Pinnacle est sorti de maintenance** (§13.11) — c'est le
   préalable à tout le reste, le système est à l'arrêt sans lui :
   ```bash
   grep -E "maintenance annoncée|Pinnacle muet|Pinnacle rétabli" valuebet.log | tail
   ```
1. **Vérifier le détecteur de marchés en retard** (§13.7) — les compteurs
   après une soirée complète. C'est le point le plus chaud.
2. **`pinnacle_doctor.py --runs 0` sur un run long** — confirmer 403 et verrous
   à 0 après une journée pleine sans redémarrage. Les runs mesurés jusqu'ici
   étaient trop courts (2 à 11 cycles) pour conclure.
3. **Trancher les deux réglages offerts** : `PINNACLE_MAX_REUSE_SEC=240`
   (§13.1) et la bande premium sur `fair_odd` (§13.5).
4. **Smarkets comme seconde référence sharp** (§6) — le plus gros levier
   restant. Les appels groupés lèvent le blocage des 26 minutes.
5. **SaaS / plateforme web** montrant les value bets encore valides — intention
   exprimée, rien de commencé.

### 13.10 Pièges rencontrés, à ne pas refaire

- **`echo 'VAR=x' >> .env` sans garde** a créé des doublons deux fois. Toujours :
  `sed -i '/^VAR=/d' .env && echo 'VAR=x' >> .env`
- **`set -euo pipefail` + `diff`** : `diff` sort en 1 dès qu'il y a une
  différence et tuait `setup.sh --check` après le premier écart. Entourer de
  `{ diff ... } || true`.
- **Un mock peut masquer un désaccord d'arité.** `send_late_market_alerts`
  attendait 4 champs et le cycle lui en passait 6 ; le `ValueError` aurait été
  avalé par le `except` du cycle, détecteur muet sans une ligne au journal. Le
  test remplaçait la fonction par un mock. Un test de bout en bout a été ajouté.
- **Un test qui dépend de `time.monotonic()` absolu** échoue dans un conteneur
  fraîchement démarré. Poser explicitement la valeur de départ.
- **Ne pas réduire une taille de lot sans mesurer** : les batches de purge
  ramenés de 200k à 50k ont rendu l'opération beaucoup plus lente. Revenus à
  200k, avec progression toutes les 5 s.

### 13.11 Panne totale du 04/08 — maintenance Pinnacle non signalée

**Le symptôme perçu** : plus aucune notification de value. Le daemon tournait,
les cycles s'enchaînaient en 14-23 s, les books soft renvoyaient leurs milliers
de cotes. Rien ne signalait de problème.

**La cause** : Pinnacle répondait `HTTP 503` avec
`{"title": "MAINTENANCE", "detail": "API is currently undergoing maintenance"}`
sur football **et** tennis, pendant des heures. Sans référence sharp, aucune
ligne juste, donc zéro value bet — le système entier était à l'arrêt.

**Pourquoi aucune alerte n'est partie** — la chaîne complète, parce que c'est
le mode de défaillance le plus coûteux du projet :

1. `tenacity` ré-emballe l'échec final dans `RetryError`, qui n'est **pas** une
   `HTTPStatusError`.
2. Le tri par code HTTP de `fetch_pinnacle_quotes` ne la voyait donc jamais.
3. L'exception traversait `_fetch_all_parallel`, y était journalisée en
   « Pinnacle skipped », et `_PINNACLE_FAILED` n'était jamais posé.
4. Le cycle affichait alors `Pinnacle sans événement (hors-saison ?) —
   skipping` — sur du football, un 4 août.
5. `_pinnacle_health`, à qui l'on disait que tout allait bien, se taisait.

**Corrigé dans `7f3d5fd`** : `RetryError` est déballée avant le tri
(`_unwrap_retry`), et les **5xx rejoignent 403/429 dans le recul** — pendant
une maintenance, réessayer à chaque cycle ne brasse que du vide. Le message
nomme la maintenance quand le code est 503, pour ne pas repartir chercher un
blocage d'IP inexistant. Trois tests, chacun vérifié en échec sans le
correctif.

**Le piège de diagnostic** : la première sonde écrite pour identifier la panne
appelait `_get`, qui est justement enveloppé par le retry — elle a donc affiché
`RetryError` sans jamais révéler le code HTTP. Pour voir le vrai code, il faut
contourner le retry :

```bash
.venv/bin/python - <<'PY'
from src.scrapers.pinnacle import PinnacleScraper, PINNACLE_BASE, SPORT_IDS
with PinnacleScraper() as p:
    for sport in ("soccer", "tennis"):
        r = p._client.get(f"{PINNACLE_BASE}/sports/{SPORT_IDS[sport]}/matchups")
        print(f"{sport:8} HTTP {r.status_code}  {r.text[:160]!r}")
PY
```

**Ce que la panne démontre** : le système dépend entièrement d'une source
unique, et une maintenance de son côté suffit à tout arrêter. C'est le
quatrième argument — et le plus concret — en faveur de la seconde référence
sharp (§6, Smarkets). Les 403 du 30/07 plafonnaient le débit ; ce 503 a coupé
le courant.

### 13.12 Comment lire une panne dans ce projet

Ordre de diagnostic, appris à ses dépens deux fois dans la session :

1. **Le daemon tourne-t-il ?** `systemctl is-active valuebet-daemon`
2. **Quelle branche, quel commit ?** `git branch --show-current && git log
   --oneline -1`. Un `git pull` qui répond « Already up to date » sur la
   mauvaise branche a coûté un aller-retour complet.
3. **Y a-t-il des détections ?** `grep "value bets:" valuebet.log | tail`. Un
   compteur figé sur la même valeur = plus rien n'est calculé.
4. **Pinnacle répond-il ?** La sonde de §13.11, jamais un `grep` sur le
   journal : les messages y sont coupés à 80 colonnes par `rich`.
5. **Par run, jamais en cumul.** `pinnacle_doctor.py --runs 0`. Un `grep -c`
   sur `valuebet.log` additionne plusieurs jours et plusieurs versions du code.

⚠️ **Le mode de défaillance dominant reste le silence** : un book répond, le
scraper tourne, rien n'en sort, et rien ne l'écrit au journal. Chaque fois
qu'un compteur ou un message a manqué dans cette session, il a fallu deux
allers-retours pour s'en rendre compte. Quand un correctif restreint ce qui est
signalé, **ajouter les compteurs de ce qui a été rejeté et pourquoi** — sinon
un filtre trop strict et une absence réelle de problème deviennent
indiscernables.
