# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round spec: typed models and the loader.

A spec is configuration, not a program. It has seats (who does a job, what
each one ``reads`` — default deny), one flow tree of bounded control shapes,
budgets counted in calls and seconds (never tokens: house rule), and what it
publishes. :func:`load_spec` reads the YAML/JSON a descriptor body would hold
and returns either a :class:`RoundSpec` or the list of :class:`Refusal` s that
stopped it; :mod:`.validate` then applies the refusal rules V1-V12.

Shapes are parsed by hand (not by a discriminated union) so that a missing
bound is reported as the named reason ``bound_missing`` at the node's path,
not as a generic schema error. Node parameters (``id``, ``deadline_s``, …)
may sit beside the shape key or inside the shape's mapping; the worked specs
use both placements.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .refs import GrammarError, Predicate, Ref, is_ident, parse_predicate, parse_ref

# ---------------------------------------------------------------------------
# Caps (design §2.4/§2.5) — the validator refuses a bound above these
# ---------------------------------------------------------------------------

CAP_WALL_S = 7200
CAP_MODEL_CALLS = 400
CAP_MAX_PARALLEL = 6
CAP_SEAT_DEADLINE_S = 900
CAP_RETRIES = 2
CAP_REBUTTAL_PASSES = 3
CAP_DEBATE_TURNS = 12
CAP_LOOP_ITERATIONS = 8
CAP_MAP_ITEMS = 12
CAP_MAP_CONCURRENCY = 4
#: Not in the design: a phase deadline may not exceed the round's wall cap.
CAP_PHASE_DEADLINE_S = CAP_WALL_S

#: Seat defaults where the design's worked specs abridge with "…" (§2.6).
DEFAULT_DEADLINE_S = {"model": 300, "judge": 300, "code": 60, "human": 900}

SHAPES = ("seat", "seq", "par", "blind", "rebuttal", "debate", "panel", "select",
          "branch", "loop", "map", "escalate", "use_round")
#: Parameters a node may carry beside (or inside) its shape key.
NODE_PARAMS = ("id", "deadline_s", "min_present", "join", "model_calls", "per", "blind")


@dataclass(frozen=True)
class Refusal:
    """One reason a spec is refused: the rule (``V1``…``V12`` or ``schema``),
    a stable reason name, the path in the spec, and a plain sentence."""

    rule: str
    reason: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "reason": self.reason, "path": self.path,
                "message": self.message}

    def __str__(self) -> str:
        return f"[{self.rule} {self.reason}] {self.path}: {self.message}"


class SpecRefused(ValueError):
    """Raised by :func:`require_spec` when a spec does not load or validate."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("; ".join(str(r) for r in refusals) or "spec refused")


# ---------------------------------------------------------------------------
# Seats, reads, inputs, budgets (pydantic: closed vocabularies, extra=forbid)
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class InputDecl(_Model):
    type: Literal["subject_ref", "text", "timestamp", "id_list"]
    optional: bool = False


class ReadSpec(_Model):
    from_: str = Field(alias="from")
    view: Literal["rows", "sentence_table", "prose"] = "rows"
    when: Literal["sealed"] = "sealed"

    @property
    def ref(self) -> Ref:
        return parse_ref(self.from_)


class CardFromItem(_Model):
    from_item: str


class FamilyRule(_Model):
    differs_from: tuple[str, ...]


class Seat(_Model):
    kind: Literal["model", "code", "judge", "human"]
    reads: tuple[ReadSpec, ...]
    output: str
    card: Union[str, CardFromItem, None] = None
    fn: str | None = None
    withheld: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()
    thinking: Literal["on", "off"] | None = None
    plane: str | None = None
    family_rule: Union[Literal["any"], FamilyRule] = "any"
    deadline_s: int | None = None
    retries: int = 0
    on_failure: Literal["absent", "stop_round", "escalate"] = "absent"
    #: What a RETRY is handed. ``none``: a clean retry. ``reasons``:
    #: the retry also gets the refused attempt's typed failure — the gates'
    #: named reasons per sentence, or the parse error — and that attempt's own
    #: output, so the SAME seat repairs its own reply. Never another seat's.
    repair: Literal["none", "reasons"] = "none"
    role: Literal["contributor", "admin"] | None = None
    on_timeout: Literal["absent", "stop_round"] | None = None

    @property
    def calls_model(self) -> bool:
        return self.kind in ("model", "judge")

    @property
    def effective_deadline_s(self) -> int:
        return self.deadline_s if self.deadline_s is not None else DEFAULT_DEADLINE_S[self.kind]

    @property
    def differs_from(self) -> tuple[str, ...]:
        return self.family_rule.differs_from if isinstance(self.family_rule, FamilyRule) else ()


class Budgets(_Model):
    wall_s: int
    model_calls: int
    max_parallel: int = 4


# ---------------------------------------------------------------------------
# The flow tree (frozen dataclasses; ``loc`` is the path in the spec)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeatLeaf:
    seat: str
    loc: str
    id: str | None = None


@dataclass(frozen=True)
class Seq:
    id: str | None
    children: tuple[Node, ...]
    loc: str


@dataclass(frozen=True)
class Join:
    mode: Literal["all", "quorum", "first"]
    k: int | None = None


@dataclass(frozen=True)
class Par:
    id: str | None
    children: tuple[Node, ...]
    join: Join
    deadline_s: int | None
    loc: str


@dataclass(frozen=True)
class Blind:
    id: str | None
    children: tuple[Node, ...]
    deadline_s: int | None
    min_present: int | None
    loc: str


@dataclass(frozen=True)
class Rebuttal:
    id: str | None
    of: str
    passes: int | None
    deadline_s: int | None
    also: tuple[ReadSpec, ...]
    loc: str


@dataclass(frozen=True)
class Debate:
    id: str | None
    seats: tuple[str, ...]
    order: Literal["round_robin", "fixed"]
    max_turns: int | None
    settle_by: str | None
    model_calls: int | None
    deadline_s: int | None
    loc: str


@dataclass(frozen=True)
class Tally:
    rule: Literal["quorum", "borda", "majority"]
    k: int | None = None
    of: int | None = None
    threshold: float | None = None


@dataclass(frozen=True)
class Panel:
    id: str | None
    judges: tuple[str, ...]
    on: str
    tally: Tally
    gates_first: tuple[str, ...]
    per: Literal["judge", "candidate"]
    deadline_s: int | None
    loc: str
    max_candidates: int | None = None


@dataclass(frozen=True)
class Select:
    id: str | None
    from_: str
    by: str
    keep_top: int | None
    keep_stands: bool
    stages: int
    loc: str


@dataclass(frozen=True)
class Case:
    when: Predicate
    do: Node


@dataclass(frozen=True)
class Branch:
    id: str | None
    cases: tuple[Case, ...]
    default: Node | None
    loc: str


@dataclass(frozen=True)
class Loop:
    id: str | None
    body: Node
    until: Predicate | None
    max_iterations: int | None
    model_calls: int | None
    loc: str


@dataclass(frozen=True)
class Map:
    id: str | None
    over: Ref
    as_: str
    body: Node
    max_items: int | None
    concurrency: int | None
    deadline_s: int | None
    blind: bool
    model_calls: int | None
    loc: str


@dataclass(frozen=True)
class Escalate:
    id: str | None
    try_: Node
    to: str
    when: Predicate
    loc: str


@dataclass(frozen=True)
class UseRound:
    id: str | None
    spec: str
    version: int | None
    loc: str


Node = Union[SeatLeaf, Seq, Par, Blind, Rebuttal, Debate, Panel, Select, Branch, Loop, Map,
             Escalate, UseRound]
PHASE_TYPES = (Seq, Par, Blind, Rebuttal, Debate, Panel, Select, Branch, Loop, Map, Escalate,
               UseRound)


def children_of(node: Node) -> tuple[Node, ...]:
    """The nested flow nodes of ``node`` (seats named inside panels/debates
    are not nodes; :mod:`.tree` places them)."""
    if isinstance(node, (Seq, Par, Blind)):
        return node.children
    if isinstance(node, Branch):
        out = tuple(c.do for c in node.cases)
        return out + ((node.default,) if node.default is not None else ())
    if isinstance(node, Loop):
        return (node.body,)
    if isinstance(node, Map):
        return (node.body,)
    if isinstance(node, Escalate):
        return (node.try_,)
    return ()


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in children_of(node):
        yield from walk(child)


@dataclass(frozen=True)
class RoundSpec:
    """A loaded spec. ``seats`` are resolved (``seat_ref`` merged)."""

    id: str
    version: int
    inputs: Mapping[str, InputDecl]
    seats: Mapping[str, Seat]
    flow: Node
    budgets: Budgets
    publishes: Literal["none", "shadow", "reader"]
    about: str = ""
    families_available: tuple[str, ...] = ()
    prior_runs: str = "none"
    sha256: str = ""
    source: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def label(self) -> str:
        return f"{self.id}/{self.version}"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.refusals: list[Refusal] = []

    def add(self, rule: str, reason: str, path: str, message: str) -> None:
        self.refusals.append(Refusal(rule, reason, path, message))


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def spec_sha256(raw: Mapping[str, Any]) -> str:
    """The content hash stamped on every row a run writes."""
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


def _scan_token_keys(obj: Any, path: str, col: _Collector) -> None:
    """House rule: no token caps. Any KEY naming tokens is refused."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if isinstance(k, str) and "token" in k.lower():
                col.add("V9", "token_budget_refused", here,
                        "budgets count calls and seconds only; no token caps (house rule)")
            _scan_token_keys(v, here, col)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_token_keys(v, f"{path}[{i}]", col)


def _int_or_none(value: Any, path: str, name: str, col: _Collector) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        col.add("schema", "schema_invalid", f"{path}.{name}", f"{name} must be an integer")
        return None
    return value


def _node_params(obj: Mapping[str, Any], shape: str, path: str,
                 col: _Collector) -> tuple[dict[str, Any], Any]:
    """Split ``obj`` into (params, shape body), merging params found inside a
    mapping-valued shape body. A parameter set in both places is refused."""
    params: dict[str, Any] = {}
    body = obj[shape]
    for k, v in obj.items():
        if k == shape:
            continue
        if k not in NODE_PARAMS:
            col.add("schema", "schema_invalid", f"{path}.{k}", f"unknown node key {k!r}")
            continue
        params[k] = v
    if isinstance(body, Mapping):
        inner = dict(body)
        for k in NODE_PARAMS:
            if k in inner:
                if k in params:
                    col.add("schema", "schema_invalid", f"{path}.{k}",
                            f"{k!r} is set both beside and inside {shape!r}")
                params[k] = inner.pop(k)
        body = inner
    return params, body


def _phase_id(params: Mapping[str, Any], shape: str, path: str, col: _Collector) -> str | None:
    pid = params.get("id")
    if pid is None:
        col.add("schema", "phase_id_missing", path, f"every {shape!r} shape needs an id")
        return None
    if not isinstance(pid, str) or not is_ident(pid):
        col.add("schema", "schema_invalid", f"{path}.id", f"phase id {pid!r} is not a slug")
        return None
    return pid


def _pred(raw: Any, path: str, col: _Collector) -> Predicate | None:
    if not isinstance(raw, str):
        col.add("schema", "predicate_invalid", path, "a predicate is one closed-form string")
        return None
    try:
        return parse_predicate(raw)
    except GrammarError as exc:
        col.add("schema", "predicate_invalid", path, str(exc))
        return None


def _ref(raw: Any, path: str, col: _Collector) -> Ref | None:
    if not isinstance(raw, str):
        col.add("schema", "reference_invalid", path, "a reference is one closed-form string")
        return None
    try:
        return parse_ref(raw)
    except GrammarError as exc:
        col.add("schema", "reference_invalid", path, str(exc))
        return None


def _children(body: Any, path: str, shape: str, col: _Collector) -> tuple[Node, ...]:
    if not isinstance(body, list) or not body:
        col.add("schema", "schema_invalid", path, f"{shape!r} takes a non-empty list of nodes")
        return ()
    out = []
    for i, child in enumerate(body):
        node = parse_node(child, f"{path}[{i}]", col)
        if node is not None:
            out.append(node)
    return tuple(out)


def _join(raw: Any, path: str, col: _Collector) -> Join:
    if raw is None or raw == "all":
        return Join("all")
    if raw == "first":
        return Join("first")
    if isinstance(raw, Mapping) and set(raw) == {"quorum"} and isinstance(raw["quorum"], int):
        return Join("quorum", int(raw["quorum"]))
    col.add("schema", "schema_invalid", f"{path}.join", "join is all | first | {quorum: k}")
    return Join("all")


def _tally(raw: Any, path: str, col: _Collector) -> Tally:
    if raw in ("borda", "majority"):
        return Tally(raw)
    if isinstance(raw, Mapping) and set(raw) == {"quorum"} and isinstance(raw["quorum"], Mapping):
        q = raw["quorum"]
        if set(q) <= {"k", "of", "threshold"} and isinstance(q.get("k"), int) \
                and isinstance(q.get("of"), int):
            thr = q.get("threshold")
            return Tally("quorum", int(q["k"]), int(q["of"]),
                         float(thr) if isinstance(thr, (int, float)) else None)
    col.add("schema", "schema_invalid", f"{path}.tally",
            "tally is majority | borda | {quorum: {k, of, threshold}}")
    return Tally("majority")


def _str_list(raw: Any, path: str, col: _Collector) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        col.add("schema", "schema_invalid", path, "expected a list of ids")
        return ()
    return tuple(raw)


def _reads(raw: Any, path: str, col: _Collector) -> tuple[ReadSpec, ...]:
    if raw is None:
        return ()
    out = []
    for i, r in enumerate(raw if isinstance(raw, list) else [None]):
        try:
            out.append(ReadSpec.model_validate(r))
        except ValidationError as exc:
            col.add("schema", "schema_invalid", f"{path}[{i}]", _first_error(exc))
    return tuple(out)


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{loc}: {err.get('msg')}" if loc else str(err.get("msg"))


def parse_node(obj: Any, path: str, col: _Collector) -> Node | None:  # noqa: C901 - one case per shape
    if not isinstance(obj, Mapping):
        col.add("schema", "schema_invalid", path, "a flow node is a mapping")
        return None
    present = [s for s in SHAPES if s in obj and not (s == "blind" and isinstance(obj[s], bool))]
    if len(present) != 1:
        col.add("schema", "schema_invalid", path,
                f"a node has exactly one shape key of {', '.join(SHAPES)}; found {present}")
        return None
    shape = present[0]
    if shape == "seat":
        extra = set(obj) - {"seat", "id"}
        if extra or not isinstance(obj["seat"], str):
            col.add("schema", "schema_invalid", path, "a seat leaf is {seat: <seat id>}")
            return None
        return SeatLeaf(obj["seat"], path, obj.get("id"))
    params, body = _node_params(obj, shape, path, col)
    pid = _phase_id(params, shape, path, col)
    here = f"{path}.{shape}"
    dl = _int_or_none(params.get("deadline_s"), path, "deadline_s", col)
    mc = _int_or_none(params.get("model_calls"), path, "model_calls", col)
    if shape == "seq":
        return Seq(pid, _children(body, here, shape, col), path)
    if shape == "par":
        return Par(pid, _children(body, here, shape, col), _join(params.get("join"), path, col),
                   dl, path)
    if shape == "blind":
        return Blind(pid, _children(body, here, shape, col), dl,
                     _int_or_none(params.get("min_present"), path, "min_present", col), path)
    if not isinstance(body, Mapping):
        col.add("schema", "schema_invalid", here, f"{shape!r} takes a mapping")
        return None
    body = dict(body)

    def take(key: str, default: Any = None) -> Any:
        return body.pop(key, default)

    node: Node | None = None
    if shape == "rebuttal":
        node = Rebuttal(pid, str(take("of", "")), _int_or_none(take("passes"), here, "passes", col),
                        dl, _reads(take("also"), f"{here}.also", col), path)
    elif shape == "debate":
        order = take("order", "fixed")
        if order not in ("round_robin", "fixed"):
            col.add("schema", "schema_invalid", f"{here}.order", "order is round_robin | fixed")
            order = "fixed"
        node = Debate(pid, _str_list(take("seats"), f"{here}.seats", col), order,
                      _int_or_none(take("max_turns"), here, "max_turns", col),
                      take("settle_by"), mc or _int_or_none(take("model_calls"), here,
                                                            "model_calls", col), dl, path)
    elif shape == "panel":
        per = params.get("per", take("per", "judge"))
        if per not in ("judge", "candidate"):
            col.add("schema", "schema_invalid", f"{here}.per", "per is judge | candidate")
            per = "judge"
        node = Panel(pid, _str_list(take("judges"), f"{here}.judges", col), str(take("on", "")),
                     _tally(take("tally"), here, col),
                     _str_list(take("gates_first"), f"{here}.gates_first", col), per, dl, path,
                     _int_or_none(take("max_candidates"), here, "max_candidates", col))
    elif shape == "select":
        keep = take("keep")
        keep_top, keep_stands = None, False
        if keep == "stands":
            keep_stands = True
        elif isinstance(keep, Mapping) and set(keep) == {"top"} and isinstance(keep["top"], int):
            keep_top = int(keep["top"])
        else:
            col.add("schema", "schema_invalid", f"{here}.keep", "keep is stands | {top: k}")
        node = Select(pid, str(take("from", "")), str(take("by", "")), keep_top, keep_stands,
                      _int_or_none(take("stages", 1), here, "stages", col) or 1, path)
    elif shape == "branch":
        cases = []
        for i, c in enumerate(take("cases") or []):
            cpath = f"{here}.cases[{i}]"
            if not isinstance(c, Mapping) or set(c) != {"when", "do"}:
                col.add("schema", "schema_invalid", cpath, "a case is {when, do}")
                continue
            pred = _pred(c["when"], f"{cpath}.when", col)
            do = parse_node(c["do"], f"{cpath}.do", col)
            if pred is not None and do is not None:
                cases.append(Case(pred, do))
        if not cases:
            col.add("schema", "schema_invalid", f"{here}.cases", "a branch needs at least one case")
        default_raw = take("default")
        default = parse_node(default_raw, f"{here}.default", col) if default_raw is not None else None
        node = Branch(pid, tuple(cases), default, path)
    elif shape == "loop":
        lbody = parse_node(take("body"), f"{here}.body", col)
        until_raw = take("until")
        until = _pred(until_raw, f"{here}.until", col) if until_raw is not None else None
        node = None if lbody is None else Loop(
            pid, lbody, until, _int_or_none(take("max_iterations"), here, "max_iterations", col),
            mc if mc is not None else _int_or_none(take("model_calls"), here, "model_calls", col),
            path)
    elif shape == "map":
        mbody = parse_node(take("body"), f"{here}.body", col)
        over = _ref(take("over"), f"{here}.over", col)
        as_ = take("as")
        if not isinstance(as_, str) or not is_ident(as_):
            col.add("schema", "schema_invalid", f"{here}.as", "map needs as: <name>")
            as_ = "item"
        blind = params.get("blind", take("blind", False))
        node = None if (mbody is None or over is None) else Map(
            pid, over, as_, mbody, _int_or_none(take("max_items"), here, "max_items", col),
            _int_or_none(take("concurrency"), here, "concurrency", col), dl, bool(blind),
            mc if mc is not None else _int_or_none(take("model_calls"), here, "model_calls", col),
            path)
    elif shape == "escalate":
        tbody = parse_node(take("try"), f"{here}.try", col)
        when = _pred(take("when"), f"{here}.when", col)
        to = take("to")
        if not isinstance(to, str):
            col.add("schema", "schema_invalid", f"{here}.to", "escalate needs to: <seat id>")
        node = None if (tbody is None or when is None or not isinstance(to, str)) else Escalate(
            pid, tbody, to, when, path)
    elif shape == "use_round":
        node = UseRound(pid, str(take("spec", "")), _int_or_none(take("version"), here,
                                                                  "version", col), path)
    for k in body:
        col.add("schema", "schema_invalid", f"{here}.{k}", f"unknown key {k!r} in {shape!r}")
    return node


def _resolve_seats(raw: Any, col: _Collector) -> dict[str, Seat]:
    if not isinstance(raw, Mapping) or not raw:
        col.add("schema", "schema_invalid", "seats", "seats is a non-empty mapping")
        return {}
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(sid: str, stack: tuple[str, ...]) -> dict[str, Any] | None:
        if sid in resolved:
            return resolved[sid]
        body = raw.get(sid)
        if not isinstance(body, Mapping):
            return None
        body = dict(body)
        base_id = body.pop("seat_ref", None)
        if base_id is None:
            resolved[sid] = body
            return body
        path = f"seats.{sid}.seat_ref"
        if not isinstance(base_id, str):
            col.add("schema", "schema_invalid", path, "seat_ref is a seat id")
            return None
        if "/" in base_id:
            col.add("V11", "shape_not_built", path,
                    "shared seat files are not available yet (SEAMS #92); define the seat "
                    "in the spec and use seat_ref to a seat id")
            return None
        if base_id in stack:
            col.add("V2", "cannot_terminate", path,
                    f"seat_ref cycle: {' -> '.join(stack + (base_id,))}")
            return None
        if base_id not in raw:
            col.add("V8", "unknown_reference", path, f"seat_ref {base_id!r} is not a seat here")
            return None
        base = resolve(base_id, stack + (base_id,))
        if base is None:
            return None
        merged = {**base, **body}
        resolved[sid] = merged
        return merged

    seats: dict[str, Seat] = {}
    for sid in raw:
        if not isinstance(sid, str) or not is_ident(sid):
            col.add("schema", "schema_invalid", f"seats.{sid}", "seat id is not a slug")
            continue
        body = resolve(sid, (sid,))
        if body is None:
            if not isinstance(raw.get(sid), Mapping):
                col.add("schema", "schema_invalid", f"seats.{sid}", "a seat is a mapping")
            continue
        try:
            seats[sid] = Seat.model_validate(body)
        except ValidationError as exc:
            col.add("schema", "schema_invalid", f"seats.{sid}", _first_error(exc))
    return seats


def parse_spec(raw: Any, *, source: str | None = None) -> tuple[RoundSpec | None, list[Refusal]]:
    """Parse a spec body (a ``{round: {...}}`` wrapper is accepted)."""
    col = _Collector()
    if isinstance(raw, Mapping) and set(raw) == {"round"}:
        raw = raw["round"]
    if not isinstance(raw, Mapping):
        col.add("schema", "schema_invalid", "", "a spec is a mapping")
        return None, col.refusals
    _scan_token_keys(raw, "", col)
    allowed = {"id", "version", "about", "inputs", "seats", "flow", "budgets", "publishes",
               "families_available", "prior_runs"}
    for k in raw:
        if k not in allowed:
            col.add("schema", "schema_invalid", str(k), f"unknown top-level key {k!r}")
    for k in ("id", "version", "inputs", "seats", "flow", "budgets", "publishes"):
        if k not in raw:
            col.add("schema", "schema_invalid", k, f"{k!r} is required")
    sid, version = raw.get("id"), raw.get("version")
    if not isinstance(sid, str) or not is_ident(sid):
        col.add("schema", "schema_invalid", "id", "id is a slug")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        col.add("schema", "schema_invalid", "version", "version is an integer >= 1")
    inputs: dict[str, InputDecl] = {}
    for name, decl in (raw.get("inputs") or {}).items():
        try:
            inputs[str(name)] = InputDecl.model_validate(decl)
        except ValidationError as exc:
            col.add("schema", "schema_invalid", f"inputs.{name}", _first_error(exc))
    seats = _resolve_seats(raw.get("seats"), col)
    flow = parse_node(raw.get("flow"), "flow", col) if "flow" in raw else None
    budgets_raw = {k: v for k, v in (raw.get("budgets") or {}).items() if "token" not in str(k)}
    budgets: Budgets | None = None
    try:
        budgets = Budgets.model_validate(budgets_raw)
    except ValidationError as exc:
        col.add("schema", "schema_invalid", "budgets", _first_error(exc))
    publishes = raw.get("publishes")
    if publishes not in ("none", "shadow", "reader"):
        col.add("schema", "schema_invalid", "publishes", "publishes is none | shadow | reader")
    prior = raw.get("prior_runs", "none")
    if prior not in ("none", "sealed_before_as_of"):
        col.add("schema", "schema_invalid", "prior_runs", "prior_runs is none | sealed_before_as_of")
    fams = raw.get("families_available") or []
    if not isinstance(fams, list) or not all(isinstance(f, str) for f in fams):
        col.add("schema", "schema_invalid", "families_available", "a list of family names")
        fams = []
    if col.refusals or flow is None or budgets is None:
        return None, col.refusals
    spec = RoundSpec(
        id=sid, version=version, inputs=inputs, seats=seats, flow=flow, budgets=budgets,
        publishes=publishes, about=str(raw.get("about") or ""),
        families_available=tuple(fams), prior_runs=prior, sha256=spec_sha256(raw),
        source=source, raw=dict(raw),
    )
    return spec, []


class SpecYamlLoader(yaml.SafeLoader):
    """YAML 1.2 booleans: only ``true`` / ``false``.

    PyYAML follows YAML 1.1, where ``on``, ``off``, ``yes`` and ``no`` are
    booleans — so the design's ``thinking: on`` and ``panel: {on: verify}``
    would load as ``True`` (a key, even). Specs are read with this loader so
    they mean what they say."""


SpecYamlLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
SpecYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=SpecYamlLoader)  # noqa: S506 - SafeLoader subclass


def read_spec_text(path: str | Path) -> Any:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return json.loads(text) if p.suffix == ".json" else load_yaml(text)


def load_spec(path: str | Path) -> tuple[RoundSpec | None, list[Refusal]]:
    """Read a spec file (YAML or JSON) and parse it; refusals on failure."""
    try:
        raw = read_spec_text(path)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        return None, [Refusal("schema", "unreadable", str(path), str(exc))]
    return parse_spec(raw, source=str(Path(path).resolve()))
