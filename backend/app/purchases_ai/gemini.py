# Ported from Ease My Bill (receipt-analyzer/backend/gemini_client.py). Reads WISE_GEMINI_API_KEY or GEMINI_API_KEY.
"""Direct Google Gemini API client — replaces emergentintegrations.LlmChat
for OCR + text extraction.

Design intent:
    * Bypasses the Emergent universal LLM key so the workspace is billed
      directly on the customer's Google Cloud project (`GEMINI_API_KEY`).
    * Presents a `LlmChat`-shaped shim so the four existing call sites in
      `extraction.py`, `daily_sheet.py`, and `expenses.py` need only a
      one-line import swap — no logic rewrite.
    * Uses the current Google GenAI SDK (`google-genai`), not the
      deprecated `google-generativeai` package.
    * Bakes in the Google-recommended retry policy for 408/429/5xx.

Public surface (mirrors the emergentintegrations import shape so the
call sites read identically):

    from gemini_client import LlmChat, UserMessage, ImageContent

    chat = LlmChat(
        api_key=GEMINI_API_KEY,
        session_id="bill-1234",           # currently unused, kept for shape parity
        system_message="You are an OCR…",
    ).with_model("google", "gemini-3.5-flash-lite")

    reply = await chat.send_message(UserMessage(
        text="Extract JSON…",
        file_contents=[ImageContent(image_base64=b64)],
    ))
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import warnings
from dataclasses import dataclass, field
from typing import Any, List, Optional

# Silence the google-genai "AFC not recommended" advisory — we never use
# automatic function calling, we just want single-turn `generate_content`
# for stateless OCR. The warning fires on every request and clutters
# server logs otherwise. It's emitted through the SDK's own logger, so
# `warnings.filterwarnings` alone is not enough — we also mute the log.
warnings.filterwarnings(
    "ignore",
    message=r".*automatic function calling.*",
    category=Warning,
)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("WISE_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""

# Single, official production model — mirrors the migration decision made
# with the founder on 2026-08-22 (hard swap from Claude Sonnet 5 to
# Gemini 3.5 Flash Lite, billed on the customer's own Google project).
DEFAULT_MODEL = "gemini-3.5-flash-lite"

# The 2.5 alias was silently retired by Google on 2026-08 — Google now
# returns HTTP 404 with a message pointing at 3.5-flash-lite. We keep an
# alias table so any call site that still passes the older ID stays alive.
_MODEL_ALIASES = {
    "gemini-2.5-flash-lite": DEFAULT_MODEL,
    "gemini-2.0-flash-lite": DEFAULT_MODEL,
    "gemini-flash-lite":     DEFAULT_MODEL,
    "gpt-4o":                DEFAULT_MODEL,
}

# wise-plus: Claude models (Anthropic API) can read bills too, when WISE_ANTHROPIC_API_KEY is set.
ANTHROPIC_API_KEY = os.environ.get("WISE_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
CLAUDE_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
# USD per million tokens (input, output); used for the cost log and the reader comparison
PRICES = {"gemini-3.5-flash-lite": (0.30, 2.50), "claude-haiku-5-5": (0.10, 0.50),
          "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
# Every model call adds {model, in, out, usd} here; the comparison script reads it.
USAGE: list[dict] = []


def _note_usage(model: str, tin: int, tout: int) -> None:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    usd = tin * pin / 1e6 + tout * pout / 1e6
    USAGE.append({"model": model, "in": tin, "out": tout, "usd": usd})
    try:
        from app.ai_usage import record          # wise-plus: kept per restaurant, for cost per scan in admin
        record(model, tin, tout, usd)
    except Exception:  # noqa: BLE001
        pass


def _resolve_model(model_id: str | None) -> str:
    if not model_id:
        return DEFAULT_MODEL
    m = model_id.strip().lower()
    if m.startswith("claude-") and m not in CLAUDE_MODELS:
        return DEFAULT_MODEL          # retired Claude names from the Emergent days
    return _MODEL_ALIASES.get(m, model_id)


# ---------------------------------------------------------------------------
# Client singleton with retry policy
# ---------------------------------------------------------------------------

_client_singleton: Optional[genai.Client] = None
_client_key: str = ""


def current_key() -> str:
    """The key set in admin (wise-plus), else the server's WISE_GEMINI_API_KEY."""
    try:
        from app.config import get_settings
        return get_settings().gemini_api_key or GEMINI_API_KEY
    except Exception:  # noqa: BLE001
        return GEMINI_API_KEY


def _client() -> genai.Client:
    """Lazy-init a single `genai.Client` for the whole process.

    The SDK is thread-safe and internally pools HTTP connections; a single
    instance is materially faster than creating one per request.
    """
    global _client_singleton, _client_key
    key = current_key()
    if _client_singleton is not None and key == _client_key:
        return _client_singleton
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured in backend/.env")
    _client_key = key
    _client_singleton = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(
            # Bounded exponential backoff on transient failures. 5 attempts
            # (~30 s worst-case) matches Google's own recommendation and is
            # cheap enough that we never surface a spurious 429 to the user.
            retry_options=types.HttpRetryOptions(
                attempts=5,
                initial_delay=1.0,
                max_delay=16.0,
                exp_base=2.0,
                jitter=1.0,
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
        ),
    )
    return _client_singleton


# ---------------------------------------------------------------------------
# LlmChat-shaped shim
# ---------------------------------------------------------------------------

@dataclass
class ImageContent:
    """Drop-in replacement for emergentintegrations' ImageContent."""
    image_base64: str


@dataclass
class UserMessage:
    """Drop-in replacement for emergentintegrations' UserMessage.

    `file_contents` accepts `ImageContent` instances; anything else is
    silently ignored (we don't support audio/video/PDF for now — the
    current codebase never sends any of those anyway).
    """
    text: str
    file_contents: List[Any] = field(default_factory=list)


class LlmChat:
    """LlmChat-shaped facade over `google-genai`'s `client.aio.models`.

    Enough of the original shape to let existing call sites work with a
    one-import swap. Deliberately narrow — we only support what the
    codebase actually uses today (text + optional image parts, string
    reply). If we need chat history or streaming later, we can grow.
    """

    def __init__(self,
                 api_key: str,
                 session_id: str = "",
                 system_message: str = "") -> None:
        # `api_key` is accepted for interface parity; we always read from
        # the process-wide env var so per-request keys can't accidentally
        # leak in from user input.
        self._session_id = session_id
        self._system_instruction = (system_message or "").strip() or None
        self._model_id: str = DEFAULT_MODEL

    def with_model(self, provider: str, model_id: str) -> "LlmChat":
        """Emergent's builder used `(provider, model_id)`. We ignore
        the provider (only Google is supported now) and resolve any
        legacy identifier to `gemini-3.5-flash-lite`."""
        self._model_id = _resolve_model(model_id)
        if provider and provider.lower() not in ("google", "gemini", ""):
            logger.debug(
                "gemini_client: ignoring legacy provider=%s, forcing Google",
                provider,
            )
        return self

    async def send_message(self, message: UserMessage) -> str:
        """Send one message and return the assistant's text reply.

        Mirrors the emergent API: input has text + optional images,
        output is a plain string (the JSON parsing happens upstream in
        the caller so nothing else has to change).
        """
        parts: List[Any] = []
        # Text part first (matches how the original LlmChat serialized).
        if message.text:
            parts.append(types.Part.from_text(text=message.text))
        # Then any image attachments — decoded from base64.
        for f in (message.file_contents or []):
            if isinstance(f, ImageContent) and f.image_base64:
                try:
                    img_bytes = base64.b64decode(f.image_base64)
                except Exception as exc:
                    logger.warning("gemini_client: bad base64 image (%s)", exc)
                    continue
                # We standardise on JPEG upstream (extraction._preprocess_image
                # writes JPEG at 85%); if we ever receive a PNG we still send
                # JPEG mime — Gemini sniffs the bytes and handles the mismatch.
                mime = _sniff_image_mime(img_bytes)
                parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

        if not parts:
            raise ValueError("send_message called with an empty message")
        if self._model_id in CLAUDE_MODELS:
            return await self._send_claude(message)

        cfg_kwargs: dict[str, Any] = {}
        if self._system_instruction:
            cfg_kwargs["system_instruction"] = self._system_instruction

        try:
            resp = await _client().aio.models.generate_content(
                model=self._model_id,
                contents=parts,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        except Exception as exc:
            # Re-raise so the caller's existing try/except (which currently
            # handles JSONDecodeError + generic Exception) sees a clean
            # message with no API key in it.
            msg = str(exc)
            # Redact any accidental key echo — belt-and-braces.
            for k in {GEMINI_API_KEY, current_key()} - {""}:
                msg = msg.replace(k, "***REDACTED***")
            raise RuntimeError(f"Gemini API call failed: {msg}") from exc

        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            _note_usage(self._model_id, int(getattr(um, "prompt_token_count", 0) or 0),
                        int(getattr(um, "candidates_token_count", 0) or 0) + int(getattr(um, "thoughts_token_count", 0) or 0))
        text = _extract_text(resp)
        if not text:
            # Empty reply usually means safety block or truncation; give the
            # caller a chance to log the finish reason.
            finish = _finish_reason(resp)
            raise RuntimeError(
                f"Gemini returned no text (finish_reason={finish}). "
                "Check safety metadata or reduce image size."
            )
        return text


    async def _send_claude(self, message: "UserMessage") -> str:
        """The same message through Anthropic's Messages API (images first, then the text)."""
        import httpx
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("Claude isn't set up (no Anthropic API key)")
        content: List[dict] = []
        for f in (message.file_contents or []):
            if isinstance(f, ImageContent) and f.image_base64:
                raw = base64.b64decode(f.image_base64)
                content.append({"type": "image", "source": {"type": "base64", "media_type": _sniff_image_mime(raw), "data": f.image_base64}})
        if message.text:
            content.append({"type": "text", "text": message.text})
        body: dict[str, Any] = {"model": self._model_id, "max_tokens": 8192, "messages": [{"role": "user", "content": content}]}
        if self._system_instruction:
            body["system"] = self._system_instruction
        last: Exception | None = None
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as c:
                    r = await c.post("https://api.anthropic.com/v1/messages", json=body,
                                     headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
                if r.status_code in (429, 500, 502, 503, 529):
                    last = RuntimeError(f"Claude busy ({r.status_code})")
                    await asyncio.sleep(2 ** attempt)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"Claude API call failed ({r.status_code}): {r.text[:300]}")
                d = r.json()
                u = d.get("usage") or {}
                _note_usage(self._model_id, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0))
                text = "".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text")
                if not text:
                    raise RuntimeError(f"Claude returned no text (stop_reason={d.get('stop_reason')})")
                return text
            except httpx.HTTPError as e:
                last = e
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Claude API call failed: {last}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sniff_image_mime(b: bytes) -> str:
    """Best-effort MIME sniff so Gemini receives an accurate mime type."""
    if len(b) < 8:
        return "image/jpeg"
    # PNG magic
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    # JPEG SOI
    if b[:2] == b"\xff\xd8":
        return "image/jpeg"
    # WebP: "RIFF….WEBP"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _extract_text(resp: Any) -> str:
    """Pull the reply string out of a `GenerateContentResponse`.

    Prefers the SDK's `.text` shortcut but falls back to walking parts
    manually in case the response has multiple candidates / non-text
    parts (extremely rare on Flash Lite but not impossible).
    """
    text = getattr(resp, "text", None)
    if text:
        return text
    # Manual walk fallback.
    try:
        for cand in (resp.candidates or []):
            for part in (getattr(cand, "content", None).parts or []):
                t = getattr(part, "text", None)
                if t:
                    return t
    except Exception:  # pragma: no cover — defensive
        pass
    return ""


def _finish_reason(resp: Any) -> str:
    try:
        for cand in (resp.candidates or []):
            fr = getattr(cand, "finish_reason", None)
            if fr:
                return str(fr)
    except Exception:  # pragma: no cover
        pass
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Debug / self-test
# ---------------------------------------------------------------------------

async def _selftest() -> str:
    """Quick sanity check runnable as `python -m gemini_client`."""
    chat = LlmChat(
        api_key=GEMINI_API_KEY,
        session_id="selftest",
        system_message="You reply in exactly 2 words.",
    ).with_model("google", DEFAULT_MODEL)
    return await chat.send_message(UserMessage(text="Say OK"))


if __name__ == "__main__":  # pragma: no cover
    import asyncio
    print(asyncio.run(_selftest()))
