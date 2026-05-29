"""Regression: consecutive agent errors trigger backoff + escalation + give-up.

Pre-fix, the orchestrator re-dispatched a failing agent every tick with no
delay. A flaky inference call could pin the loop for 15+ minutes before
external intervention. The fix:

- Track AgentState.consecutive_errors / last_error_at_turn.
- _ready_agents skips the agent for min(consecutive_errors, BACKOFF) ticks.
- At ERROR_ESCALATE_AT we route a blocker to the supervisor.
- At ERROR_GIVEUP_AT we force-complete the agent so the run can converge.
"""

from __future__ import annotations

from typing import Any

import pytest

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import (
    ERROR_BACKOFF_TICKS,
    ERROR_ESCALATE_AT,
    ERROR_GIVEUP_AT,
    Orchestrator,
)
from mau_cli.schemas import AgentState, Role


def test_consecutive_errors_increment_and_backoff(tmp_workspace):
    """One failure → skipped for one tick. Two failures → skipped for two
    ticks. Counter increments per failure, resets on a successful turn."""
    events: list[tuple[str, dict[str, Any]]] = []
    backend = MockBackend(fail_first_n={"tl-1": 2})
    orch = Orchestrator(
        backend=backend,
        max_turns=80,
        concurrency=1,
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    error_events = [(k, p) for k, p in events if k == "agent_error"]
    # tl-1 should fail exactly twice (fail_first_n=2) then succeed.
    tl_errors = [p for k, p in error_events if p.get("agent") == "tl-1"]
    assert len(tl_errors) == 2, (
        f"expected 2 tl-1 errors, got {len(tl_errors)}: {tl_errors}"
    )
    # consecutive_errors should be visible in the emitted payload.
    assert tl_errors[0]["consecutive_errors"] == 1
    assert tl_errors[1]["consecutive_errors"] == 2

    # Run should still converge despite the flake.
    kinds = [k for k, _ in events]
    assert "stopped_on_completion" in kinds, (
        f"run did not converge despite recovery; events: {sorted(set(kinds))}"
    )


def test_escalation_blocker_routed_to_supervisor(tmp_workspace):
    """After ERROR_ESCALATE_AT consecutive failures, a blocker is delivered
    to the agent's supervisor (TL's supervisor is EM)."""
    events: list[tuple[str, dict[str, Any]]] = []
    # Fail tl-1 exactly at the escalation threshold so we hit the escalate
    # branch without crossing into give-up.
    backend = MockBackend(fail_first_n={"tl-1": ERROR_ESCALATE_AT})
    orch = Orchestrator(
        backend=backend,
        max_turns=120,
        concurrency=1,
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    escalations = [p for k, p in events if k == "agent_error_escalated"]
    assert len(escalations) >= 1, (
        f"expected an escalation event after {ERROR_ESCALATE_AT} errors; "
        f"got events: {sorted({k for k, _ in events})}"
    )
    esc = escalations[0]
    assert esc["agent"] == "tl-1"
    assert esc["consecutive_errors"] == ERROR_ESCALATE_AT
    assert esc["supervisor"] == "em-1"

    # A blocker message should land in em-1's inbox (or, once consumed, the
    # canonical audit log world.messages — per-agent history was removed as it
    # only duplicated that log).
    em = orch.agents.get("em-1")
    assert em is not None
    all_msgs = list(em.state.inbox) + [
        m for m in orch.world.messages if m.to_agent == "em-1"
    ]
    blockers = [
        m for m in all_msgs
        if m.msg_type == "blocker" and "tl-1" in m.subject
    ]
    assert blockers, (
        f"no blocker subject mentioning tl-1 found on em-1; got: "
        f"{[(m.msg_type, m.subject) for m in all_msgs]}"
    )


def test_give_up_force_completes_agent(tmp_workspace):
    """After ERROR_GIVEUP_AT consecutive failures, the agent is force-completed
    with an `agent_given_up` event and the run terminates rather than stalls."""
    events: list[tuple[str, dict[str, Any]]] = []
    # Give tl-1 a fail budget high enough to exceed the give-up threshold
    # (the orchestrator should stop calling tl-1 before they exhaust their
    # fail budget).
    backend = MockBackend(fail_first_n={"tl-1": ERROR_GIVEUP_AT + 5})
    orch = Orchestrator(
        backend=backend,
        max_turns=200,
        concurrency=1,
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    given_up = [p for k, p in events if k == "agent_given_up"]
    assert given_up, (
        f"expected agent_given_up event; got: {sorted({k for k, _ in events})}"
    )
    payload = given_up[0]
    assert payload["agent"] == "tl-1"
    assert payload["consecutive_errors"] == ERROR_GIVEUP_AT

    # tl-1 should be marked complete on the agent state.
    tl = orch.agents.get("tl-1")
    assert tl is not None
    assert tl.state.status == "complete"

    # And the run must terminate (no infinite loop).
    assert orch._global_turns <= 200, "run looped past max_turns despite give-up"


def test_ready_agents_respects_backoff_predicate(tmp_workspace):
    """Direct unit test of the backoff predicate: an agent with
    consecutive_errors=2 and last_error_at_turn=T (tick) is skipped on
    ticks T+0 and T+1 (two ticks) and eligible again on T+2."""
    from mau_cli.orchestrator import Orchestrator

    backend = MockBackend()
    orch = Orchestrator(
        backend=backend,
        max_turns=10,
        concurrency=1,
        workspace=tmp_workspace,
        isolation="shared",
    )
    orch._ensure_isolation()

    # Spawn a TL agent and give them an inbox message so they'd normally
    # be ready.
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-test", "")
    from mau_cli.schemas import Message
    tl.state.inbox.append(
        Message(from_agent="user", to_agent="tl-test", msg_type="directive",
                subject="x", body="y")
    )

    # Two failures at tick 5 → skip ticks where (cur_tick - 5) < min(2, 3) = 2.
    tl.state.consecutive_errors = 2
    tl.state.last_error_at_turn = 5

    orch._tick_count = 5
    assert "tl-test" not in [a.state.name for a in orch._ready_agents()]
    orch._tick_count = 6
    assert "tl-test" not in [a.state.name for a in orch._ready_agents()]
    orch._tick_count = 7
    assert "tl-test" in [a.state.name for a in orch._ready_agents()]


def test_successful_turn_clears_consecutive_errors(tmp_workspace):
    """A clean turn after a flaky one resets the counter so a later
    transient failure gets a fresh backoff budget."""
    backend = MockBackend(fail_first_n={"tl-1": 1})
    orch = Orchestrator(
        backend=backend,
        max_turns=80,
        concurrency=1,
        workspace=tmp_workspace,
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    tl = orch.agents.get("tl-1")
    assert tl is not None
    # Counter must be cleared after the successful turn that followed the
    # single flake.
    assert tl.state.consecutive_errors == 0
    assert tl.state.last_error_at_turn is None
