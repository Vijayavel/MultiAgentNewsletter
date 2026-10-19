"""Medium-style article layout: the HTML email and its plain-text version."""

import json
from datetime import date

from conftest import review_reply

from newsletter.layout import read_minutes
from newsletter.reviewer import render
from newsletter.search import Article

TODAY = date(2026, 10, 15)


def rendered(articles, **draft_changes):
    draft = {**json.loads(review_reply()), **draft_changes}
    return render(draft, articles, set(range(1, 13)), TODAY, publication="AI & Software Daily", author="Content Agent")


def test_html_has_article_structure_in_reading_order(articles):
    html = rendered(articles).html
    order = [
        "AI &amp; Software Daily",          # masthead
        "<h1", "Today in AI",               # title
        "intro intro",                      # subtitle
        ">CA<", "Content Agent", "5 min read &middot; Oct 15, 2026",  # byline
        "<h2", "Section 0",                 # first section
        "<h3", "Chipmaker ships accelerator",
        "&middot;&middot;&middot;",         # break before the next section
        "Section 1",
        "closing closing",                  # closing thought
        "Curated from today",               # footer
    ]
    # Each marker must appear after the previous one in the visible body (the head and the
    # hidden inbox preview line repeat the title and subtitle earlier).
    at = html.index('<table role="presentation"')
    for marker in order:
        at = html.index(marker, at) + 1
    assert html.count("<h3") == 7 and html.count("<h2") == 3


def test_html_is_email_safe(articles):
    html = rendered(articles).html
    assert "<style" not in html and "<script" not in html and "<link" not in html  # inline styles only
    assert 'role="presentation"' in html and "max-width:600px" in html
    assert "font-family:-apple-system" in html and "serif;" not in html.replace("sans-serif", "")  # one sans-serif family
    assert "color:#333333" in html and "font-size:11pt;line-height:17pt" in html  # soft grey, 11pt body text


def test_sources_are_real_links_under_each_story(articles):
    html = rendered(articles).html
    for i in range(1, 8):
        assert f'<a href="https://news{i}.example.com/a/{i}"' in html and f">Source{i}</a>" in html


def test_model_text_and_urls_are_escaped(articles):
    articles = list(articles)
    articles[0] = Article("t", 'https://x.example.com/?a=1&b="2"', "<b>Evil</b>", "", "")
    r = rendered(articles, title="AI <script>alert(1)</script> & you")
    assert "<script>" not in r.html
    assert "AI &lt;script&gt;alert(1)&lt;/script&gt; &amp; you" in r.html
    assert 'href="https://x.example.com/?a=1&amp;b=&quot;2&quot;"' in r.html
    assert "&lt;b&gt;Evil&lt;/b&gt;" in r.html


def test_plain_text_version_mirrors_the_article(articles):
    md = rendered(articles).markdown
    assert md.startswith("AI & SOFTWARE DAILY\n\n# Today in AI\n\n*intro")
    assert "Content Agent · 5 min read · Oct 15, 2026" in md
    assert "## Section 0" in md and "### Chipmaker ships accelerator" in md
    assert "Sources: [Source1](https://news1.example.com/a/1)" in md


def test_word_count_and_read_time(articles):
    r = rendered(articles)
    assert 600 <= r.words <= 750
    assert read_minutes(r.words) == 5 and read_minutes(700) == 5 and read_minutes(50) == 1


def test_issue_round_trips_through_history_forms(articles):
    from newsletter.layout import issue_from_dict, issue_from_markdown, issue_to_dict

    r = rendered(articles)
    issue = render(json.loads(review_reply()), articles, set(range(1, 13)), TODAY).issue
    again = issue_from_dict(json.loads(json.dumps(issue_to_dict(issue))))
    assert issue_to_dict(again) == issue_to_dict(issue)  # source snippets/dates are not stored

    from_text = issue_from_markdown(r.markdown, TODAY)
    assert from_text.title == issue.title and from_text.subtitle == issue.subtitle and from_text.closing == issue.closing
    assert [s.heading for s in from_text.sections] == [s.heading for s in issue.sections]
    assert [[i.headline for i in s.items] for s in from_text.sections] == [[i.headline for i in s.items] for s in issue.sections]
    assert [a.url for s in from_text.sections for i in s.items for a in i.sources] == \
           [a.url for s in issue.sections for i in s.items for a in i.sources]
    assert from_text.words == issue.words
