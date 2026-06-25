# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helper catalog bound into the Starlark sandbox at evaluation time.

Per spec §2: the host registers a fixed catalog of helper functions in the
Starlark global namespace. Operators compose these with normal Starlark
syntax (`and`, `or`, `not`, comparisons, list/dict literals, `in`).

Each helper is a **factory** at this module level: it takes an evaluation
context dict and returns the bound Starlark-callable. The context dict is
built per-evaluation by the caller and carries either:

  * ``signal``  — for ``target.scope.predicate`` + ``target.sources[*].filter``
  * ``target``  — for ``analyst.subscription.targets.predicate``
  * ``event``   — for ``analyst.cadence.trigger`` (NATS event)

Helpers are total: they never raise from Starlark. Missing ctx fields →
return a conservative default (``False`` for predicates, ``""`` for str,
``[]`` for lists, ``0.0`` for floats). This matches the spec note that
"Helpers return false / raise typed errors when ctx is incomplete." We
fail-closed here rather than raise, so a partially-built ctx during the
phase-5 vertical-slice spike still evaluates without crashing.

Helpers also include per-helper "surface" tags so the resolver in
``compiler.py`` can reject `event_payload_get` in target.scope etc.

The catalog version is exposed at module level for descriptor-side pinning.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from .errors import PredicateRuntimeError

# ---------------------------------------------------------------------------
# Catalog version
# ---------------------------------------------------------------------------
#
# Bumped per spec §2 / §8. Major bump = breaking signature change or removal;
# minor = additive (new helpers); patch = doc-only.
#
# 1.1.0: catalog entries gained per-helper required-ctx-key declarations and
# per-surface ctx-builder contracts (G4 — envelope-vs-row divergence class).
# Additive metadata; helper signatures unchanged.

HELPER_CATALOG_VERSION = "1.2.0"  # 1.2.0: + contains_any (free-text, 5c thematic)


# ---------------------------------------------------------------------------
# Surface enumeration (string form, mirrored in compiler.PredicateSurface)
# ---------------------------------------------------------------------------

SURFACE_TARGET_SCOPE = "target.scope"
SURFACE_SOURCE_FILTER = "source.filter"
SURFACE_ANALYST_SUBSCRIPTION = "analyst.subscription"
SURFACE_CADENCE_TRIGGER = "cadence.trigger"
SURFACE_ALL = (
    SURFACE_TARGET_SCOPE,
    SURFACE_SOURCE_FILTER,
    SURFACE_ANALYST_SUBSCRIPTION,
    SURFACE_CADENCE_TRIGGER,
)


# ---------------------------------------------------------------------------
# Severity ordering (used by severity_at_least)
# ---------------------------------------------------------------------------
#
# Mirrors AlertPayload.severity per provenance/models.py + the conventional
# {low, medium, high, critical} ladder used across the platform. Uppercase
# inputs accepted because cadence-trigger predicates tend to compare against
# upstream conventions ("HIGH", "CRITICAL").

_SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def _severity_rank(s: object) -> int:
    if not isinstance(s, str):
        return -1
    return _SEVERITY_ORDER.get(s.lower(), -1)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _as_set(value: Any) -> set[str]:
    """Coerce a list/tuple/set/iterable-of-str → frozenset for fast lookup."""
    if value is None:
        return set()
    if isinstance(value, (set, frozenset)):
        return set(value)
    if isinstance(value, (list, tuple)):
        return set(value)
    try:
        return set(value)
    except TypeError:
        return set()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return []


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):  # bool is int but not what we want here
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        # Accept ISO-8601 with or without a trailing 'Z'
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Signal-scoped helpers (target.scope + source.filter)
# ---------------------------------------------------------------------------


def _mentions(ctx: dict[str, Any]) -> Callable[[str], bool]:
    classes = _as_set((ctx.get("signal") or {}).get("entity_classes"))

    def mentions(entity_class: object) -> bool:
        return isinstance(entity_class, str) and entity_class in classes

    return mentions


def _mentions_any(ctx: dict[str, Any]) -> Callable[[Iterable[str]], bool]:
    classes = _as_set((ctx.get("signal") or {}).get("entity_classes"))

    def mentions_any(entity_classes: object) -> bool:
        for c in _as_list(entity_classes):
            if isinstance(c, str) and c in classes:
                return True
        return False

    return mentions_any


def _geo_match(ctx: dict[str, Any]) -> Callable[..., bool]:
    """Match a signal's geo. Two call forms, both used by descriptors:

      * ``geo_match()``        — signal geo overlaps the TARGET's scope.geo
        (precomputed ``signal.geo_match`` when the matcher set it O(1), else
        ``signal.geo_provenance`` × ``target.scope_geo``). The scope form.
      * ``geo_match([codes])`` — signal geo overlaps an EXPLICIT code list, e.g.
        ``geo_match(["BR"])`` as the discovery / G20 registrars emit. This is the
        form that works on the real-time residual path (the trigger engine's
        ``matches()``), where only the signal is in scope — there is no target
        context, so the scope form would never match. Mirrors ``geo_in``.
    """
    signal_geo = _as_set((ctx.get("signal") or {}).get("geo_provenance"))
    pre = (ctx.get("signal") or {}).get("geo_match")
    target_geo = _as_set((ctx.get("target") or {}).get("scope_geo"))

    def geo_match(codes: object = None) -> bool:
        if codes is not None:
            return bool(signal_geo & _as_set(codes))
        if pre is not None:
            return bool(pre)
        return bool(signal_geo & target_geo) if signal_geo and target_geo else False

    return geo_match


def _geo_in(ctx: dict[str, Any]) -> Callable[[Iterable[str]], bool]:
    signal_geo = _as_set((ctx.get("signal") or {}).get("geo_provenance"))

    def geo_in(codes: object) -> bool:
        for c in _as_list(codes):
            if isinstance(c, str) and c in signal_geo:
                return True
        return False

    return geo_in


def _org_match(ctx: dict[str, Any]) -> Callable[[], bool]:
    """Pre-computed against the target's org watchlist (spec §2: O(1))."""
    pre = (ctx.get("signal") or {}).get("org_match")
    if pre is not None:
        result = bool(pre)

        def org_match() -> bool:
            return result

        return org_match

    signal_orgs = _as_set((ctx.get("signal") or {}).get("org_refs"))
    target_orgs = _as_set((ctx.get("target") or {}).get("watchlist_orgs"))
    overlap = bool(signal_orgs & target_orgs) if signal_orgs and target_orgs else False

    def org_match() -> bool:
        return overlap

    return org_match


def _has_tag(ctx: dict[str, Any]) -> Callable[[str], bool]:
    """All-surfaces tag check.

    Source of tags depends on context:
      * signal-scoped: ``signal.tags``
      * descriptor-scoped: ``target.tags`` (or analyst.tags via ctx["target"])
      * event-scoped: ``event.tags``

    We union all three so the same helper Just Works on every surface.
    """
    sig_tags = _as_set((ctx.get("signal") or {}).get("tags"))
    tgt_tags = _as_set((ctx.get("target") or {}).get("tags"))
    evt_tags = _as_set((ctx.get("event") or {}).get("tags"))
    tags: set[str] = sig_tags | tgt_tags | evt_tags

    def has_tag(tag: object) -> bool:
        return isinstance(tag, str) and tag in tags

    return has_tag


def _has_any_tag(ctx: dict[str, Any]) -> Callable[[Iterable[str]], bool]:
    sig_tags = _as_set((ctx.get("signal") or {}).get("tags"))
    tgt_tags = _as_set((ctx.get("target") or {}).get("tags"))
    evt_tags = _as_set((ctx.get("event") or {}).get("tags"))
    tags: set[str] = sig_tags | tgt_tags | evt_tags

    def has_any_tag(values: object) -> bool:
        for t in _as_list(values):
            if isinstance(t, str) and t in tags:
                return True
        return False

    return has_any_tag


@lru_cache(maxsize=1024)
def _word_boundary_re(term: str) -> "re.Pattern[str]":
    """Compile (cached) a WORD-BOUNDARY matcher for ``term``.

    ``(?<!\\w)term(?!\\w)`` asserts the term isn't flanked by a word character,
    so "war" matches "the war" / "war-torn" but NOT "warsaw" / "forward" /
    "airstrike". More robust than ``\\b`` for terms that begin/end with
    punctuation (e.g. "de-escalate"). The term is ``re.escape``'d (literal),
    so there is no catastrophic-backtracking risk; the cache bounds compile
    cost across the many signals a thematic slice scans."""
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")


def _contains_any(ctx: dict[str, Any]) -> Callable[[Iterable[str]], bool]:
    """Free-text match against the signal's text (case-insensitive, WORD-BOUNDARY).
    The text is the title (+ summary/body when present — most live signals carry
    only a title). The lever a THEMATIC target uses to frame a situation the
    coarse NER entity_classes can't express — e.g. an 'iran war' target whose
    predicate is ``contains_any(["iran","tehran"]) and
    contains_any(["war","strike","missile"])``. DQ-#70/F2: matches on WORD
    BOUNDARIES, not raw substrings — "war" no longer matches "warsaw"/"forward",
    so a thematic slice isn't polluted by incidental substrings. A multi-word
    term ("iran war") still matches as a bounded phrase. Bounded scan; the engine
    wall-clock budget still applies."""
    text = str((ctx.get("signal") or {}).get("text") or "").casefold()

    def contains_any(terms: object) -> bool:
        if not text:
            return False
        for t in _as_list(terms):
            if isinstance(t, str) and t and _word_boundary_re(t.casefold()).search(text):
                return True
        return False

    return contains_any


def _severity_at_least(ctx: dict[str, Any]) -> Callable[[str], bool]:
    """Compare a signal/event's severity against the supplied floor.

    Looks up ``signal.severity`` then ``event.severity`` then
    ``event.payload.severity`` then ``signal.classification_scores.severity``
    — first one found wins.
    """
    candidates: list[Any] = []
    sig = ctx.get("signal") or {}
    candidates.append(sig.get("severity"))
    evt = ctx.get("event") or {}
    candidates.append(evt.get("severity"))
    payload = evt.get("payload") or {}
    if isinstance(payload, dict):
        candidates.append(payload.get("severity"))
    cs = sig.get("classification_scores") or {}
    if isinstance(cs, dict):
        candidates.append(cs.get("severity"))

    observed_rank = -1
    for c in candidates:
        r = _severity_rank(c)
        if r > observed_rank:
            observed_rank = r

    def severity_at_least(level: object) -> bool:
        if not isinstance(level, str):
            return False
        floor = _severity_rank(level)
        if floor < 0:
            return False
        return observed_rank >= floor

    return severity_at_least


def _recent(ctx: dict[str, Any]) -> Callable[[int], bool]:
    ts = _parse_ts(
        (ctx.get("signal") or {}).get("event_timestamp")
        or (ctx.get("signal") or {}).get("ingested_at")
        or (ctx.get("event") or {}).get("timestamp")
    )

    def recent(days: object) -> bool:
        if not isinstance(days, (int, float)) or days <= 0 or ts is None:
            return False
        delta = (_now_utc() - ts).total_seconds()
        return 0 <= delta <= float(days) * 86400.0

    return recent


def _signal_age_hours(ctx: dict[str, Any]) -> Callable[[], float]:
    ts = _parse_ts(
        (ctx.get("signal") or {}).get("event_timestamp")
        or (ctx.get("signal") or {}).get("ingested_at")
    )

    def signal_age_hours() -> float:
        if ts is None:
            return 0.0
        return max(0.0, (_now_utc() - ts).total_seconds() / 3600.0)

    return signal_age_hours


def _credibility(ctx: dict[str, Any]) -> Callable[[], float]:
    """Spec §2: pre-computed when ctx is built. We just hand back the float."""
    cred = _as_float((ctx.get("signal") or {}).get("credibility"))

    def credibility() -> float:
        return cred

    return credibility


def _entity_class_in(ctx: dict[str, Any]) -> Callable[[Iterable[str]], bool]:
    classes = _as_set((ctx.get("signal") or {}).get("entity_classes"))

    def entity_class_in(allowed: object) -> bool:
        for c in _as_list(allowed):
            if isinstance(c, str) and c in classes:
                return True
        return False

    return entity_class_in


# ---------------------------------------------------------------------------
# Descriptor-scoped helpers (analyst.subscription.targets.predicate)
# ---------------------------------------------------------------------------


def _scope_geo(ctx: dict[str, Any]) -> Callable[[], list[str]]:
    geo = _as_list((ctx.get("target") or {}).get("scope_geo"))

    def scope_geo() -> list[str]:
        return [g for g in geo if isinstance(g, str)]

    return scope_geo


def _scope_entity_classes(ctx: dict[str, Any]) -> Callable[[], list[str]]:
    ecs = _as_list((ctx.get("target") or {}).get("scope_entity_classes"))

    def scope_entity_classes() -> list[str]:
        return [e for e in ecs if isinstance(e, str)]

    return scope_entity_classes


def _target_id(ctx: dict[str, Any]) -> Callable[[], str]:
    tid = _as_str((ctx.get("target") or {}).get("id"))

    def target_id() -> str:
        return tid

    return target_id


def _target_kind(ctx: dict[str, Any]) -> Callable[[], str]:
    """For target descriptors the "kind" is typically derived from the schema
    URI or a `kind` tag — fall back to ``""`` when not pre-extracted."""
    k = _as_str((ctx.get("target") or {}).get("kind"))

    def target_kind() -> str:
        return k

    return target_kind


def _abstraction_level(ctx: dict[str, Any]) -> Callable[[], str]:
    lvl = _as_str((ctx.get("target") or {}).get("abstraction_level"))

    def abstraction_level() -> str:
        return lvl

    return abstraction_level


# ---------------------------------------------------------------------------
# Event-scoped helpers (analyst.cadence.trigger)
# ---------------------------------------------------------------------------


def _event_type(ctx: dict[str, Any]) -> Callable[[], str]:
    et = _as_str((ctx.get("event") or {}).get("type"))

    def event_type() -> str:
        return et

    return event_type


def _event_payload_get(ctx: dict[str, Any]) -> Callable[[str], Any]:
    payload = (ctx.get("event") or {}).get("payload") or {}

    def event_payload_get(path: object) -> Any:
        if not isinstance(path, str) or not path:
            return None
        cur: Any = payload
        # Dot-path walk: "a.b.c" → payload["a"]["b"]["c"]; missing or non-dict
        # step returns None. We avoid raising into Starlark so the helper is
        # total per spec §2.
        for segment in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(segment)
            else:
                return None
            if cur is None:
                return None
        return cur

    return event_payload_get


# ---------------------------------------------------------------------------
# Helper catalog metadata
# ---------------------------------------------------------------------------
#
# Each entry maps the Starlark-visible name to (factory, surfaces, requires).
# The factory takes the runtime ctx and returns the bound callable. Surfaces
# is the set of binding surfaces where the helper is permitted; the compiler
# uses this for registration-time surface validation per spec §6.
#
# ``requires`` is the helper's REQUIRED-CTX declaration (G4): a tuple of
# alternative requirement GROUPS, each group a tuple of dot-paths into the
# evaluation ctx. The helper can produce a meaningful (non-default,
# non-never-match) verdict iff at least ONE group is fully provided by the
# evaluating call site. The compiler checks these against the surface's
# ctx-builder contract (``SURFACE_CTX_CONTRACTS`` below) at compile time, so
# a helper that no production ctx-builder can feed is a COMPILE ERROR rather
# than a predicate that silently never matches.

# (factory, surfaces, requires) — requires is OR-of-AND groups of ctx paths.
_CatalogEntry = tuple[
    Callable[[dict[str, Any]], Callable[..., Any]],
    tuple[str, ...],
    tuple[tuple[str, ...], ...],
]

HELPER_CATALOG: dict[str, _CatalogEntry] = {
    # --- signal-scoped (target.scope + source.filter) ----------------------
    "mentions":            (_mentions,            (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.entity_classes",),)),
    "mentions_any":        (_mentions_any,        (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.entity_classes",),)),
    # geo_match has three call forms: explicit-codes (needs signal geo only),
    # precomputed (signal.geo_match), and scope-overlap (signal geo × target
    # scope_geo). Satisfiable when ANY form is feedable.
    "geo_match":           (_geo_match,           (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.geo_provenance",),
                             ("signal.geo_match",),
                             ("signal.geo_provenance", "target.scope_geo"))),
    "geo_in":              (_geo_in,              (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.geo_provenance",),)),
    # org_match needs either the precomputed O(1) flag or the org-refs ×
    # watchlist overlap inputs. NO production ctx-builder provides either
    # today, so org_match is compile-refused on every surface until a
    # ctx-builder grows the keys (then: add them to the surface contract).
    "org_match":           (_org_match,           (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.org_match",),
                             ("signal.org_refs", "target.watchlist_orgs"))),
    "recent":              (_recent,              (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER, SURFACE_CADENCE_TRIGGER),
                            (("signal.event_timestamp",),
                             ("signal.ingested_at",),
                             ("event.timestamp",))),
    "signal_age_hours":    (_signal_age_hours,    (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.event_timestamp",),
                             ("signal.ingested_at",))),
    "credibility":         (_credibility,         (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.credibility",),)),
    "entity_class_in":     (_entity_class_in,     (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.entity_classes",),)),
    # --- all-surfaces ------------------------------------------------------
    "has_tag":             (_has_tag,             SURFACE_ALL,
                            (("signal.tags",), ("target.tags",), ("event.tags",))),
    "has_any_tag":         (_has_any_tag,         SURFACE_ALL,
                            (("signal.tags",), ("target.tags",), ("event.tags",))),
    "contains_any":        (_contains_any,        (SURFACE_TARGET_SCOPE, SURFACE_SOURCE_FILTER),
                            (("signal.text",),)),
    # --- mixed (target.scope + cadence.trigger) ----------------------------
    "severity_at_least":   (_severity_at_least,   (SURFACE_TARGET_SCOPE, SURFACE_CADENCE_TRIGGER),
                            (("signal.severity",),
                             ("signal.classification_scores",),
                             ("event.severity",),
                             ("event.payload",))),
    # --- descriptor-scoped (analyst.subscription) --------------------------
    "scope_geo":             (_scope_geo,             (SURFACE_ANALYST_SUBSCRIPTION,),
                              (("target.scope_geo",),)),
    "scope_entity_classes":  (_scope_entity_classes,  (SURFACE_ANALYST_SUBSCRIPTION,),
                              (("target.scope_entity_classes",),)),
    "target_id":             (_target_id,             (SURFACE_ANALYST_SUBSCRIPTION,),
                              (("target.id",),)),
    "target_kind":           (_target_kind,           (SURFACE_ANALYST_SUBSCRIPTION,),
                              (("target.kind",),)),
    "abstraction_level":     (_abstraction_level,     (SURFACE_ANALYST_SUBSCRIPTION,),
                              (("target.abstraction_level",),)),
    # --- event-scoped (analyst.cadence.trigger) ----------------------------
    "event_type":          (_event_type,          (SURFACE_CADENCE_TRIGGER,),
                            (("event.type",),)),
    "event_payload_get":   (_event_payload_get,   (SURFACE_CADENCE_TRIGGER,),
                            (("event.payload",),)),
}

# Order-stable iteration name list — used by surface-validation walks
HELPER_NAMES: tuple[str, ...] = tuple(HELPER_CATALOG.keys())


# ---------------------------------------------------------------------------
# Surface ctx-builder contracts (G4)
# ---------------------------------------------------------------------------
#
# What each surface's PRODUCTION ctx-builders actually provide, declared as
# dot-paths matching the ``requires`` declarations above. This is the
# compile-time authority: a predicate referencing a helper whose every
# requirement group falls outside the surface contract is rejected at
# registration (PredicateCompilationError), because at evaluation time it
# could only ever return its conservative default — the silent-never-match
# divergence class this module kills.
#
# KEEP IN SYNC with the builders (each is named below). The sync is enforced
# mechanically: ``runtime/subscription/filter.py`` self-checks its built ctx
# against this contract at import time, and tests/runtime/
# test_predicate_ctx_parity.py asserts the others.
#
# A surface with an EMPTY contract has no production evaluator feeding helper
# ctx at all — every helper reference there fails loud at compile time
# instead of registering a predicate that never runs / never matches.
# Call sites (and test rigs) that provide a different ctx can pass their own
# contract via ``compile_predicate(..., ctx_contract=...)``.

SURFACE_CTX_CONTRACTS: dict[str, frozenset[str]] = {
    # Union of the surface's two production ctx-builders:
    #   * legba.runtime.subscription.filter._signal_residual_ctx (signal.*)
    #   * legba.data.analysts.agency.resolution._is_applicable    (target.*)
    SURFACE_TARGET_SCOPE: frozenset({
        "signal.entity_classes",
        "signal.tags",
        "signal.geo_provenance",
        "signal.credibility",
        "signal.language",
        "signal.modality",
        "signal.event_timestamp",
        "signal.ingested_at",
        "signal.severity",
        "signal.classification_scores",
        "signal.text",
        "target.id",
        "target.scope_geo",
        "target.scope_entity_classes",
        "target.tags",
        "target.domain",
        "target.abstraction_level",
    }),
    # The only production compiler of source.filter predicates is the
    # discovery relabel engine (legba.data.discovery.relabel), which binds
    # candidate LABELS as bare identifiers — it feeds no helper ctx at all.
    # Helper-bearing source.filter predicates therefore fail at compile time
    # until a real source-filter evaluator lands (declare its keys here).
    SURFACE_SOURCE_FILTER: frozenset(),
    # Three production builders, identical shape:
    #   * legba.runtime.source_first_runtime._analyst_ids_for_target
    #   * legba.runtime.dapr_actors (analyst cadence target selection)
    #   * legba.runtime.subscription.sourceref._residual_match
    SURFACE_ANALYST_SUBSCRIPTION: frozenset({
        "target.id",
        "target.kind",
        "target.scope_geo",
        "target.scope_entity_classes",
        "target.tags",
        "target.abstraction_level",
    }),
    # NO production evaluator exists for analyst.cadence.trigger yet — the
    # schema compiles triggers at registration but nothing ever evaluates
    # them. Empty contract = helper-bearing triggers are refused loudly at
    # registration instead of silently never firing. When the trigger-event
    # evaluator lands, declare its event.* keys here.
    SURFACE_CADENCE_TRIGGER: frozenset(),
}

# Per-call-site contract for action-pack APPLICABILITY predicates: they bind
# on the target.scope surface but are evaluated ONLY against the target-side
# ctx built by legba.data.analysts.agency.resolution._is_applicable (no
# signal in scope). The pack schema validator and the resolver both compile
# against this narrower contract so a signal-helper applicability predicate
# (e.g. ``mentions("x")``) is refused at registration instead of silently
# never gating the pack open.
TARGET_SCOPE_APPLICABILITY_CTX: frozenset[str] = frozenset({
    "target.id",
    "target.scope_geo",
    "target.scope_entity_classes",
    "target.tags",
    "target.domain",
    "target.abstraction_level",
})


def surface_helpers(surface: str) -> set[str]:
    """Return the set of helper names available on the given surface."""
    out: set[str] = set()
    for name, entry in HELPER_CATALOG.items():
        if surface in entry[1]:
            out.add(name)
    return out


def helper_requirements(name: str) -> tuple[tuple[str, ...], ...]:
    """Return the helper's required-ctx groups (OR-of-AND dot-path tuples).

    Tolerates legacy 2-tuple catalog entries (tests monkeypatch those):
    a missing declaration means "no ctx requirement" → never refused.
    """
    entry = HELPER_CATALOG.get(name)
    if entry is None or len(entry) < 3:
        return ()
    return entry[2]


def helper_unsatisfied(name: str, provided: frozenset[str]) -> tuple[tuple[str, ...], ...]:
    """Return () if the helper is satisfiable against ``provided`` ctx keys.

    Otherwise returns the helper's requirement groups (for the error
    message). A helper is satisfiable when at least one requirement group is
    a subset of the provided keys, or when it declares no requirements.
    """
    groups = helper_requirements(name)
    if not groups:
        return ()
    for group in groups:
        if set(group) <= provided:
            return ()
    return groups


# ---------------------------------------------------------------------------
# Bind catalog → module
# ---------------------------------------------------------------------------


def bind_helpers(module: Any, ctx: dict[str, Any], surface: str) -> None:
    """Register the surface-permitted helpers on a starlark.Module instance.

    The compiler builds a fresh Module per evaluation (Modules are
    stateful — see ``compiler.py`` for why); this function does the
    name-by-name ``add_callable`` wiring.
    """
    for name in HELPER_NAMES:
        entry = HELPER_CATALOG[name]
        factory, surfaces = entry[0], entry[1]
        if surface not in surfaces:
            continue
        try:
            bound = factory(ctx)
        except Exception as exc:  # pragma: no cover - factories are total
            raise PredicateRuntimeError(
                f"binding helper {name!r} failed", cause=exc
            ) from exc
        module.add_callable(name, bound)
