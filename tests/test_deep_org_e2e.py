"""End-to-end fractal-org run on the deterministic mock backend.

product-1 → em-1 → {tl-epic-1 → {tl-sub-1 → {db-sub-1, qa-sub-1}, be-epic1},
tl-epic-2 → fe-epic2}: four manager edges root-to-leaf, briefs as mandates,
wave roll-ups, and manager-issued retirement — converging organically.
"""

from __future__ import annotations

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import WorldState


def _run_deep_org(tmp_workspace, events, concurrency: int) -> tuple[Orchestrator, WorldState]:
    orch = Orchestrator(
        backend=MockBackend(deep_org=True),
        max_turns=120,
        max_agents=20,
        concurrency=concurrency,
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    world = orch.run("build an items product")
    return orch, world


def _chain_depth(world: WorldState, name: str) -> int:
    depth, current, seen = 0, world.agents[name].manager, set()
    while current is not None:
        assert current not in seen, f"cycle in manager edges at {current}"
        seen.add(current)
        assert current in world.agents, f"dangling manager {current}"
        depth += 1
        current = world.agents[current].manager
    return depth


def test_deep_org_converges_with_four_manager_levels(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch, world = _run_deep_org(tmp_workspace, events, concurrency=3)

    kinds = {k for k, _ in events}
    assert "stopped_on_completion" in kinds
    assert "stall" not in kinds
    assert "stopped_on_turn_cap" not in kinds
    assert not [p for k, p in events if k == "spawn_rejected"], (
        "every scripted spawn carries a mandate; none should be rejected"
    )

    # Every non-root agent has a manager; edges form a tree rooted at product-1.
    for name, state in world.agents.items():
        if name == "product-1":
            assert state.manager is None
        else:
            assert state.manager in world.agents, name

    assert max(_chain_depth(world, n) for n in world.agents) >= 4

    # The org converged level by level: everyone complete, all work done.
    assert all(a.status == "complete" for a in world.agents.values())
    assert world.tasks, "the scripts create real tasks"
    assert all(t.status == "complete" for t in world.tasks.values())

    # Managers actively retired their reports on roll-up.
    retired = {p["agent"] for k, p in events if k == "agent_retired"}
    assert {"tl-epic-1", "tl-epic-2", "tl-sub-1"} <= retired

    # Mandates were set and rendered.
    assert world.agents["tl-epic-1"].brief
    assert world.agents["tl-sub-1"].brief


def test_deep_org_converges_serially(tmp_workspace, event_recorder):
    """concurrency=1 exercises strictly interleaved waiting turns."""
    on_event, events = event_recorder()
    orch, world = _run_deep_org(tmp_workspace, events, concurrency=1)
    assert "stopped_on_completion" in {k for k, _ in events}
    assert all(a.status == "complete" for a in world.agents.values())


def test_deep_org_team_scoping_held(tmp_workspace, event_recorder):
    """Epic 1's contract must never have rendered into epic 2's squad —
    the doc-scoping property, asserted on a full live run."""
    on_event, events = event_recorder()
    orch, world = _run_deep_org(tmp_workspace, events, concurrency=3)

    fe = orch.agents["fe-epic2"]
    assert fe.last_prompt, "specialist took at least one turn"
    assert "epic1-api-contract.md" not in fe.last_prompt
    # Its own squad context did render.
    assert "MANAGER: tl-epic-2" in fe.last_prompt

    be = orch.agents["be-epic1"]
    assert "epic1-api-contract.md" in be.last_prompt
