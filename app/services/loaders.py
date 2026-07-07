"""Bill loaders — convert uploaded bytes/text into a `BillExtractionResult`.

Three concrete loaders:

  * `TextLoader`  — the user typed a description ("Pay PHCN 12k by Friday").
  * `PDFLoader`   — text-based PDF. We use PyMuPDF (`fitz`) which we
                    already ship; no extra system deps.
  * `ImageLoader` — vision-capable LLM. We send the image as base64 to
                    the chat completions endpoint; no system tesseract
                    install required.

If no LLM is configured, the loaders still return what they can
extract heuristically (text-mode extracts vendor + amount with regex;
PDF mode returns the raw extracted text). The decision agent downstream
will then ask the user for missing fields.

All loaders are async and accept raw bytes (so the FastAPI
`UploadFile.read()` path works without writing to disk).
"""
from __future__ import annotations

import base64
import logging
import re
from abc import ABC, abstractmethod

from app.core.config import settings
from app.schemas.bill import BillExtractionResult

logger = logging.getLogger(__name__)

# A rough match for "₦12,345.67" or "NGN 12,345.67"
_AMOUNT_RE = re.compile(r"(?:[₦N]|NGN)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"
)


# ── LLM client (lazy, optional) ─────────────────────────────────────

def _get_llm_client():
    """Return a Groq client wrapped by instructor, or None if no key.

    The loaders work without an LLM — they fall back to regex
    extraction. This keeps the build runnable on a fresh machine
    that has no `GROQ_API_KEY` set.
    """
    api_key = settings.groq_api_key
    if not api_key:
        return None
    try:
        import instructor  # type: ignore
        from groq import Groq  # type: ignore
    except ImportError:
        return None
    return instructor.from_groq(Groq(api_key=api_key))



def _llm_extract(system: str, user: str) -> BillExtractionResult | None:
    """Call Groq with structured output. Returns None on any failure."""
    client = _get_llm_client()
    if client is None:
        return None
    try:
        result = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            response_model=BillExtractionResult,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as exc:
        logger.warning("LLM extraction failed: %s", exc)
        return None
    return result


# ── Heuristic fallback ──────────────────────────────────────────────

# Curated list of common Nigerian billers. Order matters: longer names
# first so a more specific match (e.g. "EKEDC") doesn't get shadowed
# by a substring (e.g. "EDC"). All lowercase — `_detect_vendor` does
# the case-insensitive scan.
_NG_BILLERS: tuple[str, ...] = (
    # Telecom
    "mtn nigeria", "airtel nigeria", "globacom limited", "9mobile",
    "smile communications", "spectranet", "swift networks",
    "mtn", "airtel", "glo", "9mobile",
    # Pay TV / streaming
    "showmax", "dstv nigeria", "gotv nigeria",
    "dstv", "gotv", "netflix",
    # Power (DISCOS)
    "ekedc", "ikedc", "aedc", "phed", "kedc", "nedc", "eedc", "jedi",
    "phcn", "nepa",
    # Cable / ISP / water
    "wace", "waec", "neco", "jamb",
    # Banks (for transfers)
    "gtbank", "zenith", "uba", "access", "first bank", "fidelity",
)


def _detect_vendor(text: str) -> str:
    """Scan `text` for known Nigerian biller names. Returns the matched
    name in uppercase, or "" if no match.

    Cheap O(n*m) loop is fine — `text` is at most a few KB of OCR'd
    bill text and the biller list is ~30 entries.
    """
    if not text:
        return ""
    lowered = text.lower()
    for name in _NG_BILLERS:
        if name in lowered:
            return name.upper()
    return ""


def _regex_extract(text: str) -> BillExtractionResult:
    """Best-effort bill extraction when no LLM is available.

    Picks up the amount (via `_AMOUNT_RE`) and a known biller name
    (via `_detect_vendor`). The biller keyword list covers the top
    Nigerian billers; arbitrary vendor names still require the LLM.
    """
    amount_match = _AMOUNT_RE.search(text)
    amount = float(amount_match.group(1).replace(",", "")) if amount_match else 0.0
    return BillExtractionResult(
        vendor_name=_detect_vendor(text),
        amount=amount,
        raw_text=text,
    )


# ── Local OCR fallback (RapidOCR / ONNX Runtime) ─────────────────────
#
# When the Groq LLM is down or the API key is missing, we still want
# image uploads to produce *something*. RapidOCR runs PaddleOCR's
# models on ONNX Runtime — same accuracy, no PaddlePaddle framework
# dependency, models bundled in the wheel.

# Module-level engine cache so we only load the ~200MB models once
# per process. The first image upload pays the warm-up cost (~3s on
# CPU); subsequent calls are sub-second.
_ocr_engine: object | None = None
_ocr_engine_load_failed = False


def _get_ocr_engine():
    """Lazy-init the RapidOCR engine. Returns None if the package
    isn't installed (e.g. the OCR extras weren't installed) or the
    model load failed on this platform."""
    global _ocr_engine, _ocr_engine_load_failed
    if _ocr_engine is not None:
        return _ocr_engine
    if _ocr_engine_load_failed:
        return None
    try:
        from rapidocr import RapidOCR  # type: ignore

        _ocr_engine = RapidOCR()
    except Exception as exc:
        logger.warning("RapidOCR engine failed to load: %s", exc)
        _ocr_engine_load_failed = True
        return None
    return _ocr_engine


def _ocr_extract(data: bytes) -> str:
    """Run local OCR on raw image bytes. Returns the concatenated
    recognized text, or "" on any failure (no engine, bad image,
    empty result). Never raises."""
    engine = _get_ocr_engine()
    if engine is None:
        return ""
    try:
        result = engine(data)
    except Exception as exc:
        logger.warning("OCR fallback failed: %s", exc)
        return ""
    if result is None:
        return ""
    txts = getattr(result, "txts", None)
    if not txts:
        return ""
    return "\n".join(txts)


# ── Base class ──────────────────────────────────────────────────────

class BaseLoader(ABC):
    """All loaders implement a single `extract()` async method."""

    @abstractmethod
    async def extract(self) -> BillExtractionResult:
        ...


# ── Text loader ─────────────────────────────────────────────────────

class TextLoader(BaseLoader):
    """User-typed bill description. The LLM does the heavy lifting."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def extract(self) -> BillExtractionResult:
        result = _llm_extract(
            system=(
                "You are a financial assistant. Extract bill or payment "
                "details from the user's message. If a piece of info is "
                "missing, leave it null. Today's date is "
                f"{__import__('datetime').date.today().isoformat()} — use "
                "it to resolve relative dates like 'next Friday'. "
                "For the `due_date` field, ALWAYS return an ISO 8601 date "
                "string in the form `YYYY-MM-DD` (or null if the bill "
                "has no due date). Do not return times, timezones, or "
                "natural-language phrases — just the bare date."
            ),
            user=f"Extract details from this message:\n\n{self.text}",
        )
        if result is not None:
            result.raw_text = self.text
            return result
        return _regex_extract(self.text)


# ── PDF loader ──────────────────────────────────────────────────────

class PDFLoader(BaseLoader):
    """Text-based PDF via PyMuPDF. No system tesseract required."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self._text: str | None = None

    def _extract_text(self) -> str:
        if self._text is not None:
            return self._text
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pymupdf not installed") from exc

        doc = fitz.open(stream=self.data, filetype="pdf")
        try:
            chunks: list[str] = []
            for page in doc:
                chunks.append(page.get_text())
            self._text = "\n".join(chunks)
        finally:
            doc.close()
        return self._text

    async def extract(self) -> BillExtractionResult:
        text = self._extract_text()
        if not text.strip():
            raise ValueError("No text could be extracted from this PDF.")

        result = _llm_extract(
            system=(
                "You are an expert financial auditor. Extract bill details "
                "accurately. Leave fields null if not present in the text. "
                "For the `due_date` field, ALWAYS return an ISO 8601 date "
                "string in the form `YYYY-MM-DD` (or null). Do not return "
                "times, timezones, or natural-language phrases."
            ),
            user=f"Extract details from this bill text:\n\n{text[:8000]}",
        )
        if result is not None:
            result.raw_text = text
            return result
        return _regex_extract(text)


# ── Image loader ────────────────────────────────────────────────────

class ImageLoader(BaseLoader):
    """Image-based bill. Three-tier extraction:

    1. **LLM (vision)** — best quality; sends the image to Groq's
       vision-capable model. Used when `GROQ_API_KEY` is set and the
       API is reachable.
    2. **Local OCR → LLM (text)** — if the vision LLM fails (down,
       rate-limited, etc.), run RapidOCR locally to extract raw text
       from the image, then pass that text to the LLM as a regular
       text extraction. Gives the LLM a second chance with cleaner
       input.
    3. **Local OCR → regex** — if the LLM is also down for text,
       parse the OCR'd text with `_regex_extract` + the Nigerian
       biller keyword list. The result may be incomplete (no vendor
       name for unknown billers) but at least the amount and known
       biller are filled in.

    If both the LLM and the local OCR engine are unavailable (e.g.
    the OCR extras weren't installed), `extract` raises `ValueError`
    with a clear message.
    """

    SUPPORTED_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

    def __init__(self, data: bytes, mime_type: str = "image/png") -> None:
        self.data = data
        self.mime_type = mime_type if mime_type in self.SUPPORTED_MIME else "image/png"

    _IMAGE_PROMPT = (
        "Extract bill details from this image. "
        "Leave fields null if not visible. "
        "Important note: all currency is in NGN (Nigerian Naira). "
        "For the `due_date` field, ALWAYS return an ISO 8601 date "
        "string in the form `YYYY-MM-DD` (or null if not visible). "
        "Do not return times, timezones, or natural-language "
        "phrases like 'next Friday'."
    )

    async def extract(self) -> BillExtractionResult:
        b64 = base64.b64encode(self.data).decode("ascii")
        data_url = f"data:{self.mime_type};base64,{b64}"

        # ── Tier 1: vision LLM ─────────────────────────────────────
        client = _get_llm_client()
        if client is not None:
            try:
                result = client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    response_model=BillExtractionResult,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": self._IMAGE_PROMPT},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                    strict=True,
                )
                return result
            except Exception as exc:
                logger.warning("Vision LLM failed, trying OCR fallback: %s", exc)

        # ── Tier 2: local OCR → LLM (text) ─────────────────────────
        ocr_text = _ocr_extract(self.data)
        if ocr_text.strip() and client is not None:
            result = _llm_extract(
                system=(
                    "You are an expert financial auditor. Extract bill details "
                    "accurately. Leave fields null if not present in the text. "
                    "For the `due_date` field, ALWAYS return an ISO 8601 date "
                    "string in the form `YYYY-MM-DD` (or null). Do not return "
                    "times, timezones, or natural-language phrases."
                ),
                user=f"Extract details from this bill text:\n\n{ocr_text[:8000]}",
            )
            if result is not None:
                result.raw_text = ocr_text
                return result

        # ── Tier 3: local OCR → regex ──────────────────────────────
        if ocr_text.strip():
            return _regex_extract(ocr_text)

        # No LLM, no OCR text — nothing we can do.
        raise ValueError(
            "Could not extract bill from image: no LLM available and local "
            "OCR did not return any text. Set GROQ_API_KEY or install the "
            "OCR fallback (rapidocr + onnxruntime)."
        )


# ── Factory ─────────────────────────────────────────────────────────

def loader_from_upload(
    filename: str,
    content_type: str | None,
    data: bytes,
) -> BaseLoader:
    """Pick a loader based on filename / content-type."""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()

    if name.endswith(".pdf") or "pdf" in ctype:
        return PDFLoader(data)
    if (
        name.endswith((".png", ".jpg", ".jpeg", ".webp"))
        or ctype.startswith("image/")
    ):
        mime = ctype if ctype in ImageLoader.SUPPORTED_MIME else "image/png"
        return ImageLoader(data, mime_type=mime)
    # Default: treat as text
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    return TextLoader(text)
