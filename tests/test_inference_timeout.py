"""Regression: subprocess timeout must kill the whole process tree.

Bug 6: `subprocess.run(timeout=...)` only kills the immediate child; MCP-server
grandchildren keep their pipes open and the call hangs indefinitely. We now run
the CLI in a fresh process group via `start_new_session=True` and SIGTERM /
SIGKILL the group on `TimeoutExpired`. These tests cover:

1. `_run_with_group_kill` actually reaps grandchildren by PID.
2. Plan-mode command includes `--no-session-persistence` and does NOT cap
   `--max-turns` (opus can need >1 internal iteration even without tools;
   self-imposed caps cause deterministic `error_max_turns` failures).
3. Agentic-mode command includes `--no-session-persistence`.
4. Happy-path (exit-0 + valid envelope) still returns through the new helper.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mau_cli.inference import (
    CLAUDE_INVOKE_TIMEOUT_SECONDS,
    ClaudeCLIBackend,
    _run_with_group_kill,
)


# ---- group-kill reaps grandchildren ---------------------------------------


def _pid_alive(pid: int) -> bool:
    """True if `pid` is a live process. signal 0 = existence check."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours; on macOS/Linux this is rare for child
        # PIDs of the test runner but we treat it as "still around".
        return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
def test_group_kill_reaps_grandchildren(tmp_path: Path) -> None:
    """A parent that spawns a long-sleeping grandchild and itself sleeps
    forever must, after timeout, have BOTH PIDs gone. This is the core
    regression: `subprocess.run(timeout=...)` would only kill the parent."""
    grand_pid_path = tmp_path / "grand.pid"
    parent_pid_path = tmp_path / "parent.pid"
    script = tmp_path / "p.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {parent_pid_path}\n"
        # Background a long-running grandchild and record its PID before we
        # block. `disown` would daemonize it; we want it in the same group so
        # group-kill catches it.
        "sleep 120 &\n"
        f"echo $! > {grand_pid_path}\n"
        "wait\n"
    )
    script.chmod(0o755)

    with pytest.raises(RuntimeError, match="timed out"):
        _run_with_group_kill(
            [str(script)],
            cwd=None,
            env=None,
            timeout=1,  # fires fast; the script will still be sleeping
        )

    # Both PIDs must have been written before SIGTERM. Read them now.
    parent_pid = int(parent_pid_path.read_text().strip())
    grand_pid = int(grand_pid_path.read_text().strip())

    # Give the group-kill a brief moment to reap. We already SIGTERM/SIGKILL
    # synchronously inside the helper, so this is mostly belt-and-braces.
    import time as _t

    for _ in range(20):
        if not _pid_alive(parent_pid) and not _pid_alive(grand_pid):
            break
        _t.sleep(0.05)

    assert not _pid_alive(parent_pid), f"parent PID {parent_pid} still alive after group-kill"
    assert not _pid_alive(grand_pid), (
        f"grandchild PID {grand_pid} still alive after group-kill — "
        "subprocess.run-style cleanup would leave it behind"
    )


# ---- new flags wired through to the command -------------------------------


def _capture_popen_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch `_run_with_group_kill` to record argv and return a clean envelope."""
    seen: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any):
        seen.append(list(cmd))
        proc = MagicMock()
        proc.returncode = 0
        envelope = json.dumps(
            {
                "result": '{"thoughts":"ok","status":"complete","actions":[]}',
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "total_cost_usd": 0.0,
                "iterations": [{"step": 1}],
                "modelUsage": {"model": "x"},
            }
        )
        return proc, envelope, ""

    monkeypatch.setattr("mau_cli.inference._run_with_group_kill", _fake_run)
    return seen


def test_call_plan_passes_no_session_persistence_and_no_self_imposed_turn_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan mode skips session-state writes to avoid lock contention under
    parallelism. It must NOT pass `--max-turns 1`: opus' adaptive thinking
    can produce multi-iteration plan responses, and a self-imposed cap
    triggers deterministic `error_max_turns` failures (regression: the
    original Bug 6 fix shipped this cap, $20 prod run rejected every plan
    turn with `error_max_turns` until the cap was removed)."""
    seen = _capture_popen_argv(monkeypatch)
    backend = ClaudeCLIBackend()
    backend.call_plan("sys", "user prompt")

    assert len(seen) == 1
    cmd = seen[0]
    assert "--no-session-persistence" in cmd
    # No self-imposed `--max-turns` cap in plan mode.
    assert "--max-turns" not in cmd, (
        "plan-mode commands must not carry a --max-turns cap: opus needs "
        "headroom for internal thinking iterations"
    )


def test_call_agentic_passes_no_session_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agentic mode also avoids session-file lock contention. We don't cap
    turns because specialists legitimately iterate with tools."""
    seen = _capture_popen_argv(monkeypatch)
    backend = ClaudeCLIBackend()
    backend.call_agentic("sys", "u", workspace_dir="/tmp")

    assert len(seen) == 1
    cmd = seen[0]
    assert "--no-session-persistence" in cmd
    # Agentic mode must NOT cap turns at 1.
    if "--max-turns" in cmd:
        assert cmd[cmd.index("--max-turns") + 1] != "1"


# ---- happy path still works through the new helper ------------------------


def test_invoke_happy_path_returns_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit-0 with a valid JSON envelope must round-trip cleanly. This is the
    bread-and-butter case; if the Popen rewrite broke it nothing would work."""
    proc = MagicMock()
    proc.returncode = 0
    envelope_text = json.dumps(
        {
            "result": '{"thoughts":"ok","status":"complete","actions":[]}',
            "usage": {"input_tokens": 7, "output_tokens": 3},
            "total_cost_usd": 0.01,
            "iterations": [{"step": 1}],
            "modelUsage": {"model": "claude-sonnet"},
        }
    )
    monkeypatch.setattr(
        "mau_cli.inference._run_with_group_kill",
        lambda cmd, **kw: (proc, envelope_text, ""),
    )

    backend = ClaudeCLIBackend()
    envelope, raw, duration_ms = backend._invoke(["claude"])

    assert envelope["modelUsage"]["model"] == "claude-sonnet"
    assert isinstance(duration_ms, int)


# ---- timeout constant is the documented value -----------------------------


def test_timeout_constant_is_30_minutes() -> None:
    """The bumped timeout is 1800s (30 min). Locking this down so future
    changes are deliberate — too low and we'll start killing real long
    agentic runs."""
    assert CLAUDE_INVOKE_TIMEOUT_SECONDS == 1800
