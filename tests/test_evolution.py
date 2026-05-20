"""Task 7: Evolution Agent — summarize/propose + RegressionSuite."""

from __future__ import annotations

import json
from pathlib import Path

from mau_cli.evolution import (
    EvolutionAgent,
    HarnessProposal,
    RegressionSuite,
    TranscriptSummary,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_record(
    *,
    agent: str,
    role: str,
    turn: int,
    accepted: bool = True,
    duration_ms: int = 100,
    response: str = "ok",
    prompt: str = "p",
    files: list[str] | None = None,
    isolation: str = "shared",
) -> dict:
    return {
        "turn": turn,
        "agent": agent,
        "role": role,
        "timestamp": 1000.0 + turn,
        "prompt": prompt,
        "response": response,
        "tokens": {"input": 100, "output": 50, "cost_usd": 0.0},
        "files_touched": list(files or []),
        "accepted": accepted,
        "status": "complete" if accepted else "blocked",
        "backend": "mock",
        "duration_ms": duration_ms,
        "worktree_path": None,
        "isolation": isolation,
    }


# ---- summarize -------------------------------------------------------------


def test_summarize_returns_per_agent_summaries(tmp_path):
    logs = tmp_path / "logs"

    # High-rejection agent (5 turns, 4 rejected = 80%).
    _write_jsonl(
        logs / "fe-1.jsonl",
        [
            _make_record(
                agent="fe-1", role="frontend", turn=i,
                accepted=(i == 0),
                response="missing required field 'title' in payload",
            )
            for i in range(5)
        ],
    )
    # Normal agent.
    _write_jsonl(
        logs / "be-1.jsonl",
        [
            _make_record(agent="be-1", role="backend", turn=i, accepted=True)
            for i in range(5)
        ],
    )
    # Duration outlier (very high duration_ms, all accepted).
    _write_jsonl(
        logs / "db-1.jsonl",
        [
            _make_record(
                agent="db-1", role="database", turn=i,
                accepted=True, duration_ms=10_000,
            )
            for i in range(5)
        ],
    )

    ev = EvolutionAgent(logs_dir=logs, prompts_dir=tmp_path / "prompts")
    summaries = ev.summarize()
    assert set(summaries.keys()) == {"fe-1", "be-1", "db-1"}

    fe = summaries["fe-1"]
    assert isinstance(fe, TranscriptSummary)
    assert fe.total_turns == 5
    assert fe.accepted_turns == 1
    assert fe.rejected_turns == 4

    be = summaries["be-1"]
    assert be.total_turns == 5
    assert be.accepted_turns == 5
    assert be.rejected_turns == 0


# ---- propose --------------------------------------------------------------


def test_propose_fires_on_rejection_and_duration_signals(tmp_path):
    logs = tmp_path / "logs"
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "frontend.md").write_text("# Frontend role prompt\n")

    # 5 turns, 4 rejected — over 40% threshold, ≥ 5 turns.
    _write_jsonl(
        logs / "fe-1.jsonl",
        [
            _make_record(
                agent="fe-1", role="frontend", turn=i,
                accepted=(i == 0),
                response="deliverable rejected: missing acceptance file",
                duration_ms=200,
            )
            for i in range(5)
        ],
    )
    # Normal backend (baseline for the median).
    _write_jsonl(
        logs / "be-1.jsonl",
        [
            _make_record(agent="be-1", role="backend", turn=i,
                         accepted=True, duration_ms=200)
            for i in range(5)
        ],
    )
    # Duration outlier — two slow database agents so the proposer can cite
    # ≥ 2 of them (the proposer emits 1 citation per role-summary, capped
    # at EVIDENCE_PER_PROPOSAL).
    for name in ("db-1", "db-2"):
        _write_jsonl(
            logs / f"{name}.jsonl",
            [
                _make_record(agent=name, role="database", turn=i,
                             accepted=True, duration_ms=10_000)
                for i in range(5)
            ],
        )

    ev = EvolutionAgent(logs_dir=logs, prompts_dir=prompts)
    proposals = ev.propose()

    kinds_targets = [(p.kind, p.target) for p in proposals]
    assert any(
        kind == "prompt_edit" and "frontend" in target
        for kind, target in kinds_targets
    ), f"expected prompt_edit for frontend; got {kinds_targets}"
    assert any(
        kind == "default_change" for kind, _ in kinds_targets
    ), f"expected default_change for duration outlier; got {kinds_targets}"

    # Every proposal should carry ≥ 2 evidence citations in the documented
    # form `logs/<agent>.jsonl:turn=<N>`. The rejection proposer cites two
    # rejected turns from the offending agent; the duration proposer cites
    # one summary per agent in the outlier role.
    for p in proposals:
        assert len(p.evidence) >= 2, (
            f"{p.id}: only {len(p.evidence)} evidence citations"
        )
        for cite in p.evidence:
            assert cite.startswith("logs/") and ".jsonl:turn=" in cite, cite


# ---- RegressionSuite -------------------------------------------------------


def test_regression_suite_runs_against_bundled_fixtures():
    suite = RegressionSuite()
    verdicts = suite.run()
    assert verdicts, "expected at least one bundled fixture"
    for v in verdicts:
        assert v.passed, (
            f"fixture {v.fixture} failed: completion={v.stopped_on_completion} "
            f"cap={v.stopped_on_turn_cap} notes={v.notes}"
        )
        assert v.stopped_on_completion is True
        assert v.stopped_on_turn_cap is False
