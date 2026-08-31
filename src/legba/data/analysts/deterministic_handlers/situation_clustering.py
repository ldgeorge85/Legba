# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Situation clustering — materialize ``situations`` rows from clustered findings.

The :mod:`finding_supersession` analyst stamps a ``situation_signature`` on
every finding that belongs to a multi-member cluster (the latest live finding
plus the ones it supersedes). This handler reads those stamped findings, groups
them by signature, and UPSERTS one row in the ``situations`` table per
signature — the durable, queryable "situation" object that the ``/situations``
read API, the recursive lineage walk, and the STIX incident producer all
already consume but that nothing previously WROTE (the situations table sat at 0
rows: a built-but-unproduced write-path, the last dark leg of the analysis
plane).

Pipeline position
-----------------
Runs AFTER finding_supersession on the deterministic cadence: supersession
derives + stamps the signatures, this materializes them into situations. Like
supersession (which writes its ``finding_supersessions`` link rows directly and
returns a single summary FindingPayload), this handler does its OWN situation
writes — the deterministic dispatcher persists exactly one ``analyst_output``
per run, so a per-cluster fan-out of situation rows is written here directly and
the returned FindingPayload is the run summary.

Idempotency
-----------
A situation is keyed by ``(situation_signature, analyst_id)``. A re-run UPDATES
the existing row (event_count / last_event_at / intensity / derived_from)
instead of inserting a duplicate. NEVER deletes a situation.

The mega-frame split (#64)
--------------------------
That ``analyst_id`` is the CLUSTERING handler's — one value fleet-wide — so the
frame's identity is carried entirely by the signature. While the signature was
topic-only, a country desk's seven dimensions all wrote into ONE row: at the H1
census the AR frame held 364 members, 42 of them the maritime-pilots story, and
every desk fleet-wide had exactly one open frame. The signature now carries the
producing dimension (``finding_supersession.with_dimension``), so a desk
materializes one frame per dimension instead of one per country.
:func:`_group_by_signature` normalizes members of any vintage onto the new key,
which is what makes the repair hold from the moment the code lands; migration
0188 splits the frames already stored under the old one.

The forgetting curve (H1)
-------------------------
Two clocks decide what a frame is worth. The MEMBER clock — the ``produced_at``
of the desk reads clustered into it — is wound by the product every time a desk
looks, including when it looks and sees nothing, so on its own it measures desk
cadence rather than the world. The CORROBORATION clock is wound only by a
SIGNIFICANT trajectory delta, which cannot be written without cited evidence.
``intensity_score`` is the member-recency sum SCALED by an exponential decay on
the corroboration clock, whose half-life is keyed to the frame's evidence
density; ``status`` is demoted (never promoted, never auto-closed) to
``dormant`` once corroboration is older than the desk's own slice horizon; and a
frame the trajectory ledger has CLOSED on a resolution-grounded de-escalation
now closes here too. See the block comment above the tunables for the DB-verified
defect this repairs. A frame the ledger has never moved keeps its pre-H1
behaviour exactly.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp, log, sqrt
from typing import Any, Mapping
from uuid import UUID, uuid4

from ...provenance.models import _SEVERITY_RANK
from ...provenance.models import FindingPayload
from ...situations.trajectory import (
    DORMANCY_DAYS as _TRAJECTORY_DORMANCY_DAYS,
    STATE_CLOSED as _TRAJECTORY_CLOSED,
    Corroboration,
    evidence_anchor as _shared_evidence_anchor,
    read_corroboration,
    read_current_states,
)
from ....runtime.analyst_method import AnalystMethodResult
from ....runtime.grounding import is_non_event_situation_name
from .finding_supersession import (
    _COMPOSITION_ANALYST_IDS,
    signature_dimension as _signature_dimension,
    strip_dimension as _strip_dimension,
    with_dimension as _with_dimension,
)

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "situation_clustering"
_DEFAULT_LOOKBACK_DAYS = 30
_MAX_MEMBERS = 500
_SITUATION_SCHEMA_URI = "iglu:legba/situation/jsonschema/2-0-0"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a DB)
# ---------------------------------------------------------------------------


def _topic_from_signature(sig: str) -> str:
    """Recover the topic/category from a finding_supersession signature.

    ``sig:<topic>|<entities>#dim:<dimension>`` → ``<topic>``;
    ``sit:<explicit-id>`` → ``""``.

    The ``#dim:`` tail (#64) is stripped FIRST and deliberately: ``category`` is
    what :func:`_target_for_category` resolves ``situations.target_id`` from, so
    a dimensioned signature whose topic came back as
    ``country_g20_ar#dim:internal_stability`` would silently stop matching the
    ``startswith("country")`` test and every split frame would lose its country
    home — and with it its place in that desk's grounding read.
    """
    if sig.startswith("sig:"):
        return _strip_dimension(sig)[4:].split("|", 1)[0].strip()
    return ""


def _dimension_from_signature(sig: str) -> str | None:
    """The producing dimension a #64 signature carries, or ``None`` (pre-#64 /
    explicit key). Stamped into the situation payload so a reader can tell WHICH
    of a desk's questions a frame answers without re-parsing the key."""
    return _signature_dimension(sig)


def _target_for_category(category: str, fallback: str | None) -> str | None:
    """Derive the owning target_id for a situation from its topic category.

    A per-country situation's category IS the country target slug
    (``country_g20_us``) — so populate ``situations.target_id`` with it (review
    follow-up: scope situation grounding on a real target_id, not the
    ``category==slug`` coincidence; a future THEMATIC situation then has a
    distinct target_id and never leaks into a country assessor's grounding).
    A non-country topic has no country home → fall back to the run's target_id
    (usually None for this meta analyst)."""
    if isinstance(category, str) and category.startswith("country"):
        return category
    return fallback


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic latest member: newest produced_at, then largest id."""
    def _key(r: dict[str, Any]) -> tuple[str, str]:
        # Coerce produced_at to a string so a str/NULL value never collides with
        # datetime rows under `<` (the heterogeneous-key TypeError). "" sorts
        # oldest, so a missing timestamp is never chosen as the max.
        v = r.get("produced_at")
        if v is None:
            pa = ""
        elif isinstance(v, str):
            pa = v
        else:
            iso = getattr(v, "isoformat", None)
            pa = iso() if callable(iso) else str(v)
        return (pa, str(r.get("id")))

    return max(rows, key=_key)


# DQ P6 — reject a situation NAME that is a REPORT-SNAPSHOT receipt rather than a
# frame label: a dated world snapshot ("World situational assessment — 2026-06-30")
# or a leaked JSON-envelope fragment ('"title": "World situational assessment …",'
# — the #125 parse-fallback class). A frame is a durable object; a dated snapshot
# is a point-in-time receipt whose name churns every run and pollutes /situations.
_SNAPSHOT_NAME_RE = re.compile(
    r"[-–—]\s*\d{4}-\d{2}-\d{2}\b"        # "... — YYYY-MM-DD"
    r"|^\s*[\"']?title[\"']?\s*:",        # leaked JSON '"title":' fragment
    re.IGNORECASE,
)


def _is_report_snapshot_name(title: Any) -> bool:
    """True for a dated-snapshot / leaked-JSON title that must not name a frame."""
    return isinstance(title, str) and _SNAPSHOT_NAME_RE.search(title) is not None


# FRAME-2 §2.3.2 — THE FRAME-NAMING REPAIR.
#
# THE DEFECT (DB diagnosis, read-only 2026-08-20): naming a frame after its
# LATEST member means whichever read happened to land last owns the label — and
# because a desk's units emit an absence read whenever they look and see nothing,
# the label that landed last was frequently a NEGATIVE claim. At the round's T0:
# AR = "Argentina – No Coercive Economic Measures Observed…", BF = "Burkina Faso
# – No observable shift in standing military posture", CD = "DRC – No discernible
# shift in standing military posture". The register's principal rendered content
# per desk was therefore a statement that nothing was happening, shown to every
# unit and every composition on that desk, under a clause that forbids describing
# a frame beyond "its own name and status" — so the model could not look past the
# name even when the frame's own members contradicted it.
#
# THE FIX ranks candidates on what a LABEL is for, in this order:
#   1. USABLE AT ALL — a dated-snapshot / leaked-JSON title (the DQ P6 #125
#      class) can never name a frame, whatever its severity.
#   2. POSITIVE OVER ABSENCE — a title that asserts something outranks one that
#      asserts nothing, using the SHARED ``is_non_event_situation_name``
#      predicate (the same one the grounding read filters on and the same one
#      ``_situation_fields`` stamps ``steady_state`` from, so a frame that gains
#      a positive name also stops being marked steady-state — one predicate,
#      three consumers, no drift).
#   3. SEVERITY — among titles that assert something, the desk's own worst
#      assessment names the frame.
#   4. RECENCY — the previous rule, kept as the tiebreak, so nothing about a
#      frame with one severity level changes.
#
# NOT A STORED-STATE MIGRATION. ``situations.name`` is re-derived and re-UPSERTed
# by this handler every run (``name=EXCLUDED.name``), so the fleet's existing
# frames re-label themselves on the next cadence tick with no backfill.

def _name_rank(row: dict[str, Any]) -> tuple[int, int, int, str]:
    """Sort key for :func:`_situation_name`; the MAX of these names the frame.

    ``(usable, asserts_something, severity, produced_at+id)``:

    * **usable** — 0 for a dated-snapshot / leaked-JSON title (DQ P6), which may
      never name a frame whatever else it has going for it; 1 otherwise. The
      caller checks this leg to decide whether to fall back to the topic slug.
    * **asserts_something** — a title the shared ``is_non_event_situation_name``
      predicate calls a non-event ("… – No observable shift in standing military
      posture", the live BF and CD frame names at T0) ranks BELOW every title
      that asserts something, across the frame's WHOLE member set. A frame that
      has ever asserted something is better labelled by that, however quiet it
      has since gone: the register prints ``status`` and ``last_event_at`` right
      beside the name, so a dormant frame named for its real event is honest AND
      informative, while one named for whichever absence read landed last
      actively reinforces "nothing happening" at compose time.
    * **severity** — the repair's second leg, and why it sits above recency. An
      absence read is tagged ``severity:low`` BY ITS OWN DESK while a real event
      is moderate or above, so severity is the discriminator that catches the
      absence titles the name-shape predicate misses. The live AR frame name is
      exactly that case: "Argentina – No Coercive Economic Measures Observed"
      does NOT match the non-event regex (its trailing-noun anchor has no
      "measures"), and is beaten here on severity by the elevated protest head
      instead. Unscored / unknown levels rank 0, below ``low``: a title the desk
      never graded never displaces one it did.

      FRAME-3 (2026-08-21) WEAKENS THIS LEG, deliberately and without changing
      it. The severity tag used to be the SLICE DELTA — "no change observed"
      banded low almost by construction — and is now the dimension's STANDING
      STATE, so an absence read on a desk whose standing condition is serious
      can be tagged ``high``. What survives is ``asserts_something``, which
      ranks strictly above severity and is the leg that actually reads the
      TITLE: a "No observable shift…" head loses on it whatever its severity
      (``test_window_ledger.test_frame_naming_refuses_a_non_event_title_even_at_high_severity``
      is that property, and it was written before this train). Severity remains
      the right tiebreak among titles that all assert something. Re-keying the
      rank on ``severity_delta`` was considered and rejected: a frame should be
      named for the biggest thing in it, not the most recently moving one.
    * **produced_at + id** — the previous rule (latest member wins), kept as the
      final tiebreak, so nothing changes for a frame whose members all rank
      alike. The id tail makes it deterministic: a name flickering between two
      equal candidates every cadence tick would churn ``/situations`` for no
      reason.

    NO RECENCY WINDOW. An earlier draft narrowed candidates to the frame's last
    seven days before ranking; it is left out deliberately, because
    ``produced_at`` already breaks ties by recency and a hard window would
    re-introduce the defect on exactly the frames that suffer it worst — a
    dormant frame whose only recent members are absence reads.
    """
    title = str(row.get("title") or "").strip()
    if not title or _is_report_snapshot_name(title):
        return (0, 0, 0, "")
    return (
        1,
        0 if is_non_event_situation_name(title) else 1,
        _SEVERITY_RANK.get(str(row.get("severity") or "").strip().lower(), 0),
        f"{_produced_key(row)}|{row.get('id')}",
    )


def _produced_key(row: dict[str, Any]) -> str:
    """``produced_at`` as a sortable string ("" for missing — sorts oldest, so a
    timestampless row is never chosen as the freshest). Same coercion
    :func:`_latest` makes, for the same heterogeneous-key reason."""
    v = row.get("produced_at")
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


def _situation_name(rows: list[dict[str, Any]], sig: str) -> str:
    """Human label — the member title that best NAMES the frame (see the
    FRAME-2 note above and :func:`_name_rank` for the ordering).

    Falls back to the signature's topic label when NO member carries a usable
    title, so a report receipt never mints a situation named after a raw JSON
    fragment or a churning date (DQ P6).
    """
    if not rows:
        return f"Situation {sig}"[:512]
    best = max(rows, key=_name_rank)
    if _name_rank(best)[0]:
        return str(best.get("title") or "").strip()[:512]
    topic = _topic_from_signature(sig)
    return (f"Situation: {topic}" if topic else f"Situation {sig}")[:512]


# Situation lifecycle (decay) — the "events come and go" mechanic. Intensity is
# RECENCY-WEIGHTED (each member contributes exp half-life since its produced_at)
# so a situation that stops getting fresh findings fades instead of holding a
# flat corroboration count forever; status transitions active → dormant →
# closed by the age of its most-recent member, and REOPENS (→ active)
# automatically when a new member lands (the upsert recomputes every run).
# Tunables, in days.
_INTENSITY_HALF_LIFE_DAYS = 3.0
_STATUS_ACTIVE_MAX_DAYS = 2.0
_STATUS_DORMANT_MAX_DAYS = 7.0
_LN2 = log(2.0)


# ---------------------------------------------------------------------------
# H1 — THE FORGETTING CURVE (CORRECTNESS-R2, M-1 / ATTRIBUTION §1)
# ---------------------------------------------------------------------------
#
# THE DEFECT. Everything above decays on the MEMBER clock — the ``produced_at``
# of the desk reads that clustered into the frame. That clock is wound by the
# PRODUCT, not by the world. Seven country desks write into one country frame
# several times a day, and a desk emits a read whenever it LOOKS, including the
# "no material change was observed" absence reads that make up the bulk of a
# quiet frame's membership. So every member is minutes old, every half-life
# weight is ~1.0, the sum saturates, and ``last_event_at`` is always today.
#
# Verified in the live DB at the round's T0: the AR frame stood at
# ``intensity 60.09 · event_count 396 · last_event_at 2026-08-25 · status
# active`` on a maritime pilots' strike that had ENDED on 5 August, three weeks
# earlier, and whose only wire signal in 45 days was headlined "Argentina's
# Pilots Resume Work". The desks wrote "nothing changed" into the frame; the
# frame reported that back as high intensity and recent activity; the escalation
# desk cited it at confidence 0.90 as its decisive evidence that the strike was
# live. Nothing in that loop touched the wire.
#
# THE FIX is not "add a decay" — the decay was already here. It is to make the
# curve run on a clock the product cannot wind: the CORROBORATION clock
# (:func:`legba.data.situations.trajectory.read_corroboration`), whose ticks are
# the frame's SIGNIFICANT ledger deltas. A significant delta cannot exist
# without cited evidence (checked at construction, in the DB CHECK, and by the
# verify pass over the claim that carries it), and its ``occurred_at`` IS the
# ``produced_at`` of the evidence that moved the frame. An
# ``unchanged_checkpoint`` — the row the desks' "no material change" bookkeeping
# becomes — asserts nothing and therefore winds nothing.
#
# TWO EFFECTS, both strictly one-way:
#
#   1. INTENSITY is multiplied by a PERSISTENCE FACTOR in (0, 1] that decays on
#      the corroboration clock. The member-recency sum keeps its meaning and its
#      SCALE (the alert plane's ``DEFAULT_INTENSITY_FLOOR = 2.0``, the thematic
#      proposer's floor and the ``LEGBA_SITUATION_GROUNDING_MIN_INTENSITY`` gate
#      are all calibrated against it; re-basing the units would silently move
#      three unrelated thresholds). What changes is that the sum is now the
#      CEILING a frame gets while its corroboration is fresh, not a number it
#      holds forever on its own bookkeeping.
#
#   2. STATUS can only be DEMOTED. A frame whose corroboration is stale can
#      never read ``active``, whatever its desks have been typing. It is never
#      PROMOTED here, and it is never auto-CLOSED here: closing on silence is
#      exactly what ``trajectory.next_state`` refuses (D4 — "a situation is
#      NEVER auto-closed by silence alone, it goes dormant and waits"), and this
#      clock must not smuggle in the opposite rule. Closing stays earned — a
#      resolution-grounded de-escalation, which now reaches this column too (see
#      ``trajectory_state`` in :func:`_situation_fields`).
#
# THE HALF-LIFE IS KEYED TO EVIDENCE DENSITY, which is the whole epistemic point.
# A frame the world has moved once has one article's claim on the reader's
# attention and should fade in about a day; a frame moved nine separate times is
# a standing condition and may hold for a week. ``sqrt`` rather than a linear
# ramp because corroboration has diminishing returns — the second independent
# report is worth far more than the ninth — and because it keeps the whole
# fleet's half-lives inside one order of magnitude without a special case.

#: Half-life, in days, of a frame corroborated exactly ONCE. Deliberately short:
#: one dated report is a fact about a day, and by the time it is 24 hours past
#: the desk's own 72-hour slice it has no business dominating a read.
_CORROBORATION_HALF_LIFE_BASE_DAYS = 1.5

#: Floor / ceiling on the density-scaled half-life. The floor keeps a
#: pathological zero-count row from collapsing instantly; the ceiling is the
#: fortnight the window ledger carries and the composition admits heads over
#: (``trajectory.SIGNIFICANT_WINDOW_HOURS`` = 336h), so no frame persists on the
#: strength of corroboration the rest of the read cannot even see.
_CORROBORATION_HALF_LIFE_MIN_DAYS = 1.0
_CORROBORATION_HALF_LIFE_MAX_DAYS = 14.0

#: How stale a CORROBORATED frame's evidence may be while it still reads
#: ``active``. THE DESK'S OWN SLICE (72 hours) — the admissibility horizon
#: inside which a desk is licensed to treat something as current. Past it the
#: frame is still open and still rendered; it is rendered DORMANT, which is what
#: it is.
_CORROBORATION_ACTIVE_MAX_DAYS = 3.0

#: The same question for a frame the ledger has NEVER moved, where the anchor is
#: the frame's opening rather than a measured corroboration. It gets the
#: trajectory module's OWN dormancy horizon
#: (:data:`~legba.data.situations.trajectory.DORMANCY_DAYS`, 14 days) — the
#: constant already written to answer "has this thread gone quiet enough that a
#: reader should stop expecting movement", which is exactly the question here.
#:
#: The asymmetry is the point. Three days is a statement about EVIDENCE we have
#: (adjudicated, and nothing since); a fortnight is the honest bar for a frame
#: we have never adjudicated at all, and it is what keeps a genuinely new
#: situation from reading dormant merely because the hourly tracker has not
#: reached it yet. Both bars catch the whole of the DQ sweep's class: its 24
#: never-corroborated frames run from 27.7 to 77.8 days old.
_UNCORROBORATED_ACTIVE_MAX_DAYS = float(_TRAJECTORY_DORMANCY_DAYS)

#: Env overrides, one per constant, read through :func:`_tunable`. Same idiom as
#: ``LEGBA_SITUATION_GROUNDING_MIN_INTENSITY`` next door in ``runtime.grounding``
#: — a bad or absent value falls back to the module default, so the curve is
#: retunable on a live fleet without a code change and can never be turned into
#: a crash by a typo'd env var.
_ENV_HALF_LIFE_BASE = "LEGBA_SITUATION_CORROBORATION_HALF_LIFE_DAYS"
_ENV_HALF_LIFE_MIN = "LEGBA_SITUATION_CORROBORATION_HALF_LIFE_MIN_DAYS"
_ENV_HALF_LIFE_MAX = "LEGBA_SITUATION_CORROBORATION_HALF_LIFE_MAX_DAYS"
_ENV_ACTIVE_MAX = "LEGBA_SITUATION_CORROBORATION_ACTIVE_MAX_DAYS"
_ENV_UNCORROBORATED_ACTIVE_MAX = "LEGBA_SITUATION_UNCORROBORATED_ACTIVE_MAX_DAYS"


def _tunable(env_name: str, default: float) -> float:
    """A positive float from ``env_name``, or ``default``.

    Absent, blank, unparseable or non-positive all fall back — a decay constant
    of zero or a negative half-life is not a configuration, it is a division by
    zero inside a prompt build.
    """
    raw = os.getenv(env_name)
    if not raw or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0.0 else default


def corroboration_half_life_days(corroboration_count: int) -> float:
    """The intensity half-life for a frame with ``corroboration_count`` moves.

    ``base * sqrt(n)``, clamped: 1 move → 1.5d, 4 → 3.0d, 9 → 4.5d, 16 → 6.0d,
    with a hard ceiling at the fortnight. See the block comment above for why
    density keys the curve and why the root rather than a linear ramp.

    A count of zero or less yields the floor — it is only reachable from a
    malformed ledger row, and the safe answer for "we cannot tell how well
    corroborated this is" is "assume barely".
    """
    base = _tunable(_ENV_HALF_LIFE_BASE, _CORROBORATION_HALF_LIFE_BASE_DAYS)
    low = _tunable(_ENV_HALF_LIFE_MIN, _CORROBORATION_HALF_LIFE_MIN_DAYS)
    high = _tunable(_ENV_HALF_LIFE_MAX, _CORROBORATION_HALF_LIFE_MAX_DAYS)
    if high < low:
        low, high = high, low
    if corroboration_count <= 0:
        return low
    return min(high, max(low, base * sqrt(float(corroboration_count))))


def corroboration_persistence(
    evidence_anchor_at: datetime | None,
    half_life_days: float,
    now: datetime,
) -> float:
    """The multiplier a frame's member-recency intensity keeps, in ``(0.0, 1.0]``.

    ``exp(-ln2 * evidence_age_days / half_life)`` — 1.0 while the world has just
    moved the frame, halving every ``half_life_days`` thereafter.

    ``evidence_anchor_at`` is the newest moment at which evidence is KNOWN to
    have touched this frame: its last significant ledger delta if it has one,
    and otherwise the frame's own OPENING (see :func:`_evidence_anchor`).
    ``None`` — a frame with neither — returns ``1.0``, unreachable in practice
    because a frame with no members does not materialize.
    """
    if evidence_anchor_at is None:
        return 1.0
    age_days = max(0.0, (now - evidence_anchor_at).total_seconds() / 86400.0)
    return exp(-_LN2 * age_days / max(half_life_days, 1e-6))


def _evidence_anchor(
    corroboration: Corroboration | None,
    opened_at: datetime | None,
    produced: list[datetime],
) -> tuple[datetime | None, float, int, bool]:
    """``(anchor, half_life_days, density, corroborated)`` — which clock decays.

    WHICH clock is chosen by :func:`legba.data.situations.trajectory.evidence_anchor`,
    shared with the tracker's dormancy test since #64 (before that the two asked
    the same question through different clocks and the tracker's was unreachable).
    What stays here is what only the CURVE needs: the density-keyed half-life.

    A frame the ledger HAS moved anchors on its newest significant delta, with
    that delta count as its evidence density and the density-keyed half-life.

    A frame the ledger has NEVER moved anchors on its OPENING, at the LONGEST
    half-life the system offers. That combination is deliberate and both halves
    are load-bearing.

    THE CASE. The 2026-08-27 DQ sweep found this class fleet-wide: **24 of 50
    non-closed situations have never logged a single evidence-bearing ledger
    row** — 22 with no ledger rows at all — and every one renders ``active`` at
    intensity up to 60.9 with ``updated_at`` refreshed within the minute. The
    worst is a Saudi Arabia / Houthi maritime-embargo frame, 73 days old with
    zero events ever.

    WHY THE OPENING. A situation is materialized BY findings, so its
    ``valid_from`` is the earliest moment any evidence attached to it, and
    before adjudication it is the only evidence timestamp the frame owns. An
    earlier draft left such a frame INERT, reasoning that "we have never looked"
    is not evidence of quiet. That is right about the LABEL and wrong about the
    CURVE: a frame with no recorded corroboration has no evidence of currency at
    all, which is not a weaker claim to persistence than a stale frame's. So the
    two DECAY alike, while the render keeps them distinguishable —
    ``last_corroborated_at`` is stamped only for a real one, and an
    anchored-on-opening frame renders ``NEVER-CORROBORATED`` with its age beside
    it. Decay on what is known; label what is known.

    WHY THE LONGEST HALF-LIFE. The density-keyed curve reads a MEASUREMENT (how
    many times the world moved this frame) and here there is no measurement to
    read — so the fleet's most charitable constant applies, and any decay this
    frame suffers is earned purely by AGE. It keeps the two ends honest at once:
    a frame opened an hour ago is fully fresh (correct — the tracker runs hourly
    and has not reached it yet), while the 73-day Saudi frame lands at ~2.7% of
    its bookkeeping intensity, under the alert plane's own 2.0 paging floor.
    Assuming the shortest half-life instead would have crushed a four-day-old
    frame to 6% on no evidence either way, which is a different lie.

    NO SIGNAL-DATE ANCHOR, and this was measured rather than assumed. Keying the
    curve on "the newest underlying signal cited by any member" was tried
    against the live AR frame and REPRODUCES the defect one hop down: a desk
    re-cites its fresh 72h slice on every run whether or not anything about the
    frame happened, so the newest cited signal is hours old on every member
    (2026-08-25 15:33 against a T0 of 18:58). The same is true of "the newest
    member above ``low`` severity" — for AR that is also NOW. The trajectory
    ledger's significant deltas are the only clock in the substrate that cannot
    be wound by the pipeline's own cadence, because a significant delta cannot
    be written without cited evidence.
    """
    anchor, corroborated = _shared_evidence_anchor(
        corroboration, opened_at or min(produced, default=None),
    )
    if corroborated and corroboration is not None:
        return (
            anchor,
            corroboration_half_life_days(corroboration.count),
            corroboration.count,
            True,
        )
    return (
        anchor,
        _tunable(_ENV_HALF_LIFE_MAX, _CORROBORATION_HALF_LIFE_MAX_DAYS),
        0,
        False,
    )


def _aware(dt: Any) -> datetime | None:
    """Coerce a produced_at value to a tz-aware datetime, or None."""
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _decayed_intensity(produced: list[datetime], now: datetime) -> float:
    """Sum of exp(half-life) weights over member produced_at timestamps.

    The MEMBER-recency curve, unchanged. On its own it measures how busy this
    frame's DESKS have been, which is why :func:`corroboration_persistence`
    multiplies it — see the H1 block comment above.
    """
    total = 0.0
    for p in produced:
        age_days = max(0.0, (now - p).total_seconds() / 86400.0)
        total += exp(-_LN2 * age_days / _INTENSITY_HALF_LIFE_DAYS)
    return round(total, 4)


def _situation_status(
    last_event_at: datetime | None,
    now: datetime,
    *,
    evidence_anchor_at: datetime | None = None,
    corroborated: bool = True,
    trajectory_state: str | None = None,
) -> str:
    """active (fresh) → dormant (quiet) → closed (stale), on TWO clocks.

    The MEMBER clock (``last_event_at``) keeps its ladder exactly as it was:
    a frame whose desks have stopped writing entirely for a week is closed, and
    that is still the only route to auto-closure by silence.

    The EVIDENCE clock (``evidence_anchor_at``) can only DEMOTE. A frame
    whose last known contact with the world is older than
    :data:`_CORROBORATION_ACTIVE_MAX_DAYS` may not read ``active`` however
    recently its desks have restated it — the H1 repair, and the one line that
    breaks "the desks write 'nothing changed' and the frame reports it back as
    recent activity". It never promotes and it never closes: closing on silence
    is what ``trajectory.next_state`` refuses (D4), and a demotion-only rule
    cannot smuggle the opposite in through the side door.

    ``trajectory_state`` is the ledger's OWN answer, and a ledger that has
    reached :data:`~legba.data.situations.trajectory.STATE_CLOSED` has done the
    one thing that closes a frame on merit: a resolution-grounded de-escalation.
    Before H1 that verdict could not reach this column at all — it lived only in
    ``situation_events.state_to`` while both register reads gate on
    ``situations.status <> 'closed'`` — so a frame the tower had CLOSED went on
    rendering at full intensity. Honoring it here is the resolution path's last
    missing hop.
    """
    if trajectory_state == _TRAJECTORY_CLOSED:
        return "closed"
    if last_event_at is None:
        member_status = "active"
    else:
        age_days = (now - last_event_at).total_seconds() / 86400.0
        if age_days <= _STATUS_ACTIVE_MAX_DAYS:
            member_status = "active"
        elif age_days <= _STATUS_DORMANT_MAX_DAYS:
            member_status = "dormant"
        else:
            member_status = "closed"
    if member_status != "active" or evidence_anchor_at is None:
        return member_status
    evidence_age = (now - evidence_anchor_at).total_seconds() / 86400.0
    horizon = (
        _tunable(_ENV_ACTIVE_MAX, _CORROBORATION_ACTIVE_MAX_DAYS)
        if corroborated
        else _tunable(_ENV_UNCORROBORATED_ACTIVE_MAX, _UNCORROBORATED_ACTIVE_MAX_DAYS)
    )
    return "active" if evidence_age <= horizon else "dormant"


def _situation_fields(
    sig: str,
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    corroboration: Corroboration | None = None,
    opened_at: datetime | None = None,
    trajectory_state: str | None = None,
) -> dict[str, Any]:
    """Derive the situations-table column values for one cluster.

    ``corroboration`` — what the trajectory ledger knows about this frame's last
    contact with the WORLD (``None`` for a frame the ledger has never moved, in
    which case the curve anchors on the frame's opening — see
    :func:`_evidence_anchor`). ``opened_at`` — the STORED ``valid_from``, which
    reaches back past the 30-day member lookback and is therefore the honest
    opening for an old frame; falls back to the earliest current member.
    ``trajectory_state`` — the ledger's current state, whose only load-bearing
    value here is ``closed``.
    """
    now = now or datetime.now(timezone.utc)
    member_ids = [str(r["id"]) for r in rows if r.get("id")]
    produced = [p for p in (_aware(r.get("produced_at")) for r in rows) if p is not None]
    last_event_at = max(produced, default=None)
    anchor_at, half_life, corroboration_count, corroborated = _evidence_anchor(
        corroboration, _aware(opened_at), produced,
    )
    persistence = corroboration_persistence(anchor_at, half_life, now)
    status = _situation_status(
        last_event_at, now,
        evidence_anchor_at=anchor_at,
        corroborated=corroborated,
        trajectory_state=trajectory_state,
    )
    name = _situation_name(rows, sig)
    return {
        "situation_signature": sig,
        "name": name,
        "category": _topic_from_signature(sig),
        # #64 — WHICH of the desk's questions this frame answers. Derived from
        # the key rather than from the members so it is stable even on a tick
        # where the member set is empty-ish, and stamped rather than re-parsed by
        # every reader.
        "dimension": _dimension_from_signature(sig),
        # DQ P6 — mark a steady-state / non-event frame authoritatively at
        # MATERIALIZATION (not only name-filtered on read) using the SAME shared
        # predicate the grounding read uses, so the two never drift. Stored in the
        # situation ``data`` payload; a steady-state frame is a "nothing to
        # report" / status-quo read and must not head the intensity ranking.
        "steady_state": is_non_event_situation_name(name),
        "event_count": len(rows),
        # Recency-weighted intensity (exp half-life) over the MEMBERS, scaled by
        # the H1 persistence factor on the CORROBORATION clock — so the number
        # fades as the WORLD goes quiet, not merely as the desks stop typing.
        # Falls back to the raw count only when no produced_at is resolvable
        # (still scaled: an unresolvable-timestamp frame is not a fresher one).
        "intensity_score": round(
            (_decayed_intensity(produced, now) if produced else float(len(rows)))
            * persistence,
            4,
        ),
        # Lifecycle status — drives the timeline span fade + "comes and goes".
        "status": status,
        "last_event_at": last_event_at,
        # H1 — THE EVIDENCE AGE, stamped at materialization so every register
        # read can print it without a second join. ``data`` is the same home
        # ``steady_state`` uses for a derived marker, and for the same reason:
        # one writer computes it, every reader sees the same answer.
        #
        # TWO KEYS, deliberately. ``evidence_anchor_at`` is what the curve ran
        # on and is always present; ``last_corroborated_at`` is present ONLY
        # when the ledger really moved this frame. Never defaulted to
        # ``last_event_at`` — substituting a bookkeeping timestamp for an
        # evidence one is the exact defect M-1 records, where "the latest event
        # timestamp on 20 August 2026" was a pipeline field printed by a desk as
        # a world date.
        "evidence_anchor_at": anchor_at,
        "last_corroborated_at": anchor_at if corroborated else None,
        "corroboration_count": corroboration_count,
        "persistence": round(persistence, 4),
        # Temporal frame (Phase 5a, migration 0040): a situation is valid FROM
        # its earliest member finding and stays open (valid_until NULL) while
        # active/dormant; when it CLOSES we stamp valid_until = last_event_at so
        # it expresses "active over [t0, t1)" like facts/nexuses. This is what
        # makes a situation a persistent FRAME rather than a mutable snapshot.
        "valid_from": min(produced, default=None),
        "valid_until": last_event_at if status == "closed" else None,
        "member_finding_ids": member_ids[:_MAX_MEMBERS],
    }


def _group_by_signature(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Members by signature, DIMENSIONED (#64).

    The grouping key is the member's stored ``situation_signature`` normalized
    through :func:`finding_supersession.with_dimension`, so a row stamped before
    the re-key groups under the same key a row stamped after it does. Without
    that hop this handler would go on materializing the country-absorbing
    mega-frame out of its own back-catalogue for the whole 30-day lookback,
    however the supersession layer had re-keyed. It is idempotent, so a member
    already carrying its dimension is untouched.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        # DQ P6 — a COMPOSITION / META producer's report never materializes as a
        # situation (the live SQL fetch already excludes them; this also covers
        # the synthetic deps=None path). ``analyst_id`` is absent on some legacy
        # synthetic rows → treated as non-composition (unchanged behavior).
        if str(r.get("analyst_id") or "") in _COMPOSITION_ANALYST_IDS:
            continue
        sig = r.get("situation_signature")
        if not sig:
            continue
        groups.setdefault(_with_dimension(str(sig), r.get("analyst_id")), []).append(r)
    return groups


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


async def _upsert_situation(
    conn: Any,
    *,
    fields: dict[str, Any],
    analyst_id: str,
    analyst_version: str | None,
    target_id: str | None,
    run_id: Any,
) -> str:
    """Insert or update ONE situation keyed by (signature, analyst_id).

    Returns ``"created"`` or ``"updated"``. NEVER deletes.

    Atomic UPSERT on the ``(situation_signature, analyst_id)`` unique index
    (migration 0040) — replaces the prior racy SELECT-then-INSERT/UPDATE and
    promotes ``situation_signature`` to a first-class column (it is also kept
    in ``data`` for the member-id payload). ``valid_from``/``valid_until``
    carry the temporal frame: ``valid_from`` only ever moves EARLIER
    (``LEAST(stored, min(current members))``) so a frame's start can be pulled
    back to the true earliest member but never drifts FORWARD when old members
    age out of the lookback (this also self-heals the 0040→0041 backfill); and
    ``valid_until`` tracks the CURRENT lifecycle each run — stamped to
    ``last_event_at`` when the frame is closed, and re-set to NULL (re-opened)
    when a fresh member flips it back to active/dormant (the ``ON CONFLICT DO
    UPDATE`` writes ``EXCLUDED.valid_until``, which ``_situation_fields``
    derives as ``None`` for any non-closed status). So an open frame is always
    ``valid_until IS NULL`` and a closed one always carries its close time —
    consistent with the facts/nexuses temporal gate the grounding read uses.
    """
    sig = fields["situation_signature"]
    derived_from = [UUID(m) for m in fields["member_finding_ids"]]
    corroborated_at = fields.get("last_corroborated_at")
    anchor_at = fields.get("evidence_anchor_at")
    data = {
        "situation_signature": sig,
        "member_finding_ids": fields["member_finding_ids"],
        "sub_handler": SUB_HANDLER_NAME,
        # DQ P6 — authoritative steady-state marker (see _situation_fields).
        "steady_state": bool(fields.get("steady_state")),
        # H1 — the evidence clock, stamped for the register reads.
        # ``last_corroborated_at`` is written ONLY when the ledger really moved
        # this frame, so a reader can never mistake "never corroborated" for
        # "corroborated at the epoch"; ``evidence_anchor_at`` is what the curve
        # actually ran on and is always written when it exists.
        "corroboration_count": int(fields.get("corroboration_count") or 0),
        "persistence": float(fields.get("persistence") or 1.0),
    }
    # #64 — the producing dimension, present only on a dimensioned (post-re-key)
    # frame. Absent rather than null on a legacy key, so "this frame predates the
    # split" and "this frame's producer is unknown" stay distinguishable (the
    # latter is a real value, UNATTRIBUTED_DIMENSION).
    #
    # ITS READER IS A HUMAN, and that is stated rather than assumed: `data` is
    # returned verbatim by `/api/v1/situations` and the lineage walk, so an
    # operator looking at two frames on one desk can see WHICH question each
    # answers without re-parsing the key. No code path reads it, and it is not
    # claimed to have one — the 08-29 census caught `steady_state` being written
    # for a consumer that re-derives it anyway, and this comment exists so this
    # field is never mistaken for machinery.
    dimension = fields.get("dimension")
    if dimension:
        data["dimension"] = str(dimension)
    if isinstance(anchor_at, datetime):
        data["evidence_anchor_at"] = anchor_at.isoformat()
    if isinstance(corroborated_at, datetime):
        data["last_corroborated_at"] = corroborated_at.isoformat()
    run_uuid = None
    if run_id:
        try:
            run_uuid = UUID(str(run_id))
        except (ValueError, TypeError):
            run_uuid = None

    row = await conn.fetchrow(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, target_id, analyst_id, analyst_version,
             produced_at, derived_from, schema_uri, run_id,
             situation_signature, valid_from, valid_until)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),$12,$13,$14,$15,$16,$17)
        ON CONFLICT (situation_signature, analyst_id)
            WHERE situation_signature IS NOT NULL
        DO UPDATE SET
            name=EXCLUDED.name, category=EXCLUDED.category,
            event_count=EXCLUDED.event_count,
            intensity_score=EXCLUDED.intensity_score,
            last_event_at=EXCLUDED.last_event_at,
            derived_from=EXCLUDED.derived_from, data=EXCLUDED.data,
            status=EXCLUDED.status, valid_until=EXCLUDED.valid_until,
            valid_from=LEAST(situations.valid_from, EXCLUDED.valid_from),
            target_id=COALESCE(situations.target_id, EXCLUDED.target_id),
            updated_at=NOW()
        RETURNING (xmax = 0) AS inserted
        """,
        uuid4(), json.dumps(data), fields["name"], fields["status"],
        fields["category"], fields["last_event_at"], fields["event_count"],
        fields["intensity_score"],
        _target_for_category(fields["category"], target_id),
        analyst_id, analyst_version,
        derived_from, _SITUATION_SCHEMA_URI, run_uuid,
        sig, fields["valid_from"], fields["valid_until"],
    )
    # xmax = 0 on the freshly-INSERTed tuple, non-zero when ON CONFLICT took the
    # UPDATE branch — the idiomatic upsert created/updated discriminator.
    return "created" if row and row["inserted"] else "updated"


_SIGNATURE_IDS_SQL = """
    SELECT situation_signature, id,
           COALESCE(valid_from, created_at) AS opened_at
      FROM situations
     WHERE analyst_id = $1
       AND situation_signature = ANY($2::text[])
"""


@dataclass(frozen=True)
class _LedgerState:
    """What is already known about this run's frames, keyed by SIGNATURE."""

    corroboration: dict[str, Corroboration]
    trajectory_state: dict[str, str]
    opened_at: dict[str, datetime]


async def _read_ledger_state(
    conn: Any, *, analyst_id: str, signatures: Any,
) -> _LedgerState:
    """The stored frames + the trajectory ledger's view of them, by SIGNATURE.

    The ledger is keyed by ``situations.id`` and this handler works in
    signatures (it is about to MINT the ids for any frame that does not exist
    yet), so one lookup bridges the two — and that same lookup carries the
    STORED ``valid_from``, which is the frame's true opening and reaches back
    past this run's 30-day member lookback. That matters: the fallback evidence
    anchor for a never-corroborated frame IS its opening, and a 73-day-old frame
    whose opening was clipped to 30 days would read as less stale than it is.

    A signature with no row yet is a first-materialization: absent from all
    three maps, so it anchors on its earliest member — a brand-new frame, fully
    fresh, which is correct.

    DEGRADE-NOT-BREAK. A read that fails logs at WARNING and yields empty maps,
    so this run materializes as it did before H1 rather than failing the
    deterministic cadence over an enrichment. WARNING and not debug because a
    silently-inert forgetting curve is the defect this mechanism exists to stop.
    """
    sigs = [str(s) for s in signatures]
    empty = _LedgerState({}, {}, {})
    if not sigs:
        return empty
    try:
        rows = await conn.fetch(_SIGNATURE_IDS_SQL, analyst_id, sigs)
        by_id = {str(r["id"]): str(r["situation_signature"]) for r in rows}
        opened = {
            str(r["situation_signature"]): r["opened_at"]
            for r in rows
            if isinstance(r["opened_at"], datetime)
        }
        if not by_id:
            return empty
        ids = list(by_id)
        corroboration = await read_corroboration(conn, ids)
        states = await read_current_states(conn, ids)
    except Exception as exc:  # noqa: BLE001 — enrichment, never a failed run
        logger.warning(
            "situation_clustering.ledger_state_failed err=%s — H1 evidence "
            "decay is INERT this run", exc,
        )
        return empty
    return _LedgerState(
        corroboration={by_id[k]: v for k, v in corroboration.items() if k in by_id},
        trajectory_state={by_id[k]: v for k, v in states.items() if k in by_id},
        opened_at=opened,
    )


async def _resolve_pool(
    pool: Any,
    *,
    analyst_id: str,
    analyst_version: str | None,
    target_id: str | None,
    run_id: Any,
    lookback_days: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Materialize situations from signature-stamped findings.

    Returns ``(created, updated, clusters)``.
    """
    created = 0
    updated = 0
    clusters: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            -- FRAME-2: ``severity`` joins the projection so the frame NAME can
            -- prefer the desk's own worst recent assessment over whichever read
            -- happened to land last. It is a first-class read column (lifted
            -- from the ``severity:<level>`` tag at write, S3-T4), so this costs
            -- one more projected column and no join.
            SELECT id, title, produced_at, situation_signature, analyst_id,
                   severity
            FROM analyst_outputs
            WHERE kind = 'finding' AND situation_signature IS NOT NULL
              AND produced_at > NOW() - INTERVAL '{int(lookback_days)} days'
              AND analyst_id <> ALL($1::text[])
            ORDER BY produced_at ASC, id ASC
            """,
            list(_COMPOSITION_ANALYST_IDS),
        )
        groups = _group_by_signature([dict(r) for r in rows])
        ledger = await _read_ledger_state(
            conn, analyst_id=analyst_id, signatures=groups.keys(),
        )
        for sig, members in groups.items():
            fields = _situation_fields(
                sig, members,
                corroboration=ledger.corroboration.get(sig),
                opened_at=ledger.opened_at.get(sig),
                trajectory_state=ledger.trajectory_state.get(sig),
            )
            action = await _upsert_situation(
                conn, fields=fields, analyst_id=analyst_id,
                analyst_version=analyst_version, target_id=target_id, run_id=run_id,
            )
            created += action == "created"
            updated += action == "updated"
            clusters.append({
                "situation_signature": sig,
                "event_count": fields["event_count"],
                "action": action,
            })
    return created, updated, clusters


def _resolve_synthetic(inputs: list[dict[str, Any]]) -> tuple[int, int, list[dict[str, Any]]]:
    """deps=None path (unit tests): group pre-shaped rows, no DB writes."""
    groups = _group_by_signature([dict(r) for r in inputs])
    clusters = [
        {
            "situation_signature": sig,
            "event_count": len(members),
            "action": "synthetic",
            "name": _situation_fields(sig, members)["name"],
            "category": _topic_from_signature(sig),
        }
        for sig, members in groups.items()
    ]
    return 0, 0, clusters


def _build_finding(
    *, created: int, updated: int, clusters: list[dict[str, Any]] | None, target_id: str | None,
) -> FindingPayload:
    n = len(clusters or [])
    title = f"Situation clustering: {n} situations ({created} new, {updated} updated)"
    if target_id:
        title = f"{title} for {target_id}"
    return FindingPayload(
        title=title[:2048],
        body="\n".join([f"situations={n}", f"created={created}", f"updated={updated}"])[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "situations_created": created,
            "situations_updated": updated,
            "clusters": clusters if clusters is not None and len(clusters) <= 100 else None,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Options
    -------
    lookback_days:
        Only findings this recent are eligible (default 30) — older settled
        history isn't a live situation.
    """
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_version = options.get("analyst_version")
    target_id = options.get("target_id")
    run_id = options.get("run_id")
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))

    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    if pool is not None:
        try:
            created, updated, clusters = await _resolve_pool(
                pool,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                target_id=target_id,
                run_id=run_id,
                lookback_days=lookback_days,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("situation_clustering.pool_failed err=%s", exc)
            created, updated, clusters = 0, 0, []
        clusters_for_finding = clusters if len(clusters) <= 100 else None
    else:
        created, updated, clusters = _resolve_synthetic(inputs)
        clusters_for_finding = clusters

    finding = _build_finding(
        created=created, updated=updated, clusters=clusters_for_finding, target_id=target_id,
    )
    # Emit a FEED finding only when a NEW situation actually formed. A run that
    # only re-touched existing situations (created == 0) is an idempotent
    # refresh — the situation rows carry the updates; repeating the identical
    # "N situations (0 new, N updated)" summary into the feed every cadence tick
    # is noise. The run is still fully traced (force_trace_only skips only the
    # analyst_outputs row, not the trace or the situation side-writes).
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=(created == 0),
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
