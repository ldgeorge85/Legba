# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult WRITE vocabulary — what a consult write may say (C-2b-1; design C-2a §1, §5, §8).

A consult write is the operator's instruction, given in a consult turn the
operator ARMED (``allow_writes``), carried out through the ``consult_write``
pack's tools and executed by ONE executor in the registry
(``registry/consult_write_exec.py``). This module is the one authority on the
words it may use: the object classes, each class's verbs and whether each is
BUILT or a declared SEAM, the writable and protected paths, the tier rule,
the refusal causes, the ledger statuses, the confirm-code alphabet, and the
per-turn limits — and on the five tools' closed grammar.

**A leaf** (the C-1a vocabulary idiom): nothing from ``legba`` is imported, so
the slim registry image, the runtime handler and the consult loop all read the
same words. The drift guard (``tests/data_pkg/test_consult_write_frame.py``)
holds: pack tools = registered specs = the vocabulary's built tools = the
executor's branches.

**Platform, not exemplar.** The classes are the platform's object classes —
sources, subjects (code: targets), units, compositions, records (entities,
facts), proposals. W5 built ONE class end to end: ``source``; W6 adds
``subject`` (code: targets) and the analyst classes ``unit`` and
``composition`` (branches ``registry/consult_write_subject.py``,
``consult_write_unit.py``, ``consult_write_composition.py``); W11 adds the
RECORD classes ``entity`` (``registry/consult_write_entity.py`` — edit, merge,
unmerge before compaction, revert) and ``fact`` (``registry/consult_write_fact.py``
— ``assert_fact`` / ``close_fact`` / revert, ``source_type='operator'``); their
tools name their class themselves, :data:`TOOL_CLASS`. Every other
``(class, verb)`` is a declared seam: it is not offered to the model, and
reached anyway (the route, a direct call) it raises :class:`WriteKindNotBuilt`
— ``kind_not_built`` on the tool path, HTTP 501 with the seam id on the route.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

__all__ = [
    "BUILT_CLASSES",
    "CLASSES",
    "CODE_ALPHABET",
    "COMPOSITION_KINDS",
    "COMPOSITION_WRITABLE",
    "CONSULT_WRITE_PACK_ID",
    "ENTITY_WRITABLE",
    "FACT_REF_SEP",
    "SUPERSEDE_MODES",
    "LIMITS",
    "MEMBERSHIP_PATH",
    "NOT_WRITABLE_ANALYST_KINDS",
    "OPTIONS_PREFIX",
    "PROMPT_PATH",
    "RECEIPT_VERSION",
    "RECORD_CLASSES",
    "RERUN_CLASSES",
    "STATUSES",
    "TERMINAL_STATUSES",
    "TOOL_CLASS",
    "TOOL_SCHEMAS",
    "TOOL_VERB",
    "UNIT_WRITABLE",
    "VOCABULARY",
    "WINDOW_MAX_HOURS",
    "WRITE_BLOCK_CAUSES",
    "WRITE_TOOL_NAMES",
    "WriteKindNotBuilt",
    "analyst_tier",
    "code_hash",
    "code_matches",
    "entity_tier",
    "fact_ref",
    "fact_ref_of",
    "fact_tier",
    "is_protected",
    "mint_code",
    "parse_fact_ref",
    "require_built",
    "seam_for",
    "source_tier",
    "subject_tier",
    "verb_for",
    "writable_paths",
]

#: The grant pack the write tools live in (``descriptors/action_pack_consult_write.yaml``).
CONSULT_WRITE_PACK_ID = "consult_write"

#: Receipt schema version, stamped on every receipt (C-2a §3).
RECEIPT_VERSION = "consult_write/1"

#: Every object class the plane knows (C-2a §1), in display order.
CLASSES: tuple[str, ...] = ("source", "subject", "unit", "composition", "entity", "fact", "proposal")

#: ``(class, verb)`` → ``"built"`` or ``("seam", n)``. Closed: a verb not here
#: is not a word of the plane. ``inspect`` is the read verb every class has.
VOCABULARY: dict[tuple[str, str], Any] = {
    ("source", "inspect"): "built",
    ("source", "create"): "built",
    ("source", "update"): "built",
    ("source", "transition"): "built",
    ("source", "retire"): "built",
    ("source", "revert"): "built",
    # The subject class (code: Family.TARGET) — W6, registry/consult_write_subject.py.
    **{("subject", v): "built" for v in ("inspect", "create", "update", "transition", "retire", "revert")},
    # W6 — units and compositions (``Family.ANALYST``; the class is the kind,
    # COMPOSITION_KINDS below). A composition's retire is ``transition_object``
    # to ``retired`` (C-2a §1 names it Tier B).
    **{("unit", v): "built" for v in ("inspect", "update", "transition", "retire", "rerun", "revert")},
    **{("composition", v): "built" for v in ("inspect", "update", "transition", "retire", "rerun", "revert")},
    # SEAMS #64 — unit create (a new unit is a design act, not a turn; declared
    # beyond v1) and the proposal decision class.
    ("unit", "create"): ("seam", 64),
    **{("proposal", v): ("seam", 64) for v in ("inspect", "accept", "reject", "revert")},
    # W11 — the entity record class (registry/consult_write_entity.py). An
    # unmerge after compaction (or of a merge with no recorded pre-image), a
    # split and a retire are SEAMS #68: the branch refuses them
    # ``kind_not_built`` naming the seam; they are not words of their own.
    **{("entity", v): "built" for v in ("inspect", "edit", "merge", "unmerge", "revert")},
    # W11 C-2b-2/facts — the fact record class (``registry/consult_write_fact.py``;
    # ``source_type='operator'``, D6). With both record halves built, SEAMS #65
    # is resolved.
    **{("fact", v): "built" for v in ("inspect", "assert", "close", "revert")},
}

#: Classes with at least one built write verb — the tools' ``class`` enum.
BUILT_CLASSES: tuple[str, ...] = tuple(
    c for c in CLASSES if any(VOCABULARY.get((c, v)) == "built" for (cc, v) in VOCABULARY if cc == c)
)

#: The tools offered (the design's ten minus the seam'd ones, which are not
#: offered: ``decide_proposal``). W6 adds ``rerun_object`` (units and
#: compositions); W11 the entity record tools ``edit_entity``,
#: ``merge_entities``, ``unmerge_entity`` and the fact record tools
#: ``assert_fact``, ``close_fact``.
WRITE_TOOL_NAMES: tuple[str, ...] = (
    "inspect_object",
    "create_object",
    "update_object",
    "transition_object",
    "revert_write",
    "rerun_object",
    "edit_entity",
    "merge_entities",
    "unmerge_entity",
    "assert_fact",
    "close_fact",
)

#: tool → verb (``transition_object`` to ``retired`` is the ``retire`` verb).
TOOL_VERB: dict[str, str] = {
    "inspect_object": "inspect",
    "create_object": "create",
    "update_object": "update",
    "transition_object": "transition",
    "revert_write": "revert",
    "rerun_object": "rerun",
    "edit_entity": "edit",
    "merge_entities": "merge",
    "unmerge_entity": "unmerge",
    "assert_fact": "assert",
    "close_fact": "close",
}

#: A RECORD tool names its class itself (it carries no ``class`` argument):
#: tool → class. The executor reads the class from here before ``args``.
TOOL_CLASS: dict[str, str] = {
    "edit_entity": "entity",
    "merge_entities": "entity",
    "unmerge_entity": "entity",
    "assert_fact": "fact",
    "close_fact": "fact",
}

#: The write gate's refusal vocabulary (C-2a §5.3), checked against the event
#: vocabulary like ``tool_rounds.ARG_BLOCK_CAUSES``: a cause not here never
#: reaches a governor event or a ledger row. ``no_change`` is the frame's one
#: addition: a patch that re-states the head writes nothing (the registry's
#: update is a no-op and mints no receipt), so it is refused, not "executed".
WRITE_BLOCK_CAUSES: frozenset[str] = frozenset({
    "writes_not_armed",
    "writes_disabled",
    "class_disabled",
    "ref_not_read",
    "stale_read",
    "protected_field",
    "path_not_writable",
    "class_not_writable",
    "kind_not_built",
    "no_operator_turn",
    "write_cap_reached",
    "spend_ceiling_reached",
    "preflight_failed",
    "superseded_since",
    "billed_rerun_not_offered",
    "no_change",
})

#: ``consult_writes.status`` — forward only (migration 0227's trigger).
STATUSES: tuple[str, ...] = (
    "staged", "pending_confirmation", "executed", "refused", "failed", "cancelled", "expired",
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"executed", "refused", "failed", "cancelled", "expired"})

#: 4 characters from an alphabet with no look-alikes (no 0/O, 1/I/L, 2/Z, 5/S, 8/B).
CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"

LIMITS: dict[str, int] = {
    "executed_per_turn": 10,
    "staged_per_turn": 5,
    "confirm_ttl_seconds": 900,
    "code_len": 4,
    "reason_max_chars": 300,
    "operator_words_max_chars": 2000,
    "activation_deadline_seconds": 180,
    "activation_poll_seconds": 15,
    "inspect_versions": 5,
    "id_max_chars": 200,
}

# ---------------------------------------------------------------------------
# The source class (C-2a §1 row 1)
# ---------------------------------------------------------------------------

#: The paths an ``update_object`` patch may name on a source. Closed: anything
#: else is ``path_not_writable`` (or ``protected_field`` below). Fetch options
#: are the request-shaping ``config`` keys; the endpoint (``config.url``,
#: ``config.url_template``) and every credential are not on the list — a new
#: endpoint is a new source.
_SOURCE_WRITABLE: tuple[str, ...] = (
    "identity.name",
    "cadence.schedule",
    "cadence.cooldown_seconds",
    "cadence.jitter_seconds",
    "scope.tags",
    "scope.access_class",
    "config.max_items_per_pull",
    "config.max_entries_per_poll",
    "config.lookback_days",
    "config.lookback_hours",
    "config.lookback_minutes",
    "config.timeout_seconds",
    "config.page_size",
    "config.language",
)

#: Paths refused ``protected_field`` on every class (C-2a §1). A ``*_secret``
#: config key is protected by suffix.
_PROTECTED: tuple[str, ...] = (
    "identity.id", "identity.version", "identity.schema_uri", "schema_uri",
)


#: The paths an ``update_object`` patch may name on a subject (C-2a §1 row 2:
#: scope geo / tags / entity classes, name). Cadence is NOT here: a target
#: carries no cadence the runtime reads (``consult_write_subject`` says why).
_SUBJECT_WRITABLE: tuple[str, ...] = (
    "identity.name",
    "scope.geo",
    "scope.tags",
    "scope.entity_classes",
)

_WRITABLE: dict[str, tuple[str, ...]] = {
    "source": _SOURCE_WRITABLE,
    "subject": _SUBJECT_WRITABLE,
}


def writable_paths(cls: str) -> tuple[str, ...]:
    return _WRITABLE.get(cls, ())


def is_protected(path: str) -> bool:
    return path in _PROTECTED or path.endswith("_secret")


def source_tier(verb: str, *, from_state: str | None, to_state: str | None) -> str:
    """``A`` (executes on the armed turn) or ``B`` (staged; the operator confirms).

    Tier B for a source: activate (it starts outbound fetching), pause an
    ACTIVE source, retire (cannot be undone). Everything else is Tier A.
    """
    if verb == "retire":
        return "B"
    if verb == "transition":
        if to_state == "active":
            return "B"
        if to_state == "paused" and from_state == "active":
            return "B"
    return "A"


def subject_tier(verb: str, *, from_state: str | None, to_state: str | None) -> str:
    """Tier for a subject: pause of an ACTIVE subject and retire are B; the rest A."""
    if verb == "retire":
        return "B"
    if verb == "transition" and to_state == "paused" and from_state == "active":
        return "B"
    return "A"


# ---------------------------------------------------------------------------
# The unit and composition classes (C-2a §1 rows 3-4; W6)
# ---------------------------------------------------------------------------

#: ``identity.kind`` values whose analysts are COMPOSITIONS (composition by
#: quotation over other analysts' heads, ``subscription.other_analysts``).
#: Every other analyst kind is a UNIT, except the consult surfaces below.
COMPOSITION_KINDS: frozenset[str] = frozenset({"meta_findings_synthesizer"})

#: Analyst kinds the plane never writes: the consult surfaces themselves (a
#: consult turn does not re-configure the consult that is running it).
NOT_WRITABLE_ANALYST_KINDS: frozenset[str] = frozenset({"consult_on_demand", "deep_consult"})

#: The prompt path — writable only when the head carries an INLINE prompt and
#: is not under a byte-identity pin (D9: the pin list is the
#: ``protected_prompts`` key of ``update_object``'s pack config; absent = every
#: prompt is pinned). Any prompt edit is Tier B.
PROMPT_PATH = "method.system_prompt"
#: A composition's inputs (its slice membership). Any edit is Tier B: it moves the tower.
MEMBERSHIP_PATH = "subscription.other_analysts"
#: ``method.options.<key>`` — writable only for a key the option catalog
#: (``legba.data.option_catalog``) declares for the head's kind (or, for a
#: deterministic unit, its sub-handler); a reserved key is ``protected_field``,
#: an undeclared one ``path_not_writable``. The catalog is the contract.
OPTIONS_PREFIX = "method.options."
#: The runtime reads a window only as ``"<hours>h"`` (anything else silently
#: degrades to its 24 h default), so a window is written in that form, 1..a year.
WINDOW_MAX_HOURS = 8760

#: The paths a patch may name, per class (``method.options.*`` = a declared knob).
UNIT_WRITABLE: tuple[str, ...] = (
    "method.options.*", "subscription.targets.time_window", "eval.rubric", PROMPT_PATH,
)
COMPOSITION_WRITABLE: tuple[str, ...] = (
    "method.options.*", MEMBERSHIP_PATH, "subscription.other_analysts.time_window", PROMPT_PATH,
)

#: The classes ``rerun_object`` takes.
RERUN_CLASSES: tuple[str, ...] = ("unit", "composition")


def analyst_tier(verb: str, *, from_state: str | None, to_state: str | None, paths: Any = ()) -> str:
    """Tier for a unit / composition write: B for retire, pausing an ACTIVE
    head, any prompt edit and any composition membership edit; else A."""
    if verb == "retire":
        return "B"
    if verb == "transition" and to_state == "paused" and from_state == "active":
        return "B"
    if verb == "update" and ({PROMPT_PATH, MEMBERSHIP_PATH} & set(paths)):
        return "B"
    return "A"


# ---------------------------------------------------------------------------
# The entity record class (C-2a §1 row 5; W11)
# ---------------------------------------------------------------------------

#: The keys an ``edit_entity`` patch may name (closed; anything else is
#: ``path_not_writable``). ``aliases_add`` / ``aliases_remove`` edit the
#: profile's alias list (``data.merged_aliases`` — the surface forms entity
#: resolution canonicalizes onto the profile); ``geo_*`` is the geometry
#: (C-2a §12: ``geo_country``, ``geo_region``, and the point). ``entity_class``
#: binds through the live ``entity_class`` family or is refused.
ENTITY_WRITABLE: tuple[str, ...] = (
    "canonical_name",
    "entity_class",
    "aliases_add",
    "aliases_remove",
    "geo_country",
    "geo_region",
    "geo_lat",
    "geo_lon",
)


def entity_tier(verb: str, *, reverses: str | None = None) -> str:
    """Tier for an entity write. Edit, merge and unmerge are B (the operator
    confirms: an edit renames what every future mention resolves onto; a
    merge or an unmerge moves two records). A revert of an edit is A (it
    restores the stored pre-image); a revert of a merge (an unmerge) or of an
    unmerge (a re-merge) is B, like the verb it performs."""
    if verb == "revert":
        return "A" if reverses == "edit" else "B"
    return "B"


# ---------------------------------------------------------------------------
# The fact class (C-2a §1 row 6; W11 C-2b-2/facts)
# ---------------------------------------------------------------------------

#: The named modes of ``assert_fact``'s ``supersede`` (else a list of fact ids):
#: ``none`` closes no differing value; ``differing`` closes the pair's open
#: rows whose value differs EXCEPT seed/curated rows (the instance's curated
#: baseline is closed only by naming its id). A same-value MACHINE row
#: (ingestion / agent / proposed) is replaced by the operator's row in every
#: mode — one open row per triple; a same-value AUTHORITATIVE row
#: (seed / curated / operator) makes the assert ``no_change``.
SUPERSEDE_MODES: tuple[str, ...] = ("none", "differing")

#: ``inspect_object(class='fact', id=…)`` takes a fact id or ``<subject> | <predicate>``.
FACT_REF_SEP = " | "


def fact_ref(subject: Any, predicate: Any) -> str:
    """The canonical object ref of a fact PAIR (lower-cased, trimmed)."""
    return f"{str(subject).strip().lower()}{FACT_REF_SEP}{str(predicate).strip().lower()}"


def fact_ref_of(args: Any) -> str | None:
    """The pair ref of an ``assert_fact`` call's args, or None."""
    if not isinstance(args, dict) or not args.get("subject") or not args.get("predicate"):
        return None
    return fact_ref(args["subject"], args["predicate"])


def parse_fact_ref(ref: Any) -> tuple[str, str] | None:
    """``(subject, predicate)`` from ``<subject> | <predicate>`` (the LAST ``|``
    splits), or None when the ref names no pair."""
    subject, sep, predicate = str(ref or "").rpartition("|")
    if not sep or not subject.strip() or not predicate.strip():
        return None
    return subject.strip(), predicate.strip()


def fact_tier(closes: int) -> str:
    """Tier for a fact write: B when it closes or supersedes MORE THAN ONE
    open row ("supersede-all", C-2a §1); A otherwise."""
    return "B" if closes > 1 else "A"


# ---------------------------------------------------------------------------
# Built / seam
# ---------------------------------------------------------------------------


def seam_for(cls: str, verb: str) -> int | None:
    """The SEAMS number of a declared-unbuilt ``(class, verb)``; None when built."""
    entry = VOCABULARY.get((cls, verb))
    if entry == "built":
        return None
    if isinstance(entry, tuple):
        return int(entry[1])
    return None


def verb_for(tool: str, args: dict[str, Any]) -> str:
    verb = TOOL_VERB[tool]
    if tool == "transition_object" and args.get("to_state") == "retired":
        return "retire"
    return verb


class WriteKindNotBuilt(NotImplementedError):
    """A declared-unbuilt ``(class, verb)`` was reached (SEAMS #64; #65 was resolved in W11)."""

    def __init__(self, cls: str, verb: str, seam: int) -> None:
        super().__init__(f"{cls}.{verb} is not built (SEAMS #{seam})")
        self.cls, self.verb, self.seam = cls, verb, seam


def require_built(cls: str, verb: str) -> None:
    """Raise :class:`WriteKindNotBuilt` for a seam'd pair; ``ValueError`` for an unknown one."""
    entry = VOCABULARY.get((cls, verb))
    if entry is None:
        raise ValueError(f"{cls}.{verb} is not a word of the write plane")
    if entry != "built":
        raise WriteKindNotBuilt(cls, verb, int(entry[1]))


# ---------------------------------------------------------------------------
# The confirm code
# ---------------------------------------------------------------------------


def mint_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(LIMITS["code_len"]))


def code_hash(write_id: str, code: str) -> str:
    """At rest the code is a salted sha256 — never the code itself."""
    return hashlib.sha256(f"{write_id}:{code.strip().upper()}".encode("utf-8")).hexdigest()


def code_matches(write_id: str, code: str, stored_hash: str | None) -> bool:
    return bool(stored_hash) and hmac.compare_digest(code_hash(write_id, code), str(stored_hash))


# ---------------------------------------------------------------------------
# The tools' closed grammar (registered into tool_rounds.TOOL_SCHEMAS by the
# consult write gate; a JSON-able dict here so the leaf imports nothing)
# ---------------------------------------------------------------------------

#: The RECORD classes have their own tools; the head tools (create / update /
#: transition) take the descriptor classes only. ``inspect_object`` reads any.
RECORD_CLASSES: tuple[str, ...] = ("entity", "fact")
_CLASS = {"type": "string", "enum": list(BUILT_CLASSES)}
_HEAD_CLASS = {"type": "string", "enum": [c for c in BUILT_CLASSES if c not in RECORD_CLASSES]}
_ENTITY_ID = {"type": "string", "minLength": 36, "maxLength": 36}
_ENTITY_VERSION = {"type": "string", "pattern": "^[0-9]{1,9}$", "minLength": 1, "maxLength": 9}
_ID = {"type": "string", "minLength": 1, "maxLength": LIMITS["id_max_chars"]}
_VERSION = {"type": "string", "minLength": 16, "maxLength": 64}
_REASON = {"type": "string", "minLength": 1, "maxLength": LIMITS["reason_max_chars"]}
_STATES = {"type": "string", "enum": ["draft", "configured", "active", "paused", "retired"]}


def _closed(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


#: name → (description, closed JSON schema).
TOOL_SCHEMAS: dict[str, tuple[str, dict[str, Any]]] = {
    "inspect_object": (
        "Read one object's head before changing it: its writable fields, version, state, "
        "the last versions, its protected paths and the consult writes on it. Every change "
        "needs an inspect in an EARLIER round.",
        _closed({"class": _CLASS, "id": _ID}, ["class", "id"]),
    ),
    "create_object": (
        "Create a new object from a full body; it lands in draft. The id must not exist.",
        _closed({"class": _HEAD_CLASS, "body": {"type": "object"}, "reason": _REASON}, ["class", "body", "reason"]),
    ),
    "update_object": (
        "Change allowlisted fields: patch is {path: value} (e.g. {\"cadence.cooldown_seconds\": 600}); "
        "expected_version is the version inspect_object returned.",
        _closed(
            {"class": _HEAD_CLASS, "id": _ID, "expected_version": _VERSION,
             "patch": {"type": "object"}, "reason": _REASON},
            ["class", "id", "expected_version", "patch", "reason"],
        ),
    ),
    "transition_object": (
        "Move an object through its lifecycle (draft, configured, active, paused, retired). "
        "Retired cannot be undone; prefer paused.",
        _closed(
            {"class": _HEAD_CLASS, "id": _ID, "expected_version": _VERSION, "to_state": _STATES,
             "reason": _REASON},
            ["class", "id", "expected_version", "to_state", "reason"],
        ),
    ),
    "revert_write": (
        "Undo one executed consult write of this session by its write id (inspect_object lists them).",
        _closed({"write_id": _ID, "reason": _REASON}, ["write_id", "reason"]),
    ),
    "rerun_object": (
        "Force one run of an ACTIVE unit or composition now (trigger 'method'). It cannot be undone: "
        "its outputs enter the tower and the next run supersedes them. A subject-bound one needs "
        "subject (a subject id a read returned); inspect_object says which.",
        _closed(
            {"class": {"type": "string", "enum": list(RERUN_CLASSES)}, "id": _ID, "subject": _ID,
             "reason": _REASON},
            ["class", "id", "reason"],
        ),
    ),
    "edit_entity": (
        "Edit one entity record: patch keys canonical_name, entity_class (a value of the "
        "entity_class family), aliases_add / aliases_remove (lists of names), geo_country, "
        "geo_region, geo_lat, geo_lon. entity_id and expected_version are what "
        "inspect_object(class='entity') returned in an earlier round. The operator confirms it.",
        _closed(
            {"entity_id": _ENTITY_ID, "expected_version": _ENTITY_VERSION,
             "patch": {"type": "object"}, "reason": _REASON},
            ["entity_id", "expected_version", "patch", "reason"],
        ),
    ),
    "merge_entities": (
        "Merge loser_id INTO keeper_id: the loser becomes a redirect to the keeper and its name "
        "and aliases fold onto the keeper. Both ids must come from inspect_object(class='entity') "
        "in an earlier round. The operator confirms it; it can be unmerged until the lifecycle "
        "compacts the loser (inspect says whether that has happened).",
        _closed(
            {"keeper_id": _ENTITY_ID, "loser_id": _ENTITY_ID, "reason": _REASON},
            ["keeper_id", "loser_id", "reason"],
        ),
    ),
    "unmerge_entity": (
        "Split a merged entity back out (loser_id is the merged one) and record that the two are "
        "distinct. Only before compaction, and only for a merge whose pre-image was recorded; "
        "inspect_object says whether it can. The operator confirms it.",
        _closed({"loser_id": _ENTITY_ID, "reason": _REASON}, ["loser_id", "reason"]),
    ),
    "assert_fact": (
        "Record a fact as the operator's own word (source 'operator', confidence 1.0 unless given; "
        "it grounds like curated). supersede: 'none' closes no other value; 'differing' closes the "
        "pair's open rows with a different value except seed/curated ones; or a list of fact ids "
        "that inspect_object(class='fact', id='<subject> | <predicate>') returned in an earlier round. "
        "Closing more than one row waits for the operator's Confirm.",
        _closed(
            {"subject": {"type": "string", "minLength": 1, "maxLength": 2048},
             "predicate": {"type": "string", "minLength": 1, "maxLength": 512},
             "value": {"type": "string", "minLength": 1, "maxLength": 4096},
             "valid_from": {"type": "string", "minLength": 4, "maxLength": 40},
             "confidence": {"type": "number", "minimum": 0, "maximum": 1},
             "supersede": {"oneOf": [
                 {"type": "string", "enum": list(SUPERSEDE_MODES)},
                 {"type": "array", "items": _ID, "minItems": 1, "maxItems": 20},
             ]},
             "cites": {"type": "array", "items": _ID, "maxItems": 20},
             "reason": _REASON},
            ["subject", "predicate", "value", "supersede", "reason"],
        ),
    ),
    "close_fact": (
        "Close one open fact (valid until now) by its id — an id inspect_object(class='fact') "
        "returned in an earlier round. Nothing is deleted; revert_write reopens it.",
        _closed({"fact_id": _ID, "reason": _REASON}, ["fact_id", "reason"]),
    ),
}
