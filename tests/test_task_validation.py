"""Regression: create_task validates its assignee.

A task assigned to a non-existent agent used to be recorded anyway. It would
never be worked and never complete, silently wedging the run's completion
predicate (no_open_tasks stays False forever). The orchestrator now rejects it
and routes a blocker back to the creator.
"""

from __future__ import annotations

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import Role


def _orch(tmp_workspace):
    events: list[tuple[str, dict]] = []
    orch = Orchestrator(
        backend=MockBackend(),
        workspace=tmp_workspace,
        isolation="shared",
        on_event=lambda k, p: events.append((k, p)),
    )
    return orch, events


def test_create_task_unknown_assignee_is_rejected(tmp_workspace):
    orch, events = _orch(tmp_workspace)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    before = len(orch.world.tasks)

    orch._apply_action(
        tl, {"type": "create_task", "title": "orphan", "assignee": "ghost"}
    )

    assert len(orch.world.tasks) == before, "task with unknown assignee must not be recorded"
    assert any(k == "create_task_invalid" for k, _ in events)
    # The creator is told why, via a blocker, so it can spawn/rename and retry.
    assert any(
        m.msg_type == "blocker" and m.to_agent == "tl-1"
        for m in orch.world.messages
    )


def test_create_task_known_assignee_succeeds(tmp_workspace):
    orch, events = _orch(tmp_workspace)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")

    orch._apply_action(
        tl,
        {"type": "create_task", "id": "task_x", "title": "real", "assignee": "be-1"},
    )

    assert "task_x" in orch.world.tasks
    assert "task_x" in be.state.assigned_tasks
    assert any(k == "task_created" for k, _ in events)


def test_create_task_dangling_dependency_warns_but_creates(tmp_workspace):
    """Forward references are legal (the dep may be created later), so a dangling
    dep emits an observability breadcrumb rather than rejecting the task."""
    orch, events = _orch(tmp_workspace)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")

    orch._apply_action(
        tl,
        {
            "type": "create_task",
            "id": "task_y",
            "title": "depends on nothing real",
            "assignee": "be-1",
            "depends_on": ["task_does_not_exist"],
        },
    )

    assert "task_y" in orch.world.tasks, "forward-ref dep must not block creation"
    warnings = [p for k, p in events if k == "task_dependency_unknown"]
    assert warnings and "task_does_not_exist" in warnings[0]["unknown"]
