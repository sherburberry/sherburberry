"""data_fetcher.py - Fetch the latest news and open-access papers for six topic areas.

Every source (RSS feed, arXiv, PubMed) is fetched independently. A failure in
one source is logged and skipped, so the rest of the run is never affected.

Public entry point:
    fetch_all_topics() -> {"AI": [{"title", "summary", "source", "topic", "url"}, ...], ...}

Run standalone to test:  python -m src.data_fetcher
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------
ITEMS_PER_TOPIC = int(os.getenv("ITEMS_PER_TOPIC", "3"))  # keep token usage small
MAX_PAPERS_PER_TOPIC = 1       # guarantee at most one paper per topic (rest is news)
PER_SOURCE_LIMIT = 10          # candidates pulled per source before ranking
REQUEST_TIMEOUT = 15           # seconds
SUMMARY_CHARS = 500            # trim summaries to protect the LLM context
HEADERS = {"User-Agent": "morning-brief-bot/1.0 (personal use)"}
EPOCH = datetime.min.replace(tzinfo=timezone.utc)  # sort key for undated items

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ARXIV_API = "https://export.arxiv.org/api/query"
_ARXIV_LOCK = threading.Lock()  # arXiv asks for <= 1 request per 3 seconds

PUBMED_PSYCHOLOGY = (
    '(psychology[MeSH Terms] OR "cognitive neuroscience"[Title/Abstract] '
    'OR "behavioral science"[Title/Abstract]) AND hasabstract AND "free full text"[sb]'
)

# ----------------------------------------------------------------------------
# Source registry: topic -> [(kind, display name, target)]
#   kind: "rss" (news), "rss_paper" (RSS feed of papers), "arxiv" (category), "pubmed" (query)
# Feed URLs occasionally change - if a source keeps failing in the logs, swap it here.
# ----------------------------------------------------------------------------
SOURCES: dict[str, list[tuple[str, str, str]]] = {
    "Psychology": [
        ("rss", "ScienceDaily - Psychology", "https://www.sciencedaily.com/rss/mind_brain/psychology.xml"),
        ("rss", "PsyPost", "https://www.psypost.org/feed"),
        ("pubmed", "PubMed (open access)", PUBMED_PSYCHOLOGY),
    ],
    "Economics": [
        ("rss", "The Guardian - Economics", "https://www.theguardian.com/business/economics/rss"),
        ("rss", "BBC - Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("rss_paper", "NBER - New Working Papers", "https://www.nber.org/rss/new.xml"),
        ("arxiv", "arXiv econ.GN", "econ.GN"),
    ],
    "Politics": [
        ("rss", "BBC - Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
        ("rss", "The Guardian - Politics", "https://www.theguardian.com/politics/rss"),
        ("rss", "NPR - Politics", "https://feeds.npr.org/1014/rss.xml"),
    ],
    "AI": [
        ("rss", "MIT Technology Review - AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
        ("rss", "The Verge - AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("arxiv", "arXiv cs.AI", "cs.AI"),
    ],
    "Cybersecurity": [
        ("rss", "Krebs on Security", "https://krebsonsecurity.com/feed/"),
        ("rss", "The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
        ("rss", "BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
        ("arxiv", "arXiv cs.CR", "cs.CR"),
    ],
    "Finance": [
        ("rss", "CNBC - Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
        ("rss", "MarketWatch - Top Stories", "https://feeds.marketwatch.com/marketwatch/topstories/"),
        ("arxiv", "arXiv q-fin.GN", "q-fin.GN"),
    ],
}


@dataclass
class Candidate:
    """An article plus the metadata needed for ranking (not exposed to callers)."""

    item: dict[str, str]
    published: datetime
    is_paper: bool


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _clean(text: str | None, limit: int = SUMMARY_CHARS) -> str:
    """Strip HTML tags/entities, collapse whitespace and truncate at a word boundary."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _entry_datetime(entry) -> datetime:
    """Best-effort publish time (UTC) of a feedparser entry; EPOCH if unknown."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else EPOCH


def _candidate(topic, source, title, summary, url, published, is_paper) -> Candidate:
    """Build a Candidate whose `item` follows the standard five-key schema."""
    return Candidate(
        item={"title": title, "summary": summary, "source": source, "topic": topic, "url": url},
        published=published,
        is_paper=is_paper,
    )


# ----------------------------------------------------------------------------
# Fetchers (each raises on failure; _run_source() handles errors)
# ----------------------------------------------------------------------------
def fetch_rss(topic: str, source: str, url: str, is_paper: bool = False) -> list[Candidate]:
    """Fetch an RSS/Atom feed (with a hard timeout) and normalise its entries."""
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    out = []
    for entry in feed.entries[:PER_SOURCE_LIMIT]:
        title = _clean(entry.get("title"), 200)
        link = entry.get("link")
        if not title or not link:
            continue  # skip malformed entries
        summary = _clean(entry.get("summary") or entry.get("description"))
        out.append(_candidate(topic, source, title, summary, link, _entry_datetime(entry), is_paper))
    return out


def fetch_arxiv(topic: str, category: str) -> list[Candidate]:
    """Fetch the newest arXiv preprints in a category (e.g. 'cs.AI', 'econ.GN')."""
    params = {
        "search_query": f"cat:{category}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": PER_SOURCE_LIMIT,
    }
    with _ARXIV_LOCK:  # serialise + pause to respect arXiv's rate guidance
        resp = requests.get(ARXIV_API, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        time.sleep(3)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    out = []
    for entry in feed.entries:
        title = _clean(entry.get("title"), 200)
        link = entry.get("link")
        if not title or not link:
            continue
        out.append(_candidate(topic, f"arXiv {category} (preprint)", title,
                              _clean(entry.get("summary")), link, _entry_datetime(entry), True))
    return out


def _pubmed_date(article: ET.Element) -> datetime:
    """Electronic publication date of a PubMed record, or EPOCH if absent."""
    node = article.find(".//ArticleDate")
    if node is not None:
        try:
            return datetime(int(node.findtext("Year")), int(node.findtext("Month")),
                            int(node.findtext("Day")), tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return EPOCH


def fetch_pubmed(topic: str, term: str) -> list[Candidate]:
    """Search PubMed (last 14 days) and fetch titles + abstracts via NCBI E-utilities."""
    search = requests.get(
        f"{EUTILS}/esearch.fcgi",
        params={"db": "pubmed", "term": term, "retmax": PER_SOURCE_LIMIT, "sort": "pub_date",
                "retmode": "json", "reldate": 14, "datetype": "pdat"},
        headers=HEADERS, timeout=REQUEST_TIMEOUT,
    )
    search.raise_for_status()
    ids = search.json()["esearchresult"]["idlist"]
    if not ids:
        return []

    fetched = requests.get(
        f"{EUTILS}/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
        headers=HEADERS, timeout=REQUEST_TIMEOUT,
    )
    fetched.raise_for_status()
    root = ET.fromstring(fetched.content)

    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID")
        title_node = art.find(".//ArticleTitle")
        if not pmid or title_node is None:
            continue
        title = _clean("".join(title_node.itertext()), 200)  # itertext() keeps <i> contents
        abstract = " ".join("".join(n.itertext()) for n in art.findall(".//Abstract/AbstractText"))
        journal = art.findtext(".//Journal/Title") or "PubMed"
        out.append(_candidate(topic, f"{journal} (via PubMed)", title, _clean(abstract),
                              f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", _pubmed_date(art), True))
    return out


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def _run_source(topic: str, spec: tuple[str, str, str]) -> list[Candidate]:
    """Run one source; on ANY error log a warning and return [] so others continue."""
    kind, name, target = spec
    try:
        if kind == "rss":
            return fetch_rss(topic, name, target)
        if kind == "rss_paper":
            return fetch_rss(topic, name, target, is_paper=True)
        if kind == "arxiv":
            return fetch_arxiv(topic, target)
        if kind == "pubmed":
            return fetch_pubmed(topic, target)
        raise ValueError(f"unknown source kind {kind!r}")
    except Exception as exc:  # deliberately broad: one bad feed must never stop the brief
        log.warning("[%s] source '%s' failed: %s", topic, name, exc)
        return []


def _select(candidates: list[Candidate], n: int) -> list[Candidate]:
    """Pick the n most recent unique items, reserving up to MAX_PAPERS_PER_TOPIC for papers."""
    seen, unique = set(), []
    for c in sorted(candidates, key=lambda c: c.published, reverse=True):
        if c.item["url"] not in seen:
            seen.add(c.item["url"])
            unique.append(c)

    papers = [c for c in unique if c.is_paper]
    news = [c for c in unique if not c.is_paper]
    chosen = news[: n - min(MAX_PAPERS_PER_TOPIC, len(papers))]
    chosen += papers[: n - len(chosen)]  # top up with papers if news is thin
    return sorted(chosen, key=lambda c: c.published, reverse=True)


def fetch_all_topics() -> dict[str, list[dict[str, str]]]:
    """Fetch all sources in parallel and return the top items per topic.

    Blocking (uses threads) - from async code call via `asyncio.to_thread`.
    """
    jobs = [(topic, spec) for topic, specs in SOURCES.items() for spec in specs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: _run_source(*job), jobs))

    pooled: dict[str, list[Candidate]] = {topic: [] for topic in SOURCES}
    for (topic, _), candidates in zip(jobs, results):
        pooled[topic].extend(candidates)

    return {topic: [c.item for c in _select(cands, ITEMS_PER_TOPIC)] for topic, cands in pooled.items()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch_all_topics(), indent=2, ensure_ascii=False))
