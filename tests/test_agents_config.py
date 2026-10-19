"""config/agents.toml: the shipped file is valid, edits take effect, and mistakes are caught early."""

import re
from datetime import date
from pathlib import Path

import pytest

import agent
from newsletter.agents import AgentsConfigError, load_agents
from newsletter.config import DEFAULT_AGENTS_FILE, load_settings
from newsletter.writers import writer_prompts

SHIPPED = DEFAULT_AGENTS_FILE.read_text()


@pytest.fixture
def agents_file(tmp_path, monkeypatch):
    """Write a variant of the shipped config and point the app at it."""
    def _write(text: str) -> Path:
        path = tmp_path / "agents.toml"
        path.write_text(text)
        monkeypatch.setenv("NEWSLETTER_AGENTS_FILE", str(path))
        return path
    return _write


def test_shipped_config_is_valid():
    """Checks structure only, so editing angles, queries or counts doesn't break it."""
    a = load_agents(DEFAULT_AGENTS_FILE, min_writers=2)
    assert len(a.writers) >= 2 and all(w.angle for w in a.writers)
    assert a.queries and set(a.query_freshness) <= set(a.queries)
    assert a.reviewer_provider == "cerebras" and 1 <= a.min_stories <= a.max_stories


def test_editing_an_angle_changes_that_writers_prompt(agents_file):
    agents_file(SHIPPED.replace("open-source AI, developer tools", "Rust and WebAssembly tooling"))
    s = load_settings()
    deepseek = next(w for w in s.agents.writers if w.name == "deepseek")
    system, _ = writer_prompts(deepseek, [], s, date(2026, 10, 17))
    assert "Your angle: Rust and WebAssembly tooling" in system


def test_disabled_writer_is_skipped(agents_file):
    agents_file(SHIPPED.replace('provider = "google"\nangle = "AI products, platforms and the business of AI (funding, deals, adoption)"\nenabled = true',
                                'provider = "google"\nangle = "AI products, platforms and the business of AI (funding, deals, adoption)"\nenabled = false'))
    assert [w.name for w in load_settings().agents.writers] == ["openai", "deepseek", "openrouter"]


def test_story_count_flows_into_the_prompt(agents_file):
    agents_file(SHIPPED.replace("min_stories = 4\nmax_stories = 6", "min_stories = 3\nmax_stories = 5"))
    s = load_settings()
    system, _ = writer_prompts(s.agents.writers[0], [], s, date(2026, 10, 17))
    assert "Pick 3 to 5 stories" in system


def test_literal_dollar_sign(agents_file):
    agents_file(SHIPPED.replace("No hype, no promotional language.", "No hype; ignore $$5 price tags."))
    s = load_settings()
    system, _ = writer_prompts(s.agents.writers[0], [], s, date(2026, 10, 17))
    assert "ignore $5 price tags" in system


@pytest.mark.parametrize("change, message", [
    (('provider = "deepseek"', 'provider = "mistral"'), "unknown provider 'mistral'"),
    (("Your angle: $angle", "Your angle: $angel"), r"writer.system: unknown placeholder\(s\) \$angel"),
    (("Your angle: $angle. ", ""), r"writer.system: must use \$angle"),
    (("must total $word_min", "must total $min_words"), r"reviewer.system: unknown placeholder\(s\) \$min_words"),
    (('name = "google"', 'name = "openai"'), "duplicate writer name 'openai'"),
    (("min_items = 3", "min_items = 0"), "'min_items' must be at least 1"),
    (("min_stories = 4", "min_stories = 9"), "min_stories <= max_stories"),
    (('[reviewer]\nprovider = "cerebras"', '[reviewer]\nprovider = 42'), "'provider' must be a str"),
    (("[search]", "[serch]"), r"missing \[search\] section"),
    (('freshness = "w" },\n', 'freshness = "y" },\n'), "freshness must be one of d, w, m"),
    (('type = "web" },\n', 'type = "images" },\n'), "type must be one of news, web"),
    (("queries = [", "queries = ["), None),  # unchanged: sanity check that the harness passes
])
def test_mistakes_are_caught_with_a_clear_message(agents_file, change, message):
    agents_file(SHIPPED.replace(*change, 1))
    if message is None:
        load_settings()
        return
    with pytest.raises(AgentsConfigError, match=message):
        load_settings()


def test_too_few_enabled_writers_for_the_minimum(agents_file):
    agents_file(re.sub(r"enabled = true", "enabled = false", SHIPPED, count=3))
    with pytest.raises(AgentsConfigError, match="1 writer\\(s\\) enabled, but NEWSLETTER_MIN_WRITERS is 2"):
        load_settings()


def test_broken_toml_and_missing_file(agents_file, monkeypatch, tmp_path):
    agents_file(SHIPPED + "\n[[writers]\n")
    with pytest.raises(AgentsConfigError, match="not valid TOML"):
        load_settings()
    monkeypatch.setenv("NEWSLETTER_AGENTS_FILE", str(tmp_path / "nope.toml"))
    with pytest.raises(AgentsConfigError, match="Agents config not found"):
        load_settings()


def test_agent_reports_config_problem_and_sends_nothing(agents_file, monkeypatch):
    agents_file(SHIPPED.replace('provider = "deepseek"', 'provider = "mistral"'))
    monkeypatch.setenv("NEWSLETTER_TO", "me@example.com")
    monkeypatch.setattr(agent, "build_newsletter", lambda *a, **k: pytest.fail("must not build"))
    assert agent.main([]) == 1


def test_a_query_can_have_its_own_time_window(agents_file, monkeypatch):
    from types import SimpleNamespace

    from newsletter import search
    agents_file(SHIPPED.replace('    "AI chips data center",\n', '    { query = "AI chips data center", freshness = "m" },\n'))
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen[json["q"]] = (url.rsplit("/", 1)[-1], json.get("tbs"))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"news": [], "organic": []})

    monkeypatch.setattr(search.requests, "post", fake_post)
    search.search_news(load_settings())
    assert seen["AI chips data center"] == ("news", "qdr:m")
    assert seen["new AI model release"] == ("news", "qdr:d")  # plain strings keep NEWS_FRESHNESS
    web = [q for q in load_settings().agents.query_type]
    assert web and all(seen[q] == ("search", None) for q in web)  # web pages: no time window


def test_web_results_become_articles_with_their_domain(monkeypatch):
    from types import SimpleNamespace

    from newsletter import search

    def fake_post(url, json, headers, timeout):
        page = {"title": "QCon San Francisco 2026", "link": "https://www.qconsf.com/", "snippet": "Nov 16-18, 2026"}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"organic": [page], "news": []})

    monkeypatch.setattr(search.requests, "post", fake_post)
    s = load_settings()
    s.agents.queries = ["conferences"]
    s.agents.query_type = {"conferences": "web"}
    [a] = search.search_news(s)
    assert (a.title, a.source, a.published, a.snippet) == ("QCon San Francisco 2026", "qconsf.com", "web page", "Nov 16-18, 2026")
