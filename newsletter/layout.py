"""
layout.py - Turn a finished issue into a Medium-style article: an HTML email and a matching
plain-text (Markdown) version.

Email clients ignore most CSS, so the HTML uses tables and inline styles only, with one clean
sans-serif stack (system UI fonts) for everything, soft dark-grey text, bold headings and
roomy line spacing. Every piece of model-written text is HTML-escaped.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from html import escape
from urllib.parse import urlparse

from .search import Article

# Reading speed for "N min read", rounded up: 601-750 words (the newsletter's target) = 5 min.
WORDS_PER_MINUTE = 150

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, 'Helvetica Neue', Helvetica, Arial, sans-serif"
INK = "#333333"  # soft dark grey for all text, not pure black
MUTED = "#6b6b6b"
RULE = "#f2f2f2"
ACCENT = "#1a8917"  # Medium's green, used once: the avatar


@dataclass
class Item:
    headline: str
    body: str
    sources: list[Article]


@dataclass
class Section:
    heading: str
    items: list[Item] = field(default_factory=list)


@dataclass
class Issue:
    title: str
    subtitle: str
    sections: list[Section]
    closing: str
    day: date
    words: int = 0


def read_minutes(words: int) -> int:
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


def source_label(article: Article) -> str:
    return article.source or urlparse(article.url).netloc.removeprefix("www.")


def initials(name: str) -> str:
    letters = [w[0] for w in name.split() if w[:1].isalpha()]
    return ("".join(letters[:2]) or "N").upper()


def count_words(markdown: str) -> int:
    """Words a reader reads: link text counts, URLs and markdown syntax do not."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.\-]*", text))


def issue_words(issue: Issue) -> int:
    """Reading length: subtitle, headings, stories and closing (not the byline, sources or footer)."""
    return count_words(" ".join(
        [issue.subtitle, issue.closing]
        + [s.heading for s in issue.sections]
        + [f"{i.headline} {i.body}" for s in issue.sections for i in s.items]
    ))


# ---------- stored form (history file), so a sent issue can be re-rendered later ----------

def issue_to_dict(issue: Issue) -> dict:
    return {
        "title": issue.title, "subtitle": issue.subtitle, "closing": issue.closing,
        "day": issue.day.isoformat(), "words": issue.words,
        "sections": [
            {"heading": s.heading, "items": [
                {"headline": i.headline, "body": i.body,
                 "sources": [{"title": a.title, "url": a.url, "source": a.source} for a in i.sources]}
                for i in s.items
            ]}
            for s in issue.sections
        ],
    }


def issue_from_dict(d: dict) -> Issue:
    sections = [
        Section(s["heading"], [
            Item(i["headline"], i["body"],
                 [Article(a.get("title", ""), a["url"], a.get("source", ""), "", "") for a in i.get("sources", [])])
            for i in s.get("items", [])
        ])
        for s in d.get("sections", [])
    ]
    issue = Issue(d["title"], d.get("subtitle", ""), sections, d.get("closing", ""), date.fromisoformat(d["day"]))
    issue.words = d.get("words") or issue_words(issue)
    return issue


def issue_from_markdown(markdown: str, day: date) -> Issue:
    """Rebuild an issue from the plain-text version (for history saved before the structured form)."""
    blocks = [b.strip() for b in markdown.split("\n\n")]
    title, subtitle, closing, sections = "", "", "", []
    n = 0
    while n < len(blocks):
        b = blocks[n]
        if b.startswith("# ") and not title:
            title = b[2:]
        elif b.startswith("## "):
            sections.append(Section(b[3:]))
        elif b.startswith("### ") and sections:
            body = blocks[n + 1] if n + 1 < len(blocks) else ""
            links = blocks[n + 2] if n + 2 < len(blocks) and blocks[n + 2].startswith("Sources:") else ""
            sources = [Article("", url, label, "", "") for label, url in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", links)]
            sections[-1].items.append(Item(b[4:], body, sources))
            n += 2 if links else 1
        elif b.startswith("*") and b.endswith("*") and not sections and title and not subtitle:
            subtitle = b.strip("*")
        elif sections and b and not b.startswith(("---", "*Curated", "Sources:")):
            closing = b
        n += 1
    issue = Issue(title, subtitle, sections, closing, day)
    issue.words = issue_words(issue)
    return issue


# ---------- plain text (also the stored copy and the terminal preview) ----------

def to_markdown(issue: Issue, publication: str, author: str) -> str:
    parts = [
        publication.upper(),
        f"# {issue.title}",
        f"*{issue.subtitle}*" if issue.subtitle else "",
        f"{author} · {read_minutes(issue.words)} min read · {issue.day:%b %d, %Y}",
    ]
    for section in issue.sections:
        parts.append(f"## {section.heading}")
        for item in section.items:
            links = " · ".join(f"[{source_label(a)}]({a.url})" for a in item.sources)
            parts += [f"### {item.headline}", item.body, f"Sources: {links}"]
    parts += [issue.closing, "---", "*Curated from today's news by four independent AI writers and edited by a review agent.*"]
    return "\n\n".join(p for p in parts if p)


# ---------- HTML email ----------

def _p(text: str, style: str) -> str:
    return f'<p style="margin:0;{style}">{escape(text)}</p>'


def _item_html(item: Item) -> str:
    links = " &middot; ".join(
        f'<a href="{escape(a.url, quote=True)}" style="color:{INK};text-decoration:underline;">{escape(source_label(a))}</a>'
        for a in item.sources
    )
    return (
        f'<tr><td style="padding:14px 0 0 0;">'
        f'<h3 style="margin:0 0 4px 0;font-family:{SANS};font-size:12.5pt;line-height:17pt;font-weight:700;'
        f'letter-spacing:-0.1px;color:{INK};">{escape(item.headline)}</h3>'
        + _p(item.body, f"font-family:{SANS};font-size:11pt;line-height:17pt;color:{INK};")
        + f'<p style="margin:4px 0 0 0;font-family:{SANS};font-size:9pt;line-height:13pt;color:{MUTED};">'
        f"Sources: {links}</p></td></tr>"
    )


def _section_html(section: Section, first: bool) -> str:
    divider = "" if first else (
        f'<tr><td align="center" style="padding:20px 0 0 0;font-family:{SANS};font-size:14pt;'
        f'letter-spacing:8pt;color:{INK};">&middot;&middot;&middot;</td></tr>'
    )
    heading = (
        f'<tr><td style="padding:{"20px" if first else "8px"} 0 0 0;">'
        f'<h2 style="margin:0;font-family:{SANS};font-size:15pt;line-height:20pt;font-weight:700;'
        f'letter-spacing:-0.2px;color:{INK};">{escape(section.heading)}</h2></td></tr>'
    )
    return divider + heading + "".join(_item_html(i) for i in section.items)


def to_html(issue: Issue, publication: str, author: str) -> str:
    minutes = read_minutes(issue.words)
    preheader = escape(issue.subtitle[:140])
    sections = "".join(_section_html(s, n == 0) for n, s in enumerate(issue.sections))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{escape(issue.title)}</title></head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff;">
<tr><td align="center" style="padding:0 8pt;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;">

<tr><td style="padding:16px 0 10px 0;border-bottom:1px solid {RULE};font-family:{SANS};font-size:11pt;font-weight:700;letter-spacing:0.2px;color:{INK};">{escape(publication)}</td></tr>

<tr><td style="padding:18px 0 0 0;">
<h1 style="margin:0;font-family:{SANS};font-size:18pt;line-height:23pt;font-weight:700;letter-spacing:-0.3px;color:{INK};">{escape(issue.title)}</h1>
</td></tr>
<tr><td style="padding:6px 0 0 0;">{_p(issue.subtitle, f"font-family:{SANS};font-size:12pt;line-height:17pt;color:{MUTED};")}</td></tr>

<tr><td style="padding:12px 0 10px 0;border-bottom:1px solid {RULE};">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="32" height="32" align="center" valign="middle" style="width:32px;height:32px;border-radius:16px;background:{ACCENT};font-family:{SANS};font-size:9pt;font-weight:700;color:#ffffff;">{escape(initials(author))}</td>
<td style="padding-left:10px;font-family:{SANS};">
<div style="font-size:11pt;line-height:15pt;color:{INK};">{escape(author)}</div>
<div style="font-size:10pt;line-height:13pt;color:{MUTED};">{minutes} min read &middot; {issue.day:%b %d, %Y}</div>
</td></tr></table>
</td></tr>

{sections}

<tr><td style="padding:16px 0 0 0;">{_p(issue.closing, f"font-family:{SANS};font-size:11pt;line-height:17pt;color:{INK};")}</td></tr>

<tr><td style="padding:20px 0 16px 0;">
<div style="border-top:1px solid {RULE};padding-top:10px;font-family:{SANS};font-size:9pt;line-height:13pt;color:{MUTED};">
Curated from today&rsquo;s news by four independent AI writers and edited by a review agent.<br>
Sent to you by {escape(author)} &middot; {escape(publication)}
</div></td></tr>

</table></td></tr></table>
</body></html>"""
