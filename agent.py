"""
agent.py - Build today's multi-agent newsletter and send it as ONE email.

Four writer agents (OpenAI, Google, DeepSeek, OpenRouter) draft stories from a
shared news search; a review agent (Cerebras) ranks them and writes the final
newsletter; EmailService delivers it with the existing Gmail guardrails.

Usage:
    python agent.py                     # build, show, approve, send
    python agent.py --dry-run           # build and preview only; never sends
    python agent.py --dry-run --save-html preview.html
    python agent.py --to someone@example.com
    python agent.py --resend            # resend the latest sent issue in the current layout
    python agent.py --regenerate        # write a fresh issue now and replace the latest one

A normal run within NEWSLETTER_MIN_GAP_HOURS (12) of the last send does nothing, so the schedule
never sends twice. --resend is the explicit way to send the latest issue again: no new content,
no AI calls, the minimum gap does not apply, and history is left as it is.
--regenerate is the explicit way to rebuild the latest issue with fresh content (for example after
editing config/agents.toml): it ignores the gap, checks repeats against the issues before the one it
replaces, and takes that issue's place in history.
"""

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
from email_service import EmailService  # noqa: E402  (after load_dotenv)
from newsletter.agents import AgentsConfigError  # noqa: E402
from newsletter.config import load_settings  # noqa: E402
from newsletter.history import History, SentIssue, utc_now  # noqa: E402
from newsletter.layout import issue_from_dict, issue_from_markdown, issue_to_dict, to_html, to_markdown  # noqa: E402
from newsletter.pipeline import PipelineError, build_newsletter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent")


def approve(to: list[str], subject: str) -> bool:
    """Human-in-the-loop check before anything leaves the system (the body is printed above)."""
    if os.getenv("EMAIL_REQUIRE_APPROVAL", "true").lower() != "true":
        return True
    print("\n" + "=" * 60)
    print(f"TO:      {', '.join(to)}")
    print(f"SUBJECT: {subject}")
    print("=" * 60)
    try:
        return input("Send this email? [y/N]: ").strip().lower() == "y"
    except EOFError:  # no one at the keyboard (e.g. CI): never send unapproved
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-agent AI newsletter")
    parser.add_argument("--dry-run", action="store_true", help="build and preview only; never send")
    parser.add_argument("--to", help="comma-separated recipients (default: NEWSLETTER_TO)")
    parser.add_argument("--save-html", metavar="PATH", help="also write the email's HTML to this file")
    redo = parser.add_mutually_exclusive_group()
    redo.add_argument("--resend", action="store_true",
                      help="resend the latest issue from history, re-rendered in the current layout")
    redo.add_argument("--regenerate", action="store_true",
                      help="write a fresh issue now, ignoring the 12-hour gap, and replace the latest one")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except AgentsConfigError as e:
        log.error("Agents config problem, nothing sent: %s", e)
        return 1
    if args.to:
        settings.to = [a.strip() for a in args.to.split(",") if a.strip()]
    if not settings.to and not args.dry_run:
        log.error("No recipients: set NEWSLETTER_TO or pass --to.")
        return 1

    history = History(settings.history_file, settings.history_keep, timedelta(hours=settings.history_max_age_hours))
    if args.resend:
        return resend(history, settings, args)

    # Checked before any API call, so a run inside the gap costs nothing. --regenerate is the
    # explicit override: it replaces the latest issue rather than adding a second one.
    wait = None if args.regenerate else too_soon(history, settings)
    if wait and not args.dry_run:
        log.info("Not sending: %s Use --resend to send the latest issue again.", wait)
        return 0
    if wait:
        log.info("Dry run continues, but a real send would be skipped: %s", wait)

    previous = history.recent()
    if args.regenerate and previous:
        log.info("Regenerating: will replace %r (sent %s UTC)", previous[0].subject, f"{previous[0].sent_at:%Y-%m-%d %H:%M}")
        previous = previous[1:]  # the issue being replaced is not a repeat source
    log.info("Checking against %d recently sent issue(s)", len(previous))
    try:
        letter = build_newsletter(settings, previous=previous)
    except PipelineError as e:
        log.error("Newsletter not sent: %s", e)
        return 1

    print("\n" + letter.markdown + "\n")
    print(f"[{letter.word_count} words, {letter.item_count} stories]")
    for r in letter.rankings:
        print(f"  reviewer score  {r.get('agent', '?'):<11} {r.get('score', '?')}/10  {r.get('notes', '')}")
    if args.save_html:
        Path(args.save_html).write_text(letter.html, encoding="utf-8")
        print(f"HTML preview written to {args.save_html}")

    if args.dry_run:
        print("Dry run: nothing was sent.")
        return 0
    if not approve(settings.to, letter.title):
        log.info("Not sent: declined at approval prompt.")
        return 1
    # Check again: another run may have sent while this one was being written.
    wait = None if args.regenerate else too_soon(history, settings)
    if wait:
        log.info("Not sending: %s", wait)
        return 0

    # Medium-style HTML article, with the Markdown version as the plain-text alternative.
    result = EmailService().send(
        to=settings.to, subject=letter.title, body=letter.html, body_format="html", text=letter.markdown,
    )
    if not result.ok:
        log.error("Send failed: %s", result.error)
        return 1
    if result.provider == "dry_run":  # EMAIL_DRY_RUN=true: logged, not delivered, so not recorded
        return 0
    try:
        history.record(SentIssue(
            utc_now(), letter.title, letter.markdown, letter.headlines, letter.urls,
            issue_to_dict(letter.issue) if letter.issue else None,
        ), replace_latest=args.regenerate)
    except OSError as e:
        log.error("Email sent, but saving it to %s failed: %s", settings.history_file, e)
        return 1
    return 0


def resend(history: History, settings, args) -> int:
    """Send the most recent issue again, rebuilt in the current layout. Not recorded as a new issue."""
    issues = history.recent()
    if not issues:
        log.error("Nothing to resend: no issue sent in the last %g hours.", settings.history_max_age_hours)
        return 1
    last = issues[0]
    # Issues saved before the structured form only have the plain text; rebuild from that.
    issue = issue_from_dict(last.issue) if last.issue else issue_from_markdown(last.markdown, last.sent_at.date())
    html = to_html(issue, settings.publication, settings.author)
    text = to_markdown(issue, settings.publication, settings.author)
    log.info("Resending %r (first sent %s UTC) in the current layout", issue.title, f"{last.sent_at:%Y-%m-%d %H:%M}")
    print("\n" + text + "\n")
    if args.save_html:
        Path(args.save_html).write_text(html, encoding="utf-8")
        print(f"HTML preview written to {args.save_html}")
    if args.dry_run:
        print("Dry run: nothing was sent.")
        return 0
    if not approve(settings.to, issue.title):
        log.info("Not sent: declined at approval prompt.")
        return 1
    result = EmailService().send(to=settings.to, subject=issue.title, body=html, body_format="html", text=text)
    if not result.ok:
        log.error("Resend failed: %s", result.error)
    return 0 if result.ok else 1


def too_soon(history: History, settings) -> str | None:
    """Why a send now would break the minimum gap, or None if it is fine."""
    now = utc_now()
    last = history.last_sent_at(now)
    gap = timedelta(hours=settings.min_gap_hours)
    if last is None or now - last >= gap:
        return None
    hours = (now - last).total_seconds() / 3600
    return (
        f"the last newsletter went out {hours:.1f} h ago ({last:%Y-%m-%d %H:%M} UTC); "
        f"the minimum gap is {settings.min_gap_hours:g} h, so the next send is allowed after "
        f"{last + gap:%Y-%m-%d %H:%M} UTC."
    )


if __name__ == "__main__":
    sys.exit(main())
