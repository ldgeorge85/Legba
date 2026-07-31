# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dapr reminder orphan-GC sweep (release-engineering; REVIEW §3.4 operability).

Background
----------
Daprd's scheduler persists actor reminders in its embedded etcd across
container restarts. The runtime's *only* cleanup today is **actor
self-disarm on fire** (``reminder_guard_decision`` in
:mod:`legba.runtime.dapr_actors`): when a stale reminder *fires*, the
actor recognises it as no-longer-head / retired and unregisters it.

That leaves a real failure window the RUNBOOK calls out (§0, §11 "Clean
up stale daprd reminders"): a reminder whose owning actor has **retired**
but which *never fires again* (idled out, or the scheduler etcd lost the
recurrence mapping) is an **orphan** — nothing reactivates it, so the
self-disarm path never runs. The only remediation documented today is a
FULL wipe of ``deploy/dapr-scheduler-data`` (which also nukes the *live*
reminders). This module is the surgical alternative: a periodic sweep
that unregisters only **provably-orphan** reminders and leaves every live
reminder untouched.

Safety contract (the load-bearing invariant)
---------------------------------------------
A buggy sweep that unregisters a LIVE reminder *re-creates the exact
stall it is meant to fix*. So this sweep acts ONLY on reminders whose
owning ``actor_state`` row is **RETIRED** — i.e. a row the reconcile loop
has already decided should run nothing. It NEVER touches an ``active`` /
``paused`` / ``error`` / ``draft`` / ``configured`` actor, and it NEVER
infers an actor that has no ``actor_state`` row at all (those are handled
by the version-drift sibling sweep + the on-fire guard, which have the
descriptor head to compare against — this sweep deliberately does not
guess). Retired is the one lifecycle where "this actor owns zero
reminders" is unambiguously the desired state.

Mechanism (part 1 — desired-vs-observed, ships now)
---------------------------------------------------
For each retired ``actor_state`` row we derive the reminder name(s) that
actor *would* have registered when it was live (from the actor kind +
its recorded ``source_cursors``), then check daprd for real:
``GET /v1.0/actors/{type}/{id}/reminders/{name}`` first, and only when
that confirms the reminder genuinely exists do we issue the
``DELETE`` against the same sidecar HTTP API. This GET-before-DELETE
order matters: daprd returns a 2xx on ``DELETE`` for a reminder that was
never there just as readily as for one it actually removed, so a bare
DELETE-and-count-2xx sweep cannot tell a real orphan from an absent one
(2026-07 DQ finding — see :data:`ReminderDeleter`). Only a GET-hit
followed by a successful DELETE counts toward :attr:`ReminderGCResult.removed`;
a GET-miss is tallied under :attr:`ReminderGCResult.already_absent` and
never counted as :attr:`ReminderGCResult.took_action`. Re-running the
sweep is still cheap (it's read-mostly against the sidecar), and in
steady state ``removed`` is genuinely zero — ``already_absent`` is not,
because retired ``actor_state`` rows are never pruned (the actor_id
grammar embeds ``content_hash[:16]``, so every descriptor edit mints a
new actor_id and leaves the old one RETIRED forever — see
``ActorStateStore.list_by_lifecycle``); each of those stale rows is a
distinct candidate every sweep. That volume is legitimate re-checking,
not a bug this module can safely dedupe away: a retired actor_id is its
own point in daprd's reminder namespace, so two different retired rows
that happen to derive the same reminder *name* are not interchangeable
candidates and collapsing them risks skipping a real orphan. We do NOT
re-activate the actor (no ``run`` / ``activate`` proxy call) — reaching
the sidecar by name avoids waking a retired actor.

When a sweep *actually removes* one or more reminders it logs at INFO and
fires an operator alert via the injected ``alert_publish`` closure
(a NATS publish to ``legba.alerts.reminder_gc``) so an orphan that was
silently halting cadence is now observable.

Part 2 (scheduler-side enumeration of the full reminder set — to GC
reminders whose ``actor_state`` row was itself lost) is intentionally
deferred behind ``LEGBA_REMINDER_GC_SCHEDULER_SCAN`` and tracked as a
declared seam (docs/SEAMS.md) because it requires reading the
dapr-scheduler etcd keyspace directly (daprd 1.17.9 exposes no
reminder-listing API on :3500). The desired-vs-observed sweep here
closes the documented orphan-on-retire failure mode without that.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .lifecycle import RETIRED
from .state import ActorStateRecord, ActorStateStore

logger = logging.getLogger(__name__)

# The operator alert is a BEST-EFFORT side-channel: it publishes onto
# ``legba.alerts.reminder_gc`` for live subscribers (the UI feed / a tail
# consumer), exactly like the liveness watchdog (``legba.alerts.high``) and the
# ``alert`` output kind (``legba.alerts.<severity>``). NONE of the
# ``legba.alerts.*`` subjects are backed by a JetStream stream by design — they
# are a core-NATS notification surface, not a durable log. But
# ``NatsStore.publish_json`` does a JetStream ``js.publish``, which REQUIRES a
# capturing stream and returns ``nats: no response from stream`` when none
# exists. With no alerts stream provisioned, every sweep that removed an orphan
# was logging ``reminder_gc.alert_publish_failed`` at WARNING — pure noise, the
# GC itself succeeded. We classify that no-stream / no-responders family as
# BENIGN (the alert simply had no live capturing surface) and log it once at
# DEBUG, matching the established precedent in
# ``dapr_actors`` analyst-output publish. A genuinely different publish failure
# (broker drain, serialisation) still surfaces at WARNING so a real regression
# is not masked. We deliberately do NOT create a durable alerts stream here:
# that would change the retention semantics of the whole ``legba.alerts.*``
# surface (shared by the watchdog + alert sink) far beyond this best-effort tail.
_BENIGN_PUBLISH_MARKERS = ("no response from stream", "no responders", "no stream")
_no_stream_logged = False


def _is_benign_publish_error(exc: Exception) -> bool:
    """True iff the publish failed only because no stream captures the subject."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _BENIGN_PUBLISH_MARKERS)


# Sidecar HTTP base — mirrors consult_api / deep_consult_api resolution so
# the GC reaches the same daprd the rest of the runtime uses.
DAPR_SIDECAR_URL_ENV = "LEGBA_DAPR_SIDECAR_URL"
DAPR_SIDECAR_URL_DEFAULT = "http://dapr-sidecar:3500"

# actor_kind (actor_state) → Dapr actor type (registered with the runtime).
_ACTOR_TYPE_BY_KIND: dict[str, str] = {
    "target": "TargetActor",
    "analyst": "AnalystActor",
    "source": "SourceActor",
}


def dapr_sidecar_url() -> str:
    """Resolve the daprd sidecar HTTP base URL (env-overridable)."""
    return os.getenv(DAPR_SIDECAR_URL_ENV, DAPR_SIDECAR_URL_DEFAULT).strip().rstrip("/")


def orphan_reminder_names(record: ActorStateRecord) -> list[str]:
    """The reminder name(s) a now-retired actor would have registered.

    Pure: derives names from the actor kind + recorded source cursors. No
    daprd / network call. Returned names are the GC delete candidates.

      * ``source``  → ``poll_<descriptor_id>`` — the SourceActor poll
        reminder (``source_actor.py`` registers ``poll_<source_id>``; for
        a single-source actor ``source_id == descriptor_id``). We also
        emit ``poll_<cursor_source_id>`` for every recorded
        ``source_cursors`` key so a multi-source actor (or a renamed
        source) is fully covered.
      * ``analyst`` → ``run_cadence`` — the AnalystActor cadence reminder.
      * ``target``  → ``run_source_<source_id>`` for each recorded cursor
        — the **legacy** target-owned poll path (L-205 retired it; any
        surviving reminder is pre-pivot pollution the on-fire guard also
        disarms). Targets register no reminders in the source-first
        runtime, so this is empty for a clean actor.

    Conservative by construction: an unrecognised kind yields ``[]`` (the
    sweep skips it) rather than guessing a name that might collide with a
    live actor's reminder.
    """
    kind = record.actor_kind
    descriptor_id = record.descriptor_id
    cursor_ids = list(record.source_cursors.keys())

    if kind == "source":
        names = [f"poll_{descriptor_id}"]
        names.extend(f"poll_{sid}" for sid in cursor_ids if sid != descriptor_id)
        # Stable, de-duplicated order.
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out
    if kind == "analyst":
        return ["run_cadence"]
    if kind == "target":
        return [f"run_source_{sid}" for sid in cursor_ids]
    return []


@dataclass
class ReminderGCResult:
    """Outcome of one sweep — auditable + assertable in tests."""

    retired_scanned: int = 0
    candidates: int = 0
    removed: int = 0
    already_absent: int = 0  # GET-miss: genuinely nothing to delete, not an action
    failed: int = 0
    removed_names: list[tuple[str, str]] = field(default_factory=list)  # (actor_id, name)

    @property
    def took_action(self) -> bool:
        return self.removed > 0


# A pluggable "delete one reminder by name" — production wires a daprd
# sidecar GET-then-DELETE; tests inject a recorder. Returns True iff the
# reminder was CONFIRMED present (a GET-hit) and then successfully
# deleted; False iff the GET already showed it absent (nothing to do —
# not an action, not a failure). A per-attempt exception (network error,
# an unexpected non-2xx/404 status) is the caller's signal to count it as
# `failed` instead — see :func:`build_sidecar_reminder_deleter`. This
# distinction is load-bearing: daprd's DELETE returns a 2xx for a
# reminder that was never there just as readily as for one it actually
# removed, so counting "removed" straight off the DELETE response (the
# pre-fix behaviour) cannot tell a real orphan from an absent one. Tests
# assert the exact call set, which is the safety-critical surface.
ReminderDeleter = Callable[[str, str, str], Awaitable[bool]]
# An operator-alert publish closure: (subject, payload_bytes) -> awaitable.
AlertPublisher = Callable[[str, bytes], Awaitable[None]]


async def sweep_orphan_reminders(
    *,
    state_store: ActorStateStore,
    delete_reminder: ReminderDeleter,
    alert_publish: AlertPublisher | None = None,
) -> ReminderGCResult:
    """Unregister reminders owned by RETIRED actors. Provably-orphan only.

    Args:
      state_store: source of truth for observed actor lifecycle.
      delete_reminder: ``(actor_type, actor_id, reminder_name) -> bool``.
        Must be idempotent (a missing reminder is a no-op success).
      alert_publish: optional ``(subject, payload) -> awaitable`` used to
        fire an operator alert when the sweep actually removed something.

    Returns a :class:`ReminderGCResult`. Never raises on a per-actor
    failure — one un-deletable reminder must not abort the whole sweep
    (the next sweep retries; reconcile stays converging).
    """
    result = ReminderGCResult()
    try:
        retired = await state_store.list_by_lifecycle(RETIRED)
    except Exception as exc:  # pragma: no cover — store outage
        logger.warning("reminder_gc.list_failed err=%s", exc)
        return result

    result.retired_scanned = len(retired)
    for rec in retired:
        # Defence in depth: never act on a non-retired row even if the
        # lister ever returned one. (list_by_lifecycle filters, but the
        # invariant is too important to rely on a single guard.)
        if rec.lifecycle != RETIRED:
            continue
        actor_type = _ACTOR_TYPE_BY_KIND.get(rec.actor_kind)
        if actor_type is None:
            logger.debug(
                "reminder_gc.skip_unknown_kind actor_id=%s kind=%s",
                rec.actor_id, rec.actor_kind,
            )
            continue
        for name in orphan_reminder_names(rec):
            result.candidates += 1
            try:
                removed = await delete_reminder(actor_type, rec.actor_id, name)
            except Exception as exc:
                result.failed += 1
                logger.warning(
                    "reminder_gc.delete_failed actor_id=%s reminder=%s err=%s",
                    rec.actor_id, name, exc,
                )
                continue
            if removed:
                result.removed += 1
                result.removed_names.append((rec.actor_id, name))
                logger.info(
                    "reminder_gc.removed actor_id=%s reminder=%s",
                    rec.actor_id, name,
                )
            else:
                result.already_absent += 1
                logger.debug(
                    "reminder_gc.already_absent actor_id=%s reminder=%s",
                    rec.actor_id, name,
                )

    if result.took_action and alert_publish is not None:
        import json

        payload = json.dumps(
            {
                "kind": "reminder_gc",
                "severity": "info",
                "removed": result.removed,
                "retired_scanned": result.retired_scanned,
                "reminders": [
                    {"actor_id": a, "reminder": n} for a, n in result.removed_names
                ],
                "summary": (
                    f"reconciliation sweep unregistered {result.removed} orphan "
                    f"daprd reminder(s) owned by retired actors"
                ),
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            await alert_publish("legba.alerts.reminder_gc", payload)
        except Exception as exc:  # best-effort telemetry — never blocks the GC
            if _is_benign_publish_error(exc):
                # No capturing stream for the alert subject — the GC already
                # succeeded; this is expected on a stack with no alerts stream
                # provisioned. Log ONCE at DEBUG instead of a WARNING per sweep.
                global _no_stream_logged
                if not _no_stream_logged:
                    _no_stream_logged = True
                    logger.debug(
                        "reminder_gc.alert_publish_no_stream subject=%s err=%s "
                        "(benign — no capturing stream; GC succeeded, "
                        "suppressing further occurrences)",
                        "legba.alerts.reminder_gc", exc,
                    )
            else:
                logger.warning("reminder_gc.alert_publish_failed err=%s", exc)

    logger.info(
        "reminder_gc.sweep retired_scanned=%d candidates=%d removed=%d "
        "already_absent=%d failed=%d",
        result.retired_scanned, result.candidates, result.removed,
        result.already_absent, result.failed,
    )
    return result


def build_sidecar_reminder_deleter(
    *,
    sidecar_url: str | None = None,
    timeout_s: float = 15.0,
) -> ReminderDeleter:
    """Production :data:`ReminderDeleter` backed by the daprd sidecar.

    GET-then-DELETE, deliberately in that order:

      * ``GET /v1.0/actors/{type}/{id}/reminders/{name}`` first. daprd
        (per the actor-runtime error table — ``ErrActorReminderNotFound``,
        HTTP 404) answers 404 when the named reminder does not exist for
        that actor, and 200 with the reminder body when it does. A 404
        (or a 200 with an empty/null body, defensively — some daprd
        builds have returned that instead of a 404 for the same case)
        means there is genuinely nothing to remove: return ``False``
        without ever calling DELETE.
      * Only on a confirmed GET-hit do we issue the
        ``DELETE /v1.0/actors/{type}/{id}/reminders/{name}`` and return
        ``True`` on a 2xx.

    This exists because ``DELETE`` alone is not a safe removed/absent
    signal: daprd returns a 2xx for a reminder that was never registered
    just as readily as for one it actually deleted (idempotent by design),
    so counting straight off the DELETE response — the pre-fix behaviour
    here — reported every already-absent candidate as "removed" and made
    the GC's alert fire, and look identical, on every single sweep. Any
    other GET status, or a non-2xx DELETE after a confirmed GET-hit, is
    raised so the caller (:func:`sweep_orphan_reminders`) counts it under
    ``failed`` rather than silently swallowing it as absent.

    Does NOT activate the actor through the run path.
    """
    base = (sidecar_url or dapr_sidecar_url()).rstrip("/")

    async def _delete(actor_type: str, actor_id: str, reminder_name: str) -> bool:
        import httpx

        url = f"{base}/v1.0/actors/{actor_type}/{actor_id}/reminders/{reminder_name}"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            get_resp = await client.get(url)
            if get_resp.status_code == 404:
                return False  # confirmed absent — nothing to delete
            if get_resp.status_code != 200:
                raise RuntimeError(
                    f"reminder_gc sidecar GET non-200/404 actor={actor_id} "
                    f"reminder={reminder_name} status={get_resp.status_code}"
                )
            if not _reminder_body_present(get_resp):
                # Defensive: some daprd builds answer 200 + empty/null body
                # instead of 404 for a reminder that does not exist.
                return False

            del_resp = await client.delete(url)

        if 200 <= del_resp.status_code < 300:
            return True
        raise RuntimeError(
            f"reminder_gc sidecar DELETE non-2xx actor={actor_id} "
            f"reminder={reminder_name} status={del_resp.status_code} "
            "(GET confirmed it existed)"
        )

    return _delete


def _reminder_body_present(resp: Any) -> bool:
    """True iff a 200 GET response body actually describes a reminder.

    Defence against daprd builds that answer a missing reminder with
    ``200`` + an empty/null body rather than a ``404`` — treat that the
    same as a 404 rather than tripping the DELETE path on nothing.
    """
    raw = resp.content
    if not raw or raw.strip() in (b"{}", b"null", b'""'):
        return False
    try:
        body = resp.json()
    except ValueError:
        return True  # non-empty, non-JSON body — assume it's real
    return bool(body)


__all__ = [
    "AlertPublisher",
    "ReminderDeleter",
    "ReminderGCResult",
    "build_sidecar_reminder_deleter",
    "dapr_sidecar_url",
    "orphan_reminder_names",
    "sweep_orphan_reminders",
]
