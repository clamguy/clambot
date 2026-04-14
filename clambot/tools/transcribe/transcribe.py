"""TranscribeTool — download audio from a URL and transcribe it to text.

Provides :class:`TranscribeTool`, a :class:`~clambot.tools.base.BuiltinTool`
subclass that orchestrates the full transcription pipeline:
URL → yt-dlp download → optional ffmpeg chunking → Whisper API → text.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from clambot.config.schema import TranscribeToolConfig
from clambot.tools.base import BuiltinTool, ToolApprovalOption
from clambot.tools.transcribe.audio import chunk_if_needed, download_audio
from clambot.tools.transcribe.whisper import transcribe_files

__all__ = ["TranscribeTool", "TranscribeToolConfig"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class TranscribeTool(BuiltinTool):
    """Built-in tool that downloads audio from a URL and transcribes it.

    Uses yt-dlp to extract audio, ffmpeg to chunk files exceeding 25 MB,
    and a configurable Whisper API for speech-to-text transcription.

    Args:
        config: Optional tool configuration.  Uses sensible defaults when
                ``None`` is passed.
        secret_store: Optional :class:`SecretStore` for resolving the
                ``GROQ_API_KEY`` secret used by OpenAI-compatible Whisper
                endpoints.
    """

    def __init__(
        self,
        config: TranscribeToolConfig | None = None,
        secret_store: Any | None = None,
    ) -> None:
        self._config = config or TranscribeToolConfig()
        self._secret_store = secret_store

    # ------------------------------------------------------------------
    # BuiltinTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Registered tool name: ``"transcribe"``."""
        return "transcribe"

    @property
    def description(self) -> str:
        """Human-readable description surfaced to the LLM."""
        return "Download audio from a URL and transcribe it to text using Whisper."

    @property
    def usage_instructions(self) -> list[str]:
        """Prompt guidance for generation-time transcribe usage."""
        return [
            "Use for media URLs (YouTube/Vimeo/etc.) when speech transcript text is needed.",
            "Input is {url, language?}; language is an optional ISO 639-1 hint.",
            "Check result.error before reading result.transcript.",
            "Return result.transcript text for downstream summarize/translate steps.",
        ]

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the ``transcribe`` tool parameters."""
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "URL to transcribe (any yt-dlp supported site: "
                        "YouTube, Vimeo, Twitter, etc.)."
                    ),
                },
                "language": {
                    "type": "string",
                    "description": ("ISO 639-1 language hint (e.g. 'en', 'es'). Optional."),
                },
            },
            "required": ["url"],
        }

    @property
    def returns(self) -> dict[str, Any]:
        """Return value schema for ``transcribe``."""
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Original URL.",
                },
                "title": {
                    "type": "string",
                    "description": "Media title.",
                },
                "duration_seconds": {
                    "type": "number",
                    "description": "Audio duration in seconds.",
                },
                "transcript": {
                    "type": "string",
                    "description": "Full transcription text.",
                },
                "chunk_count": {
                    "type": "integer",
                    "description": "Number of chunks transcribed.",
                },
                "error": {
                    "type": "string",
                    "description": "Error message on failure.",
                },
            },
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute the full transcription pipeline.

        1. Resolve optional API key from secret store/env.
        2. Download audio via yt-dlp.
        3. Guard: check duration against configured maximum.
        4. Chunk large files via ffmpeg if needed.
        5. Transcribe all chunks via configured Whisper endpoint.
        6. Return result dict with url, title, duration, transcript, chunk_count.

        All exceptions are caught and returned as ``{"error": "..."}`` dicts.
        """
        url = args.get("url", "")
        language = args.get("language")

        # Resolve API key (used by OpenAI-compatible endpoints).
        api_key = ""
        if self._secret_store is not None:
            api_key = self._secret_store.get("GROQ_API_KEY") or ""
        if not api_key:
            api_key = os.environ.get("GROQ_API_KEY", "")

        if self._config.whisper_api_style == "openai" and not api_key:
            return {
                "error": "Secret 'GROQ_API_KEY' not found",
            }

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)

                # Download audio
                audio_info = download_audio(url, tmp_path, self._config.audio_format)

                # Duration guard
                if audio_info.duration > self._config.max_duration_seconds:
                    return {
                        "error": (
                            f"Audio duration ({audio_info.duration:.0f}s) "
                            f"exceeds maximum allowed "
                            f"({self._config.max_duration_seconds}s)."
                        ),
                        "url": url,
                        "title": audio_info.title,
                        "duration_seconds": audio_info.duration,
                    }

                # Chunk if needed
                chunks = chunk_if_needed(
                    audio_info.path,
                    tmp_path,
                    chunk_secs=self._config.chunk_duration_seconds,
                )

                # Transcribe
                transcript = transcribe_files(
                    chunks,
                    api_key,
                    model=self._config.whisper_model,
                    language=language,
                    api_url=self._config.whisper_api_url,
                    api_style=self._config.whisper_api_style,
                    request_timeout=self._config.whisper_request_timeout_seconds,
                )

                return {
                    "url": url,
                    "title": audio_info.title,
                    "duration_seconds": audio_info.duration,
                    "transcript": transcript,
                    "chunk_count": len(chunks),
                }

        except Exception as exc:
            logger.exception("Transcribe tool failed for URL: %s", url)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return approval scope options scoped to the URL hostname.

        Args:
            args: The tool argument dict containing at least ``"url"``.

        Returns:
            List with a single ``host:<domain>`` approval option, or empty
            if the hostname cannot be parsed.
        """
        url = args.get("url", "")
        parsed = urlparse(url)
        options: list[ToolApprovalOption] = []

        if parsed.hostname:
            options.append(
                ToolApprovalOption(
                    id=f"host:{parsed.hostname}",
                    label=f"Allow Always: host {parsed.hostname}",
                    scope=f"host:{parsed.hostname}",
                )
            )

        return options
