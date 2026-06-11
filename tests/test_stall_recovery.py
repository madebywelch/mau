"""Stall recovery: interventions clear error backoff, unattended escalations
never wedge the run, and identical verify failures escalate to the manager
instead of looping the same retry until the turn cap.
"""

from __future__ import annotations

from typing import Any

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import (
    VERIFY_LOOP_ESCALATE_AT,
    Orchestrator,
)
from mau_cli.schemas import AgentTurn, Message, Role, Task


def _orch(tmp_workspace, events, **kw):
    return Orchestrator(
        backend=MockBackend(),
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
        **kw,
    )


# ---- interventions clear error backoff -----------------------------------------


def test_intervention_message_clears_backoff(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-test", "")
    tl.state.consecutive_errors = 2
    tl.state.last_error_at_turn = 5
    orch._tick_count = 5

    assert "tl-test" not in [a.state.name for a in orch._ready_agents()]

    orch.bus.deliver(
        Message(from_agent="em-1", to_agent="tl-test", msg_type="blocker",
                subject="try again", body="…")
    )
    # One immediate retry granted; the error count survives so a failed
    # retry resumes backoff at the higher count.
    assert tl.state.last_error_at_turn is None
    assert tl.state.consecutive_errors == 2
    assert "tl-test" in [a.state.name for a in orch._ready_agents()]
    assert [p for k, p in events if k == "backoff_cleared_by_message"]


def test_status_message_does_not_clear_backoff(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-test", "")
    tl.state.consecutive_errors = 2
    tl.state.last_error_at_turn = 5
    orch._tick_count = 5

    orch.bus.deliver(
        Message(from_agent="em-1", to_agent="tl-test", msg_type="status",
                subject="fyi", body="…")
    )
    assert tl.state.last_error_at_turn == 5
    assert "tl-test" not in [a.state.name for a in orch._ready_agents()]


# ---- unattended escalation handling --------------------------------------------


def test_unattended_no_manager_escalation_parks_and_self_directs(
    tmp_workspace, event_recorder
):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events, unattended=True)
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")  # no manager edge

    orch._escalate(be, "which auth provider?")
    assert orch.world.pending_user_questions  # parked for post-run review
    assert [p for k, p in events if k == "escalation_unresolvable"]
    directives = [
        m for m in be.state.inbox
        if m.msg_type == "directive" and "decide and proceed" in m.subject.lower()
    ]
    assert directives, "self-directive not delivered"
    assert be.state.unanswered_escalations == 1
    assert be.state.status != "complete"

    # Second unresolvable escalation: give up on the agent so the org converges.
    orch._escalate(be, "still unsure")
    assert be.state.status == "complete"
    given_up = [p for k, p in events if k == "agent_given_up"]
    assert given_up and given_up[-1]["reason"] == "unresolvable_escalation"


def test_attended_no_manager_escalation_just_parks(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events, unattended=False)
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")
    orch._escalate(be, "which auth provider?")
    orch._escalate(be, "and which database?")
    assert len(orch.world.pending_user_questions) == 2
    assert be.state.unanswered_escalations == 0
    assert be.state.status != "complete"


# ---- verify-loop bounding: criterion path --------------------------------------


def _squad_with_failing_task(orch: Orchestrator) -> tuple[Any, Task]:
    orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    be = orch._spawn_agent(Role.BACKEND, "be-1", "", manager="tl-1")
    task = Task(
        id="t1",
        title="ship file",
        assignee="be-1",
        creator="tl-1",
        acceptance_criteria=[
            {
                "text": "the file exists",
                "verifier": "path_exists",
                "spec": {"paths": ["never/created.txt"]},
            }
        ],
    )
    orch.world.tasks["t1"] = task
    be.state.assigned_tasks.append("t1")
    return be, task


def _deliver_turn() -> AgentTurn:
    return AgentTurn(
        status="complete",
        actions=[
            {"type": "deliverable", "title": "claim", "summary": "done",
             "files_touched": []},
            {"type": "complete", "summary": "done"},
        ],
    )


def test_identical_criterion_failures_escalate_instead_of_looping(
    tmp_workspace, event_recorder
):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    be, task = _squad_with_failing_task(orch)

    for _ in range(VERIFY_LOOP_ESCALATE_AT - 1):
        orch._apply_turn(be, _deliver_turn())
    # Pre-threshold: normal retry blockers to the agent.
    agent_blockers = [m for m in be.state.inbox if m.msg_type == "blocker"]
    assert len(agent_blockers) == VERIFY_LOOP_ESCALATE_AT - 1
    assert not [p for k, p in events if k == "verify_loop_escalated"]

    orch._apply_turn(be, _deliver_turn())  # third identical failure
    escalated = [p for k, p in events if k == "verify_loop_escalated"]
    assert escalated and escalated[0]["count"] == VERIFY_LOOP_ESCALATE_AT
    # No NEW retry blocker to the agent — the loop is stopped...
    assert len([m for m in be.state.inbox if m.msg_type == "blocker"]) == (
        VERIFY_LOOP_ESCALATE_AT - 1
    )
    # ...the manager is told instead, with the verbatim verifier output.
    tl_blockers = [
        m for m in orch.world.agents["tl-1"].inbox if m.msg_type == "blocker"
    ]
    assert tl_blockers and "verify loop" in tl_blockers[-1].subject
    # The agent is held a few evaluations so the manager can intervene.
    assert be.state.hold_until_tick is not None
    assert "be-1" not in [a.state.name for a in orch._ready_agents()]
    # Streak resets — intervention gets a fresh failure budget.
    assert task.acceptance_criteria[0].consecutive_identical_failures == 0
    # Task is NOT cancelled (a live manager can still redirect).
    assert task.status != "cancelled"


def test_different_failure_summary_resets_streak(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    be, task = _squad_with_failing_task(orch)

    orch._apply_turn(be, _deliver_turn())
    orch._apply_turn(be, _deliver_turn())
    assert task.acceptance_criteria[0].consecutive_identical_failures == 2
    # The failure mode changes (e.g. the agent half-fixed it) → fresh streak.
    task.acceptance_criteria[0].last_summary = "a different failure"
    orch._apply_turn(be, _deliver_turn())
    assert task.acceptance_criteria[0].consecutive_identical_failures == 1
    assert not [p for k, p in events if k == "verify_loop_escalated"]


def test_unattended_verify_loop_with_no_manager_cancels_task(
    tmp_workspace, event_recorder
):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events, unattended=True)
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")  # no manager
    task = Task(
        id="t1",
        title="ship file",
        assignee="be-1",
        creator="be-1",
        acceptance_criteria=[
            {
                "text": "the file exists",
                "verifier": "path_exists",
                "spec": {"paths": ["never/created.txt"]},
            }
        ],
    )
    orch.world.tasks["t1"] = task
    be.state.assigned_tasks.append("t1")

    for _ in range(VERIFY_LOOP_ESCALATE_AT):
        orch._apply_turn(be, _deliver_turn())

    assert task.status == "cancelled"
    abandoned = [p for k, p in events if k == "task_abandoned"]
    assert abandoned and abandoned[0]["task_id"] == "t1"
    # A cancelled task's criteria are moot — they must not block completion.
    be.state.status = "complete"
    assert orch._is_done()


# ---- verify-loop bounding: ad-hoc verify actions --------------------------------


def test_identical_adhoc_verify_failures_escalate(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    be = orch._spawn_agent(Role.BACKEND, "be-1", "", manager="tl-1")

    verify_turn = AgentTurn(
        status="working",
        actions=[
            {"type": "verify", "verifier": "path_exists",
             "spec": {"paths": ["never/created.txt"]}}
        ],
    )
    for _ in range(VERIFY_LOOP_ESCALATE_AT - 1):
        orch._apply_turn(be, verify_turn)
    assert not [p for k, p in events if k == "verify_loop_escalated"]

    orch._apply_turn(be, verify_turn)
    escalated = [p for k, p in events if k == "verify_loop_escalated"]
    assert escalated and "verify action" in escalated[0]["source"]
    # Streak reset after escalation.
    assert all(v == 0 for v in be.state.verify_failures.values())


def test_hold_expires_with_evaluations(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")
    be.state.hold_until_tick = orch._tick_count + 2

    assert "be-1" not in [a.state.name for a in orch._ready_agents()]
    orch._tick_count += 2
    assert "be-1" in [a.state.name for a in orch._ready_agents()]
    assert be.state.hold_until_tick is None  # cleared on expiry


def test_intervention_clears_hold(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")
    be.state.hold_until_tick = orch._tick_count + 99

    orch.bus.deliver(
        Message(from_agent="tl-1", to_agent="be-1", msg_type="directive",
                subject="re-aim", body="…")
    )
    assert be.state.hold_until_tick is None
    assert "be-1" in [a.state.name for a in orch._ready_agents()]
