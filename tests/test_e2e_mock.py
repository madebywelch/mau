"""End-to-end mock-backend smoke test + session.json round-trip."""

from __future__ import annotations

import json
from pathlib import Path

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import Workspace


def test_full_mock_run_completes(tmp_workspace):
    events: list[tuple[str, dict]] = []
    orch = Orchestrator(
        backend=MockBackend(),
        max_turns=30,
        concurrency=2,
        workspace=tmp_workspace,
        on_event=lambda k, p: events.append((k, p)),
        isolation="shared",
    )
    orch.run("build a hello-world feature with tests")

    kinds = [k for k, _ in events]
    assert "stopped_on_completion" in kinds, (
        f"run did not converge; events were {sorted(set(kinds))}"
    )
    assert "stopped_on_turn_cap" not in kinds, (
        "max_turns reached — mock run should converge well below the cap"
    )


def test_transcripts_written_for_run(tmp_workspace):
    orch = Orchestrator(
        backend=MockBackend(),
        max_turns=30,
        workspace=tmp_workspace,
        isolation="shared",
    )
    orch.run("build a hello-world feature")

    logs_dir = Path(tmp_workspace.logs_dir)
    assert any(
        logs_dir.glob("*.jsonl")
    ), f"no transcripts written under {logs_dir}"


def test_session_json_round_trips_persistent_fields(tmp_workspace):
    """session.json round-trips: load it back and confirm every persistent
    field is preserved (shared_docs versions, policies, criteria state,
    satisfied_doc_versions)."""
    orch = Orchestrator(
        backend=MockBackend(),
        max_turns=30,
        workspace=tmp_workspace,
        isolation="shared",
    )

    # Seed a policy explicitly so we know one exists post-run regardless of
    # whether the mock backend records any via record_policy actions.
    orch._ensure_isolation()
    orch.world.add_policy("test policy", "global", "user", turn=0)

    orch.run("build a hello-world feature with tests")

    raw = Path(tmp_workspace.session_file).read_text()
    assert raw, "session.json wasn't written"
    snapshot = json.loads(raw)

    # Sanity: top-level fields exist.
    assert snapshot["request"]
    assert "shared_docs" in snapshot
    assert "policies" in snapshot
    assert "tasks" in snapshot

    # Now rehydrate into a fresh orchestrator and confirm field-by-field.
    fresh_ws = Workspace(root=tmp_workspace.root)
    fresh = Orchestrator(
        backend=MockBackend(), workspace=fresh_ws, isolation="shared"
    )
    fresh.load_from_disk(snapshot)

    # 1. shared_docs versions preserved (latest content + per-version hash).
    for name, original_versions in orch.world.shared_docs.items():
        rehydrated = fresh.world.shared_docs.get(name)
        assert rehydrated is not None, f"missing rehydrated doc {name}"
        # Same number of versions (no dedup loss).
        assert len(rehydrated) == len(original_versions), (
            f"doc {name}: had {len(original_versions)} versions, now {len(rehydrated)}"
        )
        # Latest content + hash preserved.
        assert rehydrated[-1].content == original_versions[-1].content
        assert rehydrated[-1].hash == original_versions[-1].hash

    # 2. policies preserved.
    original_ids = {p.id for p in orch.world.policies}
    rehydrated_ids = {p.id for p in fresh.world.policies}
    assert original_ids == rehydrated_ids, (
        f"policy IDs differ: original={original_ids} rehydrated={rehydrated_ids}"
    )

    # 3. tasks: acceptance criteria + last_status preserved + satisfied_doc_versions.
    for tid, orig_task in orch.world.tasks.items():
        rehydrated_task = fresh.world.tasks.get(tid)
        assert rehydrated_task is not None, f"missing task {tid}"
        assert rehydrated_task.status == orig_task.status
        assert len(rehydrated_task.acceptance_criteria) == len(
            orig_task.acceptance_criteria
        )
        for orig_c, new_c in zip(
            orig_task.acceptance_criteria, rehydrated_task.acceptance_criteria
        ):
            assert orig_c.text == new_c.text
            assert orig_c.verifier == new_c.verifier
            assert orig_c.last_status == new_c.last_status
        assert (
            rehydrated_task.satisfied_doc_versions
            == orig_task.satisfied_doc_versions
        )
