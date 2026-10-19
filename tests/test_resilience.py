"""Resilience: busy providers are retried with longer waits, stuck writers are cut off by the
deadline, and the newsletter goes out as long as two writers succeed."""

import time
from datetime import date

import pytest
from conftest import WRITERS, review_reply, writer_reply
from openai import APIConnectionError, OpenAIError

from newsletter import pipeline
from newsletter.config import load_settings
from newsletter.pipeline import PipelineError, build_newsletter
from newsletter.writers import run_writers

TODAY = date(2026, 10, 13)


def slow(seconds, reply):
    def _reply():
        time.sleep(seconds)
        return reply
    return _reply


def test_busy_provider_recovers_after_a_longer_wait(fake_llm, articles, no_retry_waits):
    fake_llm.replies["google"] = [APIConnectionError(request=None), writer_reply()]
    [sub] = run_writers(articles, load_settings(), TODAY, writers=[WRITERS[1]])
    assert sub.ok
    assert no_retry_waits == [10.0]
    assert fake_llm.providers_called() == ["google", "google"]


def test_busy_provider_gives_up_after_configured_waits(fake_llm, articles, no_retry_waits, monkeypatch):
    monkeypatch.setenv("LLM_RETRY_WAITS", "5,15,45")
    fake_llm.replies["google"] = APIConnectionError(request=None)
    [sub] = run_writers(articles, load_settings(), TODAY, writers=[WRITERS[1]])
    assert not sub.ok and "APIConnectionError" in sub.error
    assert no_retry_waits == [5.0, 15.0, 45.0]
    assert len(fake_llm.calls) == 4


def test_permanent_errors_are_not_retried(fake_llm, articles, no_retry_waits):
    fake_llm.replies["openai"] = OpenAIError("invalid model")
    [sub] = run_writers(articles, load_settings(), TODAY, writers=[WRITERS[0]])
    assert not sub.ok and len(fake_llm.calls) == 1 and no_retry_waits == []


def test_stuck_writer_is_cut_off_at_the_deadline(fake_llm, articles):
    s = load_settings()
    s.writers_deadline = 0.3
    fake_llm.replies.update({p: writer_reply() for p in ("openai", "deepseek", "openrouter")})
    fake_llm.replies["google"] = slow(3, writer_reply())

    started = time.monotonic()
    subs = {sub.agent: sub for sub in run_writers(articles, s, TODAY)}
    assert time.monotonic() - started < 1.5
    assert "writer deadline" in subs["google"].error
    assert all(subs[a].ok for a in ("openai", "deepseek", "openrouter"))


@pytest.fixture
def fake_search(monkeypatch, articles):
    monkeypatch.setattr(pipeline, "search_news", lambda s: articles)


def test_two_of_four_is_enough(fake_llm, fake_search):
    """One writer down, one stuck past the deadline: still a newsletter."""
    s = load_settings()
    s.writers_deadline = 0.3
    fake_llm.replies.update({
        "openai": writer_reply(),
        "google": APIConnectionError(request=None),
        "deepseek": slow(3, writer_reply()),
        "openrouter": writer_reply(),
        "cerebras": review_reply(),
    })
    letter = build_newsletter(s, TODAY)
    assert 600 <= letter.word_count <= 750


def test_one_of_four_is_not_enough(fake_llm, fake_search):
    s = load_settings()
    s.writers_deadline = 0.3
    fake_llm.replies.update({
        "openai": writer_reply(),
        "google": APIConnectionError(request=None),
        "deepseek": slow(3, writer_reply()),
        "openrouter": OpenAIError("bad key"),
    })
    with pytest.raises(PipelineError, match="Only 1 writer"):
        build_newsletter(s, TODAY)
