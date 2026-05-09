"""Live terminal UI built on Rich.

Runs the orchestrator in a worker thread and re-renders the layout on a
high-frequency refresh cadence so users get tangible activity signals:

  - A Braille spinner cycles next to every "thinking" agent so the screen
    is visibly alive even while a single Claude call is in flight.
  - Each thinking agent shows the elapsed time of its current turn so you
    can tell whether things are progressing or genuinely hung.
  - Each agent has its own token / cost line so you see who's spending what.
  - The footer shows seconds since the last orchestrator event — if that
    number stops growing, *something* is moving.
"""

from __future__ import annotations

import threading
import time as _time
from collections import deque
from typing import Any, Optional

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import AgentState, Role, WorldState


ROLE_COLORS: dict[str, str] = {
    "product": "magenta",
    "engineering_manager": "cyan",
    "tech_lead": "blue",
    "frontend": "green",
    "backend": "yellow",
    "database": "red",
    "qa": "bright_magenta",
    "devops": "bright_blue",
}

STATUS_GLYPHS: dict[str, str] = {
    "idle": "·",
    "working": "▶",
    "blocked": "■",
    "complete": "✓",
}

# Braille spinner — same frame set as Rich's "dots" spinner.
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:4.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    return f"{n/1_000_000:.2f}M"


class TUI:
    """Drives the orchestrator and renders live state to the terminal."""

    EVENT_RING_SIZE = 60
    REFRESH_PER_SECOND = 12  # spinner needs >= 8 Hz to feel smooth

    def __init__(self, orchestrator: Orchestrator, console: Optional[Console] = None):
        self.orchestrator = orchestrator
        self.console = console or Console()
        self.events: deque[tuple[float, str, dict[str, Any]]] = deque(maxlen=self.EVENT_RING_SIZE)
        self._lock = threading.Lock()
        self._frame = 0
        self._last_event_at: float = _time.monotonic()
        self._started_at: float = _time.monotonic()
        orchestrator.on_event = self._record_event

    def _record_event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.events.append((_time.monotonic(), kind, payload))
            self._last_event_at = _time.monotonic()

    def _spinner(self) -> str:
        return SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]

    # ---- run --------------------------------------------------------------

    def run(self, request: str) -> WorldState:
        return self._drive(lambda: self.orchestrator.run(request))

    def resume(self) -> WorldState:
        return self._drive(lambda: self.orchestrator.resume())

    def _drive(self, fn) -> WorldState:
        result_holder: dict[str, WorldState] = {}
        error_holder: dict[str, BaseException] = {}

        def worker() -> None:
            try:
                result_holder["world"] = fn()
            except BaseException as e:  # noqa: BLE001
                error_holder["err"] = e

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        layout = self._make_layout()
        with Live(
            layout,
            console=self.console,
            refresh_per_second=self.REFRESH_PER_SECOND,
            screen=False,
        ):
            while thread.is_alive():
                self._frame += 1
                self._render_into(layout)
                thread.join(timeout=1.0 / self.REFRESH_PER_SECOND)
            self._frame += 1
            self._render_into(layout)

        if "err" in error_holder:
            raise error_holder["err"]
        return result_holder["world"]

    # ---- layout -----------------------------------------------------------

    def _make_layout(self) -> Layout:
        root = Layout()
        root.split_column(
            Layout(name="header", size=5),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=10),
        )
        # Wider left panel — team table now carries per-agent metrics, so
        # it needs more room than the previous 1:2 split allowed.
        root["body"].split_row(
            Layout(name="team", ratio=5),
            Layout(name="right", ratio=6),
        )
        root["right"].split_column(
            Layout(name="tasks", size=12),
            Layout(name="messages", ratio=1),
        )
        return root

    def _render_into(self, layout: Layout) -> None:
        world = self.orchestrator.world
        layout["header"].update(self._header(world))
        layout["team"].update(self._team_panel(world))
        layout["tasks"].update(self._tasks_panel(world))
        layout["messages"].update(self._messages_panel(world))
        layout["footer"].update(self._footer(world))

    # ---- panels -----------------------------------------------------------

    def _header(self, world: WorldState) -> Panel:
        title = Text("MAU-CLI", style="bold white on blue")
        title.append("  ", style="dim")
        title.append(f"backend={self.orchestrator.backend.name}", style="cyan")
        title.append("  ·  ", style="dim")
        in_flight = sum(
            1 for a in world.agents.values() if a.status == "thinking"
        )
        if in_flight:
            title.append(f"{self._spinner()} {in_flight} in flight", style="bold yellow")
        else:
            title.append("idle", style="dim")
        title.append("  ·  ", style="dim")
        title.append(
            f"agents={len(world.agents)}  tasks={len(world.tasks)}  "
            f"msgs={len(world.messages)}",
            style="dim",
        )
        title.append("  ·  ", style="dim")
        title.append(world.usage.short(), style="green")

        request_text = world.request or "(awaiting request)"
        if len(request_text) > 140:
            request_text = request_text[:137] + "…"
        request = Text(request_text, style="italic dim", overflow="ellipsis", no_wrap=True)

        if world.workspace:
            ws_line = Text(f"workspace: {world.workspace.code_dir}", style="dim", overflow="ellipsis", no_wrap=True)
            return Panel(
                Group(Align.left(title), Align.left(request), Align.left(ws_line)),
                border_style="blue",
            )
        return Panel(Group(Align.left(title), Align.left(request)), border_style="blue")

    def _agent_status_cell(self, agent: AgentState) -> Text:
        """Status glyph + name, colored by role. Animated spinner if thinking."""
        color = ROLE_COLORS.get(agent.role.value, "white")
        if agent.status == "thinking":
            glyph = self._spinner()
            glyph_style = "bold yellow"
        else:
            glyph = STATUS_GLYPHS.get(agent.status, "?")
            glyph_style = color
        cell = Text()
        cell.append(f"{glyph} ", style=glyph_style)
        cell.append(agent.name, style=f"bold {color}")
        return cell

    def _team_panel(self, world: WorldState) -> Panel:
        table = Table(
            show_header=True,
            header_style="bold dim",
            box=None,
            expand=True,
            pad_edge=False,
        )
        table.add_column("agent", no_wrap=True)
        table.add_column("role", style="dim", no_wrap=True)
        table.add_column("state", no_wrap=True)
        table.add_column("turns", justify="right", style="dim", no_wrap=True)
        table.add_column("tokens (in/out)", justify="right", style="dim", no_wrap=True)
        table.add_column("$", justify="right", style="green", no_wrap=True)

        for agent in world.agents.values():
            color = ROLE_COLORS.get(agent.role.value, "white")
            state_text = Text()
            if agent.status == "thinking" and agent.thinking_started_at:
                elapsed = _time.monotonic() - agent.thinking_started_at
                state_text.append("thinking ", style="bold yellow")
                state_text.append(_fmt_elapsed(elapsed), style="yellow")
            elif agent.status == "blocked":
                state_text.append("blocked", style="bold red")
            elif agent.status == "complete":
                state_text.append("complete", style="bold green")
            elif agent.status == "working":
                state_text.append("working", style="cyan")
            else:
                state_text.append(agent.status, style="dim")

            if agent.specialization:
                spec = agent.specialization
                if len(spec) > 28:
                    spec = spec[:25] + "…"
                state_text.append(f"  {spec}", style="dim italic")

            tokens = (
                f"{_fmt_tokens(agent.usage.input_tokens)}/"
                f"{_fmt_tokens(agent.usage.output_tokens)}"
            )

            table.add_row(
                self._agent_status_cell(agent),
                Text(agent.role.value, style=color),
                state_text,
                str(agent.turns_taken),
                tokens,
                f"${agent.usage.cost_usd:.3f}",
            )

        if not world.agents:
            table.add_row(Text("(no agents yet)", style="dim"), "", "", "", "", "")

        # Footer summary inside the team panel.
        in_flight = sum(1 for a in world.agents.values() if a.status == "thinking")
        complete = sum(1 for a in world.agents.values() if a.status == "complete")
        summary = Text()
        summary.append(f"  {self._spinner()} ", style="bold yellow" if in_flight else "dim")
        summary.append(f"{in_flight} thinking", style="yellow" if in_flight else "dim")
        summary.append("   ", style="dim")
        summary.append(f"✓ {complete} complete", style="green")
        summary.append("   ", style="dim")
        summary.append(f"total {world.usage.short()}", style="dim")

        return Panel(
            Group(table, Text(""), summary),
            title="[bold]team[/bold]",
            border_style="white",
            padding=(1, 1),
        )

    def _tasks_panel(self, world: WorldState) -> Panel:
        table = Table(show_header=True, header_style="bold dim", box=None, expand=True)
        table.add_column("id", style="dim", no_wrap=True)
        table.add_column("title", overflow="fold", ratio=2)
        table.add_column("assignee", style="cyan", no_wrap=True)
        table.add_column("status", no_wrap=True)
        table.add_column("deps", style="dim", no_wrap=True)

        for task in list(world.tasks.values())[-10:]:
            status_color = {
                "pending": "yellow",
                "in_progress": "cyan",
                "blocked": "red",
                "complete": "green",
                "cancelled": "dim",
            }.get(task.status, "white")
            deps_label = ",".join(task.depends_on) if task.depends_on else "—"
            table.add_row(
                task.id,
                task.title,
                task.assignee or "—",
                Text(task.status, style=status_color),
                deps_label,
            )

        if not world.tasks:
            table.add_row("", Text("(no tasks yet)", style="dim"), "", "", "")

        return Panel(table, title="[bold]tasks[/bold]", border_style="white")

    def _messages_panel(self, world: WorldState) -> Panel:
        lines: list[Text] = []
        for msg in world.messages[-12:]:
            from_color = ROLE_COLORS.get(
                world.agents[msg.from_agent].role.value, "white"
            ) if msg.from_agent in world.agents else "white"
            to_color = ROLE_COLORS.get(
                world.agents[msg.to_agent].role.value, "white"
            ) if msg.to_agent in world.agents else "white"
            line = Text(overflow="ellipsis", no_wrap=True)
            line.append(f"[{msg.msg_type:11s}] ", style="dim")
            line.append(msg.from_agent, style=from_color)
            line.append(" → ", style="dim")
            line.append(msg.to_agent, style=to_color)
            line.append(f"  {msg.subject}", style="white")
            lines.append(line)
        if not lines:
            lines.append(Text("(no messages yet)", style="dim"))
        return Panel(Group(*lines), title="[bold]messages[/bold]", border_style="white")

    def _footer(self, world: WorldState) -> Panel:
        with self._lock:
            recent = list(self.events)[-7:]
        lines: list[Text] = []
        for ts, kind, payload in recent:
            age = _time.monotonic() - ts
            t = Text(overflow="ellipsis", no_wrap=True)
            t.append(f"-{_fmt_elapsed(age):>6s}  ", style="dim")
            t.append(f"{kind:22s}", style="bold cyan")
            t.append(_short_payload(payload), style="dim")
            lines.append(t)
        if world.finished and world.final_summary:
            lines.append(Text(""))
            for line in world.final_summary.splitlines()[:3]:
                lines.append(Text(line, style="bold green", overflow="ellipsis", no_wrap=True))
        if not lines:
            lines.append(Text("(no events)", style="dim"))

        # Heartbeat: shows the user the screen is live even when nothing
        # has changed. Updates every frame.
        last_event_age = _time.monotonic() - self._last_event_at
        uptime = _time.monotonic() - self._started_at
        in_flight_names = [a.name for a in world.agents.values() if a.status == "thinking"]
        heartbeat = Text(overflow="ellipsis", no_wrap=True)
        heartbeat.append(f"{self._spinner()} ", style="bold yellow" if in_flight_names else "dim")
        if in_flight_names:
            heartbeat.append(
                f"in flight: {', '.join(in_flight_names)}",
                style="yellow",
            )
        else:
            heartbeat.append("nothing thinking right now", style="dim")
        heartbeat.append(
            f"   · last event {_fmt_elapsed(last_event_age)} ago"
            f" · uptime {_fmt_elapsed(uptime)}",
            style="dim",
        )
        lines.append(heartbeat)

        return Panel(Group(*lines), title="[bold]activity[/bold]", border_style="white")


def _short_payload(payload: dict[str, Any]) -> str:
    parts = []
    for k, v in payload.items():
        s = str(v)
        if len(s) > 50:
            s = s[:47] + "..."
        parts.append(f"{k}={s}")
    return "  ".join(parts)
