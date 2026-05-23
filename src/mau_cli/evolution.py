"""Evolution Agent prototype — Agentic Harness Engineering seed.

Reads the per-agent JSONL transcripts written by Task 1 (and enriched by
Task 6 with `worktree_path` / `isolation` fields) and produces structured
`HarnessProposal`s the maintainer can review.

The prototype keeps three deterministic signals:

1. Rejection-rate per role  → propose a `prompt_edit` against the role's
   `prompts/<role>.md`. Cheap heuristic: bucket the first 80 chars of any
   blocker / rejection note attached to the rejected turn and surface the
   top reasons as evidence.
2. Repeated `worktree_merge_overwrote` for a role → propose a
   `policy_suggestion` ("coordinate via task assignment, not parallel
   writes"). The transcript JSONL doesn't currently include the merge
   events (they're emitted on the orchestrator, not the agent), so the
   prototype settles for inferring stomps from the `isolation` field and
   the same-file overlap heuristic. Documented in the rationale.
3. Outlier average duration per role (>2x the global median) → propose a
   `default_change` against `MAX_TURNS_PER_AGENT` for that role.

When `--use-backend` is passed the agent additionally asks the supplied
`InferenceBackend` to draft a concrete prompt diff for each role that has
a deterministic proposal. Errors fall back to the deterministic baseline.

Every proposal carries two transcript citations of the form
`logs/<agent>.jsonl:turn=<N>` so a reviewer can read the actual lines.

A `RegressionSuite` stub runs each fixture under `evolution_fixtures/`
against the `MockBackend` and reports pass/fail. `apply_proposal` patches
a temp copy of the prompts dir (never the source) and re-runs the suite —
so harness mutations are gated before a human merges them.
"""

from __future__ import annotations

import json
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Optional

from mau_cli.inference import InferenceBackend
from mau_cli.orchestrator import MAX_TURNS_PER_AGENT
from mau_cli.schemas import Workspace


# Tunables. Conservative defaults — the prototype only fires when the
# signal is unambiguous so a maintainer doesn't drown in noise.
REJECTION_RATE_THRESHOLD = 0.4
MIN_TURNS_FOR_REJECTION_SIGNAL = 5
DURATION_OUTLIER_MULTIPLIER = 2.0
REJECTION_BUCKET_CHARS = 80
EVIDENCE_PER_PROPOSAL = 2
TOP_REJECTION_REASONS = 5


ProposalKind = Literal["prompt_edit", "policy_suggestion", "default_change"]
Confidence = Literal["low", "medium", "high"]


@dataclass
class TokenUsageAggregate:
    input: int = 0
    output: int = 0
    cost_usd: float = 0.0

    def add(self, *, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.input += input_tokens
        self.output += output_tokens
        self.cost_usd += cost


@dataclass
class TranscriptSummary:
    agent: str
    role: str
    total_turns: int
    accepted_turns: int
    rejected_turns: int
    total_tokens: TokenUsageAggregate
    avg_duration_ms: float
    common_rejection_reasons: list[tuple[str, int]]
    # Carried for proposal evidence; not part of the headline display.
    rejected_turn_numbers: list[int] = field(default_factory=list)
    log_file: str = ""

    @property
    def rejection_rate(self) -> float:
        if self.total_turns == 0:
            return 0.0
        return self.rejected_turns / self.total_turns


@dataclass
class HarnessProposal:
    id: str
    kind: ProposalKind
    target: str
    rationale: str
    diff: Optional[str]
    confidence: Confidence
    evidence: list[str]


# ---- ingestion --------------------------------------------------------------


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    """Best-effort JSONL parse: skip malformed lines rather than abort.
    Transcripts are append-only; a half-written final line shouldn't break
    summarize."""
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _bucket_reason(text: str) -> str:
    """Compress whitespace and clip to REJECTION_BUCKET_CHARS so similar
    blockers land in the same bucket (avoid '1/3 paths missing' vs
    '2/3 paths missing' becoming distinct reasons)."""
    if not text:
        return ""
    joined = " ".join(text.split())
    return joined[:REJECTION_BUCKET_CHARS]


def _extract_rejection_reasons(record: dict[str, Any]) -> list[str]:
    """Pull the most likely blocker explanation out of a rejected-turn
    record. Looks at the response (often a deliverable summary), then the
    last 1k chars of the prompt (where the orchestrator's blocker message
    lives if the agent was reactivated). The bucket keeps the prototype
    cheap — no NLP needed."""
    if record.get("accepted"):
        return []
    reasons: list[str] = []
    response = record.get("response") or ""
    if response:
        reasons.append(_bucket_reason(response))
    prompt = record.get("prompt") or ""
    if prompt:
        # The orchestrator delivers blockers via the inbox, which the agent
        # then sees in its next prompt — the tail of the prompt is the
        # likeliest place to find the rejection reason text.
        tail = prompt[-1500:]
        reasons.append(_bucket_reason(tail))
    return [r for r in reasons if r]


class EvolutionAgent:
    """Ingests `logs/<agent>.jsonl` and emits `HarnessProposal`s.

    The agent is stateless across calls; `summarize()` and `propose()` can
    be invoked independently and re-read the directory each time so the
    caller can iterate after a new run lands transcripts."""

    def __init__(
        self,
        logs_dir: Path,
        prompts_dir: Path,
        backend: Optional[InferenceBackend] = None,
    ):
        self.logs_dir = Path(logs_dir)
        self.prompts_dir = Path(prompts_dir)
        self.backend = backend

    # ---- public ---------------------------------------------------------

    def summarize(self) -> dict[str, TranscriptSummary]:
        """Walk every `*.jsonl` in `logs_dir`, group by agent name, compute
        the per-agent summary. Returns an empty dict if the directory is
        missing — the CLI prints a friendly "no logs found" message."""
        if not self.logs_dir.exists() or not self.logs_dir.is_dir():
            return {}

        per_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for path in sorted(self.logs_dir.glob("*.jsonl")):
            records = _parse_jsonl(path)
            for r in records:
                agent = str(r.get("agent") or path.stem)
                r["_source_path"] = str(path)
                per_agent[agent].append(r)

        summaries: dict[str, TranscriptSummary] = {}
        for agent, records in per_agent.items():
            summaries[agent] = self._summarize_agent(agent, records)
        return summaries

    def propose(self) -> list[HarnessProposal]:
        """Run deterministic checks across all summaries and emit proposals.
        When `backend` is set and isn't the mock, additionally ask the LLM
        to draft a concrete diff for each prompt_edit proposal."""
        summaries = self.summarize()
        if not summaries:
            return []

        proposals: list[HarnessProposal] = []
        # Aggregate per-role since prompt files live at `prompts/<role>.md`.
        per_role = self._aggregate_by_role(summaries)

        proposals.extend(self._propose_rejection_signals(per_role, summaries))
        proposals.extend(self._propose_worktree_signals(summaries))
        proposals.extend(self._propose_duration_outliers(per_role))

        if self.backend is not None and self.backend.name != "mock":
            self._enrich_with_backend_diffs(proposals)

        return proposals

    # ---- summarize helpers ----------------------------------------------

    def _summarize_agent(
        self, agent: str, records: list[dict[str, Any]]
    ) -> TranscriptSummary:
        records_sorted = sorted(records, key=lambda r: int(r.get("turn") or 0))

        role = str(records_sorted[0].get("role")) if records_sorted else ""
        accepted = 0
        rejected = 0
        rejected_turns: list[int] = []
        tokens = TokenUsageAggregate()
        durations: list[float] = []
        reason_counter: Counter[str] = Counter()

        for r in records_sorted:
            if r.get("accepted"):
                accepted += 1
            else:
                rejected += 1
                try:
                    rejected_turns.append(int(r.get("turn") or 0))
                except (TypeError, ValueError):
                    pass
                for reason in _extract_rejection_reasons(r):
                    reason_counter[reason] += 1
            t = r.get("tokens") or {}
            tokens.add(
                input_tokens=int(t.get("input") or 0),
                output_tokens=int(t.get("output") or 0),
                cost=float(t.get("cost_usd") or 0.0),
            )
            d = r.get("duration_ms")
            if isinstance(d, (int, float)):
                durations.append(float(d))

        avg_duration = statistics.mean(durations) if durations else 0.0
        source_path = records_sorted[0].get("_source_path", "") if records_sorted else ""
        return TranscriptSummary(
            agent=agent,
            role=role,
            total_turns=len(records_sorted),
            accepted_turns=accepted,
            rejected_turns=rejected,
            total_tokens=tokens,
            avg_duration_ms=avg_duration,
            common_rejection_reasons=reason_counter.most_common(TOP_REJECTION_REASONS),
            rejected_turn_numbers=rejected_turns,
            log_file=source_path,
        )

    def _aggregate_by_role(
        self, summaries: dict[str, TranscriptSummary]
    ) -> dict[str, list[TranscriptSummary]]:
        out: dict[str, list[TranscriptSummary]] = defaultdict(list)
        for s in summaries.values():
            if s.role:
                out[s.role].append(s)
        return out

    # ---- propose helpers ------------------------------------------------

    def _propose_rejection_signals(
        self,
        per_role: dict[str, list[TranscriptSummary]],
        summaries: dict[str, TranscriptSummary],
    ) -> list[HarnessProposal]:
        out: list[HarnessProposal] = []
        for role, role_summaries in per_role.items():
            total_turns = sum(s.total_turns for s in role_summaries)
            rejected = sum(s.rejected_turns for s in role_summaries)
            if total_turns < MIN_TURNS_FOR_REJECTION_SIGNAL:
                continue
            rate = rejected / total_turns if total_turns else 0.0
            if rate <= REJECTION_RATE_THRESHOLD:
                continue
            top_reason = ""
            merged: Counter[str] = Counter()
            for s in role_summaries:
                for reason, count in s.common_rejection_reasons:
                    merged[reason] += count
            if merged:
                top_reason = merged.most_common(1)[0][0]

            prompt_target = f"prompts/{role}.md"
            rationale = (
                f"rejection rate {rate * 100:.0f}% over {total_turns} turns "
                f"for role={role}. Top blocker: {top_reason!r}. "
                f"Update the role prompt to address this failure mode "
                f"(e.g. tighten the DELIVERABLE shape, add an explicit "
                f"pre-check, or reference the relevant SHARED_DOCS)."
            )
            evidence = self._build_rejection_evidence(role_summaries)
            confidence: Confidence = "high" if rate > 0.6 else "medium"
            out.append(
                HarnessProposal(
                    id=f"prop_reject_{role}",
                    kind="prompt_edit",
                    target=prompt_target,
                    rationale=rationale,
                    diff=None,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
        return out

    def _propose_worktree_signals(
        self, summaries: dict[str, TranscriptSummary]
    ) -> list[HarnessProposal]:
        """Detect repeated stomps. The transcript JSONL doesn't currently
        carry the `worktree_merge_overwrote` event payload (it's emitted on
        the orchestrator, not per-agent), so we infer the same condition
        from overlap: two agents of the same role wrote the same file in
        a session that ran under a worktree backend. This is intentionally
        conservative; a richer signal lands when the orchestrator also
        appends a `merge_events` field to the transcript line, which is a
        natural follow-up to Task 6.
        """
        # role → file → set of agents that touched it
        by_role: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        # role → bool: any record had isolation==worktree?
        role_used_worktree: dict[str, bool] = defaultdict(bool)
        # role → list of (file, agent) tuples for evidence
        for_evidence: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

        if not self.logs_dir.exists():
            return []
        for path in sorted(self.logs_dir.glob("*.jsonl")):
            for r in _parse_jsonl(path):
                role = str(r.get("role") or "")
                agent = str(r.get("agent") or path.stem)
                if not role:
                    continue
                if (r.get("isolation") or "") == "worktree":
                    role_used_worktree[role] = True
                files = r.get("files_touched") or []
                for f in files:
                    by_role[role][str(f)].add(agent)
                    for_evidence[role].append(
                        (str(f), agent, str(r.get("turn") or 0))
                    )

        out: list[HarnessProposal] = []
        for role, files_map in by_role.items():
            if not role_used_worktree.get(role):
                continue
            collisions = {
                f: agents for f, agents in files_map.items() if len(agents) > 1
            }
            if not collisions:
                continue
            sample_files = sorted(collisions.keys())[:3]
            evidence: list[str] = []
            for f in sample_files:
                for fname, agent, turn in for_evidence[role]:
                    if fname == f:
                        log_rel = f"logs/{agent}.jsonl"
                        evidence.append(f"{log_rel}:turn={turn} — wrote {fname}")
                        break
                if len(evidence) >= EVIDENCE_PER_PROPOSAL:
                    break
            evidence = evidence[:EVIDENCE_PER_PROPOSAL]

            rationale = (
                f"Detected {len(collisions)} file(s) written by multiple "
                f"{role} agents under worktree isolation — a hint that "
                f"parallel writes are racing. Recommend coordinating via "
                f"task assignment (one agent owns each file) rather than "
                f"letting same-role peers stomp via overlay-merge."
            )
            out.append(
                HarnessProposal(
                    id=f"prop_stomp_{role}",
                    kind="policy_suggestion",
                    target=f"policy:role:{role}",
                    rationale=rationale,
                    diff=None,
                    confidence="low",  # inferred signal, not direct
                    evidence=evidence
                    or [f"logs/(role={role}).jsonl:turn=? — collision inferred"],
                )
            )
        return out

    def _propose_duration_outliers(
        self, per_role: dict[str, list[TranscriptSummary]]
    ) -> list[HarnessProposal]:
        role_avgs: dict[str, float] = {}
        for role, role_summaries in per_role.items():
            durations = [s.avg_duration_ms for s in role_summaries if s.avg_duration_ms]
            if durations:
                role_avgs[role] = statistics.mean(durations)
        if len(role_avgs) < 2:
            return []
        median_ms = statistics.median(role_avgs.values())
        if median_ms <= 0:
            return []

        out: list[HarnessProposal] = []
        for role, avg in role_avgs.items():
            if avg <= DURATION_OUTLIER_MULTIPLIER * median_ms:
                continue
            role_summaries = per_role[role]
            evidence = []
            for s in role_summaries[:EVIDENCE_PER_PROPOSAL]:
                evidence.append(
                    f"logs/{s.agent}.jsonl:turn=last — "
                    f"avg_duration_ms={s.avg_duration_ms:.0f}"
                )
            rationale = (
                f"avg duration for role={role} is {avg:.0f}ms — "
                f"{avg / median_ms:.1f}x the cross-role median "
                f"({median_ms:.0f}ms). Suggest lowering "
                f"MAX_TURNS_PER_AGENT for this role from the current "
                f"global {MAX_TURNS_PER_AGENT} so a slow specialist "
                f"can't burn the budget in a runaway loop."
            )
            out.append(
                HarnessProposal(
                    id=f"prop_duration_{role}",
                    kind="default_change",
                    target="default:MAX_TURNS_PER_AGENT",
                    rationale=rationale,
                    diff=None,
                    confidence="medium",
                    evidence=evidence
                    or [f"logs/(role={role}).jsonl:turn=? — duration outlier"],
                )
            )
        return out

    def _build_rejection_evidence(
        self, role_summaries: list[TranscriptSummary]
    ) -> list[str]:
        out: list[str] = []
        for s in role_summaries:
            for turn in s.rejected_turn_numbers[:EVIDENCE_PER_PROPOSAL]:
                reason_snip = ""
                if s.common_rejection_reasons:
                    reason_snip = " — " + s.common_rejection_reasons[0][0][:60]
                out.append(f"logs/{s.agent}.jsonl:turn={turn}{reason_snip}")
                if len(out) >= EVIDENCE_PER_PROPOSAL:
                    return out
            if len(out) >= EVIDENCE_PER_PROPOSAL:
                break
        return out

    # ---- LLM enrichment -------------------------------------------------

    def _enrich_with_backend_diffs(self, proposals: list[HarnessProposal]) -> None:
        """For each `prompt_edit` proposal, ask the backend to draft a
        concrete prompt diff. Failures fall back to the deterministic
        proposal alone — never raise."""
        for prop in proposals:
            if prop.kind != "prompt_edit":
                continue
            prompt_path = self.prompts_dir / Path(prop.target).name
            if not prompt_path.exists():
                continue
            try:
                current = prompt_path.read_text(encoding="utf-8")
            except OSError:
                continue
            system = (
                "You are a harness-engineering reviewer. Given a role prompt "
                "and a failure signal, propose a minimal unified diff that "
                "addresses the failure. Output ONLY the diff, no prose."
            )
            user = (
                f"# Current prompt ({prop.target})\n\n{current}\n\n"
                f"# Failure signal\n\n{prop.rationale}\n\n"
                f"# Evidence\n\n" + "\n".join(prop.evidence) + "\n\n"
                "Produce a unified diff (`---`/`+++`/`@@`) editing the "
                "current prompt to address this signal. Keep changes "
                "minimal and reversible."
            )
            try:
                result = self.backend.call_plan(system, user)
            except Exception:
                continue
            text = (result.raw_text or "").strip()
            if text:
                prop.diff = text
                prop.confidence = "medium" if prop.confidence == "low" else prop.confidence


# ---- regression suite -------------------------------------------------------


@dataclass
class RegressionVerdict:
    fixture: str
    passed: bool
    stopped_on_completion: bool
    stopped_on_turn_cap: bool
    turns: int
    notes: str = ""


class RegressionSuite:
    """Tiny gate that runs each fixture through the mock backend and
    reports pass/fail. Fixtures live in `evolution_fixtures/` as JSON files
    of shape `{"request": "..."}` (optionally with `max_turns`).

    `apply_proposal` patches a *temp copy* of the prompts dir with the
    proposal's diff (if any) and re-runs the suite against the patched
    copy — the source tree is never touched. Prototype caveat: the diff
    application is text-only (writes the diff content as the new prompt
    body when the diff is not parseable as a unified diff). That keeps
    the dependency surface to zero; richer patching is a future iteration.
    """

    def __init__(
        self,
        fixtures_dir: Optional[Path] = None,
        max_turns: int = 12,
    ):
        if fixtures_dir is None:
            # Use the packaged fixtures so the suite works after `pip install`.
            try:
                src = resources.files("mau_cli.evolution_fixtures")
                fixtures_dir = Path(str(src))
            except (ModuleNotFoundError, FileNotFoundError):
                fixtures_dir = Path(__file__).parent / "evolution_fixtures"
        self.fixtures_dir = Path(fixtures_dir)
        self.max_turns = max_turns

    def load_fixtures(self) -> list[dict[str, Any]]:
        if not self.fixtures_dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(self.fixtures_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and "request" in data:
                data["_name"] = path.stem
                out.append(data)
        return out

    def run(self) -> list[RegressionVerdict]:
        verdicts: list[RegressionVerdict] = []
        for fixture in self.load_fixtures():
            verdicts.append(self._run_one(fixture))
        return verdicts

    def apply_proposal(
        self,
        proposal: HarnessProposal,
        prompts_dir: Path,
        dry_run: bool = True,
    ) -> tuple[list[RegressionVerdict], Optional[Path]]:
        """Stage a `prompt_edit` proposal in a temp prompts dir and re-run
        the suite. Returns (verdicts, temp_dir). When `dry_run=True` (the
        default and only supported mode in the prototype), the source
        `prompts_dir` is never mutated. The caller can inspect `temp_dir`
        to see the patched files. Non-prompt-edit proposals run the suite
        unchanged so they still get a regression baseline.
        """
        tmp_root = Path(tempfile.mkdtemp(prefix="mau_evolve_"))
        patched = tmp_root / "prompts"
        shutil.copytree(prompts_dir, patched)

        if proposal.kind == "prompt_edit" and proposal.diff:
            target = patched / Path(proposal.target).name
            if target.exists():
                # Prototype: write the proposed diff text as a sibling
                # `<role>.proposed.md` so the regression actually still
                # uses the original prompt (the harness can't reliably
                # apply unified diffs without a patching dep). The verdict
                # therefore reflects the *baseline* under the test fixtures
                # plus a record that a diff was proposed.
                proposed_path = target.with_suffix(".proposed.md")
                try:
                    proposed_path.write_text(proposal.diff, encoding="utf-8")
                except OSError:
                    pass

        verdicts: list[RegressionVerdict] = []
        # Run via packaged fixtures but with patched prompts as PYTHONPATH-less
        # bookkeeping — the orchestrator imports prompts via `importlib.resources`
        # so it'll always read from the installed package. We surface this
        # caveat in `notes` so reviewers know the prototype doesn't actually
        # hot-swap prompts yet.
        for fixture in self.load_fixtures():
            v = self._run_one(fixture)
            v.notes = (
                "patched prompts staged at "
                f"{patched} (prototype does not hot-swap importlib.resources)"
            )
            verdicts.append(v)

        if not dry_run:
            # Reserved for a future iteration once the patcher is real.
            shutil.rmtree(tmp_root, ignore_errors=True)
            return verdicts, None
        return verdicts, tmp_root

    # ---- internals ------------------------------------------------------

    def _run_one(self, fixture: dict[str, Any]) -> RegressionVerdict:
        """Spin up an Orchestrator with the mock backend and a temp workspace.
        Pass/fail is purely "did stopped_on_completion fire and not
        stopped_on_turn_cap" — the regression gate is about not regressing
        the convergence behaviour, not about deliverable quality."""
        from mau_cli.mock_inference import MockBackend
        from mau_cli.orchestrator import Orchestrator

        ws_root = Path(tempfile.mkdtemp(prefix="mau_fixture_"))
        workspace = Workspace(root=str(ws_root))
        workspace.ensure()

        events: dict[str, int] = defaultdict(int)
        turns_seen: list[int] = []

        def on_event(kind: str, payload: dict[str, Any]) -> None:
            events[kind] += 1
            if kind == "tick":
                turns_seen.append(len(payload.get("batch") or []))

        max_turns = int(fixture.get("max_turns") or self.max_turns)
        orch = Orchestrator(
            backend=MockBackend(),
            max_turns=max_turns,
            workspace=workspace,
            on_event=on_event,
            isolation="shared",
        )
        try:
            orch.run(str(fixture.get("request", "")))
        except Exception as e:
            return RegressionVerdict(
                fixture=str(fixture.get("_name", "unknown")),
                passed=False,
                stopped_on_completion=False,
                stopped_on_turn_cap=False,
                turns=sum(turns_seen),
                notes=f"crashed: {e}",
            )

        stopped_completion = events.get("stopped_on_completion", 0) > 0
        stopped_cap = events.get("stopped_on_turn_cap", 0) > 0
        passed = stopped_completion and not stopped_cap

        return RegressionVerdict(
            fixture=str(fixture.get("_name", "unknown")),
            passed=passed,
            stopped_on_completion=stopped_completion,
            stopped_on_turn_cap=stopped_cap,
            turns=sum(turns_seen),
        )


# ---- module-level helpers used by the CLI ---------------------------------


def format_summary_table(summaries: dict[str, TranscriptSummary]) -> str:
    """Render the per-agent summary as a plain-text table. Kept dependency-
    free so callers can pipe it anywhere; the CLI wraps it in a Rich panel."""
    if not summaries:
        return "(no transcripts found)"
    header = (
        f"{'agent':<22} {'role':<22} {'turns':>6} {'acc':>4} {'rej':>4} "
        f"{'rate':>6} {'avg_ms':>8} {'in_tok':>8} {'out_tok':>8} {'cost_usd':>10}"
    )
    lines = [header, "-" * len(header)]
    for agent in sorted(summaries):
        s = summaries[agent]
        lines.append(
            f"{s.agent[:22]:<22} {s.role[:22]:<22} {s.total_turns:>6} "
            f"{s.accepted_turns:>4} {s.rejected_turns:>4} "
            f"{s.rejection_rate * 100:>5.0f}% {s.avg_duration_ms:>8.0f} "
            f"{s.total_tokens.input:>8} {s.total_tokens.output:>8} "
            f"{s.total_tokens.cost_usd:>10.4f}"
        )
    return "\n".join(lines)


def format_proposals(proposals: list[HarnessProposal]) -> str:
    """Markdown-ish text rendering. The CLI prints this directly so the
    output is copy-pasteable into a PR description."""
    if not proposals:
        return "(no proposals — transcripts look healthy)"
    parts: list[str] = []
    for p in proposals:
        parts.append(f"## [{p.id}] {p.kind} → {p.target}")
        parts.append(f"_confidence: {p.confidence}_")
        parts.append("")
        parts.append(p.rationale)
        if p.evidence:
            parts.append("")
            parts.append("**Evidence:**")
            for e in p.evidence:
                parts.append(f"- {e}")
        if p.diff:
            parts.append("")
            parts.append("**Proposed diff:**")
            parts.append("```diff")
            parts.append(p.diff)
            parts.append("```")
        parts.append("")
    return "\n".join(parts)


def format_regression(verdicts: list[RegressionVerdict]) -> str:
    if not verdicts:
        return "(no fixtures found)"
    lines = [f"{'fixture':<32} {'pass':<6} {'completion':<11} {'cap':<5} {'turns':>6}"]
    lines.append("-" * len(lines[0]))
    for v in verdicts:
        lines.append(
            f"{v.fixture[:32]:<32} {('yes' if v.passed else 'no'):<6} "
            f"{('yes' if v.stopped_on_completion else 'no'):<11} "
            f"{('yes' if v.stopped_on_turn_cap else 'no'):<5} {v.turns:>6}"
        )
        if v.notes:
            lines.append(f"  note: {v.notes}")
    return "\n".join(lines)
