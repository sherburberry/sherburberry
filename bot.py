"""bot.py - Telegram bot entry point.  Run with:  python -m src.bot"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults

from . import config
from .scheduler import run_briefing, schedule_daily_briefing

log = logging.getLogger(__name__)


def _is_owner(update: Update) -> bool:
    """Only the configured chat may trigger paid API calls."""
    return update.effective_chat is not None and update.effective_chat.id == config.TELEGRAM_CHAT_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start - greet and show the chat ID (handy for initial setup)."""
    await update.message.reply_text(
        f"👋 Hi! Your chat ID is {update.effective_chat.id}.\n"
        "Use /briefing to get the morning brief on demand."
    )


async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/briefing - generate and send the briefing right now (owner only)."""
    if not _is_owner(update):
        return  # silently ignore strangers
    await update.message.reply_text("☕ Brewing your briefing - back in about 30 seconds…")
    try:
        await run_briefing(context.bot, config.TELEGRAM_CHAT_ID)
    except Exception:
        log.exception("Manual briefing failed")
        await update.message.reply_text("⚠️ Sorry, the briefing failed - check the logs.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled errors instead of crashing the bot."""
    log.error("Unhandled error", exc_info=context.error)


def main() -> None:
    """Build the application, register handlers and the daily job, then poll."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs full request URLs, which contain the bot token - keep that out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(tzinfo=config.TIMEZONE))
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("briefing", briefing))
    app.add_error_handler(on_error)
    schedule_daily_briefing(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
