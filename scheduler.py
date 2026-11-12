"""scheduler.py - The briefing pipeline and its daily 09:00 (Singapore) schedule.

Also runnable as a one-shot job (useful for cron / GitHub Actions):
    python -m src.scheduler
"""
from __future__ import annotations

import asyncio
import logging
from datetime import time as dtime

from telegram import Bot
from telegram.ext import Application, ContextTypes

from . import config
from .data_fetcher import fetch_all_topics
from .messaging import send_long_message
from .synthesizer import synthesize_briefing

log = logging.getLogger(__name__)


async def run_briefing(bot: Bot, chat_id: int) -> None:
    """Fetch -> synthesize -> send. Shared by the daily job and the /briefing command."""
    # fetch_all_topics is blocking (requests + threads), so keep it off the event loop
    articles = await asyncio.to_thread(fetch_all_topics)
    log.info("Fetched %d items across %d topics", sum(map(len, articles.values())), len(articles))
    briefing = await synthesize_briefing(articles)
    await send_long_message(bot, chat_id, briefing)


async def _daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: run the briefing and report failures to the chat."""
    try:
        await run_briefing(context.bot, config.TELEGRAM_CHAT_ID)
    except Exception:
        log.exception("Scheduled briefing failed")
        try:
            await context.bot.send_message(config.TELEGRAM_CHAT_ID, "⚠️ Today's briefing failed - check the logs.")
        except Exception:
            log.exception("Could not send failure notice")


def schedule_daily_briefing(app: Application) -> None:
    """Register the daily job at BRIEFING_HOUR:BRIEFING_MINUTE in the configured timezone."""
    if app.job_queue is None:
        raise RuntimeError('JobQueue missing - install with: pip install "python-telegram-bot[job-queue]"')
    app.job_queue.run_daily(
        _daily_job,
        time=dtime(config.BRIEFING_HOUR, config.BRIEFING_MINUTE, tzinfo=config.TIMEZONE),
        name="daily_briefing",
        # If the process restarts shortly after 09:00, still fire (within 1h) instead of skipping.
        job_kwargs={"misfire_grace_time": 3600, "coalesce": True},
    )
    log.info("Daily briefing scheduled for %02d:%02d %s",
             config.BRIEFING_HOUR, config.BRIEFING_MINUTE, config.TIMEZONE.key)


async def _once() -> None:
    async with Bot(config.TELEGRAM_BOT_TOKEN) as bot:
        await run_briefing(bot, config.TELEGRAM_CHAT_ID)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_once())
