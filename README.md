# MAU-CLI

**Multi-Agent Unit** — a terminal-native orchestrator that simulates an
engineering team and runs them against a single product request you type
in. You play Product. The CLI plays everyone else, and they **write
real code on disk**.

## 30-second demo

```bash
pip install -e .
mau --backend mock "Build a TODO API with auth"
```

That writes a working stub project under `./.mau/runs/<timestamp>/workspace/`
using a deterministic mock backend — no API calls, no spend. Replace
`--backend mock` with your real Claude or Codex CLI to ship actual code.

## What it is

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

Inference runs through whatever Claude or Codex CLI is already installed
and authenticated on your machine. There's also a deterministic mock
backend that produces a working sample workspace without spending tokens.

For the full role responsibilities and inter-agent communication patterns,
see [engineering-communication-flow.md](./engineering-communication-flow.md).

## Install

Python 3.10+.

```bash
pip install -e .
# or, isolated:
pipx install .
```

This puts `mau` on your PATH. You'll also need the
[Claude](https://docs.claude.com/en/docs/claude-code) or
[Codex](https://github.com/openai/codex) CLI installed and
authenticated locally — unless you stay on `--backend mock`.

## Usage

```bash
# Greenfield: spin up a new project from scratch
mau "Build a recipe-sharing app with photo uploads and search"

# Interactive: prompt for the request, then drop into the live TUI
mau

# Brownfield: work inside an existing repo
mau --in . "Add a search endpoint and a UI for it"

# Resume the most recent interrupted run
mau --resume

# Free demo (deterministic, no API spend)
mau --backend mock "Build a TODO API with auth"

# Hard-cap the spend in dollars
mau --max-budget 5.00 "..."
```

Backend auto-detects in the order `claude → codex → mock`. Override with
`--backend {claude|codex|mock}`.

## Greenfield vs brownfield

**Greenfield** (default): agents create a fresh `workspace/` under
`./.mau/runs/<timestamp>/`. Nothing in your current directory is touched.

**Brownfield** (`--in <path>`): agents work *inside* the existing project at
`<path>` and write code to its root. Run metadata (PRDs, contracts, session
log) lives at `<path>/.mau/runs/<timestamp>/`. If a `.gitignore` exists,
`.mau/` is appended automatically. Pass `--in` with no value to use the
current directory.

`--in` and `--workspace` are mutually exclusive — pick one.

## What lands on disk

Greenfield:

```
.mau/runs/2026-05-08-153012/
├── workspace/         ← real code, written by specialist agents
│   ├── migrations/
│   ├── server/
│   └── web/
├── shared/            ← cross-agent docs (PRD, architecture, API, schema)
├── logs/
└── session.json       ← full WorldState — agents, tasks, messages, usage
```

Brownfield:

```
<your-project>/        ← agents write code here, alongside your existing files
└── .mau/runs/2026-05-08-153012/
    ├── shared/        ← cross-agent docs
    ├── logs/
    └── session.json
```

`session.json` is the complete audit log: every message between agents,
every task with its dependency graph, who delivered what, total token
spend. It's also what `--resume` reads to pick up an interrupted run.

## How a turn works (skim)

Two execution modes:

- **Plan mode** (Product, Engineering Manager, Tech Lead): one-shot
  inference returning strict JSON with actions like `spawn_agent`,
  `create_task`, `write_doc`, `send_message`, `complete`. Fast (~5–20s).
- **Agentic mode** (Frontend, Backend, Database, QA, DevOps): full
  tool-using `claude -p` / `codex exec` run inside the workspace with
  `Read,Write,Edit,Glob,Grep,Bash`. Each specialist ends its final
  message with a `<DELIVERABLE>{...}</DELIVERABLE>` block the
  orchestrator parses deterministically. Slower (~30s – several minutes).

Tasks declare `depends_on: [task_id...]`. Blocked agents auto-escalate
upward after 3 consecutive blocked turns; unresolved escalations surface
in the final "questions for you" panel.

## Cost

Use `--max-budget <usd>` to hard-cap a run. Use `--backend mock` for
free architecture demos or CI smoke tests. Live token spend appears
in the TUI header; the final summary panel and `session.json` both
report totals.

Each agent turn = one inference call. A typical run with real Claude:

| Role        | Mode    | Approx cost per turn               |
|-------------|---------|------------------------------------|
| Product     | plan    | ~$0.10–0.30 (one call)             |
| EM          | plan    | ~$0.10–0.30 (one call)             |
| Tech Lead   | plan    | ~$0.20–0.50 (publishes contracts)  |
| Specialists | agentic | $0.30–2.00 each (depends on scope) |

So $1–10 for a moderate feature. Larger features scale linearly with the
specialist headcount and turn budget.

## All flags

<details>
<summary>Full reference</summary>

| Flag | Description |
|---|---|
| `REQUEST` | Positional: the initiative to run. Omit for interactive mode. |
| `--backend {auto,claude,codex,mock}` | Inference backend. Default `auto`. |
| `--max-turns N` | Cap orchestrator turns (default 80). |
| `--max-agents N` | Cap agents spawned (default 12). |
| `--concurrency N` | Parallel agent turns via thread pool (default 3). |
| `--workspace PATH` | Greenfield workspace root. Defaults to `./.mau/runs/<ts>/`. |
| `--in [PATH]` | Brownfield: run inside an existing repo. `--in` alone = `cwd`. |
| `--resume [PATH]` | Resume from a workspace dir or `session.json`. Bare `--resume` = most recent. |
| `--max-budget USD` | Hard-cap total spend; halts the run when reached. |
| `--no-tui` | Skip the live TUI; print events as plain text. |
| `--save PATH` | Also persist a copy of `session.json` to this path. |
| `--policy RULE` | Repeatable. Seed a durable policy. See `--policy 'role:<role>=<rule>'`. |
| `--isolation {auto,shared,worktree}` | Per-agent isolation backend. `auto` picks worktree in a git repo. |
| `--version` | Show version and exit. |
| `-h, --help` | Show help and exit. |

Subcommands: `mau run [...]` (default), `mau evolve {summarize,propose,regress}`.

</details>

## Evolution Agent (prototype)

A first cut at **Agentic Harness Engineering** — a separate agent that
ingests the per-agent `logs/<agent>.jsonl` transcripts and proposes
mutations to the harness itself (prompt edits, durable policies, default
changes). The prototype is read-only: it prints `HarnessProposal`s with
citations to specific transcript lines, but never edits the prompts dir.
Mutations get applied by a human after review.

```bash
# Per-agent stats (turns, accept/reject, tokens, avg duration, top reasons)
mau evolve summarize --logs-dir .mau/runs/<ts>/logs

# Print proposals based on deterministic transcript signals
mau evolve propose --logs-dir .mau/runs/<ts>/logs

# Same, but also ask the configured backend to draft concrete prompt diffs
mau evolve propose --use-backend --backend claude --logs-dir .mau/runs/<ts>/logs

# Regression suite — run the mock backend against bundled fixtures and
# report pass/fail. Used to gate any proposed harness mutation before it
# touches the source tree.
mau evolve regress
```

Deterministic signals the prototype fires on today:

| Signal | Kind | Target |
|---|---|---|
| Role's rejection rate > 40% over ≥ 5 turns | `prompt_edit` | `prompts/<role>.md` |
| Same file written by multiple same-role agents under `worktree` isolation | `policy_suggestion` | `policy:role:<role>` |
| Role's avg duration > 2x the cross-role median | `default_change` | `default:MAX_TURNS_PER_AGENT` |

Every proposal carries an evidence list of `logs/<agent>.jsonl:turn=<N>`
citations so you can pop open the transcript and see the exact lines
the signal fired on.

**Regression gate.** Before any of these get merged, the bundled
`RegressionSuite` runs each fixture in `src/mau_cli/evolution_fixtures/`
against the deterministic `MockBackend` and verifies the run still
reaches `stopped_on_completion` without hitting `stopped_on_turn_cap`.
The gate is the philosophy: a harness mutation that breaks the
fixture's convergence is rejected before a human ever reviews it. The
prototype's `apply_proposal` copies the prompts dir to a temp location
and stages the proposed diff there — your source tree is never mutated.

## Roadmap

- **Specialist messaging.** Allow specialists to send messages mid-task
  (currently they only deliver or block).
- **Pluggable backends.** OpenAI, Anthropic API direct, Ollama for
  local models.
- **Hot-swappable prompts** in the Evolution Agent's regression run, so
  proposed prompt diffs actually drive the mock-backend fixtures rather
  than the prototype's "patch staged" record.
- **Single-binary distribution.** Rewrite in Go with bubbletea TUI.

## Contributing

Issues and PRs welcome. The codebase is small enough to read end-to-end —
start with [`src/mau_cli/orchestrator.py`](./src/mau_cli/orchestrator.py)
for the turn loop and dependency graph, then [`src/mau_cli/cli.py`](./src/mau_cli/cli.py)
for the entry point.

## License

Apache License 2.0. See [LICENSE](./LICENSE) for the full text.
