# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1-FIX D1 — a forced run's ``as_of`` and targets reach the handler.

WHAT BROKE, LIVE. On 2026-09-16 the grader's first forced run PUT
``{"trigger_kind": "method", "as_of": "2026-09-16T19:30:00+00:00",
"grader_targets": ["country_watch_il"]}`` to the actor's ``method/run``. The
sidecar answered 200, the dispatch log said ``sub_handler=correctness_grader``,
and the finding said ``as_of=2026-09-16T22:32:27`` — the wall clock — and "wrote
no unit numbers (no targets)". Both body values had been dropped.

WHERE. ``AnalystActor.run`` reads its OWN contract keys (``trigger_kind``,
``target_filter``) off the top level of the payload and merges the handler's
per-run parameters from the NESTED ``options`` object only
(``for k, v in (payload.get("options") or {}).items()``). Nothing reads an
unknown top-level key, and nothing complains about one: the script was sending
parameters down a channel that does not exist. ``dapr_actors.py`` is frozen and
correct here — the fix is the script, and this file is the proof, so the same
shape cannot silently rot again.

WHY IT IS AN ACTOR TEST AND NOT A HANDLER TEST.
``tests/data_pkg/test_correctness_grader.py`` already drives
``deterministic.run_method`` end to end against a real Postgres, and it PASSED
throughout the defect: the drop happened one layer ABOVE the dispatcher, in the
payload→options assembly, which only the actor's own ``run`` performs. A test
that starts at the dispatcher cannot see this class of bug at all. So this one
starts where the sidecar does — ``AnalystActor.run(body)``, with the body
:func:`scripts.correctness_grade_now.build_body` actually sends — over the REAL
descriptor YAML, the REAL deterministic dispatcher and a REAL Postgres, and
asserts on the ``unit_correctness`` row's ``as_of`` column: the stamp that
reaches the database, not a mapping somebody inspected on the way past.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import _correctness_grade as GRADE
from legba.data.analysts.deterministic_handlers import correctness_grader as CG
from legba.data.analysts.deterministic_handlers._correctness_rubric import (
    RUBRIC_SHA256,
)
from legba.data.config import PostgresConfig
from legba.data.schemas.analyst import AnalystDescriptor
from legba.runtime import dapr_actors
from legba.runtime.dapr_actors import ACTIVE, AnalystActor, _AnalystDeps

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTOR_PATH = REPO_ROOT / "descriptors" / "analyst_correctness_grader.yaml"

#: The stamp the live forced run asked for — the one that was dropped.
BODY_AS_OF = datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc)
TARGET = "country_watch_il_methodbody"
_VERSION = "abcdef0123456789" + "0" * 48
_ACTOR_ID = f"analyst::correctness_grader::{_VERSION[:16]}"

#: One judgeable desk claim, and the reference development that bears on it.
_DESK_BODY = (
    "Israel's 27 October general election took formal shape inside the "
    "window: 38 candidate lists were filed with the Central Elections "
    "Committee by the Tuesday deadline, the last of them Likud."
)
_VERBATIM = (
    "Israeli political parties have submitted their slates for the "
    "Knesset elections on October 27."
)
_REFERENCE = {
    "header": {
        "round": "G1FIX", "country": "IL",
        "window": "2026-09-02T19:30:00+00:00 -> 2026-09-16T19:30:00+00:00",
        "builder": "pytest-lane",
    },
    "ref_bands": {"internal_stability": "elevated"},
    "ref_developments": [{
        "item_id": "RD-IL-1",
        "summary": "38 candidate lists were filed by the deadline.",
        "decisive_span": _VERBATIM,
        "outlet": "Al Jazeera",
        "publish_date": "2026-09-09",
        "dimension": "internal_stability",
    }],
    "gaps": [],
}


def _grade_now_module() -> Any:
    """The on-demand script, imported as a module.

    The body under test must be the body the SCRIPT sends; a test that
    hand-wrote the same dict would pass while the script kept sending the
    broken shape. ``scripts/`` is not a package, so it goes on the path the
    same way ``tests/data_pkg/test_voice4_flip_kit.py`` does — and off
    ``__file__``, never a hardcoded repo root, so a worktree tests ITS OWN
    script rather than the main checkout's.
    """
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("correctness_grade_now")


# ---------------------------------------------------------------------------
# The live-substrate half
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async def _wipe(conn):
        await conn.execute(
            "DELETE FROM unit_correctness WHERE target_id = $1", TARGET
        )
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = $1", TARGET
        )
        await conn.execute(
            "DELETE FROM grader_calibrations WHERE notes = 'pytest g1fix'"
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE target_id = $1", TARGET
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'correctness_grader'"
        )

    async with pg_pool.acquire() as conn:
        await _wipe(conn)
    yield
    async with pg_pool.acquire() as conn:
        await _wipe(conn)


async def _seed(conn) -> UUID:
    """A reference whose window ENDS at the body's stamp, a passing gate row,
    and one desk head — the live shape, exactly."""
    ref_id = uuid4()
    await conn.execute(
        """
        INSERT INTO unit_references (
            id, target_id, window_start, window_end, built_at, builder,
            ref_json, span_verified_rate, thin_dimensions, sha256
        ) VALUES ($1,$2,$3,$4,$5,'pytest-lane',$6::jsonb,NULL,'{}'::text[],$7)
        """,
        ref_id, TARGET, BODY_AS_OF - timedelta(days=14), BODY_AS_OF,
        BODY_AS_OF, json.dumps(_REFERENCE), "d" * 64,
    )
    await conn.execute(
        """
        INSERT INTO grader_calibrations (
            id, rubric_sha, model_ids, pooled, pairwise, gate_pass, packet_sha,
            n_atoms, notes
        ) VALUES ($1,$2,$3::jsonb,0.8444,'[]'::jsonb,TRUE,$4,30,'pytest g1fix')
        ON CONFLICT DO NOTHING
        """,
        uuid4(), RUBRIC_SHA256, json.dumps(GRADE.model_ids()), "e" * 64,
    )
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, analyst_id, analyst_version, run_id, target_id, kind, title,
             body, confidence, data, produced_at, created_at, schema_uri)
        VALUES ($1,'internal_stability','0000000000000000',$2,$3,'finding',
                'internal stability read',$4,1.0,'{}'::jsonb,$5,$5,$6)
        """,
        uuid4(), uuid4(), TARGET, _DESK_BODY,
        BODY_AS_OF - timedelta(hours=2),
        "iglu:legba/finding/jsonschema/1-0-0",
    )
    return ref_id


# ---------------------------------------------------------------------------
# Actor scaffolding — the fakes are the SIDECAR's job, never the run's
# ---------------------------------------------------------------------------


class _FakeStateManager:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def try_get_state(self, name: str):
        if name in self._store:
            return True, self._store[name]
        return False, None

    async def set_state(self, name: str, value: Any) -> None:
        self._store[name] = value

    async def save_state(self) -> None:
        return None


class _FakeActorId:
    def __init__(self, actor_id: str) -> None:
        self.id = actor_id


class _Usage:
    def __init__(self) -> None:
        self.prompt_tokens = 100
        self.completion_tokens = 20
        self.reasoning_tokens = 0
        self.cost_estimate_usd = 0.0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _CannedF0:
    """The $0 core plane, stubbed at the handler boundary. No network, no spend
    — the LLM is not what this test is about."""

    def __init__(self) -> None:
        self.calls = 0
        self.component_id = "llm.primary.openai_compat"

    async def chat_complete(self, messages, **kwargs):
        self.calls += 1
        return _Response(json.dumps({
            "core_claim": "lists were filed",
            "verdict": "contains",
            "decisive_span": _VERBATIM,
            "reason": "the reference bears it out",
        }))


class _KindDeps:
    """What the deterministic dispatcher hands the sub-handler: the real pool
    and the family extras the deps builder would have wired."""

    def __init__(self, pool, extras: dict[str, Any]) -> None:
        self.pg_pool = pool
        self.extras = extras
        self.nats_publish = None


async def _one_row_read_slice(conn, *, descriptor, target_filter):
    """A non-empty slice so ``run`` reaches dispatch. The handler IGNORES its
    inputs by design (it reads the reference table and the heads itself), which
    is why one synthetic row is honest here rather than a shortcut."""
    return [{"id": str(uuid4()), "target_id": TARGET, "title": "slice row"}]


def _descriptor() -> AnalystDescriptor:
    """The SHIPPED descriptor, parsed exactly as the registry parses it.

    Its ``method.options`` block declares ``grader_targets: []`` and no
    ``as_of`` — so this file also pins the precedence the fix depends on: the
    forced run's parameters beat the standing descriptor config.
    """
    body = yaml.safe_load(DESCRIPTOR_PATH.read_text(encoding="utf-8"))
    body["identity"]["version"] = _VERSION
    body["identity"]["state"] = "active"
    return AnalystDescriptor.model_validate(body)


def _make_actor() -> AnalystActor:
    actor = object.__new__(AnalystActor)
    actor.id = _FakeActorId(_ACTOR_ID)
    actor._state_manager = _FakeStateManager()
    return actor


async def _seed_active(actor: AnalystActor) -> None:
    await actor._set_record({
        "actor_id": _ACTOR_ID,
        "actor_kind": "analyst",
        "descriptor_id": "correctness_grader",
        "descriptor_version": _VERSION,
        "lifecycle": ACTIVE,
        "error_count": 0,
    })


@pytest.fixture(autouse=True)
def _reset_deps_registry():
    dapr_actors.clear_deps_registry()
    yield
    dapr_actors.clear_deps_registry()


def _register(pool, llm) -> None:
    kind_deps = _KindDeps(pool, {CG.F0_DEPS_EXTRA_KEY: llm})
    deps = _AnalystDeps.model_construct(
        descriptor=_descriptor(),
        deps=kind_deps,
        run_method=deterministic.run_method,
        kind_deps=kind_deps,
        output_kind=dapr_actors.OutputKind.FINDING,
        budget=None,
        fallback_run_method=None,
        fallback_kind_deps=None,
        primary_llm_ref="",
        fallback_llm_ref="",
        receipt_chain=None,
        read_slice=_one_row_read_slice,
    )

    async def resolver(_actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)
    dapr_actors._ANALYST_DEPS.pop(_ACTOR_ID, None)


async def _force(pool, llm, body: dict[str, Any]) -> dict[str, Any]:
    _register(pool, llm)
    actor = _make_actor()
    await _seed_active(actor)
    return await actor.run(body)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


async def test_the_method_body_as_of_reaches_the_persisted_unit_row(
    pg_pool, clean_slate, monkeypatch
):
    """THE REGRESSION. The stamp in the PUT body is the stamp on the row.

    Not "the handler saw it" and not "the receipt printed it": the
    ``unit_correctness.as_of`` column, which is what every later read of this
    number joins on. The body is the script's own, so the channel under test is
    the one production uses.
    """
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(GRADE.CEILING_ENV, raising=False)
    async with pg_pool.acquire() as conn:
        ref_id = await _seed(conn)

    body = _grade_now_module().build_body([TARGET], BODY_AS_OF.isoformat())
    assert body["options"]["as_of"] == BODY_AS_OF.isoformat()

    llm = _CannedF0()
    outcome = await _force(pg_pool, llm, body)
    assert outcome["outcome"] == "success", outcome

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM unit_correctness WHERE target_id = $1", TARGET
        )
    assert len(rows) == 1, "the forced run graded nothing"
    row = rows[0]
    assert row["as_of"] == BODY_AS_OF, (
        "the method body's as_of did not reach the handler — the run stamped "
        f"{row['as_of']!r}. This is defect D1."
    )
    assert row["analyst_id"] == "internal_stability"
    assert row["reference_id"] == ref_id
    assert row["rubric_sha"] == RUBRIC_SHA256
    assert llm.calls >= 1


async def test_the_method_body_targets_bound_the_sweep(
    pg_pool, clean_slate, monkeypatch
):
    """``grader_targets`` travels too, and BOUNDS the population.

    The descriptor ships ``grader_targets: []`` (every target with a current
    reference). The body names one; the receipt must show that one, which is
    also the proof that the forced run's parameters beat the standing
    descriptor config rather than the other way round.
    """
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed(conn)

    body = _grade_now_module().build_body([TARGET], BODY_AS_OF.isoformat())
    outcome = await _force(pg_pool, _CannedF0(), body)
    assert outcome["outcome"] == "success", outcome

    async with pg_pool.acquire() as conn:
        receipt = await conn.fetchrow(
            """
            SELECT data->'data' AS payload FROM analyst_outputs
             WHERE analyst_id = 'correctness_grader'
             ORDER BY created_at DESC LIMIT 1
            """
        )
    assert receipt is not None, "the sweep wrote no receipt finding"
    payload = receipt["payload"]
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    assert payload["targets"] == [TARGET]
    assert payload["as_of"] == BODY_AS_OF.isoformat()
    assert payload["n_units_written"] == 1
    assert [t["status"] for t in payload["per_target"]] == ["ok"]


async def test_a_top_level_body_key_is_the_channel_that_does_not_exist(
    pg_pool, clean_slate, monkeypatch
):
    """The defect, pinned as behaviour so nobody "simplifies" the body back.

    Sent at the TOP level — the shape the script used on 2026-09-16 — ``as_of``
    is dropped silently and the run stamps now(), which no reference covers.
    The assertion is deliberately on the OLD shape: it documents that the
    actor's contract is nested-``options`` and that a top-level knob fails
    QUIETLY, which is why the script must never send one again.

    Note what this run DOES do post-D2: it still grades, because the reference
    stays current past its window_end and now() is inside the grace. The
    number it writes is simply stamped at the wrong instant — which is worse
    than grading nothing, and exactly why D1 and D2 had to be fixed together.
    """
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed(conn)

    legacy = {
        "trigger_kind": "method",
        "as_of": BODY_AS_OF.isoformat(),
        "grader_targets": [TARGET],
    }
    outcome = await _force(pg_pool, _CannedF0(), legacy)
    assert outcome["outcome"] == "success", outcome

    async with pg_pool.acquire() as conn:
        receipt = await conn.fetchrow(
            """
            SELECT data->'data' AS payload FROM analyst_outputs
             WHERE analyst_id = 'correctness_grader'
             ORDER BY created_at DESC LIMIT 1
            """
        )
    payload = receipt["payload"]
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    # now(), not the body's stamp — the drop, reproduced.
    assert payload["as_of"] != BODY_AS_OF.isoformat()
    stamped = datetime.fromisoformat(payload["as_of"])
    drift = abs((datetime.now(timezone.utc) - stamped).total_seconds())
    assert drift < 300, (
        "a dropped as_of falls back to now(); this run stamped "
        f"{payload['as_of']}, which is neither the body's stamp nor now"
    )


async def test_the_script_sends_the_nested_options_shape():
    """The body builder, checked against the actor's actual contract keys."""
    module = _grade_now_module()
    body = module.build_body(["country_watch_il"], "2026-09-16T19:30:00+00:00")
    assert body["trigger_kind"] == "method"
    assert body["options"] == {
        "grader_targets": ["country_watch_il"],
        "as_of": "2026-09-16T19:30:00+00:00",
    }
    assert "as_of" not in body and "grader_targets" not in body
    # No parameters at all: no empty options object to merge.
    assert module.build_body(None, None) == {"trigger_kind": "method"}
