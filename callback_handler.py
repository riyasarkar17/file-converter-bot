"""
Callback query handler — processes inline keyboard button presses.

When the user taps a conversion button the bot:
  1. Acknowledges the callback immediately (removes loading spinner).
  2. Sends a "processing…" message.
  3. Downloads the file from Telegram.
  4. Runs the conversion via ConversionService.
  5. Sends the resulting file back or reports an error.
  6. Updates / deletes the progress message.
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

from bot.database import repository
from bot.services import CONVERSION_LABELS, ConversionType, conversion_service
from bot.services.file_service import file_service
from bot.utils.file_utils import release_file
from bot.utils.validators import EXT_TO_MIME, resolve_mime

logger = logging.getLogger(__name__)

# Keys matching those set in file_handler.py
_KEY_FILE_ID = "pending_file_id"
_KEY_FILE_NAME = "pending_file_name"
_KEY_FILE_MIME = "pending_file_mime"
_KEY_FILE_SIZE = "pending_file_size"

# Conversion type → output extension
OUTPUT_EXTENSIONS: dict[str, str] = {
    "img_to_jpg": ".jpg",
    "img_to_png": ".png",
    "img_to_webp": ".webp",
    "img_to_pdf": ".pdf",
    "img_resize": ".png",
    "img_compress": ".jpg",
    "txt_to_pdf": ".pdf",
    "pdf_to_images": ".zip",
}

# Input file extension by MIME
MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
}


async def handle_conversion_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle a conversion button press."""
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    await query.answer()  # dismiss the loading spinner on the button

    data = query.data or ""
    if not data.startswith("convert:"):
        return

    conversion_key = data.split(":", 1)[1]

    # ── Recover pending file metadata ─────────────────────────────────────
    file_id: str | None = context.user_data.get(_KEY_FILE_ID)
    file_name: str = context.user_data.get(_KEY_FILE_NAME, "file")
    mime_type: str = context.user_data.get(_KEY_FILE_MIME, "")
    file_size: int = context.user_data.get(_KEY_FILE_SIZE, 0)

    if not file_id or not mime_type:
        await query.edit_message_text(
            "⚠️ Session expired. Please re-send your file."
        )
        return

    # ── Resolve conversion type ───────────────────────────────────────────
    try:
        ctype = ConversionType(conversion_key)
    except ValueError:
        await query.edit_message_text("❓ Unknown conversion. Please try again.")
        return

    label = CONVERSION_LABELS.get(ctype, conversion_key)

    # ── Send progress message ─────────────────────────────────────────────
    progress_msg = await query.edit_message_text(
        f"⚙️ *Converting* `{file_name}` *→* {label}…\nThis may take a moment.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # ── Create DB log entry ───────────────────────────────────────────────
    log_id = await repository.create_conversion_log(
        user_id=user.id,
        original_filename=file_name,
        original_mime=mime_type,
        original_size_bytes=file_size,
        conversion_type=ctype.value,
    )

    # ── Download file ─────────────────────────────────────────────────────
    input_suffix = MIME_TO_EXT.get(mime_type, Path(file_name).suffix or ".bin")
    output_suffix = OUTPUT_EXTENSIONS.get(ctype.value, ".bin")

    input_path: Path | None = None
    output_path: Path | None = None

    try:
        tg_file = await context.bot.get_file(file_id)
        input_path = await file_service.download(
            tg_file=tg_file,
            suffix=input_suffix,
            user_id=user.id,
        )

        # Build output path next to the input
        output_path = input_path.with_name(
            input_path.stem + "_converted" + output_suffix
        )

        # ── Run conversion ────────────────────────────────────────────────
        result = await conversion_service.convert(
            conversion_type=ctype,
            input_path=input_path,
            output_path=output_path,
            user_id=user.id,
            log_id=log_id,
            original_filename=file_name,
            original_size=file_size,
            mime_type=mime_type,
        )

        # ── Handle result ─────────────────────────────────────────────────
        if result.success:
            final_path = result.output_path
            if final_path and final_path.exists():
                caption = _build_success_caption(
                    file_name, label, result.duration_seconds, result.extra_info
                )
                await _send_file(
                    context=context,
                    chat_id=user.id,
                    file_path=final_path,
                    caption=caption,
                )
                await progress_msg.edit_text(
                    f"✅ Conversion complete!\n{caption}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                release_file(final_path)
            else:
                await progress_msg.edit_text(
                    "❌ Conversion succeeded but the output file was not found."
                )
        else:
            await progress_msg.edit_text(
                f"❌ *Conversion failed*\n\n`{result.error_message}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    except TelegramError as exc:
        logger.error("Telegram error during conversion: %s", exc)
        await _safe_edit(progress_msg, f"❌ Telegram error: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in callback handler: %s", exc)
        await _safe_edit(
            progress_msg,
            "❌ An unexpected error occurred. Please try again later.",
        )
    finally:
        # Belt-and-suspenders cleanup of any leftover temp files
        if input_path:
            release_file(input_path)
        if output_path:
            release_file(output_path)

        # Clear pending state
        for key in (_KEY_FILE_ID, _KEY_FILE_NAME, _KEY_FILE_MIME, _KEY_FILE_SIZE):
            context.user_data.pop(key, None)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_success_caption(
    original: str,
    label: str,
    duration: float,
    extra: str,
) -> str:
    parts = [f"🎉 *{original}* → {label}"]
    if extra:
        parts.append(extra)
    parts.append(f"⏱ {duration:.1f}s")
    return "\n".join(parts)


async def _send_file(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_path: Path,
    caption: str,
) -> None:
    """Send the converted file to the user, choosing the right method."""
    suffix = file_path.suffix.lower()
    with open(file_path, "rb") as fh:
        if suffix in (".jpg", ".jpeg", ".png", ".webp"):
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=fh,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await context.bot.send_document(
                chat_id=chat_id,
                document=fh,
                filename=file_path.name,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )


async def _safe_edit(msg, text: str) -> None:
    """Edit a message, ignoring errors if it can't be modified."""
    try:
        await msg.edit_text(text)
    except TelegramError:
        pass


def get_callback_handlers() -> list[CallbackQueryHandler]:
    return [
        CallbackQueryHandler(
            handle_conversion_callback, pattern=r"^convert:"
        )
    ]
