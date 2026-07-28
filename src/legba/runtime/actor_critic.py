# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Critic context resolution (DB-reading free fns) — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Mapping
from uuid import UUID

import asyncpg

from ..data.schemas.analyst import AnalystDescriptor

if TYPE_CHECKING:  # pragma: no cover - typing-only; avoids dapr_actors import cycle
    from .dapr_actors import _AnalystDeps

logger = logging.getLogger(__name__)


def _extract_primary_model_ref(descriptor: AnalystDescriptor) -> str:
    """Resolve an LLM model string from an analyst descriptor.

    The descriptor's ``method.llm.primary`` slot carries a property-factory
    StackRef dump — typically ``{"raw": "llm.primary.openai_compat", ...}``.
    We surface the ``raw`` value (the StackRef path) as the canonical model
    identity so the critic's heterogeneity guard can compare it against
    the critic's own LLM subprovider string.

    Returns an empty string if the descriptor has no resolvable LLM
    primary — the heterogeneity guard handles missing identity as an
    audit-gap warning rather than a hard failure.
    """
    method = getattr(descriptor, "method", None)
    if method is None:
        return ""
    llm = getattr(method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return ""
    primary = llm.get("primary")
    if isinstance(primary, Mapping):
        return str(primary.get("raw") or "")
    if isinstance(primary, str):
        return primary
    return ""


async def _resolve_critic_context(
    conn: asyncpg.Connection,
    *,
    deps: "_AnalystDeps",
    target_filter: str | None,
    payload_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the critic-kind's per-run ``options`` dict.

    The critic kind's :func:`run_method` needs four runtime-supplied
    options keys (per L-105 / L-175):

      * ``rubric`` — the analyzed analyst's ``descriptor.eval.rubric``;
      * ``analyzed_model`` — primary LLM stack-ref of the analyzed
        analyst (so the heterogeneity guard can refuse self-correlation);
      * ``analyzed_output_id`` — the ``analyst_outputs.id`` (UUID) of the
        row being graded;
      * ``allow_self_correlated`` — the analyzed analyst's
        ``descriptor.eval.allow_self_correlated`` (typed schema field
        per L-105 §3 / Wave-B integration; legacy
        ``eval.optimizer["allow_self_correlated"]`` is accepted as a
        fall-through for descriptors that predate the typed field).

    The helper looks up the analyzed analyst's descriptor via the
    ``analyst_descriptors`` table (head row), parses the JSONB body
    locally to avoid coupling to the registry's Python surface, and
    returns the assembled options dict ready for ``options.update(...)``
    in the actor's run path.

    The analyzed analyst's id is resolved in this priority order:

      1. ``payload_options["analyzed_analyst_id"]`` (caller passes
         explicitly — the production runtime path),
      2. critic descriptor's ``eval.optimizer["analyzed_analyst_id"]``
         (descriptor-pinned target — a critic that exclusively grades
         one analyst can hardcode this).
      3. ``target_filter`` (legacy code-paths that pass the analyzed
         analyst's id via the ``target_filter`` channel).

    Returns an empty dict when the analyzed analyst can't be resolved —
    the critic kind's own missing-rubric / missing-model handling then
    surfaces the gap (raises :class:`MissingRubricError` or logs the
    heterogeneity-guard warning).
    """
    payload_options = payload_options or {}
    out: dict[str, Any] = {}

    # 1. Identify the analyzed analyst id.
    analyzed_id: str | None = (
        payload_options.get("analyzed_analyst_id")
        or _critic_descriptor_pinned_analyst_id(deps.descriptor)
        or target_filter
    )
    if not analyzed_id:
        return out

    # 2. Identify the analyzed-output row id (the row being graded).
    analyzed_output_id = payload_options.get("analyzed_output_id")
    if analyzed_output_id is not None:
        out["analyzed_output_id"] = str(analyzed_output_id)

    # 3. Look up the analyzed analyst's descriptor body.
    row = await conn.fetchrow(
        "SELECT body FROM analyst_descriptors "
        "WHERE descriptor_id = $1 AND is_head = TRUE",
        analyzed_id,
    )
    if row is None:
        # No descriptor found — return what we have so far; the critic
        # kind's MissingRubricError surfaces the gap downstream.
        return out

    body = row["body"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            body = None
    if not isinstance(body, dict):
        return out

    eval_block = body.get("eval") if isinstance(body.get("eval"), dict) else None
    method_block = body.get("method") if isinstance(body.get("method"), dict) else None

    # rubric
    if eval_block and isinstance(eval_block.get("rubric"), str):
        out["rubric"] = eval_block["rubric"]

    # allow_self_correlated — typed field first, legacy optimizer-dict second.
    if eval_block is not None:
        typed = eval_block.get("allow_self_correlated")
        if isinstance(typed, bool):
            out["allow_self_correlated"] = typed
        else:
            opt = eval_block.get("optimizer") or {}
            if isinstance(opt, dict):
                legacy = opt.get("allow_self_correlated")
                if isinstance(legacy, bool):
                    out["allow_self_correlated"] = legacy

    # analyzed_model — primary LLM stack ref of the analyzed analyst.
    if method_block is not None:
        llm = method_block.get("llm") or {}
        if isinstance(llm, dict):
            primary = llm.get("primary")
            if isinstance(primary, dict):
                raw = primary.get("raw")
                if isinstance(raw, str) and raw:
                    out["analyzed_model"] = raw
            elif isinstance(primary, str) and primary:
                out["analyzed_model"] = primary

    # Stamp the analyzed analyst id + version so the critic's row
    # carries the full provenance without the kind re-querying.
    out.setdefault("analyzed_analyst_id", analyzed_id)
    version = body.get("identity", {}).get("version") if isinstance(body.get("identity"), dict) else None
    if isinstance(version, str) and version:
        out.setdefault("analyzed_analyst_version", version)

    return out


def _critic_descriptor_pinned_analyst_id(descriptor: AnalystDescriptor) -> str | None:
    """Return the descriptor-pinned analyzed analyst id, if any.

    A critic descriptor that exclusively grades one analyst can stamp
    the target via ``eval.optimizer["analyzed_analyst_id"]`` (mirrors
    the L-176 optimizer's analyzed-target pointer).  Returns ``None``
    when unset.
    """
    eval_block = getattr(descriptor, "eval", None)
    if eval_block is None:
        return None
    opt = getattr(eval_block, "optimizer", None) or {}
    if not isinstance(opt, Mapping):
        return None
    pinned = opt.get("analyzed_analyst_id")
    return pinned if isinstance(pinned, str) and pinned else None


# ---------------------------------------------------------------------------
# P0-T2 — MANDATORY faithfulness verify, persisted as a critique (the gate)
# ---------------------------------------------------------------------------


# V1 (journal verify profile): cap the raw source_text carried per resolved
# ref so a fat cited body can't blow the judge's context (mirrors the unit
# bridge's evidence cap ethos).
_JOURNAL_EVIDENCE_TEXT_CHARS = 4000

# T-3: the honesty flag appended to a journal entry row when the verify pass's
# support-judge marked ANY of its claims contradicted-by-its-own-source
# (UnsupportedSpan.reason == 'judge_contradicted'). Durable + renderable — the
# CONSEQUENCE arm of V1 (the gate DETECTED the Rubio inversion via judge_
# contradicted; this makes the finding stick on the entry, not just in the
# critique row). The verify report already CONTAINS this; T-3 only records it.
_JOURNAL_CONTRADICTED_FLAG = "contradicted_claims"


async def _stamp_journal_contradicted_flag(
    conn: "asyncpg.Connection", entry_id: Any
) -> None:
    """Append ``contradicted_claims`` to a journal entry's ``honesty_flags`` array
    (idempotent — never duplicates the flag). Degrade-not-fail: a write error is
    logged and swallowed; a monitoring/consequence write must never break the run
    that produced the entry (the entry already persisted; the critique already
    landed). ``journal_entries.honesty_flags`` is a Postgres ``text[]``."""
    try:
        await conn.execute(
            """
            UPDATE journal_entries
               SET honesty_flags = array_append(honesty_flags, $2)
             WHERE id = $1
               AND NOT (honesty_flags @> ARRAY[$2]::text[])
            """,
            entry_id,
            _JOURNAL_CONTRADICTED_FLAG,
        )
    except Exception as exc:  # pragma: no cover — never break the run
        logger.warning(
            "actor_critic.journal_verify.contradicted_flag_write_failed "
            "entry_id=%s err=%s", entry_id, exc,
        )


async def _resolve_journal_citation_bridge(
    conn: asyncpg.Connection, ordered_refs: list[str]
) -> list[dict[str, Any]]:
    """Resolve a journal entry's cited substrate uuids into the standard
    citations bridge the faithfulness verify binds ``[N]`` markers against.

    ``ordered_refs[N-1]`` is the uuid behind marker ``[N]`` (the contract from
    ``build_journal_verify_inputs``). Each ref may point at an
    ``analyst_outputs`` row (findings/instrument outputs the GATHER tools
    returned) or a raw ``signals`` row (the priming slice's citable ids) —
    try both. An unresolvable ref still gets a bridge entry with empty
    ``source_text`` (marked unresolved) so the ordinals never skew; the judge
    treats absent evidence honestly rather than mis-binding the rest."""
    entries: list[dict[str, Any]] = []
    if not ordered_refs:
        return entries
    uuids: list[UUID] = []
    for u in ordered_refs:
        try:
            uuids.append(UUID(u))
        except (ValueError, AttributeError):
            uuids.append(UUID(int=0))  # placeholder — resolves to nothing

    resolved: dict[str, dict[str, Any]] = {}

    async def _lookup(sql: str, keys: list[UUID]) -> list[Any]:
        try:
            return await conn.fetch(sql, keys)
        except Exception:  # noqa: BLE001 — a table absent on a slim deploy
            return []      # degrades to unresolved, never breaks the verify

    # 1) analyst_outputs (findings / instrument outputs the GATHER tools return)
    for r in await _lookup(
        "SELECT id, title, body, analyst_id FROM analyst_outputs "
        "WHERE id = ANY($1::uuid[])", uuids,
    ):
        resolved[str(r["id"]).lower()] = {
            "title": str(r["title"] or "")[:300],
            "source": str(r["analyst_id"] or "substrate"),
            "source_text": str(r["body"] or "")[:_JOURNAL_EVIDENCE_TEXT_CHARS],
        }
    # 2) signals (the priming slice's citable ids)
    still = [uu for u, uu in zip(ordered_refs, uuids) if u.lower() not in resolved]
    for r in await _lookup(
        "SELECT id, payload, canonical_url FROM signals WHERE id = ANY($1::uuid[])",
        still,
    ):
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        title = next(
            (str(payload[k]).strip() for k in ("title", "headline", "name")
             if isinstance(payload.get(k), str) and payload[k].strip()), "",
        )
        body_txt = next(
            (str(payload[k]).strip()
             for k in ("distilled_body", "summary", "body", "text", "content")
             if isinstance(payload.get(k), str) and payload[k].strip()), "",
        )
        resolved[str(r["id"]).lower()] = {
            "title": title[:300],
            "source": str(r["canonical_url"] or "signal")[:300],
            "source_text": body_txt[:_JOURNAL_EVIDENCE_TEXT_CHARS],
        }
    # 3) the remaining legitimate journal ref kinds (mirrors the UI chip
    #    resolver journal_api._REF_TABLES): situations / facts / nexuses /
    #    hypotheses. For these row kinds the composed label IS the substance
    #    (a fact's triple is its whole content), so it serves as source_text.
    #    journal_entries stays DELIBERATELY absent (§3.5 + B0-8 de-echo: a
    #    prior entry is the journal's own memory, not substrate evidence — a
    #    fact claim resting only on self-citation is honestly unsupported).
    for sql in (
        "SELECT id, name AS label, name AS text FROM situations WHERE id = ANY($1::uuid[])",
        "SELECT id, subject || ' ' || predicate || ' ' || value AS label, "
        "subject || ' ' || predicate || ' ' || value AS text "
        "FROM facts WHERE id = ANY($1::uuid[])",
        "SELECT id, label, label AS text FROM nexuses WHERE id = ANY($1::uuid[])",
        "SELECT id, LEFT(thesis, 240) AS label, thesis AS text "
        "FROM hypotheses WHERE id = ANY($1::uuid[])",
    ):
        still = [uu for u, uu in zip(ordered_refs, uuids) if u.lower() not in resolved]
        if not still:
            break
        for r in await _lookup(sql, still):
            resolved[str(r["id"]).lower()] = {
                "title": str(r["label"] or "")[:300],
                "source": "substrate",
                "source_text": str(r["text"] or "")[:_JOURNAL_EVIDENCE_TEXT_CHARS],
            }

    for i, u in enumerate(ordered_refs, start=1):
        entry: dict[str, Any] = {"marker": f"[{i}]"}
        hit = resolved.get(u.lower())
        if hit is not None:
            # signal_id ONLY on a genuinely resolved ref — the deterministic
            # floor counts non-empty signal_id as support, so stamping it on an
            # unresolved (possibly fabricated) uuid would let fabrication PASS
            # the floor whenever the judge soft-fails (adversarial review C-1;
            # the unit path's honesty guarantee, preserved).
            entry["signal_id"] = u
            entry.update(hit)
        else:
            entry["title"] = "(unresolved substrate ref)"
            entry["source"] = "unresolved"
            entry["source_text"] = ""
        entries.append(entry)
    return entries


async def verify_inline_target_finding(
    conn: asyncpg.Connection,
    *,
    deps: "_AnalystDeps",
    finding_id: UUID,
    finding_payload: Any,
    run_id: Any,
    target_id: str | None = None,
) -> dict[str, Any] | None:
    """Run the faithfulness verify pass over a just-emitted FINDING and PERSIST
    the verdict as a ``critique`` so the existing critic-actuation gate folds
    ``overall_score`` into ``effective_confidence = min(confidence,
    overall_score)``.

    Handles TWO citation conventions through the single generalized verify pass
    (name retained for its callers):

      * ``inline_target`` — the unit ``[N]`` → signal bridge (P0-T2), ALWAYS
        verified (a finding with no citations floors honestly-low).
      * ``meta_findings_synthesizer`` — the per-country COMPOSITION's
        ``[[ref:<uuid>]]`` → sub-claim bridge (P3-T3/T7). Verified only when the
        payload carries a ``data['citations']`` key: an honest-EMPTY composition
        (no citations key) and the GLOBAL meta (never sets one) are no-ops. The
        composition path additionally passes ``finding_confidence`` so the T7
        hedge-laundering / anti-double-counting cap folds through the gate.

    The DETERMINISTIC citation floor ALWAYS runs; the optional LLM judge engages
    only when ``deps.verify_judge`` is wired (the host sets it iff the descriptor
    declares ``method.llm.verify`` AND ``LEGBA_VERIFY_LLM_JUDGE`` is on). The
    critique row is written ON THE SAME ``conn`` so the verdict lands in the same
    actor turn.

    Returns the verification dict (for the trace/return envelope) or ``None`` when
    nothing was verified. Best-effort: NEVER raises into the run path — a verify
    failure logs and the finding stays durable + un-demoted.
    """
    kind = getattr(deps.descriptor.identity, "kind", None)
    body = str(getattr(finding_payload, "body", "") or "")
    # M13/M15: the finding's title + run target feed the write/verify-time
    # world-knowledge + cross-target guards inside verify_finding_faithfulness.
    title = str(getattr(finding_payload, "title", "") or "")
    data = getattr(finding_payload, "data", None)
    citations = data.get("citations") if isinstance(data, Mapping) else None
    # S3-T1: the finding's structured I&W block, if any. A 'triggered' indicator
    # without a citation demotes faithfulness (the verify pass folds it into the
    # score); absent → None → no-op. Only unit inline_target findings carry it.
    indicators = data.get("indicators") if isinstance(data, Mapping) else None

    # SCOPE GUARD — the unit inline_target kind (always) OR a COMPOSITION
    # meta_findings_synthesizer finding that actually emitted a citation bridge.
    # The honest-EMPTY composition returns before its CITE block with NO citations
    # key, and the GLOBAL meta never sets one → both are no-ops here (the second
    # gate; the first is the dapr_actors fire condition on target_id).
    # M16 — the cross_analyst_correlator is graded through the SAME composition
    # (sub-claim) verify path: it cites other analyst_outputs via ``[[ref:N]]``
    # markers resolved into ``data['citations']`` (ref_kind='finding'), so its
    # confidence is clamped to faithfulness like every other LLM peer. Verify only
    # when a citation bridge is present (a blind_spot that is pure absence prose
    # emits none → no-op, honest-low by construction).
    # V1 (journal verify profile, the chronicle gate) — the journal_assessor
    # kind (BOTH tiers: entry + consolidation share identity.kind). The entry's
    # ``[[ref:<uuid>]]``-cited FACT claims are re-shaped into the standard
    # ``[N]`` + citations-bridge form and graded by the SAME floor + judge;
    # ``perspective`` claims never enter the document (§10 flag-never-strip —
    # the entry itself is NEVER mutated; the verdict is a side critique row).
    is_composition = kind in ("meta_findings_synthesizer", "cross_analyst_correlator")
    is_journal = kind == "journal_assessor"
    if kind == "inline_target":
        pass
    elif is_composition:
        if citations is None:
            return None
    elif is_journal:
        from ..data.analysts.journal_assessor import build_journal_verify_inputs

        body, ordered_refs = build_journal_verify_inputs(finding_payload)
        if not body:
            # An all-perspective (or all-uncited) entry has nothing judgeable —
            # a valid entry, not a failure. REFLECT already flagged uncited facts.
            return None
        citations = await _resolve_journal_citation_bridge(conn, ordered_refs)
    else:
        return None

    # COMPOSITION only: pass the finding's own confidence so the T7 hedge-
    # laundering check can compare an asserted clause confidence against its cited
    # sub-claim's ceiling. The unit path passes None → byte-identical.
    finding_confidence: float | None = None
    if is_composition:
        try:
            finding_confidence = float(getattr(finding_payload, "confidence"))
        except (TypeError, ValueError):
            finding_confidence = None

    from ..data.provenance._core import AnalystContext
    from ..data.provenance.verify import (
        build_faithfulness_critique_payload,
        verify_finding_faithfulness,
    )
    from ..data.provenance.writes import write_critique

    try:
        report = await verify_finding_faithfulness(
            body=body,
            citations=citations,
            judge_llm=deps.verify_judge,
            finding_confidence=finding_confidence,
            indicators=indicators,
            title=title,
            target_id=target_id,
            # E-1: the same-turn conn powers the facts-reconciled officeholder
            # guard (stale_leader_vs_facts — flag/demote only, never a
            # correction; degrade-not-drop inside the guard on a read failure).
            facts_conn=conn,
        )
    except Exception as exc:  # pragma: no cover — verify must never break a run
        logger.warning(
            "actor_critic.verify.failed finding_id=%s err=%s", finding_id, exc,
        )
        return None

    # JOURNAL judge-down honesty (j6 review #2, the "cheaper sibling" hole): a
    # journal's deterministic floor is RESOLVE-based, and a token-sprayed entry
    # cites real (resolvable) ids — so a floor-only score would certify
    # fabricated attribution whenever the judge soft-fails. For the journal
    # profile ONLY: if the LLM judge did not actually run, land NO critique row
    # at all — an UN-JUDGED entry (the chronicle gate treats critique-absent as
    # not-cleared, never as resolve-passed). Units/compositions keep the
    # labelled floor fallback (their bridges carry source_text the floor
    # meaningfully checks; the journal's failure mode is attribution itself).
    if is_journal and report.judge_status != "llm":
        logger.warning(
            "actor_critic.journal_verify.judge_unavailable entry_id=%s reason=%s "
            "— entry left UN-JUDGED (no critique row; floor would certify "
            "resolvable-but-unsupporting citations)",
            finding_id, report.judge_unavailable_reason,
        )
        return None

    # Identity of the analyzed analyst (the finding's producer) + the judge.
    analyzed_analyst_id = str(deps.descriptor.identity.id)
    analyzed_analyst_version = str(deps.descriptor.identity.version)
    analyzed_model = _extract_primary_model_ref(deps.descriptor)
    judge_model = str(getattr(deps.verify_judge, "subprovider", "") or "deterministic-floor")
    # P2-4: the RESOLVED judge stack-ref (the JudgeRoute the host wired behind
    # deps.verify_judge). Stamped into the critique row (``judge_llm_ref``) so
    # provenance records which model judged, forever. "" = floor-only (no judge
    # wired); judge_status in the report says whether the judge actually graded.
    judge_llm_ref = str(getattr(deps, "verify_judge_ref", "") or "")

    payload = build_faithfulness_critique_payload(
        report,
        analyzed_output_id=finding_id,
        analyzed_analyst_id=analyzed_analyst_id,
        analyzed_analyst_version=analyzed_analyst_version,
        analyzed_model=analyzed_model,
        judge_model=judge_model,
        judge_llm_ref=judge_llm_ref,
    )

    # The verify pass IS the critic here — stamp the analyst_ctx with this
    # analyst's identity (the verify is an in-run side-write, not a separate
    # critic actor). target_id NULL: a faithfulness critique is not target-scoped.
    ctx = AnalystContext(
        analyst_id=analyzed_analyst_id,
        analyst_version=analyzed_analyst_version,
        run_id=run_id,
        target_id=None,
        target_version=None,
    )
    try:
        # C-3 (adversarial review): a JOURNAL critique's subject lives in
        # journal_entries, which the integrity sweep's lineage catalogs do not
        # (and should not) cover — stamping the entry id into derived_from
        # would permanently pollute the dangling-lineage audit. The linkage
        # already lives in data.analyzed_output_id; findings keep the edge.
        row, dlq = await write_critique(
            conn,
            analyst_ctx=ctx,
            payload=payload,
            derived_from=([] if is_journal else [finding_id]),
        )
        if row is None:
            logger.warning(
                "actor_critic.verify.critique_dlq finding_id=%s — faithfulness "
                "critique failed validation (sent to DLQ)", finding_id,
            )
    except Exception as exc:  # pragma: no cover — best-effort persist
        logger.warning(
            "actor_critic.verify.persist_failed finding_id=%s err=%s",
            finding_id, exc,
        )

    # T-3: CONSEQUENCE for a contradicted claim. The verify report's support-judge
    # marks a claim contradicted-by-its-own-source with reason 'judge_contradicted'
    # (verify.py). When a JOURNAL entry carries any such verdict, append a durable,
    # renderable 'contradicted_claims' honesty flag to the entry row itself — so the
    # finding sticks on the entry, not only in the side critique. Degrade-not-fail
    # (the stamp helper swallows write errors); never touches the API/UI.
    if is_journal and any(
        getattr(s, "reason", None) == "judge_contradicted"
        for s in report.unsupported_spans
    ):
        await _stamp_journal_contradicted_flag(conn, finding_id)

    return report.as_dict()


async def verify_structural_claims_finding(
    conn: asyncpg.Connection,
    *,
    deps: "_AnalystDeps",
    finding_id: UUID,
    finding_payload: Any,
    run_id: Any,
    derived_from: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Run the C2b ``structural_claims`` verify over a just-emitted STRUCTURAL
    finding and PERSIST the verdict as a ``critique`` (the standard contract).

    The claim-bearing structural analysts (``STRUCTURAL_CLAIMS_VERIFY_ANALYSTS``)
    emit findings OUTSIDE the faithfulness pass but assert CHECKABLE quantities
    (a converged-cell distinct-count, an echo count, a rollup identity). This
    DETERMINISTICALLY re-derives each declared claim from the constituent set the
    finding recorded and writes a ``kind='critique'`` row carrying a
    ``structural_verified`` marker + per-claim ledger. A finding with no
    ``data['structural_claims']`` block is a NO-OP (writes nothing; the row keeps
    its honest ``unverified — structural`` badge). NEVER raises into the run path.

    OFF-safe: the critique's ``overall_score`` is pinned to 1.0 unless
    ``LEGBA_STRUCTURAL_VERIFY_GATE`` is on (compute-and-show, do-not-gate — the
    verdict is shown via the badge + verification detail without demoting
    effective_confidence). Returns the verification dict (for the trace) or None.
    """
    data = getattr(finding_payload, "data", None)
    if not isinstance(data, Mapping):
        return None

    from ..data.provenance._core import AnalystContext
    from ..data.provenance.verify import (
        build_structural_critique_payload,
        verify_structural_claims,
    )
    from ..data.provenance.writes import write_critique

    try:
        report = verify_structural_claims(data=data, derived_from=derived_from)
    except Exception as exc:  # pragma: no cover — verify must never break a run
        logger.warning(
            "actor_critic.structural_verify.failed finding_id=%s err=%s",
            finding_id, exc,
        )
        return None

    # No declared claims → nothing to verify → no critique (honest no-op).
    if not report.had_claims:
        return None

    analyzed_analyst_id = str(deps.descriptor.identity.id)
    analyzed_analyst_version = str(deps.descriptor.identity.version)
    payload = build_structural_critique_payload(
        report,
        analyzed_output_id=finding_id,
        analyzed_analyst_id=analyzed_analyst_id,
        analyzed_analyst_version=analyzed_analyst_version,
    )
    # The verify pass IS the critic; stamp this analyst's identity. A structural
    # critique is not target-scoped (target_id NULL), mirroring the faithfulness
    # critique.
    ctx = AnalystContext(
        analyst_id=analyzed_analyst_id,
        analyst_version=analyzed_analyst_version,
        run_id=run_id,
        target_id=None,
        target_version=None,
    )
    try:
        row, dlq = await write_critique(
            conn,
            analyst_ctx=ctx,
            payload=payload,
            derived_from=[finding_id],
        )
        if row is None:
            logger.warning(
                "actor_critic.structural_verify.critique_dlq finding_id=%s — "
                "structural critique failed validation (sent to DLQ)", finding_id,
            )
    except Exception as exc:  # pragma: no cover — best-effort persist
        logger.warning(
            "actor_critic.structural_verify.persist_failed finding_id=%s err=%s",
            finding_id, exc,
        )

    if report.miscount:
        logger.warning(
            "actor_critic.structural_verify.miscount finding_id=%s analyst=%s "
            "miscount=%d checkable=%d — a structural finding misstates its own "
            "evidence", finding_id, analyzed_analyst_id, report.miscount,
            report.checkable,
        )

    return {
        "structural_verify": True,
        "structural_verified": report.structural_verified,
        "checkable_claims": report.checkable,
        "supported_claims": report.supported,
        "miscount_claims": report.miscount,
        "unverifiable_claims": report.unverifiable,
    }
