# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FLOW GRAPH — a semantic graph derived from descriptor heads (N-J, v0).

Pure functions, no I/O: :func:`derive_graph` takes the ACTIVE and PAUSED head
rows of every descriptor family (plus the LLM stack components, for model
names) and returns ``{nodes, edges, undeclared, report}``. No layout is ever
part of the payload: the canvas computes it, nothing is persisted.

THE RULE: an edge exists only where a relation is DECLARED in a descriptor
field, or is mechanically derivable from one with the same matcher the runtime
uses. Every edge names its ``basis`` — the field it was read from. A relation
that only code knows (which table a handler writes, which tool reads which
store, who reads a finding without subscribing to it) is NOT drawn: it is
listed per node under ``undeclared`` and counted in ``report``, which is a
finding for the redesign, not a gap to paper over.

Edge bases (data flows left → right):

* ``target.sources[i].source_id`` / ``target.sources[i].source_selector`` —
  source → target. Selectors resolve with the runtime's own semantics
  (``runtime/subscription/sourceref.py``: ``_scope_match`` + the Starlark
  residual, ``open`` sources only, tenant = target ``scope.owner_tenant`` or
  ``default``); a parity test pins this copy against that one.
* ``subscription.targets.predicate`` — target → analyst, the analyst's
  bind-time predicate evaluated against each target with the context
  ``consult_write_analyst._check_subject`` builds. ``id_list`` likewise.
* ``subscription.other_analysts[i]`` — producer analyst → consumer analyst.
* ``grounding.sources[i]`` — grounding store → analyst.
* ``action_packs[i]`` — analyst → pack (the tools it is offered).
* ``target.allowed_action_packs[i]`` — target → pack (a grant).
* ``action_pack.channels[i]`` — pack → sink.
* ``outputs[i]`` — analyst/target → sink (nats_stream, a2a_skill, mcp_tool …).
* ``eval.judge`` — judged analyst → judge analyst.
* ``identity.inherits[i]`` — parent → child.
* ``target.coordination.subscribes_to[i]`` — target → target.

Registry-slim: stdlib + ``legba.data.predicates`` (Starlark; in the registry
image) only. A subprocess guard test poisons the runtime-only packages.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

#: The families the registry stores, in render order.
KNOWN_FAMILIES = ("source", "target", "analyst", "action_pack", "collection")

#: Node kinds by family (a pack renders as the "pack" kind).
FAMILY_TO_KIND = {
    "source": "source",
    "target": "target",
    "analyst": "analyst",
    "action_pack": "pack",
    "collection": "collection",
}

#: Only these lifecycle states are drawn (the brief: active and paused heads).
DRAWN_STATES = frozenset({"active", "paused"})

#: The tenant any target may draw from (``sourceref.SHARED_TENANT``).
SHARED_TENANT = "shared"

# --------------------------------------------------------------------------
# small readers — every one tolerates any JSON shape
# --------------------------------------------------------------------------


def _obj(v: Any) -> dict:
    if isinstance(v, (str, bytes, bytearray)):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _raw(v: Any) -> Any:
    """A FactoryValue dump (``{"raw": x, ...}``) or a plain scalar."""
    if isinstance(v, dict) and "raw" in v:
        return v.get("raw")
    return v


def _strs(v: Any) -> list[str]:
    return [str(x) for x in _list(v) if isinstance(x, (str, int, float))]


def node_id(family: str, descriptor_id: str) -> str:
    return f"{FAMILY_TO_KIND.get(family, family)}:{descriptor_id}"


# --------------------------------------------------------------------------
# per-node summaries
# --------------------------------------------------------------------------


def _model_routes(body: dict, stack_models: Mapping[str, Mapping[str, Any]]) -> dict:
    """``method.llm`` stack refs → {role: {component_id, model, state}}."""
    llm = _obj(_obj(body.get("method")).get("llm"))
    out: dict[str, Any] = {}
    for role, val in sorted(llm.items()):
        if not isinstance(val, dict):
            continue
        cid = val.get("raw")
        if val.get("factory_kind") != "stack_ref" or not isinstance(cid, str):
            continue
        comp = stack_models.get(cid) or {}
        out[role] = {
            "component_id": cid,
            "model": comp.get("model"),
            "component_state": comp.get("state"),
        }
    return out


def _cadence(family: str, body: dict) -> dict | None:
    cad = _obj(body.get("cadence"))
    if not cad:
        return None
    if family == "source":
        return {
            "schedule": _raw(cad.get("schedule")),
            "cooldown_seconds": cad.get("cooldown_seconds"),
            "jitter_seconds": cad.get("jitter_seconds"),
        }
    return {
        "schedule": cad.get("fallback_schedule"),
        "trigger": cad.get("trigger"),
        "cooldown_seconds": cad.get("cooldown_seconds"),
    }


def _target_scope(family: str, body: dict) -> dict | None:
    if family == "analyst":
        sub = _obj(body.get("subscription"))
        t = sub.get("targets")
        if not isinstance(t, dict):
            return {"bound": False, "note": "no subscription.targets: runs unbound (global)"}
        return {
            "bound": True,
            "predicate": t.get("predicate"),
            "id_list": t.get("id_list"),
            "data_types": _strs(t.get("data_types")),
            "time_window": t.get("time_window"),
        }
    if family in ("source", "target"):
        sc = _obj(body.get("scope"))
        return {
            "tags": _strs(sc.get("tags")),
            "geo": _strs(sc.get("geo")),
            "predicate": sc.get("predicate"),
        }
    return None


def _group(family: str, row: Mapping[str, Any], body: dict) -> str:
    if family == "analyst":
        kind = row.get("kind") or _obj(body.get("identity")).get("kind") or "unknown"
        return f"analyst:{kind}"
    if family in FAMILY_TO_KIND:
        return {"source": "sources", "target": "targets", "action_pack": "packs",
                "collection": "collections"}[family]
    return f"unknown:{family}"


def _label(row: Mapping[str, Any], body: dict) -> str:
    return str(row.get("name") or _obj(body.get("identity")).get("name") or row.get("descriptor_id"))


def _descriptor_node(row: Mapping[str, Any], stack_models: Mapping[str, Mapping[str, Any]]) -> dict:
    family = str(row.get("family") or "unknown")
    raw_body = row.get("body")
    body = _obj(raw_body)
    method = _obj(body.get("method"))
    subkind = row.get("kind")
    if family == "analyst":
        subkind = row.get("kind") or _obj(body.get("identity")).get("kind")
    return {
        "id": node_id(family, str(row.get("descriptor_id"))),
        "descriptor_id": row.get("descriptor_id"),
        "family": family,
        "kind": FAMILY_TO_KIND.get(family, "unknown"),
        "subkind": subkind,
        "method_kind": method.get("kind") if family == "analyst" else None,
        "group": _group(family, row, body),
        "label": _label(row, body),
        "state": row.get("state"),
        "version": row.get("version"),
        "cadence": _cadence(family, body),
        "model_route": _model_routes(body, stack_models) if family == "analyst" else {},
        "target_scope": _target_scope(family, body),
        # The FULL body, verbatim — unknown fields survive untouched. A body
        # that is not a JSON object is returned as it came.
        "fields": body if body else raw_body,
        "undeclared": [],
    }


def _synthetic_node(nid: str, kind: str, label: str, group: str, **extra: Any) -> dict:
    return {
        "id": nid, "descriptor_id": None, "family": None, "kind": kind,
        "subkind": extra.pop("subkind", None), "method_kind": None, "group": group,
        "label": label, "state": None, "version": None, "cadence": None,
        "model_route": {}, "target_scope": None, "fields": extra.pop("fields", {}),
        "undeclared": [], **extra,
    }


# --------------------------------------------------------------------------
# matchers (mirrors of the runtime's; parity-tested)
# --------------------------------------------------------------------------


def source_scope_match(sel: Mapping[str, Any], src: Mapping[str, Any], *, target_tenant: str) -> bool:
    """``runtime/subscription/sourceref._scope_match`` over plain dicts.

    ``src`` = {kind, owner_tenant, tags, geo, languages}.
    """
    pinned = sel.get("owner_tenant")
    if pinned is not None:
        if src.get("owner_tenant") != pinned:
            return False
    elif src.get("owner_tenant") not in (target_tenant, SHARED_TENANT):
        return False
    tags, geo, langs, kinds = (_strs(sel.get(k)) for k in ("tags", "geo", "languages", "kinds"))
    if tags and not set(tags).issubset(set(src.get("tags") or [])):
        return False
    if geo and not (set(geo) & set(src.get("geo") or [])):
        return False
    if langs and not (set(langs) & set(src.get("languages") or [])):
        return False
    if kinds and src.get("kind") not in kinds:
        return False
    return True


def _eval_predicate(pred: str, ctx: dict, cache: dict) -> bool | None:
    """True/False, or None when the predicate cannot be compiled/evaluated
    (an unevaluable predicate matches nothing at runtime; it is reported)."""
    from ..predicates import PredicateSurface, compile_predicate

    try:
        compiled = cache.get(pred)
        if compiled is None:
            compiled = compile_predicate(pred, PredicateSurface.ANALYST_SUBSCRIPTION)
            cache[pred] = compiled
        return bool(compiled.evaluate(ctx))
    except Exception:  # noqa: BLE001 — reported, never raised
        return None


def target_ctx(target_id: str, body: dict) -> dict:
    """The context ``consult_write_analyst._check_subject`` evaluates against."""
    scope, ident = _obj(body.get("scope")), _obj(body.get("identity"))
    return {"target": {
        "id": target_id, "kind": str(ident.get("kind") or ""),
        "scope_geo": _strs(scope.get("geo")),
        "scope_entity_classes": _strs(scope.get("entity_classes")),
        "tags": _strs(scope.get("tags")),
        "abstraction_level": str(ident.get("abstraction_level") or ""),
    }}


# --------------------------------------------------------------------------
# the derivation
# --------------------------------------------------------------------------


class _Builder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.report: dict[str, Any] = defaultdict(int)
        self.unresolved_refs: list[dict] = []
        self.predicate_errors: list[dict] = []

    def add_node(self, node: dict) -> None:
        self.nodes.setdefault(node["id"], node)

    def ref(self, family: str, descriptor_id: str, *, via: str) -> str:
        """The node id for a referenced descriptor; a placeholder node when
        the reference does not resolve to an active/paused head."""
        nid = node_id(family, descriptor_id)
        if nid not in self.nodes:
            self.nodes[nid] = _synthetic_node(
                nid, "unresolved", descriptor_id, "unresolved",
                subkind=FAMILY_TO_KIND.get(family, family),
            )
            self.nodes[nid]["undeclared"].append(
                f"referenced by {via} but no active or paused {family} head has this id "
                "(retired, draft, or missing)")
            self.unresolved_refs.append({"id": nid, "via": via})
        return nid

    def edge(self, src: str, dst: str, basis: str, relation: str, **attrs: Any) -> None:
        key = (src, dst, basis)
        if key in self.edges:
            return
        self.edges[key] = {
            "id": f"{src}->{dst}#{basis}",
            "source": src, "target": dst,
            "basis": basis, "relation": relation, **attrs,
        }


def derive_graph(
    rows: Iterable[Mapping[str, Any]],
    *,
    stack_models: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Rows of every family's heads → the semantic graph. Never raises on a
    body shape: unknown families render as generic nodes with raw fields."""
    stack_models = stack_models or {}
    b = _Builder()
    by_family: dict[str, list[dict]] = defaultdict(list)
    # Sorted first, so every derived string (a placeholder's "referenced by",
    # report order) is the same whatever order the rows arrived in.
    for row in sorted(rows, key=lambda r: (str(r.get("family")), str(r.get("descriptor_id")))):
        if str(row.get("state")) not in DRAWN_STATES:
            continue
        node = _descriptor_node(row, stack_models)
        b.add_node(node)
        by_family[node["family"]].append({"row": row, "node": node, "body": _obj(row.get("body"))})

    for fam, items in by_family.items():
        if fam not in KNOWN_FAMILIES:
            b.report["unknown_family_nodes"] += len(items)
            for it in items:
                it["node"]["undeclared"].append(
                    f"family {fam!r} is unknown to the canvas: fields shown raw, no edges derived")

    _source_target_edges(b, by_family)
    _target_analyst_edges(b, by_family)
    _analyst_edges(b, by_family)
    _target_misc_edges(b, by_family)
    _pack_edges(b, by_family)
    _undeclared(b, by_family)

    nodes = sorted(b.nodes.values(), key=lambda n: n["id"])
    edges = sorted(b.edges.values(), key=lambda e: (e["source"], e["target"], e["basis"]))
    by_basis: dict[str, int] = defaultdict(int)
    for e in edges:
        by_basis[e["basis"].split("[")[0]] += 1
    report = dict(b.report)
    report.update({
        "nodes": len(nodes),
        "edges": len(edges),
        "edges_by_basis": dict(sorted(by_basis.items())),
        "nodes_by_kind": dict(sorted(_count(n["kind"] for n in nodes).items())),
        "unresolved_refs": b.unresolved_refs,
        "predicate_errors": b.predicate_errors,
        "inexpressible": inexpressible_relations(nodes),
    })
    return {"nodes": nodes, "edges": edges, "report": report}


def _count(it: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for k in it:
        out[k] += 1
    return out


def _source_target_edges(b: _Builder, fam: Mapping[str, list[dict]]) -> None:
    sources = []
    for it in fam.get("source", []):
        body, row = it["body"], it["row"]
        sc = _obj(body.get("scope"))
        sources.append({
            "id": it["node"]["id"], "descriptor_id": row.get("descriptor_id"),
            "kind": row.get("kind") or _obj(body.get("identity")).get("kind"),
            "owner_tenant": sc.get("owner_tenant", "default"),
            "tags": _strs(sc.get("tags")), "geo": _strs(sc.get("geo")),
            "languages": _strs(sc.get("languages")),
            "policy": body.get("subscription_policy", "open"),
        })
    by_id = {s["descriptor_id"]: s for s in sources}
    cache: dict = {}
    for it in fam.get("target", []):
        body, tnode = it["body"], it["node"]
        tenant = _obj(body.get("scope")).get("owner_tenant", "default")
        for i, ref in enumerate(_list(body.get("sources"))):
            ref = _obj(ref)
            if ref.get("source_id"):
                sid = str(ref["source_id"])
                src = by_id.get(sid)
                nid = src["id"] if src else b.ref("source", sid, via=f"{tnode['id']} target.sources[{i}].source_id")
                b.edge(nid, tnode["id"], f"target.sources[{i}].source_id", "feeds")
                continue
            sel = _obj(ref.get("source_selector"))
            if not sel:
                continue
            matched = 0
            for src in sources:
                if src["policy"] != "open" or not source_scope_match(sel, src, target_tenant=tenant):
                    continue
                if sel.get("predicate"):
                    ok = _eval_predicate(str(sel["predicate"]), {"target": {
                        "id": src["descriptor_id"], "kind": src["kind"], "scope_geo": src["geo"],
                        "scope_entity_classes": [], "tags": src["tags"], "abstraction_level": "",
                    }}, cache)
                    if not ok:
                        continue
                b.edge(src["id"], tnode["id"], f"target.sources[{i}].source_selector", "feeds")
                matched += 1
            if matched == 0:
                tnode["undeclared"].append(
                    f"target.sources[{i}].source_selector matches no active/paused open source")


def _target_analyst_edges(b: _Builder, fam: Mapping[str, list[dict]]) -> None:
    targets = [(it["node"], it["body"], str(it["row"].get("descriptor_id"))) for it in fam.get("target", [])]
    cache: dict = {}
    for it in fam.get("analyst", []):
        anode, body = it["node"], it["body"]
        t = _obj(_obj(body.get("subscription")).get("targets"))
        if not t:
            continue
        data_types = _strs(t.get("data_types"))
        id_list = _strs(t.get("id_list")) if isinstance(t.get("id_list"), list) else None
        pred = t.get("predicate")
        matched = 0
        for tnode, tbody, tid in targets:
            if id_list is not None:
                ok: bool | None = tid in id_list
                basis = "subscription.targets.id_list"
            elif pred:
                ok = _eval_predicate(str(pred), target_ctx(tid, tbody), cache)
                basis = "subscription.targets.predicate"
                if ok is None:
                    b.predicate_errors.append({"analyst": anode["id"], "target": tnode["id"], "predicate": pred})
                    continue
            else:
                ok, basis = True, "subscription.targets (no predicate: every target)"
            if ok:
                b.edge(tnode["id"], anode["id"], basis, "binds", data_types=data_types,
                       time_window=t.get("time_window"))
                matched += 1
        if matched == 0:
            anode["undeclared"].append("subscription.targets matches no active/paused target")


def _analyst_edges(b: _Builder, fam: Mapping[str, list[dict]]) -> None:
    for it in fam.get("analyst", []):
        anode, body = it["node"], it["body"]
        aid = anode["id"]
        sub = _obj(body.get("subscription"))
        for i, oa in enumerate(_list(sub.get("other_analysts"))):
            oa = _obj(oa)
            if not oa.get("id"):
                continue
            src = b.ref("analyst", str(oa["id"]), via=f"{aid} subscription.other_analysts[{i}]")
            b.edge(src, aid, f"subscription.other_analysts[{i}]", "subscribes",
                   data_types=_strs(oa.get("data_types")), time_window=oa.get("time_window"))
        g = _obj(body.get("grounding"))
        for i, s in enumerate(_strs(g.get("sources"))):
            sid = f"store:grounding:{s}"
            b.add_node(_synthetic_node(sid, "store", f"grounding: {s}", "stores", subkind="grounding_source"))
            b.edge(sid, aid, f"grounding.sources[{i}]", "grounds", enabled=bool(g.get("enabled")))
        for i, ap in enumerate(_list(body.get("action_packs"))):
            pid = _obj(ap).get("pack_id")
            if pid:
                dst = b.ref("action_pack", str(pid), via=f"{aid} action_packs[{i}]")
                b.edge(aid, dst, f"action_packs[{i}]", "offered_tools")
        for i, out in enumerate(_list(body.get("outputs"))):
            _sink_edge(b, aid, _obj(out), f"outputs[{i}]")
        judge = _obj(body.get("eval")).get("judge")
        if isinstance(judge, str) and judge:
            b.edge(aid, b.ref("analyst", judge, via=f"{aid} eval.judge"), "eval.judge", "judged_by")
        for i, parent in enumerate(_strs(_obj(body.get("identity")).get("inherits"))):
            b.edge(b.ref("analyst", parent, via=f"{aid} identity.inherits[{i}]"), aid,
                   f"identity.inherits[{i}]", "inherits")


def _sink_edge(b: _Builder, src: str, out: dict, basis: str) -> None:
    kind = str(out.get("kind") or "unknown")
    cfg = _obj(out.get("config"))
    name = cfg.get("channel") or cfg.get("skill_id") or cfg.get("tool_name") or cfg.get("subject") or cfg.get("name") or "?"
    sid = f"sink:{kind}:{name}"
    b.add_node(_synthetic_node(sid, "sink", f"{kind}: {name}", "sinks", subkind=kind, fields=out))
    b.edge(src, sid, basis, "emits")


def _target_misc_edges(b: _Builder, fam: Mapping[str, list[dict]]) -> None:
    for it in fam.get("target", []):
        tnode, body = it["node"], it["body"]
        tid = tnode["id"]
        for i, ap in enumerate(_list(body.get("allowed_action_packs"))):
            pid = _obj(ap).get("pack_id") if isinstance(ap, dict) else ap
            if pid:
                b.edge(tid, b.ref("action_pack", str(pid), via=f"{tid} allowed_action_packs[{i}]"),
                       f"target.allowed_action_packs[{i}]", "grants")
        for i, out in enumerate(_list(body.get("outputs"))):
            _sink_edge(b, tid, _obj(out), f"target.outputs[{i}]")
        coord = _obj(body.get("coordination"))
        for i, s in enumerate(_list(coord.get("subscribes_to"))):
            other = _obj(s).get("target_id") or _obj(s).get("id")
            if other:
                b.edge(b.ref("target", str(other), via=f"{tid} coordination.subscribes_to[{i}]"), tid,
                       f"target.coordination.subscribes_to[{i}]", "subscribes")


def _pack_edges(b: _Builder, fam: Mapping[str, list[dict]]) -> None:
    for it in fam.get("action_pack", []):
        pnode, body = it["node"], it["body"]
        for i, ch in enumerate(_list(body.get("channels"))):
            ch = _obj(ch)
            kind = str(ch.get("kind") or "channel")
            name = ch.get("name") or _obj(ch.get("config")).get("subject") or "?"
            sid = f"sink:{kind}:{name}"
            b.add_node(_synthetic_node(sid, "sink", f"{kind}: {name}", "sinks", subkind=kind, fields=ch))
            b.edge(pnode["id"], sid, f"action_pack.channels[{i}]", "emits")
        pnode["tools"] = [
            {"name": _obj(t).get("name"), "impl": _obj(t).get("impl"),
             "async_job": _obj(t).get("async_job")}
            for t in _list(body.get("tools"))
        ]


# --------------------------------------------------------------------------
# honesty: what the descriptors cannot say
# --------------------------------------------------------------------------

#: Stable wording, shown verbatim in the inspector.
UNDECLARED_CONSUMERS = "undeclared: consumers of this output are not declared in the registry"
UNDECLARED_WRITES = ("undeclared: the tables/stores this analyst writes are not declared "
                     "(identity.type_signature.output_type names a Python type, not a store)")
UNDECLARED_HANDLER_READS = ("undeclared: what this handler reads is not declared — "
                            "method.sub_handler names code ({handler})")
UNDECLARED_TOOL_STORES = "undeclared: which store each tool reads or writes is not declared in the pack"
UNDECLARED_STORE_WRITER = "undeclared: the producer of this grounding store is not declared in the registry"
UNDECLARED_TRIGGER = ("undeclared: cadence.trigger is a predicate over runtime events; its "
                      "producer cannot be resolved from the registry")
UNDECLARED_PROCEDURE = ("undeclared: the step sequence inside a run lives in code; the inspector "
                        "shows the steps observed in the latest trace (observed, not declared)")
UNDECLARED_SOURCE_SINK = ("undeclared: the ingest path from this source's stream into the signals "
                          "table is code (pipeline enrichment/filters are declared, the store is not)")


def _undeclared(b: _Builder, fam: Mapping[str, list[dict]]) -> None:
    # A declared CONSUMER is a registry object that reads the output: another
    # analyst's subscription or a judge. An ``outputs[]`` binding declares a
    # delivery channel, not who reads it, so it does not count.
    consumed = {e["source"] for e in b.edges.values() if e["relation"] in ("subscribes", "judged_by")}
    for it in fam.get("analyst", []):
        n, body = it["node"], it["body"]
        method = _obj(body.get("method"))
        if n["id"] not in consumed:
            n["undeclared"].append(UNDECLARED_CONSUMERS)
            b.report["analysts_without_declared_consumer"] += 1
        n["undeclared"].append(UNDECLARED_WRITES)
        b.report["analysts_without_declared_writes"] += 1
        if method.get("sub_handler"):
            n["undeclared"].append(UNDECLARED_HANDLER_READS.format(handler=method.get("sub_handler")))
            b.report["handlers_with_undeclared_reads"] += 1
        if _obj(body.get("cadence")).get("trigger"):
            n["undeclared"].append(UNDECLARED_TRIGGER)
            b.report["unresolvable_triggers"] += 1
        n["undeclared"].append(UNDECLARED_PROCEDURE)
        if method.get("prompt_module") and not method.get("system_prompt"):
            n["undeclared"].append(
                f"undeclared: the prompt text lives in code ({method.get('prompt_module')}); "
                "the descriptor holds only the reference")
            b.report["prompts_held_in_code"] += 1
    for it in fam.get("action_pack", []):
        it["node"]["undeclared"].append(UNDECLARED_TOOL_STORES)
    for it in fam.get("source", []):
        it["node"]["undeclared"].append(UNDECLARED_SOURCE_SINK)
    for n in b.nodes.values():
        if n["kind"] == "store":
            n["undeclared"].append(UNDECLARED_STORE_WRITER)


def inexpressible_relations(nodes: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The redesign finding: relation kinds the current descriptor schema has
    no field for, with how many live nodes each one leaves unexplained."""
    def n_with(text: str) -> int:
        prefix = text[:48]
        return sum(1 for n in nodes if any(str(u).startswith(prefix) for u in n.get("undeclared", [])))

    return [
        {"relation": "analyst → store written (tables, side tables, indexes)",
         "nodes": n_with(UNDECLARED_WRITES)},
        {"relation": "analyst output → consumer that reads it without subscribing "
                     "(tools, grounding, UI, graders)", "nodes": n_with(UNDECLARED_CONSUMERS)},
        {"relation": "deterministic handler → what it reads", "nodes": n_with(UNDECLARED_HANDLER_READS)},
        {"relation": "tool → store read/written", "nodes": n_with(UNDECLARED_TOOL_STORES)},
        {"relation": "grounding store → its producer", "nodes": n_with(UNDECLARED_STORE_WRITER)},
        {"relation": "source stream → signals table (ingest)", "nodes": n_with(UNDECLARED_SOURCE_SINK)},
        {"relation": "procedure: the step sequence inside a run", "nodes": n_with(UNDECLARED_PROCEDURE)},
        {"relation": "prompt text (held in code, referenced by module path)",
         "nodes": n_with("undeclared: the prompt text lives in code")},
        {"relation": "event trigger → its producer", "nodes": n_with(UNDECLARED_TRIGGER)},
        {"relation": "sampling actually sent (hard-coded per call site, gateway defaults)",
         "nodes": sum(1 for n in nodes if n.get("model_route"))},
    ]


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------


def _adj(edges: Sequence[Mapping[str, Any]]) -> tuple[dict, dict]:
    out_, in_ = defaultdict(set), defaultdict(set)
    for e in edges:
        out_[e["source"]].add(e["target"])
        in_[e["target"]].add(e["source"])
    return out_, in_


def _walk(start: Iterable[str], step: Mapping[str, set], limit: int | None = None) -> set[str]:
    seen = set(start)
    frontier = set(start)
    depth = 0
    while frontier and (limit is None or depth < limit):
        nxt = set()
        for n in frontier:
            nxt |= step.get(n, set())
        frontier = nxt - seen
        seen |= frontier
        depth += 1
    return seen


def apply_scope(graph: Mapping[str, Any], scope: str | None, *, depth: int = 1) -> dict[str, Any]:
    """Filter a derived graph. ``scope``:

    * ``all`` / empty — everything;
    * ``family:<group>`` — the group's nodes plus their direct neighbours
      (marked ``context: true``);
    * ``target:<descriptor_id>`` — the target's chain: its feeding sources and
      everything downstream of it;
    * ``node:<node id>`` — the neighbourhood within ``depth`` hops, both ways.

    Unknown scopes raise ``ValueError`` (the route answers 400).
    """
    nodes, edges = list(graph["nodes"]), list(graph["edges"])
    if not scope or scope == "all":
        return {**graph, "scope": "all"}
    out_, in_ = _adj(edges)
    ids = {n["id"] for n in nodes}
    kind, _, arg = scope.partition(":")
    if kind == "family":
        core = {n["id"] for n in nodes if n["group"] == arg}
        if not core:
            raise ValueError(f"no nodes in group {arg!r}")
        keep = core | _walk(core, out_, 1) | _walk(core, in_, 1)
    elif kind == "target":
        start = f"target:{arg}"
        if start not in ids:
            raise ValueError(f"no target {arg!r}")
        core = _walk([start], out_) | _walk([start], in_, 1)
        keep = core
    elif kind == "node":
        if arg not in ids:
            raise ValueError(f"no node {arg!r}")
        d = max(1, min(int(depth), 3))
        core = {arg}
        keep = _walk([arg], out_, d) | _walk([arg], in_, d)
    else:
        raise ValueError(f"unknown scope {scope!r}")
    kept_nodes = [dict(n, context=n["id"] not in core) for n in nodes if n["id"] in keep]
    kept_edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return {**graph, "nodes": kept_nodes, "edges": kept_edges, "scope": scope}


__all__ = [
    "DRAWN_STATES",
    "KNOWN_FAMILIES",
    "UNDECLARED_CONSUMERS",
    "apply_scope",
    "derive_graph",
    "inexpressible_relations",
    "node_id",
    "source_scope_match",
    "target_ctx",
]
