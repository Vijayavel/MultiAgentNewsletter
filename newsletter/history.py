"""
history.py - The last few newsletters actually sent, kept in a small JSON file.

Used for two things:
- the minimum gap between sends (NEWSLETTER_MIN_GAP_HOURS, default 12);
- giving the writers and the review agent what was already sent, so nothing repeats.

The file keeps at most NEWSLETTER_HISTORY_KEEP issues (default 2) and drops any issue
older than NEWSLETTER_HISTORY_MAX_AGE_HOURS (default 48) every time it is read or written.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("newsletter.history")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SentIssue:
    sent_at: datetime  # timezone-aware, UTC
    subject: str
    markdown: str
    headlines: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    issue: dict | None = None  # structured issue (layout.issue_to_dict) for re-rendering a resend

    def to_json(self) -> dict:
        return {**asdict(self), "sent_at": self.sent_at.astimezone(timezone.utc).isoformat()}

    @classmethod
    def from_json(cls, raw: dict) -> SentIssue:
        sent_at = datetime.fromisoformat(raw["sent_at"])
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        return cls(sent_at, raw.get("subject", ""), raw.get("markdown", ""),
                   list(raw.get("headlines") or []), list(raw.get("urls") or []), raw.get("issue"))


def format_previous(previous: list[SentIssue]) -> str:
    """Recently sent issues, with their send times, for the writers' and reviewer's prompts."""
    blocks = []
    for issue in previous:
        lines = "\n".join(f"- {h}" for h in issue.headlines)
        blocks.append(f"Sent {issue.sent_at:%Y-%m-%d %H:%M} UTC - \"{issue.subject}\":\n{lines}")
    return "\n\n".join(blocks)


class History:
    def __init__(self, path: Path, keep: int = 2, max_age: timedelta = timedelta(hours=48)):
        self.path = Path(path)
        self.keep = keep
        self.max_age = max_age

    def _prune(self, issues: list[SentIssue], now: datetime) -> list[SentIssue]:
        """Newest first; nothing older than max_age; at most `keep` issues."""
        fresh = [i for i in issues if now - i.sent_at <= self.max_age]
        return sorted(fresh, key=lambda i: i.sent_at, reverse=True)[: self.keep]

    def _read(self) -> list[SentIssue]:
        if not self.path.exists():
            return []
        try:
            return [SentIssue.from_json(r) for r in json.loads(self.path.read_text(encoding="utf-8"))]
        except (ValueError, KeyError, TypeError) as e:
            log.warning("Ignoring unreadable history file %s: %s", self.path, e)
            return []

    def _write(self, issues: list[SentIssue]) -> None:
        # Write to a temp file and swap it in, so a crash can't leave half a file behind.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".history-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([i.to_json() for i in issues], f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def recent(self, now: datetime | None = None) -> list[SentIssue]:
        """Issues still within the window, newest first. Old entries are deleted from the file."""
        now = now or utc_now()
        stored = self._read()
        issues = self._prune(stored, now)
        if len(issues) != len(stored) and self.path.exists():
            self._write(issues)
            log.info("History: removed %d old issue(s)", len(stored) - len(issues))
        return issues

    def last_sent_at(self, now: datetime | None = None) -> datetime | None:
        issues = self.recent(now)
        return issues[0].sent_at if issues else None

    def record(self, issue: SentIssue, now: datetime | None = None, replace_latest: bool = False) -> None:
        """Save a sent issue. replace_latest: it supersedes the newest stored issue (a regenerated send)."""
        now = now or utc_now()
        stored = self._prune(self._read(), now)
        if replace_latest and stored:
            stored = stored[1:]
        self._write(self._prune([issue, *stored], now))
        log.info("History: recorded %r sent at %s", issue.subject, issue.sent_at.isoformat())
