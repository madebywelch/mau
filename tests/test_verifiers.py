"""Task 2: Verifier registry + `verify` action dispatch through the orchestrator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mau_cli.schemas import AgentTurn, Role, Workspace
from mau_cli.verifiers import (
    ParseContractVerifier,
    PathExistsVerifier,
    RunCommandVerifier,
    VERIFIERS,
)


# ---- registry shape ---------------------------------------------------------


def test_verifiers_registry_contents():
    assert set(VERIFIERS.keys()) == {"path_exists", "run_command", "parse_contract"}
    assert isinstance(VERIFIERS["path_exists"], PathExistsVerifier)
    assert isinstance(VERIFIERS["run_command"], RunCommandVerifier)
    assert isinstance(VERIFIERS["parse_contract"], ParseContractVerifier)


# ---- path_exists ------------------------------------------------------------


def test_path_exists_all_present(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    res = PathExistsVerifier().run({"paths": ["a.txt", "b.txt"]}, tmp_path)
    assert res.ok is True
    assert "2" in res.summary or "all" in res.summary.lower()


def test_path_exists_one_missing(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    res = PathExistsVerifier().run({"paths": ["a.txt", "missing.txt"]}, tmp_path)
    assert res.ok is False
    assert "missing.txt" in res.summary
    assert res.details["missing"] == ["missing.txt"]


# ---- run_command ------------------------------------------------------------


def test_run_command_echo_ok(tmp_path):
    res = RunCommandVerifier().run({"command": "echo hello"}, tmp_path)
    assert res.ok is True
    assert "hello" in res.details["stdout_tail"]
    assert res.details["exit"] == 0


def test_run_command_false_exit(tmp_path):
    res = RunCommandVerifier().run({"command": "false"}, tmp_path)
    assert res.ok is False
    assert res.details["exit"] != 0


def test_run_command_timeout(tmp_path):
    res = RunCommandVerifier().run(
        {"command": "sleep 5", "timeout_seconds": 1}, tmp_path
    )
    assert res.ok is False
    assert "timed out" in res.summary.lower()


def test_run_command_retries_python3_on_127(tmp_path, monkeypatch):
    """Exit 127 with `python` as the first token retries once with `python3`.

    Deterministic via PATH manipulation: we point PATH at a directory
    containing only a `python3` shim and verify both that the bare `python`
    command fails (127) and that the verifier transparently retries.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Minimal `python3` shim that prints "3" and exits 0 — enough to verify
    # the substituted command succeeds.
    py3 = bin_dir / "python3"
    py3.write_text("#!/bin/sh\necho 3\nexit 0\n")
    py3.chmod(0o755)
    # Pin PATH so `python` is genuinely missing and `python3` is found.
    monkeypatch.setenv("PATH", str(bin_dir))

    res = RunCommandVerifier().run({"command": "python --version"}, tmp_path)
    assert res.ok is True, f"retry should have promoted to python3; got {res}"
    assert res.details.get("substituted_python3") is True
    assert res.details.get("substituted_from") == "python --version"
    assert res.details.get("substituted_to") == "python3 --version"


def test_run_command_does_not_substitute_non_python(tmp_path):
    """Only `python`/`pip` get retried — other 127s must not be rewritten."""
    res = RunCommandVerifier().run(
        {"command": "definitely-nonexistent-binary-xyzzy"}, tmp_path
    )
    assert res.ok is False
    # Must NOT have attempted any substitution.
    assert "substituted_python3" not in res.details
    assert "substituted_to" not in res.details


# ---- parse_contract ---------------------------------------------------------


def test_parse_contract_valid_python(tmp_path):
    f = tmp_path / "good.py"
    f.write_text("x = 1\n")
    res = ParseContractVerifier().run({"path": "good.py"}, tmp_path)
    assert res.ok is True


def test_parse_contract_broken_python(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def x(:\n")
    res = ParseContractVerifier().run({"path": "bad.py"}, tmp_path)
    assert res.ok is False
    assert "syntaxerror" in res.summary.lower()


def test_parse_contract_valid_json(tmp_path):
    f = tmp_path / "good.json"
    f.write_text('{"a": 1}')
    res = ParseContractVerifier().run({"path": "good.json"}, tmp_path)
    assert res.ok is True


def test_parse_contract_broken_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text('{"a":')
    res = ParseContractVerifier().run({"path": "bad.json"}, tmp_path)
    assert res.ok is False
    assert "jsondecode" in res.summary.lower() or "json" in res.summary.lower()


def test_parse_contract_yaml_skipped_without_pyyaml(tmp_path):
    """yaml/yml files are skipped (ok=True) when PyYAML isn't importable."""
    if "yaml" in sys.modules:
        pytest.skip("PyYAML is installed in this environment")
    try:
        import yaml  # noqa: F401
        pytest.skip("PyYAML is installed in this environment")
    except ImportError:
        pass
    f = tmp_path / "anything.yaml"
    f.write_text("a: 1\n")
    res = ParseContractVerifier().run({"path": "anything.yaml"}, tmp_path)
    assert res.ok is True
    assert "skipped" in res.summary.lower()


# ---- orchestrator dispatch --------------------------------------------------


def test_verify_action_dispatch_passed(mock_orchestrator, tmp_workspace, event_recorder):
    """A `verify` action that succeeds emits `verify_passed`."""
    on_event, captured = event_recorder()
    orch = mock_orchestrator(on_event=on_event)

    # Ensure the isolation backend is initialized (otherwise verify_skipped).
    orch._ensure_isolation()
    agent = orch._spawn_agent(Role.QA, "qa-1", "")
    # Create the file we'll verify, so PathExistsVerifier passes.
    (Path(tmp_workspace.code_dir) / "exists.txt").write_text("hi")

    captured.clear()
    orch._apply_action(
        agent,
        {
            "type": "verify",
            "verifier": "path_exists",
            "spec": {"paths": ["exists.txt"]},
        },
    )
    kinds = [k for k, _ in captured]
    assert "verify_passed" in kinds
    assert agent.state.name not in orch._rejected_this_turn


def test_verify_action_dispatch_failed(mock_orchestrator, tmp_workspace, event_recorder):
    """A failing `verify` emits `verify_failed` and rejects the agent's turn."""
    on_event, captured = event_recorder()
    orch = mock_orchestrator(on_event=on_event)

    orch._ensure_isolation()
    agent = orch._spawn_agent(Role.QA, "qa-2", "")

    captured.clear()
    orch._apply_action(
        agent,
        {
            "type": "verify",
            "verifier": "path_exists",
            "spec": {"paths": ["definitely-missing.txt"]},
        },
    )
    kinds = [k for k, _ in captured]
    assert "verify_failed" in kinds
    assert agent.state.name in orch._rejected_this_turn
