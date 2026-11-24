# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Origins and origin groups — where content comes from (data-model lane 9).

An ORIGIN is one place material comes from: a source descriptor
(``source:<descriptor id>``), a knowledge set (``knowledge_set:<set>``), or a
host on the licence ledger reached outside any descriptor
(``ledger_host:<host>``). Every origin belongs to one ORIGIN GROUP, the unit
independence is meant to count: two feeds of one publisher, or two channels of
one claimant, are ONE group, so they can never be counted as two confirmations.

THE FIRST ASSIGNMENT IS DERIVED, rule by rule, and the rule is recorded on the
row (``origins.rule`` + ``rule_detail``) so every grouping can be argued with:

``claim_declared_claimant``
    ``scope.source_class == 'claim'``: the group is the declared claimant
    (``claimant:<name>``) — every source carrying one party's own claims is
    one voice, whatever host serves it.
``telegram_channel``
    a ``telegram_channel`` descriptor with exactly one channel: the group is
    that channel (``telegram:<channel>``).
``telegram_bundle``
    a ``telegram_channel`` descriptor listing several channels: ONE group for
    the whole bundle (``telegram_bundle:<descriptor id>``). Known wrong: the
    channels are different publishers, so this UNDER-counts independence among
    them until an admin splits the bundle into per-channel descriptors.
``proxied_publisher``
    the address is an in-stack proxy (:data:`PROXY_HOSTS`, the RSSHub feed
    generator): the group is the publisher the proxy route names
    (``publisher:<first path segment>``). Known wrong: the same publisher
    fetched directly lands in its ``host:`` group, so a proxied and a direct
    feed of one publisher are two groups until an admin joins them.
``publisher_host``
    the descriptor's configured address (``config.url`` / ``url_template`` /
    ``base_url``): the group is the registrable domain of its host
    (``host:<domain>``), so ``feeds.example.org`` and ``www.example.org`` are
    one publisher.
``licence_ledger_host``
    a ``source_credibility`` row (the per-host licence ledger): the same
    ``host:<domain>`` group, so a page fetched outside any descriptor counts
    with the publisher's feeds, not beside them.
``knowledge_set``
    a knowledge set (today: a ``seed_batches.kind``): its own group
    (``set:<kind>``) — nothing in the set's metadata names its publisher.
``descriptor_self``
    nothing above applies (no address, an unparseable one): the descriptor is
    its own group (``descriptor:<id>``), which is exactly what independence
    counting assumes today.
``admin_correction``
    an admin moved the origin (:func:`correct_origin`); a corrected row is
    never re-derived.

The registrable domain uses a SHORT built-in list of two-label public suffixes
(:data:`TWO_LABEL_SUFFIXES`) plus the institutional umbrella domains
(:data:`UMBRELLA_DOMAINS`); a host under any other multi-label suffix is
grouped one label too wide or too narrow, one publisher on two registrable
domains (``.com`` and ``.net``) is two groups, and departments that publish
on paths of one host are one group. Those, the telegram bundles and the
proxied publishers are the known wrong cases
(``scripts/report_origin_groups.py --list`` names them).

INDEPENDENCE IS NOT REWIRED HERE. :func:`origin_groups_for_sources` is the
read a counter would use; nothing calls it on a live path in this lane.

Pure derivation is stdlib only; the store functions take an asyncpg
connection. The admin route is ``registry/origins_api.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from .principals import validate_handle

__all__ = [
    "DERIVATION_PRINCIPAL",
    "Derivation",
    "OriginGroupRead",
    "RULES",
    "PROXY_HOSTS",
    "TWO_LABEL_SUFFIXES",
    "UMBRELLA_DOMAINS",
    "correct_origin",
    "derive_knowledge_set",
    "derive_ledger_host",
    "derive_source",
    "derived_assignments",
    "origin_groups_for_sources",
    "registrable_domain",
    "sync_derived",
]

#: The principal every derived assignment names.
DERIVATION_PRINCIPAL = "rule:origin_derivation"

RULES: tuple[str, ...] = (
    "claim_declared_claimant", "telegram_channel", "telegram_bundle", "proxied_publisher",
    "publisher_host",
    "licence_ledger_host", "knowledge_set", "descriptor_self", "admin_correction",
)

#: Two-label public suffixes the registrable-domain cut knows (a host under
#: one keeps three labels). Deliberately short and in code: the full public
#: suffix list is a dependency this lane does not add.
TWO_LABEL_SUFFIXES: frozenset[str] = frozenset({
    "co.uk", "org.uk", "gov.uk", "ac.uk", "ltd.uk", "plc.uk", "me.uk",
    "com.au", "net.au", "org.au", "gov.au", "edu.au",
    "co.nz", "org.nz", "govt.nz", "co.jp", "go.jp", "or.jp", "ne.jp", "ac.jp",
    "co.kr", "go.kr", "or.kr", "com.cn", "gov.cn", "org.cn", "com.hk", "gov.hk",
    "com.tw", "gov.tw", "com.sg", "gov.sg", "com.my", "gov.my", "co.in", "gov.in",
    "nic.in", "org.in", "co.id", "go.id", "or.id", "com.pk", "gov.pk", "com.bd",
    "gov.bd", "co.il", "gov.il", "org.il", "co.za", "gov.za", "org.za", "com.ng",
    "gov.ng", "co.ke", "go.ke", "com.eg", "gov.eg", "com.sa", "gov.sa", "gov.ae",
    "com.tr", "gov.tr", "org.tr", "com.br", "gov.br", "org.br", "com.ar", "gob.ar",
    "com.mx", "gob.mx", "com.co", "gov.co", "gob.pe", "com.pe", "gob.cl", "gob.ve",
    "com.ua", "gov.ua", "com.ru", "gov.ru", "com.pl", "gov.pl", "gc.ca", "gov.ph",
    "com.ph", "com.vn", "gov.vn", "com.qa", "gov.qa", "com.kw", "gov.kw", "com.lb",
    "gov.lb", "com.jo", "gov.jo", "gov.iq", "gov.ir", "ac.ir", "gov.af", "gov.ye",
})

_SLUG = re.compile(r"[^a-z0-9._@-]+")


def _slug(text: str) -> str:
    return _SLUG.sub("-", (text or "").strip().lower()).strip("-")[:200] or "unnamed"


#: Domains under which many INSTITUTIONS publish, each on its own subdomain
#: (``ecb.europa.eu``, ``ecdc.europa.eu``, ``cert.ssi.gouv.fr``): treated like a
#: public suffix, so each institution is its own group. A department that
#: publishes on a PATH of the umbrella host (``www.gov.uk/government/
#: organisations/<dept>``) is not separable by host and stays in one group.
UMBRELLA_DOMAINS: frozenset[str] = frozenset({"europa.eu", "gouv.fr", "gov.uk"})

#: Hosts that PROXY other publishers (an in-stack feed generator): the
#: publisher is the first path segment (``http://rsshub:1200/apnews/...``).
PROXY_HOSTS: frozenset[str] = frozenset({"rsshub"})


def registrable_domain(host: str | None) -> str | None:
    """The publisher part of a host: ``feeds.example.org`` -> ``example.org``,
    ``www.bbc.co.uk`` -> ``bbc.co.uk``, ``www.ecb.europa.eu`` ->
    ``ecb.europa.eu``. ``None`` for an empty host or an IP literal."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or re.fullmatch(r"[0-9.:\[\]]+", h):
        return None
    labels = [lbl for lbl in h.split(".") if lbl]
    if labels and labels[0] == "www" and len(labels) > 2:
        labels = labels[1:]
    if len(labels) <= 2:
        return ".".join(labels)
    tail2 = ".".join(labels[-2:])
    if tail2 in TWO_LABEL_SUFFIXES or tail2 in UMBRELLA_DOMAINS:
        return ".".join(labels[-3:])
    return tail2


@dataclass(frozen=True)
class Derivation:
    """One origin and the group a rule puts it in."""

    origin_kind: str          # source | knowledge_set | ledger_host
    ref: str                  # descriptor id | set key | host
    label: str
    group_key: str
    group_label: str
    rule: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def origin_key(self) -> str:
        return f"{self.origin_kind}:{self.ref}"


def _raw(value: Any) -> Any:
    """A descriptor config value is ``{"raw": …, "factory_kind": …}``."""
    return value.get("raw") if isinstance(value, Mapping) and "raw" in value else value


def _address(config: Mapping[str, Any]) -> str | None:
    for key in ("url", "url_template", "base_url", "endpoint", "feed_url"):
        raw = _raw(config.get(key))
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def derive_source(descriptor_id: str, kind: str | None, body: Mapping[str, Any] | None,
                  *, name: str | None = None) -> Derivation:
    """The derived origin of one source descriptor head (pure)."""
    body = body if isinstance(body, Mapping) else {}
    scope = body.get("scope") if isinstance(body.get("scope"), Mapping) else {}
    config = body.get("config") if isinstance(body.get("config"), Mapping) else {}
    label = str(name or (body.get("identity") or {}).get("name") or descriptor_id)

    def _d(group_key: str, group_label: str, rule: str, **detail: Any) -> Derivation:
        return Derivation("source", descriptor_id, label, group_key, group_label, rule, detail)

    claim = scope.get("claim") if isinstance(scope.get("claim"), Mapping) else None
    if scope.get("source_class") == "claim" and claim and str(claim.get("claimant") or "").strip():
        claimant = str(claim["claimant"]).strip()
        return _d(f"claimant:{_slug(claimant)}", claimant, "claim_declared_claimant",
                  claimant=claimant)
    if kind == "telegram_channel":
        channels = _raw(config.get("channels"))
        names = sorted({_slug(str(c).lstrip("@")) for c in channels or [] if str(c).strip()}) \
            if isinstance(channels, list) else []
        if len(names) == 1:
            return _d(f"telegram:{names[0]}", f"@{names[0]}", "telegram_channel", channel=names[0])
        if len(names) > 1:
            return _d(f"telegram_bundle:{descriptor_id}", label, "telegram_bundle",
                      channels=len(names))
    address = _address(config)
    if address:
        parsed = urlparse(address if "//" in address else f"//{address}")
        host = parsed.hostname
        segment = next((seg for seg in parsed.path.split("/") if seg), "")
        if host in PROXY_HOSTS and segment:
            return _d(f"publisher:{_slug(segment)}", segment, "proxied_publisher",
                      proxy=host, publisher=segment)
        domain = registrable_domain(host)
        if domain:
            return _d(f"host:{domain}", domain, "publisher_host", host=host)
    return _d(f"descriptor:{descriptor_id}", label, "descriptor_self",
              why="no address the rule could read")


def derive_ledger_host(host: str) -> Derivation:
    """The origin of a host on the licence ledger (``source_credibility``)."""
    domain = registrable_domain(host) or _slug(host)
    return Derivation("ledger_host", host.strip().lower(), host.strip().lower(),
                      f"host:{domain}", domain, "licence_ledger_host", {"host": host})


def derive_knowledge_set(set_key: str, *, label: str | None = None) -> Derivation:
    """The origin of a knowledge set: its own group."""
    key = _slug(set_key)
    return Derivation("knowledge_set", key, label or set_key, f"set:{key}", label or set_key,
                      "knowledge_set", {})


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

_SOURCE_HEADS_SQL = """
SELECT descriptor_id, kind, name, body FROM source_descriptors WHERE is_head
"""
_LEDGER_HOSTS_SQL = "SELECT source_host FROM source_credibility"
_KNOWLEDGE_SETS_SQL = "SELECT DISTINCT kind FROM seed_batches WHERE kind IS NOT NULL"

_UPSERT_GROUP_SQL = """
WITH ins AS (
    INSERT INTO origin_groups (key, label, written_by)
    VALUES ($1, $2, legba_principal($3))
    ON CONFLICT (key) DO NOTHING
    RETURNING id
)
SELECT id FROM ins UNION ALL SELECT id FROM origin_groups WHERE key = $1 LIMIT 1
"""
_ORIGIN_BY_KEY_SQL = "SELECT id, origin_group_id, corrected, rule FROM origins WHERE key = $1"
_INSERT_ORIGIN_SQL = """
INSERT INTO origins (key, origin_kind, ref, label, origin_group_id, rule, rule_detail, written_by)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, legba_principal($8))
RETURNING id
"""
_REDERIVE_ORIGIN_SQL = """
UPDATE origins SET origin_group_id = $2, rule = $3, rule_detail = $4::jsonb, label = $5,
       updated_at = now(), written_by = legba_principal($6)
 WHERE id = $1 AND NOT corrected
"""
_CORRECT_ORIGIN_SQL = """
UPDATE origins SET origin_group_id = $2, rule = 'admin_correction',
       rule_detail = jsonb_build_object('reason', $3::text, 'derived_rule', rule),
       corrected = true, updated_at = now(), written_by = legba_principal($4)
 WHERE id = $1
"""
_LOG_SQL = """
INSERT INTO origin_assignment_log (origin_id, from_group_id, to_group_id, rule, reason, written_by)
VALUES ($1, $2, $3, $4, $5, legba_principal($6))
"""


async def derived_assignments(conn: Any) -> list[Derivation]:
    """Every origin derivable from what the database holds now (read-only)."""
    out: list[Derivation] = []
    for r in await conn.fetch(_SOURCE_HEADS_SQL):
        body = r["body"]
        if isinstance(body, (str, bytes)):
            body = json.loads(body)
        out.append(derive_source(r["descriptor_id"], r["kind"], body, name=r["name"]))
    for r in await conn.fetch(_LEDGER_HOSTS_SQL):
        if r["source_host"]:
            out.append(derive_ledger_host(str(r["source_host"])))
    for r in await conn.fetch(_KNOWLEDGE_SETS_SQL):
        out.append(derive_knowledge_set(str(r["kind"])))
    return out


async def _group_id(conn: Any, key: str, label: str, written_by: str) -> Any:
    return await conn.fetchval(_UPSERT_GROUP_SQL, key, label[:300], written_by)


async def sync_derived(conn: Any, derivations: Iterable[Derivation] | None = None,
                       *, written_by: str = DERIVATION_PRINCIPAL) -> dict[str, int]:
    """Write the derived assignment: a new origin is inserted; a derived one
    whose rule now says another group moves; a CORRECTED one is never touched.
    Every insert and move is an ``origin_assignment_log`` row. Idempotent: a
    second run with nothing changed writes nothing. Run in one transaction."""
    validate_handle(written_by)
    derivations = list(derivations) if derivations is not None else await derived_assignments(conn)
    counts = {"inserted": 0, "moved": 0, "unchanged": 0, "corrected_kept": 0}
    async with conn.transaction():
        for d in derivations:
            gid = await _group_id(conn, d.group_key, d.group_label, written_by)
            row = await conn.fetchrow(_ORIGIN_BY_KEY_SQL, d.origin_key)
            detail = json.dumps(dict(d.detail), sort_keys=True)
            if row is None:
                oid = await conn.fetchval(
                    _INSERT_ORIGIN_SQL, d.origin_key, d.origin_kind, d.ref, d.label[:300],
                    gid, d.rule, detail, written_by,
                )
                await conn.execute(_LOG_SQL, oid, None, gid, d.rule, "derived", written_by)
                counts["inserted"] += 1
            elif row["corrected"]:
                counts["corrected_kept"] += 1
            elif row["origin_group_id"] != gid:
                await conn.execute(_REDERIVE_ORIGIN_SQL, row["id"], gid, d.rule, detail,
                                   d.label[:300], written_by)
                await conn.execute(_LOG_SQL, row["id"], row["origin_group_id"], gid, d.rule,
                                   "re-derived", written_by)
                counts["moved"] += 1
            else:
                counts["unchanged"] += 1
    return counts


class OriginCorrectionError(ValueError):
    """An unknown origin, or a malformed group key / reason."""


_GROUP_KEY = re.compile(r"^[a-z][a-z_]*:.{1,250}$")


async def correct_origin(conn: Any, origin_id: Any, *, group_key: str, group_label: str | None,
                         reason: str, written_by: str) -> dict[str, Any]:
    """An admin moves one origin to another group (created if new). The
    origin is marked corrected (never re-derived) and the move is logged with
    the admin's principal and reason."""
    validate_handle(written_by)
    if not _GROUP_KEY.match(group_key or ""):
        raise OriginCorrectionError("group_key is '<kind>:<name>' (e.g. host:example.org)")
    if not (reason or "").strip():
        raise OriginCorrectionError("a reason is required")
    async with conn.transaction():
        row = await conn.fetchrow("SELECT id, origin_group_id FROM origins WHERE id = $1::uuid FOR UPDATE",
                                  str(origin_id))
        if row is None:
            raise OriginCorrectionError(f"no origin {origin_id}")
        gid = await _group_id(conn, group_key, group_label or group_key, written_by)
        await conn.execute(_CORRECT_ORIGIN_SQL, row["id"], gid, reason.strip()[:1000], written_by)
        await conn.execute(_LOG_SQL, row["id"], row["origin_group_id"], gid, "admin_correction",
                           reason.strip()[:1000], written_by)
    return {"origin_id": str(row["id"]), "from_group_id": str(row["origin_group_id"]),
            "to_group_id": str(gid), "group_key": group_key}


@dataclass(frozen=True)
class OriginGroupRead:
    """The group a source counts as. ``stored`` False = no origin row yet:
    the source is its own group (``source:<id>``), the fallback independence
    counting already assumes (``statement_independence.group_key``)."""

    group_key: str
    group_id: str | None
    stored: bool


_GROUPS_FOR_SOURCES_SQL = """
SELECT o.ref, g.id::text AS group_id, g.key
  FROM origins o JOIN origin_groups g ON g.id = o.origin_group_id
 WHERE o.origin_kind = 'source' AND o.ref = ANY($1::text[])
"""


async def origin_groups_for_sources(conn: Any, source_ids: Iterable[str]) -> dict[str, OriginGroupRead]:
    """THE READ an independence counter would use: each source id's group.
    Read-only; nothing on a live path calls it in this lane."""
    ids = sorted({str(s) for s in source_ids if s})
    found = {r["ref"]: OriginGroupRead(r["key"], r["group_id"], True)
             for r in await conn.fetch(_GROUPS_FOR_SOURCES_SQL, ids)}
    return {s: found.get(s) or OriginGroupRead(f"source:{s}", None, False) for s in ids}
