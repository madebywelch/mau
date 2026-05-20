"""Regression: --max-budget must stop dispatching new turns once met.

Pre-fix, the budget check only fired between turns in `_main_loop`. A single
expensive turn could land while spend was just under the cap and push the
total well past it. The fix adds a pre-flight check in `_tick` so no fresh
turn starts after `total_cost_usd >= max_budget_usd`.
"""

from __future__ import annotations

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator


def test_budget_cap_stops_dispatching_new_turns(tmp_workspace):
    """Once `world.total_cost_usd >= max_budget_usd`, _tick refuses to dispatch
    any new turn and emits `budget_reached` exactly once."""
    events: list[tuple[str, dict]] = []
    # Each agent turn costs $0.50. The first 2 turns push spend to $1.00,
    # which meets the $1.00 cap. The third turn must NOT be dispatched.
    backend = MockBackend(cost_per_call_usd=0.50)
    orch = Orchestrator(
        backend=backend,
        max_turns=30,
        concurrency=1,
        workspace=tmp_workspace,
        max_budget_usd=1.00,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    kinds = [k for k, _ in events]
    # The cap must have been announced.
    assert "budget_reached" in kinds, f"budget_reached missing; got {sorted(set(kinds))}"
    # And it must be announced exactly once (one-shot guard).
    assert kinds.count("budget_reached") == 1, (
        f"budget_reached emitted {kinds.count('budget_reached')} times — should be 1"
    )
    # The run must NOT have terminated by hitting the turn cap; it should
    # have halted because of the budget.
    assert "stopped_on_turn_cap" not in kinds


def test_budget_pre_flight_blocks_first_overage_turn(tmp_workspace):
    """Concrete leak case from the real run: spend already exceeds the cap
    when _tick runs, so no fresh turn is started — total stays bounded by
    the in-flight calls already accounted for."""
    events: list[tuple[str, dict]] = []
    backend = MockBackend(cost_per_call_usd=2.0)
    orch = Orchestrator(
        backend=backend,
        max_turns=30,
        concurrency=1,
        workspace=tmp_workspace,
        max_budget_usd=1.0,  # cap below the per-call cost
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    # The first turn naturally exceeds the cap (cost 2.0 > 1.0). Subsequent
    # ticks must see the cap as met and refuse to start anything new.
    # We assert this by counting turns: the orchestrator records each
    # dispatched turn into _global_turns, and at most a small handful should
    # have run before the budget guard cuts the loop off. (Pre-fix, runs
    # would dispatch many turns despite a low cap.)
    assert orch._global_turns <= 2, (
        f"too many turns dispatched after budget hit: {orch._global_turns}"
    )
    kinds = [k for k, _ in events]
    assert "budget_reached" in kinds
