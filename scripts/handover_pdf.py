#!/usr/bin/env python3
"""HANDOVER.md → PDF lisible, avec sommaire et sans nombres coupés.

POURQUOI UN SCRIPT ET PAS UNE COMMANDE
--------------------------------------
Le HANDOVER fait plus de 7 000 lignes et 26 sections. Un `pandoc` nu en sort
un mur sans repères, et surtout il RECOUPE les nombres : la version du 03/09
affichait « 25 758 » avec le 25 sur une ligne et le 758 sur la suivante, ce
qui se lit comme deux nombres. Le correctif est ici et pas dans le Markdown,
parce que le Markdown doit rester lisible en texte brut.

CE QUE CE SCRIPT GARANTIT
-------------------------
1. **AUCUN NOMBRE COUPÉ.** Les espaces de millier deviennent des espaces fines
   insécables (U+202F), et les unités collées à leur nombre (« 25 € », « +2,11
   pt », « n = 951 ») passent en espace insécable. Un nombre coupé en deux
   dans un document de décision est une erreur de lecture, pas un défaut
   d'esthétique.
2. **AUCUN TABLEAU DÉBORDANT.** Les tableaux du HANDOVER portent les chiffres
   qui décident ; un tableau tronqué à droite perd silencieusement une
   colonne. Ils sont mis à l'échelle, jamais rognés.
3. **LES BANNIÈRES D'AVERTISSEMENT SE VOIENT.** Une section dont la conclusion
   a été amendée porte un bloc « ⚠️ » en tête. Si ce bloc se fond dans le
   corps du texte, le PDF diffuse une conclusion périmée — exactement ce que
   la bannière existe pour empêcher.

Usage :
    python3 -m scripts.handover_pdf                  # → HANDOVER.pdf
    python3 -m scripts.handover_pdf --out /tmp/x.pdf
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent

FINE = " "       # espace fine insécable — sépare sans permettre la coupe
INSEC = " "      # espace insécable ordinaire

# Les binaires possibles, dans l'ordre de préférence. Le conteneur d'analyse
# n'a que `chromium` ; une machine de bureau peut avoir l'un des autres.
NAVIGATEURS = ("chromium", "chromium-browser", "google-chrome",
               "google-chrome-stable", "/opt/pw-browsers/chromium/chrome-linux/chrome")


def _chercher_navigateur() -> str:
    for nom in NAVIGATEURS:
        chemin = shutil.which(nom) or (nom if Path(nom).is_file() else None)
        if chemin:
            return chemin
    # Playwright installe le binaire sous un nom versionné ; on le retrouve
    # plutôt que d'exiger que l'appelant le connaisse.
    for base in (Path("/opt/pw-browsers"), Path.home() / ".cache/ms-playwright"):
        if base.is_dir():
            for c in sorted(base.glob("chromium*/chrome-linux/chrome")):
                return str(c)
    raise SystemExit(
        "Aucun navigateur pour rendre le PDF. Installer chromium, ou passer\n"
        "  --navigateur /chemin/vers/chrome.")


# ── Les nombres qu'il ne faut pas couper ─────────────────────────────

# Un groupe de milliers : « 25 758 », « 1 180 », « 42 122 ». On ne touche QUE
# les espaces encadrées de chiffres — « 5 sports » garde son espace normale.
_MILLIERS = re.compile(r"(?<=\d) (?=\d{3}\b)")

# Une valeur et son unité. La liste est explicite plutôt que générique : un
# « 15 minutes » coupé se relit, un « 2,11 pt » coupé se lit comme autre chose.
_UNITES = re.compile(
    r"(?<=[\d,]) (?=(?:%|€|pt\b|ptn?s\b|h\b|min\b|s\b|j\b|×|x\b))")

# « n = 951 », « t = 4,12 », « z = +11,1 » : trois jetons qui ne valent que
# lus ensemble.
_RELATION = re.compile(r"\b([ntzσp]|CLV|ROI|EV)\s*([=<>≥≤])\s*([+\-−]?[\d,.]+)")


def _insecables(texte: str) -> str:
    texte = _MILLIERS.sub(FINE, texte)
    texte = _UNITES.sub(INSEC, texte)
    texte = _RELATION.sub(lambda m: f"{m.group(1)}{INSEC}{m.group(2)}"
                                    f"{INSEC}{m.group(3)}", texte)
    return texte


# ⚠️ UNE CLÔTURE DE BLOC DOIT ÊTRE EN DÉBUT DE LIGNE. Le HANDOVER contient
# 104 suites de trois accents graves, dont 10 AU MILIEU d'une ligne (citées
# dans du texte). Un motif qui les apparie toutes ferait fermer un vrai bloc
# par un accent cité, et tout le texte jusqu'au bloc suivant ressortirait
# intact, nombres coupés compris.
#
# Honnêteté sur ce garde : vérifié le 12/09, il ne change RIEN au rendu
# actuel — les dix accents cités tombent tous à des endroits où l'appariement
# naïf retombait par chance sur ses pieds. Il est là parce que la prochaine
# édition du document n'aura pas cette chance, pas parce qu'il a corrigé un
# symptôme observé. Le « 2 612 » du sommaire venait d'ailleurs (voir
# `_titre_propre`).
_FENCES = re.compile(r"(^```[\s\S]*?^```|`[^`\n]+`)", re.M)


def _proteger_hors_code(md: str) -> str:
    """Appliquer les insécables PARTOUT SAUF dans les blocs de code.

    Un ` 1 000` dans une commande shell doit rester une espace ordinaire :
    copier-coller une espace fine dans un terminal donne une commande qui
    échoue avec un message incompréhensible. C'est déjà arrivé dans ce projet
    avec un bloc `.env` recopié depuis une réponse."""
    morceaux = _FENCES.split(md)
    return "".join(m if i % 2 else _insecables(m)
                   for i, m in enumerate(morceaux))


# ── Le rendu ─────────────────────────────────────────────────────────

CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm; }
:root { --encre:#15181d; --doux:#5a6472; --trait:#d7dce3; --fond:#fff;
        --alerte:#b4451a; --alerte-fond:#fdf4ee; --code-fond:#f5f7fa; }
* { box-sizing: border-box; }
body { font-family: "DejaVu Sans", "Helvetica Neue", Arial, sans-serif;
       font-size: 9.4pt; line-height: 1.52; color: var(--encre);
       background: var(--fond); margin: 0; }
h1, h2, h3, h4 { line-height: 1.25; font-weight: 700; margin: 1.35em 0 .5em;
                 break-after: avoid; page-break-after: avoid; }
h1 { font-size: 20pt; letter-spacing: -.01em; }
h2 { font-size: 14pt; border-bottom: 2px solid var(--encre);
     padding-bottom: .22em; margin-top: 0; break-before: page;
     page-break-before: always; }
h3 { font-size: 11.4pt; color: #0f1319; }
h4 { font-size: 9.8pt; color: var(--doux); text-transform: uppercase;
     letter-spacing: .05em; }
p, li { orphans: 3; widows: 3; }
p { margin: .5em 0; }
ul, ol { margin: .5em 0 .5em 1.15em; padding-left: .5em; }
li { margin: .22em 0; }
a { color: inherit; text-decoration: none; }
code { font-family: "DejaVu Sans Mono", Menlo, monospace; font-size: .88em;
       background: var(--code-fond); padding: .08em .3em; border-radius: 3px;
       overflow-wrap: anywhere; }
pre { background: var(--code-fond); border: 1px solid var(--trait);
      border-radius: 5px; padding: .6em .75em; overflow: visible;
      break-inside: avoid; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: .82em; line-height: 1.42;
           white-space: pre-wrap; overflow-wrap: anywhere; }

/* ⚠️ LES TABLEAUX PORTENT LES CHIFFRES QUI DÉCIDENT. Ils ne sont jamais
   rognés à droite : `table-layout:auto` + `word-break` les fait tenir. */
table { border-collapse: collapse; width: 100%; max-width: 100%;
        table-layout: auto; margin: .75em 0;
        font-size: 8.4pt; break-inside: avoid; page-break-inside: avoid; }
/* ⚠️ `word-break: break-word` COUPE LES MOTS N'IMPORTE OÙ. Dans une colonne
   étroite il écrivait « book » verticalement, une lettre par ligne.
   `overflow-wrap: anywhere` ne coupe QUE si le mot ne tient pas seul. */
th, td { border: 1px solid var(--trait); padding: .3em .45em;
         text-align: left; vertical-align: top;
         word-break: normal; overflow-wrap: anywhere; hyphens: none; }
th { background: #eef1f5; font-weight: 700; }
/* ⚠️ PAS DE `nowrap` ICI. Il forçait chaque colonne à sa largeur naturelle,
   poussait le tableau hors de la page et faisait DISPARAÎTRE la dernière
   colonne — sur un document dont les tableaux portent les chiffres qui
   décident. Ce sont les espaces insécables posées en amont qui empêchent un
   nombre d'être coupé, pas la mise en page. */
td:not(:first-child), th:not(:first-child) { text-align: right; }
/* La première colonne porte les noms de fichiers et de books. Trop étroite,
   `overflow-wrap` y coupait `pinnacle.py` en « pinnacle.p / y ». */
td:first-child, th:first-child { min-width: 9.5em; }
td code, th code { padding: .05em .15em; }
tr:nth-child(even) td { background: #fafbfc; }

/* ⚠️ UNE BANNIÈRE D'AVERTISSEMENT QUI SE FOND DANS LE TEXTE DIFFUSE UNE
   CONCLUSION PÉRIMÉE. Elle doit se voir de loin, page tournée. */
blockquote { margin: .8em 0; padding: .55em .8em; border-left: 3px solid var(--alerte);
             background: var(--alerte-fond); color: #3a2a20;
             break-inside: avoid; page-break-inside: avoid; }
blockquote p { margin: .25em 0; }
hr { border: 0; border-top: 1px solid var(--trait); margin: 1.4em 0; }

.garde { break-after: page; page-break-after: always; padding-top: 22mm; }
.garde h1 { font-size: 30pt; margin: 0 0 .15em; border: 0; }
.garde .sous { font-size: 12pt; color: var(--doux); margin: 0 0 2.2em; }
.garde .meta { font-size: 9pt; color: var(--doux); border-top: 1px solid var(--trait);
               padding-top: .8em; }
.sommaire { break-after: page; page-break-after: always; }
.sommaire h2 { break-before: avoid; page-break-before: avoid; }
.sommaire ul { list-style: none; margin-left: 0; padding-left: 0;
               column-count: 2; column-gap: 10mm; }
.sommaire li { font-size: 8.6pt; margin: .12em 0; break-inside: avoid; }
.sommaire li.n2 { font-weight: 700; margin-top: .5em; break-before: avoid; }
.sommaire li.n3 { padding-left: .9em; color: var(--doux); }
"""


def construire_html(md: str, titre: str, meta: str) -> str:
    import markdown as md_lib

    # ⚠️ PAS de `nl2br`. Le HANDOVER est replié à 80 colonnes pour être lisible
    # en texte brut ; garder ces retours à la ligne en PDF donnerait des
    # paragraphes hachés au hasard de la largeur d'un terminal. Le Markdown
    # recompose, c'est précisément son rôle.
    conv = md_lib.Markdown(extensions=["tables", "fenced_code", "toc",
                                       "sane_lists", "attr_list"],
                           extension_configs={"toc": {"toc_depth": "2-3"}})
    corps = conv.convert(_proteger_hors_code(md))

    # Le sommaire est reconstruit à la main : `toc` de `markdown` rend une
    # arborescence imbriquée dont la mise en colonnes casse. Une liste plate
    # étiquetée par niveau se répartit proprement.
    items = _sommaire_plat(conv.toc_tokens)

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>{html.escape(titre)}</title><style>{CSS}</style></head><body>
<section class="garde">
  <h1>{html.escape(titre)}</h1>
  <p class="sous">Valuebet — état du système, décisions, et ce qui reste ouvert</p>
  <p class="meta">{meta}</p>
</section>
<section class="sommaire"><h2>Sommaire</h2><ul>{items}</ul></section>
{corps}
</body></html>"""


_BALISE = re.compile(r"<[^>]+>")


def _titre_propre(nom: str) -> str:
    """Le titre d'une entrée de sommaire, échappé UNE fois et non deux.

    ⚠️ DEUX PIÈGES, TOUS DEUX OBSERVÉS SUR LE VRAI DOCUMENT.

    1. Le titre arrive déjà rendu en HTML, donc déjà échappé. Le réechapper
       affichait « P&amp;L réel » en toutes lettres dans le sommaire — sur un
       document dont la moitié des sections parlent de P&L.

    2. `markdown.extensions.toc` NORMALISE LES BLANCS du nom qu'il rend :
       l'espace fine insécable posée en amont y redevient une espace
       ordinaire, et « 2 612 » repartait coupable dans le sommaire alors que
       le corps du texte était correct. On repose donc les insécables ICI, sur
       le titre seul — c'est du texte, jamais du code, la question du §
       « ne pas toucher aux blocs shell » ne se pose pas.
    """
    return _insecables(html.escape(html.unescape(_BALISE.sub("", nom))))


def _sommaire_plat(jetons, niveau: int = 2) -> str:
    out = []
    for t in jetons:
        out.append(f'<li class="n{niveau}"><a href="#{t["id"]}">'
                   f'{_titre_propre(t["name"])}</a></li>')
        if t.get("children"):
            out.append(_sommaire_plat(t["children"], niveau + 1))
    return "".join(out)


def rendre(html_txt: str, sortie: Path, navigateur: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "handover.html"
        src.write_text(html_txt, encoding="utf-8")
        cmd = [navigateur, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
               "--virtual-time-budget=20000", "--export-tagged-pdf",
               f"--print-to-pdf={sortie}", src.as_uri()]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if not sortie.exists() or sortie.stat().st_size == 0:
            raise SystemExit(f"Le rendu a échoué :\n{r.stderr[-2000:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=str(RACINE / "HANDOVER.md"))
    ap.add_argument("--out", default=str(RACINE / "HANDOVER.pdf"))
    ap.add_argument("--titre", default="HANDOVER")
    ap.add_argument("--navigateur", default=None)
    a = ap.parse_args()

    src = Path(a.source)
    if not src.is_file():
        raise SystemExit(f"Introuvable : {src}")
    md = src.read_text(encoding="utf-8")

    rev = subprocess.run(["git", "-C", str(RACINE), "log", "-1",
                          "--format=%h · %ad", "--date=format:%d/%m/%Y"],
                         capture_output=True, text=True)
    meta = (f"{len(md.splitlines()):,}".replace(",", FINE) + " lignes"
            + (f"  ·  révision {rev.stdout.strip()}" if rev.returncode == 0 else ""))

    sortie = Path(a.out)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    rendre(construire_html(md, a.titre, meta),
           sortie, a.navigateur or _chercher_navigateur())
    print(f"{sortie}  ({sortie.stat().st_size / 1e6:.1f} Mo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
