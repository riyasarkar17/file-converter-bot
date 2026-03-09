"""
main.py — application entry point.

Bootstraps:
  1. Logging
  2. Database
  3. Telegram Application with all handlers
  4. Background cleanup task
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from telegram.ext import Application, ApplicationBuilder

from bot.database import close_db, init_db
from bot.handlers import get_callback_handlers, get_command_handlers, get_file_handlers
from bot.utils.file_utils import cleanup_loop
from config import settings
from config.logging_config import setup_logging

logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Called once the Application is initialised, before polling starts."""
    await init_db()
    logger.info("Database ready.")

    # Kick off periodic temp-file cleanup (every 5 minutes)
    asyncio.create_task(cleanup_loop(interval_seconds=300))
    logger.info("Background cleanup task scheduled.")


async def post_shutdown(application: Application) -> None:
    """Called after the bot stops polling."""
    await close_db()
    logger.info("Shutdown complete.")


def build_application() -> Application:
    """Construct and configure the Telegram Application."""
    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register all handlers
    for handler in get_command_handlers():
        app.add_handler(handler)
    for handler in get_file_handlers():
        app.add_handler(handler)
    for handler in get_callback_handlers():
        app.add_handler(handler)

    return app


def main() -> None:
    """Set up logging, build the app, and start polling."""
    setup_logging(log_level=settings.log_level, log_dir=settings.log_dir)
    logger.info("Starting FileConverter Bot…")

    application = build_application()

    # Graceful shutdown on SIGTERM (used by Docker / Railway)
    def _sigterm_handler(signum, frame):
        logger.info("SIGTERM received — shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    logger.info("Bot is polling. Press Ctrl+C to stop.")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
