"""Unit tests for `app.services.loaders`.

Covers:
  * `_regex_extract` — amount detection + biller keyword detection
  * `_detect_vendor` — Nigerian biller keyword list
  * `_ocr_extract` — graceful failure when RapidOCR is unavailable
  * `TextLoader` — falls back to regex when the LLM is down
  * `ImageLoader` — 3-tier fallback chain (vision LLM → OCR+LLM → OCR+regex)

The `_ocr_extract` tests stub the engine to avoid loading the ~200MB
ONNX models in CI. The actual model load + inference is exercised in
the integration test that uploads a real PNG.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.schemas.bill import BillExtractionResult
from app.services import loaders
from app.services.loaders import (
    ImageLoader,
    TextLoader,
    _detect_vendor,
    _ocr_extract,
    _regex_extract,
)

# ── _detect_vendor ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        # Order matters: longer names first wins.
        ("DSTV Subscription - Pay ₦5,000", "DSTV"),
        ("MTN NIGERIA airtime ₦1000", "MTN NIGERIA"),
        ("Pay MTN 1000 by Friday", "MTN"),
        ("EKEDC bill for March ₦12,500", "EKEDC"),
        ("AEDC", "AEDC"),
        ("gotv package", "GOTV"),
        # Banks
        ("Transfer to GTBank", "GTBANK"),
        # Negative: no match
        ("Some random vendor", ""),
        ("", ""),
    ],
)
def test_detect_vendor_finds_known_billers(text: str, expected: str) -> None:
    assert _detect_vendor(text) == expected


# ── _regex_extract ────────────────────────────────────────────────────


def test_regex_extract_picks_amount_with_naira_symbol() -> None:
    r = _regex_extract("DSTV Subscription - Pay ₦5,000.00")
    assert r.vendor_name == "DSTV"
    assert float(r.amount) == 5000.0


def test_regex_extract_picks_amount_with_NGN_prefix() -> None:
    r = _regex_extract("NGN 10,000.50 - PHCN March bill")
    assert r.vendor_name == "PHCN"
    assert float(r.amount) == 10000.50


def test_regex_extract_picks_amount_with_N_prefix() -> None:
    r = _regex_extract("Invoice N2500 for DSTV")
    assert r.vendor_name == "DSTV"
    assert float(r.amount) == 2500.0


def test_regex_extract_returns_zero_amount_when_no_match() -> None:
    r = _regex_extract("No amount here, just a vendor name.")
    assert float(r.amount) == 0.0


def test_regex_extract_preserves_raw_text() -> None:
    text = "DSTV Subscription - Pay ₦5,000.00"
    r = _regex_extract(text)
    assert r.raw_text == text


# ── _ocr_extract (with stubbed engine) ────────────────────────────────


def test_ocr_extract_returns_empty_string_when_engine_unavailable() -> None:
    """If rapidocr isn't installed, the helper silently returns ''
    instead of raising — image uploads can still proceed (Tier 2
    skips to Tier 3 or raises the final ValueError)."""
    with patch.object(loaders, "_get_ocr_engine", return_value=None):
        assert _ocr_extract(b"any bytes") == ""


def test_ocr_extract_returns_concatenated_texts() -> None:
    """Happy path: engine returns txts=['hello', 'world'] → 'hello\nworld'."""
    fake_result = type("R", (), {"txts": ["hello", "world"]})()

    class FakeEngine:
        def __call__(self, data: bytes) -> Any:
            assert data == b"image-bytes"
            return fake_result

    with patch.object(loaders, "_get_ocr_engine", return_value=FakeEngine()):
        assert _ocr_extract(b"image-bytes") == "hello\nworld"


def test_ocr_extract_returns_empty_when_engine_returns_none() -> None:
    """Engine returns None (e.g. unparseable image) → ''."""
    class FakeEngine:
        def __call__(self, data: bytes) -> None:
            return None

    with patch.object(loaders, "_get_ocr_engine", return_value=FakeEngine()):
        assert _ocr_extract(b"garbage") == ""


def test_ocr_extract_returns_empty_when_txts_is_none() -> None:
    """Engine returns a result with txts=None (empty image) → ''."""
    fake_result = type("R", (), {"txts": None})()

    class FakeEngine:
        def __call__(self, data: bytes) -> Any:
            return fake_result

    with patch.object(loaders, "_get_ocr_engine", return_value=FakeEngine()):
        assert _ocr_extract(b"empty") == ""


def test_ocr_extract_swallows_exceptions() -> None:
    """Engine throws → '' (no exception propagated)."""
    class BrokenEngine:
        def __call__(self, data: bytes) -> None:
            raise RuntimeError("model crashed")

    with patch.object(loaders, "_get_ocr_engine", return_value=BrokenEngine()):
        assert _ocr_extract(b"any") == ""


# ── TextLoader fallback ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_loader_falls_back_to_regex_when_no_llm() -> None:
    """If the LLM is unavailable, the text loader uses regex + the
    biller keyword list. Amount and known vendor are extracted."""
    with patch.object(loaders, "_get_llm_client", return_value=None):
        loader = TextLoader("Pay DSTV ₦5000 by Friday")
        result = await loader.extract()
    assert result.vendor_name == "DSTV"
    assert float(result.amount) == 5000.0


@pytest.mark.asyncio
async def test_text_loader_falls_back_to_regex_when_llm_raises() -> None:
    """If the LLM call throws, the text loader catches it and falls
    back to regex."""
    class BrokenClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**_: Any) -> None:
                    raise RuntimeError("groq down")

    with patch.object(loaders, "_get_llm_client", return_value=BrokenClient()):
        loader = TextLoader("DSTV NIGERIA ₦12,000 March bill")
        result = await loader.extract()
    assert result.vendor_name == "DSTV NIGERIA"  # long form matches first
    assert float(result.amount) == 12000.0


# ── ImageLoader 3-tier fallback ───────────────────────────────────────


@pytest.mark.asyncio
async def test_image_loader_uses_vision_llm_when_available() -> None:
    """If the vision LLM is available, it wins (no OCR needed)."""
    canned = BillExtractionResult(
        vendor_name="DSTV", amount=5000.0, currency="NGN"
    )

    class FakeChatCompletions:
        @staticmethod
        def create(**_: Any) -> Any:
            return canned

    class FakeClient:
        chat = type("chat", (), {"completions": FakeChatCompletions})()

    with (
        patch.object(loaders, "_get_llm_client", return_value=FakeClient()),
        patch.object(loaders, "_ocr_extract") as mock_ocr,
    ):
        # OCR engine should never be called in this case.
        loader = ImageLoader(b"png-bytes", mime_type="image/png")
        result = await loader.extract()
    assert result.vendor_name == "DSTV"
    assert float(result.amount) == 5000.0
    mock_ocr.assert_not_called()


@pytest.mark.asyncio
async def test_image_loader_falls_back_to_ocr_plus_regex_when_llm_unavailable() -> None:
    """When the LLM is fully unavailable (no client), the loader
    falls through to OCR + regex extraction."""
    with patch.object(loaders, "_get_llm_client", return_value=None), patch.object(
        loaders, "_ocr_extract", return_value="DSTV Subscription - Pay ₦5,000.00"
    ):
        loader = ImageLoader(b"png-bytes", mime_type="image/png")
        result = await loader.extract()
    assert result.vendor_name == "DSTV"
    assert float(result.amount) == 5000.0
    assert result.raw_text == "DSTV Subscription - Pay ₦5,000.00"


@pytest.mark.asyncio
async def test_image_loader_falls_back_to_ocr_plus_text_llm_when_vision_fails() -> None:
    """When the vision LLM raises, the loader falls through to OCR,
    then tries the LLM (text mode) on the OCR'd text."""
    canned = BillExtractionResult(
        vendor_name="MTN", amount=1000.0, currency="NGN"
    )

    class FailingVisionClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**_: Any) -> None:
                    raise RuntimeError("vision model down")

    # After OCR, _llm_extract re-imports _get_llm_client to get a
    # client for text mode. We stub that to return a text-mode
    # client. The simplest way is to make the LLM extract succeed
    # on the OCR text by patching _llm_extract directly.
    with (
        patch.object(loaders, "_get_llm_client", return_value=FailingVisionClient()),
        patch.object(loaders, "_ocr_extract", return_value="MTN airtime ₦1,000"),
        patch.object(loaders, "_llm_extract", return_value=canned) as mock_text_llm,
    ):
        loader = ImageLoader(b"png-bytes", mime_type="image/png")
        result = await loader.extract()
    assert result.vendor_name == "MTN"
    assert float(result.amount) == 1000.0
    mock_text_llm.assert_called_once()
    # The text-mode LLM was called with the OCR'd text.
    call_args = mock_text_llm.call_args
    assert "MTN airtime ₦1,000" in call_args.kwargs.get("user", call_args.args[1] if len(call_args.args) > 1 else "")


@pytest.mark.asyncio
async def test_image_loader_raises_when_no_llm_and_ocr_returns_empty() -> None:
    """When both the LLM and OCR are unavailable (or return empty),
    the loader raises ValueError with a clear message."""
    with (
        patch.object(loaders, "_get_llm_client", return_value=None),
        patch.object(loaders, "_ocr_extract", return_value=""),
    ):
        loader = ImageLoader(b"png-bytes", mime_type="image/png")
        with pytest.raises(ValueError, match="no LLM available and local OCR"):
            await loader.extract()


@pytest.mark.asyncio
async def test_image_loader_uses_ocr_regex_when_text_llm_also_fails() -> None:
    """When the vision LLM fails, OCR runs, then the text LLM also
    fails, the loader falls back to regex on the OCR text."""
    class FailingClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**_: Any) -> None:
                    raise RuntimeError("groq down")

    with (
        patch.object(loaders, "_get_llm_client", return_value=FailingClient()),
        patch.object(loaders, "_ocr_extract", return_value="AEDC power ₦7,500"),
    ):
        loader = ImageLoader(b"png-bytes", mime_type="image/png")
        result = await loader.extract()
    assert result.vendor_name == "AEDC"
    assert float(result.amount) == 7500.0
