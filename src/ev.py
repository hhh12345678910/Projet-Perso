from __future__ import annotations


def fair_odd(prob: float) -> float:
    if prob <= 0.0 or prob >= 1.0:
        raise ValueError("prob must be in (0, 1)")
    return 1.0 / prob


def ev_pct(odd_taken: float, fair_prob: float) -> float:
    return (odd_taken * fair_prob - 1.0) * 100.0


def kelly_fraction(odd_taken: float, fair_prob: float) -> float:
    """Full Kelly stake as fraction of bankroll. Negative means do not bet."""
    b = odd_taken - 1.0
    if b <= 0.0:
        return 0.0
    q = 1.0 - fair_prob
    return (b * fair_prob - q) / b


def kelly_stake(odd_taken: float, fair_prob: float, bankroll: float, fraction: float = 0.25) -> float:
    """Fractional Kelly (default quarter Kelly — standard for value betting)."""
    k = kelly_fraction(odd_taken, fair_prob)
    if k <= 0.0:
        return 0.0
    return max(0.0, bankroll * k * fraction)
