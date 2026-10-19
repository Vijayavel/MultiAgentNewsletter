"""Shared fixtures: fake keys, fake news articles, and a fake OpenAI-compatible client."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import dotenv
import pytest

# Never read the real .env (with real keys) into the test process.
dotenv.load_dotenv = lambda *a, **k: False

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from newsletter import llm  # noqa: E402
from newsletter.agents import load_agents  # noqa: E402
from newsletter.config import DEFAULT_AGENTS_FILE  # noqa: E402
from newsletter.search import Article  # noqa: E402

# The writer roster from the real config/agents.toml, in file order.
WRITERS = load_agents(DEFAULT_AGENTS_FILE).writers

PROVIDER_KEYS = {
    "OPENAI_API_KEY": "key-openai",
    "GOOGLE_API_KEY": "key-google",
    "DEEPSEEK_API_KEY": "key-deepseek",
    "OPENROUTER_API_KEY": "key-openrouter",
    "CEREBRAS_API_KEY": "key-cerebras",
}
BASE_URL_TO_PROVIDER = {
    None: "openai",
    "https://generativelanguage.googleapis.com/v1beta/openai/": "google",
    "https://api.deepseek.com": "deepseek",
    "https://openrouter.ai/api/v1": "openrouter",
    "https://api.cerebras.ai/v1": "cerebras",
}


@pytest.fixture(autouse=True)
def history_file(clean_env, monkeypatch, tmp_path):
    """Each test gets its own sent-email history file, never the real one."""
    path = tmp_path / "history.json"
    monkeypatch.setenv("NEWSLETTER_HISTORY_FILE", str(path))
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Known fake keys; no real model, URL or newsletter overrides leak in from the shell."""
    import os

    for name in list(os.environ):
        if name.endswith(("_MODEL", "_BASE_URL")) or name.startswith(("NEWSLETTER_", "NEWS_", "EMAIL_", "SMTP_")):
            monkeypatch.delenv(name, raising=False)
    for name, value in PROVIDER_KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SEARCH_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "key-serper")


@pytest.fixture
def articles():
    return [
        Article(f"Story {i} headline", f"https://news{i}.example.com/a/{i}", f"Source{i}", "1 day ago", f"Snippet {i}.")
        for i in range(1, 13)
    ]


def words(n: int, word: str = "word") -> str:
    return " ".join([word] * n)


def writer_reply(sources_list=((1, 2), (3,), (4, 5), (6,), (7,), (8,))) -> str:
    return json.dumps({"stories": [
        {"headline": f"Headline about {s[0]}", "summary": "Two factual sentences. Second one.",
         "why_it_matters": "It matters.", "topic": "Topic", "sources": list(s)}
        for s in sources_list
    ]})


HEADLINES = [
    "Chipmaker ships accelerator", "Robotics startup raises funds", "Cloud region outage",
    "Open weights model lands", "Browser zero-day patched", "Senate passes AI bill",
    "Compiler gets faster", "Database adds vectors", "Quantum lab result",
]


def review_reply(body_words: int = 80, items: int = 7, sources=None) -> str:
    """A reviewer draft; with the defaults the rendered body is about 650 words."""
    sources = sources or [[i] for i in range(1, items + 1)]
    headlines = iter(HEADLINES)
    per_section = [sources[i : i + 3] for i in range(0, len(sources), 3)]
    return json.dumps({
        "rankings": [{"agent": "openai", "score": 8, "notes": "solid"}, {"agent": "google", "score": 7, "notes": "ok"}],
        "title": "Today in AI",
        "intro": words(30, "intro"),
        "sections": [
            {"heading": f"Section {n}", "items": [
                {"headline": next(headlines), "body": words(body_words, "body"),
                 "sources": src, "from_stories": ["openai-1"]}
                for src in group
            ]}
            for n, group in enumerate(per_section)
        ],
        "closing": words(10, "closing"),
    })


class FakeLLM:
    """Stands in for openai.OpenAI. `replies[provider]` is a str, an Exception, or a list of them (in order)."""

    def __init__(self):
        self.replies = {}
        self.calls = []  # (provider, api_key, base_url, model, kwargs)

    def factory(self, **client_kwargs):
        fake = self

        class Completions:
            def create(self, **kwargs):
                provider = BASE_URL_TO_PROVIDER[client_kwargs.get("base_url")]
                fake.calls.append((provider, client_kwargs["api_key"], client_kwargs.get("base_url"), kwargs["model"], kwargs))
                reply = fake.replies[provider]
                if isinstance(reply, list):
                    reply = reply.pop(0) if len(reply) > 1 else reply[0]
                if callable(reply):  # e.g. a slow provider
                    reply = reply()
                if isinstance(reply, Exception):
                    raise reply
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                )

        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    def providers_called(self):
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch):
    """Record retry waits instead of sleeping through them."""
    waits = []
    monkeypatch.setattr(llm, "_sleep", waits.append)
    return waits


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm, "OpenAI", lambda **kw: fake.factory(**kw))
    return fake
