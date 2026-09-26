# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — THE FENCES. Every one of them measured, none of them optional.

R1 ran the same model on the same prompt twice and the difference between the
runs IS this module. Run 1, with no fences: 13 developments, **1 of 13 spans
verified**, 11 source URLs the model had never fetched (several invented), 12 of
17 fetches burned on one host that 401'd it every time. Run 2, with three of the
fences below: 19 developments, **15 of 19 spans verified**, ZERO unfetched URLs,
8 of 8 dimensions carrying two verified developments. Same model, same prompt,
same web. The fences are the instrument.

Run 2 still shipped a FALSE development and an out-of-window one, which is why
two more fences are here that R1 only recommended:

  **F1 — FETCHED-URL MANIFEST.** A development may cite ONLY a URL this run
  actually fetched and archived. Prompted at commit time (the model is handed
  the list) and ENFORCED here at verify time, because a prompt is a request and
  a fence is a fence. Measured effect: unfetched citations 11 → 0.

  **F2 — DOMAIN BLOCKLIST.** After ``block_after`` failures a host is refused
  for the rest of the run and dropped from search results. Measured effect:
  Reuters fetch attempts 12 → 2, and the freed budget bought run 2's eleven
  extra successful fetches.

  **F3 — NOTE ENFORCEMENT.** After every fetch the model's next message must be
  a plain-text NOTE recording what the page establishes, with the span copied
  out of the text it was just shown. Tool calls that try to skip it are REFUSED
  and cost no budget. Run 2's harness refused 21 such calls. Without it the
  model writes its spans from memory after the page has been evicted, which is
  what produced run 1's 7.7% verification rate.

  **F4 — DATE GATE.** A development is accepted only if its page yields a
  publish date that falls inside the window. No date ⇒ REJECTED and counted.
  This kills the exact item run 2 shipped: a November-2024 appointment dated
  off a masthead and published as an in-window leadership transition. A
  reference carrying that marks a CORRECT read as contradicted — worse than no
  reference at all. WHICH dates count is :mod:`._reference_dates`' six-rung
  ladder (JSON-LD → meta → ``<time>`` → the URL → one unambiguous dateline →
  an official host's ``Last-Modified``); every accepted development carries the
  rung that dated it in ``date_source``, and ``date_disagreement`` when the
  page's own rungs answered differently.

  **F5 — TIER 1–2 DISCOVERY ALLOWLIST.** Discovery is filtered to official /
  IGO / central-bank / court / wire / record-press hosts. Run 2 anchored on
  ``londondaily.com``, ``hidabroot.com`` and ``energy-pedia.com`` and tiered
  them 2. Tier 3 is admitted ONLY for a dimension still carrying fewer than two
  Tier 1–2 developments after the first pass, and every item admitted that way
  is MARKED so a reader can see which parts of the reference rest on it.

  **F6 — SPAN VERIFICATION.** Every ``decisive_span`` must be an exact
  substring of the archived page after whitespace / curly-quote normalisation.
  Unverified developments are DROPPED, not flagged-and-kept, and the drop is
  counted. R1's comparison could not re-derive the Opus lane's self-reported
  33/33 because that lane shipped no archive; this one ships the archive.

AND THE CONSEQUENCE FENCE: per-dimension ``thin``. A dimension with fewer than
two SURVIVING verified developments is recorded thin, and ``thin_dimensions``
is the column the correctness grader already reads. A thin reference therefore
under-claims rather than mis-grades — which is the entire hybrid design: the
free lane builds a skeleton it can defend, and says out loud where it is thin.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from ._reference_page import (
    ReferenceArchive,
    host_of,
    norm_span,
    parse_iso_date,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# F5 — the Tier 1–2 discovery allowlist
# ---------------------------------------------------------------------------

#: Operator override, comma-separated hosts, ADDED to the defaults below (never
#: replacing them — an operator widening discovery should not silently narrow
#: it). Read at call time so a recreate is the only thing needed to change it.
ALLOWLIST_ENV = "LEGBA_REFERENCE_TIER12_HOSTS"

#: TIER 1 — primary and official. Government, IGO, central bank, court,
#: statistics office, election commission. The stage-1 instruction's own
#: category, as hosts. Matched by SUFFIX, so ``www.mfa.gov.il`` and
#: ``mfa.gov.il`` both match ``gov.il``.
TIER1_SUFFIXES: frozenset[str] = frozenset({
    # multilateral / IGO
    "un.org", "press.un.org", "news.un.org", "ohchr.org", "iaea.org",
    "who.int", "imf.org", "worldbank.org", "oecd.org", "wto.org",
    "nato.int", "europa.eu", "consilium.europa.eu", "ec.europa.eu",
    "icj-cij.org", "icc-cpi.int", "opcw.org", "unhcr.org", "unrwa.org",
    "ifrc.org", "icrc.org", "iea.org", "opec.org", "fatf-gafi.org",
    # national government TLD patterns (suffix match covers subdomains)
    "gov", "gov.uk", "gov.il", "gov.ua", "gov.au", "gov.in", "gov.za",
    "go.jp", "gouv.fr", "bund.de", "gob.es", "gob.mx", "gov.br", "gc.ca",
    "gov.ie", "gov.pl", "gov.sg", "gov.tr", "government.se", "regjeringen.no",
    # Wave O / o2: two official publishers the top-ups actually fetched and
    # that no suffix above reaches. ``bundestag.de`` is the German federal
    # parliament and is NOT under ``bund.de``; ``gov.pk`` is Pakistan's
    # government second level (``radio.gov.pk``, the state broadcaster). Both
    # were landing on Tier 3 — corroboration only — while an ordinary
    # newspaper sat on Tier 2.
    "bundestag.de", "gov.pk",
    # central banks / financial authorities
    "ecb.europa.eu", "federalreserve.gov", "bankofengland.co.uk",
    "bankisrael.gov.il", "boj.or.jp", "rbi.org.in", "bis.org",
    "treasury.gov", "sec.gov", "cbr.ru",
    # courts and legal record
    "supremecourt.uk", "supremecourt.gov", "curia.europa.eu", "echr.coe.int",
    # statistics
    "cbs.gov.il", "ons.gov.uk", "eurostat.ec.europa.eu", "bls.gov",
    "eia.gov", "census.gov",
})

#: TIER 2 — record press and domain reference monitors: major wires, newspapers
#: of record, and the monitors the stage-1 bounds name (ICG, ISW, IISS).
TIER2_SUFFIXES: frozenset[str] = frozenset({
    # wires
    "reuters.com", "apnews.com", "afp.com", "bloomberg.com", "dpa.com",
    "ansa.it", "efe.com", "kyodonews.net", "pap.pl",
    # newspapers / broadcasters of record
    "bbc.com", "bbc.co.uk", "ft.com", "nytimes.com", "wsj.com",
    "economist.com", "theguardian.com", "telegraph.co.uk", "dw.com",
    "lemonde.fr", "nikkei.com", "asahi.com", "spiegel.de", "faz.net",
    "zeit.de", "elpais.com", "corriere.it", "nrc.nl", "aljazeera.com",
    "france24.com", "rfi.fr", "euronews.com", "cnbc.com", "cnn.com",
    "nbcnews.com", "abcnews.go.com", "cbsnews.com", "npr.org",
    "washingtonpost.com", "latimes.com", "thetimes.co.uk", "independent.co.uk",
    "scmp.com", "straitstimes.com", "haaretz.com", "timesofisrael.com",
    "jpost.com", "kyivindependent.com", "pravda.com.ua",
    # domain reference monitors
    "crisisgroup.org", "understandingwar.org", "iiss.org", "sipri.org",
    "acleddata.com", "ucdp.uu.se", "chathamhouse.org", "carnegieendowment.org",
    "cfr.org", "csis.org", "rand.org", "amnesty.org", "hrw.org",
})

#: Hosts that are genuinely Tier 1-2 and that this lane CANNOT READ. Measured,
#: twice, in R1 and again in the first live R2 build: reuters.com serves 401 to
#: everything (robots.txt is ``Disallow: /`` for ``*`` — a permanent, deliberate
#: closure), apnews.com and timesofisrael.com serve a Cloudflare JS challenge,
#: haaretz.com is a subscription paywall. curl_cffi browser impersonation was
#: measured to change NOTHING on any of the four.
#:
#: They keep their TIER — they are wires and papers of record and saying
#: otherwise would be a lie about the source ladder — and they are dropped from
#: DISCOVERY, because a result the lane cannot read is not a lead, it is a
#: budget sink. The first live build spent 13 of 17 fetch attempts on Reuters.
#: This is DATA about reachability, not a claim about quality, which is why it
#: is a separate set from the tier lists and why a host leaves it by becoming
#: readable rather than by being re-argued.
#: D6 ADDED ``bloomberg.com``, measured on the 06:43Z Australia build: two
#: fetches, both unusable, both recorded as host failures by the blocklist. It
#: is a hard subscription paywall with a bot challenge in front of it, which is
#: the ``haaretz.com`` case exactly.
#:
#: D6 ALSO GAVE THIS SET A FETCH FENCE. Until then it was a DISCOVERY fence
#: only: results were dropped, and a model that wrote the URL out of its own
#: head still spent a pack invocation learning what this set already knew. The
#: 06:43Z build spent four of its eight fetch calls exactly that way. A fetch to
#: a host on this list is now refused in ``ToolPlane.fetch_page``, at no tool-
#: budget cost, before the pack is touched.
UNFETCHABLE_SUFFIXES: frozenset[str] = frozenset({
    "reuters.com", "reutersconnect.com",
    "apnews.com",
    "timesofisrael.com",
    "haaretz.com",
    "ft.com",
    "bloomberg.com",
})

#: What a host that is on NEITHER list is. Corroboration only, per stage-1.
TIER3 = 3

#: A dimension below this many TIER 1–2 developments after the first pass may
#: admit Tier 3 sources — the escape hatch that stops the allowlist turning a
#: thin record into an empty one.
TIER3_ADMISSION_BELOW = 2


def _suffix_match(host: str, suffixes: Iterable[str]) -> bool:
    """Does ``host`` equal, or sit under, any suffix?

    Suffix matching is on LABEL boundaries: ``gov.il`` matches ``mfa.gov.il``
    and never ``notgov.il``. A bare ``gov`` suffix therefore matches
    ``state.gov`` and ``*.gov`` and not ``gov.example.com``, which is the
    intent — ``.gov`` is a controlled TLD.
    """
    for suffix in suffixes:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def extra_allowed_hosts() -> frozenset[str]:
    raw = os.environ.get(ALLOWLIST_ENV, "")
    return frozenset(
        h.strip().lower().removeprefix("www.")
        for h in raw.split(",") if h.strip()
    )


def tier_of(url: str, extra: Iterable[str] = ()) -> int:
    """``1``, ``2`` or ``3`` for a URL's host. Unknown hosts are Tier 3.

    Fail-safe direction matters: an unrecognised host lands on Tier 3
    (corroboration only), never on Tier 2. A new outlet must be ADDED to be
    trusted; it is never trusted by not being recognised.
    """
    host = host_of(url)
    if not host:
        return TIER3
    if _suffix_match(host, TIER1_SUFFIXES):
        return 1
    if _suffix_match(host, TIER2_SUFFIXES) or _suffix_match(host, extra):
        return 2
    if _suffix_match(host, extra_allowed_hosts()):
        return 2
    return TIER3


def is_tier12(url: str, extra: Iterable[str] = ()) -> bool:
    return tier_of(url, extra) <= 2


def is_unfetchable(url: str) -> bool:
    """Is this host MEASURED unreadable by this lane? See UNFETCHABLE_SUFFIXES.

    Used to drop a result from DISCOVERY, never to reject a development: if one
    of these hosts ever does serve a page, the span check and the date gate
    judge it on exactly the same terms as any other.
    """
    host = host_of(url)
    return bool(host) and _suffix_match(host, UNFETCHABLE_SUFFIXES)


# ---------------------------------------------------------------------------
# F2 — the domain blocklist
# ---------------------------------------------------------------------------


@dataclass
class DomainBlocklist:
    """Refuse a host that has refused this run ``block_after`` times.

    ``block_after=0`` disables the fence entirely (run 1's behaviour), which
    exists only so a test can measure the fence against its own absence.
    """

    block_after: int = 2
    failures: dict[str, int] = field(default_factory=dict)
    refused: dict[str, int] = field(default_factory=dict)

    def record_failure(self, url: str) -> int:
        host = host_of(url)
        if not host:
            return 0
        self.failures[host] = self.failures.get(host, 0) + 1
        return self.failures[host]

    def blocked(self, url: str) -> bool:
        if self.block_after <= 0:
            return False
        host = host_of(url)
        return bool(host) and self.failures.get(host, 0) >= self.block_after

    def refuse(self, url: str) -> str:
        host = host_of(url)
        self.refused[host] = self.refused.get(host, 0) + 1
        return (
            f"{host} has refused this lane {self.failures.get(host, 0)} times; "
            "the builder will not spend another call on it. Find the same "
            "matter at a different outlet."
        )

    def filter_results(
        self, results: Sequence[Mapping[str, Any]]
    ) -> tuple[list[Mapping[str, Any]], list[str]]:
        if self.block_after <= 0:
            return list(results), []
        kept, dropped = [], []
        for result in results:
            url = str(result.get("url") or "")
            if self.blocked(url):
                dropped.append(host_of(url))
                continue
            kept.append(result)
        return kept, sorted(set(dropped))

    def as_record(self) -> dict[str, Any]:
        return {
            "block_after": self.block_after,
            "failures_by_host": dict(sorted(self.failures.items())),
            "refusals_by_host": dict(sorted(self.refused.items())),
        }


# ---------------------------------------------------------------------------
# F4 — the date gate
# ---------------------------------------------------------------------------

REJECT_NO_DATE = "no_machine_readable_date"
REJECT_OUT_OF_WINDOW = "outside_window"
REJECT_NOT_FETCHED = "url_not_fetched"
REJECT_SPAN_UNVERIFIED = "span_unverified"
REJECT_PAGE_UNUSABLE = "page_unusable"
REJECT_NO_SPAN = "no_decisive_span"
REJECT_TIER3_NOT_ADMITTED = "tier3_not_admitted"

#: Every rejection reason, so a receipt can report zeros rather than omit a key.
REJECTION_REASONS = (
    REJECT_NOT_FETCHED,
    REJECT_PAGE_UNUSABLE,
    REJECT_NO_DATE,
    REJECT_OUT_OF_WINDOW,
    REJECT_NO_SPAN,
    REJECT_SPAN_UNVERIFIED,
    REJECT_TIER3_NOT_ADMITTED,
)


def date_gate(
    page_date: str | None,
    window_start: date,
    window_end: date,
) -> tuple[bool, str]:
    """``(accepted, reason)`` for ONE page's publish date.

    The date is the PAGE's, never the model's: a development is dated by the
    page it cites, and a model-supplied ``publish_date`` is treated as a claim
    about the page rather than as evidence. That inversion is the whole fence.

    WHICH dates count is :mod:`._reference_dates`' question, not this one's —
    six ranked rungs from JSON-LD down to an official host's ``Last-Modified``,
    each naming itself in ``date_source``. This gate reads the ISO value the
    ladder produced and asks only whether it exists and falls in the window.
    """
    parsed = parse_iso_date(page_date)
    if parsed is None:
        return False, REJECT_NO_DATE
    if parsed < window_start or parsed > window_end:
        return False, REJECT_OUT_OF_WINDOW
    return True, ""


# ---------------------------------------------------------------------------
# F1 + F6 + F4 applied together: the commit-time fence pipeline
# ---------------------------------------------------------------------------


@dataclass
class FenceStats:
    """What the fences did, in numbers a receipt can publish without recounting."""

    candidates: int = 0
    accepted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    tier_counts: dict[str, int] = field(default_factory=dict)
    tier3_admitted: int = 0
    snippet_spans: int = 0

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    @property
    def span_verified_rate(self) -> float | None:
        """verified / candidates — NEVER null when anything was a candidate.

        Zero candidates is the one honest null: a rate over an empty denominator
        is not 0.0, it is undefined, and 0.0 would read as "every span failed".
        """
        if self.candidates <= 0:
            return None
        return round(self.accepted / self.candidates, 6)

    def as_record(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "verified": self.accepted,
            "span_verified_rate": self.span_verified_rate,
            "rejected": {r: self.rejected.get(r, 0) for r in REJECTION_REASONS},
            "rejected_total": sum(self.rejected.values()),
            "tier_counts": dict(sorted(self.tier_counts.items())),
            "tier3_admitted": self.tier3_admitted,
            "spans_traced_to_snippet": self.snippet_spans,
        }


def apply_fences(
    developments: Sequence[Mapping[str, Any]],
    *,
    archive: ReferenceArchive,
    window_start: date,
    window_end: date,
    tier3_dimensions: Iterable[str] = (),
    snippet_blob: str = "",
    extra_tier2: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], FenceStats]:
    """Run F1, F4, F5 and F6 over the model's committed developments.

    Returns the SURVIVORS and the counts. Order of checks is deliberate and
    cheapest-decisive-first, but more importantly it is ORDERED BY WHAT THE
    FAILURE MEANS, so a rejected item is attributed to the fence that actually
    caught it rather than to whichever ran first:

      1. **F1** — was this URL fetched at all? (an invented URL is not a bad
         quote, it is a fabrication, and must not be counted as a span failure)
      2. was the page usable, or a stub / challenge / paywall interstitial?
      3. **F4** — does the PAGE carry a date this lane can read, in window?
      4. **F6** — is the span an exact substring of the archived text?
      5. **F5** — is the source Tier 1–2, or Tier 3 admitted for this dimension?

    A development that fails any check is DROPPED. Nothing is repaired, nothing
    is guessed, and the model's own text is never edited — only kept or not.
    """
    tier3_ok = {str(d) for d in tier3_dimensions}
    stats = FenceStats()
    survivors: list[dict[str, Any]] = []
    normalised_snippets = norm_span(snippet_blob) if snippet_blob else ""

    for raw in developments:
        stats.candidates += 1
        dev = dict(raw)
        url = str(dev.get("source_url") or "").strip()

        # --- F1: the fetched-URL manifest, enforced ------------------------
        page = archive.lookup_exact(url)
        if page is None:
            loose = archive.lookup_loose(url)
            if loose is None:
                stats.reject(REJECT_NOT_FETCHED)
                continue
            # A loose match means the model mistyped a URL it really read. The
            # development is admissible against the page it actually fetched,
            # and the correction is RECORDED rather than silently applied.
            dev["source_url_as_written"] = url
            dev["source_url"] = loose.final_url or loose.url
            page = loose

        if not page.usable:
            stats.reject(REJECT_PAGE_UNUSABLE)
            continue

        # --- F4: the date gate --------------------------------------------
        dated, reason = date_gate(page.publish_date, window_start, window_end)
        if not dated:
            stats.reject(reason)
            continue
        dev["publish_date"] = page.publish_date
        dev["date_source"] = page.date_source
        if page.date_disagreement:
            # A page whose weaker rungs answered with a DIFFERENT date than the
            # one that dated it. The strongest rung still wins — that is the
            # precedence — and the dissent rides the row so a reader can see
            # that the page argued with itself and go and look.
            dev["date_disagreement"] = list(page.date_disagreement)

        # --- F6: span verification ----------------------------------------
        span = norm_span(str(dev.get("decisive_span") or ""))
        if not span:
            stats.reject(REJECT_NO_SPAN)
            continue
        if span not in norm_span(archive.text_for(page)):
            if normalised_snippets and span in normalised_snippets:
                stats.snippet_spans += 1
                dev["span_traced_to"] = "a search snippet, not the fetched page"
            stats.reject(REJECT_SPAN_UNVERIFIED)
            continue

        # --- F5: the tier allowlist ---------------------------------------
        tier = tier_of(dev["source_url"], extra_tier2)
        dims = _dimensions(dev)
        if tier >= TIER3:
            if not (tier3_ok and any(d in tier3_ok for d in dims)):
                stats.reject(REJECT_TIER3_NOT_ADMITTED)
                continue
            dev["tier3_admitted"] = True
            dev["tier3_admitted_for"] = sorted(d for d in dims if d in tier3_ok)
            stats.tier3_admitted += 1

        dev["source_tier"] = tier
        dev["span_verified"] = True
        dev["archive_sha256"] = page.archive_sha256
        dev["archive_path"] = page.archive_path
        dev["span_source"] = "archived_page"
        dev["outlet_host"] = page.host
        dev["fetch_ts"] = page.fetch_ts
        if page.depth:
            dev["licence_depth"] = page.depth
            dev["licence_depth_reason"] = page.depth_reason
        dev["archive_durable"] = bool(page.stored_body)
        if page.depth and page.depth != "full_text":
            # The LICENCE rule forbade storing this host's body. The span still
            # verified — it was checked against the text held in memory before
            # that text was discarded — and the development is kept with the
            # flag the ledger asks for, so a reader knows the archive cannot
            # re-serve it. NOTE what this does NOT key on: `stored_body` is also
            # false when there is simply no writable archive root, which is an
            # OPERATIONAL fact about this box and not a licence fact about this
            # publisher. Conflating them would label perfectly ordinary spans
            # snippet-sourced on any deployment without an archive volume.
            dev["span_source"] = "snippet"
            dev["archive_path"] = ""
        stats.tier_counts[f"tier{tier}"] = (
            stats.tier_counts.get(f"tier{tier}", 0) + 1
        )
        stats.accepted += 1
        survivors.append(dev)

    return survivors, stats


def _dimensions(dev: Mapping[str, Any]) -> list[str]:
    raw = dev.get("dimension")
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(d).strip() for d in raw if str(d).strip()]
    return []


def dimension_counts(
    developments: Sequence[Mapping[str, Any]],
    dimensions: Sequence[str],
    *,
    tier12_only: bool = False,
) -> dict[str, int]:
    """Per-dimension development counts over a fixed dimension list.

    ``tier12_only`` DERIVES the tier from each development's ``source_url``
    rather than reading a ``source_tier`` field. That is not defensiveness for
    its own sake: R1 run 1's tiering was not merely wrong but INVERTED — it
    labelled nine items Tier 1, seven of them pages it had never read. A count
    that decides whether to lower the source bar must not be computed from a
    number the model chose.
    """
    counts = {d: 0 for d in dimensions}
    for dev in developments:
        if tier12_only and not is_tier12(str(dev.get("source_url") or "")):
            continue
        for dim in _dimensions(dev):
            if dim in counts:
                counts[dim] += 1
    return counts


def thin_from_counts(counts: Mapping[str, int], below: int) -> list[str]:
    return sorted(d for d, n in counts.items() if n < below)


def tier3_dimensions_after_first_pass(
    noted: Sequence[Mapping[str, Any]],
    dimensions: Sequence[str],
    *,
    below: int = TIER3_ADMISSION_BELOW,
) -> list[str]:
    """Which dimensions may admit Tier 3 sources, after the first pass.

    The escape hatch for F5. It is computed from the TIER 1–2 developments the
    model has noted so far — so a dimension the record simply does not carry at
    record-press level opens up, and one that is already well anchored never
    does. Naming them (rather than flipping a global switch) is what keeps the
    marking honest: an item is admitted FOR a dimension, and says so.
    """
    counts = dimension_counts(noted, dimensions, tier12_only=True)
    return thin_from_counts(counts, below)


# ---------------------------------------------------------------------------
# F3 — the NOTE turn, as a state machine
# ---------------------------------------------------------------------------


@dataclass
class NoteGate:
    """After a successful fetch, the next turn must be a NOTE.

    Held here rather than inline in the loop so the fence is unit-testable
    without a model. ``max_consecutive`` stops a model that cannot produce a
    NOTE from spinning forever against the fence — after that many refusals in
    a row the loop lets the call through and RECORDS that it did, because a
    silently different protocol is worse than a measured exception to one.

    D6 LOWERED ``max_consecutive`` FROM THREE TO ONE: at most one refusal per
    fetch. The 06:43Z Australia build spent 18 rounds being refused and granted
    4 exceptions — a fifth of its model rounds and none of its tool budget, for
    a protocol the model was not going to learn inside one build. The record no
    longer depends on the model obeying: the loop writes its own manifest entry
    for every page (``_reference_manifest.record_page``) and the commit turn is
    built from THAT. The refusal is still here because a model that does write
    the NOTE writes a better reference, and one reminder is what buys it.
    """

    enabled: bool = True
    max_consecutive: int = 1
    pending: bool = False
    refusals: int = 0
    consecutive: int = 0
    exceptions: int = 0

    def after_fetch(self, *, ok: bool) -> None:
        self.pending = self.enabled and ok

    def should_refuse(self) -> bool:
        if not (self.enabled and self.pending):
            return False
        if self.consecutive >= self.max_consecutive:
            self.exceptions += 1
            self.pending = False
            self.consecutive = 0
            return False
        return True

    def refuse(self) -> str:
        self.refusals += 1
        self.consecutive += 1
        return (
            "REFUSED: protocol violation. You just read a page and did not "
            "write its NOTE. Your next message must be plain text beginning "
            "'NOTE:' recording what that page establishes, with the span "
            "copied verbatim out of the text you were shown. The page text is "
            "about to be evicted and your NOTE is the only record that survives."
        )

    def note_written(self) -> None:
        self.pending = False
        self.consecutive = 0

    def as_record(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "refusals": self.refusals,
            "exceptions_granted": self.exceptions,
        }


__all__ = [
    "ALLOWLIST_ENV",
    "REJECTION_REASONS",
    "REJECT_NOT_FETCHED",
    "REJECT_NO_DATE",
    "REJECT_NO_SPAN",
    "REJECT_OUT_OF_WINDOW",
    "REJECT_PAGE_UNUSABLE",
    "REJECT_SPAN_UNVERIFIED",
    "REJECT_TIER3_NOT_ADMITTED",
    "TIER1_SUFFIXES",
    "TIER2_SUFFIXES",
    "TIER3",
    "TIER3_ADMISSION_BELOW",
    "UNFETCHABLE_SUFFIXES",
    "DomainBlocklist",
    "FenceStats",
    "NoteGate",
    "apply_fences",
    "date_gate",
    "dimension_counts",
    "is_tier12",
    "is_unfetchable",
    "thin_from_counts",
    "tier3_dimensions_after_first_pass",
    "tier_of",
]
