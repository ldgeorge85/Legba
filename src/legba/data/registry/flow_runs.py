# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run shaping for the Flow canvas — ``analyst_traces`` rows → sizes and ids.

Pure functions over a trace row as asyncpg returns it (``jsonb`` columns may
arrive as ``str``). The rule is SIZES AND IDS, NOT PAYLOADS: a step carries its
scalar fields and the byte size of anything nested; an LLM call carries its
token counts, finish reason and the LENGTH of its completion, not the text; a
row ref is an id. Bodies are fetched one at a time through the capped
``/flow/run/{run_id}/body`` sub-route (:func:`body_part`).

The step sequence is what the run RECORDED (``intermediate_steps`` in array
order) — the procedure lives in code, so the canvas labels it "observed, not
declared".

Registry-slim: stdlib only.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Sequence

#: The largest body the per-step fetch returns, in bytes of JSON text.
BODY_CAP_BYTES = 65_536

#: Scalar strings longer than this are cut in step summaries.
SCALAR_CAP = 200

#: How many input row ids a run detail lists (the count is always exact).
INPUT_REF_LIST_CAP = 50

#: Parts the body sub-route serves.
BODY_PARTS = ("step", "llm_call", "tool_call", "prompt", "prompt_tail",
              "input_payload", "output_payload", "error_payload")

#: Keys on an output row's ``data`` (or ``data.data``) that carry a gate or
#: check verdict. Whitelisted, so a new key is shown only once someone names it.
GATE_KEYS = (
    "verification", "span_checks", "external_audit", "level_indicator_demotions",
    "faithfulness_score", "guard_suppressed", "guard_suppression_reason",
    "refusal", "bearing_gate", "evidence_gate", "field_notes_fallback", "honesty_note",
)


def _json(v: Any) -> Any:
    if isinstance(v, (str, bytes, bytearray)):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _size(v: Any) -> int:
    if v is None:
        return 0
    try:
        return len(json.dumps(v, default=str, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(v).encode("utf-8"))


def _iso(v: Any) -> str | None:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v is not None else None


def _duration_ms(start: Any, end: Any) -> int | None:
    if isinstance(start, datetime) and isinstance(end, datetime):
        return int((end - start).total_seconds() * 1000)
    return None


def _scalar_summary(d: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (bool, int, float)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v if len(v) <= SCALAR_CAP else v[:SCALAR_CAP] + f"… [{len(v)} chars]"
        else:
            out[k] = {"_bytes": _size(v)}
    return out


def _list(v: Any) -> list:
    v = _json(v)
    return v if isinstance(v, list) else []


def llm_call_summary(i: int, c: Any) -> dict[str, Any]:
    c = c if isinstance(c, Mapping) else {}
    text = c.get("completion_text")
    keep = ("at", "leg", "model", "component_id", "subprovider", "status", "duration_ms",
            "prompt_chars", "prompt_tokens", "completion_tokens", "reasoning_tokens",
            "total_tokens", "finish_reason", "tool_call_count", "prompt_sha256", "error")
    out = {k: c.get(k) for k in keep if k in c}
    out["index"] = i
    out["completion_chars"] = len(text) if isinstance(text, str) else None
    return out


def tool_call_summary(i: int, t: Any) -> dict[str, Any]:
    t = t if isinstance(t, Mapping) else {}
    out = {k: t.get(k) for k in ("at", "name", "pack", "status", "duration_ms", "admitted",
                                 "units", "source", "error", "outcome") if k in t}
    out["index"] = i
    out["args_bytes"] = _size(t.get("args"))
    for k in ("result", "result_preview", "output"):
        if k in t:
            out["result_bytes"] = _size(t.get(k))
            break
    return out


def step_summary(i: int, s: Any) -> dict[str, Any]:
    s = s if isinstance(s, Mapping) else {"value": s}
    out = _scalar_summary(s)
    out["index"] = i
    out["bytes"] = _size(s)
    return out


def step_summaries(steps: Any) -> list[dict[str, Any]]:
    return [step_summary(i, s) for i, s in enumerate(_list(steps))]


def tokens_of(llm_calls: Any) -> int:
    total = 0
    for c in _list(llm_calls):
        if isinstance(c, Mapping) and isinstance(c.get("total_tokens"), (int, float)):
            total += int(c["total_tokens"])
    return total


def run_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """One line of the node's recent-runs list."""
    calls = _list(row.get("llm_calls"))
    finish: dict[str, int] = {}
    for c in calls:
        if isinstance(c, Mapping):
            fr = str(c.get("finish_reason"))
            finish[fr] = finish.get(fr, 0) + 1
    tokens = row.get("tokens")
    return {
        "run_id": str(row.get("run_id")),
        "analyst_version": row.get("analyst_version"),
        "target_id": row.get("target_id"),
        "trigger": row.get("cadence_trigger"),
        "status": row.get("status"),
        "started_at": _iso(row.get("run_started_at")),
        "duration_ms": _duration_ms(row.get("run_started_at"), row.get("run_ended_at")),
        "tokens": int(tokens) if isinstance(tokens, (int, float)) else tokens_of(calls),
        "llm_calls": len(calls) if calls else row.get("n_llm_calls"),
        "tool_calls": row.get("n_tool_calls"),
        "finish_reasons": finish,
    }


def observed_calls(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per (component, model, leg) over recent runs: what was actually served."""
    agg: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        for c in _list(r.get("llm_calls")):
            if not isinstance(c, Mapping):
                continue
            key = (c.get("component_id"), c.get("model"), c.get("leg") or "main")
            a = agg.setdefault(key, {"component_id": key[0], "model": key[1], "leg": key[2],
                                     "calls": 0, "finish_reasons": {}, "max_completion_tokens": 0,
                                     "max_prompt_tokens": 0})
            a["calls"] += 1
            fr = str(c.get("finish_reason"))
            a["finish_reasons"][fr] = a["finish_reasons"].get(fr, 0) + 1
            for src, dst in (("completion_tokens", "max_completion_tokens"), ("prompt_tokens", "max_prompt_tokens")):
                v = c.get(src)
                if isinstance(v, (int, float)) and v > a[dst]:
                    a[dst] = int(v)
    return sorted(agg.values(), key=lambda a: (str(a["component_id"]), str(a["leg"])))


def gate_verdicts(data: Any) -> dict[str, Any]:
    """The whitelisted verdict keys on an output row's data, size-capped."""
    data = _json(data)
    if not isinstance(data, Mapping):
        return {}
    out: dict[str, Any] = {}
    for scope, d in (("data", data), ("data.data", _json(data.get("data")))):
        if not isinstance(d, Mapping):
            continue
        for k in GATE_KEYS:
            if k in d:
                v = d[k]
                out[f"{scope}.{k}"] = v if _size(v) <= 4096 else {"_bytes": _size(v), "_truncated": True}
    for k in ("scores", "overall_score", "judge_route", "judge_model"):
        if k in data:
            out[f"data.{k}"] = data[k]
    return out


def run_detail(
    row: Mapping[str, Any],
    output_rows: Sequence[Mapping[str, Any]] = (),
    critiques: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    steps = _list(row.get("intermediate_steps"))
    calls = _list(row.get("llm_calls"))
    tools = _list(row.get("tool_calls"))
    in_refs = [str(x) for x in (row.get("input_row_refs") or [])]
    out_refs = [str(x) for x in (row.get("output_row_refs") or [])]
    prompt = row.get("prompt_rendered")
    tail = (_json(row.get("input_payload")) or {})
    tail = tail.get("prompt_tail") if isinstance(tail, Mapping) else None
    by_output: dict[str, list] = {}
    for c in critiques:
        by_output.setdefault(str(c.get("analyzed_output_id")), []).append({
            "id": str(c.get("id")), "produced_at": _iso(c.get("produced_at")),
            **gate_verdicts(c.get("data")),
        })
    outputs = []
    for o in output_rows:
        oid = str(o.get("id"))
        outputs.append({
            "id": oid, "table": o.get("table"), "kind": o.get("kind"), "title": o.get("title"),
            "schema_uri": o.get("schema_uri"), "produced_at": _iso(o.get("produced_at")),
            "bytes": o.get("bytes"), "honesty_flags": o.get("honesty_flags"),
            "gates": gate_verdicts(o.get("data")), "judge_critiques": by_output.get(oid, []),
        })
    err = _json(row.get("error_payload"))
    return {
        "run_id": str(row.get("run_id")),
        "analyst_id": row.get("analyst_id"),
        "analyst_version": row.get("analyst_version"),
        "target_id": row.get("target_id"),
        "trigger": row.get("cadence_trigger"),
        "status": row.get("status"),
        "started_at": _iso(row.get("run_started_at")),
        "ended_at": _iso(row.get("run_ended_at")),
        "duration_ms": _duration_ms(row.get("run_started_at"), row.get("run_ended_at")),
        "tokens": tokens_of(calls),
        "receipt_hash": row.get("receipt_hash"),
        "prev_receipt_hash": row.get("prev_receipt_hash"),
        "prompt_sha256": row.get("prompt_sha256"),
        "prompt_chars": len(prompt) if isinstance(prompt, str) else None,
        "prompt_tail_chars": (tail or {}).get("total_chars") if isinstance(tail, Mapping) else None,
        "error": (err if _size(err) <= 2048 else {"_bytes": _size(err), "_truncated": True}) if err else None,
        "sequence_label": "observed, not declared",
        "steps": [step_summary(i, s) for i, s in enumerate(steps)],
        "llm_calls": [llm_call_summary(i, c) for i, c in enumerate(calls)],
        "tool_calls": [tool_call_summary(i, t) for i, t in enumerate(tools)],
        "input_refs": {"count": len(in_refs), "ids": in_refs[:INPUT_REF_LIST_CAP],
                       "listed": min(len(in_refs), INPUT_REF_LIST_CAP)},
        "output_refs": {"count": len(out_refs), "ids": out_refs},
        "input_payload_bytes": row.get("input_payload_bytes"),
        "output_payload_bytes": row.get("output_payload_bytes"),
        "outputs": outputs,
    }


def body_part(row: Mapping[str, Any], part: str, index: int | None) -> dict[str, Any]:
    """One body, capped at :data:`BODY_CAP_BYTES`. Raises ``KeyError`` for an
    unknown part and ``IndexError`` for an index out of range."""
    if part not in BODY_PARTS:
        raise KeyError(part)
    if part in ("step", "llm_call", "tool_call"):
        col = {"step": "intermediate_steps", "llm_call": "llm_calls", "tool_call": "tool_calls"}[part]
        items = _list(row.get(col))
        if index is None or not 0 <= index < len(items):
            raise IndexError(index)
        value: Any = items[index]
    elif part == "prompt":
        value = row.get("prompt_rendered")
    elif part == "prompt_tail":
        p = _json(row.get("input_payload"))
        value = p.get("prompt_tail") if isinstance(p, Mapping) else None
    else:
        value = _json(row.get(part))
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False, indent=1)
    raw = text.encode("utf-8")
    truncated = len(raw) > BODY_CAP_BYTES
    if truncated:
        text = raw[:BODY_CAP_BYTES].decode("utf-8", errors="ignore")
    return {"part": part, "index": index, "bytes": len(raw), "truncated": truncated,
            "cap_bytes": BODY_CAP_BYTES, "text": text}


__all__ = [
    "BODY_CAP_BYTES",
    "BODY_PARTS",
    "GATE_KEYS",
    "body_part",
    "gate_verdicts",
    "observed_calls",
    "run_detail",
    "run_summary",
    "step_summaries",
    "tokens_of",
]
