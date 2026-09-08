#!/usr/bin/env python3
"""Le rapport CLV et ROI, en un fichier qu'on peut lire, imprimer et archiver.

POURQUOI CE SCRIPT PLUTÔT QU'UN COPIER-COLLER
---------------------------------------------
Le PDF du 3 septembre a été fabriqué à la main à partir d'une sortie collée
dans une conversation. C'était faux comme méthode : refaire le même document un
mois plus tard demandait de recoller, de recompter, et de refaire confiance à
un intermédiaire. Une liste de 3 900 lignes ne passe de toute façon pas par un
copier-coller.

Ce script produit le document DEPUIS LA BASE, sur la VM, en une commande.

⚠️ IL NE RECALCULE RIEN. Il appelle `clv_roi_matrix.preparer` et
`clv_roi_matrix._cellule` — les fonctions mêmes qui produisent la sortie texte.
Recopier ici la requête SQL, la porte du canal ou la clé de déduplication
ferait deux outils qui prétendent mesurer la même chose et qui divergeraient au
premier changement (§17.7). Si le tableau HTML et le tableau texte diffèrent un
jour, c'est un bug, pas une variante.

⚠️ IL N'INTERPRÈTE RIEN. Le PDF du 3 septembre portait une section « Lecture »
écrite à la main : des phrases sur ce que les chiffres voulaient dire. Un
script ne peut pas écrire ça honnêtement, et lui en faire produire une
imitation serait pire que rien — une prose générée a l'air d'une analyse. La
section « Ce que les chiffres disent » ne contient donc QUE des énoncés
calculés : effectifs sous seuil, bandes qui franchissent Bonferroni, écart
entre les deux populations. L'interprétation reste au lecteur.

SORTIE : un fichier HTML autonome, sans dépendance, sans réseau. Ouvrir dans un
navigateur, puis Imprimer → Enregistrer en PDF. Les règles `@media print` sont
écrites pour ça : format paysage, pas de coupure au milieu d'un tableau.

Usage :
    .venv/bin/python -m scripts.rapport_clv_roi --premium \\
        --books kambi,ladbrokes_be --depuis 2026-08-01 --jusqu-a 2026-09-08

    .venv/bin/python -m scripts.rapport_clv_roi --premium \\
        --books kambi,ladbrokes_be --depuis 2026-07-01 --axe semaine --lister \\
        --out ~/rapport_semaines.html
"""
from __future__ import annotations

import argparse
import html
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))

from scripts.clv_roi_matrix import (_axe, _cellule, _gains,  # noqa: E402
                                    preparer)
from src.clv import clv_pct  # noqa: E402
from src.clv import pnl as clv_pnl  # noqa: E402
from src.clv import settle as clv_settle  # noqa: E402
from src.config import load_env_file  # noqa: E402

MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre")

# Palette d'ÉTAT, jamais de série : vert « good », rouge « critical ». Les deux
# steps sont ceux du système de conception, pris dans leur variante TEXTE
# (contraste ≥ 4,5 sur fond clair — un chiffre se lit, il ne se survole pas).
#
# ⚠️ LA COULEUR NE PORTE JAMAIS SEULE. Chaque valeur signée porte son signe,
# et chaque cellule sous effectif porte « ⚠️ ». Un lecteur daltonien, une
# impression en noir et blanc, ou un écran mal réglé perdent la couleur — ils
# ne perdent pas l'information.
CSS = """
:root {
  --encre: #0b0b0b; --encre-2: #52514e; --encre-3: #8a8880;
  --fond: #ffffff; --fond-2: #f6f6f4; --trait: #e2e1dc;
  --marine: #1f4e79; --marine-clair: #2a78d6;
  --bon: #006300; --mauvais: #a32020;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--fond); color: var(--encre);
  font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
  "Helvetica Neue", Arial, sans-serif; }
.page { max-width: 1180px; margin: 0 auto; padding: 34px 30px 60px; }
h1 { color: var(--marine); font-size: 27px; margin: 0 0 6px; letter-spacing: -.3px; }
h2 { color: var(--marine); font-size: 19px; margin: 34px 0 10px;
  padding-top: 8px; border-top: 2px solid var(--trait); }
h3 { color: var(--marine); font-size: 15px; margin: 22px 0 6px; }
.sous { color: var(--encre-2); font-size: 12.5px; margin: 0 0 20px; max-width: 90ch; }
.sous b { color: var(--encre); font-weight: 600; }

.kpis { display: flex; flex-wrap: wrap; gap: 1px; background: var(--trait);
  border: 1px solid var(--trait); margin: 0 0 20px; }
.kpi { flex: 1 1 165px; background: var(--fond); padding: 12px 15px; }
.kpi .lib { color: var(--encre-2); font-size: 11px; text-transform: none; }
.kpi .val { color: var(--marine); font-size: 25px; font-weight: 700;
  letter-spacing: -.5px; margin: 2px 0 1px; font-variant-numeric: tabular-nums; }
.kpi .note { color: var(--encre-3); font-size: 11px; }

.encadre { background: var(--fond-2); border-left: 3px solid var(--marine-clair);
  padding: 13px 16px; margin: 16px 0; }
.encadre p { margin: 0 0 7px; font-size: 12.5px; color: var(--encre-2); }
.encadre p:last-child { margin: 0; }
.encadre b { color: var(--encre); }

table { border-collapse: collapse; width: 100%; margin: 8px 0 4px;
  font-variant-numeric: tabular-nums; font-size: 12px; }
th { background: var(--marine); color: #fff; text-align: right; padding: 6px 8px;
  font-weight: 600; white-space: nowrap; }
th:first-child { text-align: left; }
td { padding: 5px 8px; text-align: right; border-bottom: 1px solid var(--trait);
  white-space: nowrap; }
td:first-child { text-align: left; }
tr.total td { font-weight: 700; background: var(--fond-2);
  border-top: 2px solid var(--encre-3); }
tr:hover td { background: #fafaf8; }
.bon { color: var(--bon); } .mauvais { color: var(--mauvais); }
.faible { color: var(--encre-3); }

.liste { font: 11px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.liste .grp { margin: 18px 0 4px; font-weight: 700; color: var(--marine);
  border-bottom: 1px solid var(--trait); padding-bottom: 3px; font-size: 12px; }
.liste table { font-size: 11px; }
.liste td { padding: 2px 7px; border-bottom: 1px solid #f0efec; }
.liste th { background: var(--fond-2); color: var(--encre-2); font-size: 10px;
  font-weight: 600; padding: 3px 7px; border-bottom: 1px solid var(--trait); }

footer { margin-top: 40px; padding-top: 10px; border-top: 1px solid var(--trait);
  color: var(--encre-3); font-size: 11px; }
code { background: var(--fond-2); padding: 1px 4px; border-radius: 3px;
  font-size: 11px; }

@media print {
  @page { size: A4 landscape; margin: 12mm; }
  body { font-size: 10.5px; }
  .page { max-width: none; padding: 0; }
  h2 { break-before: auto; break-after: avoid; }
  table, .encadre, .kpis { break-inside: avoid; }
  tr { break-inside: avoid; }
  .liste table { break-inside: auto; }
  tr:hover td { background: none; }
}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _num(v, suffixe="", dec=2, signe=True) -> str:
    """Une valeur, ou « — ». JAMAIS zéro à la place d'inconnu."""
    if v is None:
        return '<span class="faible">—</span>'
    classe = "bon" if v > 0 else ("mauvais" if v < 0 else "")
    txt = f"{v:+.{dec}f}{suffixe}" if signe else f"{v:.{dec}f}{suffixe}"
    return f'<span class="{classe}">{_e(txt)}</span>' if classe else _e(txt)


def _entier(v) -> str:
    return "—" if v is None else f"{v:,}".replace(",", " ")


COLONNES = [("opp.", "n_opportunites"), ("matchs", "n_matchs"),
            ("joués", "n_joues"), ("n CLV", "n_clv")]


def _ligne_html(libelle: str, c: dict, total: bool = False) -> str:
    """⚠️ Le marqueur d'effectif est un ICÔNE + un titre, pas une couleur.
    Sous 30 paris réglés, l'intervalle de confiance du ROI dépasse l'écart
    qu'on cherche à lire — et c'est vrai que le lecteur voie les couleurs ou
    non."""
    faible = c["n_regles"] < 30
    marque = (' <span title="moins de 30 paris réglés : indice, pas résultat">'
              '⚠️</span>' if faible else "")
    gpn = f"{c['gagnes']} / {c['perdus']} / {c['annules']}"
    return (
        f'<tr class="{"total" if total else ""}">'
        f"<td>{_e(libelle)}{marque}</td>"
        + "".join(f"<td>{_entier(c[k])}</td>" for _lib, k in COLONNES)
        + f"<td>{_num(c['clv_moy_pct'], ' %')}</td>"
        f"<td>{_num(c['sigma_clv'], '', 1)}</td>"
        f"<td>{_num(c['clv_positives_pct'], ' %', 0, signe=False)}</td>"
        f"<td>{_entier(c['n_regles'])}</td>"
        f"<td>{_e(gpn)}</td>"
        f"<td>{_num(c['roi_pct'], ' %')}</td>"
        f"<td>{_num(c['sigma_roi'], '', 1)}</td>"
        f"<td>{_num(c['pnl_eur'], ' €', 0)}</td>"
        "</tr>")


def _table(titre_col: str, lignes: list) -> str:
    entetes = ([titre_col] + [lib for lib, _k in COLONNES]
               + ["CLV", "σCLV", "CLV+", "réglés", "G / P / N", "ROI", "σROI",
                  "P&L"])
    return ("<table><thead><tr>"
            + "".join(f"<th>{_e(h)}</th>" for h in entetes)
            + "</tr></thead><tbody>" + "".join(lignes) + "</tbody></table>")


def _bloc_liste(opp: list, stake: float, bande_de) -> str:
    """Les paris nommés. La seule sortie où l'on peut reconnaître un match."""
    par_bande = defaultdict(list)
    for r in opp:
        par_bande[bande_de(r)].append(r)
    out = ['<h2>Les paris, un par un</h2>',
           '<p class="sous">Statut : ✅ gagné · ❌ perdu · ➖ annulé · ⏳ pas '
           'encore réglé. Ce sont les opportunités <b>dédupliquées</b> — '
           'exactement celles qui ont produit les tableaux ci-dessus, et non '
           'les détections brutes, dix fois plus nombreuses.</p>',
           '<div class="liste">']
    for bande in sorted(par_bande):
        lot = sorted(par_bande[bande],
                     key=lambda r: str(r["detected_at"] or ""), reverse=True)
        gains = _gains(lot, stake)
        mise = stake * len(gains)
        resume = f"{len(lot)} paris"
        if gains:
            resume += (f" · {len(gains)} réglés · ROI "
                       f"{100 * sum(gains) / mise:+.2f} % · "
                       f"P&L {sum(gains):+.0f} €")
        else:
            resume += " · aucun réglé"
        out.append(f'<div class="grp">{_e(bande)} — {_e(resume)}</div>')
        # ⚠️ UN EN-TÊTE PAR GROUPE, ET C'EST UN CHOIX D'IMPRESSION. Deux
        # colonnes de pourcentages se suivent (EV, puis CLV) : sans libellés,
        # une page qui tombe au milieu d'une liste devient indéchiffrable, et
        # c'est justement au milieu d'une liste que les pages tombent.
        out.append(
            "<table><thead><tr>"
            "<th></th><th>date</th><th style='text-align:left'>match</th>"
            "<th style='text-align:left'>marché</th>"
            "<th style='text-align:left'>pari</th><th>cote</th>"
            "<th style='text-align:left'>book</th><th>EV</th><th>CLV</th>"
            "<th>P&amp;L</th></tr></thead><tbody>")
        for r in lot:
            statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                                r["winner"], r["home_score"], r["away_score"])
            pnl = clv_pnl(statut, float(r["odd_taken"]), stake)
            marque = {"won": "✅", "lost": "❌"}.get(
                statut, "⏳" if pnl is None else "➖")
            cl = r["closing_fair_odd"]
            clv = (clv_pct(float(r["odd_taken"]), float(cl)) * 100
                   if cl and float(cl) > 0 else None)
            pari = r["outcome_label"] + (
                f" {r['line']:g}" if r["line"] is not None else "")
            out.append(
                "<tr>"
                f"<td>{marque}</td>"
                f"<td>{_e((r['start_time'] or '')[:10])}</td>"
                f"<td style='text-align:left'>{_e(r['home'])} – {_e(r['away'])}</td>"
                f"<td style='text-align:left'>{_e(r['market'])}</td>"
                f"<td style='text-align:left'>{_e(pari)}</td>"
                f"<td>@ {float(r['odd_taken']):.2f}</td>"
                f"<td style='text-align:left'>{_e(r['book'])}</td>"
                f"<td>{_num(float(r['ev_pct']) if r['ev_pct'] is not None else None, ' %')}</td>"
                f"<td>{_num(clv, ' %')}</td>"
                f"<td>{_num(pnl, ' €')}</td>"
                "</tr>")
        out.append("</tbody></table>")
    out.append("</div>")
    return "".join(out)


def _constats(par_sport: dict, tous: dict) -> list:
    """UNIQUEMENT des énoncés calculés. Aucune interprétation.

    ⚠️ C'est la différence entre ce script et le PDF fait à la main. Une prose
    générée ressemble à une analyse sans en être une ; mieux vaut trois faits
    vérifiables qu'un paragraphe qui a l'air intelligent."""
    out = []
    if tous["n_clv"] and tous["n_regles"]:
        ecart = abs(tous["n_clv"] - tous["n_regles"]) / max(tous["n_clv"],
                                                            tous["n_regles"])
        out.append(
            f"<b>Les deux colonnes ne portent pas sur la même population.</b> "
            f"La CLV exige une clôture capturée, le ROI un résultat de match : "
            f"{_entier(tous['n_clv'])} paris valorisés en CLV contre "
            f"{_entier(tous['n_regles'])} réglés en euros"
            + (f", soit {100 * ecart:.0f} % d'écart — les comparer suppose de "
               f"regarder d'abord si ces deux populations se ressemblent."
               if ecart > 0.15 else
               ". Les deux ordres de grandeur se ressemblent, la comparaison a "
               "donc du sens ici."))
    for sport, c in sorted(par_sport.items()):
        if c["n_opportunites"] >= 20 and c["n_regles"] < c["n_opportunites"] * 0.35:
            out.append(
                f"<b>{_e(sport)} : {c['n_regles']} paris réglés sur "
                f"{c['n_opportunites']} opportunités.</b> Son ROI est mesuré "
                f"sur moins d'un tiers de ce qu'il a produit. Si les matchs "
                f"dont le résultat remonte ne sont pas un tirage au hasard de "
                f"ses matchs, ce ROI décrit un sous-ensemble, pas le sport.")
    faibles = [s for s, c in par_sport.items() if 0 < c["n_regles"] < 30]
    if faibles:
        out.append(
            f"<b>Sous seuil : {_e(', '.join(sorted(faibles)))}.</b> Moins de "
            f"30 paris réglés au total — à cet effectif l'intervalle de "
            f"confiance du ROI dépasse largement l'écart qu'on cherche à lire.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true")
    ap.add_argument("--canal", default=None)
    ap.add_argument("--books", default=None)
    ap.add_argument("--depuis", default=None, metavar="AAAA-MM-JJ")
    ap.add_argument("--jusqu-a", default=None, dest="jusqu_a",
                    metavar="AAAA-MM-JJ")
    ap.add_argument("--jours", type=float, default=0)
    ap.add_argument("--stake", type=float, default=25.0)
    ap.add_argument("--axe", choices=("cote", "delai", "ev", "clv", "semaine"),
                    default="cote")
    ap.add_argument("--lister", action="store_true",
                    help="Ajouter l'annexe des paris nommés.")
    ap.add_argument("--titre", default=None)
    ap.add_argument("--out", default="rapport_clv_roi.html")
    a = ap.parse_args()
    # Ces deux-là n'existent que pour `preparer` ; le rapport ne les expose pas.
    a.porte_sur, a.comparer = "cote", False
    if a.canal:
        a.premium = True
    load_env_file()

    porte, porte_desc, books, rows, fenetre, selectionner = preparer(a)
    opp = list(selectionner(porte).values())
    if not opp:
        raise SystemExit("Aucune opportunité après filtrage : rien à écrire.")

    titre_col, bande_de, ordre = _axe(a.axe)
    par_cle = defaultdict(list)
    for r in opp:
        par_cle[((r["sport"] or "?"), bande_de(r))].append(r)
    ordre = list(ordre) + sorted({k[1] for k in par_cle} - set(ordre))

    sports = sorted({k[0] for k in par_cle})
    par_sport = {s: _cellule([r for r in opp if (r["sport"] or "?") == s],
                             a.stake) for s in sports}
    tous = _cellule(opp, a.stake)

    # ⚠️ Les sports SANS AUCUN pari réglé sortent dans leur propre tableau au
    # lieu de polluer les moyennes avec des colonnes vides. Le PDF du 3/09
    # faisait déjà ce partage, et c'est ce qui a rendu visible que la chaîne
    # de résultats ne couvre que le football et le tennis.
    mesures = [s for s in sports if par_sport[s]["n_regles"] > 0]
    hors = [s for s in sports if par_sport[s]["n_regles"] == 0]

    aujourdhui = datetime.now(timezone.utc)
    date_txt = f"{aujourdhui.day} {MOIS[aujourdhui.month - 1]} {aujourdhui.year}"
    periode = ""
    if a.depuis or a.jusqu_a:
        # « du X au Y inclus », mais « du X à aujourd'hui » : « au aujourd'hui »
        # ne se dit pas, et un rapport qu'on imprime se relit.
        debut = f"du {a.depuis}" if a.depuis else "depuis le début"
        periode = (f"{debut} au {a.jusqu_a} inclus" if a.jusqu_a
                   else f"{debut} à aujourd'hui")
    elif a.jours:
        periode = f"{a.jours:g} derniers jours"
    titre = a.titre or (f"CLV et ROI par {titre_col} et par sport")

    h = [f"<!doctype html><html lang=fr><meta charset=utf-8>",
         f"<title>{_e(titre)}</title><style>{CSS}</style>",
         '<body><div class="page">',
         f"<h1>{_e(titre)}</h1>",
         '<p class="sous">',
         f"Porte : <b>{_e(porte_desc)}</b> · books "
         f"<b>{_e(', '.join(sorted(books)) if books else 'tous')}</b> · "
         f"mise notionnelle constante de <b>{a.stake:g} €</b> · "
         f"<b>{_entier(len(opp))}</b> opportunités dédupliquées sur "
         f"{_entier(len(rows))} lignes"
         + (f" · <b>{_e(periode)}</b>" if periode else "")
         + f" · relevé du {date_txt}</p>"]

    h.append('<div class="kpis">')
    for lib, val, note in (
            ("Opportunités", _entier(tous["n_opportunites"]),
             f"sur {_entier(tous['n_matchs'])} matchs"),
            ("Paris réglés", _entier(tous["n_regles"]),
             f"{tous['gagnes']} gagnés · {tous['perdus']} perdus"),
            ("CLV moyenne", _num(tous["clv_moy_pct"], " %"),
             f"{tous['clv_positives_pct'] or 0:.0f} % positives · "
             f"n = {_entier(tous['n_clv'])}"),
            ("ROI réalisé", _num(tous["roi_pct"], " %"),
             (f"{tous['sigma_roi']:+.1f} sigma" if tous["sigma_roi"]
              else "sigma indisponible")),
            ("P&L notionnel", _num(tous["pnl_eur"], " €", 0),
             f"mise constante de {a.stake:g} €")):
        h.append(f'<div class="kpi"><div class="lib">{_e(lib)}</div>'
                 f'<div class="val">{val}</div>'
                 f'<div class="note">{_e(note)}</div></div>')
    h.append("</div>")

    h.append('<div class="encadre"><p><b>Ce que ce document mesure</b></p>'
             '<p>Le rendement du flux réellement reçu : la porte du canal est '
             'lue dans la configuration en base, pas recopiée, et seuls les '
             'books retenus comptent. Le filtre de books s\'applique '
             '<b>avant</b> la déduplication — c\'est donc « le meilleur prix '
             'parmi mes comptes », et non le meilleur prix du marché.</p>')
    if fenetre and "PAS ENCORE JOUÉS" in fenetre:
        n = fenetre.split("⚠️ ")[1].split(" lignes")[0]
        h.append(f'<p>⚠️ <b>{_e(n)} lignes portent sur des matchs pas encore '
                 f'joués</b> : ni CLV ni résultat. Les colonnes CLV et ROI '
                 f'décrivent les matchs déjà joués, pas la fenêtre entière — '
                 f'et d\'autant moins que le délai de détection est long.</p>')
    h.append("</div>")

    for sport in mesures:
        lignes = []
        for bande in ordre:
            lot = par_cle.get((sport, bande))
            if lot:
                lignes.append(_ligne_html(bande, _cellule(lot, a.stake)))
        if not lignes:
            continue
        lignes.append(_ligne_html("TOTAL", par_sport[sport], total=True))
        h.append(f"<h2>{_e(sport.capitalize())}</h2>")
        h.append(_table(titre_col, lignes))

    h.append("<h2>Tous sports confondus</h2>")
    h.append(_table(titre_col, [_ligne_html("TOUS SPORTS", tous, total=True)]))

    if hors:
        h.append("<h3>Sports hors périmètre de mesure</h3>")
        h.append('<p class="sous">Aucun pari réglé : la chaîne de résultats ne '
                 'les couvre pas. Ils sont listés pour que leur absence soit '
                 '<b>visible plutôt que silencieuse</b>.</p>')
        rs = ["<table><thead><tr><th>Sport</th><th>Opportunités</th>"
              "<th>Matchs</th><th>Joués</th><th>n CLV</th></tr></thead><tbody>"]
        for s in hors:
            c = par_sport[s]
            rs.append(f"<tr><td>{_e(s)}</td><td>{_entier(c['n_opportunites'])}"
                      f"</td><td>{_entier(c['n_matchs'])}</td>"
                      f"<td>{_entier(c['n_joues'])}</td>"
                      f"<td>{_entier(c['n_clv'])}</td></tr>")
        h.append("".join(rs) + "</tbody></table>")

    constats = _constats(par_sport, tous)
    if constats:
        h.append("<h2>Ce que les chiffres disent</h2>")
        h.append('<p class="sous">Uniquement des énoncés <b>calculés</b>. '
                 'L\'interprétation n\'est pas dans ce document : un paragraphe '
                 'produit par un script ressemble à une analyse sans en être '
                 'une.</p><div class="encadre">')
        h.extend(f"<p>{c}</p>" for c in constats)
        h.append("</div>")

    if a.lister:
        h.append(_bloc_liste(opp, a.stake, bande_de))

    cmd = "scripts.rapport_clv_roi " + " ".join(
        x for x in [
            "--premium" if a.premium else "",
            f"--canal {a.canal}" if a.canal else "",
            f"--books {a.books}" if a.books else "",
            f"--depuis {a.depuis}" if a.depuis else "",
            f"--jusqu-a {a.jusqu_a}" if a.jusqu_a else "",
            f"--jours {a.jours:g}" if a.jours else "",
            f"--axe {a.axe}" if a.axe != "cote" else "",
            "--lister" if a.lister else "",
        ] if x)
    h.append(
        "<h2>Méthode</h2>"
        '<p class="sous">Déduplication sur <b>équipes + jour + marché + '
        'pari</b>, meilleure cote gardée — jamais sur <code>event_key</code>, '
        'que le tennis multiplie par onze quand une référence révise un '
        'horaire. La CLV est mesurée contre la clôture <b>dévigée</b>, jamais '
        'contre la cote affichée : la commission médiane de Pinnacle vaut '
        '6,6 %, et l\'ignorer offrirait ce chiffre à tout le monde. Le sigma '
        'est celui du ROI, calculé sur la dispersion réelle des gains.</p>'
        f'<footer>Source : <code>{_e(cmd)}</code> · base '
        f'<code>{_e(a.db)}</code> · généré le {date_txt}. '
        f'Ce document est produit depuis la base par un script : le '
        f'régénérer donne le même résultat.</footer>')
    h.append("</div></body></html>")

    Path(a.out).write_text("".join(h), encoding="utf-8")
    print(f"✓ Rapport écrit : {a.out}")
    print(f"  {len(opp)} opportunités, {tous['n_regles']} réglés, "
          f"ROI {tous['roi_pct']:+.2f} %" if tous["roi_pct"] is not None
          else f"  {len(opp)} opportunités, aucun pari réglé")
    print("  Ouvrir dans un navigateur, puis Imprimer → Enregistrer en PDF "
          "(paysage).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
