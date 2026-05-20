"""Task 6: per-agent isolation backends — shared vs git worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mau_cli.isolation import (
    GitWorktreeBackend,
    SharedWorkspaceBackend,
    make_isolation_backend,
)


# ---- factory selection ------------------------------------------------------


def test_factory_non_git_falls_back_to_shared(tmp_workspace):
    """Auto mode against a non-git code_dir returns SharedWorkspaceBackend."""
    backend = make_isolation_backend(Path(tmp_workspace.code_dir), mode="auto")
    assert isinstance(backend, SharedWorkspaceBackend)


def test_factory_git_mode_worktree(tmp_git_workspace):
    backend = make_isolation_backend(
        Path(tmp_git_workspace.code_dir), mode="worktree"
    )
    assert isinstance(backend, GitWorktreeBackend)
    backend.cleanup()


# ---- GitWorktreeBackend behaviour ------------------------------------------


def test_worktree_acquire_per_agent(tmp_git_workspace):
    backend = make_isolation_backend(
        Path(tmp_git_workspace.code_dir), mode="worktree"
    )
    try:
        path_a = backend.acquire("agent-a")
        path_b = backend.acquire("agent-b")
        assert path_a != path_b
        assert path_a.exists()
        assert path_b.exists()
        # Both should live under .mau-worktrees/.
        assert ".mau-worktrees" in str(path_a)
        assert ".mau-worktrees" in str(path_b)
    finally:
        backend.cleanup()


def test_worktree_release_merge_true_overlays(tmp_git_workspace):
    backend = make_isolation_backend(
        Path(tmp_git_workspace.code_dir), mode="worktree"
    )
    try:
        worktree = backend.acquire("agent-a")
        (worktree / "merged.txt").write_text("hello")
        backend.release("agent-a", merge=True)

        target = Path(tmp_git_workspace.code_dir) / "merged.txt"
        assert target.exists()
        assert target.read_text() == "hello"
    finally:
        backend.cleanup()


def test_worktree_release_merge_false_discards(tmp_git_workspace):
    backend = make_isolation_backend(
        Path(tmp_git_workspace.code_dir), mode="worktree"
    )
    try:
        worktree = backend.acquire("agent-b")
        (worktree / "discarded.txt").write_text("nope")
        backend.release("agent-b", merge=False)

        main = Path(tmp_git_workspace.code_dir) / "discarded.txt"
        assert not main.exists()
    finally:
        backend.cleanup()


def test_worktree_same_role_stomp_emits_overwrote(tmp_git_workspace):
    """Two same-role agents writing the same path: the second merge stomps
    the first and the backend emits `worktree_merge_overwrote`."""
    events: list[tuple[str, dict]] = []

    backend = make_isolation_backend(
        Path(tmp_git_workspace.code_dir),
        mode="worktree",
        emit=lambda k, p: events.append((k, p)),
    )
    try:
        wt_a = backend.acquire("fe-1")
        wt_b = backend.acquire("fe-2")

        (wt_a / "shared.tsx").write_text("// fe-1\n")
        (wt_b / "shared.tsx").write_text("// fe-2\n")

        # First merge — fe-1 writes first.
        backend.release("fe-1", merge=True)
        # Second merge — fe-2 overwrites. The dst file's mtime is now newer
        # than fe-2's baseline (captured when fe-2 acquired), so the backend
        # must flag the overwrite.
        backend.release("fe-2", merge=True)

        overwrote = [p for k, p in events if k == "worktree_merge_overwrote"]
        assert overwrote, f"expected worktree_merge_overwrote, got {[k for k,_ in events]}"
        # The clobbered file should be in the listing.
        assert any("shared.tsx" in f for f in overwrote[-1]["files"])
    finally:
        backend.cleanup()


# ---- brownfield + dirty repo -----------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "mau-test",
        "GIT_AUTHOR_EMAIL": "mau-test@example.com",
        "GIT_COMMITTER_NAME": "mau-test",
        "GIT_COMMITTER_EMAIL": "mau-test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, check=True, capture_output=True
    )


def test_brownfield_dirty_repo_falls_back_to_shared(tmp_git_workspace):
    """Brownfield + uncommitted changes → factory refuses worktree and
    falls back to shared, emitting `worktree_disabled`."""
    # Make the repo dirty: add an untracked file.
    (Path(tmp_git_workspace.code_dir) / "untracked.txt").write_text("dirty")

    events: list[tuple[str, dict]] = []
    backend = make_isolation_backend(
        Path(tmp_git_workspace.code_dir),
        mode="auto",
        brownfield=True,
        emit=lambda k, p: events.append((k, p)),
    )
    assert isinstance(backend, SharedWorkspaceBackend)
    assert any(k == "worktree_disabled" for k, _ in events)
