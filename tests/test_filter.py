from src.filter import is_noise_event


def test_clean_event_is_not_noise():
    assert is_noise_event("Real Madrid", "Barcelona") is False
    assert is_noise_event("Liverpool FC", "Manchester City") is False


def test_youth_teams_are_blocked():
    assert is_noise_event("Manchester U21", "Liverpool") is True
    assert is_noise_event("Real Madrid", "Barcelona U19") is True
    assert is_noise_event("Bayern U17", "Dortmund U17") is True
    assert is_noise_event("Some Team U23", "Other") is True


def test_reserve_teams_are_blocked():
    assert is_noise_event("Ajax Reserves", "PSV") is True
    assert is_noise_event("Madrid", "Barca Reserve") is True


def test_esoccer_player_handles_are_blocked():
    assert is_noise_event("Tottenham (Beckham)", "Chelsea") is True
    assert is_noise_event("Real Madrid", "Barcelona (Cruyff 22)") is True
    assert is_noise_event("Manchester City (William)", "Arsenal") is True


def test_esoccer_keywords_are_blocked():
    assert is_noise_event("Esoccer Battle", "Some League") is True
    assert is_noise_event("eFootball Cup", "match") is True
    assert is_noise_event("Cyber Live Arena", "match") is True
    assert is_noise_event("Virtual Premier League", "match") is True


def test_league_label_is_also_checked():
    # Some scrapers know the league name separately — pass it too.
    assert is_noise_event("Team A", "Team B", "U21 Premier League") is True
    assert is_noise_event("Team A", "Team B", "Esports Battle") is True
    assert is_noise_event("Team A", "Team B", "La Liga") is False


def test_none_and_empty_are_safe():
    assert is_noise_event() is False
    assert is_noise_event("", "", "") is False
    # The shape of the call we make from scrapers — handle missing labels.
    assert is_noise_event("Real Madrid", "Barcelona", None) is False
