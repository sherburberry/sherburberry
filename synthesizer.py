"""synthesizer.py - Turn raw articles into a Telegram-ready morning brief with Claude.

Provides:
    synthesize_briefing(articles)       -> async
    synthesize_briefing_sync(articles)  -> sync (handy for scripts/tests)

Output is Telegram-flavoured HTML (<b>, <i>, <a href>) - more forgiving than
MarkdownV2, whose escaping rules break on almost any real-world text.

Test end-to-end:  python -m src.synthesizer
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 2500
ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code"}  # subset of Telegram HTML we permit

SYSTEM_PROMPT = """\
You are a witty, sharp and insightful interdisciplinary research assistant writing \
a private morning briefing for one curious reader who follows Psychology, Economics, \
Politics, AI, Cybersecurity and Finance.

YOUR JOB
1. Read the articles supplied inside <articles>. Pick only the genuinely high-impact or \
interesting items - it is fine to skip weak ones.
2. Connect dots ACROSS disciplines wherever a real link exists (e.g. an AI advance that \
changes the cybersecurity threat picture, or an economic policy that shapes behaviour). \
Never force a connection that is not there.
3. Write in a warm, witty, intelligent voice that reads well over morning coffee. \
Crisp bullets, no jargon without a quick gloss, no filler, no hype.

STRUCTURE (in this order)
- One-line greeting with today's date and a small hook.
- "🔗 <b>Connecting the dots</b>": 2-3 bullets on cross-domain links, each citing its sources.
- One bold heading per topic that has items (emoji + name), then 1-3 bullets: what \
happened + why it matters, in 1-2 sentences, with a link to the source.
- "☕ <b>One thing to chew on</b>": a single thought-provoking question or takeaway.
Target about 3,500 characters; never exceed 6,000.

FACTUAL RULES
- Use ONLY facts present in the supplied titles/summaries. Do not invent details, numbers or quotes.
- If an item is an arXiv preprint, say it is a preprint (not peer reviewed). Hedge appropriately on early findings.
- Every link must use a URL copied EXACTLY from the input. Never invent URLs.

FORMAT RULES - Telegram HTML ONLY
- Allowed tags: <b>, <i>, <a href="URL">anchor text</a>, <code>. No other tags. No Markdown. No code fences.
- Escape literal & < > in text as &amp; &lt; &gt; (including & inside URLs: &amp;).
- Bullets use the "•" character at the start of a line.
- Separate sections with a blank line. Never keep a tag open across a blank line.
- Output ONLY the briefing - no preface and no commentary about the task.
"""


def build_articles_block(articles: dict[str, list[dict[str, str]]]) -> str:
    """Render the fetcher output as a compact, numbered plain-text block for the prompt."""
    lines: list[str] = []
    n = 0
    for topic, items in articles.items():
        if not items:
            continue
        lines.append(f"## {topic}")
        for item in items:
            n += 1
            lines.append(
                f"[{n}] {item['title']}\n"
                f"    Source: {item['source']}\n"
                f"    URL: {item['url']}\n"
                f"    Summary: {item['summary'] or '(no summary available)'}"
            )
        lines.append("")
    return "\n".join(lines)


def _build_user_prompt(articles: dict[str, list[dict[str, str]]]) -> str:
    """Compose the user message: today's date + the articles."""
    today = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%A, %d %B %Y")
    return (
        f"Today is {today} (Singapore). Write this morning's briefing.\n\n"
        f"<articles>\n{build_articles_block(articles)}\n</articles>"
    )


def _extract_text(response: anthropic.types.Message) -> str:
    """Concatenate the text blocks of a Claude response."""
    return "".join(block.text for block in response.content if block.type == "text")


def _sanitize_telegram_html(text: str) -> str:
    """Remove code fences and any tag Telegram would reject (defence in depth)."""
    text = re.sub(r"^```(?:html)?\s*|\s*```$", "", text.strip())

    def keep_allowed(match: re.Match) -> str:
        return match.group(0) if match.group(2).lower() in ALLOWED_TAGS else ""

    return re.sub(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>", keep_allowed, text)


def _is_empty(articles: dict[str, list[dict[str, str]]]) -> bool:
    return not any(articles.values())


_EMPTY_MESSAGE = "☕ <b>Good morning!</b>\n\nEvery news source failed to load this morning - check the bot logs."


async def synthesize_briefing(articles: dict[str, list[dict[str, str]]], model: str | None = None) -> str:
    """Generate the briefing asynchronously (preferred inside the Telegram bot)."""
    if _is_empty(articles):
        return _EMPTY_MESSAGE
    client = anthropic.AsyncAnthropic(timeout=90.0)  # reads ANTHROPIC_API_KEY; retries 2x by default
    response = await client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(articles)}],
    )
    return _sanitize_telegram_html(_extract_text(response))


def synthesize_briefing_sync(articles: dict[str, list[dict[str, str]]], model: str | None = None) -> str:
    """Blocking version of `synthesize_briefing`."""
    if _is_empty(articles):
        return _EMPTY_MESSAGE
    client = anthropic.Anthropic(timeout=90.0)
    response = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(articles)}],
    )
    return _sanitize_telegram_html(_extract_text(response))


if __name__ == "__main__":
    from .data_fetcher import fetch_all_topics

    print(synthesize_briefing_sync(fetch_all_topics()))
