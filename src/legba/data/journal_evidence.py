# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Journal evidence gate — the deterministic checks that keep a journal-family
row from publishing (or being remembered) without evidence behind it.

WHY. After a model change about half
of the journal runs collapsed at the FIELD-NOTES handoff: the model returned a
170–300-char "let me pull my instruments" stub or a pseudo tool call instead of
cited notes, ``_field_notes`` accepted it, and NARRATE then wrote closed-book
from nothing — invented leads and invented ``[[ref:]]`` uuids (18 of 41 diary
entries measured resolved 0–11 % of their refs; good entries resolve
≥ 92 %, and their notes run ≥ 2,944 chars). One fabricated entry also carried
model-written control text (``<system-warning>``, ``make_fragmented_tool_call``)
that the consolidator copied on through ``get_journal_delta``.

Four pieces live here, all pure or read-only, no model calls:

  * :func:`notes_are_degenerate` — the FIELD-NOTES gate (short or uncited notes
    are discarded and NARRATE gets the full gathered context instead);
  * :func:`find_control_text` — the ONE list of model/harness control-text
    patterns a published body may never carry;
  * :func:`resolve_ref_existence` + :func:`evaluate_publish_gate` +
    :func:`declared_unavailable_payload` — the PUBLISH gate: a row whose cited
    refs do not resolve (or that cites nothing) is replaced by a declared
    "No entry this window" row flagged ``evidence_unavailable``;
  * :data:`MEMORY_ROW_FILTER_SQL` — the predicate every reader that feeds
    earlier journal rows back to a model applies (closed, superseded,
    corrected and evidence-unavailable rows are not memory).
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (measured 11-13; see the module docstring).
# ---------------------------------------------------------------------------

#: Field notes shorter than this are a collapsed handoff, not notes. Good runs
#: measured ≥ 2,944 chars; collapsed ones 136–283 (two outliers 1,321 / 2,411
#: still fail the citation test below).
NOTES_MIN_CHARS = 1_000

#: The citation marker the journal prompts ask for.
REF_MARKER = "[[ref:"

#: Every INLINE citation form the journal-family tiers actually write
#: (measured on live rows): ``[[ref:<uuid>]]`` (the prompted form, ~all tiers),
#: ``[[<uuid>]]`` (lens/inquiry/entry), ``[<uuid>]`` and ``[ref:<uuid>]``,
#: full-width ``【<uuid>】`` / ``【ref:<uuid>】`` (core-plane models), and the
#: prefixed ``[[find:<uuid>]]`` / ``[[sh:<uuid>]]`` / ``[[ref=<uuid>]]``. Only
#: the ``[[ref:<uuid>]]`` form is folded into ``cited_substrate_refs`` upstream,
#: so a row written in another form (lens 5b0b4ecc: ten resolving ``[[uuid]]``)
#: carried zero structured refs. A uuid counts only right after an opening
#: bracket (optionally ``word:``/``word=``); bare uuids in prose (run ids) do not.
_UUID_PAT = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
INLINE_REF_RE = re.compile(
    r"(?:\[\[?|【|［)\s*(?:[A-Za-z_]{1,16}\s*[:=]\s*)?(" + _UUID_PAT + r")"
)


#: SHORT-ID citations. Measured over open journal-family rows:
#: crossroads / lens / inquiry rows also cite a row by the FIRST
#: 8 HEX characters of its id — bare inside a marker (``[[ref:8f8bd8ef]]``,
#: ``[[0be128b1]]``, ``[a8b400c3, bd0f9532]``: 262 refs) or zero-padded to uuid
#: shape (``[[ref:d25139d3-0000-0000-0000-000000000000]]``: crossroads 59fad301,
#: 20 refs). No renderer hands the model truncated ids (the crossroads block,
#: the governed tools and the ledger all print full uuids) — the model
#: abbreviates. Both forms are carried as the PADDED form and resolve only by a
#: unique prefix match (:func:`resolve_ref_existence`). A bare short id counts
#: only inside a marker, closed by ``]``/``,``, and only when it carries a hex
#: letter (``[20261027]`` is a date, not a ref).
PREFIX_REF_SUFFIX = "-0000-0000-0000-000000000000"
_PREFIX_REF_RE = re.compile(r"^[0-9a-f]{8}" + re.escape(PREFIX_REF_SUFFIX) + r"$")
SHORT_REF_RE = re.compile(
    r"(?:\[\[?|【|［)\s*(?:[A-Za-z_]{1,16}\s*[:=]\s*)?([0-9a-fA-F]{8})(?=\s*[\],】］])"
    r"|(?<=,)\s*([0-9a-fA-F]{8})(?=\s*[\],】］])"
)

#: Typographic hyphens some models write inside uuids (U+2010–U+2015, U+2212):
#: ``[[ref:a4878e1d‑8807‑…]]`` is a full uuid, not an 8-char short id.
_HYPHENS = str.maketrans({c: "-" for c in "‐‑‒–—―−"})


def is_prefix_ref(ref: Any) -> bool:
    """True for the padded short-id form ``xxxxxxxx-0000-0000-0000-000000000000``."""
    return bool(_PREFIX_REF_RE.match(str(ref).lower()))


def _short_refs(text: str) -> list[str]:
    out: list[str] = []
    for m in SHORT_REF_RE.finditer(text):
        short = (m.group(1) or m.group(2) or "").lower()
        if short and re.search(r"[a-f]", short):
            out.append(short + PREFIX_REF_SUFFIX)
    return out


def extract_inline_refs(text: str | None) -> list[str]:
    """Every uuid cited inline in ``text`` in any :data:`INLINE_REF_RE` form,
    plus every bare short id in a marker as its padded form
    (:data:`SHORT_REF_RE`), lower-cased, first-seen order, de-duplicated."""
    if not text:
        return []
    text = text.translate(_HYPHENS)
    found = [m.group(1).lower() for m in INLINE_REF_RE.finditer(text)]
    return list(dict.fromkeys(found + _short_refs(text)))


#: The NESTED citation group: ``[[ref:<a>] [ref:<b>]]`` — one
#: outer bracket pair around several single-bracket refs (one consolidation
#: wrote 7 of its 18 refs this way). The canonical ``[[ref:<uuid>]]``
#: marker regex matches neither member, so REFLECT folded none of them into
#: ``claims`` / ``cited_substrate_refs``. Members may be separated by
#: whitespace, a comma or a semicolon.
_NESTED_MEMBER = r"\[ref:\s*" + _UUID_PAT + r"\s*\]"
NESTED_REF_GROUP_RE = re.compile(
    r"\[\[ref:\s*" + _UUID_PAT + r"\s*\](?:\s*[,;]?\s*" + _NESTED_MEMBER + r")+\s*\]"
)
#: The canonical marker OR a nested group, in text order.
_MARKER_OR_NESTED_RE = re.compile(
    NESTED_REF_GROUP_RE.pattern + r"|\[\[ref:(" + _UUID_PAT + r")\]\]"
)
_UUID_RE = re.compile(_UUID_PAT)


def marker_refs(text: str | None) -> list[str]:
    """The uuids cited by ``[[ref:<uuid>]]`` markers AND nested
    ``[[ref:<a>] [ref:<b>]]`` groups in ``text``, lower-cased, in text order
    (duplicates kept — the caller decides). This is the parser REFLECT folds
    into ``claims`` / ``cited_substrate_refs``; the gate's wider
    :func:`extract_inline_refs` already counts both forms."""
    if not text:
        return []
    out: list[str] = []
    for m in _MARKER_OR_NESTED_RE.finditer(text):
        if m.group(1):
            out.append(m.group(1).lower())
        else:
            out.extend(u.lower() for u in _UUID_RE.findall(m.group(0)))
    return out


def collect_refs(structured: Iterable[Any] | None, body: str | None) -> list[str]:
    """THE ref set the gate judges: the structured ``cited_substrate_refs`` UNION
    the inline markers in ``body`` (every :data:`INLINE_REF_RE` form, the
    nested ``[[ref:a] [ref:b]]`` group included — :func:`marker_refs`),
    lower-cased and de-duplicated. The union only widens what is COUNTED; each
    id must still resolve, so invented ids fail."""
    out = [str(r).lower() for r in (structured or ())]
    out.extend(marker_refs(body))
    out.extend(extract_inline_refs(body))
    return list(dict.fromkeys(out))

#: Fraction of cited refs that must resolve to a stored row for a draft to
#: publish as written. Good entries resolve ≥ 92 %; fabricated ones 0–11 %.
MIN_REF_RESOLVE_RATIO = 0.8

#: The honesty flag on a declared "no entry" row. Readers that feed earlier
#: rows back to a model skip any row carrying it.
EVIDENCE_UNAVAILABLE_FLAG = "evidence_unavailable"

#: The tiers the publish gate covers — every journal-family entry kind that is
#: written from gathered evidence. ``correction`` is written by an operator
#: script from stored ids, never by a model, and is not gated.
GATED_ENTRY_KINDS = frozenset({
    "entry", "consolidation", "chronicle", "lens", "lens_diff",
    "inquiry", "crossroads",
})

#: Tiers whose rows are legitimately citation-free by design (measured 11-13):
#: a crossroads entry cites its block's ordinals, not uuids; an inquiry's
#: opening cycle reads an empty ledger; a lens opens on collection posture and,
#: on an empty pull, says so ("the substrate is silent") rather than invent.
#: For these, ZERO refs alone never withholds — only control text, a stub /
#: tool-call body, or cited refs that do not resolve do. ``entry``,
#: ``consolidation``, ``chronicle`` and ``lens_diff`` keep the zero-refs bar.
CITATION_OPTIONAL_KINDS = frozenset({"lens", "inquiry", "crossroads"})

#: An uncited body shorter than this is a stub, not an entry. Every cited live
#: row is ≥ 600 chars; every uncited one under it is a stub ("Pulling the
#: tower top now.", a question back to the operator, a pseudo tool call). The
#: shortest honest "nothing came back" lens note runs 1,993.
STUB_MIN_CHARS = 600

#: A pseudo tool call written as the body (``{"tool": "search", "args": …}``,
#: ``{"query_facts": {…}}``).
TOOL_CALL_BODY_RE = re.compile(
    r'\{\s*"(?:tool|name|function|args|arguments|query_[a-z_]+)"\s*:', re.IGNORECASE,
)

#: The entry kind of an append-only correction row (see journal_correction.py).
CORRECTION_ENTRY_KIND = "correction"

DECLARED_TITLE = "No entry this window: evidence handoff failed"

#: Every table a journal ``[[ref:]]`` can legitimately point at. The same set the
#: 11-13 investigation resolved against; a uuid found in none of them is a ref
#: to nothing. ``journal_entries`` is included because a chorus diff cites the
#: lens rows it referees.
REF_TABLES: tuple[str, ...] = (
    "signals", "analyst_outputs", "facts", "events", "situations",
    "journal_entries", "hypotheses", "nexuses", "entity_profiles",
    "acute_forecasts", "situation_events", "proposed_edges", "entity_edges",
    "claim_contentions", "fact_contention", "unit_correctness_claims",
)

#: The predicate (alias-free, AND-joinable) a memory reader appends to its
#: ``journal_entries`` WHERE clause: open, never superseded or corrected, and not
#: a declared evidence-unavailable row.
MEMORY_ROW_FILTER_SQL = (
    "valid_until IS NULL AND superseded_by IS NULL "
    f"AND NOT ('{EVIDENCE_UNAVAILABLE_FLAG}' = ANY(honesty_flags))"
)

# ---------------------------------------------------------------------------
# Control text — the ONE pattern list.
# ---------------------------------------------------------------------------

#: Model/harness control text that is never journal prose. Seen live 11-12:
#: ``<system-warning>…`` and ``<FORCED_TOOL_CALL> make_fragmented_tool_call_001``
#: (model-written, then copied by the consolidator), and ``[[call
#: get_journal_delta]]`` pseudo calls in collapsed field notes. The tag forms
#: cover the family (system-*, tool/function-call blocks, chat special tokens).
CONTROL_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("system_tag", re.compile(
        r"<\s*/?\s*system[-_ ]?(?:warning|reminder|message|prompt|note|notice)\b",
        re.IGNORECASE)),
    ("forced_tool_call", re.compile(r"FORCED_TOOL_CALL", re.IGNORECASE)),
    ("make_tool_call", re.compile(r"\bmake_\w*tool_call\w*", re.IGNORECASE)),
    ("tool_call_tag", re.compile(
        r"<\s*/?\s*(?:tool_call|tool_use|tool_result|function_call|function_calls|"
        r"invoke|parameter)\b", re.IGNORECASE)),
    ("pseudo_call_marker", re.compile(r"\[\[\s*call\s+\w+", re.IGNORECASE)),
    ("chat_special_token", re.compile(r"<\|[a-z_]{2,40}\|>", re.IGNORECASE)),
)


def find_control_text(text: str | None) -> list[str]:
    """Names of every control-text pattern ``text`` matches (``[]`` = clean)."""
    if not text:
        return []
    return [name for name, rx in CONTROL_TEXT_PATTERNS if rx.search(text)]


# ---------------------------------------------------------------------------
# FIELD-NOTES gate.
# ---------------------------------------------------------------------------


def notes_degenerate_reason(notes: str | None) -> str | None:
    """Why ``notes`` cannot stand as the NARRATE handoff, or ``None`` when they
    can. Empty notes return ``"empty"`` (the pre-existing fallback)."""
    text = (notes or "").strip()
    if not text:
        return "empty"
    if len(text) < NOTES_MIN_CHARS:
        return "too_short"
    if REF_MARKER not in text and not extract_inline_refs(text):
        return "no_ref_markers"
    if find_control_text(text):
        return "control_text"
    return None


#: Appended to the FIELD-NOTES prompt. The call is offered no tools, but the
#: gathered prompt above it was written for the tool-using GATHER phase; since
#: the 10-23 cutover the model has answered that with "let me pull my
#: instruments" and pseudo tool calls instead of notes.
NOTES_NO_TOOLS_PREFACE = (
    "\n\nGATHERING IS OVER. Everything you read is in the material above — "
    "that is your whole evidence for this step. You have NO tools in this "
    "step: do not call, announce, plan or simulate a tool call (no JSON, no "
    "[[call ...]], no tags). Any earlier instruction to call a tool belonged "
    "to the gathering phase and no longer applies. Write from the material "
    "above, citing it."
)


def notes_prompt(base_prompt: str, instruction: str, strip: Iterable[str] = ()) -> str:
    """The FIELD-NOTES user prompt: the gathered prompt with every gather-phase
    tool orientation in ``strip`` removed, then the no-tools preface, then the
    tier's notes instruction."""
    prompt = base_prompt
    for orientation in strip:
        if orientation:
            prompt = prompt.replace(orientation, "")
    return prompt + NOTES_NO_TOOLS_PREFACE + instruction


def notes_are_degenerate(notes: str | None) -> bool:
    return notes_degenerate_reason(notes) is not None


#: The ``steps`` kind ``_field_notes`` appends when it discards the notes.
FIELD_NOTES_FALLBACK_KIND = "degenerate_fallback"


def field_notes_fallback_record(steps: Iterable[Any]) -> dict[str, Any] | None:
    """The row-data stamp for a run whose field notes were discarded:
    ``{"reason", "notes_chars"}`` from the trace step, or ``None``."""
    for s in steps or ():
        if (
            isinstance(s, dict)
            and s.get("phase") == "field_notes"
            and s.get("kind") == FIELD_NOTES_FALLBACK_KIND
        ):
            return {"reason": s.get("reason"), "notes_chars": s.get("notes_chars", 0)}
    return None


# ---------------------------------------------------------------------------
# PUBLISH gate.
# ---------------------------------------------------------------------------


def _as_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


#: The creation-time column per :data:`REF_TABLES` table (default ``created_at``)
#: — a prefix ref resolves only to a row that existed before the citing row.
REF_TABLE_TIME_COLUMN: dict[str, str] = {
    "acute_forecasts": "issued_at", "claim_contentions": "retrieved_at",
}

#: Rows read per prefix per table. Two already make a prefix ambiguous; the cap
#: keeps every probe a bounded primary-key range scan (live EXPLAIN 11-13 on
#: proposed_edges, 1.34 M rows: Index Scan on the pkey, cost 36, 0.3 ms).
PREFIX_SCAN_CAP = 16

_PREFIX_SQL = (
    "SELECT p.lo::text AS lo, t.id::text AS id, t.ts AS ts "
    "FROM unnest($1::uuid[], $2::uuid[]) AS p(lo, hi) "
    "JOIN LATERAL (SELECT id, {ts} AS ts FROM {table} "
    "WHERE id >= p.lo AND id <= p.hi ORDER BY id LIMIT {cap}) t ON true"
)


def _as_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class ResolvedRefs(set):
    """The resolved ref set, plus how the short-id refs resolved: ``by_prefix``
    (the padded refs counted as resolved) and ``prefix_matches`` (padded ref →
    every ``(table, id, created)`` its prefix matched, capped), so a caller
    judging many rows at once can re-cut it per citing row (:meth:`as_of`)."""

    by_prefix: frozenset[str] = frozenset()
    prefix_matches: dict[str, list[tuple[str, str, Any]]] = {}

    def as_of(self, before: Any) -> "ResolvedRefs":
        """A copy whose prefix refs resolve only to a row created strictly
        before ``before`` (``None`` = no cut), and only when EXACTLY ONE row
        qualifies; an ambiguous or empty prefix stays unresolved."""
        cut = _as_dt(before)
        by_prefix: set[str] = set()
        for ref, hits in self.prefix_matches.items():
            rows = {
                (t, i) for t, i, ts in hits
                if cut is None or ((d := _as_dt(ts)) is not None and d < cut)
            }
            capped = any(n >= PREFIX_SCAN_CAP for n in Counter(t for t, _, _ in hits).values())
            if not capped and len(rows) == 1:
                by_prefix.add(ref)
        out = ResolvedRefs((self - self.by_prefix) | by_prefix)
        out.by_prefix = frozenset(by_prefix)
        out.prefix_matches = self.prefix_matches
        return out


async def _prefix_matches(
    conn: Any, prefix_refs: Sequence[str],
) -> dict[str, list[tuple[str, str, Any]]]:
    """Every row (capped per table) whose id starts with each padded ref's
    8-char prefix: ONE batched primary-key range query per table."""
    los = [r[:8] + "-0000-0000-0000-000000000000" for r in prefix_refs]
    his = [r[:8] + "-ffff-ffff-ffff-ffffffffffff" for r in prefix_refs]
    out: dict[str, list[tuple[str, str, Any]]] = {r: [] for r in prefix_refs}
    for table in REF_TABLES:
        sql = _PREFIX_SQL.format(
            ts=REF_TABLE_TIME_COLUMN.get(table, "created_at"), table=table,
            cap=PREFIX_SCAN_CAP,
        )
        try:
            rows = await conn.fetch(sql, los, his)
        except Exception as exc:  # noqa: BLE001 — one missing table never fails the gate
            logger.warning("journal_evidence.prefix_failed table=%s err=%s", table, exc)
            continue
        for r in rows:
            out.setdefault(str(r["lo"]).lower(), []).append(
                (table, str(r["id"]).lower(), r["ts"]),
            )
    return out


async def resolve_ref_existence(
    conn: Any, refs: Sequence[Any], *, before: Any = None,
) -> ResolvedRefs | None:
    """The subset of ``refs`` (lower-case strings) that exist in any
    :data:`REF_TABLES` table. Read-only, one indexed lookup per table. A table
    whose lookup fails is skipped; ``None`` when EVERY lookup failed (the
    caller then cannot judge the ratio and must not pretend it can).

    A padded short-id ref (:func:`is_prefix_ref`) that does not match exactly
    resolves when its 8-char prefix matches EXACTLY ONE row across the tables,
    created before ``before`` (``None`` = any stored row — the live gate runs
    before the citing row exists). Those refs are on ``.by_prefix``."""
    exact = await _resolve_exact(conn, refs)
    if exact is None:
        return None
    out = ResolvedRefs(exact)
    pending = sorted({
        str(r).lower() for r in refs if is_prefix_ref(r) and str(r).lower() not in exact
    })
    if pending:
        out.prefix_matches = await _prefix_matches(conn, pending)
    return out.as_of(before)


async def _resolve_exact(conn: Any, refs: Sequence[Any]) -> set[str] | None:
    uuids = list({u for u in (_as_uuid(r) for r in refs) if u is not None})
    if not uuids:
        return set()
    found: set[str] = set()
    failures = 0
    for table in REF_TABLES:
        remaining = [u for u in uuids if str(u) not in found]
        if not remaining:
            break
        try:
            rows = await conn.fetch(
                f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", remaining,
            )
        except Exception as exc:  # noqa: BLE001 — one missing table never fails the gate
            failures += 1
            logger.warning("journal_evidence.resolve_failed table=%s err=%s", table, exc)
            continue
        found.update(str(r["id"]).lower() for r in rows)
    if failures == len(REF_TABLES):
        return None
    return found


@dataclass(frozen=True)
class GateVerdict:
    withhold: bool
    reason: str | None
    refs_total: int
    refs_resolved: int | None
    control_text: tuple[str, ...] = field(default_factory=tuple)
    #: Of ``refs_resolved``, how many resolved by a unique short-id prefix
    #: rather than an exact id — visible on every gate stamp.
    refs_resolved_by_prefix: int = 0

    @property
    def ratio(self) -> float | None:
        if self.refs_resolved is None or not self.refs_total:
            return None
        return self.refs_resolved / self.refs_total

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "refs_total": self.refs_total,
            "refs_resolved": self.refs_resolved,
            "refs_resolved_by_prefix": self.refs_resolved_by_prefix,
            "min_ratio": MIN_REF_RESOLVE_RATIO,
            **({"control_text": list(self.control_text)} if self.control_text else {}),
        }


def hollow_body_reason(body: str | None) -> str | None:
    """``"tool_call_body"`` / ``"stub_body"`` for a body that is a pseudo tool
    call or shorter than :data:`STUB_MIN_CHARS`, else ``None``."""
    text = (body or "").strip()
    if len(text) >= STUB_MIN_CHARS:
        return None
    return "tool_call_body" if TOOL_CALL_BODY_RE.search(text) else "stub_body"


def evaluate_publish_gate(
    *, body: str | None, refs: Sequence[Any], resolved: set[str] | None,
    entry_kind: str | None = None,
) -> GateVerdict:
    """Pure. Withhold when the body carries control text, cites nothing, or
    fewer than :data:`MIN_REF_RESOLVE_RATIO` of its refs resolve. ``refs`` is
    the :func:`collect_refs` union. For :data:`CITATION_OPTIONAL_KINDS` an
    uncited row is withheld only when it is a stub / tool-call body. ``resolved
    is None`` (resolution unavailable) gates on control text and zero refs
    only."""
    ref_ids = list(dict.fromkeys(str(r).lower() for r in refs))
    total = len(ref_ids)
    n_resolved = None if resolved is None else sum(1 for r in ref_ids if r in resolved)
    by_prefix = getattr(resolved, "by_prefix", None) or ()
    n_prefix = sum(1 for r in ref_ids if r in by_prefix)
    control = tuple(find_control_text(body))
    if control:
        return GateVerdict(True, "control_text", total, n_resolved, control, n_prefix)
    if total == 0:
        if entry_kind in CITATION_OPTIONAL_KINDS:
            stub = hollow_body_reason(body)
            return GateVerdict(bool(stub), stub, 0, 0)
        return GateVerdict(True, "no_refs", 0, 0)
    if n_resolved is not None and n_resolved / total < MIN_REF_RESOLVE_RATIO:
        return GateVerdict(True, "refs_unresolved", total, n_resolved, (), n_prefix)
    return GateVerdict(False, None, total, n_resolved, (), n_prefix)


def verdict_detail(verdict: GateVerdict) -> str:
    """One clause saying what failed, with the counts."""
    if verdict.reason == "control_text":
        detail = (
            "the draft carried model control text ("
            + ", ".join(verdict.control_text) + ") and was refused"
        )
    elif verdict.reason == "no_refs":
        detail = "the draft cited no stored evidence"
    elif verdict.reason == "tool_call_body":
        detail = "the draft was a tool call, not an entry"
    elif verdict.reason == "stub_body":
        detail = "the draft was an uncited stub, not an entry"
    else:
        detail = f"{verdict.refs_resolved} of {verdict.refs_total} cited refs resolve"
        if verdict.refs_resolved_by_prefix:
            detail += f" ({verdict.refs_resolved_by_prefix} by unique short-id prefix)"
    return detail


def declared_body(verdict: GateVerdict) -> str:
    """The declared row's body: what failed, with the counts."""
    return (
        f"{DECLARED_TITLE} — {verdict_detail(verdict)}. The draft was withheld; "
        "nothing in it is published or remembered."
    )


def draft_sha256(body: str | None) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def declared_unavailable_payload(payload: Any, verdict: GateVerdict) -> Any:
    """A copy of ``payload`` (a ``JournalPayload``) carrying the declared
    "No entry this window" row in place of the draft: no claims, no refs, the
    ``evidence_unavailable`` flag, and the gate counts + the draft's sha256 in
    ``data.evidence_gate``. The draft text itself goes on the trace, never on
    the served row."""
    data = dict(getattr(payload, "data", None) or {})
    data["evidence_gate"] = {
        **verdict.as_dict(),
        "withheld_draft_sha256": draft_sha256(getattr(payload, "body", "")),
        "withheld_draft_chars": len(getattr(payload, "body", "") or ""),
    }
    return payload.model_copy(update={
        "title": DECLARED_TITLE,
        "body": declared_body(verdict),
        "claims": [],
        "cited_substrate_refs": [],
        "honesty_flags": [EVIDENCE_UNAVAILABLE_FLAG],
        "data": data,
    })


def withheld_draft_step(payload: Any, verdict: GateVerdict) -> dict[str, Any]:
    """The trace step that keeps the withheld draft (audit only)."""
    return {
        "phase": "publish_gate",
        "kind": EVIDENCE_UNAVAILABLE_FLAG,
        **verdict.as_dict(),
        "withheld_draft": {
            "title": getattr(payload, "title", ""),
            "body": getattr(payload, "body", ""),
            "cited_substrate_refs": [
                str(r) for r in (getattr(payload, "cited_substrate_refs", None) or [])
            ],
            "honesty_flags": list(getattr(payload, "honesty_flags", None) or []),
            "sha256": draft_sha256(getattr(payload, "body", "")),
        },
    }


__all__ = [
    "CITATION_OPTIONAL_KINDS",
    "CONTROL_TEXT_PATTERNS",
    "INLINE_REF_RE",
    "PREFIX_REF_SUFFIX",
    "PREFIX_SCAN_CAP",
    "REF_TABLE_TIME_COLUMN",
    "ResolvedRefs",
    "SHORT_REF_RE",
    "is_prefix_ref",
    "STUB_MIN_CHARS",
    "TOOL_CALL_BODY_RE",
    "collect_refs",
    "extract_inline_refs",
    "hollow_body_reason",
    "CORRECTION_ENTRY_KIND",
    "DECLARED_TITLE",
    "EVIDENCE_UNAVAILABLE_FLAG",
    "FIELD_NOTES_FALLBACK_KIND",
    "GATED_ENTRY_KINDS",
    "GateVerdict",
    "MEMORY_ROW_FILTER_SQL",
    "MIN_REF_RESOLVE_RATIO",
    "NOTES_NO_TOOLS_PREFACE",
    "NOTES_MIN_CHARS",
    "REF_MARKER",
    "REF_TABLES",
    "declared_body",
    "declared_unavailable_payload",
    "verdict_detail",
    "draft_sha256",
    "evaluate_publish_gate",
    "field_notes_fallback_record",
    "find_control_text",
    "notes_are_degenerate",
    "notes_degenerate_reason",
    "notes_prompt",
    "resolve_ref_existence",
    "withheld_draft_step",
]
