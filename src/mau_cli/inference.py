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
import sys
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mau_cli.schemas import TokenUsage


# Two retries with short backoff. The error pattern we target (claude returns
# exit=1 with a structured-but-empty envelope) is transient; in practice a
# 1-3s gap is enough for whatever rate-limit / concurrent-session conflict
# triggered it to clear.
CLAUDE_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 3.0)

# Heuristic markers in codex stderr that suggest a retryable issue rather than
# a malformed prompt. Matched case-insensitively.
_CODEX_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "rate limit",
    "connection",
    "temporarily",
)


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

    def __init__(
        self,
        binary: str = "claude",
        model: Optional[str] = None,
        on_retry: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        self.binary = binary
        self.model = model
        # Optional sink for "this invocation transiently failed and we retried"
        # signals. Orchestrator wires this to an `inference_retried` event so
        # transcripts capture the flake. Best-effort: callback errors are
        # swallowed so a buggy observer can't kill an otherwise-healthy run.
        self.on_retry = on_retry

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
        # Retry pattern: one initial attempt + len(CLAUDE_RETRY_BACKOFF_SECONDS)
        # retries. We only retry when claude returns a structured envelope that
        # indicates no actual model output happened (transient backend flake).
        # A real failure — invalid prompt, unknown flag, anything where stdout
        # isn't a parseable envelope or where the envelope contains substantive
        # content — raises on the first attempt.
        last_err: Optional[str] = None
        attempts = [0.0, *CLAUDE_RETRY_BACKOFF_SECONDS]
        for attempt, delay in enumerate(attempts):
            if delay:
                _time.sleep(delay)
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

            if proc.returncode == 0:
                try:
                    envelope = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    envelope = {"result": proc.stdout}
                return envelope, proc.stdout, duration_ms

            envelope: Optional[dict[str, Any]] = None
            try:
                envelope = json.loads(proc.stdout)
            except json.JSONDecodeError:
                envelope = None

            if envelope is not None and self._envelope_is_transient(envelope):
                last_err = self._format_error(proc, envelope, attempt)
                self._notify_retry(attempt, last_err, envelope)
                continue

            raise RuntimeError(self._format_error(proc, envelope, attempt))

        raise RuntimeError(f"claude CLI exhausted retries: {last_err}")

    def _notify_retry(
        self, attempt: int, err: str, envelope: Optional[dict[str, Any]]
    ) -> None:
        payload = {
            "backend": self.name,
            "attempt": attempt,
            "error": err,
            "envelope_keys": sorted(envelope.keys()) if envelope else [],
        }
        if self.on_retry is not None:
            try:
                self.on_retry(payload)
            except Exception:
                pass
        # Always leave a breadcrumb in stderr so a real-backend run shows the
        # flake even when the caller didn't wire `on_retry`.
        print(
            f"[claude-retry attempt={attempt}] {err}",
            file=sys.stderr,
        )

    @staticmethod
    def _envelope_is_transient(envelope: dict[str, Any]) -> bool:
        """Heuristic: response parsed but contains no actual model output.

        Pattern observed in the wild — exit code 1 with a structured envelope
        whose `iterations` and `modelUsage` are empty, or whose error subtype
        explicitly flags an in-execution failure. These reliably clear on
        retry; real prompt errors don't."""
        subtype = envelope.get("subtype")
        if envelope.get("is_error") and subtype in {
            "error_during_execution",
            "error_max_turns",
        }:
            return True
        if envelope.get("iterations") == [] and envelope.get("modelUsage") == {}:
            return True
        return False

    @staticmethod
    def _format_error(
        proc: subprocess.CompletedProcess, envelope: Optional[dict[str, Any]], attempt: int
    ) -> str:
        stderr_tail = (proc.stderr or "").strip()[-400:]
        if envelope is not None:
            keys = (
                "subtype",
                "is_error",
                "result",
                "terminal_reason",
                "api_error_status",
                "session_id",
            )
            snapshot = {k: envelope.get(k) for k in keys if k in envelope}
            return (
                f"claude CLI exit={proc.returncode} (attempt={attempt}) "
                f"envelope={snapshot} stderr={stderr_tail!r}"
            )
        stdout_tail = (proc.stdout or "").strip()[-300:]
        return (
            f"claude CLI exit={proc.returncode} (attempt={attempt}) "
            f"stderr={stderr_tail!r} stdout_tail={stdout_tail!r}"
        )


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

    def __init__(
        self,
        binary: str = "codex",
        on_retry: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        self.binary = binary
        self.on_retry = on_retry

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

        # Codex doesn't emit a structured envelope, so we use a coarser
        # signal: retry once on a non-zero exit when stderr looks like a
        # transient network / rate-limit error. A real failure (malformed
        # prompt, syntax error) surfaces unchanged.
        last_err: Optional[str] = None
        for attempt in (0, 1):
            start = _time.monotonic()
            proc = subprocess.run(
                cmd, capture_output=True, text=True, cwd=cwd, timeout=900
            )
            duration_ms = int((_time.monotonic() - start) * 1000)

            if proc.returncode == 0:
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

            last_err = (
                f"codex CLI exit={proc.returncode} (attempt={attempt}): "
                f"{(proc.stderr or '').strip()[:300]}"
            )
            if attempt == 0 and self._stderr_looks_transient(proc.stderr or ""):
                self._notify_retry(attempt, last_err)
                _time.sleep(1.0)
                continue
            raise RuntimeError(last_err)

        raise RuntimeError(f"codex CLI exhausted retries: {last_err}")

    @staticmethod
    def _stderr_looks_transient(stderr: str) -> bool:
        low = stderr.lower()
        return any(marker in low for marker in _CODEX_TRANSIENT_MARKERS)

    def _notify_retry(self, attempt: int, err: str) -> None:
        payload = {"backend": self.name, "attempt": attempt, "error": err}
        if self.on_retry is not None:
            try:
                self.on_retry(payload)
            except Exception:
                pass
        print(
            f"[codex-retry attempt={attempt}] {err}",
            file=sys.stderr,
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
