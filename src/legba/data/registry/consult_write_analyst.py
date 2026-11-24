# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write executor's ANALYST machinery — the shared body of the
``unit`` and ``composition`` branches (C-2b-1, W6; design C-2a §1 rows 3-4, §4, D9, D10).

Both classes are ``Family.ANALYST`` heads; the class is the KIND
(``consult_write_vocabulary.COMPOSITION_KINDS``), never an id. The two branch
modules (``consult_write_unit.py``, ``consult_write_composition.py``) bind this
machinery to their class; the source branch's :class:`Plan` / :class:`Refused`
are reused so the executor sees one shape.

* :func:`inspect` — the head's writable fields, version, state, the last
  versions, what is protected (a pinned prompt, reserved option keys), the
  option knobs the catalog declares for the head, whether a rerun is offered
  and whether it needs a subject, and this session's revertible writes.
* :func:`plan` — update (a patch over the class's allowlisted paths:
  ``method.options.<declared knob>`` validated through
  ``legba.data.option_catalog`` — the catalog is the contract; the window; a
  unit's rubric; a composition's membership; the inline prompt unless pinned),
  transition / retire through the lifecycle FSM, and **rerun**: a forced run
  posted to the analyst's actor with ``trigger_kind: "method"`` (no cadence
  cooldown), recorded with ``reversal.kind = none``. Every new body is
  preflighted through :func:`runtime_parse`, the runtime's own parse.
* :func:`plan_revert` — rollback to ``pre.version`` / the inverse transition,
  refused ``superseded_since`` when the post-image is no longer the head.
* :func:`activation` (D10) — the DURABLE runtime signal is the first
  ``analyst_traces`` row at the new version (``verified``); ``parse_failed`` is
  the runtime resolver's own parse (``AnalystDescriptor.model_validate`` over
  the ``/typed`` shape, ``runtime/dapr_host.py`` ``analyst_deps_resolver``)
  run here over the bytes the registry serves the runtime — deterministic, so
  a failure here is a failure there. No runtime edit: the trace table already
  carries ``analyst_version``.

D9: a pinned prompt is ``protected_field``. The pin list is INSTANCE data —
the ``protected_prompts`` list in ``update_object``'s config in the
``consult_write`` pack (the flip kit's set, ``scripts/voice4_flip``, held equal
by a guard test) — never a name in this code. A pack without the key pins
every prompt (fail closed). A rerun is offered only when every LLM stack
component the head names is in ``rerun_object``'s ``unbilled_components``
(else ``billed_rerun_not_offered``; absent key = nothing is unbilled).

A leaf of the registry package: it imports the descriptor registry, the
vocabulary, the option catalog leaf and the schema package — no runtime
module, no analyst module.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

import httpx

from .. import consult_write_vocabulary as W
from ..schemas.lifecycle import ALLOWED_TRANSITIONS, LifecycleState
from .consult_write_source import Plan, Refused
from .descriptor import Family
from .errors import DescriptorNotFound, VersionConflict

logger = logging.getLogger(__name__)

__all__ = [
    "FAMILY", "activation", "class_of", "drain_reruns", "inspect", "option_specs", "plan",
    "plan_revert", "runtime_parse", "subject_bound",
]

FAMILY = Family.ANALYST

#: Mirrored from ``registry/consult_api.py`` (importing it here would cycle
#: through the write front); ``tests/data_pkg/test_consult_write_unitcomp.py``
#: holds them equal.
DAPR_SIDECAR_URL_ENV = "LEGBA_DAPR_SIDECAR_URL"
DAPR_SIDECAR_URL_DEFAULT = "http://dapr-sidecar:3500"
ACTOR_TYPE = "AnalystActor"

RERUN_TIMEOUT_MAX_SECONDS = 1800.0
_PROMPT_MAX_CHARS = 60_000
_RUBRIC_MAX_CHARS = 20_000
_MEMBERS_MAX = 64
_WINDOW_RE = re.compile(r"^\s*(\d+)\s*h\s*$", re.IGNORECASE)

#: In-flight forced runs (registry-owned, like the consult's detached runs).
_RERUNS: set[asyncio.Task[Any]] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _short(version: str | None) -> str:
    return (version or "")[:8]


def _sidecar_url() -> str:
    return (os.getenv(DAPR_SIDECAR_URL_ENV, DAPR_SIDECAR_URL_DEFAULT).strip().rstrip("/")
            or DAPR_SIDECAR_URL_DEFAULT)


# ---------------------------------------------------------------------------
# what a head is
# ---------------------------------------------------------------------------


def kind_of(body: Mapping[str, Any]) -> str:
    kind = (body.get("identity") or {}).get("kind")
    return str(getattr(kind, "value", kind) or "")


def class_of(body: Mapping[str, Any]) -> str | None:
    """``unit`` / ``composition`` by kind; None for a consult surface."""
    kind = kind_of(body)
    if kind in W.NOT_WRITABLE_ANALYST_KINDS:
        return None
    return "composition" if kind in W.COMPOSITION_KINDS else "unit"


def subject_bound(body: Mapping[str, Any]) -> bool:
    """True when the runtime runs the head once PER SUBJECT (a ``targets``
    selector, not an ``id_list`` union) — the ``_cadence_targets`` rule."""
    targets = (body.get("subscription") or {}).get("targets")
    return isinstance(targets, Mapping) and not targets.get("id_list")


def _inline_prompt(body: Mapping[str, Any]) -> bool:
    method = body.get("method") or {}
    return not method.get("prompt_module") and bool(str(method.get("system_prompt") or "").strip())


def runtime_parse(body: Mapping[str, Any]) -> str | None:
    """Why the RUNTIME could not parse ``body``, or None: the analyst deps
    resolver's ``AnalystDescriptor.model_validate(typed, strict=False)`` over the
    ``/typed`` wire shape, round-tripped (D10's first leg)."""
    from ..schemas.analyst import AnalystDescriptor

    try:
        typed = AnalystDescriptor.model_validate(dict(body), strict=False)
        AnalystDescriptor.model_validate(typed.model_dump(mode="json", by_alias=True), strict=False)
    except Exception as exc:  # noqa: BLE001 — the message is the refusal
        return " ".join(str(exc).split())[:600]
    return None


def _model(body: Mapping[str, Any]) -> Any:
    from ..schemas.analyst import AnalystDescriptor

    return AnalystDescriptor.model_validate(dict(body), strict=False)


def option_specs(body: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(route, {name: OptionSpec})`` — the catalog the runtime validates this
    head's ``method.options`` against: the sub-handler's for a deterministic
    head (``sub_handler``, else the id), the kind's otherwise."""
    from ..option_catalog.handler_options import ANALYST_KIND_OPTIONS, HANDLER_OPTIONS

    method = body.get("method") or {}
    if method.get("kind") == "deterministic":
        route = str(method.get("sub_handler") or (body.get("identity") or {}).get("id") or "")
        specs = HANDLER_OPTIONS.get(route, ())
    else:
        route = kind_of(body)
        specs = ANALYST_KIND_OPTIONS.get(route, ())
    return route, {s.name: s for s in specs}


def _spec_view(spec: Any) -> dict[str, Any]:
    out = {"kind": spec.kind, "min": spec.minimum, "max": spec.maximum,
           "choices": list(spec.choices) if spec.choices else None, "doc": spec.doc[:160]}
    return {k: v for k, v in out.items() if v is not None}


def _mismatch(cls: str, aid: str, actual: str | None) -> str:
    if actual is None:
        return f"{aid} is a consult surface; the write plane does not change it."
    return f"{aid} is a {actual}, not a {cls}; name it as class {actual}."


async def _head(registry: Any, cls: str, aid: str) -> Any:
    try:
        head = await registry.get(aid, family=FAMILY)
    except DescriptorNotFound as exc:
        raise Refused("ref_not_read", f"{cls} {aid!r} does not exist.") from exc
    actual = class_of(head.body or {})
    if actual != cls:
        raise Refused("class_not_writable", _mismatch(cls, aid, actual))
    return head


async def _typed_body(registry: Any, aid: str) -> dict[str, Any]:
    typed = await registry.get_typed(aid, family=FAMILY)
    return typed.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# the instance's lists, read off the consult_write pack (never names in code)
# ---------------------------------------------------------------------------


async def _tool_config(registry: Any, tool: str) -> Mapping[str, Any] | None:
    try:
        pack = await registry.get(W.CONSULT_WRITE_PACK_ID, family=Family.ACTION_PACK)
    except DescriptorNotFound:
        return None
    for spec in (pack.body or {}).get("tools") or ():
        if isinstance(spec, Mapping) and spec.get("name") == tool:
            return spec.get("config") or {}
    return None


async def pinned(registry: Any, aid: str) -> tuple[bool, str]:
    """``(is_pinned, why)`` for the head's prompt (D9)."""
    pins = ((await _tool_config(registry, "update_object")) or {}).get("protected_prompts")
    if not isinstance(pins, list):
        return True, ("the consult_write pack declares no protected_prompts list, so every prompt "
                      "is treated as pinned")
    if aid in pins:
        return True, (f"{aid}'s prompt is under a byte-identity pin; it changes through the pin "
                      "discipline (draft, replay, re-pin), never through consult")
    return False, ""


def llm_refs(body: Mapping[str, Any]) -> list[str]:
    """The LLM stack components the head names (``method.llm`` stack refs)."""
    out: list[str] = []
    for value in ((body.get("method") or {}).get("llm") or {}).values():
        if isinstance(value, Mapping) and value.get("factory_kind") == "stack_ref" and value.get("raw"):
            out.append(str(value["raw"]))
    return sorted(set(out))


async def billed_refs(registry: Any, body: Mapping[str, Any]) -> list[str]:
    """The head's LLM components NOT declared unbilled (empty = a rerun costs $0)."""
    unbilled = ((await _tool_config(registry, "rerun_object")) or {}).get("unbilled_components")
    refs = llm_refs(body)
    if not isinstance(unbilled, list):
        return refs
    return [r for r in refs if r not in unbilled]


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def editable_view(cls: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The writable paths' current values (``None`` = unset: the default stands)."""
    out: dict[str, Any] = {}
    _route, specs = option_specs(body)
    options = (body.get("method") or {}).get("options") or {}
    for name in sorted(specs):
        out[W.OPTIONS_PREFIX + name] = options.get(name)
    sub = body.get("subscription") or {}
    if cls == "unit":
        if isinstance(sub.get("targets"), Mapping):
            out["subscription.targets.time_window"] = sub["targets"].get("time_window")
        out["eval.rubric"] = (body.get("eval") or {}).get("rubric")
    else:
        members = [m for m in sub.get("other_analysts") or () if isinstance(m, Mapping)]
        out[W.MEMBERSHIP_PATH] = [m.get("id") for m in members]
        windows = sorted({str(m.get("time_window")) for m in members})
        out["subscription.other_analysts.time_window"] = windows[0] if len(windows) == 1 else windows
    if _inline_prompt(body):
        out[W.PROMPT_PATH] = (body.get("method") or {}).get("system_prompt")
    return out


def writable_paths(cls: str, body: Mapping[str, Any], *, prompt_pinned: bool) -> list[str]:
    return [p for p in editable_view(cls, body) if p != W.PROMPT_PATH or not prompt_pinned]


async def inspect(registry: Any, cls: str, aid: str, *, session_id: str | None) -> dict[str, Any]:
    try:
        head = await registry.get(aid, family=FAMILY)
    except DescriptorNotFound:
        return {"found": False, "class": cls, "id": aid}
    actual = class_of(head.body or {})
    if actual != cls:
        return {"found": False, "class": cls, "id": aid, "detail": _mismatch(cls, aid, actual)}
    try:
        body = await _typed_body(registry, aid)
    except Exception as exc:  # noqa: BLE001 — a head that does not build cannot be changed
        return {"found": False, "class": cls, "id": aid,
                "detail": f"the head does not parse, so it cannot be changed here: {exc}"[:600]}
    route, specs = option_specs(body)
    prompt_pinned, _why = await pinned(registry, aid)
    billed = await billed_refs(registry, body)
    bound = subject_bound(body)
    async with registry.pg.acquire() as conn:
        versions = await conn.fetch(
            f"SELECT version, state, is_head, created_at FROM {FAMILY.table} "
            "WHERE descriptor_id = $1 ORDER BY created_at DESC LIMIT $2",
            aid, W.LIMITS["inspect_versions"],
        )
        writes = await conn.fetch(
            "SELECT id, verb, status, executed_at, reversed_by_write_id "
            "FROM consult_writes WHERE class = $3 AND object_ref = $1 "
            "AND session_id = $2::uuid AND status = 'executed' AND verb NOT IN ('revert', 'rerun') "
            "ORDER BY created_at DESC LIMIT 10",
            aid, session_id, cls,
        ) if session_id else []
    prompt = ("pinned" if prompt_pinned and _inline_prompt(body)
              else "inline" if _inline_prompt(body)
              else "from code (prompt_module); not a descriptor field" if (body.get("method") or {}).get("prompt_module")
              else "none")
    if head.state != "active":
        rerun = {"offered": False, "why": f"the {cls} is {head.state}; no actor runs it"}
    elif billed:
        rerun = {"offered": False, "why": f"it runs on {', '.join(billed)}, not declared unbilled"}
    else:
        rerun = {"offered": True, "needs_subject": bound}
    return {
        "found": True,
        "class": cls,
        "id": aid,
        "name": head.name,
        "kind": kind_of(body),
        "version": head.version,
        "state": head.state,
        "fields": editable_view(cls, body),
        "writable": writable_paths(cls, body, prompt_pinned=prompt_pinned),
        "options_declared": {"route": route, **{n: _spec_view(s) for n, s in sorted(specs.items())}},
        "protected": ["identity.*", "schema_uri", "method.options.<reserved runtime keys>"]
        + ([W.PROMPT_PATH] if prompt_pinned and _inline_prompt(body) else []),
        "prompt": prompt,
        "subject_bound": bound,
        "rerun": rerun,
        "versions": [
            {"version": r["version"], "state": r["state"], "head": r["is_head"],
             "created_at": r["created_at"].isoformat()}
            for r in versions
        ],
        "writes": [
            {"write_id": str(r["id"]), "verb": r["verb"],
             "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None,
             "reverted": r["reversed_by_write_id"] is not None}
            for r in writes
        ],
        "confirmation": (
            f"pausing an active {cls}, retiring it, any prompt edit"
            + (" and any change to its inputs" if cls == "composition" else "")
            + " need the operator's confirmation; retire cannot be undone — prefer paused. "
            "A rerun cannot be undone."
        ),
    }


# ---------------------------------------------------------------------------
# the patch grammar
# ---------------------------------------------------------------------------


def _window(value: Any) -> str:
    hours: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        hours = value
    elif isinstance(value, str) and (m := _WINDOW_RE.match(value)):
        hours = int(m.group(1))
    if hours is None or not 1 <= hours <= W.WINDOW_MAX_HOURS:
        raise Refused(
            "preflight_failed",
            f"a window is written as '<hours>h', 1 to {W.WINDOW_MAX_HOURS}; got {value!r} "
            "(the runtime reads no other form).",
        )
    return f"{hours}h"


async def _members(registry: Any, aid: str, body: Mapping[str, Any], value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise Refused("preflight_failed", f"{W.MEMBERSHIP_PATH} takes a non-empty list of analyst ids.")
    ids = [v.strip() for v in value]
    if len(set(ids)) != len(ids) or len(ids) > _MEMBERS_MAX:
        raise Refused("preflight_failed", f"inputs must be distinct and at most {_MEMBERS_MAX}.")
    if aid in ids:
        raise Refused("preflight_failed", "a composition cannot quote itself.")
    current = {m["id"]: m for m in (body.get("subscription") or {}).get("other_analysts") or ()
               if isinstance(m, Mapping) and m.get("id")}
    template = next(iter(current.values()), None)
    for new_id in (i for i in ids if i not in current):
        try:
            row = await registry.get(new_id, family=FAMILY)
        except DescriptorNotFound as exc:
            raise Refused("ref_not_read", f"analyst {new_id!r} does not exist.") from exc
        if row.state == "retired":
            raise Refused("preflight_failed", f"analyst {new_id} is retired; a composition cannot quote it.")
    out: list[dict[str, Any]] = []
    for i in ids:
        if i in current:
            out.append(copy.deepcopy(dict(current[i])))
        else:
            out.append({
                "id": i,
                "data_types": list((template or {}).get("data_types") or ["finding"]),
                "time_window": str((template or {}).get("time_window") or "24h"),
            })
    return out


async def apply_patch(
    registry: Any, cls: str, aid: str, body: Mapping[str, Any], patch: Mapping[str, Any],
) -> dict[str, Any]:
    """The new body, or :class:`Refused`. Never mutates ``body``."""
    if not isinstance(patch, Mapping) or not patch:
        raise Refused("path_not_writable", "patch must name at least one writable path.")
    from ..option_catalog.handler_options import RESERVED_OPTION_KEYS

    paths = [str(p) for p in patch]
    protected = sorted(p for p in paths if W.is_protected(p) or p.startswith("identity."))
    if protected:
        raise Refused("protected_field", f"{', '.join(protected)} cannot be changed through consult.")
    out = copy.deepcopy(dict(body))
    route, specs = option_specs(body)
    method = out.setdefault("method", {})
    sub = out.setdefault("subscription", {})
    for path in sorted(paths, key=lambda p: (p != W.MEMBERSHIP_PATH, p)):
        value = patch[path]
        if path.startswith(W.OPTIONS_PREFIX):
            key = path[len(W.OPTIONS_PREFIX):]
            if key in RESERVED_OPTION_KEYS or key.startswith("_"):
                raise Refused("protected_field", f"method.options.{key} is stamped by the runtime; a descriptor may not set it.")
            spec = specs.get(key)
            if spec is None:
                raise Refused(
                    "path_not_writable",
                    f"method.options.{key} is not a knob the option catalog declares for {route or 'this head'}"
                    f"; declared: {', '.join(sorted(specs)) or 'none'}.",
                )
            options = method.setdefault("options", {})
            if value is None:
                options.pop(key, None)  # unset: the handler's own default stands
                continue
            ok, coerced, cause = spec.validate(value)
            if not ok:
                raise Refused("preflight_failed", f"method.options.{key}: {cause} (got {value!r}).")
            options[key] = coerced
        elif path == W.PROMPT_PATH:
            if not _inline_prompt(body):
                raise Refused("path_not_writable", f"{aid}'s prompt is not an inline descriptor prompt (it comes from code).")
            is_pinned, why = await pinned(registry, aid)
            if is_pinned:
                raise Refused("protected_field", why)
            if not isinstance(value, str) or not value.strip() or len(value) > _PROMPT_MAX_CHARS:
                raise Refused("preflight_failed", f"the prompt must be non-empty text of at most {_PROMPT_MAX_CHARS} characters.")
            method["system_prompt"] = value
        elif cls == "unit" and path == "subscription.targets.time_window":
            if not isinstance(sub.get("targets"), dict):
                raise Refused("path_not_writable", f"{aid} has no subject binding, so no slice window to set.")
            sub["targets"]["time_window"] = _window(value)
        elif cls == "unit" and path == "eval.rubric":
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > _RUBRIC_MAX_CHARS):
                raise Refused("preflight_failed", f"the rubric must be non-empty text of at most {_RUBRIC_MAX_CHARS} characters, or null.")
            if isinstance(out.get("eval"), dict):
                out["eval"]["rubric"] = value
            elif value is not None:
                out["eval"] = {"rubric": value}
        elif cls == "composition" and path == W.MEMBERSHIP_PATH:
            sub["other_analysts"] = await _members(registry, aid, out, value)
        elif cls == "composition" and path == "subscription.other_analysts.time_window":
            members = sub.get("other_analysts") or []
            if not members:
                raise Refused("path_not_writable", f"{aid} quotes no inputs, so no input window to set.")
            window = _window(value)
            for m in members:
                m["time_window"] = window
        else:
            allowed = writable_paths(cls, body, prompt_pinned=False)
            raise Refused("path_not_writable", f"{path} is not a writable {cls} path; writable: {', '.join(allowed)}.")
    return out


def _membership_card(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    old = [m.get("id") for m in (before.get("subscription") or {}).get("other_analysts") or ()]
    new = [m.get("id") for m in (after.get("subscription") or {}).get("other_analysts") or ()]
    added = [i for i in new if i not in old]
    removed = [i for i in old if i not in new]
    return " · inputs " + ", ".join([f"+{i}" for i in added] + [f"−{i}" for i in removed] or ["reordered"])


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _legal(from_state: str, to_state: str) -> bool:
    try:
        return LifecycleState(to_state) in ALLOWED_TRANSITIONS[LifecycleState(from_state)]
    except ValueError:
        return False


async def plan(registry: Any, cls: str, verb: str, args: Mapping[str, Any]) -> Plan:
    if verb == "rerun":
        return await _plan_rerun(registry, cls, args)
    aid = str(args["id"])
    expected = str(args["expected_version"])
    head = await _head(registry, cls, aid)
    if head.version != expected:
        raise Refused(
            "stale_read",
            f"{cls} {aid} is at {_short(head.version)} ({head.state}), not {_short(expected)}; inspect it again.",
        )
    pre = {"version": head.version, "state": head.state}
    canon = {k: args[k] for k in ("class", "id", "expected_version", "reason") if k in args}
    if verb == "update":
        body = await _typed_body(registry, aid)
        patch = dict(args.get("patch") or {})
        new_body = await apply_patch(registry, cls, aid, body, patch)
        if new_body == body:
            raise Refused("no_change", "the change re-states the head; nothing was written.")
        failed = runtime_parse(new_body)
        if failed:
            raise Refused("preflight_failed", failed)
        typed = _model(new_body)

        async def mutate_update(*, actor: str, in_tx: Any) -> Any:
            return await registry.update(aid, typed, actor, actor_role="operator_via_consult", in_tx=in_tx)

        card = f"Update {cls} {aid} ({', '.join(sorted(patch))})"
        if W.MEMBERSHIP_PATH in patch:
            card += _membership_card(body, new_body)
        if W.PROMPT_PATH in patch:
            card += " · prompt edit"
        return Plan(
            cls=cls, verb="update", object_ref=aid, args={**canon, "patch": patch},
            tier=W.analyst_tier("update", from_state=head.state, to_state=None, paths=patch),
            pre=pre, post_state=head.state, reversal={"kind": "rollback", "to_version": head.version},
            mutate=mutate_update, expected_version=head.version, card=card,
        )
    to_state = "retired" if verb == "retire" else str(args["to_state"])
    if to_state == head.state:
        raise Refused("no_change", f"{cls} {aid} is already {to_state}.")
    if not _legal(head.state, to_state):
        raise Refused("preflight_failed", f"{head.state} → {to_state} is not a legal lifecycle move for a {cls}.")
    tier = W.analyst_tier(verb, from_state=head.state, to_state=to_state)
    if verb == "retire":
        async def mutate_retire(*, actor: str, in_tx: Any) -> Any:
            return await registry.retire(
                aid, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=str(args.get("reason") or "")[:300] or None, in_tx=in_tx,
            )

        return Plan(
            cls=cls, verb="retire", object_ref=aid, args={**canon, "to_state": "retired"}, tier=tier,
            pre=pre, post_state="retired", reversal={"kind": "none", "why": "retired is terminal"},
            mutate=mutate_retire, expected_version=head.version,
            card=f"Retire {cls} {aid} · cannot be undone",
        )
    body = await _typed_body(registry, aid)
    body["identity"]["state"] = to_state
    failed = runtime_parse(body)
    if failed:
        raise Refused("preflight_failed", failed)
    typed = _model(body)

    async def mutate_transition(*, actor: str, in_tx: Any) -> Any:
        return await registry.update(aid, typed, actor, actor_role="operator_via_consult", in_tx=in_tx)

    inverse = (
        {"kind": "transition", "to_state": head.state} if _legal(to_state, head.state)
        else {"kind": "none", "why": f"{to_state} → {head.state} is not a legal move"}
    )
    word = {"active": "Activate", "paused": "Pause"}.get(to_state, f"Move to {to_state}:")
    return Plan(
        cls=cls, verb="transition", object_ref=aid, args={**canon, "to_state": to_state}, tier=tier,
        pre=pre, post_state=to_state, reversal=inverse, mutate=mutate_transition,
        expected_version=head.version, card=f"{word} {cls} {aid}",
    )


# ---------------------------------------------------------------------------
# rerun — a forced run (trigger_kind "method"), no undo
# ---------------------------------------------------------------------------


async def _check_subject(registry: Any, aid: str, body: Mapping[str, Any], subject: str) -> None:
    try:
        target = await registry.get(subject, family=Family.TARGET)
    except DescriptorNotFound as exc:
        raise Refused("ref_not_read", f"subject {subject!r} does not exist.") from exc
    if target.state != "active":
        raise Refused("preflight_failed", f"subject {subject} is {target.state}; its runs are off.")
    pred = ((body.get("subscription") or {}).get("targets") or {}).get("predicate")
    if not pred:
        return
    from ..predicates import PredicateSurface, compile_predicate

    tb = target.body or {}
    scope, ident = tb.get("scope") or {}, tb.get("identity") or {}
    ctx = {"target": {
        "id": subject, "kind": str(ident.get("kind") or ""),
        "scope_geo": list(scope.get("geo") or []),
        "scope_entity_classes": list(scope.get("entity_classes") or []),
        "tags": list(scope.get("tags") or []),
        "abstraction_level": str(ident.get("abstraction_level") or ""),
    }}
    try:
        matched = bool(compile_predicate(pred, PredicateSurface.ANALYST_SUBSCRIPTION).evaluate(ctx))
    except Exception:  # noqa: BLE001 — an unevaluable predicate matches nothing, as at runtime
        matched = False
    if not matched:
        raise Refused("preflight_failed", f"subject {subject} is not one of {aid}'s subjects ({pred!r} does not match it).")


async def _plan_rerun(registry: Any, cls: str, args: Mapping[str, Any]) -> Plan:
    aid = str(args["id"])
    head = await _head(registry, cls, aid)
    if head.state != "active":
        raise Refused("preflight_failed", f"{cls} {aid} is {head.state}; a forced run of a head no actor runs does nothing.")
    try:
        body = await _typed_body(registry, aid)
    except Exception as exc:  # noqa: BLE001
        raise Refused("preflight_failed", f"the head does not parse: {exc}"[:600]) from exc
    failed = runtime_parse(body)
    if failed:
        raise Refused("preflight_failed", failed)
    billed = await billed_refs(registry, body)
    if billed:
        raise Refused(
            "billed_rerun_not_offered",
            f"{aid} runs on {', '.join(billed)}, which the consult_write pack does not declare unbilled; "
            "a forced run is offered only where it costs nothing.",
        )
    subject = str(args.get("subject") or "").strip() or None
    if subject_bound(body):
        if subject is None:
            raise Refused("preflight_failed", f"{aid} runs once per subject; name the subject to run it for.")
        await _check_subject(registry, aid, body, subject)
        actor_id, payload = f"analyst::{aid}::{subject}", {"trigger_kind": "method", "target_filter": subject}
    else:
        if subject is not None:
            raise Refused("preflight_failed", f"{aid} runs one pass over its whole slice; it takes no subject.")
        actor_id, payload = f"analyst::{aid}::{head.version[:16]}", {"trigger_kind": "method"}
    timeout = min(RERUN_TIMEOUT_MAX_SECONDS, float((body.get("method") or {}).get("timeout_seconds") or 180) * 3 + 60)

    async def mutate_rerun(*, actor: str, in_tx: Any) -> Any:
        async with registry.pg.transaction() as conn:
            row = await conn.fetchrow(
                f"SELECT version, state FROM {FAMILY.table} WHERE descriptor_id = $1 AND is_head LIMIT 1", aid,
            )
            if row is None or row["state"] != "active":
                raise VersionConflict(f"{cls} {aid} is no longer active")
            await registry._write_audit(
                conn, action="rerun", family=FAMILY, descriptor_id=aid, actor=actor,
                actor_role="operator_via_consult", from_version=row["version"], to_version=row["version"],
                change_summary={"rerun": {"trigger_kind": "method", "actor_id": actor_id, "subject": subject}},
                in_tx=in_tx,
            )
        if getattr(in_tx, "fired", False):  # committed: now post the run
            _spawn(_dispatch(registry, write_id=actor.split(":", 1)[1], analyst_id=aid, actor_id=actor_id,
                             payload=payload, subject=subject, timeout=timeout))

    return Plan(
        cls=cls, verb="rerun", object_ref=aid,
        args={"class": cls, "id": aid, "subject": subject, "reason": args.get("reason")},
        tier="A", pre={"version": head.version, "state": head.state}, post_state=head.state,
        reversal={"kind": "none", "why": "a rerun's outputs enter the tower; the next run supersedes them"},
        mutate=mutate_rerun, expected_version=head.version,
        card=f"Rerun {cls} {aid}" + (f" for {subject}" if subject else "") + " now",
        extra={"actor_id": actor_id},
    )


def _spawn(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _RERUNS.add(task)
    task.add_done_callback(_RERUNS.discard)


async def drain_reruns() -> None:
    """Wait for in-flight forced runs (tests, shutdown)."""
    if _RERUNS:
        await asyncio.gather(*list(_RERUNS), return_exceptions=True)


async def _stamp(registry: Any, write_id: str, verdict: Mapping[str, Any]) -> None:
    import json

    async with registry.pg.acquire() as conn:
        await conn.execute(
            "UPDATE consult_writes SET activation = $2::jsonb, "
            "receipt = jsonb_set(COALESCE(receipt, '{}'::jsonb), '{activation}', $2::jsonb) WHERE id = $1",
            UUID(write_id), json.dumps(dict(verdict), sort_keys=True, default=str),
        )


async def _dispatch(
    registry: Any, *, write_id: str, analyst_id: str, actor_id: str, payload: Mapping[str, Any],
    subject: str | None, timeout: float,
) -> None:
    """Post the forced run (the consult front door's own actor path) and stamp
    the ledger row with what landed: the first ``method``-triggered trace."""
    url = f"{_sidecar_url()}/v1.0/actors/{ACTOR_TYPE}/{actor_id}/method/run"
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    base = {"trigger_kind": "method", "actor_id": actor_id, "subject": subject}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.put(url, json=dict(payload))
        code = int(resp.status_code)
        try:
            answer = resp.json()
        except Exception:  # noqa: BLE001
            answer = {}
    except Exception as exc:  # noqa: BLE001 — recorded, never raised
        logger.warning("consult_write.rerun_failed write_id=%s err=%s", write_id, exc)
        await _stamp(registry, write_id, {**base, "status": "run_failed",
                                          "error": f"{type(exc).__name__}: {exc}"[:400], "at": _now_iso()})
        return
    answer = answer if isinstance(answer, Mapping) else {}
    sql = (
        "SELECT run_id, status, run_started_at, analyst_version FROM analyst_traces "
        "WHERE analyst_id = $1 AND cadence_trigger = 'method' AND run_started_at >= $2"
        + (" AND target_id = $3" if subject else "")
        + " ORDER BY run_started_at LIMIT 1"
    )
    async with registry.pg.acquire() as conn:
        trace = await conn.fetchrow(sql, analyst_id, since, *([subject] if subject else []))
    verdict: dict[str, Any] = {**base, "http": code, "outcome": answer.get("outcome"), "at": _now_iso()}
    if trace is not None:
        verdict.update(status="run_landed", run_id=str(trace["run_id"]), trace_status=trace["status"],
                       run_started_at=trace["run_started_at"].isoformat(), version=trace["analyst_version"])
    else:
        verdict.update(status="run_not_landed",
                       why=str(answer.get("reason") or answer.get("error") or f"no method-triggered trace (HTTP {code})")[:400])
    await _stamp(registry, write_id, verdict)


# ---------------------------------------------------------------------------
# revert — the stored handle, never a fresh model guess
# ---------------------------------------------------------------------------


async def plan_revert(registry: Any, cls: str, original: Mapping[str, Any], newer: str | None) -> Plan:
    aid = str(original["object_ref"])
    reversal = dict(original.get("reversal") or {})
    post = dict(original.get("post_image") or {})
    pre = dict(original.get("pre_image") or {})
    kind = reversal.get("kind")
    if kind == "none" or kind is None:
        raise Refused("preflight_failed",
                      f"write {original['id']} cannot be undone: {reversal.get('why') or 'no reversal'}.")
    head = await _head(registry, cls, aid)
    if head.version != post.get("version") or head.state != post.get("state"):
        raise Refused(
            "superseded_since",
            f"{cls} {aid} moved since write {original['id']} (now {_short(head.version)}, {head.state})"
            + (f"; the newer change is write {newer}" if newer else "") + ". Nothing was reverted.",
        )
    current = {"version": head.version, "state": head.state}
    canon = {"write_id": str(original["id"]), "class": cls, "id": aid}
    no_undo = {"kind": "none", "why": "a revert is undone by asking for the change again"}
    if kind == "rollback":
        target = str(reversal["to_version"])

        async def mutate_rollback(*, actor: str, in_tx: Any) -> Any:
            return await registry.rollback(
                aid, target, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=f"revert of consult write {original['id']}", in_tx=in_tx,
            )

        return Plan(
            cls=cls, verb="revert", object_ref=aid, args=canon, tier="A", pre=current,
            post_state=str(pre.get("state") or head.state), reversal=no_undo, mutate=mutate_rollback,
            expected_version=head.version, card=f"Revert {cls} {aid} to {_short(target)}",
            extra={"to_version": target},
        )
    if kind == "transition":
        to_state = str(reversal["to_state"])
        body = await _typed_body(registry, aid)
        body["identity"]["state"] = to_state
        failed = runtime_parse(body)
        if failed:
            raise Refused("preflight_failed", failed)
        typed = _model(body)

        async def mutate_back(*, actor: str, in_tx: Any) -> Any:
            return await registry.update(aid, typed, actor, actor_role="operator_via_consult", in_tx=in_tx)

        return Plan(
            cls=cls, verb="revert", object_ref=aid, args=canon,
            tier=W.analyst_tier("transition", from_state=head.state, to_state=to_state),
            pre=current, post_state=to_state, reversal=no_undo, mutate=mutate_back,
            expected_version=head.version, card=f"Revert {cls} {aid} to {to_state}",
        )
    raise Refused("preflight_failed", f"unknown reversal kind {kind!r}.")


# ---------------------------------------------------------------------------
# activation (D10) — one probe
# ---------------------------------------------------------------------------


async def activation(registry: Any, aid: str, version: str, since: datetime) -> dict[str, Any] | None:
    """One look at the new head: a verdict dict, or None (keep watching)."""
    now = _now_iso()
    head = await registry.get(aid, family=FAMILY)
    if head.version != version:
        return {"status": "superseded", "head": head.version, "at": now}
    try:
        failed = runtime_parse(await _typed_body(registry, aid))
    except Exception as exc:  # noqa: BLE001 — /typed itself failing is what the runtime would see
        failed = f"the served /typed shape does not build: {exc}"
    if failed:
        return {"status": "parse_failed", "error": failed[:600], "at": now}
    if head.state != "active":
        return {"status": "not_applicable", "why": f"the analyst is {head.state}; no actor runs it",
                "parsed": True, "at": now}
    async with registry.pg.acquire() as conn:
        trace = await conn.fetchrow(
            "SELECT run_id, status, run_started_at, cadence_trigger FROM analyst_traces "
            "WHERE analyst_id = $1 AND analyst_version = $2 AND run_started_at >= $3 "
            "ORDER BY run_started_at LIMIT 1",
            aid, version, since,
        )
    if trace is None:
        return None
    return {"status": "verified", "first_run_at": trace["run_started_at"].isoformat(),
            "run_id": str(trace["run_id"]), "trace_status": trace["status"],
            "trigger": trace["cadence_trigger"], "at": now}
