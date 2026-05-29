"""CLI entry point — `mau` command.

Three surfaces:
  mau "build a user dashboard"          # one-shot, runs to completion
  mau                                   # interactive: prompts for the request,
                                        # then renders the live TUI
  mau evolve {summarize,propose,regress}  # AHE prototype — see evolution.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from mau_cli import __version__
from mau_cli.evolution import (
    EvolutionAgent,
    RegressionSuite,
    format_proposals,
    format_regression,
    format_summary_table,
)
from mau_cli.inference import select_backend
from mau_cli.orchestrator import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_AGENTS,
    DEFAULT_MAX_TURNS,
    Orchestrator,
)
from mau_cli.schemas import Workspace
from mau_cli.tui import TUI


def _default_workspace_root() -> str:
    """Per-session directory under ./.mau/runs/<timestamp>/."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return str(Path.cwd() / ".mau" / "runs" / stamp)


SPLASH = r"""
   __  ___ ___ _   _    ___ _    ___
  |  \/  | /   \ | | |  / __| |  |_ _|
  | |\/| |/ /^\ \ |_| | | (__| |__ | |
  |_|  |_|\_/ \_/____/   \___|____|___|
   Multi-Agent Unit  ·  v{version}
"""


# Subcommands users may invoke directly. Anything else passed at the group
# level (positional or unknown option) is treated as args for `run`, so the
# original `mau "build a thing"` / `mau --backend mock "..."` invocations
# keep working after Task 7 turned `mau` into a subcommand group.
_KNOWN_SUBCOMMANDS = {"run", "evolve"}
_GROUP_ONLY_FLAGS = {"--help", "-h", "--version"}


class _MauGroup(click.Group):
    """Group that forwards bare requests / unknown flags to `run`.

    Click would otherwise reject `mau --backend mock "..."` because
    `--backend` isn't a group option. We rewrite the argv so anything that
    isn't a known subcommand or a recognised group flag gets prepended with
    `run`, restoring the pre-Task-7 ergonomics.
    """

    def parse_args(self, ctx, args):  # type: ignore[override]
        if args:
            first = args[0]
            if first not in _KNOWN_SUBCOMMANDS and first not in _GROUP_ONLY_FLAGS:
                args = ["run", *args]
        return super().parse_args(ctx, args)


@click.group(
    name="mau",
    cls=_MauGroup,
    invoke_without_command=True,
    help="MAU-CLI — orchestrate a simulated engineering team via local Claude / Codex.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="mau")
@click.pass_context
def main(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        # Bare `mau` with no positional / subcommand → interactive prompt.
        ctx.invoke(run)


@main.command(
    name="run",
    help="Run a single orchestration session (the default behaviour).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.argument("request", required=False, nargs=-1)
@click.option(
    "--backend",
    type=click.Choice(["auto", "claude", "codex", "mock"], case_sensitive=False),
    default="auto",
    help="Inference backend (default: auto-detect claude → codex → mock).",
)
@click.option("--max-turns", type=int, default=DEFAULT_MAX_TURNS, show_default=True)
@click.option("--max-agents", type=int, default=DEFAULT_MAX_AGENTS, show_default=True)
@click.option("--concurrency", type=int, default=DEFAULT_CONCURRENCY, show_default=True)
@click.option(
    "--workspace",
    "workspace_path",
    type=click.Path(file_okay=False, writable=True),
    default=None,
    help="Workspace root. Defaults to ./.mau/runs/<timestamp>/. Code goes in <root>/workspace/.",
)
@click.option(
    "--in",
    "in_path",
    type=click.Path(file_okay=False, exists=True, resolve_path=True),
    is_flag=False,
    flag_value=".",
    default=None,
    help="Brownfield mode: agents work directly in this existing project "
    "directory and write files to its root. Metadata goes to "
    "<path>/.mau/runs/<ts>/. Pass `--in` with no value to use the current "
    "directory. Mutually exclusive with --workspace.",
)
@click.option(
    "--resume",
    "resume_path",
    default=None,
    help="Resume an interrupted run. Pass a workspace dir or a session.json path. "
    "If omitted as a value, picks the most recent ./.mau/runs/<dir>/.",
    is_flag=False,
    flag_value="__auto__",  # `--resume` with no value → auto-select most recent
)
@click.option(
    "--max-budget",
    "max_budget_usd",
    type=float,
    default=None,
    help=(
        "Hard cap on total spend in USD. The orchestrator refuses to "
        "dispatch any new agent turn once spend reaches this value, but "
        "in-flight turns from the prior tick still complete, so the final "
        "total may overshoot slightly."
    ),
)
@click.option(
    "--no-tui",
    is_flag=True,
    help="Skip the live TUI; print events as plain text.",
)
@click.option(
    "--save",
    type=click.Path(dir_okay=False, writable=True),
    help="Also persist a final session JSON to this extra path (in addition to <workspace>/session.json).",
)
@click.option(
    "--policy",
    "policies",
    multiple=True,
    help="Durable policy the team must follow for this run (and resumes). "
    "Repeatable. Format: '<rule>' for global, or 'role:<role>=<rule>' / "
    "'task:<id>=<rule>' to scope. e.g. --policy 'no force-pushes to main' "
    "--policy 'role:devops=always run db migration plan before deploy'.",
)
@click.option(
    "--isolation",
    type=click.Choice(["auto", "shared", "worktree"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Per-agent isolation backend. 'auto' uses 'shared' for greenfield "
    "runs and per-agent git worktrees for brownfield runs in a clean git repo. "
    "'shared' forces single-cwd mode (correct cumulative/integration "
    "semantics). 'worktree' forces per-agent worktrees and fails if the "
    "workspace isn't a git repo (note: worktrees reset to HEAD each turn and "
    "exclude git-ignored files, so integration verifiers see a partial tree).",
)
def run(
    request: tuple[str, ...],
    backend: str,
    max_turns: int,
    max_agents: int,
    concurrency: int,
    workspace_path: Optional[str],
    in_path: Optional[str],
    resume_path: Optional[str],
    max_budget_usd: Optional[float],
    no_tui: bool,
    save: Optional[str],
    policies: tuple[str, ...],
    isolation: str,
) -> None:
    console = Console()
    console.print(Text(SPLASH.format(version=__version__), style="bold cyan"))

    if in_path and workspace_path:
        console.print(
            "[red]--in and --workspace are mutually exclusive. "
            "Pick one: --in points at an existing project; --workspace "
            "creates a fresh greenfield root.[/red]"
        )
        sys.exit(2)

    resume_snapshot: Optional[dict] = None
    resume_workspace_root: Optional[str] = None
    if resume_path is not None:
        resolved = _resolve_resume_path(resume_path, console)
        if resolved is None:
            sys.exit(1)
        resume_workspace_root, resume_snapshot = resolved

    user_request = " ".join(request).strip()
    if resume_snapshot is not None:
        # Use the persisted request; the TUI shows it in the header.
        user_request = resume_snapshot.get("request") or user_request
    elif not user_request:
        user_request = _interactive_prompt(console)
        if not user_request:
            console.print("[yellow]No request given. Goodbye.[/yellow]")
            return

    backend_obj = select_backend(backend.lower())
    if resume_workspace_root:
        workspace = Workspace(
            root=resume_workspace_root,
            code_dir_override=(resume_snapshot or {}).get("workspace_code_dir_override"),
            brownfield=bool((resume_snapshot or {}).get("workspace_brownfield", False)),
        )
    elif in_path is not None:
        target = Path(in_path).resolve()
        root = target / ".mau" / "runs" / time.strftime("%Y%m%d-%H%M%S")
        workspace = Workspace(
            root=str(root),
            code_dir_override=str(target),
            brownfield=True,
        )
    else:
        workspace = Workspace(root=str(Path(workspace_path or _default_workspace_root()).resolve()))
    workspace.ensure()

    budget_str = f"${max_budget_usd:.2f}" if max_budget_usd is not None else "unlimited"
    mode_str = "brownfield (existing codebase)" if workspace.brownfield else "greenfield"
    code_dir_line = (
        f"[bold]Code dir:[/bold]  {workspace.code_dir}\n" if workspace.brownfield else ""
    )
    console.print(
        Panel(
            Text.from_markup(
                f"[bold]Backend:[/bold]   {backend_obj.name}\n"
                f"[bold]Mode:[/bold]      {mode_str}\n"
                f"[bold]Workspace:[/bold] {workspace.root}\n"
                f"{code_dir_line}"
                f"[bold]Limits:[/bold]    turns={max_turns}  agents={max_agents}  "
                f"concurrency={concurrency}  budget={budget_str}"
            ),
            border_style="cyan",
            title="config",
        )
    )

    orch = Orchestrator(
        backend=backend_obj,
        max_turns=max_turns,
        max_agents=max_agents,
        concurrency=concurrency,
        workspace=workspace,
        max_budget_usd=max_budget_usd,
        isolation=isolation.lower(),  # type: ignore[arg-type]
    )

    if resume_snapshot is not None:
        rehydrated = orch.load_from_disk(resume_snapshot)
        console.print(
            Panel(
                Text.from_markup(
                    f"[bold]Resumed:[/bold] {workspace.root}\n"
                    f"[bold]Agents:[/bold]   {len(orch.agents)} restored\n"
                    f"[bold]Tasks:[/bold]    {len(orch.world.tasks)} restored\n"
                    f"[bold]Messages:[/bold] {len(orch.world.messages)} replayed\n"
                    f"[bold]Policies:[/bold] {sum(1 for p in orch.world.policies if p.active)} active\n"
                    f"[bold]Spent:[/bold]    {orch.world.usage.short()}"
                ),
                border_style="green",
                title="resume",
            )
        )
        if not rehydrated:
            console.print("[red]Session state had no agents — nothing to resume.[/red]")
            sys.exit(1)

    # Atomically seed --policy entries before the team starts (or resumes).
    # add_policy dedupes on (text, scope), so re-passing the same flag on
    # resume is a no-op rather than duplicating the rule.
    for raw in policies:
        text, scope = _parse_policy_flag(raw)
        if not text:
            console.print(f"[yellow]Skipping empty --policy entry: {raw!r}[/yellow]")
            continue
        orch.world.add_policy(text=text, scope=scope, source="user", turn=0)

    try:
        if no_tui:
            world = _run_plain(orch, user_request, console, resumed=resume_snapshot is not None)
        else:
            tui = TUI(orch, console=console)
            world = tui.resume() if resume_snapshot is not None else tui.run(user_request)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)

    console.print()
    files_lines: list[str] = []
    for agent in world.agents.values():
        if agent.files_touched:
            files_lines.append(f"  {agent.name} ({agent.role.value}):")
            for f in agent.files_touched:
                files_lines.append(f"    - {f}")
    files_block = "\n".join(files_lines) if files_lines else "  (no files written)"

    console.print(
        Panel(
            Text(
                f"{world.final_summary or '(no summary)'}\n\n"
                f"Total spend: {world.usage.short()}\n"
                f"Workspace:   {workspace.code_dir}\n\n"
                f"Files produced:\n{files_block}"
            ),
            title="[bold green]final summary[/bold green]",
            border_style="green",
        )
    )

    if world.pending_user_questions:
        console.print(
            Panel(
                "\n".join(
                    f"[{m.from_agent}] {m.subject}\n  {m.body}"
                    for m in world.pending_user_questions
                )
                + "\n\nTip: if your answer is a rule the team should follow "
                + "going forward, resume with `mau --resume --policy '<rule>'` "
                + "(or `--policy 'role:devops=<rule>'`) so the rule persists "
                + "into every future agent prompt.",
                title="[bold yellow]escalations / questions for you[/bold yellow]",
                border_style="yellow",
            )
        )

    active_policies = [p for p in world.policies if p.active]
    if active_policies:
        # Use Text (not a markup string) so policy IDs in square brackets
        # aren't interpreted as Rich markup tags.
        body = Text(
            "\n".join(
                f"  - [{p.id}] (scope={p.scope}, source={p.source}) {p.text}"
                for p in active_policies
            )
        )
        console.print(
            Panel(
                body,
                title="[bold]active policies[/bold]",
                border_style="cyan",
            )
        )

    if save:
        path = Path(save)
        path.write_text(json.dumps(world.snapshot(), indent=2, default=str))
        console.print(f"[dim]session saved to {path}[/dim]")


def _parse_policy_flag(raw: str) -> tuple[str, str]:
    """Parse one --policy value into (text, scope).

    Accepts:
      'rule text'                       → ('rule text', 'global')
      'role:devops=rule text'           → ('rule text', 'role:devops')
      'task:task_abc123=rule text'      → ('rule text', 'task:task_abc123')
      'global=rule text'                → ('rule text', 'global')
    Returns ('', 'global') for whitespace-only input."""
    s = raw.strip()
    if not s:
        return "", "global"
    head, sep, rest = s.partition("=")
    if sep and head.strip() and (
        head.strip() == "global"
        or head.strip().startswith("role:")
        or head.strip().startswith("task:")
    ):
        return rest.strip(), head.strip()
    return s, "global"


def _interactive_prompt(console: Console) -> str:
    console.print(
        Panel(
            Text(
                "You're acting as Product. Describe the initiative you want the team to take on.\n"
                "The Product agent will draft a PRD, hand to the Engineering Manager, "
                "who will spawn a Tech Lead and specialists, with dependencies tracked.",
                style="white",
            ),
            title="[bold]Briefing[/bold]",
            border_style="cyan",
        )
    )
    return Prompt.ask("[bold cyan]> initiative[/bold cyan]").strip()


def _run_plain(orch: Orchestrator, request: str, console: Console, resumed: bool = False):
    """No-TUI fallback: log every event line by line."""

    def on_event(kind: str, payload: dict) -> None:
        console.print(f"[dim]{kind}[/dim] {payload}")

    orch.on_event = on_event
    return orch.resume() if resumed else orch.run(request)


def _resolve_resume_path(arg: str, console: Console) -> Optional[tuple[str, dict]]:
    """Resolve `--resume` arg to (workspace_root, snapshot_dict). Returns
    None and prints to console on failure."""
    if arg == "__auto__":
        # No value supplied: pick the most recent ./.mau/runs/*/ in CWD.
        runs_dir = Path.cwd() / ".mau" / "runs"
        if not runs_dir.exists():
            console.print(
                f"[red]No ./.mau/runs/ directory in {Path.cwd()}; nothing to auto-resume.[/red]"
            )
            return None
        candidates = sorted(
            (p for p in runs_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            console.print(f"[red]No runs found under {runs_dir}.[/red]")
            return None
        target = candidates[0]
        console.print(f"[dim]Auto-resuming most recent run: {target}[/dim]")
    else:
        target = Path(arg).resolve()

    if target.is_file():
        session_path = target
        ws_root = target.parent
    else:
        ws_root = target
        session_path = target / "session.json"

    if not session_path.exists():
        console.print(f"[red]No session.json at {session_path}.[/red]")
        return None
    if session_path.stat().st_size == 0:
        console.print(
            f"[red]session.json at {session_path} is empty (likely killed mid-write before "
            f"the atomic-write fix). Cannot resume — please start fresh.[/red]"
        )
        return None
    try:
        snapshot = json.loads(session_path.read_text())
    except json.JSONDecodeError as e:
        console.print(f"[red]session.json is corrupt: {e}[/red]")
        return None

    return str(ws_root), snapshot


# ---- evolve subcommand group ------------------------------------------------
#
# Surfaces the Evolution Agent prototype. Each subcommand is read-only against
# the prompts dir; even `regress` patches a temp copy. Mutations stay gated
# behind a human review of the printed proposals.


def _default_prompts_dir() -> Path:
    """Resolve the packaged prompts dir. Falls back to the source layout when
    running from a checkout without an install."""
    try:
        return Path(str(__import__("mau_cli.prompts", fromlist=["__file__"]).__file__)).parent
    except Exception:
        return Path(__file__).parent / "prompts"


def _resolve_logs_dir(explicit: Optional[str]) -> Optional[Path]:
    """Pick a logs dir to ingest. Honours `--logs-dir` first; otherwise looks
    for the most recent `./.mau/runs/<ts>/logs/`."""
    if explicit:
        return Path(explicit).resolve()
    runs_dir = Path.cwd() / ".mau" / "runs"
    if not runs_dir.exists():
        return None
    candidates = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for c in candidates:
        logs = c / "logs"
        if logs.exists():
            return logs
    return None


@main.group(
    name="evolve",
    help="Agentic Harness Engineering prototype — ingest transcripts and "
    "propose harness mutations gated by a regression suite.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def evolve() -> None:
    pass


@evolve.command(
    name="summarize",
    help="Print per-agent transcript stats: turns, accept/reject rates, "
    "tokens, avg duration, and top rejection reasons.",
)
@click.option(
    "--logs-dir",
    "logs_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory of `<agent>.jsonl` transcripts. Defaults to the most "
    "recent ./.mau/runs/<ts>/logs/.",
)
def evolve_summarize(logs_dir: Optional[str]) -> None:
    console = Console()
    resolved = _resolve_logs_dir(logs_dir)
    if resolved is None:
        console.print("[yellow]No logs directory found.[/yellow]")
        return
    agent = EvolutionAgent(
        logs_dir=resolved, prompts_dir=_default_prompts_dir()
    )
    summaries = agent.summarize()
    console.print(Panel(Text(format_summary_table(summaries)),
                        title=f"transcripts @ {resolved}", border_style="cyan"))


@evolve.command(
    name="propose",
    help="Emit HarnessProposals based on transcript signals. Read-only — "
    "proposals print to stdout; the prompts dir is never mutated.",
)
@click.option(
    "--logs-dir",
    "logs_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory of `<agent>.jsonl` transcripts.",
)
@click.option(
    "--use-backend",
    is_flag=True,
    help="Also ask the configured backend to draft concrete prompt diffs for "
    "prompt_edit proposals. Skipped when the resolved backend is the mock.",
)
@click.option(
    "--backend",
    type=click.Choice(["auto", "claude", "codex", "mock"], case_sensitive=False),
    default="auto",
    help="Inference backend used when --use-backend is set.",
)
def evolve_propose(
    logs_dir: Optional[str], use_backend: bool, backend: str
) -> None:
    console = Console()
    resolved = _resolve_logs_dir(logs_dir)
    if resolved is None:
        console.print("[yellow]No logs directory found.[/yellow]")
        return
    backend_obj = select_backend(backend.lower()) if use_backend else None
    agent = EvolutionAgent(
        logs_dir=resolved,
        prompts_dir=_default_prompts_dir(),
        backend=backend_obj,
    )
    proposals = agent.propose()
    console.print(Panel(Text(format_proposals(proposals)),
                        title=f"proposals @ {resolved}", border_style="cyan"))


@evolve.command(
    name="regress",
    help="Run the regression suite against the bundled fixtures using the "
    "mock backend. Reports per-fixture pass/fail.",
)
@click.option(
    "--fixtures",
    "fixtures_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory of fixture JSON files. Defaults to the packaged "
    "src/mau_cli/evolution_fixtures/.",
)
def evolve_regress(fixtures_dir: Optional[str]) -> None:
    console = Console()
    suite = RegressionSuite(
        fixtures_dir=Path(fixtures_dir).resolve() if fixtures_dir else None
    )
    verdicts = suite.run()
    console.print(Panel(Text(format_regression(verdicts)),
                        title="regression results",
                        border_style="green" if all(v.passed for v in verdicts) else "yellow"))


if __name__ == "__main__":
    main()
