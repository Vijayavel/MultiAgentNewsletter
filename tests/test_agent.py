"""agent.py: exactly one email on success, none on failure, dry run, a declined approval,
or inside the 12-hour gap; every real send is recorded in the history file."""

import json
from datetime import timedelta

import pytest

import agent
from email_service import EmailResult
from newsletter.history import History, SentIssue, utc_now
from newsletter.pipeline import PipelineError
from newsletter.reviewer import Newsletter

LETTER = Newsletter(
    title="Today in AI",
    markdown="# Today in AI\n\n## Models\n\n**New model** — It shipped. ([Reuters](https://reuters.com/x))",
    word_count=650, rankings=[{"agent": "openai", "score": 8, "notes": "ok"}], item_count=7,
    headlines=["New model"], urls=["https://reuters.com/x"],
    html="<!doctype html><html><body><h1>Today in AI</h1></body></html>",
)


class FakeEmail:
    sent = []
    provider = "smtp"

    def __init__(self):
        pass

    def send(self, to, subject, body, **kw):
        FakeEmail.sent.append((to, subject, body))
        FakeEmail.kwargs = kw
        return EmailResult(True, FakeEmail.provider, to, message_id="m1")


@pytest.fixture
def run(monkeypatch):
    FakeEmail.sent, FakeEmail.provider = [], "smtp"
    builds = []
    monkeypatch.setattr(agent, "EmailService", FakeEmail)
    monkeypatch.setenv("NEWSLETTER_TO", "me@example.com")
    monkeypatch.setenv("EMAIL_REQUIRE_APPROVAL", "false")

    def _run(*argv, letter=LETTER):
        def build(settings, previous=None):
            builds.append(previous)
            if isinstance(letter, Exception):
                raise letter
            return letter
        monkeypatch.setattr(agent, "build_newsletter", build)
        return agent.main(list(argv))
    _run.builds = builds
    return _run


def sent_hours_ago(history_file, hours, subject="Earlier issue"):
    History(history_file).record(SentIssue(utc_now() - timedelta(hours=hours), subject, "# x", ["Old story"], ["https://old.example.com"]))


def test_sends_exactly_one_email(run):
    assert run() == 0
    assert FakeEmail.sent == [(["me@example.com"], "Today in AI", LETTER.html)]
    assert FakeEmail.kwargs == {"body_format": "html", "text": LETTER.markdown}  # HTML article + plain-text copy


def test_dry_run_sends_nothing_and_can_save_html(run, tmp_path, history_file):
    out = tmp_path / "preview.html"
    assert run("--dry-run", "--save-html", str(out)) == 0
    assert FakeEmail.sent == []
    assert not history_file.exists()
    assert out.read_text() == LETTER.html


def test_pipeline_failure_sends_nothing(run, history_file):
    assert run(letter=PipelineError("Only 1 writer(s) succeeded")) == 1
    assert FakeEmail.sent == [] and not history_file.exists()


def test_declined_or_unanswerable_approval_sends_nothing(run, monkeypatch):
    monkeypatch.setenv("EMAIL_REQUIRE_APPROVAL", "true")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run() == 1

    def no_stdin(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", no_stdin)
    assert run() == 1
    assert FakeEmail.sent == []


def test_to_flag_overrides_and_missing_recipients_fail(run, monkeypatch):
    assert run("--to", "a@example.com, b@example.com") == 0
    assert FakeEmail.sent[-1][0] == ["a@example.com", "b@example.com"]
    monkeypatch.delenv("NEWSLETTER_TO")
    assert run() == 1


# ---- sent-email history and the 12-hour gap ----

def test_real_send_is_recorded_with_timestamp_and_content(run, history_file):
    before = utc_now()
    assert run() == 0
    [saved] = json.loads(history_file.read_text())
    assert saved["subject"] == "Today in AI" and saved["markdown"] == LETTER.markdown
    assert saved["headlines"] == ["New model"] and saved["urls"] == ["https://reuters.com/x"]
    sent_at = History(history_file).recent()[0].sent_at
    assert before <= sent_at <= utc_now() and sent_at.tzinfo is not None


def test_second_run_within_12_hours_does_not_send_or_build(run, history_file):
    sent_hours_ago(history_file, 11.5)
    assert run() == 0
    assert FakeEmail.sent == [] and run.builds == []  # stopped before any API call


def test_run_after_12_hours_sends_and_passes_previous_issue_to_the_review(run, history_file):
    sent_hours_ago(history_file, 12.5)
    assert run() == 0
    assert len(FakeEmail.sent) == 1
    [previous] = run.builds
    assert [p.subject for p in previous] == ["Earlier issue"]


def test_back_to_back_runs_send_once(run):
    assert run() == 0
    assert run() == 0
    assert len(FakeEmail.sent) == 1


def test_dry_run_inside_the_gap_still_previews(run, history_file):
    sent_hours_ago(history_file, 1)
    assert run("--dry-run") == 0
    assert FakeEmail.sent == [] and len(run.builds) == 1


def test_email_dry_run_mode_is_not_recorded(run, history_file):
    FakeEmail.provider = "dry_run"  # EMAIL_DRY_RUN=true: logged by email_service, not delivered
    assert run() == 0
    assert not history_file.exists()


def test_gap_is_configurable(run, history_file, monkeypatch):
    monkeypatch.setenv("NEWSLETTER_MIN_GAP_HOURS", "1")
    sent_hours_ago(history_file, 2)
    assert run() == 0 and len(FakeEmail.sent) == 1


# ---- --resend: the latest issue again, in the current layout ----

from datetime import date as _date  # noqa: E402

from newsletter.layout import Issue, Item, Section, issue_to_dict, to_markdown  # noqa: E402
from newsletter.search import Article  # noqa: E402

SAMPLE = Issue(
    "Yesterday in AI", "What mattered yesterday.",
    [Section("Models", [Item("New model ships", "It shipped. " * 30, [Article("", "https://reuters.com/x", "Reuters", "", "")])])],
    "That's all.", _date(2026, 10, 16), words=640,
)


def record_sample(history_file, hours_ago=1, structured=True):
    History(history_file).record(SentIssue(
        utc_now() - timedelta(hours=hours_ago), SAMPLE.title, to_markdown(SAMPLE, "AI & Software Daily", "Content Agent"),
        ["New model ships"], ["https://reuters.com/x"], issue_to_dict(SAMPLE) if structured else None,
    ))


def test_resend_inside_the_gap_sends_latest_issue_in_current_layout(run, history_file):
    record_sample(history_file, hours_ago=1)
    before = history_file.read_text()
    assert run("--resend") == 0
    [(to, subject, html)] = FakeEmail.sent
    assert subject == "Yesterday in AI" and "Yesterday in AI</h1>" in html
    assert "color:#333333" in html and 'href="https://reuters.com/x"' in html
    assert FakeEmail.kwargs["body_format"] == "html" and "### New model ships" in FakeEmail.kwargs["text"]
    assert run.builds == []                      # no new content, no AI calls
    assert history_file.read_text() == before    # a resend is not a new issue


def test_resend_works_for_issues_saved_as_plain_text_only(run, history_file):
    record_sample(history_file, structured=False)
    assert run("--resend") == 0
    html = FakeEmail.sent[0][2]
    assert "New model ships</h3>" in html and 'href="https://reuters.com/x"' in html and ">Reuters</a>" in html


def test_resend_with_empty_history_fails(run):
    assert run("--resend") == 1 and FakeEmail.sent == []


def test_resend_dry_run_sends_nothing(run, history_file, tmp_path):
    record_sample(history_file)
    out = tmp_path / "resend.html"
    assert run("--resend", "--dry-run", "--save-html", str(out)) == 0
    assert FakeEmail.sent == [] and "Yesterday in AI</h1>" in out.read_text()


def test_normal_send_stores_structured_issue_for_later_resend(run, history_file):
    from dataclasses import replace
    assert run(letter=replace(LETTER, issue=SAMPLE)) == 0
    [saved] = json.loads(history_file.read_text())
    assert saved["issue"]["title"] == "Yesterday in AI"
    assert saved["issue"]["sections"][0]["items"][0]["sources"][0]["url"] == "https://reuters.com/x"


# ---- --regenerate: fresh content now, replacing the latest issue ----

def test_regenerate_inside_the_gap_builds_sends_and_replaces_latest(run, history_file):
    sent_hours_ago(history_file, 30, subject="Two days ago")
    sent_hours_ago(history_file, 2, subject="This morning")
    assert run("--regenerate") == 0
    assert len(FakeEmail.sent) == 1
    [previous] = run.builds
    assert [p.subject for p in previous] == ["Two days ago"]  # replaced issue isn't a repeat source
    assert [i.subject for i in History(history_file).recent()] == ["Today in AI", "Two days ago"]


def test_regenerate_with_no_history_is_a_normal_send(run, history_file):
    assert run("--regenerate") == 0
    assert [i.subject for i in History(history_file).recent()] == ["Today in AI"]


def test_regenerate_and_resend_cannot_be_combined(run):
    with pytest.raises(SystemExit):
        run("--regenerate", "--resend")
