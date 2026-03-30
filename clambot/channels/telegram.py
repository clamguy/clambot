"""Telegram channel implementation using python-telegram-bot.

Long-polling based — no webhook or public IP needed.  Supports:
- Typing indicators (per correlation_id)
- Status message lifecycle (send → edit-in-place → delete)
- MarkdownV2 rendering with plain-text fallback
- Message chunking at 4096 chars
- Approval inline keyboards
- Callback query handling for approval flow
- SOCKS5 proxy via HTTPXRequest
- Source filtering via pipe-segment matching
- File upload: documents, photos, voice, audio, video saved to workspace upload/
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from clambot.bus.events import InboundMessage, OutboundMessage
from clambot.bus.queue import MessageBus
from clambot.channels.base import BaseChannel
from clambot.channels.telegram_utils import chunk_text, convert_to_markdownv2
from clambot.config.schema import TelegramConfig
from clambot.utils.tasks import tracked_task
from clambot.utils.text import sanitize_args_for_display

logger = logging.getLogger(__name__)

__all__ = ["TelegramChannel"]

BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("new", "Start a new conversation"),
    BotCommand("help", "Show available commands"),
]


# Default extensions when mime_type is unavailable or unmapped
_MEDIA_TYPE_DEFAULT_EXT: dict[str, str] = {
    "photo": ".jpg",
    "voice": ".ogg",
    "audio": ".mp3",
    "video": ".mp4",
    "file": "",
}


def _extension_for_media(media_type: str, mime_type: str | None, file_name: str | None) -> str:
    """Derive a file extension from *mime_type* or *media_type* defaults."""
    if file_name:
        ext = Path(file_name).suffix
        if ext:
            return ext
    if mime_type:
        ext = mimetypes.guess_extension(mime_type, strict=False)
        if ext:
            return ext
    return _MEDIA_TYPE_DEFAULT_EXT.get(media_type, "")


class TelegramChannel(BaseChannel):
    """Telegram channel using long polling.

    Manages typing indicators, status messages, approval keyboards,
    and message chunking with MarkdownV2 fallback.
    """

    name = "telegram"

    def __init__(
        self, config: TelegramConfig, bus: MessageBus, *, workspace: Path | None = None
    ) -> None:
        super().__init__(config, bus, workspace=workspace)
        self.config: TelegramConfig = config
        self._app: Application | None = None

        # Upload directory for incoming files
        self._upload_dir: Path | None = None
        if workspace is not None:
            self._upload_dir = Path(workspace) / "upload"
            self._upload_dir.mkdir(parents=True, exist_ok=True)

        # Typing indicator state: correlation_id → (stop_event, task)
        self._typing_indicators: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}

        # Status message tracking: correlation_id → message_id
        self._status_messages: dict[str, int] = {}
        self._status_done: set[str] = set()  # correlation_ids that are finished

        # Approval message tracking: approval_id → message_id
        self._approval_messages: dict[str, int] = {}
        # Short→full approval ID mapping (compact callback_data)
        self._short_to_full_approval_id: dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        """Build the Application, register handlers, start polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True

        # Build application with generous connection pool
        req = HTTPXRequest(
            connection_pool_size=16,
            pool_timeout=5.0,
            connect_timeout=30.0,
            read_timeout=30.0,
        )
        builder = (
            Application.builder().token(self.config.token).request(req).get_updates_request(req)
        )
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)

        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Command handlers
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("new", self._forward_command))
        self._app.add_handler(CommandHandler("help", self._on_help))

        # Callback query handler (approval buttons)
        self._app.add_handler(CallbackQueryHandler(self._on_callback_query))

        # Message handler (text, photos, documents, voice, audio, video)
        self._app.add_handler(
            MessageHandler(
                (
                    filters.TEXT
                    | filters.PHOTO
                    | filters.Document.ALL
                    | filters.VOICE
                    | filters.AUDIO
                    | filters.VIDEO
                )
                & ~filters.COMMAND,
                self._on_message,
            )
        )

        logger.info("Starting Telegram bot (polling mode)...")

        await self._app.initialize()
        await self._app.start()

        # Register bot commands menu
        try:
            await self._app.bot.set_my_commands(BOT_COMMANDS)
        except Exception as exc:
            logger.warning("Failed to register bot commands: %s", exc)

        # Start polling
        await self._app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop polling, cancel typing indicators, shut down."""
        self._running = False

        # Cancel all typing indicators
        for corr_id in list(self._typing_indicators):
            self._stop_typing_indicator(corr_id)

        if self._app:
            logger.info("Stopping Telegram bot...")
            try:
                await self._app.updater.stop()
            except Exception as exc:
                logger.debug("Error stopping Telegram updater: %s", exc)
            try:
                await self._app.stop()
            except Exception as exc:
                logger.debug("Error stopping Telegram app: %s", exc)
            try:
                await self._app.shutdown()
            except Exception as exc:
                logger.debug("Error shutting down Telegram app: %s", exc)
            self._app = None

    # ── Send dispatch ─────────────────────────────────────────

    async def send(self, outbound: OutboundMessage) -> None:
        """Route an outbound message by type."""
        if not self._app:
            logger.warning("Telegram bot not running, dropping message")
            return

        msg_type = outbound.type

        if msg_type == "text":
            await self._send_text(outbound)
        elif msg_type == "approval_pending":
            await self._send_approval_keyboard(outbound)
        elif msg_type == "secret_pending":
            await self._send_secret_request(outbound)
        elif msg_type == "status_update":
            await self._send_or_edit_status(outbound)
        elif msg_type == "status_delete":
            await self._delete_status(outbound)
        else:
            logger.warning("Unknown outbound type %r, falling back to text", msg_type)
            await self._send_text(outbound)

    # ── Text sending ──────────────────────────────────────────

    async def _send_text(self, outbound: OutboundMessage) -> None:
        """Send a text message, stopping typing and deleting status first."""
        assert self._app is not None

        self._stop_typing_indicator(outbound.correlation_id)
        await self._try_delete_status(outbound.correlation_id, chat_id_str=outbound.target)

        try:
            chat_id = int(outbound.target)
        except (ValueError, TypeError):
            logger.error("Invalid target (chat_id): %s", outbound.target)
            return

        if not outbound.content:
            return

        chunks = chunk_text(outbound.content, max_len=4096)
        for chunk in chunks:
            await self._send_chunk(chat_id, chunk, outbound.reply_to)

    async def _send_chunk(self, chat_id: int, text: str, reply_to: str | None = None) -> None:
        """Send a single text chunk, trying MarkdownV2 first then plain text."""
        assert self._app is not None

        reply_to_id = int(reply_to) if reply_to else None

        # Try MarkdownV2 first
        try:
            mdv2 = convert_to_markdownv2(text)
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=mdv2,
                parse_mode="MarkdownV2",
                reply_to_message_id=reply_to_id,
            )
            return
        except BadRequest:
            logger.debug("MarkdownV2 parse failed, falling back to plain text")
        except Exception as exc:
            logger.debug("MarkdownV2 send error: %s, falling back", exc)

        # Fallback to plain text
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_id,
            )
        except Exception as exc:
            logger.error("Failed to send plain text message: %s", exc)

    # ── Approval keyboard ─────────────────────────────────────

    async def _send_approval_keyboard(self, outbound: OutboundMessage) -> None:
        """Send an approval request with inline keyboard buttons."""
        assert self._app is not None

        self._stop_typing_indicator(outbound.correlation_id)
        await self._try_delete_status(outbound.correlation_id, chat_id_str=outbound.target)

        try:
            chat_id = int(outbound.target)
        except (ValueError, TypeError):
            logger.error("Invalid target for approval: %s", outbound.target)
            return

        metadata = outbound.metadata or {}
        approval_id = metadata.get("approval_id", "")
        tool_name = metadata.get("tool_name", "unknown")
        args = metadata.get("args", {})
        options = metadata.get("options", [])

        # Build buttons — callback_data must be ≤ 64 bytes (Telegram limit).
        # Use compact format: "a:<action_char>|id:<short_id>[|o:<opt_id>]"
        short_id = approval_id.replace("-", "")[:24]
        # Store the mapping so the callback handler can resolve the full id
        self._short_to_full_approval_id[short_id] = approval_id

        buttons: list[list[InlineKeyboardButton]] = []

        # "Allow Once" button
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Allow Once",
                    callback_data=f"a:o|id:{short_id}",
                )
            ]
        )

        # Per-tool scope options (if any)
        for idx, opt in enumerate(options):
            opt_id = opt.get("id", "") if isinstance(opt, dict) else getattr(opt, "id", "")
            opt_label = opt.get("label", "") if isinstance(opt, dict) else getattr(opt, "label", "")

            # Store the full opt_id in a mapping; use a short index
            # in callback_data to stay within the 64-byte limit.
            opt_key = f"{short_id}:{idx}"
            self._short_to_full_approval_id[f"opt:{opt_key}"] = opt_id

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"Always: {opt_label}",
                        callback_data=f"a:A|id:{short_id}|o:{idx}",
                    )
                ]
            )

        # "Reject" button
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Reject",
                    callback_data=f"a:r|id:{short_id}",
                )
            ]
        )

        keyboard = InlineKeyboardMarkup(buttons)

        # Format display text — strip query strings from URLs for readability
        display_args = sanitize_args_for_display(args) if args else {}
        args_display = json.dumps(display_args, indent=2, default=str) if display_args else "{}"
        display_text = f"Approval required for tool: {tool_name}\n\nArguments:\n{args_display}"

        try:
            sent = await self._app.bot.send_message(
                chat_id=chat_id,
                text=display_text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            self._approval_messages[approval_id] = sent.message_id
        except Exception as exc:
            logger.error("Failed to send approval keyboard: %s", exc)

    # ── Secret request ──────────────────────────────────────────

    async def _send_secret_request(self, outbound: OutboundMessage) -> None:
        """Send a secret-required message with usage instructions."""
        assert self._app is not None

        self._stop_typing_indicator(outbound.correlation_id)
        await self._try_delete_status(outbound.correlation_id, chat_id_str=outbound.target)

        try:
            chat_id = int(outbound.target)
        except (ValueError, TypeError):
            logger.error("Invalid target for secret request: %s", outbound.target)
            return

        metadata = outbound.metadata or {}
        missing = metadata.get("missing_secrets", [])
        names_str = ", ".join(missing) if missing else "unknown"

        text = (
            f"Secret{'s' if len(missing) != 1 else ''} required: {names_str}\n\n"
            f"Reply with the value, or use /secret {missing[0] if missing else 'NAME'} <value>"
        )

        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.error("Failed to send secret request: %s", exc)

    # ── Status messages ───────────────────────────────────────

    async def _send_or_edit_status(self, outbound: OutboundMessage) -> None:
        """Send a new status message or edit an existing one in place.

        Refuses to create new status messages after ``_delete_status``
        has been called for the same correlation_id — prevents late
        progress events from leaving orphaned "..." messages.
        """
        assert self._app is not None

        try:
            chat_id = int(outbound.target)
        except (ValueError, TypeError):
            return

        corr_id = outbound.correlation_id

        # Already finished — don't create a new status message
        if corr_id in self._status_done:
            return

        existing_msg_id = self._status_messages.get(corr_id)

        if existing_msg_id is not None:
            # Edit existing status message
            try:
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=existing_msg_id,
                    text="...",
                )
            except Exception as exc:
                logger.debug("Failed to edit status message (may have been deleted): %s", exc)
        else:
            # Send new status message
            try:
                sent = await self._app.bot.send_message(
                    chat_id=chat_id,
                    text="...",
                )
                self._status_messages[corr_id] = sent.message_id
            except Exception as exc:
                logger.debug("Failed to send status message: %s", exc)

    async def _delete_status(self, outbound: OutboundMessage) -> None:
        """Delete the status message for a correlation_id and mark as done."""
        self._status_done.add(outbound.correlation_id)
        await self._try_delete_status(outbound.correlation_id, chat_id_str=outbound.target)

    async def _try_delete_status(self, correlation_id: str, chat_id_str: str | None = None) -> None:
        """Delete a tracked status message if it exists."""
        self._status_done.add(correlation_id)
        msg_id = self._status_messages.pop(correlation_id, None)
        if msg_id is None or self._app is None:
            # Evict from done-set after a while to prevent unbounded growth
            if len(self._status_done) > 500:
                self._status_done.clear()
            return

        if chat_id_str is None:
            return

        try:
            await self._app.bot.delete_message(
                chat_id=int(chat_id_str),
                message_id=msg_id,
            )
        except Exception as exc:
            logger.debug("Failed to delete status message (already deleted or invalid): %s", exc)

    # ── Typing indicator ──────────────────────────────────────

    def _start_typing_indicator(self, correlation_id: str, chat_id: str) -> None:
        """Start a typing indicator loop for the given correlation."""
        # Stop any existing indicator for this correlation
        self._stop_typing_indicator(correlation_id)

        stop_event = asyncio.Event()
        task = tracked_task(self._typing_loop(stop_event, chat_id), name="typing-indicator")
        self._typing_indicators[correlation_id] = (stop_event, task)

    def _stop_typing_indicator(self, correlation_id: str) -> None:
        """Stop the typing indicator for a correlation."""
        entry = self._typing_indicators.pop(correlation_id, None)
        if entry is not None:
            stop_event, task = entry
            stop_event.set()
            if not task.done():
                task.cancel()

    async def _typing_loop(self, stop_event: asyncio.Event, chat_id: str) -> None:
        """Send 'typing' action every 4s until stop_event is set."""
        try:
            while not stop_event.is_set() and self._app:
                try:
                    await self._app.bot.send_chat_action(
                        chat_id=int(chat_id),
                        action="typing",
                    )
                except Exception as exc:
                    logger.debug("Failed to send typing action: %s", exc)
                # Wait 4 seconds or until stopped
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=4.0)
                    break  # stop_event was set
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    # ── Inbound handlers ──────────────────────────────────────

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text/photo/document/voice/audio/video messages."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = str(message.chat_id)
        source = self._build_source(user)

        # Build content parts
        content_parts: list[str] = []
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Correlation for this turn
        correlation_id = str(uuid.uuid4())

        # Start typing before processing
        self._start_typing_indicator(correlation_id, chat_id)

        # ── Download media attachments ────────────────────────
        media_paths: list[str] = []
        media_file, media_type, file_name = None, None, None

        if message.document:
            media_file = message.document
            media_type = "file"
            file_name = message.document.file_name
        elif message.photo:
            media_file = message.photo[-1]  # largest resolution
            media_type = "photo"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
            file_name = getattr(message.audio, "file_name", None)
        elif message.video:
            media_file = message.video
            media_type = "video"
            file_name = getattr(message.video, "file_name", None)

        if media_file and self._app and self._upload_dir:
            saved = await self._download_media(
                media_file,
                media_type or "file",
                file_name,
            )
            if saved:
                rel_path = f"upload/{saved.name}"
                media_paths.append(rel_path)
                content_parts.append(
                    f'[The user uploaded a file to the workspace at "{rel_path}". '
                    f'Read it with: fs({{operation: "read", path: "{rel_path}"}}).]'
                )

        content = "\n".join(content_parts) if content_parts else "[empty message]"

        metadata: dict[str, Any] = {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": message.chat.type != "private",
        }

        await self._handle_message(
            source=source,
            chat_id=chat_id,
            content=content,
            correlation_id=correlation_id,
            media=tuple(media_paths),
            metadata=metadata,
        )

    # ── Media download helper ─────────────────────────────────

    async def _download_media(
        self,
        media_file: Any,
        media_type: str,
        file_name: str | None,
    ) -> Path | None:
        """Download a Telegram media file to the workspace upload directory.

        Returns the saved :class:`~pathlib.Path` on success, ``None`` on failure.
        """
        if self._upload_dir is None or self._app is None:
            return None

        try:
            tg_file = await self._app.bot.get_file(media_file.file_id)

            # Determine filename
            mime_type = getattr(media_file, "mime_type", None)
            ext = _extension_for_media(media_type, mime_type, file_name)

            if file_name:
                safe_name = Path(file_name).name  # strip any directory components
            else:
                short_id = media_file.file_id[:12]
                safe_name = f"{short_id}{ext}"

            # Ensure uniqueness — append short uuid if collision
            dest = self._upload_dir / safe_name
            if dest.exists():
                stem = dest.stem
                suffix = dest.suffix
                unique = uuid.uuid4().hex[:6]
                safe_name = f"{stem}_{unique}{suffix}"
                dest = self._upload_dir / safe_name

            await tg_file.download_to_drive(str(dest))
            logger.info("Downloaded %s to %s", media_type, dest)
            return dest
        except Exception as exc:
            logger.error("Failed to download %s media: %s", media_type, exc)
            return None

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle approval inline keyboard callbacks."""
        query = update.callback_query
        if not query or not query.data:
            return

        # Answer immediately to clear Telegram's loading spinner
        await query.answer()

        # Parse compact callback format: "a:<action>|id:<short_id>[|o:<opt>]"
        raw = query.data or ""
        if raw.startswith("a:"):
            parts = dict(p.split(":", 1) for p in raw.split("|") if ":" in p)
            action_char = parts.get("a", "")
            action = {"o": "allow_once", "A": "allow_always", "r": "reject"}.get(
                action_char, action_char
            )
            short_id = parts.get("id", "")
            approval_id = self._short_to_full_approval_id.pop(short_id, short_id)
            raw_opt = parts.get("o", "")
            # Resolve option index back to full opt_id via mapping
            opt_key = f"opt:{short_id}:{raw_opt}"
            option_id = self._short_to_full_approval_id.pop(opt_key, raw_opt)
        else:
            # Legacy JSON format fallback
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return
            action = data.get("action", "")
            approval_id = data.get("approval_id", "")
            option_id = data.get("option_id", "")

        if not approval_id:
            return

        # Map action to decision and grant scope
        decision, grant_scope = self._parse_approval_action(action, option_id)

        # Build source/chat_id from callback
        user = update.effective_user
        chat_id = str(query.message.chat_id) if query.message else ""
        source = self._build_source(user) if user else ""

        # Reuse a fresh correlation_id for the /approve inbound.
        # No typing indicator — approval resolution is instant and the
        # original agent turn (still in-flight) provides its own status.
        correlation_id = str(uuid.uuid4())

        # Build metadata for the /approve command
        metadata: dict[str, Any] = {
            "approval_id": approval_id,
            "decision": decision,
            "grant_scope": grant_scope,
        }

        # Delete the approval message
        approval_msg_id = self._approval_messages.pop(approval_id, None)
        if approval_msg_id and query.message:
            try:
                await self._app.bot.delete_message(
                    chat_id=int(chat_id),
                    message_id=approval_msg_id,
                )
            except Exception as exc:
                logger.debug("Failed to delete approval keyboard message: %s", exc)

        # Emit as /approve inbound
        msg = InboundMessage(
            channel=self.name,
            source=source,
            chat_id=chat_id,
            content=f"/approve {approval_id}",
            correlation_id=correlation_id,
            metadata=metadata,
        )
        await self.bus.inbound.put(msg)

    # ── Command handlers ──────────────────────────────────────

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        await update.message.reply_text(
            f"Hi {user.first_name}! I'm ClamBot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        if not update.message:
            return
        await update.message.reply_text(
            "ClamBot commands:\n/new — Start a new conversation\n/help — Show available commands"
        )

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus (e.g. /new)."""
        if not update.message or not update.effective_user:
            return
        source = self._build_source(update.effective_user)
        chat_id = str(update.message.chat_id)
        correlation_id = str(uuid.uuid4())
        self._start_typing_indicator(correlation_id, chat_id)
        await self._handle_message(
            source=source,
            chat_id=chat_id,
            content=update.message.text or "",
            correlation_id=correlation_id,
        )

    # ── Error handler ─────────────────────────────────────────

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log handler/polling errors."""
        logger.error("Telegram error: %s", context.error)

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _build_source(user: Any) -> str:
        """Build ``"user_id|username"`` source string."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    @staticmethod
    def _parse_approval_action(action: str, option_id: str = "") -> tuple[str, str]:
        """Map callback action to ``(decision, grant_scope)``."""
        if action == "allow_once":
            return "ALLOW", ""
        if action == "allow_always":
            return "ALLOW", option_id or "always"
        if action == "reject":
            return "DENY", ""
        return "ALLOW", ""
