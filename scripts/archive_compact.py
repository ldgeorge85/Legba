#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compress the evidence archive IN PLACE, one verified object at a time.

DRY-RUN BY DEFAULT: every candidate is read, hash-verified and compressed in
memory so the before/after numbers are MEASURED, and nothing is written.
``--apply`` writes.

Per object ``{root}/{hh}/{sha256}`` (only the 256 two-hex directories — the
``grading/``, ``reference/`` and ``reference_notes/`` subtrees are other
writers' layouts and are never touched):

1. skip writer temp files (``.tmp.*``) and anything not named by a 64-hex
   address; skip non-regular files;
2. read; classify against the ADDRESS (the file name): already zstd and
   decoding to the address → skip (this is what makes the job resumable);
   original bytes hashing to the address → candidate; anything else →
   MISMATCH, reported, never touched;
3. with ``--shas-file`` (the sha256s the database knows, one per line;
   ``cas:sha256/<hex>`` accepted), a file no row names is an ORPHAN — reported
   and skipped unless ``--include-orphans``; a row naming a file absent from
   a scanned prefix is reported as ROW-WITHOUT-FILE;
4. compress (zstd, ``--level``, default 3); an object that does not shrink by
   at least ``--min-saving`` stays as it is;
5. ``--apply``: write the frame to a temp file in the same directory, fsync,
   read it BACK and decode-verify it against the address, copy mode / owner /
   atime+mtime from the original, re-check the original has not been
   replaced or removed meanwhile, then ``os.replace`` (atomic) and fsync the
   directory. The original is never removed before its verified replacement
   takes its place; a kill at any point leaves either the original or a
   verified replacement, plus at worst a ``.tmp.compact.*`` file the next run
   sweeps.

Concurrency: the live archiver writes temp+rename and never rewrites an
existing address, and every reader detects the codec by frame magic
(``legba.data.archive_codec``), so the job runs beside both. A reader holding
the old file open keeps reading the old inode.

Bounded I/O: ``--max-mb-per-min`` paces the bytes READ (default 300, i.e.
5 MB/s); the process lowers its CPU priority (nice 10) and its I/O class
(``ionice -c2 -n7`` when the binary exists). ``--limit`` and ``--prefix``
(repeatable, or ``00-0f`` ranges) stage a run.

Run it where the archive is mounted and the ``zstandard`` binding exists —
the runtime image after the N-K roll::

    docker run --rm -v legba_archive:/archive -v "$PWD/scripts:/s:ro" \\
        --entrypoint python legba/legba-runtime-dapr:latest \\
        /s/archive_compact.py --root /archive --prefix 00 [--apply]

Exit status: 0 done (mismatches are REPORTED, not fatal), 2 usage/env.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from legba.data import archive_codec
except ImportError:  # run from a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from legba.data import archive_codec

HEX = set("0123456789abcdef")
TMP_PREFIX = ".tmp.compact."
STALE_TMP_SECONDS = 3600
MAX_LISTED = 200


def _is_address(name: str) -> bool:
    return len(name) == 64 and all(c in HEX for c in name)


def parse_prefixes(values: list[str] | None) -> list[str]:
    """``["00", "1a-1f"]`` → the sorted two-hex prefixes; all 256 when empty."""
    if not values:
        return [f"{i:02x}" for i in range(256)]
    out: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            part = part.strip().lower()
            if not part:
                continue
            lo, _, hi = part.partition("-")
            hi = hi or lo
            if len(lo) != 2 or len(hi) != 2 or not set(lo + hi) <= HEX:
                raise ValueError(f"bad prefix {part!r} (want two hex digits or aa-bb)")
            out.update(f"{i:02x}" for i in range(int(lo, 16), int(hi, 16) + 1))
    return sorted(out)


def load_shas(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    shas: set[str] = set()
    for line in path.read_text().splitlines():
        s = line.strip().lower()
        if s.startswith("cas:sha256/"):
            s = s[len("cas:sha256/"):]
        if _is_address(s):
            shas.add(s)
    return shas


class Pacer:
    """Bytes-per-minute pacing of reads (``0`` = unpaced)."""

    def __init__(self, mb_per_min: float) -> None:
        self.rate = mb_per_min * (1 << 20) / 60.0
        self.start = time.monotonic()
        self.bytes = 0

    def account(self, n: int) -> None:
        self.bytes += n
        if self.rate <= 0:
            return
        ahead = self.bytes / self.rate - (time.monotonic() - self.start)
        if ahead > 0:
            time.sleep(ahead)


@dataclass
class Report:
    mode: str
    root: str
    prefixes: str
    level: int
    files_seen: int = 0
    compressed: int = 0             # would compress (dry-run) / did (apply)
    already_compressed: int = 0
    incompressible: int = 0
    orphans: int = 0
    orphans_compressed: int = 0
    mismatches: int = 0
    rows_without_file: int = 0
    skipped_tmp: int = 0
    skipped_foreign: int = 0
    skipped_changed: int = 0        # original replaced/removed mid-run
    errors: int = 0
    stale_tmp_removed: int = 0
    bytes_before: int = 0           # at-rest bytes of every verified object seen
    bytes_after: int = 0            # at-rest bytes after (projected in dry-run)
    bytes_read: int = 0
    seconds: float = 0.0
    mismatch_paths: list[str] = field(default_factory=list)
    orphan_paths: list[str] = field(default_factory=list)
    rows_without_file_sample: list[str] = field(default_factory=list)
    error_samples: list[str] = field(default_factory=list)

    def note(self, bucket: list[str], item: str) -> None:
        if len(bucket) < MAX_LISTED:
            bucket.append(item)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _sweep_stale_tmp(d: Path, rep: Report) -> None:
    now = time.time()
    for entry in os.scandir(d):
        if entry.name.startswith(TMP_PREFIX):
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime > STALE_TMP_SECONDS:
                    os.unlink(entry.path)
                    rep.stale_tmp_removed += 1
            except OSError:
                pass


def replace_verified(path: Path, sha: str, frame: bytes, before: os.stat_result) -> str:
    """Swap ``path`` for ``frame`` after verifying it. Returns ``"ok"``,
    ``"changed"`` (original replaced/removed meanwhile — left alone) or raises."""
    tmp = path.parent / f"{TMP_PREFIX}{os.getpid()}.{sha[:16]}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(frame)
            fh.flush()
            os.fsync(fh.fileno())
        if archive_codec.decode_verified(tmp.read_bytes(), sha) is None:
            raise RuntimeError(f"read-back of compressed {sha} does not verify")
        os.chmod(tmp, before.st_mode & 0o7777)
        try:
            os.chown(tmp, before.st_uid, before.st_gid)
        except PermissionError:
            pass  # not root: ownership is already ours
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
        try:
            now = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return "changed"
        if (now.st_ino, now.st_size, now.st_mtime_ns) != (
                before.st_ino, before.st_size, before.st_mtime_ns):
            return "changed"
        os.replace(tmp, path)
        _fsync_dir(path.parent)
        return "ok"
    finally:
        tmp.unlink(missing_ok=True)


def compact(root: Path, *, prefixes: list[str], apply: bool, level: int = 3,
            limit: int | None = None, shas: set[str] | None = None,
            include_orphans: bool = False, min_saving: float = 0.05,
            pacer: Pacer | None = None, progress_every: int = 5000,
            out=sys.stderr) -> Report:
    rep = Report(mode="apply" if apply else "dry-run", root=str(root),
                 prefixes=(f"{prefixes[0]}..{prefixes[-1]}" if len(prefixes) > 1 else prefixes[0]),
                 level=level)
    t0 = time.monotonic()
    done = 0
    seen_addresses: set[str] = set()
    for pfx in prefixes:
        d = root / pfx
        if not d.is_dir():
            continue
        if apply:
            _sweep_stale_tmp(d, rep)
        for name in sorted(os.listdir(d)):
            if limit is not None and done >= limit:
                break
            if name.startswith(".tmp."):
                rep.skipped_tmp += 1
                continue
            if not _is_address(name):
                rep.skipped_foreign += 1
                continue
            path = d / name
            try:
                before = os.stat(path, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    rep.skipped_foreign += 1
                    continue
                data = path.read_bytes()
            except FileNotFoundError:
                rep.skipped_changed += 1
                continue
            except OSError as exc:
                rep.errors += 1
                rep.note(rep.error_samples, f"{pfx}/{name}: {exc}")
                continue
            rep.files_seen += 1
            seen_addresses.add(name)
            rep.bytes_read += len(data)
            if pacer is not None:
                pacer.account(len(data))
            try:
                codec = archive_codec.stored_codec(data, name)
            except archive_codec.ArchiveCodecUnavailable as exc:
                print(f"archive_compact: {exc}", file=out)
                raise SystemExit(2) from exc
            if codec is None:
                rep.mismatches += 1
                rep.note(rep.mismatch_paths, f"{pfx}/{name}")
                continue
            rep.bytes_before += len(data)
            if codec == archive_codec.CODEC_ZSTD:
                rep.already_compressed += 1
                rep.bytes_after += len(data)
                continue
            orphan = shas is not None and name not in shas
            if orphan:
                rep.orphans += 1
                rep.note(rep.orphan_paths, f"{pfx}/{name}")
                if not include_orphans:
                    rep.bytes_after += len(data)
                    continue
            frame = archive_codec.encode(data, codec=archive_codec.CODEC_ZSTD, level=level)
            if frame.codec != archive_codec.CODEC_ZSTD:
                print("archive_compact: the zstandard binding is not importable here", file=out)
                raise SystemExit(2)
            if len(frame.data) > len(data) * (1.0 - min_saving):
                rep.incompressible += 1
                rep.bytes_after += len(data)
                continue
            done += 1
            if not apply:
                rep.compressed += 1
                rep.orphans_compressed += int(orphan)
                rep.bytes_after += len(frame.data)
            else:
                try:
                    outcome = replace_verified(path, name, frame.data, before)
                except Exception as exc:  # never fatal for the run; original intact
                    rep.errors += 1
                    rep.note(rep.error_samples, f"{pfx}/{name}: {exc}")
                    rep.bytes_after += len(data)
                    continue
                if outcome == "ok":
                    rep.compressed += 1
                    rep.orphans_compressed += int(orphan)
                    rep.bytes_after += len(frame.data)
                else:
                    rep.skipped_changed += 1
                    rep.bytes_after += len(data)
            if progress_every and rep.files_seen % progress_every == 0:
                print(f"archive_compact: {pfx} files={rep.files_seen} "
                      f"before={rep.bytes_before} after={rep.bytes_after} "
                      f"mismatch={rep.mismatches} t={time.monotonic() - t0:.0f}s", file=out)
        if limit is not None and done >= limit:
            break
    if shas is not None:
        scanned = set(prefixes)
        missing = sorted(s for s in shas if s[:2] in scanned and s not in seen_addresses)
        # With --limit the scan stopped early: only report what it covered.
        if limit is None:
            rep.rows_without_file = len(missing)
            for s in missing[:MAX_LISTED]:
                rep.rows_without_file_sample.append(f"{s[:2]}/{s}")
    rep.seconds = round(time.monotonic() - t0, 2)
    return rep


def _lower_priority() -> None:
    try:
        os.nice(10)
    except OSError:
        pass
    ionice = shutil.which("ionice")
    if ionice:
        subprocess.run([ionice, "-c", "2", "-n", "7", "-p", str(os.getpid())],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=os.environ.get("LEGBA_ARCHIVE_ROOT", "/var/lib/legba/archive"))
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--prefix", action="append", help="two-hex prefix or aa-bb range (repeatable)")
    ap.add_argument("--limit", type=int, default=None, help="stop after N objects compressed")
    ap.add_argument("--level", type=int, default=archive_codec.configured_level())
    ap.add_argument("--max-mb-per-min", type=float, default=300.0, help="read pacing; 0 = unpaced")
    ap.add_argument("--min-saving", type=float, default=0.05)
    ap.add_argument("--shas-file", type=Path, default=None,
                    help="sha256s the database references (enables orphan / row-without-file checks)")
    ap.add_argument("--include-orphans", action="store_true",
                    help="also compress verified files no row names (default: report only)")
    ap.add_argument("--report", type=Path, default=None, help="write the JSON report here too")
    ap.add_argument("--no-nice", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"archive_compact: root {root} is not a directory", file=sys.stderr)
        return 2
    if not archive_codec.zstd_available():
        print("archive_compact: the 'zstandard' binding is not importable — run in "
              "the runtime image (or pip install zstandard)", file=sys.stderr)
        return 2
    try:
        prefixes = parse_prefixes(args.prefix)
    except ValueError as exc:
        print(f"archive_compact: {exc}", file=sys.stderr)
        return 2
    if not args.no_nice:
        _lower_priority()
    rep = compact(
        root, prefixes=prefixes, apply=args.apply, level=args.level, limit=args.limit,
        shas=load_shas(args.shas_file), include_orphans=args.include_orphans,
        min_saving=args.min_saving, pacer=Pacer(args.max_mb_per_min),
    )
    body = json.dumps(rep.__dict__, indent=2)
    print(body)
    if args.report is not None:
        args.report.write_text(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
