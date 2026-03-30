"""Audio extraction and chunking utilities for the transcribe tool.

Provides:
- :func:`_ensure_yt_dlp` — dynamic ``yt-dlp`` dependency installer
- :func:`_check_ffmpeg` — ffmpeg availability check
- :class:`AudioInfo` — metadata about a downloaded audio file
- :func:`download_audio` — download audio from a URL via yt-dlp
- :func:`chunk_if_needed` — split large audio files via ffmpeg
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AudioInfo",
    "download_audio",
    "chunk_if_needed",
]


# ---------------------------------------------------------------------------
# Dynamic dependency management
# ---------------------------------------------------------------------------


def _ensure_yt_dlp() -> Any:
    """Import ``yt_dlp``, installing it dynamically on first ``ImportError``.

    Returns:
        The ``yt_dlp`` module.

    Raises:
        RuntimeError: If ``pip install yt-dlp`` fails or the module still
            cannot be imported after installation.
    """
    try:
        import yt_dlp  # type: ignore[import-untyped]

        return yt_dlp
    except ImportError:
        pass

    # Attempt dynamic install — try uv first (project standard), fall back to pip
    installed = False
    for cmd in (
        ["uv", "pip", "install", "yt-dlp"],
        [sys.executable, "-m", "pip", "install", "yt-dlp"],
    ):
        try:
            subprocess.check_call(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            installed = True
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    if not installed:
        raise RuntimeError(
            "Failed to install yt-dlp. Please install it manually: uv pip install yt-dlp"
        )

    # Retry import after install — invalidate finder caches so Python sees
    # the newly-installed package without a process restart.
    importlib.invalidate_caches()
    try:
        import yt_dlp  # type: ignore[import-untyped]

        return yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp was installed but could not be imported. Please check your Python environment."
        ) from exc


# ---------------------------------------------------------------------------
# ffmpeg check
# ---------------------------------------------------------------------------


def _check_ffmpeg() -> bool:
    """Check whether ``ffmpeg`` is available on the system PATH.

    Returns:
        ``True`` if ``ffmpeg`` is found, ``False`` otherwise.
    """
    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# AudioInfo dataclass
# ---------------------------------------------------------------------------


@dataclass
class AudioInfo:
    """Metadata about a downloaded audio file.

    Attributes:
        path: Path to the downloaded audio file on disk.
        title: Title of the media (from yt-dlp metadata).
        duration: Duration in seconds.
        filesize: Size of the audio file in bytes.
    """

    path: Path
    title: str
    duration: float
    filesize: int


# ---------------------------------------------------------------------------
# Audio download
# ---------------------------------------------------------------------------


def download_audio(
    url: str,
    output_dir: Path,
    audio_format: str = "mp3",
) -> AudioInfo:
    """Download audio from *url* using yt-dlp and return an :class:`AudioInfo`.

    Uses ``yt_dlp.YoutubeDL`` as a Python library (not subprocess) to extract
    audio in the requested format.

    Args:
        url: Any URL supported by yt-dlp (YouTube, Vimeo, Twitter, etc.).
        output_dir: Directory to write the downloaded audio file into.
        audio_format: Target audio format (default ``"mp3"``).

    Returns:
        An :class:`AudioInfo` with the file path, title, duration, and size.

    Raises:
        RuntimeError: If yt-dlp is not available or the download fails.
    """
    yt_dlp = _ensure_yt_dlp()

    output_template = str(output_dir / "%(title).100s.%(ext)s")

    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info is None:
        raise RuntimeError(f"yt-dlp returned no info for URL: {url}")

    title = info.get("title", "Unknown")
    duration = float(info.get("duration", 0) or 0)

    # Find the downloaded audio file — yt-dlp may change the extension
    # after post-processing, so we search the output directory.
    audio_files = sorted(output_dir.glob(f"*.{audio_format}"))
    if not audio_files:
        # Fallback: look for any audio file
        audio_files = sorted(
            p
            for p in output_dir.iterdir()
            if p.is_file() and p.suffix in (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac")
        )
    if not audio_files:
        raise RuntimeError(f"yt-dlp download succeeded but no audio file found in {output_dir}")

    audio_path = audio_files[0]
    filesize = audio_path.stat().st_size

    return AudioInfo(
        path=audio_path,
        title=title,
        duration=duration,
        filesize=filesize,
    )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_if_needed(
    audio_path: Path,
    output_dir: Path,
    max_bytes: int = 24_000_000,
    chunk_secs: int = 600,
) -> list[Path]:
    """Split *audio_path* into chunks if it exceeds *max_bytes*.

    If the file is small enough, returns ``[audio_path]`` unchanged.
    Otherwise, uses ``ffmpeg`` segment mode to split the audio into
    chunks of *chunk_secs* seconds each.

    Args:
        audio_path: Path to the audio file.
        output_dir: Directory to write chunk files into.
        max_bytes: Maximum file size in bytes before chunking (default 24 MB).
        chunk_secs: Duration of each chunk in seconds (default 600).

    Returns:
        Sorted list of :class:`Path` objects — either ``[audio_path]`` or
        the chunk files.

    Raises:
        RuntimeError: If ffmpeg is not available but chunking is required.
    """
    if audio_path.stat().st_size <= max_bytes:
        return [audio_path]

    if not _check_ffmpeg():
        raise RuntimeError(
            "ffmpeg is required to split audio files larger than "
            f"{max_bytes} bytes but was not found on the system PATH. "
            "Please install ffmpeg."
        )

    ext = audio_path.suffix
    chunk_pattern = str(output_dir / f"chunk_%03d{ext}")

    subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(audio_path),
            "-f",
            "segment",
            "-segment_time",
            str(chunk_secs),
            "-c",
            "copy",
            chunk_pattern,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    chunks = sorted(output_dir.glob(f"chunk_*{ext}"))
    if not chunks:
        raise RuntimeError(f"ffmpeg segmentation produced no output files in {output_dir}")

    return chunks
