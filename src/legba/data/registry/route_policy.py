# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MINIMUM ROLE of every registry route — the sign-in switch's table (A-1).

With ``LEGBA_AUTH_SIGNIN=1`` the app-level gate (``route_roles.
enforce_route_role``) reads THIS table instead of the October class table
(``route_roles.ROUTE_ROLES``, which stays the switch-off answer unchanged).
Three roles, ascending (``legba.shared.auth_signin``):

  * ``reader``      — the reader surfaces (board, subjects, ledger, watch,
                      the record), their own Consult sessions, their own
                      watch list, read receipts and exports;
  * ``contributor`` — a reader, plus attributed judgements and knowledge
                      edits that go through a gate: grading labels and the
                      journal's proposal decisions;
  * ``admin``       — everything: sources, descriptors, the stack, the vault,
                      users, curation, the engine's own surfaces (the
                      "operator side": system gauges, evaluation internals,
                      the optimizer, flow, budgets, the drift queue). The
                      owner is an admin with ``users.is_owner``.

EVERY ROUTE IS DECLARED. Unlike the October table (where an unlisted GET was
``read``), a route missing here is refused below ``admin`` when the switch is
on, and ``scripts/gen_api_roles_doc.py --check`` fails on any mounted route
that has no entry (or an entry the app no longer mounts). ``HEAD`` reads its
``GET`` entry; a WebSocket upgrade is listed as ``WEBSOCKET``.

PUBLIC routes (no credential: the probes, the API description, the three
credential steps) are ``route_roles.PUBLIC_ROUTES`` and carry ``reader`` here.

Registry-slim LEAF: stdlib + ``legba.shared.auth_signin``.
"""

from __future__ import annotations

from legba.shared import auth_signin as signin
from legba.shared.auth_signin import (
    ROLE_ADMIN as A,
    ROLE_CONTRIBUTOR as C,
    ROLE_READER as R,
    ROLE_RANK,
    ROLES,
)

_REG = "/api/v1/registry"
_V3 = "/api/v1/v3"
_V1 = "/api/v1"

#: One line per role — rendered into docs/API_ROLES.md.
ROLE_DESCRIPTIONS: dict[str, str] = {
    A: (
        "Everything: sources, descriptors, the stack, the vault, users, "
        "curation, Consult-driven changes, and the engine's own surfaces "
        "(system gauges, evaluation internals, optimizer, flow, budgets). "
        "Every service credential and the break-glass admin token are this "
        "role (services are still refused on the human-only routes). The "
        "owner is an admin with `users.is_owner`."
    ),
    C: (
        "Everything `reader` can, plus attributed judgements and gated "
        "knowledge edits: grading labels, the gold-set worksheet, the "
        "journal's proposal decisions."
    ),
    R: (
        "The reader surfaces, the user's own Consult sessions and runs, "
        "their own watch list, read receipts and exports. No change to "
        "shared configuration or the record."
    ),
}

#: ``(METHOD, mounted path template) → minimum role``. Grouped by module.
ROUTE_MIN_ROLES: dict[tuple[str, str], str] = {
    # -- api.py — registry core: configuration is admin --------------------
    ("GET", f"{_REG}/action_packs"): A,
    ("GET", f"{_REG}/action_packs/{{descriptor_id}}"): A,
    ("GET", f"{_REG}/audit"): A,
    ("GET", f"{_REG}/audit/{{entry_id}}"): A,
    ("GET", f"{_REG}/config/status"): A,
    ("GET", f"{_REG}/conversions"): A,
    ("POST", f"{_REG}/conversions"): A,
    ("DELETE", f"{_REG}/conversions/{{webhook_id}}"): A,
    ("GET", f"{_REG}/dead_letter"): A,
    ("GET", f"{_REG}/dead_letter/{{entry_id}}"): A,
    ("POST", f"{_REG}/dead_letter/{{entry_id}}/resubmit"): A,
    ("GET", f"{_REG}/descriptors"): A,
    ("POST", f"{_REG}/descriptors/{{family}}"): A,
    ("GET", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}"): A,
    ("PUT", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}"): A,
    ("GET", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/history"): A,
    ("GET", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/typed"): A,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/promote"): A,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/retire"): A,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/rollback"): A,
    ("POST", f"{_REG}/descriptors/{{family}}/{{descriptor_id}}/transition"): A,
    ("GET", f"{_REG}/governor_events"): A,
    ("GET", f"{_REG}/sources"): A,
    ("GET", f"{_REG}/sources/{{descriptor_id}}"): A,
    ("GET", f"{_REG}/stack"): A,
    ("POST", f"{_REG}/stack"): A,
    ("GET", f"{_REG}/stack/by-kind/{{kind}}/{{name}}"): A,
    ("GET", f"{_REG}/stack/{{component_id}}"): A,
    ("PUT", f"{_REG}/stack/{{component_id}}"): A,
    ("POST", f"{_REG}/stack/{{component_id}}/healthcheck"): A,
    ("POST", f"{_REG}/stack/{{component_id}}/retire"): A,
    ("POST", f"{_REG}/stack/{{component_id}}/rollback"): A,
    ("POST", f"{_REG}/targets/{{target_id}}/backfill"): A,
    ("POST", f"{_REG}/vault/secrets"): A,
    ("DELETE", f"{_REG}/vault/secrets/{{secret_id}}"): A,
    ("GET", f"{_REG}/vault/secrets/{{secret_id}}/exists"): A,
    ("GET", f"{_REG}/vocabulary"): R,  # labels the console renders
    ("GET", f"{_REG}/vocabulary/{{family}}"): R,
    ("POST", f"{_REG}/vocabulary/{{family}}"): A,
    ("PUT", f"{_REG}/vocabulary/{{family}}/{{entry_id}}"): A,
    ("DELETE", f"{_REG}/vocabulary/{{family}}/{{entry_id}}"): A,
    ("GET", f"{_REG}/ui_panels"): R,  # the console's panel catalogue
    ("GET", f"{_REG}/ui_panels/by_slot/{{layout_slot}}"): R,
    ("WEBSOCKET", f"{_REG}/events"): R,  # display policy applies per frame
    # -- server.py / framework / metrics_api.py: PUBLIC ------------------
    ("GET", f"{_REG}/healthz"): R,
    ("GET", f"{_REG}/docs"): R,
    ("GET", f"{_REG}/openapi.json"): R,
    ("GET", "/metrics"): R,
    # -- projection_api.py ----------------------------------------------
    ("GET", f"{_REG}/projection/{{plane}}"): R,
    ("GET", f"{_REG}/projection/{{plane}}/{{section}}"): R,
    # -- users_api.py ---------------------------------------------------
    ("POST", f"{_V1}/auth/accept"): R,  # public
    ("POST", f"{_V1}/auth/sign-in"): R,  # public
    ("POST", f"{_V1}/auth/sign-out"): R,  # public
    ("GET", f"{_V1}/auth/me"): R,
    ("POST", f"{_V1}/auth/invite"): A,
    ("GET", f"{_V1}/auth/users"): A,
    ("PATCH", f"{_V1}/auth/users/{{user_id}}"): A,
    # -- consult: own sessions and runs (owner-checked in the handlers) --
    ("POST", f"{_V1}/consult"): R,  # an ARMED turn needs admin (session_auth)
    ("GET", f"{_V1}/consult/runs/{{request_id}}"): R,
    ("POST", f"{_V1}/consult/runs/{{request_id}}/stop"): R,
    ("POST", f"{_V1}/consult/runs/{{request_id}}/synthesize"): R,
    ("GET", f"{_V1}/consult/sessions"): R,
    ("GET", f"{_V1}/consult/sessions/{{session_id}}"): R,
    ("GET", f"{_V1}/consult/stream/{{request_id}}"): R,
    ("GET", f"{_V1}/consult/usage"): R,  # own usage; another's needs admin
    ("POST", f"{_V1}/deep_consult"): R,
    ("GET", f"{_V1}/deep_consult/{{task_id}}"): R,
    # consult_writes_api.py — substrate changes (sources, targets, units,
    # entities, facts) through one route: admin until the per-kind check
    # lets a contributor's fact through (SEAMS: per-kind consult writes).
    ("GET", f"{_V1}/consult/writes"): A,
    ("POST", f"{_V1}/consult/writes"): A,
    ("POST", f"{_V1}/consult/writes/{{write_id}}/cancel"): A,
    ("POST", f"{_V1}/consult/writes/{{write_id}}/confirm"): A,
    ("POST", f"{_V1}/consult/writes/{{write_id}}/revert"): A,
    # -- own rows -------------------------------------------------------
    ("POST", f"{_V1}/read-events"): R,
    ("GET", f"{_V1}/read-events/rollup"): A,  # every reader's receipts
    ("GET", f"{_V3}/me/seen"): R,  # the caller's own cursor (S-1)
    ("PUT", f"{_V3}/me/seen"): R,
    ("GET", f"{_V3}/watchlist"): R,
    ("POST", f"{_V3}/watchlist"): R,
    ("PUT", f"{_V3}/watchlist/{{watch_id}}"): R,
    ("DELETE", f"{_V3}/watchlist/{{watch_id}}"): R,
    ("POST", f"{_V3}/export"): R,
    # -- contributor: attributed judgements, gated knowledge edits ------
    ("GET", f"{_V1}/eval/labels"): C,
    ("POST", f"{_V1}/eval/labels"): C,
    ("GET", f"{_V3}/eval/goldset/worksheet"): C,
    ("POST", f"{_V3}/eval/goldset/label"): C,
    ("GET", f"{_V1}/journal_proposals"): C,
    ("POST", f"{_V1}/journal_proposals/{{proposal_id}}/accept"): C,
    ("POST", f"{_V1}/journal_proposals/{{proposal_id}}/reject"): C,
    # alert_ack_api.py (S-2): acknowledge / snooze an alert episode — shared
    # state (it clears "needs you" for everyone), so not a reader's write.
    # Below admin only the world topic's kinds (404 otherwise).
    ("POST", f"{_V3}/system/alerts/{{episode_id}}/ack"): C,
    ("DELETE", f"{_V3}/system/alerts/{{episode_id}}/ack"): C,
    ("POST", f"{_V3}/system/alerts/{{episode_id}}/snooze"): C,
    ("DELETE", f"{_V3}/system/alerts/{{episode_id}}/snooze"): C,
    # forecast_resolution_api.py (F-1): a contributor decides a question's
    # outcome unless they entered a number on it (checked in the handler);
    # correcting a decided outcome is an admin's. The record of one
    # question (U-11) is read by whoever decides it: contributor.
    ("GET", f"{_V3}/ledger/resolution-records"): C,
    ("GET", f"{_V3}/ledger/resolution-queue"): C,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/confirm"): C,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/reject"): C,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/judgement"): C,
    ("POST", f"{_V3}/ledger/resolutions/{{record_id}}/correct"): A,
    # -- reader surfaces ------------------------------------------------
    ("GET", f"{_V3}/system/alerts/{{episode_id}}/attention"): R,  # S-2 state
    ("GET", f"{_V3}/absence"): R,
    ("GET", f"{_V3}/belief"): R,
    ("GET", f"{_V3}/contentions"): R,
    ("GET", f"{_V3}/counts"): R,
    ("GET", f"{_V3}/eval/band_trajectory"): R,  # the "since" tile
    ("GET", f"{_V3}/events"): R,
    ("GET", f"{_V3}/events/{{event_id}}"): R,
    ("GET", f"{_V3}/events/{{event_id}}/lifecycle"): R,
    ("GET", f"{_V3}/graph/arcs"): R,
    ("GET", f"{_V3}/layers/divergence"): R,
    ("GET", f"{_V3}/ledger/forecasts"): R,
    ("GET", f"{_V3}/ledger/scorecard"): R,
    # eval_runs_api.py (E-3) — the owner's published evaluation runs
    # (aggregates only) on the reader Ledger; publishing is a host script.
    ("GET", f"{_V3}/eval/runs"): R,
    ("GET", f"{_V3}/ledger/seal-proof"): R,  # F-1: a seal's anchor inclusion proof
    ("GET", f"{_V3}/map/at_sea"): R,
    ("GET", f"{_V3}/map/series"): R,
    ("GET", f"{_V3}/map/subject_reads"): R,
    ("GET", f"{_V3}/narratives"): R,
    ("GET", f"{_V3}/narratives/echo"): R,
    ("GET", f"{_V3}/narratives/{{contention_id}}"): R,
    ("GET", f"{_V3}/since"): R,  # include=needs_you: world kinds below admin
    ("GET", f"{_V3}/since/counts"): R,  # the badge poll (S-1)
    ("GET", f"{_V3}/situations"): R,
    ("GET", f"{_V3}/situations/{{situation_id}}/trajectory"): R,
    ("GET", f"{_V3}/source-quality"): R,
    ("GET", f"{_V3}/sources/{{source_id}}/assurance"): R,
    ("GET", f"{_V3}/sources/{{source_id}}/quality"): R,
    ("GET", f"{_V3}/timeline"): R,
    ("GET", f"{_V1}/contention"): R,
    ("GET", f"{_V1}/entities"): R,
    ("GET", f"{_V1}/entities/graph"): R,
    ("GET", f"{_V1}/entities/{{entity_id}}"): R,
    ("GET", f"{_V1}/findings"): R,
    ("GET", f"{_V1}/findings/{{finding_id}}"): R,
    ("GET", f"{_V1}/graph/edge/{{edge_id}}"): R,
    ("GET", f"{_V1}/graph/ego"): R,
    ("GET", f"{_V1}/graph/structure"): R,
    ("GET", f"{_V1}/journal"): R,
    ("GET", f"{_V1}/journal/{{entry_id}}"): R,
    ("GET", f"{_V1}/lineage/{{row_kind}}/{{row_id}}"): R,
    # record_sheet_api.py (U-10) — the Record sheet's reads; the stored text
    # is withheld from a reader by the display policy (twice), never by role.
    ("GET", f"{_V3}/record/integrity"): R,
    ("GET", f"{_V3}/record/receipts"): R,
    ("GET", f"{_V3}/record/version/{{version_id}}/text"): R,
    ("GET", f"{_V3}/record/signal/{{signal_id}}/item"): R,
    # DM-A: what a finding's sentences rest on (ids, labels, hashes; a quote
    # cut to one display span) — no publisher text beyond that span.
    ("GET", f"{_V3}/record/finding/{{finding_id}}/support"): R,
    # DM-C: the held family's reads (nothing feeds them but the desk shadow,
    # behind a switch that is off; no console surface reads them yet).
    ("GET", f"{_V3}/record/assertions"): R,
    ("GET", f"{_V3}/record/assertions/{{assertion_id}}"): R,
    ("GET", f"{_V1}/signals"): R,
    ("GET", f"{_V1}/situations"): R,
    ("GET", f"{_V1}/source_credibility"): R,
    ("GET", f"{_V1}/source_credibility/{{host}}"): R,
    # -- RA-1: the reader surfaces' reads of engine figures ---------------
    # The first signed-in walk as a reader (roll 89) found the reader
    # surfaces (the coverage footer, the Wall, the subject page, the Ledger,
    # Watch) calling these as admin. Each answer is aggregate figures or ids
    # with states and times — no secret, credential, prompt, private note or
    # person — so the read itself is opened; every change stays where it was.
    ("GET", f"{_V3}/system/source-firing"): R,  # per-source counts, states, times
    ("GET", f"{_V3}/system/analyst-cadence"): R,  # per-analyst run counts, status
    ("GET", f"{_V3}/system/judge-stats"): R,  # verdict counts and means by provider
    ("GET", f"{_V3}/eval/grader_roster"): R,  # per-desk graded shares
    ("GET", f"{_V3}/eval/country_scorecard"): R,  # the banded card per subject
    ("GET", f"{_V3}/system/receipt-anchors"): R,  # daily public timestamp roots
    ("GET", f"{_V3}/evidence/drift"): R,  # edit counts (summary)
    ("GET", f"{_V3}/evidence/drift/editors"): R,  # per-source edit record (counts)
    ("GET", f"{_V3}/system/staleness-debt"): R,  # open review-flag counts
    ("GET", f"{_V3}/eval/scorecard"): R,  # critic score rollups per analyst
    ("GET", f"{_V3}/eval/calibration"): R,  # calibration aggregates
    ("GET", f"{_V3}/eval/correctness"): R,  # per-unit correctness shares
    ("GET", f"{_V1}/eval/scores"): R,  # per-unit eval scoreboard
    # per-unit correctness shares; with ?claims=1 a non-admin's claim ledger
    # carries no decisive reference spans (unit_correctness_api)
    ("GET", f"{_V1}/units/{{target_id}}/correctness"): R,
    # RA-1: the narrow reader reads of two admin lists (reader_reads_api) —
    # the roster (no body beyond the summary, no owner) and the Watch's
    # alert log (the world's kinds; no who, no note, no sinks, no rule path).
    # The admin lists (/registry/descriptors, /registry/sources,
    # /v3/system/alerts/log) keep their role and their whole answer.
    ("GET", f"{_V3}/roster"): R,
    ("GET", f"{_V3}/watch/alerts"): R,
    # -- admin: curation --------------------------------------------------
    ("PUT", f"{_V1}/source_credibility/{{host}}"): A,
    ("DELETE", f"{_V1}/source_credibility/{{host}}"): A,
    ("POST", f"{_V1}/source_credibility/bulk"): A,
    ("GET", f"{_V3}/collection-requirements"): A,
    ("GET", f"{_V3}/collection-requirements/{{requirement_id}}"): A,
    ("PATCH", f"{_V3}/collection-requirements/{{requirement_id}}"): A,
    # origins_api.py (data-model lane 9): reading which group a source
    # counts in is a reader's (like the credibility ledger); moving one is
    # the admin's.
    ("GET", f"{_REG}/origins"): R,
    ("GET", f"{_REG}/origins/{{origin_id}}/log"): R,
    ("PUT", f"{_REG}/origins/{{origin_id}}/group"): A,
    ("POST", f"{_REG}/origins/derive"): A,
    ("GET", f"{_V3}/retention-policies"): A,
    ("GET", f"{_V3}/retention-policies/{{policy_name}}"): A,
    ("PATCH", f"{_V3}/retention-policies/{{policy_name}}"): A,
    ("GET", f"{_V3}/optimizer/candidates"): A,
    ("GET", f"{_V3}/optimizer/candidates/{{candidate_id}}/diff"): A,
    ("POST", f"{_V3}/optimizer/candidates/{{candidate_id}}/review"): A,
    # -- admin: the engine's own surfaces ("operator side") ---------------
    ("GET", f"{_V3}/system/alerts/log"): A,
    ("GET", f"{_V3}/system/alerts/volume"): A,
    ("GET", f"{_V3}/system/backup-status"): A,
    ("GET", f"{_V3}/system/escalations"): A,
    ("GET", f"{_V3}/system/external-audit"): A,
    ("GET", f"{_V3}/system/judge-stats/health"): A,
    ("GET", f"{_V3}/system/production-gauge"): A,
    ("GET", f"{_V3}/system/search-health"): A,
    ("GET", f"{_V3}/runtime/actors"): A,
    ("GET", f"{_V3}/streams/consumer_lag"): A,
    ("GET", f"{_V3}/eval/analyst_runtime"): A,
    ("GET", f"{_V3}/eval/desk_baselines"): A,
    ("GET", f"{_V3}/eval/subject_scorecard"): A,
    ("GET", f"{_V3}/evidence/drift/assessments"): A,
    ("GET", f"{_V3}/evidence/drift/flags"): A,
    ("GET", f"{_V3}/evidence/drift/history"): A,
    ("GET", f"{_V3}/flow/graph"): A,
    ("GET", f"{_V3}/flow/node/{{node_id}}"): A,
    ("GET", f"{_V3}/flow/run/{{run_id}}"): A,
    ("GET", f"{_V3}/flow/run/{{run_id}}/body"): A,
    ("GET", f"{_V3}/flow/version/{{node_id}}"): A,
    ("GET", f"{_V3}/graph-triggers"): A,
    ("GET", f"{_V3}/collections/coverage"): A,
    ("GET", f"{_V3}/ingestion/intake"): A,
    ("GET", f"{_V1}/budget/demotions"): A,
    ("GET", f"{_V1}/budget/envelope"): A,
    ("GET", f"{_V1}/budget/ledger"): A,
    ("GET", f"{_V1}/analysts/runtime"): A,
    ("GET", f"{_V1}/analysts/{{analyst_id}}/critiques"): A,
    ("GET", f"{_V1}/analysts/{{analyst_id}}/outputs"): A,
    ("GET", f"{_V1}/analysts/{{analyst_id}}/runs"): A,
    ("GET", f"{_V1}/targets/{{target_id}}/runtime"): A,
}

#: Methods a route may be declared under.
DECLARABLE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "WEBSOCKET"})


def _validate() -> None:
    for (method, path), role in ROUTE_MIN_ROLES.items():
        if role not in ROLE_RANK:
            raise ValueError(f"ROUTE_MIN_ROLES {method} {path}: unknown role {role!r}")
        if method not in DECLARABLE_METHODS:
            raise ValueError(f"ROUTE_MIN_ROLES {method} {path}: bad method")
        if not path.startswith("/"):
            raise ValueError(f"ROUTE_MIN_ROLES {method} {path}: path must be absolute")


_validate()


def declared_key(method: str, path_template: str) -> tuple[str, str]:
    """The table key a mounted ``(method, template)`` is declared under
    (``HEAD`` reads ``GET``)."""
    m = method.upper()
    return ("GET" if m == "HEAD" else m, path_template)


def undeclared_routes(mounted: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Mounted routes with no entry (``OPTIONS`` is the framework's)."""
    return sorted(
        k for k in {declared_key(m, p) for m, p in mounted if m.upper() != "OPTIONS"}
        if k not in ROUTE_MIN_ROLES
    )


def stale_entries(mounted: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Entries the app no longer mounts."""
    keys = {declared_key(m, p) for m, p in mounted}
    return sorted(k for k in ROUTE_MIN_ROLES if k not in keys)


def role_counts() -> dict[str, int]:
    return {r: sum(1 for v in ROUTE_MIN_ROLES.values() if v == r) for r in ROLES}


__all__ = [
    "DECLARABLE_METHODS",
    "ROUTE_MIN_ROLES",
    "ROLE_DESCRIPTIONS",
    "declared_key",
    "role_counts",
    "signin",
    "stale_entries",
    "undeclared_routes",
]
