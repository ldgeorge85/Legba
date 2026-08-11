# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Judge evidence rendering — the first extracted brick of the judge subsystem
(the regrowth-gate seam named in advance; split 2026-08-05 when the W1-D x
v-voice merge pushed verify.py past its DO-NOT-RAISE 6,200 ceiling).

``_marker_to_evidence`` renders a citation marker into the evidence view the
LLM judge grades against — including W1-D's OUTLET line, which rides OUTSIDE
the evidence cap. ``verify`` re-exports the name; shared verify-module names
late-bind through ``_verify()`` at call time (no load-time cycle).
"""
from __future__ import annotations

from typing import Any, Mapping

from .absence_slice import _is_machine_structured_row


def _verify():
    from . import verify
    return verify


# ---------------------------------------------------------------------------
# V-I4 (2026-08-05) — THE MACHINE-CODED ROWS THE V-B ROUTE ALREADY DROPS.
#
# W1(c) established the rule and the round-4 population proves it works:
# `absence_slice_machine_rows_excluded` fired **2,109 times across 150 rows** in
# one day. A GDELT/CAMEO row is a machine coding OF an article — an actor pair,
# a root code, a Goldstein number — not the article, and not testimony. It can
# neither verify nor violate a claim, so it leaves the eligible set entirely.
#
# The judge's evidence view never got the filter. The panel's H13:
#
#   claim  "No material change since the prior read [121]."
#   quote  "STUDENT <-> PAPUA: protest in Jakarta, Jakarta Raya, Indonesia"
#
# — citation `[10]`, whose own evidence text OPENS "GDELT/CAMEO structured event
# record (machine coding of a news report, not article text)". The underlying
# article is "Explainer: Why SMALL protests are drawing OUTSIZED attention on
# social media", which SUPPORTS the claim. `absence_slice_machine_rows_excluded`
# = 15 fired on this very critique, on the other path, while this one hard-failed
# the finding on a code label. Round-4 §6.2, and one of eight wrong hard fails.
#
# Two surfaces, mirroring the OUTLET line's precedent:
#   * the evidence view CAVEATS the row, so the judge is told what it is holding
#     even when the analyst's own rendering did not survive into the citation;
#   * :func:`machine_coded_ordinals` names them, so the severity chain can bar
#     them from grounding a hard fail. Barring, not hiding: the row stays
#     visible, the quote still resolves, and the demotion says truthfully that
#     the evidence is a coding rather than pretending the quote was invented.
# ---------------------------------------------------------------------------

#: The caveat prefix, riding OUTSIDE the evidence cap for the same reason the
#: OUTLET line does: it is one short line and it is the whole point.
_MACHINE_CODED_LINE = (
    "MACHINE-CODED: this entry is a GDELT/CAMEO structured event record — a "
    "machine coding of a news report, not the report's own words. It is not "
    "testimony and cannot contradict a claim.\n"
)


#: The analyst's own rendered tag, and the ONLY reliable discriminator on this
#: path. See the replay note below.
_CAMEO_EVIDENCE_TAG = "GDELT/CAMEO structured event record"
#: How far into the evidence text the tag must appear. It is the first line of a
#: rendered coding; a window keeps a leading title from hiding it, and keeps a
#: real article that merely MENTIONS GDELT from being swept up.
_CAMEO_TAG_WINDOW = 160


def _is_machine_coded_citation(entry: Mapping[str, Any]) -> bool:
    """Is the evidence THE JUDGE WAS SHOWN a machine coding rather than reporting?

    THE DISCRIMINATOR IS THE TEXT, not the source id — and that is a correction
    the offline replay of the 14 hard fails forced. On the V-B route a
    ``source.gdelt.files`` row IS a coding, so the source id is sufficient there.
    On the CITATION path it is not: the same handler supplies entries whose TITLE
    is CAMEO-shaped ("THE US <-> BRAZIL: coerce in Brazil") while their
    ``source_text`` is the real article ("The United States has revoked the visa
    of Brazil's ambassador in Washington…"). That entry is testimony, it is
    ``[11]`` of the round-4 panel's H1/H7, and those are two of the six hard fails
    the panel scored CORRECT. Keying on the source id demoted them — a real
    catch lost to a fix aimed at something else.

    So: whichever text the judge was actually grounded on decides. Only when
    there is no text at all do the V-B markers answer, because then the coding is
    all there is to read. Never raises.
    """
    primary = str(entry.get("source_text") or "").lstrip()
    if primary:
        return _CAMEO_EVIDENCE_TAG in primary[:_CAMEO_TAG_WINDOW]
    fallback = str(
        entry.get("snippet") or entry.get("evidence_text") or entry.get("summary") or ""
    ).lstrip()
    if fallback:
        return _CAMEO_EVIDENCE_TAG in fallback[:_CAMEO_TAG_WINDOW]
    return _is_machine_structured_row(
        source_id=str(entry.get("source_id") or ""),
        provenance_kind=str(entry.get("provenance_kind") or ""),
        title=str(entry.get("title") or ""),
    )


def machine_coded_ordinals(citations: Any) -> set[int]:
    """The ``[N]`` / ``[[ref:N]]`` ordinals whose citation is a MACHINE CODING.

    Empty for every finding that cites none — so a caller folding this in is
    byte-identical for them. Never raises.
    """
    out: set[int] = set()
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping) or not _is_machine_coded_citation(entry):
            continue
        marker = entry.get("marker")
        if not isinstance(marker, str):
            continue
        m = _verify()._CLAIM_MARKER_RE.search(marker) or _verify(
        )._REF_MARKER_RE.search(marker)
        if m:
            out.add(int(m.group(1)))
    return out

def _marker_to_evidence(citations: Any) -> dict[int, str]:
    """Map each citation's ``[N]`` marker index → its EVIDENCE TEXT — the cited
    signal's authoritative source (+ title, + the analyst's working summary as
    labelled secondary context) — so the LLM judge can verify a claim against the
    signal's actual CONTENT rather than an opaque UUID.

    The unit judge previously received :func:`_marker_to_signal_id` (``{N ->
    signal_id}``), i.e. UUIDs; a judge handed only a UUID cannot verify anything
    and marks even properly-cited claims ``unsupported`` (the dominant unit-score
    crusher). This mirrors the composition path's ``_ordinal_evidence_map`` (which
    already supplies sub-claim text).

    FAITHFULNESS TRUST BOUNDARY: when the citation carries ``source_text`` (the RAW
    article the summarizer distilled from — NEVER the analyst-read ``distilled_body``
    summary), the judge is grounded on that SOURCE, LABELLED authoritative; the
    analyst's ``snippet`` (its working text, distilled-first) rides along as
    LABELLED secondary context ONLY when it is a genuinely DISTINCT summary (F4). A
    claim present only in the summary but absent from a COMPLETE source is thus
    UNSUPPORTED (a summarizer hallucination can't be rubber-stamped). When the
    source is an EXCERPT (``source_truncated`` / re-truncated here), the judge is
    told so and softens to "contradicted => unsupported" — a claim the analyst
    faithfully drew from deep in a long article is NOT false-demoted for being past
    the cut (F1). Entries with NO ``source_text`` (old data, non-signal path) keep
    the prior title/snippet/source/id fallback chain byte-for-byte, at the ORIGINAL
    600-char cap (F3). Never fabricates evidence; only entries carrying a resolvable
    ``signal_id`` (a real cited signal) contribute.
    """
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        sid = entry.get("signal_id")
        marker = entry.get("marker")
        if not (isinstance(sid, str) and sid) or not isinstance(marker, str):
            continue
        m = _verify()._CLAIM_MARKER_RE.search(marker)
        if not m:
            continue
        # (#116e) Feed the judge the cited signal's TITLE + evidence text, mirroring
        # the composition path's evidence_text — a title alone can be too terse for
        # the judge to confirm a specific claim, so a properly-cited clause gets
        # mis-graded DOWN. Fall back to title-only, then snippet-only, then the
        # source URL, then the id — never fabricated.
        title = entry.get("title")
        source = entry.get("source")
        source_text = entry.get("source_text")
        snippet = (
            entry.get("snippet")
            or entry.get("evidence_text")
            or entry.get("summary")
        )
        title_txt = title.strip() if isinstance(title, str) and title.strip() else ""
        # V-H1: the OUTLET. An attribution claim ("near-identical framing across
        # CBC, NPR and the BBC [1][16][54]") names WHO published, and the judge's
        # evidence view carried title/snippet/source-URL but never the outlet ref,
        # so every such claim was unverifiable BY CONSTRUCTION — the 08-03 panel
        # verified all six outlets of `soft_fail#2` by hand and the judge still
        # graded it unsupported. Prefixed rather than appended so it survives the
        # cap, and rendered ONLY when the citation carries one (every pre-V-H1
        # citation is byte-identical).
        outlet = entry.get("source_id")
        outlet_txt = (
            f"OUTLET: {outlet.strip()}\n"
            if isinstance(outlet, str) and outlet.strip()
            else ""
        )
        # V-I4: the machine-coding caveat, on the same terms as the OUTLET line.
        if _is_machine_coded_citation(entry):
            outlet_txt = _MACHINE_CODED_LINE + outlet_txt
        snip_txt = snippet.strip() if isinstance(snippet, str) and snippet.strip() else ""
        src_full = (
            source_text.strip()
            if isinstance(source_text, str) and source_text.strip()
            else ""
        )
        src_txt = src_full[:_verify()._EVIDENCE_SOURCE_CHARS]
        # F1: the SOURCE is an EXCERPT if it was flagged truncated at build time
        # (cleaned raw exceeded the store cap) OR we re-truncate it here. Either way
        # the judge must NOT demote a cited claim merely for being absent from the
        # shown text — only for being CONTRADICTED by it.
        source_truncated = bool(entry.get("source_truncated")) or (
            len(src_full) > _verify()._EVIDENCE_SOURCE_CHARS
        )
        if src_txt:
            # TRUST BOUNDARY: ground the judge on the RAW authoritative SOURCE; the
            # analyst summary is LABELLED secondary context only (the judge prompt
            # says a fact present only in a COMPLETE source is UNSUPPORTED; for an
            # EXCERPT the summary shows what the fuller article covered). F4: skip the
            # summary line when the analyst read raw directly — i.e. snippet is the
            # same as, or a leading prefix of, the source (no distinct distilled_body).
            parts = []
            if title_txt:
                parts.append(title_txt)
            if source_truncated:
                parts.append(
                    "SOURCE (authoritative excerpt — the full article is longer "
                    f"than shown): {src_txt}"
                )
            else:
                parts.append(f"SOURCE (authoritative): {src_txt}")
            if snip_txt and snip_txt != src_txt and not src_txt.startswith(snip_txt):
                parts.append(f"Analyst summary: {snip_txt}")
            text = "\n".join(parts)
            cap = _verify()._EVIDENCE_TOTAL_CHARS
        elif title_txt and snip_txt:
            # Backward-compat (no source_text): the prior title + snippet evidence.
            text = f"{title_txt} — {snip_txt}"
            cap = _verify()._EVIDENCE_LEGACY_CHARS
        elif title_txt:
            text = title_txt
            cap = _verify()._EVIDENCE_LEGACY_CHARS
        elif snip_txt:
            text = snip_txt
            cap = _verify()._EVIDENCE_LEGACY_CHARS
        elif isinstance(source, str) and source.strip():
            text = source.strip()
            cap = _verify()._EVIDENCE_LEGACY_CHARS
        else:
            text = sid
            cap = _verify()._EVIDENCE_LEGACY_CHARS
        # V-H1: the outlet rides OUTSIDE the cap. It is one short line, it is the
        # only field an attribution claim can be checked against, and letting a
        # 3,600-char article budget decide whether it survives would reintroduce
        # the defect intermittently — the worst possible failure mode for a
        # calibration read.
        out[int(m.group(1))] = outlet_txt + str(text)[:cap]
    # QW1-B: fold in the DESK GROUNDING blocks the unit was shown. They occupy
    # ordinals the signal loop above never visits (no ``signal_id``), so this is
    # purely additive — a finding with no grounding block is byte-identical.
    # Without this the judge would be handed NOTHING for a block-backed [N] and
    # would mark a correctly-cited continuity clause unsupported: the same
    # "judge handed only a UUID" failure the #116e fix removed for signals.
    out.update(_verify()._grounding_ordinals(citations))
    return out


