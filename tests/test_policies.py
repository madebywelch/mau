"""Task 5: durable policies — add/retire/scope/round-trip + prompt injection."""

from __future__ import annotations

import json
from pathlib import Path

from mau_cli.agent import Agent
from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import (
    AgentState,
    Policy,
    Role,
    Task,
    WorldState,
    Workspace,
)


# ---- WorldState.add_policy / active_policies --------------------------------


def test_add_policy_dedups_active():
    ws = WorldState()
    p1 = ws.add_policy("no force-push", "global", "user", turn=1)
    p2 = ws.add_policy("no force-push", "global", "user", turn=2)
    assert p1 is p2
    assert len(ws.policies) == 1


def test_add_policy_retired_does_not_dedup():
    ws = WorldState()
    p1 = ws.add_policy("rule X", "global", "user", turn=1)
    p1.active = False
    p2 = ws.add_policy("rule X", "global", "user", turn=2)
    # Retired policy shouldn't satisfy the dedup check.
    assert p1 is not p2
    assert len(ws.policies) == 2


def test_active_policies_role_filter():
    ws = WorldState()
    g = ws.add_policy("global rule", "global", "user", turn=1)
    devops = ws.add_policy("devops rule", "role:devops", "user", turn=1)
    frontend = ws.add_policy("frontend rule", "role:frontend", "user", turn=1)

    matched = ws.active_policies("role:devops")
    ids = [p.id for p in matched]
    assert g.id in ids
    assert devops.id in ids
    assert frontend.id not in ids


def test_active_policies_task_filter():
    ws = WorldState()
    g = ws.add_policy("global rule", "global", "user", turn=1)
    t1 = ws.add_policy("task rule 1", "task:t1", "user", turn=1)
    t2 = ws.add_policy("task rule 2", "task:t2", "user", turn=1)

    matched = ws.active_policies("task:t1")
    ids = [p.id for p in matched]
    assert g.id in ids
    assert t1.id in ids
    assert t2.id not in ids


# ---- orchestrator action dispatch -------------------------------------------


def test_record_policy_action_mutates_state_and_emits(
    mock_orchestrator, tmp_workspace, event_recorder
):
    on_event, captured = event_recorder()
    orch = mock_orchestrator(on_event=on_event)
    orch._ensure_isolation()

    # PM is allowed to send actions; using any spawned role works for this test.
    agent = orch._spawn_agent(Role.PRODUCT, "product-1", "")
    captured.clear()
    orch._apply_action(
        agent,
        {"type": "record_policy", "text": "no deploys past 4pm", "scope": "global"},
    )

    assert len(orch.world.policies) == 1
    assert orch.world.policies[0].text == "no deploys past 4pm"
    kinds = [k for k, _ in captured]
    assert "policy_recorded" in kinds


def test_retire_policy_action(mock_orchestrator, tmp_workspace, event_recorder):
    on_event, captured = event_recorder()
    orch = mock_orchestrator(on_event=on_event)
    orch._ensure_isolation()
    agent = orch._spawn_agent(Role.PRODUCT, "product-1", "")

    policy = orch.world.add_policy("retire me", "global", "user", turn=0)
    captured.clear()
    orch._apply_action(
        agent, {"type": "retire_policy", "policy_id": policy.id}
    )
    assert policy.active is False
    kinds = [k for k, _ in captured]
    assert "policy_retired" in kinds


# ---- prompt injection -------------------------------------------------------


def test_build_user_prompt_filters_policies_by_scope(tmp_workspace):
    world = WorldState(workspace=tmp_workspace)
    world.add_policy("global rule G", "global", "user", turn=1)
    world.add_policy("devops rule D", "role:devops", "user", turn=1)
    world.add_policy("frontend rule F", "role:frontend", "user", turn=1)
    world.add_policy("task1 rule T1", "task:t1", "user", turn=1)
    world.add_policy("task2 rule T2", "task:t2", "user", turn=1)

    # Set up an open t1 for this agent.
    state = AgentState(name="devops-1", role=Role.DEVOPS)
    state.assigned_tasks.append("t1")
    world.tasks["t1"] = Task(id="t1", title="x", assignee="devops-1", status="in_progress")
    world.tasks["t2"] = Task(id="t2", title="y", assignee="other", status="in_progress")
    world.agents["devops-1"] = state

    agent = Agent(state, MockBackend())
    prompt = agent.build_user_prompt(world)

    assert "global rule G" in prompt
    assert "devops rule D" in prompt
    assert "task1 rule T1" in prompt
    assert "frontend rule F" not in prompt
    assert "task2 rule T2" not in prompt


# ---- snapshot round-trip ----------------------------------------------------


def test_policy_round_trips_through_disk(tmp_workspace):
    """Persist a policy via snapshot/_persist, load it back, and confirm
    the rehydrated Policy matches."""
    orch = Orchestrator(backend=MockBackend(), workspace=tmp_workspace, isolation="shared")
    orch._ensure_isolation()
    pol = orch.world.add_policy("rule R", "role:devops", "user", turn=3)

    orch._persist()
    raw = json.loads(Path(tmp_workspace.session_file).read_text())
    assert any(p["id"] == pol.id for p in raw["policies"])

    # Round-trip through _rehydrate_policies on a fresh orchestrator.
    fresh = Orchestrator(backend=MockBackend(), workspace=tmp_workspace, isolation="shared")
    fresh._rehydrate_policies(raw["policies"])
    rehydrated = [p for p in fresh.world.policies if p.id == pol.id]
    assert rehydrated
    r = rehydrated[0]
    assert r.text == pol.text
    assert r.scope == pol.scope
    assert r.source == pol.source
    assert r.active == pol.active
    assert r.created_turn == pol.created_turn
