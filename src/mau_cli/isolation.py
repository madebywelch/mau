"""Per-agent isolation backends.

Concurrency runs N agent turns in parallel via a thread pool. Each agentic-mode
turn is `claude -p` / `codex exec` with full Read/Write/Edit/Bash. If every
agent shares one cwd, two specialists can stomp each other's files mid-flight
and the verifier sees a contaminated tree.

This module supplies a tiny interface (`acquire` → per-agent dir, `release` →
merge-or-discard, `cleanup` → tear down) and two implementations:

- `SharedWorkspaceBackend` — current behaviour. Every agent uses the same dir.
- `GitWorktreeBackend` — `git worktree add .mau-worktrees/<agent>` per agent.
  Successful turns overlay-merge changed files back to the main workspace;
  rejections discard. Conflicts surface as a `worktree_merge_overwrote` event
  rather than auto-merging, since git merge-resolution requires judgement the
  orchestrator can't make in-band.

Selection is `auto` by default: probe with `git rev-parse --is-inside-work-tree`
and pick `GitWorktreeBackend` if the workspace is a git repo, else fall back
to shared. Brownfield repos with uncommitted changes refuse the worktree
backend (we'd lose the user's state on merge) and emit a warning.

Out of scope: submodules, LFS, sparse-checkout, mid-turn rebasing of the
main worktree. The intent is "make verification meaningful," not "ship a
production sandbox."
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol


IsolationMode = Literal["auto", "shared", "worktree"]
EventEmitter = Callable[[str, dict[str, Any]], None]


class IsolationBackend(Protocol):
    """Per-agent code directory provider."""

    name: str

    def acquire(self, agent_name: str) -> Path:
        """Return (creating if necessary) the per-agent code dir."""
        ...

    def release(
        self, agent_name: str, *, merge: bool
    ) -> Optional[list[Path]]:
        """Release the worktree taken by `acquire`. On `merge=True`, copy
        changed files back to the main workspace and return their paths
        (relative to the repo root). On `merge=False`, discard. Returns
        `None` when there is nothing to merge (shared backend, or no
        changes detected)."""
        ...

    def cleanup(self) -> None:
        """Best-effort teardown of any per-agent state."""
        ...


def _is_git_repo(path: Path) -> bool:
    if shutil.which("git") is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _has_uncommitted_changes(path: Path) -> bool:
    """Returns True if `git status --porcelain` reports anything. We use
    this as the safety gate in brownfield mode: if the user has dirty work,
    we refuse to layer worktree merges on top of it and fall back to shared
    mode so we don't clobber their edits."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if proc.returncode != 0:
        return True
    return bool(proc.stdout.strip())


def _git_toplevel(path: Path) -> Optional[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out) if out else None


class SharedWorkspaceBackend:
    """No-op backend — every agent shares `code_dir`. Matches pre-Task-6
    behaviour. Safe in single-agent runs and in non-git workspaces."""

    name = "shared"

    def __init__(self, code_dir: Path):
        self.code_dir = code_dir.resolve()

    def acquire(self, agent_name: str) -> Path:
        return self.code_dir

    def release(self, agent_name: str, *, merge: bool) -> Optional[list[Path]]:
        return None

    def cleanup(self) -> None:
        return None


class GitWorktreeBackend:
    """Per-agent `git worktree`. Lazily creates `.mau-worktrees/<agent>` the
    first time an agent acquires; subsequent acquires reuse the same dir so
    we amortize the linked-worktree setup cost across an agent's turns.

    Merge semantics on `release(merge=True)`:
      - Diff the worktree against the merge baseline (HEAD captured at the
        last release/acquire) to compute changed files.
      - For each changed file, copy it into the main workspace, overwriting.
      - Emit `worktree_merge_overwrote` when a target file's mtime is newer
        than the merge baseline — i.e. another agent's merge stomped first.
        This is informational; the later merge still wins (no rebase).
      - Refresh the merge baseline so the next turn starts from a clean diff.

    On `release(merge=False)` we run `git checkout -- .` + `git clean -fd`
    so the worktree starts the next turn matching the main tree.

    On `cleanup()` we `git worktree remove --force` each worktree and remove
    `.mau-worktrees/`. The `keep_worktrees` flag (set true in brownfield mode)
    suppresses this so users can inspect what each agent did after a run."""

    name = "worktree"

    def __init__(
        self,
        git_root: Path,
        merge_dest: Optional[Path] = None,
        worktrees_dir: Optional[Path] = None,
        emit: Optional[EventEmitter] = None,
        keep_worktrees: bool = False,
    ):
        # git_root: where .git lives — used for `git worktree add` and as the
        # default home for `.mau-worktrees/`.
        # merge_dest: where overlay-copied files land. In greenfield runs
        # `code_dir` is nested under the git toplevel (e.g. root/workspace/),
        # so merging back into git_root would orphan files outside the
        # workspace the orchestrator advertises. Defaults to git_root so
        # brownfield behaviour (repo root == code dir) is unchanged.
        self.repo_root = git_root.resolve()
        self.merge_dest = (
            merge_dest.resolve() if merge_dest is not None else self.repo_root
        )
        self.worktrees_dir = (
            worktrees_dir.resolve()
            if worktrees_dir is not None
            else self.repo_root / ".mau-worktrees"
        )
        self.emit = emit or (lambda *_: None)
        self.keep_worktrees = keep_worktrees
        # agent_name → worktree Path
        self._worktrees: dict[str, Path] = {}
        # agent_name → merge baseline mtime (epoch seconds). Files in the
        # main tree newer than this when we go to overlay are "someone else
        # got here first" and trigger worktree_merge_overwrote.
        self._baselines: dict[str, float] = {}

    # ---- public API ------------------------------------------------------

    def acquire(self, agent_name: str) -> Path:
        existing = self._worktrees.get(agent_name)
        if existing is not None and existing.exists():
            return existing

        slug = _safe_slug(agent_name)
        target = self.worktrees_dir / slug
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        # Stale dir from a previous interrupted run — clear it before re-adding.
        if target.exists():
            self._git_worktree_remove(target)

        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "add",
             "--detach", str(target), "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for {agent_name}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        self._worktrees[agent_name] = target
        self._baselines[agent_name] = _newest_mtime(self.merge_dest)
        self.emit("worktree_created", {"agent": agent_name, "path": str(target)})
        return target

    def release(self, agent_name: str, *, merge: bool) -> Optional[list[Path]]:
        worktree = self._worktrees.get(agent_name)
        if worktree is None or not worktree.exists():
            return None

        if merge:
            changed = self._changed_paths(worktree)
            merged: list[Path] = []
            overwrote: list[str] = []
            baseline = self._baselines.get(agent_name, 0.0)
            for rel in changed:
                src = worktree / rel
                dst = self.merge_dest / rel
                if not src.exists():
                    # Deletion — skip for now. Mau agents overwhelmingly
                    # add/edit; deletion semantics are intentionally out of
                    # scope (would need a confirmation flow).
                    continue
                if dst.exists():
                    try:
                        if dst.stat().st_mtime > baseline:
                            overwrote.append(str(rel))
                    except OSError:
                        pass
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    continue
                merged.append(rel)
            if overwrote:
                self.emit(
                    "worktree_merge_overwrote",
                    {"agent": agent_name, "files": overwrote},
                )
            self._reset_worktree(worktree)
            self._baselines[agent_name] = _newest_mtime(self.merge_dest)
            self.emit(
                "worktree_merged",
                {"agent": agent_name, "count": len(merged),
                 "files": [str(p) for p in merged]},
            )
            return merged

        # discard
        self._reset_worktree(worktree)
        self._baselines[agent_name] = _newest_mtime(self.merge_dest)
        self.emit("worktree_discarded", {"agent": agent_name})
        return None

    def cleanup(self) -> None:
        if self.keep_worktrees:
            self.emit(
                "worktree_cleanup",
                {"kept": True, "count": len(self._worktrees),
                 "dir": str(self.worktrees_dir)},
            )
            return
        for agent_name, path in list(self._worktrees.items()):
            self._git_worktree_remove(path)
            self._worktrees.pop(agent_name, None)
        # Best-effort: drop the directory itself if it's empty.
        try:
            if self.worktrees_dir.exists() and not any(self.worktrees_dir.iterdir()):
                self.worktrees_dir.rmdir()
        except OSError:
            pass
        self.emit("worktree_cleanup", {"kept": False})

    # ---- helpers ---------------------------------------------------------

    def _changed_paths(self, worktree: Path) -> list[Path]:
        """Files that differ from the worktree's HEAD plus any untracked
        files. We don't restrict to staged changes — agents `Write` files
        without staging, so working-tree diff is the right surface."""
        seen: set[str] = set()
        out: list[Path] = []

        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if diff.returncode == 0:
            for line in diff.stdout.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    out.append(Path(line))

        untracked = subprocess.run(
            ["git", "-C", str(worktree), "ls-files",
             "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30,
        )
        if untracked.returncode == 0:
            for line in untracked.stdout.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    out.append(Path(line))
        return out

    def _reset_worktree(self, worktree: Path) -> None:
        subprocess.run(
            ["git", "-C", str(worktree), "checkout", "--", "."],
            capture_output=True, text=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "clean", "-fd"],
            capture_output=True, text=True, timeout=30,
        )

    def _git_worktree_remove(self, path: Path) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "remove",
             "--force", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if path.exists():
            try:
                shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


def _safe_slug(agent_name: str) -> str:
    """Filesystem-safe stem for the worktree dir. Keeps it readable so
    `ls .mau-worktrees/` is obvious during debugging."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in agent_name)
    cleaned = cleaned.strip("-_") or "agent"
    return cleaned[:64]


# Directories never worth walking for the merge-baseline mtime: VCS internals,
# our own scratch dirs, and large generated/dependency trees. Pruning these
# keeps the per-acquire/-release walk proportional to source-file count rather
# than to the size of node_modules.
_MTIME_SKIP_DIRS = frozenset(
    {
        ".git", ".mau", ".mau-worktrees", "node_modules", ".venv", "venv",
        "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".next", ".nuxt", "target", ".gradle", ".idea", ".tox", "vendor",
    }
)


def _newest_mtime(path: Path) -> float:
    """Highest mtime under `path`, skipping VCS internals, our scratch dirs,
    and heavy generated/dependency trees (see `_MTIME_SKIP_DIRS`). Used as the
    merge baseline. If we can't walk the tree, return 0.0 so every existing
    file looks "older than baseline" (i.e. no false-positive overwrote events).

    The pruning matters: this runs on every acquire and every release, so a
    naive walk would stat every file in `node_modules` per agent per turn."""
    newest = 0.0
    try:
        for root, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in _MTIME_SKIP_DIRS]
            for fn in filenames:
                try:
                    m = (Path(root) / fn).stat().st_mtime
                    if m > newest:
                        newest = m
                except OSError:
                    continue
    except OSError:
        return 0.0
    return newest


def make_isolation_backend(
    code_dir: Path,
    mode: IsolationMode = "auto",
    *,
    brownfield: bool = False,
    emit: Optional[EventEmitter] = None,
) -> IsolationBackend:
    """Factory. Selects the worktree backend when:
      - `mode` is `"worktree"` (explicit opt-in), OR
      - `mode` is `"auto"`, the run is *brownfield*, and the repo is clean.

    `auto` in a *greenfield* run deliberately resolves to **shared**. The
    worktree backend would be built off the host repo's HEAD (greenfield
    workspaces commonly live nested under a repo the user ran inside), so
    agents would start every turn from a pristine checkout of unrelated source
    and never see peers' merged work — integration verifiers (`pytest`, build
    commands) can't pass against a partial tree. Shared gives correct
    cumulative semantics. Force `--isolation worktree` to override.

    Brownfield safety: if the repo has uncommitted changes we *refuse* the
    worktree backend and fall back to shared, emitting `worktree_disabled`
    so the user can `git stash` and re-resume. We also pass `keep_worktrees`
    in brownfield so the per-agent dirs survive for inspection after the run.

    Forcing `mode="worktree"` on a non-git dir is an error — the worktree
    backend can't be built without a repo, and silently downgrading would
    surprise the caller."""
    emit = emit or (lambda *_: None)
    code_dir = code_dir.resolve()

    if mode == "shared":
        return SharedWorkspaceBackend(code_dir)

    is_repo = _is_git_repo(code_dir)
    if not is_repo:
        if mode == "worktree":
            raise RuntimeError(
                f"isolation='worktree' requested but {code_dir} is not a git repo"
            )
        return SharedWorkspaceBackend(code_dir)

    if mode == "auto" and not brownfield:
        # Greenfield default: shared (see docstring — avoids worktrees off the
        # host repo's HEAD and the broken integration semantics that follow).
        return SharedWorkspaceBackend(code_dir)

    toplevel = _git_toplevel(code_dir) or code_dir

    if brownfield and _has_uncommitted_changes(toplevel):
        emit(
            "worktree_disabled",
            {
                "reason": "uncommitted_changes",
                "code_dir": str(toplevel),
                "hint": "git stash, then resume to enable per-agent worktrees",
            },
        )
        return SharedWorkspaceBackend(code_dir)

    # Worktree selected (explicit, or brownfield-auto with a clean repo). Make
    # its limitations observable up front: each per-agent worktree is reset to
    # HEAD every turn and excludes git-ignored files (node_modules, .venv,
    # .env), and verifiers run against that pre-merge partial tree — so a
    # cross-agent integration check won't see peers' merged work or ignored
    # deps. `--isolation shared` is the escape hatch.
    emit(
        "worktree_isolation_caveat",
        {
            "git_root": str(toplevel),
            "merge_dest": str(code_dir),
            "note": (
                "per-agent worktrees reset to HEAD each turn and exclude "
                "git-ignored files; verifiers run against a partial tree. Use "
                "--isolation shared if a verifier needs the merged workspace "
                "or ignored deps."
            ),
        },
    )
    # Greenfield (explicit worktree): `git init` may live at the parent of
    # `code_dir` (run root with `workspace/` nested under it). The worktree
    # backend runs `git worktree add` against the toplevel but merges back into
    # `code_dir` so files land where every other component expects.
    return GitWorktreeBackend(
        git_root=toplevel,
        merge_dest=code_dir,
        emit=emit,
        keep_worktrees=brownfield,
    )
