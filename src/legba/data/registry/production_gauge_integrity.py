# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""INTEGRITY loops for S-1 — three gauges that watch the engine's own honesty.

The four original S-1 loop classes all ask one question: *is this loop
producing?* These three ask a different one: *is what it produces still what we
think it is?* Same machinery, same ``LoopGauge`` contract, same watermark and
paging path — ``read_gauge`` picks them up and everything downstream (route,
totals, ``production_deficit`` trigger, escalation refire, ntfy fan-out) works
with no further wiring.

**judge_availability** — the 26-hour silent outage. On 2026-08-03 the judge
component began returning ``402 payment_required`` and kept doing so for a day
and a night. Every critique written meanwhile carried a floor-only verdict. The
fallback behaved exactly as designed. The result was 611 scored critiques, a
fleet-wide mean faithfulness drop of 0.21, an acceptance panel that could measure
nothing, and **no alarm of any kind**. The fallback is correct; its silence is
not. Analysts kept running, so no production loop noticed — the deficit was in
the GRADER, and nothing in the tower gauged the grader.

**descriptor_prompt_drift** — a bounded unit's system prompt is not in the code
image. It is ``analyst_descriptors.body.method.system_prompt``, a registry DB
row, put there by a human running ``voice_prompt_puts.py``. So the analytic
method of the system is a DB row rather than a tracked file, and a prompt fix can
sit correct-in-tree and wrong-in-production indefinitely. The K-3 audit already
found three registry PUTs perm-blocked and a "reifier phantom prompt pkg
(tree-fixed; live PUT owed)". There is a gauge for loops that do not produce and
a gauge for module size; there was none for "the live prompt is not the tree's".

**descriptor_state_drift** — the same disease one level up. Bulk registration
ships descriptors ``state: draft`` on purpose (activation is the operator's), so
the tree's word for a source that has been running for a month is still "draft".
Measured 2026-08-05: of 157 descriptors present in both tree and registry, 76
disagree, and 68 are ``draft`` in the tree while ``active`` live. A
re-registration from the repo would take sixty-eight running descriptors
OFF-LINE — one ``bringup_register_*.py`` away, with nothing in the tree saying so.

All three are READ-ONLY. They never PUT; they report, and a human decides which
side — the tree or the registry — is the one that is wrong.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .production_gauge import (
    GaugeConfig,
    LoopGauge,
    _ungauged,
    severity_for_ratio,
)

logger = logging.getLogger(__name__)

LOOP_JUDGE_AVAILABILITY = "judge_availability"
LOOP_DESCRIPTOR_PROMPT_DRIFT = "descriptor_prompt_drift"
LOOP_DESCRIPTOR_STATE_DRIFT = "descriptor_state_drift"

INTEGRITY_LOOP_CLASSES: tuple[str, ...] = (
    LOOP_JUDGE_AVAILABILITY,
    LOOP_DESCRIPTOR_PROMPT_DRIFT,
    LOOP_DESCRIPTOR_STATE_DRIFT,
)

# Quiet-by-design reasons these loops add to the S-1 vocabulary.
QUIET_NO_CRITIQUES = "no_critiques_in_window"
QUIET_JUDGE_NOT_CONFIGURED = "judge_never_configured"
QUIET_NO_MANIFEST = "prompt_manifest_unavailable"
QUIET_NO_LIVE_PROMPTS = "no_live_descriptor_prompts"
QUIET_JUDGE_QUERY_FAILED = "judge_query_failed"
QUIET_DRIFT_QUERY_FAILED = "drift_query_failed"
QUIET_NO_LIVE_DESCRIPTORS = "no_live_descriptors"
QUIET_STATE_QUERY_FAILED = "state_drift_query_failed"
#: Live rows existed and the manifest existed, but NOTHING was co-present in both
#: — so zero comparisons were made. That is "we cannot say", not "it is fine", and
#: the S-1 family draws that line hard: ``ungauged`` is never folded into ``ok``,
#: because a single health percentage would hide the difference.
QUIET_NOTHING_COPRESENT = "no_copresent_descriptors"

#: The manifest the R4 comparison is made against — the compiled tree side. See
#: scripts/gen_descriptor_prompt_manifest.py for why the tree itself cannot be
#: read at runtime.
MANIFEST_PATH = Path(__file__).with_name("descriptor_prompts.json")

#: Defensive bound on one drift read.
_MAX_DESCRIPTORS = 500


# ---------------------------------------------------------------------------
# judge_availability
# ---------------------------------------------------------------------------

#: Critiques in the trailing window, split by whether the LLM judge actually
#: graded them. ``judge_status`` lives in the projected verification block; the
#: pre-P2-4 legacy NULL reads as ``deterministic``, which is conservative in the
#: right direction (an unknown grader is not a working one).
_JUDGE_SQL = """
    SELECT
      count(*)::int AS critiques,
      count(*) FILTER (
        WHERE coalesce(
          c.data->'data'->'verification'->>'judge_status', 'deterministic'
        ) = 'llm'
      )::int AS judged,
      count(*) FILTER (
        WHERE (c.data->'data'->'verification'->>'judge_llm_ref') IS NOT NULL
          AND (c.data->'data'->'verification'->>'judge_llm_ref') <> ''
      )::int AS judge_wired,
      max(c.created_at) FILTER (
        WHERE c.data->'data'->'verification'->>'judge_status' = 'llm'
      ) AS last_judged_at
      FROM analyst_outputs c
     WHERE c.kind = 'critique'
       AND c.title LIKE 'Faithfulness verify%'
       AND c.created_at > $1
"""


def judge_availability_gauge(
    row: Mapping[str, Any], *, now: datetime, cfg: GaugeConfig
) -> LoopGauge:
    """Is the LLM judge actually grading?

    The measured quantity is the ADJUDICATED SHARE — critiques with
    ``judge_status='llm'`` over all critiques written in the trailing window. A
    healthy fleet sits near 1.0; the outage sat at exactly 0.0 for 26 hours while
    every other gauge stayed green.

    ``ratio`` is the shortfall expressed in multiples of the tolerance, so it
    rides the SHARED severity ramp (1x medium, 2x high, 4x critical) that every
    other S-1 loop uses. A total outage always lands ``critical`` — which is the
    entire point: the one condition that silently invalidates every faithfulness
    number in the substrate must page at the top of the ladder.
    """
    critiques = int(row.get("critiques") or 0)
    judged = int(row.get("judged") or 0)
    judge_wired = int(row.get("judge_wired") or 0)
    last_judged = row.get("last_judged_at")

    if critiques == 0:
        # No critiques at all is a PRODUCTION deficit and the analyst loops own
        # it. Reporting it here too would double-page one condition.
        return _ungauged(
            LOOP_JUDGE_AVAILABILITY,
            LOOP_JUDGE_AVAILABILITY,
            "LLM judge availability",
            QUIET_NO_CRITIQUES,
            window_days=cfg.window_days,
        )
    if judge_wired == 0 and judged == 0:
        # The floor-only posture is a legitimate CONFIGURATION (no judge
        # component wired), not a failure. Gauged and visible; never paged.
        return _ungauged(
            LOOP_JUDGE_AVAILABILITY,
            LOOP_JUDGE_AVAILABILITY,
            "LLM judge availability",
            QUIET_JUDGE_NOT_CONFIGURED,
            critiques=critiques,
            window_days=cfg.window_days,
        )

    share = judged / float(critiques)
    floor = max(0.0, min(1.0, float(cfg.judge_min_adjudicated_share)))
    shortfall = floor - share
    if shortfall <= 0:
        return LoopGauge(
            loop_class=LOOP_JUDGE_AVAILABILITY,
            loop_id=LOOP_JUDGE_AVAILABILITY,
            label="LLM judge availability",
            state="ok",
            ratio=0.0,
            expected=f"at least {floor:.0%} of critiques adjudicated by the judge",
            actual=f"{judged}/{critiques} adjudicated ({share:.1%})",
            last_production_at=last_judged,
            evidence={
                "critiques": critiques,
                "judged": judged,
                "adjudicated_share": round(share, 4),
                "floor": floor,
                "window_days": cfg.window_days,
            },
        )

    # Shortfall in multiples of the tolerance band beneath the floor. A complete
    # outage (share 0.0) is `floor / tolerance`, which with the defaults is 4.0 —
    # critical, by construction.
    tolerance = max(1e-6, float(cfg.judge_share_tolerance))
    ratio = shortfall / tolerance
    return LoopGauge(
        loop_class=LOOP_JUDGE_AVAILABILITY,
        loop_id=LOOP_JUDGE_AVAILABILITY,
        label="LLM judge availability",
        state="deficit",
        severity=severity_for_ratio(ratio),
        ratio=round(ratio, 3),
        expected=f"at least {floor:.0%} of critiques adjudicated by the judge",
        actual=(
            f"{judged}/{critiques} adjudicated ({share:.1%}) — "
            f"{critiques - judged} critiques carry a floor-only PROVISIONAL "
            f"verdict"
        ),
        last_production_at=last_judged,
        evidence={
            "critiques": critiques,
            "judged": judged,
            "unjudged": critiques - judged,
            "adjudicated_share": round(share, 4),
            "floor": floor,
            "shortfall": round(shortfall, 4),
            "window_days": cfg.window_days,
            "note": (
                "every faithfulness score written while this is open is the "
                "deterministic floor's, not an adjudicated verdict"
            ),
        },
    )


async def read_judge_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    """Degrade LOUD: a failed read is an ``ungauged`` row carrying the error,
    never a silent zero (which would read as a healthy judge)."""
    since = now - timedelta(days=max(1, int(cfg.judge_window_days)))
    try:
        row = await conn.fetchrow(_JUDGE_SQL, since)
    except Exception as exc:  # pragma: no cover — degrade-not-drop
        logger.warning("production_gauge.judge_query_failed err=%s", exc)
        return [
            _ungauged(
                LOOP_JUDGE_AVAILABILITY,
                LOOP_JUDGE_AVAILABILITY,
                "LLM judge availability",
                QUIET_JUDGE_QUERY_FAILED,
                error=str(exc)[:300],
            )
        ]
    return [judge_availability_gauge(row or {}, now=now, cfg=cfg)]


# ---------------------------------------------------------------------------
# descriptor_prompt_drift
# ---------------------------------------------------------------------------

_DRIFT_SQL = """
    SELECT d.descriptor_id,
           d.state,
           d.body->'method'->>'system_prompt' AS system_prompt
      FROM analyst_descriptors d
     WHERE d.is_head
       AND d.state = 'active'
       AND d.body->'method'->>'system_prompt' IS NOT NULL
     LIMIT $1
"""


def _normalized_prompt_hash(text: str) -> str:
    """MUST match ``scripts/gen_descriptor_prompt_manifest.prompt_hash``.

    Trailing per-line whitespace is stripped and the text normalized to one
    trailing newline: YAML block scalars and a JSON round-trip through the
    registry disagree about trailing whitespace in ways no human ever meant as a
    prompt change, and an un-normalized hash would report drift on everything
    forever — which is the same as reporting it on nothing.
    """
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    return hashlib.sha256(
        ("\n".join(lines).strip() + "\n").encode("utf-8")
    ).hexdigest()


def load_prompt_manifest(path: Path | None = None) -> dict[str, Any]:
    """The compiled tree side. ``{}`` when unavailable — the gauge then reports
    ``ungauged``/``prompt_manifest_unavailable`` rather than pretending zero
    drift, because a missing manifest and a matching one are not the same fact."""
    p = path or MANIFEST_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("production_gauge.prompt_manifest_unreadable err=%s", exc)
        return {}
    prompts = data.get("prompts")
    return prompts if isinstance(prompts, dict) else {}


def descriptor_drift_gauge(
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    now: datetime,
    cfg: GaugeConfig,
) -> LoopGauge:
    """Does every active descriptor's LIVE prompt match the tree's?

    Three outcomes per descriptor, and the distinction matters:

    * **match** — nothing to say;
    * **diverged** — the live prompt and the tree's differ. Someone edited one
      and not the other, and which one is authoritative is a human decision;
    * **untracked** — the live descriptor carries an inline prompt the tree has
      no record of at all. Strictly worse than divergence: there is nothing to
      diff against, and the method is unreviewable.

    A descriptor in the manifest but NOT live is deliberately NOT a deficit —
    that is an un-deployed or paused unit, which the cadence loops already own.
    """
    if not manifest:
        return _ungauged(
            LOOP_DESCRIPTOR_PROMPT_DRIFT,
            LOOP_DESCRIPTOR_PROMPT_DRIFT,
            "Descriptor prompt drift (live vs tree)",
            QUIET_NO_MANIFEST,
        )
    if not rows:
        return _ungauged(
            LOOP_DESCRIPTOR_PROMPT_DRIFT,
            LOOP_DESCRIPTOR_PROMPT_DRIFT,
            "Descriptor prompt drift (live vs tree)",
            QUIET_NO_LIVE_PROMPTS,
            tracked=len(manifest),
        )

    diverged: list[str] = []
    untracked: list[str] = []
    matched = 0
    for row in rows:
        desc_id = str(row.get("descriptor_id") or "")
        live = row.get("system_prompt")
        if not desc_id or not isinstance(live, str) or not live.strip():
            continue
        entry = manifest.get(desc_id)
        if not isinstance(entry, Mapping):
            untracked.append(desc_id)
            continue
        if _normalized_prompt_hash(live) == entry.get("sha256"):
            matched += 1
        else:
            diverged.append(desc_id)

    checked = matched + len(diverged) + len(untracked)
    bad = len(diverged) + len(untracked)
    if checked == 0:
        return _ungauged(
            LOOP_DESCRIPTOR_PROMPT_DRIFT,
            LOOP_DESCRIPTOR_PROMPT_DRIFT,
            "Descriptor prompt drift (live vs tree)",
            QUIET_NOTHING_COPRESENT,
            tracked=len(manifest),
        )
    if bad == 0:
        return LoopGauge(
            loop_class=LOOP_DESCRIPTOR_PROMPT_DRIFT,
            loop_id=LOOP_DESCRIPTOR_PROMPT_DRIFT,
            label="Descriptor prompt drift (live vs tree)",
            state="ok",
            ratio=0.0,
            expected=f"all {checked} active inline prompts match the tree",
            actual=f"{matched}/{checked} match",
            evidence={"checked": checked, "matched": matched, "tracked": len(manifest)},
        )

    # One divergence is already worth a look; the ramp escalates with breadth,
    # because a fleet-wide mismatch means a whole PUT run never landed.
    ratio = bad / max(1.0, float(cfg.drift_severity_divisor))
    logger.warning(
        "production_gauge.descriptor_prompt_drift diverged=%s untracked=%s "
        "— the live analytic method does not match the tree",
        diverged, untracked,
    )
    return LoopGauge(
        loop_class=LOOP_DESCRIPTOR_PROMPT_DRIFT,
        loop_id=LOOP_DESCRIPTOR_PROMPT_DRIFT,
        label="Descriptor prompt drift (live vs tree)",
        state="deficit",
        severity=severity_for_ratio(ratio),
        ratio=round(ratio, 3),
        expected=f"all {checked} active inline prompts match the tree",
        actual=(
            f"{len(diverged)} diverged, {len(untracked)} untracked in the tree "
            f"({matched}/{checked} match)"
        ),
        evidence={
            "checked": checked,
            "matched": matched,
            "diverged": sorted(diverged)[:50],
            "untracked": sorted(untracked)[:50],
            "tracked": len(manifest),
            "note": (
                "the live prompt is the analytic method actually running; run "
                "scripts/voice_prompt_puts.py --apply to push the tree's, or "
                "update the tree if the live row is the correct one"
            ),
        },
    )


async def read_descriptor_drift_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    """READ-ONLY (SELECT + a package-data read). Degrades loud."""
    try:
        rows = await conn.fetch(_DRIFT_SQL, _MAX_DESCRIPTORS)
    except Exception as exc:  # pragma: no cover — degrade-not-drop
        logger.warning("production_gauge.drift_query_failed err=%s", exc)
        return [
            _ungauged(
                LOOP_DESCRIPTOR_PROMPT_DRIFT,
                LOOP_DESCRIPTOR_PROMPT_DRIFT,
                "Descriptor prompt drift (live vs tree)",
                QUIET_DRIFT_QUERY_FAILED,
                error=str(exc)[:300],
            )
        ]
    return [
        descriptor_drift_gauge(
            [dict(r) for r in (rows or [])],
            load_prompt_manifest(),
            now=now,
            cfg=cfg,
        )
    ]


# ---------------------------------------------------------------------------
# descriptor_state_drift (R4, extended)
# ---------------------------------------------------------------------------
#
# The sibling defect, and the more dangerous one. Prompt drift means the live
# unit thinks differently from the tree. STATE drift means the live unit EXISTS
# differently from the tree — and because bulk registration ships descriptors
# `state: draft` on purpose ("bulk registration creates NO live actor;
# activation is the operator's"), the tree's word for a source that has been
# running for a month is still "draft".
#
# Measured 2026-08-05 against the live registry: of 157 descriptors present in
# both, 76 disagree, and 68 of those are `draft` in the tree while `active`
# live. A re-registration from the repo would DEACTIVATE sixty-eight running
# descriptors. That is not a hypothetical: it is one `bringup_register_*.py`
# away, and nothing in the tree said so.

#: Live states a descriptor is RUNNING in. A tree state outside this set paired
#: with a live state inside it is the deactivation hazard.
_LIVE_RUNNING = frozenset({"active", "configured", "paused"})

#: OPERATOR POLICY 2026-08-11: tree ``draft`` + live running is the EXPECTED
#: lifecycle, not drift — the tree ships bulk-registered descriptors
#: ``state: draft`` by design and activation is a live act (the FSM route).
#: Those rows are reported as ``expected_promotions`` (so bringup scripts still
#: have the do-not-re-register list) but never page. The remaining hazard set
#: is tree ``retired`` + live running: the tree declaring a running descriptor
#: dead is a genuine disagreement someone must resolve.
_TREE_EXPECTED_PROMOTION = frozenset({"draft"})

#: Tree states that page when the live head is running.
_TREE_INERT = frozenset({"retired"})

_STATE_SQL = """
    SELECT 'analyst' AS family, descriptor_id, state
      FROM analyst_descriptors WHERE is_head
     UNION ALL
    SELECT 'source', descriptor_id, state
      FROM source_descriptors WHERE is_head
     UNION ALL
    SELECT 'action_pack', descriptor_id, state
      FROM action_pack_descriptors WHERE is_head
     LIMIT $1
"""


def load_state_manifest(path: Path | None = None) -> dict[str, Any]:
    """The tree's declared state per ``<family>:<id>``. ``{}`` when unavailable."""
    p = path or MANIFEST_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("production_gauge.state_manifest_unreadable err=%s", exc)
        return {}
    states = data.get("states")
    return states if isinstance(states, dict) else {}


def descriptor_state_gauge(
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    now: datetime,
    cfg: GaugeConfig,
) -> LoopGauge:
    """Does every live head's state match what the tree says it should be?

    Two grades of disagreement, and the loop reports them separately because the
    remediations are opposite:

    * **deactivation hazard** — the tree says ``draft``/``retired`` and the live
      head is running. Re-registering from the repo takes it OFF-line. This is
      the number that matters; it is the one an operator can trip by accident.
    * **ordinary divergence** — any other mismatch (tree ``active``, live
      ``paused``/``retired``). Usually the operator deliberately paused
      something; worth seeing, not worth fearing.

    A descriptor in one and not the other is NOT drift: live-only means a
    registration the tree never carried (real, but a different question), and
    tree-only means an un-deployed file, which is the normal state of a repo.
    """
    if not manifest:
        return _ungauged(
            LOOP_DESCRIPTOR_STATE_DRIFT,
            LOOP_DESCRIPTOR_STATE_DRIFT,
            "Descriptor state drift (live vs tree)",
            QUIET_NO_MANIFEST,
        )
    if not rows:
        return _ungauged(
            LOOP_DESCRIPTOR_STATE_DRIFT,
            LOOP_DESCRIPTOR_STATE_DRIFT,
            "Descriptor state drift (live vs tree)",
            QUIET_NO_LIVE_DESCRIPTORS,
            tracked=len(manifest),
        )

    hazard: list[str] = []
    diverged: list[str] = []
    promotions: list[str] = []
    matched = 0
    for row in rows:
        family = str(row.get("family") or "")
        desc_id = str(row.get("descriptor_id") or "")
        live_state = str(row.get("state") or "").strip()
        if not family or not desc_id or not live_state:
            continue
        entry = manifest.get(f"{family}:{desc_id}")
        if not isinstance(entry, Mapping):
            continue  # live-only: a different question, not this loop's
        tree_state = str(entry.get("state") or "").strip()
        if not tree_state or tree_state == live_state:
            matched += 1
            continue
        label = f"{family}:{desc_id} tree={tree_state} live={live_state}"
        if tree_state in _TREE_EXPECTED_PROMOTION and live_state in _LIVE_RUNNING:
            # Operator policy 2026-08-11: the designed draft→live promotion.
            promotions.append(label)
        elif tree_state in _TREE_INERT and live_state in _LIVE_RUNNING:
            hazard.append(label)
        else:
            diverged.append(label)

    checked = matched + len(hazard) + len(diverged) + len(promotions)
    if checked == 0:
        return _ungauged(
            LOOP_DESCRIPTOR_STATE_DRIFT,
            LOOP_DESCRIPTOR_STATE_DRIFT,
            "Descriptor state drift (live vs tree)",
            QUIET_NOTHING_COPRESENT,
            tracked=len(manifest),
            live_heads=len(rows),
        )
    if not hazard and not diverged:
        return LoopGauge(
            loop_class=LOOP_DESCRIPTOR_STATE_DRIFT,
            loop_id=LOOP_DESCRIPTOR_STATE_DRIFT,
            label="Descriptor state drift (live vs tree)",
            state="ok",
            ratio=0.0,
            expected=f"all {checked} co-present descriptors agree on state",
            actual=(
                f"{matched}/{checked} agree"
                + (f"; {len(promotions)} expected draft→live promotions"
                   if promotions else "")
            ),
            evidence={
                "checked": checked,
                "matched": matched,
                "expected_promotions": sorted(promotions)[:120],
            },
        )

    # The HAZARD count drives severity. Ordinary divergence is reported but does
    # not escalate: an operator pausing a source on purpose must not be able to
    # push this loop to critical and train everyone to ignore it.
    ratio = len(hazard) / max(1.0, float(cfg.state_drift_severity_divisor))
    logger.warning(
        "production_gauge.descriptor_state_drift hazard=%d diverged=%d — %d live "
        "descriptors would be DEACTIVATED by a re-registration from the tree; "
        "sample=%s",
        len(hazard), len(diverged), len(hazard), sorted(hazard)[:5],
    )
    return LoopGauge(
        loop_class=LOOP_DESCRIPTOR_STATE_DRIFT,
        loop_id=LOOP_DESCRIPTOR_STATE_DRIFT,
        label="Descriptor state drift (live vs tree)",
        state="deficit" if hazard else "ok",
        severity=severity_for_ratio(ratio) if hazard else "info",
        ratio=round(ratio, 3),
        expected=f"all {checked} co-present descriptors agree on state",
        actual=(
            f"{len(hazard)} tree-RETIRED but running live, {len(diverged)} "
            f"otherwise diverged, {len(promotions)} expected draft→live "
            f"promotions ({matched}/{checked} agree)"
        ),
        evidence={
            "checked": checked,
            "matched": matched,
            "deactivation_hazard": sorted(hazard)[:80],
            "diverged": sorted(diverged)[:40],
            "expected_promotions": sorted(promotions)[:120],
            "note": (
                "OPERATOR POLICY 2026-08-11: tree draft + live running is the "
                "designed promotion lifecycle and does not page; the "
                "expected_promotions list is still the do-not-re-register set "
                "for bringup_register_* scripts. Tree retired + live running "
                "pages — that disagreement someone must actually resolve."
            ),
        },
    )


async def read_descriptor_state_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    """READ-ONLY. Degrades loud."""
    try:
        rows = await conn.fetch(_STATE_SQL, _MAX_DESCRIPTORS)
    except Exception as exc:  # pragma: no cover — degrade-not-drop
        logger.warning("production_gauge.state_drift_query_failed err=%s", exc)
        return [
            _ungauged(
                LOOP_DESCRIPTOR_STATE_DRIFT,
                LOOP_DESCRIPTOR_STATE_DRIFT,
                "Descriptor state drift (live vs tree)",
                QUIET_STATE_QUERY_FAILED,
                error=str(exc)[:300],
            )
        ]
    return [
        descriptor_state_gauge(
            [dict(r) for r in (rows or [])],
            load_state_manifest(),
            now=now,
            cfg=cfg,
        )
    ]


async def read_integrity_loops(
    conn: Any, *, now: Optional[datetime] = None, cfg: Optional[GaugeConfig] = None
) -> list[LoopGauge]:
    """All three integrity loops, for ``read_gauge``."""
    now = now or datetime.now(tz=timezone.utc)
    cfg = cfg or GaugeConfig()
    loops = await read_judge_loops(conn, now=now, cfg=cfg)
    loops.extend(await read_descriptor_drift_loops(conn, now=now, cfg=cfg))
    loops.extend(await read_descriptor_state_loops(conn, now=now, cfg=cfg))
    return loops


__all__ = [
    "INTEGRITY_LOOP_CLASSES",
    "LOOP_DESCRIPTOR_PROMPT_DRIFT",
    "LOOP_DESCRIPTOR_STATE_DRIFT",
    "LOOP_JUDGE_AVAILABILITY",
    "MANIFEST_PATH",
    "descriptor_drift_gauge",
    "descriptor_state_gauge",
    "judge_availability_gauge",
    "load_prompt_manifest",
    "load_state_manifest",
    "read_descriptor_drift_loops",
    "read_descriptor_state_loops",
    "read_integrity_loops",
    "read_judge_loops",
]
