# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""inquiry — the STATEFUL journal voice (Program 5 archetype 2).

planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §1/§2/§5. A lens is STATELESS:
the same declared prior over today's delta, no memory, no question it is
carrying, no way to ask the platform anything. An INQUIRY is stateful. It owns

  * a PUT-able ``method.options.brief`` (free text <= 2,000 chars) and an
    optional ``target_scope`` — the operator's mandate, live-editable through
    ``PUT /api/v1/descriptors/analyst/{id}`` with no code edit;
  * a LEDGER (``inquiry_ledger``, migration 0215 — lane p5_state) read at PLAN
    and written at REFLECT, carried across cycles as THE inquiry's own state;
  * the same staged arc the journal family already proved:
    PLAN -> GATHER -> FIELD-NOTES -> NARRATE -> REFLECT -> HONESTY.

Its output is one ``journal_entries`` row with ``entry_kind='inquiry'`` (the
crossroads descriptor, lane p5_crossroads, rides the SAME kind and writes
``entry_kind='crossroads'``) — OFF the fact/finding/nexus chain, ``derived_from``
empty, exactly like every other journal-family tier.

WHY ITS OWN KIND MODULE AND NOT A SIXTH journal_assessor TIER. The tier
discriminator on ``journal_assessor`` is the analyst id, and every tier there
shares one contract: read, narrate, stop. The inquiry adds a STATE READ before
the prompt is rendered and a STATE WRITE after the body is final, plus a
descriptor-borne brief that changes what the run is FOR. That is a different
method, so it is a different kind — and being a different kind is also what
keeps the diary's apparatus contract (feed denominators, ``[[instrument]]``
markers, the apparatus-postscript rule) from leaking into an investigator's
voice.

WHAT IS REUSED, NOT COPIED. The staged-arc building blocks are imported from
``journal_assessor`` and used as they stand: ``_field_notes`` and
``_narrate_with_tools`` (both take this module's OWN seam instruction through
their optional ``instruction`` parameter — added for exactly this, absent =>
byte-identical for every journal tier), ``_reflect_claims``,
``_repair_ref_markers``, ``_rewrite_gathered_citations``, ``_derive_title``,
``_forced_honesty_flags`` and ``journal_window``. The GATHER loop itself is
``inline_target._gather``, the same one the journal rides.

THE READ SURFACE (§2 grants). ``substrate_read`` is the DEFAULT GATHER binding
— its 19 tool names are ``inline_target._GATHER_READ_TOOLS``, so they route
through ``binding`` unchanged. ``journal_read``'s own instruments and the
``inquiry_state`` ledger tools are NOT in that set, so they ride the per-tool
``options['gather_tool_bindings']`` channel (``extra_write_tools`` in ``_gather``
is that channel: "recognized, but routed to its OWN pack's binding"), which is
what makes ``Agency.run_pack_tool`` enforce tool<->pack ownership per call. NO
``web_access`` and NO ``research`` pack: the inquiry has no web by construction.

THE SEAM WITH lane p5_state. The ledger pack's three tools are named here as
constants (:data:`INQUIRY_STATE_TOOLS`) and invoked through the governed binding
by NAME. This module never imports the pack module, so the two lanes land
independently; at integration ``agency/inquiry_state.py`` becomes the source of
truth for these names and this module re-exports them.

THE SEAM WITH lane p5_crossroads. ONE hook: ``method.options.pre_pass_module``,
a ``module:function`` string resolved at PLAN and called as
``pre_pass_block(options, deps) -> str | None``. When it returns text, that text
is rendered ABOVE the state block as a code-computed block (the 7e spread-block
pattern: the model narrates numbers it was HANDED, never numbers it derived). A
descriptor that does not set the option renders byte-identically to one written
before the hook existed.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from legba.prompts.inquiry import (
    PRE_PASS_BLOCK_HEADER,
    STATE_BLOCK_HEADER,
    STATE_EMPTY_LINE,
    STATE_SECTION_HEADERS,
)

from ..provenance.consumption import CONSUMPTION_CONTEXT_JOURNAL
from ..provenance.kinds import OutputKind
from ..provenance.models import JournalPayload
from .agency.journal_read import JOURNAL_READ_TOOLS
from .inline_target import (
    AnalystMethodResult,
    InlineTargetDeps,
    LLMHandlerLike,
    _extract_json,
    _gather,
    _GATHER_READ_TOOLS,
)
from .journal_assessor import (
    _derive_title,
    _field_notes,
    _forced_honesty_flags,
    _narrate_with_tools,
    _reflect_claims,
    _repair_ref_markers,
    _rewrite_gathered_citations,
)
from .journal_clusters import journal_window
from .question_text import is_deictic

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Host-discovered constants (the same discovery contract journal_assessor uses)
# ---------------------------------------------------------------------------

KIND_NAME = "inquiry"

#: The marker the wave-D brief pins: the kind's own name as a module constant,
#: so a rename cannot silently disconnect the descriptor from its handler.
INQUIRY_KIND = KIND_NAME

SCHEMA_VERSION = "legba/analyst.inquiry/1-0-0"
HANDLER_VERSION = "0.1.0"
PROMPT_MODULE_PATH = "legba.prompts.inquiry:INQUIRY_SYSTEM"

#: The 11th OutputKind — the host's dispatcher writes a ``journal_entries`` row
#: (NOT a finding). This is what keeps the kind off-chain.
OUTPUT_KIND: OutputKind = OutputKind.JOURNAL

#: The default META global signal slice primes the run; the ledger + GATHER carry
#: the rest. Same posture (and same reason) as journal_assessor's.
READ_SLICE = None

#: The journal ``entry_kind`` this kind writes. The CROSSROADS descriptor
#: (lane p5_crossroads) rides the same kind with the same machinery and writes
#: its own kind, exactly as the lens/lens_diff split works one module over: the
#: tier IS the descriptor, never a per-descriptor mode flag.
ENTRY_KIND = "inquiry"
CROSSROADS_ENTRY_KIND = "crossroads"
CROSSROADS_ANALYST_ID = "crossroads"

# ---------------------------------------------------------------------------
# The inquiry_state pack contract (design §3) — NAMES ONLY.
# ---------------------------------------------------------------------------
# Lane p5_state owns the pack module, its handlers, its migration and the
# deps-builder scope fence that keeps one inquiry out of another's ledger. This
# module codes against the NAMES through the governed binding so the two lanes
# can land independently; at integration these should become re-exports of
# ``agency.inquiry_state`` so there is one source of truth.

INQUIRY_STATE_PACK_ID = "inquiry_state"
LEDGER_READ_TOOL = "ledger_read"
LEDGER_WRITE_TOOL = "ledger_write"
LEDGER_CLOSE_TOOL = "ledger_close"
INQUIRY_STATE_TOOLS: tuple[str, ...] = (
    LEDGER_READ_TOOL, LEDGER_WRITE_TOOL, LEDGER_CLOSE_TOOL,
)

#: The EXISTING open-question faucet (``propose_facts``'s ``open_question``
#: tool -> a ``hypotheses`` row with ``status='open_question'`` -> the research
#: ladder). The inquiry DISPATCHES a question through this shape when — and only
#: when — a binding for it is wired; the pilot grants no write pack, so nothing
#: new can spend. There is deliberately NO net-new dispatch tool (design §3).
OPEN_QUESTION_TOOL = "open_question"

#: Tool names that must route to their OWN pack's binding rather than to the
#: default substrate_read one. Passed to ``_gather`` as ``extra_write_tools``,
#: which is that routing channel — the journal_read instruments are reads, but
#: they belong to a DIFFERENT pack than the read binding, and pack ownership is
#: what the routing decides.
ROUTED_PACK_TOOLS: tuple[str, ...] = tuple(
    dict.fromkeys(JOURNAL_READ_TOOLS + INQUIRY_STATE_TOOLS + (OPEN_QUESTION_TOOL,))
)

# ---------------------------------------------------------------------------
# Bounds. Every one of these caps a cost or a blast radius, never a style.
# ---------------------------------------------------------------------------

#: Descriptor brief ceiling (design §2). Enforced at READ as well as in the
#: option spec, so a legacy row that predates the spec is truncated, not obeyed.
BRIEF_MAX_CHARS = 2_000

#: Ledger rows rendered into the PLAN prompt, per section. A standing inquiry's
#: open set is small by design; this stops a neglected ledger from eating the
#: window.
_STATE_ROWS_PER_SECTION = 12

#: Characters of one ledger row's text rendered into the state block.
_STATE_ROW_TEXT_CHARS = 400

#: Rows ONE run may add to the ledger, across all kinds. The point of an inquiry
#: is a small set of live hypotheses, not a growing pile.
_LEDGER_MAX_WRITES_PER_RUN = 6

#: Rows ONE run may close. Closing is cheaper than opening but still bounded.
_LEDGER_MAX_CLOSES_PER_RUN = 6

#: The LEDGER phase gets ONE turn. It runs after the body is final, so there is
#: nothing to negotiate — the model either has rows or it does not.
_LEDGER_MAX_ROUNDS = 1

#: Tool rounds the tools-live NARRATE may spend (journal_assessor's own cap).
_NARRATE_MAX_TOOL_ROUNDS = 2

#: Honesty flags this kind forces deterministically, on top of the journal
#: family's calibration-derived pair.
HYPOTHESIS_WITHOUT_TEST_FLAG = "hypothesis_refused_no_resolution_test"
QUESTION_NOT_SELF_CONTAINED_FLAG = "question_refused_deictic"
LEDGER_UNREACHABLE_FLAG = "inquiry_ledger_unreachable"


def build_prompt_module() -> Any:
    """The inquiry runs on the in-actor envelope, not the GEPA compile surface,
    so it has no DSPy module. Returning the persona STRING keeps the discovery
    contract uniform (a caller that introspects ``build_prompt_module`` gets the
    voice); ``run_method`` threads the system prompt directly."""
    from legba.prompts.inquiry import INQUIRY_SYSTEM
    return INQUIRY_SYSTEM


def entry_kind_for_analyst(analyst_id: str | None) -> str:
    """Select the journal ``entry_kind`` from the running analyst id.

    The CROSSROADS id writes ``crossroads``; every other id on this kind writes
    ``inquiry``. Pure function of the id — the tier is the descriptor, never a
    per-descriptor mode flag (the same rule ``journal_assessor`` follows for its
    six tiers).
    """
    if analyst_id == CROSSROADS_ANALYST_ID:
        return CROSSROADS_ENTRY_KIND
    return ENTRY_KIND


# ---------------------------------------------------------------------------
# Options — the descriptor-borne mandate (X-1 ``method.options``)
# ---------------------------------------------------------------------------


def resolve_brief(options: Mapping[str, Any]) -> str:
    """The operator's brief, trimmed to :data:`BRIEF_MAX_CHARS`.

    Absent/blank returns ``""`` — and an inquiry with no brief is a REAL state,
    not an error: it renders an explicit "no brief is set" line rather than
    inventing a mandate for itself.
    """
    raw = options.get("brief")
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:BRIEF_MAX_CHARS]


def resolve_target_scope(options: Mapping[str, Any]) -> tuple[str, ...]:
    """The optional ``target_scope`` list of target ids, de-duplicated in order.

    Context only: it is rendered into the prompt as the desks the inquiry is
    accountable for. It is NOT a query filter — the inquiry reads the whole
    substrate through its granted packs, and fencing its READS to a scope would
    make "the record is silent outside my desks" unfalsifiable.
    """
    raw = options.get("target_scope")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: dict[str, None] = {}
    for item in raw:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


async def resolve_pre_pass_block(
    options: Mapping[str, Any],
    deps: Any,
    *,
    steps: list[dict[str, Any]],
) -> str:
    """Resolve + call ``method.options.pre_pass_module`` (the lane-3 hook).

    The option is a ``module:function`` string — the SAME colon form
    ``method.prompt_module`` uses, and for the same reason: a dotted path is
    ambiguous between a module and an attribute, and the ambiguity is what makes
    a typo resolve to something plausible instead of failing.

    The referenced callable is invoked as ``pre_pass_block(options, deps)`` and
    may be sync or async. A non-empty string return is rendered as a
    code-computed block above the state block; ``None``/``""`` renders nothing.

    DEGRADE-NOT-DROP: a missing module, a missing attribute, a non-callable, a
    raise or a non-string return all log a WARNING, stamp a step and render
    NOTHING. A pre-pass is an enrichment — it must never be able to fail a run
    whose reading is otherwise sound. Absent option => no import, no call, no
    step: byte-identical to a descriptor written before the hook existed.
    """
    ref = options.get("pre_pass_module")
    if not isinstance(ref, str) or ":" not in ref:
        if ref:
            logger.warning(
                "inquiry.pre_pass.bad_reference ref=%r — expected the colon "
                "form 'module:function'; rendering nothing", ref,
            )
            steps.append({"phase": "plan", "kind": "pre_pass_bad_reference"})
        return ""
    module_path, _, attr = ref.partition(":")
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, attr)
        result = fn(options, deps)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 — enrichment never fails the run
        logger.warning("inquiry.pre_pass.failed ref=%s err=%s", ref, exc)
        steps.append({"phase": "plan", "kind": "pre_pass_failed", "ref": ref})
        return ""
    if not isinstance(result, str) or not result.strip():
        steps.append({"phase": "plan", "kind": "pre_pass_empty", "ref": ref})
        return ""
    steps.append({
        "phase": "plan",
        "kind": "pre_pass_block",
        "ref": ref,
        "block_chars": len(result),
    })
    return f"{PRE_PASS_BLOCK_HEADER}\n{result.strip()}\n"


# ---------------------------------------------------------------------------
# PLAN — the ledger read + the state block
# ---------------------------------------------------------------------------


async def read_ledger(tool_bindings: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Read this inquiry's OPEN ledger rows through the governed pack binding.

    Returns ``(rows, failure_reason)``. ``failure_reason`` is ``""`` on a clean
    read (including a clean EMPTY read — a first cycle is not a failure) and a
    short token otherwise. The caller renders an honest block either way and
    forces :data:`LEDGER_UNREACHABLE_FLAG` on a real failure, because an inquiry
    that silently forgets its own state would read as a confident first cycle
    every single run — the worst possible failure mode for a continuity voice.

    The per-descriptor SCOPE FENCE lives in the deps builder (lane p5_state): the
    binding is built for THIS descriptor, so ``ledger_read`` can only ever see
    this inquiry's rows. Nothing here filters by descriptor, deliberately — a
    filter here would be a second, drifting copy of a fence that must hold at
    the governed boundary.
    """
    binding = tool_bindings.get(LEDGER_READ_TOOL)
    if binding is None:
        return [], "unbound"
    try:
        outcome = await binding.run_tool(LEDGER_READ_TOOL, {"status": "open"})
    except Exception as exc:  # noqa: BLE001 — degrade to an honest empty block
        logger.warning("inquiry.ledger_read.failed err=%s", exc)
        return [], "error"
    if not outcome.admitted:
        logger.warning(
            "inquiry.ledger_read.blocked cause=%s", getattr(outcome, "block_cause", "")
        )
        return [], "blocked"
    result = outcome.tool_result
    if result is None or result.status == "failed":
        return [], "failed"
    rows = (result.output or {}).get("rows") or []
    return [r for r in rows if isinstance(r, Mapping)], ""


def render_state_block(
    rows: list[dict[str, Any]], *, failure_reason: str = "",
) -> str:
    """Render the carried ledger as THE inquiry's own state (design §1).

    Grouped by ``kind`` in the persona's declared section order, each row shown
    with its id (so the model can CLOSE it by id), its status, its age in days
    and — for a hypothesis — the frozen resolution test. A row whose kind is
    unrecognised still renders, under its raw kind: an unrendered row is a row
    the inquiry silently forgot.

    Pure function of its arguments (the one DB read lives at the caller), so
    every render test runs without a database.
    """
    header = STATE_BLOCK_HEADER
    if failure_reason:
        return (
            f"{header}\n"
            f"YOUR LEDGER COULD NOT BE READ THIS CYCLE ({failure_reason}). You "
            "are carrying state you cannot see. Do NOT narrate this as a first "
            "cycle and do NOT re-open hypotheses you may already hold: read "
            "what you can from the record, say plainly in the entry that your "
            "ledger was unreachable, and keep this cycle's new rows to what "
            "this cycle genuinely established.\n"
        )
    if not rows:
        return f"{header}\n{STATE_EMPTY_LINE}\n"

    now = datetime.now(timezone.utc)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("kind") or "observation"), []).append(row)

    lines = [header]
    ordered_kinds = list(STATE_SECTION_HEADERS) + [
        k for k in grouped if k not in STATE_SECTION_HEADERS
    ]
    for kind in ordered_kinds:
        bucket = grouped.get(kind)
        if not bucket:
            continue
        lines.append("")
        lines.append(STATE_SECTION_HEADERS.get(kind, kind.upper()))
        for row in bucket[:_STATE_ROWS_PER_SECTION]:
            lines.append(_render_state_row(row, now=now))
        if len(bucket) > _STATE_ROWS_PER_SECTION:
            lines.append(
                f"  ... and {len(bucket) - _STATE_ROWS_PER_SECTION} more open "
                f"{kind} rows not shown."
            )
    return "\n".join(lines) + "\n"


def _render_state_row(row: Mapping[str, Any], *, now: datetime) -> str:
    """One ledger row as a prompt line: id, age, status, text, frozen test."""
    text = str(row.get("text") or "").strip()[:_STATE_ROW_TEXT_CHARS]
    status = str(row.get("status") or "open")
    age = _age_days(row.get("created_at"), now=now)
    # An unparsable/absent created_at drops the age rather than printing an
    # empty one: a row whose age the model cannot read must not look like a row
    # with no age, and "(status=open)" is the honest shape for that.
    meta = f"{age}d old, status={status}" if age is not None else f"status={status}"
    line = f"  [{row.get('id')}] ({meta}) {text}"
    test = str(row.get("resolution_test") or "").strip()
    if test:
        line += f"\n      RESOLUTION TEST (frozen): {test[:_STATE_ROW_TEXT_CHARS]}"
    resolves_by = str(row.get("resolves_by") or "").strip()
    if resolves_by:
        line += f"\n      RESOLVES BY: {resolves_by}"
    dispatched = str(row.get("dispatched_to") or "").strip()
    if dispatched:
        line += f"\n      DISPATCHED TO: {dispatched}"
    return line


def _age_days(raw: Any, *, now: datetime) -> int | None:
    """Whole days between ``raw`` (a timestamp or ISO string) and ``now``."""
    if isinstance(raw, datetime):
        created = raw
    elif isinstance(raw, str) and raw:
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds() // 86400))


def render_brief_block(brief: str, target_scope: tuple[str, ...]) -> str:
    """The operator's mandate, verbatim, plus the accountable desks.

    Verbatim matters: the brief is a registry row an operator PUTs, and the copy
    an audit points at as "the mandate this run actually saw" has to be the same
    bytes. An empty brief renders an explicit line — an inquiry that quietly
    invented its own mandate would be the worst kind of drift.
    """
    if brief:
        lines = ["YOUR BRIEF (verbatim, as the operator wrote it):", brief]
    else:
        lines = [
            "YOUR BRIEF: none is set on this descriptor. Say so in the entry "
            "and confine this cycle to following your open ledger rows; do not "
            "invent a mandate for yourself."
        ]
    if target_scope:
        lines.append(
            "TARGET SCOPE (the desks you are accountable for): "
            + ", ".join(target_scope)
            + ". You may read outside them when the brief genuinely leads "
            "there — say so when you do."
        )
    return "\n".join(lines) + "\n"


def render_user_prompt(
    inputs: list[dict[str, Any]],
    *,
    brief: str,
    target_scope: tuple[str, ...] = (),
    state_block: str = "",
    pre_pass_block: str = "",
) -> str:
    """Assemble the PLAN prompt: pre-pass -> state -> brief -> priming slice.

    ORDER IS THE CONTRACT. The code-computed pre-pass leads (numbers the model
    was handed, before it can start reasoning past them), the carried state comes
    next (the persona's "read it first"), then the mandate, then the priming
    slice. A pure function of its arguments — the two reads behind the first two
    blocks live at the caller.
    """
    lines: list[str] = []
    if pre_pass_block:
        lines.append(pre_pass_block)
    if state_block:
        lines.append(state_block)
    lines.append(render_brief_block(brief, target_scope))
    rendered_rows = [
        row for row in journal_window(inputs)
        if row.get("id") is not None and str(row.get("title") or "").strip()
    ]
    if rendered_rows:
        lines.append(
            "THE PRIMING SLICE (the recent global signal window — a STARTING "
            "POINT, not your material). Your brief is what you are reading for; "
            "use your tools to investigate it. Cite any factual assertion "
            "inline as [[ref:<uuid>]] using ONLY UUIDs your tools returned or "
            "the [[ref:...]] ids on the rows below."
        )
        for row in rendered_rows:
            title = str(row.get("title") or "").strip()
            lines.append(f"  [[ref:{row['id']}]] {title}")
    else:
        lines.append(
            "THE PRIMING SLICE IS EMPTY this cycle. That is not a reason to "
            "write nothing and it is not a fact about the world: go to the "
            "record directly — search the corpus, read the desks' findings and "
            "assessments, walk the graph — and answer your brief from there. "
            "Never fabricate material the slice did not carry."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The per-phase seam strings — this kind's own voice, not the diary's.
# ---------------------------------------------------------------------------

_MEMORY_ORIENTATION = (
    "\n\nYOUR CONTINUITY IS THE LEDGER BLOCK ABOVE, and get_journal_delta is "
    "where your prior entries are kept. Your entries are off-chain, so a "
    "list_findings(analyst_id=<yourself>) self-read is EMPTY BY DESIGN — never "
    "read the finding chain for your own past and never narrate that emptiness "
    "as blindness. A prior entry is MEMORY, never evidence for a fact claim."
)

_FIELD_NOTES_INSTRUCTION = (
    "\n\nFIELD NOTES (the ONE handoff before you write). You have finished "
    "investigating. Now, still in your own voice, write your working notes on "
    "your brief: every observation worth keeping, each carrying its substrate "
    "ref(s) inline as [[ref:<uuid>]]. Say explicitly, for EACH open ledger row "
    "you were carrying, whether this cycle touched it — settled it, weakened "
    "it, or left it untouched because the record was silent. Keep the numbers, "
    "the names, the dates and the refs; DROP only the raw tool-JSON exhaust. Do "
    "NOT write the entry yet."
)

_NARRATE_INSTRUCTION = (
    "\n\nNOW WRITE THE INQUIRY ENTRY from your field notes above, following "
    "your system instructions: open on the ledger, then your reading of the "
    "record on your brief, every factual claim carrying its [[ref:<uuid>]] "
    "inline (reuse the refs from your notes). State each hypothesis you are "
    "opening TOGETHER WITH the test that would settle it. Name what the record "
    "is silent on. If — and ONLY if — you must check one more thing before "
    'committing a claim, you MAY emit a single tool call as strict JSON ({"tool"'
    ': "<name>", "args": {...}}) and you will get the result; otherwise write '
    "the entry as plain markdown prose (NOT a JSON object)."
)

_EMPTY_BODY = "(empty inquiry entry)"
_TITLE_FALLBACK = "Inquiry"


def gather_catalog(*, ledger_bound: bool) -> str:
    """The GATHER tool catalog, built FROM the granted tool tuples.

    Never hand-listed: the substrate read surface is ``_GATHER_READ_TOOLS``, the
    instruments are ``JOURNAL_READ_TOOLS``, and the ledger tools appear only when
    a binding for them was actually wired this run. A catalog that advertises a
    tool the run cannot reach is the silent-bypass shape the journal's own
    propose phase was built to stop being invisible.
    """
    lines = [
        "\n\nBefore you write, investigate the record. Each query must be a "
        "single strict-JSON object.\nAvailable tools:",
        "  SUBSTRATE (the raw record, the corpus, the graph, the as-of "
        "register): " + ", ".join(_GATHER_READ_TOOLS) + ".",
        "  FINISHED INTELLIGENCE + INSTRUMENTS: "
        + ", ".join(JOURNAL_READ_TOOLS) + ".",
    ]
    if ledger_bound:
        lines.append(
            "  YOUR LEDGER: " + LEDGER_READ_TOOL + "(status?) re-reads your "
            "carried rows. You will be asked for your NEW rows after the entry "
            "is written — do not write them now."
        )
    lines.append(
        "\nYou have NO web access: there is no tool here that fetches a page, "
        "and asking for one wastes a round."
    )
    lines.append(
        "\nProtocol:\n"
        '  - To query, reply with strict JSON: {"tool": "<name>", "args": {...}}\n'
        '  - When you have gathered enough, reply with: {"done": true}\n'
        "  - Do not write the entry yet — you will be asked for it after "
        "gathering."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# REFLECT coda — the LEDGER phase (design §3)
# ---------------------------------------------------------------------------

_LEDGER_PHASE_PROMPT = (
    "Your entry is written and its citations are bound. This is the ONE moment "
    "you write to your ledger — the state the next cycle of this inquiry will "
    "wake up holding.\n\n"
    "Reply with a SINGLE strict-JSON object, and nothing else:\n"
    "{\n"
    '  "close": [{"id": "<ledger row id you are closing>", "status": '
    '"confirmed|refuted|answered|expired|withdrawn", "reason": "<one sentence '
    'naming what settled it>", "cited_refs": ["<uuid>", ...]}],\n'
    '  "hypotheses": [{"text": "<the hypothesis>", "resolution_test": "<what '
    'the record would have to show to confirm or refute it>", "resolves_by": '
    '"<optional ISO date>"}],\n'
    '  "questions": [{"text": "<a SELF-CONTAINED question naming the actor, '
    'the place and the period>", "counter": "<optional competing reading>", '
    '"cited_refs": ["<uuid>", ...]}],\n'
    '  "observations": [{"text": "<the thing you banked>", "cited_refs": '
    '["<uuid>", ...]}]\n'
    "}\n\n"
    "RULES THE CODE ENFORCES, not courtesy:\n"
    "  - a hypothesis with no resolution_test is DISCARDED, unread;\n"
    "  - a question that does not say what it is about is DISCARDED;\n"
    "  - every uuid in cited_refs must be one YOUR ENTRY ALREADY CITES — "
    "anything else is dropped rather than stored as a ref that resolves to "
    "nothing;\n"
    f"  - at most {_LEDGER_MAX_WRITES_PER_RUN} new rows and "
    f"{_LEDGER_MAX_CLOSES_PER_RUN} closes this run.\n"
    "Writing NOTHING is a legitimate answer on a cycle that settled nothing and "
    "opened nothing: reply with {} and cost yourself nothing.\n\n"
    "YOUR ENTRY:\n"
)


def plan_ledger_writes(
    parsed: Any, cited_refs: list[UUID],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Turn the LEDGER phase's reply into VALIDATED writes + closes.

    Pure, and deliberately so: the whole sealed-ledger discipline (design §3) is
    testable without a database, an LLM or a pack.

      * a ``hypothesis`` with no non-empty ``resolution_test`` is REFUSED — the
        constraint ``inquiry_ledger_hypothesis_needs_test`` refuses it at the DB
        too, and this is the copy that makes the refusal VISIBLE (a counted,
        flag-raising drop) instead of a 500 the model never sees;
      * a ``question`` that is still deictic ("the incident", "that attack") is
        REFUSED through the platform's EXISTING open-question discipline
        (``question_text.is_deictic``, the CW-3 guard), because a question with a
        dangling referent is unanswerable the moment it leaves this entry;
      * every ``cited_refs`` entry is intersected with what the ENTRY actually
        cites — a ref the body never carried would be a citation the inquiry
        invented for its own bookkeeping;
      * the per-run caps are applied last, so a refusal never costs a good row
        its slot.

    Returns ``(writes, closes, counters)``.
    """
    counters = {
        "hypotheses": 0, "questions": 0, "observations": 0, "closes": 0,
        "refused_no_test": 0, "refused_deictic": 0, "dropped_refs": 0,
    }
    if not isinstance(parsed, Mapping):
        return [], [], counters
    allowed = {str(r) for r in cited_refs}

    def _refs(entry: Mapping[str, Any]) -> list[str]:
        raw = entry.get("cited_refs") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        kept: list[str] = []
        for item in raw:
            text = str(item).strip().lower()
            try:
                canonical = str(UUID(text))
            except (ValueError, AttributeError):
                counters["dropped_refs"] += 1
                continue
            if canonical in allowed:
                kept.append(canonical)
            else:
                counters["dropped_refs"] += 1
        return kept

    writes: list[dict[str, Any]] = []
    for entry in _as_entries(parsed.get("hypotheses")):
        text = str(entry.get("text") or "").strip()
        test = str(entry.get("resolution_test") or "").strip()
        if not text:
            continue
        if not test:
            counters["refused_no_test"] += 1
            continue
        row: dict[str, Any] = {
            "kind": "hypothesis", "text": text, "resolution_test": test,
        }
        resolves_by = str(entry.get("resolves_by") or "").strip()
        if resolves_by:
            row["resolves_by"] = resolves_by
        refs = _refs(entry)
        if refs:
            row["cited_refs"] = refs
        writes.append(row)
        counters["hypotheses"] += 1

    for entry in _as_entries(parsed.get("questions")):
        text = str(entry.get("text") or entry.get("question") or "").strip()
        if not text:
            continue
        if is_deictic(text):
            counters["refused_deictic"] += 1
            continue
        row = {"kind": "question", "text": text}
        counter_text = str(entry.get("counter") or "").strip()
        if counter_text:
            row["counter"] = counter_text
        refs = _refs(entry)
        if refs:
            row["cited_refs"] = refs
        writes.append(row)
        counters["questions"] += 1

    for entry in _as_entries(parsed.get("observations")):
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        row = {"kind": "observation", "text": text}
        refs = _refs(entry)
        if refs:
            row["cited_refs"] = refs
        writes.append(row)
        counters["observations"] += 1

    closes: list[dict[str, Any]] = []
    for entry in _as_entries(parsed.get("close")):
        row_id = str(entry.get("id") or "").strip()
        status = str(entry.get("status") or "").strip()
        if not row_id or status not in _CLOSE_STATUSES:
            continue
        closes.append({
            "id": row_id,
            "status": status,
            "reason": str(entry.get("reason") or "").strip(),
            "cited_refs": _refs(entry),
        })
        counters["closes"] += 1

    return (
        writes[:_LEDGER_MAX_WRITES_PER_RUN],
        closes[:_LEDGER_MAX_CLOSES_PER_RUN],
        counters,
    )


#: The close statuses the ledger's ``inquiry_ledger_status_vocab`` constraint
#: admits (design §3), minus ``open`` — closing to ``open`` is not a close.
_CLOSE_STATUSES = frozenset(
    {"confirmed", "refuted", "answered", "expired", "withdrawn"}
)


def _as_entries(raw: Any) -> list[Mapping[str, Any]]:
    """Coerce one LEDGER-phase array leniently; a non-mapping entry is dropped."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [e for e in raw if isinstance(e, Mapping)]


async def _dispatch_question(
    row: Mapping[str, Any], *, tool_bindings: Mapping[str, Any],
) -> str:
    """Raise one question through the platform's EXISTING open-question faucet.

    Design §3, the money line: "Dispatch is NOT a tool of its own." When a
    binding for ``open_question`` is wired (i.e. the descriptor grants the write
    pack) the question goes through THAT tool — the same one every desk uses,
    into ``hypotheses(status='open_question')`` and onward to the research ladder
    — and the returned row id becomes the ledger row's ``dispatched_to``. When no
    binding is wired (the pilot, which grants no write pack) this returns ``""``
    and the ledger row simply carries no dispatch: nothing new can spend, and the
    paid rung stays behind the operator's word exactly as it does today.
    """
    binding = tool_bindings.get(OPEN_QUESTION_TOOL)
    refs = list(row.get("cited_refs") or [])
    if binding is None or not refs:
        return ""
    args: dict[str, Any] = {"question": row["text"], "derived_from": refs}
    if row.get("counter"):
        args["counter"] = row["counter"]
    try:
        outcome = await binding.run_tool(OPEN_QUESTION_TOOL, args)
    except Exception as exc:  # noqa: BLE001 — never fail a written entry
        logger.warning("inquiry.question_dispatch.failed err=%s", exc)
        return ""
    if not outcome.admitted or outcome.tool_result is None:
        return ""
    if outcome.tool_result.status == "failed":
        return ""
    output = outcome.tool_result.output or {}
    return str(output.get("hypothesis_id") or output.get("id") or "")


async def ledger_phase(
    deps: InlineTargetDeps,
    *,
    body: str,
    cited_refs: list[UUID],
    analyst_id: str | None,
    tool_bindings: Mapping[str, Any],
    steps: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Ask for the cycle's ledger rows, validate them, and write them.

    Runs AFTER the body is final and REFLECT has bound the citations — the same
    placement, and the same reasoning, as the journal's PROPOSE phase: at GATHER
    the model has read nothing and has nothing to bank; at NARRATE a structured
    reply is caught as a tool-call leak and fails the run. This is the one moment
    it has both a formed reading and a legal channel for it.

    DEGRADE-NOT-DROP throughout: an LLM error, an unparsable reply, an unbound
    tool or a blocked call never fails a run whose entry is already written.
    Returns ``(usage, counters)``.
    """
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    counters: dict[str, int] = {"written": 0, "closed": 0}
    write_binding = tool_bindings.get(LEDGER_WRITE_TOOL)
    if write_binding is None:
        steps.append({"phase": "ledger", "kind": "pack_not_effective"})
        return usage_total, counters
    if not body:
        steps.append({"phase": "ledger", "kind": "no_entry"})
        return usage_total, counters

    prompt = _LEDGER_PHASE_PROMPT + body
    for _ in range(_LEDGER_MAX_ROUNDS):
        try:
            content, usage = await _reason(deps, prompt)
        except Exception as exc:  # noqa: BLE001 — the entry is already written
            logger.warning("inquiry.ledger.llm_failed err=%s", exc)
            steps.append({"phase": "ledger", "kind": "llm_error"})
            return usage_total, counters
        for key in usage_total:
            usage_total[key] += int(usage.get(key, 0) or 0)
        parsed = _extract_json(content or "")
        writes, closes, validation = plan_ledger_writes(parsed, cited_refs)
        counters.update(validation)
        steps.append({
            "phase": "ledger", "kind": "planned",
            "writes": len(writes), "closes": len(closes), **validation,
        })
        for row in writes:
            args = {k: v for k, v in row.items() if k != "counter"}
            if row["kind"] == "question":
                dispatched = await _dispatch_question(
                    row, tool_bindings=tool_bindings
                )
                if dispatched:
                    args["dispatched_to"] = dispatched
            if await _run_ledger_tool(
                write_binding, LEDGER_WRITE_TOOL, args,
                analyst_id=analyst_id, steps=steps,
            ):
                counters["written"] += 1
        close_binding = tool_bindings.get(LEDGER_CLOSE_TOOL)
        for row in closes:
            if close_binding is None:
                steps.append({"phase": "ledger", "kind": "close_unbound"})
                break
            if await _run_ledger_tool(
                close_binding, LEDGER_CLOSE_TOOL, dict(row),
                analyst_id=analyst_id, steps=steps,
            ):
                counters["closed"] += 1
    return usage_total, counters


async def _reason(deps: InlineTargetDeps, prompt: str) -> tuple[str, Mapping[str, int]]:
    """One LLM turn on the VOICE handler (the coda is the inquiry's own words)."""
    from .inline_target import _reason_via_llm

    return await _reason_via_llm(
        deps.narrate_llm(),
        user_prompt=prompt,
        max_tokens=deps.narrate_tokens(),
        temperature=deps.temperature,
        system_prompt=deps.system_prompt,
    )


async def _run_ledger_tool(
    binding: Any,
    tool_name: str,
    args: dict[str, Any],
    *,
    analyst_id: str | None,
    steps: list[dict[str, Any]],
) -> bool:
    """One governed ledger call. Returns True iff the row actually landed.

    Every failure mode is stamped on the receipt: a blocked call, a failed
    handler and an exception are three different stories and an operator reading
    ``analyst_traces.intermediate_steps`` needs to be able to tell them apart.
    """
    detail = ""
    admitted = False
    try:
        outcome = await binding.run_tool(tool_name, args)
        admitted = bool(outcome.admitted)
        if not admitted:
            detail = f"blocked: {outcome.block_cause}"
        elif outcome.tool_result is None or outcome.tool_result.status == "failed":
            admitted = False
            detail = (
                f"failed: {outcome.tool_result.error}"
                if outcome.tool_result is not None
                else "failed: tool produced no result"
            )
    except Exception as exc:  # noqa: BLE001 — never fail a written entry
        detail = f"failed: {exc!s}"
    if detail:
        logger.warning(
            "inquiry.ledger.%s analyst_id=%s detail=%s", tool_name, analyst_id, detail,
        )
    steps.append({
        "phase": "ledger", "kind": "tool_call", "tool": tool_name,
        "row_kind": args.get("kind") or args.get("status"), "admitted": admitted,
        **({"detail": detail} if detail else {}),
    })
    return admitted


# ---------------------------------------------------------------------------
# run_method
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: InlineTargetDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one inquiry cycle over the substrate.

    PLAN (pre-pass hook -> ledger read -> brief -> priming slice) -> GATHER (the
    bounded ReAct loop over substrate_read + the journal instruments) ->
    FIELD-NOTES -> NARRATE (tools live) -> REFLECT (permissive per-claim citation
    flag) -> LEDGER (the validated state write) -> HONESTY (deterministic flags).
    The persona is threaded on EVERY call. Emits a ``JournalPayload`` and returns
    ``derived_from=[]`` — off the chain, like every journal-family row.
    """
    if not isinstance(deps, InlineTargetDeps):
        deps = InlineTargetDeps(llm=deps)

    analyst_id = options.get("analyst_id")
    entry_kind = entry_kind_for_analyst(analyst_id)
    tool_bindings: Mapping[str, Any] = options.get("gather_tool_bindings") or {}
    steps: list[dict[str, Any]] = [
        {"phase": "wake", "kind": "tier", "entry_kind": entry_kind}
    ]
    now = datetime.now(timezone.utc)
    period_end = now
    period_start = now - timedelta(hours=24)
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}

    def _fold(u: Mapping[str, int]) -> None:
        for key in usage:
            usage[key] += int(u.get(key, 0) or 0)

    # --- PLAN -----------------------------------------------------------
    brief = resolve_brief(options)
    target_scope = resolve_target_scope(options)
    pre_pass = await resolve_pre_pass_block(options, deps, steps=steps)
    ledger_rows, ledger_failure = await read_ledger(tool_bindings)
    steps.append({
        "phase": "plan", "kind": "ledger_read",
        "rows": len(ledger_rows),
        **({"failure": ledger_failure} if ledger_failure else {}),
    })
    state_block = render_state_block(ledger_rows, failure_reason=ledger_failure)
    user_prompt = render_user_prompt(
        inputs,
        brief=brief,
        target_scope=target_scope,
        state_block=state_block,
        pre_pass_block=pre_pass,
    ) + _MEMORY_ORIENTATION
    steps.append({
        "phase": "plan", "kind": "render_prompt",
        "in_count": len(inputs), "prompt_chars": len(user_prompt),
        "prompt_module": PROMPT_MODULE_PATH,
        "brief_chars": len(brief), "target_scope": list(target_scope),
    })

    # --- GROUND (Tier-1 knowledge grounding, if the descriptor opted in) ---
    if deps.grounding_hook is not None:
        try:
            preamble = await deps.grounding_hook(inputs, options)
        except Exception as exc:  # noqa: BLE001 — grounding never fails a run
            logger.warning("inquiry.grounding.failed err=%s", exc)
            preamble = None
        if preamble:
            user_prompt = f"{preamble}\n{user_prompt}"
            steps.append({"phase": "ground", "kind": "inject_preamble"})

    # --- GATHER ---------------------------------------------------------
    active_binding = options.get("agency_binding") or deps.agency_binding
    citation_extension: dict[int, dict[str, Any]] = {}
    if active_binding is not None:
        gathered, gather_usage, _refs, _gsteps, citation_extension = await _gather(
            deps,
            binding=active_binding,
            user_prompt=user_prompt,
            target_id=None,
            analyst_id=analyst_id,
            steps=steps,
            tool_bindings=tool_bindings,
            gather_system=gather_catalog(
                ledger_bound=LEDGER_READ_TOOL in tool_bindings
            ),
            extra_read_tools=(),
            extra_write_tools=ROUTED_PACK_TOOLS,
        )
        _fold(gather_usage)
        if gathered:
            user_prompt = f"{gathered}\n{user_prompt}"
    else:
        steps.append({"phase": "gather", "kind": "no_binding"})

    # --- FIELD NOTES + NARRATE -------------------------------------------
    field_notes, fn_usage = await _field_notes(
        deps, base_prompt=user_prompt, analyst_id=analyst_id, steps=steps,
        instruction=_FIELD_NOTES_INSTRUCTION,
    )
    _fold(fn_usage)
    body, narrate_usage = await _narrate_with_tools(
        deps,
        field_notes=field_notes,
        binding=active_binding,
        analyst_id=analyst_id,
        steps=steps,
        instruction=_NARRATE_INSTRUCTION,
        tool_names=_GATHER_READ_TOOLS,
    )
    _fold(narrate_usage)
    body = (body or "").strip()

    # --- REFLECT ---------------------------------------------------------
    # Same two pre-REFLECT repairs the journal runs, for the same reasons: a
    # GATHER-corpus [N] dies at render unless it is rewritten to a durable
    # [[ref:uuid]] (V4/J4), and a hand-transcribed uuid one hex digit off its
    # real row is repaired against the window the narrator actually saw (B5) —
    # uniquely, or left alone. Neither ever fabricates a ref.
    body, rewritten = _rewrite_gathered_citations(body, citation_extension)
    if rewritten:
        steps.append({
            "phase": "reflect", "kind": "gathered_citation_bridge",
            "rewritten": rewritten,
        })
    window_ref_ids = {
        str(r["id"]) for r in journal_window(inputs) if r.get("id") is not None
    }
    body, ref_repairs = _repair_ref_markers(body, gathered_ref_ids=window_ref_ids)
    claims, cited_refs, reflect_flags = _reflect_claims(body)
    steps.append({
        "phase": "reflect", "kind": "permissive_citation_flag",
        "claims": len(claims), "cited_refs": len(cited_refs),
        "flags": list(reflect_flags),
    })

    # --- LEDGER (the REFLECT coda) ---------------------------------------
    ledger_usage, ledger_counters = await ledger_phase(
        deps,
        body=body,
        cited_refs=cited_refs,
        analyst_id=analyst_id,
        tool_bindings=tool_bindings,
        steps=steps,
    )
    _fold(ledger_usage)

    # --- HONESTY ---------------------------------------------------------
    # The calibration-derived pair is read through the JOURNAL_READ binding
    # (get_calibration belongs to that pack, not to the substrate_read one this
    # kind gathers through); absent => the conservative both-flags default the
    # journal already returns, which is the honest answer for a run that could
    # not read its own calibration.
    honesty_flags = await _forced_honesty_flags(
        tool_bindings.get("get_calibration"), steps=steps,
    )
    if ledger_failure:
        honesty_flags.append(LEDGER_UNREACHABLE_FLAG)
    if ledger_counters.get("refused_no_test"):
        honesty_flags.append(HYPOTHESIS_WITHOUT_TEST_FLAG)
    if ledger_counters.get("refused_deictic"):
        honesty_flags.append(QUESTION_NOT_SELF_CONTAINED_FLAG)

    # --- PERSIST ---------------------------------------------------------
    # ``data`` is the per-row metadata column every journal tier uses (lens_id /
    # matrix one module over). The yield instrument (design §4) reads the LEDGER,
    # not this column — what rides here is what a READER of the entry needs to
    # see beside it: which brief produced it and what this cycle did to the state.
    row_data: dict[str, Any] = {
        "brief": brief,
        "target_scope": list(target_scope),
        "ledger": {
            "carried": len(ledger_rows),
            "written": ledger_counters.get("written", 0),
            "closed": ledger_counters.get("closed", 0),
            "refused_no_test": ledger_counters.get("refused_no_test", 0),
            "refused_deictic": ledger_counters.get("refused_deictic", 0),
        },
    }
    if ref_repairs:
        row_data["ref_repairs"] = ref_repairs
    payload = JournalPayload(
        entry_kind=entry_kind,
        title=_derive_title(body, fallback=_TITLE_FALLBACK),
        body=body or _EMPTY_BODY,
        claims=claims,
        cited_substrate_refs=cited_refs,
        period_start=period_start,
        period_end=period_end,
        supersedes=None,
        honesty_flags=honesty_flags,
        data=row_data,
    )
    steps.append({
        "phase": "narrate", "kind": "coerce_journal", "entry_kind": entry_kind,
        "body_chars": len(body), "claims": len(claims),
        "cited_refs": len(cited_refs), "honesty_flags": list(honesty_flags),
        "data_keys": sorted(row_data.keys()),
    })
    steps.append({"phase": "persist", "kind": "envelope", "derived_from": 0})

    # KW-1 forward-consumption index: the rendered window is this kind's
    # consumption point, stamped as a SIDECAR (never derived_from — the row stays
    # the off-chain node).
    consumed_edges: list[tuple[UUID, str]] = []
    for row in journal_window(inputs):
        rid = row.get("id")
        if rid is None:
            continue
        try:
            consumed_edges.append((UUID(str(rid)), CONSUMPTION_CONTEXT_JOURNAL))
        except (ValueError, AttributeError, TypeError):
            continue
    return AnalystMethodResult(
        finding=payload,
        usage=usage,
        derived_from=[],
        intermediate_steps=steps,
        consumed_edges=consumed_edges,
    )


__all__ = [
    "KIND_NAME",
    "INQUIRY_KIND",
    "OUTPUT_KIND",
    "READ_SLICE",
    "PROMPT_MODULE_PATH",
    "ENTRY_KIND",
    "CROSSROADS_ENTRY_KIND",
    "CROSSROADS_ANALYST_ID",
    "INQUIRY_STATE_PACK_ID",
    "INQUIRY_STATE_TOOLS",
    "LEDGER_READ_TOOL",
    "LEDGER_WRITE_TOOL",
    "LEDGER_CLOSE_TOOL",
    "OPEN_QUESTION_TOOL",
    "ROUTED_PACK_TOOLS",
    "BRIEF_MAX_CHARS",
    "HYPOTHESIS_WITHOUT_TEST_FLAG",
    "QUESTION_NOT_SELF_CONTAINED_FLAG",
    "LEDGER_UNREACHABLE_FLAG",
    "build_prompt_module",
    "entry_kind_for_analyst",
    "gather_catalog",
    "ledger_phase",
    "plan_ledger_writes",
    "read_ledger",
    "render_brief_block",
    "render_state_block",
    "render_user_prompt",
    "resolve_brief",
    "resolve_pre_pass_block",
    "resolve_target_scope",
    "run_method",
]
