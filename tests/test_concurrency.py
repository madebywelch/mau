"""Regression: inference workers are pure, so concurrent turns don't race on
shared WorldState.

Pre-fix, `Agent.run_turn` mutated `world.usage`, the agent inbox, and read
`world.agents`/`world.tasks` from inside the thread-pool worker while the main
thread mutated the same state — losing cost updates, dropping inbox messages,
and risking `dict changed size during iteration`. The fix builds the prompt and
applies all mutation on the orchestrator's main thread; the worker only shells
out to the backend.
"""

from __future__ import annotations

from mau_cli.agent import Agent
from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import AgentState, Message, Role, WorldState


def test_usage_accounting_is_exact_under_concurrency(tmp_workspace):
    """With every call costing exactly $1, total cost must equal call count and
    must equal the sum of per-agent usage — no adds lost to a data race. Run at
    high concurrency so multiple turns finish near-simultaneously."""
    events: list[tuple[str, dict]] = []
    backend = MockBackend(cost_per_call_usd=1.0)
    orch = Orchestrator(
        backend=backend,
        max_turns=80,
        concurrency=8,
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch.run("build a hello-world feature with tests")

    kinds = [k for k, _ in events]
    assert "stopped_on_completion" in kinds, (
        f"run did not converge; events: {sorted(set(kinds))}"
    )

    u = orch.world.usage
    assert u.calls > 0
    # cost_per_call is 1.0, so cost (a float sum of 1.0s) must equal calls
    # exactly iff no add was lost.
    assert u.cost_usd == float(u.calls), (
        f"lost usage adds under concurrency: cost={u.cost_usd} calls={u.calls}"
    )
    # Greenfield run: every dollar flows through an agent turn, so world usage
    # must reconcile with the per-agent totals (both summed on the main thread).
    assert u.calls == sum(a.state.usage.calls for a in orch.agents.values())
    assert u.cost_usd == sum(a.state.usage.cost_usd for a in orch.agents.values())


def test_finalize_consumes_only_shown_messages(tmp_workspace):
    """A message delivered *after* the prompt was built (i.e. mid-turn) must
    survive to the next turn rather than being cleared. This is the message-loss
    race the inbox-snapshot consumption fixes."""
    world = WorldState()
    world.workspace = tmp_workspace
    backend = MockBackend()
    state = AgentState(name="em-1", role=Role.ENGINEERING_MANAGER)
    world.agents["em-1"] = state
    agent = Agent(state, backend)

    m1 = Message(from_agent="user", to_agent="em-1", msg_type="directive",
                 subject="shown", body="in the prompt")
    state.inbox.append(m1)

    prompt = agent.build_user_prompt(world)  # records consumed = {m1.id}

    # Now a message arrives while the turn is "in flight".
    m2 = Message(from_agent="user", to_agent="em-1", msg_type="directive",
                 subject="mid-turn", body="arrived after prompt build")
    state.inbox.append(m2)

    wd, ed = agent.infer_dirs(world, None)
    result = agent.infer(prompt, workspace_dir=wd, extra_dirs=ed)
    agent.finalize_turn(world, prompt, result)

    inbox_ids = {m.id for m in state.inbox}
    assert m1.id not in inbox_ids, "the message shown in the prompt must be consumed"
    assert m2.id in inbox_ids, "a message delivered mid-turn must NOT be dropped"


def test_infer_does_not_mutate_world_or_agent_state(tmp_workspace):
    """`infer` must be a pure function of its inputs — no WorldState or agent
    bookkeeping — so it's safe to run off the main thread."""
    world = WorldState()
    world.workspace = tmp_workspace
    backend = MockBackend(cost_per_call_usd=0.5)
    state = AgentState(name="be-1", role=Role.BACKEND)
    world.agents["be-1"] = state
    agent = Agent(state, backend)

    prompt = agent.build_user_prompt(world)
    wd, ed = agent.infer_dirs(world, None)

    before_world = world.usage.calls
    before_agent = state.usage.calls
    before_turns = state.turns_taken

    agent.infer(prompt, workspace_dir=wd, extra_dirs=ed, max_budget_usd=None)

    assert world.usage.calls == before_world, "infer must not touch world.usage"
    assert state.usage.calls == before_agent, "infer must not touch agent usage"
    assert state.turns_taken == before_turns, "infer must not advance the turn counter"
