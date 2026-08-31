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

VERDICTS: frozenset[str] = frozenset(
    {
        VERDICT_SUPPORTED,
        VERDICT_CONTRADICTED,
        VERDICT_NOT_FOUND,
        VERDICT_UNCHECKED,
    }
)

#: Verdicts that represent a COMPLETED external check (the heartbeat's
#: ``claims_checked`` counts these, never UNCHECKED — an auditor whose search
#: plane is dead must not look busy).
CHECKED_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_NOT_FOUND}
)


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
    "CHECKED_VERDICTS",
    "CheckableClaim",
    "ClaimVerdict",
    "SampledHead",
    "VERDICTS",
    "VERDICT_CONTRADICTED",
    "VERDICT_NOT_FOUND",
    "VERDICT_SUPPORTED",
    "VERDICT_UNCHECKED",
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
    "strip_json_fence",
]
