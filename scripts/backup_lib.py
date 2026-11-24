#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backup set's pure logic, shared by scripts/backup.sh and
scripts/restore_drill.sh (stdlib only — it runs on the host python3).

What lives here is everything a test can pin without a running stack:

  * the set id and the 7-daily / 4-weekly retention plan;
  * the destination grammar (local path, rsync-over-ssh, rclone/S3);
  * the content-addressed archive's incremental delta and its object paths;
  * the manifest (assembled from per-component fragments) and the drill's
    verifications against it (exact table counts, [before, after] ranges for
    stores that keep taking writes during the snapshot, the archive sample's
    re-hash);
  * the key-escrow inventory — which named secrets the sealed artifact holds,
    by NAME only, never a value;
  * the status files the registry's ``/v3/system/backup-status`` route reads
    (``last_backup.json`` / ``last_drill.json``), written atomically;
  * the OpenSearch logical export / import (scroll out, bulk in), because the
    live node runs without ``path.repo`` and a snapshot repository would cost
    an OpenSearch restart.

CLI: ``python3 scripts/backup_lib.py <subcommand> ...`` (see ``main``).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.request
from typing import Iterable, Iterator

FORMAT_VERSION = "legba-backup/1"
SET_ID_FORMAT = "%Y%m%dT%H%M%SZ"
_SET_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# The named secrets the escrow must carry. The vault master key decrypts every
# stack_credentials row; the registry signing key signs the receipt/audit
# chains and the audit-chain checkpoints (audit_checkpointer_wiring reuses it).
ESCROW_REQUIRED = ("LEGBA_DATA_MASTER_KEY", "LEGBA_REGISTRY_SIGNING_KEY")
# Either the inline key or its keyfile sibling satisfies the signer.
ESCROW_ALTERNATES = {"LEGBA_REGISTRY_SIGNING_KEY": "LEGBA_REGISTRY_SIGNING_KEY_FILE"}
KNOB_PREFIX = "LEGBA_BACKUP_"
ESCROW_OPTIONAL = (
    "LEGBA_REGISTRY_SIGNING_KEY_FILE",
    "LEGBA_SIGNING_KEY_PATH",
    "LEGBA_REGISTRY_API_TOKEN",
    "LEGBA_REGISTRY_SERVICE_TOKEN",  # X2-B1: the fleet's principal
    "LEGBA_BASIC_AUTH_HASH",
)


# ---------------------------------------------------------------------------
# Set ids + retention
# ---------------------------------------------------------------------------


def set_id_for(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime(SET_ID_FORMAT)


def parse_set_id(set_id: str) -> _dt.datetime | None:
    if not _SET_ID_RE.match(set_id or ""):
        return None
    return _dt.datetime.strptime(set_id, SET_ID_FORMAT).replace(tzinfo=_dt.timezone.utc)


def plan_retention(
    set_ids: Iterable[str], *, daily: int = 7, weekly: int = 4,
) -> tuple[list[str], list[str]]:
    """Return ``(keep, delete)``.

    Keep the newest set of each of the ``daily`` most recent days that have a
    set, plus the newest set of each of the ``weekly`` most recent ISO weeks.
    A name that is not a set id is neither kept nor deleted (never touched).
    The newest set is always kept, whatever the knobs say.
    """
    parsed = sorted(
        ((sid, ts) for sid in set(set_ids) if (ts := parse_set_id(sid)) is not None),
        key=lambda p: p[1],
        reverse=True,
    )
    keep: set[str] = set()
    days: list[_dt.date] = []
    weeks: list[tuple[int, int]] = []
    for sid, ts in parsed:
        d = ts.date()
        if d not in days:
            days.append(d)
            if len(days) <= daily:
                keep.add(sid)
        w = tuple(ts.isocalendar()[:2])
        if w not in weeks:
            weeks.append(w)
            if len(weeks) <= weekly:
                keep.add(sid)
    if parsed:
        keep.add(parsed[0][0])
    ordered = [sid for sid, _ in parsed]
    return (
        [s for s in ordered if s in keep],
        [s for s in ordered if s not in keep],
    )


# ---------------------------------------------------------------------------
# Destination grammar
# ---------------------------------------------------------------------------


def parse_dest(dest: str) -> dict:
    """``LEGBA_BACKUP_DEST`` → ``{"kind": local|ssh|rclone, ...}``.

    * ``/abs/path`` or ``file:///abs/path``           → local
    * ``ssh://user@host[:port]/abs/path``, ``user@host:/abs/path`` → ssh (rsync)
    * ``rclone:<remote>:<bucket/path>``                → rclone
    * ``s3://bucket/path``                             → rclone on the remote
      named by ``LEGBA_BACKUP_RCLONE_REMOTE`` (default ``s3``)
    Anything else raises ``ValueError`` — a destination is never guessed.
    """
    d = (dest or "").strip()
    if not d:
        raise ValueError("empty destination")
    if d.startswith("file://"):
        d = d[len("file://"):]
    if d.startswith("/"):
        return {"kind": "local", "path": d.rstrip("/") or "/"}
    if d.startswith("ssh://"):
        m = re.match(r"^ssh://([^@/]+@)?([^:/]+)(?::(\d+))?(/.*)$", d)
        if not m:
            raise ValueError(f"unparseable ssh destination: {dest!r}")
        user = (m.group(1) or "").rstrip("@")
        host = m.group(2)
        return {
            "kind": "ssh",
            "host": f"{user}@{host}" if user else host,
            "port": int(m.group(3)) if m.group(3) else None,
            "path": m.group(4).rstrip("/") or "/",
        }
    if d.startswith("rclone:"):
        rest = d[len("rclone:"):]
        if ":" not in rest:
            raise ValueError(f"rclone destination needs <remote>:<path>: {dest!r}")
        return {"kind": "rclone", "target": rest.rstrip("/")}
    if d.startswith("s3://"):
        remote = os.environ.get("LEGBA_BACKUP_RCLONE_REMOTE", "s3")
        return {"kind": "rclone", "target": f"{remote}:{d[len('s3://'):].rstrip('/')}"}
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", d):
        raise ValueError(f"unsupported destination scheme: {dest!r}")
    m = re.match(r"^([^@/:]+@)?([^:/]+):(/.*)$", d)
    if m:
        user = (m.group(1) or "").rstrip("@")
        return {
            "kind": "ssh",
            "host": f"{user}@{m.group(2)}" if user else m.group(2),
            "port": None,
            "path": m.group(3).rstrip("/") or "/",
        }
    raise ValueError(f"unrecognised destination: {dest!r}")


# ---------------------------------------------------------------------------
# Archive (content-addressed: {sha[:2]}/{sha}) — incremental by name
# ---------------------------------------------------------------------------


def archive_relpath(object_ref: str | None) -> str | None:
    """``cas:sha256/<hex>`` → ``<hex[:2]>/<hex>`` (None when not a CAS ref)."""
    if not object_ref or not object_ref.startswith("cas:sha256/"):
        return None
    h = object_ref[len("cas:sha256/"):].lower()
    if not _SHA_RE.match(h):
        return None
    return f"{h[:2]}/{h}"


def archive_delta(indexed: Iterable[str], current: Iterable[str]) -> list[str]:
    """Files present now but never shipped. Content-addressed files are
    immutable, so a name already shipped never needs shipping again."""
    have = {p.strip() for p in indexed if p.strip()}
    return sorted({p.strip() for p in current if p.strip()} - have)


def pack_lookup(pack_lists: dict[str, Iterable[str]], wanted: Iterable[str]) -> dict[str, list[str]]:
    """``{pack: [members]}`` for the wanted paths; the NEWEST pack wins when a
    path was shipped twice (a re-based chain). Missing paths go under ``""``."""
    where: dict[str, str] = {}
    for pack in sorted(pack_lists):
        for p in pack_lists[pack]:
            p = p.strip()
            if p:
                where[p] = pack
    # Each path once: two receipts can share one sha256 (the same bytes cited
    # twice), and GNU tar exits 2 on a -T name it already extracted.
    out: dict[str, list[str]] = {}
    for w in dict.fromkeys(w.strip() for w in wanted if w.strip()):
        out.setdefault(where.get(w, ""), []).append(w)
    return out


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _sha256_zstd_decoded(path: pathlib.Path) -> str | None:
    """sha256 of the zstd-decoded content of ``path``; ``None`` if it does
    not decode. Uses the ``zstandard`` binding when importable, else the
    ``zstd`` CLI (the host restore drill runs on the system python)."""
    h = hashlib.sha256()
    try:
        import zstandard  # noqa: PLC0415
    except ImportError:
        zstandard = None
    try:
        if zstandard is not None:
            with open(path, "rb") as fh:
                reader = zstandard.ZstdDecompressor().stream_reader(fh)
                for chunk in iter(lambda: reader.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        import subprocess  # noqa: PLC0415
        proc = subprocess.Popen(["zstd", "-q", "-dc", str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest() if proc.wait() == 0 else None
    except Exception:
        return None


def archive_original_sha256(path: pathlib.Path, expected: str | None = None) -> str:
    """sha256 of the ORIGINAL bytes behind an archive object. Objects may rest
    zstd-compressed (N-K, ``legba.data.archive_codec``): a file that starts
    with the zstd magic is decoded first. The raw hash is the answer when the
    decoded one does not match ``expected`` (a legacy object that merely
    begins with the magic) — the same rule the runtime reader applies."""
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head == _ZSTD_MAGIC:
        decoded = _sha256_zstd_decoded(path)
        if decoded is not None and (expected is None or decoded == expected.lower()):
            return decoded
    return sha256_file(path)


def verify_hash_sample(rows: Iterable[tuple[str, str]], root: pathlib.Path) -> dict:
    """``rows`` = ``(relpath, expected_sha256)``; re-hash each file under
    ``root``. Every row must exist and match."""
    checked = ok = 0
    problems: list[str] = []
    for rel, expected in rows:
        checked += 1
        f = root / rel
        if not f.is_file():
            problems.append(f"missing {rel}")
            continue
        got = archive_original_sha256(f, expected)
        if got != expected.lower():
            problems.append(f"mismatch {rel}: {got}")
        else:
            ok += 1
    return {"checked": checked, "ok": ok, "problems": problems}


# ---------------------------------------------------------------------------
# Manifest + verification
# ---------------------------------------------------------------------------


def build_manifest(set_id: str, fragments: dict[str, dict], *, meta: dict | None = None) -> dict:
    """One manifest from per-component fragments (``{component: {...}}``)."""
    return {
        "format": FORMAT_VERSION,
        "set_id": set_id,
        "created_at": (parse_set_id(set_id) or _dt.datetime.now(_dt.timezone.utc)).isoformat(),
        "components": dict(sorted(fragments.items())),
        **(meta or {}),
    }


def verify_exact_counts(expected: dict[str, int], restored: dict[str, int | None]) -> list[str]:
    """Postgres counts were taken INSIDE the dump's exported snapshot, so a
    faithful restore reproduces them exactly."""
    problems = []
    for name, want in sorted(expected.items()):
        got = restored.get(name)
        if got is None:
            problems.append(f"{name}: absent after restore (manifest {want})")
        elif int(got) != int(want):
            problems.append(f"{name}: restored {got} != manifest {want}")
    return problems


def verify_range_counts(expected: dict[str, dict], restored: dict[str, int | None]) -> list[str]:
    """Qdrant snapshots are taken on a store that keeps taking writes; the
    manifest brackets each count with a read before and after the snapshot,
    and the restored count must fall inside ``[before, after]``."""
    problems = []
    for name, rng in sorted(expected.items()):
        lo, hi = sorted((int(rng["before"]), int(rng["after"])))
        got = restored.get(name)
        if got is None:
            problems.append(f"{name}: absent after restore (manifest {lo}..{hi})")
        elif not lo <= int(got) <= hi:
            problems.append(f"{name}: restored {got} outside manifest {lo}..{hi}")
    return problems


# ---------------------------------------------------------------------------
# Key escrow — names only
# ---------------------------------------------------------------------------


def env_names(env_text: str) -> dict[str, bool]:
    """``{NAME: has_a_non_empty_value}`` for every assignment in a .env text.
    Values are read only to test emptiness; nothing returned carries one."""
    out: dict[str, bool] = {}
    for raw in env_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        name, _, value = line.partition("=")
        name = name.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        out[name] = bool(value.strip().strip("'\""))
    return out


def env_value(env_text: str, name: str) -> str | None:
    """The value of ``name`` in a .env text (last assignment wins)."""
    val = None
    for raw in env_text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith(name + "="):
            val = line[len(name) + 1:].strip().strip("'\"")
    return val


def escrow_inventory(env_text: str, keyfiles: dict[str, bool] | None = None) -> dict:
    """What the sealed escrow artifact holds, by name. ``complete`` is False
    when a required secret is absent (no inline value and no keyfile)."""
    names = env_names(env_text)
    keyfiles = keyfiles or {}
    secrets = {}
    for n in ESCROW_REQUIRED + ESCROW_OPTIONAL:
        secrets[n] = bool(names.get(n))
    missing = []
    for n in ESCROW_REQUIRED:
        alt = ESCROW_ALTERNATES.get(n)
        if secrets[n]:
            continue
        if alt and secrets.get(alt) and keyfiles.get(alt):
            continue
        missing.append(n)
    return {
        "secrets_present": secrets,
        "keyfiles_included": dict(sorted(keyfiles.items())),
        "env_names": len(names),
        "missing_required": missing,
        "complete": not missing,
    }


# ---------------------------------------------------------------------------
# Status files (read by the registry's /v3/system/backup-status route)
# ---------------------------------------------------------------------------

STATUS_FILES = {"backup": "last_backup.json", "drill": "last_drill.json"}


def write_status(state_dir: pathlib.Path, kind: str, payload: dict) -> pathlib.Path:
    """Atomically write ``last_<kind>.json`` (only ever on success)."""
    if kind not in STATUS_FILES:
        raise ValueError(f"unknown status kind {kind!r}")
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / STATUS_FILES[kind]
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".status-")
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------------------
# OpenSearch logical export / import
# ---------------------------------------------------------------------------


def _os_req(url: str, method: str = "GET", body: bytes | None = None,
            ctype: str = "application/json") -> dict:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"content-type": ctype})
    with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310 — loopback store
        return json.loads(r.read() or b"{}")


def os_user_indices(base: str) -> list[str]:
    rows = _os_req(f"{base}/_cat/indices?format=json&h=index")
    return sorted(
        r["index"] for r in rows
        if not r["index"].startswith(".") and not r["index"].startswith("top_queries-")
    )


def os_count(base: str, index: str) -> int:
    return int(_os_req(f"{base}/{index}/_count")["count"])


def os_index_meta(base: str, index: str) -> dict:
    """Mappings + the settings a fresh index can take back (analysis, shards)."""
    mapping = _os_req(f"{base}/{index}/_mapping")[index]["mappings"]
    settings = _os_req(f"{base}/{index}/_settings")[index]["settings"]["index"]
    keep = {k: settings[k] for k in ("number_of_shards", "analysis") if k in settings}
    return {"index": index, "mappings": mapping, "settings": {"index": keep}}


def os_export_lines(base: str, index: str, page: int = 1000) -> Iterator[bytes]:
    """NDJSON ``{"_id":..,"_source":..}`` per document, via scroll."""
    body = json.dumps({"size": page, "sort": ["_doc"]}).encode()
    res = _os_req(f"{base}/{index}/_search?scroll=5m", "POST", body)
    sid = res.get("_scroll_id")
    try:
        while True:
            hits = res["hits"]["hits"]
            if not hits:
                break
            for h in hits:
                yield json.dumps({"_id": h["_id"], "_source": h["_source"]},
                                 separators=(",", ":")).encode() + b"\n"
            res = _os_req(f"{base}/_search/scroll", "POST",
                          json.dumps({"scroll": "5m", "scroll_id": sid}).encode())
            sid = res.get("_scroll_id", sid)
    finally:
        if sid:
            try:
                _os_req(f"{base}/_search/scroll", "DELETE",
                        json.dumps({"scroll_id": sid}).encode())
            except Exception:  # noqa: BLE001 — a scroll expires on its own
                pass


def bulk_body(lines: Iterable[bytes], index: str) -> bytes:
    out = []
    for ln in lines:
        doc = json.loads(ln)
        out.append(json.dumps({"index": {"_index": index, "_id": doc["_id"]}}).encode())
        out.append(json.dumps(doc["_source"], separators=(",", ":")).encode())
    return b"\n".join(out) + b"\n"


def os_import(base: str, meta: dict, lines: Iterable[bytes], batch: int = 2000) -> int:
    index = meta["index"]
    _os_req(f"{base}/{index}", "PUT",
            json.dumps({"settings": meta["settings"], "mappings": meta["mappings"]}).encode())
    n = 0
    buf: list[bytes] = []

    def flush() -> None:
        res = _os_req(f"{base}/_bulk", "POST", bulk_body(buf, index), "application/x-ndjson")
        if res.get("errors"):
            first = next(i for i in res["items"] if i["index"].get("error"))
            raise RuntimeError(f"bulk error: {first['index']['error']}")
        buf.clear()

    for ln in lines:
        if not ln.strip():
            continue
        buf.append(ln)
        n += 1
        if len(buf) >= batch:
            flush()
    if buf:
        flush()
    _os_req(f"{base}/{index}/_refresh", "POST")
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_lines(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    p = pathlib.Path(path)
    return p.read_text().splitlines() if p.exists() else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backup_lib")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-id")
    r = sub.add_parser("retention")
    r.add_argument("--daily", type=int, default=7)
    r.add_argument("--weekly", type=int, default=4)
    r.add_argument("set_ids", nargs="*")
    d = sub.add_parser("parse-dest")
    d.add_argument("dest")
    a = sub.add_parser("archive-delta")
    a.add_argument("index_file")
    a.add_argument("current_file")
    m = sub.add_parser("manifest")
    m.add_argument("set_id")
    m.add_argument("fragments_dir")
    m.add_argument("--meta", default="{}")
    v = sub.add_parser("verify")
    v.add_argument("manifest")
    v.add_argument("restored")
    h = sub.add_parser("verify-sample")
    h.add_argument("rows_tsv")
    h.add_argument("root")
    pl = sub.add_parser("pack-lookup")
    pl.add_argument("lists_dir")
    pl.add_argument("wanted_file")
    e = sub.add_parser("escrow-inventory")
    e.add_argument("env_file")
    e.add_argument("--keyfile", action="append", default=[])
    eg = sub.add_parser("env-get")
    eg.add_argument("env_file")
    eg.add_argument("name")
    s = sub.add_parser("status")
    s.add_argument("state_dir")
    s.add_argument("kind")
    s.add_argument("payload")
    oi = sub.add_parser("os-indices")
    oi.add_argument("base")
    oc = sub.add_parser("os-count")
    oc.add_argument("base")
    oc.add_argument("index")
    om = sub.add_parser("os-meta")
    om.add_argument("base")
    om.add_argument("index")
    oe = sub.add_parser("os-export")
    oe.add_argument("base")
    oe.add_argument("index")
    ox = sub.add_parser("os-import")
    ox.add_argument("base")
    ox.add_argument("meta_file")
    args = ap.parse_args(argv)

    if args.cmd == "set-id":
        print(set_id_for())
    elif args.cmd == "retention":
        _, delete = plan_retention(args.set_ids, daily=args.daily, weekly=args.weekly)
        print("\n".join(delete))
    elif args.cmd == "parse-dest":
        try:
            print(json.dumps(parse_dest(args.dest)))
        except ValueError as exc:
            print(f"backup_lib: {exc}", file=sys.stderr)
            return 2
    elif args.cmd == "archive-delta":
        print("\n".join(archive_delta(_read_lines(args.index_file), _read_lines(args.current_file))))
    elif args.cmd == "manifest":
        frags = {}
        for f in sorted(pathlib.Path(args.fragments_dir).glob("*.json")):
            frags[f.stem] = json.loads(f.read_text())
        print(json.dumps(build_manifest(args.set_id, frags, meta=json.loads(args.meta)),
                         indent=2, sort_keys=True))
    elif args.cmd == "verify":
        man = json.loads(pathlib.Path(args.manifest).read_text())
        restored = json.loads(pathlib.Path(args.restored).read_text())
        comps = man["components"]
        problems = []
        if "postgres" in comps:
            problems += verify_exact_counts(comps["postgres"]["table_counts"],
                                            restored.get("postgres", {}))
        if "qdrant" in comps:
            problems += verify_range_counts(comps["qdrant"]["collections"],
                                            restored.get("qdrant", {}))
        if "opensearch" in comps:
            problems += verify_exact_counts(
                {k: v["exported"] for k, v in comps["opensearch"]["indices"].items()},
                restored.get("opensearch", {}))
        for p in problems:
            print(f"FAIL {p}")
        return 1 if problems else 0
    elif args.cmd == "verify-sample":
        rows = [tuple(ln.split("\t")[:2]) for ln in _read_lines(args.rows_tsv) if "\t" in ln]
        res = verify_hash_sample(rows, pathlib.Path(args.root))
        print(json.dumps(res))
        return 0 if res["checked"] and res["ok"] == res["checked"] else 1
    elif args.cmd == "pack-lookup":
        lists = {f.name[: -len(".list")]: f.read_text().splitlines()
                 for f in pathlib.Path(args.lists_dir).glob("*.list")}
        print(json.dumps(pack_lookup(lists, _read_lines(args.wanted_file))))
    elif args.cmd == "escrow-inventory":
        text = pathlib.Path(args.env_file).read_text()
        kf = {}
        for item in args.keyfile:
            name, _, present = item.partition("=")
            kf[name] = present == "1"
        print(json.dumps(escrow_inventory(text, kf), indent=2, sort_keys=True))
    elif args.cmd == "env-get":
        # Knob reads only: a secret never leaves through this door.
        if not args.name.startswith(KNOB_PREFIX):
            print(f"backup_lib: env-get reads {KNOB_PREFIX}* knobs only", file=sys.stderr)
            return 2
        p = pathlib.Path(args.env_file)
        val = env_value(p.read_text(), args.name) if p.is_file() else None
        if val is None:
            return 1
        print(val)
    elif args.cmd == "status":
        write_status(pathlib.Path(args.state_dir), args.kind, json.loads(args.payload))
    elif args.cmd == "os-indices":
        print("\n".join(os_user_indices(args.base)))
    elif args.cmd == "os-count":
        print(os_count(args.base, args.index))
    elif args.cmd == "os-meta":
        print(json.dumps(os_index_meta(args.base, args.index)))
    elif args.cmd == "os-export":
        n = 0
        out = sys.stdout.buffer
        for line in os_export_lines(args.base, args.index):
            out.write(line)
            n += 1
        out.flush()
        print(n, file=sys.stderr)
    elif args.cmd == "os-import":
        meta = json.loads(pathlib.Path(args.meta_file).read_text())
        print(os_import(args.base, meta, sys.stdin.buffer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
