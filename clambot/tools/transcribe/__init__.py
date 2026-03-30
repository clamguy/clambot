"""Transcribe tool — audio extraction and speech-to-text transcription.

This package provides the :class:`TranscribeTool` built-in tool that downloads
audio from any yt-dlp-supported URL, optionally chunks large files via ffmpeg,
and transcribes them using Groq's Whisper API.
"""

from __future__ import annotations

from clambot.tools.transcribe.transcribe import TranscribeTool

__all__: list[str] = ["TranscribeTool"]
