# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""UI-1 — the Accuracy page's *what this page does not publish* list is generated.

``legba-ui-v3/src/lib/not_published.json`` is written by
``scripts/gen_not_published.py`` from docs/SEAMS.md's measurement seams and
the eval routes' declared unmeasured classes. The committed file must be
byte-identical to a fresh generation (the ``gen_profile_ui.py --check``
idiom), and a declared source that disappears must fail the generation
rather than publish a stale absence. Pure file reads: no database, no import
of the route modules (the generator reads them with ``ast``).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "gen_not_published", REPO_ROOT / "scripts" / "gen_not_published.py"
)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

COMMITTED = REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "not_published.json"


def test_committed_json_equals_a_fresh_generation() -> None:
    assert gen.OUT == COMMITTED
    assert COMMITTED.read_text(encoding="utf-8") == gen.render(), (
        "stale: run python3 scripts/gen_not_published.py"
    )
    assert gen.main(["--check"]) == 0


def test_every_measurement_seam_is_declared_and_open() -> None:
    doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert doc["schema"] == gen.SCHEMA
    assert [s["seam"] for s in doc["seams"]] == list(gen.MEASUREMENT_SEAMS)
    for item in doc["seams"]:
        assert item["title"] and item["summary"] and item["guard"]
        assert item["source"] == f"docs/SEAMS.md #{item['seam']}"


def test_every_route_class_carries_the_routes_own_words() -> None:
    doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert [r["id"] for r in doc["route_classes"]] == [c["id"] for c in gen.ROUTE_CLASSES]
    for item, spec in zip(doc["route_classes"], gen.ROUTE_CLASSES):
        assert item["route"].startswith("/v3/eval/")
        assert item["source"] == f"{spec['file']}:{spec['symbol']}"
        assert len(item["text"]) > 40
        # the model's marker label is dropped; the sentence is kept
        assert not item["text"].startswith(("HONESTY", "TINY-n"))
    # the grader roster's unmeasured class is the Field description, verbatim
    grader = next(r for r in doc["route_classes"] if r["id"] == "grader_unmeasured")
    assert "NULL correctness share" in grader["text"]
    assert "Excluded from both means" in grader["text"]


def test_a_resolved_seam_fails_the_generation() -> None:
    md = (REPO_ROOT / "docs" / "SEAMS.md").read_text(encoding="utf-8")
    # 57 is in the resolved appendix: listing it must refuse, not publish.
    with pytest.raises(gen.GenerationError, match="resolved appendix"):
        gen.seam_items(md, (46, 57))
    with pytest.raises(gen.GenerationError, match="not a row"):
        gen.seam_items(md, (9999,))


def test_a_vanished_route_declaration_fails_the_generation(tmp_path: Path) -> None:
    for spec in gen.ROUTE_CLASSES:
        src = REPO_ROOT / str(spec["file"])
        dst = tmp_path / str(spec["file"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    target = tmp_path / "src/legba/data/provenance/external_truth.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("HONESTY_NOTE = (", "RENAMED_NOTE = ("),
        encoding="utf-8",
    )
    with pytest.raises(gen.GenerationError, match="external_truth"):
        gen.route_items(tmp_path)
