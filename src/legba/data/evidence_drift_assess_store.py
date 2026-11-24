# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The drift ASSESSMENT pass, the per-citer effect, the tracked auto-close and
the re-open (lane D-2).

THE PASS (:func:`assess_pending`), run right after D-1's classify pass:

1. every classification of a flag-writing class with no assessment under the
   CODE-ONLY stamp gets one (``assessor = code_only``) — always, whatever the
   model switch says;
2. with the model step on (``LEGBA_DRIFT_ASSESS_MODEL``, default OFF) and a
   handler wired, at most ``model_cap`` classifications per pass with no
   assessment under the MODEL stamp get one more (``code_and_model``);
3. each assessment row is written with one claim-effect row per flag of the
   classification (code verdict; with the model on, the model's answer for a
   ``not_checkable`` flag that carries a span, in its own columns).

All in one transaction per classification; a re-run writes nothing twice
(unique per (classification, stamp) and per (flag, assessment)).

SHADOW + AUTO-CLOSE (:func:`auto_close_pending`): every pass records each
flag the auto-close rule WOULD close in ``evidence_drift_would_auto_close``
(per rule version; never changes the flag) — that count is how the owner
knows when to switch it on. With ``LEGBA_DRIFT_AUTO_CLOSE`` on (default
OFF): an open or reviewed ORIGINAL flag whose latest assessment is a
spelling/name correction (by the final and the code type) and whose code
claim effect is ``cited_text_survives`` moves through D-1's lifecycle to
resolved / ``unchanged_in_substance`` as :data:`AUTO_CLOSE_PRINCIPAL`, with a
note naming the assessment and the rule version, and a row in
``evidence_drift_auto_closes``.

RE-OPEN (:func:`reopen_flag`): an auto-closed flag can be re-opened by a
person or an agent: a NEW open flag row with ``reopened_from``,
``reopened_by`` and the note (D-1's trigger never lets a flag move back).
A re-opened flag is never auto-closed again.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .evidence_drift_assess import (
    ASSESS_RULES_VERSION,
    AUTO_CLOSE_RULE_VERSION,
    Assessment,
    assess_code,
    auto_close_allowed,
    claim_effect,
    full_diff,
    page_stamps,
)
from .evidence_drift_assess_model import (
    ASSESS_PROMPT_VERSION,
    assess_prompt,
    claim_effect_prompt,
    combine,
    parse_assessment,
    parse_claim_effect,
    run_model,
    stamp,
)
from .evidence_drift_classify import FLAGGING_CLASSES

logger = logging.getLogger(__name__)

__all__ = [
    "AUTO_CLOSE_ENV",
    "AUTO_CLOSE_PRINCIPAL",
    "MODEL_CAP_ENV",
    "MODEL_ENV",
    "AssessReport",
    "AutoCloseReport",
    "assess_classification",
    "assess_pending",
    "auto_close_enabled",
    "auto_close_pending",
    "model_enabled",
    "reopen_flag",
]

MODEL_ENV = "LEGBA_DRIFT_ASSESS_MODEL"
MODEL_CAP_ENV = "LEGBA_DRIFT_ASSESS_MODEL_PER_PASS"
AUTO_CLOSE_ENV = "LEGBA_DRIFT_AUTO_CLOSE"
AUTO_CLOSE_PRINCIPAL = "system::drift_assessor"
DEFAULT_MODEL_CAP = 5
DEFAULT_ASSESS_LIMIT = 50
#: Model calls for not-checkable citers, at most this many per change.
MODEL_EFFECTS_PER_CHANGE = 5
_ON = frozenset({"1", "on", "true", "yes"})


def model_enabled() -> bool:
    return os.environ.get(MODEL_ENV, "").strip().lower() in _ON


def auto_close_enabled() -> bool:
    return os.environ.get(AUTO_CLOSE_ENV, "").strip().lower() in _ON


def model_cap() -> int:
    try:
        return max(0, int(os.environ.get(MODEL_CAP_ENV, "") or DEFAULT_MODEL_CAP))
    except ValueError:
        return DEFAULT_MODEL_CAP


@dataclass
class AssessReport:
    code_only: int = 0
    with_model: int = 0
    model_failed: int = 0
    claim_effects: int = 0
    history_rewrites: int = 0
    late_unexplained: int = 0
    model_doubts: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    errors: int = 0


_PENDING_SQL = """
SELECT c.id, c.drift_id, c.address, c.source_id, c.host, c.earlier_version_id, c.later_version_id,
       c.earlier_fetched_at, c.later_fetched_at, c.drift_class, c.rule, c.evidence,
       d.later_status
  FROM evidence_drift_classifications c
  JOIN fetch_drift_observations d ON d.id = c.drift_id
 WHERE c.drift_class = ANY($1::text[])
   AND NOT EXISTS (SELECT 1 FROM evidence_drift_assessments a
                    WHERE a.classification_id = c.id AND a.assessor_stamp = $2)
 ORDER BY c.id
 LIMIT $3
"""

_VERSIONS_SQL = """
SELECT id, address, sha256, extracted_text, object_ref, bytes_pruned_at, first_seen_at, last_seen_at
  FROM fetch_versions WHERE id = ANY($1::bigint[])
"""

_FIRST_READ_SQL = """
SELECT min(first_seen_at) FROM fetch_versions WHERE address = $1 AND receipt_kind = 'fetch'
"""

_FLAGS_SQL = """
SELECT id, cited_span, span_check, span_in_earlier, span_in_later
  FROM evidence_drift_flags WHERE classification_id = $1 AND reopened_from IS NULL ORDER BY id
"""

_INSERT_ASSESSMENT_SQL = """
INSERT INTO evidence_drift_assessments (
    classification_id, drift_id, address, source_id, host, assessor, assessor_stamp, rules_version,
    prompt_version, model_id, model_status, model_output, change_type, component_types, type_source,
    code_type, code_components, unassessable_reason, acknowledged, acknowledged_source,
    acknowledged_evidence, scope_pairs, removed_claims, claims_changed, share_changed, interval_hours,
    timing_bucket, timing_basis, standing_hours, summary, observations, history_rewrite,
    history_rewrite_inputs, acknowledged_signals, late_unexplained_change, late_unexplained_inputs,
    model_type_doubt
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14::text[], $15, $16,
          $17::text[], $18, $19, $20, $21::jsonb, $22::jsonb, $23::jsonb, $24, $25, $26, $27, $28, $29,
          $30, $31::jsonb, $32, $33::jsonb, $34::text[], $35, $36::jsonb, $37::jsonb)
ON CONFLICT (classification_id, assessor_stamp) DO NOTHING
RETURNING id
"""

_INSERT_EFFECT_SQL = """
INSERT INTO evidence_drift_claim_effects (
    flag_id, assessment_id, claim_effect, after_text, basis, model_status, model_effect,
    model_after_text, model_output
) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb)
ON CONFLICT (flag_id, assessment_id) DO NOTHING
RETURNING id
"""


def _j(v: Any) -> str:
    return json.dumps(v, default=lambda x: x.isoformat() if isinstance(x, datetime) else str(x))


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


async def _bytes_of(conn: Any, row: Any) -> Optional[bytes]:
    """A version's stored bytes, best-effort (pruned or unreadable -> None)."""
    if row is None or row["object_ref"] is None or row["bytes_pruned_at"] is not None:
        return None
    try:
        from .archive import archive_root  # noqa: PLC0415
        from .archive_store import EvidenceStore  # noqa: PLC0415
        from .archive_tier import read_original  # noqa: PLC0415

        store = EvidenceStore.from_env(archive_root())
        return await read_original(conn, store, str(row["sha256"]))
    except Exception as exc:  # noqa: BLE001 — stamps are a bonus
        logger.debug("evidence_drift_assess.bytes_unreadable version=%s err=%s", row["id"], exc)
        return None


def _span_check(flag: Any) -> Optional[dict[str, Any]]:
    sc = _json(flag["span_check"])
    if isinstance(sc, dict):
        return sc
    if flag["span_in_earlier"] is None:
        return None
    return {"span_in_earlier": flag["span_in_earlier"], "span_in_later": flag["span_in_later"],
            "missing_in_later": []}


async def assess_classification(
    conn: Any, cls: Any, *, llm: Any = None, model_id: Optional[str] = None,
    write: bool = True, read_bytes: bool = True,
) -> dict[str, Any]:
    """Assess one classification (code only when ``llm`` is None) and write
    the assessment and its claim effects. ``write=False`` computes the same
    and writes nothing. Returns a summary."""
    ids = [int(cls["earlier_version_id"])]
    if cls["later_version_id"] is not None:
        ids.append(int(cls["later_version_id"]))
    vers = {int(r["id"]): r for r in await conn.fetch(_VERSIONS_SQL, ids)}
    earlier = vers.get(int(cls["earlier_version_id"]))
    later = vers.get(int(cls["later_version_id"])) if cls["later_version_id"] is not None else None
    diff = full_diff(earlier["extracted_text"] if earlier is not None else None,
                     later["extracted_text"] if later is not None else None)
    ev = _json(cls["evidence"]) or {}
    titles = ev.get("titles") or {}
    e_stamps = page_stamps(await _bytes_of(conn, earlier)) if read_bytes else {}
    l_stamps = page_stamps(await _bytes_of(conn, later)) if read_bytes and later is not None else {}
    first_read = await conn.fetchval(_FIRST_READ_SQL, cls["address"])
    code = assess_code(
        drift_class=cls["drift_class"], rule=cls["rule"], diff=diff, address=cls["address"],
        earlier_title=titles.get("earlier"), later_title=titles.get("later"),
        earlier_stamps=e_stamps, later_stamps=l_stamps,
        earlier_seen_at=first_read or (earlier["first_seen_at"] if earlier is not None else None)
        or cls["earlier_fetched_at"],
        earlier_last_seen_at=(earlier["last_seen_at"] if earlier is not None else None) or cls["earlier_fetched_at"],
        later_seen_at=cls["later_fetched_at"], later_status=cls["later_status"],
        retention=ev.get("retention"),
    )
    final: Assessment = code
    model_status, model_output = "not_run", None
    if llm is not None:
        content, err = await run_model(llm, assess_prompt(
            diff, code_type=code.code_type, code_components=list(code.code_components),
            earlier_title=titles.get("earlier"), later_title=titles.get("later")))
        if err is not None:
            from .evidence_drift_assess_model import ModelResult  # noqa: PLC0415

            res = ModelResult("call_failed", reason=err)
        else:
            res = parse_assessment(content or "", diff)
        model_status, model_output = res.status, res.as_dict()
        final = combine(code, res)
        if res.status != "ok":
            logger.warning("evidence_drift_assess.model_failed classification=%s status=%s reason=%s "
                           "(the code assessment and summary stand)",
                           cls["id"], res.status, res.reason)
    flags = await conn.fetch(_FLAGS_SQL, int(cls["id"]))
    effects: list[dict[str, Any]] = []
    effect_calls = 0
    for f in flags:
        eff, after, basis = claim_effect(_span_check(f), diff, drift_class=cls["drift_class"])
        row = {"flag_id": int(f["id"]), "effect": eff, "after": after, "basis": basis,
               "model_status": "not_run", "model_effect": None, "model_after": None, "model_output": None}
        if llm is not None and eff == "not_checkable" and f["cited_span"] \
                and effect_calls < MODEL_EFFECTS_PER_CHANGE:
            effect_calls += 1
            content, err = await run_model(llm, claim_effect_prompt(f["cited_span"], diff))
            if err is None:
                r = parse_claim_effect(content or "", diff)
                row.update(model_status=r.status, model_output=r.as_dict(),
                           model_effect=r.data.get("effect"), model_after=r.data.get("after_quote"))
            else:
                row.update(model_status="call_failed", model_output={"reason": err})
        effects.append(row)
    st = stamp(ASSESS_PROMPT_VERSION if llm is not None else None, model_id if llm is not None else None)
    summary = {
        "classification_id": int(cls["id"]), "stamp": st, "assessment": final.as_dict(),
        "model_status": model_status, "model_output": model_output, "effects": effects,
        "assessment_id": None, "written": False,
    }
    if not write:
        return summary
    async with conn.transaction():
        aid = await conn.fetchval(
            _INSERT_ASSESSMENT_SQL, int(cls["id"]), int(cls["drift_id"]), cls["address"], cls["source_id"],
            cls["host"], "code_and_model" if llm is not None else "code_only", st, ASSESS_RULES_VERSION,
            ASSESS_PROMPT_VERSION if llm is not None else None, model_id if llm is not None else None,
            model_status, None if model_output is None else _j(model_output), final.change_type,
            list(final.component_types), final.type_source, final.code_type, list(final.code_components),
            final.unassessable_reason, final.acknowledged, final.acknowledged_source,
            _j(final.acknowledged_evidence), _j(final.scope_pairs), _j(final.removed_claims),
            final.claims_changed, final.share_changed, final.interval_hours, final.timing_bucket,
            final.timing_basis, final.standing_hours, final.summary, _j(final.observations),
            final.history_rewrite, _j(final.history_rewrite_inputs), list(final.acknowledged_signals),
            final.late_unexplained_change, _j(final.late_unexplained_inputs),
            None if final.model_type_doubt is None else _j(final.model_type_doubt),
        )
        if aid is None:
            return summary
        for e in effects:
            await conn.fetchval(
                _INSERT_EFFECT_SQL, e["flag_id"], aid, e["effect"], e["after"], _j(e["basis"]),
                e["model_status"], e["model_effect"], e["model_after"],
                None if e["model_output"] is None else _j(e["model_output"]))
    summary.update(assessment_id=int(aid), written=True)
    return summary


async def assess_pending(
    pool: Any, *, limit: int = DEFAULT_ASSESS_LIMIT, llm: Any = None, model_id: Optional[str] = None,
    model_cap_n: Optional[int] = None, read_bytes: bool = True,
) -> AssessReport:
    """One bounded assessment pass: code-only for every pending change, then
    the model step for at most ``model_cap_n`` (when ``llm`` is wired)."""
    report = AssessReport()
    classes = sorted(FLAGGING_CLASSES)
    async with pool.acquire() as conn:
        for row in await conn.fetch(_PENDING_SQL, classes, stamp(None, None), int(limit)):
            await _one(conn, row, report, llm=None, model_id=None, read_bytes=read_bytes)
        cap = model_cap() if model_cap_n is None else int(model_cap_n)
        if llm is not None and model_id and cap > 0:
            st = stamp(ASSESS_PROMPT_VERSION, model_id)
            for row in await conn.fetch(_PENDING_SQL, classes, st, cap):
                await _one(conn, row, report, llm=llm, model_id=model_id, read_bytes=read_bytes)
    return report


async def _one(conn: Any, row: Any, report: AssessReport, *, llm: Any, model_id: Optional[str],
               read_bytes: bool) -> None:
    try:
        s = await assess_classification(conn, row, llm=llm, model_id=model_id, read_bytes=read_bytes)
    except Exception as exc:  # noqa: BLE001 — one bad row must not stop the pass
        report.errors += 1
        logger.warning("evidence_drift_assess.failed classification=%s err=%s", row["id"], exc)
        return
    if not s["written"]:
        return
    a = s["assessment"]
    if llm is None:
        report.code_only += 1
    else:
        report.with_model += 1
        report.model_failed += s["model_status"] != "ok"
    report.claim_effects += len(s["effects"])
    report.history_rewrites += bool(a["history_rewrite"])
    report.late_unexplained += bool(a["late_unexplained_change"])
    report.model_doubts += a.get("model_type_doubt") is not None
    report.by_type[a["change_type"]] = report.by_type.get(a["change_type"], 0) + 1


# -- auto-close ----------------------------------------------------------------------------

#: The latest assessment of each open original flag's change, with the code
#: claim effect for that flag under it. $2: also return flags already in the
#: shadow table (only when closing).
_AUTO_CLOSE_CANDIDATES_SQL = """
SELECT f.id AS flag_id, a.id AS assessment_id, a.assessor_stamp, a.change_type, a.code_type,
       e.id AS effect_id, e.claim_effect
  FROM evidence_drift_flags f
  JOIN LATERAL (SELECT * FROM evidence_drift_assessments a
                 WHERE a.classification_id = f.classification_id
                 ORDER BY a.assessed_at DESC, a.id DESC LIMIT 1) a ON true
  JOIN evidence_drift_claim_effects e ON e.flag_id = f.id AND e.assessment_id = a.id
 WHERE f.state <> 'resolved' AND f.reopened_from IS NULL
   AND NOT EXISTS (SELECT 1 FROM evidence_drift_auto_closes x WHERE x.flag_id = f.id)
   AND NOT EXISTS (SELECT 1 FROM evidence_drift_flags r WHERE r.reopened_from = f.id)
   AND ($2 OR NOT EXISTS (SELECT 1 FROM evidence_drift_would_auto_close w
                           WHERE w.flag_id = f.id AND w.rule_version = $3))
   AND a.change_type = 'spelling_or_name_correction'
 ORDER BY f.id
 LIMIT $1
"""


@dataclass
class AutoCloseReport:
    would_close: int = 0
    closed: int = 0


async def auto_close_pending(pool: Any, *, close: bool = False, limit: int = 200) -> AutoCloseReport:
    """Record every flag the auto-close rule allows in the SHADOW table
    (always), and — only when ``close`` (the caller passes the switch) —
    resolve it as :data:`AUTO_CLOSE_PRINCIPAL`."""
    from .evidence_drift_store import resolve_flag_conn  # noqa: PLC0415

    rep = AutoCloseReport()
    async with pool.acquire() as conn:
        for c in await conn.fetch(_AUTO_CLOSE_CANDIDATES_SQL, int(limit), bool(close), AUTO_CLOSE_RULE_VERSION):
            if not auto_close_allowed(change_type=c["change_type"], code_type=c["code_type"],
                                      code_effect=c["claim_effect"]):
                continue
            async with conn.transaction():
                shadow = await conn.fetchval(
                    "INSERT INTO evidence_drift_would_auto_close (flag_id, rule_version, assessment_id, "
                    "claim_effect_id, switch_on) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING "
                    "RETURNING flag_id", int(c["flag_id"]), AUTO_CLOSE_RULE_VERSION, int(c["assessment_id"]),
                    int(c["effect_id"]), bool(close))
                rep.would_close += shadow is not None
                if not close:
                    continue
                note = (f"auto-closed by the drift assessor: assessment {c['assessment_id']} "
                        f"({c['assessor_stamp']}) says spelling/name correction and the cited text "
                        f"survives (code check); rule {AUTO_CLOSE_RULE_VERSION}. Re-open with "
                        "reopen_drift_flag.")
                state = await resolve_flag_conn(conn, int(c["flag_id"]), principal=AUTO_CLOSE_PRINCIPAL,
                                                resolution="unchanged_in_substance", note=note)
                if state != "resolved":
                    continue
                await conn.execute(
                    "INSERT INTO evidence_drift_auto_closes (flag_id, assessment_id, claim_effect_id, "
                    "rule_version, principal) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    int(c["flag_id"]), int(c["assessment_id"]), int(c["effect_id"]),
                    AUTO_CLOSE_RULE_VERSION, AUTO_CLOSE_PRINCIPAL)
                rep.closed += 1
    if rep.would_close or rep.closed:
        logger.info("evidence_drift_assess.auto_close would=%d closed=%d switch=%s rule=%s",
                    rep.would_close, rep.closed, close, AUTO_CLOSE_RULE_VERSION)
    return rep


_REOPEN_SQL = """
INSERT INTO evidence_drift_flags (
    classification_id, drift_id, address, source_id, earlier_version_id, later_version_id,
    drift_class, dependant_kind, dependant_id, depth, via_kind, via_id, record_version_id,
    cited_span, span_check, span_in_earlier, span_in_later, reopened_from, reopened_by, reopen_note
)
SELECT classification_id, drift_id, address, source_id, earlier_version_id, later_version_id,
       drift_class, dependant_kind, dependant_id, depth, via_kind, via_id, record_version_id,
       cited_span, span_check, span_in_earlier, span_in_later, id, $2, $3
  FROM evidence_drift_flags
 WHERE id = $1 AND state = 'resolved'
ON CONFLICT (reopened_from) WHERE reopened_from IS NOT NULL DO NOTHING
RETURNING id
"""


async def reopen_flag(pool: Any, flag_id: int, *, principal: str, note: str) -> dict[str, Any]:
    """Re-open an AUTO-CLOSED flag: a new open flag row linked by
    ``reopened_from``. Refuses a flag a person or a unit resolved (their
    resolution stands until they act) and a second re-open."""
    from .evidence_drift_store import _note, _principal  # noqa: PLC0415

    who = _principal(principal)
    why = _note(note)
    if not why:
        raise ValueError("a note saying why the flag is re-opened is required")
    async with pool.acquire() as conn:
        auto = await conn.fetchval("SELECT 1 FROM evidence_drift_auto_closes WHERE flag_id = $1", int(flag_id))
        if auto is None:
            known = await conn.fetchval("SELECT state FROM evidence_drift_flags WHERE id = $1", int(flag_id))
            return {"status": "unknown_flag" if known is None else "not_auto_closed"}
        new_id = await conn.fetchval(_REOPEN_SQL, int(flag_id), who, why)
        if new_id is None:
            existing = await conn.fetchval(
                "SELECT id FROM evidence_drift_flags WHERE reopened_from = $1", int(flag_id))
            return {"status": "already_reopened", "flag_id": existing}
    return {"status": "reopened", "flag_id": int(new_id), "reopened_from": int(flag_id)}
