# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI validation tests for ``scripts/manual_ingest_vectors.py``'s exemplar-corpus
flags (``--corpus`` / ``--exemplar-id``) — INFRA-FREE (dry-run only: no DB, no
Qdrant, no embedder, no registry).

Covers the task's required cases:

  * a VALID ready shelf id is accepted;
  * an ALIAS of a merged pattern resolves and is accepted;
  * a RETIRED id is refused, with the shelf's own reversal note printed;
  * an UNKNOWN corpus is still refused (with or without ``--exemplar-id``).

Plus: a HELD (not-yet-ready) id is refused, ``--exemplar-id`` without
``--corpus exemplar`` errors, and ``--corpus exemplar`` without
``--exemplar-id`` errors. None of this touches the real curated
``seeds/exemplar_shelf.yaml`` (gitignored) — every test writes its own small
shelf yaml via ``--exemplar-shelf``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "manual_ingest_vectors", REPO_ROOT / "scripts" / "manual_ingest_vectors.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


miv = _load_script()


_SHELF_RAW: dict[str, Any] = {
    "version": 1,
    "patterns": [
        {
            "id": "port_capacity_crisis",
            "slot": "15",
            "title": "Port Capacity Crisis",
            "doctrine_source": "Port/congestion economics",
            "research_ref": "planning/exemplars_research/t2_maritime_economic.md",
            "status": "ready",
        },
        {
            "id": "prewar_mobilization_warning",
            "slot": "3",
            "title": "Prewar Mobilization Warning",
            "doctrine_source": "Grabo I&W tradition",
            "research_ref": "planning/exemplars_research/t1_military_coercive.md",
            "status": "ready",
            "aliases": ["exercise_as_cover"],
            "merged_from": [{"id": "exercise_as_cover", "variant": "exercise_as_cover"}],
            "variants": [{"id": "exercise_as_cover", "framing": "snap-exercise cover"}],
        },
        {
            "id": "export_control_minerals_cascade",
            "slot": "12",
            "title": "Export-Control Minerals Cascade",
            "doctrine_source": "Export-control / critical-minerals coercion literature",
            "research_ref": "planning/exemplars_research/t2_maritime_economic.md",
            "status": "hold",
            "authoring_gate": "no clean stalled-negative found",
        },
    ],
    "retired": [
        {
            "id": "hybrid_composite",
            "slot": "22",
            "title": "Hybrid Composite",
            "disposition": "killed",
            "reason": "no clean discriminator against ordinary escalation",
            "reversal": "Operator override: re-admit with a CONTESTED flag "
            "if current relevance is reweighted.",
        }
    ],
}


def _write_shelf(tmp_path: Path) -> str:
    path = tmp_path / "exemplar_shelf.yaml"
    path.write_text(yaml.safe_dump(_SHELF_RAW), encoding="utf-8")
    return str(path)


def _write_batch(tmp_path: Path, *, corpus: str = "exemplar", exemplar_id: str | None = None) -> str:
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    (batch_dir / "batch_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "batch_id": "cli-test-batch",
                "operator": "test",
                "created_at": "2026-07-02T00:00:00Z",
                "default_provenance": "curated",
                "mode": "skip",
                "license": "CC0-1.0",
                "source_url": "https://example.invalid/doc",
                "files": {"docs": "docs.jsonl"},
            }
        ),
        encoding="utf-8",
    )
    doc: dict[str, Any] = {
        "corpus": corpus,
        "doc_id": "d1",
        "chunk_seq": 0,
        "text": "some baseline text for the batch",
    }
    if exemplar_id is not None:
        doc["data"] = {"exemplar_id": exemplar_id}
    (batch_dir / "docs.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")
    return str(batch_dir)


def _invoke(batch_dir: str, **kw: Any) -> int:
    kw.setdefault("mode", None)
    kw.setdefault("dry_run", True)
    kw.setdefault("strict", False)
    kw.setdefault("corpus", None)
    kw.setdefault("exemplar_id", None)
    kw.setdefault("shelf_path", None)
    return asyncio.run(miv._run(batch_dir, **kw))


# ---------------------------------------------------------------------------
# the required cases
# ---------------------------------------------------------------------------


def test_valid_ready_id_accepted(tmp_path):
    shelf_path = _write_shelf(tmp_path)
    batch_dir = _write_batch(tmp_path, exemplar_id="port_capacity_crisis")
    code = _invoke(
        batch_dir,
        corpus="exemplar",
        exemplar_id="port_capacity_crisis",
        shelf_path=shelf_path,
    )
    assert code == 0


def test_alias_id_accepted(tmp_path):
    shelf_path = _write_shelf(tmp_path)
    batch_dir = _write_batch(tmp_path, exemplar_id="exercise_as_cover")
    code = _invoke(
        batch_dir,
        corpus="exemplar",
        exemplar_id="exercise_as_cover",
        shelf_path=shelf_path,
    )
    assert code == 0


def test_retired_id_refused_with_reversal_note(tmp_path, capsys):
    shelf_path = _write_shelf(tmp_path)
    # Pre-flight refuses before the batch dir is even read.
    code = _invoke(
        str(tmp_path / "never-read"),
        corpus="exemplar",
        exemplar_id="hybrid_composite",
        shelf_path=shelf_path,
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "retired" in err
    assert "Operator override: re-admit" in err


def test_unknown_corpus_still_refused(tmp_path, capsys):
    code = _invoke(str(tmp_path / "never-read"), corpus="not_a_real_corpus")
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown corpus" in err
    assert "not_a_real_corpus" in err


# ---------------------------------------------------------------------------
# plus: held id, and the flag-pairing rules
# ---------------------------------------------------------------------------


def test_held_id_refused(tmp_path, capsys):
    shelf_path = _write_shelf(tmp_path)
    code = _invoke(
        str(tmp_path / "never-read"),
        corpus="exemplar",
        exemplar_id="export_control_minerals_cascade",
        shelf_path=shelf_path,
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "hold" in err
    assert "no clean stalled-negative found" in err


def test_exemplar_corpus_requires_exemplar_id(tmp_path, capsys):
    code = _invoke(str(tmp_path / "never-read"), corpus="exemplar")
    assert code == 2
    assert "requires --exemplar-id" in capsys.readouterr().err


def test_exemplar_id_without_corpus_exemplar_errors(tmp_path, capsys):
    code = _invoke(
        str(tmp_path / "never-read"), exemplar_id="port_capacity_crisis"
    )
    assert code == 2
    assert "requires --corpus" in capsys.readouterr().err


def test_world_context_corpus_unaffected_by_exemplar_flags(tmp_path):
    """The pre-existing behaviour (no --corpus/--exemplar-id at all) is
    unchanged — a plain world_context-style batch still loads on dry-run."""
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    (batch_dir / "batch_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "batch_id": "plain-batch",
                "operator": "test",
                "created_at": "2026-07-02T00:00:00Z",
                "default_provenance": "curated",
                "mode": "skip",
                "files": {"docs": "docs.jsonl"},
            }
        ),
        encoding="utf-8",
    )
    (batch_dir / "docs.jsonl").write_text(
        json.dumps(
            {"corpus": "world_context", "doc_id": "d1", "chunk_seq": 0, "text": "hello"}
        )
        + "\n",
        encoding="utf-8",
    )
    code = _invoke(str(batch_dir))
    assert code == 0
