# Multi-agent AI newsletter

Every morning, four AI writer agents draft stories from the day's news, a review agent
ranks them and writes one ~5-minute newsletter (600–750 words), and it goes out as a
single email through Gmail.

For the full system design (diagram, run walkthrough, failure rules, components), see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
news search (Serper or Brave)
        │  numbered list of today's articles
        ▼
┌──────────── writers, in parallel ────────────┐
│ OpenAI      frontier models & research        │
│ Google      AI products & business            │
│ DeepSeek    open source & developer tools     │
│ OpenRouter  architecture, infra, security     │
└───────────────────────────────────────────────┘
        │  4–6 stories each, citing article numbers
        ▼
review agent (Cerebras): score writers, pick and merge the best stories,
write the newsletter, revise until it is 600–750 words
        │
        ▼
layout.py: Medium-style article (HTML email + plain-text copy)
        │
        ▼
email_service.py: allowlist, approval prompt, dry run → Gmail
```

Writers cite articles by number and the code turns numbers into links, so no agent can
invent a source. A writer that fails or misses the 8-minute deadline is skipped; if fewer than two writers succeed, or the
review fails, **no email is sent** and the run exits with an error.

## Run it locally

### 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

### 2. Create your `.env` from the template

`.env.example` is the template that ships with the repo: every setting, with placeholders
instead of real values. Copy it to `.env` and fill in your own values. `.env` is listed in
`.gitignore`, so it stays on your machine and is never committed.

```bash
cp .env.example .env          # macOS / Linux
copy .env.example .env        # Windows (Command Prompt)
```

Then open `.env` and replace the placeholders. Only these lines need your values; the rest
already have working defaults. The values below are made-up examples, not real keys:

```dotenv
# Writer agents
OPENAI_API_KEY=sk-proj-EXAMPLE-not-a-real-key-0000000000
GOOGLE_API_KEY=AIzaSy-EXAMPLE-not-a-real-key-000000
DEEPSEEK_API_KEY=sk-EXAMPLE-not-a-real-key-00000000
OPENROUTER_API_KEY=sk-or-v1-EXAMPLE-not-a-real-key-0000000000
# Review agent
CEREBRAS_API_KEY=csk-EXAMPLE-not-a-real-key-00000000
# News and web search
SERPER_API_KEY=EXAMPLE-not-a-real-serper-key-0000000

# Who receives the newsletter (comma-separated), and who may receive it
NEWSLETTER_TO=you@example.com
EMAIL_ALLOWED_RECIPIENTS=you@example.com

# Gmail account that sends it
EMAIL_FROM=you@gmail.com
SMTP_USER=you@gmail.com
SMTP_PASSWORD=abcdabcdabcdabcd
```

Where to get each value:

| Setting | Where to get it |
| --- | --- |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `GOOGLE_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `DEEPSEEK_API_KEY` | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) (the account needs credits) |
| `CEREBRAS_API_KEY` | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) |
| `SMTP_PASSWORD` | A 16-character Gmail **App Password** (not your normal password) from [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). 2-Step Verification must be on. Paste it without spaces. |

Leave `EMAIL_DRY_RUN=true` until a `--dry-run` preview looks right, then set it to `false`.
If you only have some of the writer keys, the newsletter still sends as long as two writers
succeed; switch the others off with `enabled = false` in `config/agents.toml`.

### 3. Run

```bash
.venv/bin/python agent.py --dry-run                         # preview only, never sends
.venv/bin/python agent.py --dry-run --save-html preview.html
.venv/bin/python agent.py                                   # build, approve, send one email
.venv/bin/python agent.py --resend                          # send the latest issue again, current layout
.venv/bin/python -m pytest                                  # tests (all APIs mocked)
```

A run takes 1–3 minutes, mostly waiting on the writers.

## Configuration

Settings live in two places:

- **`config/agents.toml`**: what the agents do. The editorial brief, the search queries, the
  writer roster (name, provider, angle, on/off), story and item counts, and every instruction
  the writers and the reviewer receive, including the reviewer's revision feedback.
- **Environment variables** (`.env`, or GitHub secrets in CI): keys, models, timeouts, the word
  range, history and email settings. `.env.example` lists every one with its default.

### Editing `config/agents.toml`

Prompts use `$name` placeholders that the code fills in; each template's comment lists the ones
it may use, and `$$` is a literal dollar sign. The file is checked when a run starts: an unknown
provider or placeholder, a missing section, a duplicate writer, or fewer enabled writers than
`NEWSLETTER_MIN_WRITERS` stops the run with a clear message before any API call. Keep the JSON
shapes in the prompts as they are; the code reads those keys.

| Purpose | Variables |
| --- | --- |
| Writer keys | `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` |
| Review key | `CEREBRAS_API_KEY` |
| News search | `SEARCH_PROVIDER` (`serper` or `brave`), `SERPER_API_KEY` / `BRAVE_API_KEY`, `NEWS_FRESHNESS` |
| Models | `OPENAI_MODEL`, `GOOGLE_MODEL`, `DEEPSEEK_MODEL`, `OPENROUTER_MODEL`, `CEREBRAS_MODEL`; `<PROVIDER>_BASE_URL` |
| Newsletter | `NEWSLETTER_NAME` (masthead), `NEWSLETTER_TO`, `NEWSLETTER_MIN_WRITERS`, `NEWSLETTER_WORDS_MIN` / `_MAX`, `NEWSLETTER_AGENTS_FILE` |
| History | `NEWSLETTER_MIN_GAP_HOURS` (12), `NEWSLETTER_HISTORY_KEEP` (2), `NEWSLETTER_HISTORY_MAX_AGE_HOURS` (48), `NEWSLETTER_HISTORY_FILE` |
| Reliability | `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_RETRY_WAITS` (busy, rate-limited or unreachable providers are retried after 10s, then 30s), `WRITERS_DEADLINE_SECONDS` (writers still running after 8 minutes are skipped) |
| Email | `EMAIL_*`, `SMTP_*` (unchanged; see `email_service.py`) |

## Sent-email history

Every email that is actually delivered is saved to `newsletter_history.json` with its UTC send
time, subject, full content, headlines and source links. The file keeps the last 2 emails and
deletes anything older than 2 days each time it is read or written.

- **No repeats.** The writers and the review agent see recent issues with their send times and
  are told not to repeat them. The code then removes any item that cites an already-sent link or
  has a near-identical headline, and sends the draft back for revision if that leaves it short.
- **12-hour gap.** A run within 12 hours of the last send stops before calling any API and exits
  normally. `--dry-run` still previews.
- **Resend.** `--resend` sends the latest stored issue again, rebuilt in the current layout. It makes
  no AI calls, ignores the 12-hour gap, and does not add a new history entry.
- In GitHub Actions the file is carried between runs with the Actions cache.

## Add a writer

- **On an existing provider:** add a `[[writers]]` entry with its angle to `config/agents.toml`.
- **On a new provider:** also add a `Provider` to `PROVIDERS` in `newsletter/config.py` (key
  variable, default model, OpenAI-compatible base URL).

To switch a writer off, set `enabled = false`.

## Daily schedule

`.github/workflows/daily-newsletter.yml` runs at 5:00 AM Pacific all year. Add these
repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY` | Model provider keys |
| `SERPER_API_KEY` | News and web search key |
| `GMAIL_ADDRESS` | The Gmail account that sends the newsletter |
| `SMTP_PASSWORD` | That account's 16-character App Password |
| `NEWSLETTER_TO` | Recipient address(es), comma-separated; also the allowlist |

## Keeping secrets private

Nothing personal is in the repository: keys, passwords and email addresses live only in your
local `.env` and in GitHub repository secrets, which GitHub masks in public run logs.

- `.gitignore` excludes every `.env` variant except `.env.example` (placeholders only), plus the
  sent-email history and HTML previews, which hold your newsletter content.
- `tests/test_no_secrets_committed.py` reads your local `.env` and fails if any key, password or
  email address from it appears in a file git would commit. Run `pytest` before you push.
- Git records an author email in every commit. Use your GitHub no-reply address for this repo:
  `git config user.email "<id>+<username>@users.noreply.github.com"` (shown under GitHub
  **Settings → Emails**, with "Keep my email addresses private" switched on).
