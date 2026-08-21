# Valuebet — état du projet

Document de reprise. À lire en premier pour reprendre le travail sans
redécouvrir le contexte. Dernière mise à jour : 21/08/2026.

**Nouveau (21/08, seconde session) :**
- §21.16 : 🔴 **la couverture est MESURÉE : 12,3 %.** Un catalogue limité aux
  cinq grands championnats réglerait un huitième de tes détections football
  (10 051 détections, **395 ligues**, 152 ligues pour 80 % du volume). La
  première ligue du flux est `Club Friendlies` à 8,1 %. Aucune API à catalogue
  ne suffira. Sonde : `scripts/scores_coverage.py` (20ᵉ sonde), sans clé ni
  réseau.
- §21.16 : ⚠️ **le pont API-Football tourne**, mais le premier dry-run (32 %)
  ne mesure rien — `journee_non_pontee=2`, le palier gratuit ne sert que trois
  jours. **Ne rien payer avant un dry-run à `journee_non_pontee=0`.**
- §21.16 : 🔴 **la voie RapidAPI est morte** — listing supprimé (« API not
  found », vérifié au navigateur). Troisième échec sur cette piste ; les trois
  fichiers qui la recommandaient sont corrigés pour qu'il n'y ait pas de
  quatrième. **Il ne reste que deux voies** au §21.9 pt 1.
- §21.16 : le pont résultats n'est **pas agnostique de sa source** — serveur,
  userscript et fournisseur codent en dur la forme d'API-Football. La voie 3
  (cinquième pont) coûte trois fichiers, pas « un parseur neuf ».
- §17.1 : ⚠️ **le dépôt a de nouveau TROIS branches**, la garantie du §17.1 ne
  tient plus. Voir ci-dessous.

**Nouveau (21/08) — session « ValueDylan » :**
- §21.15 : **deux pannes de production** trouvées au passage, non corrigées —
  Betano live en 403 (cookie expiré) et un libellé néerlandais non traduit chez
  MagicBetting.
- §21.14 : mi-temps **complètes côté Circus** (4 codes, ~93 700 cotes) après
  correction du nul « Egalité », qui coûtait 66 % du marché. **Unibet abandonné**
  sur mesure. **Zéro détection, et c'est correct** : EV médiane −8,5 %.
- §21.13 : **les handicaps ne sont pas bloqués par les softs mais par notre
  devig** — les deux côtés d'un même handicap tombent dans deux groupes
  séparés. Chantier non lancé.
- §21.9 pt 1 : le compte API-Football est **réellement suspendu** (tableau de
  bord), pas seulement refusé par IP. Le pont ne peut rien.

**Nouveau (20/08) :** §21.14 — les mi-temps sont EN SERVICE et MUETTES
(collectées, deviguées, suivies en CLV, zéro alerte). §21.13 — on jette 35,5 % du payload Pinnacle (les
mi-temps) sur la foi d'un commentaire faux, et 26,9 % (les handicaps) pour une
raison valable. La mi-temps est le chantier à ouvrir en premier.

**Nouveau (20/08) :** §21.12 — la CLV n'est pas un miroir de l'EV (R² = 0,257
sur 15 222 paris clôturés, la ligne juste bouge de 5,61 % en médiane). Le KPI du
projet tient. En revanche le §20.8 sur les `under` ne se reproduit pas sur un
échantillon doublé, et le §21.9 pt 4 est annulé.

**Si tu ne lis que trois choses :** §20.4 pour le premier P&L réel du projet et
la question qu'il ouvre (la CLV mesure-t-elle vraiment l'edge ?), §11 pour le
mode de défaillance dominant (la panne silencieuse), et §21.9 pour la liste de
travail à jour.

La mesure de CLV qui fait autorité reste §18.1 (+9,49 % sur 2 025 opportunités
premium, 23,8 σ), confirmée le 17/08 sur 6 486 opportunités — voir §20.8.

État du code : **§15, §17 et §18 décrivent ce qui tourne aujourd'hui.** La §16 n'a
modifié aucun code — c'était une session de mesure. La §17, elle, a modifié le
code : Smarkets est en production comme seconde référence sharp.

⚠️ **La branche de travail est `claude/resume-clarification-1541xa`**, qui est
aussi la branche par défaut du dépôt et celle que la VM suit. **Développer
ailleurs recrée le piège des §13 et §14** — du code commité qui n'atteint
jamais la production.

⚠️ **Et le dépôt n'a plus une seule branche : il en a trois** (constaté le
21/08). La garantie écrite au §17.1 ne tient donc plus. Aucune ne diverge à ce
jour — `claude/valuedylan-ifsu3o` est à 29 commits EN RETARD et 0 en avance,
`claude/resume-clarification-restructure-kpx0l3` est sur le même commit que la
branche par défaut — mais elles rouvrent le piège pour la session suivante, qui
peut y démarrer sans le savoir. **À supprimer au navigateur** (§17.1 : la
suppression est impossible depuis une session Claude Code).

Rapport visuel de l'analyse du 11/08 (courbes d'alignement, CLV découpée) :
https://claude.ai/code/artifact/255c254a-32e3-4dfc-b275-03b8a7cd961e

Dépôt : `hhh12345678910/Projet-Perso` — branche **`claude/resume-clarification-1541xa`**
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

**Le CLV réel est de +10,2 %** sur le canal Premium, et non les +10 % du
tableur ni les +21,7 % qu'affichait `clv-report` avant correction. Plusieurs
mesures indépendantes concordent :

| Mesure | Population | n | CLV | Significativité |
|---|---|---|---|---|
| Appariement HAR ↔ clôtures | paris réglés | — | +8,26 % | — |
| Natif, canal Premium (30/07) | opportunités dédupliquées | 423 | +8,86 % | 10,1 σ |
| **Natif, canal Premium (04/08)** | **opportunités dédupliquées** | **858** | **+10,18 %** | **18,4 σ** |

La dernière ligne fait autorité — **voir §14.1 pour le détail**. Elle porte sur
**toutes** les détections éligibles au Premium, jouées ou non, ce qui supprime
le biais de sélection manuelle. IC 95 % : [+9,09 % ; +11,27 %]. 80 %
d'opportunités positives. Le doublement de l'effectif entre le 30/07 et le
04/08 a confirmé et légèrement relevé la mesure.

⚠️ **La sélection manuelle n'apporte rien de mesurable.** Mesuré deux fois, à
une semaine d'intervalle, à filtre égal (canal premium seulement) : le 30/07,
jouées +8,23 % (n=132) contre non jouées +9,15 % (n=291) ; le 04/08, jouées
+9,97 % (n=252) contre non jouées +10,27 % (n=606), écart −0,30 point,
**t = −0,25, non significatif**. Trier à la main parmi les alertes ne fait pas
mieux que le filtre automatique — donc jouer plus systématiquement ne
dégraderait rien, et le temps d'arbitrage est mieux investi ailleurs.

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
| **Golden Palace** | ✅ réactivé le 06/08 | plateforme Altenar, aucune auth — 2ᵉ meilleure couverture (§15.2) |
| **BetFirst** | ✅ réactivé le 06/08 | **hors du cycle**, cache de fond — pires prix du portefeuille (§15.2) |
| Betcenter | ❌ | cotes erronées — répond (40 313 cotes), mais reste dehors |
| 711 / Bingoal / Scooore | ❌ | jumeaux Kambi d'Unibet, prix identiques. Répondent tous les trois |
| MeridianBet | ❌ | token anti-bot — répond, 0 cote |
| Bet777 | ❌ | Gaming1/Ardent, aucun scraper. Écarté par l'utilisateur le 06/08 |
| **Smarkets** | ✅ **2ᵉ référence sharp** | exchange, API publique. Repli STRICT derrière Pinnacle — voir §17.5 |
| **MagicBetting** | ✅ via navigateur | **Digitain**, payloads chiffrés déchiffrés par leur propre WASM — §18.6. Football seul, 27 matchs |

⚠️ `tools/book_revive_check.py` sonde les books désactivés et dit lesquels
répondent encore. Leurs motifs vieillissent : « compte limité » pour Golden
Palace ne concernait que le PARI, son API ne demande aucune authentification.
Lancer cette sonde avant de croire un motif écrit il y a un mois.

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

📄 **Un aide-mémoire complet vit désormais dans `COMMANDES.md`**, à la racine —
donc sur la VM après un `git pull`, et lisible par `cat COMMANDES.md`. Chaque
commande y est précédée d'une phrase qui dit ce qu'elle AFFICHE, et **toutes
ont été exécutées avant d'y être écrites**. C'est là qu'il faut ajouter une
commande nouvelle, pas ici : ce document-ci raconte le projet, celui-là sert à
s'en servir.

⚠️ **Toujours `.venv/bin/python`, jamais `python`.** La VM n'a pas de binaire
`python` — seulement `python3`, et le paquet `python-is-python3` n'est pas
installé. `python -m …` répond « Command 'python' not found », et `python3 -m …`
tournerait hors du venv, donc sans les dépendances. Il n'y a pas non plus de
`source .venv/bin/activate` implicite : chaque commande porte son chemin.
Ce document a prescrit `python` nu pendant trois sessions ; les commandes
ci-dessous sont corrigées, mais toute commande recopiée d'une section ancienne
est à relire. Même famille que le §20.12 (`sqlite3` absent) et que la commande
du §21.6 : **une commande jamais exécutée n'est pas une commande, c'est une
intention.**

```bash
./doctor.sh                                  # état complet + cause de tout zéro
.venv/bin/python -m src.main clv-report                # LA mesure de rentabilité
.venv/bin/python -m src.main track-update              # suivi des paris joués -> CSV
.venv/bin/python -m src.main settle --from <csv>       # injecte les scores, calcule le P&L
.venv/bin/python -m src.main backfill-fair-lines       # rattrape les clôtures d'avant le correctif
.venv/bin/python -m src.main backfill-played-bets      # rattache les anciens clics à leur détection
.venv/bin/python -m src.main export-history --out <csv>   # détections + ligue + délai + overround + CLV
.venv/bin/python -m src.main features --premium         # CLV par championnat  (PAS `features-report`)
.venv/bin/python -m src.main corrections                # vitesse de correction par book (PAS `corrections-report`)
.venv/bin/python -m src.main export-tracking --out <db> # historique durable, transportable
.venv/bin/python -m src.main export-curves --out <csv> --days 7   # trajectoires (§15.1)
.venv/bin/python tools/book_revive_check.py             # quels books desactives repondent encore
.venv/bin/python -m src.main books-coverage --sport soccer,tennis   # events, horizon, ∩ Pinnacle
.venv/bin/python -m src.main betano-value-test --min-ev 0.5   # dry-run, sans alerte
.venv/bin/python -m src.main betano-coverage           # ce que contient le dump Betano
.venv/bin/python -m src.main betano-prematch-shape data/prematch/soccer.json
.venv/bin/python -m src.main inspect-json <fichier> --path a.b.0.c
.venv/bin/python -m src.main prune --days 2            # purge (VACUUM refusé si disque insuffisant)
./tools/detect-platform.sh https://www.bet777.be/fr

# Ponts navigateur — fraîcheur et cadence (les deux doivent être < 60 s)
ls -l data/circus/ data/prematch/ data/betano.json && date -u
sudo journalctl -u betano-ingest --since "5 min ago" --no-pager \
  | grep -oP "(?<=-> )\S+|(?<=prematch ')[a-z]+" | sort | uniq -c
sudo journalctl -u betano-ingest --since "30 min ago" --no-pager | grep -P "\] [45]\d\d "

# Détections par book sur une fenêtre donnée — pour juger un book récent, il
# FAUT restreindre la fenêtre : comparer 3 h à 7 jours donne 13 contre 1813.
# ⚠️ `sqlite3` est ABSENT de la VM (§20.12) : passer par le module Python.
.venv/bin/python -c 'import sqlite3,sys
q="SELECT book,COUNT(*) FROM value_bets WHERE detected_at>? GROUP BY book ORDER BY 2 DESC"
for r in sqlite3.connect("file:data/valuebet.db?mode=ro",uri=True).execute(q,(sys.argv[1],)):
    print("%-18s %s" % r)' 2026-07-30T16:30:00   # <- la fenêtre, à changer

node tools/circus-ingest.selftest.js tools/circus-ingest.user.js  # 7 scénarios

# Telegram — /scan et /book répondent dans les canaux du projet (§14.5, §15.3).
# Après toute modification de bot_listener.py : sudo systemctl restart valuebet-listener
sudo journalctl -u valuebet-listener --since "5 min ago" --no-pager | grep -v systemd

grep "done in" valuebet.log | tail -5        # durée des cycles (~17 s normal)
tac valuebet.log | awk '/══ CYCLE/{c++} c<5' | tac | grep -oiP '\b\w[\w ]*(?= skipped:)' | sort | uniq -c

# Tests — TOUJOURS avec le repertoire, jamais `pytest` seul (voir ci-dessous)
.venv/bin/python -m pytest tests/ -q
```

⚠️ **`pytest` lancé à la racine ne rend AUCUN test**, et le message n'a rien à
voir avec la cause. `test_alerts.py` à la racine n'est pas une suite pytest
mais le script d'envoi manuel du §20.6 ; il lève `SystemExit` à l'import quand
la config Telegram est absente, et pytest meurt en `INTERNALERROR` avant
d'avoir collecté quoi que ce soit — « no tests ran », sur un dépôt dont les 696
tests sont verts. **Toujours `pytest tests/`.** C'est aussi ce qui a fait
échouer la vérification de bout en bout du §20.6, restée en attente depuis le
18/08 : le script s'arrête sur une config vide avant d'atteindre la chaîne
qu'il est censé prouver.

### Services systemd

| Unité | Rôle |
|---|---|
| `valuebet-daemon` | Boucle de scan continue |
| `betano-ingest` | Réception des push navigateur |
| **`valuebet-listener`** | **Bouton « Jouer » + commande `/scan` (`bot_listener.py`)** |
| `valuebet-prune.timer` | Purge nocturne (rétention 2 jours) |
| `valuebet-close-lines.timer` | **Capture horaire des lignes de clôture** |

Les services s'appellent `valuebet-daemon` et `betano-ingest`, **pas**
`valuebet` ni `valuebet-ingest`. `betano-ingest` sert aussi Circus, malgré son
nom. Ses logs vont au journal systemd, il n'y a pas de fichier
`betano-ingest.log`.

⚠️ **`valuebet-listener` ne figurait pas dans ce tableau** jusqu'au 04/08 au
soir, alors qu'il tourne depuis juillet — le chercher a coûté un aller-retour.
Il n'a aucun fichier d'unité dans le dépôt, il n'existe que sur la VM. Un seul
process doit faire le `getUpdates` : deux instances se volent les updates et le
bouton « Jouer » devient erratique.

⚠️ `close-lines` doit tourner **plus souvent que la rétention**. Le prix de
clôture n'existe que dans notre propre capture (Pinnacle retire les marchés
prématch au coup d'envoi) — une fois les lignes purgées, le CLV de ces paris
est **définitivement perdu**.


### 4.1 Les sondes de `scripts/` — aucune n'était documentée ici

20 sondes existent, **aucune n'apparaissait dans ce document** : elles se
redécouvraient par `ls`, ou pas du tout. Toutes se lancent en
`.venv/bin/python -m scripts.<nom>` et acceptent `--help`. Aucune ne modifie
quoi que ce soit, sauf mention contraire.

⚠️ **Cette promesse était FAUSSE jusqu'au 21/08, pour quatre sondes sur vingt.**
`crossclose`, `book_health` et `check_tennis_totals` lisent un argument
POSITIONNEL et prenaient `--help` pour la donnée : `book_health --help`
répondait « Aucune détection pour --help sur la fenêtre », un message qui
envoie chercher un book absent au lieu d'aider. Et `repair_events --help`
**s'exécutait** au lieu d'afficher son aide — inoffensif parce que l'écriture
exige `--apply`, mais c'est la seule sonde qui écrit en base, donc le pire
endroit où poser ce défaut. Corrigé, et `tests/test_sondes_help.py` lance
les 15 sondes à arguments en sous-processus pour que la promesse reste vraie.

⚠️ `cycle_speed` et `magic_probe_report` n'ont volontairement AUCUN argument :
ils lisent `valuebet.log` et le répertoire des sondes, donc ne répondent que
sur la VM. Ils sont hors de ce test, pas oubliés.

**Une documentation qui promet une commande qui ne marche pas coûte plus
qu'une documentation muette** : elle fait perdre le temps de la vérifier.

**Mesurer la CLV et l'edge**

| sonde | ce qu'elle répond |
|---|---|
| `clv_split` | CLV découpée par book/sport/marché/ligue, jouable vs muet |
| `clv_independence` | la CLV mesure-t-elle autre chose que l'EV ? (§20.4, R² = 0,257) |
| `pnl_detections` | P&L sur TOUTES les détections, pas seulement les paris cliqués |
| `under_threshold` | quel seuil d'EV appliquer, mesuré par tranche (§21.12) |
| `clv_split --by cote` | **CLV par tranche de cote**, aux MÊMES bornes que `pnl_detections` — c'est la table à mettre en face du §21.17 |
| `crossclose` | rejuger les paris Smarkets contre la clôture de Pinnacle |
| `scores_coverage` | **quelle couverture de ligues une source de résultats doit avoir** (§21.16) — la seule sonde qui n'a besoin ni de clé ni de réseau |

**Comprendre un marché qui ne produit rien**

| sonde | ce qu'elle répond |
|---|---|
| `market_supply` | **où s'arrête** un marché stérile, étape par étape — l'outil à sortir en premier quand une détection manque |
| `market_expansion` | ce que les filtres écartent chez Pinnacle, par période et par type |
| `ev_outliers` | une EV énorme est-elle un cadeau du book ou une référence fausse ? |
| `check_half_time` | flux, échelles et CLV des mi-temps (§21.14) |
| `check_tennis_totals` | le correctif des totaux de jeux est-il en service ? (§21.1-21.5) |

**Relever des codes de marché sans deviner**

| sonde | ce qu'elle répond |
|---|---|
| `discover_half_time` | codes de mi-temps dans les dumps sur disque, **avec leur libellé** |
| `discover_half_time --detail <code>` | les NOMS D'ISSUES d'un marché — pourquoi un marché mappé rend peu |
| `discover_half_time_api` | idem pour les books en API directe (appels réels) |
| `handicap_conventions` | convention de ligne des handicaps, book par book (§21.13) |

**Santé et exploitation**

| sonde | ce qu'elle répond |
|---|---|
| `book_health` | un book est-il traité comme les autres, ou seulement collecté ? |
| `cycle_speed` | vitesse des cycles, lue dans `valuebet.log` |
| `repair_events` | recrée les lignes `events` des paris orphelins (**écrit en base**) |
| `magic_probe_report` / `magic_probe_show` | inventaire et forme des sondes MagicBetting |

⚠️ **`market_supply` mérite d'être connue par cœur.** C'est elle qui a montré
que zéro détection sur les mi-temps était le comportement JUSTE et non une
panne — elle sépare « pas de ligne juste en face », « book incomplet sur cette
ligne » et « EV qui n'atteint pas le seuil », trois causes qui donnent le même
symptôme : rien.

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

✅ **FAIT le 13/08 — voir §17.5.** Les trois leviers ci-dessous ont été
appliqués : appels groupés (mesurés jusqu'à 50 identifiants), horizon borné à
48 h, et sortie du cycle par cache de fond. Rafraîchissement mesuré à 14 s en
football et 5 s en tennis, contre les 26 minutes qui avaient motivé le retrait.

⚠️ **Texte historique ci-dessous, conservé pour la trace du raisonnement.** Le retrait
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

### Priorité 1 bis — une seconde référence sharp ✅ FAITE LE 13/08

⚠️ **Cette section est résolue — voir §17.5.** Smarkets tourne en production
depuis le 13/08, en repli strict derrière Pinnacle. Gain mesuré : +19 matchs de
tennis sur 57 pricés par Pinnacle, et 7 en football. Le raisonnement ci-dessous
reste valable et explique pourquoi c'était le bon candidat.

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

⚠️ **Liste dépassée — voir §14.12.** Conservée pour la trace de ce qui a été
tranché depuis : le 0 est résolu (Pinnacle répond 200), le 3 est tranché et
appliqué (§14.8), le 1 est réécrit (§14.6) et sa vérification reportée en
§14.12. Le 2 se heurte au défaut de découpage du doctor (§14.10). Le 5 a reçu
un premier morceau avec `/scan` (§14.5).

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
3. ✅ **`PINNACLE_MAX_REUSE_SEC=240` appliqué** — justifié par la mesure, voir
   §14.8. La bande premium sur `fair_odd` (§13.5) reste, elle, non tranchée.
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

---

## 14. Session du 04/08 au soir — Telegram, marchés en retard, mesure

Branche **`claude/resume-clarification-1541xa`**, neuf commits de `4f64b1f` à
`9c38213`. Quatre chantiers : le routage des canaux, une commande `/scan` sur
Telegram, la réécriture du détecteur de marchés en retard, et l'analyse
complète du CLV sur les données exportées.

⚠️ **La VM a changé de branche.** Elle suivait `claude/project-summary-2cz4sk`,
elle suit maintenant `claude/resume-clarification-1541xa`. Un `git pull` sur
l'ancienne branche répondrait « Already up to date » sans rien ramener —
toujours le piège du §13. Vérifier `git branch --show-current`.

### 14.1 La mesure qui fait autorité — 2 612 opportunités

⚠️ **PÉRIMÉE — voir §16.1.** Refaite le 11/08 sur 4 365 opportunités
(21/06 → 10/08) : le canal premium y fait **+10,40 % à 22,9 σ**. Les chiffres
ci-dessous restent lisibles comme point de comparaison historique, mais ne
doivent plus être cités. La méthode, elle, est inchangée.

Analyse de `export-history` sur 18 751 lignes couvrant le 21/06 → 04/08.

⚠️ **Seuls 9 jours sont exploitables.** Le CLV dévigé n'existe qu'à partir du
**27/07** — avant le 24/07 la couverture est de 0 %, `backfill-fair-lines` ne
pouvant rattraper que les clôtures dont les cotes n'avaient pas été purgées.
Toute affirmation portant sur « un mois de données » est fausse.

Entonnoir, à refaire à l'identique la prochaine fois :

| | n |
|---|---|
| Lignes du fichier | 18 751 |
| … avec un CLV dévigé | 5 543 |
| … prématch (délai > 0) | 4 619 |
| **→ opportunités dédupliquées** | **2 612** |
| Matchs distincts | 1 949 |

**1,77 ligne par opportunité.** Sans déduplication, tous les effectifs sont
gonflés de 77 % et toutes les significativités avec. Clé de déduplication :
`event_key + Marché + Pari`, en gardant la meilleure cote. Les 924 détections à
délai négatif sont retirées : elles comparent une cote live à une ligne
Pinnacle prématch morte depuis le coup d'envoi.

| Population | n | CLV | σ | positifs |
|---|---|---|---|---|
| Toutes opportunités | 2 612 | +7,36 % | 21,1 | 75 % |
| **Canal premium** | **858** | **+10,18 %** | **18,4** | 80 % |
| — voie cotes 1,5-4 (EV ≥ 8 %) | 771 | +8,67 % | 16,8 | 79 % |
| — **voie cotes 4-6 (EV ≥ 20 %)** | 87 | **+23,58 %** | 9,1 | **91 %** |

**La voie « cotes hautes » rend près de trois fois plus que la voie normale.**
C'est de loin le meilleur segment du système.

### 14.2 Ce que confirme la ventilation

**L'EV est informative**, croissance quasi monotone : 5-8 % → +3,85 %
(n=1279) ; 8-10 % → +6,42 % ; 12-15 % → +8,60 % ; 15-20 % → +12,56 % ;
20-30 % → +18,78 % ; > 30 % → +33,67 % (n=83).

**Le délai ne justifie toujours aucun couperet.** < 2 h +7,61 % ; 6-12 h
+9,75 % ; 12-24 h +6,65 % ; **24-48 h +3,87 % (3,3 σ, significativement
positif)** ; > 48 h +5,37 %. Troisième mesure consécutive à confirmer qu'il ne
faut pas couper cette tranche.

**Le tennis tient sa promesse** : +9,49 % sur 485 opportunités, **91 %
positives**, contre +6,85 % au football sur 2 120. Moins que les +13,50 %
annoncés en juillet, mais sur cinq fois plus de données. C'est l'argument le
plus fort pour la seconde référence sharp — Pinnacle ne price que ~71 matchs de
tennis, il plafonne le gisement le plus rentable.

⚠️ **La cote n'est pas un facteur indépendant de l'EV.** Croisement des deux :

| | cote 1,5-3 | cote 3-6 | cote > 6 |
|---|---|---|---|
| EV 5-10 % | +4,5 % (952) | +3,7 % (532) | +8,0 % (98) |
| EV 10-20 % | +7,4 % (230) | +8,9 % (341) | +10,6 % (109) |
| EV 20 %+ | +31,2 % (23) | +22,5 % (130) | +24,2 % (87) |

L'EV domine largement ; la cote ajoute un résidu réel mais modeste. Une cote
haute ne vaut pas par elle-même, elle vaut parce qu'elle porte souvent une
grosse EV. L'hypothèse du §9 (« probablement le même effet vu deux fois ») est
confirmée.

⚠️ **Les championnats ne disent toujours rien.** 81 % des détections tombent
dans la catégorie « autre » (2 114 / 2 612) : la catégorisation ne discrimine
pas. Les autres catégories font 14 à 88 opportunités, et sur dix catégories
testées l'étalement observé est dans ce que le hasard produit. Règle du §9
inchangée.

### 14.3 Un trou de routage — 739 opportunités n'atteignent aucun canal

⚠️ **PÉRIMÉE — voir §16.2.** Le trou est bien plus gros que ce qui est décrit
ici : **2 917 opportunités sur 4 365**, dont deux poches à ouvrir (cote 4–6
EV 8–20 à +7,68 % sur 403, et cote > 6 EV 20–35 à +17,35 % sur 76).

| Canal | n | CLV |
|---|---|---|
| Premium | 858 | +10,18 % |
| Critique | 36 | +34,14 % |
| Principal | 979 | +4,08 % |
| **Aucun canal** | **739** | **+7,12 %** |

Le cas le plus net : **cote > 6 avec 20 % ≤ EV < 35 %**. Trop haute pour le
premium (qui s'arrête à 6), pas assez pour le critique (qui commence à 35 %).
Ce segment donne **+15,08 % sur 45 opportunités**, et l'ensemble des cotes > 6
à EV ≥ 20 % donne **+23,55 % sur 81** — exactement la performance de la voie
premium 4-6.

**La voie « cotes hautes » s'arrête arbitrairement à 6 alors que la mesure dit
qu'elle continue au-delà.** Deux façons de récupérer ça, non tranchées :
étendre `TELEGRAM_PREMIUM_HI_MAX_ODD` à 10 ou 15, ou descendre
`TELEGRAM_MIN_CRITICAL_EV` de 35 % à 20 % pour les cotes hors bandes.

### 14.4 Routage des canaux — ce qui a changé

**Critique = débordement du premium, plus un doublon.** Le canal critique
garde son absence de limite de cote — c'est sa raison d'être, les cotes 14, 21
à EV énorme doivent y arriver — mais il ne reçoit plus ce que le premium a déjà
pris. Une cote 1,82 à 53 % d'EV est du premium, et une seule alerte suffit.
L'exclusion porte sur la **livraison**, pas l'éligibilité : si le canal premium
n'est pas configuré, le critique rattrape le pari plutôt que de le perdre.

**Les surebets ne vont plus en premium**, quelle que soit la marge. Ils ont
leur canal. La copie vers le critique au-delà de `min_critical_surebet_pct` est
conservée, elle n'a pas été remise en cause.

`max_ev_pct` reste à 1000 % : **aucun plafond d'EV**, c'est un choix explicite
de l'utilisateur. Une EV de 50, 60 ou 80 % est voulue.

### 14.5 `/scan` — les value bets encore jouables

`bot_listener.py` écoute désormais les messages en plus des clics. `/scan`
renvoie la liste de ce qui est jouable maintenant, au format des alertes, avec
un bouton **« ▶️ Tout jouer (N) »** qui enregistre d'un coup tous les paris du
message — le scan suivant est alors vide.

Règles de sélection, toutes calquées sur l'existant :
- **critères du canal premium uniquement** (`is_premium()`), pas ceux du
  principal — les 5-8 % d'EV n'ont rien à faire dans cette liste ;
- marché non joué, via le même `_load_played_keys()` que les alertes, donc la
  suppression au niveau du **marché** s'applique : jouer le 1 d'un 1X2 retire
  le X et le 2 du scan suivant ;
- plus de `min_minutes_to_kickoff` avant le coup d'envoi ;
- une seule ligne par opportunité, au meilleur prix.

⚠️ **`detected_at` ne bouge jamais.** `insert_value_bet` ne crée qu'UNE ligne
par opportunité et renvoie l'existante sans rien écrire quand le daemon la
redétecte. Filtrer sur `detected_at` sélectionne donc les paris *nouvellement
apparus*, pas les paris *encore vivants* — l'inverse de ce qu'on veut. Trois
colonnes ajoutées : `last_seen_at`, `last_odd`, `last_ev`, rafraîchies à chaque
re-détection. `detected_at`, `odd_taken` et `ev_pct` restent ceux de la
première détection : tout le CLV compare la clôture à l'EV de départ, et les
réécrire rendrait le CLV faux — l'erreur même du §1.

Réglage : `SCAN_WINDOW_MIN` (10 min par défaut). Assez pour traverser un recul
Pinnacle, assez serré pour qu'un pari mort ne traîne pas.

### 14.6 Marchés en retard — mesurer au lieu de deviner

Le détecteur du §13.7 prouvait une seule chose : que la cote n'avait pas bougé
depuis le coup d'envoi. Ça ne dit **rien** de la valeur du pari. Deux
situations la produisent :

- le book a oublié de suspendre, c'est 1-1 à la 19ᵉ, le marché est tranché →
  exploitable ;
- le book n'a pas encore repricé, il est 0-0 et rien ne s'est passé → aucun
  edge.

Le canal recevait surtout des seconds. **Le manque de fond : il n'existe aucune
référence LIVE dans le système**, le scraper Pinnacle ignorant `isLive`.

`src/live_consensus.py` fabrique une référence de substitution : sur un match
commencé, les autres books pricent en direct et le daemon collecte déjà leurs
cotes. On sépare les books **figés** des books qui ont **bougé**, on dévige les
seconds, et on mesure l'écart du prix figé contre cette ligne. Sous
`LATE_MARKET_MIN_EDGE` (15 %), silence.

Choix qui décident du résultat :
- la moyenne porte sur les **probabilités dévigées**, pas sur les cotes —
  moyenner des cotes mélange des marges et penche vers le book le plus gourmand ;
- **un book ne nourrit jamais son propre consensus**, sa cote figée tirerait la
  référence vers le prix périmé ;
- deux books vivants minimum ;
- un marché incomplet est rejeté par sa somme d'implicites (≤ 1), sans avoir à
  savoir combien d'issues il devrait porter ;
- **seul le bon côté sort** : sur un 1X2 figé à 2,40 des deux côtés, le
  domicile vaut +73 % et l'extérieur −33 %.

⚠️ **Le BTTS n'est collecté par aucun scraper.** Le cas d'origine — « les deux
équipes marquent » sur un 1-1 — reste donc indétectable. Il avait été exclu
partout parce que Pinnacle ne le price pas ; le consensus live lève cet
obstacle en principe, mais il faut d'abord ajouter la collecte chez Circus et
Betano.

### 14.7 Pinnacle est derrière Cloudflare — le test d'IP, à faire un jour

**C'est le point à reprendre en priorité sur ce sujet.**

Découvert le 04/08 : ouvrir l'API Pinnacle dans un navigateur renvoie une page
**Cloudflare** « Sorry, you have been blocked ». Le projet ne savait pas que
Pinnacle était derrière un anti-bot. Ça rend l'hypothèse du §12 — filtrage
d'ASN, comme DataDome pour Betano et le blocage Gaming1 pour Circus —
**nettement plus plausible**. Un 403 n'est d'ailleurs pas le code d'une
limitation de débit ; ce serait 429.

⚠️ **Ouvrir l'URL dans un navigateur ne teste RIEN.** La navigation n'envoie ni
`X-API-Key`, ni `Origin`, ni `Referer` — Cloudflare classe robot sur ce seul
critère, quelle que soit l'IP. Le blocage obtenu ainsi ne prouve pas que l'IP
résidentielle est refusée. C'est l'erreur commise une fois ; ne pas la refaire.

**Le protocole correct :**

1. Installer `tools/pinnacle-ip-test.user.js` dans le Tampermonkey du
   navigateur qui héberge les onglets Betano et Circus (celui sur l'IP
   résidentielle). Il rejoue les cinq en-têtes de `_headers()` à l'identique,
   via `GM_xmlhttpRequest` — le seul moyen de ne pas buter sur le CORS. Il ne
   se déclenche que par le menu Tampermonkey, aucun trafic parasite.
2. Abaisser temporairement le seuil d'alerte pour être prévenu des coupures
   courtes — les 403 observés durent 3 à 4 minutes, loin des 20 min par défaut :
   ```bash
   sed -i '/^PINNACLE_ALERT_AFTER_MIN=/d' .env && echo 'PINNACLE_ALERT_AFTER_MIN=3' >> .env
   sudo systemctl restart valuebet-daemon
   ```
   Le canal critique reste lisible : une seule alerte par panne.
3. Au prochain `🚨 Pinnacle muet`, lancer **🎯 Tester Pinnacle depuis cette IP**
   dans le menu Tampermonkey, **pendant que la panne dure**.
4. Remettre `PINNACLE_ALERT_AFTER_MIN=20` ensuite.

| VM | Maison | Conclusion | Suite |
|---|---|---|---|
| 403 | **200** | filtrage d'IP/ASN | l'espacement ne servira à rien ; il faudra `PINNACLE_PROXY` / `PINNACLE_LOCAL_IP`, ou un pont navigateur comme pour Betano |
| 403 | 403 | quota sur la clé d'API publique | l'espacement est le bon levier, les réglages actuels sont déjà la bonne réponse |

### 14.8 Réglages tranchés par la mesure

`tools/line_speed.py` donne **99,6 % de cotes Pinnacle inchangées à 60 s**
d'intervalle, 99,1 % à 120 s, 95,2 % à 300 s. Le coût d'une référence
légèrement périmée est donc de l'ordre de 0,01 à 0,09 point d'EV.

Appliqués dans `.env`, et justifiés :

```
PINNACLE_MIN_INTERVAL_SEC=60      # quasi gratuit : 99,6 % de cotes identiques
PINNACLE_MAX_REUSE_SEC=240        # 60 + 120 <= 240 : un 403 isolé ne crée plus
                                  # de trou de détection. Coût ~0,05 point d'EV.
```

⚠️ La valeur par défaut de `PINNACLE_MAX_REUSE_SEC` est **150**, pas 240. Sans
override, un recul après 403 peut dépasser la durée de réutilisation : le cache
meurt et plus rien n'est détecté. Règle de dimensionnement à garder :
**plafond de recul < MAX_REUSE**.

### 14.9 Vitesse de correction des books — combien de temps pour cliquer

`.venv/bin/python -m src.main corrections` (le nom réel de la commande est `corrections`,
**pas** `corrections-report` comme l'annonçait le §13.6 ; de même `features` et
non `features-report`).

| Book | suivis | médiane jouable | médiane alignement | CLV (§14.1) |
|---|---|---|---|---|
| **Unibet** | 336 | **34 min** | 46 min | +8,26 % |
| StarCasino | 334 | 7 min | 8 min | +7,09 % |
| Ladbrokes | 109 | 7 min | 24 min | +11,44 % |
| Circus | 84 | 6 min | 15 min | +9,14 % |
| Betano | 165 | 5 min | 25 min | +5,21 % |
| **Napoleon** | 141 | **4 min** | 9 min | +4,95 % |

Unibet est **cinq à huit fois plus lent** que tous les autres — bon edge ET le
temps de réagir. Napoleon cumule le moins bon edge et la fenêtre la plus
courte. `line_speed` classe les books dans le même ordre indépendamment
(Unibet 98,1 % de cotes inchangées à 300 s, Napoleon 94,0 %).

Conséquence pratique : **une alerte Napoleon non jouée dans les cinq minutes
est perdue**, une alerte Unibet attend une demi-heure. Ça renforce
l'orientation du §6 — moins de paris, plus gros, en privilégiant ceux dont la
fenêtre laisse le temps de miser correctement.

### 14.10 Deux défauts d'outillage à corriger

⚠️ **`pinnacle_doctor.py` découpe mal les runs.** Un run affiché à « 2 034
cycles entre 18:32:38 et 18:41:08 » avec une médiane de 20 s est
arithmétiquement impossible (un quart de seconde par cycle). Les horodatages du
journal ne portent pas la date, et le découpage mélange plusieurs jours. **Ne
pas se fier aux agrégats par run tant que ce n'est pas corrigé** — c'est le
piège du §13.4 dans l'outil censé l'éviter.

⚠️ **Les noms de books sont dédoublés dans `paris_track.csv`.** « Ladbrokes »
et « ladbrokes_be », « StarCasino » et « starcasino_sport », « Unibet / 711 /
Bingoal / Scooore » et « unibet_be » coexistent. Deux écrivains, deux formats :
`bot_listener` enregistre le nom d'affichage au clic, `track-update` réécrit
avec la valeur brute de l'enum. Toute analyse par book sur ce fichier est
coupée en deux.

### 14.11 Pièges rencontrés dans cette session

- **Un canal Telegram ne délivre pas ses messages en `message` mais en
  `channel_post`.** `/scan` ne recevait donc rien, et un update jamais livré ne
  laisse aucune trace nulle part. `allowed_updates` couvre désormais les deux.
- **`bot_listener` est lancé par systemd sans `EnvironmentFile`.** Sans
  chargement explicite de `.env`, `TelegramConfig.from_env()` renvoyait `None`
  et `/scan` n'acceptait aucun chat. Ce qui masquait la panne : `load_token()`
  lit `.env` pour son propre compte, donc le service démarrait normalement. Le
  chargeur est maintenant partagé dans `src/config.load_env_file()`.
- **`reply_markup=None` part en JSON `null`, que l'API refuse.** Le scan vide
  était le seul message sans bouton, donc le seul rejeté — et `tg()` ne
  regardait jamais la réponse de Telegram. Tout refus est désormais journalisé.
- **Sous systemd, Python tamponne stdout par blocs de 4 Ko.** Un message
  imprimé à 08:09 n'est arrivé au journal qu'à 16:10, à l'arrêt du process.
  `sys.stdout.reconfigure(line_buffering=True)` au démarrage.
- **`teams.display()` a besoin de `teams.init(storage)`**, sinon il retombe sur
  `.capitalize()` et rend « Clubbrugge ». Le daemon l'initialise, les autres
  services doivent le faire aussi.

### 14.12 À faire au prochain démarrage

⚠️ **Liste dépassée — voir §15.8.** Le point 1 est déployé et vérifié, le 5
est fait (§15.1). Le reste est repris là-bas.

1. **Vérifier les compteurs du détecteur de marchés en retard** après une
   soirée complète — les deux nouvelles causes de rejet disent si le seuil est
   bon :
   ```bash
   grep "marchés en retard —" valuebet.log | tail -20
   ```
   `écart_faible` doit absorber les anciens faux positifs. Si `retenue` reste à
   0 sur toute une soirée de football, le seuil de 15 % est trop haut.
2. **Le test d'IP Pinnacle** (§14.7) — le seul qui tranche entre filtrage d'ASN
   et quota, et il conditionne tout le reste sur ce sujet.
3. **Trancher le trou de routage** des cotes > 6 à EV 20-35 % (§14.3).
4. **Smarkets comme seconde référence sharp** (§6) — reste le plus gros levier.
   Le tennis à +9,49 % avec 91 % d'opportunités positives est plafonné par
   Pinnacle seul.
5. Corriger les deux défauts d'outillage du §14.10.
6. **Résultats automatiques** — toujours aucune source de scores, donc aucun
   P&L réel dans `paris_track.csv` (997 paris, 0 résultat rempli). Pas
   bloquant : sur cette durée le P&L mesure la chance, le CLV mesure l'edge.

---

## 15. Session du 06/08 — courbes complètes, deux books retrouvés

Branche **`claude/resume-clarification-1541xa`**, de `78194c5` à `83bc817`.
Cinq chantiers : la conservation de toutes les données, le filtre d'alertes par
book, la réactivation de deux scrapers, et **deux pannes silencieuses trouvées
qui perdaient le tennis de deux books entiers**.

### 15.1 Tout conserver — `odds_history`

Demande de l'utilisateur : garder chaque changement de cote, les horodatages,
le book, le marché, le délai, la clôture, le CLV. Pour tracer des graphes et
préparer un modèle.

**Rien de tout ça ne survivait.** `quotes` porte bien chaque cote de chaque
cycle, mais pèse 20 Go pour deux jours et se purge chaque nuit — l'historique
était détruit avant d'avoir pu servir. `bet_corrections` survit mais ne garde
que deux jalons : deux points, pas une courbe.

Table `odds_history`, **permanente, jamais purgée** :

| Colonne | |
|---|---|
| `value_bet_id` | identifie la SÉLECTION suivie |
| `book` | de qui est cette cote — **tous** les books, Pinnacle compris |
| `seen_at`, `odd` | l'instant et le prix |
| `fair_odd`, `ev_pct` | la référence AU MÊME INSTANT, et l'EV recalculée |

⚠️ **Une ligne par CHANGEMENT, jamais par cycle.** `line_speed.py` mesure 97 à
99 % de cotes identiques d'un cycle à l'autre : écrire chaque cycle
multiplierait le volume par cinquante pour répéter la même valeur. **~0,2 Go
par an au lieu de 128** — c'est ce qui rend la chose possible. La série
complète se reconstruit en propageant la dernière valeur connue : entre deux
points la cote n'est pas inconnue, elle est constante.

Trois choix qui décident du résultat :
- **`fair_odd` est la référence de l'instant**, pas celle de la détection. Sans
  elle on verrait le book bouger sans savoir s'il rejoint la référence ou si
  c'est la référence qui est venue à lui.
- **Pinnacle est une série comme les autres**, avec sa cote AFFICHÉE ;
  `fair_odd` porte à part la même ligne dévigée. L'écart entre les deux est la
  commission (6,6 % en médiane), pas un edge — d'où l'absence d'EV sur cette
  série.
- **Une sélection détectée sur trois books n'écrit QU'UNE courbe.** Sans cette
  déduplication, chacun des trois suivis relèverait les sept books.

Le suivi court **jusqu'au coup d'envoi**, plus jusqu'à l'alignement : un book
qui rejoint la ligne juste en dix minutes cessait d'être observé pendant les
six heures suivantes, alors que c'est là que le marché se forme. Borne d'âge à
168 h (`CORRECTIONS_MAX_AGE_HOURS`) parce que 41 % des sélections ont un coup
d'envoi à plus de 48 h. **Coût mesuré : 274 ms par cycle sur 581 suivis** — le
suivi n'est pas ce qui ralentit les cycles.

Export : `export-curves --out <csv> --days 7`, plat, groupable par
`value_bet_id` et `Book`. `--filled` rééchantillonne à la minute.

⚠️ **La purge ne touche à rien de tout ça.** Vérifié dans le code : elle ne
supprime que `quotes` (2 jours) et les `notified_*` (30 jours). `odds_history`,
`bet_features`, `bet_corrections`, `value_bets`, `clv_snapshots`,
`played_bets`, `events` sont permanents. `export-tracking` les emporte tous.

### 15.2 Golden Palace et BetFirst réactivés

`tools/book_revive_check.py` a tranché — **les motifs de désactivation
vieillissent, et deux étaient trompeurs** :

| Book | Motif inscrit | Réalité du 06/08 |
|---|---|---|
| Golden Palace | « compte limité » | ne concernait que le PARI. Son API Altenar ne demande **aucune authentification**. 1,8 s, 6 729 cotes, 1 354 événements |
| BetFirst | « 403 depuis le VPS » | le blocage est tombé. Mais **80 secondes** de collecte |

**Golden Palace** est la 2ᵉ meilleure couverture du portefeuille (831
événements partagés avec Pinnacle en football, contre 847 pour StarCasino).
Jamais mesuré en CLV faute de données ; il l'est maintenant.

**BetFirst** paginait séquentiellement, jusqu'à 50 pages sur 7 jours. Or
`_fetch_all_parallel` attend TOUS les books avant de rendre la main : le cycle
serait passé de 20 s à 80 s — exactement ce qui a fait retirer Smarkets (§5).
Deux corrections, puis une troisième :
1. pages en parallèle, **4 workers seulement** (ce book avait été coupé sur un
   403 ; cinquante requêtes simultanées seraient le meilleur moyen de le
   retrouver) ;
2. horizon de 7 à 3 jours ;
3. **sorti du cycle** : servi depuis un cache rafraîchi en fond
   (`BETFIRST_REFRESH_SEC`), le cycle ne l'attend jamais. Au-delà de
   `BETFIRST_MAX_AGE_SEC` le cache renvoie RIEN plutôt que des cotes mortes.

⚠️ **BetFirst est le book aux PIRES prix** : −3,20 points de CLV à sélection
identique, meilleur prix 15 % du temps. Il est là pour la DONNÉE — consensus
live, surebets, features — pas pour être joué. Le décocher dans `/book` est le
bon réglage.

⚠️ **Coupe-circuit par book, sans déploiement** : `BOOKS_DISABLED=betfirst`
dans `.env`, puis redémarrage du daemon. Liste séparée par des virgules,
insensible à la casse, et un nom inconnu est signalé au journal — sans quoi un
réglage mal orthographié ne couperait rien et ne dirait rien. Il coupe la
COLLECTE, donc les données : pour ne faire taire que les alertes en continuant
de mesurer, c'est `/book` (§15.3).

⚠️ **`books-coverage` sous-déclare systématiquement BetFirst** : la commande
prend un instantané, et le cache de fond est froid au premier appel. Relancer
deux minutes plus tard. Ce n'est pas une panne.

**Bet777 et Betcenter restent dehors**, sur décision de l'utilisateur. Betcenter
répond pourtant — 40 313 cotes, six fois plus que les autres — mais ses cotes
sont fausses, et un book faux ne pollue pas que les alertes : il fausse aussi
les surebets et le consensus live des marchés en retard.

### 15.3 `/book` — choisir qui alerte, sans jamais couper la collecte

Commande Telegram : une liste à cocher, un bouton par book, plus « Tout
activer » / « Tout couper ». Le clavier se redessine en place, l'effet est
immédiat sans redémarrer le daemon.

⚠️ **Le filtre est à UN seul endroit : dans `send_value_bet`, après la
détection.** Un book décoché continue d'être scrapé, stocké dans `value_bets`,
suivi dans `odds_history`, mesuré en CLV et sorti par tous les exports. C'est
la condition posée par l'utilisateur, et un test la verrouille explicitement.

Table d'**exceptions** (`book_alerts_off`), pas d'inscriptions : un book absent
alerte normalement. Ajouter un scraper ne demande donc rien, et une erreur de
lecture fait alerter — un book muet par accident coûte bien plus qu'une alerte
de trop. La liste des books est dynamique : ceux ayant produit une détection
sur sept jours.

### 15.4 Les noms inversés — deux books dont tout le tennis était perdu

**La trouvaille de la session.** Mesure de couverture du tennis, après
rapprochement flou :

| Book | avant | après |
|---|---|---|
| Ladbrokes | **1** match partagé sur 138 | **33** |
| BetFirst | **3** sur 88 | **24** |
| les six autres | 36 à 47 | inchangés |

Cause : Eurobet (Ladbrokes) et BetFirst nomment les joueurs **NOM D'ABORD,
séparés par une virgule** — « Griekspoor, Tallon », « Hontama, Mai » — alors
que Pinnacle écrit prénom d'abord.

⚠️ **Pourquoi c'était invisible, et pourquoi j'ai d'abord écarté cette piste.**
`team_similarity` utilise `token_set_ratio`, qui ignore l'ordre des mots et
donne **100** sur ces paires. Elle n'est donc pas en cause. Mais le
rapprochement compare des fragments de **CLÉ**, et `event_key` colle les mots :

```
tallongriekspoor  vs  griekspoortallon   ->  76,9   REJETÉ (seuil 85)
alexmichelsen     vs  michelsenalex      ->  81,8   REJETÉ
zizoubergs        vs  bergszizou         ->  66,7   REJETÉ
```

Un seul token, l'ordre redevient décisif, le score s'effondre. **Tous rejetés.**
Un book qui répond, un scraper qui tourne, des centaines de cotes par cycle, et
presque rien qui en sort — sans une seule erreur nulle part.

Correction : `src/matcher.swap_surname_first()`, appliqué **par
`normalize_team`**. Deux raisons de le placer là plutôt que dans les scrapers :
il doit s'exécuter AVANT l'effacement de la ponctuation (après, la virgule a
disparu et les deux formats sont indiscernables), et tout book adoptant cette
convention est couvert sans qu'on ait à le découvrir.

Deux gardes : une seule virgule (les paires de double « Bolelli S / Vavassori A »
n'en portent pas, un nom de club jamais), et au plus deux mots après la virgule.
Le football, déjà apparié à 573 événements, n'est pas touché.

**Bilan tennis de la journée** — environ 140 paires book × match récupérées ou
créées, dans le sport au meilleur CLV (+9,49 %, 91 % d'opportunités positives) :
Ladbrokes +32, BetFirst +21, Golden Palace +43 (nouveau), Betano +42 (onglet
remis en service).

### 15.5 Marchés en retard — mesurer au lieu de deviner (rappel §14.6)

Déployé et vérifié. Les compteurs par cycle portent désormais deux causes de
rejet supplémentaires, `sans_consensus` et `écart_faible` — c'est cette
dernière qui doit absorber les anciens faux positifs.

### 15.6 MagicBetting — Digitain, pas Gaming1

⚠️ **PÉRIMÉE — voir §18.6.** Le chiffrement est levé : c'est un module
WebAssembly, qu'on exécute au lieu de le casser. MagicBetting est en
production depuis le 15/08.

Le §6 annonce « Bet777 et Magic Betting : même plateforme Gaming1, seul `ROOM`
change ». **C'est faux pour MagicBetting.** Les HAR montrent
`sport-ak.bldiframe.com` et `sentry.digitain.tools` : c'est **Digitain**.

Bonne nouvelle — ce n'est pas un jumeau de Circus, donc pas le scénario
Unibet/711/Bingoal/Scooore. C'est une source de prix réellement indépendante.

Mauvaise nouvelle : **toutes les réponses sont chiffrées**.
`{"payload": "...", "timestamp": ...}`, entropie 7,98 bits/octet. Mesures
faites, à ne pas refaire :
- ni gzip, ni zlib, ni deflate ;
- **pas un chiffrement par blocs** — les longueurs ne sont pas multiples de 16,
  tous les restes apparaissent. C'est du flot ;
- **la clé ne dérive pas du seul timestamp** : deux réponses partageant le
  timestamp `1785958924`, XORées, donnent 0,8 % d'octets nuls — le hasard pur.

L'API est publique, sans authentification (juste un `Referer`), et derrière
Cloudflare. La clé est dans le bundle JS de l'iframe, qui n'apparaît dans aucun
HAR capturé — il vient du cache. `bootstrapper.min.js` n'est qu'un chargeur, il
ne contient rien.

**Deux voies si le sujet revient** : récupérer le bundle (Sources → déplier
`sport-ak.bldiframe.com`, ou « Disable cache » puis rechargement forcé) pour
réimplémenter le déchiffrement côté serveur ; ou un pont navigateur comme
Betano et Circus, qui lit les données après que l'application les a déchiffrées
— plus robuste à une rotation de clé, mais au prix d'un **troisième onglet
permanent**.

### 15.7 Pièges de cette session

- **Un remplacement de texte qui ne remplace rien.** Le bloc `/book` n'a jamais
  été inséré : la chaîne visée différait de deux mots de celle du fichier. La
  commande tombait donc dans le code du `/scan` et **répondait un SCAN**. Aucune
  erreur, aucune trace. Toujours utiliser un outil d'édition qui échoue quand
  le motif est absent, et **écrire le test avant de déployer**.
- **Une sonde avant une réactivation.** Sans `book_revive_check.py`, BetFirst
  serait parti en production avec ses 80 secondes, et les cycles auraient
  quadruplé — on l'aurait découvert par les symptômes.
- **`books-coverage` ne voit pas les books à cache de fond.** Deux minutes
  d'attente, ou on conclut à tort qu'un book est mort.

### 15.8 À faire au prochain démarrage

⚠️ **PÉRIMÉE — voir §16.8.** Liste refaite le 11/08 après l'analyse, avec les
priorités classées par rapport sur effort. Un seul point ci-dessous a été
tranché depuis : le volume des courbes (point 5), confirmé à 69 105 lignes
d'`odds_history` sur cinq jours.

1. **Smarkets** (§6) — reste le plus gros levier. Le tennis vient de gagner
   140 paires et reste plafonné par les ~62 matchs que Pinnacle price seul.
   Une maintenance Pinnacle de trois heures le 06/08 a de nouveau tout arrêté :
   c'est le cinquième argument.
2. **Le trou de routage** (§14.3) : cotes > 6 à EV 20-35 %, 45 opportunités à
   +15 % qui n'atteignent aucun canal. Non tranché.
3. **Les deux défauts d'outillage** (§14.10) : découpage en runs du
   `pinnacle_doctor`, noms de books dédoublés dans `paris_track.csv`.
4. **Le test d'IP Pinnacle** (§14.7) — toujours en attente d'un vrai 403. La
   coupure du 06/08 était un 503 de maintenance, qui ne discrimine rien.
5. **Volume des courbes** — vérifier après une semaine pleine :
   `SELECT COUNT(*) FROM odds_history`. Projeté à ~0,2 Go/an, à confirmer.
6. **Résultats automatiques** — toujours aucune source de scores, donc aucun
   P&L réel. Seul manque de la liste du §15.1 qui reste hors d'atteinte.

---

## 16. Session du 11/08 — l'analyse complète des trois exports

Aucun code modifié cette session : c'est une session de **mesure**. Trois
fichiers exportés de la VM (`curves.csv` 61 757 points, `detections.csv`
22 158 lignes, `tracking.db` 23 Mo) croisés pour répondre à une question :
« est-ce que ma CLV est toujours incroyable ? ».

**Rapport visuel complet, avec tous les graphiques :**
https://claude.ai/code/artifact/255c254a-32e3-4dfc-b275-03b8a7cd961e

Il contient les courbes d'alignement (une couleur par book, annotées), les
courbes de survie, la CLV par book / délai / EV / cote / ligue, la répartition
positif-négatif par book, le trou de routage et la tendance quotidienne.

### 16.1 La mesure qui fait autorité — remplace §14.1

⚠️ **PÉRIMÉE — voir §17.2.** Refaite le 13/08 sur 5 093 opportunités : le canal
premium y fait +10,13 % à 23,0 σ. Les chiffres ci-dessous restent lisibles
comme point de comparaison.

⚠️ **§14.1 est périmé.** Elle portait sur 2 612 opportunités du 21/06 → 04/08.
Cette section porte sur **4 365 opportunités** du 21/06 → 10/08. Toute
affirmation chiffrée doit venir d'ici.

Entonnoir, à refaire à l'identique la prochaine fois :

| | n |
|---|---|
| Lignes du fichier | 22 158 |
| … avec une clôture Pinnacle dévigée | 8 950 (40 %) |
| … prématch (délai > 0) | 8 000 |
| **→ opportunités dédupliquées** | **4 365** |
| Matchs distincts | 3 297 |

**1,83 ligne par opportunité** (1,77 au 04/08). Clé de déduplication inchangée :
`event_key + Marché + Pari`, meilleure cote gardée. Sans elle, tous les
effectifs sont gonflés de 83 % et les σ multipliées par 1,35.

| Périmètre | n | CLV | σ | % positives |
|---|---|---|---|---|
| Toutes opportunités | 4 365 | **+7,03 %** | 26,3 | 74 % |
| **Canal premium** | 1 397 | **+10,40 %** | 22,9 | 80 % |
| … voie 1,5–4 (EV ≥ 8) | 1 255 | +8,86 % | 21,2 | 79 % |
| … voie 4–6 (EV ≥ 20) | 142 | **+24,00 %** | 10,8 | 88 % |
| Paris réellement joués | 485 | +12,05 % | 12,1 | 78 % |

**L'edge tient et progresse.** Le 04/08 : +10,18 % sur 858 opportunités
premium. Le 10/08 : +10,40 % sur 1 397. Avec 63 % de données en plus, le
chiffre gagne un dixième au lieu de se diluer — c'est la signature d'un edge
réel. Coupé en deux : +9,23 % avant les 14 derniers jours (204 opportunités),
+10,60 % sur les 14 derniers (1 193). Moyenne glissante 5 jours : +9,8 % fin
juillet → +11,5 % le 08/08, sans un seul jour négatif.

### 16.2 Le trou de routage, enfin chiffré — remplace §14.3

⚠️ **Conclusion révisée le 13/08 — voir §17.4.** L'élargissement du premium
n'apporte plus qu'un gain de taux de +0,08 point, contre +0,40 annoncé ici. Il
reste bon pour le VOLUME (+39 %), pas pour le taux. Et une case franchement
négative est apparue : cote > 6 à EV 5-10 %, −2,30 % sur 175.

§14.3 annonçait « 45 opportunités à +15 % ». Le vrai chiffre est bien plus
gros. En rejouant les seuils de production (premium : cote 1,5–4 EV ≥ 8 et
cote 4–6 EV ≥ 20 ; critique : `TELEGRAM_MIN_CRITICAL_EV=35`, sans limite de
cote, seulement si le premium n'a pas pris) :

| Canal | n | part | CLV | σ |
|---|---|---|---|---|
| Premium | 1 397 | 32 % | +10,40 % | 22,9 |
| Critique | 51 | 1 % | +36,86 % | 6,3 |
| **AUCUN** | **2 917** | **67 %** | +4,89 % | 16,0 |

La zone muette découpée — **deux morceaux ne devraient pas y être** :

| Tranche | n | CLV | σ | % pos | verdict |
|---|---|---|---|---|---|
| **cote 4–6, EV 8–20** | **403** | **+7,68 %** | 8,6 | 74 % | **à ouvrir** |
| **cote > 6, EV 20–35** | **76** | **+17,35 %** | 5,7 | 79 % | **à ouvrir** |
| cote > 6, EV 8–20 | 209 | +8,68 % | 4,3 | 65 % | discutable |
| cote 1,5–6, EV 5–8 | 1 972 | +3,62 % | 12,1 | 71 % | laisser dehors |
| cote < 1,5 | 160 | +5,17 % | 10,2 | 85 % | laisser dehors |
| cote > 6, EV 5–8 | 97 | +0,91 % | 0,4 | 59 % | **laisser dehors** |

Mécanique du trou : le premium refuse au-dessus de la cote 4 sans EV ≥ 20 ; le
critique refuse en dessous de EV 35. Entre les deux, rien.

**Contrefactuel de l'élargissement** — si le premium prenait `cote 4–6 dès
EV ≥ 8` et `cote > 6 dès EV ≥ 20` :

| | n | CLV | σ |
|---|---|---|---|
| Premium actuel | 1 397 | +10,40 % | 22,9 |
| **Premium élargi** | **1 927** | **+10,80 %** | **24,6** |

**+38 % de volume ET un meilleur taux.** C'est le seul changement de toute
l'analyse qui ne coûte rien. Non appliqué — décision de l'utilisateur en
attente. Le code à toucher : `src/alerter.py`, `_prem_standard` et
`_prem_high_odds` (lignes ~766-781).

### 16.3 Vitesse d'alignement par book — la mesure qui manquait

Kaplan-Meier sur 4 213 opportunités de `bet_corrections`, une par
(book, event_key, marché, sélection), la plus ancienne détection gardée. Les
paris dont le match commence avant correction sont **censurés**, pas comptés
comme des échecs.

| Book | suivis | corrigés | médiane | 3ᵉ quartile | alignement | chute de cote |
|---|---|---|---|---|---|---|
| Circus | 297 | 88 % | 4 min | 11 min | 12 min | −8,9 % |
| Napoleon | 552 | 76 % | 5 min | 38 min | 23 min | −8,1 % |
| Betano | 513 | 82 % | 5 min | 25 min | 17 min | −8,9 % |
| Ladbrokes | 335 | 88 % | 6 min | 17 min | 21 min | −11,7 % |
| Golden Palace | 321 | 87 % | 6 min | 14 min | 7 min | −16,7 % |
| StarCasino | 1 128 | 86 % | 7 min | 15 min | 8 min | −14,3 % |
| **Unibet** | **1 046** | **77 %** | **38 min** | **4 h 12** | **1 h 06** | −8,3 % |

Part encore ouverte (non corrigée) après une heure : **55 % chez Unibet**,
18 à 24 % partout ailleurs.

**Unibet est l'anomalie du panel, et elle est exploitable.** Il corrige six
fois plus lentement que les autres ET garde le plancher de marge le plus bas :
sa courbe d'écart à la cote juste reste entre −1,5 et −2,7 % pendant des
heures, quand les autres sont déjà à −7 %. C'est cohérent avec sa CLV premium,
la deuxième du panel à +11,49 %.

Lecture des courbes de convergence (`curves.csv`, médiane de
`(cote − juste) / juste`) :
- à la détection, le book fautif est à **+1 à +5 %** au-dessus de la juste ;
- **à 5 minutes il est déjà à −4 à −9 %** — l'essentiel de la value disparaît
  dans cette fenêtre ;
- ensuite chaque book atteint **son plancher** et n'en bouge plus : Golden
  Palace −9,5 %, Ladbrokes −9,8 %, Napoleon −6 %. Ce plancher est la marge
  maison ;
- **Pinnacle se stabilise à −8 %** sur tout le graphique : c'est sa
  commission. C'est la preuve visuelle qu'il faut mesurer contre la clôture
  **dévigée** et jamais contre la cote affichée, sous peine d'offrir +8 % de
  CLV gratuite à tout le monde.

### 16.4 Les découpes de CLV

**Par EV — monotone sur sept tranches, sans une seule exception :**

| EV | n | CLV | σ |
|---|---|---|---|
| 5–8 % | 2 189 | +3,53 % | 12,1 |
| 8–10 % | 654 | +5,89 % | 9,8 |
| 10–12 % | 432 | +6,09 % | 8,4 |
| 12–15 % | 386 | +7,91 % | 8,7 |
| 15–20 % | 309 | +12,30 % | 11,6 |
| 20–30 % | 258 | +17,74 % | 13,9 |
| > 30 % | 137 | **+36,76 %** | 12,2 |

C'est le meilleur certificat de santé du système : l'EV calculée en amont
prédit réellement ce qui se passe sur la clôture. Le devig fonctionne, le
matching est propre, la référence est la bonne. **Et c'est la validation
chiffrée du refus de plafonner l'EV** — les gros EV ne sont pas des erreurs de
mesure, ce sont les meilleurs paris du fichier.

**Par cote :** 1,5–2 → +5,24 % · 2–2,5 → +4,99 % · 2,5–3 → +5,84 % ·
3–4 → +7,37 % · 4–6 → +8,39 % · **> 6 → +11,97 %** (468 opportunités, 8,4 σ).

**Par délai avant le coup d'envoi** (canal premium) :

| Tranche | n | CLV | σ | % pos |
|---|---|---|---|---|
| 0–2 h | 298 | +11,95 % | 12,5 | 88 % |
| 2–6 h | 198 | +11,20 % | 7,8 | 78 % |
| 6–12 h | 424 | +11,14 % | 14,3 | 83 % |
| 12–24 h | 173 | +9,92 % | 8,8 | 78 % |
| **24–48 h** | **125** | **+5,44 %** | **3,3** | **62 %** |
| > 48 h | 179 | +9,10 % | 7,4 | 74 % |

**La tranche 24–48 h est le seul vrai trou.** Explication mécanique : à deux
jours du match, la ligne Pinnacle elle-même n'est pas informée ; on mesure un
écart contre une référence qui va bouger, et la moitié de l'edge apparent est
du bruit qui se résorbe. Au-delà de 48 h ça remonte : lignes d'ouverture sur
marchés peu liquides, décalage réel mais mise praticable minuscule. **La coupe
doit viser 24–48 h précisément, pas « tout ce qui est loin ».**

**Par book** (toutes opportunités) :

| Book | n | CLV | % positives | perte moy. quand négatif |
|---|---|---|---|---|
| Ladbrokes | 293 | +10,54 % | 77 % | −11,2 % |
| Golden Palace | 58 | +8,70 % | 78 % | −9,7 % |
| Circus | 109 | +8,12 % | 71 % | −14,4 % |
| Unibet | 1 450 | +8,07 % | **80 %** | −9,6 % |
| StarCasino | 1 463 | +6,47 % | 72 % | −11,2 % |
| BetFirst | 36 | +6,08 % | 72 % | −13,3 % |
| Betano | 555 | +5,57 % | 72 % | −13,6 % |
| **Napoleon** | 401 | **+4,28 %** | **62 %** | −11,7 % |

Sur le canal premium seul : Circus +12,90 % (27, échantillon trop mince),
Unibet +11,49 % (450), Ladbrokes +11,33 % (100), StarCasino +10,84 % (479),
Betano +7,96 % (162), Napoleon +6,64 % (152).

**26 % des opportunités ont une CLV négative**, pour une perte moyenne de
−11,26 %. Ce n'est pas une anomalie, c'est la variance normale d'un edge de
7 %. Napoleon est le seul book où l'équilibre se dégrade vraiment.

**Par sport :** tennis +9,87 % sur 787 opportunités, **90 % de positives** ;
football +6,39 % sur 3 571, 71 %. Le correctif des noms inversés du 06/08
(§15.4) porte ses fruits.

**Par marché :** h2h +7,36 % (3 841) · totals **+4,58 %** (524) — la moitié.

**Par catégorie de ligue** (≥ 50 opportunités) : amicaux +9,39 % · autres
+7,82 % · féminin +7,04 % · Scandinavie +6,57 % · Europe de l'Est +5,87 % ·
top 5 +5,67 % · D2 +4,93 % · coupes +4,59 % · D3 +4,05 % · **Amérique du Sud
+3,45 %**. ⚠️ **2 502 des 4 365 opportunités tombent dans « autre »** : tant
que ce seau reste plein, cette découpe ne peut rien décider.

### 16.5 Contrefactuels — ce que coûterait chaque filtre

Tous calculés sur le canal premium tel qu'il est aujourd'hui (1 397, +10,40 %) :

| Filtre | n restant | CLV | volume gardé |
|---|---|---|---|
| Sans les détections > 24 h et < 48 h | 1 096 | +11,19 % | 78 % |
| Sans Napoleon | 1 245 | +10,86 % | 89 % |
| Sans les totals | 1 255 | +10,79 % | 90 % |
| ≤ 24 h **et** sans totals | 985 | +11,54 % | 70 % |

Aucun n'est appliqué. Le seul qui ajoute du volume au lieu d'en retirer est
l'élargissement du §16.2.

### 16.6 Ce que cette analyse ne prouve pas

À relire avant de tirer une conclusion de n'importe quel chiffre ci-dessus.

- **La CLV n'est pas de l'argent.** La table `results` est à **zéro ligne**.
  1 209 paris joués enregistrés, aucun résultat, aucun P&L calculable. Tout ce
  document mesure la *promesse* de gain. C'est le seul trou structurel du
  dispositif.
- **Pinnacle est juge et partie.** Une seule référence. S'il se trompe
  systématiquement sur un segment (petites ligues, tennis féminin, marchés
  exotiques), la CLV y sera fausse sans que rien ne le signale. Smarkets
  reste le plus gros levier ouvert du projet (§6, §15.8).
- **40 % de couverture seulement.** Les 60 % de lignes sans clôture ne sont
  pas un tirage au hasard : ce sont les matchs que Pinnacle n'a pas cotés
  jusqu'au bout, donc plutôt les moins liquides. Le vrai edge sur l'ensemble
  des détections est vraisemblablement un peu plus bas qu'affiché.
- **Échantillons jeunes** : Golden Palace (58), Circus (109), BetFirst (36).
  Leurs positions au classement ne sont pas stables ; deux à trois semaines
  de plus avant d'en tirer quoi que ce soit.
- **Les 50 jours ne sont pas 50 jours pleins.** La couverture CLV ne devient
  dense qu'à partir du 20/07. L'essentiel des 4 365 opportunités tient sur
  trois semaines, en plein été — un régime de compétitions qui n'est pas
  celui d'une saison normale.

### 16.7 Méthode, pour refaire la mesure à l'identique

```bash
# Sur la VM — les trois exports
python3 -m src.main export-history --days 60 --out detections.csv
python3 -m src.main export-curves  --days 60 --out curves.csv
gzip -c ~/Projet-Perso/data/tracking.db > tracking.db.gz
```

Règles à ne jamais relâcher :
1. **Dédupliquer par `event_key + Marché + Pari`**, meilleure cote gardée.
   Sans ça tout est faux de +83 %.
   ⚠️ **CORRIGÉ au 13/08 — voir §17.8.** Au TENNIS, `event_key` n'identifie pas
   un match : Pinnacle révise son horaire par pas de 15 min et chaque révision
   crée une clé, jusqu'à onze pour un seul match. 13,5 % des matchs de tennis
   sont concernés. La bonne clé est **équipes + jour + marché + pari**. Le
   football n'est pas touché (0,5 %).
2. **Ne garder que le prématch** (`Délai (h) > 0`).
3. **Mesurer contre la clôture dévigée**, jamais la cote affichée.
4. **Porter le σ sur chaque découpe** et signaler tout sous-groupe < 50.
5. Pour les vitesses d'alignement, **censurer** les paris non corrigés à
   `observed_until` — les compter comme « jamais corrigés » sous-estimerait
   gravement la vitesse des books.
6. Pour les courbes, **exiger ≥ 30 relevés par point** et couper l'axe à
   3 jours : au-delà les effectifs s'effondrent et les courbes remontent
   artificiellement.

### 16.8 À faire au prochain démarrage — remplace §15.8

⚠️ **PÉRIMÉE — voir §17.10.** Le point 3 (Smarkets) est fait, le point 1
(élargissement) a vu sa justification changer, et le point 2 (table `results`)
repose sur un inventaire faux — tout existe sauf le branchement, voir §17.9.

Par ordre de rapport sur effort :

1. **Élargir le premium** (§16.2) : `cote 4–6 dès EV ≥ 8` et `cote > 6 dès
   EV ≥ 20`. +38 % de volume, +0,4 point de CLV, 24,6 σ. `src/alerter.py`,
   `_prem_standard` / `_prem_high_odds`. **Décision utilisateur en attente.**
2. **Remplir la table `results`** (§16.6). Sans elle, aucun P&L réel n'existe.
   Aucune source de scores identifiée à ce jour — c'est un vrai chantier.
3. **Smarkets** comme deuxième référence sharp (§6). Toujours le plus gros
   levier structurel.
4. **Couper la tranche 24–48 h** du premium (§16.4). +0,8 point de CLV pour
   22 % de volume en moins — arbitrage à trancher.
5. **Classer les ligues** : 57 % des opportunités en « autre » rendent la
   découpe par ligue inutilisable, et c'est celle qui permettrait de couper
   l'Amérique du Sud et les coupes sans toucher au reste.
6. **Traiter les totals à part** (+4,58 % contre +7,36 %) : seuil d'EV plus
   haut ou mise réduite.
7. **Durcir Napoleon** : 38 % de CLV négative, +4,28 %, le plus mauvais des
   huit.
8. Reliquats d'outillage inchangés : découpage en runs du `pinnacle_doctor`,
   noms de books dédoublés dans `paris_track.csv` (§14.10), test d'IP
   Pinnacle en attente d'un vrai 403 (§14.7).

---

## 17. Session du 13/08 — Smarkets en production, et quatre pannes silencieuses

Branche **`claude/resume-clarification-1541xa`**, de `c9e42d4` à `1e054d9`.
Quatre chantiers : le ménage du dépôt, une mesure complète sur export frais,
la remise en service de Smarkets, et les défauts trouvés en la vérifiant.

⚠️ **Cette session a produit quatre pannes silencieuses de mon propre fait**,
toutes du type décrit au §11 : du code déployé, aucune erreur nulle part, et
rien qui sorte. Elles sont documentées en §17.5 parce que le mode de
défaillance compte plus que les correctifs.

### 17.1 Le dépôt — une seule branche désormais

Neuf branches, dont la **branche par défaut figée au 14/05** sur l'état initial
à trois commits. Toute nouvelle session démarrait donc sur du code mort — c'est
exactement ce qui s'est produit au début de celle-ci.

État final : **`claude/resume-clarification-1541xa` seule**, et branche par
défaut du dépôt. Les huit autres sont supprimées ; aucune ne portait de commit
absent d'ailleurs, sauf `crossfit-hyrox-back-workout` dont le fichier
`seance_dos_crossfit_hyrox.md` a été rapatrié à la racine avant suppression.

⚠️ **La suppression de branches est impossible depuis une session Claude Code**
(accès git en écriture seule, pas d'outil API). Elle se fait dans le navigateur,
et la branche par défaut se change dans **Settings → General**, pas dans
Settings → Branches.

🔴 **Ce « état final » n'est plus vrai — trois branches au 21/08.**
`claude/valuedylan-ifsu3o` (29 commits en retard, 0 en avance) et
`claude/resume-clarification-restructure-kpx0l3` (même commit que la branche
par défaut) se sont ajoutées. Aucune ne diverge encore, donc rien n'est perdu ;
mais chacune est un point de démarrage possible pour une session suivante, et
c'est exactement comme ça que le piège des §13-§14 s'était installé. **Les
supprimer au navigateur**, et vérifier `git branch -a` au début de chaque
session plutôt que de faire confiance à ce paragraphe.

### 17.2 La mesure du 13/08 — remplace §16.1

⚠️ **PÉRIMÉE — voir §18.1.** Refaite le 15/08 sur 5 764 opportunités, avec la
clé de déduplication corrigée du §17.8.

Export de 23 614 lignes, 21/06 → 13/08. Méthode du §16.7 appliquée sans écart.

| | n |
|---|---|
| Lignes du fichier | 23 614 |
| … avec une clôture Pinnacle dévigée | 10 406 (44 %) |
| … prématch (délai > 0) | 9 420 |
| **→ opportunités dédupliquées** | **5 093** |

| Périmètre | n | CLV | σ | % pos |
|---|---|---|---|---|
| Toutes opportunités | 5 093 | +6,36 % | 26,5 | 74 % |
| **Canal premium** | **1 726** | **+10,13 %** | **23,0** | 79 % |
| … voie 1,5–4 (EV ≥ 8) | 1 558 | +8,69 % | 21,6 | 78 % |
| … voie 4–6 (EV ≥ 20) | 182 | +24,14 % | 11,1 | 87 % |
| Canal critique | 63 | +33,99 % | 6,7 | 81 % |
| Aucun canal | 3 304 | +3,86 % | 15,0 | 71 % |

**L'edge tient** : +10,40 % sur 1 397 le 11/08, +10,13 % sur 1 726 le 13/08.

**L'EV reste monotone sur sept tranches** : +2,62 % à 5-8 % d'EV jusqu'à
+35,54 % au-delà de 30 %. Le refus de plafonner l'EV reste validé.

**Le tennis explose : +15,39 % sur 341 opportunités, 92 % de positives**, contre
+9,49 % au 11/08. Le correctif des noms inversés du 06/08 (§15.4) porte
pleinement. Football : +8,84 % sur 1 383.

⚠️ **Le creux 24-48 h est confirmé une quatrième fois** : +5,41 % (n=157) et
surtout **62 % de positives** contre 81-87 % ailleurs.

**Par book, canal premium** : Unibet +11,67 % (556, 84 % pos) · StarCasino
+10,22 % (568) · Ladbrokes +10,45 % (120) · Betano +8,83 % (210) · **Napoleon
+5,98 % (176, 62 % pos)**. Circus (44), Golden Palace (46) et BetFirst (6)
restent sous 50, donc non classables.

**Les totals se rapprochent** : +7,34 % contre +10,45 % au h2h, soit 70 % du
h2h au lieu de la moitié au 11/08. Le durcissement du §16.8 point 6 est moins
urgent qu'annoncé.

**Sélection manuelle, troisième mesure** : joués +10,73 % (528) contre non
joués +9,87 % (1 198), **t = +0,90, non significatif**. Inchangé.

### 17.3 La cote 1,50–2,00 — question posée, réponse chiffrée

| Vue | n | CLV | σ | % pos |
|---|---|---|---|---|
| Toutes détections 1,5–2 | 935 | +5,01 % | 16,1 | 79 % |
| **Dans le canal premium** | **287** | **+8,08 %** | **12,7** | **85 %** |

Le +5,01 % brut ne mesure pas une faiblesse de la cote basse : **648 des 935
détections de cette tranche sont des EV 5-8 %**, que le filtre premium écarte
déjà. Une fois ce bruit retiré, +8,08 % — et **le meilleur taux de réussite du
système, 85 %**, devant la voie 4-6.

À edge égal, préférer les cotes basses reste juste, et pour une raison
pratique : moins de variance autorise des mises plus grosses, ce qui est
exactement la contrainte du §7 (capacité en euros, pas nombre de paris).

### 17.4 L'élargissement du premium — la conclusion du §16.2 ne tient plus

| | n | CLV | σ | annoncé le 11/08 |
|---|---|---|---|---|
| Premium actuel | 1 726 | +10,13 % | 23,0 | +10,40 % |
| Premium élargi | 2 396 | +10,21 % | 25,1 | +10,80 % |

**Le gain de taux tombe de +0,40 point à +0,08 point** — c'est-à-dire à rien.
Le gain de volume tient (+39 %) et le σ monte. L'élargissement reste à faire,
mais pour ce qu'il est : **plus de volume au même taux**, pas un meilleur taux.

Les deux poches, jugées séparément :

| Poche muette | n | CLV | σ | verdict |
|---|---|---|---|---|
| cote > 6, EV 20–35 | 100 | **+15,54 %** | 5,9 | **à ouvrir** |
| cote 4–6, EV 8–20 | 507 | +6,48 % | 8,2 | dilue le premium |
| cote > 6, EV 8–20 | 231 | +5,01 % | 2,9 | laisser dehors |

⚠️ **Une case franchement négative apparaît** : **cote > 6 à EV 5-10 %,
−2,30 % sur 175 opportunités** (−1,3 σ, donc non significativement différente
de zéro). C'est le seul segment sans edge du système, et précisément celui
qu'un élargissement ne doit pas toucher.

**Décision toujours en attente.** Si une seule poche devait être ouverte, c'est
cote > 6 / EV 20-35.

### 17.5 Smarkets — remis en service

⛔ **PÉRIMÉ — Smarkets est éteint depuis le 16/08 (§19.6).** Mesuré à 0 % de
CLV positive sur 24 paris. La section reste pour le raisonnement, pas pour la
conclusion.

Le §5 l'avait retiré pour une seule raison mesurée : un rafraîchissement de
~26 minutes DANS le cycle, silenciant un sport entier. La juridiction n'était
pas en cause — API publique, sans authentification, source de données et non
lieu de pari.

**Sonde d'abord** (`tools/smarkets_probe.py`), selon la règle du §15.7. Verdict :
les deux endpoints acceptent des identifiants **groupés par virgule jusqu'à
50**, alors que le scraper faisait une requête par événement ET une par marché.
Coût mesuré de l'ancien chemin : 1,61 s par événement, soit **11,4 minutes**
pour 426 événements.

Les trois leviers du §5, tous appliqués :

1. **Appels groupés** — `fetch_markets_for_events` et `fetch_contracts_for_markets`
   groupent comme `fetch_quotes` le faisait déjà. ~1 000 requêtes → ~150.
   Attribution par l'identifiant porté par chaque objet (`event_id`,
   `market_id`), jamais par ordre d'arrivée — la règle du §10. Si le champ
   manque ou si l'API refuse le lot : repli sur les appels unitaires.
2. **Horizon borné** — `SMARKETS_HOURS=48`.
3. **Sorti du cycle** — cache de fond calqué sur BetFirst (§15.2). Le cycle lit
   le cache et repart. Rafraîchissement mesuré : **14 s en football, 5 s en
   tennis**.

**Statut : identique à tous les autres books.** Cotes persistées, clôture
capturée sur sa propre référence, CLV calculable, courbes dans `odds_history`,
colonne `Référence` dans l'export, marquage 🔵 dans l'alerte.

⚠️ **Pinnacle reste la référence numéro 1**, sans exception ni mélange. Quatre
tests le verrouillent, dont `test_pinnacle_line_is_untouched_by_the_secondary`.
Smarkets ne sert que là où Pinnacle ne price pas le marché.

**Ce que Smarkets apporte réellement**, mesuré en production :

| Sport | matchs absents de Pinnacle | taux d'appariement |
|---|---|---|
| Tennis | **19** sur 57 pricés | 67,2 % |
| Football | **7** | 98,1 % |

**Le football ne le justifie pas** — 7 matchs pour 15 s de rafraîchissement.
**Le tennis oui** : +30 % de gisement dans le sport au meilleur CLV, ce que le
§6 réclamait depuis juillet.

Cycles : 27 s avant, 32 s au pic, **29 s après stabilisation**.

### 17.6 Les quatre pannes silencieuses de la session

Toutes de mon fait, toutes du type §11 — déployé, sans erreur, et sans effet.

| Panne | Symptôme | Cause |
|---|---|---|
| Mauvais point de branchement | 8 books répondent, 33 value bets, zéro ligne Smarkets | `scan()` câblé au lieu de `_daemon_scan_sport()`. Le service passe par `daemon()`. |
| Cotes jamais persistées | aurait donné 0 CLV pour tout pari Smarkets, à jamais | le daemon persiste `pinnacle_q` et `soft_q` ; la secondaire ne passait par aucun des deux |
| Clôture cherchée chez Pinnacle | idem | `_closing_prices` codait `book='pinnacle'` en dur. Un pari de repli ne PEUT pas y être mesuré : si Pinnacle pricait ce marché, il n'y aurait pas eu de repli. |
| Clés non alignées | 7 175 cotes/cycle, **5 points** dans `odds_history` contre 800-1 700 par book | les cotes Smarkets gardaient la clé issue de LEURS noms et horaires. `remap_to_reference` ne pouvait pas être appelée telle quelle : elle **jette** les non-appariés, qui sont ici toute la raison d'être du repli. D'où `align_reference_source`. |

⚠️ **Un compteur conditionné à la présence de données ne prouve rien.** Le
premier compteur Smarkets ne s'imprimait que `si secondary`, rendant « pas
branché » et « branché mais vide » indiscernables. Il s'imprime désormais même
à zéro. C'est la règle du §13.12, qu'il a fallu réapprendre.

### 17.7 Deux règles d'outillage, apprises à mes dépens

⚠️ **`quotes` ne se consulte JAMAIS par balayage temporel large.** Deux
blocages dans la session. `SELECT ... WHERE fetched_at > ?` paraît borné, mais
l'index ne rend que des identifiants de ligne : il faut ensuite lire chaque
ligne dans une table de dizaines de Go — ~290 000 lectures aléatoires pour
cinq minutes de données. Et `COUNT(*) WHERE book = ?` sans borne ne rend jamais
la main. Les deux seuls accès sûrs : par `event_key` (indexé), ou pas du tout.
Pour compter, passer par `events`, `odds_history` ou l'API.

⚠️ **Une sonde doit lire la même source que le code qu'elle mesure.** Trois
faux diagnostics dans la session, tous parce que la sonde regardait autre chose
que le daemon :
- elle appelait le rapprochement sans les réglages de production, et affichait
  huit échecs pendant que le correctif fonctionnait ;
- elle lisait les clés Pinnacle dans `events`, qui **accumule sans expiration**,
  au lieu des cotes du cycle courant.

Corollaire : **toute source qui accumule sans expiration ment sur le présent.**
Les réglages partagés vivent désormais dans `src/matcher`
(`WIDE_TOLERANCE_BY_SPORT`, `wide_tolerance_for`) pour que production et
diagnostic ne puissent plus diverger.

### 17.8 Le tennis a plusieurs clés pour un même match

**Pinnacle révise l'heure estimée d'un match de tennis par pas de 15 minutes**
à mesure que les courts se libèrent — 19:30, 19:45, 20:35, 20:50 pour un seul
match. Chaque révision crée une `event_key` nouvelle sans effacer l'ancienne.

Mesuré sur l'export du 13/08 :

| | matchs sous plusieurs clés | lignes touchées |
|---|---|---|
| **Tennis** | **13,5 %** (jusqu'à 11 clés) | **25,1 %** |
| Football | 0,5 % | 0,8 % |

⚠️ **Conséquence sur la méthode d'analyse — corrige la règle 1 du §16.7.**
Dédupliquer sur `event_key` ne rassemble PAS ces doublons. Sur la bonne clé —
**équipes + jour + marché + pari** — le tennis premium passe de 341 à 323
opportunités et de +15,39 % à **+15,00 %**, et le premium total de +10,13 % à
+10,00 %. L'effet est modeste mais réel, et il ne concerne que le tennis.

**Conséquence sur les alertes, non corrigée par décision utilisateur** : la
suppression au niveau du marché étant clée sur `event_key`, elle ne traverse
pas une révision d'horaire. Mesuré : **57 alertes premium dupliquées** sur sept
semaines, soit environ une par jour, et jouer un pari ne fait pas taire son
jumeau. Le correctif consisterait à clé la suppression sur
`(équipes, jour, marché, sélection)` — une vingtaine de lignes, mais sur le
chemin le plus sensible du système.

⚠️ **Une hypothèse testée et RÉFUTÉE, à ne pas ressortir.** On pouvait craindre
qu'un pari détecté sous une clé ensuite révisée voie sa clôture capturée trop
tôt. La donnée dit non : couverture de clôture identique (34,8 % contre
36,4 %), CLV même supérieure (+8,35 % contre +10,78 %), aucune dégradation
selon l'ancienneté de la clé.

### 17.9 P&L réel — l'inventaire, avant de construire

⚠️ **Le §16.8 point 2 est inexact.** Il annonce « aucune source de scores
identifiée à ce jour — c'est un vrai chantier ». En réalité tout existe sauf le
branchement :

| Brique | État |
|---|---|
| Table `results` | ✅ (`winner`, `home_score`, `away_score`, `source`) |
| `record_result()` | ✅ |
| `settle()` — note gagné/perdu/annulé | ✅ h2h **et** totaux |
| `pnl()` | ✅ |
| Source de score | ✅ `_LIVE_SCORES`, alimenté chaque cycle par Betano |
| **Écriture automatique dans `results`** | ❌ rien |

**Le point d'accroche** : `forget_finished_scores()` supprime les scores des
matchs vieux de plus de 6 h — exactement l'instant où le score final est jeté.
Il suffit de l'écrire avant. Le tuple stocké est `(domicile, extérieur,
minute)`, et **la minute est la garde décisive** : un flux interrompu à la 10ᵉ
ne doit jamais être enregistré comme un 0-0 final. Seuil ~85 min.

⚠️ **Un défaut à corriger en même temps** : `all_closed_bets` ne remonte que
`winner`, et `export-history` passe `None, None` comme scores à `settle()`.
Conséquence : **les paris sur les totaux ne pourront jamais être notés**, même
avec des résultats en base.

Limites à assumer : **football uniquement** (au tennis le champ `score` porte
les points du jeu), seuls les matchs que Betano price en direct, et seulement
si l'onglet est resté ouvert. P&L partiel — mais partiel vaut mieux que la
table à zéro ligne d'aujourd'hui. Le complément propre reste l'import HAR via
`settle --from`, qui avait déjà produit le P&L de 767 paris du §1.

**Non commencé, par décision utilisateur** : « on essaiera de faire quelque
chose propre par la suite ».

### 17.10 À faire au prochain démarrage — remplace §16.8

⚠️ **PÉRIMÉE — voir §18.8.**

1. **Juger Smarkets sur sa CLV.** Un `export-history` postérieur au 15/08
   contiendra assez de paris valorisés sur lui. Découper par la colonne
   **`Référence`**, et déduplíquer sur **équipes + jour**, pas sur `event_key`
   (§17.8). C'est ce chiffre qui dira s'il faut le garder partout, au tennis
   seulement, ou nulle part.
2. **Élargir le premium** (§17.4) — mais pour le volume, pas pour le taux. Si
   une seule poche : cote > 6 / EV 20-35 (+15,54 %, 5,9 σ). Ne jamais toucher
   cote > 6 / EV 5-10, qui est négative.
3. **Remplir `results`** (§17.9) — l'inventaire est fait, il reste ~30 lignes.
4. **Couper la tranche 24-48 h** du premium — quatrième mesure concordante,
   62 % de positives contre 81-87 %.
5. **Durcir Napoleon** : +5,98 % et 62 % de positives, le plus mauvais du panel.
6. Reliquats inchangés : découpage en runs du `pinnacle_doctor`, noms de books
   dédoublés dans `paris_track.csv` (§14.10), test d'IP Pinnacle (§14.7).
7. **Optionnel** : suppression d'alerte clée sur équipes+jour (§17.8), et
   appariement des doubles au tennis — Smarkets abrège « Arevalo M/Pavic M »
   là où Pinnacle écrit les noms complets. Trois matchs concernés, marché peu
   liquide : rapport effort/gain faible.

### 17.11 Nouveaux outils et réglages

| Fichier | Rôle |
|---|---|
| `tools/smarkets_probe.py` | Mesure le coût de l'API et si les appels groupés passent |
| `tools/smarkets_match_check.py` | Pourquoi une source ne s'apparie pas — taux, écarts d'horaire, ambiguïtés |

```
SMARKETS_ENABLED=1            # coupe-circuit sans déploiement
SMARKETS_REFRESH_SEC=300      # âge déclenchant un rafraîchissement en fond
SMARKETS_MAX_AGE_SEC=1800     # au-delà, le cache rend RIEN plutôt que du périmé
SMARKETS_HOURS=48             # horizon
SMARKETS_REQUEST_DELAY=0.5    # espacement des requêtes
```

⚠️ **`export-history` a changé de forme** : colonne **`Référence`** insérée en
15ᵉ position, et `Clôture brute (Pinnacle)` renommée `Clôture brute
(référence)`. Un script lisant par numéro de colonne doit être repris ; par
nom, rien à faire.

⚠️ **`export-history` n'accepte PAS `--days`** — seulement `--out`. Le §16.7
donne une commande qui échoue. `--days` n'existe que sur `export-curves`.

---

## 18. Session du 15/08 — mesure, volume, et MagicBetting débloqué

Branche **`claude/resume-clarification-1541xa`**, de `1c06c67` à la fin.
Cinq chantiers : une mesure sur export frais, un correctif qui était resté
inerte, une crise de volume de base, l'écriture parcimonieuse des cotes, et
MagicBetting passé d'« inexploitable » à collecté.

### 18.1 La mesure du 15/08 — remplace §17.2

Export de 25 452 lignes. **Déduplication sur `équipes + jour + marché + pari`**
et non sur `event_key` — voir §17.8, le tennis a jusqu'à onze clés par match.

| Périmètre | n | CLV | σ | % pos |
|---|---|---|---|---|
| Toutes opportunités | 5 764 | +6,16 % | 27,3 | 72 % |
| **Canal premium** | **2 025** | **+9,49 %** | **23,8** | 77 % |
| … voie 1,5–4 (EV ≥ 8) | 1 843 | +8,29 % | 22,2 | 76 % |
| … voie 4–6 (EV ≥ 20) | 197 | +22,41 % | 11,0 | 84 % |
| — premium tennis | 342 | **+15,01 %** | 17,1 | **92 %** |
| — premium football | 1 681 | +8,38 % | 19,0 | 74 % |

L'edge tient. Le tennis reste, et de loin, le meilleur sport.

### 18.2 La CLV par book — et le cas Golden Palace

Déduplication **par book**, de sorte qu'une re-détection ne compte qu'une fois.

| Book | n | CLV | % pos | écart apparié | meilleur prix |
|---|---|---|---|---|---|
| Unibet | 764 | +10,38 % | 80 % | +0,48 % | 47 % |
| Circus | 159 | +8,35 % | 69 % | **−1,26 %** | 25 % |
| StarCasino | 949 | +8,24 % | 74 % | **+1,90 %** | **57 %** |
| Betano | 392 | +8,19 % | 73 % | −0,22 % | 43 % |
| Ladbrokes | 303 | +7,34 % | 71 % | −0,53 % | 30 % |
| Golden Palace | 253 | +5,97 % | 65 % | −0,77 % | 33 % |
| Napoleon | 465 | +5,24 % | 68 % | −1,81 % | 33 % |

⚠️ **La vue brute et la vue appariée se contredisent pour Circus.** Il paraît
bon (+8,35 %) mais donne le **pire prix du panel après BetFirst** à sélection
identique. Son score vient de son portefeuille de détections, pas de ses prix.
C'est le piège du §9, déjà vu sur StarCasino en sens inverse.

**L'exclusivité est la mesure qui décide** — combien d'opportunités premium ce
book est le SEUL à offrir :

| Book | exclusives | CLV des exclusives |
|---|---|---|
| Unibet | **65 %** | +10,63 % |
| StarCasino | 42 % | +8,40 % |
| Betano | 34 % | +6,91 % |
| Napoleon | 31 % | +5,86 % |
| Circus | 16 % | +5,19 % |
| **Golden Palace** | **6 %** | **+1,69 %** |

**94 % de ce que Golden Palace offre est disponible ailleurs, à meilleur prix.**

⚠️ **Mais la bonne réponse n'est PAS de couper un book.** Contrefactuels :

| Politique | paris | CLV |
|---|---|---|
| Tout jouer, book indifférent | 3 307 | +8,06 % |
| **1× par sélection, au meilleur prix** | **2 071** | **+9,57 %** |
| Tout sauf Golden Palace | 3 054 | +8,23 % |
| Sans Golden Palace ni Napoleon | 2 589 | +8,77 % |

Couper Golden Palace rapporte +0,17 point. **La règle du meilleur prix en
rapporte +1,5, avec 37 % de paris en moins** — exactement ce que réclame la
contrainte de capacité du §6. Et elle ne demande de renoncer à aucun book,
donc à aucune capacité en euros.

Décision retenue : **garder tous les books**, mais ne jouer qu'une seule fois
par sélection, sur le mieux placé. StarCasino d'abord (meilleur prix 57 % du
temps), puis Unibet. Golden Palace reste comme soupape de capacité.

### 18.3 `reference_book` — un correctif resté inerte deux jours

⚠️ **La panne la plus instructive de la session.** Symptôme : des alertes
« Fair odd calculée sur Smarkets » reçues et jouées, et pourtant
`export-history` rendait `pinnacle` sur ses 25 452 lignes, sans exception.

Cause : **`value_bets` n'a jamais eu de colonne `reference_book`**. Elle
n'existait que sur `bet_features`. L'alerte affichait donc juste — elle lit
l'objet en mémoire — pendant que tout ce qui relit la base était faux.

Et les deux correctifs du 13/08 lisaient précisément la base. Chacun portait
une **garde défensive** qui a transformé une erreur franche en mensonge :

- `_closing_prices` : `try/except KeyError` → repli sur « pinnacle ». Les paris
  Smarkets cherchaient leur clôture chez Pinnacle, ne la trouvaient jamais, et
  n'obtenaient aucun snapshot. Or `export-history` joint sur `closing = 1` :
  ils disparaissaient du fichier entier.
- `export-history` : `"reference_book" in r.keys()` toujours faux, donc
  « pinnacle » écrit partout.

**La leçon : une garde défensive sur une colonne manquante est un piège.** Elle
ne protège de rien et transforme un plantage franc en donnée fausse. Les deux
ont été supprimées : une colonne absente doit crier.

Corrigé par migration `ALTER TABLE`. Résultat immédiat : **9 détections avec
référence `smarkets` en 24 h**, mesurables pour la première fois.

⚠️ Les paris Smarkets détectés AVANT le correctif gardent `reference_book` à
NULL et sont perdus pour la mesure.

### 18.4 La purge ne suivait plus

Cycles montés à **41 s de médiane, avec des pointes à 971 s**. Ni les books ni
le volume n'étaient en cause — 64 000 cotes par cycle, identique à l'avant-veille.

Le journal de la purge disait tout : *« Budget de 1800s atteint : il reste
27 840 090 lignes à supprimer. »* Elle supprimait 19,4 M de lignes par nuit
pour ~47 M écrites, et reconduisait le retard. Disque à **85 %**, base à
**34 Go**, `VACUUM` impossible (il faudrait 37,1 Go libres, il en restait 5,3).

Rattrapage manuel : 31,5 M de lignes en 66 minutes. Budget porté à **7 200 s**
(la purge tourne à ~8 000 lignes/s et doit absorber ~52 M par jour, soit
~6 500 s — 1 800 s ne pouvait pas tenir).

⚠️ **Pendant une purge, les cycles passent de 32 s à 9-24 minutes.** Les books
corrigeant en 4 à 38 minutes (§16.3), c'est une fenêtre aveugle. Elle tombe à
04:00 UTC, où le calendrier est creux — assumé.

⚠️ **Un message qui ne peut qu'alarmer n'informe pas.** L'avertissement de
budget se déclenchait dès qu'il RESTAIT des lignes, sans regarder la durée
écoulée. Une purge terminée en 3 958 s sur 10 800 annonçait donc « budget
atteint » alors qu'elle était à l'équilibre — les lignes restantes avaient
franchi le seuil PENDANT la purge. Corrigé : les deux cas sont distingués.

### 18.5 Écriture parcimonieuse de `quotes` — le correctif de fond

La cause du problème précédent : on écrivait **toutes** les cotes de tous les
books à chaque cycle, ~52 M de lignes par jour, alors que `line_speed` mesure
**99,6 % de cotes Pinnacle identiques** d'un cycle à l'autre.

**Design B retenu** : un marché n'est écrit que s'il a bougé, et il est réécrit
**ENTIER** dès qu'une seule de ses issues change.

⚠️ Cette réécriture intégrale est le cœur du design, pas un détail.
`closing_group` exige que les issues d'une clôture partagent le même
`fetched_at`, parce que le devig retire la marge en normalisant les issues les
unes contre les autres. Mélanger deux instants y laisserait une marge parasite,
donc une **CLV fausse**. On perd un peu de compression, on garde l'invariant
dont dépend toute la mesure — et `closing_group`, la purge et le calcul de CLV
ne changent pas d'une ligne.

**Résultat mesuré : 0,1 à 0,2 % en football, 0,5 % en tennis.** 46 lignes
écrites sur 19 265 proposées. Le volume est divisé par cinq cents.

Le filet de sécurité a été écrit AVANT l'implémentation : un test rejoue la
même séquence de cotes en écriture dense puis parcimonieuse et exige des
clôtures **rigoureusement identiques**.

⚠️ **Battement de cœur** (`QUOTES_HEARTBEAT_SEC`, 30 min) : un book aux cotes
stables n'écrirait plus rien et deviendrait indiscernable d'un book en panne.
Une réécriture périodique garantit qu'un book qui répond laisse une trace.

⚠️ L'état est **en mémoire** : après un redémarrage, le premier cycle réécrit
tout et affiche 100 %. C'est correct et se répare seul.

⚠️ **`tools/line_speed.py` est faussé** par ce changement — il comptait
précisément les lignes identiques qu'on cesse d'écrire. Marqué en tête de
fichier, à repenser. `books-coverage` ne mesure plus un débit mais une
activité ; sa fenêtre de 24 h dépasse le battement, donc il reste juste.

### 18.6 MagicBetting — de « inexploitable » à collecté

⚠️ **En partie périmé — voir §19.2 et §19.3.** Le tennis est raccordé et
l'offre entière est balayée : 613 cotes football sont devenues 3 845.

⚠️ **Le §15.6 est périmé.** Il concluait que les payloads chiffrés rendaient ce
book hors d'atteinte. Il est en production.

**Ce que `request.js` a révélé.** Le déchiffrement n'est ni du JavaScript ni un
XOR : c'est un module **WebAssembly** obtenu par `window.createDecryptor()`.
Ses codes d'erreur décrivent l'algorithme — « Authentication failed » (donc
AEAD), « Decompression failed » (donc clair compressé). Les chaînes du binaire
portent les messages d'erreur d'inflate. La chaîne est :

```
base64  ->  AEAD (nonce 12 + tag 16)  ->  inflate  ->  JSON
```

D'où l'échec du §15.6 : gzip et zlib étaient testés **sur le chiffré**, alors
que la compression est à l'intérieur.

**On n'extrait pas la clé — on exécute leur déchiffreur.** Le module est
autonome (aucun de ses cinq imports n'est appelé) et tourne sous `wasmtime` en
Python. Le mapping de ses exports minifiés (`f` à `q`) est déduit des
signatures puis CONFIRMÉ par ses codes d'erreur : des octets aléatoires rendent
« Data too short » sous 29 octets et « Authentication failed » au-delà.

**Architecture.** Cloudflare sert un défi à toute IP de datacenter — mesuré, la
VM reçoit 403 là où le navigateur reçoit 200. Un pont est donc nécessaire, mais
il est **bête par construction** :

```
Onglet magicbetting.be  →  userscript : appelle l'API, POSTe le payload BRUT
                        →  /ingest-magicbetting : déchiffre côté serveur
                        →  data/magicbetting/soccer.json  →  daemon
```

Le userscript ne déchiffre rien, ne parse rien, n'attribue rien. Tout ce qui
peut se tromper est en Python, testé. C'est l'inverse du pont Circus, dont le
JavaScript devait attribuer les réponses et s'est cassé trois fois (§10).

**Marchés, reconnus par leur Id et jamais par leur nom** (celui-ci dépend du
`langId` et arrive en néerlandais) :

| Id | Marché |
|---|---|
| 1 | résultat du match → H2H |
| 3 | total de buts, toutes lignes → TOTALS |

Les Id **négatifs** (−2, −3, −2532, −2533) sont les lignes « vedettes » : le
même marché réduit à une seule ligne, en doublon. Les prendre écrirait deux
fois la même cote. Exclus par ailleurs : handicaps (2, 2532), double chance
(37), totaux asiatiques (2533).

**Vérifié en production** : 20 Ko chiffrés → 415 Ko clairs, 27 événements,
637 cotes, **612 en base et 1 détection** — ce dernier chiffre prouve que le
rapprochement des noms avec Pinnacle fonctionne.

⚠️ **La couverture est minuscule et c'est structurel.** `gettopeventslist` rend
27 matchs — c'est la liste des événements *phares*, pas l'offre complète.
612 cotes contre 7 478 pour StarCasino. Le prochain chantier est de trouver
l'endpoint de l'offre entière, exactement comme le balayage des compétitions
qu'il a fallu découvrir pour Betano (§3). Les préfixes existent dans
`request.js` : `/prematch/`, `/sportsbook/`, `/schedule/`.

⚠️ **Le tennis n'est PAS raccordé.** Il manque trois identifiants à capturer :
le `sportId` du tennis, l'Id du marché vainqueur, l'Id des totaux de jeux. Ne
pas les deviner — c'est le piège du §10, où Napoleon utilise 547 en football et
521 en tennis.

⚠️ **Le `.wasm` est servi sous un chemin versionné.** Si le déchiffrement se met
à rendre « Authentication failed » sur tout, c'est le binaire qu'il faut
re-télécharger depuis l'onglet Network (filtre Wasm).

### 18.7 Pièges de la session

- **Une garde défensive sur une colonne manquante est un piège** (§18.3). Elle
  ne protège de rien et transforme un plantage franc en donnée fausse.
- **`quotes` ne se consulte JAMAIS par balayage temporel large.** Deux blocages
  de console. `WHERE fetched_at > ?` paraît borné mais l'index ne rend que des
  identifiants de ligne : il faut ensuite lire chaque ligne dans une table de
  dizaines de Go. `COUNT(*) WHERE book = ?` sans borne ne rend jamais la main.
  Passer par `events`, `odds_history`, `value_bets` ou l'API.
- **Un userscript vit dans Tampermonkey, pas dans le dépôt.** Un `git pull` sur
  la VM ne le met pas à jour ; il faut recopier le contenu dans l'éditeur, puis
  recharger l'onglet.
- **Un même userscript peut tourner dans DEUX contextes.** La partie sport de
  magicbetting.be est une iframe : sans garde, chaque envoi partait en double.
  C'est la version « deux frames » du piège du §7.
- **Ne jamais inventer une issue.** Le parseur MagicBetting étiquetait « nul »
  tout nom non reconnu — vrai sur un 1X2, faux au tennis, où cela aurait
  fabriqué une troisième issue et faussé le devig en silence.

### 18.8 À faire au prochain démarrage — remplace §17.10

⛔ **PÉRIMÉ — remplacé par §19.9.** Les points 3, 4 et 5 sont faits ; le 2 a
changé de nature (§19.5).

1. **Vérifier la purge de 04:00** avec son budget de 7 200 s, et la taille de
   la base. Avec l'écriture parcimonieuse, `quotes` doit fondre sous le Go en
   48 h. Une fois la place libre, un `VACUUM` rendra les 34 Go du fichier.
   ```bash
   sudo journalctl -u valuebet-prune --since "06:00" --no-pager | tail -6
   ls -lh data/valuebet.db && df -h .
   ```
2. **Poser le plafond d'EV de sécurité** que le §8 réclame depuis juillet.
   Mesuré ce jour : maxima à 133, 136 et 160 % sur 24 h, et un +82,32 % sur
   Dundee United où DEUX books indépendants cotaient 3,50-3,65 contre une
   « juste » à 2,00. Quand plusieurs books s'accordent et que la référence
   s'en écarte de 75 %, c'est la référence qui est fausse.
3. **Mesurer la CLV de Smarkets et de MagicBetting** séparément, via la
   colonne `Référence` de `export-history`. C'est ce qui dira s'il faut les
   garder, et sur quels sports.
4. **L'offre complète MagicBetting** (§18.6) — 27 matchs aujourd'hui.
5. **Le tennis MagicBetting** — trois identifiants à capturer.
7. **Élargir le premium** (§17.4) : +39 % de volume au même taux. Si une seule
   poche, cote > 6 / EV 20-35 (+15,54 %, 5,9 σ). Jamais cote > 6 / EV 5-10,
   qui est négative.
7. **Remplir `results`** (§17.9) — l'inventaire est fait, ~30 lignes à écrire.
9. Reliquats : `line_speed` à repenser (§18.5), découpage en runs du
   `pinnacle_doctor`, noms de books dédoublés dans `paris_track.csv` (§14.10).

### 18.9 Réglages ajoutés

```
QUOTES_HEARTBEAT_SEC=1800     # réécriture périodique d'un marché stable
DIGITAIN_WASM=data/digitain_decrypt.wasm
MAGIC_INGEST_DIR=data/magicbetting
MAGIC_MAX_AGE_MIN=10          # garde de fraîcheur du pont
```

| Fichier | Rôle |
|---|---|
| `src/scrapers/digitain_crypto.py` | Déchiffre via le WASM du site |
| `src/scrapers/magicbetting.py` | Parseur + lecture du dump |
| `tools/magicbetting-ingest.user.js` | Pont navigateur |

---

## 19. Session du 16/08 — MagicBetting à pleine offre, Smarkets retiré

Journée à deux faces. Côté gain : MagicBetting passe de la vitrine à l'offre
entière et gagne le tennis. Côté perte évitée : Smarkets, branché la veille,
s'avère produire des lignes justes fausses et coûter de l'argent — il est
éteint. Les deux découvertes viennent du même réflexe, mesurer au lieu de
supposer.

### 19.1 La base de données — le VACUUM se résoudra seul

`data/valuebet.db` pèse 34 Go dont **17,9 Go de données vivantes**, pour 7,5 Go
de disque libre. Le `VACUUM` reste donc impossible, mais **rien n'est cassé** :

- la purge n'est PAS en retard — la plus ancienne cote datait de 2,1 jours pour
  une rétention de 2 ;
- ces 17,9 Go sont ce que pesaient deux jours sous l'ANCIEN régime d'écriture.

Profil mesuré, par tranche d'une heure :

```
14/08 08h → 15/08 08h    ~2 500 000 lignes/h     ancien régime
15/08 14h                   630 736              transition
15/08 20h → 16/08 06h       ~63 000 lignes/h     écriture parcimonieuse
```

Facteur 40. La fenêtre de rétention sera **100 % parcimonieuse au matin du
18/08**, les données vivantes tomberont sous le Go, et le `VACUUM` passera sans
effort. Rien à faire d'ici là.

⚠️ **L'estimation d'espace du `VACUUM` était fausse** et le refusait quand il
devenait utile : elle valait `taille_du_fichier × 1,1`, ce qui suppose de
recopier 34 Go alors que la base compactée en fait moins d'un. Elle lit
maintenant `(page_count − freelist_count) × page_size`, la taille réelle des
données vivantes. Corrigé au passage : `VACUUM` écrit sa copie temporaire dans
`SQLITE_TMPDIR`, pas à côté de la base — la garde mesurait donc l'espace d'un
disque pendant que la copie partait sur un autre.

**Procédure quand ce sera possible** : `VACUUM INTO` un fichier neuf, vérifier
que `value_bets` et `closing_snapshots` ont le même compte qu'avant, puis
échanger **daemon arrêté**. Renommer sous un daemon qui tient le fichier ouvert
le laisserait écrire dans l'ancien inode, et ces écritures disparaîtraient sans
la moindre erreur.

### 19.2 MagicBetting — le tennis raccordé

Identifiants relevés sur capture réelle, jamais devinés (§10) :

| | Football | Tennis |
|---|---|---|
| SId | 1 | **3** |
| Vainqueur | Id 1 (1X2) | **Id 702** |
| Totaux | Id 3 (buts) | **Id 3** (jeux, lignes 16,5 à 28) |

Le mapping des marchés est devenu **par sport**, et c'était indispensable : le
même Id 1 signifie 1X2 en football et « total pair/impair » en tennis. Un
dictionnaire commun aurait lu ce dernier comme un vainqueur à trois issues sur
un sport qui n'en a que deux — devig faux, aucune erreur levée. Deux tests
tiennent la frontière dans les deux sens.

**La ligne 16,5–28 est ce qui a permis de trancher** entre jeux et sets : les
deux s'appellent « total », portent une ligne et affichent Au-dessus/Moins, et
un total de sets vaudrait 2,5. Confondre les deux n'aurait levé aucune erreur.

Écartés volontairement : les « vainqueur » à 38 issues (401499, 401522, 401523)
sont des vainqueurs de **tournoi**, sans contrepartie chez Pinnacle.

### 19.3 MagicBetting — le balayage de l'offre entière

`gettopeventslist` rendait 27 matchs. Ce n'était pas une limite d'API mais la
page d'accueil : le catalogue du site annonce `EC=1173` en football. On
collectait 2 % de l'offre.

Hiérarchie réelle, dont les noms de paramètres **se contredisent d'un endpoint
à l'autre** — `champId` chez l'un désigne ce que `tournamentId` désigne chez
l'autre, tandis que `championshipId` désigne un PAYS :

```
getmixedsportlistbyperiod                  -> sports, avec EC
getmixedchampionshiplistbyperiod?sportId   -> 90 pays, avec EC
getmixedtournamentsbyperiod?championshipId -> compétitions sous TL, avec EC
…withoutright?tournamentId&stakeTypes      -> les matchs
```

Le compteur `EC` présent à chaque niveau permet de trier et de ne demander que
ce qui pèse.

**Le pont ne construit plus aucune URL.** Il demande à la VM « quoi appeler
maintenant », appelle, et repose la réponse brute à l'adresse indiquée. Le
contrat tient en une ligne :

```json
{"fetch": [{"path": "/common/…", "post_to": "/ingest-…"}]}
```

Une réponse peut être un plan à son tour — c'est ainsi que le catalogue se
construit **sans que le script sache ce qu'est un catalogue**. Les URL rendues
restent RELATIVES : l'identifiant de session de 36 caractères n'est connu que
du navigateur et expire.

Deux rythmes : les grosses compétitions reviennent chaque minute, la longue
traîne défile par tranches avec un curseur circulaire. L'offre est assemblée
depuis **un fichier par compétition** — un balayage interrompu ne vide plus
l'offre, et un morceau qui cesse d'être rafraîchi en sort tout seul.

**Résultat mesuré** — et il a continué de monter après le déploiement, la
longue traîne se remplissant tranche par tranche à chaque tour :

| Sport | Avant | +1 h | +2 h | |
|---|---|---|---|---|
| Football | 519 | 3 845 | **17 297** | ×33 |
| Tennis | 216 | 728 | **1 016** | ×4,7 |

MagicBetting est devenu la **deuxième source du portefeuille en football**,
derrière Pinnacle (30 708) et devant StarCasino (5 244). Le tennis dépasse
Pinnacle lui-même (530). Coût mesuré : +2 s par cycle.

⚠️ Ce n'est plus un complément, c'est un pilier — ce qui rend deux choses plus
urgentes : mesurer sa CLV dès que les clôtures tomberont, et surveiller les
`BOOK SUSPECT` d'`ev_outliers`, puisque trente fois plus de matchs signifie
autant d'occasions supplémentaires de mal apparier un nom d'équipe.

### 19.4 La référence à la marge aberrante

Cinq détections entre +146 % et +260 % d'EV, toutes des totaux over 6,5. Cause,
sur Hodd — Strommen :

```
Smarkets  4.5 over  5.13   5.5 over 10.66   6.5 over 1.98 / under 1.07
```

À 6,5 la cote « over » **redescend**, et la somme des probabilités atteint
144 %. Ce n'est pas un prix mais une offre isolée que personne n'a prise — ce
qu'un carnet d'ordres affiche quand même. Pinnacle s'arrêtant à 4,5 sur ce
match, le repli s'est déclenché exactement là où l'exchange est vide.

**MagicBetting était irréprochable** : son échelle 0,5 → 6,5 est monotone et
son over 6,5 à 12,69 cohérent. Le book avait raison, la référence était fausse.

Une ligne de référence dont la marge dépasse 120 % (deux issues) ou 130 %
(trois) est désormais écartée. Le contrôle **doit précéder le devig**, qui
normalise à 100 % quoi qu'on lui donne : nourri d'une ligne à 144 %, il rendait
une probabilité d'apparence normale et l'aberration devenait indétectable en
aval.

### 19.5 Les EV extrêmes — signalées, jamais coupées

Le §8 réclamait un plafond d'EV depuis juillet. **C'était le mauvais outil** :
un plafond aurait masqué le défaut du §19.4 sans le corriger, et aurait
supprimé aussi les vraies grosses EV.

À la place, l'alerte porte un bandeau au-delà de 100 % (seuils 100/120/130)
disant quoi vérifier — comparer aux autres softbooks, et renoncer s'ils
s'accordent entre eux loin de la « juste ». L'alerte reste entière.

Le bandeau et le contrôle de marge sont complémentaires : le second attrape une
cause précise et automatique, le premier couvre le reste — une référence peut
être fausse **en restant cohérente**, et alors seul l'œil décide.

### 19.6 Smarkets — éteint, sur mesure

Un pari valorisé sur la référence de repli est aussi **clôturé** sur elle : on
mesurait Smarkets contre lui-même. Battre une ligne bruitée ne prouve rien.

Pinnacle ne price JAMAIS ces marchés (26 paris sur 28, zéro purge) : impossible
de juger par lui. La seule règle indépendante restante est le **consensus
dévigé des softbooks** — médiane sur au moins trois books, parce qu'Unibet,
711, Bingoal et Scooore sont tous Kambi et que leur accord ne reflète qu'une
source.

| Règle de mesure | n | moyenne | médiane | positifs |
|---|---|---|---|---|
| clôture Smarkets | 24 | +32,82 % | +8,95 % | 58,3 % |
| **consensus softbooks** | 24 | **−20,58 %** | **−21,70 %** | **0,0 %** |

**Zéro positif sur vingt-quatre** — environ une chance sur seize millions que
ce soit le hasard. Et 53 points d'écart entre les deux règles, qui mesurent
exactement ce que vaut une référence jugée par elle-même.

Le mécanisme, sur un cas : `radualbot — viktorfrydrych`, juste Smarkets à 3,71
quand **six books** s'accordent sur 10,22. Smarkets voit 27 % de chances là où
le marché en voit 10.

⚠️ **Le défaut est structurel, pas accidentel.** Le repli ne se déclenche que
sur les marchés que Pinnacle ignore, donc sur les compétitions confidentielles,
donc là où le carnet d'un exchange est vide. Il va chercher sa référence
précisément où elle est la moins fiable.

`SMARKETS_ENABLED=0` : plus d'appel, plus de cache, plus de cote stockée. Le
scraper et ses tests restent en place, la mesure se refera si la liquidité
change. **Le book reste dans `SHARP_BOOKS`, et c'est un invariant à tenir même
éteint** — l'en retirer en ferait un book *soft*, donc un book où l'on chasse
une erreur de prix.

### 19.7 Le trou `events`

`event_rows` était construit à partir des seuls événements Pinnacle. Tout match
qu'il ne price pas restait sans ligne — donc précisément ceux où le repli se
déclenche. Ces paris sortaient sans nom d'équipe, sans ligue et sans heure de
coup d'envoi : **sans CLV possible**, faute de savoir quand relever la clôture,
et comptés quand même dans les moyennes. Vu : une détection à +90 % affichée
« None — None ».

La source est maintenant l'ensemble des clés de `fair`. C'est la bonne borne
parce que tout value bet est adossé à une ligne juste. `build_event_rows` est
extraite pour être testable — la règle s'était perdue en silence.

### 19.8 Pièges de la session

- **Un userscript Tampermonkey tourne dans un BAC À SABLE.** Patcher
  `window.fetch` remplace une copie que la page n'appelle jamais. Résultat
  parfaitement trompeur : 16 captures, toutes du CMS, aucune sportive — parce
  que le CMS passe par `XMLHttpRequest`, dont le prototype EST partagé. Il faut
  `@grant unsafeWindow` et tout brancher sur `unsafeWindow`.
- **`@connect *` ne suffit pas** à supprimer le dialogue de confirmation, et un
  « Interdire » cliqué une fois est mémorisé : les envois échouent ensuite en
  silence. Nommer l'hôte explicitement.
- **Les endpoints d'une même API ne rendent pas la même enveloppe.**
  `gettopeventslist` rend un tableau nu, celui du balayage un objet. Le serveur
  exigeait une liste et refusait chaque récolte avec « no events » — en
  accusant le site.
- **Un extracteur dupliqué donne tort au bon code.** Le rapport de sondes avait
  sa propre copie, récursive : il comptait correctement 25 événements pendant
  que la production les refusait tous. On en concluait que l'endpoint était bon
  alors que rien ne l'exploitait.
- **Mesurer une référence avec elle-même ne dit rien** (§19.6), et l'écart avec
  la vraie mesure atteignait 53 points de CLV.
- **Ne jamais additionner « jamais pricé » et « cotes purgées ».** Le premier
  condamne un mécanisme, le second demande juste d'allonger la rétention. Les
  confondre fait conclure au pire sur une limite d'outillage.
- **Un redémarrage ne se détecte pas à la durée.** La mesure des cycles
  excluait les écarts de plus de 600 s, mais un `systemctl restart` a laissé
  **2 s** entre le cycle interrompu (CYCLE 19) et le premier du nouveau
  processus (CYCLE 1). Ces faux cycles courts passaient et tiraient le minimum
  vers le bas. C'est le compteur qui repart à 1 qui est le signal fiable.
- **`python3` du système n'a pas les dépendances** : utiliser `.venv/bin/python`.
- **La sortie du daemon va dans `valuebet.log`, pas dans `journalctl`** — celui
  du serveur d'ingestion, si.

### 19.9 À faire au prochain démarrage — remplace §18.8

0. **Vérifier que l'onglet Betano est ouvert.** Mesuré le 16/08 en fin de
   journée : quatre heures sans une seule cote, la garde de fraîcheur refusant
   des prix de 216 minutes. Betano produisait 3 520 cotes en football. Le
   symptôme n'apparaît que dans `valuebet.log`, jamais en alerte.
   ```bash
   grep -c "Betano.*périmé" valuebet.log
   ```
1. **Le `VACUUM`** le 18/08 au matin, selon la procédure du §19.1. Puis monter
   `PRUNE_DAYS` à 7-14 : le risque §7 (CLV perdue sur un match reporté)
   disparaît, et `crossclose` cesse d'être bridé par la rétention.
2. **Mesurer la CLV de MagicBetting**, maintenant qu'il pèse. Les clôtures
   n'étaient pas encore tombées le 16/08 (0 sur 21 détections).
   `.venv/bin/python scripts/book_health.py magicbetting`
3. **Surveiller les `BOOK SUSPECT`** de `ev_outliers` : le balayage a multiplié
   les matchs par sept, donc les mauvais appariements aussi. Un cas mesuré le
   16/08 (MagicBetting seul à 8,70 quand six books tiennent 5,67).
4. **La dérive des cycles** : 24 s le matin, 47 s l'après-midi, sans lien avec
   le balayage (+2 s seulement). Cause non identifiée.
5. **Élargir le premium** (§17.4) : +39 % de volume au même taux.
6. **Remplir `results`** (§17.9) — devenu plus important : c'est la seule règle
   de jugement qui ne dépende d'aucune référence.
7. Reliquats : `line_speed` à repenser (§18.5), découpage en runs du
   `pinnacle_doctor`, noms de books dédoublés dans `paris_track.csv` (§14.10).

### 19.10 Nouveaux outils et réglages

| Outil | Répond à |
|---|---|
| `scripts/book_health.py` | un book est-il *traité*, ou seulement collecté ? |
| `scripts/ev_outliers.py` | cette EV énorme, c'est le book ou la référence ? |
| `scripts/crossclose.py` | rejuge un repli contre Pinnacle puis le consensus |
| `scripts/repair_events.py` | recrée les lignes `events` manquantes |
| `scripts/magic_probe_report.py` | quels sports et marchés dans les sondes ? |
| `scripts/magic_probe_show.py` | quelle FORME a la réponse d'un endpoint ? |
| `scripts/cycle_speed.py` | les cycles ralentissent-ils ? |
| `tools/magicbetting-discover.user.js` | capture les appels que le site fait |

```
SMARKETS_ENABLED=0            # éteint (§19.6)
SMARKETS_AS_REFERENCE=0       # seconde barrière, même si la collecte revient
MAGIC_BUDGET_EVENTS=500       # événements visés par tour et par sport
MAGIC_MAX_CALLS=30            # appels par tour — le vrai garde-fou
MAGIC_MAX_COUNTRIES=25
MAGIC_CATALOG_TTL_SEC=1800
MAGIC_PART_MAX_AGE_MIN=20     # âge max d'un morceau dans l'assemblage
MAGIC_REBUILD_SEC=10
```

### 19.11 Le listener était absent du dépôt — et tournait sur du code périmé

`/book` affichait « magicbetting » au lieu de « MagicBetting » alors que le
libellé existait dans `_BOOK_NAMES` depuis le 15/08. Le fichier sur disque
était à jour ; **le processus en mémoire ne l'était pas** — il avait démarré le
11/08 et n'avait jamais été relancé.

Cause : `valuebet-listener.service` existait sur la VM mais **pas dans le
dépôt**. `setup.sh` ne le posait pas, aucune procédure ne rappelait de le
redémarrer après un `git pull`, et les autres services — auxquels on pense —
masquaient l'oubli.

Deux corrections :

- `scripts/valuebet-listener.service.in` versionné, et le listener ajouté à
  `UNITS` et `ENABLE` de `setup.sh`.
- `bash scripts/setup.sh --check` compare désormais l'heure de démarrage de
  chaque service au dernier commit et signale ceux qui tournent sur du code
  antérieur.

⚠️ **La leçon dépasse le cas.** Un service `active (running)` peut servir du
code vieux de plusieurs jours : Python charge tout en mémoire au démarrage. Ici
le symptôme était une majuscule ; le même mécanisme aurait pu laisser hors
service un correctif sur les alertes ou sur le bouton « Jouer », en affichant
tout au vert. C'est le §11 appliqué au déploiement.

**Après chaque `git pull`** :

```bash
sudo systemctl restart valuebet-daemon valuebet-listener betano-ingest
bash scripts/setup.sh --check
```

### 19.12 Mises relevées à 35 € / 45 €

Demandé en fin de session, après la mesure : 15 % des détections franchissent le
palier d'EV, donc la mise moyenne réelle passe de 25,8 € à **36,5 €** — +41 %
d'exposition par pari.

```
STAKE_MODE=flat
STAKE_PCT=0            # 0 => pilotage en euros, pas en % de bankroll
STAKE_BASE_EUR=35
STAKE_EV_TIER=15
STAKE_EV_MULT=1.25     # 35 x 1,25 = 43,75 -> arrondi a 45
```

Pilotage en **euros** et non en pourcentage d'une bankroll relevée à 1 750 € :
les deux donnent 35 / 45, mais afficher « 2.0 % de 1750€ » sur chaque alerte
serait faux si le capital n'a pas suivi. Un chiffre rassurant et faux, lu à
chaque pari, finit par tromper. Si le capital est réellement à 1 750 €,
`STAKE_PCT=2.0` + `TELEGRAM_BANKROLL=1750` redonne l'affichage du risque.

**45 € reste sous 50 €**, et c'est heureux : l'arrondi passe de paliers de 5 €
à des paliers de 10 € au-delà, ce qui rend les mises plus visibles pour un book
qui surveille les comptes gagnants.

⚠️ **Ce que ce niveau de mise engage.** À 2,8 % du capital si la bankroll est
restée à 1 250 €, une série de 15 pertes — occurrence attendue sur quelques
centaines de paris à cote 2,50 — coûte 42 % du capital. Et surtout : `results`
est toujours vide, donc **aucun gain réel n'est mesuré**, seulement la CLV, qui
est un indicateur avancé. Deux défauts qui faisaient perdre de l'argent en
silence ont été trouvés ce seul 16/08 ; il peut en rester. Remplir `results`
n'est plus une amélioration, c'est une condition.

### 19.13 État des lieux au moment de la passation

| | |
|---|---|
| Cycles | 46 s de médiane (24 s le matin — dérive non expliquée, §19.9 point 4) |
| Base | 34 Go dont 17,9 Go vivants ; `VACUUM` possible le 18/08 |
| Books actifs | Pinnacle, MagicBetting, StarCasino, GoldenPalace, Unibet, Circus, Napoleon, Ladbrokes, Betano |
| Éteints | Smarkets (§19.6), BetFirst (`BOOKS_DISABLED`) |
| Mises | 35 € / 45 € au-dessus de 15 % d'EV |
| Tests | 622, tous verts |

**Trois ponts navigateur doivent rester ouverts** : Betano, Circus et
MagicBetting. Chacun a sa garde de fraîcheur, et chacune ne parle que dans
`valuebet.log`.

**Après chaque `git pull`** :

```bash
sudo systemctl restart valuebet-daemon valuebet-listener betano-ingest
bash scripts/setup.sh --check
```

---

## 20. Sessions des 17 et 18/08 — le P&L réel, enfin

Branche **`claude/resume-clarification-1541xa`**, dix-sept commits de `fa748ec`
à `2b1a1ae`. Le chantier central : `results` était vide depuis juillet, donc
tout le projet mesurait une promesse de gain et jamais un euro. Elle est
remplie. Autour : une alerte qui manquait, une base compactée de 34 Go à 1,9,
et quatre outils de mesure.

⚠️ **Quatre hypothèses ont été posées puis démenties par la mesure** au cours
de ces sessions — un book qui décrocherait, la durée de cycle qui suivrait la
charge, une alternance du rapprochement, et un plafond tennis qu'on croyait
majeur. Elles sont conservées en §20.9 parce que le raisonnement qui les a
tuées vaut mieux que les conclusions elles-mêmes.

### 20.1 Le P&L réel — la chaîne complète

Tout existait sauf l'écriture dans `results`. Vérifié brique par brique :
`record_result` (`storage.py:1485`), `settle()` (`clv.py:28`, h2h **et**
totaux, push = annulé), `pnl()` (`clv.py:54`, rend `None` si inconnu),
`played_bets_with_clv` qui joignait déjà `results`, et `track-update` qui
calcule le P&L par pari.

Le chaînon manquant est `results-update`, et son `--dry-run` **EST** la sonde
de couverture — il dit quelle part de nos matchs les sources savent résoudre,
sur notre univers et non sur la plaquette du fournisseur.

**La population notée est celle des DÉTECTIONS, pas des paris cliqués.** Le
projet mesure depuis juillet toutes les opportunités éligibles, jouées ou non,
précisément pour supprimer le biais de sélection manuelle. Ne noter que les
paris cliqués le rétablirait dans le P&L.

### 20.2 Deux sources, mesurées avant d'être crues

Aucun fournisseur ne fait les deux sports : **API-Sports couvre 1 200+ ligues
de football mais PAS le tennis**. D'où deux adaptateurs derrière
`ScoreProvider`, chacun coupé en parsing pur (testé sur échantillons réels) et
appel HTTP (qui ne décide de rien).

Ce que la sonde a tranché, et qui aurait été faux en silence :

| Piège | Ce que la mesure a dit |
|---|---|
| **Football, prolongations** | AET/PEN écartés : leur score à 90 min est AMBIGU — un AET rend `fulltime` 3-4 avec `extratime` 0-1, donc la prolongation est déjà dedans et le score réglementaire n'apparaît nulle part. Or 1X2 et totaux se règlent sur 90 min. Coût : 10 matchs sur 1 215 |
| **Tennis, statut** | Les 149 matchs d'une journée sont TOUS « completed », dont 17 sans vainqueur et 28 dont le vainqueur n'a pas le compte de sets requis. S'y fier écrirait un résultat faux pour **30 %** des matchs. Remplacé par un invariant vérifiable : le vainqueur doit avoir exactement les sets que son format exige |
| **Tennis, doubles** | Noms mutilés — « Mi / Victoria Luiza Barros », « - Bohrer Martins / Garcia Vidal ». 21 % des lignes, écartées |
| **Tennis, filtre de date** | S'appelle `from`/`to`. Le paramètre `date` est accepté et **silencieusement ignoré** : une requête sur le 15/08 rendait des matchs du 24/06 au 17/08 |

Sur les 1 177 matchs FT du football, `goals` et `score.fulltime` sont
rigoureusement identiques et `teams.home.winner` n'est jamais contredit — la
donnée y est irréprochable.

⚠️ **Au tennis, `home_score` porte les JEUX et jamais les sets.** Médiane 22,
plage 14-39 sur 104 matchs complets, ce qui recouvre les lignes 16,5-28 du
§19.2. Des sets rendraient « under » gagnant partout, sans lever d'erreur.

⚠️ **Et le vainqueur ne se déduit jamais des jeux** : 6-0 6-7 6-7 donne 18 jeux
à celui qui perd le match.

### 20.3 Le bug qui inventait 1 210 €

⚠️ **La panne la plus instructive du projet à ce jour, parce qu'elle vivait
dans la mesure censée détecter les pannes.**

`match_event` apparie les deux orientations — « A vs B » et « B vs A » —, ce
qui est indispensable au tennis où la notion de domicile n'existe pas. **Mais
il ne dit pas laquelle il a retenue**, et l'adaptateur inscrivait
`winner="home"` dès que le joueur 1 de la source gagnait.

Sur un appariement inversé, le pari `home` était donc noté sur le joueur que
NOUS appelons `away`. Rien ne pouvait le signaler : le score et le vainqueur
restaient parfaitement cohérents entre eux, seul leur rattachement à nos noms
était faux.

| | Avant | Après |
|---|---|---|
| P&L | +2 062,44 € | **+852,78 €** |
| ROI | +52,88 % | **+21,87 %** |

**Ce qui a mis la puce à l'oreille, c'est l'écart avec la CLV**, pas une
erreur. À +16,65 % de CLV sur des cotes à 3,09, la probabilité implicite est
de 37,7 %, donc ~59 gagnants attendus sur 156. Il y en avait 82 — **3,8 σ**.
Un P&L trop beau est un symptôme, exactement comme un P&L trop mauvais.

Après correctif, le P&L reconstitué (+21,87 %) et la CLV (+16,65 %) s'écartent
de **0,5 σ**. Pour la première fois du projet, l'indicateur avancé et l'argent
disent la même chose.

L'orientation est déduite en comparant la ressemblance de notre domicile avec
chacun des deux camps. En cas d'égalité on ne touche à rien — inverser sur une
hésitation créerait l'erreur qu'on évite. Le retournement porte sur les camps,
les scores ET le vainqueur ensemble ; n'en bouger que deux laisserait un
résultat cohérent en apparence et faux en profondeur.

### 20.4 Ce que le P&L dit, et ce qu'il ne dira jamais

⚠️ **C'est un P&L RECONSTITUÉ sur mises fictives**, pas un relevé bancaire.
`played_bets.stake` est la mise recommandée par l'alerteur — la colonne
s'appelle d'ailleurs « Mise fictive ».

**Il détecte les catastrophes, pas les nuances.** Un écart de 20 points entre
CLV et P&L fait ~9 σ sur 640 paris : impossible à rater. Un écart de 3 points
fait 1,4 σ : indétectable. C'est un contrôle d'intégrité, et il a justifié son
existence en trouvant 1 210 € qui n'existaient pas.

**Premier résultat, sur le tennis seul :**

| Population | n | ROI | σ | gagnés | cote moy |
|---|---|---|---|---|---|
| Toutes détections | 2 945 | **−0,34 %** | −0,1 | 44,3 % | 3,06 |
| **Canal premium** | 1 119 | **+6,69 %** | 1,7 | 44,5 % | 2,68 |
| Paris joués | 156 | +21,87 % | — | 52,6 % | 3,09 |

**Le filtre premium est validé en euros** : sept points d'écart avec
l'ensemble. Et joués contre tout le premium fait **1,3 σ** — cinquième mesure
consécutive disant que la sélection manuelle n'apporte rien.

⚠️ **Mais le ROI réalisé est très en dessous de la CLV.** Elle annonce +9 à
+10 % sur toutes les détections tennis ; le ROI sort à −0,34 %, soit **3,8 σ**
d'écart sur 2 945 opportunités. Trois lectures, non tranchées :

1. La CLV ne se convertit jamais en ROI à l'identique — mais 9 points, c'est
   beaucoup ;
2. 26 % des détections restent non résolues (doubles, abandons) et pourraient
   différer systématiquement ;
3. Le devig au tennis surestime peut-être l'outsider — le profil le suggère,
   cote moyenne 3,06 pour 44,3 % de réussite.

**C'est devenu la question la plus importante du projet : la CLV mesure-t-elle
vraiment l'edge ?** Le football tranchera — il triplerait l'effectif et il est
indépendant.

### 20.5 Le pont résultats — un quatrième anti-bot

API-Sports **refuse les IP de datacenter et le déguise en suspension de
compte**. Mesuré avec une seule et même clé : 200 depuis une IP résidentielle,
`{"access": "Your account is suspended"}` depuis la VM comme depuis un
conteneur cloud. RapidAPI aurait relayé, mais son site rend « Something Went
Wrong » sur sa propre page d'éditeur — piste fermée.

Le pont réutilise le serveur d'ingestion et le motif du §18.6 : le userscript
demande à la VM quoi appeler, appelle, repose la réponse BRUTE. Trois
différences avec les trois autres ponts, toutes en sa faveur — **pas d'onglet
dédié** (il se greffe sur ceux déjà ouverts), **quelques appels par jour**, et
**un PC éteint 24 h ne perd rien**, il prend du retard.

⚠️ Le plan gratuit d'API-Football ne sert qu'une **fenêtre de trois jours**
autour d'aujourd'hui. Aucun rattrapage historique n'est possible en gratuit.
Une journée refusée pour motif `plan` reçoit une pierre tombale et n'est plus
jamais réclamée — sinon le pont brûlerait du quota chaque heure sans aucune
chance d'aboutir. Un refus `access` n'en reçoit pas : il est temporaire.

⚠️ **Le compte reste suspendu au 18/08.** C'est le seul blocage du P&L
football, et donc de la question du §20.4.

### 20.6 Alerte « softbook muet »

Un book qui cesse de produire ne se voyait nulle part. Le 16/08, Betano est
resté quatre heures sans une seule cote — onglet fermé — et le seul symptôme
était une ligne de `valuebet.log`.

**Deux seuils, et il faut les deux : 15 minutes ET 5 cycles.** La durée seule
se déclencherait sur un cycle anormalement long (9-24 min pendant une purge) ;
le nombre de cycles seul ne veut rien dire non plus, un cycle durant de 24 s à
24 min selon le moment.

15 min plutôt que les 20 de Pinnacle : les books les plus rapides corrigent en
4 à 6 minutes (§16.3), donc un quart d'heure d'aveuglement coûte déjà des
occasions. Et contrairement à Pinnacle, dont la panne se voit à l'effondrement
des détections, un seul book absent ne change rien de visible.

**La liste surveillée se construit seule** : un couple (book, sport) y entre le
jour où il rend sa première cote. Un book coupé par `BOOKS_DISABLED` n'y entre
jamais et ne peut donc pas alerter à vide — c'est ce qui distingue « ce book
est absent » de « ce book n'a jamais existé ici ».

Mesurée sur `soft_raw` et non `soft_q` : la question est « ce book répond-il »,
pas « ses cotes s'apparient-elles ». Les confondre enverrait chercher un
onglet fermé pendant que le vrai problème est le rapprochement.

⚠️ **Non vérifiée de bout en bout au 18/08.** Le test manuel a échoué sur une
config Telegram vide — voir §20.10.

### 20.7 La fenêtre d'avant coup d'envoi, ouverte de 15 à 5 minutes

Le code tenait les alertes de dernière minute pour « disproportionnellement des
erreurs de ligne périmée ». **La mesure dit l'inverse.**

| Délai | n | CLV | positives |
|---|---|---|---|
| **5-10 min** | 50 | **+11,20 %** | **88,0 %** |
| **10-15 min** | 49 | **+10,73 %** | **91,8 %** |
| 15-30 min | 106 | +8,95 % | 83,0 % |
| 30-60 min | 118 | +6,98 % | 80,5 % |

La tranche 5-15 min porte les deux meilleurs taux de positives du tableau.

**Et ce n'est pas un mirage.** Le contrôle qui compte est l'écart CLV moins EV
de départ : **−2,4 à −3,3 points** sur 5-15 min contre −4,6 à −6,7 plus loin.
La ligne bouge donc bel et bien dans ces dernières minutes, le CLV n'y répète
pas l'EV qu'on a soi-même calculée — c'était l'erreur du §1, écartée ici.

⚠️ Le taux de positives de 90 % reste **en partie mécanique** : moins de temps
avant la clôture, c'est moins d'occasions de basculer. C'est le CLV moyen qui
porte le signal.

⚠️ **5 minutes est un plancher physique, pas un arbitrage** : la tranche 0-5
min est vide sur tout l'historique, Pinnacle retirant ses marchés prématch
avant le coup d'envoi.

Gain par canal : principal +3,49 → +5,32 % (168 opportunités), premium
+9,73 → +10,97 % (99), critique +6.

**Les surebets et middles gardent leur propre fenêtre à 15 min**
(`TELEGRAM_SUREBET_MIN_MINUTES_TO_KICKOFF`). Le risque n'y est pas le même : un
value bet est protégé par sa référence sharp, un surebet est une incohérence
ENTRE deux books, où une seule cote figée fabrique un arbitrage fantôme.

### 20.8 Analyses du 17/08 sur export frais

Export de 26 923 lignes, 21/06 → 17/08. Méthode du §16.7, déduplication sur
équipes + jour + marché + pari. **6 486 opportunités, 4 813 matchs.**

**L'edge n'a pas bougé** malgré une semaine ressentie comme mauvaise :

| Période | n | CLV | σ | positives |
|---|---|---|---|---|
| Avant août | 608 | +9,81 % | 14,9 | 79,6 % |
| Août complet | 1 545 | +9,78 % | 21,8 | 76,6 % |
| 10-17/08 | 709 | +9,04 % | 13,2 | 74,3 % |

L'écart de 0,77 point fait 1,1 σ. Sur cette même fenêtre où la bankroll a
reculé de 638 €, les paris joués affichaient **+9,23 % de CLV et 75,5 % de
positives** — un épisode à 1,8 σ sous l'espérance, soit une semaine sur trente.

**Par book, ta fenêtre 10-17/08 (premium)** : Unibet +11,93 % (237, 84,8 %
pos) · Betano +10,82 % (69) · Ladbrokes +9,52 % (40) · StarCasino +8,33 %
(200) · **Napoleon +2,60 % (88, 61,4 % pos)**.

⚠️ **Napoleon est le seul book faible sur les trois fenêtres indépendantes**
(+3,85 %, +5,55 %, +2,60 %), avec le pire taux de positives à chaque fois. Sur
328 opportunités premium cumulées, ce n'est plus du bruit.

**Over / Under** — l'écart est réel et significatif :

| | n | CLV | positives |
|---|---|---|---|
| h2h | 5 631 | +7,11 % | 72,2 % |
| **OVER** | 418 | **+6,50 %** | 77,3 % |
| **UNDER** | 437 | **+3,80 %** | 74,1 % |

2,7 points d'écart, **z = 2,7, p ≈ 0,007**. Les `under` hauts sont les plus
faibles : under 3.5 à +3,45 %, under 4.5 à +2,51 %, contre over 4.5 à +8,50 %.

⚠️ **Aucun total de tennis n'apparaît avec une clôture** — zéro sur tout
l'historique, alors que les totaux de jeux sont collectés. Perte de mesure
silencieuse, non élucidée.

**Les ligues ne disent rien.** 4 529 opportunités portent une ligue, 564 ligues
distinctes, dont **12 seulement atteignent 40 opportunités**. Aucune n'est
négative — la plus faible est à +3,29 %. Et le test de permutation sur 2 000
tirages donne **p = 0,62** : les 7 points d'écart entre la meilleure et la pire
sont ce que le pur hasard produit. **Il n'y a rien à couper.**

Les deux plus faibles sont les qualifications UEFA (Conference +3,29 %, Europa
+3,49 %). À poser comme hypothèse et retester dans deux mois sur données
neuves — pas à exploiter aujourd'hui.

⚠️ **Une conclusion rétractée.** Une première analyse annonçait que les
opportunités multi-books valaient le double des exclusivités. En changeant la
définition (match plutôt que sélection) et la fenêtre, le classement s'inverse
pour la moitié des books : StarCasino fait +11,48 % en exclusivité contre
+9,56 % accompagné, Napoleon +10,23 % contre +3,30 %. **Une conclusion qui ne
survit pas à un changement de découpe n'est pas une conclusion.** Seul Circus
perd vraiment en exclusivité (+5,91 % contre +9,78 %).

### 20.9 Quatre hypothèses démenties par la mesure

Conservées parce que le raisonnement compte plus que le résultat.

| Hypothèse | Ce que la mesure a dit |
|---|---|
| « Un book décroche un cycle sur deux » — 14 000 cotes football manquantes | **Faux.** Les neuf books répondent dans les deux cycles, aux mêmes volumes au chiffre près. L'alternance venait de la ligne `cotes`, dont le dénominateur additionne plus que la sortie du rapprochement. Artefact de comptage |
| « La durée de cycle suit la charge » | **Faux.** 24 s de médiane à 26 800 cotes contre 27 s à 41 000 — trois secondes pour 55 % de volume en plus. La dérive du §19.9 point 4 reste inexpliquée |
| « Le rapprochement alterne » | **Faux.** Stable à 59 % en football et 41-42 % en tennis, six cycles d'affilée, sur 780 et 128 lignes justes constantes |
| « Le plafond tennis est le plus gros levier » | **Faux, et c'est le plus utile.** Voir ci-dessous |

⚠️ **Le plafond tennis, correctement mesuré.** Pinnacle ne price que **129
événements de tennis, horizon J+0** — il n'ouvre ses marchés que le jour même.
Les books en offrent 187 à 433, à J+1 ou J+2. Vérifié dans le code : le
scraper n'a **aucun filtre de date**, le J+0 vient de Pinnacle.

Mais les matchs manquants sont à J+1/J+2, et c'est là que le tennis vaut le
moins : **+3,90 % à 24-48 h contre +9 à +11 % sous 24 h**, et seulement 4 % des
détections tennis dépassent 24 h. Surtout, **ce n'est pas un trou de
couverture mais un décalage de calendrier** : un match affiché à J+2 devient
référençable quand son jour arrive, et il est alors détecté dans la fenêtre où
le tennis rapporte le plus. Une seconde référence sharp au tennis apporterait
donc du volume dans la tranche la plus faible du système.

### 20.10 Le disque — 35 Go récupérés

`df` restait à 85-89 % alors que la purge fonctionnait : **SQLite supprime des
lignes, il ne rend pas d'espace.** Le fichier gardait 34 Go dont **34,06 Go de
freelist pour 2,13 Go de données vivantes** — 94 % de vide.

Procédure suivie, et deux écarts au §19.1 qui comptent :

1. **Daemon arrêté AVANT le VACUUM**, pas seulement pour l'échange : compacter
   pendant que le daemon écrit produit un instantané, et tout ce qu'il écrirait
   entre le VACUUM et l'échange serait perdu. Avec 2,13 Go l'opération dure
   trois minutes.
2. **`valuebet-listener` doit être arrêté aussi.** Oublié la première fois, il
   tenait le verrou et le VACUUM échouait sur `database is locked`.

⚠️ Deux échecs avant de réussir : un `VACUUM INTO` refuse un fichier cible
existant, et le brouillon partiel de 1,9 Go occupait l'espace qui manquait au
suivant. Le supprimer a suffi.

Résultat : **disque 89 % → 18 %**, base 34 Go → 1,9 Go, aucune ligne perdue
(six tables vérifiées identiques avant échange).

⚠️ **`PRUNE_MAX_SECONDS` était resté à 1 800 s** alors que le §18.4 l'avait
porté à 7 200. C'est pour ça que 21,6 M de lignes traînaient en retard, et que
la plus ancienne cote datait de 3 jours pour une rétention de 2. Corrigé.

⚠️ **`PRUNE_DAYS` n'est PAS dans `.env`** — il tourne sur son défaut de 2. À
ajouter, pas à modifier, le jour où on veut 7.

### 20.11 Outils et correctifs d'outillage

| Outil | Répond à |
|---|---|
| `results-update` | remplit `results` ; `--dry-run` = la sonde de couverture |
| `scripts/pnl_detections.py` | P&L sur TOUTES les détections, pas seulement les cliquées |
| `clv-report --since/--until` | l'edge a-t-il bougé cette semaine, ou est-ce la variance ? |
| `tools/scores-ingest.user.js` | pont navigateur pour les résultats football |

**Quatre défauts d'outillage corrigés**, tous du même genre — un chiffre faux
lu comme vrai :

⚠️ **`cycle_speed.py` mesurait la PÉRIODE et l'appelait « durée d'un cycle ».**
L'écart entre deux en-têtes contient le sommeil de 20 s : on lisait 47 s là où
le cycle en coûtait 26. Toute comparaison avant/après optimisation était
faussée du même décalage. Les deux mesures sont désormais affichées côte à
côte.

⚠️ **`track-update` divisait le ROI par un forfait constant** alors que le P&L
par pari utilisait la vraie mise. Tant que tout valait 25 € les deux
coïncidaient ; depuis les 35/45 € du §19.12, le ROI affiché aurait été faux dès
le premier calcul.

⚠️ **Aucune commande ne chargeait `.env`.** Le daemon le reçoit par
`scan-daemon.sh` ; une commande tapée à la main ne recevait rien. Le piège en
était à sa **quatrième** occurrence (§14.11 en compte deux). Corrigé à la
racine par un callback Typer, avant n'importe quelle commande — `setdefault`,
donc l'environnement de systemd gagne toujours.

⚠️ **Le rapprochement ne journalisait pas ce qu'il jette.** Nouveau compteur :
`rapprochement : N/M (X %) sur K lignes justes`. Le nombre de lignes justes
n'est pas décoratif — un rendement qui chute parce que la référence a rétréci
et un rendement qui chute parce que les noms ne s'apparient plus appellent deux
correctifs opposés.

### 20.12 Pièges rencontrés

- **Une conclusion tirée avant la mesure est une conjecture.** Quatre fois en
  deux sessions (§20.9). À chaque fois c'est le chiffre qui a tranché, jamais
  le raisonnement.
- **Un P&L trop beau est un symptôme.** +52,88 % de ROI contre +16,65 % de CLV
  faisait 3,8 σ — c'est ça qui a fait chercher le bug d'orientation, pas une
  erreur visible.
- **Un mock peut annuler ce qu'il teste.** Le test de cadence remplaçait `_get`
  en entier, or la cadence vit dedans. Il fallait remplacer la couche HTTP.
  Variante du §13.10.
- **Lire l'environnement à l'import fige la configuration** au démarrage du
  process. Fait deux fois dans ces sessions (`provider_for`, puis la cadence
  tennis), corrigé deux fois. Toujours lire à l'appel ou à la construction.
- **Une garde défensive sur une donnée manquante ment.** Rappel du §18.3,
  reconfirmé : `_football_error_message` réécrit désormais « account
  suspended » pour désigner l'IP et non le compte — le message brut avait déjà
  coûté un aller-retour.
- **Des `...` collés à la place d'une clé** dans `.env` ont produit deux
  diagnostics faux (un 403 et un « unauthorized ») interprétés comme des
  refus d'API. Toujours vérifier la longueur de ce qui a été écrit.
- **`systemctl stop` qui « bloque » est normal** — il attend la fin du cycle,
  jusqu'à 90 s. Et `sleep 300` n'affiche rien pendant cinq minutes.
- **`sqlite3` n'est pas installé sur la VM.** Passer par `.venv/bin/python`.

### 20.13 Réglages ajoutés

```
SCORES_FOOTBALL_KEY=              # API-Sports, palier gratuit = fenêtre 3 jours
SCORES_FOOTBALL_RAPIDAPI_KEY=     # bascule sur RapidAPI (piste fermée, site cassé)
SCORES_FOOTBALL_BRIDGE=0          # 1 => lit data/scores/ au lieu d'appeler
SCORES_INGEST_DIR=data/scores
SCORES_BRIDGE_DAYS=2              # fenêtre réellement servie par le gratuit
SCORES_FINAL_AFTER_SEC=21600      # au-delà, une journée n'est plus redemandée
SCORES_TENNIS_KEY=                # Live Tennis, Basic 9,99 $/mois obligatoire
SCORES_TENNIS_MIN_INTERVAL_SEC=1.1   # 54 req/min, sous la limite de 60
BOOK_ALERT_AFTER_MIN=15           # softbook muet : les DEUX seuils requis
BOOK_ALERT_AFTER_CYCLES=5
TELEGRAM_MIN_MINUTES_TO_KICKOFF=5        # value bets, plancher physique
TELEGRAM_SUREBET_MIN_MINUTES_TO_KICKOFF=15   # surebets, risque différent
PRUNE_MAX_SECONDS=7200            # était resté à 1800 malgré le §18.4
TELEGRAM_BANKROLL=3720            # capital réellement exposé
```

⚠️ Le palier gratuit de Live Tennis donne **20 appels d'historique par MOIS**,
pas 100 par jour comme sa grille le laisse croire. Basic est indispensable.

### 20.14 État des lieux

| | |
|---|---|
| Disque | **18 %**, base 1,9 Go |
| Cycles | 27 s de travail, 47 s de période, stables |
| `results` | **3 091 lignes** (tennis) — plus jamais vide |
| Tests | **679 passés**, 4 ignorés |
| Books actifs | Pinnacle, MagicBetting, StarCasino, GoldenPalace, Unibet, Circus, Napoleon, Ladbrokes, Betano |
| Mises | 35 € / 45 € au-dessus de 15 % d'EV |

### 20.15 À faire au prochain démarrage — remplace §19.9

1. **Débloquer le compte API-Football** — [dashboard.api-football.com](https://dashboard.api-football.com).
   C'est le seul blocage du P&L football, **et donc de la question la plus
   importante du projet** : la CLV mesure-t-elle vraiment l'edge ? Le tennis
   suggère 9 points d'écart, le football tranchera. Prendre un plan payant
   ensuite : le gratuit ne sert que trois jours, donc aucun rattrapage.
2. **Vérifier l'alerte softbook de bout en bout** (§20.6) — le test manuel a
   échoué sur une config Telegram vide, la chaîne de livraison n'est donc pas
   prouvée.
3. ~~**Durcir Napoleon**~~ — sans objet : il est en sourdine dans `/book`
   depuis, et n'est plus joué. Un book muet n'a pas besoin de seuil, il ne
   peut plus coûter un euro. Voir §21.8.
4. **Relever le seuil d'EV sur les `under`**, surtout 3.5 et 4.5 (§20.8). Seul
   effet significatif de toute l'analyse.
5. ~~**Décocher BetFirst dans `/book`**~~ — fait. Voir §21.8.
6. ~~**Les totaux de tennis sans clôture**~~ — cause trouvée et corrigée le
   19/08 : ils n'étaient pas collectés du tout. Voir §21.
7. **Résorber le retard de purge** avant de passer `PRUNE_DAYS` à 7 : allonger
   la rétention pendant le rattrapage l'aggraverait.
11. Reliquats : découpage en runs du `pinnacle_doctor`, noms de books dédoublés
   dans `paris_track.csv` (§14.10), dérive de cycles inexpliquée (§19.9 pt 4),
   `line_speed` à repenser (§18.5).

---

## 21. Sessions des 19-20/08 — les totaux de jeux du tennis, jamais collectés

### 21.1 Le constat

Sur l'export complet des détections : **5 019 h2h au tennis, zéro over/under**.
Pas « peu » — zéro, depuis le premier jour. Or six books affichent des totaux
de jeux au tennis, et le tennis est le meilleur sport du système (CLV premium
+15 %, 92 % de paris à CLV positive). Le §20.15 le listait comme « perte de
mesure silencieuse » sans en connaître la cause.

Interrogation de la base sur la fenêtre J±1, marché `totals`, tennis :

```
magicbetting   21 lignes : [17.5 … 23.0]
golden_palace   6 lignes
starcasino      6 lignes
unibet          5 lignes
betano          3 lignes
circus          3 lignes
pinnacle        1 ligne  : [2.5]      ← la référence
```

Pinnacle donnait **2,5** en face des 17,5-23,0 des books. Une ligne de **sets**
contre des lignes de **jeux** : aucun appariement n'était possible, donc aucune
ligne juste, donc aucune détection. Sans erreur, sans log — le mode §11.

### 21.2 La cause

`/sports/33/matchups` (tennis) renvoie **166 entrées** : 72 racines et
94 enfants, dont 81 marqués `units: "Games"` et 13 `units: "Sets"`.

| famille | `total` | échelle |
|---|---|---|
| racine | 2,5 | **sets** |
| enfant `Games` | 15,0 à 33,5 | **jeux** |

Les 320 marchés de totaux en jeux portent tous le `matchupId` d'un **enfant**.
L'index, lui, ne retenait que les racines :

```python
indexed = {m["id"]: m for m in matchups if ... and not m.get("parent")}
```

Ils tombaient donc intégralement sur `if matchup is None: continue`. Sonde :

```
totaux SETS  :  63  dont matchupId connu : 51
totaux JEUX  : 320  dont matchupId connu :  0
```

### 21.3 Le piège que le correctif aurait pu ouvrir

La racine et son enfant `Games` décrivent le **même match** : même `event_key`.
Indexer les enfants tels quels aurait donc fusionné deux échelles sous une
seule clé — et elles **se croisent** :

```
spread ±1,5 sur la racine       → handicap d'un SET
spread ±1,5 sur l'enfant Games  → handicap d'un JEU
```

**20 des 72 matchs relevés** présentaient les deux. Deviguer 1,5 jeu contre
1,5 set n'aurait levé aucune erreur : le groupe aurait eu ses deux côtés, la
marge aurait été plausible, seule la ligne juste aurait été fausse. C'est la
confusion jeux/sets du §19.2, dans sa version silencieuse — celle qui produit
des paris de valeur inventés au lieu de zéro pari.

D'où une règle volontairement étroite : **d'un matchup en jeux on ne prend que
les totaux**, dont l'échelle (≥ 15) ne recoupe jamais celle des sets (2,5 ou
3,5). Les handicaps et `team_total` en jeux restent dehors tant qu'une ligne ne
portera pas son unité. Les enfants `Sets` sont ignorés : ils redisent la racine.

### 21.4 Trois détails du payload, tous mesurés

1. **Les participants de l'enfant sont suffixés** — `Katarina Zavatska (Games)`.
   Les lire fabriquerait une `event_key` fantôme, sans h2h en face et invisible
   au réalignement des softs. Les noms et l'heure viennent du bloc `parent`
   embarqué, complet sur 81/81.
2. **`parent.isLive` ment toujours** : False sur 81/81, y compris pour un match
   commencé depuis **909 minutes**. C'est `isLive` de l'ENFANT qui dit la
   vérité (14 cas). S'y fier fait entrer des prix en direct dans la référence.
3. **20 enfants sur 81 n'avaient plus leur racine au calendrier** (tous
   au-delà du coup d'envoi). Résoudre le parent via l'index les aurait perdus ;
   le bloc `parent` embarqué suffit.

### 21.5 Ce que ça donne, mesuré avant et après sur l'API réelle

Même relevé, même minute, code d'avant puis code d'après :

| tennis | avant | après |
|---|---|---|
| `totals` — cotes | 106 | **672** |
| `totals` — événements | 53 | **63** |
| `totals` — lignes | `{2.5}` | `{2.5, 15.0 … 24.5}` |
| dont événements avec un h2h en face | 53 | **58** |
| `h2h` | 132 | 132 |
| `handicap` | 210 | 210 |
| doublons (clé, marché, ligne, côté) | 0 | **0** |

Football : **30 269 cotes, identiques au octet près** — le correctif ne touche
que les sports qui produisent des matchups `units`, c'est-à-dire le tennis.

⚠️ Ce tableau mesure ce qui sort du scraper, **pas** un gain de détections. Le
gain réel dépend de l'appariement des lignes des books avec celles de Pinnacle,
et se lira sur la base après quelques cycles — pas avant. Six hypothèses ont
déjà été démenties par la mesure dans ces sessions ; celle-ci attend la sienne.

### 21.6 Vérification à faire tourner sur la VM

Après `git pull` et redémarrage du service, laisser passer deux ou trois cycles
puis :

```bash
.venv/bin/python -m scripts.check_tennis_totals
```

Attendu : `pinnacle` avec une quinzaine de lignes entre 15 et 25, plus 2.5.
S'il reste à une seule ligne, le correctif n'est pas en service.

La sonde répond aux trois questions dans l'ordre où elles se conditionnent —
Pinnacle publie-t-il des lignes de jeux, un book en offre-t-il en face, et
combien de détections sur 24 h — puis rend le verdict de la prédiction du
§21.6 bis. Deux tests la tiennent, un par régime : une sonde qui rendrait le
même verdict avant et après le correctif ne servirait à rien.

⚠️ **La commande d'origine de cette section ne pouvait pas tourner**, et
personne ne l'aurait su avant de la coller. Trois défauts, dont deux fatals :

| Défaut | Conséquence |
|---|---|
| `sqlite3` appelé | absent de la VM (§20.12) — `command not found` |
| colonne `outcome_line` | elle s'appelle **`line`** — `no such column` |
| balayage de `quotes` par `fetched_at` | le motif interdit par le §17.7 |

Le troisième n'est plus un problème, et c'est utile de savoir pourquoi :
l'interdiction du §17.7 visait le régime d'écriture dense, ~2,5 M lignes par
heure. Depuis l'écriture parcimonieuse du §18.5 il en reste ~63 000, soit
quelques secondes de lecture. **Une règle de prudence peut être rendue caduque
par un correctif ailleurs** — celle-ci l'a été par le §18.5, sans que rien ne
la relie à lui.

⚠️ La leçon est celle du §11 appliquée à la documentation : **une commande
écrite mais jamais exécutée est du code non testé.** Elle figurait comme « la
première chose à faire au démarrage » et aurait coûté un aller-retour au
prochain démarrage, sur le point le plus urgent de la liste.

Puis, une fois des matchs clôturés :

```bash
.venv/bin/python -m scripts.clv_split --by sport,market --min 20
```

⚠️ `clv-report` n'accepte **ni `--sport` ni `--market`** — seulement
`--detected`, `--since` et `--until`. Tout découpage passe par `clv_split`.

### 21.6 bis Le taux attendu — une prédiction falsifiable

Une heure après la mise en service : **283 couples devigués, 630 cotes de books
comparables, 0 détection**. Ce zéro n'est pas une anomalie, et le vérifier
comptait autant que le correctif lui-même.

`scripts/market_supply.py` sur la même fenêtre, football contre tennis :

| totals | football | tennis (jeux) |
|---|---|---|
| cotes comparables (2 h) | 10 708 | 630 |
| médiane d'EV | −8,1 % | **−7,9 %** |
| p90 | −5,1 % | **−4,5 %** |
| au-dessus de +2 % | 0,3 % | **0,5 %** |
| détections réelles (24 h) | 77 | — |

La distribution d'EV des totaux de jeux est **indiscernable de celle des totaux
de football**, légèrement meilleure même. Les trois pannes possibles sont
écartées : Pinnacle price des deux côtés sur 283/283, aucune référence écartée
pour marge aberrante, zéro book incomplet. La chaîne est saine ; la marge des
books belges y est simplement normale.

Le tennis pesant 5,9 % du volume comparable du football, le taux attendu est :

> **77 × 0,059 ≈ 4 à 5 détections de totaux de tennis par jour**, à EV moyenne
> autour de **+7 à 8 %** (les totaux rendent moins que les h2h : +7,8 % contre
> +11,4 % au football).

⚠️ À vérifier 24 h après la mise en service, avec la requête du §21.6. **Moins
de 2 : il reste un blocage, reprendre la chasse. Autour de 5 : le correctif fait
son travail.** Une estimation antérieure de 7/jour, tirée d'une fenêtre de 3 h,
est à ignorer — celle-ci repose sur 24 h.

Reste inconnu, et c'est la vraie question : la **CLV** de ces détections. Celle
du tennis h2h est excellente (+15 % en premium) ; celle des totaux de jeux n'a
jamais existé. `.venv/bin/python -m scripts.clv_split --by sport,market` tranchera une fois
des matchs clôturés — pas avant.

### 21.8 Cinq books sur huit sont muets — 48 % des détections

Relevé le 19/08 : `book_alerts_off` contient **betano_be, betfirst,
napoleon_be, starcasino_sport**.

| book | détections 7 j | EV moy | état |
|---|---|---|---|
| starcasino_sport | 1 217 | +10,9 % | **muet** |
| unibet_be | 1 177 | +10,6 % | actif |
| golden_palace | 785 | +11,3 % | actif |
| napoleon_be | 661 | +11,7 % | **muet** |
| betano_be | 473 | +10,9 % | **muet** |
| ladbrokes_be | 407 | +12,6 % | actif |
| circus_be | 350 | +12,6 % | actif |
| magicbetting | 169 | +17,2 % | **muet** (depuis le 20/08) |

**2 520 détections sur 5 239 — 48 % — n'atteignent plus aucun canal**, dont le
book de tête en volume.

⚠️ **`magicbetting` s'est ajouté aux muets le 20/08**, portant le compte de
quatre à cinq. **Le compte y est limité** — mise en sourdine volontaire, rien à
réparer : alerter sur un book où l'on ne peut plus miser ne produit que du
bruit.

**Ce silence-là dit quelque chose sur l'edge, et c'est la question du §20.4.**
Le book limité est celui dont la CLV mesurée est **la plus haute du parc,
+17,2 %** — devant les sept autres. Un book limite les comptes qu'il juge
gagnants, avec ses propres données de clôture et sans rien savoir de notre
mesure. Deux jugements indépendants désignent donc le même book comme le plus
rentable. C'est un signal externe, faible mais réel, que la CLV mesure bien un
edge et pas un artefact de mesure — le premier qui ne vienne pas de nos propres
chiffres. À ne pas surinterpréter : n=169 détections, et une limitation dépend
aussi du volume et du profil de mise, pas seulement de la rentabilité.

**Deux motifs de silence, à ne pas confondre.** Un book coupé par choix
d'exploitation se rallume ; un book où le compte est limité, non. Seul
`magicbetting` est documenté ici comme limité (20/08) — **le motif des quatre
autres n'a jamais été relevé**, et cette colonne reste donc à remplir. Sans
elle, la liste des muets se relit comme un réglage réversible, ce qu'elle n'est
plus entièrement.

Le réglage `/book` ne coupe QUE la notification
(`alerter.py:667`) : les cotes restent collectées, stockées, suivies en courbe
et exportées, et les détections restent écrites dans `value_bets`. C'est
voulu — faire taire sans cesser de mesurer.

⚠️ **Conséquence sur toute analyse à partir d'ici.** `value_bets` mélange
désormais ce qui est jouable et ce qui ne l'est plus, dans une proportion qui
n'est plus marginale. Une moyenne de CLV tous books confondus mesure un flux
qui n'existe pour personne : un book muet à CLV faible tire les chiffres vers le
bas et fait croire à une dégradation pendant que le flux réellement reçu
s'améliore. **Toute comparaison de books doit séparer les deux groupes**, et
tout chiffre « global » doit dire lequel des deux il décrit.

Conséquence aussi sur la prédiction du §21.6 bis : 4-5 **détections** par jour
sur les totaux de tennis, mais ~2-3 **alertes**, deux des meilleures
opportunités relevées venant de StarCasino, muet.

**Question ouverte, mesurable.** StarCasino est muet sur TOUS les sports alors
que le constat de départ portait sur son ROI au tennis. Si son football est
sain, 1 200 détections sont coupées pour un problème qui n'en concerne qu'une
partie. À trancher avec `.venv/bin/python -m scripts.clv_split --by book,sport` avant
d'envisager quoi que ce soit — un mute par sport serait un développement, pas
un réglage.

### 21.11 La prédiction du §21.6 bis, vérifiée — 10 détections, pas 4-5

Relevé du 20/08, 24 h après la mise en service, avec
`.venv/bin/python -m scripts.check_tennis_totals` :

| | prédit (§21.6 bis) | observé |
|---|---|---|
| détections `tennis/totals` / jour | 4 à 5 | **10** |
| EV moyenne | +7 à 8 % | **+6,9 %** (pondérée) |

Pinnacle publie **33 638 cotes en jeux sur 24 lignes** ; 600 matchs portent une
ligne de jeux et **580 ont un book en face**. Le correctif du §21.2 est en
service, et le gisement du §21.6 bis existe.

**Le compte double du prédit devait d'abord se lire comme la panne du §21.3** —
un total de jeux lu à la mauvaise échelle fabrique des paris de valeur inventés,
et c'est exactement le profil qu'il produirait. Trois vérifications l'écartent,
et aucune ne repose sur la bonne foi du scraper :

1. **L'appariement exige la clé exacte** `(event_key, market, line)`
   (`main.py:448`). Une ligne de sets (2,5-4,5) ne peut pas rencontrer une ligne
   de jeux (≥ 14,5) : les échelles ne se recoupent pas, et c'est *pourquoi* la
   règle étroite du §21.3 est sûre.
2. **Pinnacle est restreint à `period == 0`** (`pinnacle.py:373`). Les totaux de
   jeux **par set** — la seule autre échelle qui pourrait se confondre — ne
   rentrent jamais dans la référence.
3. **Les handicaps sont exclus d'office** (`main.py:446`), pour le motif du
   §21.3 déjà.

L'EV observée est le second argument, et il est indépendant : une confusion
d'échelle produit des EV erratiques et énormes, pas une moyenne à +6,9 % qui
tombe dans la bande prédite +7-8 %.

**L'explication du 10 est la concentration, pas un gisement plus large.** Une
détection vaut pour un couple (match, book, ligne) — dédoublonné par
`find_value_bet_id`, donc pas de recomptage entre cycles. Un seul match mal
pricé rend donc plusieurs détections : 3 books × 3 lignes en font 9 à lui seul.
La prédiction, elle, était une moyenne journalière tirée d'un ratio de volume.
Sur un Poisson de moyenne 4,5, P(≥ 10) ≈ 1,7 % — et un Poisson **sous-estime**
la dispersion d'un phénomène groupé. L'écart ne demande donc pas d'explication
supplémentaire.

Le volet **D** de la sonde fait tourner ces contrôles à chaque exécution : il
liste chaque détection avec son échelle, dénonce toute ligne tombant dans la
bande morte 4,5-14,5 (signature §21.3), signale une référence Pinnacle à un seul
côté ou une référence de repli (§17), et affiche la concentration.

⚠️ **Ce que ce relevé ne dit pas : si ces paris sont bons.** Une détection est
une opportunité, pas un gain — c'est la leçon du §20.4. La CLV des totaux de
jeux n'a toujours jamais été mesurée. `clv_split --by sport,market --min 20`
tranchera, et rien avant lui.

⚠️ Un détail à ne pas relire comme une panne : `unibet_be` sort 6 cotes sous
`GAMES_MIN_LINE`, sur une ligne à **14,5**. Ce n'est pas une ligne de sets — un
set ne dépasse pas 13 jeux, tie-break compris. C'est un total de match court, et
le seuil à 15 de la sonde le classe simplement hors « jeux » au comptage.

### 21.12 Le seuil des `under` : la tâche est morte, la CLV est saine

Session du 20/08. Le §21.9 pt 4 demandait de « relever le seuil d'EV sur les
`under` » d'après le §20.8. Deux sondes ont été écrites pour choisir le chiffre
(`scripts/under_threshold.py`, `scripts/clv_independence.py`). **Aucun seuil n'a
été modifié, et il ne faut pas en modifier.**

**1. Le constat du §20.8 ne se reproduit plus.** Sur 774 `under` clôturés contre
437 à l'époque :

| | §20.8 (17/08) | 20/08 |
|---|---|---|
| h2h | +7,11 % | +6,45 % (n=13 544) |
| OVER | +6,50 % (n=418) | +5,88 % (n=904) |
| UNDER | **+3,80 %** (n=437) | **+6,25 %** (n=774) |

Les `under` sont désormais **devant** les `over`, et l'écart de 0,37 point pour
un σ combiné de 0,70 vaut **0,5 σ** — rien. Par ligne, retournement franc :
under 3.5 passe de +3,45 % (le plus faible du §20.8) à **+9,47 %** (le plus fort).
L'échantillon a presque doublé et il dit l'inverse. **Le §20.8 mesurait du
bruit** ; son p ≈ 0,007 n'a pas survécu au doublement des données.

**2. L'effet qui semblait le remplacer n'est pas propre aux `under`.** La CLV
monte fortement avec l'EV — mais sur tous les marchés :

| marché | n | pente CLV/EV | σ |
|---|---|---|---|
| h2h | 13 544 | +0,86 | 0,01 |
| over | 904 | +0,71 | 0,03 |
| under | 774 | +1,09 | 0,04 |

Relever le seuil des seuls `under` aurait donc traité comme un défaut local une
propriété de tout le flux. **La vraie question est celle du seuil GLOBAL**, et
elle reste ouverte.

**3. La CLV n'est PAS un miroir de l'EV — hypothèse testée et écartée.** Ces
pentes proches de 1 faisaient craindre le pire : CLV et EV partagent le même
numérateur (la cote prise) et ne diffèrent que par le déplacement de la ligne
juste. Si celle-ci ne bougeait pas, la CLV réénoncerait l'estimation du modèle
au lieu de la confronter au marché — elle serait nécessairement flatteuse, et le
§20.4 s'expliquerait sans mystère. `clv_independence` sur 15 222 paris clôturés
tranche :

| | |
|---|---|
| déplacement médian de la ligne juste | **5,61 %** |
| déplacement moyen | 11,58 % |
| 9e décile | 27,77 % |
| clôtures identiques au bit près | 13,1 % (sous le seuil d'alarme, structurel) |
| délai médian détection → clôture | 8,3 h |
| **R² de CLV sur EV** | **0,257** |

**La ligne bouge, et beaucoup.** Un R² de 0,26 dit que les trois quarts de la
variance de la CLV ne viennent pas de l'EV. **Le KPI du projet tient debout** :
la CLV mesure bien le déplacement du marché. C'est la septième hypothèse
démentie par la mesure dans ces sessions, et la première qui soit une bonne
nouvelle.

⚠️ Ça ne referme pas le §20.4. Les trois lectures y restent entières, dont la
troisième — un devig qui surestimerait l'outsider au tennis. Une CLV
indépendante peut rester fausse si la référence l'est.

**4. La droite, exploitable.** Globalement :

> **CLV ≈ 0,849 × EV − 3,22** (CLV moyenne +6,40 % pour EV moyenne +11,33 %)

Le marché reprend ~3,2 points fixes, puis laisse 85 % du reste. Conséquences :

- **La CLV s'annule à EV ≈ 3,8 %.** En dessous, une détection a une CLV
  attendue négative.
- Le plancher réel de détection est **5 %** (aucun `under` clôturé sous ce
  seuil), pas les 2 % de `ScanConfig.min_ev_pct` — à élucider, c'est
  probablement le réglage du daemon.
- **À EV 5 %, la CLV attendue n'est que +1,0 %** ; à 6 %, +1,9 %. Le gros du
  volume détecté vaut donc bien moins que la moyenne de +6,40 % ne le suggère.
  **C'est l'argument sérieux pour relever le seuil global** — mais c'est un
  arbitrage volume/qualité qui n'a pas été fait ici.

La droite colle à 0,3 point près sur les tranches d'EV 4–6, 6–8 et 8–12 %. Sur
la tranche `EV ≥ 12 %` l'observation est à +21,94 %, très au-dessus de ce que
la droite donnerait pour une EV moyenne de 18 % — mais **l'EV moyenne de cette
tranche n'a pas été relevée**, et `max_ev_pct` valant 1000, elle peut être bien
plus haute. Non tranché : conclure à une non-linéarité serait prématuré. Une
colonne « EV moy » a été ajoutée au tableau 3 de `under_threshold` pour la
prochaine exécution.

**Ce qu'il ne faut PAS faire** : lire le tableau 4 de `under_threshold` comme un
menu. « Seuil à 8 % → CLV +13,46 % » est vrai et trompeur — le gain vient de la
pente globale, pas des `under`, et il se paie en volume (290 paris gardés
sur 774).

### 21.13 Élargir les marchés — mesuré, et les deux commentaires faux

Session du 20/08, en vue du §21.10 (vendre les value bets, donc besoin de
volume). Le projet n'exploite que `h2h` et `totals` en match plein : sur
15 222 paris clôturés, **aucun handicap, aucune mi-temps**. Deux exclusions en
sont la cause. `scripts/market_expansion.py` lit l'API sans les filtres.

**Relevé sur l'API réelle, 51 098 marchés ouverts au football, 1 188 au tennis :**

| football | marchés | part |
|---|---|---|
| période 0 (match plein) | 32 947 | gardé |
| **période 1 (mi-temps)** | **18 098** | **jeté — 35,5 %** |
| période 8 | 53 | jeté |
| dont période 0 : moneyline | 12 377 | gardé |
| dont période 0 : `spread` | 8 876 | **jeté — 26,9 %** |
| dont période 0 : total | 8 201 | gardé |
| dont période 0 : `team_total` | 3 493 | jeté (non mappé) |

**1. `circus.py:35` était factuellement FAUX.** Il affirmait « mi-temps —
Pinnacle ne price que le match complet ici ». Pinnacle publie **18 098 marchés
de période 1** au football, dont 6 475 moneyline et 4 880 totals. La croyance
venait de notre propre filtre `pinnacle.py` (`period != 0`) : le scraper
n'ayant jamais rien remonté, le soft a été exclu à son tour. **Raisonnement
circulaire, corrigé dans le code.** Les softs, eux, les proposent — Gaming1
nomme ses `1st-half-*`, Betano son `OUH1`.

**2. Le « ~45 % » des handicaps était surévalué de moitié.** Mesuré : `spread`
fait **26,9 %** de la période 0 au football, 36,7 % au tennis. Corrigé dans
`main.py`.

**3. Pinnacle signe chaque côté, sans une exception** : 8 876/8 876 au football
et 324/324 au tennis en lignes opposées (`[-1.25, 1.25]`, `[-3.0, 3.0]`). La
convention de la référence est donc parfaitement régulière — **tout le travail
de normalisation est du côté des softs**, book par book.

**Le classement, en types DÉJÀ supportés** (moneyline + total, football) :

| chantier | marchés | gain relatif | risque |
|---|---|---|---|
| **mi-temps (période 1)** | **11 355** | **+55 %** | faible |
| handicaps (période 0) | 8 876 | +43 % | élevé |
| handicaps de mi-temps | 4 960 | — | élevé |

### Les handicaps : le blocage n'est pas celui qu'on croyait

**Mesuré le 21/08, et ça change le chantier.** Le commentaire de `main.py`
attribuait le refus des handicaps aux conventions de signe des books soft.
C'est vrai mais secondaire. Le vrai obstacle est chez nous :

> `_group_quotes` groupe sur la ligne **SIGNÉE** — `(event, market, line)`.
> Pinnacle publiant `home@-1` et `away@+1`, les deux côtés d'un MÊME marché
> tombent dans DEUX groupes séparés, d'une issue chacun. `_devig_group` en
> exigeant au moins deux, **aucun handicap n'est devigable, y compris chez la
> référence.**

Exploiter les handicaps demande donc d'abord une **clé de groupe canonique**
(|ligne| + côté normalisé), avant toute question de convention soft. C'est un
changement au cœur du devig, pas un mappage de codes — nettement plus lourd que
les mi-temps.

⚠️ **Deuxième surprise : aucun soft ne produit de handicap aujourd'hui.** Le
relevé ne fait apparaître que Pinnacle (7 531 groupes). Circus les exclut
explicitement (`Handicap`, `handicap-TeamNumber` dans ses BetType non
exploités) ; pour les autres, la sonde le dit désormais book par book — « aucun
handicap » et « lignes illisibles » sont deux diagnostics opposés qu'elle
confondait.

**Convention de Pinnacle : SIGNÉE, sans exception** — 6 590 groupes en lignes
opposées. Les 941 restants sont des handicaps de **ligne 0**, qui n'ont pas de
signe par nature ; les compter comme « miroir » faisait déclarer Pinnacle
« deux conventions mêlées », un faux verdict de refus sur la référence même du
projet. Corrigé.

**Les handicaps — première mesure à faire, 21/08.** Le blocage n'est pas le
mappage : `betano.py`, `unibet.py`, `goldenpalace.py`, `ladbrokes.py` et
`betcenter.py` **parsent déjà** les handicaps, ils sont jetés en aval
(`_fetch_all_parallel`). Toute la question est la convention de ligne, et elle
se mesure :

```bash
.venv/bin/python -m scripts.handicap_conventions --sport soccer
```

Elle classe chaque book en **SIGNÉE** (les deux côtés en lignes opposées,
comme Pinnacle — alignable directement), **MIROIR** (même |ligne| des deux
côtés, le signe se déduit de home/away) ou refusé. Un book qui MÊLE les deux
conventions est refusé : lire son signe serait juste une fois sur deux, et
l'erreur ne lèverait rien.

⚠️ `_fetch_all_parallel` accepte désormais `keep_handicaps=True`, réservé aux
sondes. La production continue de les jeter. Ce détour évite de refaire la
collecte dans la sonde — une sonde qui recalcule autre chose que la production
ment (§17.7).

**La mi-temps est le bon premier chantier, et ce n'est pas le plus gros par
hasard : c'est le moins risqué.** Un `h2h` ou un `totals` de mi-temps a
exactement la même sémantique qu'en match plein — aucun devig nouveau, aucune
convention de signe. Les handicaps, eux, demandent de normaliser chaque soft un
par un, avec la panne silencieuse du §21.3 au bout en cas d'erreur.

⚠️ **Mais le blocage technique de la mi-temps est réel, et il est structurel.**
`OddQuote` **n'a pas de champ `period`**, et le devig groupe sur
`(event_key, market, line)`. En l'état, un `total 1.5` de mi-temps et un
`total 1.5` de match plein tomberaient dans le MÊME groupe : marge plausible,
deux côtés présents, aucune erreur levée, et une ligne juste fausse. C'est
exactement le §21.3. **La période doit devenir partie de la clé AVANT que la
moindre cote de mi-temps entre dans le pipeline** — c'est la condition, pas une
amélioration ultérieure.

Portée du chantier mi-temps : `models.py` (période sur `OddQuote`, ou types de
marché distincts), `pinnacle.py` (lever le filtre en portant la période), les
scrapers softs (rouvrir les `1st-half-*` aujourd'hui exclus), le schéma de
`quotes`, le groupement du devig, et l'alerteur (une alerte doit DIRE que c'est
une mi-temps).

⚠️ **Ces chiffres sont un plafond, pas une prévision.** Un marché chez Pinnacle
ne produit une détection que si un soft le price en face. Le §21.6 bis l'a déjà
montré : correctif en service, 630 cotes comparables, zéro détection la première
heure. Le vrai gain se lira sur la base, après quelques cycles.

⚠️ **Et le §21.12 complique l'arbitrage commercial.** Élargir à seuil constant
gonfle surtout le bas du spectre, là où la CLV attendue n'est que +1,0 % (EV
5 %) à +1,9 % (EV 6 %). Du volume vendable qui ne rapporterait presque rien à
l'acheteur. **Trancher le seuil global devrait probablement précéder
l'élargissement**, pas le suivre.

### 21.14 Les mi-temps sont en service — muettes, mais mesurées

Session du 20/08, suite du §21.13. Les marchés de première mi-temps entrent
dans le pipeline. **Aucune alerte n'en sort**, à la demande : ils sont
collectés, devigués, stockés, clôturés et suivis en CLV — seul l'envoi est
coupé. Même principe que la sourdine par book du §21.8.

**Le choix de conception, et pourquoi il est le bon.** La période n'est PAS un
champ posé à côté du marché. Ce sont des **types de marché distincts** :

| | |
|---|---|
| `h2h_h1` | 1X2 / moneyline, première mi-temps |
| `totals_h1` | over/under, première mi-temps |

Un champ `period` aurait fait porter la sûreté à chaque chemin de code qui
construit une clé : un seul oubli, et un `total 1.5` de mi-temps rejoignait le
`total 1.5` du match plein — deux côtés présents, marge plausible, aucune
erreur levée, ligne juste fausse. Le §21.3 en version étendue. **Avec des types
séparés, la séparation est acquise par construction, et tout code écrit avant
eux (`market == MarketType.TOTALS`) ne voit plus que le match plein.** Un
chemin non mis à jour IGNORE les mi-temps au lieu de les confondre : l'erreur
tombe du bon côté.

**Football seulement.** Chez Pinnacle, `period 1` vaut mi-temps au football
mais **premier set au tennis**. Les réunir sous un même type rejouerait la
confusion jeux/sets du §19.2. Le tennis reste au match plein tant qu'une
capture n'aura pas établi ses propres types.

**Ce qui a changé, fichier par fichier :**

| fichier | changement |
|---|---|
| `models.py` | `H2H_H1`, `TOTALS_H1`, plus `is_half_time`, `base_market`, `TOTALS_LIKE`, `HALF_TIME_MARKETS` |
| `pinnacle.py` | `period in (0, 1)`, football seul ; `spread` reste dehors en période 1 aussi |
| `main.py` | `_flip_outcome_for_swap` teste `TOTALS_LIKE` — un `totals_h1` tombait sinon dans la branche home↔away |
| `alerter.py` | `is_half_time` → aucun canal, ni principal, ni premium, ni critique |
| `betano.py` | `OUH1` quitte les exclus et devient `TOTALS_H1` ; les dispatches passent par `base_market` |
| `circus.py` | quatre codes mappés ; dispatch par `base_market` — sinon un `h2h_h1` tombait dans la branche des totaux et ses 506 marchés disparaissaient en silence |

⚠️ **Le même piège a été rencontré dans les TROIS scrapers** : `pinnacle.py`,
`betano.py` et `circus.py` commutaient tous sur `market == MarketType.H2H` ou
`TOTALS`. Un nouveau type de marché tombe donc, par défaut, dans une branche
qui ne l'attend pas. **Tout scraper à qui l'on ajoutera la mi-temps devra
passer son dispatch par `base_market`** — c'est la première chose à vérifier,
avant même le mappage des codes.

⚠️ **Aucune migration de schéma.** `quotes.market` et `value_bets.market` sont
des colonnes TEXT : les nouveaux types s'y écrivent sans changement. C'est un
effet secondaire heureux du choix par types, pas une chance.

**État par book au 21/08** — lu dans les tables de marché, pas de mémoire :

| book | mi-temps | état |
|---|---|---|
| **Circus** | `h2h_h1` + `totals_h1` | ✅ 4 codes en service, ~93 700 cotes |
| **Betano** | `totals_h1` seul | ⚠️ **aucun 1X2 de mi-temps** — confirmé sur dump frais (`OUH1` ×753, rien d'autre) |
| MagicBetting | — | ❌ **n'en propose pas**, mesuré deux fois sur 18 identifiants |
| Unibet + 711/Bingoal/Scooore | — | ❌ **abandonné**, coût disproportionné (ci-dessous) |
| GoldenPalace, Ladbrokes | — | ❓ rien vu sur l'endpoint interrogé — ne prouve pas une absence |
| StarCasino, Betfirst, Napoleon | — | ❓ **jamais sondés**, pas de table d'identifiants |
| Betcenter | — | hors périmètre, non utilisé |

Les cinq codes mappés, tous relevés sur dump réel par
`scripts/discover_half_time.py`, **aucun déduit** :

| book | code | libellé | volume |
|---|---|---|---|
| Circus | `half-time-totals-over-under-OverUnder` | 1ère mi-temps - Total de buts | ×603 |
| Circus | `1HalfP1XP2` | 1ère mi-temps - Vainqueur | ×492 |
| Circus | `1st-half-total-OverUnder` | … (Plus de/Moins de) | ×163 |
| Circus | `1st-half-1x2` | 1ère mi-temps - Vainqueur | ×163 |
| Betano | `OUH1` | But en première mi-temps | ×753 |

⚠️ **Circus écrit chacun de ses deux marchés de DEUX façons**, comme ses
totaux de jeux au tennis où n'en reconnaître qu'une avait coûté 64 % du volume.
Ici la moitié la plus facile à manquer est la plus grosse.

**Pour relever un book sans deviner :**

```bash
.venv/bin/python -m scripts.discover_half_time              # dumps sur disque
.venv/bin/python -m scripts.discover_half_time_api          # books en API
.venv/bin/python -m scripts.discover_half_time --detail <BetType>   # noms d'issues
```

⚠️ Ces sondes testent le code **et** le libellé, et disent quand elles n'ont
rien su lire — sans quoi un champ mal nommé donnerait « ce book n'a pas de
mi-temps » au lieu de « je n'ai pas su lire ». Les deux se ressemblent trait
pour trait, et la version API a produit ce faux verdict deux fois avant d'être
corrigée.

**Unibet : chantier fermé le 21/08, sur mesure.** `sevenelevenbe.py`,
`bingoal.py` et `scooore.py` importent `_MARKET_BY_TYPE_ID` d'Unibet : l'ouvrir
aurait valu **quatre books d'un coup**, le meilleur rapport effort/couverture
restant. Trois constats l'ont fermé :

1. **Kambi identifie ses mi-temps par `criterion.id`** — `11928` = « Total
   Goals - 1st Half », `11927` = « Half Time » — et non par `betOfferType.id`.
   `parse_listview` ne regarde que le second : ce n'est pas une entrée à
   ajouter dans une table, c'est un changement de la logique de mappage, à
   répercuter sur les quatre books.
2. **Ces deux identifiants sont des entrées de CATALOGUE**, rattachées à
   aucune offre cotée. L'endpoint `listView` — le seul qu'utilise le scraper —
   n'expose pas les offres de mi-temps. Les atteindre demanderait
   `betoffer/event/{id}.json`, **un appel par événement** : 700 à 1 400
   requêtes HTTP par cycle, là où `fetch_all_events` en fait déjà jusqu'à 100.
3. **Le cycle est déjà saturé** : 34 s de traitement plus 20 s de pause, contre
   ~17 s avant ce chantier.

**Le facteur décisif reste la valeur.** Les mi-temps mesurées chez Circus
donnent −8,5 % d'EV médiane sur les totaux et −10,0 % sur les h2h, pour **zéro
détection**. Rien ne laisse penser qu'Unibet ferait mieux : on triplerait le
coût de collecte pour du volume dont la valeur mesurée est nulle. **À rouvrir
seulement si la CLV des mi-temps de Circus s'avère bonne.**

Betano étant muet dans `/book` (§21.8), son flux de mi-temps est doublement
silencieux — ce qui n'empêche ni la détection ni la CLV.

**Ce qui garde le tout :** `tests/test_half_time.py`, 12 tests. Les deux qui
comptent — un soft de mi-temps ne se valorise PAS sur la ligne de match plein
(sinon +61 % d'EV fantôme sur l'exemple), et la clôture d'une mi-temps ne lit
pas les cotes du match plein (sinon la CLV demandée serait un chiffre faux et
plausible). Les trois tests qui gardaient l'ancien comportement ont été
réécrits en conservant ce qui reste vrai : la période 2 est toujours écartée,
et `OUH1` ne doit **jamais** devenir un `TOTALS`.

**Le coût, mesuré le 20/08.** Le cycle passe de ~17 s à **31-38 s** (moyenne
~34 s). Mais le chiffre qui compte n'est pas là : `daemon` fait
`time.sleep(breather)` APRÈS le cycle, et le service passe `--breather 20`.
**La période réelle entre deux rafraîchissements d'un même marché passe donc de
~37 s à ~54 s, soit +46 % de péremption.** C'est la vraie contrepartie de ce
chantier, et elle porte sur TOUT le flux, mi-temps comprise ou non.

Le levier immédiat si ça pèse : `--breather 5` dans l'unité systemd, qui
ramène la période à ~39 s au prix d'un CPU jamais au repos. À ne faire que si
la CLV des mi-temps le justifie — sinon le bon geste est de refermer le
robinet, pas d'accélérer.

**Relevé du 20/08, après mappage de Circus :**

| | |
|---|---|
| Pinnacle `totals_h1` | 408 846 cotes, 1 367 events, 13 lignes [0,5 … 3,5] |
| Pinnacle `h2h_h1` | 90 378 cotes, 769 events |
| Circus `h2h_h1` | 47 927 cotes, 624 events |
| Circus `totals_h1` | 45 788 cotes, 734 events, 3 lignes [0,5 … 2,5] |
| échelle | **1,14** contre 3,53 au match plein — saine |
| **détections** | **ZÉRO** |
| Betano | mappé, **aucune cote** |

✅ **Zéro détection est le comportement CORRECT — mesuré le 21/08.**
`market_supply` sur 2 h tranche : la chaîne est saine, c'est la marge des books
qui bloque.

| | totals_h1 | h2h_h1 |
|---|---|---|
| couples devigables / cotés | 6 088 / 6 088 | 735 / 735 |
| références écartées pour marge | 0 | 0 |
| cotes de books comparables | 1 368 | 222 |
| **EV médiane** | **−8,5 %** | **−10,0 %** |
| p90 | −3,5 % | −6,6 % |
| meilleure EV observée | **+0,6 %** | **−1,9 %** |
| au-dessus de +2 % | **0** | **0** |

À comparer aux références du §21.6 bis : football totals de match plein
−8,1 % de médiane, tennis totaux de jeux −7,9 %. **Les totaux de mi-temps sont
donc dans la norme** (leur p90 à −3,5 % est même meilleur), simplement avec une
queue plus mince. Au taux du football plein (0,3 % au-dessus de +2 %), on
attendrait ~4 opportunités sur ces 1 368 cotes ; en observer 0 a une
probabilité de 1,8 % — bas, mais pas absurde. **Le h2h de mi-temps est
franchement moins bon** (−10,0 % de médiane, meilleure EV à −1,9 %) : Circus y
cote plus large.

**Conclusion : rien à réparer côté détection.** Le gisement de mi-temps est
réel en volume mais pauvre en valeur aux seuils actuels. À reconsidérer si le
seuil global du §21.12 bouge.

### Le nul s'écrivait « Egalité » — un mot, 66 % du marché

66 % des `h2h_h1` de Circus étaient jetés : 864 cotes sur 1 318 en « book
incomplet sur cette ligne », contre 0 pour les totaux. `--detail` a donné la
cause en une ligne :

```
×499   'Egalité'   🔴 TRADUIT PAR AUCUNE des deux fonctions — cote JETÉE
×163   'X'         → h2h:draw
```

**Gaming1 nomme le nul différemment SELON LE MARCHÉ, pas selon la langue.**
`P1XP2` et `1st-half-1x2` écrivent « X » ; `1HalfP1XP2` écrit « Egalité », que
`_DRAW = {"x", "nul", "draw"}` ne connaissait pas. Les 499 cotes de nul
partaient à la poubelle, et comme `find_value_bets` écarte un groupe amputé
d'une issue au contrôle de structure, **c'est le marché entier qui
disparaissait** — 1 497 cotes pour 499 mots manquants. Corrigé.

⚠️ **La cause de fond n'était pas le mot, c'était une asymétrie.** Un BetType
inconnu était signalé dans les logs ; un NOM D'ISSUE inconnu disparaissait sans
bruit. `parse_prematch` accepte désormais `unknown_outcomes`, et le daemon
journalise « noms d'issues non traduits, cotes JETÉES ». Les noms d'équipes en
sont exclus, sans quoi la liste devient illisible donc ignorée.

**Le mode `--detail` vaut pour TOUT marché mappé qui rendrait peu** — un code
reconnu ne garantit pas des issues traduites :

```bash
.venv/bin/python -m scripts.discover_half_time --detail <BetType>
```

✅ **Le match plein est indemne — vérifié le 21/08 sur le dump réel.**
`--detail P1XP2` donne « X » sur **979 marchés sur 979**, aucun « Egalité ».
La panne était donc confinée à `1HalfP1XP2`, et le flux principal n'a jamais
rien perdu. C'était la seule hypothèse de cette session qui aurait coûté
rétroactivement ; elle est écartée.

🔴 **Betano est MORT depuis deux jours, et ça n'a rien à voir avec les
mi-temps.** `data/prematch/soccer.json` date du **19/08 09:36**, relevé le
21/08 15:40. Le prématch est ignoré au-delà de 30 min d'âge
(`BETANO_PREMATCH_MAX_AGE_MIN`), donc **Betano ne remonte plus AUCUNE cote** —
`check_half_time` le confirme, zéro cote tous marchés confondus sur 24 h. Le
pont navigateur ne pousse plus. C'est un book sur huit hors service, à traiter
en priorité sur tout ce chantier.

⚠️ **Ancien constat, désormais dépassé :** zéro détection avec les deux côtés
en base. Ce n'est plus le
cas « normal » du §21.6 bis (où le soft manquait) : Pinnacle et Circus cotent
tous deux la mi-temps, sur des lignes qui se recouvrent (0,5 / 1,5 / 2,5). À
élucider avec l'outil qui a déjà répondu à cette question :

```bash
.venv/bin/python -m scripts.market_supply --sport soccer --market totals_h1 --hours 2
.venv/bin/python -m scripts.market_supply --sport soccer --market h2h_h1 --hours 2
```

Il décompose étape par étape pourquoi une cote de book n'est pas comparable :
ligne juste absente, book incomplet sur la ligne, ou simplement une EV qui
n'atteint jamais le seuil. **L'hypothèse la plus probable est la dernière** —
les marchés de mi-temps portent des marges de book plus larges que le match
plein — mais le §21.6 bis a montré que la vérifier vaut autant que le
correctif lui-même.

**Unibet : hypothèse de contamination DÉMENTIE.** `discover_half_time_api` a
relevé 3 marchés de mi-temps chez Kambi sous les identifiants `11927`/`11928`,
**qu'on ne mappe pas** : rien de faux n'entre aujourd'hui. La crainte du §21.14
— `parse_listview` mappant sur le seul `betOfferType.id` sans regarder
`criterion` — était fondée en théorie mais ne se réalise pas ici, Kambi donnant
des identifiants distincts à ses périodes. GoldenPalace et Ladbrokes n'ont rien
rendu sur l'endpoint interrogé, ce qui ne prouve pas une absence.

**Première mise en service, relevée le 20/08 :** `totals_h1` et `h2h_h1`
arrivent bien en base côté Pinnacle (8 794 cotes de totaux de mi-temps au
premier relevé), échelle saine — ligne moyenne **1,13** contre **3,45** au
match plein, exactement le rapport attendu entre 45 et 90 minutes. **Zéro
détection**, ce qui est normal : seul Betano est mappé côté soft.

**À surveiller :**

```bash
.venv/bin/python -m scripts.check_half_time
```

⚠️ Cette sonde a menti à sa première exécution : sa section A annonçait
« AUCUNE cote de mi-temps, le correctif n'est pas en service » pendant que sa
section B en comptait 8 794. Deux requêtes distinctes décrivaient le même fait
et se sont contredites. Corrigé : **une seule lecture alimente désormais les
deux sections**, la contradiction est impossible par construction. Leçon à
retenir au-delà de cette sonde — un diagnostic qui se contredit est pire
qu'absent, il envoie chercher une panne qui n'existe pas.

Elle vérifie l'existence du flux, **l'échelle des lignes** — une mi-temps doit
coter sous le match plein, moins de buts se marquant en 45 min qu'en 90, et
l'inverse signalerait un mélange des deux échelles — puis la CLV.

**La condition pour lever le silence** : que `check_half_time` section D, ou
`clv_split --by market`, montre une CLV comparable à celle du match plein sur
un effectif suffisant. Le §21.12 vient de rappeler qu'un constat tiré de 437
paris peut s'inverser à 774 : **ne pas allumer les alertes sous 200 paris
clôturés.** Le silence se lève dans `alerter.py`, à l'endroit marqué.

⚠️ **Volume attendu : faible au début**, un seul soft étant mappé. Le §21.13
annonçait +55 % de marchés *chez Pinnacle* ; ce plafond ne se réalisera qu'à
mesure que les softs seront ajoutés sur capture.

### 21.15 Deux pannes de production, vues au passage le 21/08

Repérées dans la sortie d'une collecte, **aucune n'est corrigée**. Ni l'une ni
l'autre n'a de rapport avec les chantiers de la session — elles se sont
signalées seules, ce qui est exactement ce qu'on attend des avertissements
ajoutés ces derniers jours.

**1. Betano live est en 403 — cookie expiré.**

```
Betano live skipped: 403 from Betano — your cookie likely expired.
Re-capture cf_clearance + datadome from your browser.
```

Le **prématch** continue de remonter (6 541 cotes au relevé), c'est donc une
perte partielle : seules les cotes en direct manquent. À rapprocher de l'autre
constat du 21/08 — `data/prematch/soccer.json` datait du **19/08 09:36** pour
un relevé du 21/08 15:40, et Betano ne remontait alors plus RIEN. Le pont a été
rallumé depuis. **Ces deux pannes sont distinctes** : le dump périmé venait du
pont navigateur arrêté, le 403 vient des cookies anti-bot. Les confondre ferait
rallumer un pont déjà actif.

**2. MagicBetting : `Gelijkspel` non reconnu.**

```
MagicBetting : issue 'Gelijkspel' non reconnue sur un marché à 2 issues
```

C'est **le même défaut que « Egalité » chez Circus**, en néerlandais : un
libellé d'issue qu'aucune table ne traduit, donc une cote jetée. Le
signalement, lui, fonctionne — c'est précisément le mécanisme ajouté le 21/08
après la panne Circus, et il a trouvé son deuxième cas en un jour.

⚠️ Le message dit « marché à 2 issues », ce qui interroge : un nul sur un
marché à deux issues n'a pas de sens. Soit le marché est un 1X2 dont une issue
manque déjà, soit `Gelijkspel` y est mal placé. **À regarder avant de simplement
ajouter le mot à une table** — la correction évidente masquerait peut-être un
problème de structure. MagicBetting étant limité (§21.8), l'urgence est faible.

**Ce que ces deux cas confirment.** Les libellés d'issues sont traduits par
égalité exacte dans TOUS les scrapers, et chaque book a ses propres mots dans
ses propres langues. C'est une classe de panne récurrente, silencieuse par
nature, et le seul remède est le signalement systématique des libellés non
traduits — à généraliser aux scrapers qui ne l'ont pas encore.

### 21.16 La couverture, enfin mesurable — et le pont n'est pas agnostique

Session du 21/08 au soir. Objectif : le §21.9 pt 1, remplacer la source de
résultats football. **Aucune source n'a été branchée, et c'est le constat qui
l'explique.**

**Ce qui bloque une session Claude Code ici.** Le proxy d'egress de
l'environnement ne laisse passer que quelques hôtes (registres de paquets,
Anthropic). `api.sofascore.com`, `thesportsdb.com` et `rapidapi.com` répondent
tous **403 au CONNECT**. Aucune source candidate n'est joignable depuis une
session, et donc aucun échantillon réel n'est capturable. Or la règle du
projet est que **le parseur se teste contre des échantillons réels**
(`src/score_sources.py`, en-tête) : écrire un parseur SofaScore ou autre
depuis ici reviendrait à deviner une forme de payload, c'est-à-dire à refaire
le §21.13 — deux commentaires faux, un chantier fondé dessus.

⚠️ **Conséquence à retenir pour les sessions futures** : brancher une nouvelle
source de données est un travail de NAVIGATEUR, pas de session. La session
écrit le plan, les sondes et les tests ; la capture se fait chez toi. C'est
déjà le motif des quatre ponts, il vaut aussi pour leur mise en place.

**1. La couverture était un critère invoqué, jamais mesuré.** Le §21.9 pt 1
élimine les API gratuites généralistes parce que « les détections portent sur
des centaines de ligues ». Personne ne l'a vérifié, et ce chiffre décide
pourtant lequel des trois chemins vaut la peine — le plus cher étant un
cinquième pont.

`scripts/scores_coverage.py` (20ᵉ sonde) le mesure sur NOTRE base, sans clé,
sans quota et sans réseau. Elle ne regarde aucune source : elle chiffre la
**demande**, seul côté mesurable hors ligne. Elle rend la part des détections
football qu'un catalogue limité aux cinq grands championnats réglerait, la
concentration par paliers (combien de ligues pour 50/80/90/95/99 % du volume),
la ventilation par catégorie de `src/leagues.py`, et la taille de la queue
longue.

    .venv/bin/python -m scripts.scores_coverage --min-ev 5

**Comment lire le résultat.** La part du top 5 est un **plafond**, pas une
promesse : une source « qui couvre les grands championnats » ne les couvre pas
forcément tous ni toute la saison. Et c'est la FORME de la queue qui tranche,
pas le nombre de ligues — cent ligues dont cinq portent 90 % du volume et cent
ligues à parts égales appellent deux décisions opposées.

⚠️ La sonde compte les détections sans ligne `events` (§19.7) et celles dont la
ligue est vide, **et les annonce séparément**. Sans ça, « nos détections
tiennent en peu de ligues » et « la moitié de nos détections n'a pas de ligue
en base » donnent le même tableau rassurant. Six tests couvrent le verdict,
l'exclusion du tennis, ce signalement, le filtre d'EV et les paliers.

**2. Le pont résultats n'est pas agnostique de sa source.** Le §21.9 pt 1
présentait la voie 3 comme « un parseur neuf » sur une plomberie réutilisable.
C'est faux, et vérifié en lisant les trois fichiers :

| Fichier | Ce qui est codé en dur |
|---|---|
| `scripts/betano_ingest_server.py` | `_handle_scores_plan` fabrique `/fixtures?date=…&timezone=UTC` ; `_handle_scores_ingest` exige un champ `response` et lit `errors` pour distinguer refus définitif et temporaire |
| `tools/scores-ingest.user.js` | `API_BASE`, l'en-tête `x-apisports-key`, le `@connect v3.football.api-sports.io` |
| `src/score_sources.py` | `BridgedFootballScores` appelle `parse_apifootball_results` en dur |

Le parti « le userscript ne comprend rien » (§18.6) tient toujours et reste
bon — mais il a pour corollaire que **c'est le serveur qui porte le
fournisseur**. Changer de source demande donc trois fichiers, dont le serveur
d'ingestion qui sert aussi les trois autres ponts. Rien d'insurmontable ; le
chiffrage du §21.9 était simplement optimiste.

Le seul point vraiment agnostique est plus bas : `provider_for(sport)` est un
point d'extension net, et `src/scores.py` ne nomme aucun fournisseur. Le
rapprochement (`bind_results`, `match_event`, son compteur `sans_candidat`) ne
bougera pas d'une ligne quelle que soit la voie retenue.

**3. 🔴 La voie RapidAPI est MORTE — vérifiée, plus déduite.** Le listing
`rapidapi.com/api-sports/api/api-football` répond **« NOT_FOUND — API not
found »**, constaté au navigateur le 21/08.

C'est le troisième échec sur cette même piste, sous trois formes différentes,
et c'est ce qui la rend coûteuse : elle a l'air rouvrable à chaque fois.

| Quand | Ce qu'on a vu | Ce qu'on en a conclu |
|---|---|---|
| 18/08 (§20.5) | « Something Went Wrong » sur la page d'éditeur | « piste fermée » |
| 21/08 (§21.9) | introuvable sur le site | « à re-chercher » ← l'erreur |
| 21/08 (§21.16) | `NOT_FOUND — API not found` | **listing supprimé** |

Le raisonnement en sa faveur était pourtant JUSTE, et c'est pour ça qu'il
revenait : un abonnement RapidAPI est un compte SÉPARÉ, et c'est
l'infrastructure de RapidAPI qui appelle API-Sports — ni le refus d'IP ni la
suspension ne s'y appliquent. La parade était bonne ; c'est le guichet qui a
fermé.

⚠️ **Le code de la route RapidAPI reste en place et n'a pas été retiré** —
il est correct, il coûte zéro tant que `SCORES_FOOTBALL_RAPIDAPI_KEY` est vide,
et il servira tel quel si API-Sports reparaît sur une place de marché. Mais les
trois fichiers qui le RECOMMANDAIENT ont été corrigés (`.env.example`,
`_football_error_message`, la docstring d'`ApiFootballScores`) : ils envoyaient
chaque nouvelle session sur cette page. **Une piste morte doit être écrite
morte à l'endroit où on la lit**, pas seulement dans un compte rendu de
session.

**Il ne reste donc que deux voies** pour le §21.9 pt 1 : nouveau compte
API-Sports appelé depuis le pont dès le premier jour, ou cinquième pont vers un
site de scores public.

**4. Le serveur d'ingestion était aveugle sur ses propres refus — corrigé.**
Trouvé en branchant le pont résultats : le userscript n'écrivait qu'une ligne
(`déclenchement manuel`) et le journal de la VM ne montrait **aucune** trace de
`/scores-plan`, pendant que les trois autres ponts y écrivaient en continu.

La cause : `log_message` est muet (choix délibéré, le serveur journalise
lui-même), mais `do_GET` n'appelait `_log` **ni sur ses 401 ni sur ses 404**.
`do_POST` journalisait déjà ses 401 — c'est cette asymétrie qui a coûté le
diagnostic. Résultat, deux causes opposées rendaient le même symptôme :

| Cause | Ce qu'on voyait | Ce qu'il fallait faire |
|---|---|---|
| jeton faux dans Tampermonkey | rien | corriger le userscript |
| requête jamais partie (réseau, pare-feu, Tampermonkey) | rien | regarder le réseau |

⚠️ **Donc « pas de ligne dans `journalctl` » ne prouvait PAS que la requête
n'était pas arrivée.** C'est la panne dominante du projet (§11) retournée
contre l'outil de diagnostic lui-même — le pire endroit où elle puisse être.

Corrigé : les deux routes GET protégées (`/scores-plan`, `/magic-plan`)
journalisent leur 401 avec la route et l'IP appelante — **jamais le jeton
fourni**, le journal part dans systemd et se relit à plusieurs — et les 404
sont journalisés en GET comme en POST, parce que c'est la signature d'un
userscript qui appelle une route mal orthographiée. Six tests d'intégration
(`tests/test_ingest_server_refusals.py`) lancent le vrai serveur sur un port
éphémère et lisent sa sortie ; quatre échouent sur l'ancien code, vérifié.

⚠️ **Déploiement obligatoire pour en profiter** : `git pull &&
sudo systemctl restart betano-ingest`. Sans le redémarrage, le serveur continue
de servir l'ancien code muet — c'est le §19.11.

**5. LA MESURE, faite le 21/08 sur la VM — la couverture n'est plus une
intuition.** `scores_coverage --min-ev 5` sur **10 051 détections football** :

| | |
|---|---|
| Ligues distinctes | **395** |
| Réglées par un catalogue top 5 seul | **12,3 %** (1 240) |
| Hors top 5 | **87,7 %**, sur 351 ligues |
| Ligues pour couvrir 80 % du volume | **152** |
| Ligues pour couvrir 99 % | 337 |

Le §21.9 avait raison, et le chiffre est pire que « des centaines de ligues »
ne le laissait croire. Mais le plus décisif est le PROFIL, pas le compte :

| Catégorie | Part | Ligues |
|---|---|---|
| Coupes | 20,1 % | 66 |
| Autre | 16,4 % | 93 |
| top 5 | 12,3 % | 44 |
| Europe de l'Est | 10,8 % | 42 |
| **Amicaux** | **8,4 %** | **2** |
| Scandinavie | 7,9 % | 26 |
| Amérique du Sud | 7,3 % | 23 |
| Féminin | 6,3 % | 56 |
| D2 + D3 | 10,4 % | 43 |

🔴 **La première ligue du flux est `Club Friendlies`, à 8,1 %** — et les
amicaux sont ce qu'une API de résultats couvre le plus mal, faute de calendrier
officiel. Derrière : Poland 3rd Liga, Finland Kolmonen, Norway 3rd Division,
England Northern Premier League, Guatemala Primera de Ascenso, Brazil Serie C,
Brasileiro Women. Ce sont exactement les compétitions qu'un catalogue laisse
tomber. **Aucune API « grands championnats + quelques autres » ne suffira.**

⚠️ **Le premier dry-run n'a rien mesuré, et il faut savoir pourquoi.**
`results-update --dry-run --days 2` a rendu 32 % — mais avec
`journee_non_pontee=2` dans les compteurs : deux journées entières manquaient,
le palier gratuit d'API-Football ayant posé des pierres tombales sur les 17,
18 et 19/08 (fenêtre de trois jours). Les 32 % mélangeaient donc « la source
n'a pas ce match » et « ce jour n'a jamais été demandé ». Sur le seul jour
réellement ponté, l'ordre de grandeur est **~64 %**. **Ne rien décider, et
surtout ne rien payer, sur un dry-run dont `journee_non_pontee` n'est pas à
zéro** — c'est le §13.12 sous une forme nouvelle : deux causes, un chiffre.

**6. Le compteur de ligues vides a servi dès son premier tour — et a été
amélioré.** La sonde a signalé **12 156 détections sans ligue**, soit PLUS que
les 10 051 comptées. Vérification : elles vont du 21/06 au **01/08**, donc
antérieures à la capture de la ligue (§13) — trou HISTORIQUE, la mesure vaut
pour le flux courant. Mais il a fallu une requête SQL à la main pour le savoir,
alors que « trou historique » et « trou actif » appellent des décisions
opposées et affichaient le même avertissement. La sonde donne désormais
l'intervalle de dates et tranche elle-même :

    ✅ HISTORIQUE  → le tableau décrit le flux courant, décide dessus
    🔴 ACTIF       → la ligue cesse d'être capturée, répare AVANT de décider

**7. `--days` ne peut PAS mesurer une source — `--day` ajouté.** La fenêtre de
`--days` va jusqu'à `maintenant - 2 h`, donc elle contient **toujours la
journée en cours**, dont aucune source n'a le résultat et dont le pont n'a pas
encore déposé le fichier. `journee_non_pontee` ne peut donc jamais retomber à
zéro, et le taux affiché mélange en permanence « la source n'a pas ce match »
et « ce jour n'a pas été demandé ». La sonde d'acceptation était inutilisable
pour ce à quoi elle sert : décider de payer.

    results-update --dry-run --day 2026-08-20 --sport soccer

`--day` borne sur une journée UTC entière et révolue, **sans le rabot de deux
heures** — le rogner perdrait les matchs de fin de soirée, c'est-à-dire
l'Amérique du Sud, 7 % du flux. La fenêtre est désormais dans le titre du
tableau : un taux se relit des jours plus tard, et « 32 % » ne veut rien dire
sans savoir sur quoi il porte. Cinq tests, dont un qui garde le défaut de
`--days` sous surveillance pour qu'on sache qu'il est structurel.

**8. Le catalogue gratuit n'est PAS restreint — mon pronostic était faux.**
Inventaire des 181 matchs du 20/08 réellement reçus : **56 ligues**, et le
contenu tranche. France Ligue 3, Georgia Liga 3, Egypt Second League, Colombia
Primera B, Estonia Esiliiga A et B, Kazakhstan 1. Division, Uzbekistan Pro
League A, Kuwait Division 1, Ecuador Liga Pro Serie B, Tanzania Ligi kuu Bara,
India Calcutta Premier Division, Norway Nasjonal U19, USA MLS Next Pro, NWSL
Women, Colombia Liga Femenina, AFC Women's Champions League, Ukraine U19 —
**et `World - Friendlies`**.

Troisièmes divisions, féminin, jeunes, pays obscurs, amicaux : le palier
gratuit descend très bas. Les 181 matchs étaient un jeudi calme, pas un
catalogue amputé. J'avais prédit qu'API-Football plafonnerait sous les besoins
du projet à cause des 8,4 % d'amicaux — **l'inventaire dit le contraire**, et
il faut le lire avant de payer quoi que ce soit ou de construire un pont.

⚠️ Les noms de ligue diffèrent entre Pinnacle et API-Football (« Poland - 3rd
Liga » contre « Georgia - Liga 3 ») et **ça n'a aucune importance** :
`match_event` apparie sur les NOMS D'ÉQUIPE et l'horaire, jamais sur la ligue.

**9. LA MESURE PROPRE : 68 % sur le 20/08.** Premier dry-run sans
`journee_non_pontee` :

| Sport | À noter | Résolus | % | Sans résultat | Écartés source |
|---|---|---|---|---|---|
| soccer | 95 | 65 | **68 %** | 30 | non_termine=11, score_manquant=0 |

C'est le premier chiffre du projet qui mesure vraiment la couverture d'une
source de résultats football sur notre univers.

⚠️ **68 % est un PLANCHER, pas un plafond.** Les 11 `non_termine` sont des
matchs que la source A, mais dans un état qu'on refuse (reportés, annulés, et
les AET/PEN exclus à dessein — §score_sources). Une partie des 30 manquants est
donc peut-être présente chez la source sans être notable. La couverture réelle
est entre 68 % et ~79 %.

⚠️ **n = 95, un jeudi.** À ±9 points près. À refaire sur un dimanche, quand les
troisièmes divisions et les amicaux jouent — c'est là que le chiffre comptera.

**10. Le taux global ne dit pas si le manque est biaisé — diagnostic ajouté.**
À 68 %, deux situations opposées donnent le même pourcentage : un manque
RÉPARTI (le P&L reste représentatif) ou CONCENTRÉ sur quelques compétitions
(le P&L penche vers ce que la source couvre — précisément ce que le §21.9
redoute pour le §20.4). `--dry-run` affiche désormais la couverture par ligue,
triée par manques, et **nomme les ligues où la source ne résout RIEN** :

    ⚠️ 3 ligue(s) où la source ne résout RIEN — trou de catalogue, pas
       d'appariement : Club Friendlies, Finland - Kolmonen, …

Une ligue à zéro est un trou de CATALOGUE ; une ligue à 8/12 est un
appariement de noms qui glisse. Deux causes, deux remèdes, et elles étaient
indiscernables. Deux tests, dont un qui vérifie l'absence de faux positif
quand tout est couvert.

**11. 🔴 LE VRAI DÉFAUT : tout le football féminin était inappariable.** Le
tableau par ligue, dès son premier tour, a montré ceci sur le 20/08 :

| Notre ligue | Résolus | Ce que le fichier contenait |
|---|---|---|
| AFC - Champions League Women | **0/2** | `World - AFC Women's Champions League`, **12 matchs** |
| Colombia - Liga Women | **0/3** | `Colombia - Liga Femenina`, **4 matchs** |
| USA - National Womens Soccer League | **0/1** | `USA - NWSL Women`, **1 match** |

**La source AVAIT ces matchs.** La cause est la barrière de classe du matcher,
qui est juste et doit rester : `team_similarity` renvoie `0.0` dès que la
classe diffère, sinon un « Portland Thorns » masculin noterait les paris pris
sur le féminin. Mais elle lit la classe dans le NOM D'ÉQUIPE, et les deux
sources ne la mettent pas au même endroit :

| | ligue | équipe |
|---|---|---|
| Pinnacle | `Colombia - Liga Women` | `Deportivo Cali (W)` |
| API-Football | `Colombia - Liga Femenina` | `Deportivo Cali` |

Chez eux l'équipe est un nom de club NU → classée `main` ; la nôtre est
`xwomen` → similarité 0, **aucun match féminin appariable, en silence**. Le
féminin pèse **6,1 % du flux sur 55 ligues**, et les jeunes ont le même défaut
(`Ukraine - U19 League` était dans le fichier).

🔴 **Et le sens du décalage était l'INVERSE de ce que j'avais écrit.** Le
premier correctif est parti du mauvais côté, n'a rien changé, et **seul le
compteur `classe_reportee=0` l'a dit** — le taux, lui, était identique au point
près. Relevé sur les deux côtés, noms réels du 20/08 :

| | ligue | équipe |
|---|---|---|
| API-Football | `NWSL Women` | `Houston Dash W` ✅ **déjà marqué** |
| Pinnacle (nous) | `USA - National Womens Soccer League` | `Houston Dash` ❌ **nu** |

Ce sont NOS noms qui sortent « main », les leurs qui sont `xwomen`. Le
correctif est donc dans `bind_results` : `OurEvent` porte désormais sa
`league`, et la classe en est reprise avant l'appariement. Le report côté
source reste en place comme filet idempotent — si une compétition arrive un
jour avec des équipes nues — mais ce n'est PAS lui qui corrige le féminin.

⚠️ **La leçon, et elle est chère : vérifier LES DEUX CÔTÉS avant de corriger
un appariement.** J'avais relevé la convention d'un seul, supposé l'autre, et
écrit la supposition comme un fait dans une docstring et un commit. Un compteur
l'a rattrapée ; sans lui, le correctif serait passé pour appliqué.

Quatorze tests, dont **trois de non-régression qui comptent plus que le
correctif** : Rosenborg masculin et Rosenborg féminin jouent le même soir — cas
réel du 20/08 — et aucun des deux ne doit prendre le résultat de l'autre.

⚠️ **Et mon propre message de diagnostic était faux.** Il annonçait « trou de
catalogue, pas d'appariement » sur une ligue à zéro. C'est précisément
l'inverse ici. Corrigé : le tableau signale, il ne tranche plus, et donne la
commande qui sépare les deux causes. **Une sonde qui conclut trop vite est pire
qu'une sonde muette** — elle envoie construire un pont dont on n'a pas besoin.

**12. Résultat du correctif : 68 % → 76 %** sur le 20/08, +7 matchs. Colombia
Liga Women, AFC Champions League Women et NWSL disparaissent du tableau des
manques. Restent 23 non résolus sur 95.

**13. Les compteurs d'APPARIEMENT étaient invisibles — affichés maintenant.**
La colonne « Écartés source » ne montre que ce que le FOURNISSEUR a écarté ;
`classe_posee`, `orientation_corrigee` et `resultat_inutilisable` n'étaient
affichés nulle part. Deux se lisent comme des alertes :
`orientation_corrigee` dit que des résultats ont été retournés (§scores, piège
n°2), et `classe_posee` à zéro sur une journée qui compte du féminin dit que la
classe n'est pas reprise. **C'est très exactement l'erreur du 21/08, restée
invisible parce qu'aucun compteur n'était affiché** — et elle a été annoncée à
l'utilisateur comme vérifiable avant qu'on s'aperçoive qu'elle ne l'était pas.

**14. Les 23 restants : le manque est RÉPARTI. La question du biais est
close.** Relevé des deux côtés sur les zéros restants :

| Ligue | Manquants | Cause établie |
|---|---|---|
| Finland - Kolmonen | 4 | **trou de catalogue** — zéro fixture côté source |
| Iceland - 4. Deild | 3 | **trou de catalogue** |
| Estonia Meistriliiga Women / Iceland Urvalsdeild Women | 4 | **trou de catalogue** |
| Argentina - Reserve League | 1 | autre compétition que la `Reserve League` de la source |
| 5 ligues à 1 manquant | 5 | appariements isolés |

Aucune grande catégorie du flux n'est amputée : **le P&L sera représentatif**,
et c'est la réponse à l'avertissement du §21.9. À 76 % réparti, API-Football
suffit pour trancher le §20.4.

⚠️ **L'hypothèse « les réserves sont un défaut de classe » était FAUSSE.**
Relevé : côté source `'Platense Res.'` → `main`, côté nous `'platense'` →
`main`. Les deux sont `main`, la barrière ne joue pas, et
`Argentina - Liga Pro Reserves` (6 matchs) se résout déjà. La règle
`xreserve` n'a PAS été ajoutée aux règles de ligue — et `\bii\b` y aurait fait
passer « Romania - Liga II », une vraie deuxième division, pour du football de
réserve. Deuxième supposition de convention démentie par le relevé en deux
tours : **relever les deux côtés n'est pas une précaution, c'est la méthode.**

**15. 🔴 CHANTIER OUVERT : `events.home` stocke les noms COMPACTÉS de la clé.**
Trouvé dans le même relevé, non corrigé :

    'tampereunitedxreserve' -> classe 'xreserve'
    'bocajuniors', 'colonsantafe', 'estudiantesriocuarto'

`build_event_rows` dérive les noms de `parse_event_key(ek)`, qui rend le nom
NORMALISÉ et compacté, **tag interne `xreserve` compris**. Or l'en-tête de
`scores.py` prescrit explicitement l'inverse — « les noms BRUTS, pas
normalisés : les noms compactés de la clé sont précisément ce qui avait fait
perdre tout le tennis de deux books au §15.4 ». L'intention documentée et le
code se contredisent depuis le début.

Deux conséquences, l'une latente et l'autre diffuse :
- le tag `xreserve` dans le nom rend ces équipes **inappariables dès que la
  source aura la compétition** — ça n'a rien cassé le 20/08 uniquement parce
  que la Kolmonen est absente du catalogue ;
- les noms compactés dégradent l'appariement flou partout, en silence.

Le registre `teams` répare ça À LA LECTURE, sans toucher à ce que le daemon
écrit : il est clé sur la MÊME forme compactée et rend le nom d'origine, tag
compris. Branché dans `results_update`.

⚠️ **MAIS il ne récupère aucun match, et il ne faut pas le croire.** Mesuré sur
douze événements réels du 20/08 : le compactage coûte 2 à 12 points, et
**zéro** bascule sous le seuil de 85 — parce que l'appariement décide sur la
MOYENNE des deux côtés, et qu'un nom faible est toujours rattrapé par son
partenaire. Exemple : « platense » + « colonsantafe » contre « Platense Res. »
+ « Colón Res. » donne 90,0 compacté contre 93,8 brut. Les deux passent.

C'est donc de la MARGE (90 → 100), pas une réparation. La première version de
ce constat annonçait des matchs perdus : elle raisonnait par ÉQUIPE alors que
l'appariement décide par ÉVÉNEMENT.

⚠️ **Et le test qui le « prouvait » passait pour deux mauvaises raisons
cumulées** : `assert "0 %" in output` est vrai quand la sortie dit « 100 % »
(sous-chaîne), et le cache `teams._DISPLAY` est GLOBAL au processus —
`init()` ne le vide pas, donc un test héritait des noms du précédent. Les deux
défauts se compensaient. Corrigés : assertions sur « où la source manque »,
qui n'est pas ambigu, et fixture `autouse` qui vide le registre.

Le correctif de FOND — écrire les noms bruts dans `events` — reste ouvert.
`OddQuote` ne les porte pas (seulement `event_key`, `league`,
`book_event_key`) : il faudrait les remonter du scraper. Priorité faible
maintenant qu'on sait que le coût mesuré est nul.

**Ce qui reste à faire, dans l'ordre** :
1. ~~Mesurer ce que le correctif féminin récupère~~ — fait : **76 %**.
2. Refaire sur un DIMANCHE, quand les troisièmes divisions et les amicaux
   jouent. n=95 un jeudi, c'est ±9 points.
3. **Lire le tableau par ligue, pas le pourcentage.** Manque réparti →
   API-Football suffit, il ne reste qu'à payer la fenêtre de dates. Manque
   concentré → le P&L serait biaisé et la voie 3 reprend le dessus.
4. Pour chaque ligue à zéro qui reste, vérifier dans le fichier avant de
   conclure — le féminin a montré que le zéro ment.

### 21.17 CLV et P&L se contredisent sur les grosses cotes — la meilleure piste

Session du 21/08, après branchement du pont. Premier `pnl_detections` de la
session, sur 3 059 opportunités dédupliquées :

| Population | n | ROI |
|---|---|---|
| Paris JOUÉS (`track-update`) | 169 réglés | +21,36 % (+902 € / 4 225 €) |
| **Toutes détections** | **3 059** | **−0,33 %, −0,1 σ** |

⚠️ **Le +21,36 % ne mesure rien**, et le §1 l'avait déjà établi sur un
effectif quatre fois plus grand : sur 767 paris, +794 € attendus contre
+1 854 € observés, « environ 1 050 € des gains sont de la chance ». À 169
paris réglés, ne rien en conclure.

Le chiffre honnête est **−0,33 %, indiscernable de zéro**. La CLV du même flux
vaut +8,77 %. **C'est le §20.4 posé frontalement.**

**Le motif : les grosses cotes s'effondrent.**

| tranche | n | ROI | σ | gagnés |
|---|---|---|---|---|
| 1,0-1,8 | 784 | +2,57 % | 1,0 | 69,9 % |
| 1,8-2,3 | 624 | +9,22 % | 2,3 | 54,2 % |
| 2,3-3,0 | 566 | −1,41 % | −0,3 | 37,6 % |
| 3,0-4,0 | 449 | +8,93 % | 1,2 | 32,4 % |
| **4,0-6,0** | **406** | **−20,34 %** | **−2,3** | 17,2 % |
| **> 6,0** | **230** | **−16,20 %** | −1,1 | 11,7 % |

C'est la TROISIÈME LECTURE du §20.4 — « un devig qui surestimerait l'outsider
au tennis » — et elle a désormais un signal. Ce qu'un plafond de cote donnerait
sur ces mêmes données :

| plafond | volume gardé | P&L | ROI |
|---|---|---|---|
| aucun | 100 % | −251 € | −0,33 % |
| **4,0** | **79,2 %** | **+2 746 €** | **+4,53 %** |
| 3,0 | 64,5 % | +1 743 € | +3,53 % |
| 2,3 | 46,0 % | +1 942 € | +5,52 % |

🔴 **Pourquoi c'est la meilleure piste de la session, et pourquoi il ne faut
RIEN couper encore.**

Le §9 et le §17.3 ont mesuré que la cote n'est pas un critère — **mais sur la
CLV, pas sur le P&L**. Si la CLV est flatteuse sur les grosses cotes, aucune
analyse fondée sur elle ne pouvait voir ce motif. **C'est le premier endroit du
projet où CLV et P&L se contredisent franchement**, et donc le premier levier
sérieux sur le §20.4.

Trois raisons de ne rien trancher tout de suite :
1. **96 % de tennis** (2 945 / 3 059). Le football pèse 114 détections, ROI
   +0,08 %, 0,0 σ — rien. Ce tableau ne dit rien du flux qu'on vient de
   raccorder.
2. **−2,3 σ sur une tranche parmi six examinées.** Retenir la plus extrême de
   six découpes gonfle mécaniquement la significativité, et le §21.12 vient de
   rappeler ce qu'un p ≈ 0,007 vaut : le §20.8 n'a pas survécu au doublement de
   l'échantillon.
3. Le plafond à 2,3 rend +5,52 % pour 46 % du volume, celui à 4,0 rend
   +4,53 % pour 79 % : **la courbe n'est pas monotone**, ce qui est le signe
   qu'on lit encore beaucoup de bruit.

**Outil ajouté** : `clv_split --by cote`, aux mêmes bornes que
`pnl_detections`. Deux découpages différents rendraient la contradiction
illisible ; identiques, les deux tables se superposent et le §9 (« la cote
n'est pas un critère », mesuré sur la CLV seule) peut enfin se relire en face
du P&L.

**À faire** : laisser le football s'accumuler (le pont capture seul), puis
comparer `clv_split --by cote` et `pnl_detections` tranche par tranche, et
découper par sport. Si le motif tient sur
le football aussi, c'est un plafond de cote ; s'il est propre au tennis, c'est
le devig du tennis qu'il faut regarder — et le §20.4 aura sa réponse.

### 21.9 État des lieux et à faire — remplace §20.15

| | |
|---|---|
| Tests | **845 passés**, 4 ignorés — `pytest tests/`, jamais `pytest` seul (§4) |
| `results` | 3 091 lignes (tennis) |
| Books actifs en ALERTE | unibet, golden_palace, ladbrokes, circus |
| Books muets (donnée seule) | betano, betfirst, napoleon, starcasino, **magicbetting** — **48 %** (§21.8) |
| Comptes joués | Unibet + ses jumeaux Kambi (711, Bingoal, Scooore) déjà exploités |
| Détections 24 h | soccer h2h 382, tennis h2h 157, soccer totals 77, tennis totals 10 (§21.11), **mi-temps 0** (§21.14, normal) |
| Marchés collectés | h2h, totals — plus `h2h_h1`/`totals_h1` **muets** (§21.14). Handicaps toujours jetés (§21.13) |
| Cycle | **34 s** + 20 s de pause = ~54 s entre deux rafraîchissements, contre ~37 s avant les mi-temps |
| Capital | ~3 720 € (relevé du 17/08) |

1. **Remplacer la source de résultats football** — le compte API-Football est
   **réellement suspendu**, constaté sur le tableau de bord le 21/08 et sans
   réponse du fournisseur. ⚠️ Ne pas confondre avec le refus d'IP du 16/08 :
   les deux rendent le MÊME message d'API, et seul le tableau de bord les
   sépare. Le pont navigateur ne contourne que le refus d'IP, il est inopérant
   contre une suspension.

   Trois voies, par coût croissant :
   - **Nouveau compte API-Sports + pont dès le départ** — parseur, appariement
     et pont inchangés, tout le savoir mesuré conservé (filtre `FT`, exclusion
     AET/PEN validée sur 1 215 matchs). ⚠️ Vérifier la **fenêtre de dates** du
     palier gratuit : le backfill vise 60 jours, et c'est ce point, pas le
     quota, qui décidera d'un plan payant. Appeler depuis le pont **dès le
     premier jour**, sinon l'IP de datacenter risque de refaire suspendre le
     compte.
   - **RapidAPI (listing « API-FOOTBALL » d'API-Sports)** — `ApiFootballScores`
     supporte déjà cette route : poser `SCORES_FOOTBALL_RAPIDAPI_KEY` suffit,
     zéro code. 🔴 **RAYÉE le 21/08 : le listing n'existe plus** — la page
     répond « NOT_FOUND — API not found », constaté au navigateur. Troisième
     échec sur cette piste, et le « à re-chercher » écrit ici était justement
     l'invitation à y retourner une quatrième fois. **N'y retourne pas** ; voir
     §21.16 pt 3 pour le détail et pourquoi le code de la route reste en place.
   - **Cinquième pont navigateur vers un site de scores public** — aucune clé,
     aucun quota, aucune limite de date, couverture illimitée. C'est
     l'architecture que le projet maîtrise le mieux (§3, « l'actif technique le
     plus important »). Demande un parseur neuf, mais `match_event` est
     générique et son compteur `sans_candidat` mesure l'alignement des noms
     avant tout engagement.

     ⚠️ **Corrigé le 21/08 : cette voie coûte TROIS fichiers, pas un parseur.**
     Le pont résultats n'est pas agnostique de sa source — la forme
     d'API-Football est codée en dur à trois endroits : `_handle_scores_plan`
     fabrique l'URL `/fixtures?date=…`, `_handle_scores_ingest` exige un champ
     `response` et lit `errors` pour poser sa pierre tombale, et
     `tools/scores-ingest.user.js` fixe `API_BASE`, l'en-tête `x-apisports-key`
     et son `@connect`. Le parti « le userscript ne comprend rien » (§18.6)
     tient toujours, mais c'est le SERVEUR qui porte le fournisseur. Voir
     §21.16.

   ⚠️ **La couverture est le critère qui élimine les API gratuites
   généralistes** : les détections portent sur des centaines de ligues, et une
   source limitée aux grands championnats ne trancherait le §20.4 que sur un
   sous-ensemble non représentatif.

   ➜ **Commence par le chiffrer, la sonde existe depuis le 21/08** :
   `.venv/bin/python -m scripts.scores_coverage --min-ev 5`. Elle dit quelle
   part des détections football un catalogue limité aux cinq grands
   championnats réglerait, et sur combien de ligues se répartit le reste.
   **Ce nombre décide entre les trois voies** — au-dessus de ~80 %, une API à
   catalogue redevient candidate et le pont navigateur est du travail inutile ;
   en dessous, la voie 3 s'impose. Jusqu'ici « des centaines de ligues » était
   une intuition, jamais une mesure (§21.16).

   Ensuite, quelle que soit la voie : `SCORES_FOOTBALL_BRIDGE=1`,
   `SCORES_BRIDGE_DAYS=60`, puis `results-update --days 60 --sport soccer` et
   `track-update`. Reste le premier point du projet : c'est le seul moyen de
   trancher « la CLV mesure-t-elle vraiment l'edge ? ». Ensuite : plan payant
   (pour la fenêtre de dates, PAS le plan proxy), `SCORES_FOOTBALL_BRIDGE=1`,
   `SCORES_BRIDGE_DAYS=60`, puis `results-update --days 60 --sport soccer` et
   `track-update`.
2. ~~**Vérifier la prédiction du §21.6 bis**~~ — fait le 20/08 : **10 détections**
   contre 4-5 prédites, sur des lignes de jeux authentiques. Le correctif du
   §21.2 est en service et rend plus que prévu. Voir §21.11. **Ce qui reste :
   la CLV de ces détections**, seule mesure qui dise si l'edge est réel —
   `.venv/bin/python -m scripts.clv_split --by sport,market --min 20`, une fois assez de
   matchs clôturés.
3. ~~**Capturer les codes de mi-temps des autres softs**~~ — **fait le 21/08,
   et le chantier est CLOS** (§21.14). Circus mappé (4 codes, ~93 700 cotes),
   Betano à moitié (totaux seuls, il n'expose pas de 1X2 de mi-temps),
   MagicBetting n'en propose pas, Unibet abandonné sur mesure. **Ce qui reste :
   la CLV**, quand des matchs auront clôturé —
   `.venv/bin/python -m scripts.check_half_time`. C'est elle qui décidera de
   lever le silence ou de refermer le robinet, sachant que le coût mesuré est
   +46 % de péremption sur TOUT le flux.

   ⚠️ **N'attends pas de détections.** L'EV médiane est de −8,5 % sur les
   totaux de mi-temps et −10,0 % sur les h2h, avec une meilleure valeur
   observée à +0,6 %. Le gisement est réel en volume, nul en valeur aux seuils
   actuels.
4. **Démarrer le relevé public horodaté** (§21.10). Sa valeur est
   proportionnelle à son ancienneté : c'est le seul actif du projet qui ne se
   rattrape pas.
5. ~~**Relever le seuil d'EV sur les `under`**~~ — **annulé le 20/08, mesuré**.
   Le constat du §20.8 ne se reproduit pas sur un échantillon doublé : les
   `under` sont devant les `over`, et l'effet EV→CLV n'a rien de propre aux
   `under`. Voir §21.12. **À la place** : trancher le seuil GLOBAL. La CLV
   attendue n'est que +1,0 % à EV 5 % et +1,9 % à 6 %, contre +6,40 % de
   moyenne — le gros du volume vaut bien moins que la moyenne ne le suggère.
   Arbitrage volume/qualité, non fait.
6. **Vérifier l'alerte softbook de bout en bout** (§20.6) — le test manuel avait
   échoué sur une config Telegram vide, la chaîne de livraison n'est pas prouvée.
7. **Élargir le premium** (§17.4, +39 % de volume) — et cette fois avec un œil
   sur le §21.10 : élargir le premium change ce que tu vendrais.
8. **`PRUNE_DAYS=7`** — ⚠️ **cette ligne date d'AVANT les mi-temps.** Le volume
   de cotes a augmenté d'environ 55 % côté Pinnacle depuis, et la base grossit
   d'autant. À re-vérifier (`df -h ~ && ls -lh data/valuebet.db`) avant
   d'allonger la rétention : le projet a déjà connu le mur des 34 Go avec un
   `VACUUM` impossible (§19.1).

9. **Les handicaps : NE PAS lancer** en l'état (§21.13). Le blocage n'est pas
   la convention des books soft mais notre propre devig — `_group_quotes`
   groupant sur la ligne SIGNÉE, les deux côtés d'un même handicap tombent dans
   deux groupes d'une issue chacun, et rien n'est devigable, y compris chez
   Pinnacle. Il faudrait une clé de groupe canonique au cœur du devig, ET
   mapper les handicaps book par book (aucun soft n'en produit aujourd'hui) —
   tout ça pour un gisement dont la valeur n'a jamais été mesurée. La leçon des
   mi-temps est fraîche : 93 700 cotes, zéro détection.

10. **Petites réparations** (§21.15) : cookie Betano live à recapturer,
    `Gelijkspel` de MagicBetting à élucider.
11. Reliquats : découpage en runs du `pinnacle_doctor`, noms de books dédoublés
   dans `paris_track.csv` (§14.10), dérive de cycles inexpliquée (§19.9 pt 4),
   `line_speed` à repenser (§18.5), handicap de jeux inexploité (§21.7).

### 21.10 Le volet commercial — ce qui est décidé, ce qui bloque

Décision prise : **si le système est vendu, l'argument portera sur le canal
premium seul**, pas sur la sélection globale ni le ROI global. C'est le bon
choix — le premium est le seul sous-ensemble dont le ROI réel a été mesuré
séparément (+6,69 % contre −0,34 % pour le reste, §20.8).

**Le verrou est statistique, et il est chiffré.** À des cotes de 1,5 à 4,
l'écart-type d'un pari unitaire vaut ~100 % de la mise, donc l'erreur-type du
ROI vaut 100 % / √N :

| N premium clôturés | 1σ | +6,69 % vaut |
|---|---|---|
| 100 | ±10,0 % | 0,7σ — indistinguable de zéro |
| 300 | ±5,8 % | 1,2σ |
| **800** | **±3,5 %** | **1,9σ — le minimum annonçable** |
| 1 800 | ±2,4 % | 2,8σ |

Sous ~800 paris premium clôturés, « +6,69 % de ROI » n'est pas un résultat mais
un bruit favorable. La CLV premium (+15 % au tennis, 92 % de positives) est un
argument indépendant et solide, mais ce n'est pas un ROI. Compteur :
`.venv/bin/python -m scripts.pnl_detections --premium`, lire le n et le σ.

⚠️ Deux réserves à ne pas oublier au moment de rédiger quoi que ce soit :
le premium contient du football, dont le P&L n'est pas mesuré (si la preuve est
tennis, l'annonce doit dire tennis) ; et +6,69 % est un ROI sur **mise
notionnelle**, qui suppose la cote disponible à l'instant de l'alerte — un
abonné arrivant 90 secondes plus tard aura une autre cote, et il vaut mieux
l'écrire que se le faire reprocher.

**Ce qui vend vraiment**, ce n'est pas un backtest — n'importe qui en produit un
— mais un relevé public horodaté et non retouchable. Tout existe déjà (alertes
temps réel, `track-update`, `close-lines`) ; il ne manque que la publication au
moment de la détection dans un canal public en lecture seule. À démarrer avant
d'en avoir besoin.

**Le problème structurel :** la valeur du produit tient à la couverture des
books BELGES, donc la clientèle ne peut être que belge — la juridiction la plus
restrictive d'Europe en matière de communication sur les jeux d'argent depuis
l'arrêté royal de 2023. **Question pour un avocat avant le premier euro
encaissé**, pas après : est-ce que cela relève de la Commission des jeux de
hasard, et des restrictions de publicité ? La sortie de ce piège serait
d'étendre le scraper à d'autres pays (NL, FR) — plusieurs mois de travail.

**Nom.** Recommandation retenue : **`equodds.com`**, avec `aequodds.com` en
redirection. Racine latine *aequo* (« équitable ») + odds, et surtout une
orthographe qu'un francophone écrit spontanément après l'avoir entendue —
« équodds » → `equodds`. Le bouche-à-oreille (forums, Telegram) étant le canal
d'acquisition principal, un nom inécrivable coûte des clients qu'on ne compte
jamais ; c'est ce qui disqualifie la graphie savante `aequodds` comme nom
principal, tout en la gardant comme actif défensif.

⚠️ **`purodds` a été écarté, et il ne faut pas y revenir.** Le domaine était
libre, mais **pureodds.io** est un casino crypto actif (jeux « provably fair »,
USDC, sur invitation). Une lettre d'écart, prononciation identique, sens
identique, même classe 41 : risque de confusion manuel. Et surtout, la
contamination d'image est disqualifiante — un produit qui se vend sur la rigueur
statistique ne peut pas se faire confondre avec un casino crypto anonyme, sans
compter la friction bancaire que ce voisinage provoque.

Libres au registre `.com` le 19-20/08 (RDAP Verisign, autoritatif) :
`equodds`, `aequodds`, `aequline`, `aequoline`, `oddsaequo`, `cotejuste`,
`justecote`, `lignejuste`, `cotevraie`, `deuceline`, `truodd`, `vigcut`.

⚠️ **Aucune recherche d'antériorité n'a été faite.** Domaine libre ≠ marque
libre : BOIP (Benelux) et EUIPO restent à vérifier — sur `equodds`, mais aussi
sur `pureodds`, pour savoir si le casino a déposé quelque chose qui couvrirait
la classe visée. Une recherche web n'a remonté aucune marque, ce qui ne prouve
rien.

### 21.7 Reste ouvert

- **Le `max_gap` des middles** est un nombre absolu. Un écart d'un but au
  football et d'un jeu au tennis n'ont pas le même sens ; la formule d'EV, elle,
  reste juste puisque `P_mid` vient des prix Pinnacle eux-mêmes. À rouvrir avec
  des chiffres, pas avant.
- **Le handicap de jeux** (664 prix par relevé) reste inexploité. Le débloquer
  demande que la ligne porte son unité — un changement de modèle, pas un
  correctif de scraper.
