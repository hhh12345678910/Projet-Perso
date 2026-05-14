from __future__ import annotations

from typing import Iterable


def _implied(odds: Iterable[float]) -> list[float]:
    return [1.0 / o for o in odds]


def overround(odds: Iterable[float]) -> float:
    return sum(_implied(odds)) - 1.0


def devig_multiplicative(odds: list[float]) -> list[float]:
    imp = _implied(odds)
    total = sum(imp)
    return [p / total for p in imp]


def devig_additive(odds: list[float]) -> list[float]:
    imp = _implied(odds)
    n = len(imp)
    margin = sum(imp) - 1.0
    return [p - margin / n for p in imp]


def devig_power(odds: list[float], tol: float = 1e-9, max_iter: int = 200) -> list[float]:
    imp = _implied(odds)
    lo, hi = 0.5, 2.0
    for _ in range(max_iter):
        k = (lo + hi) / 2
        s = sum(p ** k for p in imp)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return [p ** k for p in imp]


def devig_shin(odds: list[float], tol: float = 1e-10, max_iter: int = 200) -> list[float]:
    """Shin (1992/1993). Solves for insider proportion z, returns true probabilities."""
    imp = _implied(odds)
    s = sum(imp)
    if s <= 1.0:
        return [p / s for p in imp]

    lo, hi = 0.0, 0.5
    for _ in range(max_iter):
        z = (lo + hi) / 2
        probs = [
            ((z * z + 4.0 * (1.0 - z) * (pi * pi) / s) ** 0.5 - z) / (2.0 * (1.0 - z))
            for pi in imp
        ]
        total = sum(probs)
        if abs(total - 1.0) < tol:
            break
        if total > 1.0:
            lo = z
        else:
            hi = z
    z = (lo + hi) / 2
    return [
        ((z * z + 4.0 * (1.0 - z) * (pi * pi) / s) ** 0.5 - z) / (2.0 * (1.0 - z))
        for pi in imp
    ]


METHODS = {
    "multiplicative": devig_multiplicative,
    "additive": devig_additive,
    "power": devig_power,
    "shin": devig_shin,
}


def devig(odds: list[float], method: str = "shin") -> list[float]:
    if method not in METHODS:
        raise ValueError(f"unknown devig method: {method}")
    return METHODS[method](odds)
