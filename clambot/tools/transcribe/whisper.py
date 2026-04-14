"""Whisper API clients for speech-to-text transcription.

Provides :func:`transcribe_files` which sends audio files to either OpenAI-
compatible Whisper endpoints (for example Groq) or the local Whisper ASR
webservice API. Handles per-chunk errors gracefully by inserting
``[transcription gap]`` markers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import httpx

__all__ = ["transcribe_files"]

logger = logging.getLogger(__name__)

GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_OPENAI_REQUEST_TIMEOUT = 120.0  # seconds per chunk
_LOCAL_WHISPER_ASR_REQUEST_TIMEOUT = 600.0  # seconds per chunk
WhisperApiStyle = Literal["openai", "whisper_asr"]


def transcribe_files(
    paths: list[Path],
    api_key: str,
    model: str = "whisper-large-v3",
    language: str | None = None,
    api_url: str = GROQ_WHISPER_URL,
    api_style: WhisperApiStyle = "openai",
    request_timeout: float | None = None,
) -> str:
    """Transcribe one or more audio files via a configured Whisper API.

    Args:
        paths: Ordered list of audio file paths to transcribe.
        api_key: API key/token used for authenticated endpoints.
        model: Whisper model identifier (default ``"whisper-large-v3"``).
        language: Optional ISO 639-1 language hint (e.g. ``"en"``).
        api_url: Transcription API endpoint URL.
        api_style: Endpoint compatibility mode. ``"openai"`` uses
            ``/audio/transcriptions`` semantics; ``"whisper_asr"`` uses
            ``/asr`` semantics from whisper-asr-webservice.
        request_timeout: Optional per-chunk HTTP timeout in seconds.
            If omitted, defaults to ``120`` for ``"openai"`` endpoints and
            ``600`` for ``"whisper_asr"`` endpoints.

    Returns:
        Concatenated transcript text with chunks separated by spaces.
        Chunks that fail are represented as ``"[transcription gap]"``.

    Raises:
        ValueError: If *paths* is empty, or if *api_key* is empty for
            ``api_style="openai"``.
    """
    if api_style == "openai" and not api_key:
        raise ValueError("Groq API key is required for transcription")
    if not paths:
        raise ValueError("At least one audio file path is required")

    segments: list[str] = []
    timeout = _resolve_request_timeout(api_style, request_timeout)

    with httpx.Client(timeout=timeout) as client:
        for path in paths:
            try:
                text = _transcribe_single(
                    client,
                    path,
                    api_key,
                    model,
                    language,
                    api_url,
                    api_style,
                )
                segments.append(text)
            except Exception:
                logger.warning("Transcription failed for chunk %s", path.name, exc_info=True)
                segments.append("[transcription gap]")

    return " ".join(segments)


def _resolve_request_timeout(api_style: WhisperApiStyle, request_timeout: float | None) -> float:
    """Resolve the per-request timeout in seconds."""
    if request_timeout is not None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be > 0 seconds")
        return request_timeout

    if api_style == "whisper_asr":
        return _LOCAL_WHISPER_ASR_REQUEST_TIMEOUT
    return _OPENAI_REQUEST_TIMEOUT


_MAX_RETRIES = 4
_INITIAL_BACKOFF = 2.0  # seconds
_RETRYABLE_STATUS_CODES: set[int] = {408, 429, 500, 502, 503, 504}


def _retry_backoff(path: Path, attempt: int, reason: str) -> None:
    """Sleep before retrying a failed transcription request."""
    import time

    wait = _INITIAL_BACKOFF * (2**attempt)
    logger.info(
        "%s on %s — retrying in %.1fs (attempt %d/%d)",
        reason,
        path.name,
        wait,
        attempt + 1,
        _MAX_RETRIES,
    )
    time.sleep(wait)


def _transcribe_single(
    client: httpx.Client,
    path: Path,
    api_key: str,
    model: str,
    language: str | None,
    api_url: str = GROQ_WHISPER_URL,
    api_style: WhisperApiStyle = "openai",
) -> str:
    """Transcribe a single audio file and return the text.

    Retries with exponential backoff on 429 (rate limit) responses.

    Args:
        client: Active :class:`httpx.Client` to reuse across chunks.
        path: Path to the audio file to transcribe.
        api_key: Bearer token for the transcription API.
        model: Whisper model identifier.
        language: Optional ISO 639-1 language hint.
        api_url: Transcription API endpoint URL.

    Raises:
        httpx.HTTPStatusError: On non-2xx, non-429 responses or
            after exhausting retries.
        KeyError: If the response JSON lacks a ``text`` field.
    """
    if api_style == "whisper_asr":
        return _transcribe_whisper_asr(client, path, api_key, language, api_url)

    return _transcribe_openai(client, path, api_key, model, language, api_url)


def _transcribe_openai(
    client: httpx.Client,
    path: Path,
    api_key: str,
    model: str,
    language: str | None,
    api_url: str,
) -> str:
    """Transcribe using OpenAI-compatible ``/audio/transcriptions`` APIs."""
    data: dict[str, str] = {"model": model}
    if language:
        data["language"] = language

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    for attempt in range(_MAX_RETRIES + 1):
        try:
            with open(path, "rb") as f:
                files = {"file": (path.name, f, "application/octet-stream")}
                request_kwargs: dict[str, Any] = {"data": data, "files": files}
                if headers:
                    request_kwargs["headers"] = headers
                response = client.post(api_url, **request_kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                _retry_backoff(path, attempt, f"{exc.__class__.__name__}")
                continue
            raise

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
            _retry_backoff(path, attempt, f"HTTP {response.status_code}")
            continue

        response.raise_for_status()
        return response.json()["text"]

    response.raise_for_status()
    return response.json()["text"]


def _transcribe_whisper_asr(
    client: httpx.Client,
    path: Path,
    api_key: str,
    language: str | None,
    api_url: str,
) -> str:
    """Transcribe using whisper-asr-webservice ``/asr`` API semantics."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    params: dict[str, str] = {
        "task": "transcribe",
        "output": "json",
    }
    if language:
        params["language"] = language

    for attempt in range(_MAX_RETRIES + 1):
        try:
            with open(path, "rb") as f:
                files = {"audio_file": (path.name, f, "application/octet-stream")}
                request_kwargs: dict[str, Any] = {"params": params, "files": files}
                if headers:
                    request_kwargs["headers"] = headers
                response = client.post(api_url, **request_kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < _MAX_RETRIES:
                _retry_backoff(path, attempt, f"{exc.__class__.__name__}")
                continue
            raise

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
            _retry_backoff(path, attempt, f"HTTP {response.status_code}")
            continue

        response.raise_for_status()
        return _extract_whisper_asr_text(response)

    response.raise_for_status()
    return _extract_whisper_asr_text(response)


def _extract_whisper_asr_text(response: httpx.Response) -> str:
    """Extract transcript text from whisper-asr-webservice response body."""
    payload: Any = None
    try:
        payload = response.json()
    except Exception:
        payload = None

    text = _extract_text_from_payload(payload)
    if text:
        return text

    raw_text = response.text
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()

    raise ValueError("Whisper ASR response did not include transcript text")


def _extract_text_from_payload(payload: Any) -> str | None:
    """Return transcript text from JSON payload, if present."""
    if isinstance(payload, str):
        text = payload.strip()
        return text or None

    if not isinstance(payload, dict):
        return None

    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    segments = payload.get("segments")
    if not isinstance(segments, list):
        return None

    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_text = segment.get("text") or segment.get("transcript")
        if isinstance(segment_text, str) and segment_text.strip():
            parts.append(segment_text.strip())

    return " ".join(parts) if parts else None
