# Newsletter Agent Architecture

Every morning at 5:00 AM Pacific, four writer agents draft stories from a shared news search, a review agent turns the best of them into one 600–750 word article, and a single email goes out through Gmail. One config file defines the agents and their instructions, and a small history file keeps sends 12 hours apart and stops stories repeating.

## Diagram

```mermaid
flowchart TD
    GH["GitHub Actions<br/>cron 12:00 / 13:00 UTC, DST gate: 5 AM PT<br/>history kept in the Actions cache"]
    CLI["Local run<br/>python agent.py<br/>--dry-run --resend --regenerate --to"]
    SEC[("Secrets<br/>.env locally, GitHub secrets in CI")]
    HIST[("newsletter_history.json<br/>last 2 issues, none over 48 h old")]
    CFG[("config/agents.toml<br/>writers, angles, instructions, search queries")]

    ORCH["agent.py: orchestrator<br/>12-hour gap check, build, preview, approval,<br/>send one email, record in history"]

    GH --> ORCH
    CLI --> ORCH
    SEC -.->|keys| ORCH
    HIST -.->|read before a run| ORCH
    ORCH -.->|written after each real send| HIST

    subgraph PIPE["newsletter/ pipeline.py"]
        SEARCH["search.py: shared search<br/>17 queries, news (past day or week)<br/>and web pages for events, about 115 articles"]

        subgraph WRITERS["writers.py: 4 agents in parallel, 8-minute deadline, retries after 10 s and 30 s"]
            W1["OpenAI gpt-5.4-mini<br/>frontier models and research"]
            W2["Google gemini-flash-latest<br/>AI products and business"]
            W3["DeepSeek deepseek-flash<br/>open source, dev tools, AI apps"]
            W4["OpenRouter qwen/qwen3.7-plus<br/>architecture, jobs, Bay Area conferences"]
        end

        GATE{"2+ writers<br/>succeeded?"}
        REVIEW["reviewer.py: Cerebras gpt-oss-120b<br/>scores writers, merges the best stories,<br/>Apps and Upcoming events sections, skips repeats"]
        CHECKS["Code checks (not the model)<br/>article numbers to real links,<br/>repeats and already-sent stories dropped"]
        LAYOUT["layout.py: Medium-style article<br/>11pt sans-serif, HTML + plain-text copy"]
    end

    SERPER[("Serper API<br/>Brave as an alternative")]
    STOP["Stop: no email sent<br/>run exits 1, GitHub alerts you"]
    EMAIL["email_service.py: guardrails<br/>allowlist, approval prompt, dry run"]
    GMAIL["Gmail SMTP<br/>App Password, TLS via certifi"]
    INBOX(["Inbox, about 5:00 AM PT"])

    ORCH -->|"last send 12 h+ ago, or --regenerate"| SEARCH
    ORCH -.->|"--resend: latest issue from history, no AI calls"| LAYOUT
    CFG -.-> SEARCH
    CFG -.-> WRITERS
    CFG -.-> REVIEW
    SEARCH <--> SERPER
    SEARCH --> WRITERS
    WRITERS -->|"4-6 stories each, citing article numbers"| GATE
    GATE -->|no| STOP
    GATE -->|yes| REVIEW
    REVIEW -->|"outside 600-750 words: revise, up to 3 drafts"| REVIEW
    REVIEW -->|failed| STOP
    REVIEW --> CHECKS --> LAYOUT --> EMAIL --> GMAIL --> INBOX
```

Solid arrows are the normal path. Dotted arrows are configuration, history and the resend path.

## How a run works

1. GitHub Actions starts at 12:00 and 13:00 UTC. A daylight saving check lets only the 5:00 AM Pacific run continue, and the sent-email history is restored from the Actions cache.
2. `agent.py` reads the history. If the last email went out less than 12 hours ago, it stops here and sends nothing, unless the run was started with `--regenerate`.
3. `search.py` runs the queries in `config/agents.toml`: news searches over the past day or week, plus web searches for upcoming Bay Area events. It numbers the unique results, typically about 115.
4. The enabled writers in `agents.toml` run at the same time, each with its own angle, and return 4–6 stories that cite article numbers. Writers whose angle covers apps or events add those stories; all of them skip recent headlines.
5. If at least two writers succeed, the Cerebras reviewer scores them and writes one issue from the best stories, with Apps and Upcoming events sections when writers supply them, avoiding anything the last two emails covered.
6. Code swaps article numbers for real links, drops repeats and already-sent stories, and counts words. Drafts outside 600–750 words go back for revision.
7. `layout.py` turns the issue into a Medium-style article; `email_service.py` sends it through Gmail, and the issue is saved to history.

## When it sends, and when it doesn't

| Situation | Result |
| --- | --- |
| 4, 3 or 2 writers succeed | **Sends** one email |
| Last email under 12 h ago | Skips before any API call; exits normally |
| `--regenerate` | **Sends** a fresh issue now, ignoring the gap, and replaces the latest one in history |
| `--resend` | **Sends** the latest issue again in the current layout; no AI calls, gap not applied |
| Provider busy or unreachable | Retried after 10 s, then 30 s |
| Writer still running at 8 min | Skipped; the others continue |
| 0 or 1 writers succeed | **No email**, run fails |
| Fewer than 8 articles found | **No email**, run fails |
| Reviewer fails, or length stays far off | **No email**, run fails |
| `config/agents.toml` has a mistake | **No email**, run fails before any API call |

## Components

| File | Role | External service |
| --- | --- | --- |
| `agent.py` | Entry point: 12-hour gap, preview, approval prompt, a single send, resend, regenerate, exit code | None |
| `config/agents.toml` | Writer roster and angles, search queries, and every instruction the agents follow | None |
| `newsletter/agents.py` | Loads and validates `agents.toml`; stops the run on any mistake | None |
| `newsletter/config.py` | Providers and runtime settings, read from environment variables | None |
| `newsletter/history.py` | Last 2 sent issues with UTC timestamps; deletes anything over 48 hours old | None (a JSON file; Actions cache in CI) |
| `newsletter/search.py` | Shared news and web search with duplicate removal | Serper (or Brave) |
| `newsletter/llm.py` | One JSON call to any OpenAI-compatible API, with timeouts and retries | All five model providers |
| `newsletter/writers.py` | Writer agents in parallel, each with an editorial angle | OpenAI, Google, DeepSeek, OpenRouter |
| `newsletter/reviewer.py` | Ranking, merging, Apps and events sections, repeat checks, length control | Cerebras |
| `newsletter/layout.py` | Medium-style HTML article and its plain-text copy | None |
| `newsletter/pipeline.py` | Runs the steps and decides whether the result is fit to send | None |
| `email_service.py` | Allowlist, dry run, HTML and plain-text email | Gmail SMTP |

## Configuration

- **`config/agents.toml`**: what the agents do. Writers, angles, search queries and every instruction.
- **Environment variables** (`.env` locally, GitHub secrets in CI): keys, models, timeouts, word range, history size and the 12-hour gap. See `.env.example`.

Neither needs a code change.
