"""
agents.py - Load and check config/agents.toml: the writer roster, search queries and every
instruction the agents follow.

Everything is validated when the file is loaded, so a typo, an unknown provider or a bad
placeholder stops the run with a clear message before any API call is made.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from .config import PROVIDERS

# Placeholders each template may use. A template using anything else is rejected.
PLACEHOLDERS = {
    "writer.system": {"brief", "angle", "min_stories", "max_stories"},
    "writer.previous_note": set(),
    "reviewer.system": {"word_min", "word_max"},
    "reviewer.revision": {"words", "items", "repeats_note", "word_min", "word_max", "min_items"},
    "reviewer.repeats_note": {"repeats"},
}
# Placeholders a template must use, or it would silently ignore part of the config.
REQUIRED = {"writer.system": {"angle"}}


class AgentsConfigError(Exception):
    """Raised when config/agents.toml is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Writer:
    name: str
    provider: str  # key into newsletter.config.PROVIDERS
    angle: str
    enabled: bool = True


FRESHNESS = {"d": "past day", "w": "past week", "m": "past month"}
QUERY_TYPES = ("news", "web")


@dataclass
class AgentsConfig:
    brief: str
    queries: list[str]
    writers: list[Writer]  # enabled writers only, in file order
    min_stories: int
    max_stories: int
    writer_system: Template
    previous_note: str
    reviewer_provider: str
    min_items: int
    reviewer_system: Template
    revision: Template
    repeats_note: Template
    path: Path = field(default=Path("config/agents.toml"))
    # Per-query time window ("d", "w" or "m"); queries not listed use NEWS_FRESHNESS.
    query_freshness: dict[str, str] = field(default_factory=dict)
    # Per-query search type: "news" (default) or "web" (event listings, conference sites).
    query_type: dict[str, str] = field(default_factory=dict)


def _need(table: dict, key: str, kind: type, where: str):
    if key not in table:
        raise AgentsConfigError(f"{where}: missing '{key}'")
    value = table[key]
    if kind is str and isinstance(value, str):
        value = value.strip()
        if not value:
            raise AgentsConfigError(f"{where}: '{key}' is empty")
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise AgentsConfigError(f"{where}: '{key}' must be a {kind.__name__}")
    return value


def _template(text: str, name: str) -> Template:
    t = Template(text)
    if not t.is_valid():
        raise AgentsConfigError(f"{name}: invalid placeholder (write $$ for a literal dollar sign)")
    used = set(t.get_identifiers())
    unknown = used - PLACEHOLDERS[name]
    if unknown:
        allowed = ", ".join(f"${p}" for p in sorted(PLACEHOLDERS[name])) or "none"
        raise AgentsConfigError(f"{name}: unknown placeholder(s) {', '.join('$' + u for u in sorted(unknown))}; allowed: {allowed}")
    missing = REQUIRED.get(name, set()) - used
    if missing:
        raise AgentsConfigError(f"{name}: must use {', '.join('$' + m for m in sorted(missing))}")
    return t


def load_agents(path: Path, min_writers: int = 1) -> AgentsConfig:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AgentsConfigError(f"Agents config not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise AgentsConfigError(f"{path}: not valid TOML: {e}") from None

    def table(name: str) -> dict:
        value = data.get(name)
        if not isinstance(value, dict):
            raise AgentsConfigError(f"{path}: missing [{name}] section")
        return value

    newsletter, search, writer, reviewer = table("newsletter"), table("search"), table("writer"), table("reviewer")

    queries: list[str] = []
    query_freshness: dict[str, str] = {}
    query_type: dict[str, str] = {}
    for n, raw in enumerate(_need(search, "queries", list, "[search]"), 1):
        # A query is a plain string, or { query = "...", freshness = "w" } for its own time window.
        if isinstance(raw, dict):
            text = _need(raw, "query", str, f"[search] query #{n}")
            fresh = raw.get("freshness")
            if fresh is not None:
                if fresh not in FRESHNESS:
                    raise AgentsConfigError(f"[search] query #{n}: freshness must be one of {', '.join(FRESHNESS)}")
                query_freshness[text] = fresh
            kind = raw.get("type", "news")
            if kind not in QUERY_TYPES:
                raise AgentsConfigError(f"[search] query #{n}: type must be one of {', '.join(QUERY_TYPES)}")
            if kind != "news":
                query_type[text] = kind
        elif isinstance(raw, str) and raw.strip():
            text = raw.strip()
        else:
            raise AgentsConfigError(f"[search] query #{n}: must be a string or {{ query = \"...\", freshness = \"w\" }}")
        queries.append(text)
    if not queries:
        raise AgentsConfigError("[search]: 'queries' needs at least one search")

    writers: list[Writer] = []
    seen: set[str] = set()
    for n, raw in enumerate(data.get("writers") or [], 1):
        where = f"[[writers]] #{n}"
        if not isinstance(raw, dict):
            raise AgentsConfigError(f"{where}: must be a table")
        w = Writer(
            _need(raw, "name", str, where), _need(raw, "provider", str, where),
            _need(raw, "angle", str, where), raw.get("enabled", True) is not False,
        )
        if w.name in seen:
            raise AgentsConfigError(f"{where}: duplicate writer name '{w.name}'")
        if w.provider not in PROVIDERS:
            raise AgentsConfigError(f"{where}: unknown provider '{w.provider}'; known: {', '.join(PROVIDERS)}")
        seen.add(w.name)
        if w.enabled:
            writers.append(w)
    if len(writers) < min_writers:
        raise AgentsConfigError(
            f"{len(writers)} writer(s) enabled, but NEWSLETTER_MIN_WRITERS is {min_writers}; enable more writers"
        )

    min_stories, max_stories = _need(writer, "min_stories", int, "[writer]"), _need(writer, "max_stories", int, "[writer]")
    if not 1 <= min_stories <= max_stories:
        raise AgentsConfigError("[writer]: need 1 <= min_stories <= max_stories")

    reviewer_provider = _need(reviewer, "provider", str, "[reviewer]")
    if reviewer_provider not in PROVIDERS:
        raise AgentsConfigError(f"[reviewer]: unknown provider '{reviewer_provider}'; known: {', '.join(PROVIDERS)}")
    min_items = _need(reviewer, "min_items", int, "[reviewer]")
    if min_items < 1:
        raise AgentsConfigError("[reviewer]: 'min_items' must be at least 1")

    return AgentsConfig(
        brief=_need(newsletter, "brief", str, "[newsletter]"),
        queries=queries,
        writers=writers,
        min_stories=min_stories,
        max_stories=max_stories,
        writer_system=_template(_need(writer, "system", str, "[writer]"), "writer.system"),
        previous_note=_template(_need(writer, "previous_note", str, "[writer]"), "writer.previous_note").substitute(),
        reviewer_provider=reviewer_provider,
        min_items=min_items,
        reviewer_system=_template(_need(reviewer, "system", str, "[reviewer]"), "reviewer.system"),
        revision=_template(_need(reviewer, "revision", str, "[reviewer]"), "reviewer.revision"),
        repeats_note=_template(reviewer.get("repeats_note", ""), "reviewer.repeats_note"),
        path=path,
        query_freshness=query_freshness,
        query_type=query_type,
    )
