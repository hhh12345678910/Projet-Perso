from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.matcher import (
    event_key,
    match_event,
    normalize_team,
    parse_event_key,
    reconcile_event_keys,
    team_similarity,
    tolerance_for,
)
from src.models import Book, Event


def _ev(home: str, away: str, ts: datetime, book: Book = Book.PINNACLE) -> Event:
    return Event(
        sport="soccer",
        league="Jupiler Pro League",
        home=home,
        away=away,
        start_time=ts,
        source_id="x",
        book=book,
    )


def test_normalize_team_removes_noise_and_accents():
    assert normalize_team("Real Madrid CF") == "real madrid"
    assert normalize_team("Standard de Liège") == "standard liege"
    assert normalize_team("RSC Anderlecht") == "anderlecht"
    assert normalize_team("KV Mechelen") == "mechelen"
    assert normalize_team("R. Charleroi SC") == "charleroi"


def test_team_similarity_handles_variants():
    assert team_similarity("Standard Liège", "Standard de Liege") > 90
    assert team_similarity("Club Brugge KV", "Club Brugge") > 90


def test_match_event_finds_within_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    a = _ev("Standard Liège", "Anderlecht", t)
    b = _ev("Standard de Liege", "RSC Anderlecht", t + timedelta(minutes=3), book=Book.UNIBET_BE)
    assert match_event(a, [b]) is b


def test_match_event_rejects_outside_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    a = _ev("Standard Liège", "Anderlecht", t)
    b = _ev("Standard Liège", "Anderlecht", t + timedelta(hours=3), book=Book.UNIBET_BE)
    assert match_event(a, [b]) is None


def test_match_event_handles_home_away_swap():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    a = _ev("Anderlecht", "Club Brugge", t)
    b = _ev("Club Brugge KV", "RSC Anderlecht", t, book=Book.UNIBET_BE)
    assert match_event(a, [b]) is b


def test_event_key_is_deterministic_and_normalised():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    k1 = event_key("Standard Liège", "RSC Anderlecht", t)
    k2 = event_key("standard de liege", "Anderlecht", t)
    assert k1 == k2


def test_parse_event_key_roundtrip():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    k = event_key("Valencia CF", "Rayo Vallecano", t)
    start, home, away = parse_event_key(k)
    assert start == t
    assert home == "valencia"
    assert away == "rayovallecano"
    assert parse_event_key("garbage") is None


def test_reconcile_keeps_exact_match():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    k = event_key("Valencia", "Rayo Vallecano", t)
    assert reconcile_event_keys([k], [k]) == {k: (k, False)}


def test_reconcile_maps_soft_key_within_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    ref = event_key("Standard Liège", "RSC Anderlecht", t)
    soft = event_key("Standard de Liege", "Anderlecht", t + timedelta(minutes=4))
    assert reconcile_event_keys([ref], [soft]) == {soft: (ref, False)}


def test_reconcile_flags_team_order_swap():
    # Same event, but the soft book lists Senegal as home instead of Nigeria.
    # The matcher should still link the two keys, with the swap flag set.
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    ref = event_key("Nigeria", "Senegal", t)
    soft = event_key("Senegal", "Nigeria", t)
    result = reconcile_event_keys([ref], [soft])
    assert result == {soft: (ref, True)}


def test_reconcile_rejects_outside_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    ref = event_key("Standard Liège", "Anderlecht", t)
    soft = event_key("Standard Liège", "Anderlecht", t + timedelta(hours=3))
    assert reconcile_event_keys([ref], [soft]) == {}


# ── Hardening: class gate (women / youth / reserve) ──────────────────────────

def test_women_team_does_not_match_mens_team():
    assert team_similarity("Anderlecht", "Anderlecht Women") == 0.0
    assert team_similarity("Club Brugge", "Club Brugge (W)") == 0.0


def test_women_variants_still_match_each_other():
    # Different books spell the women's side differently — they must still pair.
    assert team_similarity("Anderlecht Women", "RSC Anderlecht (W)") >= 85
    assert team_similarity("Lyon Feminin", "Lyon Dames") >= 80


def test_youth_and_reserve_do_not_match_senior():
    assert team_similarity("Genk", "Genk U21") == 0.0
    assert team_similarity("Bayern Munich", "Bayern Munich II") == 0.0


def test_plain_teams_unaffected_by_class_gate():
    # No class marker -> behaves exactly as before (still matches).
    assert team_similarity("Manchester United", "Man United") >= 80
    assert team_similarity("Anderlecht", "RSC Anderlecht") >= 85


NOW = datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc)


def test_class_gate_blocks_mismatch_in_reconcile():
    ref = {event_key("Anderlecht", "Genk", NOW)}
    # A women's fixture of the same clubs must NOT map onto the men's reference.
    cand = event_key("Anderlecht Women", "Genk Women", NOW)
    mapping = reconcile_event_keys(ref, [cand])
    assert cand not in mapping


def test_ambiguity_guard_skips_when_two_refs_tie():
    # Two near-identical reference events the same minute -> candidate can't be
    # pinned to one -> skipped instead of guessing.
    r1 = event_key("Tallinn Kalev", "Parnu", NOW)
    r2 = event_key("Tallinn Levadia", "Parnu", NOW)
    cand = event_key("Tallinn", "Parnu", NOW)
    mapping = reconcile_event_keys({r1, r2}, [cand])
    assert cand not in mapping


def test_single_clear_match_still_maps():
    ref = event_key("Anderlecht", "Club Brugge", NOW)
    cand = event_key("RSC Anderlecht", "Club Brugge", NOW)
    mapping = reconcile_event_keys({ref}, [cand])
    assert cand in mapping and mapping[cand][0] == ref


def _key(hhmm: str, home: str, away: str) -> str:
    return f"202608011{hhmm}::{home}__vs__{away}"


def test_tennis_tolerates_a_shifted_start_time():
    """Un match de tennis commence quand le précédent sur le court finit :
    chaque book publie sa propre estimation. Mesuré, dix des dix-neuf matchs
    Pinnacle non couverts avaient des noms identiques à 100 % et n'étaient
    écartés que par un décalage de 20 à 130 minutes."""
    ref = ["202608011400::norbertgombos__vs__kilianfeldbausch"]
    cand = {"202608011610::norbertgombos__vs__kilianfeldbausch"}

    assert reconcile_event_keys(ref, cand, time_tolerance_minutes=tolerance_for("tennis"))
    assert not reconcile_event_keys(ref, cand, time_tolerance_minutes=tolerance_for("soccer"))


def test_soccer_keeps_a_short_window():
    """Les horaires de football sont fixes et annoncés : un écart de deux
    heures y est un signal, pas du bruit."""
    assert tolerance_for("soccer") == 10
    assert tolerance_for("tennis") >= 120
    # Le football n'a pas d'entrée dédiée : il doit rester sur le défaut, pour
    # qu'élargir le tennis ne déplace rien ailleurs.
    assert tolerance_for(None) == tolerance_for("soccer")


def test_a_wider_window_does_not_defeat_the_name_check():
    """Élargir la fenêtre ne doit pas rendre le nom facultatif : c'est lui qui
    reste le juge."""
    ref = ["202608011400::norbertgombos__vs__kilianfeldbausch"]
    cand = {"202608011610::rogerfederer__vs__rafaelnadal"}
    assert not reconcile_event_keys(ref, cand, time_tolerance_minutes=tolerance_for("tennis"))


# ── Noms « Nom, Prénom » — Ladbrokes et BetFirst ──────────────────────────
# Le tennis de ces deux books était perdu ENTIÈREMENT et en silence : 1 match
# partagé avec Pinnacle sur 138 pour Ladbrokes, 3 sur 88 pour BetFirst, quand
# les autres books étaient entre 36 et 47.

def test_surname_first_names_are_reordered():
    from src.matcher import swap_surname_first
    assert swap_surname_first("Griekspoor, Tallon") == "Tallon Griekspoor"
    assert swap_surname_first("Hontama, Mai") == "Mai Hontama"
    assert swap_surname_first("Ku, Y") == "Y Ku"
    assert swap_surname_first("Urribarrens Ramirez, Iker") == "Iker Urribarrens Ramirez"


def test_doubles_pairs_and_clubs_are_left_alone():
    """Les paires de double n'ont pas de virgule, les clubs non plus. Toucher
    à ce qui marche casserait le football, déjà apparié à 573 événements."""
    from src.matcher import swap_surname_first
    for name in ("Bolelli S / Vavassori A", "Radovanovic T / Rolland de Ravel C",
                 "Club Brugge", "RSC Anderlecht", "Loureiro J V C / Ribeiro E"):
        assert swap_surname_first(name) == name


def test_a_comma_that_is_not_a_name_separator_is_left_alone():
    """Au plus deux mots après la virgule : un prénom, éventuellement composé —
    pas une seconde entité."""
    from src.matcher import swap_surname_first
    assert swap_surname_first("Dupont, Jean Michel") == "Jean Michel Dupont"
    assert swap_surname_first("Machin, Une Autre Equipe Entiere") == \
        "Machin, Une Autre Equipe Entiere"
    for bad in ("Griekspoor,", ", Tallon", "A, B, C"):
        assert swap_surname_first(bad) == bad


def test_normalization_makes_both_formats_identical():
    """Le point qui compte : après normalisation, les deux écritures donnent la
    MÊME clé — sinon aucun rapprochement n'est possible."""
    from src.matcher import normalize_team
    assert normalize_team("Griekspoor, Tallon") == normalize_team("Tallon Griekspoor")
    assert normalize_team("Hontama, Mai") == normalize_team("Mai Hontama")


def test_the_key_fragments_finally_match():
    """Sans la remise en ordre le score tombait à 77, sous le seuil de 85 —
    invisible pour team_similarity, qui ignore l'ordre et donnait 100."""
    from rapidfuzz import fuzz
    from src.matcher import normalize_team
    a = normalize_team("Tallon Griekspoor").replace(" ", "")
    b = normalize_team("Griekspoor, Tallon").replace(" ", "")
    assert a == b
    assert fuzz.ratio(a, b) == 100
