"""Message bus — routes messages between agents and into the audit log.

The bus is intentionally simple: in-memory, single-process. Delivery is
synchronous from the orchestrator's perspective (the orchestrator calls
`deliver` while applying actions). Concurrency is handled at the agent-turn
level, not at the message level.
"""

from __future__ import annotations

import threading

from mau_cli.schemas import Message, WorldState


class MessageBus:
    def __init__(self, world: WorldState):
        self.world = world
        self._lock = threading.Lock()

    def deliver(self, message: Message) -> None:
        """Route a message to its target agent's inbox (and history)."""
        with self._lock:
            self.world.messages.append(message)
            self.world.log.append(message.short())

            if message.to_agent == "broadcast":
                for agent in self.world.agents.values():
                    if agent.name != message.from_agent:
                        agent.inbox.append(message)
                        agent.history.append(message)
                return

            if message.to_agent == "user":
                self.world.pending_user_questions.append(message)
                return

            target = self.world.agents.get(message.to_agent)
            if target is None:
                # Unknown recipient — log it and drop. Agents will see error
                # via missing reply, supervisor escalation kicks in eventually.
                self.world.log.append(
                    f"!! undeliverable: {message.short()} (no agent named {message.to_agent})"
                )
                return

            target.inbox.append(message)
            target.history.append(message)

            # Record in sender's history too, if they exist.
            sender = self.world.agents.get(message.from_agent)
            if sender is not None:
                sender.history.append(message)
