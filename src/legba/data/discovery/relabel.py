# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Relabel-rule evaluator — per L-106 §3.

The discovery handler emits :class:`CandidateTarget` instances with a raw
``label_set``. The *registry* (per L-181 / L-182 — not the handler) walks
the descriptor's ``relabel:`` block in declared order and rewrites each
candidate's labels into descriptor template variables.

This module owns the per-rule action dispatch + the chain evaluator. It
is deterministic — given the same candidate + ruleset it always produces
the same materialized labels (or the same ``drop`` decision).

Public entry points:

  * :func:`apply_relabel_rule` — single-rule evaluation.
  * :func:`evaluate_relabel_chain` — full chain; honors short-circuit
    drop semantics so a ``drop`` mid-chain skips subsequent rules.
  * :class:`RelabelResult` — chain return shape: either the rewritten
    label_set, or a ``dropped`` flag with the rule index that caused it.

Action coverage (all 9 per L-106 §3):

  ============ =============================================================
  action       semantics
  ============ =============================================================
  set          Copy ``source_labels[0]`` to ``target_label``.
  set_list     Copy, wrapping scalar into single-element list.
  format       Jinja-ish template; filters lower / upper / slug / trim.
  lookup       Side-table lookup against a named stack lookup table.
  lookup_languages  Convenience: country_iso2 → list of locale codes.
  merge_list   Append a static list (``extend_with``) onto an existing
               list label.
  keep         Filter-in: drop if Starlark predicate evaluates false.
  drop         Filter-out: drop if Starlark predicate evaluates true.
  hash_mod     Shard via ``hash(source_labels) % modulus == eq``.
  ============ =============================================================

The Starlark integration for ``keep`` / ``drop`` is wired through the
existing L-104 :mod:`legba.data.predicates` package (Source-filter
surface; same sandbox + 5 ms wall-clock budget). The ``lookup`` action
optionally accepts an injected lookup-table dict for tests; in production
the registry passes a stack-resolved table.

Adding a new relabel action is a new ``_apply_<name>`` function below + a
registration in :data:`RELABEL_ACTION_HANDLERS`. The descriptor's
``action:`` field is a free string validated against
:data:`RELABEL_ACTIONS` + this registry at materialization time.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping

from ._contract import (
    CandidateTarget,
    RELABEL_ACTIONS,
    RelabelRule,
)


# ---------------------------------------------------------------------------
# RelabelResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelabelResult:
    """Chain-evaluation return.

    ``dropped`` is True iff a ``keep`` or ``drop`` rule short-circuited
    the chain. ``dropped_at`` carries the rule index (0-based) that
    triggered the drop, for observability + DLQ payload. ``labels``
    holds the materialized label_set for callers when the chain
    completed (or partial labels when ``dropped`` is True — useful for
    debugging which intermediate writes happened before the drop).
    """

    labels: Mapping[str, Any]
    dropped: bool = False
    dropped_at: int | None = None
    dropped_by_action: str | None = None
    dropped_reason: str = ""

    @property
    def kept(self) -> bool:
        return not self.dropped


# ---------------------------------------------------------------------------
# Helpers — value lookup + slug + Jinja-ish formatter
# ---------------------------------------------------------------------------


def _resolve_label(
    labels: Mapping[str, Any],
    metadata: Mapping[str, Any],
    name: str,
) -> Any:
    """Look up a label first in the working label_set, then in metadata.

    Dotted paths (``scope.geo``) descend dicts; single-segment names also
    fall through to metadata so rules can lift fields out of the
    discovery's ``source_metadata`` without an explicit copy step.

    Missing keys return ``None`` rather than raising; the per-action
    handlers decide whether ``None`` is a fatal precondition.
    """
    if name in labels:
        return labels[name]
    if "." in name:
        head, _, rest = name.partition(".")
        if head in labels and isinstance(labels[head], Mapping):
            cur: Any = labels[head]
            for seg in rest.split("."):
                if not isinstance(cur, Mapping) or seg not in cur:
                    cur = None
                    break
                cur = cur[seg]
            return cur
    if name in metadata:
        return metadata[name]
    return None


def _write_label(
    labels: MutableMapping[str, Any],
    name: str,
    value: Any,
) -> None:
    """Write a label, descending dotted paths into nested dicts."""
    if "." not in name:
        labels[name] = value
        return
    head, _, rest = name.partition(".")
    if head not in labels or not isinstance(labels[head], MutableMapping):
        labels[head] = {}
    cur: MutableMapping[str, Any] = labels[head]
    parts = rest.split(".")
    for seg in parts[:-1]:
        if seg not in cur or not isinstance(cur[seg], MutableMapping):
            cur[seg] = {}
        cur = cur[seg]
    cur[parts[-1]] = value


_SLUG_NON_WORD = re.compile(r"[^\w\s-]+", re.UNICODE)
_SLUG_WS = re.compile(r"[\s_-]+", re.UNICODE)


def _slug(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _SLUG_NON_WORD.sub("", s)
    s = _SLUG_WS.sub("-", s).strip("-")
    return s.lower()


_FORMAT_TOKEN = re.compile(r"\{\{\s*([^|}]+?)(?:\s*\|\s*([a-zA-Z_]+))?\s*\}\}")
"""Match ``{{ name }}`` or ``{{ name | filter }}`` — no nested expressions,
no multi-filter chains. Mirrors the L-106 §3 example surface."""


_FILTERS: dict[str, Callable[[Any], str]] = {
    "lower": lambda v: ("" if v is None else str(v).lower()),
    "upper": lambda v: ("" if v is None else str(v).upper()),
    "slug": _slug,
    "trim": lambda v: ("" if v is None else str(v).strip()),
}


def _render_format(
    template: str,
    labels: Mapping[str, Any],
    metadata: Mapping[str, Any],
    candidate: CandidateTarget,
) -> str:
    """Render a Jinja-ish template per L-106 §3.

    Supports ``{{ name }}`` and ``{{ name | filter }}`` only. Unknown
    names render as the empty string; unknown filters raise. List values
    render as the first element (matches the L-106 §3 worked example
    where ``scope.geo`` is a one-element list and ``{{ scope.geo[0] |
    lower }}`` is shorthand).
    """
    candidate_labels = dict(labels)
    # Allow rules to reference ``natural_key`` and ``id`` from the candidate
    # without having to copy them via a prior ``set`` rule.
    candidate_labels.setdefault("natural_key", candidate.natural_key)
    candidate_labels.setdefault("id", candidate.natural_key)

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        filt_name = match.group(2)
        # Strip subscript suffixes — the L-106 example uses
        # ``{{ scope.geo[0] | lower }}`` to mean "first element of the list".
        if "[" in name:
            base, _, rest = name.partition("[")
            index_str = rest.rstrip("]").strip()
            val = _resolve_label(candidate_labels, metadata, base.strip())
            if isinstance(val, (list, tuple)) and index_str.isdigit():
                idx = int(index_str)
                val = val[idx] if 0 <= idx < len(val) else None
            else:
                val = None
        else:
            val = _resolve_label(candidate_labels, metadata, name)

        if filt_name:
            if filt_name not in _FILTERS:
                raise ValueError(
                    f"unknown format filter {filt_name!r}; "
                    f"supported: {sorted(_FILTERS)}"
                )
            return _FILTERS[filt_name](val)
        if val is None:
            return ""
        if isinstance(val, (list, tuple)):
            return str(val[0]) if val else ""
        return str(val)

    return _FORMAT_TOKEN.sub(_replace, template)


# ---------------------------------------------------------------------------
# Predicate eval for keep / drop
# ---------------------------------------------------------------------------


def _eval_predicate(
    source: str,
    candidate: CandidateTarget,
    labels: Mapping[str, Any],
    source_labels: list[str],
) -> bool:
    """Evaluate a ``keep`` / ``drop`` predicate.

    Uses the L-104 :mod:`legba.data.predicates` compiler+sandbox bound
    to the ``source.filter`` surface (closest surface match — the rule
    is filtering a candidate, structurally analogous to filtering a
    signal). Falls back to a minimal Python-AST literal-eval-only
    backend if starlark-pyo3 isn't available in the test environment.

    Bound globals:

      * ``value``  — first label in ``source_labels`` (single-shot
        convenience per L-106 §3 worked example: ``value != 'antarctica'``).
      * ``labels`` — full working label set (dict).
      * ``natural_key`` — candidate's stable id.
      * ``meta``   — candidate's source_metadata (Mapping).

    Returns a Python bool. Empty / blank predicate raises ``ValueError``.
    Helper raises propagate as ``ValueError`` so the chain evaluator can
    convert them into a structured drop result with reason metadata.
    """
    if not source or not source.strip():
        raise ValueError("predicate is empty")

    primary = (
        _resolve_label(labels, candidate.source_metadata, source_labels[0])
        if source_labels
        else None
    )
    bindings: dict[str, Any] = {
        "value": primary,
        "labels": dict(labels),
        "natural_key": candidate.natural_key,
        "meta": dict(candidate.source_metadata),
    }
    for label_name in source_labels:
        # Bind each named source_label as a top-level identifier too — the
        # L-106 example uses ``region != 'antarctica'`` without going
        # through ``value``.
        ident = label_name.replace(".", "_")
        if ident.isidentifier():
            bindings[ident] = _resolve_label(
                labels, candidate.source_metadata, label_name
            )

    try:
        from legba.data.predicates import PredicateSurface, compile_predicate
    except Exception:                                            # pragma: no cover
        compile_predicate = None  # type: ignore[assignment]

    if compile_predicate is not None:
        try:
            compiled = compile_predicate(source, PredicateSurface.SOURCE_FILTER)
        except Exception as exc:                                 # pragma: no cover
            # Fall through to the safe-eval path; the Starlark sandbox is
            # the strict surface, but tests routinely run with a partial
            # helper catalog. The fallback preserves rule semantics for
            # the worked-example predicate set.
            return _safe_python_eval(source, bindings, exc)
        try:
            return bool(compiled.evaluate(bindings))
        except Exception as exc:                                 # pragma: no cover
            return _safe_python_eval(source, bindings, exc)

    return _safe_python_eval(source, bindings, None)


def _safe_python_eval(
    source: str,
    bindings: dict[str, Any],
    original_exc: Exception | None,
) -> bool:
    """Minimal AST-restricted Python eval for relabel predicates.

    Only supports comparisons, boolean ops, ``not``, names, constants,
    attribute access, subscript, ``in`` / ``not in``. No calls, no
    statements, no imports — same shape as the L-106 worked-example
    predicates (``value != 'antarctica'``, ``population > 1000000``,
    ``'en' in languages``).
    """
    import ast

    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"predicate parse failed: {exc}") from exc

    allowed_nodes: tuple[type[ast.AST], ...] = (
        ast.Expression,
        ast.BoolOp, ast.And, ast.Or,
        ast.UnaryOp, ast.Not,
        ast.Compare,
        ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
        ast.In, ast.NotIn, ast.Is, ast.IsNot,
        ast.Name, ast.Load, ast.Constant,
        ast.List, ast.Tuple, ast.Set, ast.Dict,
        ast.Attribute, ast.Subscript, ast.Index, ast.Slice,
        ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError(
                f"predicate uses banned construct: "
                f"{type(node).__name__} not allowed (predicate source: {source!r})"
            )
        if isinstance(node, ast.Name) and node.id not in bindings:
            raise ValueError(
                f"predicate references unknown name {node.id!r}; "
                f"available: {sorted(bindings)}"
            )

    code = compile(tree, "<relabel-predicate>", "eval")
    # Suppress builtins so even the fallback can't call int(), etc.
    return bool(eval(code, {"__builtins__": {}}, bindings))  # noqa: S307


# ---------------------------------------------------------------------------
# Per-action handlers
# ---------------------------------------------------------------------------


@dataclass
class _ActionCtx:
    """Mutable per-rule evaluation state passed to handlers."""

    candidate: CandidateTarget
    labels: dict[str, Any]
    lookup_tables: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    rule_index: int = 0


def _apply_set(rule: RelabelRule, ctx: _ActionCtx) -> None:
    if not rule.target_label:
        raise ValueError("'set' action requires target_label")
    if not rule.source_labels:
        raise ValueError("'set' action requires at least one source_labels entry")
    val = _resolve_label(ctx.labels, ctx.candidate.source_metadata, rule.source_labels[0])
    _write_label(ctx.labels, rule.target_label, val)


def _apply_set_list(rule: RelabelRule, ctx: _ActionCtx) -> None:
    if not rule.target_label:
        raise ValueError("'set_list' action requires target_label")
    if not rule.source_labels:
        raise ValueError("'set_list' action requires source_labels[0]")
    val = _resolve_label(ctx.labels, ctx.candidate.source_metadata, rule.source_labels[0])
    if val is None:
        wrapped: list[Any] = []
    elif isinstance(val, (list, tuple)):
        wrapped = list(val)
    else:
        wrapped = [val]
    _write_label(ctx.labels, rule.target_label, wrapped)


def _apply_format(rule: RelabelRule, ctx: _ActionCtx) -> None:
    if not rule.target_label:
        raise ValueError("'format' action requires target_label")
    if rule.replacement is None:
        raise ValueError("'format' action requires replacement")
    rendered = _render_format(
        rule.replacement, ctx.labels, ctx.candidate.source_metadata, ctx.candidate
    )
    _write_label(ctx.labels, rule.target_label, rendered)


def _apply_lookup(rule: RelabelRule, ctx: _ActionCtx) -> None:
    if not rule.target_label:
        raise ValueError("'lookup' action requires target_label")
    if not rule.source_labels:
        raise ValueError("'lookup' action requires source_labels[0]")
    if not rule.table:
        raise ValueError("'lookup' action requires table (lookup-table name)")
    key = _resolve_label(ctx.labels, ctx.candidate.source_metadata, rule.source_labels[0])
    if key is None:
        _write_label(ctx.labels, rule.target_label, rule.fallback)
        return
    table = ctx.lookup_tables.get(rule.table)
    if table is None:
        # Lookup table not present in this eval context — write the
        # fallback rather than raising. Production materialization injects
        # the stack-resolved table; tests pass a dict explicitly.
        _write_label(ctx.labels, rule.target_label, rule.fallback)
        return
    val = table.get(key, rule.fallback)
    _write_label(ctx.labels, rule.target_label, val)


_DEFAULT_COUNTRY_LANGUAGES: dict[str, list[str]] = {
    # Tiny built-in table covering the worked-example country codes.
    # Production materialization passes the real ISO-3166 → BCP-47 table
    # via a stack-resolved lookup; this seed keeps unit tests independent
    # of the stack registry.
    "BR": ["pt-BR"],
    "AR": ["es-AR"],
    "MX": ["es-MX"],
    "US": ["en-US"],
    "FR": ["fr-FR"],
    "DE": ["de-DE"],
    "JP": ["ja-JP"],
    "IN": ["hi-IN", "en-IN"],
    "ZA": ["en-ZA", "af-ZA", "zu-ZA"],
    "ES": ["es-ES"],
    "PT": ["pt-PT"],
    "CN": ["zh-CN"],
    "RU": ["ru-RU"],
}


def _apply_lookup_languages(rule: RelabelRule, ctx: _ActionCtx) -> None:
    if not rule.target_label:
        raise ValueError("'lookup_languages' action requires target_label")
    if not rule.source_labels:
        raise ValueError("'lookup_languages' action requires source_labels[0]")

    key = _resolve_label(ctx.labels, ctx.candidate.source_metadata, rule.source_labels[0])
    table = ctx.lookup_tables.get("languages_by_country") or _DEFAULT_COUNTRY_LANGUAGES

    fallback_value: list[Any]
    # If the rule lists a second source_labels entry, it carries the
    # already-emitted language list (handler-provided) — fall back to that
    # when the lookup misses.
    if len(rule.source_labels) > 1:
        fallback_candidate = _resolve_label(
            ctx.labels, ctx.candidate.source_metadata, rule.source_labels[1]
        )
        if isinstance(fallback_candidate, list):
            fallback_value = list(fallback_candidate)
        elif fallback_candidate is None:
            fallback_value = []
        else:
            fallback_value = [fallback_candidate]
    else:
        fb = rule.fallback
        if isinstance(fb, list):
            fallback_value = list(fb)
        elif fb is None:
            fallback_value = []
        else:
            fallback_value = [fb]

    if key is None:
        _write_label(ctx.labels, rule.target_label, fallback_value)
        return

    val = table.get(key)
    if val is None:
        _write_label(ctx.labels, rule.target_label, fallback_value)
        return
    if isinstance(val, (list, tuple)):
        _write_label(ctx.labels, rule.target_label, list(val))
    else:
        _write_label(ctx.labels, rule.target_label, [val])


def _apply_merge_list(rule: RelabelRule, ctx: _ActionCtx) -> None:
    if not rule.target_label:
        raise ValueError("'merge_list' action requires target_label")
    cur = _resolve_label(ctx.labels, ctx.candidate.source_metadata, rule.target_label)
    if cur is None:
        cur_list: list[Any] = []
    elif isinstance(cur, list):
        cur_list = list(cur)
    elif isinstance(cur, (tuple, set)):
        cur_list = list(cur)
    else:
        cur_list = [cur]
    # Dedupe while preserving order.
    seen = set(repr(x) for x in cur_list)
    for item in rule.extend_with:
        key = repr(item)
        if key in seen:
            continue
        cur_list.append(item)
        seen.add(key)
    _write_label(ctx.labels, rule.target_label, cur_list)


def _apply_keep(rule: RelabelRule, ctx: _ActionCtx) -> bool:
    """Return True iff the candidate should be *kept*. False means drop."""
    if not rule.predicate:
        raise ValueError("'keep' action requires predicate")
    return bool(
        _eval_predicate(rule.predicate, ctx.candidate, ctx.labels, rule.source_labels)
    )


def _apply_drop(rule: RelabelRule, ctx: _ActionCtx) -> bool:
    """Return True iff the candidate should be *dropped*."""
    if not rule.predicate:
        raise ValueError("'drop' action requires predicate")
    return bool(
        _eval_predicate(rule.predicate, ctx.candidate, ctx.labels, rule.source_labels)
    )


def _apply_hash_mod(rule: RelabelRule, ctx: _ActionCtx) -> bool:
    """Return True iff the candidate's source_labels hash equals ``eq``
    modulo ``modulus`` — i.e., this shard wants this candidate.

    False means drop (the candidate belongs to a different shard).
    """
    if rule.modulus is None or rule.modulus <= 0:
        raise ValueError("'hash_mod' action requires modulus > 0")
    if rule.eq is None:
        raise ValueError("'hash_mod' action requires eq")
    if not rule.source_labels:
        raise ValueError("'hash_mod' action requires source_labels")
    # Stable hash across processes — Python's built-in hash() is salted
    # per-process. Use sha256(canonical-bytes) % modulus.
    parts = [
        str(_resolve_label(ctx.labels, ctx.candidate.source_metadata, name))
        for name in rule.source_labels
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    h = int.from_bytes(digest[:8], "big", signed=False)
    return (h % rule.modulus) == rule.eq


# ---------------------------------------------------------------------------
# Action registry (extensible)
# ---------------------------------------------------------------------------


RELABEL_ACTION_HANDLERS: dict[str, Callable[[RelabelRule, _ActionCtx], Any]] = {
    "set": _apply_set,
    "set_list": _apply_set_list,
    "format": _apply_format,
    "lookup": _apply_lookup,
    "lookup_languages": _apply_lookup_languages,
    "merge_list": _apply_merge_list,
    "keep": _apply_keep,
    "drop": _apply_drop,
    "hash_mod": _apply_hash_mod,
}
"""Per-action dispatch table. Extension actions register here as a new
key. Rules with an action not in this table raise at evaluation time —
the schema-level closed set is :data:`legba.data.discovery._contract.RELABEL_ACTIONS`,
runtime extension is via this table (and a NATS-published vocabulary
event if the operator wants the schema validator to accept the new name).
"""


_FILTER_ACTIONS: frozenset[str] = frozenset(["keep", "drop", "hash_mod"])
"""Actions whose handler returns a bool deciding drop-vs-keep instead of
mutating labels."""


# ---------------------------------------------------------------------------
# Single-rule evaluator
# ---------------------------------------------------------------------------


def apply_relabel_rule(
    rule: RelabelRule,
    candidate: CandidateTarget,
    labels: MutableMapping[str, Any] | None = None,
    *,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
    rule_index: int = 0,
) -> tuple[dict[str, Any], bool, str]:
    """Apply one rule to a working label_set.

    Returns ``(labels, dropped, reason)``:

      * ``labels`` — the new working label_set (a fresh dict; the input
        is not mutated). Always populated even on drop so callers can
        inspect what intermediate writes happened.
      * ``dropped`` — True iff a ``keep`` / ``drop`` / ``hash_mod``
        action filtered this candidate out.
      * ``reason`` — human-readable reason for the drop (predicate text,
        hash-mod miss, etc.). Empty when not dropped.

    Raises ``ValueError`` if the rule's action is unknown or required
    fields are missing. The caller (chain evaluator + the registry) is
    responsible for routing unrecoverable rule errors to the DLQ.
    """
    if labels is None:
        labels = dict(candidate.label_set)
    working = dict(labels)
    ctx = _ActionCtx(
        candidate=candidate,
        labels=working,
        lookup_tables=lookup_tables or {},
        rule_index=rule_index,
    )

    if rule.action not in RELABEL_ACTION_HANDLERS:
        if rule.action in RELABEL_ACTIONS:
            # Action is in the schema-closed set but not in the runtime
            # dispatch table — should never happen because we own both,
            # but the message is precise for the operator.
            raise ValueError(
                f"relabel action {rule.action!r} declared in RELABEL_ACTIONS "
                f"but no handler is registered in RELABEL_ACTION_HANDLERS"
            )
        raise ValueError(
            f"unknown relabel action {rule.action!r}; "
            f"known actions: {sorted(RELABEL_ACTION_HANDLERS)}"
        )

    handler = RELABEL_ACTION_HANDLERS[rule.action]

    if rule.action == "keep":
        kept = bool(handler(rule, ctx))
        if not kept:
            return (
                working,
                True,
                f"'keep' predicate evaluated false: {rule.predicate!r}",
            )
        return (working, False, "")

    if rule.action == "drop":
        dropped = bool(handler(rule, ctx))
        if dropped:
            return (
                working,
                True,
                f"'drop' predicate evaluated true: {rule.predicate!r}",
            )
        return (working, False, "")

    if rule.action == "hash_mod":
        kept = bool(handler(rule, ctx))
        if not kept:
            return (
                working,
                True,
                f"'hash_mod' shard miss (modulus={rule.modulus}, eq={rule.eq})",
            )
        return (working, False, "")

    # Mutating action — the handler writes into ctx.labels in place.
    handler(rule, ctx)
    return (working, False, "")


# ---------------------------------------------------------------------------
# Chain evaluator
# ---------------------------------------------------------------------------


def evaluate_relabel_chain(
    candidate: CandidateTarget,
    rules: list[RelabelRule],
    *,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
) -> RelabelResult:
    """Apply a relabel chain in declared order; short-circuit on drop.

    Per L-106 §3 each rule sees the *output* of the previous rule, not
    the original candidate's label_set. The initial working set is the
    candidate's ``label_set``; rules mutate it via ``target_label``
    writes; ``keep`` / ``drop`` / ``hash_mod`` short-circuit.

    Returns a :class:`RelabelResult` with the final labels and the drop
    decision. The materialization caller (L-181 / L-182's registry-side
    expander) substitutes the labels into the template body to produce a
    full target descriptor instance.
    """
    working: dict[str, Any] = dict(candidate.label_set)
    for idx, rule in enumerate(rules):
        working, dropped, reason = apply_relabel_rule(
            rule,
            candidate,
            working,
            lookup_tables=lookup_tables,
            rule_index=idx,
        )
        if dropped:
            return RelabelResult(
                labels=working,
                dropped=True,
                dropped_at=idx,
                dropped_by_action=rule.action,
                dropped_reason=reason,
            )

    return RelabelResult(labels=working, dropped=False)


__all__ = [
    "RELABEL_ACTION_HANDLERS",
    "RelabelResult",
    "apply_relabel_rule",
    "evaluate_relabel_chain",
]
