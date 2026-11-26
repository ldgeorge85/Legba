# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collision-free migration names.

Numbered migrations (``NNNN_slug.sql``) are closed above ``LAST_NUMBERED`` and
keep their order; new ones are time-named (``YYYYMMDDTHHMM_slug.sql``, UTC) and
apply after them. The ledger records by name, so applying twice applies
nothing the second time. The last test is the point of the scheme: two lanes
add migrations independently, merge, and nothing is renumbered.
"""
from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from legba.data import migrate
from legba.data.migrations import (
    LAST_NUMBERED,
    MIGRATIONS_DIR,
    MigrationNameError,
    migration_sort_key,
    new_migration_name,
    ordered_migration_files,
)


def test_shipped_names_all_parse_and_keep_their_historic_order():
    ordered = [p.name for p in ordered_migration_files()]
    shipped = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert ordered == shipped  # numbered files: byte-for-byte the old sorted() order
    assert migrate.primary_migration_files() == ordered_migration_files()


def test_numbered_names_close_at_the_highest_numbered_file_present():
    """The rule as it stands: numbered names are closed above the
    highest numbered migration present when merged, after branches that had
    already taken 0263-0268 (0266 one byte path and 0268 the desk-rows shadow
    keep their reserved numbers). No new file may be numbered, so the highest
    numbered file present stays exactly LAST_NUMBERED."""
    numbered = [int(p.name[:4]) for p in MIGRATIONS_DIR.glob("*.sql") if p.name[:4].isdigit() and p.name[4] == "_"]
    assert max(numbered) == LAST_NUMBERED == 268
    names = {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
    assert "0266_one_byte_path.sql" in names and "0268_desk_rows_shadow.sql" in names
    assert sum(n.startswith("0266_") for n in names) == 1 and sum(n.startswith("0268_") for n in names) == 1


def test_time_named_sort_after_every_numbered_one_then_by_time_then_slug():
    names = [
        "20261115T0900_b_second.sql", "0262_last_numbered.sql",
        "20261115T0900_a_first.sql", "20261114T2359_earlier.sql", "0001_baseline.sql",
    ]
    assert sorted(names, key=migration_sort_key) == [
        "0001_baseline.sql", "0262_last_numbered.sql", "20261114T2359_earlier.sql",
        "20261115T0900_a_first.sql", "20261115T0900_b_second.sql",
    ]
    # the explicit key and plain name order agree, so `ORDER BY name` readers
    # of the ledger (the release manifest's "latest migration") stay right
    assert sorted(names, key=migration_sort_key) == sorted(names)


@pytest.mark.parametrize("bad", [
    f"{LAST_NUMBERED + 1:04d}_too_late.sql",   # numbered above the closed range
    "20261340T0900_bad_month.sql",             # not a real time
    "20261115-0900_dash.sql",
    "0300-Capital.sql",
    "20261115T0900_Has_Caps.sql",
])
def test_a_name_the_runner_cannot_order_is_refused(bad):
    with pytest.raises(MigrationNameError):
        migration_sort_key(bad)


def test_the_runner_refuses_a_bad_name_instead_of_skipping_it(tmp_path):
    (tmp_path / "0001_ok.sql").write_text("")
    (tmp_path / "notes.sql").write_text("")
    (tmp_path / "0001_dup.sql").write_text("")
    with pytest.raises(MigrationNameError) as err:
        ordered_migration_files(tmp_path)
    assert "notes.sql" in str(err.value) and "share number 0001" in str(err.value)


def test_new_migration_name_is_utc_and_valid():
    when = datetime(2026, 11, 14, 12, 32, tzinfo=timezone.utc)
    assert new_migration_name("claim_host_class", when) == "20261114T1232_claim_host_class.sql"
    with pytest.raises(MigrationNameError):
        new_migration_name("Bad Slug", when)


class _FakeConn:
    """Just enough of asyncpg.Connection for the runner: a name-keyed ledger."""

    def __init__(self) -> None:
        self.ledger: dict[str, str] = {}
        self.bodies: list[str] = []

    async def execute(self, sql: str, *args):
        if sql.lstrip().startswith("INSERT INTO legba_data_migrations"):
            assert args[0] not in self.ledger, f"applied twice: {args[0]}"
            self.ledger[args[0]] = args[1]
        elif not sql.lstrip().startswith(("CREATE TABLE IF NOT EXISTS", "SET ", "LOAD ")):
            self.bodies.append(sql)

    async def fetchrow(self, sql: str, *args):
        return {"?column?": 1} if args and args[0] in self.ledger else None

    @asynccontextmanager
    async def transaction(self):
        yield


def _lane(base: Path, root: Path, adds: dict[str, str]) -> Path:
    shutil.copytree(base, root)
    for name, body in adds.items():
        (root / name).write_text(body)
    return root


@pytest.mark.asyncio
async def test_two_lanes_add_migrations_independently_and_merge_without_renumbering(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    for name in ("0001_baseline.sql", "0257_indexes.sql", "0258_claim_host_class.sql"):
        (main / name).write_text(f"-- {name}\nSELECT 1;\n")

    # Each branch names its migration from its own clock — no shared counter read.
    lane_a = _lane(main, tmp_path / "lane_a", {
        new_migration_name("widget_table", datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)):
            "CREATE TABLE IF NOT EXISTS widget (id int);\n"})
    lane_b = _lane(main, tmp_path / "lane_b", {
        new_migration_name("gadget_index", datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)):
            "CREATE INDEX IF NOT EXISTS gadget_ix ON gadget (id);\n"})

    # The merge is a plain union of files: no two names can be equal (the slug
    # differs even inside one minute), so git merges them with no conflict.
    merged = _lane(main, tmp_path / "merged", {})
    for lane in (lane_a, lane_b):
        for path in lane.glob("2*.sql"):
            assert not (merged / path.name).exists()
            shutil.copy(path, merged / path.name)

    order = [p.name for p in ordered_migration_files(merged)]
    assert order == [
        "0001_baseline.sql", "0257_indexes.sql", "0258_claim_host_class.sql",
        "20261115T0900_gadget_index.sql", "20261115T0900_widget_table.sql",
    ]
    # neither lane's own ordering of the shared prefix moved
    for lane in (lane_a, lane_b):
        mine = [p.name for p in ordered_migration_files(lane)]
        assert [n for n in order if n in mine] == mine

    # A host that already ran branch A's file applies only branch B's on the merge,
    # and a second run applies nothing: the ledger is keyed by name.
    conn = _FakeConn()
    first = await migrate.apply_files(conn, ordered_migration_files(lane_a))
    assert first == [p.name for p in ordered_migration_files(lane_a)]
    second = await migrate.apply_files(conn, ordered_migration_files(merged))
    assert second == ["20261115T0900_gadget_index.sql"]
    assert await migrate.apply_files(conn, ordered_migration_files(merged)) == []
    assert list(conn.ledger) == [
        "0001_baseline.sql", "0257_indexes.sql", "0258_claim_host_class.sql",
        "20261115T0900_widget_table.sql", "20261115T0900_gadget_index.sql",
    ]
