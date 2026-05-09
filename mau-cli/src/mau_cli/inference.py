"""Inference adapter: shells out to the local `claude` or `codex` CLI.

Two modes:

- `call_plan(system, user)` — structured-JSON one-shot. Used by Product / EM /
  Tech Lead so the orchestrator can dispatch their actions.
- `call_agentic(system, user, workspace)` — full tool-using run inside a
  workspace directory. Used by specialists (FE/BE/DB/QA/DevOps) so they can
  actually read, write, and edit files. Returns the final assistant text;
  the orchestrator extracts a structured DELIVERABLE block from it.

Both backends are assumed to already be authenticated on the user's machine.
A mock backend is included for testing and offline demos.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from mau_cli.schemas import TokenUsage


# JSON-extraction. Models occasionally wrap JSON in fences or add prose.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# DELIVERABLE block extraction for agentic mode. Specialists end their final
# message with <DELIVERABLE>{...}</DELIVERABLE> so we can parse a structured
# summary even when the rest of the response is free-form prose / tool output.
_DELIVERABLE_RE = re.compile(
    r"<DELIVERABLE>\s*(\{.*?\})\s*</DELIVERABLE>", re.DOTALL | re.IGNORECASE
)


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction: direct parse → fenced block → outer braces."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty inference response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _FENCE_RE.search(text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidate = text[first : last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"could not extract JSON from response: {text[:200]!r}")


def extract_deliverable(text: str) -> Optional[dict[str, Any]]:
    """Pull the <DELIVERABLE>{...}</DELIVERABLE> block from agentic output."""
    match = _DELIVERABLE_RE.search(text or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


@dataclass
class InferenceResult:
    raw_text: str
    parsed: dict[str, Any]  # for plan mode this is the action JSON; for agentic, the deliverable (or {})
    backend: str
    duration_ms: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    files_touched: list[str] = field(default_factory=list)


class InferenceBackend(ABC):
    name: str

    @abstractmethod
    def call_plan(self, system_prompt: str, user_prompt: str) -> InferenceResult:
        """One-shot, JSON-only. Used by planning roles."""

    @abstractmethod
    def call_agentic(
        self,
        system_prompt: str,
        user_prompt: str,
        workspace_dir: str,
        extra_dirs: Optional[list[str]] = None,
        max_budget_usd: Optional[float] = None,
    ) -> InferenceResult:
        """Full tool-using run inside `workspace_dir`. Returns final text +
        structured DELIVERABLE if the agent emitted one."""

    @abstractmethod
    def available(self) -> bool: ...


# ---- Claude Code CLI -------------------------------------------------------

# Tools allowed for specialist (code-gen) agents. Bash is intentionally
# included (engineers run `npm install`, `pytest`, etc.) but agents are
# scoped to the workspace directory by `cwd`.
_AGENTIC_TOOLS = "Read,Write,Edit,Glob,Grep,Bash"


class ClaudeCLIBackend(InferenceBackend):
    """Wraps `claude -p`. Plan mode uses --output-format json; agentic mode
    additionally enables tools, sets cwd, and adds shared-doc directories."""

    name = "claude"

    def __init__(self, binary: str = "claude", model: Optional[str] = None):
        self.binary = binary
        self.model = model

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    # --- plan -------------------------------------------------------------

    def call_plan(self, system_prompt: str, user_prompt: str) -> InferenceResult:
        cmd = [
            self.binary,
            "-p",
            "--output-format", "json",
            "--append-system-prompt", system_prompt,
            "--disallowedTools", _AGENTIC_TOOLS,  # plan mode: no tools
        ]
        if self.model:
            cmd += ["--model", self.model]
        # `--` terminates option parsing so the prompt isn't consumed by a
        # variadic flag (e.g. --disallowedTools).
        cmd += ["--", user_prompt]

        envelope, raw_text, duration_ms = self._invoke(cmd)
        inner = envelope.get("result") or envelope.get("text") or raw_text
        parsed = extract_json(inner)
        return InferenceResult(
            raw_text=inner,
            parsed=parsed,
            backend=self.name,
            duration_ms=duration_ms,
            usage=_usage_from_envelope(envelope),
        )

    # --- agentic ----------------------------------------------------------

    def call_agentic(
        self,
        system_prompt: str,
        user_prompt: str,
        workspace_dir: str,
        extra_dirs: Optional[list[str]] = None,
        max_budget_usd: Optional[float] = None,
    ) -> InferenceResult:
        cmd = [
            self.binary,
            "-p",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--append-system-prompt", system_prompt,
            "--allowedTools", _AGENTIC_TOOLS,
        ]
        for d in (extra_dirs or []):
            cmd += ["--add-dir", d]
        if max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(max_budget_usd)]
        if self.model:
            cmd += ["--model", self.model]
        # `--` terminates option parsing so the prompt isn't consumed by
        # variadic flags like --allowedTools or --add-dir.
        cmd += ["--", user_prompt]

        envelope, raw_text, duration_ms = self._invoke(cmd, cwd=workspace_dir)
        inner = envelope.get("result") or envelope.get("text") or raw_text
        deliverable = extract_deliverable(inner) or {}
        files_touched = list(deliverable.get("files_touched", []) or [])
        return InferenceResult(
            raw_text=inner,
            parsed=deliverable,
            backend=self.name,
            duration_ms=duration_ms,
            usage=_usage_from_envelope(envelope),
            files_touched=files_touched,
        )

    # --- helpers ----------------------------------------------------------

    def _invoke(
        self, cmd: list[str], cwd: Optional[str] = None
    ) -> tuple[dict[str, Any], str, int]:
        start = _time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,  # don't block on stdin
            timeout=900,
            env={**os.environ, "CLAUDE_CODE_DISABLE_TELEMETRY": "1"},
        )
        duration_ms = int((_time.monotonic() - start) * 1000)

        if proc.returncode != 0:
            stderr_tail = proc.stderr.strip()[-800:]
            stdout_tail = proc.stdout.strip()[-300:]
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}\nstderr: {stderr_tail}\nstdout: {stdout_tail}"
            )

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = {"result": proc.stdout}
        return envelope, proc.stdout, duration_ms


def _usage_from_envelope(envelope: dict[str, Any]) -> TokenUsage:
    usage = envelope.get("usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cost_usd=float(envelope.get("total_cost_usd", 0.0) or 0.0),
        calls=1,
    )


# ---- Codex CLI -------------------------------------------------------------


class CodexCLIBackend(InferenceBackend):
    """Wraps `codex exec`. No usage envelope — token tracking is best-effort."""

    name = "codex"

    def __init__(self, binary: str = "codex"):
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def call_plan(self, system_prompt: str, user_prompt: str) -> InferenceResult:
        return self._exec(system_prompt, user_prompt, cwd=None, plan=True)

    def call_agentic(
        self,
        system_prompt: str,
        user_prompt: str,
        workspace_dir: str,
        extra_dirs: Optional[list[str]] = None,
        max_budget_usd: Optional[float] = None,
    ) -> InferenceResult:
        return self._exec(system_prompt, user_prompt, cwd=workspace_dir, plan=False)

    def _exec(
        self, system_prompt: str, user_prompt: str, cwd: Optional[str], plan: bool
    ) -> InferenceResult:
        combined = f"{system_prompt}\n\n---\n\n{user_prompt}"
        cmd = [self.binary, "exec", "--quiet", combined]

        start = _time.monotonic()
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, timeout=900
        )
        duration_ms = int((_time.monotonic() - start) * 1000)

        if proc.returncode != 0:
            raise RuntimeError(
                f"codex CLI exited {proc.returncode}: {proc.stderr.strip()[:300]}"
            )

        text = proc.stdout
        if plan:
            parsed = extract_json(text)
            files = []
        else:
            parsed = extract_deliverable(text) or {}
            files = list(parsed.get("files_touched", []) or [])
        return InferenceResult(
            raw_text=text,
            parsed=parsed,
            backend=self.name,
            duration_ms=duration_ms,
            usage=TokenUsage(calls=1),
            files_touched=files,
        )


# ---- Backend selection ----------------------------------------------------


def select_backend(preference: Optional[str] = None) -> InferenceBackend:
    if preference == "mock":
        from mau_cli.mock_inference import MockBackend
        return MockBackend()

    candidates: list[InferenceBackend]
    if preference == "claude":
        candidates = [ClaudeCLIBackend()]
    elif preference == "codex":
        candidates = [CodexCLIBackend()]
    else:
        candidates = [ClaudeCLIBackend(), CodexCLIBackend()]

    for backend in candidates:
        if backend.available():
            return backend

    if preference and preference not in (None, "auto"):
        raise RuntimeError(f"requested backend '{preference}' not found on PATH")

    from mau_cli.mock_inference import MockBackend
    return MockBackend()
