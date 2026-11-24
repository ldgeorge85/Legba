# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``analyst_traces_retention`` sub-handler — TTL purge of aged analyst traces.

S-6 (disk-creep remediation). ``analyst_traces`` is the per-run debug/telemetry
receipt table (one row per analyst run: prompt, llm_calls, tool_calls,
output_payload, receipt chain). It had NO retention and grew forever
(~470MB / 164k rows, +5.4k/day), feeding the disk pressure that breached
OpenSearch's watermark. The answer MIRRORED the ``signals_retention``
precedent (migration 0036) so closely that this module's own header used to
say so verbatim — which is exactly why the C2 "one janitor" pass (2026-07-28
coherence audit) folded both onto one engine instead of two copies of the
same TTL-purge shape.

This module is now a thin DELEGATE onto the shared :mod:`._retention_sweep`
engine, which executes the ``analyst_traces_retention`` row of the
``retention_policies`` config table (migration 0109). Behavior is
BYTE-IDENTICAL to the pre-consolidation standalone implementation (git
history has the retired body; the shared engine's ``_purge_traces`` /
``_finding_traces`` are that same code, parameterized) — this is a
REGISTRATION SHIM, not a rewrite:

  * DELETION IS OFF BY DEFAULT (owner decision 2026-11-15: traces are kept
    forever). Even with a positive TTL nothing is deleted unless
    ``LEGBA_ANALYST_TRACES_DELETE_ENABLED`` is truthy; the daily run then
    reports ``deletion=off``.
  * ``ttl_days <= 0`` (the DEFAULT, seeded into the policy row) DISABLES the
    purge — the job ships inert until an operator sets a deliberately
    generous positive TTL.
  * Opt-in levers, highest first: an explicit run ``options["ttl_days"]``
    (forced runs / tests) > the descriptor's own ``method.options.ttl_days``,
    which X-1 made real — a CADENCE fire now carries the descriptor's declared
    options, so this no longer needs a forced run > the
    ``LEGBA_ANALYST_TRACES_TTL_DAYS`` env var (the pre-X-1 path; still honored,
    still the only one costing a container recreate — ff65f78).
  * FK children stay DB-handled (``analyst_critiques.trace_id`` CASCADE,
    ``output_dead_letter.run_id`` SET NULL) — see the shared engine's
    ``_purge_traces`` adapter, which counts the cascade honestly.

CADENCE-HEALTH SAFETY (unchanged): the System Status / telemetry read
(``runtime_telemetry_api``) aggregates a 7-DAY window over ``analyst_traces``,
and the liveness watchdog reads ``max(run_started_at)`` per analyst. Any
operator-set TTL must stay WELL ABOVE 7 days (30+ recommended) — the policy
row's ``description`` column carries this reminder too.

Output ``data`` keys (unchanged):
    traces_purged       int — analyst_traces rows deleted this run
    critiques_cascaded  int — analyst_critiques rows CASCADE-deleted with them
    ttl_days            int — the effective TTL (0 = disabled)

RETIREMENT NOTE: this module stays importable/registered in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS` under the SAME
sub_handler name (``analyst_traces_retention``) so no descriptor/dispatch
change is required — it just no longer carries its own purge SQL.

TRACE BULK TO COMPRESSED OBJECTS - DESIGN, NOT BUILT (SEAMS #trace-bulk-cold)
-----------------------------------------------------------------------------
Owner decision 2026-11-15: keep every trace's fingerprint forever and its bulk
as a compressed file. Deletion is off (above); this is the other half. It was
measured on live and judged too large to land half-built, so it is designed
here and declared in docs/SEAMS.md.

Measured on live (read-only, 2026-11-15): 112,403 rows, 1,988 MB with indexes,
about 1.5 GB of it the six bulk columns (``output_payload`` 687 MB,
``prompt_rendered`` 493 MB, ``llm_calls`` 190 MB, ``intermediate_steps`` 69 MB,
``input_payload`` 54 MB, ``tool_calls`` 4 MB; sizes as Postgres already
compresses them in TOAST). About 2,550 runs a day, so about 45 MB a day, 1.35 GB
a month, 16 GB a year while nothing is deleted. A sample of 3,000 runs older than
14 days: 26 KB of JSON per run, 3.6 KB after zstd level 3, 2.3 KB at level 19
(the archive codec's default is level 3; see ``archive_codec``). That is 9 MB a
day, about 3.3 GB a year (2.1 GB at level 19) against 16 GB in the table, and
about 400 MB for the existing 112k rows against about 1.5 GB now.

The shape:

* One object per run: the canonical JSON of the six bulk columns, zstd-compressed,
  content-addressed by the sha256 of the canonical JSON, in a DEDICATED root
  (``LEGBA_TRACE_ARCHIVE_ROOT``), never the evidence archive: that archive's
  retention sweep, tier mover and orphan checks are about fetched bytes and
  must never see a trace. ``archive_store.EvidenceStore(root).put(body)`` is the
  writer and ``.get(sha)`` the verified reader (it re-hashes on read).
* The row keeps its fingerprint and a pointer and nothing bulky:
  ``receipt_hash``/``prev_receipt_hash`` (the chain; ``prompt_rendered`` feeds
  ``compute_receipt_hash``, so the object is what lets a chain be re-verified),
  ``prompt_sha256`` (the full prompt), new nullable ``bulk_sha256``,
  ``bulk_codec``, ``bulk_bytes``, ``bulk_moved_at``; the six bulk columns become
  NULL or their empty defaults in the same transaction that records the pointer.
* A mover (this handler's sibling, daily, behind ``LEGBA_TRACE_COLD``, default
  off) takes runs older than N days in bounded batches: write the object, read it
  back and re-hash, then UPDATE the row; a failed read-back leaves the row
  untouched. A dry-run script reports the rows, bytes and compression ratio for
  the existing 112k rows before the switch is ever turned on.

Why it is not a contained change (the estimate, one lane, about four days):

* Six readers take the bulk of ANY age and must hydrate from the object:
  ``registry/lineage_api`` (show-the-work, the reason for the whole decision),
  ``registry/layers_api``, ``registry/flow_api``, ``data/_frame_content``,
  ``analysts/optimizer`` and ``deterministic_handlers/_reference_diff``. The
  ``llm_calls`` readers (``consult_turn_cost``, production gauge metering,
  runtime telemetry) read short recent windows, so they stay on the row if the
  mover's age floor is above their window (30 days or more); ``llm_calls`` can
  stay hot for good (190 MB) to make that unconditional.
* The REGISTRY image cannot read an object today: it has no ``zstandard`` and no
  mount of an archive volume (only the runtime image has both). The read path
  needs the dependency in ``docker/Dockerfile.registry`` and a read-only mount of
  the trace root, which is a deploy-topology change, not a code change.
* The trace root is the only copy of that text once the row is emptied, so it
  needs its own backup line in ``scripts/backup.sh`` and a restore proof.
* Tests: a byte-identical read-back of a sample, the chain re-verified from
  objects, every reader on a moved row, and the mover under a crash between the
  write and the UPDATE.

Until it lands, with deletion off, the table grows about 45 MB a day (see the
disk ceiling in the data-layers review, section 7 note C).
"""

from __future__ import annotations

from typing import Any, Mapping

from ....runtime.analyst_method import AnalystMethodResult
from . import _retention_sweep

SUB_HANDLER_NAME = "analyst_traces_retention"


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Delegates to :func:`legba.data.analysts.deterministic_handlers.
    _retention_sweep.handle_policy` with
    ``policy_name="analyst_traces_retention"``. ``deps is None`` (unit path)
    or ``ttl_days <= 0`` (default) yields a zeroed, no-purge run — identical
    to the pre-consolidation behavior.
    """
    return await _retention_sweep.handle_policy(
        SUB_HANDLER_NAME, inputs, options, deps
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
