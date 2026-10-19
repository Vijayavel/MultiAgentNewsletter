"""Writer agents: output parsing, failure isolation, and retry on bad JSON."""

import json
from datetime import date

from conftest import WRITERS, writer_reply
from openai import APITimeoutError

from newsletter.config import load_settings
from newsletter.llm import parse_json
from newsletter.writers import parse_stories, run_writers

TODAY = date(2026, 10, 13)


def test_parse_stories_drops_stories_without_valid_sources():
    data = {"stories": [
        {"headline": "Good", "summary": "S.", "sources": [1, 2, 2]},
        {"headline": "Out of range", "summary": "S.", "sources": [99]},
        {"headline": "No sources", "summary": "S.", "sources": []},
        {"headline": "", "summary": "Missing headline", "sources": [1]},
        {"headline": "String id", "summary": "S.", "sources": ["3"]},
        "not a dict",
    ]}
    stories = parse_stories(data, "openai", n_articles=12)
    assert [s.headline for s in stories] == ["Good"]
    assert stories[0].sources == [1, 2] and stories[0].id == "openai-1"


def test_parse_stories_caps_at_six():
    data = {"stories": [{"headline": f"H{i}", "summary": "S.", "sources": [1]} for i in range(10)]}
    assert len(parse_stories(data, "google", 12)) == 6


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": 2} thanks') == {"a": 2}


def test_one_failing_provider_does_not_stop_the_others(fake_llm, articles):
    fake_llm.replies.update({
        "openai": writer_reply(),
        "google": APITimeoutError(request=None),
        "deepseek": writer_reply(),
        "openrouter": json.dumps({"stories": []}),
    })
    subs = {s.agent: s for s in run_writers(articles, load_settings(), TODAY)}
    assert subs["openai"].ok and subs["deepseek"].ok
    assert not subs["google"].ok and "APITimeoutError" in subs["google"].error
    assert not subs["openrouter"].ok and "no usable stories" in subs["openrouter"].error


def test_invalid_json_is_retried_once(fake_llm, articles):
    fake_llm.replies["openai"] = ["not json at all", writer_reply()]
    [sub] = run_writers(articles, load_settings(), TODAY, writers=WRITERS[:1])
    assert sub.ok
    assert fake_llm.providers_called().count("openai") == 2


def test_prompt_carries_brief_angle_and_numbered_articles(fake_llm, articles):
    fake_llm.replies["deepseek"] = writer_reply()
    run_writers(articles, load_settings(), TODAY, writers=[WRITERS[2]])
    messages = fake_llm.calls[0][4]["messages"]
    assert "AI Daily Newsletter" in messages[0]["content"]
    assert "open-source AI" in messages[0]["content"]
    assert "[12] Story 12 headline" in messages[1]["content"]
