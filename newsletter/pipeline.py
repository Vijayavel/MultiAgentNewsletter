"""
pipeline.py - search -> four writers in parallel -> review -> one finished newsletter.

Raises PipelineError whenever the result is not fit to send, so the caller never
emails a half-built newsletter.
"""

from __future__ import annotations

import logging
from datetime import date

from .agents import Writer
from .config import Settings
from .history import SentIssue
from .reviewer import Newsletter, ReviewError, review
from .search import SearchError, search_news
from .writers import run_writers

log = logging.getLogger("newsletter.pipeline")


class PipelineError(Exception):
    """Raised when no newsletter fit for delivery could be produced."""


def build_newsletter(
    s: Settings, today: date | None = None, writers: list[Writer] | None = None,
    previous: list[SentIssue] | None = None,
) -> Newsletter:
    """`previous` = recently sent issues; writers avoid them and the reviewer removes any repeat."""
    today = today or date.today()

    try:
        articles = search_news(s)
    except SearchError as e:
        raise PipelineError(f"News search failed: {e}") from e
    if len(articles) < s.min_articles:
        raise PipelineError(f"Only {len(articles)} news articles found (need {s.min_articles}).")

    submissions = run_writers(articles, s, today, writers, previous)
    ok = [sub for sub in submissions if sub.ok]
    failed = [f"{sub.agent}: {sub.error}" for sub in submissions if not sub.ok]
    log.info("Writers: %d of %d succeeded%s", len(ok), len(submissions), f" (failed: {failed})" if failed else "")
    if len(ok) < s.min_writers:
        raise PipelineError(f"Only {len(ok)} writer(s) succeeded (need {s.min_writers}). Failures: {failed}")

    try:
        return review(ok, articles, s, today, previous)
    except ReviewError as e:
        raise PipelineError(str(e)) from e
