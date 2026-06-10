"""Message bus — routes messages between agents and into the audit log.

The bus is intentionally simple: in-memory, single-process. Delivery is
synchronous and single-threaded: the orchestrator calls `deliver` only while
applying actions on its main thread (inference workers are pure and never
touch WorldState). Concurrency is handled at the agent-turn level, not here,
so no locking is required. `world.messages` is the canonical audit log; per
agent we keep only the unread `inbox` (consumed each turn).

Two routing rules with org-level consequences live here:

- Intervention messages (blocker / directive / escalation / answer) clear the
  recipient's error-backoff stamp and any verify-loop hold, granting one
  immediate retry — a manager's correction must actually reach a backoff'd
  agent instead of rotting in the inbox while the window plays out. The
  consecutive-error count is preserved, so a failed retry resumes backoff at
  the higher count rather than hot-looping.

- `broadcast` from an agent fans out to its team only (manager + peers +
  direct reports), not the whole org. At hundreds of agents a global
  broadcast would make every agent dispatch-ready next tick — a stampede.
  Broadcasts from the user/orchestrator stay global.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mau_cli.schemas import AgentState, Message, WorldState

# Message types that represent someone actively intervening on the recipient
# (as opposed to passive status/deliverable traffic).
INTERVENTION_MSG_TYPES = ("blocker", "directive", "escalation", "answer")


class MessageBus:
    def __init__(
        self,
        world: WorldState,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ):
        self.world = world
        self.on_event = on_event or (lambda *_: None)

    def deliver(self, message: Message) -> None:
        """Route a message to its target agent's inbox (and the audit log)."""
        self.world.messages.append(message)
        self.world.log.append(message.short())

        if message.to_agent == "broadcast":
            for agent in self._broadcast_targets(message.from_agent):
                agent.inbox.append(message)
                self._clear_backoff(agent, message)
            return

        if message.to_agent == "user":
            self.world.pending_user_questions.append(message)
            return

        target = self.world.agents.get(message.to_agent)
        if target is None:
            # Unknown recipient — log it and drop. Agents will see the error
            # via a missing reply; supervisor escalation kicks in eventually.
            self.world.log.append(
                f"!! undeliverable: {message.short()} (no agent named {message.to_agent})"
            )
            return

        target.inbox.append(message)
        self._clear_backoff(target, message)

    def _broadcast_targets(self, sender_name: str) -> list[AgentState]:
        """Team-scoped fan-out: manager + peers (same manager) + direct
        reports of the sender. Non-agent senders (user, orchestrator) keep
        the legacy org-wide fan-out."""
        sender = self.world.agents.get(sender_name)
        if sender is None:
            return [
                a for a in self.world.agents.values() if a.name != sender_name
            ]
        team: dict[str, AgentState] = {}
        for a in self.world.agents.values():
            if a.name == sender_name:
                continue
            if (
                a.name == sender.manager  # my manager
                or a.manager == sender_name  # my reports
                or (sender.manager is not None and a.manager == sender.manager)  # peers
            ):
                team[a.name] = a
        return list(team.values())

    def _clear_backoff(self, target: AgentState, message: Message) -> None:
        if message.msg_type not in INTERVENTION_MSG_TYPES:
            return
        cleared = False
        if target.last_error_at_turn is not None:
            target.last_error_at_turn = None  # keep consecutive_errors
            cleared = True
        if target.hold_until_tick is not None:
            target.hold_until_tick = None
            cleared = True
        if cleared:
            self.on_event(
                "backoff_cleared_by_message",
                {
                    "agent": target.name,
                    "msg_type": message.msg_type,
                    "from": message.from_agent,
                },
            )
