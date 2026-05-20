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
from importlib import resources

from mau_cli.inference import InferenceBackend, InferenceResult
from mau_cli.schemas import (
    AgentState,
    AgentTurn,
    CODE_GEN_ROLES,
    Role,
    TokenUsage,
    WorldState,
)


def load_role_prompt(role: Role) -> str:
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
        # to learn the contract; planners write them to publish it.
        if world.shared_docs:
            lines.append("SHARED_DOCS:")
            for name, content in world.shared_docs.items():
                lines.append(f"  --- {name} ---")
                # Truncate generously; specialists need full content.
                if len(content) > 4000:
                    lines.append(content[:4000] + "\n…[truncated]")
                else:
                    lines.append(content)
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
                        lines.append(f"      ✓ {ac}")
            lines.append("")

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

    def run_turn(self, world: WorldState) -> AgentTurn:
        prompt = self.build_user_prompt(world)
        if self.is_code_gen and world.workspace is not None:
            result = self._run_agentic(world, prompt)
        else:
            result = self.backend.call_plan(self.system_prompt, prompt)
            self.state.usage.add(result.usage)
            world.usage.add(result.usage)

        self.last_prompt = prompt
        self.last_result = result

        turn = self._result_to_turn(result)
        # Track files touched on the agent.
        for f in result.files_touched:
            if f not in self.state.files_touched:
                self.state.files_touched.append(f)

        self.state.inbox.clear()
        self.state.turns_taken += 1
        return turn

    def _run_agentic(self, world: WorldState, prompt: str) -> InferenceResult:
        ws = world.workspace
        assert ws is not None
        result = self.backend.call_agentic(
            system_prompt=self.system_prompt,
            user_prompt=prompt,
            workspace_dir=ws.code_dir,
            extra_dirs=[ws.shared_dir],
        )
        self.state.usage.add(result.usage)
        world.usage.add(result.usage)
        return result

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

        return AgentTurn(
            thoughts=str(deliverable.get("summary", ""))[:500],
            status="complete",
            actions=[
                {
                    "type": "deliverable",
                    "title": deliverable.get("title", "Deliverable"),
                    "summary": deliverable.get("summary", ""),
                    "files_touched": result.files_touched,
                },
                {"type": "complete", "summary": deliverable.get("summary", "")},
            ],
        )
