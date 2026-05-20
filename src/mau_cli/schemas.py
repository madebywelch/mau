"""Core dataclasses: Messages, Tasks, AgentState, Workspace, and the Action
protocol agents emit on each turn.

Kept dependency-free (stdlib dataclasses) so the orchestration layer can be
tested without the full Rich/Click stack.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
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
CriterionStatus = Literal["pending", "passed", "failed"]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def now() -> float:
    return time()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _doc_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


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
class AcceptanceCriterion:
    """A single acceptance criterion. `text` is the human-readable form; if
    `verifier` is set, the orchestrator can run it against the workspace and
    record the outcome in `last_*`."""

    text: str = ""
    verifier: Optional[str] = None  # name in verifiers.VERIFIERS
    spec: Optional[dict[str, Any]] = None
    last_status: CriterionStatus = "pending"
    last_summary: Optional[str] = None
    last_checked_turn: Optional[int] = None


def _coerce_criteria(
    raw: Optional[list[Any]],
) -> list[AcceptanceCriterion]:
    """Accept the agent-emitted shape, which is still a list of plain strings
    for back-compat, OR a list of dicts, OR a list of AcceptanceCriterion."""
    if not raw:
        return []
    out: list[AcceptanceCriterion] = []
    for item in raw:
        if isinstance(item, AcceptanceCriterion):
            out.append(item)
        elif isinstance(item, str):
            out.append(AcceptanceCriterion(text=item))
        elif isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            verifier = item.get("verifier")
            spec = item.get("spec")
            out.append(
                AcceptanceCriterion(
                    text=text,
                    verifier=str(verifier) if verifier else None,
                    spec=dict(spec) if isinstance(spec, dict) else None,
                    last_status=item.get("last_status", "pending"),
                    last_summary=item.get("last_summary"),
                    last_checked_turn=item.get("last_checked_turn"),
                )
            )
    return out


@dataclass
class DocVersion:
    """One revision of a shared doc. Persisted in WorldState.shared_docs so
    agents (and the transcript) can correlate who wrote what when, and
    deliverables can record which exact version they were satisfied against."""

    content: str = ""
    hash: str = ""  # sha256 short hex (16 chars)
    author: str = "system"
    timestamp: str = field(default_factory=now_iso)
    turn: int = 0


@dataclass
class Policy:
    """A durable human-approval rule that every future agent prompt re-sees.

    Promoted from one-shot `ask_user` answers (and CLI --policy flags) into
    first-class WorldState so the team doesn't forget "never deploy without
    a migration plan" the moment the question scrolls off the inbox. Persists
    across turns and across `--resume`. Contrast with the orchestrator's
    ephemeral `_turn_cap_announced` / `_completion_announced` /
    `_rejected_this_turn` flags, which are intentionally not snapshotted —
    those are per-tick guards, not durable governance state."""

    id: str = field(default_factory=lambda: _id("pol"))
    text: str = ""
    scope: str = "global"  # "global" | "role:<role>" | "task:<task_id>"
    source: str = "user"  # agent name or "user"
    created_at: str = field(default_factory=now_iso)
    created_turn: int = 0
    active: bool = True


@dataclass
class Task:
    id: str = field(default_factory=lambda: _id("task"))
    title: str = ""
    description: str = ""
    assignee: str = ""  # agent name
    creator: str = ""
    status: TaskStatus = "pending"
    depends_on: list[str] = field(default_factory=list)  # task IDs
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    deliverable_summary: Optional[str] = None
    # doc name → hash of the version the agent was looking at when the
    # deliverable landed. Lets later analysis see "did Task X close against
    # a stale tech contract?".
    satisfied_doc_versions: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    def __post_init__(self) -> None:
        # Boundary coercion: agent-emitted JSON still uses plain strings.
        self.acceptance_criteria = _coerce_criteria(self.acceptance_criteria)

    def is_unblocked(self, all_tasks: dict[str, "Task"]) -> bool:
        return all(
            all_tasks.get(dep) and all_tasks[dep].status == "complete"
            for dep in self.depends_on
        )

    def criteria_with_verifier(self) -> list[AcceptanceCriterion]:
        return [c for c in self.acceptance_criteria if c.verifier]


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
    # Run-tracking for the consecutive-error backoff (Bug 5). Resets to 0
    # on any successful turn. The orchestrator skips the agent for a few
    # ticks after each consecutive error and escalates / gives up at
    # configured thresholds so a flaky agent can't pin the run forever.
    # `last_error_at_turn` stores the orchestrator's tick counter (NOT
    # the per-agent turns_taken) so the backoff window ages even when no
    # agent dispatched (otherwise a sole-failing agent would be skipped
    # forever).
    consecutive_errors: int = 0
    last_error_at_turn: Optional[int] = None
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
    "verify",
    "check_criterion",
    "write_doc",
    "record_policy",
    "retire_policy",
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

    Greenfield layout (default):
      <root>/
        workspace/  ← agents write code here
        shared/     ← cross-agent artifacts (PRD, api-contract, schema, ...)
        logs/       ← per-agent JSONL transcripts
        session.json ← serialized WorldState

    Brownfield layout (--in <project>):
      <project>/        ← agents write code directly into the existing repo
      <project>/.mau/runs/<ts>/
        shared/
        logs/
        session.json
    """

    root: str  # absolute path
    code_dir_override: Optional[str] = None  # brownfield: path to existing repo
    brownfield: bool = False

    @property
    def code_dir(self) -> str:
        return self.code_dir_override or str(Path(self.root) / "workspace")

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
        Path(self.shared_dir).mkdir(parents=True, exist_ok=True)
        Path(self.logs_dir).mkdir(parents=True, exist_ok=True)
        if self.brownfield:
            self._ensure_gitignore_entry()
        else:
            Path(self.code_dir).mkdir(parents=True, exist_ok=True)

    def _ensure_gitignore_entry(self) -> None:
        """Append `.mau/` to the project's .gitignore if it has one and
        the entry isn't there yet. Don't create the file — some users
        don't track this dir under git, and creating it would surprise them."""
        gi = Path(self.code_dir) / ".gitignore"
        if not gi.exists():
            return
        try:
            existing = gi.read_text(encoding="utf-8")
        except OSError:
            return
        # Match `.mau` or `.mau/` on its own line, ignoring trailing whitespace.
        for raw_line in existing.splitlines():
            line = raw_line.strip()
            if line in (".mau", ".mau/"):
                return
        suffix = "" if existing.endswith("\n") or not existing else "\n"
        try:
            gi.write_text(existing + suffix + ".mau/\n", encoding="utf-8")
        except OSError:
            pass


@dataclass
class WorldState:
    """Shared state mutated by the orchestrator. Single-writer, multi-reader."""

    request: str = ""  # original user prompt
    workspace: Optional[Workspace] = None
    agents: dict[str, AgentState] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)  # full audit log
    # name → version history, newest last. All writers must go through
    # put_doc; readers wanting the latest go through get_doc.
    shared_docs: dict[str, list[DocVersion]] = field(default_factory=dict)
    # Durable human-approval rules. See Policy docstring; agent.py injects
    # the matching subset into every prompt so rules survive across turns
    # and across `--resume`.
    policies: list[Policy] = field(default_factory=list)
    pending_user_questions: list[Message] = field(default_factory=list)
    log: list[str] = field(default_factory=list)  # human-readable event log
    started_at: float = field(default_factory=now)
    finished: bool = False
    final_summary: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    # Brownfield-only pre-flight scan state. Lives outside `agents` because
    # the analyst isn't a Role and doesn't take orchestrator turns. The TUI
    # consults these so the user can see discovery is in flight rather than
    # mistaking a slow scan for a hang. discovery_started_at is monotonic
    # (matches AgentState.thinking_started_at semantics).
    discovery_status: Literal["none", "in_progress", "complete", "failed"] = "none"
    discovery_started_at: Optional[float] = None

    def get_doc(self, name: str) -> Optional[str]:
        versions = self.shared_docs.get(name)
        return versions[-1].content if versions else None

    def get_doc_version(self, name: str) -> Optional[DocVersion]:
        versions = self.shared_docs.get(name)
        return versions[-1] if versions else None

    def active_policies(
        self, scope_filter: Optional[str] = None
    ) -> list["Policy"]:
        """Return active policies visible under `scope_filter`. `global` is
        always returned. When `scope_filter` is None, only globals match.

        For `role:<role>` filters, a policy with scope `role:<role>` matches.
        For `task:<task_id>` filters, a policy with scope `task:<task_id>`
        matches. Exact match on the rest; no wildcards. The ordering matches
        insertion order so prompts render deterministically across reruns."""
        out: list[Policy] = []
        for p in self.policies:
            if not p.active:
                continue
            if p.scope == "global":
                out.append(p)
                continue
            if scope_filter is not None and p.scope == scope_filter:
                out.append(p)
        return out

    def add_policy(
        self, text: str, scope: str, source: str, turn: int
    ) -> "Policy":
        """Append and return a new Policy. If an active policy with the same
        (text, scope) already exists, return the existing one — dedupes the
        common case of the user passing --policy on resume."""
        text = text.strip()
        scope = (scope or "global").strip() or "global"
        for existing in self.policies:
            if existing.active and existing.text == text and existing.scope == scope:
                return existing
        policy = Policy(
            text=text,
            scope=scope,
            source=source or "user",
            created_turn=turn,
        )
        self.policies.append(policy)
        return policy

    def put_doc(
        self, name: str, content: str, author: str, turn: int
    ) -> DocVersion:
        """Append a new DocVersion. If the latest version already has the
        same content hash, return the existing version unchanged so we don't
        pile up duplicates from idempotent re-publishes (e.g. codebase map
        refresh that produced an identical scan)."""
        h = _doc_hash(content)
        versions = self.shared_docs.setdefault(name, [])
        if versions and versions[-1].hash == h:
            return versions[-1]
        version = DocVersion(
            content=content, hash=h, author=author, turn=turn
        )
        versions.append(version)
        return version

    def snapshot(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "workspace_root": self.workspace.root if self.workspace else None,
            "workspace_code_dir_override": (
                self.workspace.code_dir_override if self.workspace else None
            ),
            "workspace_brownfield": (
                self.workspace.brownfield if self.workspace else False
            ),
            "started_at": self.started_at,
            "finished": self.finished,
            "agents": {n: asdict(a) for n, a in self.agents.items()},
            "tasks": {tid: asdict(t) for tid, t in self.tasks.items()},
            "messages": [asdict(m) for m in self.messages],
            "shared_docs": {
                name: [asdict(v) for v in versions]
                for name, versions in self.shared_docs.items()
            },
            "policies": [asdict(p) for p in self.policies],
            "log": list(self.log),
            "final_summary": self.final_summary,
            "usage": asdict(self.usage),
        }
