# Carrousel Instagram « 01 — EV expliqué »

Premier épisode de la série pédagogique EQUODDS (Cotes · Fair odds · +EV · CLV).
Format 4:5 (1080 × 1350 px), fond blanc, charte EQUODDS (navy `#0B1F3A`,
bleu `#2563EB`, bleu clair `#38BDF8`, vert `#22C55E` réservé au +EV).

## Contenu

| Fichier | Rôle |
|---|---|
| `slides/ev-01.png` … `ev-10.png` | les 10 slides prêtes à publier, dans l'ordre |
| `slides/sheet.png` | planche contact des 10 slides |
| `legende.md` | légende Instagram + hashtags |
| `carousel.html` | aperçu HTML autonome des 10 slides (polices embarquées) |
| `source/copy.json` | texte et données de chaque slide (la seule chose à éditer) |
| `source/build.js` | génère `carousel.html` à partir de `copy.json` |
| `source/chart.js` | simulation déterministe des parcours de bankroll (slide variance) |
| `source/render_all.js` | capture chaque slide en PNG avec Playwright/Chromium |
| `source/logo.svg` | symbole EQUODDS redessiné en SVG (dégradé bleu) |
| `source/base.css`, `source/slides.css` | système graphique des slides |
| `source/fonts/fonts.css` | Montserrat (titres) et Manrope (texte), embarquées en base64 |

## Régénérer

```bash
cd marketing/carrousels/01-ev-explique/source
npm i -g playwright            # une fois ; Chromium doit être disponible pour Playwright
export NODE_PATH=$(npm root -g)
node build.js copy.json ../carousel.html          # texte → HTML
node render_all.js ../carousel.html ../slides 1   # HTML → PNG (1080 × 1350)
node sheet.js ../slides                           # planche contact
```

Le dernier argument de `render_all.js` est le facteur d’échelle (2 = 2160 × 2700 px).
Le script signale tout élément qui déborde de sa slide.

## Règles de rédaction

- Vouvoiement, ton direct, aucune promesse de gain, aucune incitation à parier
  (contenu éducatif ; cadre légal belge à valider avant diffusion).
- Typographie française appliquée automatiquement par `build.js` : espaces
  insécables avant `; : ! ? %`, dans les guillemets « » et les milliers.
- Chaque chiffre affiché doit rester cohérent avec `src/ev.py` :
  `EV % = (cote × proba juste − 1) × 100`.
