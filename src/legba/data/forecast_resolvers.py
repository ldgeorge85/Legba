# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The forecast ledger's resolvers — deterministic RULES, never a verdict (ST1-A).

A resolver reads a sealed question (its resolver kind, spec, window, due date,
grace, condition and void rules) and the evidence rows a search collected, and
returns a RESOLUTION PROPOSAL:

  * ``outcome`` — ``yes`` | ``no`` | ``void`` | ``undetermined`` (the record
    cannot decide yet: grace still running, a source below the bar, a series
    not held) | ``needs_judgement`` (a clause of the sealed criterion is not a
    rule — "is this the exchange agreed at the named talks" — and the question
    waits for the operator's override);
  * ``clauses`` — each clause of the criterion, ``met`` | ``unmet`` |
    ``judgement`` | ``unknown``, with the evidence that satisfies it;
  * ``evidence`` — the rows and the sentence SPANS it rests on, each with its
    publisher, source class, fetch and publication times and where they sit
    relative to the window, and the clause it satisfies;
  * ``judgements`` — exactly which human judgement is needed (each one goes to
    the operator's override path: an ``override_log`` row);
  * ``void_rule`` — the pre-registered void rule that applies, if any, and
    whether its reading is arguable;
  * ``searched`` — the scope behind any absence (window, patterns, rows
    scanned, publishers, the official sources the corpus holds per actor): an
    absence is a scoped absence, not a guess.

No model is called anywhere here. Matching is regex over source text, sentence
by sentence; every term list is in this file and versioned by
:data:`RESOLVER_VERSION`, so a proposal can be re-derived from the same rows.

Built kinds: the four the sealed questions use — ``event_reported``,
``official_document``, ``official_result``, ``series_threshold`` (and ``also``
sub-conditions, negated or not). ``count_threshold`` and
``scheduled_event_status`` are declared SEAMS (#76) and refuse loud
(:class:`ResolverKindNotBuilt`).

LEAF: stdlib only (the registry-slim scorecard route imports the store that
imports this).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "PROPOSAL_OUTCOMES",
    "RESOLVER_VERSION",
    "BUILT_KINDS",
    "Proposal",
    "ResolverKindNotBuilt",
    "ResolverContext",
    "SearchPlan",
    "evaluate",
    "search_plans",
    "void_rule_sentences",
]

#: The rule set's revision: every lexicon, threshold and combination rule.
RESOLVER_VERSION = "2026-11/st1.2"

PROPOSAL_OUTCOMES: tuple[str, ...] = ("yes", "no", "void", "undetermined", "needs_judgement")
BUILT_KINDS: frozenset[str] = frozenset(
    {"event_reported", "official_document", "official_result", "series_threshold"}
)

#: Publishers whose rows are machine-coded aggregates, not reporting.
AGGREGATOR_PREFIXES: tuple[str, ...] = ("source.gdelt.",)

#: Two independent reporting sources (plan 10 §3.2) — the default bar.
INDEPENDENT_SOURCES_REQUIRED = 2


class ResolverKindNotBuilt(RuntimeError):
    """SEAMS #76: a resolver kind the sealed questions do not use yet.
    Refuse-loud; the job records it in its receipt and raises an alert."""

    def __init__(self, kind: str) -> None:
        super().__init__(
            f"resolver kind {kind!r} is not built (SEAMS #76): no proposal is written; "
            "the operator resolves it through the override path"
        )
        self.kind = kind


# ---------------------------------------------------------------------------
# The lexicon (versioned with RESOLVER_VERSION)
# ---------------------------------------------------------------------------

#: Actor code -> the names a source text uses. Short all-capital tokens are
#: matched case-sensitively ("US" never matches "us").
ACTORS: dict[str, tuple[str, ...]] = {
    "US": ("United States", "U.S.", "USA", "US", "American", "Americans", "Pentagon",
           "CENTCOM", "Central Command", "Washington", "White House"),
    "IR": ("Iran", "Iranian", "Iranians", "Tehran", "Islamic Republic"),
    "IL": ("Israel", "Israeli", "Israelis", "IDF", "Israel Defense Forces", "Netanyahu"),
    "RU": ("Russia", "Russian", "Russians", "Moscow", "Kremlin", "Putin"),
    "UA": ("Ukraine", "Ukrainian", "Ukrainians", "Kyiv", "Kiev", "Zelensky", "Zelenskyy"),
    "TW": ("Taiwan", "Taiwanese", "Taipei"),
}
#: Case-insensitive stems for the SQL pre-filter (no short capital tokens).
ACTOR_SQL_STEMS: dict[str, str] = {
    "US": r"united states|u\.s\.|american|pentagon|centcom|central command|white house|washington",
    "IR": r"iran|tehran",
    "IL": r"israel|idf|netanyahu",
    "RU": r"russia|moscow|kremlin|putin",
    "UA": r"ukrain|kyiv|kiev|zelensk",
    "TW": r"taiwan|taipei",
}
#: Who speaks for an actor when it ACKNOWLEDGES an act (spec confirmation
#: "acknowledgement", "CENTCOM report").
ACKNOWLEDGERS: dict[str, str] = {
    "US": r"CENTCOM|Central Command|Pentagon|Department of (?:Defense|War)|"
          r"(?:Defense|War) Secretary|Secretary of (?:Defense|War)|White House|"
          r"(?:US|U\.S\.) (?:military|officials?|forces|defense officials?)",
    "IR": r"Iranian (?:government|foreign ministry|officials?)|Tehran said|IRGC said",
    "IL": r"IDF said|Israeli military said|Israel Defense Forces said|Israeli officials? said",
    "RU": r"Kremlin|Russian (?:Defence|Defense) Ministry|Russian Foreign Ministry|Peskov|Zakharova",
    "UA": r"Ukrainian (?:government|officials?|General Staff|Coordination Headquarters)|"
          r"Zelensk(?:y|yy) said|Umerov|Lubinets|Budanov",
}

_PARTY_ALIASES: dict[str, tuple[str, ...]] = {
    "KMT": ("KMT", "Kuomintang", "Nationalist Party"),
    "DPP": ("DPP", "Democratic Progressive Party"),
    "TPP": ("TPP", "Taiwan People's Party"),
}

#: An event class is matched by its family; the family is detected from the
#: spec's own words (class / doc_type), never from the question's slug.
_FAMILIES: dict[str, dict[str, str]] = {
    "prisoner_exchange": {
        "detect": r"prisoner|\bpows?\b|captive",
        "sql": r"(prisoner|\mpows?\M|captive)[^.]{0,80}(exchange|swap)|(exchange|swap)[^.]{0,40}(prisoner|\mpows?\M|captive)",
        "class": r"(?i:prisoners?|\bPOWs?\b|captives?|servicemen|soldiers|defenders|detainees)[^.]{0,80}"
                 r"(?i:exchang|swap|releas|return|home)|(?i:exchang|swap)[^.]{0,60}(?i:prisoner|\bPOW|captive)",
        "realis": r"(?i:\b(?:exchanged|swapped|were (?:released|freed|returned|handed over|brought home)|"
                  r"returned home|came home|have returned|has returned|handed over|took place|carried out|"
                  r"completed|was conducted|were exchanged|have been (?:returned|freed|released)|brought home)\b)",
    },
    "strike": {
        "detect": r"strike|attack|bomb",
        "sql": r"strike|struck|airstrike|bomb|missile|drone attack|attack",
        "class": r"(?i:\b(?:strikes?|struck|air ?strikes?|bomb(?:ed|ing|s)?|missile|drone attack|attack(?:ed|s)?)\b)",
        "realis": r"(?i:\b(?:struck|bombed|hit|attacked|launched|carried out|targeted|conducted|destroyed|"
                  r"damaged|was hit|were hit|killed)\b)",
    },
    "halt": {
        "detect": r"halt|moratorium|ceasefire|truce|pause",
        "sql": r"(halt|moratorium|pause|ceasefire|truce)[^.]{0,80}(energy|refiner|power|infrastructure)|(energy|refiner|power|infrastructure)[^.]{0,80}(halt|moratorium|pause|ceasefire|truce)",
        "class": r"(?i:\b(?:halt|moratorium|pause|ceasefire|cease-fire|truce)\b)",
        "realis": r"(?i:\b(?:announced|agreed|declared|took effect|in effect|entered into force|came into force|"
                  r"ordered|accepted|signed)\b)",
    },
    "document": {
        "detect": r"memorand|framework|\bmou\b|agreement|communiqu|decree|declaration",
        "sql": r"memorand|framework|\mmou\M|communiqu",
        "class": r"(?i:memorandum|framework (?:agreement|document|deal|accord)|framework\b|\bMOU\b|communiqu[eé])",
        "realis": r"(?i:\b(?:signed|published|released|announced|agreed|concluded|initial(?:l)?ed|"
                  r"acknowledged|unveiled|reached|finali[sz]ed)\b)",
    },
    "result": {
        "detect": r"$^",
        "sql": r"",
        "class": r"",
        "realis": r"(?i:\b(?:won|wins|elected|re-elected|reelected|victory|defeated|clinched|secured|"
                  r"was chosen|claimed victory|sworn in)\b)",
    },
}
_ENERGY = r"(?i:refiner(?:y|ies)|oil depot|fuel depot|energy (?:infrastructure|facilit(?:y|ies)|sites?)|" \
          r"power (?:plant|station|grid|lines?)|substation|gas (?:facility|plant|infrastructure)|" \
          r"thermal power|oil (?:terminal|facility)|energy)"
_ENERGY_SQL = r"refiner|oil depot|fuel depot|energy|power plant|power station|substation|gas facilit|oil terminal"

#: A sentence in the conditional, future, denial or agreement-to-act mood is
#: not a report that the act happened.
_IRREALIS = re.compile(
    r"(?i:\b(?:will|would|could|may|might|plans?|planned|planning|expected|expects|should|"
    r"threaten(?:s|ed)?|warn(?:s|ed)?|if|possible|potential|prepar(?:e|es|ing)|ready to|"
    r"consider(?:s|ing)?|propos(?:e|ed|al)|aim(?:s|ing)? to|seek(?:s|ing)? to|denied|deny|denies|"
    r"no sign|no evidence|poll(?:s|ing)?|projected|likely|forecast)\b)"
)
_AGREE_TO_ACT = re.compile(r"(?i:\bagree(?:d|s|ment)?\s+(?:to|on)\b|\bdeal\b)")
#: Markers that an attribution is contested (a judgement, not a rule).
_ATTRIBUTION_DOUBT = re.compile(
    r"(?i:\b(?:unclaimed|did not claim|has not claimed|no one claimed|suspected|blamed|attributed to|"
    r"allegedly|reportedly|denied responsibility|unknown (?:drone|aircraft|attackers?))\b)"
)
#: Background, not news: a sentence recalling earlier acts ("have from time to
#: time swapped", "the most recent exchange took place on Oct.", "signed in
#: June") is not a report of an act inside the window.
_RETRO = re.compile(
    r"(?i:\b(?:from time to time|most recent|previous(?:ly)?|earlier this|last (?:year|month|week|time)|"
    r"years? ago|months? ago|in the past|since the (?:start|beginning)|throughout|history|historic(?:al)?|"
    r"late supreme leader|during the (?:more |most )?(?:recent )?war|most intense phase|at the start of the war|"
    r"before the war|occasionally|at times|regularly|repeatedly|routinely|periodically)\b)"
)
_MONTHS: dict[str, int] = {
    m: i for i, names in enumerate((
        ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
        ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
        ("october", "oct"), ("november", "nov"), ("december", "dec")), start=1) for m in names
}
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted((m.capitalize() for m in _MONTHS if m != "may"),
                                                 key=len, reverse=True)) + r")\b\.?")
_YEAR_RE = re.compile(r"\b(19\d\d|20\d\d)\b")


def _retrospective(s: str, ws: datetime | None, we: datetime | None) -> bool:
    """True when the sentence is about an earlier time than the window: a
    recollection marker, or a month / year it names that the window does not
    span."""
    if _RETRO.search(s):
        return True
    if ws is None or we is None:
        return False
    years = set(range(ws.year, we.year + 1))
    if any(int(y) not in years for y in _YEAR_RE.findall(s)):
        return True
    months: set[int] = set()
    cur = datetime(ws.year, ws.month, 1, tzinfo=timezone.utc)
    while cur <= we and len(months) < 12:
        months.add(cur.month)
        cur = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=timezone.utc)
    return any(_MONTHS[m.lower()] not in months for m in _MONTH_RE.findall(s))


_POSTPONED = re.compile(r"(?i:\b(?:postponed|cancelled|canceled|delayed|suspended|called off)\b)")
_FIRST_BALLOT = re.compile(r"(?i:\bfirst (?:ballot|round|vote)\b|\bsingle ballot\b|\bon the first try\b)")
_LATER_BALLOT = re.compile(r"(?i:\b(?:second|third|fourth|fifth|another|multiple|several) (?:ballots?|rounds?|votes?)\b)")

_STOPWORDS = frozenset(
    "a an the of on in at by to for and or with from candidate candidates elected election "
    "race first ballot ballots wins win won result results vote voting mayor mayoral".split()
)
#: Words a target area may carry and still be "plain" (a country, not a place
#: inside it that a rule cannot locate).
_PLAIN_TARGET_WORDS = frozenset("territory soil the of".split())

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘(])|\n{1,}")
_MAX_SPAN = 600
_TAG_RE = re.compile(r"<[^>]+>")


def _term_re(terms: Iterable[str]) -> re.Pattern[str]:
    parts = []
    for t in terms:
        esc = re.escape(t)
        if t.isupper() and len(t) <= 4:
            parts.append(rf"\b{esc}\b")
        else:
            parts.append(rf"(?i:\b{esc})")
    return re.compile("|".join(parts) or r"$^")


_ACTOR_RES: dict[str, re.Pattern[str]] = {k: _term_re(v) for k, v in ACTORS.items()}


def _actor_re(code: str) -> re.Pattern[str]:
    if code in _ACTOR_RES:
        return _ACTOR_RES[code]
    return _term_re([code])


def _actor_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _codes_named_in(text: str) -> list[str]:
    """Actor codes whose names appear in a free-text field (``target_area``)."""
    return [code for code, rx in _ACTOR_RES.items() if rx.search(text or "")]


def _family(text: str) -> str:
    low = (text or "").lower()
    for name in ("prisoner_exchange", "halt", "document", "strike"):
        if re.search(_FAMILIES[name]["detect"], low):
            return name
    return "generic"


def _significant_tokens(text: str) -> list[str]:
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]+", text or "")
    return [t for t in toks if t.lower() not in _STOPWORDS and (t[0].isupper() or t.isupper())]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        s = str(value).strip()
        if not s:
            return None
        if re.fullmatch(r"\d{8}", s):
            s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    dt = _instant(value)
    return dt.isoformat() if dt else None


def _window_bounds(spec_window: Any, start: Any, end: Any) -> tuple[datetime | None, datetime | None]:
    ws, we = _instant(start), _instant(end)
    if isinstance(spec_window, (list, tuple)) and len(spec_window) == 2:
        s2, e2 = _instant(spec_window[0]), _instant(spec_window[1])
        if s2 is not None:
            ws = s2
        if e2 is not None:
            # A bare date as the end of a window means the whole day.
            raw = str(spec_window[1])
            we = e2 + timedelta(hours=23, minutes=59, seconds=59) if len(raw) <= 10 else e2
    return ws, we


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass
class SearchPlan:
    """What the evidence search reads for one clause set (``part``: ``main``
    or ``also[i]``). ``sql_all`` are case-insensitive Postgres AREs that must
    ALL match a row's title+summary+text; ``since``/``until`` bound fetch time."""

    part: str
    kind: str
    family: str
    sql_all: list[str]
    since: datetime | None
    until: datetime | None
    actors: list[str] = field(default_factory=list)


@dataclass
class ResolverContext:
    """The instant the proposal is read at, and whether the question is
    treated as due (``as_if_due`` for the rehearsal)."""

    as_of: datetime
    as_if_due: bool = False

    def due(self, due_at: datetime) -> bool:
        return self.as_if_due or self.as_of >= due_at

    def closed(self, due_at: datetime, grace_hours: int) -> bool:
        return self.as_if_due or self.as_of >= due_at + timedelta(hours=int(grace_hours))


@dataclass
class Proposal:
    outcome: str
    resolver_kind: str
    as_of: datetime
    clauses: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    judgements: list[dict[str, Any]] = field(default_factory=list)
    void_rule: dict[str, Any] | None = None
    searched: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    lean: str | None = None
    resolver_version: str = RESOLVER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "lean": self.lean,
            "resolver_kind": self.resolver_kind,
            "resolver_version": self.resolver_version,
            "as_of": _iso(self.as_of),
            "clauses": self.clauses,
            "evidence": self.evidence,
            "judgements": self.judgements,
            "void_rule": self.void_rule,
            "searched": self.searched,
            "notes": self.notes,
        }


def _q_get(q: Mapping[str, Any], key: str, default: Any = None) -> Any:
    v = q.get(key) if isinstance(q, Mapping) else getattr(q, key, default)
    return default if v is None else v


# ---------------------------------------------------------------------------
# Void rules — split the sealed text into what it says, never rewrite it
# ---------------------------------------------------------------------------


def void_rule_sentences(void_rules: str | None) -> list[dict[str, Any]]:
    """Each sentence of the sealed void rules, classified:

    ``operator``  "... is decided by the operator as an override_log row";
    ``silence``   nothing published / no outlet reports by due + grace —
                  ``scopes`` names whose silence it needs (``official``,
                  ``outlets``);
    ``premise``   the event is postponed / not held / does not convene;
    ``other``.
    """
    out: list[dict[str, Any]] = []
    for i, s in enumerate(re.split(r"(?<=[.;])\s+", (void_rules or "").strip()), start=1):
        s = s.strip()
        if not s:
            continue
        low = s.lower()
        if "decided by the operator" in low or "the operator decides" in low:
            kind = "operator"
        elif re.search(r"postpone|not held|does not convene|cancel|premise", low):
            kind = "premise" if not re.search(r"publishes no|no roll call|no result", low) else "premise_or_silence"
        elif re.search(r"\bnothing\b|\banything\b|publishes no|no outlet|no primary|no two|no value|address", low):
            kind = "silence"
        else:
            kind = "other"
        scopes = []
        if re.search(r"government|centcom|official|cec|clerk|eia|authority|publishes", low):
            scopes.append("official")
        if re.search(r"outlet|reporting source|independent", low):
            scopes.append("outlets")
        out.append({"index": i, "text": s, "kind": kind, "scopes": scopes})
    return out


def _operator_judgements(void_rules: str | None) -> list[dict[str, Any]]:
    return [
        {"key": f"void_rule_{s['index']}", "question": s["text"], "source": "void_rules",
         "route": "override_log"}
        for s in void_rule_sentences(void_rules) if s["kind"] == "operator"
    ]


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", str(text or ""))).strip()


def _sentences(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(field, sentence)`` — the title, then the summary and text split."""
    out: list[tuple[str, str]] = []
    title = _clean(row.get("title"))
    if title:
        out.append(("title", title))
    seen = {title}
    for fld in ("summary", "text"):
        body = _TAG_RE.sub(" ", str(row.get(fld) or ""))
        for s in _SENTENCE_SPLIT.split(body):
            s = _clean(s)
            if len(s) < 12 or s in seen:
                continue
            seen.add(s)
            out.append((fld, s[:_MAX_SPAN]))
    return out


def publisher_of(source_id: str) -> str:
    parts = str(source_id or "").split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "source" else str(source_id or "")


def _is_aggregator(source_id: str) -> bool:
    return any(str(source_id or "").startswith(p) for p in AGGREGATOR_PREFIXES)


def _relation(t: datetime | None, ws: datetime | None, we: datetime | None) -> str:
    if t is None:
        return "unknown"
    if ws is not None and t < ws:
        return "before_window"
    if we is not None and t > we:
        return "after_window"
    return "inside_window"


def _evidence_entry(row: Mapping[str, Any], span: str, fld: str, clause: str,
                    ws: datetime | None, we: datetime | None, part: str) -> dict[str, Any]:
    pub = _instant(row.get("published_at"))
    fetched = _instant(row.get("fetched_at"))
    return {
        "ref": f"signal:{row.get('signal_id')}",
        "signal_id": str(row.get("signal_id")),
        "source_id": row.get("source_id"),
        "publisher": publisher_of(str(row.get("source_id") or "")),
        "source_class": row.get("source_class"),
        "url": row.get("url"),
        "title": _clean(row.get("title"))[:300],
        "published_at": _iso(pub),
        "fetched_at": _iso(fetched),
        "published_vs_window": _relation(pub, ws, we),
        "fetched_vs_window": _relation(fetched, ws, we),
        "clause": clause,
        "part": part,
        "field": fld,
        "span": span,
    }


def _event_time(row: Mapping[str, Any]) -> datetime | None:
    return _instant(row.get("published_at")) or _instant(row.get("fetched_at"))


def _official_for(row: Mapping[str, Any], codes: Sequence[str]) -> bool:
    if (row.get("source_class") or "") != "official":
        return False
    geo = {str(g).upper() for g in (row.get("source_geo") or [])}
    return bool(geo & {c.upper() for c in codes})


def _independent_count(entries: Iterable[Mapping[str, Any]], rows_by_id: Mapping[str, Mapping[str, Any]]) -> int:
    """Distinct publishers; two state outlets of one state count once."""
    keys: set[str] = set()
    for e in entries:
        row = rows_by_id.get(str(e.get("signal_id")), {})
        if row.get("source_class") == "state_media":
            geo = sorted(str(g).upper() for g in (row.get("source_geo") or []))
            keys.add("state_media:" + (",".join(geo) or publisher_of(str(row.get("source_id")))))
        else:
            keys.add(publisher_of(str(e.get("source_id") or "")))
    return len(keys)


# ---------------------------------------------------------------------------
# Search plans (what to read) — derived from the spec, used by the search
# ---------------------------------------------------------------------------


def _main_window(q: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
    ws, we = _window_bounds(spec.get("window"), _q_get(q, "window_start"), _q_get(q, "window_end"))
    if spec.get("in_effect_on") or spec.get("by"):
        # A document "by" / "in effect on" a date may be published any time
        # after the seal; the question's window then bounds its sub-conditions.
        ws, we = None, None
    if ws is None:
        ws = _instant(_q_get(q, "sealed_at")) or _instant(_q_get(q, "created_at"))
    if we is None:
        we = _instant(spec.get("by") or spec.get("in_effect_on")) or _instant(_q_get(q, "due_at"))
        if we is not None and len(str(spec.get("by") or spec.get("in_effect_on") or "x" * 11)) <= 10:
            we = we + timedelta(hours=23, minutes=59, seconds=59)
    return ws, we


def _plan_for(part: str, kind: str, spec: Mapping[str, Any], ws: datetime | None,
              we: datetime | None, grace_until: datetime | None) -> SearchPlan | None:
    if kind == "series_threshold":
        return None
    if kind == "official_result":
        toks = _significant_tokens(str(spec.get("race") or ""))
        outcome = _outcome_terms(spec)
        sql = []
        if toks:
            sql.append("|".join(re.escape(t.lower()) for t in toks[:3]))
        if outcome:
            sql.append("|".join(re.escape(t.lower()) for t in outcome))
        start = _instant(spec.get("election_date") or spec.get("vote_date")) or ws
        return SearchPlan(part, kind, "result", sql, start, grace_until, [])
    text = str(spec.get("class") or spec.get("doc_type") or "")
    fam = _family(text)
    actors = _actor_codes(spec.get("actors")) + _actor_codes(spec.get("actor")) \
        + _actor_codes(spec.get("issuers"))
    target_codes = _codes_named_in(str(spec.get("target_area") or ""))
    sql: list[str] = []
    if fam in _FAMILIES:
        sql.append(_FAMILIES[fam]["sql"])
    else:
        toks = [t.lower() for t in re.findall(r"[A-Za-z]{4,}", text)][:2]
        sql += [re.escape(t) for t in toks]
    if re.search(r"refiner|energy", text, re.I) and fam == "strike":
        sql.append(_ENERGY_SQL)
    anchor = [ACTOR_SQL_STEMS[c] for c in (target_codes or [a for a in actors if a != "US"] or actors)
              if c in ACTOR_SQL_STEMS]
    if anchor:
        sql.append("|".join(anchor))
    until = grace_until if grace_until and (we is None or grace_until > we) else we
    return SearchPlan(part, kind, fam, sql, ws, until, actors)


def _grace_until(q: Mapping[str, Any]) -> datetime | None:
    due = _instant(_q_get(q, "due_at"))
    return None if due is None else due + timedelta(hours=int(_q_get(q, "grace_hours", 0) or 0))


def _rehearsal_shift(ws: datetime | None, we: datetime | None,
                     ctx: ResolverContext | None) -> tuple[datetime | None, datetime | None]:
    """Rehearsal only (``as_if_due``): a window that has not opened yet is
    moved, length kept, to end at the as-of — so the rule is exercised on
    today's record. A real run never shifts a window."""
    if ctx is None or not ctx.as_if_due or ws is None or we is None or ws <= ctx.as_of:
        return ws, we
    delta = we - ctx.as_of
    return ws - delta, we - delta


def search_plans(q: Mapping[str, Any], ctx: ResolverContext | None = None) -> list[SearchPlan]:
    """The searches a question needs: its main clause set, then each ``also``."""
    kind = str(_q_get(q, "resolver_kind"))
    spec = _q_get(q, "resolver_spec", {}) or {}
    ws, we = _rehearsal_shift(*_main_window(q, spec), ctx)
    gu = _grace_until(q)
    plans: list[SearchPlan] = []
    main = _plan_for("main", kind, spec, ws, we, gu)
    if main:
        plans.append(main)
    for i, sub in enumerate(spec.get("also") or [], start=1):
        sspec = sub.get("spec") or {}
        sws, swe = _rehearsal_shift(*_window_bounds(sspec.get("window"), ws, we), ctx)
        p = _plan_for(f"also[{i}]", str(sub.get("kind")), sspec, sws, swe, gu)
        if p:
            plans.append(p)
    return plans


# ---------------------------------------------------------------------------
# The kinds
# ---------------------------------------------------------------------------


def _outcome_terms(spec: Mapping[str, Any]) -> list[str]:
    toks = _significant_tokens(str(spec.get("outcome") or ""))
    out: list[str] = []
    for t in toks:
        out += list(_PARTY_ALIASES.get(t, (t,)))
    return out


def _part_result(outcome: str, clauses: list[dict[str, Any]], evidence: list[dict[str, Any]],
                 judgements: list[dict[str, Any]], notes: list[str], lean: str | None,
                 topical: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": outcome, "clauses": clauses, "evidence": evidence,
            "judgements": judgements, "notes": notes, "lean": lean, "topical": topical}


def _event_like(part: str, kind: str, spec: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                ws: datetime | None, we: datetime | None) -> dict[str, Any]:
    """``event_reported`` and ``official_document`` share one shape: a class
    of act by named actors, reported in the realis mood inside a window."""
    text = str(spec.get("class") or spec.get("doc_type") or "")
    fam = _family(text)
    f = _FAMILIES.get(fam)
    class_re = re.compile(f["class"]) if f else _term_re(re.findall(r"[A-Za-z]{4,}", text)[:2])
    realis_re = re.compile(f["realis"]) if f else re.compile(_FAMILIES["document"]["realis"])
    energy = bool(re.search(r"refiner|energy", text, re.I))
    energy_re = re.compile(_ENERGY)
    doer = list(dict.fromkeys(
        _actor_codes(spec.get("actor")) + (_initiator_code(spec) if spec.get("initiated_by") else [])))
    actors = _actor_codes(spec.get("actors"))
    issuers = _actor_codes(spec.get("issuers"))
    target_text = str(spec.get("target_area") or "")
    target_codes = _codes_named_in(target_text)
    clauses: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    judgements: list[dict[str, Any]] = []
    notes: list[str] = []
    confirming: list[dict[str, Any]] = []
    per_issuer: dict[str, list[dict[str, Any]]] = {c: [] for c in issuers}
    topical_pubs: set[str] = set()
    official_channel = 0
    doubt_spans: list[dict[str, Any]] = []
    outside = 0
    rows_by_id = {str(r.get("signal_id")): r for r in rows}
    ack_codes = doer or actors or issuers
    ack_re = re.compile("|".join(ACKNOWLEDGERS[c] for c in ack_codes if c in ACKNOWLEDGERS) or r"$^")
    for row in rows:
        if _is_aggregator(str(row.get("source_id"))):
            continue
        t = _event_time(row)
        rel = _relation(t, ws, we)
        whole = " ".join(s for _, s in _sentences(row))
        if not class_re.search(whole):
            continue
        if any(not _actor_re(c).search(whole) for c in (actors or issuers or doer)):
            continue
        if rel != "inside_window":
            outside += 1
            continue
        topical_pubs.add(publisher_of(str(row.get("source_id"))))
        if ack_re.search(whole) or _official_for(row, ack_codes):
            official_channel += 1
        title = _clean(row.get("title"))
        for fld, s in _sentences(row):
            if not (class_re.search(s) and realis_re.search(s)):
                continue
            if _IRREALIS.search(s) or (fam != "document" and fam != "halt" and _AGREE_TO_ACT.search(s)):
                continue
            if _retrospective(s, ws, we):
                continue
            ctx = s if fld == "title" else f"{title} {s}"
            if energy and not energy_re.search(ctx):
                continue
            if any(not _doer_acts(c, s, realis_re) for c in doer):
                continue
            if issuers and any(not _actor_re(c).search(s) for c in issuers):
                continue  # the document is BETWEEN the issuers: the span names them all
            if target_codes and not any(_actor_re(c).search(s) for c in target_codes):
                continue
            entry = _evidence_entry(row, s, fld, "event", ws, we, part)
            if issuers:
                named = [c for c in issuers if _actor_re(c).search(s) or _official_for(row, [c])]
                if not named:
                    continue
                entry["clause"] = "issued_by:" + ",".join(named)
                for c in named:
                    per_issuer[c].append(entry)
            confirming.append(entry)
            if _ATTRIBUTION_DOUBT.search(s):
                doubt_spans.append(entry)
            break  # one span per row is enough to cite
    evidence.extend(confirming)
    # The confirmation clause.
    confirmation = str(spec.get("confirmation") or "")
    ack_mode = bool(re.search(r"acknowledg|centcom|report by|reported by", confirmation, re.I)) and bool(doer)
    official_hits = [e for e in confirming if _official_for(rows_by_id.get(e["signal_id"], {}), ack_codes)]
    independent = _independent_count(confirming, rows_by_id)
    if ack_mode:
        acked = [e for e in confirming if ack_re.search(e["span"]) or e in official_hits]
        conf_met = bool(acked)
        conf_text = f"acknowledged by the actor or its named channel ({confirmation})"
        conf_ev = acked
    else:
        conf_met = bool(official_hits) or independent >= INDEPENDENT_SOURCES_REQUIRED
        conf_text = (confirmation or "one primary official source or two independent reporting sources")
        conf_ev = official_hits or confirming
    event_clause = {
        "id": f"{part}:event", "text": f"{text} — realis report inside the window",
        "status": "met" if confirming else "unmet",
        "evidence": [e["ref"] for e in confirming],
    }
    clauses.append(event_clause)
    if issuers:
        for c in issuers:
            clauses.append({
                "id": f"{part}:issuer:{c}", "text": f"acknowledged/issued by {c}",
                "status": "met" if per_issuer[c] else "unmet",
                "evidence": [e["ref"] for e in per_issuer[c]],
            })
    clauses.append({
        "id": f"{part}:confirmation", "text": conf_text,
        "status": "met" if conf_met else ("unmet" if not confirming else "below_bar"),
        "evidence": [e["ref"] for e in conf_ev],
        "independent_publishers": independent,
        "official_rows": len(official_hits),
    })
    # Qualitative clauses a rule cannot settle.
    plain_target = target_text and not (
        set(w.lower() for w in re.findall(r"[A-Za-z]+", target_text))
        - {n.lower() for c in target_codes for n in " ".join(ACTORS[c]).split()}
        - _PLAIN_TARGET_WORDS
    )
    if target_text:
        clauses.append({
            "id": f"{part}:target", "text": f"target area: {target_text}",
            "status": ("met" if confirming else "unmet") if plain_target else "judgement",
            "evidence": [e["ref"] for e in confirming],
        })
        if not plain_target and confirming:
            judgements.append({
                "key": f"{part}:target_area", "route": "override_log", "source": "resolver_spec",
                "question": f"Does the reported location satisfy the sealed target area "
                            f"'{target_text}'? A rule matches the country, not the place inside it.",
            })
    if spec.get("initiated_by"):
        clauses.append({
            "id": f"{part}:initiated_by", "text": f"initiated by {spec.get('initiated_by')}",
            "status": ("judgement" if doubt_spans else "met") if confirming else "unmet",
            "evidence": [e["ref"] for e in doubt_spans or confirming],
        })
        if doubt_spans:
            judgements.append({
                "key": f"{part}:attribution", "route": "override_log", "source": "evidence",
                "question": "Sources carry attribution doubt (suspected / unclaimed / blamed): is the act "
                            f"initiated by {spec.get('initiated_by')}?",
            })
    if spec.get("basis") and confirming:
        judgements.append({
            "key": f"{part}:basis", "route": "override_log", "source": "resolver_spec",
            "question": f"Is the reported act the one named in the sealed basis: '{spec.get('basis')}'?",
        })
    if spec.get("in_effect_on") and confirming:
        judgements.append({
            "key": f"{part}:in_effect", "route": "override_log", "source": "resolver_spec",
            "question": f"Is the announced act still in effect on {spec.get('in_effect_on')} "
                        "(not revoked or broken)?",
        })
    if outside:
        notes.append(f"{outside} matching rows fall outside the window (not counted)")
    notes.append("an article's publication date stands for the event date; a report inside the "
                 "window about an act before it would need the operator")
    if not confirming:
        lean = "no"
    elif issuers and not all(per_issuer[c] for c in issuers):
        lean = "undetermined"
    elif conf_met:
        lean = "yes"
    else:
        lean = "below_bar"
    topical = {"publishers": sorted(topical_pubs), "official_channel_rows": official_channel}
    return _part_result("", clauses, evidence, judgements, notes, lean, topical)


def _doer_acts(code: str, s: str, realis_re: re.Pattern[str]) -> bool:
    """The actor is the one acting: named shortly before the realis verb, or
    after it as its agent ("... was struck by Israel")."""
    actor = _actor_re(code).pattern
    return bool(
        re.search(rf"(?:{actor})[^.;]{{0,60}}?(?:{realis_re.pattern})", s)
        or re.search(rf"(?:{realis_re.pattern})[^.;]{{0,30}}?\bby\b[^.;]{{0,30}}?(?:{actor})", s)
    )


def _initiator_code(spec: Mapping[str, Any]) -> list[str]:
    codes = _codes_named_in(str(spec.get("initiated_by") or ""))
    return codes or _actor_codes(spec.get("initiated_by"))


def _official_result(part: str, spec: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                     ws: datetime | None, we: datetime | None) -> dict[str, Any]:
    race_toks = _significant_tokens(str(spec.get("race") or ""))
    race_re = _term_re(race_toks) if race_toks else re.compile(r"$^")
    outcome_terms = _outcome_terms(spec)
    outcome_re = _term_re(outcome_terms) if outcome_terms else re.compile(r"$^")
    win_re = re.compile(_FAMILIES["result"]["realis"])
    authority = str(spec.get("authority") or "")
    auth_toks = [t for t in _significant_tokens(authority) if len(t) > 2][:4]
    auth_re = _term_re(auth_toks) if auth_toks else re.compile(r"$^")
    qualifier_first = bool(re.search(r"first ballot", str(spec.get("outcome") or ""), re.I))
    start = _instant(spec.get("election_date") or spec.get("vote_date")) or ws
    yes: list[dict[str, Any]] = []
    contrary: list[dict[str, Any]] = []
    first_ok: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    postponed: list[dict[str, Any]] = []
    authority_seen = 0
    topical_pubs: set[str] = set()
    rows_by_id = {str(r.get("signal_id")): r for r in rows}
    for row in rows:
        if _is_aggregator(str(row.get("source_id"))):
            continue
        t = _event_time(row)
        if start is not None and t is not None and t < start:
            continue
        sents = _sentences(row)
        whole = " ".join(s for _, s in sents)
        if not all(re.search(rf"(?i:\b{re.escape(tok)})", whole) for tok in race_toks):
            continue
        topical_pubs.add(publisher_of(str(row.get("source_id"))))
        if auth_re.search(whole):
            authority_seen += 1
        hit = None
        for fld, s in sents:
            if _POSTPONED.search(s) and race_re.search(s):
                postponed.append(_evidence_entry(row, s, fld, "premise", start, we, part))
            if not (win_re.search(s) and race_re.search(s)) or _IRREALIS.search(s):
                continue
            if _retrospective(s, start, we):
                continue
            hit = (fld, s)
            break
        if hit is None:
            continue
        fld, s = hit
        if outcome_re.search(s) or (fld != "title" and outcome_re.search(_clean(row.get("title")))):
            e = _evidence_entry(row, s, fld, "outcome", start, we, part)
            yes.append(e)
            if qualifier_first:
                if _FIRST_BALLOT.search(whole):
                    first_ok.append(e)
                if _LATER_BALLOT.search(whole):
                    later.append(e)
        else:
            contrary.append(_evidence_entry(row, s, fld, "contrary_winner", start, we, part))
    evidence = yes + contrary + postponed
    yes_n = _independent_count(yes, rows_by_id)
    con_n = _independent_count(contrary, rows_by_id)
    clauses = [
        {"id": f"{part}:result", "text": f"{spec.get('race')}: {spec.get('outcome')}",
         "status": "met" if yes_n >= INDEPENDENT_SOURCES_REQUIRED and not contrary else
                   ("judgement" if yes and contrary else ("below_bar" if yes else "unmet")),
         "evidence": [e["ref"] for e in yes], "independent_publishers": yes_n,
         "contrary_publishers": con_n},
        {"id": f"{part}:authority", "text": f"published by {authority}",
         "status": "met" if authority_seen else "unknown",
         "note": f"{authority_seen} matching rows name the authority; the authority's own "
                 "publication is not held in the corpus (reporting stands in for it)"},
    ]
    judgements: list[dict[str, Any]] = []
    notes: list[str] = []
    if start is not None and we is not None and start > we:
        notes.append(f"the vote ({_iso(start)[:10]}) is after the as-of: no result can be reported yet")
    if contrary:
        judgements.append({
            "key": f"{part}:winner", "route": "override_log", "source": "evidence",
            "question": "Reports name a winner without the sealed outcome's terms "
                        f"({', '.join(outcome_terms)}): who won, and does it match '{spec.get('outcome')}'?",
        })
    if qualifier_first:
        st = "unmet" if later else ("met" if first_ok else ("judgement" if yes else "unmet"))
        clauses.append({"id": f"{part}:first_ballot", "text": "on the first ballot", "status": st,
                        "evidence": [e["ref"] for e in (later or first_ok)]})
        if yes and st == "judgement":
            judgements.append({
                "key": f"{part}:first_ballot", "route": "override_log", "source": "resolver_spec",
                "question": "The win is reported but no source says on which ballot: was it the first?",
            })
    if postponed:
        judgements.append({
            "key": f"{part}:premise", "route": "override_log", "source": "void_rules",
            "question": "Sources report a postponement or cancellation: did the premise fail (void)?",
        })
    if not yes:
        lean = "no_result"
    elif qualifier_first and later:
        lean = "no"
    elif yes_n >= INDEPENDENT_SOURCES_REQUIRED:
        lean = "yes"
    else:
        lean = "below_bar"
    topical = {"publishers": sorted(topical_pubs), "official_channel_rows": authority_seen}
    return _part_result("", clauses, evidence, judgements, notes, lean, topical)


def _as_of_vintages(observations: Sequence[Mapping[str, Any]], as_of: datetime) -> list[Mapping[str, Any]]:
    """One value per (series, subject, day): the newest vintage RECORDED at or
    before ``as_of`` (a later revision never leaks into an earlier reading).
    A row with no ``record_time`` counts as recorded."""
    best: dict[tuple[Any, Any, Any], Mapping[str, Any]] = {}
    for o in observations:
        if o.get("value") is None or _instant(o.get("valid_from")) is None:
            continue
        rt = _instant(o.get("record_time"))
        if rt is not None and _aware(rt) > _aware(as_of):
            continue
        key = (o.get("series_id"), o.get("subject"), _instant(o["valid_from"]))
        held = best.get(key)
        if held is None or (_aware(_instant(held.get("record_time")) or datetime.min)
                            <= _aware(rt or datetime.min)):
            best[key] = o
    return list(best.values())


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _series_threshold(q: Mapping[str, Any], spec: Mapping[str, Any],
                      observations: Sequence[Mapping[str, Any]], ctx: ResolverContext) -> Proposal:
    """The value on the sealed date, or on the last trading day before it.

    THE HOLIDAY RULE, READ STRICTLY (st1.2). A value dated BEFORE the sealed
    date stands in for it only once the series holds a day AFTER the date:
    only then has the publisher's release passed the date without a value for
    it (a holiday). A publisher that releases weekly (EIA's spot tables) has
    not published the date yet for days after it — reading the day before
    then would settle the question on the wrong value. So: the date itself if
    held; else, with a later day held, the newest day in the 7 before it;
    else nothing yet (undetermined, and at due + grace a judgement, never a
    void read off our own ingest lag)."""
    on = _instant(spec.get("on"))
    op = str(spec.get("op"))
    value = float(spec.get("value"))
    due = _instant(_q_get(q, "due_at"))
    grace = int(_q_get(q, "grace_hours", 0) or 0)
    held = _as_of_vintages(observations, ctx.as_of)
    days = sorted(held, key=lambda o: _instant(o["valid_from"]))
    newest = _instant(days[-1]["valid_from"]) if days else None
    exact = [o for o in days if on is not None and _instant(o["valid_from"]) == on]
    later = [o for o in days if on is not None and _instant(o["valid_from"]) > on]
    before = [o for o in days if on is not None and on - timedelta(days=7) <= _instant(o["valid_from"]) < on]
    if exact:
        obs, basis = exact[-1], "the sealed date"
    elif later and before:
        obs, basis = before[-1], "the last trading day before the date (a later day is held: the date had no value)"
    else:
        obs, basis = None, None
    searched = {
        "series": spec.get("series"), "source": spec.get("source"), "on": spec.get("on"),
        "holiday_rule": spec.get("holiday_rule"),
        "observations_held": len(held),
        "newest_held": _iso(newest),
        "held_after_date": len(later),
        "lookback_days": 7,
        "vintage": "newest recorded at or before the as-of",
    }
    p = Proposal(outcome="undetermined", resolver_kind="series_threshold", as_of=ctx.as_of, searched=searched)
    void = next((s for s in void_rule_sentences(_q_get(q, "void_rules")) if s["kind"] == "silence"), None)
    clause = f"{spec.get('series')} {op} {value} on {spec.get('on')}"
    if obs is not None:
        v = float(obs["value"])
        ok = {">=": v >= value, ">": v > value, "<=": v <= value, "<": v < value, "==": v == value}[op]
        p.evidence.append({
            "ref": f"observation:{obs.get('id')}", "series_id": obs.get("series_id"),
            "valid_from": _iso(obs.get("valid_from")), "value": v, "unit": obs.get("unit"),
            "source_id": obs.get("source_id"), "source_url": obs.get("source_url"),
            "sha256": obs.get("sha256"), "fetch_sha256": obs.get("fetch_sha256") or obs.get("sha256"),
            "fetch_version_id": obs.get("fetch_version_id"),
            "record_time": _iso(obs.get("record_time")), "clause": "value_on_date", "basis": basis,
        })
        p.clauses.append({"id": "main:value", "text": clause, "status": "met" if ok else "unmet",
                          "evidence": [p.evidence[0]["ref"]], "observed": v})
        p.outcome = "yes" if ok else "no"
        p.lean = p.outcome
        if not ctx.due(due):
            p.outcome = "undetermined"
            p.notes.append("value held but the question is not yet due")
        return p
    p.clauses.append({"id": "main:value", "text": clause, "status": "unknown", "evidence": []})
    if not held:
        p.notes.append(f"series {spec.get('source')}/{spec.get('series')} is not held in observations: a "
                       "coverage gap, not evidence that the publisher published nothing")
        if ctx.closed(due, grace):
            p.outcome = "needs_judgement"
            p.judgements.append({
                "key": "main:series_value", "route": "override_log", "source": "coverage",
                "question": f"The series is not ingested. Enter the published {spec.get('series')} value "
                            f"for {spec.get('on')} (or the last trading day before) with its source, or "
                            "confirm the publisher published none (void).",
            })
        return p
    if not later:
        p.notes.append(f"the series is held through {_iso(newest)}, not past {spec.get('on')}: the release "
                       "covering the date is not held yet (the publisher's lag or our poll)")
        if ctx.closed(due, grace):
            p.outcome = "needs_judgement"
            p.void_rule = void and {**void, "applies": "unknown", "arguable": True,
                                    "why": "the series is not held past the date, so its silence is not shown"}
            p.judgements.append({
                "key": "main:series_not_past_date", "route": "override_log", "source": "coverage",
                "question": f"The series is held only through {_iso(newest)}. Enter the published "
                            f"{spec.get('series')} value for {spec.get('on')} (or the last trading day "
                            "before) with its release, or confirm the publisher published none (void).",
            })
        return p
    if ctx.closed(due, grace):
        p.outcome = "void" if void else "needs_judgement"
        p.void_rule = void and {**void, "applies": True, "arguable": False}
        p.notes.append("the series is held past the date but carries no value for it or the 7 days before")
    return p


# ---------------------------------------------------------------------------
# Combination
# ---------------------------------------------------------------------------


def _combine(parts: list[tuple[dict[str, Any], bool]]) -> str:
    """AND over the parts, each ``(result, negate)``; returns a lean."""
    leans = []
    for res, negate in parts:
        lean = res["lean"]
        if negate:
            lean = {"yes": "no", "no": "yes", "no_result": "yes"}.get(lean, lean)
        leans.append(lean)
    if "no" in leans:
        return "no"
    if "no_result" in leans:
        return "no_result"
    if all(lean == "yes" for lean in leans):
        return "yes"
    if "below_bar" in leans:
        return "below_bar"
    return "undetermined"


def evaluate(
    q: Mapping[str, Any],
    rows_by_part: Mapping[str, Sequence[Mapping[str, Any]]],
    ctx: ResolverContext,
    *,
    observations: Sequence[Mapping[str, Any]] = (),
    searched_by_part: Mapping[str, Mapping[str, Any]] | None = None,
) -> Proposal:
    """The proposal for ``q`` on the rows its :func:`search_plans` returned."""
    kind = str(_q_get(q, "resolver_kind"))
    spec = _q_get(q, "resolver_spec", {}) or {}
    if kind not in BUILT_KINDS:
        raise ResolverKindNotBuilt(kind)
    for sub in spec.get("also") or []:
        if str(sub.get("kind")) not in BUILT_KINDS - {"series_threshold"}:
            raise ResolverKindNotBuilt(str(sub.get("kind")))
    if kind == "series_threshold":
        return _series_threshold(q, spec, observations, ctx)
    due = _instant(_q_get(q, "due_at"))
    grace = int(_q_get(q, "grace_hours", 0) or 0)
    ws, we_full = _rehearsal_shift(*_main_window(q, spec), ctx)

    def _clip(t: datetime | None) -> datetime | None:
        return ctx.as_of if ctx.as_if_due and t is not None and ctx.as_of < t else t

    we = _clip(we_full)
    parts: list[tuple[str, dict[str, Any], bool, str]] = []
    if kind == "official_result":
        parts.append(("main", _official_result("main", spec, rows_by_part.get("main", ()), ws, we), False, kind))
    else:
        parts.append(("main", _event_like("main", kind, spec, rows_by_part.get("main", ()), ws, we), False, kind))
    for i, sub in enumerate(spec.get("also") or [], start=1):
        sspec = sub.get("spec") or {}
        sws, swe = _rehearsal_shift(*_window_bounds(sspec.get("window"), ws, we_full), ctx)
        swe = _clip(swe)
        name = f"also[{i}]"
        res = _event_like(name, str(sub.get("kind")), sspec, rows_by_part.get(name, ()), sws, swe)
        parts.append((name, res, bool(sub.get("negate")), str(sub.get("kind"))))
    p = Proposal(outcome="undetermined", resolver_kind=kind, as_of=ctx.as_of)
    for name, res, negate, _ in parts:
        for c in res["clauses"]:
            c["negated"] = negate
        p.clauses += res["clauses"]
        p.evidence += res["evidence"]
        p.judgements += res["judgements"]
        p.notes += [f"{name}: {n}" for n in res["notes"]]
    lean = _combine([(res, negate) for _, res, negate, _ in parts])
    p.lean = lean
    p.searched = {
        "window": {"start": _iso(ws), "end": _iso(we)},
        "due_at": _iso(due), "grace_hours": grace,
        "as_if_due": ctx.as_if_due,
        "parts": {name: {**dict((searched_by_part or {}).get(name, {})),
                         "topical_publishers": res["topical"]["publishers"],
                         "official_channel_rows": res["topical"]["official_channel_rows"],
                         "negated": negate}
                  for name, res, negate, _ in parts},
    }
    closed = ctx.closed(due, grace)
    is_due = ctx.due(due)
    main = parts[0][1]
    voids = void_rule_sentences(_q_get(q, "void_rules"))
    if not main["evidence"] and kind != "official_result":
        # Nothing confirms the act: under AND that decides the question (no
        # sub-condition's judgement can make it yes). Before due + grace it is
        # not yet an answer.
        p.judgements = []
        if not closed:
            p.outcome = "undetermined"
            p.notes.append("no confirming report yet; grace still running")
            return p
        p.outcome, p.void_rule = _absence_reading(main, voids)
        if p.outcome == "needs_judgement":
            p.judgements.append({
                "key": "main:quiet_no_show", "route": "override_log", "source": "void_rules",
                "question": "Nothing was reported on the act in the window by any channel the sealed void "
                            "rule names. Void (the rule read literally) or no (the act did not happen)?",
            })
        return p
    if main["evidence"]:
        p.judgements += _operator_judgements(_q_get(q, "void_rules"))
        cond = _q_get(q, "condition")
        if cond:
            p.judgements.append({
                "key": "condition", "route": "override_log", "source": "condition",
                "question": f"Does the sealed condition hold: '{cond}'? If it failed, is the question void?",
            })
    if lean == "yes":
        p.outcome = "needs_judgement" if p.judgements else ("yes" if is_due else "undetermined")
        if p.outcome == "undetermined":
            p.notes.append("confirmed before the due date: an early resolution names this evidence")
    elif lean == "no":
        p.outcome = "needs_judgement" if p.judgements else ("no" if is_due else "undetermined")
    elif kind == "official_result" and lean == "no_result":
        if not closed:
            p.outcome = "undetermined"
            p.notes.append("no report of the result yet; grace still running")
        else:
            p.outcome = "needs_judgement"
            silence = next((s for s in voids if s["kind"] in ("silence", "premise_or_silence")), None)
            p.void_rule = silence and {**silence, "applies": "unknown", "arguable": True,
                                       "why": "the authority's own publication is not held in the corpus"}
            p.judgements.append({
                "key": "main:no_result_found", "route": "override_log", "source": "coverage",
                "question": "No report of the result was found in the corpus by due + grace. Did the "
                            "authority publish a result (enter it), or none (void per the sealed rule)?",
            })
    elif lean == "below_bar":
        p.outcome = "needs_judgement" if closed else "undetermined"
        if closed:
            p.judgements.append({
                "key": "main:below_bar", "route": "override_log", "source": "resolver_spec",
                "question": "The act is reported below the confirmation bar (one publisher, no primary "
                            "official source): confirm 'no', or name the second source.",
            })
    else:
        p.outcome = "needs_judgement" if (closed and p.judgements) else "undetermined"
    return p


def _absence_reading(main: Mapping[str, Any], voids: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Due + grace passed, nothing confirms the act: ``no`` or the silence
    void rule. A silence rule reads ``void`` only when every channel it names
    was silent; when that is the only reading of a quiet no-show (nobody
    reports an act that did not happen) it is ARGUABLE and the operator
    decides."""
    silence = next((s for s in voids if s["kind"] == "silence"), None)
    topical = main["topical"]
    if silence is None:
        return "no", None
    channels = {
        "official": topical["official_channel_rows"] > 0,
        "outlets": len(topical["publishers"]) > 0,
    }
    scopes = silence["scopes"] or ["outlets"]
    spoke = [s for s in scopes if channels.get(s)]
    if spoke:
        return "no", {**silence, "applies": False, "arguable": False,
                      "why": f"the channel(s) the rule names were not silent: {', '.join(spoke)}"}
    return "needs_judgement", {
        **silence, "applies": True, "arguable": True,
        "why": "every channel the sealed rule names was silent inside the window: read literally the "
               "question is void, yet a quiet no-show (the act did not happen and nobody reported it) "
               "reads as 'no' — the operator decides",
    }
