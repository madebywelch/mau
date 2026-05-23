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

import difflib
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Optional

from mau_cli.agent import Agent
from mau_cli.inference import InferenceBackend
from mau_cli.isolation import (
    IsolationBackend,
    IsolationMode,
    make_isolation_backend,
)
from mau_cli.message_bus import MessageBus
from mau_cli.schemas import (
    AcceptanceCriterion,
    AgentState,
    AgentTurn,
    DocVersion,
    Message,
    Policy,
    ROLES_THAT_SPAWN,
    Role,
    SUPERVISOR_OF,
    Task,
    TokenUsage,
    Workspace,
    WorldState,
    _doc_hash,
    _id,
    now,
    now_iso,
)
from mau_cli.verifiers import VERIFIERS, VerifierResult


# Tunable limits. Conservative by default to keep token spend bounded.
DEFAULT_MAX_TURNS = 80
DEFAULT_MAX_AGENTS = 12
DEFAULT_CONCURRENCY = 3
ESCALATION_AFTER_BLOCKED_TURNS = 3
MAX_TURNS_PER_AGENT = 12  # safety cap per agent to avoid runaway loops

# Bug 5 — consecutive-error backoff. After an agent_error we skip the agent
# for `min(consecutive_errors, ERROR_BACKOFF_TICKS)` subsequent ticks so the
# orchestrator can't burn turns retrying the same flaky agent every loop
# iteration. At ERROR_ESCALATE_AT we route a blocker to the supervisor; at
# ERROR_GIVEUP_AT we force-complete the agent so the run keeps moving rather
# than stalling on one stuck participant.
ERROR_BACKOFF_TICKS = 3
ERROR_ESCALATE_AT = 3
ERROR_GIVEUP_AT = 5

# Re-scan the codebase and re-publish shared/codebase.md every N landed
# deliverables in brownfield mode. The paper calls out that one-shot scans
# decay — refreshing this keeps agents from reading a stale map as ground
# truth. put_doc dedups by hash so a no-change scan is a no-op.
CODEBASE_REFRESH_EVERY_DELIVERABLES = 10
DIFF_PREVIEW_LINES = 40
NEW_DOC_PREVIEW_LINES = 20


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
        isolation: IsolationMode = "auto",
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
        # Tick counter (not turns!). Increments every _tick() call, even
        # if no agent dispatched. Used to age error-backoff windows so a
        # stalled agent eventually gets reconsidered.
        self._tick_count = 0
        # Per-turn flag: agents whose deliverable was rejected this turn.
        # The "complete" action handler consults this to avoid marking a
        # rejected agent complete (which would freeze them out of `_ready_agents`).
        self._rejected_this_turn: set[str] = set()
        # Per-turn cwd: the worktree path the agent ran in this tick. Set in
        # `_safe_turn`, read by `_apply_action` so verifiers run against the
        # agent's pre-merge tree (the whole point of per-agent isolation).
        # Cleared between ticks.
        self._turn_cwd: dict[str, Path] = {}
        # One-shot guards so the same termination event isn't emitted twice
        # from `_is_done` (which is polled on every loop iteration).
        self._turn_cap_announced = False
        self._completion_announced = False
        self._budget_reached_announced = False
        # Number of accepted deliverables; gates periodic codebase refresh.
        self._landed_deliverables = 0

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

        # Per-agent isolation backend. `auto` selects worktrees when the
        # workspace is inside a git repo, else falls back to shared. We
        # defer construction so `worktree_disabled` events emit *after*
        # the caller has wired `on_event` (CLI does that after the
        # Orchestrator ctor returns). `_ensure_isolation` is idempotent
        # and called from both `run()` and `resume()`.
        self._isolation_mode: IsolationMode = isolation
        self._isolation_initialized = False
        self.isolation: IsolationBackend  # set in _ensure_isolation
        from mau_cli.isolation import SharedWorkspaceBackend
        self.isolation = SharedWorkspaceBackend(
            Path(workspace.code_dir) if workspace is not None else Path.cwd()
        )

    # ---- public API -------------------------------------------------------

    def _ensure_isolation(self) -> None:
        """Build the real isolation backend now that `on_event` is wired,
        so `worktree_disabled` and friends surface to the caller. Called
        once per session from `run()` / `resume()`."""
        if self._isolation_initialized:
            return
        self._isolation_initialized = True
        if self.world.workspace is None:
            return
        from mau_cli.isolation import make_isolation_backend as _make
        try:
            self.isolation = _make(
                code_dir=Path(self.world.workspace.code_dir),
                mode=self._isolation_mode,
                brownfield=self.world.workspace.brownfield,
                emit=self._emit,
            )
        except Exception as e:
            self._emit("isolation_init_failed", {"error": str(e)})

    def run(self, user_request: str) -> WorldState:
        self.world.request = user_request
        self._emit("session_start", {"request": user_request})
        self._ensure_isolation()

        if (
            self.world.workspace is not None
            and self.world.workspace.brownfield
            and self.world.get_doc("codebase.md") is None
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
        self._run_codebase_scan(reason="initial")

    def refresh_codebase_map(self, force: bool = False) -> Optional[DocVersion]:
        """Re-run the brownfield codebase scan and publish a fresh version of
        codebase.md. Returns the published DocVersion, or None if the scan
        was skipped/failed. If the new scan is byte-identical to the previous
        version, put_doc dedups and no new version is appended (the existing
        one is returned).

        `force` is currently unused — put_doc's hash-dedup makes redundant
        scans cheap on the harness side, but the caller still pays the
        analyst's inference cost, so callers typically gate themselves."""
        if self.world.workspace is None or not self.world.workspace.brownfield:
            return None
        return self._run_codebase_scan(reason="refresh")

    def _run_codebase_scan(self, *, reason: str) -> Optional[DocVersion]:
        ws = self.world.workspace
        if ws is None:
            return None
        try:
            system = (
                resources.files("mau_cli.prompts")
                .joinpath("_codebase_analyst.md")
                .read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            self._emit("discovery_skipped", {"reason": "analyst prompt not found"})
            return None

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
        self._emit("discovery_start", {"project_root": ws.code_dir, "reason": reason})
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
            self._emit("discovery_error", {"error": str(e), "reason": reason})
            return None

        self.world.usage.add(result.usage)
        if not shared_path.exists():
            self.world.discovery_status = "failed"
            self.world.discovery_started_at = None
            self._emit("discovery_no_output", {"path": str(shared_path), "reason": reason})
            return None
        try:
            content = shared_path.read_text(encoding="utf-8")
        except OSError as e:
            self.world.discovery_status = "failed"
            self.world.discovery_started_at = None
            self._emit("discovery_read_error", {"error": str(e), "reason": reason})
            return None

        version = self._publish_doc(
            name="codebase.md",
            content=content,
            author="system",
            persist_to_disk=False,  # analyst already wrote shared_path itself
        )
        self.world.discovery_status = "complete"
        self.world.discovery_started_at = None
        self._emit(
            "discovery_complete",
            {"size": len(content), "reason": reason, "hash": version.hash},
        )
        return version

    def resume(self, fallback_request: Optional[str] = None) -> WorldState:
        """Continue an existing session. World state has already been
        rehydrated from disk by `load_from_disk`. If state is partial
        (no agents, e.g. soft-resume from a corrupted session.json), seed
        a fresh Product agent so the team can pick up against the existing
        shared docs and workspace files."""
        self._ensure_isolation()
        if not self.agents:
            request = fallback_request or self.world.request or "(see shared/prd.md)"
            self.world.request = request
            self._emit(
                "session_soft_resume",
                {
                    "request": request,
                    "shared_docs": list(self.world.shared_docs.keys()),
                },
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
                    if not self._budget_reached_announced:
                        self._emit(
                            "budget_reached",
                            {"spent": self.world.usage.cost_usd},
                        )
                        self._budget_reached_announced = True
                    break
                progressed = self._tick()
                self._persist()
                if not progressed:
                    # Agents in error-backoff aren't a stall — they'll be
                    # eligible again after a few ticks elapse. Skip the
                    # stall break in that case so the backoff window
                    # actually plays out.
                    if self._any_agent_in_error_backoff():
                        continue
                    if not self._unblock_stalled():
                        self._emit("stall", {})
                        break
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            try:
                self.isolation.cleanup()
            except Exception as e:
                self._emit("worktree_cleanup_error", {"error": str(e)})

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
                        content = f.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    self.world.put_doc(
                        name=f.name,
                        content=content,
                        author="disk",
                        turn=0,
                    )

        if not snapshot:
            return False

        self.world.request = snapshot.get("request", "") or self.world.request
        self._rehydrate_shared_docs(snapshot.get("shared_docs") or {})
        self._rehydrate_policies(snapshot.get("policies") or [])

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
                    satisfied_doc_versions=dict(t.get("satisfied_doc_versions") or {}),
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

    def _rehydrate_shared_docs(self, raw: Any) -> None:
        """Restore shared_docs from a snapshot. Tolerates both the new
        list[DocVersion] shape and the legacy dict[str, str] shape so existing
        on-disk sessions can resume without manual migration."""
        if not isinstance(raw, dict):
            return
        for name, value in raw.items():
            if isinstance(value, str):
                self.world.shared_docs[name] = [
                    DocVersion(
                        content=value,
                        hash=_doc_hash(value),
                        author="legacy",
                        timestamp=now_iso(),
                        turn=0,
                    )
                ]
                continue
            if not isinstance(value, list):
                continue
            versions: list[DocVersion] = []
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                content = str(entry.get("content", ""))
                try:
                    turn = int(entry.get("turn") or 0)
                except (TypeError, ValueError):
                    turn = 0
                versions.append(
                    DocVersion(
                        content=content,
                        hash=str(entry.get("hash") or _doc_hash(content)),
                        author=str(entry.get("author") or "unknown"),
                        timestamp=str(entry.get("timestamp") or now_iso()),
                        turn=turn,
                    )
                )
            if versions:
                self.world.shared_docs[name] = versions

    def _rehydrate_policies(self, raw: Any) -> None:
        """Restore policies from a snapshot. Tolerant of missing/legacy keys
        so pre-Task-5 session.json files resume cleanly (default to no
        policies)."""
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text", "")).strip()
            if not text:
                continue
            try:
                created_turn = int(entry.get("created_turn") or 0)
            except (TypeError, ValueError):
                created_turn = 0
            self.world.policies.append(
                Policy(
                    id=str(entry.get("id") or _id("pol")),
                    text=text,
                    scope=str(entry.get("scope") or "global"),
                    source=str(entry.get("source") or "user"),
                    created_at=str(entry.get("created_at") or now_iso()),
                    created_turn=created_turn,
                    active=bool(entry.get("active", True)),
                )
            )

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
        self._tick_count += 1
        # Pre-flight budget check: refuse to dispatch any new turn if we're
        # already at/over the cap. The post-turn check in _main_loop only
        # fires AFTER a turn lands, by which point a $1+ call can have piled
        # onto an already-met cap. The dispatch-time gate ensures no fresh
        # turn starts after `total_cost_usd >= max_budget_usd`. In-flight
        # turns from a prior tick still complete; spend can overshoot
        # slightly because of them, but no new expensive turn gets queued.
        if self._over_budget():
            if not self._budget_reached_announced:
                self._emit(
                    "budget_reached", {"spent": self.world.usage.cost_usd}
                )
                self._budget_reached_announced = True
            return False

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
            # Acquire the per-agent cwd on the orchestrator thread (worktree
            # creation calls `git`, which we don't want racing inside the
            # pool). For non-agentic roles, the isolation backend still
            # hands back a path but no `git` call has to happen — shared
            # returns code_dir; worktree creates exactly once per agent.
            cwd = self._acquire_cwd(agent)
            futures[self._executor.submit(self._safe_turn, agent, cwd)] = agent

        # Apply each completed turn synchronously in arrival order.
        for future in list(futures.keys()):
            agent = futures[future]
            try:
                turn = future.result()
            except Exception as e:
                self._handle_agent_error(agent, str(e))
                continue

            self._apply_turn(agent, turn)
            # Successful turn — clear the consecutive-error counter so a
            # later transient flake gets a fresh backoff budget.
            if agent.state.consecutive_errors:
                agent.state.consecutive_errors = 0
                agent.state.last_error_at_turn = None
            if self._global_turns >= self.max_turns:
                self._emit("stopped_on_turn_cap", {"turns": self._global_turns})
                self._emit("max_turns_reached", {})  # back-compat alias
                self.world.final_summary = "Halted: max_turns reached."
                return True

        return True

    def _handle_agent_error(self, agent: Agent, error: str) -> None:
        """Apply consecutive-error bookkeeping, escalation, and give-up logic.
        Called from `_tick` when an agent's inference future raises."""
        agent.state.consecutive_errors += 1
        # Stamp the tick (not the global turn count): the backoff predicate
        # needs to age even when no agent dispatched, which only happens via
        # the tick counter.
        agent.state.last_error_at_turn = self._tick_count
        agent.state.notes.append(f"inference error: {error}")
        self._emit(
            "agent_error",
            {
                "agent": agent.state.name,
                "error": error,
                "consecutive_errors": agent.state.consecutive_errors,
            },
        )
        # Discard the worktree — turn aborted, nothing to merge.
        self._release_cwd(agent, merge=False)

        if agent.state.consecutive_errors >= ERROR_GIVEUP_AT:
            # Force-complete so the rest of the team can converge. We
            # intentionally sacrifice this agent's contribution rather than
            # let a flaky participant stall the whole run.
            agent.state.status = "complete"
            agent.state.notes.append(
                f"force-completed after {agent.state.consecutive_errors} consecutive errors"
            )
            self._emit(
                "agent_given_up",
                {
                    "agent": agent.state.name,
                    "consecutive_errors": agent.state.consecutive_errors,
                    "last_error": error,
                },
            )
            return

        if agent.state.consecutive_errors == ERROR_ESCALATE_AT:
            # Hit the escalation threshold exactly once — route a blocker
            # to the supervisor via the same channel as a verify-failed
            # rejection. The agent stays `blocked` so the backoff predicate
            # still keeps them out of the next few ticks.
            agent.state.status = "blocked"
            self._notify_supervisor_of_error(agent, error)
        else:
            agent.state.status = "blocked"

    def _notify_supervisor_of_error(self, agent: Agent, error: str) -> None:
        """Deliver a blocker to the agent's supervisor (or the user if the
        agent sits at the top of SUPERVISOR_OF)."""
        supervisor_role = SUPERVISOR_OF.get(agent.state.role)
        target_name: Optional[str] = None
        if supervisor_role is not None and supervisor_role != Role.USER:
            target_name = self._first_agent_of_role(supervisor_role)
        if target_name is None:
            target_name = "user"
        body = (
            f"Agent {agent.state.name} ({agent.state.role.value}) has failed "
            f"{agent.state.consecutive_errors} turns in a row. "
            f"Last error: {error[:600]}\n\n"
            "The orchestrator is backing off for a few ticks before retrying. "
            "If this keeps happening the agent will be force-completed and "
            "their contribution will be lost — reassign, redirect, or "
            "investigate."
        )
        self.bus.deliver(
            Message(
                from_agent="orchestrator",
                to_agent=target_name,
                msg_type="blocker",
                subject=f"{agent.state.name} stuck after {agent.state.consecutive_errors} errors",
                body=body,
            )
        )
        self._emit(
            "agent_error_escalated",
            {
                "agent": agent.state.name,
                "supervisor": target_name,
                "consecutive_errors": agent.state.consecutive_errors,
                "last_error": error,
            },
        )

    def _acquire_cwd(self, agent: Agent) -> Path:
        """Acquire and remember the per-agent cwd for this turn. Failures
        (e.g. git worktree add error) fall back to the shared workspace
        path so the turn still runs — losing isolation on this tick is
        better than dropping the agent's work entirely."""
        try:
            path = self.isolation.acquire(agent.state.name)
        except Exception as e:
            self._emit(
                "worktree_acquire_failed",
                {"agent": agent.state.name, "error": str(e)},
            )
            if self.world.workspace is not None:
                path = Path(self.world.workspace.code_dir)
            else:
                path = Path.cwd()
        self._turn_cwd[agent.state.name] = path
        return path

    def _release_cwd(self, agent: Agent, *, merge: bool) -> None:
        if agent.state.name not in self._turn_cwd:
            return
        try:
            self.isolation.release(agent.state.name, merge=merge)
        except Exception as e:
            self._emit(
                "worktree_release_failed",
                {"agent": agent.state.name, "error": str(e), "merge": merge},
            )
        self._turn_cwd.pop(agent.state.name, None)

    def _safe_turn(self, agent: Agent, cwd: Path) -> AgentTurn:
        return agent.run_turn(self.world, cwd=cwd)

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
            # Consecutive-error backoff: skip this agent until enough ticks
            # have elapsed since their last error. min(consecutive_errors,
            # ERROR_BACKOFF_TICKS) means 1 tick after first error, 2 after
            # second, capped at ERROR_BACKOFF_TICKS to keep the wait bounded.
            # We compare against `_tick_count` rather than `_global_turns` so
            # the window ages even when no other agent dispatched in between.
            if (
                s.consecutive_errors
                and s.last_error_at_turn is not None
                and (self._tick_count - s.last_error_at_turn)
                < min(s.consecutive_errors, ERROR_BACKOFF_TICKS)
            ):
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

    def _any_agent_in_error_backoff(self) -> bool:
        """True if at least one not-yet-given-up agent is currently being
        skipped purely because of the error backoff window. Used by the
        main loop to keep ticking instead of declaring stall while the
        backoff plays out — the next tick will age the window."""
        for agent in self.agents.values():
            s = agent.state
            if s.status == "complete":
                continue
            if (
                s.consecutive_errors
                and s.last_error_at_turn is not None
                and (self._tick_count - s.last_error_at_turn)
                < min(s.consecutive_errors, ERROR_BACKOFF_TICKS)
            ):
                return True
        return False

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

            # Merge the agent's worktree back to the main workspace iff this
            # turn wasn't rejected. Done *after* action application so the
            # deliverable's verifiers and acceptance-criterion auto-checks
            # have already run against the pre-merge tree (uncontaminated).
            self._release_cwd(agent, merge=not rejected)

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
        worktree_path = self._turn_cwd.get(agent.state.name)
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
            "worktree_path": str(worktree_path) if worktree_path else None,
            "isolation": self.isolation.name,
        }
        try:
            path = self.logs_dir / f"{agent.state.name}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as e:
            self._emit("transcript_error", {"agent": agent.state.name, "error": str(e)})

    # ---- workspace-aware helpers ----------------------------------------

    def _verify_files(
        self, claimed: list[str], cwd: Optional[Path] = None
    ) -> list[str]:
        """Return paths that the agent claimed but don't exist on disk
        within the workspace. Paths outside the workspace are also flagged.

        Runs against the agent's worktree (passed as `cwd`) so we catch
        "claimed but never written" before merging the worktree into the
        shared tree. Falls back to the workspace root for callers without
        per-agent context (e.g. legacy paths)."""
        if not claimed or self.world.workspace is None:
            return []
        ws_root = cwd or Path(self.world.workspace.code_dir)
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
                self.bus.deliver(
                    Message(
                        from_agent=agent.state.name,
                        to_agent=task.assignee,
                        msg_type="task",
                        subject=f"Assigned: {task.title}",
                        body=(
                            f"Task {task.id}\n"
                            f"{task.description}\n"
                            f"Acceptance: {_format_criteria_inline(task.acceptance_criteria)}\n"
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
            cwd = self._turn_cwd.get(agent.state.name)
            missing = self._verify_files(files, cwd=cwd)
            if missing:
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

            # Auto-run criteria-attached verifiers for whichever task this
            # deliverable will close. Failures route through the same channel
            # as a verify-action failure so the agent gets reactivated.
            target_task = self._first_open_assigned_task(agent)
            if target_task is not None:
                failures = self._check_task_criteria(target_task, cwd=cwd)
                if failures:
                    self._reject_deliverable_for_criteria(agent, target_task, failures)
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
                    if agent.last_doc_versions:
                        task.satisfied_doc_versions = dict(agent.last_doc_versions)
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
            self._landed_deliverables += 1
            self._maybe_refresh_codebase_map()

        elif action_type == "write_doc":
            name = str(action.get("name", "")).strip()
            content = str(action.get("content", ""))
            if not name:
                self._emit("write_doc_invalid", {"agent": agent.state.name})
                return
            self._publish_doc(name=name, content=content, author=agent.state.name)

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

        elif action_type == "record_policy":
            text = str(action.get("text", "")).strip()
            if not text:
                self._emit(
                    "record_policy_invalid",
                    {"agent": agent.state.name, "reason": "empty text"},
                )
                return
            scope = str(action.get("scope", "global")).strip() or "global"
            before = len(self.world.policies)
            policy = self.world.add_policy(
                text=text,
                scope=scope,
                source=agent.state.name,
                turn=self._global_turns,
            )
            self._emit(
                "policy_recorded",
                {
                    "agent": agent.state.name,
                    "id": policy.id,
                    "text": policy.text,
                    "scope": policy.scope,
                    "source": policy.source,
                    "deduped": len(self.world.policies) == before,
                },
            )

        elif action_type == "retire_policy":
            policy_id = str(action.get("policy_id", "")).strip()
            target: Optional[Policy] = None
            for p in self.world.policies:
                if p.id == policy_id:
                    target = p
                    break
            if target is None or not target.active:
                self._emit(
                    "retire_policy_invalid",
                    {
                        "agent": agent.state.name,
                        "policy_id": policy_id,
                        "reason": "unknown or already retired",
                    },
                )
                return
            target.active = False
            self._emit(
                "policy_retired",
                {
                    "agent": agent.state.name,
                    "id": target.id,
                    "scope": target.scope,
                    "text": target.text,
                },
            )

        elif action_type == "verify":
            self._dispatch_verify(agent, action)

        elif action_type == "check_criterion":
            self._dispatch_check_criterion(agent, action)

        else:
            self._emit("unknown_action", {"agent": agent.state.name, "type": action_type})

    def _dispatch_verify(self, agent: Agent, action: dict[str, Any]) -> None:
        """Run the named verifier against the agent's worktree. Running against
        the worktree (not the shared workspace) is the whole point of per-agent
        isolation — we see what the agent just produced before it merges.
        Failures surface as blocker messages (same channel as a rejected
        deliverable) and flip the per-turn rejection bit so transcripts mark
        the turn as not accepted."""
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

        ws_root = self._turn_cwd.get(agent.state.name) or Path(
            self.world.workspace.code_dir
        )
        try:
            result = verifier.run(spec, ws_root)
        except Exception as e:
            result = VerifierResult(
                ok=False,
                summary=f"verifier crashed: {e}",
                details={"error": str(e)},
            )

        if result.details.get("substituted_python3"):
            self._emit(
                "verifier_substituted_python3",
                {
                    "agent": agent.state.name,
                    "verifier": verifier_name,
                    "from": result.details.get("substituted_from"),
                    "to": result.details.get("substituted_to"),
                },
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

    def _maybe_refresh_codebase_map(self) -> None:
        """In brownfield mode, periodically re-run the codebase scan so the
        shared map doesn't decay as agents restructure the project. No-op in
        greenfield (no existing project to map). put_doc dedups by hash, so
        a no-change scan doesn't pile up duplicate versions in shared_docs."""
        if self.world.workspace is None or not self.world.workspace.brownfield:
            return
        if CODEBASE_REFRESH_EVERY_DELIVERABLES <= 0:
            return
        if self._landed_deliverables % CODEBASE_REFRESH_EVERY_DELIVERABLES != 0:
            return
        try:
            self.refresh_codebase_map()
        except Exception as e:
            self._emit("codebase_refresh_error", {"error": str(e)})

    # ---- shared-doc publishing ------------------------------------------

    def _publish_doc(
        self,
        *,
        name: str,
        content: str,
        author: str,
        persist_to_disk: bool = True,
    ) -> DocVersion:
        """Single funnel for every write into world.shared_docs.

        Versions via WorldState.put_doc (hash-dedups), mirrors the latest
        content to <workspace>/shared/<name> for tools that Read the file
        directly, and emits a `doc_updated` event so the TUI / transcript
        can show a unified diff of what changed."""
        prev = self.world.get_doc_version(name)
        version = self.world.put_doc(
            name=name, content=content, author=author, turn=self._global_turns
        )
        if version is prev:
            # No-op republish — dedup'd, nothing to broadcast.
            return version
        if persist_to_disk and self.world.workspace is not None:
            try:
                path = Path(self.world.workspace.shared_dir) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            except OSError as e:
                self._emit("doc_write_disk_error", {"name": name, "error": str(e)})
        self._emit(
            "doc_updated",
            {
                "name": name,
                "new_hash": version.hash,
                "prev_hash": prev.hash if prev else None,
                "author": author,
                "turn": version.turn,
                "diff_preview": _doc_diff_preview(
                    prev.content if prev else None, content
                ),
            },
        )
        return version

    # ---- criterion checking ---------------------------------------------

    def _first_open_assigned_task(self, agent: Agent) -> Optional[Task]:
        for tid in agent.state.assigned_tasks:
            task = self.world.tasks.get(tid)
            if task and task.status != "complete":
                return task
        return None

    def _run_criterion(
        self,
        criterion: AcceptanceCriterion,
        cwd: Optional[Path] = None,
    ) -> Optional[VerifierResult]:
        """Run a criterion's verifier (if any) and write the outcome back
        onto the criterion. Returns the result, or None if no verifier.

        `cwd` is the agent's worktree when invoked from the deliverable path;
        for ad-hoc `check_criterion` actions outside an active turn we fall
        back to the shared workspace."""
        if not criterion.verifier:
            return None
        verifier = VERIFIERS.get(criterion.verifier)
        if verifier is None:
            criterion.last_status = "failed"
            criterion.last_summary = f"unknown verifier {criterion.verifier!r}"
            criterion.last_checked_turn = self._global_turns
            return VerifierResult(ok=False, summary=criterion.last_summary)
        if self.world.workspace is None:
            return None
        ws_root = cwd or Path(self.world.workspace.code_dir)
        spec = dict(criterion.spec or {})
        try:
            result = verifier.run(spec, ws_root)
        except Exception as e:
            result = VerifierResult(
                ok=False, summary=f"verifier crashed: {e}", details={"error": str(e)}
            )
        if result.details.get("substituted_python3"):
            self._emit(
                "verifier_substituted_python3",
                {
                    "criterion": criterion.text,
                    "verifier": criterion.verifier,
                    "from": result.details.get("substituted_from"),
                    "to": result.details.get("substituted_to"),
                },
            )
        criterion.last_status = "passed" if result.ok else "failed"
        criterion.last_summary = result.summary
        criterion.last_checked_turn = self._global_turns
        return result

    def _check_task_criteria(
        self,
        task: Task,
        cwd: Optional[Path] = None,
    ) -> list[tuple[int, AcceptanceCriterion, VerifierResult]]:
        """Run every criterion-attached verifier for `task`. Returns the
        failing (index, criterion, result) tuples — empty list means all
        passed (or there were no verifiers attached). `cwd` is the agent's
        worktree so checks see the pre-merge state."""
        failures: list[tuple[int, AcceptanceCriterion, VerifierResult]] = []
        for idx, criterion in enumerate(task.acceptance_criteria):
            if not criterion.verifier:
                continue
            result = self._run_criterion(criterion, cwd=cwd)
            if result is None:
                continue
            payload = {
                "task_id": task.id,
                "criterion_index": idx,
                "text": criterion.text,
                "verifier": criterion.verifier,
                "ok": result.ok,
                "summary": result.summary,
            }
            if result.ok:
                self._emit("criterion_passed", payload)
            else:
                self._emit(
                    "criterion_failed",
                    {**payload, "details": result.details},
                )
                failures.append((idx, criterion, result))
        return failures

    def _reject_deliverable_for_criteria(
        self,
        agent: Agent,
        task: Task,
        failures: list[tuple[int, AcceptanceCriterion, VerifierResult]],
    ) -> None:
        self._rejected_this_turn.add(agent.state.name)
        summary_lines = [
            f"  - [{idx}] {c.text}: {r.summary}" for idx, c, r in failures
        ]
        agent.state.notes.append(
            f"deliverable rejected — {len(failures)} criterion(s) failed for {task.id}"
        )
        self.bus.deliver(
            Message(
                from_agent="orchestrator",
                to_agent=agent.state.name,
                msg_type="blocker",
                subject=f"Deliverable rejected — {len(failures)} acceptance criteria failed",
                body=(
                    f"Task {task.id} ({task.title}) cannot complete: "
                    f"the following acceptance criteria have verifiers that failed.\n"
                    + "\n".join(summary_lines)
                    + "\n\nFix the work and emit a fresh DELIVERABLE."
                ),
                references=[task.id],
            )
        )
        self._emit(
            "deliverable_rejected",
            {
                "agent": agent.state.name,
                "task_id": task.id,
                "criterion_failures": [
                    {"index": idx, "text": c.text, "summary": r.summary}
                    for idx, c, r in failures
                ],
            },
        )

    def _dispatch_check_criterion(
        self, agent: Agent, action: dict[str, Any]
    ) -> None:
        task_id = str(action.get("task_id", "")).strip()
        task = self.world.tasks.get(task_id)
        if task is None:
            self._emit(
                "check_criterion_invalid",
                {"agent": agent.state.name, "reason": "unknown task_id", "task_id": task_id},
            )
            return
        try:
            idx = int(action.get("criterion_index"))
        except (TypeError, ValueError):
            self._emit(
                "check_criterion_invalid",
                {"agent": agent.state.name, "reason": "criterion_index must be int"},
            )
            return
        if idx < 0 or idx >= len(task.acceptance_criteria):
            self._emit(
                "check_criterion_invalid",
                {"agent": agent.state.name, "reason": "index out of range"},
            )
            return
        criterion = task.acceptance_criteria[idx]
        if not criterion.verifier:
            self._emit(
                "check_criterion_skipped",
                {
                    "agent": agent.state.name,
                    "task_id": task_id,
                    "criterion_index": idx,
                    "reason": "no verifier attached",
                },
            )
            return
        cwd = self._turn_cwd.get(agent.state.name)
        result = self._run_criterion(criterion, cwd=cwd)
        if result is None:
            return
        payload = {
            "agent": agent.state.name,
            "task_id": task_id,
            "criterion_index": idx,
            "verifier": criterion.verifier,
            "ok": result.ok,
            "summary": result.summary,
        }
        self._emit(
            "criterion_passed" if result.ok else "criterion_failed",
            payload if result.ok else {**payload, "details": result.details},
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
            if not self._turn_cap_announced:
                self._emit("stopped_on_turn_cap", {"turns": self._global_turns})
                self._turn_cap_announced = True
            return True
        if not self.agents:
            return False
        # Organizational completion: all agents complete and no open tasks.
        all_complete = all(a.state.status == "complete" for a in self.agents.values())
        no_open_tasks = all(
            t.status in ("complete", "cancelled") for t in self.world.tasks.values()
        )
        if not (all_complete and no_open_tasks):
            return False

        # Objective stop predicate. If any task has criteria with a verifier
        # attached, require all such criteria to have passed. If NO criterion
        # across all tasks has a verifier (legacy/narrative-only runs), fall
        # back to organizational completion alone — matches pre-Task-3 behaviour.
        any_verifier = False
        unmet: list[tuple[str, int, str, str]] = []
        for task in self.world.tasks.values():
            for idx, c in enumerate(task.acceptance_criteria):
                if not c.verifier:
                    continue
                any_verifier = True
                if c.last_status != "passed":
                    unmet.append((task.id, idx, c.text, c.last_status))
        if not any_verifier:
            self._announce_completion(objective=False)
            return True
        if unmet:
            self._emit(
                "completion_blocked_by_criteria",
                {
                    "unmet": [
                        {"task_id": t, "index": i, "text": txt, "status": st}
                        for t, i, txt, st in unmet
                    ]
                },
            )
            return False
        self._announce_completion(objective=True)
        return True

    def _announce_completion(self, *, objective: bool) -> None:
        if self._completion_announced:
            return
        self._completion_announced = True
        self._emit(
            "stopped_on_completion",
            {
                "objective": objective,
                "turns": self._global_turns,
                "tasks": len(self.world.tasks),
            },
        )

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


def _doc_diff_preview(prev: Optional[str], new: str) -> str:
    """Short, human-readable diff preview for the `doc_updated` event.

    For an entirely new doc, return the first NEW_DOC_PREVIEW_LINES lines.
    Otherwise, a unified diff truncated to DIFF_PREVIEW_LINES lines."""
    if prev is None:
        head = new.splitlines()[:NEW_DOC_PREVIEW_LINES]
        return "\n".join(head)
    diff = difflib.unified_diff(
        prev.splitlines(),
        new.splitlines(),
        fromfile="prev",
        tofile="new",
        lineterm="",
        n=2,
    )
    out: list[str] = []
    for i, line in enumerate(diff):
        if i >= DIFF_PREVIEW_LINES:
            out.append(f"…[diff truncated after {DIFF_PREVIEW_LINES} lines]")
            break
        out.append(line)
    return "\n".join(out)


def _format_criteria_inline(criteria: list[AcceptanceCriterion]) -> str:
    if not criteria:
        return "(none)"
    parts: list[str] = []
    for c in criteria:
        if c.verifier:
            parts.append(f"{c.text} [verifier={c.verifier}]")
        else:
            parts.append(c.text)
    return "; ".join(parts)


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
    last_error_at = d.get("last_error_at_turn")
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
        consecutive_errors=int(d.get("consecutive_errors") or 0),
        last_error_at_turn=int(last_error_at) if last_error_at is not None else None,
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
