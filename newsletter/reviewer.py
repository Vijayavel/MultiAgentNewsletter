"""
reviewer.py - The review agent (Cerebras): rank all submissions, pick the best stories
across writers, and synthesize one ~5-minute newsletter.

Its provider, instructions and revision feedback are in config/agents.toml.
The model only chooses and writes; the code enforces the rules it could get wrong:
links come from real search results, duplicate stories are dropped, and the body
must land in the word range (the draft goes back for revision if it does not).
"""

from __future__ import annotations

import difflib
import json
import logging
from dataclasses import dataclass, field
from datetime import date

from .config import PROVIDERS, Settings
from .history import SentIssue, format_previous
from .layout import Issue, Item, Section, count_words, issue_words, to_html, to_markdown  # noqa: F401 (count_words re-exported)
from .llm import chat_json
from .search import Article
from .writers import Submission

log = logging.getLogger("newsletter.reviewer")

SAME_HEADLINE = 0.8  # difflib ratio above which two headlines count as the same story


class ReviewError(Exception):
    """Raised when the review agent cannot produce a newsletter fit to send."""


@dataclass
class Newsletter:
    title: str
    markdown: str
    word_count: int
    rankings: list[dict] = field(default_factory=list)
    item_count: int = 0
    headlines: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    html: str = ""  # the Medium-style email; `markdown` is its plain-text version
    issue: Issue | None = None  # structured form, stored in history so it can be re-rendered


@dataclass
class Rendered:
    markdown: str
    words: int
    items: int
    headlines: list[str]
    urls: list[str]
    repeats: list[str]  # headlines dropped because a recent email already covered them
    html: str = ""
    issue: Issue | None = None


def _similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > SAME_HEADLINE


def _cited_articles(submissions: list[Submission]) -> set[int]:
    return {i for sub in submissions for story in sub.stories for i in story.sources}


def review_prompts(
    submissions: list[Submission], articles: list[Article], s: Settings, today: date,
    previous: list[SentIssue] | None = None,
) -> tuple[str, str]:
    system = s.agents.reviewer_system.substitute(word_min=s.word_min, word_max=s.word_max)
    cited = sorted(_cited_articles(submissions))
    candidates = [
        {
            "writer": sub.agent,
            "stories": [
                {"id": st.id, "headline": st.headline, "summary": st.summary,
                 "why_it_matters": st.why_it_matters, "topic": st.topic, "sources": st.sources}
                for st in sub.stories
            ],
        }
        for sub in submissions
    ]
    sources = "\n".join(
        f"[{i}] {articles[i - 1].title} | {articles[i - 1].source} | {articles[i - 1].published}" for i in cited
    )
    already_sent = format_previous(previous or []) or "None in the last 48 hours."
    user = (
        f"Today is {today:%A, %B %d, %Y}.\n\nRecently sent issues (do not repeat):\n{already_sent}\n\n"
        f"Cited articles:\n{sources}\n\n"
        f"Writer submissions:\n{json.dumps(candidates, indent=1)}"
    )
    return system, user


def title_of(draft: dict) -> str:
    return str(draft.get("title", "")).strip() or "Daily AI & Software Briefing"


def render(
    draft: dict, articles: list[Article], allowed: set[int], today: date, previous: list[SentIssue] | None = None,
    publication: str = "AI & Software Daily", author: str = "Content Agent",
) -> Rendered:
    """Turn the reviewer's JSON into an article (HTML + Markdown), dropping repeats within the
    issue and from recent issues."""
    sent_urls = {u for issue in previous or [] for u in issue.urls}
    sent_headlines = [h for issue in previous or [] for h in issue.headlines]
    used_urls: set[str] = set()
    used_headlines: list[str] = []
    repeats: list[str] = []
    sections: list[Section] = []

    for section in draft.get("sections") or []:
        kept: list[Item] = []
        for item in section.get("items") or []:
            ids = [i for i in item.get("sources") or [] if isinstance(i, int) and i in allowed]
            headline, text = str(item.get("headline", "")).strip(), str(item.get("body", "")).strip()
            urls = {articles[i - 1].url for i in ids}
            if not (ids and headline and text):
                continue
            # Already sent: an article from a recent email, or the same headline as a recent story.
            if urls & sent_urls or any(_similar(headline, h) for h in sent_headlines):
                log.info("Dropped item already sent recently: %s", headline)
                repeats.append(headline)
                continue
            # Duplicate within this issue: same article already used, or a near-identical headline.
            if urls & used_urls or any(_similar(headline, h) for h in used_headlines):
                log.info("Dropped duplicate item: %s", headline)
                continue
            used_urls |= urls
            used_headlines.append(headline)
            kept.append(Item(headline, text, [articles[i - 1] for i in ids]))
        if kept:
            sections.append(Section(str(section.get("heading", "")).strip() or "More news", kept))

    issue = Issue(
        title=title_of(draft), subtitle=str(draft.get("intro", "")).strip(), sections=sections,
        closing=str(draft.get("closing", "")).strip(), day=today,
    )
    issue.words = issue_words(issue)
    return Rendered(
        to_markdown(issue, publication, author), issue.words, sum(len(s.items) for s in sections),
        used_headlines, sorted(used_urls), repeats, to_html(issue, publication, author), issue,
    )


def review(
    submissions: list[Submission], articles: list[Article], s: Settings, today: date,
    previous: list[SentIssue] | None = None,
) -> Newsletter:
    a = s.agents
    provider = PROVIDERS[a.reviewer_provider]
    allowed = _cited_articles(submissions)
    system, user = review_prompts(submissions, articles, s, today, previous)
    best: tuple[int, Newsletter] | None = None

    for attempt in range(1, s.review_attempts + 1):
        try:
            draft = chat_json(provider, s, system, user, s.reviewer_max_tokens)
        except Exception as e:
            raise ReviewError(f"Review agent failed: {e}") from e
        r = render(draft, articles, allowed, today, previous, s.publication, s.author)
        words, items = r.words, r.items
        letter = Newsletter(
            title_of(draft), r.markdown, words, draft.get("rankings") or [], items, r.headlines, r.urls, r.html, r.issue,
        )
        log.info("Review draft %d: %d words, %d items, %d repeat(s) removed", attempt, words, items, len(r.repeats))

        if items >= a.min_items and s.word_min <= words <= s.word_max:
            return letter
        miss = 0 if s.word_min <= words <= s.word_max else min(abs(words - s.word_min), abs(words - s.word_max))
        if items >= a.min_items and (best is None or miss < best[0]):
            best = (miss, letter)
        # Ask for a revision with concrete feedback.
        repeats_note = a.repeats_note.substitute(repeats=r.repeats) if r.repeats else ""
        feedback = a.revision.substitute(
            words=words, items=items, repeats_note=repeats_note,
            word_min=s.word_min, word_max=s.word_max, min_items=a.min_items,
        )
        user = f"{user}\n\nYour previous draft:\n{json.dumps(draft)}\n\n{feedback}"

    # Close enough (within 15% of the range) is still a good 5-minute read; further off is not sent.
    if best and s.word_min * 0.85 <= best[1].word_count <= s.word_max * 1.15:
        log.warning("Using closest draft: %d words (target %d-%d)", best[1].word_count, s.word_min, s.word_max)
        return best[1]
    raise ReviewError(
        f"No draft met the length/quality bar after {s.review_attempts} attempts "
        f"(target {s.word_min}-{s.word_max} words, at least {a.min_items} items)."
    )
