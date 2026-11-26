# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The case-bearing columns resolve to a registered value or
``unclassified``.

* ``legba.data.case_columns``: a desk (subject) id in a category column moves
  to ``data.subject_id`` and the column says ``unclassified``; a namespaced
  value that binds is written as its registered key; ``event_type`` outside
  its family and ``entity_type`` outside ``entity_class`` bind
  ``unclassified`` (never the catch-all ``entity``); the producer's value is
  kept as ``data.category_raw`` and the TOPIC KEY reads it back byte for byte.
* The ``event_type`` platform family: seeded from
  ``descriptors/vocabulary_event_type.yaml`` (walker-loaded), equal to the
  literal, and migration 0230's CHECK admits ``unclassified``.
* The real producers (``write_situation``, ``situation_clustering``'s upsert,
  ``write_event``, ``entity_resolution``) write registered values.
* ``scripts/retrofit_case_columns.py``: the dry-run census on seeded rows;
  ``--apply`` resumes from its cursor and a second pass writes nothing.

Every row and descriptor a test seeds is deleted in its teardown.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import random
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data import case_columns as cc
from legba.data import tags as tags_mod
from legba.data.postgres import PostgresStore
from legba.data.schemas.vocabulary import PLATFORM_FAMILIES
from legba.data.tags import VocabularyIndex, install_vocabulary_provider

REPO_ROOT = Path(__file__).resolve().parents[2]


def _rand(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _idx(*, event_types=("incident", "development", "shift", "threshold"),
         entity_classes=("person", "organization", "entity", "location"),
         profile: str = "acme") -> VocabularyIndex:
    entries = [{"family": "event_type", "value": v} for v in event_types]
    entries += [{"family": "entity_class", "value": v} for v in entity_classes]
    entries += [
        {"family": f"{profile}.domain", "value": "energy", "aliases": ["power"]},
        {"family": "salience_class", "value": "kinetic_strike"},
    ]
    return VocabularyIndex.from_entries(entries)


@pytest.fixture(autouse=True)
def _reset_binding_state():
    tags_mod.reset_unknown_log()
    install_vocabulary_provider(None)
    yield
    install_vocabulary_provider(None)
    tags_mod.reset_unknown_log()


# ---------------------------------------------------------------------------
# The binding (pure)
# ---------------------------------------------------------------------------


def test_a_subject_id_in_a_category_moves_to_the_subject_field():
    b = cc.bind_category("country_g20_us", families=_idx(), subjects={"country_g20_us"})
    assert (b.category, b.status, b.subject_id) == ("unclassified", "subject", "country_g20_us")
    assert b.data_fields() == {"subject_id": "country_g20_us", "category_raw": "country_g20_us"}
    # a registered hint on the row's data becomes the class
    b = cc.bind_category("country_g20_us", families=_idx(), subjects={"country_g20_us"},
                         hint="kinetic_strike")
    assert (b.category, b.subject_id) == ("salience_class:kinetic_strike", "country_g20_us")


def test_a_registered_category_is_written_as_its_key_and_anything_else_is_unclassified():
    idx = _idx()
    assert cc.bind_category("acme.domain:power", families=idx).category == "acme.domain:energy"
    assert cc.bind_category("kinetic_strike", families=idx).category == "salience_class:kinetic_strike"
    for raw in ("news", "severity:high", "acme.domain:fusion", "region:americas", "", "src:x"):
        b = cc.bind_category(raw, families=idx)
        assert (b.category, b.status, b.subject_id) == ("unclassified", "unclassified", None), raw
        assert b.data_fields() == {"category_raw": raw}  # never dropped
    # an unloaded family is unclassified, never guessed from a literal
    assert cc.bind_category("kinetic_strike").category == "unclassified"
    # the subject lookup is only ever asked about bare values
    assert not cc.needs_subject_lookup("acme.domain:power")
    assert not cc.needs_subject_lookup("unclassified") and not cc.needs_subject_lookup("")
    assert cc.needs_subject_lookup("country_g20_us")


def test_the_binding_is_idempotent_on_its_own_output():
    idx = _idx()
    for raw in ("country_g20_us", "acme.domain:power", "news", "", "kinetic_strike"):
        b1 = cc.bind_category(raw, families=idx, subjects={"country_g20_us"})
        # the retrofit re-binds the PRODUCER's value (category_raw when present)
        again = b1.data_fields().get("category_raw", b1.category)
        b2 = cc.bind_category(again, families=idx, subjects={"country_g20_us"})
        assert b2 == b1


def test_stamp_category_data_sets_subject_raw_and_classifications_without_mutating():
    idx = _idx()
    data = {"x": 1, "subject_id": "stale", "category_raw": "stale"}
    b = cc.bind_category("acme.domain:power", families=idx)
    out = cc.stamp_category_data("events", b, data, families=idx)
    assert data == {"x": 1, "subject_id": "stale", "category_raw": "stale"}
    assert out == {
        "x": 1, "category_raw": "acme.domain:power",
        "classifications": [{"family": "acme.domain", "value": "energy", "status": "bound"}],
    }
    b = cc.bind_category("country_g20_us", families=idx, subjects={"country_g20_us"})
    assert cc.stamp_category_data("situations", b, {}, families=idx)["classifications"] == [
        {"family": "src", "value": "country_g20_us", "status": "legacy_src"},
    ]


def test_event_type_outside_the_family_is_unclassified_and_the_family_never_widens_the_check():
    assert cc.bind_event_type("development", families=_idx()) == "development"
    assert cc.bind_event_type("bogus", families=_idx()) == "unclassified"
    narrowed = _idx(event_types=("incident",))
    assert cc.bind_event_type("development", families=narrowed) == "unclassified"
    widened = _idx(event_types=("incident", "rumour"))
    assert cc.bind_event_type("rumour", families=widened) == "unclassified"
    assert cc.event_type_values(widened) == {"incident"}
    assert cc.event_type_values(None) == set(cc.EVENT_TYPES)  # unloaded → the literal


def test_entity_type_outside_entity_class_is_unclassified_never_the_catch_all():
    idx = _idx(entity_classes=("person", "organization"))
    assert cc.bind_entity_type("person", families=idx) == "person"
    assert cc.bind_entity_type("gpe", families=idx) == "unclassified"
    assert cc.bind_entity_type("entity", families=idx) == "unclassified"
    # unloaded → the seed literal (vocabulary.ENTITY_CLASSES), which holds `entity`
    assert cc.bind_entity_type("entity") == "entity"
    assert cc.bind_entity_type("armed_group") == "unclassified"


def test_the_topic_key_and_the_filter_read_the_producers_value():
    assert cc.topic_key_sql("s") == "COALESCE(s.data->>'category_raw', s.category)"
    assert cc.category_filter_sql("e", 4) == (
        "(e.category = $4 OR e.data->>'category_raw' = $4)")
    from legba.data.analysts.deterministic_handlers import _event_candidates as ec
    from legba.data.analysts.deterministic_handlers import _open_event_read as oer
    assert cc.topic_key_sql("s") + " AS category" in ec._SITUATION_CANDIDATES_SQL
    assert cc.topic_key_sql("e") + " AS category" in oer.OPEN_EVENTS_SQL
    assert cc.topic_key_sql("e") + " AS category" in oer.MINTED_EVENT_SQL
    assert ec._EVENT_TYPES == set(cc.EVENT_TYPES)


def test_case_columns_is_a_leaf():
    tree = ast.parse((REPO_ROOT / "src/legba/data/case_columns.py").read_text())
    allowed = {"__future__", "dataclasses", "typing", "tags", "taxon_families", "vocabulary"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0] if node.level == 0 else (node.module or "")
            names = {a.name for a in node.names} if not node.module else {mod}
            assert names <= allowed, names
        elif isinstance(node, ast.Import):
            raise AssertionError(f"plain import in case_columns.py: {ast.dump(node)}")


# ---------------------------------------------------------------------------
# The event_type family + migration 0230
# ---------------------------------------------------------------------------


def test_event_type_is_a_platform_family_seeded_from_its_file():
    assert "event_type" in PLATFORM_FAMILIES and "event_type" in tags_mod.PLATFORM_FAMILIES
    body = yaml.safe_load((REPO_ROOT / "descriptors/vocabulary_event_type.yaml").read_text())
    assert body["family"] == "event_type"
    assert [e["value"] for e in body["entries"]] == list(cc.EVENT_TYPES)
    spec = importlib.util.spec_from_file_location(
        "bringup_walker_tax4", REPO_ROOT / "scripts" / "bringup_register_descriptors.py")
    walker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = walker
    spec.loader.exec_module(walker)  # type: ignore[union-attr]
    seeds = [s for s in walker.load_vocabulary_seeds() if s.family == "event_type"]
    assert [s.value for s in seeds] == list(cc.EVENT_TYPES)
    assert all(s.notes for s in seeds)


def test_migration_0230_is_a_strict_superset_check():
    sql = (REPO_ROOT / "src/legba/data/migrations/0230_events_event_type_unclassified.sql").read_text()
    assert "DROP CONSTRAINT IF EXISTS events_event_type_check" in sql
    for v in (*cc.EVENT_TYPES, "unclassified"):
        assert f"'{v}'" in sql
    code = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "BEGIN" not in code and "COMMIT" not in code  # routed through the runner


# ---------------------------------------------------------------------------
# Real driver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg(migrated_pg):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def world(pg):
    """A registered SUBJECT (a target descriptor), a private analyst id, and a
    teardown that deletes every row the test wrote."""
    tag = _rand()
    subject = f"country_tax4_{tag}"
    analyst = f"analyst.tax4_{tag}"
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO target_descriptors (descriptor_id, version, schema_uri, is_head,"
            " abstraction_level, state, owner, name, body, created_at)"
            " VALUES ($1, '1.0.0', 'legba/target/2.0.0', TRUE, 'L1', 'draft',"
            " 'tax4@test', 'TAX-4 subject', '{}'::jsonb, now())",
            subject,
        )
    signal_ids: list = []
    yield {"subject": subject, "analyst": analyst, "tag": tag, "signals": signal_ids}
    async with pg.acquire() as conn:
        await conn.execute(
            "ALTER TABLE event_lifecycle_events DISABLE TRIGGER "
            "trg_event_lifecycle_events_forbid_delete")
        try:
            await conn.execute("DELETE FROM events WHERE analyst_id = $1", analyst)
        finally:
            await conn.execute(
                "ALTER TABLE event_lifecycle_events ENABLE TRIGGER "
                "trg_event_lifecycle_events_forbid_delete")
        await conn.execute("DELETE FROM situations WHERE analyst_id = $1", analyst)
        # by NAME only: the resolver sweeps every unresolved signal in the
        # shared DB, so its analyst id is not ours alone
        await conn.execute(
            "DELETE FROM entity_profiles WHERE canonical_name LIKE $1", f"%{tag.title()}%")
        await conn.execute("DELETE FROM signals WHERE id = ANY($1::uuid[])", signal_ids)
        await conn.execute("DELETE FROM target_descriptors WHERE descriptor_id = $1", subject)


async def _signal(conn, world, *, payload=None, fetched_at=None):
    sid = await conn.fetchval(
        "INSERT INTO signals (source_id, modality, payload, content_hash, fetched_at, tags)"
        " VALUES ('tax4_src', 'text', $1::jsonb, $2, $3, '{}'::text[]) RETURNING id",
        json.dumps(payload or {}), f"h-{uuid4().hex}",
        fetched_at or datetime.now(tz=timezone.utc),
    )
    world["signals"].append(sid)
    return sid


def _data(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def _provider(idx: VocabularyIndex):
    async def _p():
        return idx
    install_vocabulary_provider(_p)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_new_row_through_the_real_producers_carries_a_registered_value(
    pg, world, monkeypatch,
):
    from legba.data.analysts.deterministic_handlers.entity_resolution import _resolve_batch
    from legba.data.analysts.deterministic_handlers.situation_clustering import (
        _upsert_situation,
    )
    from legba.data.provenance import AnalystContext, write_event, write_situation
    from legba.data.provenance.models import EventPayload, EventSignalLinkPayload

    _provider(_idx(event_types=("incident",), entity_classes=("organization",),
                   profile="acme"))
    subject, analyst = world["subject"], world["analyst"]
    ctx = AnalystContext(analyst_id=analyst, analyst_version="t", run_id=uuid4(),
                         target_id=None, target_version=None)
    async with pg.acquire() as conn:
        sig = await _signal(conn, world)
        # situations — the analyst write path: a desk id is a SUBJECT
        out, dlq = await write_situation(
            conn, analyst_ctx=ctx,
            payload={"name": "Sit", "category": subject,
                     "situation_signature": f"sig-{uuid4().hex}"},
            derived_from=[sig],
        )
        assert dlq is None, dlq
        row = await conn.fetchrow("SELECT category, data FROM situations WHERE id = $1", out.id)
        assert row["category"] == "unclassified"
        d = _data(row["data"])
        assert (d["subject_id"], d["category_raw"]) == (subject, subject)
        # situations — the clusterer's upsert: a namespaced value binds to its key
        skey = f"sig-{uuid4().hex}"
        await _upsert_situation(
            conn,
            fields={
                "situation_signature": skey, "member_finding_ids": [str(uuid4())],
                "name": "Clustered", "status": "active", "category": "acme.domain:power",
                "last_event_at": datetime.now(tz=timezone.utc), "event_count": 1,
                "intensity_score": 0.5, "valid_from": datetime.now(tz=timezone.utc),
                "valid_until": None,
            },
            analyst_id=analyst, analyst_version="t", target_id=None, run_id=None,
        )
        row = await conn.fetchrow(
            "SELECT category, data FROM situations WHERE situation_signature = $1", skey)
        assert row["category"] == "acme.domain:energy"
        assert _data(row["data"])["classifications"] == [
            {"family": "acme.domain", "value": "energy", "status": "bound"}]
        # events — subject moved, event_type outside the (narrowed) family →
        # unclassified, which migration 0230's CHECK admits
        monkeypatch.setenv("LEGBA_EVENTS", "1")
        evt_sig = f"evt:tax4 {uuid4().hex[:8]}#evt:xx"
        out, dlq = await write_event(
            conn, analyst_ctx=ctx,
            payload=EventPayload(
                event_signature=evt_sig, title="An occurrence", category=subject,
                event_type="development",
                signals=[EventSignalLinkPayload(
                    signal_id=sig, linked_at=datetime.now(tz=timezone.utc),
                    source_class="reporting")],
            ),
            derived_from=[sig],
        )
        assert dlq is None, dlq
        row = await conn.fetchrow(
            "SELECT category, event_type, data FROM events WHERE event_signature = $1", evt_sig)
        assert (row["category"], row["event_type"]) == ("unclassified", "unclassified")
        assert _data(row["data"])["subject_id"] == subject
        # the topic key the matcher reads is the producer's value
        assert await conn.fetchval(
            f"SELECT {cc.topic_key_sql('e')} FROM events e WHERE e.event_signature = $1",
            evt_sig) == subject
        # entity profiles — a class outside the loaded entity_class family
        name = "Halvard " + world["tag"].title()
        await _signal(
            conn, world,
            payload={"title": f"{name} spoke", "entities": [{"text": name, "class": "person"}]},
            fetched_at=datetime(1970, 1, 2, tzinfo=timezone.utc),
        )
    await _resolve_batch(pg, batch_limit=50, analyst_id=analyst)
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT entity_type, entity_class FROM entity_profiles"
            " WHERE lower(canonical_name) = lower($1)", name)
    assert row is not None, "the resolver did not write the profile"
    assert (row["entity_type"], row["entity_class"]) == ("unclassified", "person")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "retrofit_case_columns", REPO_ROOT / "scripts" / "retrofit_case_columns.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


async def _legacy_rows(conn, world) -> dict[str, list]:
    """Rows in the PRE-TAX-4 shape, written straight to the tables."""
    analyst, subject = world["analyst"], world["subject"]
    sits = []
    for cat in (subject, subject, "severity:high", "acme.domain:power"):
        sits.append(await conn.fetchval(
            "INSERT INTO situations (id, data, name, status, category, analyst_id,"
            " produced_at, schema_uri, derived_from)"
            " VALUES ($1, '{}'::jsonb, 'legacy', 'active', $2, $3, now(),"
            " 'iglu:legba/situation/jsonschema/2-0-0', '{}'::uuid[]) RETURNING id",
            uuid4(), cat, analyst))
    evts = []
    for cat, et in ((subject, "incident"), ("news", "development"), ("", "shift")):
        evts.append(await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title, category, event_type)"
            " VALUES ($1, $2, 'legacy', $3, $4) RETURNING id",
            f"evt:{uuid4().hex}", analyst, cat, et))
    ents = []
    for et in ("person", "entity", "gpe"):
        ents.append(await conn.fetchval(
            "INSERT INTO entity_profiles (canonical_name, entity_type, entity_class, data,"
            " analyst_id) VALUES ($1, $2, $2, '{}'::jsonb, $3) RETURNING id",
            f"Legacy {world['tag'].title()} {et}", et, analyst))
    return {"situations": sits, "events": evts, "entity_profiles": ents}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_census_on_seeded_rows(pg, world):
    rt = _load_script()
    idx = _idx(event_types=("incident", "development"))
    async with pg.acquire() as conn:
        await _legacy_rows(conn, world)
        census = await rt.take_census(conn, idx, batch_size=2, analyst_id=world["analyst"])
        unchanged = await conn.fetchval(
            "SELECT count(*) FROM situations WHERE analyst_id = $1 AND category = $2",
            world["analyst"], world["subject"])
    assert unchanged == 2  # the dry run wrote nothing
    rep = rt.summarize(census, idx)
    assert rep["rows_scanned"] == {"situations": 4, "events": 3, "entity_profiles": 3}
    assert rep["rows_to_write"] == {"situations": 4, "events": 3, "entity_profiles": 3}
    sc = rep["columns"]["situations.category"]
    assert sc["by_status"] == {
        "registered": {"values": 1, "rows": 1},
        "subject_moved": {"values": 1, "rows": 2},
        "unclassified": {"values": 1, "rows": 1},
    }
    assert sc["top_unmapped"] == [{"raw": "severity:high", "rows": 1}]
    ec = rep["columns"]["events.category"]
    assert ec["by_status"]["subject_moved"] == {"values": 1, "rows": 1}
    assert {r["raw"] for r in ec["top_unmapped"]} == {"news", ""}
    et = rep["columns"]["events.event_type"]
    assert et["by_status"]["unclassified"] == {"values": 1, "rows": 1}
    assert et["top_unmapped"] == [{"raw": "shift", "rows": 1}]
    en = rep["columns"]["entity_profiles.entity_type"]
    assert en["by_status"]["registered"] == {"values": 2, "rows": 2}
    assert en["top_unmapped"] == [{"raw": "gpe", "rows": 1}]
    text = rt.render(rep)
    assert "situations.category" in text and "top 1 unmapped" in text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_resumes_from_its_cursor_and_is_idempotent(pg, world, tmp_path):
    rt = _load_script()
    idx = _idx(event_types=("incident", "development"))
    analyst, subject = world["analyst"], world["subject"]
    cur = tmp_path / "cursor.json"
    async with pg.acquire() as conn:
        ids = await _legacy_rows(conn, world)
        topic_before = {
            r["id"]: r["k"] for r in await conn.fetch(
                f"SELECT s.id, {cc.topic_key_sql('s')} AS k FROM situations s"
                " WHERE s.analyst_id = $1", analyst)
        }
        st = await rt.apply(conn, idx, cursor_path=cur, batch_size=2, max_batches=1,
                            analyst_id=analyst)
        assert st["tables"]["situations"]["batches"] == 1 and not st["done"]
        assert json.loads(cur.read_text())["tables"]["situations"]["last_id"] == (
            st["tables"]["situations"]["last_id"])
        st = await rt.apply(conn, idx, cursor_path=cur, batch_size=2, analyst_id=analyst)
        assert st["done"]
        assert st["tables"]["situations"]["rows_scanned"] == 4
        assert st["tables"]["situations"]["rows_written"] == 4
        assert st["tables"]["events"]["rows_written"] == 3
        assert st["tables"]["entity_profiles"]["rows_written"] == 3
        sits = {r["id"]: r for r in await conn.fetch(
            "SELECT id, category, data FROM situations WHERE analyst_id = $1", analyst)}
        evts = {r["id"]: r for r in await conn.fetch(
            "SELECT id, category, event_type, data FROM events WHERE analyst_id = $1", analyst)}
        ents = {r["id"]: r for r in await conn.fetch(
            "SELECT id, entity_type, entity_class, data FROM entity_profiles"
            " WHERE analyst_id = $1", analyst)}
        topic_after = {
            r["id"]: r["k"] for r in await conn.fetch(
                f"SELECT s.id, {cc.topic_key_sql('s')} AS k FROM situations s"
                " WHERE s.analyst_id = $1", analyst)
        }
        # a second, fresh pass computes exactly what the rows hold: nothing to write
        census = await rt.take_census(conn, idx, analyst_id=analyst)
        st2 = await rt.apply(conn, idx, cursor_path=tmp_path / "c2.json", analyst_id=analyst)
    s0 = sits[ids["situations"][0]]
    assert s0["category"] == "unclassified"
    assert {k: _data(s0["data"])[k] for k in ("subject_id", "category_raw")} == {
        "subject_id": subject, "category_raw": subject}
    s2 = sits[ids["situations"][2]]
    assert (s2["category"], _data(s2["data"]).get("subject_id")) == ("unclassified", None)
    assert _data(s2["data"])["category_raw"] == "severity:high"
    s3 = sits[ids["situations"][3]]
    assert s3["category"] == "acme.domain:energy"
    assert _data(s3["data"])["classifications"] == [
        {"family": "acme.domain", "value": "energy", "status": "bound"}]
    assert topic_after == topic_before  # the signature/matcher key never moved
    e0, e1, e2 = (evts[i] for i in ids["events"])
    assert (e0["category"], _data(e0["data"])["subject_id"]) == ("unclassified", subject)
    assert (e1["category"], e1["event_type"]) == ("unclassified", "development")
    assert (e2["event_type"], _data(e2["data"])["category_raw"]) == ("unclassified", "")
    p, ent, gpe = (ents[i] for i in ids["entity_profiles"])
    assert (p["entity_type"], ent["entity_type"], gpe["entity_type"]) == (
        "person", "entity", "unclassified")
    assert gpe["entity_class"] == "gpe"  # entity_class is not this lane's column
    assert _data(p["data"])["classifications"] == [
        {"family": "entity_class", "value": "person", "status": "bound"}]
    assert sum(census.rows_to_write.values()) == 0
    assert all(t["rows_written"] == 0 for t in st2["tables"].values())
    # a mismatched cursor refuses rather than silently restarting
    with pytest.raises(SystemExit):
        async with pg.acquire() as conn:
            await rt.apply(conn, idx, cursor_path=cur, analyst_id="other")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0230_admits_unclassified_and_still_refuses_a_stranger(pg, world):
    async with pg.acquire() as conn:
        eid = await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title, event_type)"
            " VALUES ($1, $2, 't', 'unclassified') RETURNING id",
            f"evt:{uuid4().hex}", world["analyst"])
        assert eid is not None
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO events (event_signature, analyst_id, title, event_type)"
                " VALUES ($1, $2, 't', 'rumour')", f"evt:{uuid4().hex}", world["analyst"])
