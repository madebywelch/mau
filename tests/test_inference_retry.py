"""Regression: ClaudeCLIBackend retries transient empty-envelope exit-1 flakes.

Live-run pattern: claude returns exit=1 with a parseable envelope but no
actual model output (iterations: [], modelUsage: {}). These are transient
and clear on retry. Real prompt errors (where the envelope contains
substantive content, or where stdout isn't a valid envelope at all) must
surface immediately.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mau_cli.inference import (
    CLAUDE_RETRY_BACKOFF_SECONDS,
    ClaudeCLIBackend,
    CodexCLIBackend,
)


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> tuple[Any, str, str]:
    """Return the (proc, stdout, stderr) shape that `_run_with_group_kill`
    produces. Only `returncode` matters on the proc itself."""
    fake_proc = MagicMock()
    fake_proc.returncode = returncode
    return (fake_proc, stdout, stderr)


def _transient_envelope() -> dict[str, Any]:
    return {
        "iterations": [],
        "modelUsage": {},
        "permission_denials": [],
        "terminal_reason": "completed",
        "uuid": "abc-123",
    }


def _good_envelope(result: str = '{"thoughts":"ok","status":"complete","actions":[]}') -> dict[str, Any]:
    return {
        "result": result,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "total_cost_usd": 0.0001,
        "iterations": [{"step": 1}],
        "modelUsage": {"model": "claude-sonnet"},
    }


# ---- Bug 4: claude transient flake retried --------------------------------


def test_claude_retries_then_succeeds_on_transient_envelope():
    """Two transient exit-1 envelopes followed by a clean success → caller
    sees the third envelope, no exception."""
    bad = _proc(1, stdout=json.dumps(_transient_envelope()))
    good = _proc(0, stdout=json.dumps(_good_envelope()))

    backend = ClaudeCLIBackend()
    with patch(
        "mau_cli.inference._run_with_group_kill",
        side_effect=[bad, bad, good],
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        envelope, raw, _ = backend._invoke(["claude"])

    assert envelope.get("result")
    assert envelope.get("modelUsage", {}).get("model") == "claude-sonnet"
    assert run_mock.call_count == 3


def test_claude_on_retry_callback_invoked():
    """Optional on_retry hook fires once per retry with the attempt index."""
    bad = _proc(1, stdout=json.dumps(_transient_envelope()))
    good = _proc(0, stdout=json.dumps(_good_envelope()))

    seen: list[dict[str, Any]] = []
    backend = ClaudeCLIBackend(on_retry=lambda payload: seen.append(payload))
    with patch(
        "mau_cli.inference._run_with_group_kill", side_effect=[bad, good]
    ), patch("mau_cli.inference._time.sleep"):
        backend._invoke(["claude"])

    assert len(seen) == 1
    assert seen[0]["attempt"] == 0
    assert seen[0]["backend"] == "claude"
    assert "iterations" in seen[0]["envelope_keys"]


def test_claude_does_not_retry_on_non_transient_exit_1():
    """A non-transient exit-1 envelope (e.g. iterations populated) raises
    immediately. The cleaner formatted message surfaces envelope context."""
    non_transient = {
        "result": "your prompt was malformed",
        "iterations": [{"step": 1}],
        "modelUsage": {"model": "claude-sonnet"},
        "is_error": False,
    }
    bad = _proc(1, stdout=json.dumps(non_transient), stderr="bad flag")

    backend = ClaudeCLIBackend()
    with patch(
        "mau_cli.inference._run_with_group_kill", side_effect=[bad]
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        with pytest.raises(RuntimeError) as exc:
            backend._invoke(["claude"])

    assert run_mock.call_count == 1
    # Cleaner message: surfaces envelope fields rather than raw stdout dump.
    assert "envelope=" in str(exc.value)
    assert "claude CLI exit=1" in str(exc.value)


def test_claude_does_not_retry_when_stdout_is_not_json():
    """Exit=1 with unparseable stdout → real failure; surface immediately."""
    bad = _proc(1, stdout="oops not json", stderr="something exploded")

    backend = ClaudeCLIBackend()
    with patch(
        "mau_cli.inference._run_with_group_kill", side_effect=[bad]
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        with pytest.raises(RuntimeError) as exc:
            backend._invoke(["claude"])

    assert run_mock.call_count == 1
    assert "stdout_tail=" in str(exc.value)


def test_claude_exhausts_retries_when_always_transient():
    """All 3 attempts return a transient envelope → exhausted-retries
    RuntimeError mentioning the envelope context."""
    bad = _proc(1, stdout=json.dumps(_transient_envelope()))

    backend = ClaudeCLIBackend()
    expected_attempts = 1 + len(CLAUDE_RETRY_BACKOFF_SECONDS)
    with patch(
        "mau_cli.inference._run_with_group_kill",
        side_effect=[bad] * expected_attempts,
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        with pytest.raises(RuntimeError) as exc:
            backend._invoke(["claude"])

    assert run_mock.call_count == expected_attempts
    assert "exhausted retries" in str(exc.value)
    assert "envelope=" in str(exc.value)


def test_claude_retries_on_explicit_error_subtype():
    """Envelope with is_error + subtype=error_during_execution is also
    transient even if iterations weren't empty."""
    transient_subtype = {
        "is_error": True,
        "subtype": "error_during_execution",
        "iterations": [{"step": 1}],
        "modelUsage": {"model": "x"},
    }
    bad = _proc(1, stdout=json.dumps(transient_subtype))
    good = _proc(0, stdout=json.dumps(_good_envelope()))

    backend = ClaudeCLIBackend()
    with patch(
        "mau_cli.inference._run_with_group_kill", side_effect=[bad, good]
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        envelope, _, _ = backend._invoke(["claude"])

    assert run_mock.call_count == 2
    assert envelope.get("result")


def test_claude_does_not_retry_on_error_max_turns():
    """`error_max_turns` is deterministic — when it comes from our own argv
    cap it would just fail again on retry; when it comes from claude's
    default cap the prompt itself drives unbounded iteration and retrying
    burns budget. Regression: an earlier Bug 6 fix put this subtype on the
    retry list, and a real $20 production run exhausted retries on every
    plan turn until it was removed."""
    not_transient = {
        "is_error": True,
        "subtype": "error_max_turns",
        "terminal_reason": "max_turns",
        "iterations": [{"step": 1}],
        "modelUsage": {"model": "x"},
    }
    bad = _proc(1, stdout=json.dumps(not_transient))

    backend = ClaudeCLIBackend()
    with patch(
        "mau_cli.inference._run_with_group_kill", side_effect=[bad]
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        with pytest.raises(RuntimeError):
            backend._invoke(["claude"])

    assert run_mock.call_count == 1, "must not retry error_max_turns"


# ---- Bug 4 (codex side): coarser stderr-marker retry ----------------------


def test_codex_retries_on_transient_stderr_then_succeeds():
    """codex exit=1 with stderr matching a transient marker retries once."""
    bad = _proc(1, stderr="error: connection timed out")
    good = _proc(0, stdout="hello\n<DELIVERABLE>{\"files_touched\":[]}</DELIVERABLE>")

    backend = CodexCLIBackend()
    with patch(
        "mau_cli.inference._run_with_group_kill", side_effect=[bad, good]
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        result = backend.call_agentic("sys", "u", workspace_dir="/tmp")

    assert run_mock.call_count == 2
    assert result.backend == "codex"


def test_codex_does_not_retry_on_real_error():
    """codex exit=1 with stderr that doesn't look transient (e.g. syntax
    error) raises immediately."""
    bad = _proc(1, stderr="syntax error in prompt: unexpected token")

    backend = CodexCLIBackend()
    with patch(
        "mau_cli.inference._run_with_group_kill", side_effect=[bad]
    ) as run_mock, patch("mau_cli.inference._time.sleep"):
        with pytest.raises(RuntimeError) as exc:
            backend.call_agentic("sys", "u", workspace_dir="/tmp")

    assert run_mock.call_count == 1
    assert "syntax error" in str(exc.value)
