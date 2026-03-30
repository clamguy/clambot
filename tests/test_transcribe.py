"""Tests for the transcribe tool — Phase 1, 2 & 3: Audio + Whisper + Tool."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from clambot.config.schema import TranscribeToolConfig
from clambot.tools.transcribe import TranscribeTool
from clambot.tools.transcribe.audio import (
    AudioInfo,
    _check_ffmpeg,
    _ensure_yt_dlp,
    chunk_if_needed,
)
from clambot.tools.transcribe.whisper import transcribe_files

# ---------------------------------------------------------------------------
# _ensure_yt_dlp tests
# ---------------------------------------------------------------------------


class TestEnsureYtDlp:
    """Tests for dynamic yt-dlp dependency management."""

    def test_ensure_yt_dlp_already_installed(self) -> None:
        """When yt_dlp is importable, no subprocess call is made."""
        mock_module = MagicMock()
        with patch.dict("sys.modules", {"yt_dlp": mock_module}):
            result = _ensure_yt_dlp()
        assert result is mock_module

    def test_ensure_yt_dlp_installs_on_import_error(self) -> None:
        """When yt_dlp is not importable, uv/pip install is called and import retried."""
        import builtins

        mock_module = MagicMock()
        import_count = 0
        _real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            nonlocal import_count
            if name == "yt_dlp":
                import_count += 1
                if import_count == 1:
                    raise ImportError("no module named yt_dlp")
                return mock_module
            return _real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=fake_import),
            patch("subprocess.check_call") as mock_subprocess,
        ):
            result = _ensure_yt_dlp()

        mock_subprocess.assert_called_once()
        assert "yt-dlp" in mock_subprocess.call_args[0][0]
        assert result is mock_module

    def test_ensure_yt_dlp_all_installers_fail_raises(self) -> None:
        """When all install methods fail, RuntimeError is raised."""
        import builtins
        import subprocess as sp

        _real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yt_dlp":
                raise ImportError("no module named yt_dlp")
            return _real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=fake_import),
            patch(
                "subprocess.check_call",
                side_effect=sp.CalledProcessError(1, "cmd"),
            ),
            pytest.raises(RuntimeError, match="Failed to install yt-dlp"),
        ):
            _ensure_yt_dlp()


# ---------------------------------------------------------------------------
# _check_ffmpeg tests
# ---------------------------------------------------------------------------


class TestCheckFfmpeg:
    """Tests for ffmpeg availability check."""

    def test_check_ffmpeg_found(self) -> None:
        """Returns True when ffmpeg is on PATH."""
        with patch("clambot.tools.transcribe.audio.shutil.which", return_value="/usr/bin/ffmpeg"):
            assert _check_ffmpeg() is True

    def test_check_ffmpeg_missing(self) -> None:
        """Returns False when ffmpeg is not on PATH."""
        with patch("clambot.tools.transcribe.audio.shutil.which", return_value=None):
            assert _check_ffmpeg() is False


# ---------------------------------------------------------------------------
# chunk_if_needed tests
# ---------------------------------------------------------------------------


class TestChunkIfNeeded:
    """Tests for audio file chunking logic."""

    def test_chunk_if_needed_small_file_returns_single(self, tmp_path: Path) -> None:
        """File under max_bytes is returned as a single-element list."""
        audio = tmp_path / "small.mp3"
        audio.write_bytes(b"x" * 1000)  # 1 KB — well under default 24 MB
        result = chunk_if_needed(audio, tmp_path)
        assert result == [audio]

    def test_chunk_if_needed_calls_ffmpeg(self, tmp_path: Path) -> None:
        """File over max_bytes triggers ffmpeg segment split."""
        audio = tmp_path / "large.mp3"
        audio.write_bytes(b"x" * 100)  # 100 bytes

        # Create fake chunk output files before run is called
        chunk_dir = tmp_path / "chunks"
        chunk_dir.mkdir()
        (chunk_dir / "chunk_000.mp3").write_bytes(b"a" * 50)
        (chunk_dir / "chunk_001.mp3").write_bytes(b"a" * 50)

        with (
            patch("clambot.tools.transcribe.audio._check_ffmpeg", return_value=True),
            patch("clambot.tools.transcribe.audio.subprocess.run") as mock_run,
        ):
            # Use chunk_dir as output so the glob finds the pre-created files
            result = chunk_if_needed(audio, chunk_dir, max_bytes=50, chunk_secs=300)

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "ffmpeg" in call_args[0][0]
        assert "-segment_time" in call_args[0][0]
        assert "300" in call_args[0][0]
        assert len(result) == 2

    def test_chunk_if_needed_no_ffmpeg_raises(self, tmp_path: Path) -> None:
        """Large file without ffmpeg raises RuntimeError."""
        audio = tmp_path / "large.mp3"
        audio.write_bytes(b"x" * 100)

        with patch("clambot.tools.transcribe.audio._check_ffmpeg", return_value=False):
            with pytest.raises(RuntimeError, match="ffmpeg is required"):
                chunk_if_needed(audio, tmp_path, max_bytes=50)

    def test_chunk_if_needed_exact_boundary(self, tmp_path: Path) -> None:
        """File exactly at max_bytes is returned without chunking."""
        audio = tmp_path / "exact.mp3"
        audio.write_bytes(b"x" * 100)
        result = chunk_if_needed(audio, tmp_path, max_bytes=100)
        assert result == [audio]


# ---------------------------------------------------------------------------
# AudioInfo dataclass tests
# ---------------------------------------------------------------------------


class TestAudioInfo:
    """Tests for the AudioInfo dataclass."""

    def test_audio_info_fields(self, tmp_path: Path) -> None:
        """AudioInfo stores all required fields."""
        p = tmp_path / "test.mp3"
        p.write_bytes(b"data")
        info = AudioInfo(path=p, title="Test Title", duration=120.5, filesize=4)
        assert info.path == p
        assert info.title == "Test Title"
        assert info.duration == 120.5
        assert info.filesize == 4


# ---------------------------------------------------------------------------
# Phase 2: Whisper Transcription Client tests
# ---------------------------------------------------------------------------


class TestTranscribeFiles:
    """Tests for the Groq Whisper transcription client."""

    def test_transcribe_single_file(self, tmp_path: Path) -> None:
        """Single file is transcribed with correct POST multipart fields."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio data")

        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "Hello world"}
        mock_response.raise_for_status = MagicMock()

        with patch("clambot.tools.transcribe.whisper.httpx.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            MockClient.return_value = mock_client

            result = transcribe_files([audio], api_key="test-key", model="whisper-large-v3")

        assert result == "Hello world"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        # Verify URL
        assert call_kwargs[0][0] == "https://api.groq.com/openai/v1/audio/transcriptions"
        # Verify auth header
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer test-key"
        # Verify model in data
        assert call_kwargs[1]["data"]["model"] == "whisper-large-v3"

    def test_transcribe_multiple_chunks_concatenated(self, tmp_path: Path) -> None:
        """Two chunks are transcribed and joined with space separator."""
        chunk1 = tmp_path / "chunk_000.mp3"
        chunk2 = tmp_path / "chunk_001.mp3"
        chunk1.write_bytes(b"audio1")
        chunk2.write_bytes(b"audio2")

        responses = [
            MagicMock(
                json=MagicMock(return_value={"text": "First part"}), raise_for_status=MagicMock()
            ),
            MagicMock(
                json=MagicMock(return_value={"text": "Second part"}), raise_for_status=MagicMock()
            ),
        ]

        with patch("clambot.tools.transcribe.whisper.httpx.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.post.side_effect = responses
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            MockClient.return_value = mock_client

            result = transcribe_files([chunk1, chunk2], api_key="test-key")

        assert result == "First part Second part"
        assert mock_client.post.call_count == 2

    def test_transcribe_chunk_error_continues(self, tmp_path: Path) -> None:
        """Failed chunk inserts gap marker; remaining chunks still processed."""
        chunk1 = tmp_path / "chunk_000.mp3"
        chunk2 = tmp_path / "chunk_001.mp3"
        chunk1.write_bytes(b"audio1")
        chunk2.write_bytes(b"audio2")

        ok_response = MagicMock(
            json=MagicMock(return_value={"text": "Good chunk"}),
            raise_for_status=MagicMock(),
        )

        with patch("clambot.tools.transcribe.whisper.httpx.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.post.side_effect = [
                httpx.HTTPStatusError("Server error", request=MagicMock(), response=MagicMock()),
                ok_response,
            ]
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            MockClient.return_value = mock_client

            result = transcribe_files([chunk1, chunk2], api_key="test-key")

        assert "[transcription gap]" in result
        assert "Good chunk" in result
        assert result == "[transcription gap] Good chunk"

    def test_transcribe_missing_api_key(self, tmp_path: Path) -> None:
        """Empty API key raises ValueError."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"audio")

        with pytest.raises(ValueError, match="Groq API key is required"):
            transcribe_files([audio], api_key="")

    def test_transcribe_empty_paths_raises(self) -> None:
        """Empty paths list raises ValueError."""
        with pytest.raises(ValueError, match="At least one audio file"):
            transcribe_files([], api_key="test-key")

    def test_transcribe_with_language_hint(self, tmp_path: Path) -> None:
        """Language parameter is included in POST data when provided."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"audio data")

        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "Hola mundo"}
        mock_response.raise_for_status = MagicMock()

        with patch("clambot.tools.transcribe.whisper.httpx.Client") as MockClient:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            MockClient.return_value = mock_client

            result = transcribe_files([audio], api_key="key", language="es")

        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["data"]["language"] == "es"
        assert result == "Hola mundo"


# ---------------------------------------------------------------------------
# Phase 3: TranscribeTool tests
# ---------------------------------------------------------------------------


class TestTranscribeTool:
    """Tests for the TranscribeTool BuiltinTool implementation."""

    def test_transcribe_tool_name_and_schema(self) -> None:
        """Tool name is 'transcribe' and schema has 'url' required."""
        tool = TranscribeTool()
        assert tool.name == "transcribe"
        assert tool.schema["type"] == "object"
        assert "url" in tool.schema["properties"]
        assert "url" in tool.schema["required"]

    def test_transcribe_tool_to_schema_openai_format(self) -> None:
        """to_schema() returns valid OpenAI function-call format."""
        tool = TranscribeTool()
        schema = tool.to_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "transcribe"
        assert "parameters" in schema["function"]
        assert "returns" in schema["function"]

    def test_transcribe_tool_returns_schema_defined(self) -> None:
        """returns property is non-empty and has expected fields."""
        tool = TranscribeTool()
        returns = tool.returns
        assert returns
        assert "properties" in returns
        props = returns["properties"]
        assert "url" in props
        assert "title" in props
        assert "duration_seconds" in props
        assert "transcript" in props
        assert "chunk_count" in props
        assert "error" in props

    def test_transcribe_execute_no_api_key(self) -> None:
        """Returns error matching Secret '<name>' not found pattern for interactive prompting."""
        tool = TranscribeTool()
        env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            result = tool.execute({"url": "https://youtube.com/watch?v=test"})
        assert "error" in result
        assert result["error"] == "Secret 'GROQ_API_KEY' not found"

    def test_transcribe_execute_api_key_from_secret_store(self, tmp_path: Path) -> None:
        """API key resolved from SecretStore takes priority over env var."""
        mock_store = MagicMock()
        mock_store.get.return_value = "store-key"

        tool = TranscribeTool(secret_store=mock_store)

        mock_audio_info = AudioInfo(
            path=tmp_path / "test.mp3",
            title="Store Key Test",
            duration=60.0,
            filesize=1000,
        )

        env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "clambot.tools.transcribe.transcribe.download_audio",
                return_value=mock_audio_info,
            ),
            patch(
                "clambot.tools.transcribe.transcribe.chunk_if_needed",
                return_value=[tmp_path / "test.mp3"],
            ),
            patch(
                "clambot.tools.transcribe.transcribe.transcribe_files",
                return_value="Transcribed via store key",
            ) as mock_transcribe,
        ):
            result = tool.execute({"url": "https://youtube.com/watch?v=test"})

        mock_store.get.assert_called_with("GROQ_API_KEY")
        mock_transcribe.assert_called_once()
        # Verify the store key was passed to transcribe_files
        assert mock_transcribe.call_args[0][1] == "store-key"
        assert result["transcript"] == "Transcribed via store key"

    def test_transcribe_execute_success(self, tmp_path: Path) -> None:
        """Full pipeline with mocked internals produces expected result dict."""
        tool = TranscribeTool()

        mock_audio_info = AudioInfo(
            path=tmp_path / "test.mp3",
            title="Test Video",
            duration=120.0,
            filesize=1000,
        )

        with (
            patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}),
            patch(
                "clambot.tools.transcribe.transcribe.download_audio",
                return_value=mock_audio_info,
            ),
            patch(
                "clambot.tools.transcribe.transcribe.chunk_if_needed",
                return_value=[tmp_path / "test.mp3"],
            ),
            patch(
                "clambot.tools.transcribe.transcribe.transcribe_files",
                return_value="Hello world transcript",
            ),
        ):
            result = tool.execute({"url": "https://youtube.com/watch?v=test"})

        assert result["url"] == "https://youtube.com/watch?v=test"
        assert result["title"] == "Test Video"
        assert result["duration_seconds"] == 120.0
        assert result["transcript"] == "Hello world transcript"
        assert result["chunk_count"] == 1

    def test_transcribe_execute_duration_exceeded(self, tmp_path: Path) -> None:
        """Rejects audio exceeding max_duration_seconds."""
        config = TranscribeToolConfig(max_duration_seconds=60)
        tool = TranscribeTool(config=config)

        mock_audio_info = AudioInfo(
            path=tmp_path / "long.mp3",
            title="Long Video",
            duration=3600.0,
            filesize=100_000,
        )

        with (
            patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}),
            patch(
                "clambot.tools.transcribe.transcribe.download_audio",
                return_value=mock_audio_info,
            ),
        ):
            result = tool.execute({"url": "https://youtube.com/watch?v=long"})

        assert "error" in result
        assert "exceeds" in result["error"]
        assert result["duration_seconds"] == 3600.0

    def test_transcribe_approval_options_has_host(self) -> None:
        """Approval options contain host:<domain> scope."""
        tool = TranscribeTool()
        options = tool.get_approval_options({"url": "https://www.youtube.com/watch?v=test"})
        assert len(options) >= 1
        assert any("host:" in opt.scope for opt in options)
        assert any("www.youtube.com" in opt.scope for opt in options)


# ---------------------------------------------------------------------------
# Phase 5: Integration Validation tests
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory for registry tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "memory").mkdir()
    return ws


class TestTranscribeIntegration:
    """Phase 5: Integration tests for transcribe tool dispatch and schema via registry."""

    def test_transcribe_tool_dispatch_via_registry(self, tmp_path: Path) -> None:
        """Build registry, dispatch 'transcribe' with mocked internals, verify result dict shape."""
        from clambot.tools import build_tool_registry

        ws = _make_workspace(tmp_path)
        registry = build_tool_registry(workspace=ws)

        mock_audio_info = AudioInfo(
            path=tmp_path / "test.mp3",
            title="Integration Test Video",
            duration=90.0,
            filesize=5000,
        )

        with (
            patch.dict(os.environ, {"GROQ_API_KEY": "test-key-integration"}),
            patch(
                "clambot.tools.transcribe.transcribe.download_audio",
                return_value=mock_audio_info,
            ),
            patch(
                "clambot.tools.transcribe.transcribe.chunk_if_needed",
                return_value=[tmp_path / "test.mp3"],
            ),
            patch(
                "clambot.tools.transcribe.transcribe.transcribe_files",
                return_value="Integration test transcript text",
            ),
        ):
            result = registry.dispatch("transcribe", {"url": "https://example.com/video"})

        # Verify result is a dict with all expected keys
        assert isinstance(result, dict)
        assert "url" in result
        assert "title" in result
        assert "duration_seconds" in result
        assert "transcript" in result
        assert "chunk_count" in result
        # No error key on success
        assert "error" not in result
        # Verify values match mocked data
        assert result["url"] == "https://example.com/video"
        assert result["title"] == "Integration Test Video"
        assert result["duration_seconds"] == 90.0
        assert result["transcript"] == "Integration test transcript text"
        assert result["chunk_count"] == 1

    def test_transcribe_tool_schema_in_registry_schemas(self, tmp_path: Path) -> None:
        """get_schemas() includes transcribe tool in OpenAI function-call format."""
        from clambot.tools import build_tool_registry

        ws = _make_workspace(tmp_path)
        registry = build_tool_registry(workspace=ws)
        schemas = registry.get_schemas()

        # Find the transcribe schema among all tool schemas
        transcribe_schema = None
        for schema in schemas:
            if schema.get("function", {}).get("name") == "transcribe":
                transcribe_schema = schema
                break

        assert transcribe_schema is not None, "transcribe tool not found in get_schemas()"

        # Verify OpenAI function-call format structure
        assert transcribe_schema["type"] == "function"
        func = transcribe_schema["function"]
        assert func["name"] == "transcribe"
        assert "description" in func
        assert len(func["description"]) > 0

        # Verify parameters schema
        assert "parameters" in func
        params = func["parameters"]
        assert params["type"] == "object"
        assert "url" in params["properties"]
        assert "url" in params.get("required", [])

        # Verify returns schema is included
        assert "returns" in func
        returns = func["returns"]
        assert "properties" in returns
        assert "transcript" in returns["properties"]
