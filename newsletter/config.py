"""
config.py - Every setting for the multi-agent newsletter, read from the environment.

Settings are read when load_settings() is called (not at import time), so the
.env file, GitHub Actions secrets and test overrides all work the same way.

What the agents do and how they are instructed is in config/agents.toml (see agents.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agents import AgentsConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Editorial settings (writers, prompts, search queries) live in config/agents.toml.
DEFAULT_AGENTS_FILE = PROJECT_ROOT / "config" / "agents.toml"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible model provider. Model and URL are overridable per provider."""

    name: str
    key_env: str
    default_model: str
    default_base_url: str | None
    token_param: str = "max_tokens"  # OpenAI's newer models only accept max_completion_tokens

    @property
    def prefix(self) -> str:
        return self.name.upper()

    @property
    def api_key(self) -> str:
        return env(self.key_env)

    @property
    def model(self) -> str:
        return env(f"{self.prefix}_MODEL", self.default_model)

    @property
    def base_url(self) -> str | None:
        return env(f"{self.prefix}_BASE_URL") or self.default_base_url


PROVIDERS: dict[str, Provider] = {
    "openai": Provider("openai", "OPENAI_API_KEY", "gpt-5.4-mini", None, "max_completion_tokens"),
    "google": Provider(
        "google", "GOOGLE_API_KEY", "gemini-flash-latest",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    ),
    "deepseek": Provider("deepseek", "DEEPSEEK_API_KEY", "deepseek-flash", "https://api.deepseek.com"),
    "openrouter": Provider("openrouter", "OPENROUTER_API_KEY", "qwen/qwen3.7-plus", "https://openrouter.ai/api/v1"),
    "cerebras": Provider("cerebras", "CEREBRAS_API_KEY", "gpt-oss-120b", "https://api.cerebras.ai/v1"),
}


@dataclass
class Settings:
    to: list[str] = field(default_factory=list)
    # Writers, search queries and agent instructions, from config/agents.toml
    agents: AgentsConfig | None = None
    agents_file: Path = DEFAULT_AGENTS_FILE
    # Article masthead and byline
    publication: str = "AI & Software Daily"
    author: str = "Content Agent"
    # News search: "serper" or "brave"; freshness "d" = past day, "w" = past week
    search_provider: str = "serper"
    freshness: str = "d"
    results_per_query: int = 10
    min_articles: int = 8
    # Writers and reviewer
    min_writers: int = 2
    word_min: int = 600
    word_max: int = 750
    review_attempts: int = 3
    llm_timeout: float = 300.0
    llm_max_retries: int = 3
    retry_waits: list[float] = field(default_factory=lambda: [10.0, 30.0])
    # After this, go ahead with the writers that finished (if at least min_writers did).
    writers_deadline: float = 480.0
    # Sent-email history: repeat checks and the minimum gap between sends
    history_file: Path = PROJECT_ROOT / "newsletter_history.json"
    history_keep: int = 2
    history_max_age_hours: float = 48.0
    min_gap_hours: float = 12.0
    writer_max_tokens: int = 16000
    reviewer_max_tokens: int = 16000


def load_settings() -> Settings:
    s = Settings()
    s.to = _split(env("NEWSLETTER_TO"))
    s.publication = env("NEWSLETTER_NAME") or s.publication
    s.author = env("EMAIL_FROM_NAME").strip('"') or s.author
    s.search_provider = env("SEARCH_PROVIDER", "serper").lower()
    s.freshness = env("NEWS_FRESHNESS", "d").lower()
    s.min_writers = int(env("NEWSLETTER_MIN_WRITERS", "2"))
    s.word_min = int(env("NEWSLETTER_WORDS_MIN", "600"))
    s.word_max = int(env("NEWSLETTER_WORDS_MAX", "750"))
    s.llm_timeout = float(env("LLM_TIMEOUT_SECONDS", "300"))
    s.llm_max_retries = int(env("LLM_MAX_RETRIES", "3"))
    s.retry_waits = [float(w) for w in _split(env("LLM_RETRY_WAITS", "10,30"))]
    s.writers_deadline = float(env("WRITERS_DEADLINE_SECONDS", "480"))
    if env("NEWSLETTER_HISTORY_FILE"):
        s.history_file = Path(env("NEWSLETTER_HISTORY_FILE"))
    s.history_keep = int(env("NEWSLETTER_HISTORY_KEEP", "2"))
    s.history_max_age_hours = float(env("NEWSLETTER_HISTORY_MAX_AGE_HOURS", "48"))
    s.min_gap_hours = float(env("NEWSLETTER_MIN_GAP_HOURS", "12"))
    if env("NEWSLETTER_AGENTS_FILE"):
        s.agents_file = Path(env("NEWSLETTER_AGENTS_FILE"))
    from .agents import load_agents  # imported here: agents.py needs PROVIDERS from this module

    s.agents = load_agents(s.agents_file, s.min_writers)
    return s
