# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a COMPOSITION's LLM response becomes a :class:`FindingPayload`.

The output-coercion seam of ``meta_findings_synthesizer``: the model's JSON
contract in, one finding out, and — the part this module exists to get right —
what happens when the contract does not parse.

WHY A SEPARATE MODULE. ``meta_findings_synthesizer.py`` sat at 5,248 lines
against a 5,250 ceiling (``tests/test_module_size_gate.py``) — two lines of
headroom — and the house rule on that ratchet is to EXTRACT a cohesive unit
rather than raise the number. Output coercion is one: it is pure, it needs no
slice, no LLM and no DB, and it is the whole of what the JSON-envelope-leak fix
touches. The synthesizer imports it ONE WAY and re-exports ``_coerce_finding``
and ``_looks_like_resolvable_evidence``, so every existing caller and test
resolves unchanged.

THE DEGRADE CONTRACT (2026-08-29). A composition whose response does not parse
has THREE possible shapes, and they are not the same event:

  * PROSE — the model answered in markdown instead of JSON. That is a
    formatting miss over real analysis; prose IS content, so it degrades to an
    ``unstructured`` finding carrying the prose, byte-for-byte as before.
  * A JSON ENVELOPE WE CAN UNWRAP — the contract is right there and recoverable
    (see :func:`output_contract.salvage_json_envelope`). Take the ``title`` and
    ``body`` out of it; publish the markdown, never the wrapper.
  * A JSON ENVELOPE WE CANNOT UNWRAP — truncated, unterminated, or not a
    finding at all. :class:`OutputContractError` propagates out of ``_run``,
    which does not catch it; the actor classifies it per ``kind_contracts`` §7
    and the cycle lands in the DLQ where a human sees it. The cycle is NOT
    silently dropped and the wrapper is NOT silently published.

WHAT THE DEGRADE STILL KEEPS. A salvaged finding is still a DEGRADE: it keeps
the ``unstructured`` (or ``coerce_failed``) tag and the 0.30 confidence exactly
as before, so every downstream predicate keyed on those tags — the composition
admissibility floor, the window ledger, the tiered-evidence periphery — sees
the identical population it saw yesterday. The ONLY thing that changes is that
the body column holds the model's markdown instead of the model's JSON. The new
``envelope_salvaged`` tag and ``data.envelope_salvaged`` marker record that the
salvage ran, without moving any of those predicates (they test for the presence
of ``unstructured``/``coerce_failed``, which is still there).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence
from uuid import UUID

from ..provenance.models import FindingPayload
from .output_contract import (
    OutputContractError,
    is_unusable_output,
    salvage_json_envelope,
)

logger = logging.getLogger("legba.data.analysts.meta_findings_synthesizer")

__all__ = [
    "_coerce_finding",
    "_looks_like_resolvable_evidence",
]

_EVIDENCE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _looks_like_resolvable_evidence(item: str) -> bool:
    """True when ``item`` is a genuinely resolvable evidence identifier: an
    absolute http(s) URL or a UUID.

    P2 gallery finding #3 (evidence-field contamination): a composition
    finding cites structurally via ``[[ref:N]]`` markers IN THE BODY, resolved
    to ``data.citations`` by the CITE step in ``_run`` — the ``evidence``
    array is a legacy per-unit-analyst field the model tends to fill by
    echoing its OWN citation markers back (bare ints like ``"2"``/``"14"``,
    bracket-style ``"[1]"``, or composition-tier ``"ref:1"`` / ``"[[ref:16]]"``
    strings — see the live captures in the P2 gallery: three different
    schemes, all meaningless once detached from the tier that emitted them).
    None of that is resolvable on its own; a genuine URL or UUID is. Used by
    :func:`_coerce_finding` to filter the model's own ``evidence`` list so a
    composition's stored ``evidence`` never becomes scheme soup a LATER,
    higher tier then renders as if it meant something (the SAME contamination
    ``_defuse_child_ref_markers`` defuses on the render side for
    ``[[ref:N]]``-shaped body/evidence text already in the wild).
    """
    text = item.strip()
    if not text:
        return False
    if _EVIDENCE_URL_RE.match(text):
        return True
    try:
        UUID(text)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _degraded_finding(
    raw: str,
    *,
    fallback_title: str,
    meta_marks: dict[str, Any],
    tag: str,
) -> FindingPayload:
    """The DEGRADE path, shared by every non-parsing branch of
    :func:`_coerce_finding` so they cannot drift.

    Tries the JSON-envelope unwrap FIRST. When ``raw`` is prose the unwrap
    returns ``{}`` and the prose is kept verbatim — the pre-2026-08-29 behaviour
    for that shape, byte-for-byte. When ``raw`` is a JSON envelope the unwrap
    either yields the inner markdown or RAISES, and the raise is deliberately
    NOT caught here: publishing the wrapper is the defect this closes, and a
    JSON blob in the body column is indistinguishable from analysis at every
    layer above this one.

    ``raw_llm_response`` is stamped either way, so the trace still carries what
    the model actually said even when the body now carries only its markdown.
    """
    salvaged = salvage_json_envelope(raw)
    tags = [tag, "meta"]
    data: dict[str, Any] = {**meta_marks, "raw_llm_response": raw[:8000]}
    if salvaged:
        logger.warning(
            "meta_findings_synthesizer.finding.envelope_salvaged tag=%s "
            "raw_chars=%d body_chars=%d titled=%s",
            tag, len(raw), len(salvaged["body"]), "title" in salvaged,
        )
        tags.append("envelope_salvaged")
        data["envelope_salvaged"] = True
        title = salvaged.get("title") or fallback_title
        body = salvaged["body"]
    else:
        title = fallback_title
        body = raw
    return FindingPayload(
        title=title[:2048] if salvaged else title[:200],
        body=body[:32000],
        confidence=0.3,
        tags=tags,
        data=data,
    )


def _coerce_finding(
    raw: str,
    *,
    fallback_title: str,
    contributing_analysts: Sequence[str],
) -> FindingPayload:
    """Parse the LLM JSON response into a :class:`FindingPayload`.

    Always stamps ``data.meta = True`` and ``data.contributing_analysts``
    so downstream filters can find meta-findings without joining lineage.

    Parsing degrades rather than fails whenever something readable survives —
    prose keeps its finding, a recoverable JSON envelope is unwrapped to its
    markdown — and FAILS LOUD when nothing does. See the module docstring for
    the three shapes; the one outcome that no longer exists is publishing the
    model's JSON wrapper as the body (the 2026-08-29 world-composition leak).
    """
    meta_marks = {
        "meta": True,
        "contributing_analysts": list(contributing_analysts),
    }

    parsed: Any
    try:
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
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("meta_findings_synthesizer.finding.parse_failed err=%s", exc)
        return _degraded_finding(
            raw, fallback_title=fallback_title,
            meta_marks=meta_marks, tag="unstructured",
        )

    # A JSON scalar or array. This branch CANNOT be the envelope leak — a value
    # that parsed to a non-dict was not a `{...}` object in the first place — so
    # the salvage below will decline it and keep the text. Passing ``raw`` rather
    # than the old ``str(parsed)`` keeps what the model actually wrote instead of
    # a Python repr of it; no live row has ever taken this branch (all 84 census
    # rows start with `{` and so came from the parse-failure branch above).
    if not isinstance(parsed, dict):
        return _degraded_finding(
            raw, fallback_title=fallback_title,
            meta_marks=meta_marks, tag="unstructured",
        )

    try:
        tags_in = [str(t) for t in (parsed.get("tags") or [])][:50]
        # Stamp the meta tag idempotently so downstream filters can match
        # without parsing the JSONB data column.
        if "meta" not in tags_in:
            tags_in.append("meta")
        # P2 gallery finding #3: keep only GENUINELY RESOLVABLE evidence
        # identifiers (a URL or a UUID) — never the model's own bare-int /
        # bracket / [[ref:N]] citation-scheme echo, which means nothing once
        # this composition's `evidence` array is copied forward and rendered
        # by a HIGHER tier (see :func:`_looks_like_resolvable_evidence`). An
        # evidence list with nothing resolvable stays EMPTY rather than
        # carrying scheme soup.
        evidence_in = [
            str(e) for e in (parsed.get("evidence") or [])
            if _looks_like_resolvable_evidence(str(e))
        ][:50]
        body_in = str(parsed.get("body") or "")
        # A JSON object that PARSES is not automatically a finding. The tool-call
        # shape the census turns up — ``{"action": "search_corpus", "query": …}``
        # emitted into the answer channel — parses cleanly and carries no ``body``
        # at all, which used to land an EMPTY body: the shape verify scores a
        # vacuous faithfulness 1.00 on 0 checkable claims, indistinguishable from
        # a perfect read on every dashboard above this one. Same doctrine as the
        # envelope rule, same outcome: fail loud, let the cadence retry. Real
        # composition bodies are never empty — the HONEST EMPTY rule still puts
        # prose in the body — so this cannot fire on analysis.
        if is_unusable_output(body_in):
            raise OutputContractError(
                "composition response parsed but carries no readable body "
                f"(keys={sorted(parsed)[:12]}); refusing to publish an empty "
                f"finding: {raw.strip()[:200]!r}"
            )
        return FindingPayload(
            title=str(parsed.get("title") or fallback_title)[:2048],
            body=body_in[:65536],
            confidence=float(parsed.get("confidence", 0.5)),
            evidence=evidence_in,
            tags=tags_in,
            data={**meta_marks, "raw_llm_response": raw[:8000]},
        )
    except OutputContractError:
        raise
    except Exception as exc:
        logger.warning("meta_findings_synthesizer.finding.coerce_failed err=%s", exc)
        return _degraded_finding(
            raw, fallback_title=fallback_title,
            meta_marks=meta_marks, tag="coerce_failed",
        )
