# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write PATCH GRAMMAR and the PREFLIGHT (C-2b-1; design C-2a §1, §4 D10).

Two pure functions both sides of a consult write call — the runtime handler
(which preflights before it posts) and the registry executor (which preflights
again at execution, and again at confirm):

* :func:`apply_patch` — ``update_object`` takes ``{path: value}`` on the
  class's ALLOWLISTED paths (``consult_write_vocabulary.writable_paths``),
  never a body. A protected path is ``protected_field``; any other undeclared
  path is ``path_not_writable`` (the 09-28 class: a key the catalog does not
  declare is refused, not silently carried). The patch is deep-set onto a COPY
  of the head the caller re-read under ``expected_version``. A value written
  over a factory-valued field (``{factory_kind, raw}``, e.g. a cron schedule)
  may be given bare; it is wrapped in the field's own factory kind.
* :func:`preflight` — the body parses through the RUNTIME'S OWN parser for the
  class: the source actor's ``SourceDescriptor.model_validate(typed, strict=False)``
  over the ``/typed`` wire shape; for a subject, the target resolver's
  ``TargetDescriptor.model_validate(typed, strict=False)``. A body the runtime cannot parse never lands
  (the 09-28 dead-analyst class, D10's first leg).

Depends only on the vocabulary leaf and the schema package (which the slim
registry image ships).
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from . import consult_write_vocabulary as W

__all__ = ["PatchProblem", "apply_patch", "editable_view", "normalize_create", "preflight"]

#: ``(cause, detail)`` — the cause is a ``WRITE_BLOCK_CAUSES`` word.
PatchProblem = tuple[str, str]


def _get(body: Mapping[str, Any], path: str) -> Any:
    node: Any = body
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set(body: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = body
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    prior = node.get(parts[-1])
    if isinstance(prior, Mapping) and "factory_kind" in prior and not isinstance(value, Mapping):
        value = {**prior, "raw": value}
    node[parts[-1]] = value


def apply_patch(
    cls: str, body: Mapping[str, Any], patch: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, PatchProblem | None]:
    """``(new_body, None)`` or ``(None, (cause, detail))``. Never mutates ``body``."""
    if not isinstance(patch, Mapping) or not patch:
        return None, ("path_not_writable", "patch must name at least one allowlisted path.")
    allowed = set(W.writable_paths(cls))
    protected = sorted(str(p) for p in patch if W.is_protected(str(p)))
    if protected:
        return None, (
            "protected_field",
            f"{', '.join(protected)} cannot be changed through consult.",
        )
    unknown = sorted(str(p) for p in patch if str(p) not in allowed)
    if unknown:
        return None, (
            "path_not_writable",
            f"{', '.join(unknown)} is not a writable {cls} path; writable: {', '.join(sorted(allowed))}.",
        )
    unset = sorted(str(p) for p in patch if str(p).startswith("config.") and _get(body, str(p)) is None)
    if unset:
        # A fetch option is a per-kind, factory-valued key: consult may change
        # one the source already carries, never invent one its handler may not read.
        return None, (
            "path_not_writable",
            f"{', '.join(unset)} is not set on this {cls}; consult changes existing fetch options only.",
        )
    out = copy.deepcopy(dict(body))
    for path, value in patch.items():
        _set(out, str(path), copy.deepcopy(value))
    return out, None


def normalize_create(body: Mapping[str, Any], *, now_iso: str) -> dict[str, Any]:
    """A ``create_object`` body as it will be registered: it lands ``draft``
    with the placeholder version the registry stamps; owner/created defaulted."""
    out = copy.deepcopy(dict(body or {}))
    identity = dict(out.get("identity") or {})
    identity["state"] = "draft"
    identity["version"] = "0" * 16
    identity.setdefault("owner", "operator_via_consult")
    identity.setdefault("created", now_iso)
    out["identity"] = identity
    return out


def editable_view(cls: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The allowlisted paths' current values (what ``inspect_object`` shows)."""
    out: dict[str, Any] = {}
    for path in W.writable_paths(cls):
        value = _get(body, path)
        if isinstance(value, Mapping) and "factory_kind" in value:
            value = value.get("raw")
        out[path] = value
    return out


def _runtime_model(cls: str) -> Any:
    """The model the RUNTIME parses ``cls`` with (``/typed``, ``strict=False``), or None."""
    if cls == "source":
        from .schemas.source import SourceDescriptor

        return SourceDescriptor
    if cls == "subject":  # runtime/dapr_host._target_deps_resolver
        from .schemas.target import TargetDescriptor

        return TargetDescriptor
    if cls in ("unit", "composition"):  # the analyst deps resolver (registry/consult_write_analyst.runtime_parse)
        from .schemas.analyst import AnalystDescriptor

        return AnalystDescriptor
    return None


def preflight(cls: str, body: Mapping[str, Any]) -> str | None:
    """Why the runtime could NOT parse ``body`` for ``cls``, or None."""
    model = _runtime_model(cls)
    if model is None:
        return f"no runtime parser is declared for class {cls!r}"
    try:
        typed = model.model_validate(dict(body), strict=False)
        # The runtime reads the /typed wire shape: the dump must round-trip.
        model.model_validate(typed.model_dump(mode="json", by_alias=True), strict=False)
    except Exception as exc:  # noqa: BLE001 — the message is the refusal
        text = " ".join(str(exc).split())
        return text[:600]
    return None
