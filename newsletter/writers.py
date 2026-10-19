"""
writers.py - Four independent content agents, one per provider, run in parallel.

Each writer gets the same numbered list of search results but a different editorial
angle, so their picks overlap less. Writers cite articles by number only; the code
maps numbers back to URLs, so a writer cannot invent a link.

The roster, angles and instructions are in config/agents.toml. To add a writer on a new
provider, add the Provider in config.py and a [[writers]] entry in agents.toml.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from .agents import Writer
from .config import PROVIDERS, Settings
from .history import SentIssue, format_previous
from .llm import chat_json
from .search import Article

log = logging.getLogger("newsletter.writers")


@dataclass
class Story:
    id: str
    agent: str
    headline: str
    summary: str
    why_it_matters: str
    topic: str
    sources: list[int]  # 1-based indexes into the shared article list


@dataclass
class Submission:
    agent: str
    provider: str
    model: str
    stories: list[Story] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.stories)


def format_articles(articles: list[Article]) -> str:
    return "\n".join(
        f"[{i}] {a.title} | {a.source} | {a.published}\n    {a.snippet}" for i, a in enumerate(articles, 1)
    )


def writer_prompts(
    writer: Writer, articles: list[Article], s: Settings, today: date, previous: list[SentIssue] | None = None,
) -> tuple[str, str]:
    a = s.agents
    system = a.writer_system.substitute(
        brief=a.brief, angle=writer.angle, min_stories=a.min_stories, max_stories=a.max_stories,
    )
    user = f"Today is {today:%A, %B %d, %Y}.\n\n"
    if previous:
        user += f"{a.previous_note}\n{format_previous(previous)}\n\n"
    user += f"Search results:\n{format_articles(articles)}"
    return system, user


def parse_stories(data: dict, agent: str, n_articles: int, max_stories: int = 6) -> list[Story]:
    """Keep only well-formed stories that cite at least one real article."""
    stories = []
    for raw in data.get("stories") or []:
        if not isinstance(raw, dict):
            continue
        sources = sorted({i for i in raw.get("sources") or [] if isinstance(i, int) and 1 <= i <= n_articles})
        headline, summary = str(raw.get("headline", "")).strip(), str(raw.get("summary", "")).strip()
        if not (headline and summary and sources):
            continue
        stories.append(Story(
            id=f"{agent}-{len(stories) + 1}", agent=agent, headline=headline, summary=summary,
            why_it_matters=str(raw.get("why_it_matters", "")).strip(),
            topic=str(raw.get("topic", "")).strip(), sources=sources,
        ))
        if len(stories) == max_stories:
            break
    return stories


def run_writer(
    writer: Writer, articles: list[Article], s: Settings, today: date, previous: list[SentIssue] | None = None,
) -> Submission:
    provider = PROVIDERS[writer.provider]
    sub = Submission(writer.name, provider.name, provider.model)
    try:
        system, user = writer_prompts(writer, articles, s, today, previous)
        reply = chat_json(provider, s, system, user, s.writer_max_tokens)
        sub.stories = parse_stories(reply, writer.name, len(articles), s.agents.max_stories)
        if not sub.stories:
            sub.error = "no usable stories (none cited a valid search result)"
    except Exception as e:  # any failure is recorded, never raised: the other writers carry on
        sub.error = str(e)
    if sub.ok:
        log.info("Writer %s (%s): %d stories", writer.name, sub.model, len(sub.stories))
    else:
        log.warning("Writer %s (%s) failed: %s", writer.name, sub.model, sub.error)
    return sub


def run_writers(
    articles: list[Article], s: Settings, today: date, writers: list[Writer] | None = None,
    previous: list[SentIssue] | None = None,
) -> list[Submission]:
    """Run all writers at once (default: the enabled writers in agents.toml); any writer not
    done by the deadline is counted as failed.

    Daemon threads (not a thread pool) so a stuck provider can neither hold up the
    newsletter nor keep the process alive after the email is sent.
    """
    writers = s.agents.writers if writers is None else writers
    results: dict[str, Submission] = {}

    def work(w: Writer) -> None:
        results[w.name] = run_writer(w, articles, s, today, previous)

    threads = [threading.Thread(target=work, args=(w,), daemon=True, name=f"writer-{w.name}") for w in writers]
    for t in threads:
        t.start()
    deadline = time.monotonic() + s.writers_deadline
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))

    subs = []
    for w in writers:
        sub = results.get(w.name)
        if sub is None:
            provider = PROVIDERS[w.provider]
            sub = Submission(w.name, provider.name, provider.model,
                             error=f"no reply within the {s.writers_deadline:.0f}s writer deadline")
            log.warning("Writer %s (%s) failed: %s", w.name, provider.model, sub.error)
        subs.append(sub)
    return subs
