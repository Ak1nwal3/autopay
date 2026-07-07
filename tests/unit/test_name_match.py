"""Unit tests for `app.services.name_match`.

Pure-Python (no DB) — the helper is fuzzy-string-only, so we can
exhaustively test the corner cases here without spinning up Postgres.
"""
from __future__ import annotations

import pytest

from app.services.name_match import names_match


# ── Happy path: subsidiary / suffix variations ─────────────────────


@pytest.mark.parametrize(
    "extracted,resolved",
    [
        ("DSTV", "DSTV NIGERIA LIMITED"),
        ("DSTV", "DSTV NIG LTD"),
        ("DSTV Nigeria Ltd", "DSTV NIG LTD"),
        ("MTN", "MTN NIGERIA COMMUNICATIONS PLC"),
        ("MTN Nigeria", "MTN NIGERIA COMMUNICATIONS PLC"),
        ("Acme Holdings", "Acme Holdings Ltd"),
        ("Globacom", "GLOBACOM LIMITED"),
        ("Airtel Nigeria", "AIRTEL NETWORKS LIMITED"),
    ],
)
def test_names_match_accepts_subsidiary_variations(extracted, resolved) -> None:
    assert names_match(extracted, resolved) is True


# ── Negative cases: genuinely different entities ──────────────────


@pytest.mark.parametrize(
    "extracted,resolved",
    [
        ("DSTV", "GOTV NIGERIA LIMITED"),
        ("MTN", "AIRTEL NIGERIA"),
        ("Acme Holdings", "Beta Industries Ltd"),
        ("Globacom", "MTN Nigeria"),
    ],
)
def test_names_match_rejects_different_entities(extracted, resolved) -> None:
    assert names_match(extracted, resolved) is False


# ── Edge cases ─────────────────────────────────────────────────────


def test_names_match_exact_normalized() -> None:
    # Punctuation + case differences should be ignored.
    assert names_match("DSTV NIGERIA LTD", "dstv nigeria ltd") is True
    assert names_match("Acme & Co.", "ACME AND CO") is True


def test_names_match_rejects_empty_strings() -> None:
    assert names_match("", "DSTV LIMITED") is False
    assert names_match("DSTV", "") is False
    assert names_match("", "") is False


def test_names_match_custom_threshold() -> None:
    # A 50-threshold is more permissive — a substring match would pass.
    # Use a pair that has a real overlap so the test is meaningful.
    assert names_match("DSTV", "DSTV NIGERIA LIMITED", threshold=50) is True
    # A 99-threshold demands near-exactness. Use a pair that is
    # genuinely different — `token_set_ratio` returns 100 when one
    # name is a token-subset of the other ("DSTV" is a subset of
    # "DSTV NIGERIA LIMITED"), so we use two distinct companies.
    assert (
        names_match("DSTV NIGERIA", "GOTV NIGERIA LIMITED", threshold=99)
        is False
    )
    assert (
        names_match("DSTV NIGERIA", "DSTV NIGERIA LIMITED", threshold=99)
        is True
    )
