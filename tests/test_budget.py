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


class _BudgetSpyBackend(MockBackend):
    """Records the `max_budget_usd` passed to every agentic (specialist) call."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.agentic_budgets: list = []

    def call_agentic(
        self, system_prompt, user_prompt, workspace_dir,
        extra_dirs=None, max_budget_usd=None,
    ):
        self.agentic_budgets.append(max_budget_usd)
        return super().call_agentic(
            system_prompt, user_prompt, workspace_dir, extra_dirs, max_budget_usd
        )


def test_remaining_budget_is_threaded_into_specialist_turns(tmp_workspace):
    """Pre-fix, --max-budget reached `call_agentic` only for the brownfield
    pre-flight scan; specialist code-gen turns were dispatched with no per-call
    cap, so a single runaway turn could blow the budget. Each agentic turn must
    now receive the *remaining* budget."""
    backend = _BudgetSpyBackend(cost_per_call_usd=0.5)
    orch = Orchestrator(
        backend=backend,
        max_turns=60,
        concurrency=1,  # deterministic spend → monotonic remaining budget
        workspace=tmp_workspace,
        max_budget_usd=5.0,
        isolation="shared",
    )
    orch.run("build a hello-world feature with tests")

    assert backend.agentic_budgets, "no specialist turn ran"
    # Every specialist turn got a concrete remaining budget within the cap.
    assert all(b is not None for b in backend.agentic_budgets), (
        f"a specialist turn ran with no budget cap: {backend.agentic_budgets}"
    )
    assert all(0.0 <= b <= 5.0 for b in backend.agentic_budgets)
    # It's the *remaining* budget, not the static cap: planners spend first, so
    # the first specialist sees less than the full $5, and it never increases
    # under serial dispatch.
    assert backend.agentic_budgets[0] < 5.0
    assert backend.agentic_budgets == sorted(backend.agentic_budgets, reverse=True)


def test_no_budget_cap_passes_none_to_specialists(tmp_workspace):
    """With no --max-budget, specialist turns must receive None (uncapped)."""
    backend = _BudgetSpyBackend()
    orch = Orchestrator(
        backend=backend,
        max_turns=60,
        concurrency=1,
        workspace=tmp_workspace,
        max_budget_usd=None,
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    assert backend.agentic_budgets, "no specialist turn ran"
    assert all(b is None for b in backend.agentic_budgets)
