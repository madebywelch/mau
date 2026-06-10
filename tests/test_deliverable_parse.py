"""No-deliverable discipline: a specialist turn without a parseable
<DELIVERABLE> block is tracked, corrected, escalated, and finally given up
on — never silently re-dispatched until the turn budget burns out.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mau_cli.agent import Agent, DELIVERABLE_FORMAT_REMINDER
from mau_cli.inference import InferenceResult, extract_deliverable
from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import (
    NO_DELIVERABLE_CORRECT_AT,
    NO_DELIVERABLE_ESCALATE_AT,
    NO_DELIVERABLE_GIVEUP_AT,
    Orchestrator,
    _agent_state_from_dict,
)
from mau_cli.schemas import AgentState, AgentTurn, Role


# ---- extract_deliverable three-way contract ----------------------------------


def test_extract_deliverable_valid():
    out = extract_deliverable('x <DELIVERABLE>{"title": "t"}</DELIVERABLE>')
    assert out == {"title": "t"}


def test_extract_deliverable_invalid_json_returns_marker():
    out = extract_deliverable("x <DELIVERABLE>{not json}</DELIVERABLE>")
    assert out is not None
    assert out["_parse_error"]
    assert out["_raw_block"] == "{not json}"


def test_extract_deliverable_missing_returns_none():
    assert extract_deliverable("no block here") is None
    assert extract_deliverable("") is None


# ---- Agent._result_to_turn synthesis ------------------------------------------


def _agentic_result(parsed: dict[str, Any], raw: str = "…tail of response") -> InferenceResult:
    return InferenceResult(raw_text=raw, parsed=parsed, backend="mock", duration_ms=1)


def test_parse_error_result_becomes_no_deliverable_action():
    agent = Agent(AgentState(name="be-1", role=Role.BACKEND), MockBackend())
    turn = agent._result_to_turn(
        _agentic_result({"_parse_error": "Expecting value", "_raw_block": "{oops"})
    )
    assert turn.status == "working"
    assert turn.actions[0]["type"] == "no_deliverable"
    assert turn.actions[0]["kind"] == "parse_error"
    assert turn.actions[0]["error"] == "Expecting value"
    assert "tail of response" in turn.actions[0]["raw_tail"]


def test_missing_deliverable_becomes_no_deliverable_action():
    agent = Agent(AgentState(name="be-1", role=Role.BACKEND), MockBackend())
    turn = agent._result_to_turn(_agentic_result({}))
    assert turn.actions[0]["type"] == "no_deliverable"
    assert turn.actions[0]["kind"] == "missing"


# ---- orchestrator counter, correction, escalation, give-up --------------------


def _no_deliv_turn(kind: str = "missing") -> AgentTurn:
    action: dict[str, Any] = {
        "type": "no_deliverable",
        "kind": kind,
        "raw_tail": "…I think I did the work somewhere…",
    }
    if kind == "parse_error":
        action["error"] = "Expecting value: line 1 column 2"
    return AgentTurn(status="working", actions=[action])


def _orch_with_squad(tmp_workspace, events):
    orch = Orchestrator(
        backend=MockBackend(),
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch._spawn_agent(Role.TECH_LEAD, "tl-1", "")
    orch._spawn_agent(Role.BACKEND, "be-1", "", manager="tl-1")
    return orch


def test_counter_increments_and_corrective_blocker_quotes_format(
    tmp_workspace, event_recorder
):
    on_event, events = event_recorder()
    orch = _orch_with_squad(tmp_workspace, events)
    be = orch.agents["be-1"]

    orch._apply_turn(be, _no_deliv_turn("parse_error"))
    assert be.state.consecutive_no_deliverable == 1
    assert [p for k, p in events if k == "deliverable_parse_error"]

    assert NO_DELIVERABLE_CORRECT_AT == 2
    orch._apply_turn(be, _no_deliv_turn("parse_error"))
    assert be.state.consecutive_no_deliverable == 2
    assert [p for k, p in events if k == "deliverable_format_corrected"]
    blockers = [m for m in be.state.inbox if m.msg_type == "blocker"]
    assert blockers, "corrective blocker not delivered"
    assert DELIVERABLE_FORMAT_REMINDER in blockers[-1].body
    assert "I think I did the work" in blockers[-1].body  # quotes the raw tail


def test_coherent_turn_resets_counter(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch_with_squad(tmp_workspace, events)
    be = orch.agents["be-1"]
    orch._apply_turn(be, _no_deliv_turn())
    assert be.state.consecutive_no_deliverable == 1
    orch._apply_turn(
        be, AgentTurn(status="working", actions=[{"type": "note", "body": "ok"}])
    )
    assert be.state.consecutive_no_deliverable == 0


def test_escalates_to_manager_at_threshold(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch_with_squad(tmp_workspace, events)
    be = orch.agents["be-1"]
    for _ in range(NO_DELIVERABLE_ESCALATE_AT):
        orch._apply_turn(be, _no_deliv_turn())
    assert [p for k, p in events if k == "no_deliverable_escalated"]
    tl_inbox = orch.world.agents["tl-1"].inbox
    assert any(
        m.msg_type == "blocker" and "no deliverable" in m.subject for m in tl_inbox
    )


def test_giveup_force_completes(tmp_workspace, event_recorder):
    on_event, events = event_recorder()
    orch = _orch_with_squad(tmp_workspace, events)
    be = orch.agents["be-1"]
    for _ in range(NO_DELIVERABLE_GIVEUP_AT):
        orch._apply_turn(be, _no_deliv_turn())
    assert be.state.status == "complete"
    given_up = [p for k, p in events if k == "agent_given_up"]
    assert given_up and given_up[-1]["reason"] == "no_deliverable"


# ---- persistence of the new AgentState fields ----------------------------------


def test_new_agent_state_fields_round_trip():
    s = AgentState(
        name="be-1",
        role=Role.BACKEND,
        manager="tl-1",
        brief="own the API",
        idle_ticks=2,
        consecutive_no_deliverable=3,
        unanswered_escalations=1,
        verify_failures={"path_exists:abc": 2},
        hold_until_tick=9,
    )
    restored = _agent_state_from_dict(asdict(s))
    assert restored.manager == "tl-1"
    assert restored.brief == "own the API"
    assert restored.idle_ticks == 2
    assert restored.consecutive_no_deliverable == 3
    assert restored.unanswered_escalations == 1
    assert restored.verify_failures == {"path_exists:abc": 2}
    # Holds are evaluation-counter relative; the counter resets on resume.
    assert restored.hold_until_tick is None


def test_legacy_snapshot_defaults_new_fields():
    legacy = {"name": "tl-1", "role": "tech_lead", "status": "working"}
    restored = _agent_state_from_dict(legacy)
    assert restored.manager is None
    assert restored.brief == ""
    assert restored.idle_ticks == 0
    assert restored.consecutive_no_deliverable == 0
    assert restored.unanswered_escalations == 0
    assert restored.verify_failures == {}
    assert restored.hold_until_tick is None
