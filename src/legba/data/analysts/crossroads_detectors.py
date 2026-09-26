# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CROSSROADS pre-pass — four deterministic detectors over the day's
substrate, rendered as a block the model narrates and cites by ordinal
(Program 5 lane 3, ``planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md`` §5/§7).

THE SHAPE THIS COPIES, AND WHY. :mod:`spread_block` (Program 7 piece 7e) fixed
one class of defect: a model asked to eyeball "coordination" off a numbered list
will keep finding it, because the judgment call has no anchor. Its fix was not a
better prompt — it was to COMPUTE the arithmetic in code, hand the model the
numbers with ``[N]`` ordinals, and make the descriptor require every claim to
name the block's own markers. The crossroads has the identical exposure, one
layer up: "which desks are converging", "which desk is drifting", "who
contradicts whom" and "who has gone quiet" are all arithmetic the model would
otherwise ESTIMATE from prose it was shown, and a crossroads that estimates is
just another voice guessing. So the four detectors run here, in code, over rows
the governed read tools returned, and the block hands the model a row it did not
compute and may not restate differently.

THE FOUR DETECTORS (§5, the fixed mandate):

  * **PATTERNS** — one entity or one commodity term carried by
    >= :data:`PATTERN_MIN_DESKS` DISTINCT desks' verified findings in the window.
  * **DRIFTS** — a desk whose band (the severity tag mapped through
    :data:`scorecard_banding.SEVERITY_TO_BAND`) moved :data:`DRIFT_CYCLES`
    consecutive cycles in ONE direction while its CITED MASS did not follow.
  * **CONTRADICTIONS** — two DIFFERENT desks' verified claims about the same
    subject with opposite polarity for which the contested-claims arbiter holds
    NO contention row.
  * **SILENCES** — a roster unit whose last run is older than
    :data:`SILENCE_CADENCES` cadences, or a roster country that no lens entry
    named in the window.

EVERY ROW CARRIES ITS OWN WARRANT. A :class:`CrossroadsRow` is the kind, the
desks, the ARITHMETIC as a sentence a reviewer can recompute, the cited refs,
and one PROPOSED LEDGER ROW with a frozen ``resolution_test`` (the H13 shape the
design's §3 constraint ``inquiry_ledger_hypothesis_needs_test`` enforces at the
table). The detector proposes; the inquiry kind's REFLECT phase writes. Nothing
here writes anything.

TWO EMPTIES THAT ARE NOT THE SAME EMPTY (the B-8 lesson from
``get_assessments``'s ``disagreements: null`` vs ``[]``). "The detectors ran and
nothing fired" and "the detectors could not run" are different answers and are
rendered as different lines: :data:`_NO_FIRE_LINE` states the scope that was
actually read; :data:`_NOT_RUN_LINE` says plainly that absence here is NOT
evidence of absence out there. Collapsing them would let a run with no wired
read surface narrate a quiet world.

PRECISION OVER RECALL, everywhere — the spread-block posture. A missed pattern
costs a sentence the crossroads did not write; a FALSE pattern manufactures a
convergence out of ordinary vocabulary and hands it to a model with instructions
to narrate it. So:

  * every word-level judgement — what counts as a term, what counts as a
    direction, what counts as a citation — lives in :mod:`crossroads_vocab`,
    which is pure and states its own guards: title-only extraction, no proper
    noun out of a Title-Cased headline, DECLARED commodity and polarity
    vocabularies rather than inferred classes, and a sentence-initial LEAD that
    keys only on corpus evidence;
  * a drift group whose severity tag does not map to a band is SKIPPED, never
    banded by guess;
  * a silence claim is only ever made from a read whose own coverage contract
    survives truncation (``get_run_health`` orders stalest-first, so its quiet
    set is complete even when the row cap bites) — see :func:`detect_silences`.

READS. Everything that a GRANTED PACK TOOL covers goes through the governed
binding (``binding.run_tool(name, args)`` → ``AgencyOutcome``), the same path
``journal_assessor._forced_honesty_flags`` and ``_lens_diff_matrix`` use, so
every read this pre-pass performs lands an ``action_pack_invocations`` row.
Exactly TWO reads have no tool and run as bounded leaf SQL on ``deps.pg_pool``:

  1. :data:`_LENS_ROWS_SQL` — the lens rows for the FULL lens roster. The
     governed ``get_lens_reads`` tool is deliberately fenced to
     ``LENS_DIFF_ROSTER_IDS`` (the four function-typed faculties) so the 09-21
     leans landing could not silently re-scope ``lens_diff``'s declared
     four-prior aperture. The crossroads mandate is "a country EVERY lens
     skipped", and answering it off four of ten lenses would report a country
     ``lens_left`` named as unnamed — a fabricated silence. The SQL is the
     port's own ``get_lens_reads`` statement, same predicate and same
     ``analyst_id = ANY($1::text[])`` shape, called with the full roster.
  2. :data:`_CONTENDED_SUBJECTS_SQL` — the arbiter's open contention subjects.
     No tool on ``journal_read`` or ``substrate_read`` returns a
     ``fact_contention`` row (``query_facts`` returns facts and does not expose
     the ``contested`` marker), and the CONTRADICTIONS detector is defined by
     the arbiter's ABSENCE. A single bounded ``subject_key = ANY($1::text[])``
     membership read over the unique ``(subject_key, predicate_key)`` index —
     never a per-row probe.

NEVER RAISES. :func:`pre_pass_block` is a pre-pass on a live analyst run: a
substrate blip, an unwired binding or a malformed row must cost the block, never
the run. Every read is individually guarded and names itself in the block's
``reads that failed`` line; every detector is individually guarded; the hook
itself is wrapped. The worst case is the not-run line.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .crossroads_vocab import (
    cited_mass,
    normalize,
    proper_candidates,
    title_polarity,
    title_terms,
)
from .deterministic_handlers.scorecard_banding import (
    BAND_LADDER,
    FAITH_FLOOR,
    SEVERITY_TO_BAND,
)

logger = logging.getLogger(__name__)

#: H12 — the METHOD/SCALE version this pre-pass's published arithmetic was
#: computed under. Bump when any threshold, vocabulary or rule below changes, so
#: a crossroads entry read a week later is a machine comparison rather than a
#: guess about when the detectors moved. Mirrors
#: ``scorecard_banding.METHOD_VERSION``'s contract.
METHOD_VERSION: str = "crossroads_detectors/2026-09.2"

# ---------------------------------------------------------------------------
# Tunables — every threshold the block's arithmetic quotes, in one place.
# ---------------------------------------------------------------------------

#: The day's window. The crossroads runs daily at 12:00Z after the lenses.
DEFAULT_WINDOW_HOURS: int = 24

#: PATTERNS: a term must be carried by at least this many DISTINCT desks.
#: Three is the design's number (§5) and it is the whole precision story — two
#: desks naming an actor is ordinary coverage, not a crossroads.
PATTERN_MIN_DESKS: int = 3

#: DRIFTS: consecutive same-direction band steps required. Needs
#: ``DRIFT_CYCLES + 1`` rows for that desk x target.
DRIFT_CYCLES: int = 3

#: SILENCES: how many cadences of quiet make a unit silent, and how long a
#: cadence is assumed to be when the roster carries no schedule.
SILENCE_CADENCES: int = 3
SILENCE_CADENCE_HOURS: int = 24

#: Row caps per detector kind — a chatty window must not crowd the block out of
#: the input budget (the same reason ``spread_block._DEFAULT_MAX_FRAMINGS``
#: exists). Rows are ordered strongest-first, so a cap only sheds the weakest.
MAX_ROWS_PER_KIND: int = 5

#: Read caps handed to the governed tools. The port clamps at 200 itself; naming
#: the number here is what lets the block state its own scope honestly.
FINDINGS_LIMIT: int = 200
ASSESSMENTS_LIMIT: int = 200
RUN_HEALTH_LIMIT: int = 200
LENS_ROWS_LIMIT: int = 60

#: Refs printed per row. A row's warrant must be checkable without the block
#: becoming a uuid dump.
MAX_REFS_PER_ROW: int = 6

#: Countries named in the single country-silence row before it says "and N more".
MAX_SILENT_COUNTRIES_NAMED: int = 12

#: The roster tags that define a "unit" for the SILENCES denominator — the same
#: three ``composition_slice._DESK_ROSTER_SQL`` uses, for the same reason: it is
#: the only roster in the tree whose job is naming ABSENCE, and a supply-chain
#: desk carries neither ``g20`` nor ``watch``.
ROSTER_TAGS: frozenset[str] = frozenset({"g20", "watch", "supply_chain"})

#: The country legs of that roster (a supply-chain lane is a unit, not a
#: country, so it cannot be "a country no lens named").
COUNTRY_ROSTER_TAGS: frozenset[str] = frozenset({"g20", "watch"})

#: The GOVERNED pack-tool reads this pre-pass makes. One list, two uses: the
#: single-binding fallback in :func:`_resolve_bindings` fans out over it, and
#: :attr:`CrossroadsSubstrate.any_read_succeeded` asks whether ANY of these
#: landed — which is the real question behind "did the detectors run". The two
#: leaf reads are per-detector inputs and are deliberately NOT part of that
#: answer: every detector needs at least one of these four, so a run where all
#: four failed measured nothing no matter what the leaves returned.
GOVERNED_READS: tuple[str, ...] = (
    "list_findings", "get_assessments", "get_run_health", "list_targets",
)


# ---------------------------------------------------------------------------
# The two leaf reads (no pack tool covers either — see the module docstring).
# ---------------------------------------------------------------------------

#: The port's own ``get_lens_reads`` statement, called with the FULL lens
#: roster. Same table, same predicate, same ``= ANY($1::text[])`` shape and the
#: same ``produced_at DESC`` order as the shipped query — only the id list is
#: wider (ten, not the governed tool's fenced four).
_LENS_ROWS_SQL = (
    "SELECT id, analyst_id, title, body, produced_at "
    "FROM journal_entries "
    "WHERE entry_kind = 'lens' AND analyst_id = ANY($1::text[]) "
    "  AND valid_until IS NULL "
    "  AND produced_at >= $2 "
    "ORDER BY produced_at DESC "
    "LIMIT $3"
)

#: The arbiter's OPEN contention subjects, as a bounded membership test over the
#: unique ``(subject_key, predicate_key)`` index (migration 0055). A
#: ``collapsed`` group is a dispute the arbiter CLOSED, so it does not count as
#: "the arbiter has this" for a live contradiction.
_CONTENDED_SUBJECTS_SQL = (
    "SELECT DISTINCT subject_key FROM fact_contention "
    "WHERE status <> 'collapsed' AND subject_key = ANY($1::text[])"
)


# ---------------------------------------------------------------------------
# Rendering constants.
# ---------------------------------------------------------------------------

_BLOCK_HEADER = (
    "CROSSROADS BLOCK (computed by code from today's substrate, not by you — "
    "narrate these rows and cite each by its own [N] ordinal; never invent a "
    "row, a desk, a count or a ref, and never restate an arithmetic this block "
    "did not compute):"
)

_NO_FIRE_LINE = (
    "  (no detector fired — the four detectors RAN over the scope above and "
    "none of their thresholds was met; this is a measured quiet window, not an "
    "unread one)"
)

_NOT_RUN_LINE = (
    "  (DETECTORS DID NOT RUN — no governed read surface was wired for this "
    "run, so nothing was measured. This is NOT evidence that no pattern, "
    "drift, contradiction or silence exists; say so plainly rather than "
    "narrating a quiet day)"
)

_INSTRUMENT_REF = "[[instrument]]"


# ---------------------------------------------------------------------------
# Row + substrate models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossroadsRow:
    """One detector hit: the arithmetic, its warrant, and the ledger row it
    proposes.

    ``ordinal`` is assigned by :func:`render_crossroads_block`, not by the
    detector — the block's ``[N]`` numbering is a property of the rendered
    block, exactly as ``spread_block``'s framing ordinals are the slice's.
    """

    kind: str
    subject: str
    desks: tuple[str, ...]
    arithmetic: str
    refs: tuple[str, ...]
    ledger_kind: str
    ledger_text: str
    resolution_test: str
    dispatch: str | None = None
    instrument_only: bool = False


@dataclass(frozen=True)
class CrossroadsSubstrate:
    """The rows the four PURE detectors run over, plus the scope that produced
    them. Built by :func:`fetch_crossroads_substrate`; a test builds one
    directly and gets a deterministic answer with no event loop and no DB."""

    window_start: datetime.datetime
    window_end: datetime.datetime
    findings: tuple[Mapping[str, Any], ...] = ()
    assessments: tuple[Mapping[str, Any], ...] = ()
    run_health: tuple[Mapping[str, Any], ...] = ()
    lens_rows: tuple[Mapping[str, Any], ...] = ()
    lens_ids_expected: tuple[str, ...] = ()
    roster: tuple[Mapping[str, Any], ...] = ()
    #: ``target_id -> name`` over EVERY active head target, not just the tagged
    #: roster above — the CONTRADICTIONS leg needs a name for whatever target a
    #: finding carries, and the arbiter's subject_key is a name (see
    #: :func:`detect_contradictions`). Kept as pairs so the frozen row stays
    #: comparable; :func:`target_name_map` rebuilds the dict.
    target_names: tuple[tuple[str, str], ...] = ()
    contended_subject_keys: frozenset[str] = frozenset()
    reads_ok: tuple[str, ...] = ()
    reads_failed: tuple[str, ...] = ()

    @property
    def any_read_succeeded(self) -> bool:
        """Did anything the detectors need actually get read?

        Scoped to :data:`GOVERNED_READS` on purpose. A leaf probe that ran but
        had nothing to look up ("no subject names, so no contention to check")
        is a vacuous success, and letting it flip this flag would let a run with
        a dead pool and a dead binding render "a measured quiet window" — the
        one sentence this block exists to make impossible.
        """
        return bool(set(self.reads_ok) & set(GOVERNED_READS))

    @property
    def target_name_map(self) -> dict[str, str]:
        return dict(self.target_names)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime.datetime | None:
    """Best-effort ISO/datetime -> aware UTC datetime. Same precedence and same
    naive-is-UTC rule as ``spread_block._row_timestamp``; unresolvable is
    ``None`` and is EXCLUDED from arithmetic, never defaulted to now."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def is_verified(row: Mapping[str, Any]) -> bool:
    """A claim is VERIFIED when a faithfulness verdict exists for it and clears
    :data:`scorecard_banding.FAITH_FLOOR`.

    Reuses the scorecard engine's own dedicated faithfulness floor rather than
    inventing a second one — a crossroads that banded convergence on claims the
    band engine excludes would be two instruments disagreeing about what
    "verified" means. A row with NO critic score is NOT verified (absence of a
    verdict is not a pass).
    """
    score = row.get("critic_score")
    if score is None:
        return False
    try:
        return float(score) >= FAITH_FLOOR
    except (TypeError, ValueError):
        return False


def band_rank(severity: Any) -> int | None:
    """The severity tag's rung on :data:`scorecard_banding.BAND_LADDER`, or
    ``None`` for a severity the band engine does not map — which SKIPS the
    group rather than banding it by guess."""
    band = SEVERITY_TO_BAND.get(str(severity or "").strip().lower())
    if band is None or band not in BAND_LADDER:
        return None
    return BAND_LADDER.index(band)


def _refs(ids: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for value in ids:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_REFS_PER_ROW:
            break
    return tuple(out)


def _desk(row: Mapping[str, Any]) -> str:
    return str(row.get("analyst_id") or "").strip()


# ---------------------------------------------------------------------------
# (a) PATTERNS
# ---------------------------------------------------------------------------


def detect_patterns(
    findings: Sequence[Mapping[str, Any]],
    *,
    min_desks: int = PATTERN_MIN_DESKS,
    max_rows: int = MAX_ROWS_PER_KIND,
) -> list[CrossroadsRow]:
    """One entity or commodity term across >= ``min_desks`` DISTINCT desks'
    VERIFIED findings.

    Pure and synchronous over already-fetched rows. Ordered by desk count
    descending then term, so a cap only ever sheds the weakest convergence.

    TWO PASSES, for the lead problem :func:`_proper_candidates` documents: pass
    one collects every token the window's titles capitalise MID-SENTENCE, pass
    two extracts terms with that set as the lead allowlist. So ``Russia steps up
    strikes`` keys on ``russia`` exactly when some other title in the same
    window wrote ``… strikes inside Russia`` — corpus evidence for the capital —
    and ``Talks collapse in Geneva`` never keys on ``talks``.
    """
    verified = [r for r in findings if is_verified(r) and _desk(r)]
    confirmed_leads: set[str] = set()
    for row in verified:
        confirmed_leads |= proper_candidates(row.get("title"))[0]

    by_term_desks: dict[str, set[str]] = {}
    by_term_refs: dict[str, list[str]] = {}
    by_term_count: dict[str, int] = {}
    for row in verified:
        desk = _desk(row)
        for term in title_terms(row.get("title"), confirmed_leads=confirmed_leads):
            by_term_desks.setdefault(term, set()).add(desk)
            by_term_refs.setdefault(term, []).append(str(row.get("id") or ""))
            by_term_count[term] = by_term_count.get(term, 0) + 1

    rows: list[CrossroadsRow] = []
    for term, desks in by_term_desks.items():
        if len(desks) < min_desks:
            continue
        ordered = tuple(sorted(desks))
        hits = by_term_count.get(term, 0)
        rows.append(CrossroadsRow(
            kind="pattern",
            subject=term,
            desks=ordered,
            arithmetic=(
                f"{len(ordered)} distinct desks ({', '.join(ordered)}) carry "
                f"\"{term}\" across {hits} verified findings in this window "
                f"(threshold: >= {min_desks} desks; verified = a faithfulness "
                f"verdict >= {FAITH_FLOOR})"
            ),
            refs=_refs(by_term_refs.get(term, [])),
            ledger_kind="observation",
            ledger_text=(
                f"\"{term}\" is carried by {len(ordered)} desks this window "
                f"({', '.join(ordered)}); no single desk's brief covers the "
                f"convergence, so no desk can state it."
            ),
            resolution_test=(
                f"ANTICIPATED if a later finding cites any ref above while "
                f"naming \"{term}\"; EXPIRED if no desk names \"{term}\" in "
                f"the next {SILENCE_CADENCES} cadences."
            ),
        ))
    rows.sort(key=lambda r: (-len(r.desks), r.subject))
    return rows[:max_rows]


# ---------------------------------------------------------------------------
# (b) DRIFTS
# ---------------------------------------------------------------------------


def detect_drifts(
    assessments: Sequence[Mapping[str, Any]],
    *,
    cycles: int = DRIFT_CYCLES,
    max_rows: int = MAX_ROWS_PER_KIND,
) -> list[CrossroadsRow]:
    """A desk x target whose band moved ``cycles`` consecutive cycles in ONE
    direction while its CITED MASS did not follow.

    The mass leg is the whole point. A band that climbs on growing evidence is a
    desk doing its job; a band that climbs on the SAME evidence, three cycles
    running, is a reading changing without the record changing — and nothing
    else in the platform is positioned to notice, because each desk sees only
    its own cycle.

    A group with fewer than ``cycles + 1`` rows, or one carrying a severity the
    band engine does not map, is SKIPPED — never banded by guess.
    """
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in assessments:
        desk = _desk(row)
        target = str(row.get("target_id") or "").strip()
        if not desk or not target:
            continue
        groups.setdefault((desk, target), []).append(row)

    rows: list[CrossroadsRow] = []
    for (desk, target), members in groups.items():
        dated = [(ts, m) for m in members if (ts := _parse_ts(m.get("produced_at")))]
        if len(dated) < cycles + 1:
            continue
        dated.sort(key=lambda pair: pair[0])
        window = [m for _, m in dated[-(cycles + 1):]]
        ranks = [band_rank(m.get("severity")) for m in window]
        if any(r is None for r in ranks):
            continue
        steps = [
            int(ranks[i + 1]) - int(ranks[i])  # type: ignore[arg-type]
            for i in range(len(ranks) - 1)
        ]
        if not steps or any(s == 0 for s in steps):
            continue
        direction = 1 if steps[0] > 0 else -1
        if any((1 if s > 0 else -1) != direction for s in steps):
            continue
        masses = [cited_mass(m.get("body")) for m in window]
        mass_delta = masses[-1] - masses[0]
        mass_direction = 0 if mass_delta == 0 else (1 if mass_delta > 0 else -1)
        if mass_direction == direction:
            continue  # evidence moved WITH the band — a desk doing its job.
        bands = " -> ".join(BAND_LADDER[int(r)] for r in ranks)  # type: ignore[arg-type]
        moved = "up" if direction > 0 else "down"
        mass_phrase = (
            "unchanged" if mass_delta == 0
            else f"moved {mass_delta:+d}, against the band"
        )
        rows.append(CrossroadsRow(
            kind="drift",
            subject=f"{desk} on {target}",
            desks=(desk,),
            arithmetic=(
                f"band {bands} ({cycles} consecutive {moved} steps); cited mass "
                f"{masses[0]} -> {masses[-1]} ({mass_phrase}); method "
                f"{METHOD_VERSION}"
            ),
            refs=_refs(m.get("id") for m in window),
            ledger_kind="hypothesis",
            ledger_text=(
                f"{desk}'s band on {target} has moved {moved} for {cycles} "
                f"consecutive cycles while the evidence it cites has not "
                f"followed: the movement is a change of reading, not a change "
                f"in the record."
            ),
            resolution_test=(
                f"REFUTED if {desk}'s next row on {target} adds cited refs "
                f"(the evidence caught up); CONFIRMED if the band moves "
                f"{moved} again on the same or fewer cited refs. Frozen at "
                f"write."
            ),
        ))
    rows.sort(key=lambda r: (r.desks[0], r.subject))
    return rows[:max_rows]


# ---------------------------------------------------------------------------
# (c) CONTRADICTIONS
# ---------------------------------------------------------------------------


def contention_subject_key(subject: Any) -> str:
    """The arbiter's own ``subject_key`` rule: ``lower(subject)``, trimmed
    (migration 0055). Keyed identically here so "the arbiter has no contention
    for this" is a real membership test and not a near-miss."""
    return str(subject or "").strip().lower()


def detect_contradictions(
    findings: Sequence[Mapping[str, Any]],
    contended_subject_keys: frozenset[str] | set[str] = frozenset(),
    *,
    target_names: Mapping[str, str] | None = None,
    max_rows: int = MAX_ROWS_PER_KIND,
) -> list[CrossroadsRow]:
    """Two DIFFERENT desks' verified claims on the same subject with opposite
    polarity that the arbiter holds no contention for.

    The subject is the TARGET the two findings share — the one subject identity
    both desks demonstrably agree they are writing about, rather than a term
    match that could pair two unrelated sentences.

    THE ARBITER KEY IS THE TARGET'S NAME, NOT ITS SLUG, and the distinction is
    load-bearing rather than cosmetic. ``fact_contention.subject_key`` is
    ``lower(subject)`` over the FACTS table (migration 0055), and a fact's
    subject is an entity as written — ``iran``, never ``country_watch_ir``. A
    detector that probed the slug would get "no contention" for every subject on
    earth and fire on every opposed pair the arbiter is ALREADY adjudicating,
    which is the precise noise this leg exists to avoid. ``target_names`` is the
    ``target_id -> name`` map ``list_targets`` returns; a target it cannot name
    is SKIPPED, because the arbiter condition cannot be checked for it and
    firing on an unchecked condition is how a false contradiction gets written.

    ``contended_subject_keys`` is the arbiter's OPEN subject set
    (:data:`_CONTENDED_SUBJECTS_SQL`); a subject the arbiter already holds is
    NOT a crossroads finding — the machine is on it, and re-raising it is work
    the operator has to triage twice.
    """
    contended = {contention_subject_key(k) for k in contended_subject_keys}
    names = {str(k): str(v) for k, v in (target_names or {}).items() if v}
    by_target: dict[str, list[Mapping[str, Any]]] = {}
    for row in findings:
        if not is_verified(row):
            continue
        target = str(row.get("target_id") or "").strip()
        if not target or not _desk(row):
            continue
        by_target.setdefault(target, []).append(row)

    rows: list[CrossroadsRow] = []
    for target, members in by_target.items():
        subject_name = names.get(target, "")
        if not subject_name:
            continue  # unnameable subject -> the arbiter check is impossible.
        subject_key = contention_subject_key(subject_name)
        if subject_key in contended:
            continue
        positives = [m for m in members if title_polarity(m.get("title")) > 0]
        negatives = [m for m in members if title_polarity(m.get("title")) < 0]
        pair = _opposed_pair(positives, negatives)
        if pair is None:
            continue
        up, down = pair
        desks = (_desk(up), _desk(down))
        rows.append(CrossroadsRow(
            kind="contradiction",
            subject=f"{subject_name} ({target})",
            desks=desks,
            arithmetic=(
                f"{desks[0]} reads {subject_name} escalatory and {desks[1]} "
                f"reads it de-escalatory in the same window, both verified "
                f"(>= {FAITH_FLOOR}); the arbiter holds NO open contention "
                f"keyed on subject_key '{subject_key}'"
            ),
            refs=_refs([up.get("id"), down.get("id")]),
            ledger_kind="question",
            ledger_text=(
                f"Two desks read {subject_name} in opposite directions this "
                f"window and nothing in the platform reconciles them: "
                f"{desks[0]} vs {desks[1]}."
            ),
            resolution_test=(
                f"ANSWERED when the arbiter opens a contention on "
                f"'{subject_key}', or when one of the two findings is "
                f"superseded. Frozen at write."
            ),
            dispatch=(
                "propose_correction(diff={\"op\": \"correct_situation\", "
                f"\"subject\": \"{subject_name}\", \"claims\": "
                f"[\"{str(up.get('id') or '')}\", \"{str(down.get('id') or '')}\"]}}, "
                "rationale=...) — the existing journal_propose shape. NOTE: "
                "this descriptor does NOT grant journal_propose, so record the "
                "question in the ledger with dispatched_to unset and say the "
                "dispatch is pending a grant."
            ),
        ))
    rows.sort(key=lambda r: r.subject)
    return rows[:max_rows]


def _opposed_pair(
    positives: Sequence[Mapping[str, Any]],
    negatives: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """The strongest opposed pair from DIFFERENT desks, or ``None``.

    Strength is the folded ``effective_confidence`` (the critic-folded number
    ``list_findings`` already returns) — the pair the platform itself is most
    confident about, so a weak pairing can never outrank a strong one when the
    row cap bites. Two findings from the SAME desk are not a contradiction
    between desks; they are one desk changing its mind, which is that desk's
    own supersession chain and not a crossroads.
    """
    def strength(row: Mapping[str, Any]) -> float:
        value = row.get("effective_confidence")
        if value is None:
            value = row.get("confidence")
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    best: tuple[float, Mapping[str, Any], Mapping[str, Any]] | None = None
    for up in positives:
        for down in negatives:
            if _desk(up) == _desk(down):
                continue
            score = strength(up) + strength(down)
            if best is None or score > best[0]:
                best = (score, up, down)
    if best is None:
        return None
    return best[1], best[2]


# ---------------------------------------------------------------------------
# (d) SILENCES
# ---------------------------------------------------------------------------


def detect_silences(
    run_health: Sequence[Mapping[str, Any]],
    lens_rows: Sequence[Mapping[str, Any]],
    roster: Sequence[Mapping[str, Any]],
    *,
    cadences: int = SILENCE_CADENCES,
    cadence_hours: int = SILENCE_CADENCE_HOURS,
    max_rows: int = MAX_ROWS_PER_KIND,
) -> list[CrossroadsRow]:
    """A roster unit quiet for ``cadences`` cadences, or a roster country no
    lens entry named in the window.

    WHY THE UNIT LEG SURVIVES A TRUNCATED READ. ``get_run_health`` orders its
    per-analyst heads STALEST-FIRST precisely so a row cap can only ever shed
    the freshest, healthiest analysts (W2-T6) — so the quiet set it returns is
    complete even when ``truncated`` is true, and a silence claim built on it is
    not a claim about rows nobody read.

    WHY THE UNIT LEG CARRIES NO REF. A run-health row is keyed by ``run_id``,
    not by a citeable substrate row, and the port returns ``refs: []`` for
    exactly that reason. Those rows are marked ``instrument_only`` and render
    ``[[instrument]]`` — the journal_read pack's own rule for an instrument read
    with no row id. Never a fabricated ``[[ref:...]]``.

    THE COUNTRY LEG IS ONE ROW, not one per country: "which countries did every
    lens skip" is a single observation about the chorus's aperture, and twenty
    rows saying the same thing would crowd out the other three detectors.
    """
    quiet_hours = cadences * cadence_hours
    rows: list[CrossroadsRow] = []

    for entry in sorted(
        run_health,
        key=lambda r: (-(_hours_ago(r) or float("inf")), str(r.get("analyst_id") or "")),
    ):
        analyst = str(entry.get("analyst_id") or "").strip()
        if not analyst:
            continue
        hours = _hours_ago(entry)
        if hours is not None and hours <= quiet_hours:
            continue
        seen = (
            "no resolvable last run" if hours is None
            else f"last run {hours:.1f}h ago"
        )
        rows.append(CrossroadsRow(
            kind="silence",
            subject=analyst,
            desks=(analyst,),
            arithmetic=(
                f"{analyst}: {seen}; threshold {cadences} cadences = "
                f"{quiet_hours}h (run-health heads are ordered stalest-first, "
                f"so this set is complete even under the row cap)"
            ),
            refs=(),
            instrument_only=True,
            ledger_kind="observation",
            ledger_text=(
                f"{analyst} has not produced a read for {cadences} cadences; "
                f"whatever it covers is uncovered, and a silence is a finding."
            ),
            resolution_test=(
                f"CONFIRMED if {analyst} is still quiet at the next crossroads; "
                f"ANSWERED the cycle it produces a read. Frozen at write."
            ),
        ))
        if len(rows) >= max_rows - 1:
            break

    country_row = _country_silence_row(lens_rows, roster)
    if country_row is not None:
        rows.append(country_row)
    return rows[:max_rows]


def _hours_ago(entry: Mapping[str, Any]) -> float | None:
    value = entry.get("hours_ago")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _country_silence_row(
    lens_rows: Sequence[Mapping[str, Any]],
    roster: Sequence[Mapping[str, Any]],
) -> CrossroadsRow | None:
    """The single country-leg silence row, or ``None`` when every roster country
    was named (or when there is nothing to compare).

    A country counts as NAMED when its human name appears as a whole phrase in
    any lens row's title or body. The name — not the slug: a lens writes
    "Pakistan", never ``country_watch_pk``, and keying on the slug would report
    every country silent forever (the ``unit_names`` defect, from the other
    side). A roster row with no usable name is EXCLUDED from the denominator
    rather than counted as silent.
    """
    countries = [
        (str(t.get("target_id") or ""), str(t.get("name") or "").strip())
        for t in roster
        if COUNTRY_ROSTER_TAGS & {str(tag).lower() for tag in (t.get("tags") or [])}
    ]
    named_countries = [(tid, name) for tid, name in countries if len(name) >= 3]
    if not named_countries or not lens_rows:
        return None

    corpus = " ".join(
        f" {normalize(row.get('title'))} {normalize(row.get('body'))} "
        for row in lens_rows
    )
    silent = [
        (tid, name) for tid, name in named_countries
        if f" {normalize(name)} " not in f" {corpus} "
    ]
    if not silent:
        return None

    shown = [name for _, name in silent[:MAX_SILENT_COUNTRIES_NAMED]]
    more = len(silent) - len(shown)
    listed = ", ".join(shown) + (f", and {more} more" if more > 0 else "")
    lens_ids = sorted({str(r.get("analyst_id") or "") for r in lens_rows} - {""})
    return CrossroadsRow(
        kind="silence",
        subject="countries no lens named",
        desks=tuple(lens_ids),
        arithmetic=(
            f"{len(silent)} of {len(named_countries)} roster countries are "
            f"named by none of the {len(lens_rows)} lens entries read this "
            f"window ({', '.join(lens_ids) or 'no lens ids'}): {listed}"
        ),
        refs=_refs(r.get("id") for r in lens_rows),
        ledger_kind="observation",
        ledger_text=(
            f"The chorus's aperture skipped {len(silent)} roster countries this "
            f"window; the lens tier's silence about them is not the same as the "
            f"roster's silence about them."
        ),
        resolution_test=(
            "ANSWERED when a lens entry names one of these countries; "
            "CONFIRMED if the same countries are unnamed at the next "
            "crossroads. Frozen at write."
        ),
    )


# ---------------------------------------------------------------------------
# The four detectors, each guarded
# ---------------------------------------------------------------------------


def run_detectors(substrate: CrossroadsSubstrate) -> list[CrossroadsRow]:
    """Run all four detectors over ``substrate``, in the mandate's order.

    Each detector is individually guarded: a malformed row in the drift groups
    must not cost the pattern rows that already computed cleanly. A detector
    that raises contributes nothing and logs; it never propagates.
    """
    rows: list[CrossroadsRow] = []
    attempts = (
        ("patterns", lambda: detect_patterns(substrate.findings)),
        ("drifts", lambda: detect_drifts(substrate.assessments)),
        ("contradictions", lambda: detect_contradictions(
            substrate.findings,
            substrate.contended_subject_keys,
            target_names=substrate.target_name_map,
        )),
        ("silences", lambda: detect_silences(
            substrate.run_health, substrate.lens_rows, substrate.roster,
        )),
    )
    for name, detector in attempts:
        try:
            rows.extend(detector())
        except Exception as exc:  # noqa: BLE001 — one detector never sinks four
            logger.warning("crossroads.detector_failed detector=%s err=%s", name, exc)
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _scope_line(substrate: CrossroadsSubstrate) -> str:
    lens_seen = len({str(r.get("analyst_id") or "") for r in substrate.lens_rows} - {""})
    failed = set(substrate.reads_failed)
    # A read that FAILED is not a read of zero rows: the first forced run
    # (2026-09-24) printed "findings read 0 (cap 200)" beside "READS THAT
    # FAILED: list_findings", and the model's ledger hypothesis became "a
    # systemic pause caused every desk to produce no findings". The count is
    # withheld when the read failed, so nothing on this line can be mistaken
    # for a measurement.
    parts = [
        f"window {substrate.window_start.isoformat()} .. "
        f"{substrate.window_end.isoformat()}",
        ("findings UNMEASURED (list_findings read failed)"
         if "list_findings" in failed
         else f"findings read {len(substrate.findings)} (cap {FINDINGS_LIMIT})"),
        ("assessments UNMEASURED (get_assessments read failed)"
         if "get_assessments" in failed
         else f"assessments read {len(substrate.assessments)} (cap {ASSESSMENTS_LIMIT})"),
        f"run-health heads {len(substrate.run_health)}",
        f"lens ids seen {lens_seen} of {len(substrate.lens_ids_expected)}",
        f"roster targets {len(substrate.roster)}",
        f"method {METHOD_VERSION}",
    ]
    line = "  SCOPE: " + " | ".join(parts)
    if substrate.reads_failed:
        line += (
            "\n  READS THAT FAILED (their detectors are UNMEASURED, not quiet): "
            + ", ".join(substrate.reads_failed)
        )
    return line


def render_crossroads_block(
    rows: Sequence[CrossroadsRow], substrate: CrossroadsSubstrate,
) -> str:
    """The rendered block. ``[N]`` ordinals are assigned HERE, contiguously from
    1 in row order, and they are the only citation handles the mandate lets the
    model use for these numbers.

    The two empties render DIFFERENTLY on purpose (see the module docstring):
    a substrate with no successful read gets :data:`_NOT_RUN_LINE`, a substrate
    that read cleanly and matched nothing gets :data:`_NO_FIRE_LINE`.
    """
    lines = [_BLOCK_HEADER, _scope_line(substrate)]
    if not substrate.any_read_succeeded:
        lines.append(_NOT_RUN_LINE)
        return "\n".join(lines)
    if not rows:
        lines.append(_NO_FIRE_LINE)
        return "\n".join(lines)
    for ordinal, row in enumerate(rows, start=1):
        refs = (
            _INSTRUMENT_REF if row.instrument_only
            else (", ".join(row.refs) if row.refs else "(none)")
        )
        lines.append(f"  [{ordinal}] {row.kind.upper()} — {row.subject}")
        lines.append(f"        arithmetic: {row.arithmetic}")
        lines.append(f"        refs: {refs}")
        lines.append(
            f"        LEDGER ({row.ledger_kind}): {row.ledger_text}"
        )
        lines.append(f"        RESOLUTION TEST: {row.resolution_test}")
        if row.dispatch:
            lines.append(f"        DISPATCH: {row.dispatch}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fetching — the governed binding first, two declared leaf reads second
# ---------------------------------------------------------------------------


async def _run_tool(
    bindings: Mapping[str, Any], name: str, args: Mapping[str, Any],
) -> dict[str, Any] | None:
    """One governed pack-tool read, or ``None``.

    Same admit / status / output triage as
    ``journal_assessor._forced_honesty_flags`` — an un-admitted call, a failed
    ToolResult and an exception all degrade to ``None`` and the caller names the
    read in the block's failed-reads line.
    """
    binding = bindings.get(name)
    if binding is None:
        return None
    try:
        outcome = await binding.run_tool(name, dict(args))
        if (
            not outcome.admitted
            or outcome.tool_result is None
            or outcome.tool_result.status == "failed"
        ):
            logger.warning(
                "crossroads.tool_read_refused tool=%s cause=%s",
                name, getattr(outcome, "block_cause", None),
            )
            return None
        return dict(outcome.tool_result.output)
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop
        logger.warning("crossroads.tool_read_failed tool=%s err=%s", name, exc)
        return None


def _rows_of(payload: Mapping[str, Any] | None, key: str = "rows") -> tuple[Any, ...]:
    if not payload:
        return ()
    value = payload.get(key)
    return tuple(value) if isinstance(value, list) else ()


async def fetch_crossroads_substrate(
    *,
    tool_bindings: Mapping[str, Any],
    pg: Any,
    lens_analyst_ids: Sequence[str],
    now: datetime.datetime,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> CrossroadsSubstrate:
    """Read the day's substrate. Never raises; each read names itself.

    ``tool_bindings`` is the per-tool ``AgencyToolBinding`` map the kind already
    builds for GATHER (``options['gather_tool_bindings']``) — using it means the
    pre-pass reads through exactly the grants the descriptor declares, and every
    read lands an ``action_pack_invocations`` row.
    """
    window_start = now - datetime.timedelta(hours=window_hours)
    ok: list[str] = []
    failed: list[str] = []

    def _record(name: str, payload: Any) -> Any:
        (ok if payload is not None else failed).append(name)
        return payload

    findings = _rows_of(_record("list_findings", await _run_tool(
        tool_bindings, "list_findings",
        {"since_hours": window_hours, "limit": FINDINGS_LIMIT},
    )))
    # DRIFTS needs (cycles + 1) cycles of history, not one window.
    assessments = _rows_of(_record("get_assessments", await _run_tool(
        tool_bindings, "get_assessments",
        {
            "since_hours": window_hours * (DRIFT_CYCLES + 1),
            "limit": ASSESSMENTS_LIMIT,
        },
    )))
    run_health = _rows_of(_record("get_run_health", await _run_tool(
        tool_bindings, "get_run_health",
        {
            "quiet_hours": SILENCE_CADENCES * SILENCE_CADENCE_HOURS,
            "limit": RUN_HEALTH_LIMIT,
        },
    )))
    all_targets = _rows_of(_record("list_targets", await _run_tool(
        tool_bindings, "list_targets", {"active_only": True},
    )))
    all_targets = tuple(t for t in all_targets if isinstance(t, Mapping))
    # Names over EVERY active target (the contradictions leg), the tagged subset
    # for the silences denominator — two different questions, two scopes.
    target_names = target_name_pairs(all_targets)
    roster = tuple(
        t for t in all_targets
        if ROSTER_TAGS & {str(tag).lower() for tag in (t.get("tags") or [])}
    )

    lens_rows = _record(
        "journal_entries.lens (leaf)",
        await _fetch_lens_rows(pg, lens_analyst_ids, window_start),
    ) or ()
    # The arbiter probe is keyed on the target's NAME, the shape
    # ``fact_contention.subject_key`` actually holds — see
    # :func:`detect_contradictions`.
    names = dict(target_names)
    subjects = [
        names.get(str(f.get("target_id") or ""), "")
        for f in findings if isinstance(f, Mapping)
    ]
    contended: frozenset[str] | None = frozenset()
    if any(subjects):
        # Only recorded when it had something to look up — a probe with no
        # subjects is not a read that says anything about the surface.
        contended = _record(
            "fact_contention (leaf)",
            await _fetch_contended_subjects(pg, subjects),
        )

    return CrossroadsSubstrate(
        window_start=window_start,
        window_end=now,
        findings=tuple(f for f in findings if isinstance(f, Mapping)),
        assessments=tuple(a for a in assessments if isinstance(a, Mapping)),
        run_health=tuple(r for r in run_health if isinstance(r, Mapping)),
        lens_rows=tuple(lens_rows),
        lens_ids_expected=tuple(lens_analyst_ids),
        roster=roster,
        target_names=target_names,
        contended_subject_keys=frozenset(contended or ()),
        reads_ok=tuple(ok),
        reads_failed=tuple(failed),
    )


def target_name_pairs(
    targets: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    """``list_targets`` rows -> sorted ``(target_id, name)`` pairs, dropping any
    row with no usable name. Sorted so a substrate built from the same read is
    byte-identical run to run."""
    pairs = {
        str(t.get("target_id") or "").strip(): str(t.get("name") or "").strip()
        for t in targets
        if str(t.get("target_id") or "").strip() and str(t.get("name") or "").strip()
    }
    return tuple(sorted(pairs.items()))


async def _fetch_lens_rows(
    pg: Any, lens_analyst_ids: Sequence[str], since: datetime.datetime,
) -> tuple[Mapping[str, Any], ...] | None:
    """LEAF READ 1 (see the module docstring for why no tool covers it)."""
    ids = sorted({str(a) for a in lens_analyst_ids if a})
    if pg is None or not ids:
        return None
    try:
        records = await pg.fetch(_LENS_ROWS_SQL, ids, since, LENS_ROWS_LIMIT)
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop
        logger.warning("crossroads.lens_rows_failed err=%s", exc)
        return None
    return tuple(
        {
            "id": str(r["id"]),
            "analyst_id": r["analyst_id"],
            "title": r["title"],
            "body": r["body"],
            "produced_at": r["produced_at"],
        }
        for r in records
    )


async def _fetch_contended_subjects(
    pg: Any, subjects: Sequence[str],
) -> frozenset[str] | None:
    """LEAF READ 2 (see the module docstring). A bounded ``= ANY`` membership
    test over the unique ``(subject_key, predicate_key)`` index — NOT a
    per-row probe.

    ``None`` means the probe COULD NOT RUN (no pool, or the query raised);
    an empty frozenset means it ran and the arbiter holds none of these
    subjects. Nothing to probe is the second case, not the first — the null /
    ``[]`` distinction the whole block is built around.
    """
    if pg is None:
        return None
    keys = sorted({contention_subject_key(s) for s in subjects} - {""})
    if not keys:
        return frozenset()
    try:
        records = await pg.fetch(_CONTENDED_SUBJECTS_SQL, keys)
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop
        logger.warning("crossroads.contention_read_failed err=%s", exc)
        return None
    return frozenset(str(r["subject_key"]) for r in records)


# ---------------------------------------------------------------------------
# THE SEAM — the hook the inquiry kind resolves from
# ``method.options.pre_pass_module``.
# ---------------------------------------------------------------------------


def _resolve_bindings(options: Mapping[str, Any], deps: Any) -> dict[str, Any]:
    """The per-tool governed bindings this pre-pass reads through.

    Resolution order, widest contract first, so the hook works against whatever
    the kind wires: ``options['gather_tool_bindings']`` (the map
    ``journal_assessor`` already threads into GATHER) is the primary; a single
    ``options['agency_binding']`` / ``deps.agency_binding`` is folded in as a
    fallback for every tool name this pre-pass reads, since one pack binding can
    dispatch every tool its pack grants.
    """
    bindings: dict[str, Any] = {}
    raw = options.get("gather_tool_bindings")
    if isinstance(raw, Mapping):
        bindings.update({str(k): v for k, v in raw.items() if v is not None})
    fallback = options.get("agency_binding") or getattr(deps, "agency_binding", None)
    if fallback is not None:
        for name in GOVERNED_READS:
            bindings.setdefault(name, fallback)
    return bindings


async def pre_pass_block(options: Mapping[str, Any], deps: Any) -> str | None:
    """THE SEAM. Returns the rendered crossroads block for the inquiry kind to
    prepend to its user prompt.

    Contract with the kind module (lane p5_kind resolves this from
    ``method.options.pre_pass_module``):

      * it is awaited ONCE, before the prompt is rendered;
      * it reads ``options['gather_tool_bindings']`` (per-tool
        ``AgencyToolBinding``) and ``deps.pg_pool``; every other option is
        optional (``window_hours`` overrides the 24h day);
      * it returns a STRING on every path — including both empties, which are
        deliberately different strings — and NEVER raises. A caller that gets
        ``None`` has hit the one case where the hook itself could not even build
        a degrade line, and should render no block rather than a fabricated one.

    It writes nothing. The ledger rows it names are PROPOSALS the kind's REFLECT
    phase may write through the ``inquiry_state`` pack; this module has no write
    surface and no pack grant of its own.
    """
    try:
        opts: Mapping[str, Any] = options if isinstance(options, Mapping) else {}
        try:
            window_hours = int(opts.get("window_hours") or DEFAULT_WINDOW_HOURS)
        except (TypeError, ValueError):
            window_hours = DEFAULT_WINDOW_HOURS
        now = _parse_ts(opts.get("now")) or datetime.datetime.now(
            datetime.timezone.utc
        )
        substrate = await fetch_crossroads_substrate(
            tool_bindings=_resolve_bindings(opts, deps),
            pg=getattr(deps, "pg_pool", None),
            lens_analyst_ids=_lens_roster(),
            now=now,
            window_hours=max(1, window_hours),
        )
        rows = run_detectors(substrate)
        logger.info(
            "crossroads.pre_pass rows=%d reads_ok=%d reads_failed=%d",
            len(rows), len(substrate.reads_ok), len(substrate.reads_failed),
        )
        return render_crossroads_block(rows, substrate)
    except Exception as exc:  # noqa: BLE001 — a pre-pass never sinks a run
        logger.warning("crossroads.pre_pass_failed err=%s", exc)
        return f"{_BLOCK_HEADER}\n{_NOT_RUN_LINE}"


def _lens_roster() -> tuple[str, ...]:
    """Every lens id on the journal_assessor kind — the FULL roster, which is
    what "a country every lens skipped" requires. Imported at CALL TIME (the
    same late-import discipline ``spread_block._it`` and the journal_read pack's
    own ``get_lens_reads`` handler use) so module load stays acyclic."""
    try:
        from .journal_assessor import LENS_ANALYST_IDS

        return tuple(LENS_ANALYST_IDS)
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop
        logger.warning("crossroads.lens_roster_import_failed err=%s", exc)
        return ()


__all__ = [
    "CrossroadsRow",
    "CrossroadsSubstrate",
    "DRIFT_CYCLES",
    "METHOD_VERSION",
    "PATTERN_MIN_DESKS",
    "SILENCE_CADENCES",
    "band_rank",
    "cited_mass",
    "contention_subject_key",
    "detect_contradictions",
    "detect_drifts",
    "detect_patterns",
    "detect_silences",
    "fetch_crossroads_substrate",
    "is_verified",
    "pre_pass_block",
    "render_crossroads_block",
    "run_detectors",
    "target_name_pairs",
    "title_polarity",
    "title_terms",
]
