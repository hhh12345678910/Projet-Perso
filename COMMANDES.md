# Commandes utiles — aide-mémoire de la VM

Toutes ces commandes ont été **exécutées** avant d'être écrites ici. Le §4 du
HANDOVER le rappelle : *une commande jamais exécutée n'est pas une commande,
c'est une intention.*

```bash
cd ~/Projet-Perso        # toutes les commandes partent de là
```

⚠️ **Toujours `.venv/bin/python`, jamais `python`.** La VM n'a pas de binaire
`python`, et `python3` tournerait hors du venv, donc sans les dépendances.

⚠️ **`sqlite3` est ABSENT de la VM.** Pour interroger la base à la main, passer
par le module Python (exemple tout en bas).

---

## 1. Est-ce que ça tourne ?

**Affiche l'état complet du système et, s'il ne détecte rien, la cause du
zéro.** C'est la première commande à taper quand quelque chose semble anormal :
elle distingue « pas de détection parce que le marché est calme » de « pas de
détection parce qu'un maillon est cassé ».

```bash
./doctor.sh
```

**Affiche si les trois services tournent, depuis quand, et leur dernier
redémarrage.** `valuebet-daemon` scanne, `valuebet-listener` répond aux
commandes Telegram, `betano-ingest` reçoit les quatre ponts navigateur.

```bash
systemctl status valuebet-daemon valuebet-listener betano-ingest --no-pager
```

**Affiche les 30 dernières lignes du serveur d'ingestion** — donc ce que les
ponts ont réellement poussé, et les refus (401 mauvais jeton, 404 mauvaise
route) qui étaient muets avant le 21/08.

```bash
sudo journalctl -u betano-ingest --since "10 min ago" --no-pager | tail -30
```

**Affiche la durée des cinq derniers cycles.** Un cycle normal dure ~34 s plus
20 s de pause. Beaucoup plus long = Pinnacle rame ou la purge tourne.

```bash
grep "done in" valuebet.log | tail -5
```

**Affiche la vitesse des cycles, analysée depuis le journal.** Plus lisible que
le `grep` ci-dessus quand on cherche une dérive sur plusieurs heures.

```bash
.venv/bin/python -m scripts.cycle_speed
```

**Affiche ce que chaque cycle a sauté et pourquoi** — le book absent, le sport
sans calendrier, la référence manquante.

```bash
tac valuebet.log | awk '/══ CYCLE/{c++} c<5' | tac \
  | grep -oiP '\b\w[\w ]*(?= skipped:)' | sort | uniq -c
```

---

## 2. Les ponts navigateur sont-ils frais ?

**Affiche l'âge des fichiers que les ponts déposent, et l'heure UTC.** Les deux
doivent être à moins de 60 s d'écart. Un fichier vieux = onglet fermé.

```bash
ls -l data/circus/ data/prematch/ data/betano.json data/scores/soccer/ && date -u
```

**Affiche ce que le pont résultats a capté, journée par journée.** Une ligne
`200 scores soccer <jour> : N matchs` par journée réussie.

```bash
sudo journalctl -u betano-ingest --since "1 hour ago" --no-pager | grep -i scores
```

**Affiche les journées que le pont réclame encore.** `{"fetch": []}` est le cas
NORMAL — tout est à jour, ce n'est pas une panne.

```bash
curl -s -H "Authorization: Bearer $(grep '^BETANO_INGEST_TOKEN=' .env | cut -d= -f2)" \
  http://127.0.0.1:8787/scores-plan; echo
```

---

## 3. L'argent — ROI, P&L

**Affiche le ROI sur tes paris JOUÉS**, régénère `data/paris_track.csv`, et
donne la CLV réelle du même flux.

```bash
.venv/bin/python -m src.main track-update
```

⚠️ Ce ROI porte sur les paris que tu as cliqués, donc sur une population
choisie à la main et souvent petite. Le §1 l'a mesuré : sur 767 paris,
l'espérance était de +794 € pour +1 854 € observés — **environ 1 050 € des
gains étaient de la chance**. Ne dimensionne rien dessus.

**Affiche le P&L sur TOUTES les détections**, jouées ou non, à mise notionnelle
constante — découpé par tranche de cote, par sport, par marché et par book.
C'est le chiffre sans biais de sélection, celui qui répond au §20.4.

```bash
.venv/bin/python -m scripts.pnl_detections
.venv/bin/python -m scripts.pnl_detections --premium          # ce qui part vraiment
.venv/bin/python -m scripts.pnl_detections --canal "Premium"  # un canal nommé
```

⚠️ `--premium` lit la porte RÉELLE : les canaux configurés en base (§24) s'il
y en a, sinon les seuils de `TelegramConfig`. Il ne recopie plus aucun nombre.
La porte retenue est imprimée avant le tableau — **lis-la avant les chiffres** :

```
PORTE PREMIUM — canal « Premium » lu en base, 2 règle(s)
PORTE PREMIUM — porte historique — standard EV ≥ 8 % cote 1.5–4 ; longue
                EV ≥ 20 % cote 4–6 ; sports exclus de la longue : tennis
```

Si les seuils sont introuvables (ni canal en base, ni `.env` chargé), la
commande **refuse de répondre** au lieu de retomber sur les défauts du code :
un ROI plausible et faux coûte plus cher qu'une erreur franche (§18.3).

Le tableau se découpe aussi **par catégorie de ligue** et **par ligue**, avec
une ligne `⚠️ SANS LIGUE EN BASE` toujours affichée : sans elle, « je n'ai pas
de détections dans ces compétitions » et « je n'ai pas leur ligue en base »
donnent le même tableau rassurant.

```bash
.venv/bin/python -m scripts.pnl_detections --premium --min-ligue 40
.venv/bin/python -m scripts.pnl_detections --premium --top-ligues 40
```

**Remplit `events.league` là où elle est vide**, depuis les fichiers de scores
déjà sur disque — aucun réseau, aucun quota. La ligue ne vient que de Pinnacle
(`main.py:982`) : tout événement du cadre de référence dont il ne nomme pas la
compétition reste sans ligue pour toujours, et aucune analyse par championnat
ne le voit. Sonde par défaut ; `--apply` écrit, sans jamais écraser une ligue
déjà connue.

```bash
.venv/bin/python -m scripts.repair_leagues            # ce qui serait récupéré
.venv/bin/python -m scripts.repair_leagues --apply    # écrit
```

⚠️ Il ne récupère que ce que le rapprochement lie — donc l'essentiel de ce qui
porte un résultat, et rien des événements que `results-update` n'a pas su
apparier. Le féminin et les jeunes sont **sous-récupérés par construction** :
la barrière de classe se règle sur la ligue, celle qu'on cherche justement.

**Affiche la CLV ET le ROI dans la même table**, par sport et par tranche de
cote, aux mêmes bornes que `pnl_detections` — c'est la superposition que le
§21.17 réclame. `--books` accepte l'alias **`kambi`** (Unibet + 711 + Bingoal
+ Scooore, lu dans `reference.KAMBI_BOOKS`), et le filtre s'applique AVANT la
déduplication : le meilleur prix parmi les books que tu joues, pas celui du
marché.

```bash
.venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be
.venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be \
    --out clv_roi.csv
```

⚠️ `n_clv` et `réglés` ne décrivent **pas la même population** : la CLV exige
une clôture capturée, le ROI un résultat. Chaque colonne porte donc son
effectif — comparer leurs moyennes suppose de vérifier d'abord que les deux
se ressemblent.

**Le même tableau par DÉLAI avant le coup d'envoi** — `--axe delai`. Le §16.4
mesurait la CLV par délai mais s'arrêtait à un seul bloc « > 48 h » ; cet axe
le découpe en 48-72, 72-96, 96-120, 120-168 et > 168 h, et pose le ROI en face.
La table tous sports confondus s'imprime **en premier**, parce que c'est la
seule où le ROI garde un effectif lisible dans les bandes lointaines.

```bash
.venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be \
    --axe delai --stake 35
.venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be \
    --axe delai --out clv_roi_delai.csv
```

⚠️ **Le délai est celui de la PREMIÈRE détection, pas de la mise.**
`detected_at` ne bouge jamais (§14.5) : une opportunité vue à 60 h et encore
affichée à 3 h du coup d'envoi compte en 48-72 h. Ces bandes répondent à
« quand le prix est-il apparu », ce qui est bien la question de la CLV, mais
elles ne prouvent pas qu'un pari soit resté plaçable sur toute la bande.

⚠️ Deux bandes hors barème existent pour **se voir** plutôt que d'être réparties
en silence : `< 0 (LIVE)` (détection après le coup d'envoi, §9) et
`? (sans horaire)` (pas de ligne `events`, ou `detected_at` illisible). Sous la
porte premium, qui est prématch, `< 0 (LIVE)` doit rester **vide** : si elle se
remplit, c'est que la porte et le calcul du délai ne datent pas les paris de la
même façon, et le tableau est à relire avant d'en tirer quoi que ce soit.

⚠️ Le délai n'est pas indépendant du reste : les marchés ouverts tôt ne sont
pas les mêmes ligues, ni les mêmes books, ni les mêmes cotes que ceux ouverts
2 h avant. Un écart de ROI entre deux bandes peut n'être qu'un écart de
composition — croiser avec `--axe cote` avant de conclure.

⚠️ `--axe` est **refusé** avec `--comparer` : la comparaison des deux portes
n'affiche que des totaux, et un drapeau ignoré en silence est précisément le
mode de panne de ce projet (§11).

### Les deux σ, et le bloc « chaque bande contre tout le reste »

Le tableau imprime maintenant **`σCLV` et `σROI`**, et non un seul `σ` : la CLV
avait son effectif mais pas sa précision, alors que c'est elle qui décide. Sur
ce portefeuille la CLV est de l'ordre de **8 fois moins bruitée par pari** que
le P&L — c'est le seul des deux instruments capable de séparer deux bandes aux
effectifs disponibles.

Sous le tableau, un bloc teste **chaque bande contre tout le reste**, en t de
Welch, sur les deux mesures, avec le **seuil de Bonferroni** du nombre de
bandes réellement testées (2,81 pour dix bandes) et un `✔` sur celles qui le
franchissent.

⚠️ **Ne compare jamais une cellule à la ligne TOTAL.** Ce test-là est faux deux
fois : le TOTAL *contient* la bande (les échantillons se chevauchent, l'écart-type
de l'écart est sous-estimé), et on le refait dix fois sans corriger le seuil. À
dix comparaisons, **un |t| de 2,3 arrive par pur hasard** sous une vérité
parfaitement plate. Le bloc existe pour rendre ce piège inaccessible.

⚠️ Le bloc teste **tous sports confondus** : sa dernière colonne donne la part
du sport dominant de chaque bande. Une bande à 99 % soccer comparée à un reste
mixte compare aussi deux sports, pas seulement deux délais.

### `closing_gap` — le déficit de CLV loin du match est-il un artefact ?

`--axe delai` a mesuré que **le taux de capture de la clôture dépend du délai** :
sur le football, canal premium, 69,6 % des opportunités détectées à moins de
24 h ont une ligne de clôture, contre 63,2 % au-delà. Si les clôtures
manquantes ne sont pas un échantillon aléatoire, une part du déficit de CLV
des bandes lointaines est un défaut de mesure, pas un fait sur le marché.

```bash
.venv/bin/python -m scripts.closing_gap --premium
.venv/bin/python -m scripts.closing_gap --premium --sport soccer
```

**L'hypothèse testée, avec son mécanisme.** `Storage.closing_group`
(`storage.py:1848`) cherche la cote de clôture avec `WHERE event_key = ?`, une
égalité **exacte**. Or `event_key` vaut `YYYYMMDDHHMM::home__vs__away`
(`matcher.py:190`) : **la minute du coup d'envoi est dans la clé.** La clé d'un
pari est figée à sa détection, celle des cotes capturées près du coup d'envoi
porte l'horaire révisé — si Pinnacle a déplacé l'heure entre les deux,
l'égalité échoue et la clôture est perdue. `Storage._event_key_like`
(`storage.py:1293`) existe précisément pour ça, et `matcher.py:213` admet
**trois heures** de tolérance parce qu'au tennis « un match commence quand le
précédent sur le court se termine » — mais cette clé tolérante n'est utilisée
que dans les six fonctions de déduplication d'alertes, **jamais dans la capture
de clôture**. Et la probabilité qu'un horaire ait bougé croît avec le délai.

**Le témoin est ce qui décide, pas le niveau.** La sonde compare la part
d'horaires déplacés *parmi les paris sans clôture* à la même part *parmi ceux
qui en ont une*. Si les deux sont égales, le déplacement n'explique rien, quel
que soit son niveau absolu. Elle tranche dans les trois sens et le dit.

⚠️ Elle ne voit un déplacement que si le daemon a créé une ligne `events` pour
la nouvelle clé : un horaire déplacé alors que l'événement n'était plus scanné
reste invisible. Le phénomène est donc **sous-estimé, jamais surestimé**.

**Résultat mesuré (03/09/2026) : l'hypothèse est ÉCARTÉE.** Sur le football,
3,82 % d'horaires déplacés parmi les paris sans clôture contre 2,67 % parmi
ceux qui en ont une — z = +1,29. Tous sports, +3,42 points à z = +1,80. Ni
l'un ni l'autre ne se distingue. Câbler `_event_key_like` dans `closing_group`
reste une amélioration, mais ne récupérerait pas le déficit.

⚠️ **Les per-bandes de la table tous sports sont trompeuses** (z de +3 à +10 sous
24 h) : le tennis n'a pas d'heure de début fixe (`matcher.py:213`, trois heures
de tolérance, jusqu'à onze clés par match au §17.8), donc « horaire déplacé »
y mesure surtout la part de tennis de la bande. **La table `--sport soccer` est
la seule lisible pour ce témoin.**

**Deux taux de capture, pas un.** La colonne `snapshot` compte tout
`clv_snapshots.closing = 1` ; la colonne `CLV util.` exige en plus un
`fair_odd` non nul, ce qu'exige `clv_roi_matrix` pour son `n_clv` — une clôture
non déviguée ne produit aucune CLV. Sans cette distinction les deux outils
annonçaient deux taux incomparables (96 % contre 68 % sur le football) sans que
rien ne le signale. `backfill-fair-lines` (`main.py:1770`) comble l'écart sur
l'historique.

**Le bloc `LA CAPTURE PAR MARCHÉ`** testait la dérive de ligne :
`closing_group` apparie aussi sur `market` **et sur `line`**, et la ligne d'un
handicap ou d'un total dérive avec le temps — un pari pris à −0,5 est coté −1,0
au coup d'envoi, et la recherche demande toujours −0,5.

**Résultat mesuré : ÉCARTÉE elle aussi.** `h2h`, qui ne porte **aucune ligne**,
chute de 11,3 points entre le court et le lointain ; `totals`, qui en porte une
sur 100 % de ses paris, chute de 13,5. Une chute quasi identique des deux côtés
ne peut pas venir de la ligne.

### Le test qui répond, sans connaître le mécanisme

Deux mécanismes proposés, deux morts. Mais la question n'a jamais été « pourquoi
les clôtures manquent » — c'est **« leur absence fausse-t-elle la CLV mesurée »**,
et ça se teste directement. Le bloc `LES CLÔTURES MANQUANTES SONT-ELLES UN
ÉCHANTILLON NEUTRE ?` compare, dans chaque bande, les paris qui gardent leur
clôture à ceux qui la perdent, sur les deux observables disponibles **des deux
côtés** : `ev_pct` (l'edge attendu à la détection, soit `(odd_taken / fair_odd
− 1) × 100` — exactement ce que la CLV cherche à confirmer) et la cote.

Un t de Welch par bande, seuil de Bonferroni affiché. Si aucune bande ne le
franchit, les manquants sont neutres sur les deux dimensions qui prédisent la
CLV, et le déficit de capture ne fabrique pas l'écart entre bandes.

⚠️ **Ce n'est pas une preuve d'absence de biais.** Deux paris de même EV peuvent
avoir des CLV différentes, et la CLV des manquants est par construction
inobservable. Deux lots équilibrés rendent seulement un gros biais improbable.

**Compare trois schémas de mise sur la même population** — fixe, quart de Kelly
plafonné, et la règle de ton `.env` (35 € / 45 € au-dessus de `STAKE_EV_TIER`).
Les trois sont reconstruits depuis le code de production, pas recopiés. Sort le
ROI, le drawdown maximal et le capital engagé, plus la courbe d'équité point
par point.

```bash
.venv/bin/python -m scripts.staking_curves --premium --books kambi,ladbrokes_be
.venv/bin/python -m scripts.staking_curves --premium --books kambi,ladbrokes_be \
    --out courbes.csv
```

⚠️ **Le plus gros P&L n'est pas le meilleur schéma.** Le quart de Kelly ne mise
pas forcément plus que la mise fixe : à la bankroll du `.env` (1 250 €) il
engage environ un tiers de MOINS, et le classement des P&L s'inverse
mécaniquement sans qu'aucun edge n'ait bougé. Ce qui se compare, c'est le ROI
(à euro risqué égal) et le drawdown — le P&L brut ne fait que refléter le
capital engagé. La bankroll est tenue FIXE, sans composition, sinon le tableau
mesurerait la composition et non le schéma.

---

## 4. La CLV — le KPI du projet

**Affiche LA mesure de rentabilité du projet** : la CLV globale et sa
significativité. Battre la ligne de clôture prouve l'edge ; gagner sur
50 paris peut n'être que de la chance.

```bash
.venv/bin/python -m src.main clv-report
```

**Affiche la CLV découpée selon les axes demandés**, avec effectif, écart-type
de la moyenne, médiane et taux de CLV positives.

```bash
.venv/bin/python -m scripts.clv_split --by book,sport
.venv/bin/python -m scripts.clv_split --by sport,market --min 30
.venv/bin/python -m scripts.clv_split --by book --since 2026-08-01
```

**Affiche la CLV par tranche de cote**, aux MÊMES bornes que
`pnl_detections`. C'est la table à mettre en face du P&L : le §21.17 a trouvé
que les deux se contredisent sur les grosses cotes — CLV plate, P&L à −20 %
sur 4,0-6,0.

```bash
.venv/bin/python -m scripts.clv_split --by cote --min 20
```

**Affiche la CLV en séparant les books qui ALERTENT de ceux qui sont muets.**
Cinq books sur huit sont en sourdine : sans cet axe, les moyennes décrivent un
flux que personne ne reçoit.

```bash
.venv/bin/python -m scripts.clv_split --by alerte,book
```

**Affiche la CLV par championnat.** Utile pour voir si l'edge vient d'une
famille de ligues ou de tout le flux.

```bash
.venv/bin/python -m src.main features --premium
```

**Affiche si la CLV mesure autre chose que l'EV de départ** — déplacement de la
ligne juste, délai détection → clôture, R² de la CLV sur l'EV. Répond à « le
KPI tient-il debout ? » (§21.12 : R² = 0,257, il tient).

```bash
.venv/bin/python -m scripts.clv_independence
```

---

## 5. Les books — couverture et santé

**Affiche, book par book, le nombre d'événements, l'horizon couvert et
l'intersection avec Pinnacle.** C'est la mesure de couverture réelle.

```bash
.venv/bin/python -m src.main books-coverage --sport soccer,tennis
```

**Affiche si un book est traité comme les autres ou seulement collecté** —
étape par étape, de la cote brute jusqu'à la détection. À sortir quand un book
produit des cotes mais aucune alerte.

```bash
.venv/bin/python -m scripts.book_health circus_be
```

**Affiche la vitesse à laquelle chaque book corrige ses prix** — donc combien
de temps tu as pour cliquer.

```bash
.venv/bin/python -m src.main corrections
```

**Affiche quels books désactivés répondent encore.** Les motifs de désactivation
vieillissent : « compte limité » pour Golden Palace ne concernait que le PARI,
son API ne demandait aucune authentification.

```bash
.venv/bin/python tools/book_revive_check.py
```

**Affiche les détections par book sur une fenêtre donnée.** ⚠️ Restreindre la
fenêtre est indispensable pour juger un book récent : comparer 3 h à 7 jours
donne 13 contre 1 813.

```bash
.venv/bin/python -c 'import sqlite3,sys
q="SELECT book,COUNT(*) FROM value_bets WHERE detected_at>? GROUP BY book ORDER BY 2 DESC"
for r in sqlite3.connect("file:data/valuebet.db?mode=ro",uri=True).execute(q,(sys.argv[1],)):
    print("%-18s %s" % r)' 2026-08-20T00:00:00
```

---

## 6. Quand un marché ne produit rien

**Affiche OÙ s'arrête un marché stérile, étape par étape.** C'est l'outil à
sortir en PREMIER quand une détection manque : il sépare « pas de ligne juste
en face », « book incomplet sur cette ligne » et « EV sous le seuil » — trois
causes qui donnent le même symptôme, rien.

```bash
.venv/bin/python -m scripts.market_supply --help
```

**Affiche ce que les filtres écartent chez Pinnacle**, par période et par type
de marché.

```bash
.venv/bin/python -m scripts.market_expansion
```

**Affiche si une EV énorme est un cadeau du book ou une référence fausse.**

```bash
.venv/bin/python -m scripts.ev_outliers
```

---

## 7. Résultats et P&L football

**Affiche quelle couverture de ligues une source de résultats doit avoir** —
sans clé, sans quota, sans réseau. Mesuré le 21/08 : 395 ligues, dont 12 % en
top 5 seulement.

```bash
.venv/bin/python -m scripts.scores_coverage --min-ev 5
```

**Affiche ce qu'une source résout RÉELLEMENT sur ton univers, sans rien
écrire**, plus le détail par ligue et les ligues où elle ne résout rien.

```bash
.venv/bin/python -m src.main results-update --dry-run --day 2026-08-20 --sport soccer
```

⚠️ **Toujours `--day`, jamais `--days`, pour MESURER.** La fenêtre de `--days`
va jusqu'à `maintenant − 2 h`, donc elle contient toujours la journée en cours,
dont aucune source n'a encore le résultat : le taux mélange alors « la source
n'a pas ce match » et « ce jour n'a pas été demandé ».

**Écrit les résultats en base** (c'est l'étape qui débloque le P&L football).

```bash
.venv/bin/python -m src.main results-update --day 2026-08-20 --sport soccer
```

**Rattrape une fenêtre entière** — n'a de sens qu'avec un abonnement payant, le
palier gratuit ne servant que trois jours autour d'aujourd'hui.

```bash
rm -f data/scores/soccer/*.refused      # ⚠️ sinon les jours refusés ne sont JAMAIS redemandés
sed -i 's|^SCORES_BRIDGE_DAYS=.*|SCORES_BRIDGE_DAYS=60|' .env
sudo systemctl restart betano-ingest
# laisser le pont tourner, puis :
.venv/bin/python -m src.main results-update --days 60 --sport soccer
.venv/bin/python -m src.main track-update
```

---

## 7 bis. Surebets et middles — coupés, et comment les remettre

**Affiche l'état courant des deux.** Surebets coupés le 21/08, middles le
22/08 : ni calculés, ni diffusés. Absent = coupé (c'est le défaut du code).

```bash
grep -E '^SCAN_(SUREBETS|MIDDLES)=' .env || echo "absents -> coupés (défaut)"
```

**Remet les surebets en service** — calcul et diffusion sur les deux canaux.

```bash
grep -q '^SCAN_SUREBETS=' .env && sed -i 's/^SCAN_SUREBETS=.*/SCAN_SUREBETS=1/' .env \
  || echo 'SCAN_SUREBETS=1' >> .env
sudo systemctl restart valuebet-daemon
```

**Remet les middles en service.** Réglage séparé : on peut rallumer l'un sans
l'autre.

```bash
grep -q '^SCAN_MIDDLES=' .env && sed -i 's/^SCAN_MIDDLES=.*/SCAN_MIDDLES=1/' .env \
  || echo 'SCAN_MIDDLES=1' >> .env
sudo systemctl restart valuebet-daemon
```

⚠️ **Le canal des middles est le canal CLV**, partagé avec les alertes de CLV
qu'on garde. Ne jamais le couper pour faire taire les middles — la coupure
passe par le calcul.

**Lance une passe manuelle sans rien réactiver.** Cette commande reste opérante
même à `SCAN_SUREBETS=0` : c'est la façon de vérifier que le système marche
encore avant de le remettre en service.

```bash
.venv/bin/python -m src.main scan-surebets --sport soccer,tennis
```

⚠️ **Ne jamais couper en vidant `TELEGRAM_SUREBET_CHAT_ID`** : un identifiant
vide fait retomber sur le canal PRINCIPAL, donc les surebets iraient le polluer
au lieu de disparaître. Ces identifiants servent aussi la liste blanche du
listener — les effacer couperait ces canaux de `/scan`.

---

## 7 ter. Les alertes arrivent-elles vraiment ?

**Envoie une alerte de test sur chaque canal d'OPPORTUNITÉ** — value bet,
surebet, CLV — en passant par le vrai routage.

```bash
.venv/bin/python -m src.main alert-test
```

**Prouve la chaîne des alertes SYSTÈME** (book muet, Pinnacle muet, marchés en
retard). ⚠️ `alert-test` ne les couvre PAS : c'est pour ça que le §20.6 est
resté non vérifié depuis le 18/08. Cette commande pilote le vrai
`_book_health` avec une horloge avancée, donc c'est le code de production qui
décide et qui envoie.

```bash
.venv/bin/python -m src.main alert-test-system
.venv/bin/python -m src.main alert-test-system --book betano_be   # avec l'indice « onglet »
```

Deux messages doivent arriver : « 🚨 muet » puis « ✅ de retour ». Si rien
n'arrive, le défaut est dans la livraison — jeton, identifiant de canal, ou
bot absent du canal.

---

## 8. Maintenance

**Affiche la place disque et la taille de la base.** Le projet a déjà connu le
mur des 34 Go avec un `VACUUM` devenu impossible (§19.1).

```bash
df -h ~ && ls -lh data/valuebet.db
```

**Purge les vieilles cotes.** ⚠️ Le `VACUUM` est refusé si le disque est
insuffisant — vérifier la place AVANT.

```bash
.venv/bin/python -m src.main prune --days 2
```

**Recalcule les clôtures dévigées** (normalement lancé par un timer systemd).

```bash
.venv/bin/python -m src.main close-lines
```

**Lance les tests.** ⚠️ **TOUJOURS avec le répertoire.** `pytest` seul à la
racine ne rend AUCUN test : `test_alerts.py` n'est pas une suite pytest et fait
mourir la collecte avant qu'elle commence.

```bash
.venv/bin/python -m pytest tests/ -q
```

**Déploie une nouvelle version.**

```bash
git pull && sudo systemctl restart valuebet-daemon
# si bot_listener.py a changé :
sudo systemctl restart valuebet-listener
# si betano_ingest_server.py a changé :
sudo systemctl restart betano-ingest
```

---

## Les 20 sondes, en un coup d'œil

Toutes acceptent `--help` — vérifié par `tests/test_sondes_help.py`, sauf
`cycle_speed` et `magic_probe_report` qui n'ont aucun argument. Aucune ne
modifie quoi que ce soit **sauf `repair_events --apply`**.

```bash
ls scripts/*.py | xargs -n1 basename | sed 's/\.py$//'
.venv/bin/python -m scripts.<nom> --help
```

## Vitesse des cycles

### `book_latency` — où passe le temps du fetch

```bash
.venv/bin/python -m scripts.book_latency
.venv/bin/python -m scripts.book_latency --sport soccer --derniers 200
```

**Le point qui décide de tout** : `fetch_all_parallel` attend `as_completed`
sur TOUS les books d'un sport. Le fetch coûte donc **le book le plus lent**,
jamais la moyenne ni la somme. Accélérer un book qui répond en 2 s quand un
autre en met 15 ne change **rien** à la durée du cycle.

La sonde classe donc les books non par lenteur moyenne — un classement qui
désigne le mauvais coupable — mais par **le temps qu'ils ont réellement fait
perdre** : combien de fois chacun a tenu le chemin critique, et combien de
secondes il a coûté **au-dessus du deuxième**. C'est ce dernier chiffre qui dit
ce qu'un correctif rapporterait.

⚠️ Elle ne mesure que le **fetch**. Un cycle vaut le fetch plus l'analyse, les
écritures en base et les alertes. Elle compare son total aux lignes `Cycle N
done in Xs` et dit explicitement quelle part reste hors fetch — si le gros du
temps est ailleurs, aucun book n'est en cause.

⚠️ Elle a besoin des durées par book, écrites par `fetch_all_parallel` **depuis
le 03/09/2026**. Un journal antérieur, ou un daemon pas encore redémarré sur
cette version, n'en a aucune : la sonde le dit au lieu d'afficher un tableau
vide.

Le format de ces lignes vit dans `orchestration.ligne_book`, partagé par la
production et par le test — et il doit tenir **sous 80 colonnes**, parce que
hors terminal `rich` enveloppe à 80 et couperait la ligne en deux, rendant la
sonde muette sans que le format ait bougé. `tests/test_book_latency.py`
verrouille les deux.

### Le partage du temps hors fetch

`book_latency` sort maintenant un second tableau, `OÙ PASSE LE TEMPS D'UN
SPORT`, qui décompose les 13,6 s que le fetch ne couvrait pas : `fetch`,
`fair` (construction des lignes justes), `base` (les quatre écritures —
`upsert_events` et les trois `insert_quotes_sparse`), `marques` (la
déduplication des alertes), et **`reste`**.

**`reste` est la valeur la plus utile du lot** : c'est le temps qu'aucune phase
ne revendique. Tant qu'il domine, **nommer une phase de plus rapporte davantage
qu'optimiser celles qu'on voit déjà** — la sonde le dit d'elle-même au-delà de
25 %.

⚠️ Comme les durées par book, ces lignes sont écrites depuis le 03/09/2026 et
demandent un daemon redémarré.

⚠️ **La regex qui lisait ces phases perdait les deux seules à majuscule.**
`([a-zéè]+)` lisait `dRest` comme **« est »** — une phase renommée en silence —
et ne trouvait **aucune** paire dans `insVB` — une phase purement disparue.
Corrigé le 04/09 en `([A-Za-zéè_]+)`, exposé en `RE_PAIRE` et **importé** par
le test au lieu d'y être recopié : c'est la recopie qui a caché le bug pendant
toute sa durée de vie (§17.7 — une sonde doit lire la même source que la
production, sinon elle ment). Tout tableau de phases lu avant le 04/09 est
faux sur ces deux lignes-là.

### `alert_cost` — ce que l'envoi des alertes coûte au cycle

```bash
.venv/bin/python -m scripts.alert_cost
.venv/bin/python -m scripts.alert_cost --derniers 20
.venv/bin/python -m scripts.alert_cost --sport soccer
```

**Le problème qu'elle nomme.** `TelegramAlerter._send` réserve un créneau puis
**dort dans le fil du scan** (`_time.sleep`) pour respecter
`min_send_interval_s` — 3,2 s par défaut, la limite de Telegram étant d'environ
20 messages par minute et par groupe. Rien n'est asynchrone : **le cycle est à
l'arrêt pendant ces pauses**, et c'est ce que `dRest` mesure.

**Le modèle exact est un MAXIMUM, pas une somme.** `_next_slot` est tenu **par
chat** : deux canaux ont deux budgets indépendants, et le même pari envoyé à
deux canaux ne coûte pas deux pauses — le second part pendant la pause du
premier. Donc

```
dRest ≈ min_send_interval_s × (messages sur le canal LE PLUS CHARGÉ)
```

C'est ce qui condamne le correctif naïf : **paralléliser les canaux ne
rendrait rien**, ils le sont déjà de fait.

La sonde teste l'hypothèse au lieu de la supposer, et dans cet ordre :

1. **le témoin d'abord** — le `dRest` moyen des cycles qui n'ont envoyé
   **aucune** alerte. S'il est gros, l'hypothèse est morte et le reste du
   tableau ne la sauvera pas ;
2. la **pente** d'une droite passant par l'origine (zéro alerte doit coûter
   zéro seconde — une régression libre absorberait le coût dans l'ordonnée à
   l'origine et le ferait disparaître) et la **corrélation de Pearson** ;
3. **la borne** : un canal reçoit au plus un message par pari, donc
   `dRest / intervalle` ne peut pas dépasser le nombre de paris délivrés.
   Au-delà, les pauses n'expliquent pas le temps.

Le verdict exige les trois. Il en manque une et la sonde refuse de conclure,
en disant laquelle.

⚠️ **La première version de cette sonde était fausse, dans le sens qui compte.**
Elle exigeait une pente d'au moins 0,8 × l'intervalle et aurait donc **rejeté**
l'hypothèse sur le journal qui l'a fait naître : 118,6 s pour 74 paris font
1,60 s par pari, moitié moins que 3,2 s. Le tort était au seuil. Avec un
maximum par chat, **une pente sous l'intervalle est normale** — elle dit qu'un
pari sur deux est dédoublonné sur le canal le plus chargé. Un plancher est le
mauvais test ; la borne supérieure du point 3 est le bon.

⚠️ **`→ N value bet alert(s) sent` compte des PARIS, pas des messages.** C'est
la raison d'être de la borne : on n'observe pas les messages, mais on connaît
leur plafond.

Le dernier bloc compte les incidents Telegram sur **tout** le journal — 429,
envois reportés pour cooldown, réponses non-200. Zéro 429 signifie que le
ralentissement est le **rythme nominal**, pas une tempête de back-off : c'est
la distinction qui décide du correctif.

`tests/test_alert_cost.py` plante quatre journaux — l'hypothèse vraie, le
témoin qui dit non, la borne franchie, et le cas réel à pente 1,6 s qui doit
rester **confirmable** — et vérifie que le verdict bascule dans chacun.
Inverser la condition fait tomber trois tests.

### `UNIBET_PARALLEL_TERMS` — la queue d'Unibet

Unibet interroge **un endpoint par compétition** (`termKey`), jusqu'à 101 par
scan. La boucle était en file : mesuré le 03/09, Unibet tenait le chemin
critique 52 % du temps, médiane 12,1 s mais **p90 à 23,4 s** — le coût vaut
`N × latence`, et une seule requête qui repart en tenacity (3 tentatives, recul
de 1 à 8 s) ajoute son recul à toutes les suivantes.

La boucle est parallélisée, à **6 fils par défaut**, réglable :

```bash
UNIBET_PARALLEL_TERMS=6      # défaut ; 1 revient au comportement série
```

⚠️ **Ne pas monter ce chiffre à la légère.** Kambi limite le débit, et c'est
exactement pour ça que les trois jumeaux (711, Bingoal, Scooore) sont
désactivés dans `fetch_all_parallel`. Unibet est l'un des deux books du canal
premium : lâcher 100 requêtes d'un coup lui ferait courir le risque qui a déjà
coûté les trois autres.

⚠️ L'ordre de fusion est préservé : la déduplication garde le **premier**
exemplaire d'un `event_id`, donc fusionner dans l'ordre d'arrivée des threads
changerait quel exemplaire gagne — un changement de données non déterministe,
déguisé en optimisation, et invisible puisque le NOMBRE d'événements ne bouge
pas. `tests/test_unibet_parallele.py` le verrouille avec des retards qui
inversent délibérément l'ordre d'arrivée.

## Alerte « cycles ralentis »

**La lenteur n'avait aucun capteur.** `_pinnacle_health` et `_book_health`
surveillent tous deux l'**absence** de données — « ce book a-t-il répondu ? ».
Pendant un gel de trois minutes, chaque book finit par répondre, en retard :
aucun n'est « muet », et rien ne se déclenchait. Mesuré sur 10 192 cycles
(5,7 jours) : **16 gels, 2 126 s perdues, 6,3 minutes de cécité par jour**,
sans détection **ni** capture de clôture — et pas une alerte.

| réglage | défaut | pourquoi |
|---|---|---|
| `CYCLE_SLOW_SEC` | `90` | cycle normal : 28 s de médiane, 31 s de p90, 43 s au pire hors gel. 90 s = 3,2× la médiane et 2,1× le pire cas normal |
| `CYCLE_SLOW_CYCLES` | `2` | un cycle isolé arrive ; deux d'affilée décrivent un état. 2 × 90 s = 180 s, le gel qui a motivé l'alerte |
| `CYCLE_SLOW_QUIET_UTC` | `03:45-05:30` | la purge (04:00 UTC, §18.4) fait 73 % des gels — 04 h et 05 h réunies |

⚠️ **Le compteur avance PENDANT la fenêtre de silence ; seul l'envoi attend.**
Une panne réelle commencée à 04:30 et durant jusqu'à 06:00 alerte dès la sortie
de fenêtre, avec les cycles de la purge comptés dedans. Si la fenêtre arrêtait
le compteur, la purge effacerait exactement les pannes qu'on veut voir.
`tests/test_cycle_health.py` le verrouille — et la variante dangereuse fait
tomber ce test.

⚠️ **Le créneau de 07 h n'est PAS silencé** : 5 gels, 412 s, 19 % du total, et
il reste inexpliqué. C'est précisément ce qu'on veut voir arriver.

⚠️ Un `CYCLE_SLOW_QUIET_UTC` illisible ne silence **rien** et le dit. Le défaut
sûr d'une alerte est de parler, pas de se taire.

## Deux nouveaux axes et une fenêtre temporelle

```bash
--jours 7        # ne garder que les détections des 7 derniers jours
--axe ev         # découper par tranche d'EV DÉTECTÉE
--axe clv        # découper par tranche de CLV RÉALISÉE
```

`--jours` existe sur `clv_roi_matrix` **et** sur `closing_gap`, avec le même
découpage, pour que « taux de capture de la semaine » et « CLV de la semaine »
portent sur la même population.

⚠️ Le filtre porte sur `detected_at`, qui **ne bouge jamais** (§14.5) : une
opportunité vue il y a dix jours et encore affichée hier est **hors** d'une
fenêtre de sept jours. La fenêtre découpe *quand le prix est apparu*.

⚠️ Il s'applique **avant** la déduplication. Filtrer après elle pourrait retenir
un exemplaire hors fenêtre puis le jeter alors qu'un exemplaire dedans
existait — l'opportunité disparaîtrait sans raison.

`--axe ev` réutilise `_ev_bucket` de `main.py`, celui-là même dont se sert
`clv-report` : recopier ses bornes ferait diverger deux outils qui prétendent
découper la même chose (§17.7).

`--axe clv` répond à **la** question du projet : une CLV élevée annonce-t-elle
un ROI élevé ? C'est la validation du KPI lui-même.

⚠️ **La bande `sans clôture` n'est pas un déchet, c'est la moitié de la
question.** Environ un tiers des paris n'a pas de clôture capturée : leur ROI
compte, leur CLV est inconnue. Les jeter ferait lire « le ROI par tranche de
CLV » sur la sous-population dont on a réussi à mesurer la CLV — une sélection,
pas un échantillon. Elle est donc imprimée avec les autres.

## MeridianBet — débloqué le 04/09

**Le blocage n'était pas TrafficGuard.** L'API répondait `401 invalid_token` :
un refus d'**authentification**, pas un filtrage d'ASN. L'IP de la VM passe
très bien — c'est l'en-tête `Authorization: Bearer` qui manquait.

**Le jeton se prend par la porte d'entrée.** Chaque page HTML du site embarque
un jeton NEUF dans son `<script id="ng-state">`, sous `NEW_TOKEN`. Un `GET`
anonyme suffit, sans navigateur, sans pont, sans compte. C'est un jeton invité
(`scope: ["GENERAL"]`, `permissions: []`), bon pour lire l'offre publique.

| réglage | défaut | rôle |
|---|---|---|
| `MERIDIAN_TOKEN_URL` | `https://meridiansports.be/en/betting/football/` | la page où prendre le jeton |
| `MERIDIAN_TOKEN_MARGIN_SEC` | `300` | marge avant expiration |
| `MERIDIAN_TIME_FILTER` | `ALL` | fenêtre de l'offre |
| `BOOKS_DISABLED=meridianbet` | — | **coupe-circuit** |

⚠️ `NEW_TOKEN` est du **JSON encodé dans une chaîne**. Un `.get()` direct rend
la chaîne entière, l'en-tête part invalide, l'API répond 401 et le book paraît
cassé alors que le jeton était là.

⚠️ `overUnder` et `handicap` sont portés par le **groupe**, jamais par la
sélection. Les chercher sur la sélection rendrait des totaux **sans ligne**,
inutilisables pour l'appariement.

⚠️ L'`Origin` s'envoie **sans le `www.`** — c'est ce que le navigateur fait, et
une origine qui ne correspond pas est ce qu'un anti-bot vérifie en premier.

**Le coût** : une page de ~1,5 Mo par heure pour renouveler le jeton, partagée
entre tous les sports par un verrou. Négligeable.

**Repli** : si l'API se ferme, le même `ng-state` contient l'offre elle-même
(`matches-today-58-leagues`), lisible **sans aucun jeton**. Aucun autre book du
portefeuille n'a deux voies indépendantes.

**À vérifier après activation** : `scripts/book_health.py meridianbet` (le book
traverse-t-il toute la chaîne ?) et `scripts.book_latency --derniers 40` (est-il
devenu le chemin critique du cycle ?).
