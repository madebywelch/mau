"""Regression: persistence skips byte-identical writes, and pending user
questions survive a resume."""

from __future__ import annotations

from pathlib import Path

from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import Message, Workspace


def test_persist_skips_identical_snapshots(tmp_workspace):
    """Idle / error-backoff loop iterations re-call _persist with unchanged
    state; the write (and atomic rename) must be skipped so we don't churn the
    disk re-emitting the same bytes. A changed snapshot is still written."""
    orch = Orchestrator(
        backend=MockBackend(), workspace=tmp_workspace, isolation="shared"
    )
    session = Path(tmp_workspace.session_file)

    orch._persist()
    assert session.exists()

    # Remove the file, then persist again with NO state change: because the
    # serialized snapshot is identical, the write is skipped and the file is
    # NOT recreated.
    session.unlink()
    orch._persist()
    assert not session.exists(), "identical snapshot must not be rewritten"

    # A real state change must be persisted.
    orch.world.request = "something different"
    orch._persist()
    assert session.exists(), "changed snapshot must be written"


def test_pending_user_questions_round_trip(tmp_workspace):
    """Open questions/escalations to the human must be in the snapshot and be
    restored on resume — previously they were dropped."""
    orch = Orchestrator(
        backend=MockBackend(), workspace=tmp_workspace, isolation="shared"
    )
    orch.bus.deliver(
        Message(
            from_agent="qa-1",
            to_agent="user",
            msg_type="question",
            subject="which DB?",
            body="postgres or sqlite?",
        )
    )

    snap = orch.world.snapshot()
    assert snap["pending_user_questions"], "must be present in the snapshot"

    fresh = Orchestrator(
        backend=MockBackend(),
        workspace=Workspace(root=tmp_workspace.root),
        isolation="shared",
    )
    fresh.load_from_disk(snap)

    assert len(fresh.world.pending_user_questions) == 1
    assert fresh.world.pending_user_questions[0].subject == "which DB?"
