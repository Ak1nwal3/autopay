"""Fuzzy name matching for payee account validation.

When a user submits a bill for vendor "DSTV" and the bank-side
account name resolves to "DSTV NIGERIA LIMITED" we should treat
those as a match — banks are inconsistent about suffixes (LTD vs
LIMITED vs PLC), word order ("MTN NIGERIA" vs "NIGERIA MTN"), and
whether the country is in the name at all.

`names_match(extracted, resolved)` is the single source of truth
for "do these two names refer to the same entity?".

Strategy:
  1. Normalize both sides (case-fold, strip punctuation, drop
     common corporate suffixes, collapse whitespace).
  2. Use `rapidfuzz.fuzz.token_set_ratio` which is order-insensitive
     and handles "all tokens of A appear in B" cases.
  3. Compare against a threshold (default 70) — same range Stripe
     Radar uses for payee name checks.

This module is dependency-light (just rapidfuzz) and pure-Python
(no DB / I/O), so it's safe to call from any layer.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Final


# Common corporate/legal suffixes that Nigerian banks use
# inconsistently. We strip these from BOTH sides so "DSTV NIGERIA
# LTD" normalizes to "dstv nigeria" and matches "MTN NIGERIA PLC"
# only if the underlying business names are similar.
_SUFFIXES: Final[frozenset[str]] = frozenset({
    "ltd", "limited", "plc", "plc.", "nig", "nigeria",
    "company", "co", "co.", "inc", "inc.",
    "and", "&", "the",
})


def _normalize(name: str) -> str:
    """Lowercase, strip accents, drop suffixes + punctuation, collapse
    whitespace. Returns a single space-delimited string."""
    if not name:
        return ""
    # Unicode-normalize then drop combining marks (e.g. "CAFÉ" → "CAFE").
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    lower = ascii_only.lower()
    # Replace any non-alphanumeric with a space.
    alnum = re.sub(r"[^a-z0-9]+", " ", lower)
    tokens = [t for t in alnum.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


def names_match(
    extracted: str,
    resolved: str,
    *,
    threshold: int = 70,
) -> bool:
    """Return True iff `extracted` and `resolved` are likely the same
    entity, per the threshold on token_set_ratio.

    Examples:
        names_match("DSTV", "DSTV NIGERIA LIMITED") -> True
        names_match("MTN", "MTN NIGERIA COMMUNICATIONS PLC") -> True
        names_match("DSTV", "GOTV NIGERIA LIMITED") -> False
        names_match("Acme Holdings", "Acme Holdings Ltd") -> True
        names_match("Acme", "Beta") -> False

    Args:
        extracted: The vendor name on the bill (user-typed).
        resolved: The bank's official account name (from Paystack).
        threshold: Minimum token_set_ratio score (0-100) to accept.

    Returns:
        True if the names match within the threshold; False otherwise.
    """
    if not extracted or not resolved:
        # Be conservative: empty names are suspicious, never match.
        return False

    # Exact (normalized) match is always a match — the common case for
    # users who paste the bill from a clear source.
    n_extracted = _normalize(extracted)
    n_resolved = _normalize(resolved)
    if n_extracted and n_extracted == n_resolved:
        return True

    # Defer to rapidfuzz for the fuzzy case. Imported lazily so the
    # test harness can stub the dependency in exotic environments.
    from rapidfuzz import fuzz

    score = fuzz.token_set_ratio(n_extracted, n_resolved)
    return score >= threshold
