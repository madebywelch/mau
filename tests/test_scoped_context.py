"""Team-scoped context: prompts render an agent's team slice and team docs,
not the whole org — the property that keeps prompt size Θ(team) instead of
Θ(org) as runs scale to hundreds of agents. Plus team-scoped broadcast.
"""

from __future__ import annotations

from mau_cli.agent import Agent
from mau_cli.message_bus import MessageBus
from mau_cli.mock_inference import MockBackend
from mau_cli.schemas import (
    AgentState,
    Message,
    Role,
    Task,
    WorldState,
)


def _two_squad_world(tmp_workspace) -> WorldState:
    """product-1 → em-1 → {tl-a → be-a, tl-b → be-b}, with one contract doc
    published per squad lead plus the org-global PRD."""
    world = WorldState(workspace=tmp_workspace)
    agents = [
        AgentState(name="product-1", role=Role.PRODUCT),
        AgentState(name="em-1", role=Role.ENGINEERING_MANAGER, manager="product-1"),
        AgentState(name="tl-a", role=Role.TECH_LEAD, manager="em-1"),
        AgentState(name="tl-b", role=Role.TECH_LEAD, manager="em-1"),
        AgentState(name="be-a", role=Role.BACKEND, manager="tl-a"),
        AgentState(name="be-b", role=Role.BACKEND, manager="tl-b"),
    ]
    for a in agents:
        world.agents[a.name] = a
    world.put_doc("prd.md", "# PRD\nGlobal truth.", author="product-1", turn=1)
    world.put_doc("a-contract.md", "# Squad A contract", author="tl-a", turn=2)
    world.put_doc("b-contract.md", "# Squad B contract", author="tl-b", turn=2)
    return world


def test_prompt_excludes_other_teams_docs(tmp_workspace):
    world = _two_squad_world(tmp_workspace)
    prompt_b = Agent(world.agents["be-b"], MockBackend()).build_user_prompt(world)
    assert "# PRD" in prompt_b  # org-global
    assert "# Squad B contract" in prompt_b  # my lead authored it
    assert "Squad A contract" not in prompt_b  # other team's doc

    prompt_a = Agent(world.agents["be-a"], MockBackend()).build_user_prompt(world)
    assert "# Squad A contract" in prompt_a
    assert "Squad B contract" not in prompt_a


def test_doc_refs_pulls_cross_team_doc(tmp_workspace):
    world = _two_squad_world(tmp_workspace)
    world.tasks["t1"] = Task(
        id="t1",
        title="consume squad A API",
        assignee="be-b",
        creator="tl-b",
        doc_refs=["a-contract.md"],
    )
    world.agents["be-b"].assigned_tasks.append("t1")
    prompt = Agent(world.agents["be-b"], MockBackend()).build_user_prompt(world)
    assert "# Squad A contract" in prompt


def test_doc_visibility_tracks_only_rendered_versions(tmp_workspace):
    """`last_doc_versions` (deliverable provenance) records only docs the
    prompt actually showed — scoping must not corrupt the audit trail."""
    world = _two_squad_world(tmp_workspace)
    agent = Agent(world.agents["be-b"], MockBackend())
    agent.build_user_prompt(world)
    assert "b-contract.md" in agent.last_doc_versions
    assert "a-contract.md" not in agent.last_doc_versions


def test_roster_slice_only_team(tmp_workspace):
    world = _two_squad_world(tmp_workspace)
    prompt = Agent(world.agents["be-b"], MockBackend()).build_user_prompt(world)
    assert "MANAGER: tl-b" in prompt
    assert "be-a" not in prompt  # other squad's worker
    assert "agents total; you see only your team" in prompt

    # A lead sees manager, peer leads, and its reports.
    tl_prompt = Agent(world.agents["tl-a"], MockBackend()).build_user_prompt(world)
    assert "MANAGER: em-1" in tl_prompt
    assert "tl-b" in tl_prompt  # peer
    assert "be-a" in tl_prompt  # report
    assert "be-b" not in tl_prompt  # another lead's report


def test_legacy_agent_without_manager_sees_everything(tmp_workspace):
    """Agents with no manager edge (pre-fractal sessions, direct test
    construction) degrade to the old org-global behaviour."""
    world = _two_squad_world(tmp_workspace)
    legacy = AgentState(name="qa-1", role=Role.QA)  # manager=None
    world.agents["qa-1"] = legacy
    prompt = Agent(legacy, MockBackend()).build_user_prompt(world)
    assert "TEAM_ROSTER:" in prompt  # full roster rendering
    assert "# Squad A contract" in prompt
    assert "# Squad B contract" in prompt


def test_manager_sees_created_tasks(tmp_workspace):
    world = _two_squad_world(tmp_workspace)
    world.tasks["t1"] = Task(id="t1", title="ship API", assignee="be-a", creator="tl-a")
    world.tasks["t2"] = Task(
        id="t2", title="done already", assignee="be-a", creator="tl-a",
        status="complete",
    )
    prompt = Agent(world.agents["tl-a"], MockBackend()).build_user_prompt(world)
    assert "TASKS_YOU_CREATED:" in prompt
    assert "t1 [pending] ship API → be-a" in prompt
    assert "(1 already complete/cancelled)" in prompt
    # Workers don't get the section.
    worker_prompt = Agent(world.agents["be-a"], MockBackend()).build_user_prompt(world)
    assert "TASKS_YOU_CREATED:" not in worker_prompt


def test_mandate_renders_every_turn(tmp_workspace):
    world = _two_squad_world(tmp_workspace)
    world.agents["be-a"].brief = "Own the items API."
    prompt = Agent(world.agents["be-a"], MockBackend()).build_user_prompt(world)
    assert "YOUR_MANDATE:" in prompt
    assert "Own the items API." in prompt


def test_broadcast_scoped_to_team(tmp_workspace):
    world = _two_squad_world(tmp_workspace)
    bus = MessageBus(world)
    bus.deliver(
        Message(from_agent="tl-a", to_agent="broadcast", msg_type="status",
                subject="heads up", body="…")
    )
    assert any(m.subject == "heads up" for m in world.agents["em-1"].inbox)  # manager
    assert any(m.subject == "heads up" for m in world.agents["tl-b"].inbox)  # peer
    assert any(m.subject == "heads up" for m in world.agents["be-a"].inbox)  # report
    assert not any(m.subject == "heads up" for m in world.agents["be-b"].inbox)
    assert not any(m.subject == "heads up" for m in world.agents["product-1"].inbox)


def test_user_broadcast_stays_global(tmp_workspace):
    world = _two_squad_world(tmp_workspace)
    bus = MessageBus(world)
    bus.deliver(
        Message(from_agent="user", to_agent="broadcast", msg_type="status",
                subject="announcement", body="…")
    )
    for agent in world.agents.values():
        assert any(m.subject == "announcement" for m in agent.inbox), agent.name
