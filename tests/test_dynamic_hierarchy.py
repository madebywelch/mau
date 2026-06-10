"""Fractal-org regression suite: manager-as-spawner, mandate enforcement,
span of control, dynamic escalation routing, and retirement.

All tests drive the orchestrator's action-application machinery directly with
synthetic AgentTurns — no inference calls, no network.
"""

from __future__ import annotations

from typing import Any

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import (
    AUTO_RETIRE_IDLE_TICKS,
    MAX_DIRECT_REPORTS,
    MAX_TURNS_PER_AGENT,
    MAX_TURNS_PER_MANAGER,
    Orchestrator,
)
from mau_cli.schemas import AgentTurn, Message, Role, Task, Workspace


def _orch(
    workspace: Workspace,
    events: list[tuple[str, dict[str, Any]]],
    **kw: Any,
) -> Orchestrator:
    return Orchestrator(
        backend=MockBackend(),
        workspace=workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
        **kw,
    )


def _spawn_turn(name: str, role: str = "backend", **extra: Any) -> AgentTurn:
    return AgentTurn(
        status="working",
        actions=[{"type": "spawn_agent", "role": role, "name": name, **extra}],
    )


# ---- manager = spawner -------------------------------------------------------


def test_spawn_sets_manager_to_spawner(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")

    orch._apply_turn(tl, _spawn_turn("be-1", brief="Own the items API end to end."))

    be = orch.world.agents["be-1"]
    assert be.manager == "tl-1"
    assert be.brief == "Own the items API end to end."
    spawned = [p for k, p in events if k == "agent_spawned" and p["name"] == "be-1"]
    assert spawned and spawned[0]["manager"] == "tl-1"
    # The brief is also delivered as a directive so the first turn carries it.
    assert any(
        m.msg_type == "directive" and m.subject == "Your mandate"
        for m in be.inbox
    )


def test_root_spawn_has_no_manager(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    product = orch._spawn_agent(Role.PRODUCT, "product-1", "")
    assert product.state.manager is None


# ---- purposeful spawning -----------------------------------------------------


def test_purposeless_spawn_rejected(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")

    orch._apply_turn(tl, _spawn_turn("be-1"))  # no brief, no task, no directive

    assert "be-1" not in orch.world.agents
    rejected = [p for k, p in events if k == "spawn_rejected"]
    assert rejected and rejected[0]["reason"] == "no_mandate"
    blockers = [m for m in tl.state.inbox if m.msg_type == "blocker"]
    assert blockers and "purpose" in blockers[0].body


def test_spawn_with_same_turn_task_accepted(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")

    turn = AgentTurn(
        status="working",
        actions=[
            {"type": "spawn_agent", "role": "backend", "name": "be-1"},
            {
                "type": "create_task",
                "id": "task_api",
                "title": "Implement API",
                "assignee": "be-1",
            },
        ],
    )
    orch._apply_turn(tl, turn)

    assert "be-1" in orch.world.agents
    assert not [p for k, p in events if k == "spawn_rejected"]
    assert orch.world.tasks["task_api"].assignee == "be-1"


def test_spawn_with_same_turn_directive_accepted(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")

    turn = AgentTurn(
        status="working",
        actions=[
            {"type": "spawn_agent", "role": "qa", "name": "qa-1"},
            {
                "type": "send_message",
                "to": "qa-1",
                "msg_type": "directive",
                "subject": "Test plan",
                "body": "Cover the items API.",
            },
        ],
    )
    orch._apply_turn(tl, turn)
    assert "qa-1" in orch.world.agents
    assert not [p for k, p in events if k == "spawn_rejected"]


# ---- span of control ---------------------------------------------------------


def test_span_of_control_rejected_then_freed(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events, max_agents=30)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    for i in range(MAX_DIRECT_REPORTS):
        orch._spawn_agent(Role.BACKEND, f"be-{i}", "", manager="tl-1")

    orch._apply_turn(tl, _spawn_turn("be-extra", brief="overflow"))
    assert "be-extra" not in orch.world.agents
    rejected = [p for k, p in events if k == "spawn_rejected"]
    assert rejected and rejected[-1]["reason"] == "span_of_control"
    blockers = [m for m in tl.state.inbox if m.msg_type == "blocker"]
    assert blockers and "sub-lead" in blockers[-1].body

    # Retiring a report frees a slot — wave staffing.
    orch.world.agents["be-0"].status = "complete"
    orch._apply_turn(tl, _spawn_turn("be-extra", brief="overflow"))
    assert "be-extra" in orch.world.agents


# ---- SPAWNABLE_BY matrix -----------------------------------------------------


def test_spawnable_by_matrix(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    em = orch._spawn_agent(Role.ENGINEERING_MANAGER, "em-1", "")

    # EM may not spawn specialists directly — delegate through a lead.
    orch._apply_turn(em, _spawn_turn("fe-1", role="frontend", brief="UI"))
    assert "fe-1" not in orch.world.agents
    rejected = [p for k, p in events if k == "spawn_rejected"]
    assert rejected and rejected[-1]["reason"] == "role_not_spawnable"

    # EM → tech_lead is allowed; TL → TL (sub-lead) is allowed.
    orch._apply_turn(em, _spawn_turn("tl-1", role="tech_lead", brief="epic 1"))
    assert "tl-1" in orch.world.agents
    tl = orch.agents["tl-1"]
    orch._apply_turn(tl, _spawn_turn("tl-sub-1", role="tech_lead", brief="sub-domain"))
    assert "tl-sub-1" in orch.world.agents
    assert orch.world.agents["tl-sub-1"].manager == "tl-1"


def test_specialist_cannot_spawn(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    be = orch._spawn_agent(Role.BACKEND, "be-1", "")
    orch._apply_turn(be, _spawn_turn("be-2", brief="helper"))
    assert "be-2" not in orch.world.agents
    assert [p for k, p in events if k == "policy_violation"]


def test_duplicate_spawn_is_noop_and_keeps_manager(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    em = orch._spawn_agent(Role.ENGINEERING_MANAGER, "em-1", "")
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "", manager="em-1")
    other = orch._spawn_agent(Role.TECH_LEAD, "tl-2", "", manager="em-1")

    # tl-2 re-spawning the existing name must not steal the manager edge.
    orch._apply_turn(other, _spawn_turn("tl-1", role="tech_lead", brief="x"))
    assert orch.world.agents["tl-1"].manager == "em-1"
    assert [p for k, p in events if k == "spawn_duplicate"]


# ---- dynamic escalation ------------------------------------------------------


def _build_chain(orch: Orchestrator) -> None:
    orch._spawn_agent(Role.PRODUCT, "product-1", "")
    orch._spawn_agent(Role.ENGINEERING_MANAGER, "em-1", "", manager="product-1")
    orch._spawn_agent(Role.TECH_LEAD, "tl-1", "", manager="em-1")
    orch._spawn_agent(Role.TECH_LEAD, "tl-sub-1", "", manager="tl-1")
    orch._spawn_agent(Role.BACKEND, "be-1", "", manager="tl-sub-1")


def test_escalation_climbs_dynamic_tree(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    _build_chain(orch)
    be = orch.agents["be-1"]

    orch._escalate(be, "stuck on schema")
    # Nearest live manager is the sub-lead — NOT the first tech_lead by
    # role scan (which would be tl-1).
    assert any(
        m.msg_type == "escalation" and m.from_agent == "be-1"
        for m in orch.world.agents["tl-sub-1"].inbox
    )
    assert not any(
        m.from_agent == "be-1" for m in orch.world.agents["tl-1"].inbox
    )

    # A permanently turn-capped manager is skipped.
    orch.world.agents["tl-sub-1"].turns_taken = MAX_TURNS_PER_MANAGER
    orch._escalate(be, "still stuck")
    assert any(
        m.msg_type == "escalation" and m.from_agent == "be-1"
        for m in orch.world.agents["tl-1"].inbox
    )


def test_escalation_from_root_parks_for_user(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    product = orch._spawn_agent(Role.PRODUCT, "product-1", "")
    orch._escalate(product, "is dark mode in scope?")
    assert orch.world.pending_user_questions
    kinds = {k for k, _ in events}
    assert "escalation_unresolvable" in kinds
    assert "escalation_to_user" in kinds


def test_escalation_reactivates_completed_manager(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    _build_chain(orch)
    tl_sub = orch.world.agents["tl-sub-1"]
    tl_sub.status = "complete"
    tl_sub.turns_taken = 2

    orch._escalate(orch.agents["be-1"], "need a decision")
    ready = [a.state.name for a in orch._ready_agents()]
    assert "tl-sub-1" in ready
    assert [p for k, p in events if k == "agent_reactivated" and p["agent"] == "tl-sub-1"]


def test_deliverable_reactivates_completed_manager_only(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    _build_chain(orch)
    tl_sub = orch.world.agents["tl-sub-1"]
    tl_sub.status = "complete"
    tl_sub.turns_taken = 2
    be = orch.world.agents["be-1"]
    be.status = "complete"
    be.turns_taken = 2

    rollup = Message(
        from_agent="x", to_agent="tl-sub-1", msg_type="deliverable",
        subject="Roll-up", body="done",
    )
    tl_sub.inbox.append(rollup)
    be.inbox.append(
        Message(from_agent="x", to_agent="be-1", msg_type="deliverable",
                subject="FYI", body="done")
    )
    ready = [a.state.name for a in orch._ready_agents()]
    assert "tl-sub-1" in ready  # manager woken by a deliverable
    assert "be-1" not in ready  # workers are not


# ---- resume back-compat ------------------------------------------------------


def test_resume_backfills_manager_from_legacy_role_map(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    seed = _orch(tmp_workspace, events)
    # Legacy session: no manager edges at all.
    seed._spawn_agent(Role.PRODUCT, "product-1", "")
    seed._spawn_agent(Role.ENGINEERING_MANAGER, "em-1", "")
    seed._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    seed._spawn_agent(Role.BACKEND, "be-1", "")
    snapshot = seed.world.snapshot()
    for a in snapshot["agents"].values():
        a.pop("manager", None)  # simulate a pre-fractal session.json

    fresh_events: list[tuple[str, dict[str, Any]]] = []
    orch = _orch(tmp_workspace, fresh_events)
    assert orch.load_from_disk(snapshot)
    assert orch.world.agents["product-1"].manager is None
    assert orch.world.agents["em-1"].manager == "product-1"
    assert orch.world.agents["tl-1"].manager == "em-1"
    assert orch.world.agents["be-1"].manager == "tl-1"


# ---- retirement --------------------------------------------------------------


def test_retire_agent_requires_direct_manager(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    _build_chain(orch)
    em = orch.agents["em-1"]

    # be-1 reports to tl-sub-1, not em-1.
    orch._apply_turn(
        em,
        AgentTurn(status="working", actions=[{"type": "retire_agent", "name": "be-1"}]),
    )
    assert orch.world.agents["be-1"].status != "complete"
    invalid = [p for k, p in events if k == "retire_agent_invalid"]
    assert invalid and "not your direct report" in invalid[0]["reason"]


def test_retire_agent_blocked_by_open_tasks_then_succeeds(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    _build_chain(orch)
    tl_sub = orch.agents["tl-sub-1"]
    task = Task(id="t1", title="api", assignee="be-1", creator="tl-sub-1")
    orch.world.tasks["t1"] = task
    orch.world.agents["be-1"].assigned_tasks.append("t1")

    retire = AgentTurn(
        status="working",
        actions=[{"type": "retire_agent", "name": "be-1", "reason": "done"}],
    )
    orch._apply_turn(tl_sub, retire)
    assert orch.world.agents["be-1"].status != "complete"
    assert [p for k, p in events if k == "retire_agent_invalid"]

    task.status = "complete"
    orch._apply_turn(tl_sub, retire)
    assert orch.world.agents["be-1"].status == "complete"
    retired = [p for k, p in events if k == "agent_retired"]
    assert retired and retired[0]["agent"] == "be-1" and retired[0]["by"] == "tl-sub-1"


def test_auto_retire_idle_worker_but_not_busy_managers(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    orch._spawn_agent(Role.BACKEND, "be-1", "", manager="tl-1")
    orch.world.agents["tl-1"].turns_taken = 1
    orch.world.agents["be-1"].turns_taken = 1
    orch.world.agents["tl-1"].status = "working"
    orch.world.agents["be-1"].status = "working"

    for _ in range(AUTO_RETIRE_IDLE_TICKS):
        orch._ready_agents()

    # The idle worker retires; the manager still had an active report at the
    # moment its own countdown matured, so it stays alive that evaluation.
    assert orch.world.agents["be-1"].status == "complete"
    auto = [p["agent"] for k, p in events if k == "agent_auto_retired"]
    assert "be-1" in auto and "tl-1" not in auto
    # Manager was notified via a non-reactivating status message.
    assert any(
        m.msg_type == "status" and "auto-retired" in m.subject
        for m in orch.world.agents["tl-1"].inbox
    )


def test_auto_retire_spares_creators_of_open_tasks(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    orch._spawn_agent(Role.BACKEND, "be-1", "", manager="tl-1")
    tl = orch.world.agents["tl-1"]
    tl.turns_taken = 1
    tl.status = "working"
    orch.world.tasks["t1"] = Task(id="t1", title="x", assignee="be-1", creator="tl-1")
    orch.world.agents["be-1"].assigned_tasks.append("t1")

    for _ in range(AUTO_RETIRE_IDLE_TICKS + 2):
        orch._ready_agents()
    # tl-1 created an open task — it must stay alive to receive the
    # deliverable, even though its countdown is exhausted.
    assert tl.status != "complete"


# ---- role-class turn caps ----------------------------------------------------


def test_turn_caps_by_role_class(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch(tmp_workspace, events)
    tl = orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    be = orch._spawn_agent(Role.BACKEND, "be-1", "", manager="tl-1")
    assert orch._turn_cap_for(tl.state) == MAX_TURNS_PER_MANAGER
    assert orch._turn_cap_for(be.state) == MAX_TURNS_PER_AGENT

    # A manager past the WORKER cap is still reactivatable.
    tl.state.status = "complete"
    tl.state.turns_taken = MAX_TURNS_PER_AGENT
    tl.state.inbox.append(
        Message(from_agent="x", to_agent="tl-1", msg_type="directive",
                subject="more", body="…")
    )
    ready = [a.state.name for a in orch._ready_agents()]
    assert "tl-1" in ready
