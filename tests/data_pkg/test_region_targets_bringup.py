# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2-T1 — tests for the region-frame bring-up script.

Two layers, no live HTTP:

1. Every synthesised region-frame body validates against the real
   ``TargetDescriptor`` schema and has the frame shape the design calls for:
   a non-geo thematic FRAME, tagged ``region``, active with >=1 source, and
   crucially NO inline analyst (frames only — nothing subscribes yet).
2. The REST registration path (``register_frames``) against a mocked registry
   (``httpx.MockTransport``): CREATE all five when absent; idempotent /
   create-only (no POST) when heads already exist; ``--dry-run`` touches no
   network.
"""

from __future__ import annotations

import json
import pathlib
import sys

import httpx
import pytest

from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.target import TargetDescriptor

# The script uses the REST surface (httpx + _token.py); no _p17_registrar
# import, so importing it here has no LEGBA_DATA_PG_DB side effect.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
from bringup_register_region_targets import (  # noqa: E402
    FAMILY,
    REGIONS,
    build_body,
    build_region_frames,
    main,
    register_frames,
)

EXPECTED_IDS = {
    "region_europe",
    "region_mena",
    "region_indo_pacific",
    "region_americas",
    "region_africa",
}


# ---------------------------------------------------------------------------
# 1. The five frames validate + have the frame shape
# ---------------------------------------------------------------------------


def test_exactly_five_frames_with_expected_ids():
    frames = build_region_frames()
    assert len(frames) == 5
    assert {desc_id for desc_id, _body, _desc in frames} == EXPECTED_IDS


def test_frame_ids_are_unique():
    ids = [desc_id for desc_id, _body, _desc in build_region_frames()]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("region", REGIONS, ids=lambda r: r["slug"])
def test_frame_is_a_valid_tagged_thematic_region_frame(region):
    body = build_body(region)
    # Validates against the real schema (raises on any violation).
    desc = TargetDescriptor.model_validate(body, strict=False)

    # Tagged 'region' (the generic frame tag S2-T2's read filter keys on) plus
    # its own region_<slug> coverage tag (the tag the 24 country desks carry).
    assert "region" in desc.scope.tags
    assert f"region_{region['slug']}" in desc.scope.tags

    # A non-geo thematic FRAME (mirrors the iran_war situation target).
    assert desc.scope.domain == "thematic"
    assert desc.scope.geo == []
    assert desc.scope.predicate is None

    # Active + >=1 source — the schema constraint for a non-discovery active
    # target (the broad news selector satisfies it; nothing consumes it yet).
    assert desc.identity.state == LifecycleState.ACTIVE
    assert len(desc.sources) >= 1

    # Frames ONLY: no inline analyst subscribes (region_composition is S2-T2).
    assert desc.analyst is None
    assert desc.outputs == []

    # Id / name shape.
    assert desc.identity.id == f"region_{region['slug']}"
    assert desc.identity.name.startswith("Region — ")


# ---------------------------------------------------------------------------
# 2. REST registration against a mocked registry
# ---------------------------------------------------------------------------


def _mock_client(*, existing: set[str], posts: list[dict]) -> httpx.Client:
    """A sync httpx client whose transport records POSTs and answers GET-head
    with 200 for ids in ``existing`` else 404."""

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            desc_id = path.rsplit("/", 1)[-1]
            if desc_id in existing:
                return httpx.Response(200, json={"version": "deadbeefdeadbeef"})
            return httpx.Response(404)
        if request.method == "POST":
            assert path.endswith(f"/descriptors/{FAMILY}"), path
            posts.append(json.loads(request.content))
            return httpx.Response(201, json={"version": "cafebabecafebabe"})
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    return httpx.Client(
        transport=httpx.MockTransport(_handler),
        base_url="http://registry.test/api/v1/registry",
    )


def test_register_creates_all_five_when_absent():
    posts: list[dict] = []
    with _mock_client(existing=set(), posts=posts) as client:
        results, failures = register_frames(client)

    assert not failures
    assert len(results) == 5
    assert all(action == "registered" for action, _id, _ver in results)
    assert {desc_id for _action, desc_id, _ver in results} == EXPECTED_IDS

    # Every POSTed body is a valid TargetDescriptor tagged 'region'.
    assert len(posts) == 5
    assert {json.dumps(b, sort_keys=True) for b in posts}  # all distinct-ish
    posted_ids = set()
    for body in posts:
        desc = TargetDescriptor.model_validate(body, strict=False)
        assert "region" in desc.scope.tags
        posted_ids.add(desc.identity.id)
    assert posted_ids == EXPECTED_IDS


def test_register_is_idempotent_create_only():
    posts: list[dict] = []
    with _mock_client(existing=EXPECTED_IDS, posts=posts) as client:
        results, failures = register_frames(client)

    assert not failures
    assert len(results) == 5
    assert all(action == "exists" for action, _id, _ver in results)
    # Create-only: an existing head is never re-POSTed / PUT.
    assert posts == []


def test_partial_reregister_only_creates_the_missing_frame():
    already = EXPECTED_IDS - {"region_africa"}
    posts: list[dict] = []
    with _mock_client(existing=already, posts=posts) as client:
        results, failures = register_frames(client)

    assert not failures
    actions = {desc_id: action for action, desc_id, _ver in results}
    assert actions["region_africa"] == "registered"
    assert all(actions[i] == "exists" for i in already)
    assert len(posts) == 1
    assert (
        TargetDescriptor.model_validate(posts[0], strict=False).identity.id
        == "region_africa"
    )


def test_dry_run_returns_zero_and_prints_all_five(capsys):
    rc = main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    for desc_id in EXPECTED_IDS:
        assert desc_id in out
