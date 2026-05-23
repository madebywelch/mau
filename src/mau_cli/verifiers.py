"""Verifier registry — deterministic sensors for the Plan/Execute/Verify loop.

Each verifier takes a `spec` dict (shape varies by verifier) and a `workspace`
Path, and returns a `VerifierResult`. The orchestrator dispatches the
`verify` action to whichever entry in `VERIFIERS` matches `spec["verifier"]`.

Verifiers are intentionally narrow: path-exists, run-command, parse-contract.
Layered verification (file exists → file compiles → tests pass) is the
paper's recipe; this module supplies the bottom three layers.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class VerifierResult:
    ok: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


class Verifier(ABC):
    name: str

    @abstractmethod
    def run(self, spec: dict[str, Any], workspace: Path) -> VerifierResult: ...


def _resolve_in_workspace(workspace: Path, raw: str) -> tuple[Path, bool]:
    """Resolve `raw` against `workspace`. Returns (resolved_path, inside_workspace).
    Mirrors the containment check in the old `_verify_files`."""
    ws_root = workspace.resolve()
    try:
        full = (ws_root / raw).resolve()
        full.relative_to(ws_root)
    except (ValueError, OSError):
        return ws_root / raw, False
    return full, True


class PathExistsVerifier(Verifier):
    name = "path_exists"

    def run(self, spec: dict[str, Any], workspace: Path) -> VerifierResult:
        paths = list(spec.get("paths") or [])
        if not paths:
            return VerifierResult(ok=True, summary="no paths to check", details={"missing": []})
        missing: list[str] = []
        for raw in paths:
            full, inside = _resolve_in_workspace(workspace, str(raw))
            if not inside or not full.exists():
                missing.append(str(raw))
        if missing:
            return VerifierResult(
                ok=False,
                summary=f"{len(missing)}/{len(paths)} paths missing: {missing}",
                details={"missing": missing, "checked": paths},
            )
        return VerifierResult(
            ok=True,
            summary=f"all {len(paths)} paths exist",
            details={"checked": paths},
        )


class RunCommandVerifier(Verifier):
    name = "run_command"

    def run(self, spec: dict[str, Any], workspace: Path) -> VerifierResult:
        command = spec.get("command")
        if not command or not isinstance(command, str):
            return VerifierResult(
                ok=False,
                summary="missing 'command' string in spec",
                details={"spec": spec},
            )

        cwd_raw = spec.get("cwd")
        if cwd_raw:
            cwd_path, inside = _resolve_in_workspace(workspace, str(cwd_raw))
            if not inside:
                return VerifierResult(
                    ok=False,
                    summary=f"cwd {cwd_raw!r} escapes workspace",
                    details={"cwd": str(cwd_raw)},
                )
            cwd = str(cwd_path)
        else:
            cwd = str(workspace)

        timeout = float(spec.get("timeout_seconds", 60))
        expected_exit = int(spec.get("expected_exit", 0))

        result = self._run_once(command, cwd, timeout, expected_exit)

        # Macos default Pythons ship without a `python` symlink (only
        # `python3`); same story for `pip` vs `pip3` and the standard Homebrew
        # layout. Agents are instructed to use the `3` suffix but occasionally
        # forget, and rejecting a valid deliverable over a missing symlink
        # discards real work. On exit 127 (command not found) with `python`
        # or `pip` as the leading token, retry once with the suffixed form.
        if (
            not result.ok
            and result.details.get("exit") == 127
            and isinstance(command, str)
        ):
            substituted = _substitute_python3(command)
            if substituted is not None:
                retry = self._run_once(substituted, cwd, timeout, expected_exit)
                retry.details["substituted_from"] = command
                retry.details["substituted_to"] = substituted
                retry.details["substituted_python3"] = True
                return retry

        return result

    def _run_once(
        self, command: str, cwd: str, timeout: float, expected_exit: int
    ) -> VerifierResult:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            return VerifierResult(
                ok=False,
                summary=f"command timed out after {timeout:.0f}s",
                details={
                    "command": command,
                    "cwd": cwd,
                    "timeout_seconds": timeout,
                    "stdout_tail": (e.stdout or "")[-800:] if isinstance(e.stdout, str) else "",
                    "stderr_tail": (e.stderr or "")[-800:] if isinstance(e.stderr, str) else "",
                },
            )
        except OSError as e:
            return VerifierResult(
                ok=False,
                summary=f"failed to launch command: {e}",
                details={"command": command, "cwd": cwd},
            )

        ok = proc.returncode == expected_exit
        return VerifierResult(
            ok=ok,
            summary=(
                f"exit={proc.returncode} (expected {expected_exit}) for `{command}`"
            ),
            details={
                "command": command,
                "cwd": cwd,
                "exit": proc.returncode,
                "expected_exit": expected_exit,
                "stdout_tail": proc.stdout[-800:],
                "stderr_tail": proc.stderr[-800:],
            },
        )


def _substitute_python3(command: str) -> Optional[str]:
    """Return `command` with a leading `python` or `pip` swapped for `python3`
    or `pip3`. Returns None if the first whitespace-separated token isn't one
    of those (so we never silently rewrite anything else)."""
    stripped = command.lstrip()
    if not stripped:
        return None
    parts = stripped.split(maxsplit=1)
    head = parts[0]
    if head == "python":
        new_head = "python3"
    elif head == "pip":
        new_head = "pip3"
    else:
        return None
    leading_ws = command[: len(command) - len(stripped)]
    tail = "" if len(parts) == 1 else " " + parts[1]
    return f"{leading_ws}{new_head}{tail}"


class ParseContractVerifier(Verifier):
    """Parse a single file to confirm it is syntactically valid.

    Dispatches by suffix:
      .py            → ast.parse
      .json          → json.loads
      .yaml/.yml     → yaml.safe_load if PyYAML is installed, else skipped
      .ts/.tsx/.js   → `node --check` if node is on PATH, else skipped
      anything else  → skipped with a clear summary
    """

    name = "parse_contract"

    def run(self, spec: dict[str, Any], workspace: Path) -> VerifierResult:
        raw = spec.get("path")
        if not raw:
            return VerifierResult(ok=False, summary="missing 'path' in spec", details={})
        full, inside = _resolve_in_workspace(workspace, str(raw))
        if not inside:
            return VerifierResult(
                ok=False,
                summary=f"path {raw!r} escapes workspace",
                details={"path": str(raw)},
            )
        if not full.exists():
            return VerifierResult(
                ok=False,
                summary=f"path {raw!r} does not exist",
                details={"path": str(raw)},
            )

        suffix = full.suffix.lower()
        if suffix == ".py":
            return self._parse_python(full, raw)
        if suffix == ".json":
            return self._parse_json(full, raw)
        if suffix in (".yaml", ".yml"):
            return self._parse_yaml(full, raw)
        if suffix in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
            return self._parse_node(full, raw)
        return VerifierResult(
            ok=True,
            summary=f"skipped: no parser for suffix {suffix or '(none)'}",
            details={"path": str(raw), "suffix": suffix},
        )

    @staticmethod
    def _parse_python(full: Path, raw: str) -> VerifierResult:
        try:
            source = full.read_text(encoding="utf-8")
        except OSError as e:
            return VerifierResult(ok=False, summary=f"read error: {e}", details={"path": str(raw)})
        try:
            ast.parse(source, filename=str(full))
        except SyntaxError as e:
            return VerifierResult(
                ok=False,
                summary=f"SyntaxError at line {e.lineno}: {e.msg}",
                details={"path": str(raw), "lineno": e.lineno, "msg": e.msg},
            )
        return VerifierResult(ok=True, summary=f"parsed {raw} as Python", details={"path": str(raw)})

    @staticmethod
    def _parse_json(full: Path, raw: str) -> VerifierResult:
        try:
            text = full.read_text(encoding="utf-8")
        except OSError as e:
            return VerifierResult(ok=False, summary=f"read error: {e}", details={"path": str(raw)})
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            return VerifierResult(
                ok=False,
                summary=f"JSONDecodeError at line {e.lineno}: {e.msg}",
                details={"path": str(raw), "lineno": e.lineno, "msg": e.msg},
            )
        return VerifierResult(ok=True, summary=f"parsed {raw} as JSON", details={"path": str(raw)})

    @staticmethod
    def _parse_yaml(full: Path, raw: str) -> VerifierResult:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return VerifierResult(
                ok=True,
                summary=f"skipped {raw}: PyYAML not installed",
                details={"path": str(raw), "reason": "no_pyyaml"},
            )
        try:
            text = full.read_text(encoding="utf-8")
        except OSError as e:
            return VerifierResult(ok=False, summary=f"read error: {e}", details={"path": str(raw)})
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as e:
            return VerifierResult(
                ok=False,
                summary=f"YAMLError: {e}",
                details={"path": str(raw), "msg": str(e)},
            )
        return VerifierResult(ok=True, summary=f"parsed {raw} as YAML", details={"path": str(raw)})

    @staticmethod
    def _parse_node(full: Path, raw: str) -> VerifierResult:
        if shutil.which("node") is None:
            return VerifierResult(
                ok=True,
                summary=f"skipped {raw}: node not on PATH",
                details={"path": str(raw), "reason": "no_node"},
            )
        try:
            proc = subprocess.run(
                ["node", "--check", str(full)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return VerifierResult(
                ok=False,
                summary=f"node --check failed to run: {e}",
                details={"path": str(raw)},
            )
        if proc.returncode != 0:
            return VerifierResult(
                ok=False,
                summary=f"node --check exit {proc.returncode}",
                details={
                    "path": str(raw),
                    "stderr_tail": proc.stderr[-800:],
                    "stdout_tail": proc.stdout[-400:],
                },
            )
        return VerifierResult(
            ok=True,
            summary=f"parsed {raw} via node --check",
            details={"path": str(raw)},
        )


VERIFIERS: dict[str, Verifier] = {
    PathExistsVerifier.name: PathExistsVerifier(),
    RunCommandVerifier.name: RunCommandVerifier(),
    ParseContractVerifier.name: ParseContractVerifier(),
}
