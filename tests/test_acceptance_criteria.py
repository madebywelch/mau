"""Task 3: Acceptance criteria become structured, with verifier-gated `_is_done`."""

from __future__ import annotations

from pathlib import Path

from mau_cli.schemas import (
    AcceptanceCriterion,
    AgentTurn,
    Role,
    Task,
)


# ---- coercion ---------------------------------------------------------------


def test_task_coerces_string_criteria():
    t = Task(acceptance_criteria=["foo", "bar"])
    assert all(isinstance(c, AcceptanceCriterion) for c in t.acceptance_criteria)
    assert [c.text for c in t.acceptance_criteria] == ["foo", "bar"]
    assert all(c.verifier is None for c in t.acceptance_criteria)


def test_task_keeps_structured_criteria():
    t = Task(
        acceptance_criteria=[
            {
                "text": "foo",
                "verifier": "path_exists",
                "spec": {"paths": ["./x.txt"]},
            }
        ]
    )
    assert len(t.acceptance_criteria) == 1
    c = t.acceptance_criteria[0]
    assert isinstance(c, AcceptanceCriterion)
    assert c.text == "foo"
    assert c.verifier == "path_exists"
    assert c.spec == {"paths": ["./x.txt"]}
    assert c.last_status == "pending"


# ---- _is_done gating --------------------------------------------------------


def test_is_done_false_when_verifier_criterion_not_passed(mock_orchestrator, tmp_workspace):
    orch = mock_orchestrator()
    orch._ensure_isolation()
    # Mark the world as "organisationally complete": all agents complete, no
    # open tasks except one whose criterion has a verifier still pending.
    agent = orch._spawn_agent(Role.QA, "qa-1", "")
    agent.state.status = "complete"

    task = Task(
        id="t1",
        title="t",
        assignee="qa-1",
        status="complete",
        acceptance_criteria=[
            {
                "text": "x.txt exists",
                "verifier": "path_exists",
                "spec": {"paths": ["x.txt"]},
            }
        ],
    )
    orch.world.tasks[task.id] = task
    # Criterion was never run → last_status == "pending" → _is_done False.
    assert orch._is_done() is False


def test_is_done_true_legacy_no_verifier(mock_orchestrator, tmp_workspace):
    """No criterion has a verifier → fall back to organizational completion."""
    orch = mock_orchestrator()
    orch._ensure_isolation()
    agent = orch._spawn_agent(Role.QA, "qa-2", "")
    agent.state.status = "complete"

    task = Task(
        id="t2",
        title="legacy",
        assignee="qa-2",
        status="complete",
        acceptance_criteria=["just narrative"],  # text-only, no verifier
    )
    orch.world.tasks[task.id] = task
    assert orch._is_done() is True


# ---- deliverable rejected when attached criterion fails ---------------------


def test_deliverable_rejected_when_criterion_fails(
    mock_orchestrator, tmp_workspace, event_recorder
):
    """A deliverable whose attached criterion's verifier fails must be
    rejected. The task stays open, the agent gets queued back as rejected,
    and the criterion records last_status=='failed'."""
    on_event, captured = event_recorder()
    orch = mock_orchestrator(on_event=on_event)
    orch._ensure_isolation()

    agent = orch._spawn_agent(Role.QA, "qa-3", "")
    task = Task(
        id="task_qa",
        title="qa task",
        assignee="qa-3",
        status="in_progress",
        acceptance_criteria=[
            {
                "text": "must-exist.txt exists",
                "verifier": "path_exists",
                "spec": {"paths": ["must-exist.txt"]},
            }
        ],
    )
    orch.world.tasks[task.id] = task
    agent.state.assigned_tasks.append(task.id)

    # Create the file the agent "claims" so the path_exists check on the
    # claimed file passes — we want the FAILURE to come from the criterion,
    # not the missing-files prelude.
    (Path(tmp_workspace.code_dir) / "delivered.txt").write_text("d")

    captured.clear()
    orch._apply_action(
        agent,
        {
            "type": "deliverable",
            "title": "qa work",
            "summary": "done",
            "files_touched": ["delivered.txt"],
        },
    )

    # Task should NOT have flipped to done.
    assert task.status != "complete"
    # Agent should be in the rejected set.
    assert agent.state.name in orch._rejected_this_turn
    # Criterion should now reflect a failed verifier run.
    assert task.acceptance_criteria[0].last_status == "failed"
    # And an emitted event should signal the criterion failure path.
    kinds = [k for k, _ in captured]
    assert "criterion_failed" in kinds
    assert "deliverable_rejected" in kinds
