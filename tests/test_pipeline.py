"""End to end (mocked): search -> 4 writers -> review, and when NOT to produce a newsletter."""

from datetime import date
from types import SimpleNamespace

import pytest
from conftest import review_reply, writer_reply
from openai import APIConnectionError

from newsletter import pipeline, search
from newsletter.config import load_settings
from newsletter.pipeline import PipelineError, build_newsletter

TODAY = date(2026, 10, 13)
WRITER_PROVIDERS = ("openai", "google", "deepseek", "openrouter")


@pytest.fixture
def fake_search(monkeypatch, articles):
    monkeypatch.setattr(pipeline, "search_news", lambda s: articles)


def test_full_run_uses_all_five_agents_once_each(fake_llm, fake_search):
    for p in WRITER_PROVIDERS:
        fake_llm.replies[p] = writer_reply()
    fake_llm.replies["cerebras"] = review_reply()
    letter = build_newsletter(load_settings(), TODAY)
    assert sorted(fake_llm.providers_called()) == sorted([*WRITER_PROVIDERS, "cerebras"])
    assert 600 <= letter.word_count <= 750


def test_continues_with_two_writers(fake_llm, fake_search):
    fake_llm.replies.update({p: APIConnectionError(request=None) for p in ("google", "deepseek")})
    fake_llm.replies.update({"openai": writer_reply(), "openrouter": writer_reply(), "cerebras": review_reply()})
    assert build_newsletter(load_settings(), TODAY).item_count == 7


def test_stops_when_fewer_than_two_writers_succeed(fake_llm, fake_search):
    fake_llm.replies.update({p: APIConnectionError(request=None) for p in ("google", "deepseek", "openrouter")})
    fake_llm.replies["openai"] = writer_reply()
    with pytest.raises(PipelineError, match="Only 1 writer"):
        build_newsletter(load_settings(), TODAY)
    assert "cerebras" not in fake_llm.providers_called()


def test_stops_when_reviewer_fails(fake_llm, fake_search):
    for p in WRITER_PROVIDERS:
        fake_llm.replies[p] = writer_reply()
    fake_llm.replies["cerebras"] = APIConnectionError(request=None)
    with pytest.raises(PipelineError, match="Review agent failed"):
        build_newsletter(load_settings(), TODAY)


def test_stops_when_search_finds_too_little(monkeypatch, fake_llm, articles):
    monkeypatch.setattr(pipeline, "search_news", lambda s: articles[:3])
    with pytest.raises(PipelineError, match="Only 3 news articles"):
        build_newsletter(load_settings(), TODAY)
    assert fake_llm.calls == []


def test_search_dedupes_by_url_and_title_and_survives_a_failed_query(monkeypatch):
    def fake_post(url, json, headers, timeout):
        assert headers == {"X-API-KEY": "key-serper"} and json["tbs"] == "qdr:d"
        if json["q"] == "boom":
            raise search.requests.ConnectionError()
        news = [
            {"title": "A", "link": "https://x.com/a", "source": "X", "date": "1h", "snippet": "s"},
            {"title": "A", "link": "https://y.com/other", "source": "Y", "date": "1h", "snippet": "s"},
            {"title": "B", "link": "https://x.com/a/", "source": "X", "date": "1h", "snippet": "s"},
            {"title": "C", "link": "https://z.com/c", "source": "Z", "date": "1h", "snippet": "s"},
        ]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"news": news})

    monkeypatch.setattr(search.requests, "post", fake_post)
    s = load_settings()
    s.agents.queries = ["one", "boom", "two"]
    assert [a.title for a in search.search_news(s)] == ["A", "C"]


def test_search_requires_its_key(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY")
    with pytest.raises(search.SearchError, match="SERPER_API_KEY"):
        search.search_news(load_settings())
