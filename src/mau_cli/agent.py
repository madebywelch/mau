"""Agent — composes a per-turn prompt from its state, calls the inference
backend in the appropriate mode (plan vs agentic), and returns the parsed
AgentTurn for the orchestrator to apply.

Planning roles (Product, EM, Tech Lead) use `call_plan` — strict JSON only.
Code-gen roles (Frontend, Backend, Database, QA, DevOps) use `call_agentic`
— full tool access inside the workspace, with shared docs added as readable
context. They write actual files; we extract a structured DELIVERABLE block
from the final assistant text.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Optional

from mau_cli.inference import InferenceBackend, InferenceResult
from mau_cli.schemas import (
    AgentState,
    AgentTurn,
    CODE_GEN_ROLES,
    Role,
    TokenUsage,
    WorldState,
)


# Per-doc cap in characters when injecting shared docs into a prompt.
# Set generously (~60k chars ≈ ~15k tokens) so agents see the full text the
# overwhelming majority of the time, while still bounding catastrophic-size
# blowups. When a doc exceeds this, the prompt prepends a clear marker so
# the agent knows it's looking at a truncated copy.
SHARED_DOC_HARD_CAP = 60_000


@lru_cache(maxsize=None)
def load_role_prompt(role: Role) -> str:
    # Prompt files are immutable for the life of the process, so cache the
    # composed text per role — otherwise every Agent construction (including
    # re-hydrating N agents on --resume) re-reads protocol.md + <role>.md.
    pkg = "mau_cli.prompts"
    protocol = resources.files(pkg).joinpath("_protocol.md").read_text(encoding="utf-8")
    role_text = resources.files(pkg).joinpath(f"{role.value}.md").read_text(encoding="utf-8")
    return f"{protocol}\n\n---\n\n{role_text}"


class Agent:
    def __init__(self, state: AgentState, backend: InferenceBackend):
        self.state = state
        self.backend = backend
        self.system_prompt = load_role_prompt(state.role)
        self.is_code_gen = state.role in CODE_GEN_ROLES
        # Transient, set by run_turn so the orchestrator can persist the
        # prompt+response pair without changing AgentTurn's shape.
        self.last_prompt: str = ""
        self.last_result: InferenceResult | None = None
        # name → hash of the doc version this agent saw in its last prompt.
        # Snapshotted onto Tasks when this agent's deliverable lands, so
        # later analysis can see which version of each contract the
        # deliverable was satisfied against.
        self.last_doc_versions: dict[str, str] = {}
        # Message IDs rendered into the most recent prompt. `finalize_turn`
        # consumes exactly these from the inbox, so a message delivered while
        # the turn was in flight (after the prompt was built) survives to the
        # next turn instead of being silently cleared.
        self._consumed_msg_ids: set[str] = set()

    # ---- prompt construction --------------------------------------------

    def build_user_prompt(self, world: WorldState) -> str:
        s = self.state
        lines: list[str] = []
        lines.append(f"AGENT_NAME: {s.name}")
        lines.append(f"ROLE: {s.role.value}")
        if s.specialization:
            lines.append(f"SPECIALIZATION: {s.specialization}")
        lines.append(f"TURN: {s.turns_taken}")
        lines.append(f"STATUS: {s.status}")
        lines.append("")

        lines.append("ORIGINAL_USER_REQUEST:")
        lines.append(world.request)
        lines.append("")

        if world.workspace and self.is_code_gen:
            if world.workspace.brownfield:
                lines.append(f"WORKSPACE: {world.workspace.code_dir} (existing codebase)")
                lines.append(
                    "  This is a real project, not an empty sandbox. Before "
                    "writing, `Glob` and `Read` related files to learn the "
                    "conventions. Match the existing style (formatting, "
                    "imports, folder layout, test patterns). Reuse existing "
                    "utilities and components instead of duplicating. "
                    "Don't touch `.mau/`. Don't run destructive Bash commands "
                    "(no `rm -rf`, no `git reset --hard`, no `git push`)."
                )
            else:
                lines.append(f"WORKSPACE: {world.workspace.code_dir}")
                lines.append(
                    "  Your CWD is the workspace. Read/Write/Edit files here freely. "
                    "Use Bash for things like `mkdir`, `npm install`, `pytest`, etc. "
                    "Don't reach outside the workspace."
                )
            lines.append("")

        lines.append("TEAM_ROSTER:")
        for agent in world.agents.values():
            spec = f" ({agent.specialization})" if agent.specialization else ""
            lines.append(f"  - {agent.name} [{agent.role.value}{spec}] — {agent.status}")
        lines.append("")

        # Shared docs are *the* coordination artifact. Specialists read them
        # to learn the contract; planners write them to publish it. We inject
        # the full latest content plus the version hash so every agent sees
        # the same mental copy and stale-version blunders are detectable.
        self.last_doc_versions = {}
        if world.shared_docs:
            lines.append("SHARED_DOCS:")
            for name in world.shared_docs:
                version = world.get_doc_version(name)
                if version is None:
                    continue
                self.last_doc_versions[name] = version.hash
                header = (
                    f"  --- {name} "
                    f"[version={version.hash} author={version.author} "
                    f"turn={version.turn}] ---"
                )
                lines.append(header)
                content = version.content
                if len(content) > SHARED_DOC_HARD_CAP:
                    lines.append(
                        f"  [WARNING: doc exceeds {SHARED_DOC_HARD_CAP} chars and "
                        f"was truncated; version hash above identifies the full "
                        f"copy on disk at shared/{name}]"
                    )
                    lines.append(content[:SHARED_DOC_HARD_CAP] + "\n…[truncated]")
                else:
                    lines.append(content)
            lines.append("")

        # Durable policies — human-approval rules captured via record_policy
        # (or the --policy CLI flag). Re-rendered every turn so rules survive
        # across turns and resumes. Matched by scope: global always shows;
        # role:<role> shows when this agent's role matches; task:<id> shows
        # when this agent has that task open.
        my_open_tasks = {
            tid for tid in s.assigned_tasks
            if tid in world.tasks and world.tasks[tid].status != "complete"
        }
        scope_filters = [f"role:{s.role.value}"] + [f"task:{tid}" for tid in my_open_tasks]
        matching: list = list(world.active_policies())  # globals
        seen_ids = {p.id for p in matching}
        for scope in scope_filters:
            for p in world.active_policies(scope):
                if p.id not in seen_ids:
                    matching.append(p)
                    seen_ids.add(p.id)
        if matching:
            lines.append("### Active policies")
            lines.append(
                "  (Durable rules the user — or a teammate — recorded. They "
                "OVERRIDE your default judgment. If a policy conflicts with "
                "a task, flag it via send_message rather than violate it.)"
            )
            for p in matching:
                lines.append(
                    f"  - [{p.id}] (scope={p.scope}, source={p.source}) {p.text}"
                )
            lines.append("")

        my_tasks = [world.tasks[tid] for tid in s.assigned_tasks if tid in world.tasks]
        if my_tasks:
            lines.append("YOUR_TASKS:")
            for t in my_tasks:
                deps = ", ".join(t.depends_on) if t.depends_on else "none"
                lines.append(f"  - {t.id} [{t.status}] {t.title} (deps: {deps})")
                if t.description:
                    for d_line in t.description.splitlines():
                        lines.append(f"      {d_line}")
                if t.acceptance_criteria:
                    for ac in t.acceptance_criteria:
                        prefix = f"      ✓ {ac.text}"
                        if ac.verifier:
                            prefix += (
                                f" [verifier={ac.verifier}, status={ac.last_status}]"
                            )
                        lines.append(prefix)
            lines.append("")

        # Record exactly which messages this prompt shows; finalize_turn
        # consumes these (and only these) so concurrently-delivered messages
        # aren't dropped.
        self._consumed_msg_ids = {m.id for m in s.inbox}
        if s.inbox:
            lines.append("INBOX (unread, oldest first):")
            for msg in s.inbox:
                lines.append(f"  [{msg.msg_type}] from {msg.from_agent} — {msg.subject}")
                for body_line in msg.body.splitlines():
                    lines.append(f"      {body_line}")
            lines.append("")
        else:
            lines.append("INBOX: (empty)")
            lines.append("")

        relevant_deliverables: list[str] = []
        for tid in {dep for t in my_tasks for dep in t.depends_on}:
            dep_task = world.tasks.get(tid)
            if dep_task and dep_task.status == "complete" and dep_task.deliverable_summary:
                relevant_deliverables.append(
                    f"  - {tid} ({dep_task.title}) → {dep_task.deliverable_summary}"
                )
        if relevant_deliverables:
            lines.append("UPSTREAM_DELIVERABLES:")
            lines.extend(relevant_deliverables)
            lines.append("")

        if s.notes:
            lines.append("YOUR_NOTES:")
            for n in s.notes[-5:]:
                lines.append(f"  • {n}")
            lines.append("")

        if self.is_code_gen:
            lines.append(
                "INSTRUCTIONS:\n"
                "1. If your task has unmet dependencies (see YOUR_TASKS deps), do NOT do work.\n"
                "   Output a single line: <DELIVERABLE>{\"blocked\": true, \"reason\": \"...\"}</DELIVERABLE>\n"
                "2. Otherwise, implement the task by reading SHARED_DOCS and editing files in the workspace.\n"
                "   Be concrete and complete — this is not a sketch, real code goes on disk.\n"
                "3. Acceptance criteria must each be verifiably met by your changes.\n"
                "4. End your final message with EXACTLY one line:\n"
                "   <DELIVERABLE>{\"title\": \"...\", \"summary\": \"...\", \"files_touched\": [\"path/relative/to/workspace\", ...]}</DELIVERABLE>\n"
                "5. The summary will be shown to downstream teammates and to the user — make it informative."
            )
        else:
            lines.append(
                "Now produce your next turn as a single JSON object per the protocol. "
                "If you have nothing useful to do, respond with status=complete."
            )
        return "\n".join(lines)

    # ---- turn dispatch --------------------------------------------------

    def run_turn(
        self, world: WorldState, cwd: Optional[Path] = None
    ) -> AgentTurn:
        """Convenience wrapper: build → infer → finalize inline. Used by
        direct callers (tests, scripts). The orchestrator drives these three
        phases separately so the blocking `infer` runs off the main thread
        while prompt construction and state mutation stay single-threaded."""
        prompt = self.build_user_prompt(world)
        workspace_dir, extra_dirs = self.infer_dirs(world, cwd)
        result = self.infer(
            prompt,
            workspace_dir=workspace_dir,
            extra_dirs=extra_dirs,
            max_budget_usd=None,
        )
        return self.finalize_turn(world, prompt, result)

    def infer_dirs(
        self, world: WorldState, cwd: Optional[Path] = None
    ) -> tuple[Optional[str], Optional[list[str]]]:
        """Resolve (workspace_dir, extra_dirs) on the caller's thread. Planning
        roles get (None, None) — no tools, no cwd. Specialists get the per-agent
        worktree (or the shared workspace when no isolation context is given,
        e.g. backend tests) plus the shared-docs dir."""
        ws = world.workspace
        if self.is_code_gen and ws is not None:
            workspace_dir = str(cwd) if cwd is not None else ws.code_dir
            return workspace_dir, [ws.shared_dir]
        return None, None

    def infer(
        self,
        prompt: str,
        *,
        workspace_dir: Optional[str],
        extra_dirs: Optional[list[str]],
        max_budget_usd: Optional[float] = None,
    ) -> InferenceResult:
        """Pure inference — safe to run off the main thread. Touches no
        WorldState or agent state; it only shells out to the backend and
        returns the raw result. Planning roles (workspace_dir is None) use
        `call_plan`; specialists use the tool-enabled agentic path, capped at
        `max_budget_usd` so a single runaway turn can't blow the run budget."""
        if self.is_code_gen and workspace_dir is not None:
            return self.backend.call_agentic(
                system_prompt=self.system_prompt,
                user_prompt=prompt,
                workspace_dir=workspace_dir,
                extra_dirs=extra_dirs,
                max_budget_usd=max_budget_usd,
            )
        return self.backend.call_plan(self.system_prompt, prompt)

    def finalize_turn(
        self, world: WorldState, prompt: str, result: InferenceResult
    ) -> AgentTurn:
        """Main-thread bookkeeping after `infer` returns: record the tape,
        accumulate usage (single-writer, so no race on world.usage), fold in
        files touched, consume exactly the inbox messages shown in `prompt`,
        and advance the turn counter."""
        self.last_prompt = prompt
        self.last_result = result
        self.state.usage.add(result.usage)
        world.usage.add(result.usage)

        turn = self._result_to_turn(result)
        for f in result.files_touched:
            if f not in self.state.files_touched:
                self.state.files_touched.append(f)

        # Consume only the messages this turn actually saw; anything delivered
        # mid-flight stays queued for next turn.
        if self._consumed_msg_ids:
            self.state.inbox = [
                m for m in self.state.inbox if m.id not in self._consumed_msg_ids
            ]
            self._consumed_msg_ids = set()
        self.state.turns_taken += 1
        return turn

    # ---- result interpretation ------------------------------------------

    def _result_to_turn(self, result: InferenceResult) -> AgentTurn:
        """For planners, the parsed JSON IS the turn. For specialists, we
        synthesize an AgentTurn from the DELIVERABLE block (or block status)."""
        if not self.is_code_gen:
            return AgentTurn.from_dict(result.parsed)

        # Specialist: result.parsed is the DELIVERABLE (or empty if none).
        deliverable = result.parsed
        if deliverable.get("blocked"):
            return AgentTurn(
                thoughts=str(deliverable.get("reason", ""))[:500],
                status="blocked",
                actions=[],
            )

        if not deliverable:
            # Specialist produced no DELIVERABLE block — treat as a soft note.
            return AgentTurn(
                thoughts="No DELIVERABLE block; agent text saved as note.",
                status="working",
                actions=[
                    {"type": "note", "body": result.raw_text[-1000:]},
                ],
            )

        actions: list[dict[str, object]] = []
        # Optional: specialists can request verifiers as part of their
        # deliverable. They run BEFORE the deliverable so failures can
        # mark the turn rejected and short-circuit "complete".
        for v in deliverable.get("verify") or []:
            if not isinstance(v, dict):
                continue
            actions.append(
                {
                    "type": "verify",
                    "verifier": v.get("verifier", ""),
                    "spec": v.get("spec") or {},
                }
            )
        actions.extend(
            [
                {
                    "type": "deliverable",
                    "title": deliverable.get("title", "Deliverable"),
                    "summary": deliverable.get("summary", ""),
                    "files_touched": result.files_touched,
                },
                {"type": "complete", "summary": deliverable.get("summary", "")},
            ]
        )
        return AgentTurn(
            thoughts=str(deliverable.get("summary", ""))[:500],
            status="complete",
            actions=actions,
        )
