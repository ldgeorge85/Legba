# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-177 cross_analyst_correlator analyst kind.

Reads multiple analysts' outputs (``FindingPayload``, ``PredictionPayload``,
``MetaFindingPayload``, etc.) — broad subscription predicate matching many
analyst_ids — and emits one structured :class:`FindingPayload` reporting:

  * **contradictions** — two or more analysts whose outputs disagree on the
    same target / topic;
  * **agreements**     — clusters of analysts converging on the same claim;
  * **blind_spots**    — gaps where the analyst set as a whole has nothing
    to say about a topic the substrate clearly tracks.

This realises topology-redesign v2 §5.8 — and §6.4's "the global situational
awareness coordinator is not a special construct, it is a
``cross_analyst_correlator`` analyst with a broad subscription."

Per topology-redesign v2 §5.8::

    Reads:  multiple analysts' outputs simultaneously
    Method: LLM planner specialized in finding correlations, contradictions,
            agreement clusters
    Writes: meta-meta findings

Per L-102 / kind_contracts §5: this kind's runtime invocation goes through
:func:`run_method`. The runtime in :mod:`legba.runtime.dapr_actors` calls
``await deps_bundle.run_method(inputs, options)`` (two-arg shape). The host
wraps :func:`run_method` with the activation-time ``deps`` via
:class:`CrossAnalystCorrelatorRunner` (matches the
:class:`legba.data.analysts.inline_target.InlineTargetRunner` adapter
pattern) so the wire-level signature stays 2-arg while the kind-internal
signature carries ``deps`` explicitly.

Output payload shape
~~~~~~~~~~~~~~~~~~~~

The kind emits a single :class:`FindingPayload`. Per the brief, the
correlation metadata lives in the payload's ``data`` dict (the
``_AnalystOutputBase`` model uses ``extra="forbid"``, so custom keys must
nest under ``data``):

  * ``data["correlation_type"]``       — one of ``"contradiction"``,
                                          ``"agreement"``, ``"blind_spot"``.
  * ``data["referenced_outputs"]``     — list of analyst output UUIDs the
                                          correlation cites.
  * ``data["referenced_analyst_ids"]`` — the cited analysts' ids, parallel-
                                          ordered to ``referenced_outputs``
                                          where possible.
  * ``data["raw_llm_response"]``       — the LLM's raw JSON for audit.

The substrate-level ``derived_from`` (set by the runtime from
``inputs[i]["id"]`` in ``dapr_actors._read_substrate_slice`` callers) gives
the broader lineage for free — every analyst output the correlator read
shows up there. ``referenced_outputs`` is the *narrower* set the LLM
actually cited; lineage queries can use either.

Why both: ``derived_from`` is what the substrate's lineage walker uses for
reachability ("which outputs influenced this?"); ``referenced_outputs``
records the LLM's explicit claim ("these two analysts contradict each
other"), which is what the operator UI surfaces.

Detection structure in the prompt
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The system prompt frames the task as three explicit detector questions
(rather than one open-ended "find correlations") so a small / fast LLM
keeps the discrimination signal sharp:

  1. **Contradiction detector** — "If two outputs claim mutually exclusive
     facts about the same target, return correlation_type='contradiction'
     and cite both."
  2. **Agreement detector** — "If three or more outputs converge on the
     same claim, return correlation_type='agreement' and cite the cluster."
  3. **Blind-spot detector** — "If the input set covers topic X via raw
     substrate but no analyst output mentions X, return
     correlation_type='blind_spot'."

The detector priority order is contradiction > blind_spot > agreement —
contradictions are the highest-leverage signal for an operator (they
demand resolution), blind spots indicate missing analytical coverage,
agreements are the lowest-priority confirmation signal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

from ..provenance.models import FindingPayload
from ...runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity (registry key — see kind_contracts §1)
# ---------------------------------------------------------------------------


KIND_NAME: str = "cross_analyst_correlator"
SCHEMA_VERSION: str = "legba/analyst.cross_analyst_correlator/1-0-0"
HANDLER_VERSION: str = "0.1.0"
PROMPT_MODULE_PATH: str = "legba.prompts.cross_analyst_correlator.v1"

# Host-discovered constants — see L-211/L-241 integration pass.
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402

OUTPUT_KIND: _OutputKind = _OutputKind.FINDING


# Correlation types — closed enum surfaced as a Literal so callers can
# validate before round-tripping through JSON.
CorrelationType = Literal["contradiction", "agreement", "blind_spot"]
_VALID_CORRELATION_TYPES: frozenset[str] = frozenset(
    ["contradiction", "agreement", "blind_spot"]
)


# ---------------------------------------------------------------------------
# LLM port — same structural shape used by inline_target / deterministic.
# Duplicated here rather than imported from inline_target to avoid coupling
# kinds to each other (a kind-module reorg should not need to touch siblings).
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMHandlerLike(Protocol):
    """Minimum slice of ``LLMProviderHandler`` this kind depends on.

    Same shape as :class:`legba.data.analysts.inline_target.LLMHandlerLike`
    and the structural type :class:`legba.runtime.analyst_method.LLMHandlerLike`
    used by the spike (L-002a). The duplication is intentional — kinds are
    pluggable, and importing across kinds would break that.
    """

    subprovider: str

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Deps bundle the host injects at activation time
# ---------------------------------------------------------------------------


@dataclass
class CrossAnalystCorrelatorDeps:
    """Deps bundle the host injects at activation time.

    Resolved from the analyst descriptor's ``method.llm.primary`` StackRef.
    The runtime stores this on ``_AnalystDeps.run_method`` via
    :class:`CrossAnalystCorrelatorRunner`'s closure — handlers don't reach into
    the descriptor directly.

    ``max_tokens`` / ``temperature`` / ``system_prompt`` below are NOT
    descriptor-settable: the builder constructs this bundle as
    ``CrossAnalystCorrelatorDeps(llm=llm)`` (``analyst_deps_builder``), so the
    dataclass defaults are the only reachable values. X-1 made
    ``method.options`` real, but scoped it to ``kind=deterministic`` — this is
    an LLM kind, and the schema refuses an options block on it rather than
    letting one sit silently inert. Widening that is a declared follow-on, not
    an implied capability.
    """

    llm: LLMHandlerLike
    max_tokens: int = 2048
    temperature: float = 0.1                                # low — we want
    #                                                         consistent
    #                                                         categorisation
    system_prompt: str | None = None                        # None → default


# ---------------------------------------------------------------------------
# Input shaping — these are analyst-output rows, not raw signals
# ---------------------------------------------------------------------------


_MAX_INPUT_OUTPUTS = 40             # context budget; broad subscription
#                                     can return more, we trim newest-first
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 800

# M16 — the composition-style ``[[ref:N]]`` ordinal citation marker. A 1-BASED
# ordinal (small int) naming the position of the cited analyst-output block in the
# rendered slice (the SAME ``enumerate(projected, start=1)`` index the render
# stamps as ``[N]``). Wrapped ``[[ref:...]]`` so the generalized faithfulness
# verify's syntax discriminator routes it to the composition sub-claim floor
# (``_uses_subclaim_convention`` keys on ``ref_kind='finding'`` / ``[[ref:``),
# exactly as the meta_findings_synthesizer compositions do.
_REF_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]")

# Bound the per-citation evidence text stashed into ``data['citations']`` so the
# DB-free composition verify has the cited output's body without bloating the row.
_MAX_EVIDENCE_TEXT_CHARS = 600

# M17 — the cross_correlator supersession-signature prefix. Distinct from the
# composition ``composition:`` prefix (so the FU6 composition fold never touches a
# correlation head, and vice-versa) and from the content ``sig:`` / explicit
# ``sit:`` prefixes. Encodes correlation_type + the sorted referenced-TARGET set
# so a fresh run about the same targets supersedes the prior head instead of the
# feed accumulating one meta-observation head per cycle (the ~32-stale-head symptom).
_XCORR_SIG_PREFIX = "xcorr"

# M17 — blind_spot decay TTL (hours). A blind_spot that is STILL a real coverage
# gap is re-asserted every cadence (~12h) and its fresh head supersedes the prior
# same-signature head (so its produced_at keeps refreshing and it never ages out);
# only a blind_spot the correlator has STOPPED asserting (the gap was filled / the
# topic lapsed) ages past this TTL and decays. 72h = 6 cadence cycles of margin.
# Kept in sync with migration 0079's stale-sweep cutoff.
_BLIND_SPOT_DECAY_TTL_HOURS = 72


def _situation_signature(
    correlation_type: str,
    referenced_target_ids: Sequence[str],
) -> str:
    """Stable per-correlation supersession signature.

    Keyed on ``correlation_type`` + the SORTED, de-duplicated set of non-empty
    ``target_id`` values among the referenced outputs. Two runs that report the
    same relationship (e.g. a blind_spot over the same countries, a contradiction
    between the same targets) hash to the SAME signature, so the newer head
    supersedes the older via the write-path fold. When the referenced set is
    entirely target-less (all-global reads) the token is ``_global`` — those
    collapse to one rolling global head per correlation_type (bounded, and the
    blind_spot decay expires an abandoned one).

    A very long joined target list is replaced by a short stable hash so the
    signature stays a compact, index-friendly key.
    """
    targets = sorted({str(t) for t in referenced_target_ids if t})
    token = ",".join(targets) if targets else "_global"
    if len(token) > 180:
        token = hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]
    ct = correlation_type if correlation_type in _VALID_CORRELATION_TYPES else "blind_spot"
    return f"{_XCORR_SIG_PREFIX}:{ct}:{token}"


def _dedupe_live_heads(
    inputs: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """M18(a) — collapse the slice to ONE head per
    ``(analyst_id, target_id, situation_signature)`` BEFORE correlation.

    The correlator reads a 24h window; a signature-bearing producer (the
    world/region/country compositions, or the correlator itself) can have a prior
    head STILL inside the window during the brief write-to-supersede lag. Diffing a
    head against its own just-superseded predecessor manufactures a false
    'contradiction'. This keeps only the NEWEST row per signature key so two
    sequential snapshots of the SAME producer/signature are never compared as if
    simultaneous. Rows with NO signature (first-order unit findings) are passed
    through untouched — they carry no head identity to collapse, and the
    ``superseded_by IS NULL`` READ_SLICE filter already excludes their retired
    versions.
    """
    def _sig(row: Mapping[str, Any]) -> str | None:
        sig = row.get("situation_signature")
        if sig:
            return str(sig)
        data = row.get("data")
        if isinstance(data, Mapping):
            inner = data.get("data")
            if isinstance(inner, Mapping) and inner.get("situation_signature"):
                return str(inner["situation_signature"])
        return None

    def _ts(row: Mapping[str, Any]) -> Any:
        return row.get("produced_at") or row.get("created_at") or ""

    best: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    passthrough: list[Mapping[str, Any]] = []
    for row in inputs:
        sig = _sig(row)
        if not sig:
            passthrough.append(row)
            continue
        key = (
            str(row.get("analyst_id") or ""),
            str(row.get("target_id") or ""),
            sig,
        )
        cur = best.get(key)
        if cur is None or str(_ts(row)) > str(_ts(cur)):
            best[key] = row
    return passthrough + list(best.values())


def _coerce_uuid(value: Any) -> UUID | None:
    """Best-effort UUID coercion. Returns ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _output_row_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one analyst-output row into the compact form the prompt sees.

    The runtime can read analyst outputs from several tables (``signals``
    plays no role for this kind — the relevant tables are
    ``analyst_outputs`` for findings/critiques/meta_findings/alerts, plus
    the dedicated ``predictions`` / ``hypotheses`` / ``situations``
    tables). We tolerate any column-projection shape — extract what's
    there, default the rest.
    """
    output_id = _coerce_uuid(row.get("id") or row.get("output_id"))
    analyst_id = (
        row.get("analyst_id")
        or row.get("produced_by_analyst_id")
        or row.get("analyst")
        or ""
    )
    analyst_version = row.get("analyst_version") or ""
    output_kind = (
        row.get("kind")
        or row.get("output_kind")
        or row.get("payload_kind")
        or "finding"
    )
    payload = row.get("payload") or row.get("data") or {}
    if not isinstance(payload, dict):
        payload = {}
    title = str(
        row.get("title")
        or payload.get("title")
        or payload.get("name")
        or payload.get("thesis")
        or payload.get("hypothesis")
        or "(untitled)"
    )[:_MAX_TITLE_CHARS]
    body = str(
        row.get("body")
        or payload.get("body")
        or payload.get("counter_thesis")
        or ""
    )[:_MAX_BODY_CHARS]
    confidence = row.get("confidence")
    if confidence is None:
        confidence = payload.get("confidence", 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    target_id = (
        row.get("target_id")
        or payload.get("target_id")
        or payload.get("target_ref")
        or ""
    )
    produced_at = row.get("produced_at") or row.get("created_at")
    tags: list[str] = []
    raw_tags = row.get("tags") or payload.get("tags") or []
    if isinstance(raw_tags, (list, tuple)):
        tags = [str(t)[:64] for t in raw_tags][:20]
    return {
        "output_id": str(output_id) if output_id else "",
        "analyst_id": str(analyst_id),
        "analyst_version": str(analyst_version),
        "kind": str(output_kind),
        "target_id": str(target_id),
        "title": title,
        "body": body,
        "confidence": confidence,
        "tags": tags,
        "produced_at": str(produced_at) if produced_at else "",
    }


def _orient(
    inputs: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[UUID], set[str]]:
    """Sort newest-first, trim to :data:`_MAX_INPUT_OUTPUTS`.

    Returns:
      * the trimmed projected rows (LLM-facing);
      * the list of analyst-output UUIDs (``derived_from`` lineage);
      * the set of distinct ``analyst_id`` values present in the slice.

    The set of analyst_ids is included so the prompt can frame the blind-
    spot detector ("the analyst set is {X, Y, Z}; if topic T is in the
    inputs but none of {X,Y,Z} covers it, that is a blind_spot").
    """
    def _sort_key(row: Mapping[str, Any]) -> tuple[bool, Any]:
        ts = row.get("produced_at") or row.get("created_at")
        return (ts is None, ts)

    ordered = sorted(inputs, key=_sort_key, reverse=True)
    trimmed = ordered[:_MAX_INPUT_OUTPUTS]
    projected = [_output_row_summary(r) for r in trimmed]
    derived_from: list[UUID] = []
    analyst_ids: set[str] = set()
    for p in projected:
        oid = _coerce_uuid(p["output_id"])
        if oid is not None:
            derived_from.append(oid)
        if p["analyst_id"]:
            analyst_ids.add(p["analyst_id"])
    logger.debug(
        "cross_analyst_correlator.orient in=%d kept=%d derived=%d analysts=%d",
        len(inputs),
        len(trimmed),
        len(derived_from),
        len(analyst_ids),
    )
    return projected, derived_from, analyst_ids


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


from ._tradecraft import with_preamble  # noqa: E402

_DEFAULT_SYSTEM_PROMPT = with_preamble(
    """TASK — correlate the outputs of other analysts. From the slice of recent analyst outputs (findings, predictions, meta-findings, etc.), identify the single most important cross-analyst relationship.
Detector priority (apply in order; pick the highest-priority hit):
  1. CONTRADICTION — two or more outputs claim mutually exclusive facts about the same target / topic / time window. Highest-leverage: contradictions demand operator resolution.
  2. BLIND_SPOT — a topic clearly present across multiple inputs (via target_id, tags, or shared entities) has no analyst output addressing it. Cite the inputs that establish the topic and say no analyst covers it.
  3. AGREEMENT — three or more outputs converge on the same claim. Lowest priority; useful as confidence reinforcement.
Cite the specific analyst output UUIDs you used. For contradictions and agreements you MUST cite at least two distinct analyst_id values. For blind_spots, cite the outputs that establish the unaddressed topic.
GROUNDING — in the body, CITE every factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as [N] at the START of the analyst-output block it rests on (the same N is the block's position in the list below). NEVER invent an N and NEVER cite an N not shown; a clause with no cited output behind it must NOT assert a fact. A [[ref:N]] is a PROMISE that block N actually says, in substance, what the clause claims — this is what the faithfulness verify grades, so an ungrounded claim is demoted.
Respond with strict JSON, nothing else:
{"correlation_type": "contradiction" | "agreement" | "blind_spot", "title": "...", "body": "... (explain the relationship; reference analysts by id)", "referenced_outputs": ["<uuid>", ...], "referenced_analyst_ids": ["<analyst_id_a>", "<analyst_id_b>", ...], "confidence": 0.0-1.0, "tags": ["..."]}"""
)


def _render_user_prompt(
    projected: list[dict[str, Any]],
    analyst_ids: set[str],
) -> str:
    """Render the projected analyst-output slice into a user prompt."""
    header = (
        f"Distinct analyst_ids in this slice ({len(analyst_ids)}): "
        f"{sorted(analyst_ids) if analyst_ids else '(none)'}\n"
        f"Number of analyst outputs: {len(projected)}\n\n"
    )
    body_lines: list[str] = []
    for i, p in enumerate(projected, start=1):
        body_lines.append(
            f"[{i}] output_id={p['output_id']}\n"
            f"    analyst_id={p['analyst_id']} kind={p['kind']} "
            f"target={p['target_id']} confidence={p['confidence']:.2f}\n"
            f"    title: {p['title']}\n"
            f"    body:  {p['body']}\n"
            f"    tags:  {p['tags']}"
        )
    return header + "\n".join(body_lines)


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def _strip_code_fence(raw: str) -> str:
    """Strip a leading ```json fence + trailing garbage past the closing ``}``."""
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    if candidate.startswith("{"):
        depth = 0
        end = len(candidate)
        for i, c in enumerate(candidate):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        candidate = candidate[:end]
    return candidate


def _coerce_correlation(
    raw: str,
    *,
    fallback_title: str,
    valid_output_ids: set[str],
    valid_analyst_ids: set[str],
    contributing_analysts: Sequence[str] = (),
    projected: Sequence[Mapping[str, Any]] | None = None,
) -> FindingPayload:
    """Parse the LLM's JSON into a :class:`FindingPayload` with correlation metadata.

    Validates against the input slice — ``referenced_outputs`` / ``referenced_
    analyst_ids`` are filtered to those actually present in the slice, so a
    hallucinated UUID can't make it into the substrate. Filtered references
    drop silently; if the result drops below the minimum for the claimed
    correlation_type (≥2 distinct analysts for contradiction/agreement), the
    correlation_type is downgraded with the ``downgraded`` tag so the
    operator can see what happened.

    Always stamps ``data.meta = True`` and ``data.contributing_analysts`` —
    the same second-order-finding schema contract
    :mod:`meta_findings_synthesizer` honours (``_coerce_finding`` there). The
    correlator is a second-order (meta) producer too; without these marks its
    output carried ``contributing_analysts=NULL`` and downstream meta-filters
    that join on the data dict could not see it. ``contributing_analysts`` is
    the set of distinct ``analyst_id`` values present in the slice the LLM
    reasoned over (the broad set), independent of the narrower
    ``referenced_analyst_ids`` the LLM explicitly cited.

    Malformed JSON → fallback to an "unstructured" finding. The runtime's
    ``write_analyst_output`` will revalidate against the iglu schema and
    DLQ-route anything still wrong (per L-107 §6).
    """
    meta_marks = {
        "meta": True,
        "contributing_analysts": list(contributing_analysts),
    }
    candidate = _strip_code_fence(raw)
    try:
        parsed: Any = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("cross_analyst_correlator.parse_failed err=%s", exc)
        return FindingPayload(
            title=fallback_title[:200],
            body=raw[:32000],
            confidence=0.3,
            tags=["unstructured"],
            data={**meta_marks, "raw_llm_response": raw[:8000]},
        )

    if not isinstance(parsed, dict):
        return FindingPayload(
            title=fallback_title[:200],
            body=str(parsed)[:32000],
            confidence=0.3,
            tags=["unstructured"],
            data={**meta_marks, "raw_llm_response": raw[:8000]},
        )

    raw_correlation_type = str(parsed.get("correlation_type") or "").lower()
    correlation_type: str
    extra_tags: list[str] = []
    if raw_correlation_type in _VALID_CORRELATION_TYPES:
        correlation_type = raw_correlation_type
    else:
        # Unknown — default to blind_spot (the LLM didn't categorise; we
        # treat that as a coverage gap signal) and tag for inspection.
        correlation_type = "blind_spot"
        extra_tags.append("unknown_correlation_type")

    # Validate referenced UUIDs against the slice the LLM was shown.
    raw_refs = parsed.get("referenced_outputs") or []
    if not isinstance(raw_refs, list):
        raw_refs = []
    referenced_outputs: list[str] = []
    for r in raw_refs:
        rs = str(r).strip()
        if not rs:
            continue
        # Validate UUID shape first (the LLM may have hallucinated).
        if _coerce_uuid(rs) is None:
            continue
        # Only keep refs present in the slice.
        if valid_output_ids and rs not in valid_output_ids:
            continue
        if rs not in referenced_outputs:
            referenced_outputs.append(rs)

    raw_analyst_refs = parsed.get("referenced_analyst_ids") or []
    if not isinstance(raw_analyst_refs, list):
        raw_analyst_refs = []
    referenced_analyst_ids: list[str] = []
    for r in raw_analyst_refs:
        rs = str(r).strip()
        if not rs:
            continue
        if valid_analyst_ids and rs not in valid_analyst_ids:
            continue
        if rs not in referenced_analyst_ids:
            referenced_analyst_ids.append(rs)

    # Downgrade enforcement: contradiction/agreement need ≥2 distinct
    # analysts. If validation stripped citations below the threshold,
    # downgrade with a tag the operator can grep for.
    if (
        correlation_type in ("contradiction", "agreement")
        and len(referenced_analyst_ids) < 2
    ):
        extra_tags.append(f"downgraded_from_{correlation_type}")
        correlation_type = "blind_spot"

    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    parsed_tags = parsed.get("tags") or []
    if not isinstance(parsed_tags, list):
        parsed_tags = []
    tags = [str(t)[:64] for t in parsed_tags][:40]
    # Always stamp the correlation_type into tags so substrate queries
    # without the data-dict join can still filter.
    if f"correlation:{correlation_type}" not in tags:
        tags = tags + [f"correlation:{correlation_type}"]
    tags = tags + extra_tags
    tags = tags[:50]

    title = str(parsed.get("title") or fallback_title)[:2048]
    body = str(parsed.get("body") or "")[:65536]

    # M16 — resolve the model's inline ``[[ref:N]]`` ORDINAL markers against the
    # rendered slice so the MANDATORY faithfulness verify can grade each clause
    # against the cited analyst-output's actual text. ``N`` maps to
    # ``projected[N-1]`` (the SAME ``enumerate(projected, start=1)`` order the
    # render stamped). Only in-range ordinals become citations; an out-of-range
    # (fabricated) handle is dropped. Each citation carries ``ref_id`` (the cited
    # output uuid), ``ref_kind='finding'`` (routes verify to the sub-claim floor)
    # and ``evidence_text`` (the cited body) so the verify runs DB-free — mirroring
    # meta_findings_synthesizer's composition citation shape.
    citations: list[dict[str, Any]] = []
    index_by_ordinal: dict[int, Mapping[str, Any]] = {}
    target_by_output_id: dict[str, str] = {}
    if projected:
        index_by_ordinal = {n: row for n, row in enumerate(projected, start=1)}
        for row in projected:
            oid = str(row.get("output_id") or "")
            tgt = str(row.get("target_id") or "")
            if oid and tgt:
                target_by_output_id[oid] = tgt
        seen_ords: set[int] = set()
        for match in _REF_MARKER_RE.finditer(body):
            n = int(match.group(1))
            if n in seen_ords or not (1 <= n <= len(index_by_ordinal)):
                continue
            seen_ords.add(n)
            src_row = index_by_ordinal[n]
            uid = _coerce_uuid(src_row.get("output_id"))
            if uid is None:
                continue
            citation: dict[str, Any] = {
                "marker": f"[[ref:{n}]]",
                "ordinal": n,
                "ref_id": str(uid),
                "ref_kind": "finding",
                "evidence_text": str(src_row.get("body") or "")[
                    :_MAX_EVIDENCE_TEXT_CHARS
                ],
            }
            if src_row.get("analyst_id"):
                citation["source"] = str(src_row["analyst_id"])
            if src_row.get("target_id"):
                citation["target_id"] = str(src_row["target_id"])
            if src_row.get("title"):
                citation["title"] = str(src_row["title"])
            citations.append(citation)

    # M17 — derive the stable supersession signature from correlation_type + the
    # sorted set of referenced TARGET ids (of the cited outputs). A fresh run over
    # the same targets supersedes the prior head via the write-path fold.
    ref_targets = {
        target_by_output_id[o] for o in referenced_outputs if o in target_by_output_id
    }
    ref_targets |= {
        str(c["target_id"]) for c in citations if c.get("target_id")
    }
    situation_signature = _situation_signature(correlation_type, sorted(ref_targets))

    data: dict[str, Any] = {
        **meta_marks,
        "correlation_type": correlation_type,
        "referenced_outputs": referenced_outputs,
        "referenced_analyst_ids": referenced_analyst_ids,
        "situation_signature": situation_signature,
        # M17 (adversarial FIX #1) — the EXACT referenced-target set (sorted), so the
        # blind_spot decay can test scope-containment precisely (a signature's target
        # tokens can be sha1-collapsed for long sets; this keeps the raw set).
        "xcorr_targets": sorted(ref_targets),
        "raw_llm_response": raw[:8000],
    }
    if citations:
        data["citations"] = citations

    return FindingPayload(
        title=title,
        body=body,
        confidence=confidence,
        evidence=referenced_outputs[:50],
        tags=tags,
        data=data,
    )


# ---------------------------------------------------------------------------
# LLM call (direct — DSPy wrapping deferred to L-176 per L-105)
# ---------------------------------------------------------------------------


async def _reason_via_llm(
    llm: LLMHandlerLike,
    *,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    system_prompt: str,
) -> tuple[str, dict[str, int]]:
    """One chat_complete call.  Mirrors :mod:`inline_target` shape."""
    messages = [{"role": "user", "content": user_prompt}]
    response = await llm.chat_complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
    )
    content = getattr(response, "content", "") or ""
    usage_raw = getattr(response, "usage", None)
    usage_dict = {
        "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
        "completion_tokens": (
            getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
        ),
        "reasoning_tokens": (
            getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
        ),
    }
    return content, usage_dict


# ---------------------------------------------------------------------------
# Public entry — ``run_method``
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: CrossAnalystCorrelatorDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one cross_analyst_correlator run.

    ``deps`` accepts either a :class:`CrossAnalystCorrelatorDeps` bundle
    (production path) or a bare :class:`LLMHandlerLike` (test path —
    mirrors :func:`inline_target.run_method`'s back-compat shim).

    Returns :class:`AnalystMethodResult` with a single :class:`FindingPayload`
    whose ``data`` carries ``correlation_type``, ``referenced_outputs``,
    and ``referenced_analyst_ids``. The runtime takes care of
    ``derived_from`` lineage from input row ids (see
    ``dapr_actors._read_substrate_slice`` callers).
    """
    if not isinstance(deps, CrossAnalystCorrelatorDeps):
        # Back-compat path: deps is a bare LLM handler.
        deps = CrossAnalystCorrelatorDeps(llm=deps)
    system_prompt = deps.system_prompt or _DEFAULT_SYSTEM_PROMPT

    analyst_id = options.get("analyst_id") or "cross_analyst_correlator"

    if not inputs:
        # Empty subscription window — emit a diagnostic blind_spot finding
        # rather than crash. The runtime would normally short-circuit
        # before invoking us (see AnalystActor.run NOOP/no_inputs branch
        # in dapr_actors.py), but be defensive.
        logger.info(
            "cross_analyst_correlator.empty_inputs analyst_id=%s", analyst_id,
        )
        finding = FindingPayload(
            title="No analyst outputs in window",
            body=(
                "The cross_analyst_correlator subscription returned an empty "
                "slice for this run. This may indicate a misconfigured "
                "subscription predicate or that no upstream analysts have "
                "run recently."
            ),
            confidence=0.0,
            tags=["empty_slice", "correlation:blind_spot"],
            data={
                "meta": True,
                "contributing_analysts": [],
                "correlation_type": "blind_spot",
                "referenced_outputs": [],
                "referenced_analyst_ids": [],
                # M17 — a stable signature so successive empty-slice diagnostics
                # supersede (fold to one head) rather than accumulate per cycle.
                "situation_signature": _situation_signature("blind_spot", []),
                "xcorr_targets": [],
            },
        )
        return AnalystMethodResult(finding=finding, usage={})

    # M18(a) — collapse the slice to ONE head per signature key BEFORE correlation
    # so two SEQUENTIAL snapshots of the same producer (e.g. the 12h-apart world
    # composition heads) are never diffed as if simultaneous (the false-contradiction
    # class). READ_SLICE already filters superseded heads; this catches the residual
    # write-to-supersede lag window.
    deduped = _dedupe_live_heads(inputs)
    projected, _derived_from, analyst_ids = _orient(deduped)
    valid_output_ids = {p["output_id"] for p in projected if p["output_id"]}
    valid_analyst_ids = set(analyst_ids)

    user_prompt = _render_user_prompt(projected, analyst_ids)

    try:
        content, usage = await _reason_via_llm(
            deps.llm,
            user_prompt=user_prompt,
            max_tokens=deps.max_tokens,
            temperature=deps.temperature,
            system_prompt=system_prompt,
        )
    except Exception:
        # Let the runtime classify failure (transient vs hard) — see
        # kind_contracts §7. We don't swallow because failure semantics
        # gate cooldowns, reminders, and DLQ routing.
        logger.warning(
            "cross_analyst_correlator.llm_error analyst_id=%s", analyst_id,
        )
        raise

    fallback_title = f"Cross-analyst correlation ({len(projected)} outputs)"
    # ``contributing_analysts`` mirrors the meta_findings_synthesizer contract:
    # the full set of analyst_ids whose outputs fed this run (sorted for a
    # stable, deterministic order), NOT the narrower set the LLM cited.
    finding = _coerce_correlation(
        content,
        fallback_title=fallback_title,
        valid_output_ids=valid_output_ids,
        valid_analyst_ids=valid_analyst_ids,
        contributing_analysts=sorted(analyst_ids),
        projected=projected,
    )
    # Stamp analyst lineage tag so substrate filters work without joining
    # the data dict.
    tags = list(finding.tags)
    if analyst_id and f"analyst:{analyst_id}" not in tags:
        tags.append(f"analyst:{analyst_id}")
    finding = finding.model_copy(update={"tags": tags[:50]})

    return AnalystMethodResult(finding=finding, usage=usage)


# ---------------------------------------------------------------------------
# Adapter — ``AnalystRunFn``-shaped wrapper the runtime already calls
# ---------------------------------------------------------------------------


class CrossAnalystCorrelatorRunner:
    """``AnalystRunFn``-shaped wrapper around :func:`run_method`.

    The host constructs one per analyst actor and stashes it on
    ``_AnalystDeps.run_method`` so :meth:`AnalystActor.run` (in
    ``runtime/dapr_actors.py``) doesn't need to know about kind-specific
    deps bundles. ``__call__`` keeps the spike's two-arg
    ``(inputs, options)`` signature.

    Mirrors :class:`legba.data.analysts.inline_target.InlineTargetRunner`.
    """

    def __init__(
        self,
        llm: LLMHandlerLike,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.1,
        system_prompt: str | None = None,
    ) -> None:
        self._deps = CrossAnalystCorrelatorDeps(
            llm=llm,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
        )

    async def __call__(
        self,
        inputs: list[dict[str, Any]],
        options: Mapping[str, Any],
    ) -> AnalystMethodResult:
        return await run_method(inputs, options, self._deps)


# ---------------------------------------------------------------------------
# Per-kind substrate-slice reader bound to the actor-host dispatcher.
# The host dispatcher invokes ``READ_SLICE(conn, descriptor=..., ...)``
# instead of its default signals-only reader when this kind runs.
# ---------------------------------------------------------------------------


async def READ_SLICE(  # noqa: N802 — host-discovered constant alias
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor,  # type: ignore[no-untyped-def]
    target_filter,  # type: ignore[no-untyped-def]
    analyst_ids: list[str] | None = None,
    time_window_hours: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Reader exposing analyst_outputs rows the correlator reasons across.

    Mirrors :func:`legba.data.analysts.meta_findings_synthesizer.READ_SLICE`
    but does NOT restrict to ``kind='finding'``. The correlator's job is to
    find inconsistencies / agreements / blind-spots across any kind of
    analyst output (findings, predictions, situations, …) so the reader
    walks the full output stream.

    Source-analyst resolution reads the documented
    ``subscription.other_analysts[].id`` surface (the prior implementation
    read the non-existent ``subscription.targets.id_list``, which always
    yielded ``[]`` and silently degraded to the whole-pool scan below).
    Reuses the meta-synthesizer's resolution helpers so the two meta kinds
    can't drift. When ``other_analysts`` IS populated the scoped query runs;
    when it is empty the global-pool fallback is preserved (the correlator,
    unlike the synth, is allowed to read across the whole output stream).
    """
    from .meta_findings_synthesizer import (
        _resolve_other_analyst_ids,
        _resolve_window_hours,
    )

    if analyst_ids:
        ids = [str(a) for a in analyst_ids]
    else:
        ids = _resolve_other_analyst_ids(descriptor)

    if time_window_hours is None:
        time_window_hours = _resolve_window_hours(descriptor)

    # M18(a) — read only LIVE heads (``superseded_by IS NULL``). A prior
    # composition/correlator head STILL inside the 24h window but already
    # superseded MUST NOT be handed to the correlator, or it diffs a head against
    # its own retired predecessor and manufactures a false 'contradiction'. The
    # in-kind ``_dedupe_live_heads`` closes the residual write-to-supersede lag.
    if ids:
        rows = await conn.fetch(
            f"""
            SELECT id, kind, title, body, confidence, severity, data,
                   target_id, target_version, analyst_id, analyst_version,
                   produced_at, derived_from, schema_uri, run_id,
                   situation_signature
            FROM analyst_outputs
            WHERE analyst_id = ANY($1::TEXT[])
              AND produced_at > NOW() - make_interval(hours => $2)
              AND superseded_by IS NULL
            ORDER BY produced_at DESC
            LIMIT {int(limit)}
            """,
            ids,
            int(time_window_hours),
        )
    else:
        rows = await conn.fetch(
            f"""
            SELECT id, kind, title, body, confidence, severity, data,
                   target_id, target_version, analyst_id, analyst_version,
                   produced_at, derived_from, schema_uri, run_id,
                   situation_signature
            FROM analyst_outputs
            WHERE produced_at > NOW() - make_interval(hours => $1)
              AND superseded_by IS NULL
            ORDER BY produced_at DESC
            LIMIT {int(limit)}
            """,
            int(time_window_hours),
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Wave B prereq #4 — DSPy prompt module surface
# ---------------------------------------------------------------------------


def build_prompt_module() -> Any:
    """Construct and return the DSPy module bound to this analyst kind.

    Wave B prereq #4: backfilled to return a real
    :class:`legba.prompts.cross_analyst_correlator.v1.CrossAnalystCorrelatorCycle`
    so the L-176 optimizer can compile candidates against the trace set.

    The kind handler keeps the direct ``chat_complete`` path (no dspy
    hard-requirement at runtime); this module is the optimization-
    surface twin.  Lazy-imports so this file imports cleanly when dspy
    isn't installed; raises :class:`ModuleNotFoundError` otherwise.
    """
    from legba.prompts.cross_analyst_correlator.v1 import build as _build
    return _build()


__all__ = [
    "AnalystMethodResult",
    "CorrelationType",
    "CrossAnalystCorrelatorDeps",
    "CrossAnalystCorrelatorRunner",
    "FindingPayload",
    "HANDLER_VERSION",
    "KIND_NAME",
    "LLMHandlerLike",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "build_prompt_module",
    "run_method",
]
