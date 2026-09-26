# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE EVIDENTIARY CONTRACT, IN CODE — W-3 of EXTERNAL GRADING AT WIDTH (§2.3).

WHAT THIS MODULE IS. The R-rounds produced two different things: a *protocol*
(two-stage blind reference reads, twelve opus lanes) and a *contract* (the four
mechanical gates a decisive verdict has to pass before it counts). The protocol
cannot go standing — it is twelve lanes of hand grading. The contract can, and
this module is it, moved out of a rubric prompt and into functions a test can
call.

The reason it is CODE and not a prompt clause is one sentence in the C4 audit of
R2, which overturned a scored verdict:

    *"the string ``tier`` appears nowhere in the scorer … Grader right, scorer
    wrong."*  — ``PROOF_ROUND_2026-08-25/C4_AUDIT_RESULT.md`` case #10

The grader had said, inside its own atom, that its Tier-3 anchor made the
verdict inadmissible. The scorer counted it anyway, because the rule lived in
prose. At width that mistake is available ~470 times a day, so every gate below
is a function with a test, and the auditor's drain calls :func:`check_span`
rather than trusting a model to remember a rubric.

THE FOUR GATES (design §2.3; G-4 already ships inside the auditor).

* **G-1 · Source tier.** A decisive verdict needs a Tier-1 or Tier-2 source.
  :func:`source_tier_for_url` resolves a URL against the PUBLISHED registry
  below. Tier 3 DEMOTES. An **unknown** domain does NOT silently become Tier 3
  (F-3, overruled to the visible option): it resolves to
  :data:`TIER_CLASS_UNKNOWN`, the decisive verdict SURVIVES, and it is stamped
  so the publication can count and report that class *separately*. The ruled
  default would have suppressed true CONTRADICTED verdicts silently; a visible
  class suppresses nothing and hides nothing.
* **G-2 · The decisive span decides.** The quoted span must appear VERBATIM in
  the fetched page, compared through the one shared fold
  (:mod:`.text_fold`, ``fold_contains``) — never one side folded and the other
  not. No span, or a span that does not resolve, means no decisive verdict.
  R1/R2/R3's span sweeps (176/176, 215/215, 281/282) are why those rounds'
  verdicts survived audit; today's auditor validates only that the URL *appeared
  in the results*, and a page can be in a result set without saying what the
  grader claims it says.
* **G-3 · Time anchoring.** The admissible window is the READ's own
  ``data.data.evidence_window`` (plus the block's ``produced_at``), never the
  grader's judgement. A source published outside it DEMOTES, with
  ``unchecked_reason='out_of_window'``. R2-C4 case #5 rested a decisive anchor
  on a rolling page *"stamped Update 01/07/2026, seven weeks before T0"*; R3 §4.7
  found declared spans 1–2 days inside a read consumed as a 14-day country read.
  :func:`evidence_window_bounds` takes an optional ``grace_before_hours``
  (default ``0.0`` — today's rule, byte-identical) that admits a source
  published up to that many hours BEFORE ``window.oldest``; the AFTER bound
  never moves. Operator-facing as ``LEGBA_EXTERNAL_AUDIT_WINDOW_GRACE_HOURS`` /
  the ``standing_auditor`` ``window_grace_hours`` option — see
  ``_external_audit_width.py`` for the stamp-lineage discipline a nonzero
  grace requires in production.
* **G-5 · Pre-search classification.** Some claims have no world truth-maker at
  all. :func:`uncheckable_class_for_claim` decides that BEFORE a query is
  issued, deterministically, so the loop never searches for the packet's own
  bookkeeping and deflates the headline with the NOT_FOUND it inevitably gets.
  The named specimen is R3's one non-resolving span in 282, ML_A
  ``CR-2bbcd291`` — *"a self-referential provenance claim whose truth-maker is
  the packet, not the world"* — and R2-C4 case #4, *"All eight principal units
  produced verified reads in this cycle."*

THE GRADING ARCHIVE IS NOT THE EVIDENCE ARCHIVE (§0.11 / F-8). ``evidence_archive``
is keyed on ``signal_id``; routing grading pages through it would hand the desks
the grader's own evidence as a slice input and close the exact loop this program
exists to open. So the grading archive has its OWN scheme
(:data:`GRADING_CAS_PREFIX`) and its OWN subtree under the archive root
(:data:`GRADING_ARCHIVE_SUBDIR`). It is never a signal, and
:func:`grading_archive_ref` cannot be mistaken for
``legba.data.archive.cas_object_ref`` by any reader or any SQL — a test pins
that the two prefixes differ.

WHAT THIS MODULE DELIBERATELY DOES NOT DO.

* **It does not fetch.** :func:`check_span` takes the already-fetched text. The
  fetch is the auditor's, through the ``web_access`` pack binding, so it keeps
  traversing resolve ∩ allow ∩ applicability, the governor and the invocation
  ledger.
* **It does not implement robots.txt** (F-7). The robots helper is owned by the
  research lane at ``data/analysts/agency/robots.py``; the integration step
  wires ``web_fetch`` to it. A second robots implementation would be a second
  ToS posture, which is the one thing a fetch path must not have.
* **It does not decide entailment.** Whether the span *entails* the claim is the
  grader's judgement and stays with the grader. This module decides whether the
  grader's judgement is ADMISSIBLE — which is the half that R2 got wrong.

Import weight: stdlib plus :mod:`.text_fold` and :mod:`.._url_canon`, both of
which are themselves stdlib-only, so the auditor's runtime and any test can
import this without dragging the substrate in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .._url_canon import strip_www
from .text_fold import FOLD_VERSION, fold_contains, normalize_for_match

# ---------------------------------------------------------------------------
# THE VERDICT VOCABULARY
# ---------------------------------------------------------------------------
#
# Literal mirrors of ``_external_audit_sampling``'s constants. They are copies
# rather than an import for one structural reason: that module lives under
# ``analysts.deterministic_handlers`` and imports the handler surface, while
# this one has to stay importable from the registry image and from a bare test.
# The copies are not left to trust — ``test_external_span_check`` imports BOTH
# and asserts they are equal, which is the drift guard the duplication buys.

VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_CONTRADICTED = "CONTRADICTED"
VERDICT_NOT_FOUND = "NOT_FOUND"
VERDICT_UNCHECKED = "UNCHECKED"

#: §0.5's ONE new verdict: deterministic, pre-search, for a claim with no world
#: truth-maker. It is excluded from numerator and denominator both, and it is
#: counted in its own right so the reader can see how much of a read is not the
#: kind of thing an external check can reach.
VERDICT_UNCHECKABLE = "UNCHECKABLE"

#: The two verdicts a source has to EARN. Everything else is a statement about
#: the search or about the claim, never about the world.
DECISIVE_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_SUPPORTED, VERDICT_CONTRADICTED}
)

#: ``uncheckable_class`` — the closed vocabulary (§2.2, mirrored by the ledger's
#: CHECK constraint).
UNCHECKABLE_SCOPE_BOUNDED = "scope_bounded"
UNCHECKABLE_PROVENANCE = "provenance"
UNCHECKABLE_PERSPECTIVE = "perspective"
UNCHECKABLE_CLASSES: tuple[str, ...] = (
    UNCHECKABLE_SCOPE_BOUNDED,
    UNCHECKABLE_PROVENANCE,
    UNCHECKABLE_PERSPECTIVE,
)

#: ``unchecked_reason`` values THIS module emits. The full enum is wider (the
#: search plane owns the rest); these are the two the evidentiary contract can
#: cause on its own.
UNCHECKED_OUT_OF_WINDOW = "out_of_window"

#: Bumped when a GATE changes what it admits. A gate is a graded behaviour, so a
#: change here is a stamp-bearing event that must ride an
#: ``EXTERNAL_AUDIT_PIPELINE_VERSION`` bump — never a refactor. It travels on
#: every :class:`SpanCheck` so a ledger row records which contract judged it.
CONTRACT_VERSION = "external_contract.v1"


# ---------------------------------------------------------------------------
# G-1 — THE PUBLISHED SOURCE-TIER REGISTRY
# ---------------------------------------------------------------------------
#
# Transcribed from the rounds' own binding text, which every R2/R3 lane packet
# carried verbatim under "Reference-source quality bounds (binding)":
#
#   Tier 1 — primary/official: government and IGO statements and datasets
#     (IMF/World Bank/EIA/UN/IAEA), central banks, courts, election commissions,
#     UCDP/ACLED event data, company filings.
#   Tier 2 — record press and reference monitors: major wires (Reuters, AP,
#     AFP), newspapers of record (FT, NYT, WSJ, Economist, BBC, DW, Le Monde,
#     Nikkei...), and domain reference monitors (ICG CrisisWatch, ISW, IISS).
#   Tier 3 — everything else: regional outlets, think-tank blogs, aggregator
#     sites. Corroboration only.
#
# It is a REGISTRY, not a heuristic: a domain is Tier 1 or Tier 2 because it was
# written down here, which is what makes "unknown" a real and reportable answer
# rather than a scoring accident. Adding a domain is a visible diff.

TIER_PRIMARY = 1
TIER_RECORD_PRESS = 2
TIER_OTHER = 3

TIER_CLASS_1 = "tier_1"
TIER_CLASS_2 = "tier_2"
TIER_CLASS_3 = "tier_3"
#: F-3, taken on the visible option: a domain nobody registered is NOT quietly
#: Tier 3. It gets a class of its own, counted and published separately.
TIER_CLASS_UNKNOWN = "tier_unknown"

TIER_CLASSES: tuple[str, ...] = (
    TIER_CLASS_1,
    TIER_CLASS_2,
    TIER_CLASS_3,
    TIER_CLASS_UNKNOWN,
)

#: Bumped when a domain is added, removed or re-tiered. Pooling a decided rate
#: across a registry change would describe a population that never existed.
TIER_REGISTRY_VERSION = "tier_registry.v1"

#: Host SUFFIXES that make a domain primary/official by construction. These are
#: the ones no list can enumerate — every ministry, every court, every election
#: commission in every country — so they are matched structurally.
TIER_1_HOST_SUFFIXES: tuple[str, ...] = (
    ".gov",
    ".mil",
    ".int",
    ".gov.uk",
    ".parliament.uk",
    ".gob.es",
    ".gob.mx",
    ".gouv.fr",
    ".go.jp",
    ".go.kr",
    ".gov.au",
    ".gov.br",
    ".gov.in",
    ".gov.za",
    ".gov.sg",
    ".gc.ca",
    ".admin.ch",
    ".bund.de",
    ".europa.eu",
)

#: Registered Tier-1 domains: IGOs, multilateral datasets, central banks, courts
#: and the event datasets the rounds named. Matched on the registrable domain or
#: any subdomain of it (``data.imf.org`` -> ``imf.org``).
TIER_1_DOMAINS: frozenset[str] = frozenset(
    {
        # UN system and IGOs
        "un.org", "unhcr.org", "ohchr.org", "who.int", "iaea.org", "wto.org",
        "unctad.org", "fao.org", "wfp.org", "reliefweb.int", "ilo.org",
        "opcw.org", "icao.int", "imo.org", "nato.int", "osce.org",
        "africanunion.org", "asean.org", "oas.org",
        # multilateral finance and energy datasets
        "imf.org", "worldbank.org", "eia.gov", "iea.org", "oecd.org",
        "adb.org", "afdb.org", "ebrd.com", "iadb.org",
        # central banks
        "ecb.europa.eu", "federalreserve.gov", "bankofengland.co.uk",
        "boj.or.jp", "bis.org", "snb.ch", "rba.gov.au", "banxico.org.mx",
        "bundesbank.de", "banque-france.fr",
        # courts and tribunals
        "icc-cpi.int", "icj-cij.org", "echr.coe.int", "coe.int",
        # conflict event data the rounds admitted as primary
        "acleddata.com", "ucdp.uu.se", "pcr.uu.se",
        # company filings
        "sec.gov", "sedar.com",
    }
)

#: Registered Tier-2 domains: the wires, the newspapers of record, and the
#: domain reference monitors the rounds named for conflict bands.
TIER_2_DOMAINS: frozenset[str] = frozenset(
    {
        # wires
        "reuters.com", "apnews.com", "ap.org", "afp.com", "bloomberg.com",
        "dpa-international.com", "kyodonews.net", "yonhapnews.co.kr",
        "ansa.it", "efe.com", "pap.pl",
        # newspapers / broadcasters of record
        "ft.com", "nytimes.com", "wsj.com", "economist.com", "bbc.com",
        "bbc.co.uk", "dw.com", "lemonde.fr", "nikkei.com", "theguardian.com",
        "washingtonpost.com", "haaretz.com", "spiegel.de", "faz.net",
        "elpais.com", "corriere.it", "nrc.nl", "svd.se", "aftenposten.no",
        "thehindu.com", "scmp.com", "asahi.com", "mainichi.jp",
        "japantimes.co.jp", "smh.com.au", "theglobeandmail.com",
        "irishtimes.com", "lesechos.fr", "handelsblatt.com",
        # domain reference monitors
        "crisisgroup.org", "understandingwar.org", "iiss.org", "sipri.org",
        "janes.com", "lloydslist.com", "argusmedia.com", "platts.com",
        "spglobal.com",
    }
)

#: State-controlled outlets of a party to a matter are *never decisive against*
#: a claim about that matter (the rounds' binding text). They are registered
#: HERE, at Tier 3, rather than left unknown — an unknown domain is one nobody
#: has looked at, and these have been looked at. Being Tier 3 already denies
#: them a decisive verdict, which is the whole of the rule this loop can enforce
#: mechanically; the party-to-the-matter half needs a subject and is the
#: grader's.
TIER_3_REGISTERED_DOMAINS: frozenset[str] = frozenset(
    {
        "rt.com", "sputniknews.com", "tass.com", "tass.ru", "xinhuanet.com",
        "globaltimes.cn", "cgtn.com", "presstv.ir", "irna.ir", "kcna.kp",
        "prensa-latina.cu",
    }
)


@dataclass(frozen=True)
class SourceTier:
    """A URL's resolved tier, with everything the ledger row needs.

    ``tier`` is ``None`` for :data:`TIER_CLASS_UNKNOWN` — deliberately, so a
    caller that reaches for the integer gets nothing rather than a 3 nobody
    measured. ``matched`` names WHAT matched (the registrable domain or the
    suffix rule), so an operator asking "why is this Tier 1?" reads the answer
    off the row instead of re-deriving it.
    """

    url: str
    host: str
    tier: int | None
    tier_class: str
    matched: str | None
    registry_version: str = TIER_REGISTRY_VERSION

    @property
    def is_decisive_grade(self) -> bool:
        """Tier 1 or Tier 2 — the bound a decisive verdict must clear."""
        return self.tier in (TIER_PRIMARY, TIER_RECORD_PRESS)

    @property
    def is_unknown(self) -> bool:
        return self.tier_class == TIER_CLASS_UNKNOWN


def host_for_url(url: object) -> str:
    """The lowercase, ``www``-stripped host of ``url``, or ``""``.

    Total: a non-string, an empty string, a bare path or an unparseable URL all
    answer ``""``, which :func:`source_tier_for_url` then reports as UNKNOWN.
    A comparator that cannot see its input decides nothing.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    raw = url.strip()
    if "//" not in raw:
        # Tolerate a bare "reuters.com/article" — the search plane occasionally
        # hands back display URLs without a scheme.
        raw = f"//{raw}"
    try:
        host = (urlsplit(raw).hostname or "").strip().lower()
    except ValueError:
        return ""
    if not host:
        return ""
    host = strip_www(host)
    return host.rstrip(".")


def _domain_ladder(host: str) -> list[str]:
    """``news.bbc.co.uk`` -> ``[news.bbc.co.uk, bbc.co.uk, co.uk]``.

    The same progressive-trim ladder ``source_credibility.extract_lookup_hosts``
    walks; re-derived here (four lines) rather than importing that module, which
    pulls asyncpg and the Signal contract into a stdlib-only comparator.
    """
    parts = [p for p in host.split(".") if p]
    return [".".join(parts[i:]) for i in range(len(parts) - 1)] or ([host] if host else [])


def source_tier_for_url(url: object) -> SourceTier:
    """Resolve ``url`` against the published tier registry. Never raises.

    Resolution order, first hit wins: the Tier-1 suffix rules (every ministry
    and court on earth, which no list can enumerate), then the Tier-1 domain
    registry, then Tier 2, then the registered Tier-3 outlets. Anything left is
    :data:`TIER_CLASS_UNKNOWN` — **not** Tier 3 (F-3). The difference matters in
    one direction only, and it is the direction that costs truth: defaulting to
    Tier 3 suppresses a true CONTRADICTED on a legitimate regional outlet, and
    does it silently. Unknown is visible.
    """
    host = host_for_url(url)
    raw = url if isinstance(url, str) else ""
    if not host:
        return SourceTier(url=raw, host="", tier=None,
                          tier_class=TIER_CLASS_UNKNOWN, matched=None)
    for suffix in TIER_1_HOST_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            return SourceTier(url=raw, host=host, tier=TIER_PRIMARY,
                              tier_class=TIER_CLASS_1, matched=suffix)
    for candidate in _domain_ladder(host):
        if candidate in TIER_1_DOMAINS:
            return SourceTier(url=raw, host=host, tier=TIER_PRIMARY,
                              tier_class=TIER_CLASS_1, matched=candidate)
        if candidate in TIER_2_DOMAINS:
            return SourceTier(url=raw, host=host, tier=TIER_RECORD_PRESS,
                              tier_class=TIER_CLASS_2, matched=candidate)
        if candidate in TIER_3_REGISTERED_DOMAINS:
            return SourceTier(url=raw, host=host, tier=TIER_OTHER,
                              tier_class=TIER_CLASS_3, matched=candidate)
    return SourceTier(url=raw, host=host, tier=None,
                      tier_class=TIER_CLASS_UNKNOWN, matched=None)


# ---------------------------------------------------------------------------
# G-5 — THE DETERMINISTIC PRE-FILTER (no world truth-maker)
# ---------------------------------------------------------------------------
#
# The published lexicon. It is measured, not invented: over 1,619 live claims
# across three days it selects 148 = 9.1%, which is ~49 claims a day that a
# search can only ever answer NOT_FOUND about. Searching them does not merely
# waste a query — it deflates the decided rate with a stratum that was never
# decidable, which turns a truth number into a search-health number.

#: ``provenance`` — the claim's truth-maker is the PACKET, not the world.
#: R3 ML_A ``CR-2bbcd291`` and R2-C4 case #4 are both here.
_PROVENANCE_RE = re.compile(
    r"(verification floor|below the floor|principal units|unit reads?\b"
    r"|no head\b|head ages|this (?:read|packet|composition|assembly|cycle)"
    r"|produced verified reads|verified reads in this"
    r"|the corpus\b|our (?:corpus|slice|collection)"
    r"|derived[_ ]from|lineage walk)",
    re.IGNORECASE,
)

#: ``scope_bounded`` — true only OF the collection, by construction. The span
#: usually carries ``spans[].scope_tokens`` from
#: ``composition_integrity.has_collection_denominator_scope``; this lexicon is
#: the fallback for prose that never went through the assembly.
_SCOPE_BOUNDED_RE = re.compile(
    r"(this desk's collection|the collection\b|in this slice|slice covers"
    r"|as of \d|coverage of the|coverage coverage|monitored sources"
    r"|signals? (?:collected|ingested|observed) )",
    re.IGNORECASE,
)

#: ``perspective`` — a reading, not a fact. The Assessment's own FACT/PERSPECTIVE
#: splitter (D-6 §A2) is the primary detector for its population; this is the
#: shared fallback so the class exists for every population.
_PERSPECTIVE_RE = re.compile(
    r"(the (?:likely|plausible) reading|reads as|suggests that|appears to"
    r"|on balance|the more telling|what would change this|this reading)",
    re.IGNORECASE,
)


def uncheckable_class_for_claim(
    claim_text: object,
    *,
    scope_tokens: Sequence[str] | None = None,
    perspective: bool = False,
) -> str | None:
    """The claim's :data:`UNCHECKABLE_CLASSES` class, or ``None`` if searchable.

    Deterministic and PRE-SEARCH — that is the whole point. A claim whose
    truth-maker is the packet will answer NOT_FOUND to every query ever issued
    about it, and a NOT_FOUND is a statement about the search; letting ~49 of
    those a day into the searched population quietly deflates ``decided_rate``
    and makes a truth number look like a coverage number.

    ``scope_tokens`` is the assembly's own signal (``spans[].scope_tokens``) and
    WINS when present — the producer measured it, this lexicon only guesses.
    ``perspective`` is the Assessment splitter's verdict, passed in for the same
    reason.

    Order is provenance -> scope_bounded -> perspective, because a claim about
    the packet's own bookkeeping is a provenance claim even when it also names a
    collection.
    """
    if perspective:
        return UNCHECKABLE_PERSPECTIVE
    text = claim_text if isinstance(claim_text, str) else ""
    if _PROVENANCE_RE.search(text):
        return UNCHECKABLE_PROVENANCE
    if scope_tokens:
        return UNCHECKABLE_SCOPE_BOUNDED
    if _SCOPE_BOUNDED_RE.search(text):
        return UNCHECKABLE_SCOPE_BOUNDED
    if _PERSPECTIVE_RE.search(text):
        return UNCHECKABLE_PERSPECTIVE
    return None


# ---------------------------------------------------------------------------
# G-3 — THE TIME GATE
# ---------------------------------------------------------------------------


def as_datetime(value: object) -> datetime | None:
    """Coerce a wire timestamp to an aware UTC datetime, or ``None``.

    Accepts a ``datetime``, an ISO-8601 string (with or without ``Z``), and
    nothing else. ``None`` is a real answer: an undated source is not a source
    published today, and pretending otherwise is how a rolling page seven weeks
    stale becomes a decisive anchor (R2-C4 case #5).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class WindowBounds:
    """The admissible source window, resolved from a read's own stamp."""

    start: datetime | None
    end: datetime | None

    @property
    def measured(self) -> bool:
        return self.start is not None and self.end is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat() if self.start else None,
            "to": self.end.isoformat() if self.end else None,
        }


def evidence_window_bounds(
    evidence_window: Mapping[str, Any] | None,
    *,
    produced_at: object = None,
    grace_before_hours: float = 0.0,
) -> WindowBounds:
    """The admissible window from the READ's own ``evidence_window`` stamp.

    The stamp's shape is ``composition_window.evidence_window_span``'s:
    ``{"oldest": iso, "newest": iso, ...}``. ``from``/``to`` are accepted as
    aliases so a caller holding the API's own window object is not forced to
    rename it.

    The upper bound is ``max(newest, produced_at)`` — G-3's "plus the block's
    ``produced_at``" — because a read composed at 12:00Z may legitimately rest
    on a source published at 11:55Z that post-dates its newest consumed head.
    Beyond that, a source published AFTER the read cannot have supported it, and
    that is not a judgement call.

    ``grace_before_hours`` (default ``0.0`` — today's exact rule) moves ONLY
    ``start`` earlier, by that many hours. It exists because a same-event wire
    report a few hours ahead of a read's own grounding window is a timing
    artifact of when the read happened to compose, not evidence the read could
    not have seen; the live 2026-09-06 sweep found the majority of demoted,
    span-resolved proposals sitting inside a day of ``oldest`` on this side. The
    upper bound is UNTOUCHED by this parameter — a source published after the
    read cannot have supported it regardless of grace, and that half of G-3 is
    not a judgement call. A non-positive or unparsable value is a no-op.

    Flipping the operator-facing knob (``LEGBA_EXTERNAL_AUDIT_WINDOW_GRACE_HOURS``
    / the ``window_grace_hours`` handler option, both plumbed through
    ``_external_audit_width.run_width_tick``) above ``0`` in production is an
    INSTRUMENT CHANGE — see that module's banner for the stamp-lineage entry
    the operator's flip must add.

    An UNMEASURED window (``None``, or no parsable bound) returns
    ``WindowBounds(None, None)``, and :func:`check_span` then declines to run
    the gate at all. An unmeasured window is not an out-of-window source, and
    conflating them would demote every claim on a read with no datable head.
    """
    window = evidence_window if isinstance(evidence_window, Mapping) else {}
    start = as_datetime(window.get("oldest") or window.get("from"))
    end = as_datetime(window.get("newest") or window.get("to"))
    composed = as_datetime(produced_at)
    if composed is not None:
        end = composed if end is None else max(end, composed)
    if start is None or end is None:
        return WindowBounds(start=None, end=None)
    try:
        grace = float(grace_before_hours)
    except (TypeError, ValueError):
        grace = 0.0
    if grace > 0:
        start = start - timedelta(hours=grace)
    return WindowBounds(start=start, end=end)


def in_evidence_window(
    published_at: object,
    evidence_window: Mapping[str, Any] | None,
    *,
    produced_at: object = None,
    grace_before_hours: float = 0.0,
) -> bool | None:
    """Is a source published at ``published_at`` admissible? Tri-valued.

    ``True`` inside · ``False`` outside · ``None`` when the question cannot be
    asked (window unmeasured, or the source carries no publish date). ``None``
    is never silently ``True``: :func:`check_span` treats an undated source as
    inadmissible for a DECISIVE verdict and says so in its own words, and treats
    an unmeasured window as a gate that did not run. ``grace_before_hours`` is
    :func:`evidence_window_bounds`'s own knob, forwarded unchanged.
    """
    bounds = evidence_window_bounds(
        evidence_window, produced_at=produced_at, grace_before_hours=grace_before_hours
    )
    if not bounds.measured:
        return None
    published = as_datetime(published_at)
    if published is None:
        return None
    assert bounds.start is not None and bounds.end is not None  # narrowed
    return bounds.start <= published <= bounds.end


# ---------------------------------------------------------------------------
# THE GRADING ARCHIVE — its own path, never a signal (§0.11 / F-8)
# ---------------------------------------------------------------------------

#: The scheme. DIFFERENT from ``legba.data.archive.CAS_PREFIX`` on purpose: no
#: reader, no projection and no SQL ``LIKE`` can mistake a grading page for a
#: cited signal's archived bytes, which is the confusion F-8 forbids.
GRADING_CAS_PREFIX = "cas:grading/sha256/"

#: The subtree under the archive root. Sibling of the evidence CAS layout, never
#: interleaved with it, so an operator can delete, audit or move the grading
#: corpus without touching a single desk input.
GRADING_ARCHIVE_SUBDIR = "grading"


def grading_page_sha256(text: object) -> str:
    """The content address of a fetched grading page (UTF-8, NFC-free, raw).

    Hashes the bytes as fetched — NOT the fold — because the archive's job is to
    hold what the page said, and a folded copy cannot be re-checked against the
    live page later. The FOLD is applied at comparison time only.
    """
    raw = text if isinstance(text, str) else ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def grading_archive_ref(sha256_hex: str) -> str:
    """The relative content address recorded as ``external_grades.archive_ref``."""
    return f"{GRADING_CAS_PREFIX}{sha256_hex}"


def grading_archive_path(root: Path, sha256_hex: str) -> Path:
    """Filesystem location of a grading object under ``root``."""
    return Path(root) / GRADING_ARCHIVE_SUBDIR / sha256_hex[:2] / sha256_hex


def archive_grading_page(text: object, *, root: Path | None) -> str | None:
    """Write a fetched page into the grading archive; return its ``archive_ref``.

    ``root=None`` means "do not write" and returns the ref anyway, so a caller
    can content-address without a volume (every test, and the double-grade path,
    which must hand the audit rater the byte-identical envelope the primary saw
    rather than re-fetching it).

    Never raises: an unwritable volume returns the ref with the bytes unsaved
    rather than failing a verdict, because a grading run that cannot archive is
    degraded, not wrong. The caller's own degraded_reason carries that.
    """
    if not isinstance(text, str) or not text:
        return None
    digest = grading_page_sha256(text)
    ref = grading_archive_ref(digest)
    if root is None:
        return ref
    try:
        path = grading_archive_path(root, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(text, encoding="utf-8")
    except OSError:
        return ref
    return ref


# ---------------------------------------------------------------------------
# THE CHECK
# ---------------------------------------------------------------------------

#: Demotion reasons. Each is written into the ledger row's rationale, because
#: C4's overturned case is exactly what an unrecorded demotion looks like a
#: month later.
DEMOTION_SPAN_UNRESOLVED = "span_unresolved"
DEMOTION_NO_SPAN = "no_decisive_span"
DEMOTION_TIER_3 = "tier_3_only"
DEMOTION_OUT_OF_WINDOW = "out_of_window"
DEMOTION_NO_PUBLISH_DATE = "no_publish_date"

DEMOTION_REASONS: tuple[str, ...] = (
    DEMOTION_NO_SPAN,
    DEMOTION_SPAN_UNRESOLVED,
    DEMOTION_TIER_3,
    DEMOTION_OUT_OF_WINDOW,
    DEMOTION_NO_PUBLISH_DATE,
)

_REASON_PROSE: Mapping[str, str] = {
    DEMOTION_NO_SPAN: (
        "no decisive span was quoted — G-2: the span decides, and a verdict "
        "without one rests on the grader's memory of the page"
    ),
    DEMOTION_SPAN_UNRESOLVED: (
        "the quoted span does not appear in the fetched page under the shared "
        "fold — G-2: a page can be in the result set and not say this"
    ),
    DEMOTION_TIER_3: (
        "the decisive source is Tier 3 (corroboration only) — G-1: a decisive "
        "verdict needs a registered Tier-1/2 domain"
    ),
    DEMOTION_OUT_OF_WINDOW: (
        "the decisive source is published outside the read's own evidence "
        "window — G-3: a source the read could not have seen cannot support it"
    ),
    DEMOTION_NO_PUBLISH_DATE: (
        "the decisive source carries no publish date, so it cannot be anchored "
        "in the read's evidence window — G-3"
    ),
}


@dataclass(frozen=True)
class DecisiveClaim:
    """What the grader proposed: a claim, a verdict, and the span it rests on.

    Separated from the claim text so :func:`check_span` reads as what it is — a
    check on the GRADER's proposal, not a second opinion about the world.
    """

    text: str
    decisive_span: str = ""
    proposed_verdict: str = VERDICT_SUPPORTED

    @classmethod
    def coerce(cls, value: object) -> "DecisiveClaim":
        """Accept a :class:`DecisiveClaim`, a mapping, or a bare claim string."""
        if isinstance(value, DecisiveClaim):
            return value
        if isinstance(value, Mapping):
            verdict = str(
                value.get("proposed_verdict") or value.get("verdict")
                or VERDICT_SUPPORTED
            ).upper()
            return cls(
                text=str(value.get("text") or value.get("claim") or ""),
                decisive_span=str(
                    value.get("decisive_span") or value.get("span")
                    or value.get("quote") or ""
                ),
                proposed_verdict=verdict,
            )
        return cls(text=str(value or ""))


@dataclass(frozen=True)
class SpanCheck:
    """The evidentiary contract's verdict on ONE proposed decisive verdict.

    ``verdict`` is the FINAL one — the proposal if every gate passed, else
    :data:`VERDICT_NOT_FOUND`. ``demotions`` lists every gate that failed, not
    just the first: a source that is Tier 3 *and* out of window is two problems,
    and a rationale that names one of them invites a fix that changes nothing.

    ``tier_unknown`` is the F-3 field. It never demotes; it makes a decisive
    verdict on an unregistered domain COUNTABLE, so the publication can report
    that class separately instead of the number silently omitting it.
    """

    verdict: str
    proposed_verdict: str
    demoted: bool
    demotions: tuple[str, ...] = ()
    rationale: str = ""
    unchecked_reason: str | None = None
    span_resolved: bool = False
    decisive_span: str = ""
    decisive_span_sha256: str | None = None
    decisive_url: str = ""
    source_tier: int | None = None
    tier_class: str = TIER_CLASS_UNKNOWN
    tier_matched: str | None = None
    tier_unknown: bool = False
    decisive_published_at: str | None = None
    in_window: bool | None = None
    window: dict[str, Any] = field(default_factory=dict)
    archive_ref: str | None = None
    fold_version: str = FOLD_VERSION
    contract_version: str = CONTRACT_VERSION
    tier_registry_version: str = TIER_REGISTRY_VERSION

    @property
    def decisive(self) -> bool:
        return self.verdict in DECISIVE_VERDICTS

    def as_dict(self) -> dict[str, Any]:
        """The ledger-row projection — every §3.1 column this gate can fill."""
        return {
            "verdict": self.verdict,
            "proposed_verdict": self.proposed_verdict,
            "demoted": self.demoted,
            "demotions": list(self.demotions),
            "rationale": self.rationale,
            "unchecked_reason": self.unchecked_reason,
            "decisive_url": self.decisive_url or None,
            "decisive_span": self.decisive_span or None,
            "decisive_span_sha256": self.decisive_span_sha256,
            "decisive_source_tier": self.source_tier,
            "decisive_tier_class": self.tier_class,
            "decisive_published_at": self.decisive_published_at,
            "archive_ref": self.archive_ref,
            "span_resolved": self.span_resolved,
            "tier_unknown": self.tier_unknown,
            "in_window": self.in_window,
            "read_evidence_window": dict(self.window),
            "fold_version": self.fold_version,
            "contract_version": self.contract_version,
            "tier_registry_version": self.tier_registry_version,
        }


def check_span(
    claim: object,
    candidate_url: object,
    fetched_text: object,
    published_at: object = None,
    evidence_window: Mapping[str, Any] | None = None,
    *,
    produced_at: object = None,
    archive_root: Path | None = None,
    grace_before_hours: float = 0.0,
) -> SpanCheck:
    """Run G-1, G-2 and G-3 on one proposed decisive verdict. Pure; never raises.

    This is the function the auditor's hourly drain calls once per proposed
    SUPPORTED/CONTRADICTED, holding the page it already fetched. It answers a
    single question — *is this verdict admissible?* — and it answers it the way
    the rounds did, mechanically, with every failure named.

    ``claim`` is a :class:`DecisiveClaim` (or anything :meth:`DecisiveClaim.coerce`
    accepts). A NON-decisive proposal passes straight through unchanged: there
    is nothing to demote about a NOT_FOUND, and running the gates on one would
    invent a tier for a source nobody cited.

    ``grace_before_hours`` (default ``0.0``, today's byte-identical rule) is
    :func:`evidence_window_bounds`'s knob, forwarded to both the reported
    ``window`` and the G-3 comparison so the two never disagree. It only ever
    widens the window BACKWARDS — see that function's docstring.

    The gates are all evaluated — never short-circuited — so ``demotions`` is
    the complete list. The final verdict is :data:`VERDICT_NOT_FOUND` if any
    DEMOTING gate failed. ``tier_unknown`` is not a demoting gate (F-3).
    """
    proposal = DecisiveClaim.coerce(claim)
    tier = source_tier_for_url(candidate_url)
    window_bounds = evidence_window_bounds(
        evidence_window, produced_at=produced_at,
        grace_before_hours=grace_before_hours,
    )
    published = as_datetime(published_at)
    published_iso = published.isoformat() if published else None

    if proposal.proposed_verdict not in DECISIVE_VERDICTS:
        # Not a decisive proposal: record what we know, gate nothing.
        return SpanCheck(
            verdict=proposal.proposed_verdict,
            proposed_verdict=proposal.proposed_verdict,
            demoted=False,
            rationale="",
            decisive_url=str(candidate_url or ""),
            source_tier=tier.tier,
            tier_class=tier.tier_class,
            tier_matched=tier.matched,
            tier_unknown=tier.is_unknown,
            decisive_published_at=published_iso,
            window=window_bounds.as_dict(),
        )

    span = proposal.decisive_span or ""
    demotions: list[str] = []

    # -- G-2: the decisive span decides -----------------------------------
    if not normalize_for_match(span):
        span_resolved = False
        demotions.append(DEMOTION_NO_SPAN)
    else:
        span_resolved = fold_contains(fetched_text, span)
        if not span_resolved:
            demotions.append(DEMOTION_SPAN_UNRESOLVED)

    # -- G-1: source tier (unknown is VISIBLE, never a silent Tier 3) ------
    if tier.tier == TIER_OTHER:
        demotions.append(DEMOTION_TIER_3)

    # -- G-3: time anchoring ----------------------------------------------
    unchecked_reason: str | None = None
    in_window: bool | None = None
    if window_bounds.measured:
        in_window = in_evidence_window(
            published, evidence_window, produced_at=produced_at,
            grace_before_hours=grace_before_hours,
        )
        if in_window is False:
            demotions.append(DEMOTION_OUT_OF_WINDOW)
            unchecked_reason = UNCHECKED_OUT_OF_WINDOW
        elif in_window is None:
            demotions.append(DEMOTION_NO_PUBLISH_DATE)

    demoted = bool(demotions)
    verdict = VERDICT_NOT_FOUND if demoted else proposal.proposed_verdict
    archive_ref = (
        archive_grading_page(fetched_text, root=archive_root)
        if span_resolved or not demoted
        else None
    )
    rationale = _rationale(proposal, tier, demotions)

    return SpanCheck(
        verdict=verdict,
        proposed_verdict=proposal.proposed_verdict,
        demoted=demoted,
        demotions=tuple(demotions),
        rationale=rationale,
        unchecked_reason=unchecked_reason,
        span_resolved=span_resolved,
        decisive_span=span,
        decisive_span_sha256=(
            hashlib.sha256(normalize_for_match(span).encode("utf-8")).hexdigest()
            if span_resolved else None
        ),
        decisive_url=str(candidate_url or ""),
        source_tier=tier.tier,
        tier_class=tier.tier_class,
        tier_matched=tier.matched,
        tier_unknown=tier.is_unknown,
        decisive_published_at=published_iso,
        in_window=in_window,
        window=window_bounds.as_dict(),
        archive_ref=archive_ref,
    )


def _rationale(
    proposal: DecisiveClaim, tier: SourceTier, demotions: Sequence[str]
) -> str:
    """The demotion, written down. G-1's own requirement, applied to all gates.

    A verdict that was demoted and does not say why is indistinguishable, a
    month later, from a verdict the grader simply did not reach — which is the
    ambiguity the C4 audit had to resolve by hand across 27 atoms.
    """
    if not demotions:
        if tier.is_unknown:
            return (
                f"decisive verdict {proposal.proposed_verdict} on an "
                f"UNREGISTERED domain ({tier.host or 'no host'}): admitted and "
                "counted in the tier_unknown class, never pooled with Tier-1/2 "
                "(F-3, the visible option)"
            )
        return (
            f"decisive verdict {proposal.proposed_verdict} admitted: "
            f"{tier.tier_class} source, span resolves verbatim, inside the "
            "read's evidence window"
        )
    parts = [_REASON_PROSE.get(reason, reason) for reason in demotions]
    return (
        f"DEMOTED from {proposal.proposed_verdict} to {VERDICT_NOT_FOUND} — "
        + "; ".join(parts)
    )


__all__ = [
    "CONTRACT_VERSION",
    "DECISIVE_VERDICTS",
    "DEMOTION_NO_PUBLISH_DATE",
    "DEMOTION_NO_SPAN",
    "DEMOTION_OUT_OF_WINDOW",
    "DEMOTION_REASONS",
    "DEMOTION_SPAN_UNRESOLVED",
    "DEMOTION_TIER_3",
    "GRADING_ARCHIVE_SUBDIR",
    "GRADING_CAS_PREFIX",
    "TIER_1_DOMAINS",
    "TIER_1_HOST_SUFFIXES",
    "TIER_2_DOMAINS",
    "TIER_3_REGISTERED_DOMAINS",
    "TIER_CLASSES",
    "TIER_CLASS_1",
    "TIER_CLASS_2",
    "TIER_CLASS_3",
    "TIER_CLASS_UNKNOWN",
    "TIER_OTHER",
    "TIER_PRIMARY",
    "TIER_RECORD_PRESS",
    "TIER_REGISTRY_VERSION",
    "UNCHECKABLE_CLASSES",
    "UNCHECKABLE_PERSPECTIVE",
    "UNCHECKABLE_PROVENANCE",
    "UNCHECKABLE_SCOPE_BOUNDED",
    "UNCHECKED_OUT_OF_WINDOW",
    "VERDICT_CONTRADICTED",
    "VERDICT_NOT_FOUND",
    "VERDICT_SUPPORTED",
    "VERDICT_UNCHECKABLE",
    "VERDICT_UNCHECKED",
    "DecisiveClaim",
    "SourceTier",
    "SpanCheck",
    "WindowBounds",
    "archive_grading_page",
    "as_datetime",
    "check_span",
    "evidence_window_bounds",
    "grading_archive_path",
    "grading_archive_ref",
    "grading_page_sha256",
    "host_for_url",
    "in_evidence_window",
    "source_tier_for_url",
    "uncheckable_class_for_claim",
]
