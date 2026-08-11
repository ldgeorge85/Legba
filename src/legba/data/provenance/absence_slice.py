# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The V-B ABSENCE-SLICE subsystem — grammar, gazetteer, screen, slice loader.

Extracted from ``verify.py`` (K-1, 2026-08-03). The module-size gate
(``tests/test_module_size_gate.py``) named this the seam when the F-A precision
train pushed ``verify.py`` to 6,424 lines: *"the absence-slice subsystem (stage-1
screen + stage-2 adjudication + its gazetteer) is the extraction seam, owed to
cleanup Phase 2. Do not raise this again — extract."* This is that extraction,
taken BEFORE the V-G train adds to the same file.

WHAT LIVES HERE — everything the V-B branch needs that is PURE (no
``FaithfulnessReport``, no verdict arithmetic, no judge call):

  * the ABSENCE CLAIM GRAMMAR (:data:`_ABSENCE_MARKERS`,
    :func:`_is_absence_claim`, :data:`_COLLECTION_SCOPE_MARKERS`) — the calibrated
    lexical set the floor exemption, the V3 route and this branch all share;
  * the COUNTRY GAZETTEER (desk-slug expansion + the slice-scope country list +
    demonym tolerance) — the M15 cross-target guard reads the same tables;
  * the SLICE ROW model + the retained-slice LOADER
    (``analyst_traces.input_row_refs`` -> ``signals`` / ``analyst_outputs``);
  * the deterministic CLASSIFIER: scope-qualifier extraction, the W1(e) route
    exclusions, W1(d) carve-out clauses, the stage-1 term screen, and the
    stage-2 system prompt.

WHAT DELIBERATELY STAYS IN ``verify.py`` — the ORCHESTRATION
(``_fold_absence_slice`` / ``_absence_slice_stage2`` / ``_resolve_violating_row``),
because it manipulates the report + ledger types that module owns. The import
runs ONE WAY (``verify`` imports this; this imports nothing from ``verify``), so
there is no cycle, and ``verify`` RE-EXPORTS every name moved here — reaching for
``verify._ABSENCE_MARKERS`` or ``verify.load_absence_slice_rows`` still resolves.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The citation-marker shapes, spelled ONCE here so this module imports nothing
# from ``verify`` (which would close an import cycle). Used ONLY to blank markers
# out of claim prose before screening — never to RESOLVE one, which is
# ``verify._CLAIM_MARKER_RE`` / ``verify._REF_MARKER_RE``'s job. The spellings are
# pinned together by a drift guard (tests/data_pkg/test_verify_absence_slice.py
# ::test_marker_strip_mirrors_the_verify_regexes) so a change to either citation
# syntax cannot silently desync the screen.
_CITATION_MARKER_STRIP_RE = re.compile(r"\[\[ref:\d+\]\]|\[\d+\]")


# ABSENCE / negative-finding markers — a clause asserting something was NOT
# observed cannot cite a (non-existent) signal; you cite signals that EXIST, not
# their absence. This is the class that most crushed honest LOW-RISK reads.
_ABSENCE_MARKERS = (
    "no evidence",
    "no reports",
    "no report of",
    "no confirmed",
    "no indication",
    "no sign of",
    "no signs of",
    "no observable",
    "no observed",
    "no material",
    "no coordinated",
    "no discernible",
    "no credible",
    "no notable",
    "no data",
    "no direct",
    "no new",
    "no such",
    "not found",
    "none were",
    "were found in the signal",
    "absence of",
    "no relevant",
    "no significant",
    "nothing to suggest",
    "nothing indicating",
)


def _is_absence_claim(low: str) -> bool:
    """True when a lower-cased span is an ABSENCE / NEGATIVE assertion.

    Byte-identical to the FLOOR's absence exemption test in
    :func:`_is_fact_asserting` (the bare ``no ``/``none `` opener minus the
    positive-idiom guard, OR any ``_ABSENCE_MARKERS`` phrase) — extracted so the
    V3 classifier routes on the SAME calibrated lexical set the 10 in-window
    recalibrations tuned, guaranteeing the route and the floor exemption agree.
    """
    if (low.startswith("no ") or low.startswith("none ")) and not low.startswith(
        ("no fewer", "no less", "no longer", "no doubt", "no single", "no one")
    ):
        return True
    return any(marker in low for marker in _ABSENCE_MARKERS)


# Collection-scoping lexicon — language that bounds a negative to what was
# actually collected/searched, NOT the world. Deliberately GENEROUS (substring,
# lowercased): a scoped variant slipping through unflagged is the cheap error;
# flagging a genuinely scoped negative is the expensive one. Every phrasing the
# unit prompts now recommend ("in collected reporting", "this desk's sources",
# "the corpus searched", "the reviewed documents", ...) matches here.
_COLLECTION_SCOPE_MARKERS: tuple[str, ...] = (
    # desk / collection possessives
    "this desk",
    "the desk",
    "desk's",
    # collection / gathering stems (cover "collected signals", "the collection
    # window", "monitored sources", "reviewed reporting", "the corpus
    # searched", ...)
    "collected",
    "collection",
    "gathered",
    "ingested",
    "monitored",
    "sampled",
    "reviewed",
    "examined",
    "analyzed",
    "analysed",
    "searched",
    # bounded corpus / slice / window nouns
    "corpus",
    "working set",
    "signal set",
    "source set",
    "evidence set",
    "slice",
    "window",
    # available-evidence idioms
    "available reporting",
    "available sources",
    "available signals",
    "available evidence",
    "in the available",
    # bounded-referent signal/source idioms ("in the signals" names the slice;
    # bare "no signals report X" does not)
    "in the signals",
    "among the signals",
    "across the signals",
    "of the signals",
    "in these signals",
    "in the signal",
    "in the sources",
    "among the sources",
    "across the sources",
    "of the sources",
    "in these sources",
    "in our sources",
    "in the documents",
    "among the documents",
    "of the documents",
    "in the evidence",
    "in the cited",
)


# A compact country gazetteer + desk-slug expansion, MIRRORING
# legba.runtime.grounding (_KNOWN_COUNTRY_TOKENS / _TARGET_SLUG_TO_NAMES) — a
# deliberate slim-safe local copy so verify.py imports nothing from runtime.
_TARGET_SLUG_TO_COUNTRY: dict[str, tuple[str, ...]] = {
    "us": ("united states", "america", "u.s.", "usa"),
    "cn": ("china",), "ru": ("russia",), "ir": ("iran",), "il": ("israel",),
    "in": ("india",), "id": ("indonesia",), "br": ("brazil",),
    "ar": ("argentina",), "mx": ("mexico",), "ca": ("canada",),
    "fr": ("france",), "de": ("germany",), "it": ("italy",),
    "gb": ("united kingdom", "britain", "uk"), "uk": ("united kingdom", "britain"),
    "jp": ("japan",), "kr": ("south korea", "korea"), "sa": ("saudi arabia",),
    "tr": ("turkey", "turkiye"), "au": ("australia",), "za": ("south africa",),
    "eu": ("european union",), "kp": ("north korea", "dprk"), "tw": ("taiwan",),
    "ua": ("ukraine",), "pk": ("pakistan",),
}
_COUNTRY_TOKENS: frozenset[str] = frozenset({
    "united states", "america", "u.s.", "usa",
    "china", "russia", "iran", "israel", "ukraine", "india", "indonesia",
    "brazil", "argentina", "mexico", "canada", "france", "germany", "italy",
    "spain", "united kingdom", "britain", "japan", "south korea", "north korea",
    "korea", "saudi arabia", "turkey", "turkiye", "australia", "south africa",
    "egypt", "pakistan", "afghanistan", "iraq", "syria", "lebanon", "yemen",
    "venezuela", "taiwan", "vietnam", "thailand", "philippines", "nigeria",
    "romania", "poland", "greece", "hungary", "bulgaria", "serbia", "croatia",
})


def _country_desk_slug(target_id: str | None) -> str | None:
    """The trailing ISO-2 slug of a ``country_*`` target id, else ``None``."""
    if not target_id or not isinstance(target_id, str):
        return None
    tid = target_id.strip().lower()
    if "country" not in tid:
        return None
    token = tid.rsplit("_", 1)[-1]
    return token if len(token) == 2 and token.isalpha() else None


def _mentions_country(name: str, haystack_lc: str) -> bool:
    """Whole-word (token-boundary) mention of ``name`` in a casefolded haystack."""
    nlc = name.casefold()
    return re.search(rf"(?<![a-z0-9]){re.escape(nlc)}(?![a-z0-9])", haystack_lc) is not None


# V-I2 (2026-08-05) — THE SHORT FORMS A CASEFOLDED GAZETTEER CANNOT CARRY.
#
# Two acceptance panels running, `cross_target_leak` has been 100% false, on the
# same mechanism both times. The 08-05 H6 specimen: a `narrative_coordination`
# finding on the `country_g20_us` desk that says "US" at least six times — "an
# imminent US-Iran-Oman agreement", "US-focused outlets", "a US State Department
# briefing" — flagged as naming only Iran and never its own country, and stamped
# HARD, i.e. an entity-scramble accusation against a finding that is squarely on
# target. 08-04 recommendation #4 named it; it did not ship; here it is again.
#
# The gazetteer holds "united states", "america", "u.s." and "usa" and every
# comparison is casefolded, so the ONE form the corpus writes most often — bare
# "US" — cannot be added there: casefolded it is the English pronoun "us", which
# would match "tells us that" in any prose and silently disable the guard for
# that desk (the same trap the ISO-2-slug comment below already documents).
#
# So the short forms get a CASE-SENSITIVE set of their own, checked against the
# raw haystack with alphanumeric boundaries. "US" matches "US-Iran" and not
# "BUSINESS"; "u.s." keeps working from the casefolded set for the lowercase
# spellings. Demonyms ("American", "Chinese", "Israeli") are NOT here — they come
# free from :func:`_names_country`, which the guard now uses on both arms.
#
# Scope: the G20 desks plus the two Koreas and the EU, i.e. every slug in the
# table above that has an abbreviation a newsroom actually uses. A desk with no
# entry is unchanged.
_TARGET_SLUG_TO_ABBREV: dict[str, tuple[str, ...]] = {
    "us": ("US", "U.S.", "U.S", "USA", "U.S.A."),
    "gb": ("UK", "U.K.", "U.K", "GB"),
    "uk": ("UK", "U.K.", "U.K", "GB"),
    "eu": ("EU", "E.U."),
    "cn": ("PRC",),
    "kr": ("ROK",),
    "kp": ("DPRK",),
    "za": ("RSA",),
    "ae": ("UAE",),
}


def _mentions_abbrev(form: str, haystack: str) -> bool:
    """CASE-SENSITIVE whole-token mention of an abbreviation in RAW text.

    Case-sensitive because that is the whole point: "US" is a country and "us"
    is a pronoun. Boundaries are alphanumeric on both sides, so "US-Iran" and
    "the U.S. State Department" match while "BUSINESS" does not.
    """
    return (
        re.search(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", haystack)
        is not None
    )


def _mentions_own_country(slug: str, names: set[str], title: str, body: str) -> bool:
    """Does this finding name its OWN desk country, in any form it might use?

    Three surfaces, any of which is enough: the gazetteer name, its demonym (via
    :func:`_names_country`'s suffix rule + irregular table), or a case-sensitive
    abbreviation. Fail-OPEN by design — every additional form can only ever
    SUPPRESS a `cross_target_leak` flag, and that flag is hard.
    """
    haystack = f"{title}\n{body}"
    if any(_names_country(n, haystack.casefold()) for n in names if n):
        return True
    return any(
        _mentions_abbrev(form, haystack)
        for form in _TARGET_SLUG_TO_ABBREV.get(slug, ())
    )


# ---------------------------------------------------------------------------
# V-B (2026-07-31) — SCOPED-ABSENCE claims judged against the SLICE, not the
# citation subset. The readout's 6x hard-fail enrichment class.
#
# "No NEW / LARGE-SCALE / TIGHTENED X" is a claim about the WHOLE input slice —
# every row the analyst read. The judge is shown only the CITATION subset, so it
# reads topical term-PRESENCE as contradiction and hard-fails these at ~6x the
# base rate (83% of same-model hard-fails were absence claims; internal_stability
# alone carried 17 of 40 Cerebras hard-fails). The measured artifacts are exactly
# this: "None of the 25 recent signals report new or tightened sanctions
# designations … affecting Haiti", hard-failed because the slice mentions Haiti
# and sanctions — not because any row reports a new designation.
#
# The slice IS retained: ``analyst_traces.input_row_refs``, one row per run
# (run_id is the PK), written before the verify pass runs in the same actor turn.
# It was simply never consulted. Two stages, cheapest first:
#
#   STAGE 1 (deterministic, always) — take the claim's CONTENT terms (its own
#     nouns, minus stopwords, minus the absence/scope vocabulary, minus the
#     DESK'S OWN country tokens — every title on a country desk names the
#     country, so those collide with everything and carry no signal) and screen
#     the slice TITLES for a term collision. NO collision ⇒ nothing in the slice
#     is even topically about the thing said to be absent ⇒ the absence is
#     VERIFIED against its actual scope: ``absence_slice_verified``.
#   STAGE 2 (one bounded LLM call, only on a collision) — the candidate titles
#     go to the judge route: "does any of these violate this scoped negative?"
#     A violation must NAME the violating title, resolved against the candidate
#     set the same way V-D resolves an evidence quote — an unresolvable answer
#     decides nothing. This is the Haiti / "Sanctioned-headline" class, i.e.
#     exactly where the value is, and the only place cost is spent.
#
# HONESTY (B3): a slice that cannot be read — no run_id passed, trace pruned by
# the retention sweep, a read error — degrades to TODAY'S behavior, counted
# ``absence_slice_unavailable``. We never fabricate a pass from a missing slice.
#
# W31 COEXISTENCE: a claim already flagged ``unscoped_absence_claim`` is SKIPPED
# (counted ``absence_slice_scope_flagged``). The two checks are orthogonal — W31
# is about the claim's SCOPE LANGUAGE (a world-scoped negative on a thin desk),
# V-B about its CONTENT against the slice — and letting a content pass erase the
# phrasing flag would silently retire a live detector. Once the producer scopes
# the prose ("in collected reporting"), W31 clears and V-B takes over.
# ---------------------------------------------------------------------------

_ABSENCE_SLICE_CONTRADICTED = "absence_slice_contradicted"

#: Retained-slice rows we will look at (the cadence slice caps at 120; 360 is
#: three windows of headroom and still one bounded query).
_ABSENCE_SLICE_TITLE_CAP = 360
#: Candidate titles carried into the ONE stage-2 call, per finding.
_ABSENCE_SLICE_CANDIDATE_CAP = 24
#: A screen term present in this FRACTION of the slice titles or more
#: discriminates nothing (the desk's own country name is the canonical case) and
#: is dropped from stage 1. Data-driven, so it needs no per-desk gazetteer.
_ABSENCE_SLICE_UBIQUITY = 0.6
#: Minimum content terms a claim must yield before stage 1 means anything. A
#: claim with none ("nothing of note was observed") is unscreenable — today's path.
_ABSENCE_SLICE_MIN_TERMS = 1

# The SCALE / NOVELTY qualifiers that make an absence claim SCOPED — the shape
# the readout measured at 6x. An absence claim WITHOUT one of these is a plain
# negative and keeps today's route (V3 absence rubric); this branch owns the
# qualified class only.
_ABSENCE_SCOPE_QUALIFIERS: tuple[str, ...] = (
    "new", "newly", "fresh", "additional", "further", "renewed",
    "large-scale", "large scale", "largescale", "mass", "major",
    "significant", "substantial", "sweeping", "widespread",
    "tightened", "tighter", "tightening", "expanded", "expansion",
    "escalated", "escalating", "escalation",
    "sudden", "unprecedented", "systematic", "systemic", "coordinated",
    "formal", "official", "confirmed", "credible", "overt", "direct",
    "notable", "material", "serious", "concrete", "meaningful",
)

# Words that carry no topical signal in a title screen: function words, the
# absence/scope vocabulary itself, and the reporting verbs every headline shares.
_ABSENCE_SCREEN_STOPWORDS: frozenset[str] = frozenset(
    """
    none nothing neither nor without absent absence lack lacking
    report reports reported reporting record records recorded observe observed
    observable detect detected detectable identify identified indicate indicated
    indication indications evidence evident sign signs signal signals source
    sources data reporting statement statements announcement announcements
    activity activities development developments event events item items
    window period cycle month week day days today recent recently current
    currently within during across among between about against toward towards
    their there these those this that with from into over under than then
    which while where when what whom whose have has had been being were was
    are is not any all both each other others some more most such only just
    also very much many few less least same still both either
    country countries state states government governmental national international
    public private official officially available reviewed collected monitored
    analyzed analysed examined sampled ingested gathered searched corpus slice
    target targets desk desks unit units finding findings claim claims
    """.split()
)


# ---------------------------------------------------------------------------
# W1 (2026-08-02) — CONTRADICTED-BRANCH PRECISION.
#
# The 08-02 acceptance readout measured ``absence_slice_contradicted`` at ~46%
# precision while carrying 50% of ALL hard fails. The operator decision was to
# keep the class HARD and make the FILTERS precise, so a violator row must now
# clear four mechanical screens before it can contradict a scoped negative:
#
#   (a) TARGET SCOPE — a row about a DIFFERENT country than the claim's desk (or
#       than the countries the claim itself enumerates) cannot violate it. Stage
#       1 strips the desk's own country tokens to make term matching work at all
#       (every title on a country desk names the country), and that same strip is
#       what let a Benin coup headline hard-fail a SOUTH AFRICA claim and an
#       Argentina row hard-fail an Americas claim.
#   (b) COMPOSITION BODIES, NOT TITLES — a composition's slice rows are
#       ``analyst_outputs``, whose TITLE names the TOPIC and never the verdict
#       ("Mexico – Narrative Coordination … Assessment" was read as evidence that
#       Mexico HAS coordination, when the finding's own body says it does not).
#       Composed rows are screened and shown BODY-first.
#   (c) MACHINE-STRUCTURED ROWS — a GDELT/CAMEO event record is a machine coding
#       of a wire report, not reporting: "COLLEGE: protest in Japan" hard-failed a
#       "no protests" claim. Verified read-only against the live substrate:
#       ``signals.source_id = 'source.gdelt.files'`` /
#       ``raw_provenance->>'kind' = 'gdelt_files'`` (8,847 rows) carry the CAMEO
#       ``ACTOR[ <-> ACTOR]: <action>[ in <place>]`` title shape, while
#       ``source.gdelt.doc_api`` (552 rows) carries REAL headlines and is NOT
#       excluded. Both the source id and the title SHAPE are screened, so a
#       renamed feed cannot silently reopen the hole.
#   (d) CARVE-OUTS — "no escalation BEYOND the existing measures" and "no
#       CONFIRMED changes GIVEN the below-floor signals" were contradicted by the
#       very thing they exempt. The claim's carve-out clauses now ride into the
#       stage-2 prompt.
#
# Plus (e) a tighter route (volume / continuity / trajectory claims are not
# slice-checkable negatives) and (f) slice-size honesty in the detail string.
# ---------------------------------------------------------------------------

#: A composed slice row (``analyst_outputs``) is screened and SHOWN by its BODY —
#: bounded, because 24 candidates ride into one prompt.
_ABSENCE_SLICE_BODY_CHARS = 500
#: Below this many eligible slice rows, a "verified" absence is a WEAK result and
#: the detail string must say so (readout: 3/24 sampled passes verified against a
#: 1-row slice with a detail that read as strong verification).
_ABSENCE_SLICE_THIN_ROWS = 3

#: Source ids whose rows are MACHINE-STRUCTURED event codings rather than
#: reporting (see (c) above). Verified read-only against the live substrate.
_MACHINE_STRUCTURED_SOURCE_IDS: frozenset[str] = frozenset({"source.gdelt.files"})
#: The matching ``raw_provenance->>'kind'`` stamps, so a source-id rename does not
#: reopen the hole on its own.
_MACHINE_STRUCTURED_PROVENANCE_KINDS: frozenset[str] = frozenset({"gdelt_files"})
#: The CAMEO title SHAPE backstop: an ALL-CAPS actor (optionally two, joined by
#: ``<->``), a colon, then a LOWERCASE action verb — "COLLEGE: protest in Japan",
#: "SAUDI <-> SANAA: protest in Saudi Arabia". The lowercase-verb requirement is
#: what keeps a real "UN: Sudan famine declared" headline OUT of the exclusion.
_CAMEO_TITLE_RE = re.compile(
    r"^\s*[A-Z][A-Z0-9 .,'’()&/\-]{1,}"
    r"(?:<->\s*[A-Z0-9 .,'’()&/\-]+)?"
    r":\s+[a-z]"
)

# The V-B scope gazetteer. DELIBERATELY separate from ``_COUNTRY_TOKENS`` (the
# M15 cross-target guard's list): widening M15 would change which findings that
# guard flags, which is not this train's business. Keys are the ISO-2 desk slugs
# the platform actually runs (32 country desks, verified against live
# ``analyst_outputs.target_id``); values are the surface forms a slice row uses.
_SLICE_DESK_COUNTRIES: dict[str, tuple[str, ...]] = {
    "ar": ("argentina",), "au": ("australia",), "br": ("brazil",),
    "bf": ("burkina faso",), "ca": ("canada",), "cd": ("democratic republic of the congo", "drc", "congo"),
    "cn": ("china",), "de": ("germany",), "fr": ("france",),
    "gb": ("united kingdom", "britain", "uk"), "ht": ("haiti",),
    "id": ("indonesia",), "il": ("israel",), "in": ("india",),
    "ir": ("iran",), "it": ("italy",), "jp": ("japan",),
    "kp": ("north korea", "dprk"), "kr": ("south korea",),
    "ml": ("mali",), "mm": ("myanmar", "burma"), "mx": ("mexico",),
    "ne": ("niger",), "pk": ("pakistan",), "ru": ("russia",),
    "sa": ("saudi arabia",), "sd": ("sudan",), "tr": ("turkey", "turkiye"),
    "tw": ("taiwan",), "ua": ("ukraine",),
    "us": ("united states", "america", "u.s.", "usa"), "za": ("south africa",),
}

# The countries a slice row can be ABOUT. Broader than the desk list on purpose:
# the exclusion only bites when a row names a country the claim's scope does NOT
# cover, so a country missing here simply fails OPEN (the row stays a candidate),
# which is the cheap error. A country wrongly listed would exclude real
# violators, so the list is plain country names only — no demonym guesswork
# beyond the suffix tolerance in :func:`_names_country`.
_SLICE_GEO_COUNTRIES: frozenset[str] = frozenset(
    {name for names in _SLICE_DESK_COUNTRIES.values() for name in names}
    | {
        "afghanistan", "albania", "algeria", "angola", "armenia", "austria",
        "azerbaijan", "bahrain", "bangladesh", "belarus", "belgium", "benin",
        "bolivia", "bosnia", "botswana", "bulgaria", "burundi", "cambodia",
        "cameroon", "chad", "chile", "colombia", "costa rica", "croatia", "cuba",
        "cyprus", "czechia", "denmark", "djibouti", "dominican republic",
        "ecuador", "egypt", "el salvador", "eritrea", "estonia", "ethiopia",
        "finland", "gabon", "gambia", "georgia", "ghana", "greece", "guatemala",
        "guinea", "guyana", "honduras", "hungary", "iceland", "iraq", "ireland",
        "ivory coast", "jamaica", "jordan", "kazakhstan", "kenya", "kosovo",
        "kuwait", "kyrgyzstan", "laos", "latvia", "lebanon", "liberia", "libya",
        "lithuania", "madagascar", "malawi", "malaysia", "mauritania",
        "moldova", "mongolia", "montenegro", "morocco", "mozambique", "namibia",
        "nepal", "netherlands", "new zealand", "nicaragua", "nigeria",
        "north macedonia", "norway", "oman", "panama", "papua new guinea",
        "paraguay", "peru", "philippines", "poland", "portugal", "qatar",
        "romania", "rwanda", "senegal", "serbia", "sierra leone", "singapore",
        "slovakia", "slovenia", "somalia", "south sudan", "spain", "sri lanka",
        "sweden", "switzerland", "syria", "tajikistan", "tanzania", "thailand",
        "togo", "tunisia", "turkmenistan", "uganda", "united arab emirates",
        "uruguay", "uzbekistan", "venezuela", "vietnam", "yemen", "zambia",
        "zimbabwe",
    }
)


@dataclass(frozen=True)
class SliceRow:
    """One retained input-slice row, as the V-B screen sees it.

    ``text`` is what stage 1 screens and stage 2 is SHOWN — a signal's title, or
    a composed row's BODY (W1(b): a composed row's title names the topic, never
    the verdict). ``machine_structured`` marks a GDELT/CAMEO event coding, which
    is a machine reading of a wire report rather than reporting (W1(c)).
    """

    text: str
    kind: str = "signal"
    source_id: str = ""
    machine_structured: bool = False


def _row_field(row: Any, key: str, default: str = "") -> str:
    """One string column off an asyncpg Record / plain mapping, tolerantly.

    A row shape that predates a projection widening (or a test double) must not
    raise — the V-B path degrades, it never breaks the verify pass.
    """
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return str(value or default)


def _is_machine_structured_row(
    *, source_id: str, provenance_kind: str, title: str
) -> bool:
    """True for a MACHINE-CODED event record (GDELT/CAMEO), not reporting (W1(c)).

    Three independent markers, any of which is sufficient: the source id, the
    raw-provenance kind stamp, and the CAMEO title SHAPE. The shape backstop is
    what covers a composed slice row (no source id to read) and a renamed feed.
    """
    if source_id.strip().lower() in _MACHINE_STRUCTURED_SOURCE_IDS:
        return True
    if provenance_kind.strip().lower() in _MACHINE_STRUCTURED_PROVENANCE_KINDS:
        return True
    return bool(title and _CAMEO_TITLE_RE.match(title))


# The IRREGULAR demonyms the suffix rule below cannot derive ("spain" → "spanish",
# "france" → "french"). Only the forms a news headline actually uses; a country
# missing here simply fails OPEN, which is the cheap error.
_IRREGULAR_DEMONYMS: dict[str, tuple[str, ...]] = {
    "belgium": ("belgian",), "britain": ("british",), "china": ("chinese",),
    "czechia": ("czech",), "denmark": ("danish", "danes"),
    "democratic republic of the congo": ("congolese",), "congo": ("congolese",),
    "egypt": ("egyptian",), "finland": ("finnish",), "france": ("french",),
    "germany": ("german",), "greece": ("greek",), "iceland": ("icelandic",),
    "ireland": ("irish",), "italy": ("italian",), "mexico": ("mexican",),
    "myanmar": ("burmese",), "netherlands": ("dutch",), "norway": ("norwegian",),
    "philippines": ("filipino", "philippine"), "poland": ("polish", "poles"),
    "portugal": ("portuguese",), "serbia": ("serbian",), "slovakia": ("slovak",),
    "spain": ("spanish",), "sweden": ("swedish", "swedes"),
    "switzerland": ("swiss",), "thailand": ("thai",), "turkey": ("turkish",),
    "turkiye": ("turkish",), "united kingdom": ("british",),
    "venezuela": ("venezuelan",), "vietnam": ("vietnamese",),
}


def _names_country(name: str, haystack_lc: str) -> bool:
    """Whole-word country mention, tolerant of the demonym forms.

    Regular suffixes are derived ("haitian" counts as naming Haiti, "iranian" as
    Iran, "japanese" as Japan); the irregulars come from the table above. Applied
    SYMMETRICALLY (to the claim's scope countries and to a row's), so the
    tolerance cannot skew the in-scope / off-scope decision in either direction.
    """
    for form in (name, *_IRREGULAR_DEMONYMS.get(name, ())):
        # Cheap substring reject first: this runs over the whole gazetteer for
        # every slice row, and the regex is ~50x the cost of the `in` test.
        if form not in haystack_lc:
            continue
        suffix = r"(?:n|an|ian|ese|i)?" if form == name else ""
        if re.search(
            rf"(?<![a-z0-9]){re.escape(form)}{suffix}(?![a-z0-9])", haystack_lc
        ):
            return True
    return False


def _slice_scope_countries(claim: str, *, target_id: str | None) -> frozenset[str]:
    """The countries a violator row must be ABOUT to contradict this claim (W1(a)).

    Two sources, unioned: the DESK's own country (a ``country_*`` target id) and
    every country the CLAIM ITSELF names (a region composition's clause
    enumerating "Canada, Brazil, Haiti, and Mexico" scopes itself). Empty →
    the filter is inert and every row stays eligible (fail OPEN: an unmapped or
    non-country desk whose claim names nobody tells us nothing about scope).
    """
    out: set[str] = set()
    slug = _country_desk_slug(target_id)
    if slug:
        out.update(_SLICE_DESK_COUNTRIES.get(slug, ()))
        out.update(_TARGET_SLUG_TO_COUNTRY.get(slug, ()))
    claim_lc = claim.casefold()
    out.update(c for c in _SLICE_GEO_COUNTRIES if _names_country(c, claim_lc))
    return frozenset(out)


def _row_in_claim_scope(text: str, scope: frozenset[str]) -> bool:
    """Can this slice row's subject possibly bear on a claim with this scope?

    Conservative fail-OPEN, mirroring :func:`cross_target_leak_span`: an empty
    scope keeps every row; a row naming a scope country is kept; a row naming NO
    recognized country at all is kept (we cannot tell, so we do not exclude).
    ONLY a row that names some OTHER country and none of the claim's is dropped —
    the Benin-coup-headline-vs-South-Africa-claim shape.
    """
    if not scope:
        return True
    low = text.casefold()
    if any(_names_country(c, low) for c in scope):
        return True
    return not any(
        _names_country(c, low) for c in _SLICE_GEO_COUNTRIES if c not in scope
    )


async def load_absence_slice_rows(conn: Any, run_id: Any) -> list[SliceRow] | None:
    """The retained INPUT SLICE for one run, or ``None`` if unreadable.

    ``None`` is the HONEST unavailable answer (no run_id, no trace row — pruned
    by the retention sweep — or a read error); ``[]`` is a real empty slice.
    Resolves both substrate conventions: a UNIT slice's rows are ``signals``
    (screened by TITLE), a composition's are ``analyst_outputs`` (screened by
    BODY — W1(b)). Bounded by :data:`_ABSENCE_SLICE_TITLE_CAP`. Never raises.
    """
    if conn is None or run_id is None:
        return None
    try:
        row = await conn.fetchrow(
            "SELECT input_row_refs FROM analyst_traces WHERE run_id = $1", run_id
        )
        if row is None:
            return None
        refs = list(row["input_row_refs"] or [])
        if not refs:
            return []
        rows = await conn.fetch(
            "SELECT COALESCE(payload->>'title', '') AS title, "
            "       '' AS body, "
            "       COALESCE(source_id, '') AS source_id, "
            "       COALESCE(raw_provenance->>'kind', '') AS provenance_kind, "
            "       'signal' AS row_kind "
            "  FROM signals WHERE id = ANY($1::uuid[]) "
            "UNION ALL "
            "SELECT COALESCE(title, '') AS title, "
            "       COALESCE(body, '') AS body, "
            "       '' AS source_id, "
            "       '' AS provenance_kind, "
            "       'output' AS row_kind "
            "  FROM analyst_outputs WHERE id = ANY($1::uuid[]) "
            "LIMIT $2",
            refs,
            _ABSENCE_SLICE_TITLE_CAP,
        )
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop, never break verify
        logger.warning("verify.absence_slice.read_failed run_id=%s err=%s", run_id, exc)
        return None
    out: list[SliceRow] = []
    for r in rows:
        title = _row_field(r, "title").strip()
        body = _row_field(r, "body").strip()
        kind = _row_field(r, "row_kind", "signal") or "signal"
        # W1(b): a COMPOSED row's title names the topic, never the verdict — screen
        # and show its BODY, falling back to the title only when the body is empty.
        text = (body[:_ABSENCE_SLICE_BODY_CHARS] if kind == "output" else "") or title
        if not text:
            continue
        out.append(
            SliceRow(
                text=text,
                kind=kind,
                source_id=_row_field(r, "source_id"),
                machine_structured=_is_machine_structured_row(
                    source_id=_row_field(r, "source_id"),
                    provenance_kind=_row_field(r, "provenance_kind"),
                    title=title,
                ),
            )
        )
    return out


async def load_absence_slice_titles(conn: Any, run_id: Any) -> list[str] | None:
    """The screen TEXTS of :func:`load_absence_slice_rows` (the pre-W1 shape).

    Retained as the flat view of the slice — ``None`` still means unreadable and
    ``[]`` a real empty slice.
    """
    rows = await load_absence_slice_rows(conn, run_id)
    return None if rows is None else [r.text for r in rows]


def absence_scope_qualifier(claim: str) -> str | None:
    """The SCALE / NOVELTY qualifier a scoped-absence claim carries, or ``None``.

    B1 — deterministic, and gated on the SAME ``_is_absence_claim`` grammar the
    floor exemption and the V3 absence route already share, so the three cannot
    drift apart. A claim that is not an absence claim, or carries no qualifier,
    returns ``None`` and keeps today's route.
    """
    stripped = claim.strip().lstrip("#-*> ").strip()
    low = re.sub(r"[*_`]+", "", stripped).strip().lower()
    if not _is_absence_claim(low):
        return None
    for qual in _ABSENCE_SCOPE_QUALIFIERS:
        if re.search(rf"(?<![\w-]){re.escape(qual)}(?![\w-])", low):
            return qual
    return None


# ---------------------------------------------------------------------------
# W1(e) — the ROUTE exclusions. The qualifier grammar above is a SHAPE test, and
# it fires on spans that are not slice-checkable negatives at all: a VOLUME read
# ("the volume is within the desk baseline … with no material change"), a
# CONTINUITY read whose negative belongs to the PRIOR read it quotes ("compared
# with the prior read that asserted no material change, the confirmed strike
# represents a material increase"), and a TRAJECTORY judgement ("the most
# plausible near-term trajectory is no leadership change"). None of those is
# refutable by a slice row, and each was measured producing a false hard fail.
#
# The continuity test is POSITIONAL on purpose: a genuine scoped negative that
# merely CORROBORATES against the prior read in its tail ("No reports of mass
# protests … appear in the current signal set, consistent with prior assessment
# [121]") is exactly the class V-B exists to verify and must stay on the route.
# Only a continuity frame that PRECEDES — and therefore governs — the negative
# takes the claim out.
# ---------------------------------------------------------------------------

#: A VOLUME / baseline-band metric read. Narrow + unmistakable idioms only.
_ABSENCE_ROUTE_VOLUME_RE = re.compile(
    r"\bsignal_volume\b"
    r"|\bwithin\s+(?:the\s+)?(?:desk\s+)?baseline\b"
    r"|\bwithin\s+(?:the\s+)?normal\s+band\b"
    r"|\bwithin\s+(?:the\s+)?expected\s+band\b"
    r"|\bvolume\s+is\s+(?:anomalously\s+|within\s+|at\s+|below\s+|above\s+)",
    re.IGNORECASE,
)

#: A PRIOR-READ comparison frame. Governs the claim only when it PRECEDES the
#: absence marker (see the block note).
_ABSENCE_ROUTE_CONTINUITY_RE = re.compile(
    r"\bcompared\s+(?:with|to)\s+the\s+prior\b"
    r"|\b(?:matches|aligns\s+with|mirrors)\s+the\s+prior\s+read\b"
    r"|\bthe\s+prior\s+read\b[^.]{0,80}?\b(?:concluded|asserted|noted|described|found)\b"
    r"|\bdoes\s+not\s+materially\s+alter\b"
    r"|\bunchanged\s+from\s+the\s+prior\s+read\b",
    re.IGNORECASE,
)

#: A TRAJECTORY / OUTLOOK judgement whose COMPLEMENT is the negative ("the most
#: plausible near-term trajectory is *no leadership change*"). Deliberately NOT
#: the bare word: a "**Near-term trajectory:** steady — with no new energy events
#: observed" line carries a real, slice-checkable negative and must stay on the
#: route. Measured on the live pass side: the bare-word form would have pulled
#: 9.6% of verified absences off a route that was deciding them correctly.
_ABSENCE_ROUTE_TRAJECTORY_RE = re.compile(
    r"\b(?:trajectory|outlook)\s+(?:is|remains|stays|will\s+be)\b"
    r"[^.]{0,40}?(?<![\w-])(?:no|none|nothing|neither|unchanged)(?![\w-])",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# V-G2 (2026-08-03) — the CONTINUITY CLAIM itself, not merely a continuity FRAME.
#
# W1(e)'s continuity test above is POSITIONAL, and correctly so for the shape it
# was written for. It is also why the class it was meant to remove survived: the
# 08-03 re-run measured 107 of 704 V-B-routed claims (15.2%) still matching
# "no material change" / "prior read" / "remains at the level" — the exact class
# the 08-02 readout pre-registered for router removal — and they produced NINE of
# the fifteen surviving absence hard fails.
#
# The type specimen, ``hard_fail#9``: ``world_assessor`` claims *"No material
# change since the prior world read of 2026-08-03T00:00:15Z [[ref:7]]"* and is
# hard-failed by an input-slice row that is ONE OF THE FINDING'S OWN AGREEING
# CITATIONS — [[ref:4]], the escalation composition naming the same three
# hotspots the finding names. The positional test cannot reach it: the absence
# marker opens the sentence, so the prior-read referent trails it.
#
# The deeper point is that the check is STRUCTURALLY INCAPABLE of adjudicating
# this claim shape, not merely imprecise on it. "No material change SINCE the
# prior read" is a DIFF between two assessments. A sibling desk's BLUF describing
# the CURRENT state — which is all the slice can ever offer — cannot establish or
# refute a change relative to a PREVIOUS one. Deciding it against a slice row is
# a category error, and no amount of filtering makes a category error precise.
# The honest move is the one the 08-02 readout named: a continuity claim needs a
# prior-read DIFF, and if that check does not exist it must be EXEMPTED from this
# route rather than failed by it. It then grades on citation support like any
# other claim — and where its referent IS the prior read, V-G1's
# ``judge_prior_read_conflict`` is the class that catches it.
#
# Deliberately NOT positional, and deliberately narrow: three fixed shapes plus
# the "no CHANGE ... prior READ" pairing in either order. A scoped negative that
# merely mentions a prior read in passing ("No reports of mass protests appear in
# the current signal set, consistent with prior assessment [121]") carries no
# negated CHANGE noun and stays on the route, which is where V-B earns its keep.
# ---------------------------------------------------------------------------

#: Nouns that name a DIFF against a baseline rather than an event in the window.
_CONTINUITY_CHANGE_NOUN = (
    r"(?:change[sd]?|shift(?:s|ed)?|movement|deviation|departure|revision|"
    r"update[sd]?|alteration)"
)
#: The referent a continuity claim diffs against.
_CONTINUITY_PRIOR_REFERENT = (
    r"(?:prior|previous|earlier|last)\s+(?:\w+\s+){0,2}?"
    r"(?:read|assessment|assessments|composition|cycle|window|report|register|"
    r"baseline)"
)

_ABSENCE_ROUTE_CONTINUITY_CLAIM_RE = re.compile(
    # (1) The fixed idiom. "No material change" is a verdict about a DIFF, full
    # stop — there is no slice row that reports the absence of a change.
    r"\bno\s+(?:\w+\s+){0,2}?material\s+change\b"
    # (2) A negated CHANGE noun anchored to a prior-read referent, either order.
    rf"|(?<![\w-])(?:no|without)(?![\w-])[^.]{{0,80}}?\b{_CONTINUITY_CHANGE_NOUN}\b"
    rf"[^.]{{0,80}}?\b{_CONTINUITY_PRIOR_REFERENT}\b"
    rf"|\b{_CONTINUITY_PRIOR_REFERENT}\b[^.]{{0,80}}?"
    rf"(?<![\w-])(?:no|without)(?![\w-])[^.]{{0,40}}?\b{_CONTINUITY_CHANGE_NOUN}\b"
    # (3) An explicit UNCHANGED-SINCE frame, wherever it sits in the claim.
    r"|\bunchanged\s+(?:since|from|relative\s+to|versus|vs\.?|compared)\b"
    # (4) "remains at the level …" — a level-HOLDING read against a baseline.
    r"|\bremains?\s+at\s+the\s+(?:same\s+|prior\s+|previous\s+)?level\b",
    re.IGNORECASE,
)

#: The absence sits in a LEADING PREMISE clause and some other proposition is the
#: claim's main assertion ("Given the absence of X, the severity is low"). Only
#: the genuinely premise-marking connectives: "while"/"although" lead a clause
#: whose negative IS an assertion ("While no disruption is present, …") and are
#: deliberately absent.
_ABSENCE_ROUTE_SUBORDINATE_RE = re.compile(
    r"^(?:given(?:\s+that)?|absent|in\s+the\s+absence\s+of|despite|notwithstanding)\b",
    re.IGNORECASE,
)


def _first_absence_marker_pos(low: str) -> int:
    """Character offset of the claim's first absence idiom, or ``-1``."""
    positions = [low.find(m) for m in _ABSENCE_MARKERS if m in low]
    for opener in ("no ", "none ", "not ", "nothing ", "neither "):
        if low.startswith(opener):
            positions.append(0)
    hit = re.search(r"(?<![\w-])(?:no|none|nothing|neither)\s", low)
    if hit is not None:
        positions.append(hit.start())
    return min(positions) if positions else -1


def _absence_route_exclusion(claim: str) -> str | None:
    """Why this scope-qualified span is NOT a slice-checkable negative, or ``None``.

    Deterministic + pure-lexical, like the rest of the V-B classifier. A returned
    reason means the claim keeps TODAY'S route (the V3 absence rubric / the
    generic judge) instead of being decided against the input slice.
    """
    stripped = claim.strip().lstrip("#-*> ").strip()
    core = re.sub(r"[*_`]+", "", stripped)
    # Drop a leading BLUF / Assessed / Judgment label so the subordinate test sees
    # the actual clause opener.
    core = re.sub(
        r"^(?:bluf|assessed|assessment|judgment|judgement|net)\s*[:\-—–]\s*",
        "",
        core,
        flags=re.IGNORECASE,
    ).strip()
    low = core.lower()
    if _ABSENCE_ROUTE_VOLUME_RE.search(low):
        return "volume"
    if _ABSENCE_ROUTE_TRAJECTORY_RE.search(low):
        return "trajectory"
    absence_pos = _first_absence_marker_pos(low)
    cont = _ABSENCE_ROUTE_CONTINUITY_RE.search(low)
    if cont is not None and (absence_pos < 0 or cont.start() < absence_pos):
        return "continuity"
    # V-G2: the claim IS a continuity diff, wherever the referent sits. Checked
    # AFTER the positional frame so the more specific older diagnosis keeps its
    # label when both fire (the outcome is the same either way — off the route);
    # the two stay distinguishable in the receipts as
    # ``absence_slice_route_excluded_continuity`` vs ``…_continuity_claim``.
    if _ABSENCE_ROUTE_CONTINUITY_CLAIM_RE.search(low):
        return "continuity_claim"
    if _ABSENCE_ROUTE_SUBORDINATE_RE.match(low) and "," in low:
        # A leading concessive/conditional frame whose MAIN clause follows the
        # comma — the negative is a premise, not the assertion under test.
        return "subordinate"
    return None


# ---------------------------------------------------------------------------
# W1(d) — CARVE-OUTS. "No escalation BEYOND the existing measures" is not
# violated by the existing measure; "no CONFIRMED changes GIVEN the below-floor
# signals" is not violated by the below-floor signal. Stage 2 never saw those
# clauses as clauses, so it read the exempted thing as the violation. They are
# extracted deterministically and handed to the prompt.
# ---------------------------------------------------------------------------

#: Connectives that introduce an EXEMPTION from the claim's own negative.
_CARVE_OUT_CONNECTIVES: tuple[str, ...] = (
    "beyond", "other than", "apart from", "aside from", "except for", "except",
    "excluding", "besides", "outside of", "outside", "save for",
    "with the exception of", "short of", "given the", "given that", "given",
)
#: How much of the tail after a connective is carried (one clause, bounded).
_CARVE_OUT_CLAUSE_CHARS = 160


def _absence_carve_outs(claim: str) -> list[str]:
    """The EXEMPTION clauses a scoped negative carries (W1(d)), longest first.

    Each is the connective plus the clause it introduces, cut at the next clause
    boundary and bounded. Deterministic; returns ``[]`` for a claim with none, so
    the stage-2 prompt is byte-identical for every claim that carves nothing out.
    """
    core = re.sub(r"[*`]+", "", claim.strip().lstrip("#-*> ").strip())
    core = _CITATION_MARKER_STRIP_RE.sub(" ", core)
    # ``_`` is emphasis in prose but a word character in an identifier the prose
    # names ("the military_posture unit") — space it rather than deleting it.
    core = re.sub(r"\s+", " ", core.replace("_", " ")).strip()
    low = core.lower()
    out: list[str] = []
    seen: set[int] = set()
    for conn_word in _CARVE_OUT_CONNECTIVES:
        for m in re.finditer(rf"(?<![\w-]){re.escape(conn_word)}(?![\w-])", low):
            start = m.start()
            if any(abs(start - s) < 3 for s in seen):
                continue
            tail = core[start : start + _CARVE_OUT_CLAUSE_CHARS]
            # Cut at the first CLAUSE boundary so the carve-out is the clause, not
            # the rest of the sentence. "and" is deliberately NOT a boundary: it
            # joins the compound noun phrase INSIDE a carve-out far more often
            # than it starts a new clause ("beyond the existing diplomatic AND
            # border measures").
            cut = re.search(r"[;.]|\s+(?:but|while|whereas)\s+", tail)
            clause = (tail[: cut.start()] if cut else tail).strip(" ,;.—–-")
            if len(clause.split()) < 2:
                continue
            seen.add(start)
            out.append(clause)
    return sorted(out, key=len, reverse=True)[:4]


# ---------------------------------------------------------------------------
# V-G3 (2026-08-03) — SCALE QUALIFIERS. W1(d) fixed carve-outs (the clause a
# claim EXEMPTS); it did not generalise to the claim's own SCALE word, and the
# 08-03 panel found the blindness intact on both paths.
#
# ``hard_fail#3`` (and its twin on a second Canada desk): *"No evidence of MASS
# protests…"* violated by *"DOZENS protest Meta plan for MASSIVE data centre in
# Morinville."* Dozens is the NEGATION of mass — the row is the claim's own
# evidence, not its refutation. And the lexical trap is visible in the string:
# "massive" sits two words from "protest", modifying the data centre.
#
# The rubric already told the judge to grade "at the very SCALE the claim says
# did not happen" and the judge ignored it, so this is DETERMINISTIC rather than
# another sentence of prompt. Deliberately narrow — three conditions, all
# required, and a miss leaves the hard fail:
#
#   1. the claim's scope qualifier is a SCALE word (mass / large-scale / major /
#      widespread / …), not a NOVELTY or EPISTEMIC one ("new", "confirmed" —
#      those are the carve-out and W1(d) families and are unaffected);
#   2. the named violating row carries a SMALL-QUANTITY marker;
#   3. that marker sits within three tokens of one of the CLAIM'S OWN content
#      terms — "DOZENS protest" binds, "MASSIVE data centre" does not, and the
#      adjacency window is exactly what separates the signal from the decoy.
# ---------------------------------------------------------------------------

#: The SCALE arm of :data:`_ABSENCE_SCOPE_QUALIFIERS` — qualifiers a smaller
#: quantity can UNDERSHOOT. Novelty ("new", "renewed") and epistemic
#: ("confirmed", "official") qualifiers are deliberately absent: a single new
#: sanction still violates "no new sanctions".
_SCALE_QUALIFIERS: frozenset[str] = frozenset({
    "large-scale", "large scale", "largescale", "mass", "major",
    "significant", "substantial", "sweeping", "widespread", "systematic",
    "systemic", "serious", "meaningful", "material",
})

#: Quantity language that is SMALLER than any of the scale qualifiers above.
_SMALL_QUANTITY_RE = re.compile(
    r"(?<![\w-])(?:dozens?|a\s+dozen|handful|a\s+few|several|some|isolated|"
    r"scattered|sporadic|a\s+small\s+number|small\s+numbers?|one|two|three|four|"
    r"five|six|seven|eight|nine|ten|\d{1,3})(?![\w-])",
    re.IGNORECASE,
)

#: How many tokens after the quantity marker still count as ITS subject.
_SMALL_QUANTITY_WINDOW = 3


def _scale_undershoots_claim(
    qualifier: str | None, row_text: str, content_terms: set[str]
) -> bool:
    """Does this row report the claim's subject at a SMALLER scale than it asserts?

    ``qualifier`` is :func:`absence_scope_qualifier`'s answer for the claim,
    ``content_terms`` :func:`_absence_content_terms`'s. All three V-G3 conditions
    must hold; anything else returns False and the caller keeps its verdict.
    """
    if not qualifier or qualifier.lower() not in _SCALE_QUALIFIERS:
        return False
    if not content_terms:
        return False
    low = row_text.lower()
    for m in _SMALL_QUANTITY_RE.finditer(low):
        window = low[m.end() : m.end() + 64].split()[:_SMALL_QUANTITY_WINDOW]
        for token in window:
            stem = re.sub(r"[^a-z-]", "", token)
            if not stem:
                continue
            if stem in content_terms:
                return True
            # The screen stems a trailing plural, so match the row the same way.
            if len(stem) > 4 and stem.endswith("s") and stem[:-1] in content_terms:
                return True
    return False


def _absence_content_terms(claim: str, *, target_id: str | None) -> set[str]:
    """The topical terms a slice-title screen should look for (stage 1).

    Everything that carries no discriminating signal is dropped: function words,
    the absence / scope vocabulary, the collection-scope lexicon, and the DESK'S
    OWN country tokens (every title in a country slice names the country, so they
    collide with everything). Terms are singular-stemmed so "sanctions" screens
    a "Sanctioned …" headline — the exact live collision class.
    """
    stripped = claim.strip().lstrip("#-*> ").strip()
    low = re.sub(r"[*_`]+", " ", stripped).lower()
    low = _CITATION_MARKER_STRIP_RE.sub(" ", low)
    desk_tokens: set[str] = set()
    slug = _country_desk_slug(target_id)
    if slug:
        for name in _TARGET_SLUG_TO_COUNTRY.get(slug, ()):  # type: ignore[arg-type]
            desk_tokens.update(re.findall(r"[a-z]{3,}", name.lower()))
    drop = (
        _ABSENCE_SCREEN_STOPWORDS
        | set(_ABSENCE_SCOPE_QUALIFIERS)
        | desk_tokens
        | {m.strip() for m in _COLLECTION_SCOPE_MARKERS}
    )
    terms: set[str] = set()
    for token in re.findall(r"[a-z][a-z\-]{3,}", low):
        token = token.strip("-")
        if len(token) < 4 or token in drop:
            continue
        stem = token[:-1] if len(token) > 4 and token.endswith("s") else token
        if stem in drop:
            continue
        terms.add(stem)
    return terms


def _absence_slice_candidates(
    terms: set[str], titles: list[str]
) -> tuple[list[str], bool]:
    """``(candidate titles, discriminated?)`` — the stage-1 screen.

    A term present in MOST of the slice discriminates nothing: on a country desk
    every title names the country, so "haiti" collides with everything while
    carrying no signal. Such UBIQUITOUS terms are dropped from the screen
    (data-driven, so it works for every desk, not only the ones any gazetteer
    happens to list).

    ``discriminated`` is False when the filter left NOTHING to screen with — the
    claim's whole vocabulary saturates the slice. That is NOT a clean screen, so
    the caller must never read it as a verified absence; every title matching any
    ORIGINAL term becomes a candidate and the decision goes to stage 2.
    """
    if not terms or not titles:
        return [], bool(terms)
    lowered = [t.lower() for t in titles]
    n = len(lowered)

    def _df(term: str) -> int:
        return sum(1 for t in lowered if term in t)

    discriminating = (
        {t for t in terms if _df(t) < _ABSENCE_SLICE_UBIQUITY * n}
        if n >= 2
        else set(terms)
    )
    screening = discriminating or terms
    out: list[str] = []
    for title, low in zip(titles, lowered):
        if any(t in low for t in screening):
            out.append(title)
            if len(out) >= _ABSENCE_SLICE_CANDIDATE_CAP:
                break
    return out, bool(discriminating)


_ABSENCE_SLICE_JUDGE_SYSTEM = (
    "You are checking SCOPED NEGATIVE claims against the analyst's ACTUAL INPUT "
    "SLICE. Each claim asserts that something of a particular KIND or SCALE (new, "
    "large-scale, mass, tightened, ...) did NOT occur. You are given the SLICE "
    "ROWS that share vocabulary with the claim — a row is either a source "
    "headline or, for a composed desk read, an excerpt of that row's own text. "
    "For each claim decide EXACTLY ONE verdict:\n"
    "- supported: NO listed row reports the thing the claim says is absent. "
    "Sharing a topic or a word is NOT a violation — a row about sanctions does "
    "not violate 'no NEW sanctions', and background, analysis, opinion or "
    "historical coverage never violates a claim about the current window.\n"
    "- contradicted: a listed row plainly REPORTS the very thing, at the very "
    "scale, the claim says did not happen.\n"
    "- unsupported: the rows are too thin to tell either way.\n"
    "Two rules that override everything above:\n"
    "(1) CARVE-OUTS — when a claim EXEMPTS something ('beyond the existing "
    "measures', 'other than X', 'except for Y', 'given the below-floor "
    "signals'), a row reporting the EXEMPTED thing does NOT violate it. The "
    "claim already accounts for it. Any carve-outs are listed under the claim.\n"
    "(2) EPISTEMIC QUALIFIERS — a claim about what is CONFIRMED / CREDIBLE / "
    "OFFICIAL is not violated by a row that is unconfirmed, low-confidence, "
    "below the verification floor, a proposal, a bill under consideration, a "
    "threat, or a plan. Only a row reporting the thing as HAVING HAPPENED "
    "violates such a claim.\n"
    "Be conservative: when in doubt answer supported. A row is only a violation "
    "if a reader of that row alone would say the claim is false.\n"
    'Output strict JSON only: {"verdicts": ["supported"|"contradicted"|'
    '"unsupported", ...]} with one verdict per claim, in order. Alongside '
    '"verdicts", return "quotes": a list of the SAME length, one entry per claim; '
    "for a \"contradicted\" verdict the entry MUST be a VERBATIM run copied from "
    'the violating row, and for every other verdict "". Output only the JSON '
    "object."
)


# ---------------------------------------------------------------------------
# V-H4 (2026-08-04) — THE ENUMERATED DENIAL. A quote that names none of the
# listed things in full is evidencing something the claim never denied.
#
# The 08-03 panel's `hard_fail#8` is the last unearned hard fail on the judge
# path and the one W2's refutes-vs-resolves rule cannot see. economic_coercion /
# Argentina claims:
#
#   "There are no reports of FX-reserve depletion, currency crises, SWIFT bans,
#    or sovereign default pressures affecting Argentina; the economic commentary
#    focuses on domestic growth challenges and reforms rather than external
#    financial coercion"
#
# and the judge hard-fails it with a real, verbatim, SIGNAL-BACKED span:
#
#   "The rest of Argentina continues to struggle with sluggish growth, high
#    inflation, weak consumer spending, and business and mortgage defaults"
#
# Business and mortgage defaults are not sovereign default pressure; inflation is
# not a currency crisis. The quote CONFIRMS the claim's own second clause. W2's
# R1 catches only a verbatim RESTATEMENT of the claim and R2 only a prior-read
# span; an affirming PARAPHRASE satisfies both, which is exactly what the panel
# and the counter audit (flag 5) each recorded as owed.
#
# WHY THIS SHAPE AND NOT AN ENTAILMENT MODEL. Replayed read-only over every
# ``judge_contradicted`` hard fail on the 07-31/1 and 08-02/1 stamps (24 carry a
# persisted quote), three lexical formulations were measured:
#
#   * whole-claim RESTATEMENT (the quote's terms nearly a subset of the claim's)
#     — fired 4/24 and swallowed a GENUINE catch ("the current evidence shows no
#     such spikes", refuted by "signals indicate rising energy prices");
#   * DISCLOSED-CLAUSE (the quote lands on a positive clause the claim itself
#     asserts) — fired 1/24 and MISSED the Argentina row, whose quote overlaps
#     the DENIED clause more than the asserted one;
#   * ENUMERATED DENIAL, below — fired 1/24, exactly the adjudicated row, with no
#     false demotion on either stamp.
#
# A small NLI model would generalise further, and the same replay says it is not
# worth what it costs: a new model artifact on the DETERMINISTIC severity path
# (deterministic precisely because "a rubric line is a request and this needs to
# be a guarantee"), for 1 row in 24 — because V-G1 already absorbs the affirming
# class wholesale. Three of the four affirming quotes on the stamped day were
# ANALYST PROSE and are demoted before this rule ever runs.
#
# THE RULE, and why each condition is there:
#
#   1. the claim must ENUMERATE what it denies ("A, B, C, or D"). A claim with a
#      single unenumerated denial has under-specified its own scope and this
#      declines to decide — which is what keeps "no discernible shift", refuted
#      by a reported shift, HARD.
#   2. the quote must share at least one term with some enumerated item. Sharing
#      nothing means the refutation is SEMANTIC, in words the claim never used —
#      what a genuine catch usually looks like. Leave it alone.
#   3. NO enumerated item may be covered in FULL. One squarely-named denied thing
#      is a real refutation and returns immediately.
#
# Conservative in the documented direction throughout: every miss leaves the hard
# fail standing.
# ---------------------------------------------------------------------------

#: Unicode hyphens the producers emit inside compound terms ("energy-security").
#: The screen's token regex is ASCII, so an unfolded U+2011 silently splits a
#: compound into two terms and manufactures overlap that is not there.
_UNICODE_HYPHENS = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-"})

#: Where the denied span ENDS. Past any of these the sentence has stopped listing.
_DENIAL_SPAN_END_RE = re.compile(
    r"[;:.]|\s+(?:but|while|whereas|however)\s+", re.IGNORECASE
)

#: The conjunction introducing an enumeration's LAST item. Its presence is what
#: makes the span an enumeration at all (condition 1).
_DENIAL_LAST_ITEM_RE = re.compile(r",?\s+\b(?:or|nor)\b\s+", re.IGNORECASE)

#: "no <head> in A, B, or C" — the enumerated things are A/B/C and ``head`` is
#: shared by each, so a quote naming ONE of them squarely is on scope. Applied
#: only to the first item, and only when an enumeration follows it.
_DENIAL_GOVERNING_HEAD_RE = re.compile(
    r"^(?P<head>.*?)\s+\b(?:in|to|of|across|regarding)\b\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

#: Words that qualify the OBSERVER, not the thing observed. "No DISCERNIBLE
#: shift" denies a shift, and a reported shift refutes it. Left in an item's term
#: set these would make every such claim un-refutable by construction.
_DENIAL_EPISTEMIC_HEDGES: frozenset[str] = frozenset({
    "discernible", "discernable", "observable", "detectable", "perceptible",
    "noticeable", "apparent", "evident", "credible", "verified", "verifiable",
    "identifiable",
})


def denied_enumeration(claim: str) -> list[set[str]]:
    """The content terms of each thing an ENUMERATED denial lists, or ``[]``.

    ``[]`` for a claim that denies nothing, and for one that denies a SINGLE
    unenumerated thing — the under-specified case this branch declines to decide.
    """
    core = re.sub(r"[*`_]+", " ", claim.strip().lstrip("#-*> ").strip())
    core = core.translate(_UNICODE_HYPHENS)
    start = _first_absence_marker_pos(core.lower())
    if start < 0:
        return []
    tail = core[start:]
    end = _DENIAL_SPAN_END_RE.search(tail)
    span = tail[: end.start()] if end else tail
    last = _DENIAL_LAST_ITEM_RE.search(span)
    if last is None:
        return []
    items = span[: last.start()].split(",") + [span[last.end():]]
    head = _DENIAL_GOVERNING_HEAD_RE.match(items[0].strip())
    if head is not None:
        items[0] = head.group("rest")
    out: list[set[str]] = []
    for item in items:
        terms = {
            t
            for t in _absence_content_terms(item, target_id=None)
            if t not in _DENIAL_EPISTEMIC_HEDGES
        }
        if terms:
            out.append(terms)
    return out


# ---------------------------------------------------------------------------
# V-H5 (2026-08-04) — A NEGATIVE CANNOT BE REFUTED BY ANOTHER NEGATIVE.
#
# V-G2 pulled the CONTINUITY class off the V-B route and measured 6 of the
# panel's 15 surviving `absence_slice_contradicted` hard fails leaving with it.
# It named the remainder honestly and left them: "restatement-as-violator and
# composition-restates-a-unit". Three of those are what the counter audit had
# already flagged as false-positive SHAPES the shipped W1 filters do not cover.
#
# Only ONE of the three has a mechanical form clean enough to ship, and this is
# it. `country_composition` / Sudan claims "Analysis finds no coordinated
# narrative across the collected signals" and is hard-failed by an input-slice
# row that OPENS: "**BLUF**: No coordinated narrative is evident in the collected
# Sudan signals; coverage appears organic and driven by disparate events." The
# "violator" says what the claim says. It is the claim's CORROBORATION, filed as
# its refutation.
#
# The rule states itself: a scoped negative is not violated by a row whose own
# leading assertion is a NEGATIVE about the same subject. Both halves are
# required — the row's lead must be an absence claim by the SAME grammar the
# route uses, and the thing IT denies must share a topical term with the claim.
# A row that opens with an unrelated negative and then reports the denied thing
# ("No new sanctions were imposed, but protesters clashed with police") shares no
# term between the two NEGATIVES and stays a violation, which is the case that
# would otherwise be expensive.
#
# The other two shapes are NOT fixed here and are recorded as owed — see
# tests/data_pkg/test_verify_denied_scope.py::THE RESTATEMENT RESIDUALS.
# ---------------------------------------------------------------------------

#: A leading BLUF / Assessed label the composition and unit desks both emit. The
#: row's real assertion starts after it.
_ROW_LEAD_LABEL_RE = re.compile(
    r"^\s*[*_`#\s-]*(?:bluf|assessed|assessment|judgment|judgement|net|"
    r"key\s+signals)\b[*_`\s]*[:\-—–]\s*",
    re.IGNORECASE,
)

#: How much of a violating row counts as its LEADING assertion. A composition
#: BLUF is one sentence; reading further picks up the Key-points bullets, which
#: are a different assertion and would make this test mean nothing.
_ROW_LEAD_CHARS = 240


def row_restates_the_negative(row_text: Any, claim_terms: set[str]) -> bool:
    """V-H5 — does the named violating row ASSERT the claim's own negative?

    ``claim_terms`` is :func:`_absence_content_terms`'s answer for the claim.
    ``True`` only when the row's LEADING assertion is itself a negative and the
    thing it denies shares a topical term with the claim. Never raises.
    """
    if not isinstance(row_text, str) or not row_text.strip():
        return False
    lead = re.split(r"[\n;]", row_text.strip(), maxsplit=1)[0][:_ROW_LEAD_CHARS]
    lead = _ROW_LEAD_LABEL_RE.sub("", re.sub(r"[*`_]+", " ", lead)).strip()
    low = lead.lower()
    if not _is_absence_claim(low):
        return False
    start = _first_absence_marker_pos(low)
    if start < 0:
        return False
    denied = _absence_content_terms(lead[start:], target_id=None)
    return bool(denied & claim_terms)


def quote_misses_the_denied_scope(quote: Any, claim: str) -> bool:
    """V-H4 — does this refuting quote name NONE of the enumerated denied things?

    ``True`` only when the claim enumerates what it denies, the quote touches at
    least one of those items, and not one of them is named in full. Never raises.
    """
    if not isinstance(quote, str) or not quote.strip():
        return False
    items = denied_enumeration(claim)
    if not items:
        return False
    quote_terms = _absence_content_terms(
        quote.translate(_UNICODE_HYPHENS), target_id=None
    )
    if not quote_terms:
        return False
    partial = False
    for item in items:
        if not (quote_terms & item):
            continue
        if item <= quote_terms:
            return False  # a denied thing is named squarely — a real refutation
        if len(item) >= 2:
            partial = True
    return partial
