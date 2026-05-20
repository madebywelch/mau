"""Orchestrator — the conductor.

Drives the main turn loop, dispatches agent turns concurrently via a thread
pool (bounded), applies the resulting actions to WorldState, manages task
dependencies, and enforces escalation policy.

Design notes:
- Each agent turn is a one-shot inference call. Agents are stateless processors.
- Action application is single-threaded (the orchestrator's main thread)
  to keep state mutation simple and deterministic.
- Concurrency lives at the inference level: multiple agents can be "thinking"
  in parallel, but their actions land sequentially.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Optional

from mau_cli.agent import Agent
from mau_cli.inference import InferenceBackend
from mau_cli.message_bus import MessageBus
from mau_cli.schemas import (
    AgentState,
    AgentTurn,
    Message,
    ROLES_THAT_SPAWN,
    Role,
    SUPERVISOR_OF,
    Task,
    TokenUsage,
    Workspace,
    WorldState,
    _id,
    now,
)
from mau_cli.verifiers import VERIFIERS, VerifierResult


# Tunable limits. Conservative by default to keep token spend bounded.
DEFAULT_MAX_TURNS = 80
DEFAULT_MAX_AGENTS = 12
DEFAULT_CONCURRENCY = 3
ESCALATION_AFTER_BLOCKED_TURNS = 3
MAX_TURNS_PER_AGENT = 12  # safety cap per agent to avoid runaway loops


class Orchestrator:
    def __init__(
        self,
        backend: InferenceBackend,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_agents: int = DEFAULT_MAX_AGENTS,
        concurrency: int = DEFAULT_CONCURRENCY,
        workspace: Optional[Workspace] = None,
        max_budget_usd: Optional[float] = None,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
        logs_dir: Optional[Path] = None,
    ):
        self.backend = backend
        self.max_turns = max_turns
        self.max_agents = max_agents
        self.concurrency = concurrency
        self.max_budget_usd = max_budget_usd
        self.on_event = on_event or (lambda *_: None)

        self.world = WorldState()
        if workspace is not None:
            workspace.ensure()
            self.world.workspace = workspace
        self.bus = MessageBus(self.world)
        self.agents: dict[str, Agent] = {}
        self._executor = ThreadPoolExecutor(max_workers=concurrency)
        self._lock = threading.Lock()
        self._global_turns = 0
        # Per-turn flag: agents whose deliverable was rejected this turn.
        # The "complete" action handler consults this to avoid marking a
        # rejected agent complete (which would freeze them out of `_ready_agents`).
        self._rejected_this_turn: set[str] = set()

        # Per-agent transcript directory. Defaults to <workspace>/logs/ so
        # AHE / Evolution-Agent tooling has prompt+response tapes to chew on.
        # Explicit None (and no workspace) disables logging.
        if logs_dir is not None:
            self.logs_dir: Optional[Path] = Path(logs_dir)
        elif workspace is not None:
            self.logs_dir = Path(workspace.logs_dir)
        else:
            self.logs_dir = None
        if self.logs_dir is not None:
            self.logs_dir.mkdir(parents=True, exist_ok=True)

    # ---- public API -------------------------------------------------------

    def run(self, user_request: str) -> WorldState:
        self.world.request = user_request
        self._emit("session_start", {"request": user_request})

        if (
            self.world.workspace is not None
            and self.world.workspace.brownfield
            and "codebase.md" not in self.world.shared_docs
        ):
            self._discover_codebase()

        product = self._spawn_agent(Role.PRODUCT, "product-1", "")
        kickoff = Message(
            from_agent="user",
            to_agent=product.state.name,
            msg_type="directive",
            subject="New initiative",
            body=user_request,
        )
        self.bus.deliver(kickoff)
        self._persist()
        return self._main_loop()

    def _discover_codebase(self) -> None:
        """Brownfield-only pre-flight: scan the existing project and write
        codebase.md into shared_docs so all subsequent agents see it."""
        ws = self.world.workspace
        if ws is None:
            return
        try:
            system = (
                resources.files("mau_cli.prompts")
                .joinpath("_codebase_analyst.md")
                .read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            self._emit("discovery_skipped", {"reason": "analyst prompt not found"})
            return

        shared_path = Path(ws.shared_dir) / "codebase.md"
        user_prompt = (
            f"Project root: {ws.code_dir}\n"
            f"Write your scan to this absolute path: {shared_path}\n"
            "Follow your role instructions exactly. End with the DELIVERABLE line."
        )

        import time as _t
        self.world.discovery_status = "in_progress"
        self.world.discovery_started_at = _t.monotonic()
        self._persist()
        self._emit("discovery_start", {"project_root": ws.code_dir})
        try:
            result = self.backend.call_agentic(
                system_prompt=system,
                user_prompt=user_prompt,
                workspace_dir=ws.code_dir,
                extra_dirs=[ws.shared_dir],
                max_budget_usd=self.max_budget_usd,
            )
        except Exception as e:
            self.world.discovery_status = "failed"
            self.world.discovery_started_at = None
            self._emit("discovery_error", {"error": str(e)})
            return

        self.world.usage.add(result.usage)
        if shared_path.exists():
            try:
                self.world.shared_docs["codebase.md"] = shared_path.read_text(
                    encoding="utf-8"
                )
                self.world.discovery_status = "complete"
                self.world.discovery_started_at = None
                self._emit(
                    "discovery_complete",
                    {"size": len(self.world.shared_docs["codebase.md"])},
                )
            except OSError as e:
                self.world.discovery_status = "failed"
                self.world.discovery_started_at = None
                self._emit("discovery_read_error", {"error": str(e)})
        else:
            self.world.discovery_status = "failed"
            self.world.discovery_started_at = None
            self._emit("discovery_no_output", {"path": str(shared_path)})

    def resume(self, fallback_request: Optional[str] = None) -> WorldState:
        """Continue an existing session. World state has already been
        rehydrated from disk by `load_from_disk`. If state is partial
        (no agents, e.g. soft-resume from a corrupted session.json), seed
        a fresh Product agent so the team can pick up against the existing
        shared docs and workspace files."""
        if not self.agents:
            request = fallback_request or self.world.request or "(see shared/prd.md)"
            self.world.request = request
            self._emit(
                "session_soft_resume",
                {"request": request, "shared_docs": list(self.world.shared_docs.keys())},
            )
            product = self._spawn_agent(Role.PRODUCT, "product-1", "")
            self.bus.deliver(
                Message(
                    from_agent="user",
                    to_agent=product.state.name,
                    msg_type="directive",
                    subject="Resumed initiative",
                    body=(
                        f"This run was interrupted. The original initiative was:\n\n{request}\n\n"
                        "Existing artifacts from the previous attempt are in shared/ "
                        "and the workspace is partially built. Inspect what's there "
                        "(via the shared docs in your prompt and by Read'ing files) "
                        "and pick up from where the team left off. Don't redo work "
                        "that's already done."
                    ),
                )
            )
        else:
            self._emit(
                "session_resume",
                {
                    "agents": list(self.agents.keys()),
                    "tasks": len(self.world.tasks),
                    "messages": len(self.world.messages),
                },
            )
        self._persist()
        return self._main_loop()

    def _main_loop(self) -> WorldState:
        try:
            while not self._is_done():
                if self._over_budget():
                    self.world.final_summary = (
                        f"Halted: max-budget ${self.max_budget_usd:.2f} reached "
                        f"(spent ${self.world.usage.cost_usd:.4f})."
                    )
                    self._emit("budget_reached", {"spent": self.world.usage.cost_usd})
                    break
                progressed = self._tick()
                self._persist()
                if not progressed:
                    if not self._unblock_stalled():
                        self._emit("stall", {})
                        break
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

        if not self.world.final_summary:
            self.world.final_summary = self._build_final_summary()
        self.world.finished = True
        self._persist()
        self._emit("session_end", {"summary": self.world.final_summary})
        return self.world

    def load_from_disk(self, snapshot: Optional[dict[str, Any]] = None) -> bool:
        """Rehydrate world state from a snapshot dict (typically the contents
        of session.json) and from the workspace directory. Returns True if
        a hard resume was possible (agents/tasks restored), False if only
        soft-resume context was loaded (shared docs + workspace files)."""
        ws = self.world.workspace
        if ws is None:
            return False

        # Always pick up shared docs from disk so soft-resume agents see the
        # team's prior contracts even when session.json is missing/empty.
        shared_dir = Path(ws.shared_dir)
        if shared_dir.exists():
            for f in sorted(shared_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    try:
                        self.world.shared_docs[f.name] = f.read_text(encoding="utf-8")
                    except Exception:
                        pass

        if not snapshot:
            return False

        self.world.request = snapshot.get("request", "") or self.world.request
        self.world.shared_docs.update(snapshot.get("shared_docs") or {})

        usage_d = snapshot.get("usage") or {}
        self.world.usage = TokenUsage(
            input_tokens=int(usage_d.get("input_tokens", 0) or 0),
            output_tokens=int(usage_d.get("output_tokens", 0) or 0),
            cost_usd=float(usage_d.get("cost_usd", 0.0) or 0.0),
            calls=int(usage_d.get("calls", 0) or 0),
        )

        for tid, t in (snapshot.get("tasks") or {}).items():
            try:
                self.world.tasks[tid] = Task(
                    id=t.get("id", tid),
                    title=t.get("title", ""),
                    description=t.get("description", ""),
                    assignee=t.get("assignee", ""),
                    creator=t.get("creator", ""),
                    status=t.get("status", "pending"),
                    depends_on=list(t.get("depends_on") or []),
                    acceptance_criteria=list(t.get("acceptance_criteria") or []),
                    deliverable_summary=t.get("deliverable_summary"),
                    created_at=float(t.get("created_at") or now()),
                    updated_at=float(t.get("updated_at") or now()),
                )
            except Exception:
                continue

        for m in snapshot.get("messages") or []:
            try:
                self.world.messages.append(_message_from_dict(m))
            except Exception:
                continue

        for name, a in (snapshot.get("agents") or {}).items():
            try:
                state = _agent_state_from_dict(a)
            except Exception:
                continue
            # An agent that was mid-call when killed gets reset so it can
            # retry. thinking_started_at is wall-clock and stale post-kill.
            if state.status == "thinking":
                state.status = "working"
            state.thinking_started_at = None
            self.world.agents[name] = state
            self.agents[name] = Agent(state, self.backend)

        return bool(self.agents) or bool(self.world.tasks)

    def _over_budget(self) -> bool:
        if self.max_budget_usd is None:
            return False
        return self.world.usage.cost_usd >= self.max_budget_usd

    def _persist(self) -> None:
        """Atomically write session.json. Writes to a sibling .tmp file then
        renames — Path.write_text on its own is non-atomic and a kill mid-write
        truncates the file, destroying any chance of resume."""
        if self.world.workspace is None:
            return
        try:
            import os
            target = Path(self.world.workspace.session_file)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(self.world.snapshot(), indent=2, default=str))
            os.replace(tmp, target)  # atomic on POSIX
        except Exception as e:
            self._emit("persist_error", {"error": str(e)})

    # ---- main tick --------------------------------------------------------

    def _tick(self) -> bool:
        """Run one batch of concurrent turns. Returns True if any agent acted."""
        ready: list[Agent] = self._ready_agents()
        if not ready:
            return False

        # Bound batch size by configured concurrency.
        batch = ready[: self.concurrency]
        self._emit("tick", {"batch": [a.state.name for a in batch]})

        # Mark agents as thinking so they don't get re-picked while in flight.
        import time as _t
        for agent in batch:
            agent.state.status = "thinking"
            agent.state.thinking_started_at = _t.monotonic()

        futures: dict[Future, Agent] = {}
        for agent in batch:
            self._global_turns += 1
            futures[self._executor.submit(self._safe_turn, agent)] = agent

        # Apply each completed turn synchronously in arrival order.
        for future in list(futures.keys()):
            agent = futures[future]
            try:
                turn = future.result()
            except Exception as e:
                self._emit("agent_error", {"agent": agent.state.name, "error": str(e)})
                agent.state.status = "blocked"
                agent.state.notes.append(f"inference error: {e}")
                continue

            self._apply_turn(agent, turn)
            if self._global_turns >= self.max_turns:
                self._emit("max_turns_reached", {})
                self.world.final_summary = "Halted: max_turns reached."
                return True

        return True

    def _safe_turn(self, agent: Agent) -> AgentTurn:
        return agent.run_turn(self.world)

    # ---- readiness --------------------------------------------------------

    # Message types that warrant reactivating a completed agent.
    REACTIVATING_MSG_TYPES = ("directive", "task", "blocker")

    def _ready_agents(self) -> list[Agent]:
        """Agents eligible to act this tick: status not complete/thinking,
        either inbox is non-empty OR they have an unblocked in-progress task,
        and they haven't exceeded per-agent turn cap.

        A `complete` agent is reactivated if a directive/task/blocker arrives
        in their inbox — this is how follow-up corrections get picked up
        (e.g., supervisor asking the agent to redo work)."""
        ready: list[Agent] = []
        for name, agent in self.agents.items():
            s = agent.state
            if s.status == "thinking":
                continue
            if s.status == "complete":
                if s.turns_taken < MAX_TURNS_PER_AGENT and any(
                    m.msg_type in self.REACTIVATING_MSG_TYPES for m in s.inbox
                ):
                    s.status = "working"
                    self._emit("agent_reactivated", {"agent": name})
                else:
                    continue
            if s.turns_taken >= MAX_TURNS_PER_AGENT:
                if s.status != "complete":
                    s.status = "complete"
                    self._emit("agent_capped", {"agent": name})
                continue

            has_inbox = bool(s.inbox)
            has_unblocked_task = any(
                self.world.tasks[tid].is_unblocked(self.world.tasks)
                and self.world.tasks[tid].status in ("pending", "in_progress")
                for tid in s.assigned_tasks
                if tid in self.world.tasks
            )

            if has_inbox or has_unblocked_task or s.turns_taken == 0:
                # First-turn agents are always ready (lets them initialize).
                ready.append(agent)
            elif s.assigned_tasks:
                # All assigned tasks blocked → wait, increment blocked counter.
                s.status = "blocked"
                s.blocked_turns += 1
                if s.blocked_turns >= ESCALATION_AFTER_BLOCKED_TURNS:
                    self._auto_escalate(agent)
        return ready

    def _unblock_stalled(self) -> bool:
        """Best-effort sweep when no one is ready: if any agent has been
        blocked too long, force a turn so they can ask for help."""
        for agent in self.agents.values():
            if (
                agent.state.status == "blocked"
                and agent.state.blocked_turns >= ESCALATION_AFTER_BLOCKED_TURNS
            ):
                # Synthesize a self-prompt to wake them up.
                self.bus.deliver(
                    Message(
                        from_agent="orchestrator",
                        to_agent=agent.state.name,
                        msg_type="directive",
                        subject="You have been blocked. Ask for help or escalate.",
                        body=(
                            "You have made no progress for several turns. "
                            "Either send a clarifying question to a teammate, "
                            "escalate to your supervisor, or mark complete with a status note."
                        ),
                    )
                )
                agent.state.blocked_turns = 0
                return True
        return False

    # ---- action application ----------------------------------------------

    def _apply_turn(self, agent: Agent, turn: AgentTurn) -> None:
        with self._lock:
            agent.state.status = turn.status
            agent.state.thinking_started_at = None  # turn finished
            agent.state.last_activity_at = now()
            if turn.thoughts:
                agent.state.notes.append(turn.thoughts[:500])
            self._emit(
                "agent_turn",
                {
                    "agent": agent.state.name,
                    "thoughts": turn.thoughts,
                    "status": turn.status,
                    "actions": [a.get("type") for a in turn.actions],
                },
            )

            for action in turn.actions:
                try:
                    self._apply_action(agent, action)
                except Exception as e:
                    self._emit(
                        "action_error",
                        {"agent": agent.state.name, "action": action, "error": str(e)},
                    )
                    agent.state.notes.append(f"action error: {e}")

            rejected = agent.state.name in self._rejected_this_turn
            self._log_transcript(agent, turn, accepted=not rejected)

            # If a deliverable was rejected this turn, force the agent back
            # into `working` regardless of any later actions or turn-level status.
            if rejected:
                agent.state.status = "working"
                self._rejected_this_turn.discard(agent.state.name)

    def _log_transcript(self, agent: Agent, turn: AgentTurn, *, accepted: bool) -> None:
        """Append one JSONL line per agent turn to logs/<agent>.jsonl.

        Foundational for AHE: without prompt+response tapes per agent, runs
        can't be debugged, replayed, or regression-tested. Best-effort —
        a transcript write failure must never abort a turn."""
        if self.logs_dir is None or agent.last_result is None:
            return
        result = agent.last_result
        files_touched = [
            a.get("files_touched", []) for a in turn.actions if a.get("type") == "deliverable"
        ]
        flat_files = sorted({f for sub in files_touched for f in (sub or [])})
        record = {
            "turn": agent.state.turns_taken,
            "agent": agent.state.name,
            "role": agent.state.role.value,
            "timestamp": now(),
            "prompt": agent.last_prompt,
            "response": result.raw_text,
            "tokens": {
                "input": result.usage.input_tokens,
                "output": result.usage.output_tokens,
                "cost_usd": result.usage.cost_usd,
            },
            "files_touched": flat_files or list(result.files_touched),
            "accepted": accepted,
            "status": turn.status,
            "backend": result.backend,
            "duration_ms": result.duration_ms,
        }
        try:
            path = self.logs_dir / f"{agent.state.name}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as e:
            self._emit("transcript_error", {"agent": agent.state.name, "error": str(e)})

    # ---- workspace-aware helpers ----------------------------------------

    def _verify_files(self, claimed: list[str]) -> list[str]:
        """Return paths that the agent claimed but don't exist on disk
        within the workspace. Paths outside the workspace are also flagged.

        Delegates to PathExistsVerifier so the verify-action path and the
        deliverable-check path share one implementation of the containment +
        existence rules."""
        if not claimed or self.world.workspace is None:
            return []
        ws_root = Path(self.world.workspace.code_dir)
        result = VERIFIERS["path_exists"].run({"paths": list(claimed)}, ws_root)
        return list(result.details.get("missing") or [])

    def _apply_action(self, agent: Agent, action: dict[str, Any]) -> None:
        action_type = action.get("type")

        if action_type == "send_message":
            msg = Message(
                from_agent=agent.state.name,
                to_agent=str(action.get("to", "")),
                msg_type=action.get("msg_type", "status"),  # type: ignore[arg-type]
                subject=str(action.get("subject", ""))[:200],
                body=str(action.get("body", "")),
                references=list(action.get("references", []) or []),
            )
            self.bus.deliver(msg)

        elif action_type == "create_task":
            if not self._can_create_tasks(agent):
                self._emit(
                    "policy_violation",
                    {"agent": agent.state.name, "reason": "cannot create tasks"},
                )
                return
            task = Task(
                id=str(action.get("id") or _id("task")),
                title=str(action.get("title", "Untitled")),
                description=str(action.get("description", "")),
                assignee=str(action.get("assignee", "")),
                creator=agent.state.name,
                depends_on=list(action.get("depends_on", []) or []),
                acceptance_criteria=list(action.get("acceptance_criteria", []) or []),
            )
            self.world.tasks[task.id] = task
            assignee = self.world.agents.get(task.assignee)
            if assignee is not None:
                assignee.assigned_tasks.append(task.id)
                # Notify the assignee via inbox.
                self.bus.deliver(
                    Message(
                        from_agent=agent.state.name,
                        to_agent=task.assignee,
                        msg_type="task",
                        subject=f"Assigned: {task.title}",
                        body=(
                            f"Task {task.id}\n"
                            f"{task.description}\n"
                            f"Acceptance: {', '.join(task.acceptance_criteria) or '(none)'}\n"
                            f"Depends on: {', '.join(task.depends_on) or '(none)'}"
                        ),
                        references=[task.id],
                    )
                )
            self._emit("task_created", {"id": task.id, "title": task.title})

        elif action_type == "spawn_agent":
            if not self._can_spawn(agent):
                self._emit(
                    "policy_violation",
                    {"agent": agent.state.name, "reason": "cannot spawn agents"},
                )
                return
            if len(self.agents) >= self.max_agents:
                self._emit("spawn_capped", {"reason": "max_agents reached"})
                return
            try:
                role = Role(action.get("role"))
            except ValueError:
                self._emit("spawn_invalid", {"role": action.get("role")})
                return
            name = str(action.get("name") or self._unique_name(role))
            specialization = str(action.get("specialization", ""))
            self._spawn_agent(role, name, specialization)

        elif action_type == "deliverable":
            # If a prior verify in this same turn failed, refuse to record the
            # deliverable. The verifier already delivered a blocker; recording
            # the deliverable would mark the task complete despite the failure.
            if agent.state.name in self._rejected_this_turn:
                self._emit(
                    "deliverable_rejected",
                    {"agent": agent.state.name, "reason": "verify failed earlier this turn"},
                )
                return
            files = list(action.get("files_touched", []) or [])
            missing = self._verify_files(files)
            if missing:
                # Hallucination: agent claimed files that don't exist on disk.
                # Reject the deliverable, mark the agent for reactivation,
                # and tell them about the gap so they can correct it.
                agent.state.notes.append(
                    f"deliverable rejected — claimed files missing on disk: {missing}"
                )
                self._rejected_this_turn.add(agent.state.name)
                self.bus.deliver(
                    Message(
                        from_agent="orchestrator",
                        to_agent=agent.state.name,
                        msg_type="blocker",
                        subject="Deliverable rejected — claimed files don't exist",
                        body=(
                            f"You emitted a DELIVERABLE listing files_touched={files}, "
                            f"but these are missing in the workspace: {missing}.\n\n"
                            f"Did you actually call the Write tool for each file? "
                            f"Re-do the work for real this time and emit a fresh DELIVERABLE."
                        ),
                    )
                )
                self._emit(
                    "deliverable_rejected",
                    {"agent": agent.state.name, "claimed": files, "missing": missing},
                )
                return

            agent.state.deliverables.append(
                f"{action.get('title', 'deliverable')}: {action.get('summary', '')}"
            )
            for f in files:
                if f not in agent.state.files_touched:
                    agent.state.files_touched.append(f)
            anchored = False
            for tid in agent.state.assigned_tasks:
                task = self.world.tasks.get(tid)
                if task and task.status != "complete":
                    task.status = "complete"
                    summary = str(action.get("summary", ""))
                    if files:
                        summary = f"{summary} (files: {', '.join(files)})"
                    task.deliverable_summary = summary
                    self.bus.deliver(
                        Message(
                            from_agent=agent.state.name,
                            to_agent=task.creator,
                            msg_type="deliverable",
                            subject=f"Delivered: {task.title}",
                            body=summary,
                            references=[task.id],
                        )
                    )
                    self._notify_downstream(task)
                    anchored = True
                    break
            if not anchored:
                # Roll-up deliverable from an agent without an open task
                # (e.g. Tech Lead summarizing the team's work). Route to
                # their supervisor so the chain-of-command keeps moving.
                supervisor_role = SUPERVISOR_OF.get(agent.state.role)
                target = self._first_agent_of_role(supervisor_role) if supervisor_role else None
                if target:
                    self.bus.deliver(
                        Message(
                            from_agent=agent.state.name,
                            to_agent=target,
                            msg_type="deliverable",
                            subject=f"Roll-up: {action.get('title', 'deliverable')}",
                            body=str(action.get("summary", "")),
                        )
                    )

        elif action_type == "write_doc":
            name = str(action.get("name", "")).strip()
            content = str(action.get("content", ""))
            if not name:
                self._emit("write_doc_invalid", {"agent": agent.state.name})
                return
            self.world.shared_docs[name] = content
            if self.world.workspace is not None:
                path = Path(self.world.workspace.shared_dir) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            self._emit("doc_written", {"agent": agent.state.name, "name": name, "len": len(content)})

        elif action_type == "escalate":
            self._escalate(agent, str(action.get("reason", "no reason given")))

        elif action_type == "ask_user":
            self.bus.deliver(
                Message(
                    from_agent=agent.state.name,
                    to_agent="user",
                    msg_type="question",
                    subject=str(action.get("subject", "Question for product")),
                    body=str(action.get("body", "")),
                )
            )

        elif action_type == "complete":
            if agent.state.name in self._rejected_this_turn:
                # The agent's deliverable was rejected earlier in this turn —
                # don't let a trailing "complete" action freeze them out.
                return
            agent.state.status = "complete"
            summary = str(action.get("summary", "")).strip()
            if summary:
                agent.state.notes.append(f"complete: {summary}")
            self._emit("agent_complete", {"agent": agent.state.name})

        elif action_type == "note":
            agent.state.notes.append(str(action.get("body", ""))[:500])

        elif action_type == "verify":
            self._dispatch_verify(agent, action)

        else:
            self._emit("unknown_action", {"agent": agent.state.name, "type": action_type})

    def _dispatch_verify(self, agent: Agent, action: dict[str, Any]) -> None:
        """Run the named verifier against the workspace. Failures surface as
        blocker messages (same channel as a rejected deliverable) and flip
        the per-turn rejection bit so transcripts mark the turn as not
        accepted."""
        verifier_name = str(action.get("verifier", "")).strip()
        spec = action.get("spec") or {}
        if not isinstance(spec, dict):
            self._emit(
                "verify_invalid",
                {"agent": agent.state.name, "reason": "spec must be an object"},
            )
            return

        verifier = VERIFIERS.get(verifier_name)
        if verifier is None:
            self._emit(
                "verify_invalid",
                {
                    "agent": agent.state.name,
                    "verifier": verifier_name,
                    "known": sorted(VERIFIERS.keys()),
                },
            )
            agent.state.notes.append(f"verify: unknown verifier {verifier_name!r}")
            return

        if self.world.workspace is None:
            self._emit(
                "verify_skipped",
                {"agent": agent.state.name, "reason": "no workspace"},
            )
            return

        ws_root = Path(self.world.workspace.code_dir)
        try:
            result = verifier.run(spec, ws_root)
        except Exception as e:
            result = VerifierResult(
                ok=False,
                summary=f"verifier crashed: {e}",
                details={"error": str(e)},
            )

        payload = {
            "agent": agent.state.name,
            "verifier": verifier_name,
            "ok": result.ok,
            "summary": result.summary,
        }
        if result.ok:
            self._emit("verify_passed", payload)
            agent.state.notes.append(
                f"verify ok ({verifier_name}): {result.summary}"
            )
            return

        self._emit("verify_failed", {**payload, "details": result.details})
        agent.state.notes.append(
            f"verify failed ({verifier_name}): {result.summary}"
        )
        self._rejected_this_turn.add(agent.state.name)
        self.bus.deliver(
            Message(
                from_agent="orchestrator",
                to_agent=agent.state.name,
                msg_type="blocker",
                subject=f"Verify failed: {verifier_name}",
                body=(
                    f"Your `verify` action using `{verifier_name}` failed:\n"
                    f"{result.summary}\n\n"
                    f"Spec: {json.dumps(spec, default=str)[:600]}\n"
                    "Address the failure and re-run the verifier before claiming done."
                ),
            )
        )

    # ---- helpers ----------------------------------------------------------

    def _spawn_agent(self, role: Role, name: str, specialization: str) -> Agent:
        if name in self.agents:
            return self.agents[name]
        state = AgentState(name=name, role=role, specialization=specialization)
        self.world.agents[name] = state
        agent = Agent(state, self.backend)
        self.agents[name] = agent
        self._emit("agent_spawned", {"name": name, "role": role.value})
        return agent

    # ---- end Orchestrator class methods follow above; module-level helpers below ----

    def _first_agent_of_role(self, role: Optional[Role]) -> Optional[str]:
        if role is None:
            return None
        for agent in self.agents.values():
            if agent.state.role == role:
                return agent.state.name
        return None

    def _unique_name(self, role: Role) -> str:
        base = role.value.replace("_", "-")
        n = 1
        while f"{base}-{n}" in self.agents:
            n += 1
        return f"{base}-{n}"

    def _can_spawn(self, agent: Agent) -> bool:
        return agent.state.role in ROLES_THAT_SPAWN

    def _can_create_tasks(self, agent: Agent) -> bool:
        # EM and TL primarily; PM may also create high-level tasks.
        return agent.state.role in ROLES_THAT_SPAWN

    def _notify_downstream(self, completed: Task) -> None:
        """When a task completes, ping the assignees of every task that
        depends on it. They become unblocked next tick."""
        for task in self.world.tasks.values():
            if completed.id in task.depends_on and task.status != "complete":
                if task.assignee in self.world.agents:
                    self.bus.deliver(
                        Message(
                            from_agent="orchestrator",
                            to_agent=task.assignee,
                            msg_type="status",
                            subject=f"Upstream complete: {completed.title}",
                            body=(
                                f"Task {completed.id} is now complete. "
                                f"Deliverable: {completed.deliverable_summary or '(no summary)'}. "
                                f"You can proceed with {task.id}."
                            ),
                            references=[completed.id, task.id],
                        )
                    )

    def _escalate(self, agent: Agent, reason: str) -> None:
        supervisor_role = SUPERVISOR_OF.get(agent.state.role)
        if supervisor_role is None or supervisor_role == Role.USER:
            self.bus.deliver(
                Message(
                    from_agent=agent.state.name,
                    to_agent="user",
                    msg_type="escalation",
                    subject=f"Escalation from {agent.state.name}",
                    body=reason,
                )
            )
            self._emit("escalation_to_user", {"agent": agent.state.name, "reason": reason})
            return
        # Find the nearest agent of that supervisor role.
        for target in self.agents.values():
            if target.state.role == supervisor_role:
                self.bus.deliver(
                    Message(
                        from_agent=agent.state.name,
                        to_agent=target.state.name,
                        msg_type="escalation",
                        subject=f"Escalation: {agent.state.name} blocked",
                        body=reason,
                    )
                )
                self._emit(
                    "escalation",
                    {"from": agent.state.name, "to": target.state.name, "reason": reason},
                )
                return
        # No supervisor found — bubble straight to user.
        self.bus.deliver(
            Message(
                from_agent=agent.state.name,
                to_agent="user",
                msg_type="escalation",
                subject=f"Escalation from {agent.state.name} (no supervisor in team)",
                body=reason,
            )
        )

    def _auto_escalate(self, agent: Agent) -> None:
        if any(t for t in self.world.tasks.values() if t.assignee == agent.state.name and t.status != "complete"):
            self._escalate(agent, "Stuck for several turns with no inbox progress.")
        agent.state.blocked_turns = 0

    # ---- termination ------------------------------------------------------

    def _is_done(self) -> bool:
        if self.world.finished:
            return True
        if self._global_turns >= self.max_turns:
            return True
        if not self.agents:
            return False
        # Done when all agents are complete and no tasks remain in flight.
        all_complete = all(a.state.status == "complete" for a in self.agents.values())
        no_open_tasks = all(
            t.status in ("complete", "cancelled") for t in self.world.tasks.values()
        )
        return all_complete and no_open_tasks

    def _build_final_summary(self) -> str:
        lines = [
            f"Run completed in {self._global_turns} turns "
            f"with {len(self.agents)} agents and {len(self.world.tasks)} tasks.",
            "",
            "Deliverables:",
        ]
        any_delivered = False
        for agent in self.agents.values():
            for d in agent.state.deliverables:
                any_delivered = True
                lines.append(f"  - {agent.state.name} ({agent.state.role.value}): {d}")
        if not any_delivered:
            lines.append("  (none recorded)")
        return "\n".join(lines)

    # ---- events -----------------------------------------------------------

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:
            pass


# ---- module-level snapshot helpers ----------------------------------------


def _message_from_dict(d: dict[str, Any]) -> Message:
    return Message(
        id=d.get("id", ""),
        from_agent=d.get("from_agent", ""),
        to_agent=d.get("to_agent", ""),
        msg_type=d.get("msg_type", "status"),
        subject=d.get("subject", ""),
        body=d.get("body", ""),
        references=list(d.get("references") or []),
        timestamp=float(d.get("timestamp") or now()),
    )


def _agent_state_from_dict(d: dict[str, Any]) -> AgentState:
    role = Role(d["role"])
    usage_d = d.get("usage") or {}
    return AgentState(
        name=d["name"],
        role=role,
        specialization=d.get("specialization", ""),
        status=d.get("status", "idle"),
        inbox=[_message_from_dict(m) for m in d.get("inbox") or []],
        history=[_message_from_dict(m) for m in d.get("history") or []],
        assigned_tasks=list(d.get("assigned_tasks") or []),
        deliverables=list(d.get("deliverables") or []),
        files_touched=list(d.get("files_touched") or []),
        turns_taken=int(d.get("turns_taken") or 0),
        blocked_turns=int(d.get("blocked_turns") or 0),
        notes=list(d.get("notes") or []),
        usage=TokenUsage(
            input_tokens=int(usage_d.get("input_tokens") or 0),
            output_tokens=int(usage_d.get("output_tokens") or 0),
            cost_usd=float(usage_d.get("cost_usd") or 0.0),
            calls=int(usage_d.get("calls") or 0),
        ),
        thinking_started_at=None,
        last_activity_at=float(d.get("last_activity_at") or now()),
    )
