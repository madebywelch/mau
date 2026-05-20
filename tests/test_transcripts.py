"""Task 1: per-agent JSONL transcripts under <workspace>/logs/."""

from __future__ import annotations

import json
from pathlib import Path

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import (
    AgentState,
    AgentTurn,
    Role,
    Workspace,
)


REQUIRED_KEYS = {
    "turn",
    "agent",
    "role",
    "timestamp",
    "prompt",
    "response",
    "tokens",
    "files_touched",
    "accepted",
    "status",
    "backend",
    "duration_ms",
    "worktree_path",
    "isolation",
}


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_jsonl_files_written_per_agent(mock_orchestrator, tmp_workspace):
    orch = mock_orchestrator()
    orch.run("ship a hello world feature")

    logs_dir = Path(tmp_workspace.logs_dir)
    assert logs_dir.exists()
    agent_logs = sorted(logs_dir.glob("*.jsonl"))
    assert len(agent_logs) >= 1, "expected at least one agent transcript"

    # Each agent that recorded a turn should have a file named after them.
    for agent_name, agent in orch.agents.items():
        if agent.last_result is None:
            continue
        target = logs_dir / f"{agent_name}.jsonl"
        assert target.exists(), f"no transcript for {agent_name} at {target}"


def test_each_transcript_line_has_required_keys(mock_orchestrator, tmp_workspace):
    orch = mock_orchestrator()
    orch.run("ship a hello world feature")

    logs_dir = Path(tmp_workspace.logs_dir)
    for log in logs_dir.glob("*.jsonl"):
        records = _read_jsonl(log)
        assert records, f"empty transcript {log}"
        for rec in records:
            missing = REQUIRED_KEYS - set(rec.keys())
            assert not missing, (
                f"{log.name} record missing keys: {sorted(missing)} — got {sorted(rec.keys())}"
            )
            # tokens is a sub-dict; sanity-check its shape too.
            assert isinstance(rec["tokens"], dict)


def test_rejected_turn_marked_accepted_false(tmp_workspace):
    """Force the rejection bit and confirm the JSONL line records accepted=False."""
    backend = MockBackend()
    orch = Orchestrator(backend=backend, workspace=tmp_workspace, isolation="shared")
    # Spawn an agent directly so we can run a hand-built turn through the logger.
    agent = orch._spawn_agent(Role.QA, "qa-test", "")
    # The transcript writer reads `last_result` / `last_prompt`, so simulate
    # an inference call.
    result = backend.call_plan("ROLE: QA", "AGENT_NAME: qa-test\nROLE: qa\n")
    agent.last_result = result
    agent.last_prompt = "synthetic prompt"

    orch._rejected_this_turn.add(agent.state.name)
    turn = AgentTurn(thoughts="t", status="working", actions=[])
    orch._log_transcript(agent, turn, accepted=False)

    records = _read_jsonl(Path(tmp_workspace.logs_dir) / "qa-test.jsonl")
    assert records, "expected at least one transcript line"
    assert records[-1]["accepted"] is False


def test_backend_and_isolation_fields_match(mock_orchestrator, tmp_workspace):
    orch = mock_orchestrator()
    orch.run("ship a hello world feature")

    logs_dir = Path(tmp_workspace.logs_dir)
    any_records = False
    for log in logs_dir.glob("*.jsonl"):
        for rec in _read_jsonl(log):
            any_records = True
            assert rec["backend"] == "mock"
            assert rec["isolation"] in {"shared", "worktree"}
    assert any_records, "expected at least one transcript record"
