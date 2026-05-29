"""Message bus — routes messages between agents and into the audit log.

The bus is intentionally simple: in-memory, single-process. Delivery is
synchronous and single-threaded: the orchestrator calls `deliver` only while
applying actions on its main thread (inference workers are pure and never
touch WorldState). Concurrency is handled at the agent-turn level, not here,
so no locking is required. `world.messages` is the canonical audit log; per
agent we keep only the unread `inbox` (consumed each turn).
"""

from __future__ import annotations

from mau_cli.schemas import Message, WorldState


class MessageBus:
    def __init__(self, world: WorldState):
        self.world = world

    def deliver(self, message: Message) -> None:
        """Route a message to its target agent's inbox (and the audit log)."""
        self.world.messages.append(message)
        self.world.log.append(message.short())

        if message.to_agent == "broadcast":
            for agent in self.world.agents.values():
                if agent.name != message.from_agent:
                    agent.inbox.append(message)
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
