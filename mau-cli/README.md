# MAU-CLI

**Multi-Agent Unit** — a terminal-native orchestrator that simulates an
engineering team and runs them against a single product request you type
in. You play Product. The CLI plays everyone else, and they **write
real code on disk**.

```
USER (you, the human Product stakeholder)
  └─ product               ← drafts a PRD as shared/prd.md
       └─ engineering_manager      ← decomposes into epics, engages a tech lead
            └─ tech_lead           ← publishes architecture.md, api-contract.md, schema.md
                 ├─ frontend (one or more)   ┐
                 ├─ backend  (one or more)   │  agentic mode: full Read/Write/Edit/Bash
                 ├─ database                 │  on workspace/ — they ship real files
                 ├─ qa                       │
                 └─ devops                   ┘
```

Vertical decomposition (top-down) and lateral coordination (peer-to-peer)
are both first-class. Agents block on dependencies, talk to each other
directly, and escalate up the chain when stuck. Token spend and cost are
tracked live; a `--max-budget` cap halts the run when reached.

Inference runs through whatever Claude / Codex CLI is already installed
and authenticated on your machine. There's also a deterministic mock
backend that produces a working sample workspace without spending tokens.

---

## Install

Python 3.10+.

```bash
cd mau-cli
pip install -e .
```

Or with `pipx` for an isolated install:

```bash
pipx install ./mau-cli
```

This puts `mau` on your PATH.

---

## Usage

```bash
# Real run with auto-detected Claude
mau "Build a recipe-sharing app with photo uploads and search"

# Interactive mode: prompts you for the initiative, then opens the live TUI
mau

# Pin to a backend
mau --backend claude  "..."
mau --backend codex   "..."
mau --backend mock    "..."   # offline / free demo

# Specify where the workspace lives (defaults to ./.mau/runs/<timestamp>/)
mau --workspace /tmp/my-app "..."

# Hard-cap spend in dollars
mau --max-budget 5.00 "..."

# Tuning
mau --max-turns 60 --max-agents 10 --concurrency 3 "..."

# Skip the live TUI; print events line by line
mau --no-tui "..."
```

By default, the CLI auto-detects the backend (`claude` → `codex` → `mock`)
and creates a fresh workspace at `./.mau/runs/<timestamp>/`.

---

## What lands on disk

After a run:

```
.mau/runs/2026-05-08-153012/
├── workspace/         ← real code, written by specialist agents
│   ├── migrations/
│   ├── server/
│   └── web/
├── shared/            ← cross-agent docs, written by planning agents
│   ├── prd.md
│   ├── architecture.md
│   ├── api-contract.md
│   └── schema.md
├── logs/              ← reserved for per-agent transcripts (future)
└── session.json       ← full WorldState — agents, tasks, messages, usage
```

The workspace directory is the team's output. You can `cd` into it,
inspect what they built, run their code, edit it, etc.

`session.json` is the full audit log: every message between agents, every
task with its dep graph, who delivered what, total token spend, etc.

---

## How a turn works

Two execution modes:

### Plan mode (Product, EM, Tech Lead)

One-shot inference call. The agent's full state (roster, tasks, inbox,
shared docs) is composed into a prompt; the agent returns **strict JSON**:

```json
{
  "thoughts": "Brief reasoning.",
  "status": "complete",
  "actions": [
    { "type": "write_doc",   "name": "prd.md",   "content": "..." },
    { "type": "spawn_agent", "role": "engineering_manager", "name": "em-1" },
    { "type": "create_task", "assignee": "fe-1", "depends_on": ["task_be"] },
    { "type": "send_message", "to": "em-1", "msg_type": "directive", "body": "..." },
    { "type": "complete", "summary": "..." }
  ]
}
```

The orchestrator dispatches each action sequentially, mutating shared
state. Only Product / EM / Tech Lead can `spawn_agent`, `create_task`,
or `write_doc` — specialists are pure executors.

### Agentic mode (Frontend, Backend, Database, QA, DevOps)

Full tool-using run inside the workspace directory:

- `claude -p` is launched with `cwd = workspace/`
- Tools allowed: `Read,Write,Edit,Glob,Grep,Bash`
- `--add-dir shared/` so agents can read PRDs and contracts
- The shared docs are also pasted inline into the prompt as belt-and-braces

The agent reads its task + the contracts, writes/edits files, and ends
its final message with one structured line:

```
<DELIVERABLE>{"title": "...", "summary": "...", "files_touched": ["..."]}</DELIVERABLE>
```

The orchestrator parses that line; the rest of the agent's text is logged
but doesn't drive control flow. Token usage and cost from the JSON
envelope are aggregated in `world.usage`.

---

## Dependency handling

Tasks declare `depends_on: [task_id...]`. An agent assigned a task whose
deps aren't all `complete` shows up `blocked`. When upstream delivers,
the orchestrator notifies every downstream assignee, who become ready
next tick.

If an agent receives an agentic turn but their deps aren't met, the
specialist prompt instructs them to return:

```
<DELIVERABLE>{"blocked": true, "reason": "..."}</DELIVERABLE>
```

instead of doing work. The orchestrator records the block and waits.

---

## Escalation policy

- Blocked for `ESCALATION_AFTER_BLOCKED_TURNS` (default: 3) consecutive
  ticks → auto-escalate to supervisor.
- Self-escalate via the `escalate` action.
- Supervisor chain ends at the user; unresolved escalations surface in
  the final "questions for you" panel.

---

## Concurrency

Up to `--concurrency` agents run their turn in parallel via a thread pool.
Action application is single-threaded so state mutation is deterministic.
Plan-mode calls are fast (~5–20s); agentic calls run longer (~30s – several
minutes depending on task scope).

---

## Cost notes

Each agent turn = one inference call. A typical run with real Claude:

| Role       | Mode    | Approx cost per turn |
|------------|---------|----------------------|
| Product    | plan    | ~$0.10–0.30 (one call) |
| EM         | plan    | ~$0.10–0.30 (one call) |
| Tech Lead  | plan    | ~$0.20–0.50 (publishes contracts) |
| Specialists| agentic | $0.30–2.00 each (depends on scope) |

So $1–10 for a moderate feature, depending on size. Use `--max-budget` to
hard-cap. Use `--backend mock` for free architecture demos.

Token / cost stats are visible:
- Live in the TUI header
- In `session.json` under `usage`
- In the final summary panel after the run

---

## Architecture

| Component           | File                      | Responsibility |
|---------------------|---------------------------|------|
| Inference adapter   | `inference.py`            | Wraps `claude -p` / `codex exec`; two modes (plan / agentic); usage capture |
| Mock backend        | `mock_inference.py`       | Deterministic scripted demo; writes real stub files in agentic mode |
| Schemas             | `schemas.py`              | `Message`, `Task`, `AgentState`, `TokenUsage`, `Workspace`, `WorldState` |
| Per-agent turn      | `agent.py`                | Composes prompt; dispatches plan vs agentic; extracts DELIVERABLE block |
| Message bus         | `message_bus.py`          | Inbox routing + audit log |
| Conductor           | `orchestrator.py`         | Turn loop, dep graph, action dispatch, persistence, budget cap |
| Workspace lifecycle | `schemas.Workspace`       | `workspace/`, `shared/`, `logs/`, `session.json` layout |
| TUI                 | `tui.py`                  | Rich `Live`: header / team / tasks / messages / events panels |
| CLI                 | `cli.py`                  | Click entry point; flags; final summary |
| Prompts             | `prompts/*.md`            | Shared agent protocol + 8 role-specific prompts |

---

## Try it

```bash
# Free demo (deterministic mock):
mau --backend mock "Build a TODO API with auth"
ls .mau/runs/*/workspace/   # real files

# Real Claude run, $5 cap:
mau --max-budget 5.00 "Build a TODO API with auth"
```

---

## Roadmap

- **Resume sessions.** `--resume <session.json>` to continue an interrupted run.
- **Specialist messaging.** Allow specialists to send messages mid-task
  (currently they only deliver or block).
- **Per-agent transcripts.** Write each agent's prompt+response stream to `logs/<agent>.jsonl`.
- **Pluggable backends.** OpenAI, Anthropic API direct, ollama for local models.
- **Single-binary distribution.** Rewrite in Go with bubbletea TUI.

---

## License

MIT.
