# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bake-off NORMALISER — design §5.2 rules N1–N9, deterministic, no model.

This is the single file the design calls ``bakeoff_normalise.py`` (§5.2: "The
implementation is a single file … written at G0. Its sha256 goes into the run
record."). The run ledger (``common.open_run``) stamps this file's sha256;
the acceptance test is byte equality against frozen fixture outputs
(``tests/data_pkg/fixtures/bakeoff/``, §5.4). It imports nothing outside the
standard library, so its hash is the whole of its behaviour.

Every arm's record goes through :func:`normalise` identically. The rules, in
the design's order:

  N1  Unicode NFKC, plus the full-width lenticular brackets 【】 the core plane
      emits (which NFKC itself leaves alone) folded to [].
  N2  Every citation form — ``(signal:<uuid>)``, ``(finding:<uuid>)``,
      ``[[ref:N]]``, ``[N]``, ``[^N]``, ``[<uuid>]``, bare uuids, URLs — becomes ``[R1]…[Rk]``
      in order of first appearance. The receipt list carries, per R, SOURCE
      documents only (a finding was expanded to its signals by the harness):
      masthead, date with its word (published / fetched), title, first 3,000
      characters. Receipts the record names but the text never cites follow,
      numbered on. An ordinal the record cannot resolve stays an R with no
      document (unresolvable — axis 4 counts it).
  N3  Machine tells → the frozen neutral phrases of §5.3 (v1.2: a typed
      absence or an insufficient-evidence band reads "Not established: …",
      the same form the consult arms' unanswered aspects take).
  N3b (v1.2) The answerer's voice and apparatus → one neutral register
      (§5.3b): contractions expanded; words naming the answerer's own
      material ("the verified record", "the change record", "the corpus",
      "the documents I read", "the X desk", "receipts", indicator-toggle
      jargon) → "the reporting reviewed" and plain equivalents; first-person
      search verbs ("I found", "I couldn't confirm") and second-person
      address ("if you meant") → impersonal forms; desk and unit ids and
      other snake_case labels → plain words or removed; machine metadata
      ("Verification: faithfulness=…", "event id:") removed. Facts, numbers,
      dates, citations and hedges are untouched.
  N4  Process narration: a sentence matching the process lexicon with no
      date, number or proper noun outside it is deleted; otherwise only the
      matched phrase is removed.
  N5  Structure flattened: headings, emphasis, bullets, numbered lists, quotes
      and tables become plain sentences (cells joined by "; "); a title line
      (a leading ``# ``) and the as-of header line are dropped. (v1.2) A
      heading or bold lead-in that is a SECTION LABEL (§5.3b table) is mapped
      to one of five shared labels or dropped; stray emphasis markers go.
  N6  ≤ 300 words kept whole; otherwise sentences scored by BM25 against the
      question + the desk's display name, the highest kept in original order
      until 300 words (a sentence that would overflow is skipped and the fill
      continues). Who was cut is recorded, never shown to a grader.
  N7  The last line is ``Stated confidence: 0.xx`` or ``Stated confidence: none``;
      any ``Confidence: …`` line the arm wrote is removed from the text.
  N8  Explicit, unambiguous dates → ``YYYY-MM-DD``; a date with no year is left.
  N9  Hedges and content verbs are content: nothing here removes them.

Usage::

    python3 scripts/bakeoff/normalise.py <record.json> [--out normalised.json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

SCHEMA = "legba.bakeoff.normalised/1"
WORD_LIMIT = 300
BM25_K1 = 1.2
BM25_B = 0.75

# ---------------------------------------------------------------------------
# N1
# ---------------------------------------------------------------------------


def n1_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return text.replace("【", "[").replace("】", "]").replace("‑", "-")


# ---------------------------------------------------------------------------
# N2
# ---------------------------------------------------------------------------

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CITE = re.compile(
    r"\((?:signal|finding):(?P<typed>" + _UUID + r")\)"
    r"|\[(?P<buuid>" + _UUID + r")\]"
    r"|\[\[ref:(?P<refn>\d+)\]\]"
    r"|\[\^(?P<foot>\d+)\]"
    r"|\[(?P<ord>\d+)\]"
    r"|(?P<url>https?://[^\s<>\]\)\"']+[^\s<>\]\)\"'.,;:!?])"
    r"|(?P<uuid>\b" + _UUID + r"\b)"
)


def _receipt_docs(record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in record.get("receipts") or []:
        docs = []
        for d in r.get("documents") or []:
            if d.get("resolved") is False:
                continue
            docs.append({
                "masthead": n1_unicode(str(d.get("masthead") or "")),
                "date": f"{d.get('date_word') or 'published'} {d.get('date') or 'undated'}",
                "title": n1_unicode(str(d.get("title") or "")),
                "url": d.get("url"),
                "body": n1_unicode(str(d.get("body") or ""))[:3000],
            })
        out[str(r.get("ref"))] = docs
    return out


def n2_citations(text: str, record: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    cmap = {str(k): str(v) for k, v in (record.get("citation_map") or {}).items()}
    docs = _receipt_docs(record)
    order: list[str] = []

    def ref_for(m: re.Match[str]) -> str:
        if m.group("typed") or m.group("buuid"):
            return m.group("typed") or m.group("buuid")
        if m.group("uuid"):
            return m.group("uuid")
        if m.group("url"):
            return m.group("url")
        marker = m.group(0)
        return cmap.get(marker, f"unresolved:{marker}")

    def sub(m: re.Match[str]) -> str:
        ref = ref_for(m)
        if ref not in order:
            order.append(ref)
        return f"[R{order.index(ref) + 1}]"

    text = _CITE.sub(sub, text)
    for r in record.get("receipts") or []:
        ref = str(r.get("ref"))
        if ref not in order:
            order.append(ref)
    receipts = [
        {"r": f"R{i + 1}", "documents": docs.get(ref, [])}
        for i, ref in enumerate(order)
    ]
    return text, receipts


# ---------------------------------------------------------------------------
# N3 — the frozen mapping table (§5.3)
# ---------------------------------------------------------------------------

ABSENCE_KINDS = (
    "not_collected", "collected_but_silent", "source_silent", "source_stale",
    "no_read", "searched_found_nothing", "search_failed", "below_floor",
    "layer_declared_absent", "history_gap", "carried_unsupported",
)
_ABSENCE_LINE = re.compile(r"^[ \t]*(" + "|".join(ABSENCE_KINDS) + r"):[ \t]*(.*?)[ \t]*$", re.M)
_ABSENCE_TOKEN = re.compile(r"\b(" + "|".join(ABSENCE_KINDS) + r")\b")
_BAND_LINE = re.compile(r"^[ \t]*([a-z0-9_]+):[ \t]*insufficient-evidence\b.*$", re.M)
_THIN = re.compile(r"Thin evidence:[^\n]*?(?:[.!?](?=\s|$)|$)")


def _words(label: str) -> str:
    return label.replace("_", " ").strip()


def n3_tells(text: str, record: dict[str, Any]) -> str:
    def absence(m: re.Match[str]) -> str:
        kind, label = m.group(1), _words(m.group(2)).rstrip(".")
        out = f"Not established: {label}." if label else "Not established."
        if kind == "search_failed":
            out += " Searches for it failed."
        return out

    text = _ABSENCE_LINE.sub(absence, text)
    text = _BAND_LINE.sub(
        lambda m: f"Not established: {_words(m.group(1))} (too little evidence to rate).", text,
    )
    text = text.replace("[single-source]", "(single source)")
    text = re.sub(
        r"\b(\d+) wire-folded\b",
        lambda m: f"(the same wire report carried by {m.group(1)} outlets)", text,
    )
    text = _THIN.sub("The evidence gathered for this answer is thin.", text)
    text = text.replace("not observed in collected reporting", "not seen in the reporting reviewed")
    text = _ABSENCE_TOKEN.sub("not established", text)
    for item in record.get("unanswered_aspects") or []:
        item = n1_unicode(str(item)).strip().rstrip(".")
        if item:
            text += f"\n\nNot established: {item}."
    return text


# ---------------------------------------------------------------------------
# N3b — the answerer's voice and apparatus (v1.2, §5.3b)
# ---------------------------------------------------------------------------

_AP = r"['’]"
_DESK_IDS = r"(?:country_g20|country_watch|lane|flow|region|situation)_[a-z0-9_]+(?:/[a-z0-9_]+)?"
_RP = "the reporting reviewed"

#: Ordered (pattern, replacement, flags). Applied identically to every arm.
#: A replacement starting a sentence is capitalised; elsewhere it is used as
#: written. Nothing here touches a number, a date, a citation or a hedge.
VOICE_RULES: tuple[tuple[str, str, int], ...] = (
    # contractions → full forms
    (rf"\bcan{_AP}t\b", "cannot", re.I),
    (rf"\bwon{_AP}t\b", "will not", re.I),
    (rf"\b(\w+)n{_AP}t\b", r"\1 not", re.I),
    (rf"\bI{_AP}m\b", "I am", 0),
    (rf"\bI{_AP}ve\b", "I have", 0),
    (rf"\bI{_AP}d\b", "I would", 0),
    (rf"\b(it|that|there|what|here|who){_AP}s\b", r"\1 is", re.I),
    (rf"\b(they|we|you){_AP}re\b", r"\1 are", re.I),
    (rf"\b(they|we|you|I){_AP}ve\b", r"\1 have", re.I),
    (rf"\b(\w+){_AP}ll\b", r"\1 will", re.I),
    # desk / unit ids and other snake_case labels
    (rf"\s*\(\s*{_DESK_IDS}\s*\)", "", 0),
    (rf"\b{_DESK_IDS}\b", "", 0),
    (r"\b([A-Za-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", "\x00SNAKE", 0),
    # machine metadata
    (r"Verification:\s*faithfulness\s*=\s*[0-9.]+\.?", "", re.I),
    (r"\(\s*(?:event|situation|finding|signal)\s+id:\s*(\[R\d+\])\s*\)", r"\1", re.I),
    (r"\b(?:event|situation|finding|signal)\s+id:\s*", "", re.I),
    # the answerer's own material
    (r"\b(?:the|this|that|our|my)\s+verified\s+(?:record|substrate|corpus|material)\b", _RP, re.I),
    (r"\b(?:the|this|my)\s+(?:change\s+record|change\s+list|window\s+ledger|indicator\s+roster|roster|substrate|corpus|packet)\b", _RP, re.I),
    (r"\b(?:my|the)\s+records\b", _RP, re.I),
    (r"\bthe\s+(?:documents|sources|reports)\s+I\s+(?:could\s+)?(?:read|found|checked|opened|reviewed|find)\b", _RP, re.I),
    (r"\b(?:the|these|those)\s+(?:documents|sources)\b", _RP, re.I),
    (r"\bverified\s+findings\b", "reports", re.I),
    (r"\bverified\s+finding\b", "report", re.I),
    (r"\b(?:desk|unit)\s+findings\b", "reports", re.I),
    (r"\bfindings\b", "reports", re.I),
    (r"\b(the|a|any|no|one|each|neither|either|every|this|that|latest|same|its)\s+((?:[\w-]+\s+)?)finding\b", r"\1 \2report", re.I),
    (rf"\b(?:the|this|that|its|each|same|one)\s+(?:[\w-]+\s+){{0,3}}?desk{_AP}s\b", "the reporting's", re.I),
    (r"\b(?:the|this|that|its|each|same|one)\s+(?:[\w-]+\s+){0,3}?desks?\b", "the reporting", re.I),
    (r"\bdesks?\b", "reporting", re.I),
    (r"\b(?:the\s+reporting's\s+)?verified\s+record\b", _RP, re.I),
    (r"\b(?:the\s+reporting's|this)\s+collection\b", _RP, re.I),
    (r"\b(?:the|these|those)\s+receipts\b", _RP, re.I),
    (r"\breceipts\b", "sources", re.I),
    (r"\breceipt\b", "source", re.I),
    (r"\((\d+)\s+raw\s+toggles?\)", r"(reported inconsistently \1 times)", re.I),
    (r"\braw\s+toggles?\b", "inconsistent reports", re.I),
    (r"\btoggled\b", "unsettled", re.I),
    (r"\btoggl(?:es|ing)\b", "back-and-forth changes", re.I),
    (r"\bflickered\b", "changed back and forth", re.I),
    (r"\bdebounced\s+", "", re.I),
    (r"\bindicator\s+flips\b", "changes", re.I),
    (r"\bindicator\s+flip\b", "change", re.I),
    (r"\bflipped\b", "changed", re.I),
    (r"\bflips\b", "changes", re.I),
    (r"\bflip\b", "change", re.I),
    (r"\bunstable\b", "unsettled", re.I),
    (r"\bdoes\s+not\s+(?:directly\s+)?answer\s+(?:this|the)(?:\s+question)?\b", "does not establish this", re.I),
    (r"\bno\s+clean\s+change\b", "no clear change", re.I),
    (r"\bNOT CHANGES\b", "not changes", 0),
    (r"\broster\s+(item|entry)\b", r"\1", re.I),
    (r"\s*\bin\s+the\s+roster\b", "", re.I),
    (r"\broster\b", "watch list", re.I),
    (r"\bthis\s+(?:window|cycle)\b", "the window", re.I),
    (r"\bthis\s+(?:verdict|read|assessment)\b", "the assessment", re.I),
    # second-person address
    (r"\btell\s+(?:you|us)\b", "say", re.I),
    (r"\bgive\s+you\b", "give", re.I),
    (r"\bif\s+you\s+meant\b", "if the question meant", re.I),
    (r"\byou\s+(?:meant|mean)\b", "the question means", re.I),
    (r"\bwhether\s+you\s+call\b", "whether one calls", re.I),
    (r"\byou\s+asked\s+(?:about|for)\b", "the question asks about", re.I),
    (r"\byou\s+should\s+know\s+about\b", "worth noting", re.I),
    (r"\byour\s+question\b", "the question", re.I),
    # first-person search and judgement verbs
    (r"\bI\s+(?:could\s+not|cannot|did\s+not|was\s+unable\s+to|can\s+not)\s+(?:find|locate|see|identify)\b",
     f"{_RP} does not show", 0),
    (r"\bI\s+(?:could\s+not|cannot|did\s+not|was\s+unable\s+to|can\s+not)\s+(?:confirm|verify|corroborate)\b",
     f"{_RP} does not confirm", 0),
    (r"\bI\s+(?:could\s+not|cannot|did\s+not|was\s+unable\s+to|can\s+not)\s+(?:establish|determine)\b",
     f"{_RP} does not establish", 0),
    (r"\bI\s+(?:could\s+not|cannot|can\s+not)\s+(?:say|tell)\b", f"{_RP} does not say", 0),
    (r"\bI\s+(?:could\s+not|cannot|can\s+not)\s+give\b", f"{_RP} does not give", 0),
    (r"\bI\s+(?:found|saw|see|identified|located)\b", f"{_RP} shows", 0),
    (r"\bI\s+(?:checked|read|searched|reviewed|looked\s+at|opened)\b", f"{_RP} covered", 0),
    (r"\bI\s+(?:judge|assess|think|believe|estimate)(?:\s+that)?\b", "the assessment is that", 0),
    (r"\bI\s+would\s+need\b", "an answer would need", 0),
    (r"\bI\s+(?:also\s+)?need\b", "an answer needs", 0),
    (r"\bI\s+(?:could\s+not|cannot|can\s+not|was\s+unable\s+to|did\s+not)\s+(open|load|access|check|read)\b",
     r"it was not possible to \1", 0),
    (r"\bI\s+(assumed|took|treated|used|picked|covered|focused|made|would|could|cannot|can|will|did)\b",
     r"the answer \1", 0),
    (r"\bI\s+am\b", "the answer is", 0),
    (r"\bI\s+have\b", "the answer has", 0),
    (r"\bI\s+(hold|take|treat|use|read|count|cite|rate|exclude|include)\b", r"the answer \1s", 0),
)
_VOICE = tuple((re.compile(p, f), r) for p, r, f in VOICE_RULES)


def _sentence_start(s: str, pos: int) -> bool:
    before = s[:pos].rstrip(" \t*_>\"'(")
    return not before or before[-1] in ".!?:\n"


def n3b_voice(text: str) -> str:
    for rx, repl in _VOICE:
        def sub(m: re.Match[str], repl: str = repl) -> str:
            if repl == "\x00SNAKE":
                return m.group(1).replace("_", " ")
            out = m.expand(repl)
            if out and _sentence_start(m.string, m.start()):
                out = out[0].upper() + out[1:]
            return out
        text = rx.sub(sub, text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,;:.])", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# N5 (v1.2, first half) — section labels → five shared labels (§5.3b)
# ---------------------------------------------------------------------------

_L_EVID, _L_ASSESS, _L_GAPS = None, "Assessment:", None
_L_CHANGE, _L_CONTEXT = "What would change the assessment:", None
#: label (lower-case, no markup, no trailing parenthetical or ':'/'.') → shared
#: label, or None (dropped: a lead-in that only announces the answer).
SECTION_LABELS: dict[str, str | None] = {
    **dict.fromkeys(("bottom line", "bluf", "answer", "short answer", "summary"), None),
    **dict.fromkeys((
        "assessment", "my assessment", "assessed", "judgment", "judgement", "the reading", "my reading",
        "what i infer", "read-through", "what this means", "assumption",
        "key assumption"), _L_ASSESS),
    **dict.fromkeys(("why it matters",), None),
    **dict.fromkeys((
        "what changed", "indicators", "the record", "reported", "main evidence", "evidence",
        "what the sources say", "what the sources report", "what the sources show",
        "what the record shows", "what the record does show", "what the verified record shows",
        "what the verified record contains", "what the verified findings report", "what the findings say",
        "what the reports say", "what the documents do show", "what the documents show",
        "what the substrate holds", "what the roster shows", "what the receipts show anyway",
        "what the receipts show anecdotally", "what was found", "which changes bear on it",
        "changes that bear on it", "changes that bear on this", "relevant changes", "overall picture",
        "what the two changes cover", "observed"), _L_EVID),
    **dict.fromkeys((
        "gaps", "gap", "caveat", "caveats", "gaps and caveats", "what's missing", "what is missing",
        "missing evidence", "coverage", "not carried", "what this reading misses", "limits of that evidence",
        "key uncertainty", "how sure this is", "how much weight this can bear", "gaps in the roster",
        "what would answer it", "what would be needed for an exact answer", "why none of the changes bear on it",
        "limits on reading that as \"no attacks\""), _L_GAPS),
    **dict.fromkeys((
        "what would change my mind", "what would change this read", "what would change the assessment",
        "what would change this assessment", "what to watch"), _L_CHANGE),
    **dict.fromkeys((
        "tension", "open situations & tracked events", "related context", "context", "background",
        "background from before the window", "background, outside the window",
        "possible context, not a measured change", "not relevant", "incidental context"), _L_CONTEXT),
}
_HEADING = re.compile(r"^\s*(?:#{1,6}\s+(?P<h>.+?)|\*\*(?P<b>[^*]+?)\*\*:?)\s*$")
_LEADIN = re.compile(r"^(?P<pre>\s*(?:[-*+•]\s+)?)\*\*(?P<label>[^*]{1,80}?)\*\*:?\s+(?P<rest>\S.*)$")
_PLAIN_LEADIN = re.compile(
    r"^(?P<pre>\s*)(?:\*\*)?(?:Bottom line|BLUF|Short answer|Answer)\s*[:.]\s*(?:\*\*)?\s*(?P<rest>\S.*)$", re.I)
#: A numbered dimension heading ("### 3 — Escalation") keeps only its dimension.
_DIM_HEADING = re.compile(r"^\s*#{1,6}\s+\d+\s+[—–-]\s+(?P<dim>[^()\n]+?)\s*(?:\([^)]*\))?\s*$")


def _label_key(label: str) -> str:
    k = label.replace("’", "'").strip().lower()
    k = re.sub(r"\s*\([^)]*\)\s*$", "", k)
    return k.strip().rstrip(":.").strip()


#: The same labels as they read after N3b/N4 (the lead-in pass runs after N4,
#: so a label like "What's missing" arrives as "What is missing").
_LABELS_AFTER_VOICE: dict[str, str | None] = {}


def _label(key: str) -> tuple[bool, str | None]:
    if key in SECTION_LABELS:
        return True, SECTION_LABELS[key]
    if not _LABELS_AFTER_VOICE:
        for k, v in SECTION_LABELS.items():
            _LABELS_AFTER_VOICE[_label_key(n4_narration(n3b_voice(k)))] = v
    if key in _LABELS_AFTER_VOICE:
        return True, _LABELS_AFTER_VOICE[key]
    return False, None


def n5_labels(text: str) -> str:
    """Whole-line section headings (before N3b/N4: a heading is never content)."""
    out = []
    for line in text.split("\n"):
        m = _DIM_HEADING.match(line)
        if m:
            out.append(m.group("dim").strip() + ":")
            continue
        m = _HEADING.match(line)
        if m:
            hit, lab = _label(_label_key(m.group("h") or m.group("b") or ""))
            if hit:
                if lab:
                    out.append(lab)
                continue
        out.append(line)
    return "\n".join(out)


def n5_leadins(text: str) -> str:
    """Bold or plain lead-in labels on a content line (after N4, so that N4's
    content test sees the line exactly as v1.1 did)."""
    out = []
    for line in text.split("\n"):
        m = _LEADIN.match(line)
        if m:
            hit, lab = _label(_label_key(m.group("label")))
            if hit:
                out.append(m.group("pre") + (lab + " " if lab else "") + m.group("rest"))
                continue
        m = _PLAIN_LEADIN.match(line)
        if m:
            out.append(m.group("pre") + m.group("rest"))
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# N4 — process narration
# ---------------------------------------------------------------------------

TOOL_NAMES = (
    "search_signals", "query_facts", "inspect_entity", "vector_search",
    "search_context", "search_corpus", "read_document", "query_nexuses",
    "query_hypotheses", "get_timeline", "compare_targets", "query_paths",
    "find_proxy_chains", "query_brokers", "list_findings", "list_situations",
    "query_predictions", "list_targets", "list_sources", "belief_as_of",
    "query_events", "inspect_event", "series_history", "series_compare",
    "web_search", "web_fetch",
)
LEXICON = (
    "Legba", "the platform", "I searched", "my tools", "the record",
    "the composition", "the voice", "the desk", "this unit", "as-of line",
    "slice", "from memory", *TOOL_NAMES,
)
#: v1.2: a lexicon word's possessive goes with it ("Legba's" left a bare "'s").
_LEX = re.compile(
    "(?:" + "|".join(sorted((re.escape(x) for x in LEXICON), key=len, reverse=True)) + r")(?:['’]s\b)?",
    re.I,
)
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_FUNCTION_CAPS = frozenset(
    "The This That These Those A An It Its We I In On At By For From Of To With "
    "And But Or If As No Not There Their They He She His Her Our Your You Be Is "
    "Are Was Were Has Have Had Do Does Did Any All Some Most Each Every Both "
    "What When Where Which Who Why How While Since Until After Before During "
    "Over Under About Into Than Then Also However Yet So Because Although "
    "Bottom Line Summary Answer Note Evidence Assessment Gaps Context Established".split()
)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def _has_content(sentence: str) -> bool:
    rest = _LEX.sub(" ", sentence)
    rest = re.sub(r"\[R\d+\]", " ", rest)
    if re.search(r"\d", rest) or re.search(rf"\b(?:{_MONTHS})\b", rest):
        return True
    for tok in re.findall(r"[A-Za-z][A-Za-z'’\-]*", rest):
        if tok.isupper() and len(tok) >= 2:
            return True
        if tok[0].isupper() and tok not in _FUNCTION_CAPS:
            return True
    return False


def _tidy(s: str) -> str:
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"\(\s*\)", "", s)
    return s.strip()


def n4_narration(text: str) -> str:
    out_lines = []
    for line in text.split("\n"):
        kept = []
        for sent in _SENT.split(line):
            if not _LEX.search(sent):
                kept.append(sent)
            elif _has_content(sent):
                kept.append(_tidy(_LEX.sub("", sent)))
        out_lines.append(" ".join(k for k in kept if k.strip()))
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# N5 — structure
# ---------------------------------------------------------------------------

_AS_OF_LINE = re.compile(r"^\s*[*_]*\s*as[- ]of\b", re.I)


def _sentence(s: str) -> str:
    s = s.strip()
    if s and s[-1] not in ".!?:;" and not re.search(r"\[R\d+\]$", s):
        s += "."
    return s


def n5_structure(text: str) -> str:
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    out: list[str] = []
    in_fence = False
    for i, line in enumerate(lines):
        raw = line.strip()
        if raw.startswith("```"):
            in_fence = not in_fence
            continue
        if not raw or re.fullmatch(r"[-*_=]{3,}", raw):
            continue
        if i == first and re.match(r"#\s", raw):
            continue  # the title line
        if _AS_OF_LINE.match(raw):
            continue  # the as-of header line: a run constant, not content
        if raw.startswith("|"):
            cells = [c.strip() for c in raw.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            out.append(_sentence("; ".join(c for c in cells if c)))
            continue
        raw = re.sub(r"^#{1,6}\s*", "", raw)
        raw = re.sub(r"^>\s*", "", raw)
        raw = re.sub(r"^([-*+•]|\d+[.)])\s+", "", raw)
        raw = re.sub(r"(\*\*|__)(.+?)\1", r"\2", raw)
        raw = re.sub(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", r"\1", raw)
        raw = raw.replace("`", "").replace(" · ", "; ")
        raw = raw.replace("**", "").replace("__", "")  # v1.2: stray emphasis markers
        if raw.strip():
            out.append(_sentence(raw))
    return " ".join(out)


# ---------------------------------------------------------------------------
# N6 — length, by BM25 against the question + the desk's display name
# ---------------------------------------------------------------------------

_STOP = frozenset(
    "a an the and or of to in on at by for from with as is are was were be been "
    "it its this that these those what which who how did does do has have had "
    "any not no was there their they than then into over under about".split()
)


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in _STOP]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if s.strip()]


def bm25_scores(sentences: list[str], query: str) -> list[float]:
    docs = [_tokens(s) for s in sentences]
    n = len(docs)
    if not n:
        return []
    avg = sum(len(d) for d in docs) / n or 1.0
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    q = _tokens(query)
    scores = []
    for d in docs:
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
            f = tf[t]
            s += idf * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * len(d) / avg))
        scores.append(round(s, 9))
    return scores


def _wc(s: str) -> int:
    return len(s.split())


def n6_length(text: str, query: str) -> tuple[list[str], dict[str, Any]]:
    sents = split_sentences(text)
    words_in = sum(_wc(s) for s in sents)
    if words_in <= WORD_LIMIT:
        return sents, {"words_in": words_in, "words_out": words_in, "sentences_in": len(sents),
                       "dropped": [], "top": 0 if sents else None}
    scores = bm25_scores(sents, query)
    ranked = sorted(range(len(sents)), key=lambda i: (-scores[i], i))
    keep: set[int] = set()
    total = 0
    for i in ranked:
        w = _wc(sents[i])
        if total + w <= WORD_LIMIT:
            keep.add(i)
            total += w
    kept = [s for i, s in enumerate(sents) if i in keep]
    return kept, {
        "words_in": words_in, "words_out": total, "sentences_in": len(sents),
        "dropped": [i for i in range(len(sents)) if i not in keep],
        "top": ranked[0] if ranked else None,
    }


# ---------------------------------------------------------------------------
# N7 / N8
# ---------------------------------------------------------------------------

_CONF_LINE = re.compile(
    r"(?im)^[ \t*_>#-]*(?:stated[ \t]+)?confidence[ \t*_]*:[ \t*_]*[0-9.]+[ \t]*%?[ \t*_]*$"
)
_MONTH_NUM = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
               "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def _month(name: str) -> int | None:
    k = name.lower().rstrip(".")
    return _MONTH_NUM.get(k) or _MONTH_ABBR.get(k)


def _iso(y: str, mo: int | None, d: str) -> str | None:
    if mo is None or not (1 <= int(d) <= 31):
        return None
    return f"{int(y):04d}-{mo:02d}-{int(d):02d}"


def n8_dates(text: str) -> str:
    text = re.sub(
        r"\b(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?(?:\s*UTC)?",
        r"\1", text,
    )
    mon = rf"(?:{_MONTHS})\.?"

    def dmy(m: re.Match[str]) -> str:
        return _iso(m.group(3), _month(m.group(2)), m.group(1)) or m.group(0)

    def mdy(m: re.Match[str]) -> str:
        return _iso(m.group(3), _month(m.group(1)), m.group(2)) or m.group(0)

    text = re.sub(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({mon})\s+(\d{{4}})\b", dmy, text)
    text = re.sub(rf"\b({mon})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", mdy, text)
    return text


def _conf_line(value: Any) -> str:
    if value is None:
        return "Stated confidence: none"
    return f"Stated confidence: {max(0.0, min(1.0, float(value))):.2f}"


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _stage_text(raw: str, record: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text = n1_unicode(raw)
    text, receipts = n2_citations(text, record)
    text = n3_tells(text, record)
    text = _CONF_LINE.sub("", text)  # N7's removal half, before structure merges lines
    text = n5_labels(text)  # v1.2: section labels, read off the raw headings
    text = n3b_voice(text)  # v1.2
    text = n4_narration(text)
    text = n5_leadins(text)  # v1.2
    text = n5_structure(text)
    return text, receipts


def _top_sentence_confidence(record: dict[str, Any], top_sentence: str) -> Any:
    """Arm A with no composition: the unit read that supplied the top sentence."""
    for seg in (record.get("packet") or {}).get("segments") or []:
        seg_text, _ = _stage_text(seg.get("text") or "", {**record, "receipts": []})
        if top_sentence and top_sentence in seg_text and seg.get("effective_confidence") is not None:
            return seg["effective_confidence"]
    return None


def normalise(record: dict[str, Any]) -> dict[str, Any]:
    """One record → the normalised answer a grader sees (plus the cut record)."""
    text, receipts = _stage_text(record.get("raw_answer") or "", record)
    query = n1_unicode(f"{record.get('question') or ''} {record.get('desk_display_name') or ''}")
    sentences, cut = n6_length(text, query)
    confidence = record.get("stated_confidence")
    if confidence is None and record.get("confidence_source") == "top_sentence_unit":
        all_sents = split_sentences(text)
        top = cut.get("top")
        if top is not None and all_sents:
            confidence = _top_sentence_confidence(record, all_sents[top])
    body = n8_dates(" ".join(sentences)).strip()
    out_text = (body + "\n\n" if body else "") + _conf_line(confidence)
    return {
        "schema": SCHEMA,
        "question_id": record.get("question_id"),
        "arm": record.get("arm"),
        "text": out_text,
        "receipts": receipts,
        "cut": cut,
    }


def dumps(obj: Any) -> str:
    """The byte form fixtures freeze."""
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("record")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rec = json.loads(Path(a.record).read_text(encoding="utf-8"))
    text = dumps(normalise(rec))
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
