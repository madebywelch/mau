"""Core dataclasses: Messages, Tasks, AgentState, Workspace, and the Action
protocol agents emit on each turn.

Kept dependency-free (stdlib dataclasses) so the orchestration layer can be
tested without the full Rich/Click stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from time import time
from typing import Any, Literal, Optional
from uuid import uuid4


class Role(str, Enum):
    PRODUCT = "product"
    ENGINEERING_MANAGER = "engineering_manager"
    TECH_LEAD = "tech_lead"
    FRONTEND = "frontend"
    BACKEND = "backend"
    DATABASE = "database"
    QA = "qa"
    DEVOPS = "devops"
    USER = "user"  # Pseudo-role for the human-in-the-loop


SUPERVISOR_OF: dict[Role, Optional[Role]] = {
    Role.USER: None,
    Role.PRODUCT: Role.USER,
    Role.ENGINEERING_MANAGER: Role.PRODUCT,
    Role.TECH_LEAD: Role.ENGINEERING_MANAGER,
    Role.FRONTEND: Role.TECH_LEAD,
    Role.BACKEND: Role.TECH_LEAD,
    Role.DATABASE: Role.TECH_LEAD,
    Role.QA: Role.TECH_LEAD,
    Role.DEVOPS: Role.TECH_LEAD,
}


# Roles that may spawn additional agents. Specialists cannot.
ROLES_THAT_SPAWN: set[Role] = {
    Role.PRODUCT,
    Role.ENGINEERING_MANAGER,
    Role.TECH_LEAD,
}


AgentStatus = Literal["idle", "thinking", "working", "blocked", "complete"]
MessageType = Literal[
    "task",
    "question",
    "answer",
    "deliverable",
    "blocker",
    "status",
    "escalation",
    "directive",
]
TaskStatus = Literal["pending", "in_progress", "blocked", "complete", "cancelled"]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def now() -> float:
    return time()


@dataclass
class Message:
    id: str = field(default_factory=lambda: _id("msg"))
    from_agent: str = ""
    to_agent: str = ""  # agent name; "broadcast" delivers to whole team
    msg_type: MessageType = "status"
    subject: str = ""
    body: str = ""
    references: list[str] = field(default_factory=list)  # task IDs, msg IDs
    timestamp: float = field(default_factory=now)

    def short(self) -> str:
        return f"[{self.msg_type}] {self.from_agent} → {self.to_agent}: {self.subject}"


@dataclass
class Task:
    id: str = field(default_factory=lambda: _id("task"))
    title: str = ""
    description: str = ""
    assignee: str = ""  # agent name
    creator: str = ""
    status: TaskStatus = "pending"
    depends_on: list[str] = field(default_factory=list)  # task IDs
    acceptance_criteria: list[str] = field(default_factory=list)
    deliverable_summary: Optional[str] = None
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    def is_unblocked(self, all_tasks: dict[str, "Task"]) -> bool:
        return all(
            all_tasks.get(dep) and all_tasks[dep].status == "complete"
            for dep in self.depends_on
        )


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        self.calls += other.calls

    def short(self) -> str:
        return (
            f"{self.calls} calls · "
            f"{self.input_tokens:,} in / {self.output_tokens:,} out · "
            f"${self.cost_usd:.4f}"
        )


@dataclass
class AgentState:
    name: str
    role: Role
    specialization: str = ""  # e.g. "auth screens", "checkout flow"
    status: AgentStatus = "idle"
    inbox: list[Message] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    assigned_tasks: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)  # paths relative to workspace
    turns_taken: int = 0
    blocked_turns: int = 0  # for escalation
    notes: list[str] = field(default_factory=list)  # internal scratchpad
    usage: TokenUsage = field(default_factory=TokenUsage)
    # Wall-clock when this agent's current turn started (None if idle/complete).
    # The TUI uses this to render an elapsed-time indicator next to the spinner.
    thinking_started_at: Optional[float] = None
    last_activity_at: float = field(default_factory=now)

    def supervisor(self) -> Optional[Role]:
        return SUPERVISOR_OF.get(self.role)


# Roles that produce real code (file edits) vs roles that produce only
# planning artifacts (PRDs, contracts, task graphs).
CODE_GEN_ROLES: set[Role] = {
    Role.FRONTEND,
    Role.BACKEND,
    Role.DATABASE,
    Role.QA,
    Role.DEVOPS,
}


# ---- Agent action protocol -------------------------------------------------
# Every agent turn returns:
#   { "thoughts": str, "status": AgentStatus, "actions": [Action, ...] }
# Actions are heterogeneous; orchestrator dispatches on `type`.

ActionType = Literal[
    "send_message",
    "create_task",
    "spawn_agent",
    "deliverable",
    "escalate",
    "complete",
    "note",
    "ask_user",
]


@dataclass
class AgentTurn:
    thoughts: str = ""
    status: AgentStatus = "working"
    actions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentTurn":
        return cls(
            thoughts=str(data.get("thoughts", ""))[:2000],
            status=data.get("status", "working"),  # type: ignore[arg-type]
            actions=list(data.get("actions", [])),
        )


@dataclass
class Workspace:
    """The on-disk root for a session.

    Layout:
      <root>/
        workspace/  ← agents write code here
        shared/     ← cross-agent artifacts (PRD, api-contract, schema, ...)
        logs/       ← per-agent JSONL transcripts
        session.json ← serialized WorldState
    """

    root: str  # absolute path

    @property
    def code_dir(self) -> str:
        return str(Path(self.root) / "workspace")

    @property
    def shared_dir(self) -> str:
        return str(Path(self.root) / "shared")

    @property
    def logs_dir(self) -> str:
        return str(Path(self.root) / "logs")

    @property
    def session_file(self) -> str:
        return str(Path(self.root) / "session.json")

    def ensure(self) -> None:
        Path(self.code_dir).mkdir(parents=True, exist_ok=True)
        Path(self.shared_dir).mkdir(parents=True, exist_ok=True)
        Path(self.logs_dir).mkdir(parents=True, exist_ok=True)


@dataclass
class WorldState:
    """Shared state mutated by the orchestrator. Single-writer, multi-reader."""

    request: str = ""  # original user prompt
    workspace: Optional[Workspace] = None
    agents: dict[str, AgentState] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)  # full audit log
    shared_docs: dict[str, str] = field(default_factory=dict)  # name → content
    pending_user_questions: list[Message] = field(default_factory=list)
    log: list[str] = field(default_factory=list)  # human-readable event log
    started_at: float = field(default_factory=now)
    finished: bool = False
    final_summary: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)

    def snapshot(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "workspace_root": self.workspace.root if self.workspace else None,
            "started_at": self.started_at,
            "finished": self.finished,
            "agents": {n: asdict(a) for n, a in self.agents.items()},
            "tasks": {tid: asdict(t) for tid, t in self.tasks.items()},
            "messages": [asdict(m) for m in self.messages],
            "shared_docs": dict(self.shared_docs),
            "log": list(self.log),
            "final_summary": self.final_summary,
            "usage": asdict(self.usage),
        }
