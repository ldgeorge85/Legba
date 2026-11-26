# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE consult write EXECUTOR — one, in the registry process.

Every consult write lands here: the model's call (posted by the runtime's thin
handler through ``POST /consult/writes``), the operator's Confirm click, the
typed confirm code at the chat front door, Cancel, and the receipt's Revert
button. The last four execute STORED canonical args with no model in the loop.

What it owns:

* **The kill switches, re-read on every write**: env
  ``LEGBA_CONSULT_WRITES`` (default OFF), the ``consult_write`` pack's own
  lifecycle state (binding ignores it — W3-F6 — so it is read HERE), and the
  per-class ``classes_enabled`` list in the called tool's pack config.
* **The operator's turn**: ``operator_words`` is read from
  ``consult_turns`` by the turn id the REGISTRY recorded when it armed the run —
  never passed by the model or the runtime. No durable turn → ``no_operator_turn``.
* **Per-turn caps**: executed ≤ the pack's ``max_invocations_per_run``
  (10), staged ≤ 5, counted from the ledger itself.
* **Tier A / Tier B**: Tier A executes inside the call; Tier B is staged
  ``pending_confirmation`` with a server-minted 4-character code (unambiguous
  alphabet), hashed at rest and kept in clear only in this process's memory
  for the 15-minute window — the model never sees it. A new staged write on
  the same object cancels the older one; Stop on the run expires the turn's.
* **One transaction per executed write**: the registry mutation, the
  ``consult_writes`` row moving to ``executed`` and one signed audit row
  (actor ``consult_write:<id>``, role ``operator_via_consult``, the receipt in
  ``change_summary``) commit together through the registry's ``in_tx`` hook,
  which also re-checks compare-and-set against ``expected_version``.
* **Reversal**: ``revert`` executes the stored handle, refused
  ``superseded_since`` when the post-image is no longer the head.
* **The activation probe**: after a new head, watch for parse
  failure or the first poll under it, deadline 3 minutes; a proven
  ``parse_failed`` rolls back to ``pre.version`` and stamps the receipt.

A leaf of the registry package: it imports the descriptor registry, the class
branches and the vocabulary leaves — never a runtime or analyst module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from ..principals import handle_for_actor
from .. import consult_write_patch as P
from .. import consult_write_vocabulary as W
from . import consult_write_composition as _composition
from . import consult_write_entity as _entity
from . import consult_write_fact as _fact
from . import consult_write_source as _source
from . import consult_write_subject as _subject
from . import consult_write_unit as _unit
from .descriptor import Family
from .errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    VersionConflict,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Armed", "CLASS_BRANCHES", "ConsultWriteExecutor", "WRITES_ENV", "executor_for", "writes_enabled",
]

WRITES_ENV = "LEGBA_CONSULT_WRITES"

#: class → branch module. The drift guard holds the keys equal to the
#: vocabulary's built classes and each branch's ``VERBS`` to its built verbs.
CLASS_BRANCHES: dict[str, Any] = {
    "source": _source,
    "subject": _subject,
    "unit": _unit,
    "composition": _composition,
    "entity": _entity,
    "fact": _fact,  # W11 C-2b-2/facts — a record class (no registry family)
}

Refused = _source.Refused


def writes_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(WRITES_ENV, "")
    return raw.strip().lower() in {"1", "true", "on", "yes"}


@dataclass(frozen=True)
class Armed:
    """What the REGISTRY recorded when it started an armed run."""

    request_id: str
    session_id: str | None
    user_turn_id: str | None
    plane: str | None
    model_component: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if ts else None


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _jsonb(value: Any) -> str | None:
    return None if value is None else _canon(value)


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _row(record: Any) -> dict[str, Any]:
    out = dict(record)
    for key in ("args", "pre_image", "post_image", "reversal", "activation", "error", "receipt"):
        if key in out:
            out[key] = _loads(out[key])
    for key in ("id", "session_id", "user_turn_id", "reverses_write_id", "reversed_by_write_id", "audit_id",
                "written_by"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


class _StaleInTx(Exception):
    pass


class _Hook:
    """The registry's ``in_tx``: the ledger row + the receipt, in the mutation's transaction."""

    def __init__(self, ex: "ConsultWriteExecutor", row: Mapping[str, Any], plan: Any, via: str | None) -> None:
        self.ex, self.row, self.plan, self.via = ex, row, plan, via
        self.fired = False
        self.receipt: dict[str, Any] = {}

    def receipt_for(self, *, from_version: str | None, to_version: str | None) -> dict[str, Any]:
        post_version = to_version or from_version
        self.receipt = self.ex.compose_receipt(
            self.row, self.plan, via=self.via,
            post={"version": post_version, "state": self.plan.post_state},
        )
        return self.receipt

    async def __call__(self, conn: Any, *, audit_id: UUID, from_version: str | None, to_version: str | None) -> None:
        if self.plan.expected_version is not None and from_version != self.plan.expected_version:
            raise _StaleInTx(f"head moved to {from_version} inside the transaction")
        updated = await conn.fetchval(
            "UPDATE consult_writes SET status = 'executed', post_image = $2::jsonb, "
            "audit_id = $3, receipt = $4::jsonb, executed_at = now(), confirmed_via = $5 "
            "WHERE id = $1 AND status IN ('staged', 'pending_confirmation') RETURNING id",
            UUID(self.row["id"]), _canon(self.receipt["post"]), audit_id,
            _canon(self.receipt), self.via,
        )
        if updated is None:
            raise _StaleInTx("the ledger row is no longer open (cancelled or expired)")
        reverses = self.row.get("reverses_write_id")
        if reverses:
            marked = await conn.fetchval(
                "UPDATE consult_writes SET reversed_by_write_id = $2 "
                "WHERE id = $1 AND reversed_by_write_id IS NULL RETURNING id",
                UUID(str(reverses)), UUID(self.row["id"]),
            )
            if marked is None:
                raise _StaleInTx(f"write {reverses} was already reverted")
        self.fired = True


class ConsultWriteExecutor:
    """One per registry process (the consult router builds it)."""

    def __init__(
        self,
        registry: Any,
        *,
        env: Mapping[str, str] | None = None,
        parse_check: Callable[[Mapping[str, Any]], str | None] | None = None,
        probe: bool = True,
        probe_poll_seconds: float | None = None,
        probe_deadline_seconds: float | None = None,
    ) -> None:
        self.registry = registry
        self._env = env
        #: None = each class's own runtime parser (``consult_write_patch.preflight``).
        self.parse_check = parse_check
        self.probe_enabled = probe
        self.probe_poll = float(probe_poll_seconds or W.LIMITS["activation_poll_seconds"])
        self.probe_deadline = float(probe_deadline_seconds or W.LIMITS["activation_deadline_seconds"])
        self._codes: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def pg(self) -> Any:
        return self.registry.pg

    # ------------------------------------------------------------------
    # kill switches + context
    # ------------------------------------------------------------------

    async def _pack(self) -> Any | None:
        try:
            return await self.registry.get(W.CONSULT_WRITE_PACK_ID, family=Family.ACTION_PACK)
        except DescriptorNotFound:
            return None

    async def switches(self, cls: str, tool: str) -> tuple[str, str] | None:
        """The three kill switches, read NOW. None = writes may proceed."""
        if not writes_enabled(self._env):
            return "writes_disabled", f"consult writes are switched off ({WRITES_ENV} is not on)."
        pack = await self._pack()
        if pack is None:
            return "writes_disabled", "the consult_write pack is not registered."
        if pack.state != "active":
            return "writes_disabled", f"the consult_write pack is {pack.state}; writes are stopped."
        for spec in (pack.body or {}).get("tools") or ():
            if isinstance(spec, Mapping) and spec.get("name") == tool:
                enabled = (spec.get("config") or {}).get("classes_enabled") or []
                if cls not in enabled:
                    return "class_disabled", f"{cls} writes are switched off in the consult_write pack."
                return None
        return "writes_disabled", f"the consult_write pack does not carry {tool}."

    async def _per_run_cap(self) -> int:
        pack = await self._pack()
        governor = ((pack.body if pack else None) or {}).get("governor") or {}
        cap = governor.get("max_invocations_per_run")
        return int(cap) if cap is not None else W.LIMITS["executed_per_turn"]

    async def turn_context(self, armed: Armed) -> dict[str, Any] | None:
        """The operator's own words and the turn index, from ``consult_turns``."""
        if not armed.user_turn_id or not armed.session_id:
            return None
        async with self.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT t.content, (SELECT count(*) FROM consult_turns p "
                "  WHERE p.session_id = t.session_id AND p.created_at < t.created_at) AS idx "
                "FROM consult_turns t WHERE t.id = $1::uuid AND t.session_id = $2::uuid "
                "AND t.role = 'user'",
                armed.user_turn_id, armed.session_id,
            )
        if row is None:
            return None
        words = str(row["content"] or "")
        cap = W.LIMITS["operator_words_max_chars"]
        if len(words) > cap:
            words = words[:cap] + " …[truncated]"
        return {"operator_words": words, "turn_index": int(row["idx"])}

    # ------------------------------------------------------------------
    # ledger rows
    # ------------------------------------------------------------------

    async def _insert(self, **cols: Any) -> dict[str, Any]:
        cols.setdefault("id", str(uuid4()))
        args = cols.get("args") or {}
        cols["args_sha256"] = hashlib.sha256(_canon(args).encode("utf-8")).hexdigest()
        names = list(cols)
        values = [
            _jsonb(cols[n]) if n in {"args", "pre_image", "post_image", "reversal", "activation", "error", "receipt"}
            else cols[n]
            for n in names
        ]
        casts = {
            "id": "::uuid", "session_id": "::uuid", "user_turn_id": "::uuid",
            "reverses_write_id": "::uuid", "args": "::jsonb", "pre_image": "::jsonb",
            "post_image": "::jsonb", "reversal": "::jsonb", "error": "::jsonb",
            "activation": "::jsonb", "receipt": "::jsonb",
        }
        placeholders = ", ".join(f"${i + 1}{casts.get(n, '')}" for i, n in enumerate(names))
        async with self.pg.acquire() as conn:
            # The write names the person whose Consult session armed it
            # (consult_sessions.principal is the server-stamped actor).
            actor = None
            if cols.get("session_id"):
                actor = await conn.fetchval(
                    "SELECT principal FROM consult_sessions WHERE id = $1::uuid",
                    str(cols["session_id"]),
                )
            values.append(handle_for_actor(actor))
            names.append("written_by")
            placeholders += f", legba_principal(${len(values)})"
            row = await conn.fetchrow(
                f"INSERT INTO consult_writes ({', '.join(names)}) VALUES ({placeholders}) RETURNING *",
                *values,
            )
        return _row(row)

    async def get(self, write_id: str) -> dict[str, Any] | None:
        try:
            wid = UUID(str(write_id))
        except ValueError:
            return None
        async with self.pg.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM consult_writes WHERE id = $1", wid)
        return _row(row) if row is not None else None

    async def _set_status(self, write_id: str, status: str, *, cause: str | None = None, detail: str | None = None) -> None:
        async with self.pg.acquire() as conn:
            await conn.execute(
                "UPDATE consult_writes SET status = $2, error = COALESCE($3::jsonb, error) "
                "WHERE id = $1 AND status IN ('staged', 'pending_confirmation')",
                UUID(write_id), status,
                _jsonb({"cause": cause, "detail": detail}) if cause else None,
            )
        self._codes.pop(write_id, None)

    async def expire_due(self, *, session_id: str | None = None) -> int:
        """Pending rows past their expiry → ``expired``. Returns the count."""
        sql = (
            "UPDATE consult_writes SET status = 'expired', "
            "error = jsonb_build_object('cause', 'expired', 'detail', 'the confirmation window closed') "
            "WHERE status = 'pending_confirmation' AND expires_at < now()"
        )
        params: list[Any] = []
        if session_id:
            sql += " AND session_id = $1::uuid"
            params.append(session_id)
        sql += " RETURNING id"
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        for r in rows:
            self._codes.pop(str(r["id"]), None)
        return len(rows)

    async def expire_for_request(self, request_id: str, *, session_id: str | None) -> int:
        """Stop on a run expires the turn's staged writes. Stop is not an undo."""
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE consult_writes SET status = 'expired', "
                "error = jsonb_build_object('cause', 'run_stopped', 'detail', 'the run was stopped') "
                "WHERE session_id = $2::uuid AND request_id = $1 "
                "AND status = 'pending_confirmation' RETURNING id",
                request_id, session_id,
            )
        for r in rows:
            self._codes.pop(str(r["id"]), None)
        return len(rows)

    def compose_receipt(
        self, row: Mapping[str, Any], plan: Any, *, via: str | None, post: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "receipt": W.RECEIPT_VERSION,
            "write_id": row["id"],
            "session_id": row.get("session_id"),
            "user_turn_id": row.get("user_turn_id"),
            "turn_index": row.get("turn_index"),
            "request_id": row.get("request_id"),
            "operator_words": row.get("operator_words"),
            "plane": row.get("plane"),
            "model_component": row.get("model_component"),
            "class": row["class"],
            "verb": row["verb"],
            "object": row["object_ref"],
            "args_sha256": row.get("args_sha256"),
            "tier": row["tier"],
            "confirmed_via": via,
            "pre": plan.pre,
            "post": dict(post),
            "reversal": plan.reversal,
            "reverses_write_id": row.get("reverses_write_id"),
            "turn_cost_usd_at_write": (row.get("receipt") or {}).get("turn_cost_usd_at_write")
            if isinstance(row.get("receipt"), Mapping) else None,
            "invocation_id": row.get("invocation_id"),
            "activation": None,
        }

    # ------------------------------------------------------------------
    # the model's call
    # ------------------------------------------------------------------

    async def _invocation_id(self, requested_by: str | None, tool: str) -> str | None:
        if not requested_by:
            return None
        async with self.pg.acquire() as conn:
            inv = await conn.fetchval(
                "SELECT id FROM action_pack_invocations WHERE requested_by = $1 AND tool_name = $2 "
                "ORDER BY occurred_at DESC LIMIT 1",
                requested_by, tool,
            )
        return str(inv) if inv is not None else None

    async def submit(
        self,
        armed: Armed,
        *,
        tool: str,
        args: Mapping[str, Any],
        requested_by: str | None = None,
        turn_cost_usd: float | None = None,
        runtime_preflight: str | None = None,
    ) -> dict[str, Any]:
        """One model write call. Returns ``{status, write_id?, cause?, detail?, …}``; never raises
        for a refusal (WriteKindNotBuilt aside — the route maps that to 501)."""
        args = {k: v for k, v in dict(args).items() if k != "scope_predicate"}
        if tool not in W.WRITE_TOOL_NAMES:
            return {"status": "refused", "cause": "class_not_writable", "detail": f"{tool} is not a write tool."}
        if tool == "inspect_object":
            cls = str(args.get("class"))
            if cls not in W.CLASSES:
                return {"status": "refused", "cause": "class_not_writable", "detail": f"{cls!r} is not a class."}
            W.require_built(cls, "inspect")
            return await CLASS_BRANCHES[cls].inspect(self.registry, str(args.get("id")), session_id=armed.session_id)
        original: dict[str, Any] | None = None
        if tool == "revert_write":
            original = await self.get(str(args.get("write_id")))
            if original is None or original.get("session_id") != armed.session_id:
                return {"status": "refused", "cause": "ref_not_read",
                        "detail": f"write {args.get('write_id')} is not in this session's ledger."}
            cls = str(original["class"])
        else:
            cls = W.TOOL_CLASS.get(tool) or str(args.get("class"))
        verb = W.verb_for(tool, args)
        if cls not in W.CLASSES:
            return {"status": "refused", "cause": "class_not_writable", "detail": f"{cls!r} is not writable."}
        if (cls, verb) not in W.VOCABULARY:
            return {"status": "refused", "cause": "class_not_writable",
                    "detail": f"{cls} has no {verb} verb in the write plane."}
        W.require_built(cls, verb)
        object_ref = str(original["object_ref"] if original else (
            args.get("id") or args.get("entity_id") or args.get("loser_id")
            or args.get("fact_id") or W.fact_ref_of(args)
            or (args.get("body") or {}).get("identity", {}).get("id") or "?"))
        ctx = await self.turn_context(armed)
        base = {
            "session_id": armed.session_id, "user_turn_id": armed.user_turn_id,
            "request_id": armed.request_id, "plane": armed.plane,
            "model_component": armed.model_component, "class": cls, "verb": verb,
            "object_ref": object_ref, "args": args,
            "invocation_id": await self._invocation_id(requested_by, tool),
            "receipt": {"turn_cost_usd_at_write": turn_cost_usd},
            "reverses_write_id": original["id"] if original else None,
        }
        if ctx is None:
            return await self._refuse_row(base, "no_operator_turn", "this run has no durable operator turn.")
        base.update(ctx)
        switched = await self.switches(cls, tool)
        if switched is not None:
            return await self._refuse_row(base, *switched)
        if runtime_preflight:
            return await self._refuse_row(base, "preflight_failed", runtime_preflight)
        try:
            if original is not None:
                plan = await CLASS_BRANCHES[cls].plan_revert(
                    self.registry, original, await self._newer_write(original),
                )
            else:
                plan = await CLASS_BRANCHES[cls].plan(self.registry, verb, args)
        except Refused as exc:
            return await self._refuse_row(base, exc.cause, exc.detail)
        base["args"], base["object_ref"] = plan.args, plan.object_ref
        counts = await self._counts(armed)
        if plan.tier == "B":
            if counts["pending_confirmation"] >= W.LIMITS["staged_per_turn"]:
                return await self._refuse_row(
                    base, "write_cap_reached",
                    f"this turn already staged {W.LIMITS['staged_per_turn']} changes, its cap.",
                )
            return await self._stage(base, plan)
        cap = await self._per_run_cap()
        if counts["executed"] >= cap:
            return await self._refuse_row(base, "write_cap_reached", f"this turn already made {cap} changes, its cap.")
        row = await self._insert(**base, tier="A", status="staged", pre_image=plan.pre, reversal=plan.reversal)
        return await self._execute(row, plan, via=None)

    async def _counts(self, armed: Armed) -> dict[str, int]:
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, count(*) AS n FROM consult_writes "
                "WHERE session_id = $1::uuid AND request_id = $2 GROUP BY status",
                armed.session_id, armed.request_id,
            )
        out = {s: 0 for s in W.STATUSES}
        out.update({r["status"]: int(r["n"]) for r in rows})
        return out

    async def _newer_write(self, original: Mapping[str, Any]) -> str | None:
        async with self.pg.acquire() as conn:
            newer = await conn.fetchval(
                "SELECT id FROM consult_writes WHERE class = $1 AND object_ref = $2 "
                "AND status = 'executed' AND created_at > $3 AND id <> $4 "
                "ORDER BY created_at DESC LIMIT 1",
                original["class"], original["object_ref"], original["created_at"],
                UUID(str(original["id"])),
            )
        return str(newer) if newer is not None else None

    async def _refuse_row(self, base: Mapping[str, Any], cause: str, detail: str) -> dict[str, Any]:
        assert cause in W.WRITE_BLOCK_CAUSES, cause
        row = await self._insert(**{**base, "tier": "A", "status": "refused", "error": {"cause": cause, "detail": detail}})
        return {"status": "refused", "write_id": row["id"], "cause": cause, "detail": detail}

    async def _stage(self, base: dict[str, Any], plan: Any) -> dict[str, Any]:
        write_id = str(uuid4())
        code = W.mint_code()
        expires = _now() + timedelta(seconds=W.LIMITS["confirm_ttl_seconds"])
        async with self.pg.acquire() as conn:
            superseded = await conn.fetch(
                "UPDATE consult_writes SET status = 'cancelled', error = jsonb_build_object("
                "'cause', 'superseded', 'detail', 'a newer staged change names the same object') "
                "WHERE class = $1 AND object_ref = $2 AND status = 'pending_confirmation' RETURNING id",
                base["class"], base["object_ref"],
            )
        for r in superseded:
            self._codes.pop(str(r["id"]), None)
        row = await self._insert(
            **base, id=write_id, tier="B", status="pending_confirmation",
            confirm_code_hash=W.code_hash(write_id, code), expires_at=expires,
            pre_image=plan.pre, reversal=plan.reversal,
        )
        self._codes[write_id] = code
        return {
            "status": "pending_confirmation", "write_id": row["id"], "tier": "B",
            "expires_at": _iso(expires), "card": plan.card,
        }

    # ------------------------------------------------------------------
    # execution (Tier A now; Tier B at confirm; reverts)
    # ------------------------------------------------------------------

    async def _execute(self, row: dict[str, Any], plan: Any, *, via: str | None) -> dict[str, Any]:
        hook = _Hook(self, row, plan, via)
        actor = f"consult_write:{row['id']}"
        try:
            await plan.mutate(actor=actor, in_tx=hook)
        except _StaleInTx as exc:
            return await self._close(row, "refused", "stale_read", str(exc))
        except VersionConflict as exc:
            return await self._close(row, "refused", "stale_read", str(exc))
        except (IllegalLifecycleTransition, DescriptorValidationError) as exc:
            return await self._close(row, "refused", "preflight_failed", str(exc)[:600])
        except Exception as exc:  # noqa: BLE001 — a failed write is recorded, never raised
            logger.exception("consult_write.execute_failed write_id=%s", row["id"])
            return await self._close(row, "failed", None, f"{type(exc).__name__}: {exc}"[:600])
        if not hook.fired:
            return await self._close(row, "refused", "no_change", "the change re-states the head; nothing was written.")
        self._codes.pop(row["id"], None)
        if self.probe_enabled and plan.verb in {"create", "update", "transition", "revert"} and plan.post_state != "retired":
            self._spawn_probe(row["id"])
        return {"status": "executed", "write_id": row["id"], "receipt": hook.receipt, "card": plan.card}

    async def _close(self, row: Mapping[str, Any], status: str, cause: str | None, detail: str) -> dict[str, Any]:
        async with self.pg.acquire() as conn:
            await conn.execute(
                "UPDATE consult_writes SET status = $2, error = $3::jsonb "
                "WHERE id = $1 AND status IN ('staged', 'pending_confirmation')",
                UUID(row["id"]), status, _canon({"cause": cause, "detail": detail}),
            )
        self._codes.pop(row["id"], None)
        out = {"status": status, "write_id": row["id"], "detail": detail}
        if cause:
            out["cause"] = cause
        return out

    async def _replan(self, row: Mapping[str, Any]) -> Any:
        """Rebuild a staged row's plan from its STORED args (the model is not asked again)."""
        cls = row["class"]
        if row["verb"] == "revert":
            original = await self.get(str(row["reverses_write_id"]))
            if original is None:
                raise Refused("ref_not_read", "the write to revert is gone.")
            return await CLASS_BRANCHES[cls].plan_revert(self.registry, original, await self._newer_write(original))
        return await CLASS_BRANCHES[cls].plan(self.registry, row["verb"], row["args"])

    async def confirm(
        self, write_id: str, *, via: str, code: str | None = None, session_id: str | None = None,
    ) -> dict[str, Any]:
        """The operator's second word: a click or the typed code. Invokes no model."""
        await self.expire_due()
        row = await self.get(write_id)
        if row is None:
            return {"status": "not_found", "write_id": write_id}
        if session_id and row.get("session_id") != session_id:
            return {"status": "not_found", "write_id": write_id}
        if row["status"] != "pending_confirmation":
            return {"status": row["status"], "write_id": write_id, "detail": f"the change is {row['status']}."}
        if via == "typed_code" and not W.code_matches(write_id, code or "", row.get("confirm_code_hash")):
            return {"status": "pending_confirmation", "write_id": write_id, "detail": "the code does not match."}
        switched = await self.switches(row["class"], _tool_for(row["verb"]))
        if switched is not None:
            return await self._close(row, "refused", *switched)
        try:
            plan = await self._replan(row)
        except Refused as exc:
            return await self._close(row, "refused", exc.cause, exc.detail)
        return await self._execute(row, plan, via=via)

    async def cancel(self, write_id: str, *, session_id: str | None = None) -> dict[str, Any]:
        row = await self.get(write_id)
        if row is None or (session_id and row.get("session_id") != session_id):
            return {"status": "not_found", "write_id": write_id}
        if row["status"] != "pending_confirmation":
            return {"status": row["status"], "write_id": write_id}
        await self._set_status(write_id, "cancelled", cause="cancelled", detail="the operator cancelled it")
        return {"status": "cancelled", "write_id": write_id}

    async def find_by_code(self, session_id: str, code: str) -> dict[str, Any] | None:
        """The pending write in ``session_id`` whose code is ``code`` (the typed intercept)."""
        if not is_uuid(session_id):
            return None
        await self.expire_due(session_id=session_id)
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM consult_writes WHERE session_id = $1::uuid "
                "AND status = 'pending_confirmation' AND expires_at > now() ORDER BY created_at",
                session_id,
            )
        for r in rows:
            row = _row(r)
            if W.code_matches(row["id"], code, row.get("confirm_code_hash")):
                return row
        return None

    async def revert_button(self, write_id: str, *, session_id: str | None = None) -> dict[str, Any]:
        """The receipt's Revert button: the operator's own hand, executed now (no model)."""
        original = await self.get(write_id)
        if original is None or (session_id and original.get("session_id") != session_id):
            return {"status": "not_found", "write_id": write_id}
        if original["status"] != "executed":
            return {"status": "refused", "cause": "ref_not_read", "detail": f"write {write_id} is {original['status']}."}
        if original.get("reversed_by_write_id"):
            return {"status": "refused", "cause": "superseded_since",
                    "detail": f"already reverted by write {original['reversed_by_write_id']}."}
        cls = original["class"]
        switched = await self.switches(cls, "revert_write")
        base = {
            "session_id": original.get("session_id"), "user_turn_id": None, "request_id": None,
            "turn_index": None, "operator_words": f"Revert button on write {write_id}",
            "plane": None, "model_component": None, "class": cls, "verb": "revert",
            "object_ref": original["object_ref"], "args": {"write_id": write_id},
            "reverses_write_id": write_id,
        }
        if switched is not None:
            return await self._refuse_row(base, *switched)
        try:
            plan = await CLASS_BRANCHES[cls].plan_revert(self.registry, original, await self._newer_write(original))
        except Refused as exc:
            return await self._refuse_row(base, exc.cause, exc.detail)
        row = await self._insert(**{**base, "args": plan.args}, tier="A", status="staged",
                                 pre_image=plan.pre, reversal=plan.reversal)
        return await self._execute(row, plan, via="revert_button")

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    async def list(
        self, *, session_id: str | None = None, request_id: str | None = None,
        cls: str | None = None, object_ref: str | None = None, since: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if (session_id is not None and not is_uuid(session_id)):
            return []
        await self.expire_due(session_id=session_id)
        clauses, params = [], []
        for col, value, cast in (
            ("session_id", session_id, "::uuid"), ("request_id", request_id, ""),
            ("class", cls, ""), ("object_ref", object_ref, ""),
        ):
            if value is not None:
                params.append(value)
                clauses.append(f"{col} = ${len(params)}{cast}")
        if since is not None:
            params.append(since)
            clauses.append(f"created_at >= ${len(params)}")
        params.append(max(1, min(500, int(limit))))
        sql = (
            "SELECT * FROM consult_writes"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + f" ORDER BY created_at LIMIT ${len(params)}"
        )
        async with self.pg.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        out = []
        for r in rows:
            row = _row(r)
            row.pop("confirm_code_hash", None)
            if row["status"] == "pending_confirmation" and row["id"] in self._codes:
                row["confirm_code"] = self._codes[row["id"]]
            for key in ("created_at", "updated_at", "executed_at", "expires_at"):
                if isinstance(row.get(key), datetime):
                    row[key] = _iso(row[key])
            out.append(row)
        return out

    # ------------------------------------------------------------------
    # the activation probe (D10)
    # ------------------------------------------------------------------

    def _spawn_probe(self, write_id: str) -> None:
        task = asyncio.ensure_future(self._probe_loop(write_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _probe_loop(self, write_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.probe_deadline
        try:
            while True:
                verdict = await self.probe_once(write_id)
                if verdict is not None:
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    await self._stamp(write_id, {
                        "status": "not_observed", "deadline_s": int(self.probe_deadline),
                        "at": _iso(_now()),
                    })
                    return
                await asyncio.sleep(self.probe_poll)
        except Exception:  # noqa: BLE001 — a probe never takes the registry down
            logger.exception("consult_write.probe_failed write_id=%s", write_id)

    async def probe_once(self, write_id: str) -> dict[str, Any] | None:
        """One look; stamps and returns a verdict, or None to keep watching.
        A proven ``parse_failed`` rolls the head back to ``pre.version`` (D10)."""
        row = await self.get(write_id)
        if row is None or row["status"] != "executed":
            return {"status": "not_applicable"}
        post = row.get("post_image") or {}
        branch = CLASS_BRANCHES[row["class"]]
        verdict = await branch.activation(
            self.registry, row["object_ref"], str(post.get("version")),
            row.get("executed_at") or _now(),
            parse_check=self.parse_check or (lambda body: P.preflight(row["class"], body)),
        )
        if verdict is None:
            return None
        if verdict["status"] == "parse_failed":
            verdict = {**verdict, **await self._roll_back(row)}
        await self._stamp(write_id, verdict)
        return verdict

    async def _roll_back(self, row: Mapping[str, Any]) -> dict[str, Any]:
        pre = row.get("pre_image") or {}
        actor = f"consult_write:{row['id']}"
        family = CLASS_BRANCHES[row["class"]].FAMILY  # every branch exports FAMILY
        try:
            if pre.get("version"):
                await self.registry.rollback(
                    row["object_ref"], str(pre["version"]), actor, family=family,
                    actor_role="operator_via_consult",
                    reason=f"activation parse_failed after consult write {row['id']}",
                )
                return {"rolled_back_to": pre["version"]}
            await self.registry.retire(
                row["object_ref"], actor, family=family, actor_role="operator_via_consult",
                reason=f"activation parse_failed after consult write {row['id']}",
            )
            return {"rolled_back_to": None, "retired": True}
        except Exception as exc:  # noqa: BLE001 — the stamp says it could not
            logger.exception("consult_write.rollback_failed write_id=%s", row["id"])
            return {"rollback_error": f"{type(exc).__name__}: {exc}"[:400]}

    async def _stamp(self, write_id: str, verdict: Mapping[str, Any]) -> None:
        async with self.pg.acquire() as conn:
            await conn.execute(
                "UPDATE consult_writes SET activation = $2::jsonb, "
                "receipt = jsonb_set(COALESCE(receipt, '{}'::jsonb), '{activation}', $2::jsonb) "
                "WHERE id = $1",
                UUID(write_id), _canon(dict(verdict)),
            )

    async def drain(self) -> None:
        """Wait for in-flight probes (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


_EXECUTORS: "weakref.WeakKeyDictionary[Any, ConsultWriteExecutor]" = weakref.WeakKeyDictionary()


def executor_for(registry: Any) -> ConsultWriteExecutor:
    """THE executor of ``registry`` — one per registry, so the confirm codes it
    holds in memory are the ones every route of the process reads."""
    ex = _EXECUTORS.get(registry)
    if ex is None:
        ex = ConsultWriteExecutor(registry)
        _EXECUTORS[registry] = ex
    return ex


def _tool_for(verb: str) -> str:
    """The tool a stored verb came from (its pack config holds the class switch)."""
    if verb == "retire":
        return "transition_object"
    return {v: t for t, v in W.TOOL_VERB.items()}.get(verb, verb)
