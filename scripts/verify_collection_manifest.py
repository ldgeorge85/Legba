#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 7g — verify a collection manifest against the live providers.

A collection manifest (``descriptors/collection_*.yaml``) claims, per series
and per subject, what a provider actually holds: the first and last year with
a value, and how many years of the declared window are empty. This script is
what makes that claim checkable. It reads the manifest, goes to the provider,
and prints the coverage it MEASURES beside the coverage the file DECLARES.

Why it exists rather than a comment in the YAML: Program 7g's whole premise is
that a number Legba cites is a number Legba fetched. A manifest that says
"Iran, 2016-2024, one null" is worth nothing if nobody ever re-ran the fetch —
and a provider that silently freezes a series (EIA's international crude trade
stopped in 2021 and still answers 200) is exactly the failure that a
hand-written coverage block cannot notice.

WHAT IT DOES NOT DO. It does not load anything, it does not touch the
database, it does not write a row, it does not register a descriptor. There is
no DB driver imported here at all. Loading is ``scripts/load_collection.py``
(7g-1) and is a different program with a different blast radius.

FETCH SHAPES. Two, both declared per-series in the manifest's ``fetch.mode``:

``json_api``
    One HTTP GET per (series, subject) against ``source_url_template`` with
    ``{subject}``/``{iso3}`` substituted. World Bank's v2 route is this.
``bulk_jsonl``
    ONE download of a provider bulk archive (a zip holding a JSON-lines
    member), scanned once for every (series, subject) pair that names it. EIA's
    International Energy Data is this — 105k series in a 24 MB zip, so twenty
    separate calls would be twenty downloads of the same file.

KEYED SERIES. A series whose ``fetch.requires_key`` is true is SKIPPED with a
reason unless the key is in the environment under the variable the manifest
names (``fetch.key_env``). A skip is not a failure and never counts as
coverage — it prints as ``skipped`` with the reason attached, so a manifest
that quietly needs credentials cannot read as a verified one.

EXIT CODES::

    0   every series resolved for at least one subject (drift may be reported)
    1   at least one series resolved NOWHERE — no subject, no year, no value
    2   --strict was passed and measured coverage drifts from the declared
        block (or a series was skipped for a missing key)

USAGE::

    python3 scripts/verify_collection_manifest.py \\
        descriptors/collection_series_pilot_2016_2026.yaml
    python3 scripts/verify_collection_manifest.py <manifest> --strict
    python3 scripts/verify_collection_manifest.py <manifest> \\
        --cache-dir /var/tmp/legba-collections   # reuse a downloaded bulk file

Stdlib plus ``httpx`` and ``yaml``. Nothing from ``legba``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import httpx
import yaml


#: Deploy marker — grep target for this exact string.
COLLECTION_VERIFIER_VERSION = "2026-09/7g-3"

#: How long a single provider call may take. The World Bank route answers in
#: well under a second; the EIA bulk zip is 24 MB and gets its own budget.
_JSON_TIMEOUT_S = 45.0
_BULK_TIMEOUT_S = 600.0


# ===========================================================================
# 1. The parsing half — pure, no network, unit-tested against a fixture
# ===========================================================================


class ManifestError(ValueError):
    """The manifest is not a collection manifest this verifier can read.

    Raised rather than returning an empty plan: a manifest missing its
    ``manifest.series`` list must not verify as "nothing to check, all green".
    """


@dataclass(frozen=True)
class SubjectCoverage:
    """What a provider holds for ONE series and ONE subject in the window.

    ``first_year``/``last_year`` are ``None`` when the provider holds nothing —
    absence rendered as absence. ``values`` is the count of years carrying a
    number and ``nulls`` the count that do not; the two always sum to the
    window's year count, which is what makes a null count checkable rather
    than decorative.
    """

    subject: str
    first_year: int | None
    last_year: int | None
    values: int
    nulls: int
    provider_last_updated: str | None = None
    skipped_reason: str | None = None

    @property
    def held(self) -> bool:
        return self.values > 0

    def as_declared(self) -> dict[str, Any]:
        """The shape a manifest ``coverage.<SUBJECT>`` block would carry."""
        return {
            "first_valid_year": self.first_year,
            "last_valid_year": self.last_year,
            "values": self.values,
            "nulls": self.nulls,
        }


@dataclass(frozen=True)
class SeriesSpec:
    """One ``manifest.series[*]`` entry, normalised."""

    series_id: str
    provider: str
    dataset: str
    indicator: str
    unit: str
    cadence: str
    subject_kind: str
    subjects: tuple[str, ...]
    valid_from: str
    valid_to: str
    source_url_template: str
    fetch: dict[str, Any] = field(default_factory=dict)
    declared: dict[str, Any] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return str(self.fetch.get("mode") or "json_api")

    @property
    def requires_key(self) -> bool:
        return bool(self.fetch.get("requires_key"))

    @property
    def key_env(self) -> str | None:
        env = self.fetch.get("key_env")
        return str(env) if env else None

    def years(self) -> tuple[int, ...]:
        """The declared valid-time window as a tuple of years."""
        return tuple(range(_year_of(self.valid_from), _year_of(self.valid_to) + 1))


@dataclass(frozen=True)
class Manifest:
    """A parsed collection manifest: identity, subjects, series."""

    collection_id: str
    state: str
    licence_class: str
    origin_class: str
    subjects: dict[str, dict[str, Any]]
    series: tuple[SeriesSpec, ...]
    path: Path

    def iso3(self, subject: str) -> str:
        entry = self.subjects.get(subject)
        if entry is None:
            raise ManifestError(
                f"series names subject {subject!r}, which the manifest's "
                f"top-level `subjects:` block does not declare "
                f"(declared: {sorted(self.subjects)})"
            )
        iso3 = entry.get("iso3")
        if not iso3:
            raise ManifestError(
                f"subject {subject!r} carries no `iso3` — an EIA series id "
                "cannot be built without it"
            )
        return str(iso3)


def _year_of(value: Any) -> int:
    """The year of a ``YYYY-MM-DD`` date (or a bare ``YYYY``), as an int."""
    text = str(value).strip()
    head = text.split("-", 1)[0]
    if not head.isdigit() or len(head) != 4:
        raise ManifestError(f"not a date this verifier can read: {value!r}")
    return int(head)


_REQUIRED_SERIES_FIELDS = (
    "series_id",
    "provider",
    "dataset",
    "indicator",
    "unit",
    "subjects",
    "valid_from",
    "valid_to",
    "cadence",
    "source_url_template",
)


def parse_manifest(path: Path) -> Manifest:
    """Read a collection descriptor off disk into a :class:`Manifest`.

    Pure apart from the one file read: no provider is contacted here, which is
    what lets the whole parsing half be unit-tested against a fixture with no
    network at all.
    """
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ManifestError(f"{path}: not a YAML mapping")
    return parse_manifest_body(body, path)


def parse_manifest_body(body: dict[str, Any], path: Path) -> Manifest:
    """The parsing half of :func:`parse_manifest`, over an already-loaded dict."""
    identity = body.get("identity") or {}
    manifest_block = body.get("manifest") or {}
    raw_series = manifest_block.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise ManifestError(
            f"{path}: `manifest.series` is missing or empty — a collection "
            "with no series is not a collection, and must not verify green"
        )

    subjects: dict[str, dict[str, Any]] = {}
    for entry in body.get("subjects") or []:
        if not isinstance(entry, dict) or not entry.get("subject"):
            raise ManifestError(f"{path}: malformed `subjects` entry {entry!r}")
        subjects[str(entry["subject"])] = entry

    specs: list[SeriesSpec] = []
    for raw in raw_series:
        if not isinstance(raw, dict):
            raise ManifestError(f"{path}: a `manifest.series` entry is not a mapping")
        missing = [f for f in _REQUIRED_SERIES_FIELDS if raw.get(f) in (None, "", [])]
        if missing:
            raise ManifestError(
                f"{path}: series {raw.get('series_id', '<unnamed>')!r} is "
                f"missing required field(s): {missing}"
            )
        specs.append(
            SeriesSpec(
                series_id=str(raw["series_id"]),
                provider=str(raw["provider"]),
                dataset=str(raw["dataset"]),
                indicator=str(raw["indicator"]),
                unit=str(raw["unit"]),
                cadence=str(raw["cadence"]),
                subject_kind=str(raw.get("subject_kind") or body.get("subject_kind") or ""),
                subjects=tuple(str(s) for s in raw["subjects"]),
                valid_from=str(raw["valid_from"]),
                valid_to=str(raw["valid_to"]),
                source_url_template=str(raw["source_url_template"]),
                fetch=dict(raw.get("fetch") or {}),
                declared=dict(raw.get("coverage") or {}),
            )
        )

    return Manifest(
        collection_id=str(identity.get("id") or path.stem),
        state=str(identity.get("state") or "unknown"),
        licence_class=str(body.get("licence_class") or ""),
        origin_class=str(body.get("origin_class") or ""),
        subjects=subjects,
        series=tuple(specs),
        path=path,
    )


def resolve_url(spec: SeriesSpec, subject: str, iso3: str) -> str:
    """``source_url_template`` with the subject's codes substituted."""
    return spec.source_url_template.format(subject=subject, iso3=iso3)


def resolve_indicator(spec: SeriesSpec, subject: str, iso3: str) -> str:
    """``indicator`` with the subject's codes substituted (EIA series ids)."""
    return spec.indicator.format(subject=subject, iso3=iso3)


def year_map_from_world_bank(payload: Any) -> tuple[dict[int, Any], str | None]:
    """``({year: value}, provider_last_updated)`` from one World Bank v2 body.

    THE parse of a World Bank response — :func:`coverage_from_world_bank`
    counts over what this returns, and ``scripts/load_collection.py`` writes
    rows from it. Two readers, one parser: a loader that re-derived the year
    map itself could disagree with the verifier about what a provider holds,
    and the disagreement would only ever surface as a wrong number in a
    citation.

    The v2 route answers ``[envelope, rows]``; a country/indicator pair the
    provider does not hold answers ``[envelope, None]`` or an envelope carrying
    a ``message``. Both are an EMPTY map, not an error — the manifest records
    them as an unheld subject with a reason. A year whose ``value`` is null is
    kept in the map as ``None``: absence is absence, and the caller decides
    whether that means "no row" (the loader) or "a null year" (the counter).
    """
    if not isinstance(payload, list) or not payload:
        raise ManifestError(f"World Bank response is not a v2 envelope: {payload!r:.120}")
    envelope = payload[0] if isinstance(payload[0], dict) else {}
    last_updated = envelope.get("lastupdated")
    rows = payload[1] if len(payload) > 1 else None
    by_year: dict[int, Any] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        if date is None or not str(date).isdigit():
            continue
        by_year[int(date)] = row.get("value")
    return by_year, (str(last_updated) if last_updated else None)


def year_map_from_eia_record(
    record: dict[str, Any] | None,
) -> tuple[dict[int, Any], str | None]:
    """``({year: value}, provider_last_updated)`` from one EIA bulk record.

    ``record['data']`` is ``[[period, value], ...]`` newest-first, with the
    period a bare ``YYYY`` for annual series; a monthly/quarterly period on an
    annual read is skipped rather than truncated to its year. A record absent
    from the bulk file (``None``) is an empty map. Same two-readers-one-parser
    rule as :func:`year_map_from_world_bank`.
    """
    by_year: dict[int, Any] = {}
    last_updated = None
    if record is not None:
        last_updated = record.get("last_updated")
        for pair in record.get("data") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            period = str(pair[0])
            if len(period) < 4 or not period[:4].isdigit():
                continue
            if len(period) != 4:      # a monthly/quarterly period on an annual read
                continue
            by_year[int(period)] = pair[1]
    return by_year, (str(last_updated) if last_updated else None)


def coverage_from_world_bank(
    payload: Any, subject: str, years: Sequence[int]
) -> SubjectCoverage:
    """Coverage from one World Bank v2 JSON response body."""
    by_year, last_updated = year_map_from_world_bank(payload)
    return _coverage_from_year_map(subject, by_year, years, last_updated)


def coverage_from_eia_record(
    record: dict[str, Any] | None, subject: str, years: Sequence[int]
) -> SubjectCoverage:
    """Coverage from one EIA bulk JSONL series record."""
    by_year, last_updated = year_map_from_eia_record(record)
    return _coverage_from_year_map(subject, by_year, years, last_updated)


def _coverage_from_year_map(
    subject: str,
    by_year: dict[int, Any],
    years: Sequence[int],
    last_updated: Any,
) -> SubjectCoverage:
    have = sorted(y for y in years if by_year.get(y) is not None)
    return SubjectCoverage(
        subject=subject,
        first_year=have[0] if have else None,
        last_year=have[-1] if have else None,
        values=len(have),
        nulls=len(years) - len(have),
        provider_last_updated=str(last_updated) if last_updated else None,
    )


def declared_matches(spec: SeriesSpec, measured: SubjectCoverage) -> bool | None:
    """Does the manifest's declared block agree with what was measured?

    ``None`` when the manifest declares nothing for that subject — an absent
    declaration is reported as ``-``, never silently as agreement.
    """
    block = spec.declared.get(measured.subject)
    if not isinstance(block, dict):
        return None
    want = {
        "first_valid_year": block.get("first_valid_year"),
        "last_valid_year": block.get("last_valid_year"),
        "values": block.get("values"),
        "nulls": block.get("nulls"),
    }
    return want == measured.as_declared()


# ===========================================================================
# 2. The fetching half
# ===========================================================================


def fetch_world_bank(client: httpx.Client, url: str) -> Any:
    """GET one World Bank v2 URL and decode its JSON body."""
    response = client.get(url, timeout=_JSON_TIMEOUT_S)
    response.raise_for_status()
    return response.json()


def download_bulk(client: httpx.Client, url: str, destination: Path) -> Path:
    """Stream a provider bulk archive to ``destination``, reusing a cached copy.

    The EIA international file is 24 MB and carries every series this
    collection reads, so it is fetched ONCE per run (and reused across runs
    when ``--cache-dir`` points somewhere durable) rather than once per series.
    """
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    with client.stream("GET", url, timeout=_BULK_TIMEOUT_S, follow_redirects=True) as r:
        r.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)
    partial.replace(destination)
    return destination


def scan_bulk_jsonl(
    archive: Path, member: str, wanted: Iterable[str], record_key: str
) -> dict[str, dict[str, Any]]:
    """One pass over a JSON-lines member, keeping only the records asked for."""
    targets = set(wanted)
    found: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(archive) as bundle:
        with bundle.open(member) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                key = record.get(record_key)
                if key in targets:
                    found[key] = record
                    if len(found) == len(targets):
                        break
    return found


def sha256_of(path: Path) -> str:
    """The archive's digest — what a loaded row's ``sha256`` would carry."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bulk_groups(manifest: Manifest) -> dict[tuple[str, str], list[SeriesSpec]]:
    """Group ``bulk_jsonl`` series by the archive they read."""
    groups: dict[tuple[str, str], list[SeriesSpec]] = {}
    for spec in manifest.series:
        if spec.mode != "bulk_jsonl":
            continue
        url = str(spec.fetch.get("bulk_url") or "")
        member = str(spec.fetch.get("bulk_member") or "")
        if not url or not member:
            raise ManifestError(
                f"series {spec.series_id!r} declares mode bulk_jsonl without "
                "`fetch.bulk_url` and `fetch.bulk_member`"
            )
        groups.setdefault((url, member), []).append(spec)
    return groups


def _skip_reason(spec: SeriesSpec) -> str | None:
    """Why this series cannot be fetched here, or ``None`` if it can."""
    if not spec.requires_key:
        return None
    env = spec.key_env
    if env and os.environ.get(env):
        return None
    named = env or "<unnamed key_env>"
    return f"requires a provider API key; {named} is not set in the environment"


def verify(manifest: Manifest, cache_dir: Path) -> list[tuple[SeriesSpec, SubjectCoverage]]:
    """Fetch every (series, subject) the manifest declares and measure coverage."""
    results: list[tuple[SeriesSpec, SubjectCoverage]] = []
    bulk_cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}

    with httpx.Client(headers={"User-Agent": "legba-collection-verifier/1.0"}) as client:
        for key, specs in _bulk_groups(manifest).items():
            url, member = key
            if all(_skip_reason(spec) for spec in specs):
                continue
            wanted = {
                resolve_indicator(spec, subject, manifest.iso3(subject))
                for spec in specs
                for subject in spec.subjects
            }
            archive = cache_dir / Path(httpx.URL(url).path).name
            print(f"  fetching {url} ({len(wanted)} series) ...", file=sys.stderr)
            download_bulk(client, url, archive)
            print(
                f"  {archive.name}: {archive.stat().st_size} bytes, "
                f"sha256 {sha256_of(archive)}",
                file=sys.stderr,
            )
            bulk_cache[key] = scan_bulk_jsonl(
                archive, member, wanted, str(specs[0].fetch.get("record_key") or "series_id")
            )

        for spec in manifest.series:
            years = spec.years()
            reason = _skip_reason(spec)
            for subject in spec.subjects:
                if reason is not None:
                    results.append(
                        (spec, SubjectCoverage(subject, None, None, 0, len(years),
                                               skipped_reason=reason))
                    )
                    continue
                iso3 = manifest.iso3(subject)
                if spec.mode == "json_api":
                    payload = fetch_world_bank(client, resolve_url(spec, subject, iso3))
                    results.append((spec, coverage_from_world_bank(payload, subject, years)))
                elif spec.mode == "bulk_jsonl":
                    key = (str(spec.fetch["bulk_url"]), str(spec.fetch["bulk_member"]))
                    record = bulk_cache.get(key, {}).get(resolve_indicator(spec, subject, iso3))
                    results.append((spec, coverage_from_eia_record(record, subject, years)))
                else:
                    raise ManifestError(
                        f"series {spec.series_id!r}: unknown fetch mode "
                        f"{spec.mode!r} (known: json_api, bulk_jsonl)"
                    )
    return results


# ===========================================================================
# 3. The table
# ===========================================================================


def _cell(value: Any) -> str:
    """A number, or an em dash. Absence never renders as zero."""
    return "—" if value is None else str(value)


def render_table(results: Sequence[tuple[SeriesSpec, SubjectCoverage]]) -> str:
    """The coverage table — one row per (series, subject)."""
    header = (
        f"{'series':38s} {'subj':4s} {'unit':28s} {'first':>5s} {'last':>5s} "
        f"{'vals':>4s} {'null':>4s} {'declared':>12s}  status"
    )
    lines = [header, "-" * len(header)]
    for spec, cov in results:
        if cov.skipped_reason:
            status = f"skipped — {cov.skipped_reason}"
            declared = "—"
        else:
            match = declared_matches(spec, cov)
            if match is None:
                status = "ok (nothing declared)" if cov.held else "NOT HELD (nothing declared)"
            elif match:
                status = "ok" if cov.held else "ok (declared not held)"
            else:
                block = spec.declared.get(cov.subject) or {}
                status = (
                    "DRIFT — declared "
                    f"{_cell(block.get('first_valid_year'))}..{_cell(block.get('last_valid_year'))}"
                    f"/{_cell(block.get('values'))}v/{_cell(block.get('nulls'))}n"
                )
            block = spec.declared.get(cov.subject) or {}
            declared = (
                f"{_cell(block.get('values'))}v/{_cell(block.get('nulls'))}n"
                if block else "—"
            )
        lines.append(
            f"{spec.series_id:38s} {cov.subject:4s} {spec.unit:28s} "
            f"{_cell(cov.first_year):>5s} {_cell(cov.last_year):>5s} "
            f"{cov.values:>4d} {cov.nulls:>4d} {declared:>12s}  {status}"
        )
    return "\n".join(lines)


def _series_order(results: Sequence[tuple[SeriesSpec, SubjectCoverage]]) -> Iterator[str]:
    seen: set[str] = set()
    for spec, _ in results:
        if spec.series_id not in seen:
            seen.add(spec.series_id)
            yield spec.series_id


def summarise(results: Sequence[tuple[SeriesSpec, SubjectCoverage]]) -> tuple[list[str], list[str], list[str]]:
    """``(resolved_nowhere, drifted, skipped)`` — the three things that matter."""
    held: dict[str, bool] = {}
    skipped: dict[str, str] = {}
    drifted: list[str] = []
    for spec, cov in results:
        if cov.skipped_reason:
            skipped.setdefault(spec.series_id, cov.skipped_reason)
            continue
        held[spec.series_id] = held.get(spec.series_id, False) or cov.held
        if declared_matches(spec, cov) is False:
            drifted.append(f"{spec.series_id}/{cov.subject}")
    nowhere = [sid for sid in _series_order(results) if held.get(sid) is False]
    return nowhere, drifted, [f"{sid}: {why}" for sid, why in skipped.items()]


# ===========================================================================
# 4. Entry point
# ===========================================================================


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a collection manifest against the live providers.",
    )
    parser.add_argument("manifest", type=Path, help="path to a collection_*.yaml")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("TMPDIR", "/tmp")) / "legba-collection-cache",
        help="where provider bulk archives are cached between runs",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 when measured coverage drifts from the declared block, "
             "or when a series was skipped for a missing key",
    )
    args = parser.parse_args(argv)

    manifest = parse_manifest(args.manifest)
    print(
        f"collection {manifest.collection_id} (state={manifest.state}, "
        f"origin_class={manifest.origin_class}, licence_class={manifest.licence_class})",
        file=sys.stderr,
    )
    print(
        f"  {len(manifest.series)} series x {len(manifest.subjects)} subjects, "
        f"window {manifest.series[0].valid_from}..{manifest.series[0].valid_to}",
        file=sys.stderr,
    )

    results = verify(manifest, args.cache_dir)
    print(render_table(results))

    nowhere, drifted, skipped = summarise(results)
    print()
    for line in skipped:
        print(f"SKIPPED  {line}")
    for line in drifted:
        print(f"DRIFT    {line}: measured coverage differs from the declared block")
    if nowhere:
        for sid in nowhere:
            print(f"NOWHERE  {sid}: no subject in the window carries a single value")
        print(f"\nFAILED — {len(nowhere)} series resolved nowhere.")
        return 1
    print(
        f"OK — {len(set(_series_order(results)))} series, "
        f"{len(results)} (series, subject) pairs, every series resolved somewhere."
    )
    if args.strict and (drifted or skipped):
        print("STRICT — drift or a keyed skip was reported above.")
        return 2
    return 0


if __name__ == "__main__":       # pragma: no cover - CLI entry point
    raise SystemExit(main())
