"""THE ASSESSMENT CHANNEL — the voice that survives, honestly priced (D-1 §2).

Operator ruling 1: the Assessment survives from day one as a clearly-labelled
channel with a live accuracy badge, generated FROM the assembled spine, graded on
fidelity-to-its-own-spine as its own population.

WHAT THIS IS, STRUCTURALLY. A NEW ``analyst_id`` (``world_assessment``) under the
EXISTING ``identity.kind: meta_findings_synthesizer``, writing an ordinary
``kind='finding'`` row whose ``derived_from`` is EXACTLY ONE element: the
assembly it was written from. That single-element array IS the enforcement of
"input = the assembly payload only" — a machine-checkable proof no prompt clause
can provide, and the same shape as the journal's forced-empty ``derived_from``.

WHY THE KIND CHOICE MAKES FOUR INTEGRATIONS FREE (§2.1b, each re-verified here):

  1. **Verify dispatch: zero edits.** ``dapr_actors``' kind allowlist already
     admits ``meta_findings_synthesizer`` when the descriptor declares verify.
     That file is at its ceiling with no seam, so a clause that had to be added
     there would have blocked this train outright.
  2. **Rubric routing: zero edits.** ``verify._uses_subclaim_convention`` picks
     the composition rubric from the PAYLOAD — true iff a citation carries
     ``ref_kind == "finding"`` or a ``[[ref:`` marker. This channel cites its
     spine that way by construction.
  3. **Always-judged: zero edits.** ``JUDGE_SAMPLE_ALWAYS_DEFAULT`` matches on
     the analyst KIND (or id); the Assessment is never sampled out.
  4. **Read path: zero edits.** ``/api/v1/findings`` filters ``kind='finding'``
     and takes ``analyst_id``.

THE EVIDENCE MAP IS THE RECORD, AND THAT IS THE WHOLE GRADING STORY.
``faithfulness_score = supported_claims / checkable_claims`` against an evidence
map built from ``citations[].evidence_text``. This channel mints one citation per
CITED SPINE ORDINAL whose ``evidence_text`` is that block's quoted span **plus the
record's own arithmetic** — so the existing faithfulness machinery grades
fidelity-to-spine with no new rubric and no new arm. That is §3.5's re-pointing,
done at the producer where it costs nothing, rather than at verify where it would
cost a stamp.

The arithmetic half arrived 2026-09-05, after the first live Assessment graded
**0.00** on seven claims of which six were faithful. The map must be everything
the voice was HANDED (§1.1's three things), not one of the three; a claim resting
on a counter printed on the record's own page is fidelity-to-spine, and grading it
against a map that omits that counter can only ever return *unsupported*. See
:func:`assessment_evidence_text`.

FOUR THINGS THIS CHANNEL MAY NEVER DO, and where each is stopped:

  * write a fact into the record — impossible: the record is a different row,
    written by a deterministic assembler from quotation;
  * set the read's BLUF, severity or confidence — it emits no ``severity:`` tag
    (severity reaches the column only through one, ``models.severity_from_tags``)
    and its own confidence is capped at the spine's;
  * cite outside the spine — an out-of-range ordinal raises
    :class:`AssessmentConstructionError` and fails the run (§2.1 assertion 3);
  * read anything but the spine — the prompt builder's only data argument is the
    payload (``assessment_prompts.build_assessment_prompt``), and this module's
    reader fetches exactly one row.

THE FLAG. Operator ruling: ``LEGBA_COMPOSITION_ASSEMBLY`` is THE ONE REGIME
SWITCH. There is no second env var — the channel refuses to run unless the spine
it was handed is itself an assembly (``regime == "assembly"``), which is exactly
what that flag decides. The channel's own on/off is its descriptor
``identity.state``: ``draft`` registers it and creates no actor, so it is
promotable live through the lifecycle route without a deploy and without an
env-var round trip. Two switches for two different questions: "is the record
assembled" (env) and "does this analyst exist yet" (descriptor state).
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence
from uuid import UUID

from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike
from ..provenance.models import FindingPayload
from ..provenance.external_truth import record_standing_badge
from ..provenance.round_lineage import assessment_badge
from .assessment_coverage import coverage_of
from .assembly_payload import (
    ASSEMBLY_SCHEMA,
    CONTEXT_MATCH_KEY,
    CONTEXT_ORIGIN_KEY,
    REGIME_ASSEMBLY,
    assembly_enabled,
)
from .assessment_prompts import (
    # RE-EXPORTS, and they are now INERT for dispatch. P3 Lane A moved the voice
    # selection into ``system_prompt_for`` / ``prompt_version_for``, which
    # resolve from the RECORD's tier and read those two names in their own
    # module at call time. They stay importable here because this module's name
    # has been the public handle on the channel's voice since D-6 — but a caller
    # that REBINDS ``assessment_channel.ASSESSMENT_SYSTEM`` now changes nothing
    # (``scripts/d6_clause_replay.py`` was repointed in the same commit).
    ASSESSMENT_SYSTEM,  # noqa: F401 — re-exported surface
    PROMPT_VERSION,  # noqa: F401 — re-exported surface
    build_assessment_prompt,
    prompt_version_for,
    record_arithmetic,
    system_prompt_for,
)
from .assessment_unsupported import (
    find_unsupported,
    segment_sentences,
    spine_span_text,
)
from .composition_window import MAX_TITLE_CHARS

__all__ = [
    "ASSESSMENT_ANALYST_ID",
    "ASSESSMENT_ANALYST_IDS",
    "ASSESSMENT_SCHEMA",
    "ASSESSMENT_SPINE_KEY",
    "COUNTRY_ASSESSMENT_ANALYST_ID",
    "AssessmentConstructionError",
    "EVIDENCE_ARITHMETIC_RULE",
    "assessment_evidence_text",
    "assessment_spine",
    "build_assessment_payload",
    "is_assessment_run",
    "normalize_ref_markers",
    "parse_assessment_prose",
    "read_assessment_spine",
    "resolve_markers",
    "run_assessment",
]


class AssessmentConstructionError(RuntimeError):
    """The channel could not be built honestly, so it is not built at all.

    Raised for an ordinal outside the spine (§2.1 assertion 3) and for a
    ``derived_from`` that is not exactly the spine (assertion 1). Both are
    CONSTRUCTION failures rather than scores: fail loud into the existing error
    path the actor already classifies, the same call D-2 made for a span that
    cannot be cut byte-identically.
    """


#: Regime 1 was the WORLD tier only (§7 F-1), and the deferral was explicit: a
#: country Assessment is "one more entry here plus one more descriptor",
#: deliberately held back because the judge spend is 33x and 32 country boards
#: may or may not want an interpretive voice. Keeping it a SET rather than a
#: constant is what made that a decision rather than a refactor.
#:
#: P3 LANE A TAKES THE DECISION, and what forced it is not appetite but a hole.
#: Since the 09-04 demotion EVERY composition tier is an assembly: the country
#: composition quotes one sentence from each of its eight desk heads under an
#: ordinal and writes no sentence of its own. So after D-5 and D-6 the product
#: has exactly one interpretive voice, and it is the WORLD one. Nobody writes
#: the cross-dimension read of a single country any more — the read that says
#: what an energy finding and an escalation finding on the SAME desk mean
#: together. That read is the one a country board opens the product for.
ASSESSMENT_ANALYST_ID: str = "world_assessment"

#: The per-country channel. Same kind, same module, same fence — the ONLY
#: differences are that its spine is ``country_composition``, that the spine
#: read is scoped to the running target, and that the country tier hands the
#: voice each block's desk head IN FULL (``assembly_spans.SPAN_ROLE_CONTEXT_BODY``)
#: because a cross-dimension argument cannot be made out of eight lead
#: sentences. See :func:`read_assessment_spine` and
#: ``assessment_prompts.COUNTRY_ASSESSMENT_SYSTEM``.
COUNTRY_ASSESSMENT_ANALYST_ID: str = "country_assessment"

ASSESSMENT_ANALYST_IDS: frozenset[str] = frozenset({
    ASSESSMENT_ANALYST_ID,
    COUNTRY_ASSESSMENT_ANALYST_ID,
})

ASSESSMENT_SCHEMA: str = "assessment.v1"

#: The descriptor marker naming the analyst whose assembly this channel reads.
#: It lives in the OPEN ``subscription.substrate`` dict — the same seam
#: ``thematic_dimension`` / ``thematic_desks`` use, so no schema change and no
#: registry rebuild beyond the descriptor itself.
#:
#: It is also what stops the silent mis-dispatch: a target-less, verify-declaring
#: composition descriptor with no marker falls through to the WORLD branch of
#: ``READ_SLICE`` and would quietly read the region/country slice. The marker is
#: checked FIRST.
ASSESSMENT_SPINE_KEY: str = "assessment_spine"

#: How far back a spine may be and still be worth reading. A record older than
#: this is not today's record, and an Assessment written over yesterday's spine
#: while today's exists would be a silently stale read. Resolved from the
#: descriptor's own ``other_analysts[].time_window`` when the caller passes one.
DEFAULT_SPINE_WINDOW_HOURS: int = 24

_REF_RE = re.compile(r"\[\[ref:(\d{1,3})\]\]")
_BOLD_TITLE_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*$", re.MULTILINE)
_ATX_TITLE_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)

#: A bold FIRST line that is a section label, not a headline. Without this the
#: title scrape would lift ``BLUF:`` out of a body whose model skipped the
#: headline — printing a section marker as the row's ``title`` AND deleting the
#: BLUF label from the prose in the same move.
_SECTION_LABEL_RE = re.compile(
    r"^(bluf|summary|assessment|bottom line|key judg)", re.IGNORECASE
)

#: THE MARKER SPELLINGS THE CORE PLANE ACTUALLY EMITS, and why normalising them
#: is a repair rather than a fabrication.
#:
#: Measured on the 5-cycle live replay: one run in five wrote every reference as
#: ``(ref 1)`` instead of ``[[ref:1]]`` — a body with real, correct references to
#: real blocks that resolved to ZERO citations, i.e. an Assessment with no
#: evidence map, which verify would have graded as wholly unsupported. The house
#: has met this exact class before and ruled on it: the core plane emits
#: full-width ``【N】`` brackets and the citation parser normalises them before
#: resolution. This is the same token differently spelled, not a new claim: the
#: ORDINAL is the model's own, the range check still runs after, and an ordinal
#: the spine does not carry still fails the run.
#:
#: Deliberately narrow — it matches the word ``ref`` immediately followed by a
#: number. It cannot invent a reference out of prose that names none.
_LOOSE_REF_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\(\s*refs?[:\s]\s*(\d{1,3})\s*\)", re.IGNORECASE),
    re.compile(r"\[\[\s*refs?[:\s]\s*(\d{1,3})\s*\]\]", re.IGNORECASE),
    re.compile(r"\[\s*refs?[:\s]\s*(\d{1,3})\s*\]", re.IGNORECASE),
    re.compile(r"(?<!\[)\brefs?[:\s]\s*(\d{1,3})\b(?!\]\])", re.IGNORECASE),
)

TITLE_SOURCE_BOLD: str = "bold_line"
TITLE_SOURCE_ATX: str = "atx_header"
TITLE_SOURCE_FIRST_LINE: str = "first_line"
TITLE_SOURCE_FALLBACK: str = "fallback"

_FALLBACK_TITLE: str = "Assessment of the assembled record"

#: How long an UNMARKED first line may be and still be read as a headline. The
#: title contract caps the model at 90 characters; 110 leaves room for a slight
#: overrun without swallowing a paragraph's opening sentence.
_BARE_TITLE_MAX_CHARS: int = 110


# ---------------------------------------------------------------------------
# Descriptor + options resolution
# ---------------------------------------------------------------------------


def assessment_spine(descriptor: Any) -> str | None:
    """The spine analyst id this descriptor's channel reads, or ``None``.

    Reads ``subscription.substrate[ASSESSMENT_SPINE_KEY]``. Total: any descriptor
    shape that does not carry the marker returns ``None`` and every existing
    dispatch is byte-for-byte unchanged.
    """
    sub = getattr(descriptor, "subscription", None)
    substrate = getattr(sub, "substrate", None)
    if not isinstance(substrate, Mapping):
        return None
    value = substrate.get(ASSESSMENT_SPINE_KEY)
    return str(value) if value else None


def is_assessment_run(options: Mapping[str, Any]) -> bool:
    """Is this run the Assessment channel?

    Keyed on the analyst id the actor already stamps into ``options`` — the same
    key ``assembly_enabled`` reads. No new option, no actor edit.
    """
    return str(options.get("analyst_id") or "") in ASSESSMENT_ANALYST_IDS


# ---------------------------------------------------------------------------
# THE SPINE READ — one row, and the fence starts here
# ---------------------------------------------------------------------------


_SPINE_SQL = """
    SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity, f.data,
           f.target_id, f.target_version, f.analyst_id, f.analyst_version,
           f.produced_at, f.derived_from, f.schema_uri, f.run_id
      FROM analyst_outputs f
     WHERE f.kind = 'finding'
       AND f.analyst_id = $1
       AND f.superseded_by IS NULL
       AND f.data->'data'->'assembly'->>'schema' = $2
       AND f.data->'data'->'assembly'->>'regime' = $3
       AND f.produced_at >= NOW() - make_interval(hours => $4)
     ORDER BY f.produced_at DESC, f.id DESC
     LIMIT 1
"""

#: The SAME statement with ONE more predicate. Written out rather than assembled
#: from a fragment, because the world path's bytes are a contract: the two texts
#: sit side by side so a reader can see that the only difference between them is
#: ``f.target_id = $5``, and a test diffs them to prove it. A ``+ (clause if
#: target else "")`` would make that diff a runtime question.
_SPINE_SQL_TARGET = """
    SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity, f.data,
           f.target_id, f.target_version, f.analyst_id, f.analyst_version,
           f.produced_at, f.derived_from, f.schema_uri, f.run_id
      FROM analyst_outputs f
     WHERE f.kind = 'finding'
       AND f.analyst_id = $1
       AND f.superseded_by IS NULL
       AND f.data->'data'->'assembly'->>'schema' = $2
       AND f.data->'data'->'assembly'->>'regime' = $3
       AND f.produced_at >= NOW() - make_interval(hours => $4)
       AND f.target_id = $5
     ORDER BY f.produced_at DESC, f.id DESC
     LIMIT 1
"""


async def read_assessment_spine(
    conn: Any,
    *,
    spine_analyst: str,
    time_window_hours: int | None = None,
    target_filter: Any = None,
) -> list[dict[str, Any]]:
    """The ONE row this channel is allowed to see: the newest live assembly.

    Three predicates carry the contract and each is load-bearing:

      * ``superseded_by IS NULL`` — the composition head fold already keeps one
        live head per analyst; reading a superseded spine would write an
        Assessment of a record the reader cannot open.
      * ``assembly.schema = 'assembly.v1'`` AND ``regime = 'assembly'`` — a
        LEGACY-regime row carries the regime stamp and no blocks. Writing an
        Assessment from it would produce prose with no spine to be faithful to,
        which is the pre-demotion failure with a new label on it.
      * the freshness window — an empty result is an honest empty, and the actor
        NOOPs on it rather than publishing a read about nothing.

    P3 LANE A — THE FOURTH PREDICATE, and it is the whole per-country channel.
    ``country_assessment`` carries a ``subscription.targets`` block, so the
    runtime fans it out ONE worker per desk with that desk's id in
    ``target_filter``; READ_SLICE hands it straight through. Without the
    predicate all 32 workers would read the same newest ``country_composition``
    row — whichever country happened to compose last — and 31 of them would
    publish an interpretive read of another country's record under their own
    target id. That is not a degraded read, it is a mislabelled one, and the
    reader has no way to see it.

    ``target_filter`` NONE takes ``_SPINE_SQL`` UNCHANGED, four parameters and
    all: the world channel's read is byte-identical to what D-6 shipped, which
    is why the two statements are spelled out separately above.
    """
    args: tuple[Any, ...] = (
        str(spine_analyst),
        ASSEMBLY_SCHEMA,
        REGIME_ASSEMBLY,
        int(time_window_hours or DEFAULT_SPINE_WINDOW_HOURS),
    )
    if target_filter:
        rows = await conn.fetch(_SPINE_SQL_TARGET, *args, str(target_filter))
    else:
        rows = await conn.fetch(_SPINE_SQL, *args)
    return [dict(r) for r in rows]


def spine_payload(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """``data.data.assembly`` off a spine row, or ``None`` when it is not one.

    Tolerates a JSON-encoded ``data`` string (asyncpg without the pool's JSONB
    codec) the same way ``composition_window`` does — the pooled path hands back
    dicts, a bare connection hands back str, and a reader that only works on one
    of them is a reader that works in tests and not in production.
    """
    env = row.get("data")
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except (ValueError, TypeError):
            return None
    if not isinstance(env, Mapping):
        return None
    inner = env.get("data")
    if not isinstance(inner, Mapping):
        return None
    payload = inner.get("assembly")
    if not isinstance(payload, Mapping):
        return None
    if str(payload.get("schema") or "") != ASSEMBLY_SCHEMA:
        return None
    if str(payload.get("regime") or "") != REGIME_ASSEMBLY:
        return None
    if not payload.get("blocks"):
        return None
    return dict(payload)


# ---------------------------------------------------------------------------
# PROSE OUT, MARKERS IN
# ---------------------------------------------------------------------------


def parse_assessment_prose(content: str) -> tuple[str, str, str]:
    """``(title, body, title_source)`` from the model's PROSE.

    The journal's parse (``_derive_title``) narrowed to this channel's contract:
    the headline is the FIRST LINE or it is nothing. Bold first, then an ATX
    first line, then a bare one. The headline is REMOVED from the body — it is
    the row's ``title`` column and the reader renders it above the prose, so
    leaving it in would print it twice.

    WHY FIRST-LINE-ONLY, when the journal scans the whole entry. This channel's
    body shape MANDATES ``##`` section headers, so "the first ATX header
    anywhere" is reliably a SECTION NAME. The journal has no section list, which
    is exactly why the wider scan is right there and wrong here — measured on
    the live replay, the wider scan titles a headline-less read *"The reading"*.

    Every refusal falls back to the deterministic title and keeps the prose
    WHOLE: a title is worth less than a body's opening sentence, and this is the
    branch where the two compete. ``title_source`` says which arm fired, so the
    fallback rate is countable rather than invisible.
    """
    raw = str(content or "").strip()
    if not raw:
        return _FALLBACK_TITLE, "", TITLE_SOURCE_FALLBACK

    lines = raw.split("\n")
    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_idx is None:  # pragma: no cover — `raw` is non-empty here
        return _FALLBACK_TITLE, raw, TITLE_SOURCE_FALLBACK
    first = lines[first_idx].strip()
    rest = "\n".join(lines[first_idx + 1:]).strip()

    bold = re.fullmatch(r"\*\*(.+?)\*\*", first)
    atx = re.fullmatch(r"#{1,6}\s+(.+)", first)
    if bold is not None:
        candidate, source = bold.group(1).strip(), TITLE_SOURCE_BOLD
    elif atx is not None:
        candidate, source = atx.group(1).strip(), TITLE_SOURCE_ATX
    else:
        candidate, source = first, TITLE_SOURCE_FIRST_LINE

    candidate = _REF_RE.sub("", candidate).strip()
    if _SECTION_LABEL_RE.match(candidate):
        # The read opens on its own first section (``**BLUF:** …``). Lifting it
        # would print a section label as the headline and delete the label from
        # the prose in one move.
        return _FALLBACK_TITLE, raw, TITLE_SOURCE_FALLBACK
    if source == TITLE_SOURCE_FIRST_LINE and (
        not candidate
        or len(candidate) > _BARE_TITLE_MAX_CHARS
        or candidate.endswith((".", "!", "?"))
        or "[[ref:" in first
    ):
        # An unmarked first line that reads like PROSE — long, terminated, or
        # carrying a citation — is the body's opening sentence, not a headline.
        # Keep it.
        return _FALLBACK_TITLE, raw, TITLE_SOURCE_FALLBACK
    if not candidate:
        return _FALLBACK_TITLE, raw, TITLE_SOURCE_FALLBACK
    return candidate[:MAX_TITLE_CHARS], rest, source


def normalize_ref_markers(body: str) -> tuple[str, int]:
    """``(body, n_normalised)`` — the model's own refs, in the house spelling.

    Runs BEFORE resolution, and only ever rewrites the SPELLING of a reference
    the model already made. See :data:`_LOOSE_REF_RES` for the measurement that
    put it here and for why this is the ``【N】`` precedent rather than a new
    licence. The count is stamped on the row so the rate is visible: a model
    that stops emitting the house form should be legible in the data, not
    silently patched forever.
    """
    text = str(body or "")
    # STASH the canonical markers first. Without it the loose patterns re-match
    # what the earlier ones just wrote and the COUNT inflates — the number would
    # then describe the regex rather than the model, which is the whole point of
    # publishing it.
    stashed: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        stashed.append(m.group(0))
        return f"\x00{len(stashed) - 1}\x00"

    def _stash_ordinal(m: re.Match[str]) -> str:
        stashed.append(f"[[ref:{int(m.group(1))}]]")
        return f"\x00{len(stashed) - 1}\x00"

    text = _REF_RE.sub(_stash, text)
    total = 0
    for pattern in _LOOSE_REF_RES:
        # Each pattern's OUTPUT is stashed too, so the next pattern cannot
        # re-match what this one just wrote — the loose forms overlap by design
        # (``[[ref 1]]`` is also a ``[…]`` and a bare ``ref 1``).
        text, n = pattern.subn(_stash_ordinal, text)
        total += n
    text = re.sub(r"\x00(\d+)\x00", lambda m: stashed[int(m.group(1))], text)
    return text, total


def resolve_markers(
    body: str, spine_ordinals: Sequence[int],
) -> list[dict[str, int]]:
    """Every ``[[ref:N]]`` in the body, resolved onto the spine's ordinals.

    Raises :class:`AssessmentConstructionError` on an ordinal the spine does not
    carry. §2.1 assertion 3 is explicit that this is a hard construction failure
    and not a soft reason code — the channel's whole warrant is that it wrote
    from the record, and a reference to a block that is not on the record is a
    claim about a page that does not exist.
    """
    known = {int(o) for o in spine_ordinals}
    sentences = segment_sentences(body)
    out: list[dict[str, int]] = []
    stray: list[int] = []
    for sentence in sentences:
        for m in _REF_RE.finditer(sentence.text):
            ordinal = int(m.group(1))
            if ordinal not in known:
                stray.append(ordinal)
                continue
            out.append({"ordinal": ordinal, "sentence_index": sentence.index})
    if stray:
        raise AssessmentConstructionError(
            f"the Assessment cites spine ordinal(s) "
            f"{sorted(set(stray))} that the record does not carry "
            f"(it carries {sorted(known)}) — an out-of-range ordinal is a "
            f"construction failure, not a soft reason code (D-1 §2.1)"
        )
    if not out:
        # THE FENCE'S OTHER EDGE. §2.2's channel constraint is that every
        # sentence names at least one spine ordinal; a body that names NONE has
        # no evidence map at all, so verify would grade every fact-asserting
        # claim as unsupported and the row would publish an argument with
        # nothing under it. That is a construction failure for the same reason
        # an out-of-range ordinal is: the channel's entire warrant is that it
        # wrote from the record.
        raise AssessmentConstructionError(
            "the Assessment resolved ZERO spine ordinals — a body that cites no "
            "block has no evidence map, and this channel's whole warrant is "
            "that it wrote from the record (D-1 §2.2)"
        )
    return out


#: The rule between the block's quoted span and the record's own arithmetic
#: inside one ``evidence_text``. A visible boundary, because a grader (and a
#: reader auditing the map) must be able to tell the desk's words from the
#: record's counters — they carry different warrants, and running them together
#: as one paragraph would be how the second gets quoted as the first.
EVIDENCE_ARITHMETIC_RULE: str = "\n\n--- THE RECORD THIS BLOCK SITS IN ---\n"


def assessment_evidence_text(span_text: str, arithmetic: str) -> str:
    """One citation's ``evidence_text``: the block's quoted span, then the
    record's own arithmetic under :data:`EVIDENCE_ARITHMETIC_RULE`.

    The span comes FIRST and unmodified, so ``evidence_text.split(rule)[0]`` is
    still exactly ``spine_span_text(payload)[ordinal]`` — the D-6 §3.5 contract,
    now recoverable rather than merely stated. With no arithmetic to add (a
    payload carrying no counters at all) the result is byte-identical to what
    D-6 shipped.
    """
    if not arithmetic:
        return span_text
    return f"{span_text}{EVIDENCE_ARITHMETIC_RULE}{arithmetic}"


def build_assessment_citations(
    payload: Mapping[str, Any],
    *,
    spine_id: str,
    spine_analyst: str,
    markers: Sequence[Mapping[str, int]],
) -> list[dict[str, Any]]:
    """``data.data.citations`` — one entry per CITED spine ordinal.

    ``ref_id`` is the SPINE's row id on every entry, and that is the honest
    answer twice over: it is the row this channel actually read (so
    ``ref_id`` never names a head outside ``derived_from``), and it is the drill
    target a reader wants — the record on the same page.

    ``evidence_text`` is the block's QUOTED SPAN **and the record's own
    arithmetic**, which makes this list the evidence map §3.5 specifies. The
    existing faithfulness pass grades each claim against the words the record
    carried, which is precisely fidelity-to-spine.

    THE ARITHMETIC HALF IS THE 2026-09-05 FIX, and the live row that forced it is
    the first Assessment the channel ever published. It read **0.00** fidelity —
    seven claims, none supported — and six of the seven were faithful. The voice
    is handed THREE things (D-6 §1.1): the header, the rendered record, and the
    record's own arithmetic, which the prompt introduces as *"facts about the
    record, handed to you. You may quote them"*. The evidence map carried only the
    second. So "the ledger notes six reads sit below the verification floor" and
    "top-share of 1.000" — both printed on the record's own page, both quoted
    accurately — resolved against nothing, and the only verdict available to a
    judge grading a claim against an evidence map that does not contain its
    truthmaker is *unsupported*. The channel was being charged for reading the
    page it was given.

    So the same bytes ``build_assessment_prompt`` renders are appended to every
    cited ordinal's ``evidence_text``, under a labelled rule. It is repeated per
    entry rather than minted as a synthetic ordinal of its own because the
    composition rubric grades a claim **against the sub-claim its marker names**
    (``verify._run_judge``'s shared lead, verbatim) — an entry at an ordinal no
    sentence can cite would sit in the map unread by the rubric's own
    instruction. The repetition is bounded by ``BLOCK_CAP`` (8) and the block is
    five short lines.

    WHAT THIS DOES NOT DO: widen the fence by one byte. Every one of those numbers
    is already ON the spine row, in ``payload``, which is still the ONLY argument
    either this function or the prompt builder has. Nothing new is read, and a
    claim resting on the record's arithmetic was always fidelity-to-spine — it was
    simply ungradeable.
    """
    spans = spine_span_text(payload)
    arithmetic = record_arithmetic(payload)
    blocks = {int(b.get("ordinal") or 0): b for b in (payload.get("blocks") or [])}
    out: list[dict[str, Any]] = []
    for ordinal in sorted({int(m["ordinal"]) for m in markers}):
        block = blocks.get(ordinal) or {}
        entry: dict[str, Any] = {
            "marker": f"[[ref:{ordinal}]]",
            "ordinal": ordinal,
            "ref_id": str(spine_id),
            "ref_kind": "finding",
            "source": str(spine_analyst),
            "title": str(block.get("question") or ""),
            "evidence_text": assessment_evidence_text(
                spans.get(ordinal, ""), arithmetic
            ),
            # The block's own origin, carried so a lineage walk from this
            # channel reaches the DESK head in one hop rather than none. It is
            # NOT a ref_id: this channel did not read that head, the record did.
            "spine_block": {
                "finding_id": str(block.get("finding_id") or ""),
                "desk": str(block.get("desk") or ""),
                "target_id": block.get("target_id"),
            },
            "derived_from": [str(spine_id)],
        }
        # STEP E — THE SECOND ORIGIN, NAMED AS A SECOND ORIGIN. A world block
        # carries a ``context_body`` span whose body is that country's own
        # ASSESSMENT, and that assessment is a DIFFERENT ROW from the desk head
        # ``spine_block`` names. ``evidence_text`` already contains its words —
        # ``spine_span_text`` joins a block's spans, so the judge grades this
        # channel's claims against the country voice's full text, which is what
        # the world voice was actually handed. What was missing is the DRILL:
        # a reader who wants to open the country read that a sentence came out
        # of had a body with no id attached to it. This is that id, beside
        # ``spine_block`` and never merged into it — the block's origin is still
        # the desk head, and two origins stated as two is the only honest shape.
        # Absent (not null) on every block carrying no context, so a country and
        # a pre-STEP-E world citation are byte-identical to what shipped.
        context_origin = str(block.get(CONTEXT_ORIGIN_KEY) or "")
        if context_origin:
            entry["context_origin_id"] = context_origin
            match = str(block.get(CONTEXT_MATCH_KEY) or "")
            if match:
                # WHICH RULE found it. A fallback (the country composed again
                # after its voice ran, so this assessment reads a neighbouring
                # record rather than this exact one) is a weaker provenance
                # claim than an exact match, and a drill that cannot tell them
                # apart would present both as "the read behind this block".
                entry["context_match"] = match
        eff = (block.get("verify") or {}).get("effective_confidence")
        if eff is not None:
            try:
                entry["effective_confidence"] = float(eff)
            except (TypeError, ValueError):
                pass
        if str(block.get("tier") or "") == "periphery":
            entry["tier"] = "periphery"
        out.append(entry)
    return out


def build_assessment_payload(
    *,
    payload: Mapping[str, Any],
    spine_id: str,
    body: str,
    markers: Sequence[Mapping[str, int]],
    title_source: str,
    markers_normalized: int = 0,
    standing: Mapping[str, Any] | None = None,
    record_standing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``data.data.assessment`` — schema ``assessment.v1`` (§2.2).

    The badge rides here rather than being computed by the reader, because §2.3
    fixes its provenance: the external number, its ``n``, its round and its
    population travel together or not at all. ``fidelity_to_spine`` is ``None``
    with an explicit ``unmeasured`` state until D-3's arm lands — never ``0.0``,
    which on a badge reads as a measured failure.

    THE STANDING PAIR (W-7 / design §3.4, F-12). Two OPTIONAL blocks, never one:

      * ``standing``        — the VOICE's number, on the ``assessment_sentence``
        population: sentences this channel wrote. It rides the assessment badge.
      * ``record_standing`` — the RECORD's number, on the ``assembly_span``
        population: the DESK sentences the spine quotes. It rides a separate
        ``spine_badge`` key, beside the spine facts this payload already copies
        verbatim (``lead_test``, ``spine_blocks``) so the reader can print the
        arithmetic of the page next to the prose.

    They are two keys because they are two different acts of authorship, and a
    single key would invite exactly the sum F-12 forbids. Both are composed by
    ``external_truth.standing_accuracy`` — this module never reads the ledger,
    it wears what a caller already read, which is why the producer stays free of
    a DB dependency it does not otherwise have.
    """
    unsupported, checked = find_unsupported(body, payload)
    spine_badge = record_standing_badge(record_standing)
    return {
        "schema": ASSESSMENT_SCHEMA,
        "spine_id": str(spine_id),
        "spine_schema": ASSEMBLY_SCHEMA,
        "spine_regime": str(payload.get("regime") or ""),
        "spine_as_of": payload.get("as_of"),
        "spine_blocks": len(payload.get("blocks") or []),
        # Copied VERBATIM from the spine (§2.2) so the reader can print the
        # arithmetic beside the prose and a grader can see what the voice was
        # told before it wrote.
        "lead_test": dict(((payload.get("lead") or {}).get("test") or {})),
        "markers": [dict(m) for m in markers],
        "unsupported": unsupported,
        "unsupported_checked": checked,
        # H11 — what the voice left out, as numbers (see assessment_coverage).
        "coverage": coverage_of(payload, markers, body),
        # Stamped by verify when the arm exists; mirrored here for the reader.
        # Explicitly None, and the badge says "unmeasured" in words.
        "fidelity": None,
        "badge": assessment_badge(standing=dict(standing) if standing else None),
        # The RECORD's standing number, beside the voice's and never summed with
        # it. Absent (not null-valued) when nothing was handed in, so a reader
        # cannot mistake "no spine number yet" for "the spine scored nothing".
        **({"spine_badge": spine_badge} if spine_badge else {}),
        # P3-A: the TIER's version, so a row is attributable to the prompt that
        # wrote it. The world tier resolves to ``PROMPT_VERSION`` unchanged; the
        # country tier carries its own, because its system prompt asks a
        # different question and pooling the two under one version string would
        # make every before/after comparison on either of them meaningless.
        "prompt_version": prompt_version_for(payload),
        "title_source": title_source,
        # How many references arrived in a non-house spelling and were
        # re-spelled (never invented). Countable per read, so a drift in the
        # model's citation habit is legible in the data rather than absorbed
        # silently by a regex forever.
        "markers_normalized": int(markers_normalized),
    }


# ---------------------------------------------------------------------------
# THE RUN
# ---------------------------------------------------------------------------


def _coerce_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _empty_result(reason: str, *, steps: list[dict[str, Any]]) -> AnalystMethodResult:
    """The honest empty: no spine, no Assessment, and the row says which.

    Confidence 0.0 and the house ``empty_slice`` tag, so it never pollutes a
    synthesis-confidence statistic and never enters an admission pool. The
    channel writes from the record or it says nothing — it does not write
    *about* the record's absence as if that were a reading of the world.
    """
    return AnalystMethodResult(
        finding=FindingPayload(
            title="No assembled record to assess",
            body=reason,
            confidence=0.0,
            tags=["empty_slice", "meta"],
            data={"meta": True, "assessment_skipped": reason},
        ),
        usage={},
        derived_from=[],
        intermediate_steps=steps,
    )


async def run_assessment(
    inputs: Sequence[Mapping[str, Any]],
    options: Mapping[str, Any],
    *,
    llm: LLMHandlerLike,
    max_tokens: int,
    temperature: float,
) -> AnalystMethodResult:
    """One Assessment, written from ONE assembly and nothing else.

    The whole path in order: find the spine among the inputs → refuse unless it
    is an assembly under the one regime switch → build the prompt from the
    payload alone → write prose → parse title and markers → resolve every
    ordinal into the spine (or fail loud) → mint the spine-grain evidence map →
    mark what the spine cannot support → return with
    ``derived_from == [spine_id]``.
    """
    # Deferred import: ``meta_findings_synthesizer`` imports THIS module for its
    # dispatch, so a module-level import here would close the cycle. Same idiom,
    # same reason, as D-2's injected ``basis_reader``.
    from .meta_findings_synthesizer import _composition_signature, _reason_via_llm

    steps: list[dict[str, Any]] = []
    spine_row: Mapping[str, Any] | None = None
    payload: dict[str, Any] | None = None
    for row in inputs:
        candidate = spine_payload(row)
        if candidate is not None:
            spine_row, payload = row, candidate
            break

    if spine_row is None or payload is None:
        steps.append({"phase": "orient", "kind": "assessment_no_spine",
                      "inputs": len(inputs)})
        return _empty_result(
            "No assembled record (assembly.v1, regime=assembly) was available "
            "for this cycle, so no Assessment was written. This channel reads "
            "the record and nothing else.",
            steps=steps,
        )

    spine_analyst = str(spine_row.get("analyst_id") or "")
    if not assembly_enabled(spine_analyst):
        # Belt to the payload's braces. The regime stamp on the row says what
        # the producer did; this says what the runtime is currently configured
        # to do. Disagreement means the flag moved between the two runs, and the
        # honest response is not to write.
        steps.append({"phase": "orient", "kind": "assessment_regime_off",
                      "spine_analyst": spine_analyst})
        return _empty_result(
            f"The record's producer ({spine_analyst}) is not running under the "
            f"assembly regime in this environment, so there is no quotation "
            f"spine to be faithful to and no Assessment was written.",
            steps=steps,
        )

    spine_uuid = _coerce_uuid(spine_row.get("id"))
    if spine_uuid is None:
        raise AssessmentConstructionError(
            "the spine row carries no resolvable id, so derived_from cannot be "
            "the single-element array the channel's input restriction IS"
        )

    user_prompt = build_assessment_prompt(payload)
    # P3-A — THE TIER PICKS THE VOICE, and it is picked from the PAYLOAD rather
    # than from the analyst id. The payload is the only thing this channel is
    # allowed to read, so deriving the voice from it keeps the fence intact: a
    # descriptor cannot select a prompt, and a run cannot be handed a country
    # voice over a world record. Both resolvers are total and both answer the
    # world constants for a world/thematic record.
    system_prompt = system_prompt_for(payload)
    prompt_version = prompt_version_for(payload)
    steps.append({
        "phase": "orient",
        "kind": "assessment_spine",
        "spine_id": str(spine_uuid),
        "spine_analyst": spine_analyst,
        "spine_tier": str(payload.get("tier") or ""),
        "spine_target_id": spine_row.get("target_id"),
        "blocks": len(payload.get("blocks") or []),
        "prompt_chars": len(user_prompt),
        "prompt_version": prompt_version,
    })

    content, usage = await _reason_via_llm(
        llm,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=system_prompt,
    )
    steps.append({
        "phase": "reason",
        "kind": "llm_call",
        "subprovider": getattr(llm, "subprovider", "unknown"),
        "tokens": (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        ),
    })

    title, body, title_source = parse_assessment_prose(content)
    body, normalised = normalize_ref_markers(body)
    ordinals = [int(b.get("ordinal") or 0) for b in (payload.get("blocks") or [])]
    markers = resolve_markers(body, ordinals)
    assessment = build_assessment_payload(
        payload=payload,
        spine_id=str(spine_uuid),
        body=body,
        markers=markers,
        title_source=title_source,
        markers_normalized=normalised,
    )
    citations = build_assessment_citations(
        payload,
        spine_id=str(spine_uuid),
        spine_analyst=spine_analyst,
        markers=markers,
    )

    # The channel is never more confident than the record it reads. The spine's
    # own confidence is deterministic (the WEAKEST block it carries), so this is
    # the arithmetic of the page rather than a number the voice chose — and the
    # voice is not asked for one, which is the point of dropping the JSON
    # envelope.
    try:
        confidence = float(spine_row.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(max(confidence, 0.0), 1.0)

    finding = FindingPayload(
        title=title,
        body=body,
        confidence=confidence,
        # NO ``severity:`` tag — severity reaches the column only through
        # ``models.severity_from_tags``, and the Assessment may never set the
        # read's severity. The absence is the enforcement.
        tags=["meta", "assessment", f"spine:{spine_analyst}"],
        data={
            "meta": True,
            "assessment": assessment,
            "citations": citations,
            "contributing_analysts": [spine_analyst],
            # P3-A — THE TARGET, and leaving it ``None`` was a live-shaped bug
            # waiting for the second tier. ``finding_supersession`` clusters on
            # ``(situation_signature, analyst_id)``, so a per-country channel
            # stamping the world literal would collapse ALL 32 countries'
            # Assessments into ONE head: 31 desks' reads superseded by whichever
            # country ran last, invisible to the reader and impossible to
            # diagnose from the row. The world channel is target-less and
            # resolves to the same ``:world`` literal it has always stamped.
            "situation_signature": _composition_signature(
                options.get("analyst_id") or ASSESSMENT_ANALYST_ID,
                options.get("target_id") or options.get("target_filter"),
            ),
        },
    )
    steps.append({
        "phase": "reflect",
        "kind": "assessment",
        "markers": len(markers),
        "cited_ordinals": len(citations),
        "unsupported": len(assessment["unsupported"]),
        # H11 — the receipt repeats the coverage so a trace reader sees the
        # omission without opening the row.
        "blocks_carried": assessment["coverage"]["blocks_carried"],
        "blocks_cited": assessment["coverage"]["blocks_cited"],
        "blocks_uncited": len(assessment["coverage"]["blocks_uncited"]),
        "lead_named": assessment["coverage"]["lead_named"],
        "title_source": title_source,
        "body_chars": len(body),
    })
    steps.append({
        "phase": "persist",
        "kind": "envelope",
        "derived_from": 1,
    })

    result = AnalystMethodResult(
        finding=finding,
        usage=usage,
        # §2.1 assertion 1, and the reason this channel is a separate row at all.
        derived_from=[spine_uuid],
        intermediate_steps=steps,
    )
    if len(result.derived_from) != 1 or result.derived_from[0] != spine_uuid:
        raise AssessmentConstructionError(
            "derived_from must be EXACTLY the spine id — that single-element "
            "array is the enforcement of 'input = the assembly payload only'"
        )
    return result
