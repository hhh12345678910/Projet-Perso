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
