# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic sampling + strict parsing for the ``standing_auditor``.

Extracted from :mod:`standing_auditor` for the same two reasons
``structural_claims`` was extracted from ``verify``: this is a cohesive unit
with no inbound dependency on the handler, and keeping it here holds the
handler itself well clear of the module-size gate.

WHY THE SAMPLING IS SEEDED OFF THE DATE, not off a random draw. The auditor is
an EVIDENCE-PRODUCING organ: a verdict row has to be re-derivable, and "which
desks did it look at on the 14th?" must be answerable a month later from the
date alone. A ``random.shuffle`` (or any RNG idiom) makes the day's sample
unreproducible, so a disputed CONTRADICTED verdict could never be replayed
against the same slice. :func:`rotate_desks` is therefore a pure function of
(date, desk-key set): the same day + the same live desks always yields the same
rotation, and a desk that enters or leaves the fleet changes the rotation
honestly rather than silently.

ROTATION, NOT SAMPLING-WITH-REPLACEMENT. Picking `k` desks uniformly at random
each day leaves a desk uncovered for a long tail of days by pure luck. A
date-seeded ROTATION over a stably-ordered desk list visits every desk on a
fixed period (``ceil(len(desks) / k)`` days) — which is the property an
external auditor actually wants: bounded worst-case time-since-last-audit per
desk, not a uniform marginal.

PRIORITY IS A PRE-SORT, NOT AN OVERRIDE. The desk order the rotation walks is
sorted by (severity rank desc, delta-interest desc, desk key) so that within
one day's `k` slots the high-severity / just-moved desks come first — but the
rotation offset still advances every day, so a permanently-critical desk can
never monopolize every slot and starve the quiet ones. That is the same
starvation failure the alert plane's per-kind budget cap had to fix.

FULL-WIDTH BRACKETS. The core plane (gpt-oss / Qwen-family) emits CJK
lenticular brackets — ``【3】`` — where the prompt asked for ``[3]``. Every
reader that keys on ``[N]`` must normalize first (the 2026-06-30 trap). The
regex is a LOCAL MIRROR of ``export_api._VARIANT_CITATION_RE`` for the same
reason that module mirrors it rather than importing: the alternative drags a
registry module into an analyst handler, and the two-line regex is the stable
part.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict vocabulary
# ---------------------------------------------------------------------------

#: The three honest outcomes of checking one world-claim against the open web.
#: ``NOT_FOUND`` is deliberately NOT called "unsupported": external retrieval
#: that finds nothing is a statement about the SEARCH, not about the world (the
#: web_access pack's whole empty-is-suspect doctrine). Only a verdict backed by
#: quoted evidence may be SUPPORTED or CONTRADICTED.
VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_CONTRADICTED = "CONTRADICTED"
VERDICT_NOT_FOUND = "NOT_FOUND"
#: The search plane itself was degraded / unverified / unbound on this claim.
#: Distinct from NOT_FOUND: nothing was measured at all, so the claim was not
#: audited and must not be counted as if it had been.
VERDICT_UNCHECKED = "UNCHECKED"

#: ONE NEW VERDICT AT WIDTH, and only one (design §0.5). Deterministic and
#: PRE-SEARCH: the claim has no world truth-maker at all, so no search could
#: decide it and issuing one would produce NOT_FOUND and quietly deflate the
#: headline. ~9% of live claims are this class. Its three sub-classes live in
#: ``_external_audit_claims.UNCHECKABLE_CLASSES``.
#:
#: Everything ELSE the honesty needs is carried by extending the existing
#: ``unchecked_reason`` enum below rather than by minting more verdicts — a
#: verdict vocabulary that grows per failure mode stops being a vocabulary.
UNCHECKABLE_VERDICT = "UNCHECKABLE"

#: Extensions to ``unchecked_reason`` for the width plane. The shipped reasons
#: (``provider_unresolved``, ``degraded_no_results``, ``liveness_unverified``,
#: ``timeout``) are unchanged and still arrive as free text from the search leg.
#: These four are the ones the width plane decides in code.
UNCHECKED_ABSENCE_LIVENESS = "absence_liveness_unverified"
UNCHECKED_BUDGET_EXHAUSTED = "budget_exhausted"
UNCHECKED_OUT_OF_WINDOW = "out_of_window"
#: W-3's span check has not landed (or raised). A decisive verdict without it
#: rests on the grader's own assertion that a page says something, which is
#: exactly what G-2 exists to stop trusting — so it degrades here rather than
#: publishing.
UNCHECKED_SPAN_CHECK_UNAVAILABLE = "span_check_unavailable"
#: F-7, the ToS posture, in the verdict vocabulary. The decisive page's own
#: ``robots.txt`` refused the fetch (or could not be reached, which this plane
#: reads as a refusal — see ``agency/robots.py`` on failing CLOSED), so the span
#: was NEVER FETCHED and the verdict cannot be published. Distinct from
#: ``span_check_unavailable`` on purpose: that one is OUR instrument missing,
#: this one is the publisher's stated wish, and an operator reading a week of
#: UNCHECKED rows must be able to tell "we could not check" from "we were asked
#: not to look".
UNCHECKED_ROBOTS_DISALLOWED = "robots_disallowed"
#: The fetch leg was allowed and still did not return a page — a timeout, an
#: SSRF-guard refusal, a 5xx, a tool failure. Named rather than free text
#: because it is the one UNCHECKED class that a retry could plausibly clear,
#: and an operator sizing that retry needs to count it separately from the two
#: above, neither of which a retry would move.
UNCHECKED_SPAN_FETCH_FAILED = "span_fetch_failed"

#: The width plane's own CODE-DECIDED ``unchecked_reason`` set — the closed half
#: of the column. The search leg's reasons still arrive as free text (they are
#: the provider's own words about its own failure), and ``external_grades``
#: leaves ``unchecked_reason`` an unconstrained ``text`` for exactly that reason
#: (migration 0190:173 — no CHECK, unlike ``verdict`` and ``uncheckable_class``).
#: This tuple is what a consumer strata-splits on and what the requeue script
#: reads, so a new reason lands here or it is not a class anybody can count.
WIDTH_UNCHECKED_REASONS: tuple[str, ...] = (
    UNCHECKED_ABSENCE_LIVENESS,
    UNCHECKED_BUDGET_EXHAUSTED,
    UNCHECKED_OUT_OF_WINDOW,
    UNCHECKED_SPAN_CHECK_UNAVAILABLE,
    UNCHECKED_ROBOTS_DISALLOWED,
    UNCHECKED_SPAN_FETCH_FAILED,
)

VERDICTS: frozenset[str] = frozenset(
    {
        VERDICT_SUPPORTED,
        VERDICT_CONTRADICTED,
        VERDICT_NOT_FOUND,
        VERDICT_UNCHECKED,
    }
)

#: The full width vocabulary — the four above plus UNCHECKABLE. Kept as its own
#: name so :data:`VERDICTS` stays byte-identical for every flag-off reader.
WIDTH_VERDICTS: frozenset[str] = VERDICTS | {UNCHECKABLE_VERDICT}

#: Verdicts that represent a COMPLETED external check (the heartbeat's
#: ``claims_checked`` counts these, never UNCHECKED — an auditor whose search
#: plane is dead must not look busy).
CHECKED_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_NOT_FOUND}
)


# ---------------------------------------------------------------------------
# THE PLANE'S VOCABULARY — the stamps, the flag, the row keys
# ---------------------------------------------------------------------------
#
# These live HERE rather than in the handler for the reason every other
# extraction in this tree gives: they are a cohesive, dependency-free unit that
# three modules now read (the handler, the width tick, the width writes), and
# the handler was 1,492 lines against a 1,500-line gate entry — which is not a
# budget, it is a warning. ``standing_auditor`` imports them ONE WAY and
# re-exports them, so ``standing_auditor.CRITIQUE_TITLE_PREFIX`` and every test
# that reaches for it resolve byte-identically.

#: The ``SUB_HANDLERS`` name the runtime resolves into ``options['sub_handler']``,
#: and the tag every row this plane writes carries.
SUB_HANDLER_NAME = "standing_auditor"

#: This plane's OWN population-split key. Deliberately NOT
#: ``JUDGE_PIPELINE_VERSION``: external-audit verdicts and faithfulness verdicts
#: are different evidence about different questions, and a mean across the two
#: would describe a population that never existed. Bump this — never that — when
#: the prompts, the verdict vocabulary or the validation below change.
#:
#: This is the SHIPPED 6-claim sweep's stamp and it does NOT move. Flag-off
#: behaviour is byte-identical, which means the population it wrote yesterday is
#: the population it writes today, under the same key.
EXTERNAL_AUDIT_PIPELINE_VERSION = "2026-08-29/1"

#: WIDTH's own stamp — a different instrument, therefore a different population.
#: Everything a stamp is supposed to split on changes at once here: a new CLAIM
#: SOURCE (the assembly's own byte-identified spans, replacing an LLM
#: extraction), a new GRADER on a THIRD model family (Gemma-4-31B, not the $0
#: core plane), a new VERDICT (``UNCHECKABLE``) with three deterministic
#: sub-classes, a rubric with four mechanical gates, and a new row shape (one
#: critique per READ instead of one per claim). Pooling a ``2026-08-29/1``
#: verdict with a ``2026-09-05/1`` one would describe an instrument that never
#: existed — the exact discipline ``judge_pipeline_version`` carries for the
#: faithfulness plane, applied to this population from its FIRST row rather than
#: retrofitted onto it later.
#:
#: The verify stamp and ``LEGBA_JUDGE_STACK_REF`` are UNTOUCHED. This instrument
#: has always kept its own stamp family, and the correctness round's freeze on
#: the judge route is not this train's to move.
#:
#: BUMPED 2026-09-06/1 — THE SPAN CHECK NOW ACTUALLY RUNS. From the width deploy
#: (2026-09-05 ~18:00Z) to this bump the drain called W-3's ``check_span`` with
#: keyword arguments it does not accept (``claim_text=/url=/span=`` against a
#: positional ``claim, candidate_url, fetched_text``); every call raised
#: ``TypeError``, the wrapper's except-branch caught it, and EVERY decisive
#: proposal degraded to ``UNCHECKED/span_check_unavailable`` — 168 logged
#: ``external_audit.span_check_raised`` warnings and 211 ledger rows carrying a
#: decisive URL and span that were never checked against the page. Nothing
#: fetched the page at all, so G-1/G-2/G-3 never evaluated once.
#:
#: That is exactly what a stamp is for. A ``2026-09-05/1`` SUPPORTED and a
#: ``2026-09-06/1`` SUPPORTED are verdicts from two different instruments: the
#: first is the grader's unverified assertion that a page says something, the
#: second has had the page fetched (robots-gated), the span matched verbatim
#: under the shared fold, the domain tiered and the publication date anchored in
#: the read's own window. Pooling them would describe an instrument that never
#: existed. The 09-05 rows keep their stamp and stay readable as what they are.
#:
#: The re-grade path is ``scripts/width_requeue_span_check_unavailable.py``: the
#: ledger is APPEND-ONLY, so a re-graded claim lands as a NEW row under this
#: stamp beside the old one (the ledger's unique key is
#: ``(claim_key, grader_pipeline_version, grader_family)``), never as an update.
#:
#: The 2026-09-06 instrument, KEPT: heads-based window basis, zero grace. Rows
#: graded before the 2026-09-07 window work — and rows graded after it under an
#: unflipped configuration — carry this and stay poolable with each other.
#: BUMPED 2026-09-21/1 — THE ABSENCE GATE APPLIES TO AN EMPTY, NOT TO HITS.
#: Under 09-06/1 and 09-07/1 an absence-shaped claim was refused
#: (UNCHECKED/absence_liveness_unverified) whenever ``supports_absence_claim``
#: was false, and that predicate is true only for a liveness-verified EMPTY —
#: so a search that returned HITS refused the claim before the grader saw
#: them, on every rung (24 h measured: 104 of 104 absence claims, both rungs,
#: including every answer the paid serper rung returned). From this stamp,
#: only an unverified empty is refused; hits are graded under the grader's
#: claim-shape rule. That MOVES the absence population from UNCHECKED into
#: NOT_FOUND / CONTRADICTED / SUPPORTED — a different instrument, hence the
#: stamp on BOTH window configurations (…/1 heads-0, …/2 evidence-or-grace).
#: BUMPED 2026-09-21/3 — THE REFORMULATION RIDES THE PAID RUNG. Under /1 and
#: /2 a NOT_FOUND the grader could reformulate spent the ONE second search on
#: rung 0 again, so the declared paid rung was reachable only when the grader
#: offered NO better query — measured live on the 12:30Z tick: 321 searches
#: for 164 claims, zero calls to google.serper.dev. From this stamp the
#: reformulated query is pinned to ``serp_provider_order[1]`` when a paid
#: rung is declared (still exactly one second search, same
#: ``EGRESS_CALLS_PER_CLAIM`` reservation, same spend brake), and a claim's
#: verdict can rest on a different index than it could before — a different
#: instrument, hence the stamp on BOTH window configurations
#: (…/3 heads-0, …/4 evidence-or-grace).
EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS = "2026-09-21/3"

#: 2026-09-07/1 — THE ADMISSIBLE WINDOW IS NO LONGER THE HEADS' OWN SPREAD.
#: Two knobs landed against G-3 and either one, once set, makes a DIFFERENT
#: instrument than ``2026-09-06/1``:
#:
#: * ``window_basis = "evidence"`` (``_external_audit_width.WINDOW_BASIS_ENV`` /
#:   the ``window_basis`` handler option). ``read_evidence_window.oldest`` stops
#:   being the oldest consumed HEAD's ``produced_at`` and becomes the oldest
#:   ``signals.fetched_at`` under those heads' own ``derived_from`` lineage.
#:   Measured 2026-09-06 on the live ledger: the 12:00Z world read's stamped
#:   window was 3.19h wide (08:30Z-11:41Z, 33 heads) while the evidence those
#:   heads actually rest on reaches back ~13 days. The stamped window was never
#:   the read's admissible source window; it was the arrival spread of the
#:   summaries. R3 §4.7's complaint — *"every declared span is 1-2 days inside a
#:   read graded and consumed as a 14-day country read"* — is the same fact
#:   said from the other side.
#: * ``window_grace_hours > 0`` (``WINDOW_GRACE_HOURS_ENV`` / the
#:   ``window_grace_hours`` handler option, landed 2026-09-06 at default 0).
#:
#: WHY THIS STAMP IS A FUNCTION OF THE CONFIGURATION and not a constant. An
#: instrument stamp names THE MEASUREMENT TAKEN, not the code that could have
#: taken it. Every other stamp in this file is constant because the code IS the
#: instrument; here the operator's ``.env`` is part of the instrument, so a
#: constant would put one label on two measurements. Pinning it high would
#: relabel every heads/0 tick as something it is not (and orphan the 09-06 rows
#: it must pool with); pinning it low would hide the flip entirely, which is the
#: failure the 09-05 -> 09-06 lineage entry above exists to prevent. Deriving it
#: keeps the promise the constant was making: rows that share a stamp were
#: graded by the same instrument, and rows that do not, were not. The
#: configuration itself rides the receipt (``width_heartbeat_block``'s
#: ``window_basis`` / ``window_grace_hours``) so a reader can recover WHICH
#: non-default configuration a ``2026-09-07/1`` row was taken under.
EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH = "2026-09-21/4"   # was 2026-09-21/2 — see the 2026-09-21/3 entry above

#: G-3's window BASIS vocabulary — closed, because a third spelling reaching the
#: stamp selector would silently grade as "not heads" and stamp 09-07/1.
#: ``heads`` (today's default): ``oldest`` is the oldest consumed head's
#: ``produced_at``, exactly as ``composition_window.evidence_window_span``
#: stamped it. ``evidence``: ``oldest`` is the oldest ``signals.fetched_at``
#: reached by walking those heads' ``derived_from`` lineage down to signals.
#: The constants live HERE rather than in ``_external_audit_width`` because the
#: stamp keys on them and this module is that module's import ancestor.
WINDOW_BASIS_HEADS = "heads"
WINDOW_BASIS_EVIDENCE = "evidence"
WINDOW_BASES: tuple[str, ...] = (WINDOW_BASIS_HEADS, WINDOW_BASIS_EVIDENCE)


def width_pipeline_version(
    *,
    window_basis: str = WINDOW_BASIS_HEADS,
    grace_before_hours: float = 0.0,
) -> str:
    """The WIDTH stamp for the window configuration actually in force.

    :data:`EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS` for heads/0 — the
    instrument that has been running since the span-check repair, so today's
    rows stay comparable with yesterday's.
    :data:`EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH` the moment either knob
    leaves its default, because a verdict admitted only because the window
    was widened is not the same measurement as one admitted inside the
    narrow window.

    An UNRECOGNISED basis is treated as non-default (it stamps the WIDTH
    constant) rather
    than falling back to ``heads``: the resolver upstream already refuses a
    typo and keeps the predecessor, so anything arriving here that is not
    ``heads`` got here deliberately, and mislabelling a widened measurement as
    the narrow one is the failure mode that costs truth.
    """
    try:
        grace = float(grace_before_hours)
    except (TypeError, ValueError):
        grace = 0.0
    if window_basis == WINDOW_BASIS_HEADS and grace <= 0:
        return EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS
    return EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH


#: THE GLOBAL WIDTH FLAG. Default OFF. Off ⇒ ``standing_auditor`` runs its
#: shipped 6-claim sweep, byte-identical, pinned field-for-field by
#: ``test_width_flag_off_is_the_shipped_sweep``. Nothing outside this analyst
#: reads the new table or the new API block, so flag-off is a strict no-op
#: fleet-wide: no ``external_grades`` row is written, no grader is called, no
#: queue row is created, and the heartbeat carries exactly the keys it carried
#: yesterday.
WIDTH_FLAG_ENV = "LEGBA_EXTERNAL_GRADING_WIDTH"


def width_enabled() -> bool:
    """Is external grading at WIDTH switched on for this deployment?

    Read per run rather than captured at import, so an operator flipping the
    variable and recreating the runtime gets the new behaviour on the actor's
    next tick — and so a test can exercise both legs in one process without
    reloading the module.
    """
    return os.getenv(WIDTH_FLAG_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def pipeline_version(
    *,
    window_basis: str = WINDOW_BASIS_HEADS,
    grace_before_hours: float = 0.0,
) -> str:
    """The stamp this run's rows carry — the shipped one, or width's own.

    Two stamps rather than one bumped stamp, because the flag is a real A/B: a
    deployment with the flag off is running the 2026-08-29 instrument and its
    rows must keep saying so.

    With the flag ON the answer depends on the WINDOW CONFIGURATION, which the
    caller resolves once per tick (``_external_audit_width.resolve_window_config``)
    and passes here — see :func:`width_pipeline_version`. The defaults are
    today's configuration, so every existing zero-argument call site keeps
    returning exactly what it returned before.
    """
    if not width_enabled():
        return EXTERNAL_AUDIT_PIPELINE_VERSION
    return width_pipeline_version(
        window_basis=window_basis, grace_before_hours=grace_before_hours
    )


#: The critique's ``data`` sub-key + the marker every consumer reads.
EXTERNAL_AUDIT_DATA_KEY = "external_audit"

#: Title prefix for every critique this plane writes. MUST NOT collide with
#: ``'Faithfulness verify%'`` — that LIKE pin is what keeps the verify surface
#: from ever reading one of these rows as a faithfulness verdict.
CRITIQUE_TITLE_PREFIX = "External audit"

#: The ``alert_trigger_watermarks`` (mig 0091) partition this plane owns, and
#: the ``trigger_class`` its alert rows carry.
ALERT_TRIGGER_CLASS = "external_audit"
#: The single heartbeat row's key inside that partition.
HEARTBEAT_KEY = "_heartbeat"

# ---------------------------------------------------------------------------
# Full-width bracket normalization (the 2026-06-30 trap)
# ---------------------------------------------------------------------------

#: ``【3】`` / ``［3］`` / ``〔3〕`` / ``〖3〗`` wrapping a bare integer.
_VARIANT_CITATION_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")
#: A fenced ```json … ``` block the model wrapped its "strict JSON" in anyway.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def normalize_core_plane_text(text: str) -> str:
    """Normalize core-plane citation brackets to ASCII ``[N]``.

    Applied to EVERY string that leaves the model before anything keys on it —
    the claim text, the quoted evidence, the rationale. Cheap, idempotent, and
    the one thing standing between a ``【2】`` and a citation index that silently
    resolves to nothing.
    """
    if not text:
        return text
    return _VARIANT_CITATION_RE.sub(lambda m: f"[{m.group(1).strip()}]", text)


def strip_json_fence(text: str) -> str:
    """Unwrap a ```json fence the model added despite the STRICT-JSON rule."""
    if not text:
        return text
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text.strip()


def parse_strict_json_object(content: str) -> dict[str, Any] | None:
    """Parse a model reply that was asked for ONE strict JSON object.

    Returns ``None`` (never raises) when the reply is unparsable — the caller's
    degrade-not-break path treats that exactly like a timeout. Normalizes
    full-width brackets FIRST, then unwraps a fence, then falls back to the
    outermost ``{…}`` span (models occasionally prepend a sentence despite the
    instruction; salvaging the object is honest, guessing its contents is not).
    """
    if not content:
        return None
    text = strip_json_fence(normalize_core_plane_text(content))
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Severity / delta ranking (the pre-sort)
# ---------------------------------------------------------------------------

#: Standing-severity ladder, mirroring ``provenance.models._SEVERITY_RANK``.
_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "moderate": 1,
    "elevated": 2,
    "high": 3,
    "critical": 4,
}

#: How INTERESTING a severity_delta makes a head to an external auditor. A desk
#: that just MOVED is the one whose world-claims are freshest and least
#: corroborated, so ``rose``/``new`` outrank ``fell``, which outranks a desk
#: that only reports ``steady``. ``None`` (the tag was never written — an
#: honest state, never to be papered over as ``steady``) ranks BELOW steady:
#: we know less about it, but we also cannot claim it moved.
_DELTA_INTEREST: dict[str, int] = {
    "rose": 3,
    "new": 3,
    "fell": 2,
    "steady": 1,
}


def severity_rank(level: str | None) -> int:
    """Rank of a ``severity:<level>`` value; ``-1`` when absent/unknown."""
    if not level:
        return -1
    return _SEVERITY_RANK.get(str(level).strip().lower(), -1)


def delta_interest(delta: str | None) -> int:
    """How much a ``severity_delta`` raises audit priority; ``0`` when absent."""
    if not delta:
        return 0
    return _DELTA_INTEREST.get(str(delta).strip().lower(), 0)


def is_high_severity(level: str | None) -> bool:
    """True for ``high`` / ``critical`` — the alert-worthy standing band."""
    return severity_rank(level) >= _SEVERITY_RANK["high"]


# ---------------------------------------------------------------------------
# The sampled head
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SampledHead:
    """One top-layer read the run selected for audit.

    ``desk_key`` is the rotation identity: the target id for a desk read, the
    literal ``"world"`` for the target-less world read (mirroring
    ``production_gauge_staleness._desk_label``, so an operator reading both
    surfaces sees the same key).
    """

    output_id: Any
    analyst_id: str
    target_id: str | None
    desk_key: str
    title: str
    body: str
    severity: str | None
    severity_delta: str | None
    produced_at: Any = None
    #: Non-empty only for the world read, so the receipt can say WHY a head was
    #: taken outside the rotation.
    always_sampled_reason: str = ""

    @property
    def priority(self) -> tuple[int, int, str]:
        """Pre-sort key — higher severity, then a bigger move, then stable."""
        return (severity_rank(self.severity), delta_interest(self.severity_delta),
                self.desk_key)


def head_from_row(row: Mapping[str, Any], *, world: bool = False) -> SampledHead:
    """Build a :class:`SampledHead` from an ``analyst_outputs`` row.

    ``row['data']`` is the whole payload dump (the ``analyst_outputs`` contract),
    so the severity tags live at ``data['tags']``. A row whose ``data`` arrived
    as a JSON string (some drivers) is decoded; anything unreadable degrades to
    "no tags", which ranks the head LOW rather than crashing the run.
    """
    from ...provenance.models import severity_delta_from_tags, severity_from_tags

    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, Mapping):
        data = {}
    tags = data.get("tags") or []
    target_id = row.get("target_id")
    return SampledHead(
        output_id=row.get("id"),
        analyst_id=str(row.get("analyst_id") or ""),
        target_id=target_id,
        desk_key="world" if world or not target_id else str(target_id),
        title=normalize_core_plane_text(str(row.get("title") or "")),
        body=normalize_core_plane_text(str(row.get("body") or "")),
        severity=severity_from_tags(tags),
        severity_delta=severity_delta_from_tags(tags),
        produced_at=row.get("produced_at"),
        always_sampled_reason="world read — audited every run" if world else "",
    )


# ---------------------------------------------------------------------------
# The deterministic rotation
# ---------------------------------------------------------------------------


def rotation_phase(desk_keys: Sequence[str]) -> int:
    """Where in the desk list this fleet's rotation cycle STARTS.

    SHA-256 over the SORTED desk keys — the same no-RNG construction the
    verify-path judge sampler uses to be replayable. Sorting means the phase
    depends on WHICH desks exist, not on the order a query happened to return
    them.

    Deliberately NOT a function of the date. The date's contribution to the
    offset is the STEPPING term in :func:`rotate_desks`
    (``day_ordinal * take``); folding the date in here too would make the phase
    re-randomize daily and destroy the very stepping the bound depends on — the
    defect the first version of this function shipped with, and the reason
    ``test_rotation_advances_by_one_window_per_day`` exists.
    """
    material = "|".join(sorted(desk_keys))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _day_ordinal(date_key: str) -> int:
    """The date's proleptic-Gregorian ordinal — the term that makes the rotation
    STEP. An unparsable key degrades to 0: still deterministic, still replayable,
    just phase-locked (and the caller only ever passes an ISO date)."""
    try:
        return date.fromisoformat(date_key).toordinal()
    except (TypeError, ValueError):
        return 0


def rotate_desks(
    heads: Sequence[SampledHead], *, date_key: str, take: int
) -> list[SampledHead]:
    """The day's desk selection — a stepping, date-seeded rotation.

    The offset is ``(phase + day * take) % n``, where ``phase`` is the
    :func:`rotation_phase` hash over the DESK SET and ``day`` is the date's
    ordinal. BOTH terms are load-bearing and they do different jobs:

      * ``day * take`` makes consecutive days advance by exactly one window, so
        every desk is visited within ``ceil(n / take)`` days. That BOUND is the
        property an external auditor needs — a hashed offset alone gives a
        pseudorandom window position, under which a desk can go uncovered for a
        long tail of days by pure luck, and worst-case time-since-last-audit is
        unbounded.
      * the hash ``phase`` decides WHERE in the desk list the cycle starts, and
        re-derives whenever the fleet's desk set changes, so the schedule is not
        a trivially-predictable "always desks 1-3 on the 1st" — while staying a
        pure function of (date, desk set), replayable a month later from the
        date alone.

    ``take <= 0`` selects nothing. ``take`` at or above the desk count returns
    every desk in pre-sorted order: the rotation is meaningless there, and a
    small fleet's receipt should read in priority order rather than at an
    arbitrary offset.
    """
    if take <= 0 or not heads:
        return []
    ordered = sorted(heads, key=lambda h: (-h.priority[0], -h.priority[1], h.desk_key))
    n = len(ordered)
    if take >= n:
        return list(ordered)
    phase = rotation_phase([h.desk_key for h in ordered])
    offset = (phase + _day_ordinal(date_key) * take) % n
    rotated = ordered[offset:] + ordered[:offset]
    return rotated[:take]


# ---------------------------------------------------------------------------
# Extracted claims + verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckableClaim:
    """One world-claim lifted out of a top-layer read, with its search query."""

    claim: str
    query: str
    head: SampledHead

    @property
    def claim_key(self) -> str:
        """A stable id for this (head, claim) pair — the audit's dedup handle."""
        material = f"{self.head.output_id}|{self.claim}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class ClaimVerdict:
    """The audited outcome for one claim."""

    claim: CheckableClaim
    verdict: str
    rationale: str = ""
    quotes: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    #: Whatever the search plane said about itself — carried verbatim onto the
    #: critique so a NOT_FOUND is always readable next to the search's own
    #: honesty fields (status / degraded / liveness / supports_absence_claim).
    search_status: dict[str, Any] = field(default_factory=dict)
    #: Set when the verdict is UNCHECKED — the tool error or the deferral that
    #: stopped the check. Never empty for UNCHECKED.
    unchecked_reason: str = ""
    #: WHICH model rendered this verdict, off the response's own usage record —
    #: the same provenance discipline ``judge_llm_ref`` carries on a
    #: faithfulness critique. It survives a core-plane model swap, so a later
    #: audit of the audit can split its population by grader instead of
    #: assuming one. ``""`` when the response carried no model id.
    judge_model: str = ""

    @property
    def alertable(self) -> bool:
        """A CONTRADICTED verdict on a high/critical standing severity."""
        return (
            self.verdict == VERDICT_CONTRADICTED
            and is_high_severity(self.claim.head.severity)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_key": self.claim.claim_key,
            "claim": self.claim.claim,
            "query": self.claim.query,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "quotes": list(self.quotes),
            "source_urls": list(self.source_urls),
            "desk_key": self.claim.head.desk_key,
            "analyst_id": self.claim.head.analyst_id,
            "audited_output_id": str(self.claim.head.output_id),
            "severity": self.claim.head.severity,
            "severity_delta": self.claim.head.severity_delta,
            "judge_model": self.judge_model,
            "search": dict(self.search_status),
            **({"unchecked_reason": self.unchecked_reason}
               if self.unchecked_reason else {}),
        }


def parse_claims_reply(
    content: str, head: SampledHead, *, cap: int
) -> list[CheckableClaim]:
    """Parse the extraction call's reply into at most ``cap`` claims.

    Drops silently-malformed entries rather than inventing a query for them: an
    auditor that searches for a string the model never produced is auditing
    nothing. An entirely unparsable reply yields ``[]`` and the caller records
    the head as yielding no checkable claim — an honest, countable outcome.
    """
    parsed = parse_strict_json_object(content)
    if parsed is None:
        return []
    raw = parsed.get("claims")
    if not isinstance(raw, list):
        return []
    out: list[CheckableClaim] = []
    for entry in raw:
        if len(out) >= cap:
            break
        if not isinstance(entry, Mapping):
            continue
        claim = normalize_core_plane_text(str(entry.get("claim") or "")).strip()
        query = normalize_core_plane_text(str(entry.get("query") or "")).strip()
        if not claim or not query:
            continue
        out.append(
            CheckableClaim(claim=claim[:2000], query=query[:400], head=head)
        )
    return out


def parse_verdict_reply(
    content: str, claim: CheckableClaim, *, allowed_urls: Sequence[str]
) -> ClaimVerdict:
    """Parse the judge call's reply into one :class:`ClaimVerdict`.

    Two hard rules enforced HERE rather than trusted to the prompt:

      * an out-of-vocabulary (or absent) verdict becomes ``NOT_FOUND``, never a
        guess at what the model meant — the audit's whole value is that the
        three verdicts mean exactly what they say;
      * a cited URL that was NOT in the search results is DROPPED. A judge that
        invents a source URL is the precise failure this analyst exists to
        catch in others, and it must not be able to commit it itself. A
        SUPPORTED or CONTRADICTED verdict left with no surviving URL is demoted
        to ``NOT_FOUND`` — an unsourced verdict is not a verdict.
    """
    parsed = parse_strict_json_object(content)
    if parsed is None:
        return ClaimVerdict(
            claim=claim,
            verdict=VERDICT_NOT_FOUND,
            rationale="judge reply was unparsable",
        )
    verdict = str(parsed.get("verdict") or "").strip().upper()
    if verdict not in CHECKED_VERDICTS:
        verdict = VERDICT_NOT_FOUND
    rationale = normalize_core_plane_text(
        str(parsed.get("rationale") or "")
    ).strip()[:4000]

    allow = {str(u) for u in allowed_urls if u}
    quotes: list[str] = []
    urls: list[str] = []
    raw_ev = parsed.get("evidence")
    if isinstance(raw_ev, list):
        for entry in raw_ev:
            if not isinstance(entry, Mapping):
                continue
            url = str(entry.get("url") or "").strip()
            quote = normalize_core_plane_text(
                str(entry.get("quote") or "")
            ).strip()
            if url and url not in allow:
                logger.warning(
                    "standing_auditor.fabricated_url claim=%s url=%s — dropped "
                    "(not in the search results this judge was shown)",
                    claim.claim_key, url,
                )
                continue
            if url:
                urls.append(url)
            if quote:
                quotes.append(quote[:1000])

    if verdict in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED) and not urls:
        logger.warning(
            "standing_auditor.unsourced_verdict claim=%s verdict=%s — demoted "
            "to NOT_FOUND (no surviving source URL)",
            claim.claim_key, verdict,
        )
        verdict = VERDICT_NOT_FOUND
        rationale = (
            f"{rationale} [demoted: the judge returned {parsed.get('verdict')!r} "
            "with no source URL from the search results]"
        ).strip()

    return ClaimVerdict(
        claim=claim,
        verdict=verdict,
        rationale=rationale,
        quotes=quotes[:5],
        source_urls=urls[:5],
    )


__all__ = [
    "ALERT_TRIGGER_CLASS",
    "CHECKED_VERDICTS",
    "CRITIQUE_TITLE_PREFIX",
    "EXTERNAL_AUDIT_DATA_KEY",
    "EXTERNAL_AUDIT_PIPELINE_VERSION",
    "EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH",
    "EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS",
    "HEARTBEAT_KEY",
    "SUB_HANDLER_NAME",
    "WIDTH_FLAG_ENV",
    "WINDOW_BASES",
    "WINDOW_BASIS_EVIDENCE",
    "WINDOW_BASIS_HEADS",
    "CheckableClaim",
    "ClaimVerdict",
    "SampledHead",
    "UNCHECKABLE_VERDICT",
    "UNCHECKED_ABSENCE_LIVENESS",
    "UNCHECKED_BUDGET_EXHAUSTED",
    "UNCHECKED_OUT_OF_WINDOW",
    "UNCHECKED_ROBOTS_DISALLOWED",
    "UNCHECKED_SPAN_CHECK_UNAVAILABLE",
    "UNCHECKED_SPAN_FETCH_FAILED",
    "VERDICTS",
    "WIDTH_UNCHECKED_REASONS",
    "VERDICT_CONTRADICTED",
    "VERDICT_NOT_FOUND",
    "VERDICT_SUPPORTED",
    "VERDICT_UNCHECKED",
    "WIDTH_VERDICTS",
    "delta_interest",
    "head_from_row",
    "is_high_severity",
    "normalize_core_plane_text",
    "parse_claims_reply",
    "parse_strict_json_object",
    "parse_verdict_reply",
    "rotate_desks",
    "rotation_phase",
    "severity_rank",
    "pipeline_version",
    "strip_json_fence",
    "width_enabled",
    "width_pipeline_version",
]
