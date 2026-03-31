"""Groq Whisper API client for speech-to-text transcription.

Provides :func:`transcribe_files` which sends audio files to the Groq Whisper
API and returns the concatenated transcript text.  Handles per-chunk errors
gracefully by inserting ``[transcription gap]`` markers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

__all__ = ["transcribe_files"]

logger = logging.getLogger(__name__)

GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_REQUEST_TIMEOUT = 120.0  # seconds per chunk


def transcribe_files(
    paths: list[Path],
    api_key: str,
    model: str = "whisper-large-v3",
    language: str | None = None,
    api_url: str = GROQ_WHISPER_URL,
) -> str:
    """Transcribe one or more audio files via the Groq Whisper API.

    Args:
        paths: Ordered list of audio file paths to transcribe.
        api_key: Groq API key (``Bearer`` token).
        model: Whisper model identifier (default ``"whisper-large-v3"``).
        language: Optional ISO 639-1 language hint (e.g. ``"en"``).
        api_url: Transcription API endpoint URL.  Defaults to the Groq
            Whisper endpoint (``GROQ_WHISPER_URL``).  Override to point at
            any OpenAI-compatible transcription API.

    Returns:
        Concatenated transcript text with chunks separated by spaces.
        Chunks that fail are represented as ``"[transcription gap]"``.

    Raises:
        ValueError: If *api_key* is empty or *paths* is empty.
    """
    if not api_key:
        raise ValueError("Groq API key is required for transcription")
    if not paths:
        raise ValueError("At least one audio file path is required")

    segments: list[str] = []

    with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
        for path in paths:
            try:
                text = _transcribe_single(client, path, api_key, model, language, api_url)
                segments.append(text)
            except Exception:
                logger.warning("Transcription failed for chunk %s", path.name, exc_info=True)
                segments.append("[transcription gap]")

    return " ".join(segments)


_MAX_RETRIES = 4
_INITIAL_BACKOFF = 2.0  # seconds


def _transcribe_single(
    client: httpx.Client,
    path: Path,
    api_key: str,
    model: str,
    language: str | None,
    api_url: str = GROQ_WHISPER_URL,
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
    import time

    headers = {"Authorization": f"Bearer {api_key}"}

    data: dict[str, str] = {"model": model}
    if language:
        data["language"] = language

    for attempt in range(_MAX_RETRIES + 1):
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "application/octet-stream")}
            response = client.post(
                api_url,
                headers=headers,
                data=data,
                files=files,
            )

        if response.status_code == 429 and attempt < _MAX_RETRIES:
            wait = _INITIAL_BACKOFF * (2 ** attempt)
            logger.info("Rate limited on %s — retrying in %.1fs (attempt %d/%d)",
                        path.name, wait, attempt + 1, _MAX_RETRIES)
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response.json()["text"]

    # Should not reach here, but satisfy type checker
    response.raise_for_status()
    return response.json()["text"]
