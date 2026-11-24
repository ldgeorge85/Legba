# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X1-A2 — the backup set's pure logic (scripts/backup_lib.py).

Pins what the backup and the restore drill both stand on, with no stack:
retention (7 daily / 4 weekly), the destination grammar, the archive's
incremental delta and pack lookup, the drill's verifications (exact counts,
[before, after] brackets, the receipt re-hash), the escrow inventory (names,
never values), and the status-file contract the registry route reads.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import backup_lib as bl  # noqa: E402

from legba.data.registry.backup_status_api import read_backup_status  # noqa: E402

UTC = dt.timezone.utc


def _sid(y, m, d, hh=3, mm=30):
    return bl.set_id_for(dt.datetime(y, m, d, hh, mm, tzinfo=UTC))


# --------------------------------------------------------------------------
# set ids + retention
# --------------------------------------------------------------------------


def test_set_id_round_trips_and_rejects_junk():
    sid = _sid(2026, 10, 8, 21, 5)
    assert sid == "20261008T210500Z"
    assert bl.parse_set_id(sid) == dt.datetime(2026, 10, 8, 21, 5, tzinfo=UTC)
    assert bl.parse_set_id("20260312_105206") is None  # the old generation dirs
    assert bl.parse_set_id("") is None


def test_retention_keeps_seven_daily_and_four_weekly():
    # one set a night for 60 nights, plus a second set on the newest night
    start = dt.datetime(2026, 8, 10, 3, 30, tzinfo=UTC)
    sets = [bl.set_id_for(start + dt.timedelta(days=i)) for i in range(60)]
    sets.append(bl.set_id_for(start + dt.timedelta(days=59, hours=5)))
    keep, delete = bl.plan_retention(sets, daily=7, weekly=4)
    assert set(keep) | set(delete) == set(sets)
    assert not set(keep) & set(delete)
    newest = max(sets)
    assert newest in keep
    # the earlier set of the newest night is superseded (one per day)
    assert bl.set_id_for(start + dt.timedelta(days=59)) in delete
    days = {bl.parse_set_id(s).date() for s in keep}
    weeks = {tuple(bl.parse_set_id(s).isocalendar()[:2]) for s in keep}
    last7 = {(start + dt.timedelta(days=59 - i)).date() for i in range(7)}
    assert last7 <= days
    assert len(weeks) >= 4
    # 7 daily + at most 4 weekly (the weekly picks can coincide with dailies)
    assert 7 <= len(keep) <= 11
    oldest_kept = min(bl.parse_set_id(s) for s in keep)
    assert oldest_kept >= start + dt.timedelta(days=59 - 4 * 7)


def test_retention_never_touches_foreign_names_and_keeps_a_lone_set():
    keep, delete = bl.plan_retention(["20260312_105206", "lost+found", _sid(2026, 1, 1)],
                                     daily=0, weekly=0)
    assert keep == [_sid(2026, 1, 1)]
    assert delete == []


# --------------------------------------------------------------------------
# destination grammar
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dest, want",
    [
        ("/var/backups/legba", {"kind": "local", "path": "/var/backups/legba"}),
        ("file:///srv/b/", {"kind": "local", "path": "/srv/b"}),
        ("ssh://ops@vault.example:2222/srv/legba",
         {"kind": "ssh", "host": "ops@vault.example", "port": 2222, "path": "/srv/legba"}),
        ("ops@vault.example:/srv/legba",
         {"kind": "ssh", "host": "ops@vault.example", "port": None, "path": "/srv/legba"}),
        ("rclone:b2:legba-backups/prod", {"kind": "rclone", "target": "b2:legba-backups/prod"}),
        ("s3://legba-backups/prod", {"kind": "rclone", "target": "s3:legba-backups/prod"}),
    ],
)
def test_parse_dest(dest, want, monkeypatch):
    monkeypatch.delenv("LEGBA_BACKUP_RCLONE_REMOTE", raising=False)
    assert bl.parse_dest(dest) == want


@pytest.mark.parametrize("dest", ["", "relative/path", "rclone:nocolon", "ftp://x/y"])
def test_parse_dest_refuses_to_guess(dest):
    with pytest.raises(ValueError):
        bl.parse_dest(dest)


# --------------------------------------------------------------------------
# archive
# --------------------------------------------------------------------------

H1 = hashlib.sha256(b"one").hexdigest()
H2 = hashlib.sha256(b"two").hexdigest()


def test_archive_relpath_matches_the_cas_layout():
    assert bl.archive_relpath(f"cas:sha256/{H1}") == f"{H1[:2]}/{H1}"
    assert bl.archive_relpath(f"cas:sha256/{H1.upper()}") == f"{H1[:2]}/{H1}"
    assert bl.archive_relpath("cas:sha256/nothex") is None
    assert bl.archive_relpath("https://example.org/x") is None
    assert bl.archive_relpath(None) is None


def test_archive_delta_ships_only_new_names():
    assert bl.archive_delta(["a/1", "b/2", ""], ["b/2", "c/3", "a/1", "c/3"]) == ["c/3"]
    assert bl.archive_delta([], ["z", "y"]) == ["y", "z"]
    assert bl.archive_delta(["x"], []) == []


def test_pack_lookup_newest_pack_wins_and_missing_is_named():
    lists = {
        "pack-20261001T033000Z": ["aa/1", "bb/2"],
        "pack-20261008T033000Z": ["bb/2", "cc/3"],  # a re-based chain re-shipped bb/2
    }
    out = bl.pack_lookup(lists, ["aa/1", "bb/2", "cc/3", "dd/4"])
    assert out["pack-20261001T033000Z"] == ["aa/1"]
    assert sorted(out["pack-20261008T033000Z"]) == ["bb/2", "cc/3"]
    assert out[""] == ["dd/4"]
    # two receipts citing the same bytes → the path is asked of tar ONCE
    # (GNU tar exits 2 on a -T name it already extracted; the 10-08 drill)
    dup = bl.pack_lookup(lists, ["aa/1", "aa/1", "cc/3", ""])
    assert dup == {"pack-20261001T033000Z": ["aa/1"], "pack-20261008T033000Z": ["cc/3"]}


def test_verify_hash_sample(tmp_path):
    (tmp_path / H1[:2]).mkdir()
    (tmp_path / H1[:2] / H1).write_bytes(b"one")
    (tmp_path / H2[:2]).mkdir()
    (tmp_path / H2[:2] / H2).write_bytes(b"tampered")
    rows = [(f"{H1[:2]}/{H1}", H1), (f"{H2[:2]}/{H2}", H2), ("ff/" + "f" * 64, "f" * 64)]
    res = bl.verify_hash_sample(rows, tmp_path)
    assert res["checked"] == 3 and res["ok"] == 1
    assert any(p.startswith("mismatch") for p in res["problems"])
    assert any(p.startswith("missing") for p in res["problems"])


# --------------------------------------------------------------------------
# manifest + drill verification
# --------------------------------------------------------------------------


def test_exact_counts_must_match():
    want = {"signals": 10, "evidence_archive": 4}
    assert bl.verify_exact_counts(want, {"signals": 10, "evidence_archive": 4, "x": 1}) == []
    probs = bl.verify_exact_counts(want, {"signals": 9})
    assert any("signals: restored 9 != manifest 10" in p for p in probs)
    assert any("evidence_archive: absent" in p for p in probs)


def test_range_counts_bracket():
    want = {"legba_signals": {"before": 100, "after": 104}}
    assert bl.verify_range_counts(want, {"legba_signals": 102}) == []
    assert bl.verify_range_counts(want, {"legba_signals": 99})
    assert bl.verify_range_counts(want, {"legba_signals": None})


def test_cli_verify_exit_codes(tmp_path, capsys):
    frags = tmp_path / "frag"
    frags.mkdir()
    (frags / "postgres.json").write_text(json.dumps({"table_counts": {"signals": 5}}))
    (frags / "qdrant.json").write_text(json.dumps(
        {"collections": {"c": {"before": 1, "after": 2, "snapshot": "s"}}}))
    (frags / "opensearch.json").write_text(json.dumps(
        {"indices": {"i": {"before": 3, "after": 3, "exported": 3}}}))
    sid = _sid(2026, 10, 8)
    assert bl.main(["manifest", sid, str(frags)]) == 0
    man = json.loads(capsys.readouterr().out)
    assert man["format"] == bl.FORMAT_VERSION and man["set_id"] == sid
    assert set(man["components"]) == {"postgres", "qdrant", "opensearch"}
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps(man))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"postgres": {"signals": 5}, "qdrant": {"c": 2},
                                "opensearch": {"i": 3}}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"postgres": {"signals": 4}, "qdrant": {"c": 2},
                               "opensearch": {"i": 2}}))
    assert bl.main(["verify", str(mpath), str(good)]) == 0
    capsys.readouterr()
    assert bl.main(["verify", str(mpath), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "FAIL signals" in out and "FAIL i:" in out


# --------------------------------------------------------------------------
# escrow — names only
# --------------------------------------------------------------------------

_MASTER = "ab" * 32
_SIGNER = "cd" * 32


def test_escrow_inventory_never_carries_a_value():
    env = (
        "# comment\n"
        f"LEGBA_DATA_MASTER_KEY={_MASTER}\n"
        f"export LEGBA_REGISTRY_SIGNING_KEY='{_SIGNER}'\n"
        "LEGBA_REGISTRY_API_TOKEN=\n"
        "LEGBA_BASIC_AUTH_HASH='$2a$14$abc'\n"
    )
    inv = bl.escrow_inventory(env)
    assert inv["complete"] is True and inv["missing_required"] == []
    assert inv["secrets_present"]["LEGBA_DATA_MASTER_KEY"] is True
    assert inv["secrets_present"]["LEGBA_REGISTRY_API_TOKEN"] is False
    blob = json.dumps(inv)
    assert _MASTER not in blob and _SIGNER not in blob and "$2a$14$abc" not in blob


def test_escrow_inventory_incomplete_without_the_master_key():
    inv = bl.escrow_inventory(f"LEGBA_REGISTRY_SIGNING_KEY={_SIGNER}\n")
    assert inv["complete"] is False
    assert inv["missing_required"] == ["LEGBA_DATA_MASTER_KEY"]


def test_escrow_signer_keyfile_counts_only_when_included():
    env = f"LEGBA_DATA_MASTER_KEY={_MASTER}\nLEGBA_REGISTRY_SIGNING_KEY_FILE=/k/reg.key\n"
    assert bl.escrow_inventory(env, {"LEGBA_REGISTRY_SIGNING_KEY_FILE": True})["complete"]
    assert not bl.escrow_inventory(env, {"LEGBA_REGISTRY_SIGNING_KEY_FILE": False})["complete"]


def test_env_get_reads_knobs_only(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text(f"LEGBA_DATA_MASTER_KEY={_MASTER}\nLEGBA_BACKUP_DEST=/srv/b\n")
    assert bl.main(["env-get", str(env), "LEGBA_BACKUP_DEST"]) == 0
    assert capsys.readouterr().out.strip() == "/srv/b"
    assert bl.main(["env-get", str(env), "LEGBA_DATA_MASTER_KEY"]) == 2
    captured = capsys.readouterr()
    assert _MASTER not in captured.out and _MASTER not in captured.err
    assert bl.main(["env-get", str(env), "LEGBA_BACKUP_PUBKEY"]) == 1


# --------------------------------------------------------------------------
# status files ↔ the registry route's reader (one contract, both ends)
# --------------------------------------------------------------------------


def test_status_written_by_the_scripts_reads_back_through_the_route_reader(tmp_path):
    assert read_backup_status(tmp_path).model_dump() == {
        "last_backup_at": None, "last_drill_at": None}
    bl.write_status(tmp_path, "backup", {"last_backup_at": "2026-10-08T21:51:12+00:00",
                                         "set_id": "20261008T215109Z"})
    out = read_backup_status(tmp_path)
    assert out.last_backup_at == "2026-10-08T21:51:12+00:00"
    assert out.last_drill_at is None
    bl.write_status(tmp_path, "drill", {"last_drill_at": "2026-10-08T23:00:00Z"})
    assert read_backup_status(tmp_path).last_drill_at == "2026-10-08T23:00:00+00:00"
    assert not list(tmp_path.glob(".status-*"))  # atomic: no temp left behind
    with pytest.raises(ValueError):
        bl.write_status(tmp_path, "restore", {})
