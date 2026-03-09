"""
ConversionService — the core application service.

Responsibilities:
  1. Determine available conversions for a given MIME type.
  2. Execute a chosen conversion in a thread-pool executor (never blocks the loop).
  3. Log start, success, and failure to the database.
  4. Clean up temp files regardless of outcome.

This class is the single boundary between Telegram handlers and
conversion logic — handlers call only this service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from bot import converters
from bot.database import repository
from bot.database.models import ConversionStatus
from bot.utils.file_utils import release_file
from bot.utils.validators import is_image

logger = logging.getLogger(__name__)


class ConversionType(str, Enum):
    # ── Image ──────────────────────────────────────────────────────────────
    IMG_TO_JPG = "img_to_jpg"
    IMG_TO_PNG = "img_to_png"
    IMG_TO_WEBP = "img_to_webp"
    IMG_TO_PDF = "img_to_pdf"
    IMG_RESIZE = "img_resize"
    IMG_COMPRESS = "img_compress"
    # ── Document ───────────────────────────────────────────────────────────
    TXT_TO_PDF = "txt_to_pdf"
    PDF_TO_IMAGES = "pdf_to_images"


# Human-readable labels for Telegram buttons
CONVERSION_LABELS: dict[ConversionType, str] = {
    ConversionType.IMG_TO_JPG: "🖼 → JPG",
    ConversionType.IMG_TO_PNG: "🖼 → PNG",
    ConversionType.IMG_TO_WEBP: "🖼 → WebP",
    ConversionType.IMG_TO_PDF: "🖼 → PDF",
    ConversionType.IMG_RESIZE: "📐 Resize (1024px)",
    ConversionType.IMG_COMPRESS: "🗜 Compress",
    ConversionType.TXT_TO_PDF: "📄 → PDF",
    ConversionType.PDF_TO_IMAGES: "📑 → Images",
}

# MIME → list of applicable conversions
CONVERSIONS_BY_MIME: dict[str, list[ConversionType]] = {
    "image/jpeg": [
        ConversionType.IMG_TO_PNG,
        ConversionType.IMG_TO_WEBP,
        ConversionType.IMG_TO_PDF,
        ConversionType.IMG_RESIZE,
        ConversionType.IMG_COMPRESS,
    ],
    "image/png": [
        ConversionType.IMG_TO_JPG,
        ConversionType.IMG_TO_WEBP,
        ConversionType.IMG_TO_PDF,
        ConversionType.IMG_RESIZE,
        ConversionType.IMG_COMPRESS,
    ],
    "image/webp": [
        ConversionType.IMG_TO_JPG,
        ConversionType.IMG_TO_PNG,
        ConversionType.IMG_TO_PDF,
        ConversionType.IMG_RESIZE,
        ConversionType.IMG_COMPRESS,
    ],
    "image/gif": [
        ConversionType.IMG_TO_JPG,
        ConversionType.IMG_TO_PNG,
        ConversionType.IMG_TO_PDF,
    ],
    "image/bmp": [
        ConversionType.IMG_TO_JPG,
        ConversionType.IMG_TO_PNG,
        ConversionType.IMG_TO_PDF,
    ],
    "image/tiff": [
        ConversionType.IMG_TO_JPG,
        ConversionType.IMG_TO_PNG,
        ConversionType.IMG_TO_PDF,
    ],
    "text/plain": [ConversionType.TXT_TO_PDF],
    "application/pdf": [ConversionType.PDF_TO_IMAGES],
}


@dataclass
class ConversionResult:
    success: bool
    output_path: Path | None = None
    output_paths: list[Path] | None = None  # for multi-file results
    error_message: str | None = None
    duration_seconds: float = 0.0
    extra_info: str = ""


def get_available_conversions(mime_type: str) -> list[ConversionType]:
    """Return the conversion options applicable to a MIME type."""
    return CONVERSIONS_BY_MIME.get(mime_type, [])


class ConversionService:
    """Orchestrates conversion execution with logging and error handling."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    async def _run_sync(self, fn, *args):
        """Run a blocking function in the default thread pool executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    async def convert(
        self,
        conversion_type: ConversionType,
        input_path: Path,
        output_path: Path,
        user_id: int,
        log_id: int,
        original_filename: str,
        original_size: int,
        mime_type: str,
    ) -> ConversionResult:
        """
        Execute a conversion and persist the result to the database.

        Always returns a ConversionResult — never raises to the caller.
        """
        start = time.monotonic()

        # Mark as processing
        await repository.update_conversion_log(
            log_id, status=ConversionStatus.PROCESSING
        )

        try:
            result = await self._dispatch(
                conversion_type, input_path, output_path, mime_type
            )
            result.duration_seconds = time.monotonic() - start

            if result.success:
                out = result.output_path or (
                    result.output_paths[0] if result.output_paths else None
                )
                out_size = out.stat().st_size if out and out.exists() else 0
                out_name = out.name if out else None

                await repository.update_conversion_log(
                    log_id,
                    status=ConversionStatus.SUCCESS,
                    output_filename=out_name,
                    output_size_bytes=out_size,
                    duration_seconds=result.duration_seconds,
                )
                await repository.increment_user_stats(user_id, original_size)
                logger.info(
                    "Conversion success: user=%d type=%s input=%s → %s (%.2fs)",
                    user_id,
                    conversion_type.value,
                    original_filename,
                    out_name,
                    result.duration_seconds,
                )
            else:
                await repository.update_conversion_log(
                    log_id,
                    status=ConversionStatus.FAILED,
                    error_message=result.error_message,
                    duration_seconds=result.duration_seconds,
                )

        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.exception(
                "Unexpected error during conversion user=%d type=%s: %s",
                user_id,
                conversion_type.value,
                exc,
            )
            await repository.update_conversion_log(
                log_id,
                status=ConversionStatus.FAILED,
                error_message=str(exc),
                duration_seconds=elapsed,
            )
            result = ConversionResult(
                success=False,
                error_message=f"An unexpected error occurred: {exc}",
                duration_seconds=elapsed,
            )
        finally:
            # Always clean up the input temp file
            release_file(input_path)

        return result

    # ── Dispatch table ────────────────────────────────────────────────────────

    async def _dispatch(
        self,
        ctype: ConversionType,
        inp: Path,
        out: Path,
        mime_type: str,
    ) -> ConversionResult:
        """Map a ConversionType to the correct converter function."""
        try:
            if ctype == ConversionType.IMG_TO_JPG:
                await self._run_sync(converters.convert_to_jpeg, inp, out)
                return ConversionResult(success=True, output_path=out)

            elif ctype == ConversionType.IMG_TO_PNG:
                await self._run_sync(converters.convert_to_png, inp, out)
                return ConversionResult(success=True, output_path=out)

            elif ctype == ConversionType.IMG_TO_WEBP:
                await self._run_sync(converters.convert_to_webp, inp, out)
                return ConversionResult(success=True, output_path=out)

            elif ctype == ConversionType.IMG_TO_PDF:
                await self._run_sync(converters.image_to_pdf, inp, out)
                return ConversionResult(success=True, output_path=out)

            elif ctype == ConversionType.IMG_RESIZE:
                size = await self._run_sync(converters.resize_image, inp, out)
                return ConversionResult(
                    success=True,
                    output_path=out,
                    extra_info=f"Resized to {size[0]}×{size[1]}px",
                )

            elif ctype == ConversionType.IMG_COMPRESS:
                saved_pct = await self._run_sync(converters.compress_image, inp, out)
                return ConversionResult(
                    success=True,
                    output_path=out,
                    extra_info=f"Saved ~{saved_pct}% in file size",
                )

            elif ctype == ConversionType.TXT_TO_PDF:
                pages = await self._run_sync(converters.txt_to_pdf, inp, out)
                return ConversionResult(
                    success=True,
                    output_path=out,
                    extra_info=f"{pages} page(s) generated",
                )

            elif ctype == ConversionType.PDF_TO_IMAGES:
                # Output images go into a sub-directory then get zipped
                img_dir = out.parent / (out.stem + "_pages")
                img_dir.mkdir(exist_ok=True)
                paths = await self._run_sync(converters.pdf_to_images, inp, img_dir)
                if not paths:
                    return ConversionResult(
                        success=False, error_message="No pages found in PDF."
                    )
                # Zip all page images
                zip_out = out.with_suffix(".zip")
                await self._run_sync(converters.images_to_zip, paths, zip_out)
                # Release individual page images
                for p in paths:
                    release_file(p)
                return ConversionResult(
                    success=True,
                    output_path=zip_out,
                    extra_info=f"{len(paths)} page(s) extracted",
                )

            else:
                return ConversionResult(
                    success=False,
                    error_message=f"Unknown conversion type: {ctype}",
                )

        except Exception as exc:
            logger.error("Conversion error [%s]: %s", ctype.value, exc, exc_info=True)
            return ConversionResult(success=False, error_message=str(exc))


# Module-level singleton
conversion_service = ConversionService()
