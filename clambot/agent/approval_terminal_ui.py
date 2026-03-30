"""Terminal approval UI using questionary for interactive tool approval prompts."""

from __future__ import annotations

import json
from typing import Any

from clambot.tools.base import ToolApprovalOption
from clambot.utils.text import sanitize_args_for_display


class TerminalApprovalUI:
    """Terminal-based approval UI that prompts users via questionary.

    Displays tool call details and offers scope-based approval options.
    """

    def prompt(
        self,
        tool_name: str,
        args: dict[str, Any],
        options: list[ToolApprovalOption],
    ) -> tuple[str, str]:
        """Prompt the user for an approval decision.

        Args:
            tool_name: Name of the tool requesting approval.
            args: Arguments the tool will be called with.
            options: Available approval scope options from the tool.

        Returns:
            Tuple of (decision, scope) where decision is "ALLOW" or "DENY"
            and scope is "" for one-time, "always" for persistent, or a
            tool-specific scope ID.
        """
        try:
            import questionary
        except ImportError:
            # Fallback if questionary not installed — deny by default
            return ("DENY", "")

        # Format args for display — strip query strings from URLs for readability
        display_args = sanitize_args_for_display(args)
        args_display = json.dumps(display_args, indent=2, ensure_ascii=False)

        print("\n--- Tool Approval Required ---")
        print(f"Tool: {tool_name}")
        print(f"Args: {args_display}")
        print()

        # Build choices
        choices = [
            questionary.Choice("Allow Once", value=("ALLOW", "")),
        ]

        for opt in options:
            choices.append(
                questionary.Choice(
                    f"Allow Always: {opt.label}",
                    value=("ALLOW", opt.id),
                )
            )

        choices.append(questionary.Choice("Reject", value=("DENY", "")))

        result = questionary.select(
            "Choose an action:",
            choices=choices,
        ).ask()

        if result is None:
            # User pressed Ctrl+C
            return ("DENY", "")

        return result
