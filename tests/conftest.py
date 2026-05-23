"""Shared pytest fixtures for the MAU-CLI regression suite.

Tests stay focused on data structures and the deterministic mock backend so
nothing here touches the network or shells out to a real LLM CLI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import Workspace


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Workspace:
    """A vanilla greenfield Workspace rooted at `tmp_path`."""
    ws = Workspace(root=str(tmp_path))
    ws.ensure()
    return ws


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Tiny helper: run git inside `cwd` with a deterministic identity so
    `git commit` works even on hosts without a global git config."""
    env = {
        "GIT_AUTHOR_NAME": "mau-test",
        "GIT_AUTHOR_EMAIL": "mau-test@example.com",
        "GIT_COMMITTER_NAME": "mau-test",
        "GIT_COMMITTER_EMAIL": "mau-test@example.com",
        # Don't let the host's commit hooks fire under the test runner.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def tmp_git_workspace(tmp_path: Path) -> Workspace:
    """A Workspace whose `code_dir` is a git repo with one commit, so
    `GitWorktreeBackend` has something to base linked worktrees off."""
    ws = Workspace(root=str(tmp_path))
    ws.ensure()
    code = Path(ws.code_dir)
    # Initial commit so worktree add --detach HEAD works.
    _git(code, "init", "-q", "-b", "main")
    seed = code / "README.md"
    seed.write_text("seed\n", encoding="utf-8")
    _git(code, "add", "README.md")
    _git(code, "commit", "-q", "-m", "seed")
    return ws


@pytest.fixture
def mock_orchestrator(tmp_workspace: Workspace) -> Callable[..., Orchestrator]:
    """Factory that builds an Orchestrator wired to the deterministic mock
    backend. Tests usually want to override max_turns or capture events, so
    we return a factory rather than a constructed instance."""

    def _factory(
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        max_turns: int = 30,
        concurrency: int = 1,
        isolation: str = "shared",
        workspace: Workspace | None = None,
    ) -> Orchestrator:
        return Orchestrator(
            backend=MockBackend(),
            max_turns=max_turns,
            concurrency=concurrency,
            workspace=workspace or tmp_workspace,
            on_event=on_event,
            isolation=isolation,  # type: ignore[arg-type]
        )

    return _factory


@pytest.fixture
def event_recorder() -> Callable[[], tuple[Callable[[str, dict[str, Any]], None], list[tuple[str, dict[str, Any]]]]]:
    """Returns a factory that yields (on_event_callback, captured_events).
    Each call gives a fresh list so tests in the same module don't bleed."""

    def _make() -> tuple[Callable[[str, dict[str, Any]], None], list[tuple[str, dict[str, Any]]]]:
        captured: list[tuple[str, dict[str, Any]]] = []

        def _on_event(kind: str, payload: dict[str, Any]) -> None:
            captured.append((kind, payload))

        return _on_event, captured

    return _make
