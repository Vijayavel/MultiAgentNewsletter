"""
search.py - Shared news search, so every writer works from real, recent, linkable articles.

Supported providers (SEARCH_PROVIDER): serper (SERPER_API_KEY), brave (BRAVE_API_KEY).
Each query is a news search by default; agents.toml can make one a web search (for event
listings and other pages that are not news articles).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .config import Settings, env

log = logging.getLogger("newsletter.search")


class SearchError(Exception):
    """Raised when news search cannot run at all."""


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    source: str
    published: str
    snippet: str


def _domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _serper(query: str, s: Settings, freshness: str | None, kind: str) -> list[Article]:
    body = {"q": query, "num": s.results_per_query}
    if freshness:
        body["tbs"] = f"qdr:{freshness}"
    r = requests.post(
        f"https://google.serper.dev/{'news' if kind == 'news' else 'search'}",
        json=body, headers={"X-API-KEY": env("SERPER_API_KEY")}, timeout=20,
    )
    r.raise_for_status()
    if kind == "news":
        return [
            Article(n.get("title", ""), n.get("link", ""), n.get("source", ""), n.get("date", ""), n.get("snippet", ""))
            for n in r.json().get("news", [])
        ]
    return [  # web pages: event listings, conference sites and the like
        Article(n.get("title", ""), n.get("link", ""), _domain(n.get("link", "")), n.get("date", "web page"), n.get("snippet", ""))
        for n in r.json().get("organic", [])
    ]


def _brave(query: str, s: Settings, freshness: str | None, kind: str) -> list[Article]:
    params = {"q": query, "count": s.results_per_query}
    if freshness:
        params["freshness"] = f"p{freshness}"
    r = requests.get(
        f"https://api.search.brave.com/res/v1/{'news' if kind == 'news' else 'web'}/search",
        params=params, headers={"X-Subscription-Token": env("BRAVE_API_KEY"), "Accept": "application/json"}, timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    results = data.get("results", []) if kind == "news" else (data.get("web") or {}).get("results", [])
    return [
        Article(
            n.get("title", ""), n.get("url", ""), (n.get("meta_url") or {}).get("hostname", ""),
            n.get("age", "web page" if kind == "web" else ""), n.get("description", ""),
        )
        for n in results
    ]


BACKENDS = {"serper": ("SERPER_API_KEY", _serper), "brave": ("BRAVE_API_KEY", _brave)}


def _normalize(url: str) -> str:
    return url.split("#")[0].rstrip("/").lower()


def search_news(s: Settings) -> list[Article]:
    """Run every query, keep each article once (by URL and by title)."""
    if s.search_provider not in BACKENDS:
        raise SearchError(f"Unknown SEARCH_PROVIDER {s.search_provider!r}; use one of {sorted(BACKENDS)}.")
    key_env, backend = BACKENDS[s.search_provider]
    if not env(key_env):
        raise SearchError(f"{key_env} is not set.")

    def run(query: str) -> list[Article]:
        try:
            # agents.toml can give a query its own type ("news" or "web") and time window.
            kind = s.agents.query_type.get(query, "news")
            freshness = s.agents.query_freshness.get(query) or (s.freshness if kind == "news" else None)
            return backend(query, s, freshness, kind)
        except requests.RequestException as e:  # one bad query shouldn't sink the run
            log.warning("Search failed for %r: %s", query, e.__class__.__name__)
            return []

    with ThreadPoolExecutor(max_workers=5) as pool:
        batches = list(pool.map(run, s.agents.queries))

    seen_urls, seen_titles, articles = set(), set(), []
    for article in (a for batch in batches for a in batch):
        url, title = _normalize(article.url), article.title.strip().lower()
        if not url.startswith("http") or not title or url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        articles.append(article)
    log.info("Search (%s): %d unique articles from %d queries", s.search_provider, len(articles), len(s.agents.queries))
    return articles
