"""Review agent: ranking, dedupe, links, length enforcement and failure handling."""

import json
from datetime import date

import pytest
from conftest import review_reply, words
from openai import APIConnectionError

from newsletter.config import load_settings
from newsletter.reviewer import ReviewError, count_words, render, review
from newsletter.writers import Story, Submission

TODAY = date(2026, 10, 13)


def submissions():
    def sub(agent, sources):
        return Submission(agent, agent, "m", stories=[
            Story(f"{agent}-{n}", agent, f"H {agent} {n}", "S.", "W.", "T", src) for n, src in enumerate(sources, 1)
        ])
    return [sub("openai", [[1], [2], [3], [4]]), sub("google", [[5], [6], [7], [8]])]


def test_good_draft_is_accepted_with_rankings_and_real_links(fake_llm, articles):
    fake_llm.replies["cerebras"] = review_reply()
    letter = review(submissions(), articles, load_settings(), TODAY)

    assert 600 <= letter.word_count <= 750
    assert letter.item_count == 7
    assert letter.title == "Today in AI"
    assert [r["agent"] for r in letter.rankings] == ["openai", "google"]
    for i in range(1, 8):
        assert f"[Source{i}](https://news{i}.example.com/a/{i})" in letter.markdown
    assert fake_llm.providers_called() == ["cerebras"]


def test_prompt_contains_every_writers_stories(fake_llm, articles):
    fake_llm.replies["cerebras"] = review_reply()
    review(submissions(), articles, load_settings(), TODAY)
    user = fake_llm.calls[0][4]["messages"][1]["content"]
    assert '"writer": "openai"' in user and '"writer": "google"' in user
    assert "[8] Story 8 headline" in user


def test_too_short_draft_is_sent_back_for_revision(fake_llm, articles):
    fake_llm.replies["cerebras"] = [review_reply(body_words=20), review_reply()]
    letter = review(submissions(), articles, load_settings(), TODAY)
    assert 600 <= letter.word_count <= 750
    assert len(fake_llm.calls) == 2
    assert "must be 600-750 words" in fake_llm.calls[1][4]["messages"][1]["content"]


def test_slightly_long_draft_is_used_after_all_attempts(fake_llm, articles):
    fake_llm.replies["cerebras"] = review_reply(body_words=100)  # ~790 words: within 15% of the range
    letter = review(submissions(), articles, load_settings(), TODAY)
    assert 750 < letter.word_count <= 750 * 1.15
    assert len(fake_llm.calls) == 3


def test_far_off_length_is_rejected(fake_llm, articles):
    fake_llm.replies["cerebras"] = review_reply(body_words=10)
    with pytest.raises(ReviewError, match="length/quality bar"):
        review(submissions(), articles, load_settings(), TODAY)


def test_reviewer_api_failure_raises_review_error(fake_llm, articles):
    fake_llm.replies["cerebras"] = APIConnectionError(request=None)
    with pytest.raises(ReviewError, match="Review agent failed"):
        review(submissions(), articles, load_settings(), TODAY)


def test_render_drops_duplicates_and_uncited_sources(articles):
    allowed = {1, 2, 3, 4}
    draft = json.loads(review_reply(items=1))
    draft["sections"] = [{"heading": "News", "items": [
        {"headline": "Chip maker ships new GPU", "body": words(50), "sources": [1]},
        {"headline": "Same article, other angle", "body": words(50), "sources": [1]},     # same URL
        {"headline": "Chip maker ships new GPUs", "body": words(50), "sources": [2]},     # near-identical headline
        {"headline": "Invented source", "body": words(50), "sources": [11]},              # never cited by a writer
        {"headline": "Cloud outage hits region", "body": words(50), "sources": [3, 99]},  # 99 ignored
    ]}]
    r = render(draft, articles, allowed, TODAY)
    assert r.items == 2
    assert "Same article" not in r.markdown and "new GPUs" not in r.markdown and "Invented" not in r.markdown
    assert "news11.example.com" not in r.markdown
    assert r.headlines == ["Chip maker ships new GPU", "Cloud outage hits region"]
    assert r.urls == ["https://news1.example.com/a/1", "https://news3.example.com/a/3"]


def test_count_words_ignores_urls_and_markup():
    assert count_words("**Big news** — it shipped ([Reuters](https://www.reuters.com/x/y-z))") == 5
