"""Deterministic mock backend.

Plan mode: scripted JSON responses for Product / EM / Tech Lead so the
orchestrator can be exercised offline.

Agentic mode: actually writes stub files into the workspace based on role
and role specialization parsed from the system prompt. Demonstrates the
full pipeline end-to-end without spending tokens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from mau_cli.inference import InferenceBackend, InferenceResult
from mau_cli.schemas import TokenUsage


def _wrap_plan(parsed: dict[str, Any]) -> InferenceResult:
    return InferenceResult(
        raw_text=json.dumps(parsed),
        parsed=parsed,
        backend="mock",
        duration_ms=10,
        usage=TokenUsage(input_tokens=120, output_tokens=80, cost_usd=0.0, calls=1),
    )


def _wrap_agentic(text: str, deliverable: dict[str, Any], files: list[str]) -> InferenceResult:
    return InferenceResult(
        raw_text=text,
        parsed=deliverable,
        backend="mock",
        duration_ms=20,
        usage=TokenUsage(input_tokens=300, output_tokens=200, cost_usd=0.0, calls=1),
        files_touched=files,
    )


def _detect_role(system_prompt: str) -> str:
    m = re.search(r"ROLE:\s*([A-Z_ ]+)", system_prompt)
    return m.group(1).strip().lower().replace(" ", "_") if m else ""


def _detect_specialization(user_prompt: str) -> str:
    m = re.search(r"SPECIALIZATION:\s*(.+)", user_prompt)
    return m.group(1).strip() if m else ""


class MockBackend(InferenceBackend):
    name = "mock"

    def available(self) -> bool:
        return True

    # ---- plan mode -------------------------------------------------------

    def call_plan(self, system_prompt: str, user_prompt: str) -> InferenceResult:
        role = _detect_role(system_prompt)
        if role == "product":
            return _wrap_plan(self._product())
        if role == "engineering_manager":
            return _wrap_plan(self._em())
        if role == "tech_lead":
            return _wrap_plan(self._tl())
        return _wrap_plan({"thoughts": "unknown role", "status": "complete", "actions": []})

    # ---- agentic mode ----------------------------------------------------

    def call_agentic(
        self,
        system_prompt: str,
        user_prompt: str,
        workspace_dir: str,
        extra_dirs: Optional[list[str]] = None,
        max_budget_usd: Optional[float] = None,
    ) -> InferenceResult:
        role = _detect_role(system_prompt)
        spec = _detect_specialization(user_prompt)
        ws = Path(workspace_dir)
        ws.mkdir(parents=True, exist_ok=True)

        if role == "database":
            return self._write_db(ws)
        if role == "backend":
            return self._write_be(ws)
        if role == "frontend":
            return self._write_fe(ws, spec)
        if role == "qa":
            return self._write_qa(ws)
        if role == "devops":
            return self._write_devops(ws)

        # Fallback: drop a placeholder note
        path = ws / f"NOTE-{role or 'agent'}.md"
        path.write_text(f"# Mock {role} note\n\n{user_prompt[:200]}\n")
        return _wrap_agentic(
            f"Wrote {path}",
            {"title": "note", "summary": "Mock placeholder.", "files_touched": [str(path.relative_to(ws))]},
            [str(path.relative_to(ws))],
        )

    # ---- planner scripts -------------------------------------------------

    def _product(self) -> dict[str, Any]:
        return {
            "thoughts": "Reframing the request, publishing PRD, handing to EM.",
            "status": "complete",
            "actions": [
                {
                    "type": "write_doc",
                    "name": "prd.md",
                    "content": (
                        "# PRD\n\n"
                        "## Problem\nUsers need this capability.\n\n"
                        "## Goal\nDeliver a working end-to-end MVP slice.\n\n"
                        "## Scope\n- In: core flow.\n- Out: ancillary niceties.\n"
                    ),
                },
                {
                    "type": "spawn_agent",
                    "role": "engineering_manager",
                    "name": "em-1",
                    "specialization": "feature delivery",
                },
                {
                    "type": "send_message",
                    "to": "em-1",
                    "msg_type": "directive",
                    "subject": "PRD published",
                    "body": "See prd.md. Take it from here.",
                },
                {"type": "complete", "summary": "PRD delivered."},
            ],
        }

    def _em(self) -> dict[str, Any]:
        return {
            "thoughts": "Engaging tech lead with the epic.",
            "status": "complete",
            "actions": [
                {
                    "type": "spawn_agent",
                    "role": "tech_lead",
                    "name": "tl-1",
                    "specialization": "feature delivery",
                },
                {
                    "type": "send_message",
                    "to": "tl-1",
                    "msg_type": "directive",
                    "subject": "Epic: deliver requested feature",
                    "body": "Decompose into FE/BE/DB streams. Define contracts first.",
                },
                {"type": "complete", "summary": "Epic delegated."},
            ],
        }

    def _tl(self) -> dict[str, Any]:
        return {
            "thoughts": "Publishing contracts, spawning specialists, wiring deps.",
            "status": "complete",
            "actions": [
                {
                    "type": "write_doc",
                    "name": "api-contract.md",
                    "content": (
                        "# API contract\n\n"
                        "GET  /items       → 200 [{id,title}]\n"
                        "POST /items {title} → 201 {id,title}\n"
                    ),
                },
                {
                    "type": "write_doc",
                    "name": "schema.md",
                    "content": (
                        "# Schema\n\n"
                        "items(id PK, title TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())\n"
                    ),
                },
                {"type": "spawn_agent", "role": "database", "name": "db-1"},
                {"type": "spawn_agent", "role": "backend", "name": "be-1"},
                {"type": "spawn_agent", "role": "frontend", "name": "fe-1"},
                {
                    "type": "create_task",
                    "id": "task_db",
                    "title": "Implement schema migration",
                    "assignee": "db-1",
                    "depends_on": [],
                },
                {
                    "type": "create_task",
                    "id": "task_be",
                    "title": "Implement API per contract",
                    "assignee": "be-1",
                    "depends_on": ["task_db"],
                },
                {
                    "type": "create_task",
                    "id": "task_fe",
                    "title": "Build UI consuming /items",
                    "assignee": "fe-1",
                    "depends_on": ["task_be"],
                },
                {"type": "complete", "summary": "Streams launched."},
            ],
        }

    # ---- specialist file emitters ----------------------------------------

    def _write_db(self, ws: Path) -> InferenceResult:
        path = ws / "migrations" / "001_init.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "CREATE TABLE items (\n"
            "  id BIGSERIAL PRIMARY KEY,\n"
            "  title TEXT NOT NULL,\n"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n"
            ");\n"
        )
        rel = str(path.relative_to(ws))
        deliverable = {
            "title": "Schema v1",
            "summary": "Created items table with id/title/created_at.",
            "files_touched": [rel],
        }
        return _wrap_agentic(f"Wrote {rel}\n<DELIVERABLE>{json.dumps(deliverable)}</DELIVERABLE>",
                             deliverable, [rel])

    def _write_be(self, ws: Path) -> InferenceResult:
        path = ws / "server" / "items.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "from fastapi import APIRouter\n\n"
            "router = APIRouter()\n\n"
            "_items: list[dict] = []\n\n"
            "@router.get('/items')\n"
            "def list_items():\n"
            "    return _items\n\n"
            "@router.post('/items')\n"
            "def create_item(payload: dict):\n"
            "    item = {'id': len(_items)+1, 'title': payload['title']}\n"
            "    _items.append(item)\n"
            "    return item\n"
        )
        rel = str(path.relative_to(ws))
        deliverable = {
            "title": "API v1",
            "summary": "GET/POST /items implemented per contract.",
            "files_touched": [rel],
        }
        return _wrap_agentic(f"Wrote {rel}\n<DELIVERABLE>{json.dumps(deliverable)}</DELIVERABLE>",
                             deliverable, [rel])

    def _write_fe(self, ws: Path, spec: str) -> InferenceResult:
        suffix = re.sub(r"[^a-z0-9]+", "-", spec.lower()).strip("-") or "main"
        path = ws / "web" / f"{suffix}.tsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import { useEffect, useState } from 'react';\n\n"
            "export function Items() {\n"
            "  const [items, setItems] = useState<{id:number;title:string}[]>([]);\n"
            "  useEffect(() => { fetch('/items').then(r=>r.json()).then(setItems); }, []);\n"
            "  return (<ul>{items.map(i => <li key={i.id}>{i.title}</li>)}</ul>);\n"
            "}\n"
        )
        rel = str(path.relative_to(ws))
        deliverable = {
            "title": f"UI: {spec or 'items list'}",
            "summary": "List view consuming GET /items.",
            "files_touched": [rel],
        }
        return _wrap_agentic(f"Wrote {rel}\n<DELIVERABLE>{json.dumps(deliverable)}</DELIVERABLE>",
                             deliverable, [rel])

    def _write_qa(self, ws: Path) -> InferenceResult:
        path = ws / "tests" / "test_items.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "def test_create_and_list():\n"
            "    # placeholder — wire up to a real API client.\n"
            "    assert True\n"
        )
        rel = str(path.relative_to(ws))
        deliverable = {
            "title": "Test plan v1",
            "summary": "Smoke test placeholder for items API.",
            "files_touched": [rel],
        }
        return _wrap_agentic(f"Wrote {rel}\n<DELIVERABLE>{json.dumps(deliverable)}</DELIVERABLE>",
                             deliverable, [rel])

    def _write_devops(self, ws: Path) -> InferenceResult:
        path = ws / "Dockerfile"
        path.write_text("FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"-m\", \"server\"]\n")
        rel = str(path.relative_to(ws))
        deliverable = {
            "title": "Container image",
            "summary": "Minimal Dockerfile.",
            "files_touched": [rel],
        }
        return _wrap_agentic(f"Wrote {rel}\n<DELIVERABLE>{json.dumps(deliverable)}</DELIVERABLE>",
                             deliverable, [rel])
