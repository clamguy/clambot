"""Compatibility checker — validates clam language requirements.

Only JavaScript clams are supported by the amla-sandbox WASM runtime.
"""

from __future__ import annotations

from typing import Any

from .errors import INCOMPATIBLE_LANGUAGE, ClamErrorPayload, ClamErrorStage


class CompatibilityChecker:
    """Check whether a clam is compatible with the runtime.

    Currently only JavaScript is supported.
    """

    SUPPORTED_LANGUAGES = ("javascript", "js")

    def check(self, clam: Any) -> ClamErrorPayload | None:
        """Validate clam compatibility.

        Args:
            clam: A clam object or dict with a ``language`` attribute or key.

        Returns:
            ``ClamErrorPayload`` if the clam is incompatible, ``None`` if OK.
        """
        language = self._extract_language(clam)

        if language.lower() not in self.SUPPORTED_LANGUAGES:
            return ClamErrorPayload(
                code=INCOMPATIBLE_LANGUAGE,
                stage=ClamErrorStage.COMPATIBILITY,
                message=f"Unsupported language: '{language}'. Only JavaScript is supported.",
                detail={"language": language, "supported": list(self.SUPPORTED_LANGUAGES)},
                user_message=f"This clam requires '{language}' but only JavaScript is supported.",
            )

        return None

    @staticmethod
    def _extract_language(clam: Any) -> str:
        """Extract the language field from a clam object or dict."""
        if isinstance(clam, dict):
            lang = clam.get("language", "")
            if lang:
                return str(lang)
            # Fallback to metadata.language for dicts
            metadata = clam.get("metadata")
            if isinstance(metadata, dict):
                return str(metadata.get("language", ""))
            return ""

        # Try attribute access (dataclass / object)
        language = getattr(clam, "language", None)
        if language is not None:
            return str(language)

        # Try metadata dict
        metadata = getattr(clam, "metadata", None)
        if isinstance(metadata, dict):
            return str(metadata.get("language", ""))

        return ""
