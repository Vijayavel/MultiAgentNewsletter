"""
email_service.py - Provider-agnostic email sending for an AI agent.

Supported providers (set EMAIL_PROVIDER in .env):
    smtp    - any SMTP server (Brevo, Mailjet, Mailgun, Gmail, SES ...)
    brevo   - Brevo HTTP API
    resend  - Resend HTTP API

Features: address validation, recipient allowlist, markdown -> HTML,
attachments, retries with backoff, and a simple per-run rate limit.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import certifi
import requests

try:
    import markdown as _md  # optional: pip install markdown
except ImportError:
    _md = None

log = logging.getLogger("email_service")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class EmailError(Exception):
    """Raised when an email cannot be sent or is not allowed."""


@dataclass
class EmailResult:
    ok: bool
    provider: str
    recipients: list[str]
    message_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class EmailConfig:
    provider: str = field(default_factory=lambda: os.getenv("EMAIL_PROVIDER", "smtp").lower())
    sender: str = field(default_factory=lambda: os.getenv("EMAIL_FROM", ""))
    sender_name: str = field(default_factory=lambda: os.getenv("EMAIL_FROM_NAME", "AI Agent"))
    # SMTP
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    # HTTP API keys
    brevo_api_key: str = field(default_factory=lambda: os.getenv("BREVO_API_KEY", ""))
    resend_api_key: str = field(default_factory=lambda: os.getenv("RESEND_API_KEY", ""))
    # Safety
    allowed_recipients: list[str] = field(default_factory=lambda: _split(os.getenv("EMAIL_ALLOWED_RECIPIENTS", "")))
    allowed_domains: list[str] = field(default_factory=lambda: _split(os.getenv("EMAIL_ALLOWED_DOMAINS", "")))
    max_emails_per_run: int = field(default_factory=lambda: int(os.getenv("EMAIL_MAX_PER_RUN", "10")))
    max_recipients: int = field(default_factory=lambda: int(os.getenv("EMAIL_MAX_RECIPIENTS", "5")))
    dry_run: bool = field(default_factory=lambda: os.getenv("EMAIL_DRY_RUN", "false").lower() == "true")


def _split(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def markdown_to_html(text: str) -> str:
    """Agents usually write markdown; convert it for nicer emails."""
    if _md:
        body = _md.markdown(text, extensions=["tables", "fenced_code"])
    else:
        body = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return (
        '<html><body style="font-family:Arial,sans-serif;line-height:1.5;'
        f'max-width:680px;margin:auto;color:#222">{body}</body></html>'
    )


class EmailService:
    def __init__(self, config: EmailConfig | None = None):
        self.cfg = config or EmailConfig()
        self._sent_count = 0
        if not self.cfg.sender:
            raise EmailError("EMAIL_FROM is not set.")

    # ---------- public API ----------
    def send(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        body_format: str = "markdown",  # "markdown" | "html" | "text"
        attachments: list[str] | None = None,
        retries: int = 3,
        text: str | None = None,  # plain-text alternative; derived from body when omitted
    ) -> EmailResult:
        recipients = [to] if isinstance(to, str) else list(to)
        recipients = [r.strip() for r in recipients if r.strip()]
        try:
            self._check_allowed(recipients, subject, body)
        except EmailError as e:
            log.warning("Email blocked: %s", e)
            return EmailResult(False, self.cfg.provider, recipients, error=str(e))

        html, derived_text = self._render(body, body_format)
        text = text or derived_text

        if self.cfg.dry_run:
            log.info("[DRY RUN] to=%s subject=%r\n%s", recipients, subject, text[:500])
            self._sent_count += 1
            return EmailResult(True, "dry_run", recipients, message_id="dry-run")

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                msg_id = self._dispatch(recipients, subject, html, text, attachments or [])
                self._sent_count += 1
                log.info("Email sent via %s to %s", self.cfg.provider, recipients)
                return EmailResult(True, self.cfg.provider, recipients, message_id=msg_id)
            except (smtplib.SMTPAuthenticationError, EmailError) as e:
                last_err = e  # not worth retrying
                break
            except Exception as e:  # network / transient errors
                last_err = e
                wait = 2 ** attempt
                log.warning("Send attempt %d failed (%s); retrying in %ds", attempt, e, wait)
                time.sleep(wait)

        return EmailResult(False, self.cfg.provider, recipients, error=str(last_err))

    # ---------- guards ----------
    def _check_allowed(self, recipients: list[str], subject: str, body: str) -> None:
        if not recipients:
            raise EmailError("No recipients given.")
        if len(recipients) > self.cfg.max_recipients:
            raise EmailError(f"Too many recipients (max {self.cfg.max_recipients}).")
        if self._sent_count >= self.cfg.max_emails_per_run:
            raise EmailError("Per-run email limit reached.")
        if not subject.strip() or not body.strip():
            raise EmailError("Subject and body must not be empty.")
        for r in recipients:
            if not EMAIL_RE.match(r):
                raise EmailError(f"Invalid email address: {r}")
            addr, domain = r.lower(), r.lower().split("@")[1]
            if self.cfg.allowed_recipients or self.cfg.allowed_domains:
                if addr not in self.cfg.allowed_recipients and domain not in self.cfg.allowed_domains:
                    raise EmailError(f"Recipient not on allowlist: {r}")

    def _render(self, body: str, fmt: str) -> tuple[str, str]:
        if fmt == "html":
            return body, re.sub(r"<[^>]+>", "", body)
        if fmt == "text":
            return markdown_to_html(body), body
        return markdown_to_html(body), body  # markdown (default)

    # ---------- providers ----------
    def _dispatch(self, to, subject, html, text, attachments) -> str | None:
        p = self.cfg.provider
        if p == "smtp":
            return self._send_smtp(to, subject, html, text, attachments)
        if p == "brevo":
            return self._send_brevo(to, subject, html, text, attachments)
        if p == "resend":
            return self._send_resend(to, subject, html, text, attachments)
        raise EmailError(f"Unknown EMAIL_PROVIDER: {p}")

    def _send_smtp(self, to, subject, html, text, attachments):
        c = self.cfg
        msg = EmailMessage()
        msg["From"] = f"{c.sender_name} <{c.sender}>"
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        for path in attachments:
            data, name, ctype = _read_attachment(path)
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

        # Use certifi's CA bundle: python.org installs on macOS ship without one.
        ctx = ssl.create_default_context(cafile=certifi.where())
        if c.smtp_port == 465:
            server = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=30)
            server.starttls(context=ctx)
        with server:
            server.login(c.smtp_user, c.smtp_password)
            server.send_message(msg)
        return msg.get("Message-ID")

    def _send_brevo(self, to, subject, html, text, attachments):
        payload = {
            "sender": {"email": self.cfg.sender, "name": self.cfg.sender_name},
            "to": [{"email": r} for r in to],
            "subject": subject,
            "htmlContent": html,
            "textContent": text,
        }
        if attachments:
            payload["attachment"] = [
                {"name": n, "content": base64.b64encode(d).decode()}
                for d, n, _ in map(_read_attachment, attachments)
            ]
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={"api-key": self.cfg.brevo_api_key, "accept": "application/json"},
            timeout=30,
        )
        _raise_for(r)
        return r.json().get("messageId")

    def _send_resend(self, to, subject, html, text, attachments):
        payload = {
            "from": f"{self.cfg.sender_name} <{self.cfg.sender}>",
            "to": to,
            "subject": subject,
            "html": html,
            "text": text,
        }
        if attachments:
            payload["attachments"] = [
                {"filename": n, "content": base64.b64encode(d).decode()}
                for d, n, _ in map(_read_attachment, attachments)
            ]
        r = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {self.cfg.resend_api_key}"},
            timeout=30,
        )
        _raise_for(r)
        return r.json().get("id")


def _read_attachment(path: str) -> tuple[bytes, str, str]:
    p = Path(path)
    if not p.is_file():
        raise EmailError(f"Attachment not found: {path}")
    ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return p.read_bytes(), p.name, ctype


def _raise_for(resp: requests.Response) -> None:
    if resp.status_code in (401, 403):
        raise EmailError(f"Auth failed ({resp.status_code}): check your API key.")
    if resp.status_code == 400 or resp.status_code == 422:
        raise EmailError(f"Rejected by provider: {resp.text}")
    resp.raise_for_status()  # 5xx / 429 -> retried
