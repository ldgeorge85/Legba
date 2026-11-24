# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""JUDGE-2 (W11b) — the judge FAILURE CHAIN, beside the judge resolution ladder.

WHAT WAS THERE. ``analyst_deps_builder.resolve_judge_route_from_llm_block`` is a
RESOLUTION ladder — env override → ``method.llm.judge`` → ``method.llm.verify``
→ ``method.llm.primary`` — that picks ONE component. When that component's
account ran dry (OpenRouter 402, 2026-10-02 23:08Z → 10-05 02:03Z, ~51 h) every
judge call hard-failed, the transport shim rightly refused to retry a 402, and
every critique for two days was the deterministic floor: 641 on 10-03, 628 on
10-04. A $0 in-house rung was registered and healthy the whole time.

WHAT THIS ADDS. A FAILURE chain for the judge call:

  * ``method.llm.judge_chain: [ref, ...]`` declares the rungs per descriptor.
    Rung 1 is ALWAYS the ladder's resolved route (so the env override and
    ``method.llm.judge`` keep deciding it, byte-identically); the declared refs
    follow it, the resolved ref de-duplicated out. ``judge_chain: []`` is a
    one-rung chain — today's behaviour exactly. No key ⇒ the platform default
    tail (:data:`DEFAULT_JUDGE_CHAIN_TAIL` — the $0 in-house rung only; a paid
    second rung is opt-in), overridable deployment-wide by
    ``LEGBA_JUDGE_CHAIN_DEFAULT``.
  * On a call whose failure class is ROUTE-GONE (``auth`` 401/403, ``credit``
    402 or an insufficient-credits body, ``retired`` 404 — see
    ``data/provenance/judge_failure_class.py``) the session moves to the next
    rung FOR THAT VERIFY PASS and re-sends the same call there. A 429, a 5xx and
    a timeout do NOT move: they re-raise on the current rung and the transport
    shim's retry (``judge_transport.call_judge_with_retry``) behaves exactly as
    before.
  * The floor is reached only when the LAST rung failed; the critic then stamps
    ``unverified_reason`` (the last rung's class, http status and time) into the
    critique's ``data.verification`` (JSONB — no column, no migration).
  * Rungs after the first are built LAZILY through the same cached handler
    factory, on the first route-gone failure — a healthy judge costs no extra
    handler build and the wiring resolves ONE ref, as before.

WHY ``method.llm`` AND NOT ``method.options``. ``method.llm`` is the open
``dict[str, Any]`` every LLM route of a descriptor lives in (``judge``,
``verify``, ``primary``); it parses for EVERY kind. ``method.options`` is the
per-kind run-options catalog (``ANALYST_KIND_OPTIONS``) — a kind without a
catalog REFUSES it at registration, and seven judge-wired kinds have none. The
09-28 rule (a registry 200 is not a runtime parse) is met by the shape itself:
``tests/runtime/test_judge_fallback_chain.py`` validates a descriptor carrying
the knob through ``AnalystDescriptor``.

THE WEAK RUNG. The in-house 8B (``llm.verify.slm_8b``) was tried as THE judge on
2026-07-01 and dropped as too weak (harsh and mis-aimed). As a LAST rung it beats
the floor — it reads the claims against the cited text, which the floor cannot —
but its verdicts are labelled: ``judge_model`` carries its model id, the
verification block carries ``judge_chain.weak = true``, and ``judge_route`` reads
``chain:<component_id>``. Nothing pools it silently with a hosted verdict.

A LEAF: no imports from the rest of the runtime package. ``dapr_host`` calls
:func:`wire_judge_chain` in place of its old inline block.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..data.provenance.judge_failure_class import (
    CLASS_EMPTY,
    CLASS_UNRESOLVABLE,
    classify_exception,
    classify_status,
    is_route_gone,
)
from ..data.provenance.judge_transport import envelope_error

logger = logging.getLogger(__name__)

#: The descriptor key (``method.llm.judge_chain``).
JUDGE_CHAIN_KEY = "judge_chain"

#: The platform default rungs AFTER the resolved judge route: the $0 in-house
#: rung ONLY. A paid second rung (e.g. a second hosted judge) is OPT-IN — declare
#: it in ``method.llm.judge_chain`` or deployment-wide in
#: ``LEGBA_JUDGE_CHAIN_DEFAULT`` (comma-separated refs; empty string = no tail).
#: House rule: no default moves spend; a rung-1-only outage must never silently
#: shift the judge volume onto another paid model.
DEFAULT_JUDGE_CHAIN_TAIL: tuple[str, ...] = ("llm.verify.slm_8b",)
JUDGE_CHAIN_DEFAULT_ENV = "LEGBA_JUDGE_CHAIN_DEFAULT"

#: Rungs whose verdicts are WEAK (labelled ``judge_chain.weak``). Override:
#: ``LEGBA_JUDGE_WEAK_RUNGS`` (comma-separated refs).
DEFAULT_WEAK_RUNGS: tuple[str, ...] = ("llm.verify.slm_8b",)
JUDGE_WEAK_RUNGS_ENV = "LEGBA_JUDGE_WEAK_RUNGS"

#: A chain longer than this is a configuration error, not a strategy — every
#: rung is a full judge call on the failure path.
MAX_RUNGS = 5

#: ``judge_route`` prefix for a verdict a later rung wrote. The three ladder
#: classes (``configured`` / ``fallback_verify`` / ``fallback_primary``) are
#: untouched; this is an ADDITIVE value.
CHAIN_ROUTE_PREFIX = "chain:"

#: The two floor reasons that mean "the judge was ASKED and did not answer".
#: ``flag_off`` / ``no_judge_component`` / ``unsampled`` never asked anyone.
UNAVAILABLE_REASONS = frozenset({"judge_error", "judge_empty"})


def _ref(value: Any) -> str | None:
    if isinstance(value, Mapping):
        raw = value.get("raw")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None
    raw = getattr(value, "raw", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _env_refs(name: str, default: Sequence[str]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return tuple(default)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def weak_rungs() -> frozenset[str]:
    return frozenset(_env_refs(JUDGE_WEAK_RUNGS_ENV, DEFAULT_WEAK_RUNGS))


def resolve_judge_chain(llm_block: Any, first_ref: str) -> tuple[str, ...]:
    """The ordered rungs for one descriptor. Rung 1 = ``first_ref`` (the ladder's
    resolved route), always. A declared ``judge_chain`` (a list of refs — StackRef
    dumps or bare strings) supplies the rest; absent ⇒ the platform default tail.
    De-duplicated in order, bounded at :data:`MAX_RUNGS`."""
    declared: Any = None
    if isinstance(llm_block, Mapping) and JUDGE_CHAIN_KEY in llm_block:
        declared = llm_block.get(JUDGE_CHAIN_KEY)
        if not isinstance(declared, (list, tuple)):
            logger.warning(
                "judge_fallback_chain.malformed judge_chain=%r — expected a list "
                "of stack refs; the chain is the resolved judge alone", declared,
            )
            declared = []
        tail = [r for r in (_ref(v) for v in declared) if r]
    else:
        tail = list(_env_refs(JUDGE_CHAIN_DEFAULT_ENV, DEFAULT_JUDGE_CHAIN_TAIL))
    out: list[str] = [first_ref]
    for ref in tail:
        if ref not in out:
            out.append(ref)
    if len(out) > MAX_RUNGS:
        logger.warning(
            "judge_fallback_chain.truncated rungs=%d max=%d", len(out), MAX_RUNGS,
        )
    return tuple(out[:MAX_RUNGS])


def handler_model(handler: Any) -> str:
    """The configured model id behind a handler (``cfg.model_name.raw``), else
    its subprovider. Read defensively — a test double carries neither."""
    cfg = getattr(handler, "_cfg", None)
    name = getattr(cfg, "model_name", None)
    raw = getattr(name, "raw", name)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return str(getattr(handler, "subprovider", "") or "")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class JudgeChain:
    """The rungs + the cached handlers. Long-lived (one per analyst deps build);
    all PER-PASS state lives on :class:`JudgeChainSession`.

    It duck-types the handler it wraps — ``subprovider`` and ``chat_complete``
    — so any caller that does not open a session still gets rung 1 first and
    the fallback on a route-gone failure (a one-shot session per call).
    """

    def __init__(
        self,
        rungs: Sequence[str],
        *,
        first_handler: Any,
        factory: Callable[[str], Awaitable[Any]],
        route_class: str = "",
    ) -> None:
        if not rungs:
            raise ValueError("a judge chain needs at least one rung")
        self.rungs: tuple[str, ...] = tuple(rungs)
        self.route_class = route_class
        self._factory = factory
        self._handlers: dict[int, Any] = {0: first_handler}

    @property
    def first_handler(self) -> Any:
        return self._handlers[0]

    @property
    def subprovider(self) -> Any:
        return getattr(self._handlers[0], "subprovider", "")

    async def handler(self, index: int) -> Any:
        """The handler for rung ``index`` (0-based), built lazily and cached."""
        if index not in self._handlers:
            self._handlers[index] = await self._factory(self.rungs[index])
        return self._handlers[index]

    def session(self) -> "JudgeChainSession":
        return JudgeChainSession(self)

    async def chat_complete(self, messages: Any, **kwargs: Any) -> Any:
        return await self.session().chat_complete(messages, **kwargs)


class JudgeChainSession:
    """ONE verify pass over a chain. Sticky: once a rung is route-gone in this
    pass, every later call of the pass (the next partition, the shim's retry)
    starts at the rung that answered — a dead key is not asked twice per
    finding. A NEW pass starts at rung 1 again, so recovery is immediate."""

    def __init__(self, chain: JudgeChain) -> None:
        self.chain = chain
        self._index = 0
        self.used_index: int | None = None
        #: Every failed call, in order: ``{component_id, rung, class, http_status, at}``.
        self.failures: list[dict[str, Any]] = []

    # -- the duck-typed handler surface ----------------------------------

    @property
    def subprovider(self) -> Any:
        idx = self.used_index if self.used_index is not None else 0
        return getattr(self.chain._handlers.get(idx), "subprovider", "")

    async def chat_complete(self, messages: Any, **kwargs: Any) -> Any:
        rungs = self.chain.rungs
        while True:
            i = self._index
            ref = rungs[i]
            last = i + 1 >= len(rungs)
            try:
                handler = await self.chain.handler(i)
            except Exception as exc:  # noqa: BLE001 — an unbuildable rung is a gone rung
                self._record(i, CLASS_UNRESOLVABLE, None)
                logger.warning(
                    "judge_fallback_chain.rung_unresolvable rung=%d component_id=%s "
                    "err=%s", i + 1, ref, type(exc).__name__,
                )
                if last:
                    raise
                self._index = i + 1
                continue
            try:
                response = await handler.chat_complete(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001 — classified, then moved or re-raised
                cls, status = classify_exception(exc)
                self._record(i, cls, status)
                if is_route_gone(cls) and not last:
                    logger.warning(
                        "judge_fallback_chain.moved from_rung=%d component_id=%s "
                        "class=%s http_status=%s -> rung %d %s — the judge ROUTE is "
                        "gone, not busy", i + 1, ref, cls, status, i + 2, rungs[i + 1],
                    )
                    self._index = i + 1
                    continue
                raise
            err = envelope_error(response)
            if err is not None:
                cls = classify_status(err.get("code") if isinstance(err, Mapping) else None)
                if is_route_gone(cls):
                    self._record(i, cls, err.get("code"))
                    if not last:
                        self._index = i + 1
                        continue
                return response  # the shim classifies every other envelope itself
            self.used_index = i
            return response

    def _record(self, index: int, cls: str, status: Any) -> None:
        if status is None:
            token = None
        elif isinstance(status, int):
            token = str(int(status))  # a RouterStatus records its INNER code
        else:
            token = str(status)
        self.failures.append({
            "component_id": self.chain.rungs[index],
            "rung": index + 1,
            "class": cls,
            "http_status": token,
            "at": _now_iso(),
        })


def judge_stamps(
    *,
    judge: Any,
    session: Any,
    judge_model: str,
    judge_llm_ref: str,
    judge_route: str,
    report: Any,
) -> tuple[str, str, str, dict[str, Any]]:
    """``(judge_model, judge_llm_ref, judge_route, verification_extras)``.

    Byte-identical to the pre-chain stamps whenever rung 1 answered or no chain
    is wired. A LATER rung that answered restamps all three — its model id, its
    ref, ``chain:<ref>`` — and adds ``verification.judge_chain`` (what was tried,
    what answered, whether it is a weak rung). A pass that reached the floor
    after the judge was ASKED adds ``verification.unverified_reason``: the last
    failure's class, http status, component and time.
    """
    extras: dict[str, Any] = {}
    sess = session if isinstance(session, JudgeChainSession) else None
    if sess is not None and sess.used_index:
        used = sess.used_index
        ref = sess.chain.rungs[used]
        handler = sess.chain._handlers.get(used)
        judge_model = handler_model(handler) or judge_model
        judge_llm_ref = ref
        judge_route = f"{CHAIN_ROUTE_PREFIX}{ref}"
        extras["judge_chain"] = {
            "used": ref,
            "rung": used + 1,
            "model": judge_model,
            "weak": ref in weak_rungs(),
            "head": sess.chain.rungs[0],
            "tried": list(sess.failures),
        }
    elif sess is not None and sess._index > 0:
        # The chain MOVED and no rung answered: the trail is the evidence.
        extras["judge_chain"] = {
            "used": None,
            "rung": None,
            "model": None,
            "weak": False,
            "head": sess.chain.rungs[0],
            "tried": list(sess.failures),
        }
    status = getattr(report, "judge_status", None)
    reason = getattr(report, "judge_unavailable_reason", None)
    if judge is not None and status == "deterministic" and reason in UNAVAILABLE_REASONS:
        last = sess.failures[-1] if (sess is not None and sess.failures) else None
        if last is None:
            # No raised call on record (a plain handler, or a judge that ANSWERED
            # with nothing usable): read the transport shim's own receipt.
            tokens = list(getattr(report, "judge_http_statuses", None) or [])
            token = tokens[-1] if tokens else None
            if not token or token == "200":
                cls = CLASS_EMPTY if reason == "judge_empty" else str(reason)
            else:
                cls = classify_status(token)
            last = {
                "component_id": judge_llm_ref,
                "rung": (sess._index + 1) if sess is not None else 1,
                "class": cls,
                "http_status": token,
                "at": _now_iso(),
            }
        tried = [int(f["rung"]) for f in sess.failures] if sess is not None else []
        extras["unverified_reason"] = {
            "class": last["class"],
            "http_status": last.get("http_status"),
            "component_id": last.get("component_id"),
            "rungs_tried": max(tried, default=1),
            "judge_unavailable_reason": reason,
            "at": last.get("at") or _now_iso(),
        }
    return judge_model, judge_llm_ref, judge_route, extras


def open_session(judge: Any) -> Any:
    """A per-pass session over a chain; any other handler passes through."""
    return judge.session() if isinstance(judge, JudgeChain) else judge


async def wire_judge_chain(
    route: Any,
    llm_block: Any,
    *,
    llm_handler_factory: Callable[[str], Awaitable[Any]],
    actor_id: str = "",
    analyst_id: str = "",
) -> tuple[Any, str, str]:
    """``(verify_judge, verify_judge_ref, verify_judge_route)`` for the host.

    The old ``dapr_host`` block, moved: rung 1 is built through the factory
    EXACTLY as before (same ref in → same client out; the ref and the route
    class stamped are the ladder's). A chain of more than one rung wraps that
    same client; a one-rung chain (``judge_chain: []``) returns it bare.
    SOFT-FAIL: a rung-1 build error yields ``(None, "", "")`` and a warning —
    the deterministic floor runs, exactly as before.
    """
    try:
        first = await llm_handler_factory(route.component_id)
    except Exception as exc:  # noqa: BLE001 — soft-fail, floor still runs
        logger.warning(
            "analyst_deps_resolver.verify_judge_resolve_failed actor_id=%s "
            "analyst=%s component_id=%s source=%s err=%s — the faithfulness "
            "verify degrades to its deterministic floor",
            actor_id, analyst_id, route.component_id, route.source, exc,
        )
        return None, "", ""
    rungs = resolve_judge_chain(llm_block, route.component_id)
    judge: Any = first
    if len(rungs) > 1:
        judge = JudgeChain(
            rungs, first_handler=first, factory=llm_handler_factory,
            route_class=route.route_class,
        )
    logger.info(
        "analyst_deps_resolver.verify_judge_wired actor_id=%s analyst=%s "
        "judge_ref=%s source=%s route=%s chain=%s",
        actor_id, analyst_id, route.component_id, route.source,
        route.route_class, ",".join(rungs),
    )
    return judge, route.component_id, route.route_class


__all__ = [
    "CHAIN_ROUTE_PREFIX",
    "DEFAULT_JUDGE_CHAIN_TAIL",
    "DEFAULT_WEAK_RUNGS",
    "JUDGE_CHAIN_DEFAULT_ENV",
    "JUDGE_CHAIN_KEY",
    "JUDGE_WEAK_RUNGS_ENV",
    "MAX_RUNGS",
    "UNAVAILABLE_REASONS",
    "JudgeChain",
    "JudgeChainSession",
    "handler_model",
    "judge_stamps",
    "open_session",
    "resolve_judge_chain",
    "weak_rungs",
    "wire_judge_chain",
]
