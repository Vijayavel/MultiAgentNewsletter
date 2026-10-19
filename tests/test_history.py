"""History file: keeps the last two issues, deletes anything older than two days, and the
review agent and writers use it so nothing is repeated."""

import json
from datetime import date, datetime, timedelta, timezone

from conftest import WRITERS, review_reply

from newsletter.config import load_settings
from newsletter.history import History, SentIssue
from newsletter.reviewer import render, review
from newsletter.writers import Story, Submission, run_writers

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 15)


def issue(hours_ago, subject="Issue", headlines=("Old story",), urls=("https://old.example.com/1",)):
    return SentIssue(NOW - timedelta(hours=hours_ago), subject, f"# {subject}", list(headlines), list(urls))


def test_keeps_only_the_last_two_issues(history_file):
    h = History(history_file)
    for n, hours in enumerate([30, 20, 10]):
        h.record(issue(hours, f"Issue {n}"), now=NOW)
    assert [i.subject for i in h.recent(NOW)] == ["Issue 2", "Issue 1"]  # newest first
    assert len(json.loads(history_file.read_text())) == 2


def test_deletes_issues_older_than_two_days(history_file):
    h = History(history_file)
    h.record(issue(47, "Recent"), now=NOW)
    h.record(issue(5, "Today"), now=NOW)
    later = NOW + timedelta(hours=2)  # the 47-hour-old issue is now 49 hours old
    assert [i.subject for i in h.recent(later)] == ["Today"]
    assert [r["subject"] for r in json.loads(history_file.read_text())] == ["Today"]  # removed from disk too


def test_timestamps_are_stored_as_utc_iso(history_file):
    History(history_file).record(issue(1), now=NOW)
    [raw] = json.loads(history_file.read_text())
    assert raw["sent_at"] == "2026-10-15T11:00:00+00:00"


def test_missing_or_corrupt_file_is_treated_as_empty(history_file):
    h = History(history_file)
    assert h.recent(NOW) == [] and h.last_sent_at(NOW) is None
    history_file.write_text("{not json")
    assert h.recent(NOW) == []


def test_reviewer_prompt_lists_previous_issues_with_send_times(fake_llm, articles):
    fake_llm.replies["cerebras"] = review_reply()
    subs = [Submission("openai", "openai", "m", stories=[
        Story(f"openai-{n}", "openai", f"H{n}", "S.", "W.", "T", [n]) for n in range(1, 8)
    ])]
    review(subs, articles, load_settings(), TODAY, previous=[issue(14, "Yesterday's Pulse", ["GPU export rules tighten"])])
    system, user = (m["content"] for m in fake_llm.calls[0][4]["messages"])
    assert "Do not repeat a story they covered" in system
    assert 'Sent 2026-10-14 22:00 UTC - "Yesterday\'s Pulse"' in user
    assert "- GPU export rules tighten" in user


def test_items_already_sent_are_removed_and_reported(articles):
    previous = [issue(10, headlines=["Chipmaker ships accelerator"], urls=[articles[1].url])]
    draft = json.loads(review_reply())
    r = render(draft, articles, set(range(1, 13)), TODAY, previous)
    # item 1: same headline as a sent story; item 2: cites an article that was already sent
    assert r.repeats == ["Chipmaker ships accelerator", "Robotics startup raises funds"]
    assert r.items == 5
    assert "Chipmaker" not in r.markdown and articles[1].url not in r.markdown


def test_repeats_send_the_draft_back_with_feedback(fake_llm, articles):
    """First draft leans on yesterday's stories; the reviewer is told and revises."""
    subs = [Submission("openai", "openai", "m", stories=[
        Story(f"openai-{n}", "openai", f"H{n}", "S.", "W.", "T", [n]) for n in range(1, 10)
    ])]
    # Yesterday's email cited articles 1 and 2; the first draft reuses them, the revision does not.
    previous = [issue(10, headlines=["Unrelated older story"], urls=[articles[0].url, articles[1].url])]
    fake_llm.replies["cerebras"] = [review_reply(), review_reply(sources=[[i] for i in range(3, 10)])]
    letter = review(subs, articles, load_settings(), TODAY, previous)
    assert len(fake_llm.calls) == 2
    assert letter.item_count == 7 and articles[0].url not in letter.markdown
    assert "already covered them" in fake_llm.calls[1][4]["messages"][1]["content"]


def test_writers_are_told_what_was_already_sent(fake_llm, articles):
    from conftest import writer_reply
    fake_llm.replies["openai"] = writer_reply()
    run_writers(articles, load_settings(), TODAY, writers=WRITERS[:1], previous=[issue(10, headlines=["Old chip story"])])
    user = fake_llm.calls[0][4]["messages"][1]["content"]
    assert "Already sent in recent issues" in user and "- Old chip story" in user
