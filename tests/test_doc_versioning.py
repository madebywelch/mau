"""Task 4: shared_docs versioning + legacy rehydration + full-content prompts."""

from __future__ import annotations

import json
from pathlib import Path

from mau_cli.agent import Agent
from mau_cli.mock_inference import MockBackend
from mau_cli.orchestrator import Orchestrator
from mau_cli.schemas import (
    AgentState,
    DocVersion,
    Role,
    Workspace,
    WorldState,
)


# ---- put_doc / get_doc ------------------------------------------------------


def test_put_doc_dedups_by_hash():
    ws = WorldState()
    v1 = ws.put_doc("prd.md", "hello world", author="product-1", turn=1)
    v2 = ws.put_doc("prd.md", "hello world", author="product-1", turn=2)
    assert v1 is v2  # same object returned
    assert v1.hash == v2.hash
    assert len(ws.shared_docs["prd.md"]) == 1


def test_put_doc_appends_on_change():
    ws = WorldState()
    v1 = ws.put_doc("prd.md", "v1 content", author="a", turn=1)
    v2 = ws.put_doc("prd.md", "v2 content", author="a", turn=2)
    assert v1 is not v2
    assert v1.hash != v2.hash
    assert len(ws.shared_docs["prd.md"]) == 2
    assert ws.get_doc("prd.md") == "v2 content"
    assert ws.get_doc_version("prd.md") is v2


# ---- doc_updated event ------------------------------------------------------


def test_publish_doc_emits_doc_updated_with_diff(
    mock_orchestrator, tmp_workspace, event_recorder
):
    on_event, captured = event_recorder()
    orch = mock_orchestrator(on_event=on_event)
    orch._ensure_isolation()

    orch._publish_doc(name="api.md", content="line a\nline b\n", author="tl")
    captured.clear()
    orch._publish_doc(name="api.md", content="line a\nline c\n", author="tl")

    doc_updates = [p for k, p in captured if k == "doc_updated"]
    assert doc_updates, "expected a doc_updated event"
    payload = doc_updates[-1]
    assert "prev_hash" in payload and payload["prev_hash"] is not None
    assert "new_hash" in payload and payload["new_hash"]
    assert "diff_preview" in payload
    # On a real change, the unified diff should reference the from/to files.
    assert "---" in payload["diff_preview"] or "+++" in payload["diff_preview"]


# ---- legacy snapshot loader -------------------------------------------------


def test_legacy_shared_docs_snapshot_rehydrates(tmp_workspace):
    """A pre-Task-4 session.json had `shared_docs: {name: "content str"}`.
    `_rehydrate_shared_docs` must wrap each into one DocVersion."""
    snapshot = {
        "shared_docs": {"a.md": "hi"},
    }
    orch = Orchestrator(backend=MockBackend(), workspace=tmp_workspace, isolation="shared")
    orch._rehydrate_shared_docs(snapshot["shared_docs"])

    versions = orch.world.shared_docs["a.md"]
    assert len(versions) == 1
    v = versions[0]
    assert isinstance(v, DocVersion)
    assert v.content == "hi"
    assert v.author == "legacy"
    assert v.turn == 0
    assert v.hash  # populated by _doc_hash


# ---- build_user_prompt full doc content -------------------------------------


def test_build_user_prompt_includes_full_doc_content(tmp_workspace):
    """A >5000-char doc must show up in full (not truncated to 4000)."""
    world = WorldState(workspace=tmp_workspace)
    big_marker = "MARKER-{i}-MARKER"
    body = "\n".join(big_marker.format(i=i) for i in range(800))  # ~14k chars
    assert len(body) > 5000
    world.put_doc("prd.md", body, author="product-1", turn=1)

    state = AgentState(name="qa-1", role=Role.QA)
    world.agents["qa-1"] = state
    agent = Agent(state, MockBackend())

    prompt = agent.build_user_prompt(world)
    # The entire doc must appear in the rendered prompt.
    assert body in prompt, "doc content was truncated in the rendered prompt"
    # And the very last line of the doc should also be there (sanity).
    assert body.splitlines()[-1] in prompt
