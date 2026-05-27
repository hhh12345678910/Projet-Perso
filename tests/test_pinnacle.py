from __future__ import annotations

import httpx
import pytest

from src.scrapers.pinnacle import _american_to_decimal, _is_retryable


def test_american_to_decimal_negative():
    assert _american_to_decimal(-158) == pytest.approx(1.0 + 100 / 158)
    assert _american_to_decimal(-100) == pytest.approx(2.0)


def test_american_to_decimal_positive():
    assert _american_to_decimal(289) == pytest.approx(3.89)
    assert _american_to_decimal(100) == pytest.approx(2.0)


def test_american_to_decimal_invalid():
    assert _american_to_decimal(0) is None
    assert _american_to_decimal(None) is None
    assert _american_to_decimal("x") is None


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("e", request=req, response=resp)


def test_is_retryable_only_transient():
    assert _is_retryable(httpx.ConnectError("boom")) is True
    assert _is_retryable(_status_error(429)) is True
    assert _is_retryable(_status_error(503)) is True
    assert _is_retryable(_status_error(403)) is False
    assert _is_retryable(_status_error(404)) is False
