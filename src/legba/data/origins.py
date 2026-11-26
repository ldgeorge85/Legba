# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Origins and origin groups — where content comes from.

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
    a ``telegram_channel`` descriptor listing several channels: the SOURCE
    origin is one group for the whole bundle (``telegram_bundle:<descriptor
    id>``), used only for an item that names no channel. Items resolve finer,
    see ``telegram_item_channel``.
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
``telegram_item_channel``
    an ITEM of a ``telegram_channel`` source: the channel that posted it is
    the voice (``telegram:<channel>``, origin kind ``telegram_channel``),
    however many channels the descriptor carries.
``aggregator_item_domain``
    an ITEM of an aggregator source (:data:`AGGREGATOR_KINDS`, an address on
    :data:`AGGREGATOR_HOSTS`, or a source the owner map lists): the owner is the
    publisher of the underlying article, the registrable domain of its URL
    (``host:<domain>``, origin kind ``publisher_domain``), never the aggregator.
``owner_map``
    the deployment's owner map (``legba.data.owner_map``) joined the derived
    group to another owner: the same publisher on two domains, a proxied and a
    direct feed, a channel run by an outlet.
``admin_correction``
    an admin moved the origin (:func:`correct_origin`); a corrected row is
    never re-derived.

An item's owner is the finest level that is a real voice: the channel for
Telegram, the article's publisher domain for an aggregator, otherwise the
source's own group (:func:`derive_item_origin`, :func:`origin_groups_for_items`).

The registrable domain uses a SHORT built-in list of two-label public suffixes
(:data:`TWO_LABEL_SUFFIXES`), the generic-label-under-a-country-code shape
(``com.xx``, ``co.xx``, ``gov.xx``), plus the institutional umbrella domains
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

from .owner_map import EMPTY_OWNER_MAP, OwnerMap, channel_slug
from .principals import validate_handle

__all__ = [
    "AGGREGATOR_HOSTS",
    "AGGREGATOR_KINDS",
    "DEPARTMENT_PATHS",
    "DERIVATION_PRINCIPAL",
    "Derivation",
    "OriginGroupRead",
    "RULES",
    "PROXY_HOSTS",
    "TWO_LABEL_SUFFIXES",
    "UMBRELLA_DOMAINS",
    "correct_origin",
    "derive_item_origin",
    "derive_knowledge_set",
    "derive_ledger_host",
    "derive_source",
    "derived_assignments",
    "is_aggregator",
    "origin_groups_for_items",
    "origin_groups_for_sources",
    "publisher_group",
    "registrable_domain",
    "sync_derived",
]

#: The principal every derived assignment names.
DERIVATION_PRINCIPAL = "rule:origin_derivation"

RULES: tuple[str, ...] = (
    "claim_declared_claimant", "telegram_channel", "telegram_bundle", "proxied_publisher",
    "publisher_host",
    "licence_ledger_host", "knowledge_set", "descriptor_self", "admin_correction",
    "telegram_item_channel", "aggregator_item_domain", "owner_map",
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

#: A generic second-level label under a two-letter country code
#: (``com.bz``, ``co.bw``, ``gov.fj``): a public suffix this list does not name,
#: so a host under it keeps three labels. Without it every ``*.com.bz`` site
#: would share one owner, ``com.bz``.
_GENERIC_CC_SLD = re.compile(
    r"^(?:com|co|org|net|gov|gob|go|govt|gouv|mil|ac|edu|or|ne|nic|sch|gv)\.[a-z]{2}$")

_SLUG = re.compile(r"[^a-z0-9._@-]+")


def _slug(text: str) -> str:
    return _SLUG.sub("-", (text or "").strip().lower()).strip("-")[:200] or "unnamed"


#: Domains under which many INSTITUTIONS publish, each on its own subdomain
#: (``ecb.europa.eu``, ``ecdc.europa.eu``, ``cert.ssi.gouv.fr``): treated like a
#: public suffix, so each institution is its own group. A department that
#: publishes on a PATH of the umbrella host (``www.gov.uk/government/
#: organisations/<dept>``) is not separable by host and stays in one group.
UMBRELLA_DOMAINS: frozenset[str] = frozenset({"europa.eu", "gouv.fr", "gov.uk"})

#: Umbrella hosts whose departments publish under a path: the first group of
#: the pattern is the department, so two feeds of one department are one group
#: and two departments are two (``/government/organisations/<department>``).
DEPARTMENT_PATHS: Mapping[str, "re.Pattern[str]"] = {
    "gov.uk": re.compile(r"^/government/organisations/([a-z0-9-]+)"),
}

#: Source kinds whose items are articles of OTHER publishers.
AGGREGATOR_KINDS: frozenset[str] = frozenset({"gdelt_files", "gdelt_query"})

#: Registrable domains of aggregator APIs: a source whose address is on one is
#: an aggregator whatever its kind.
AGGREGATOR_HOSTS: frozenset[str] = frozenset({"gdeltproject.org"})

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
    if tail2 in TWO_LABEL_SUFFIXES or tail2 in UMBRELLA_DOMAINS or _GENERIC_CC_SLD.match(tail2):
        return ".".join(labels[-3:])
    return tail2


def publisher_group(host: str | None, path: str = "") -> tuple[str, str, str] | None:
    """The publisher group of an address: ``(group_key, label, registrable
    domain)``, or ``None`` when the host has no registrable domain. A host under
    a :data:`DEPARTMENT_PATHS` umbrella adds the department the path names."""
    domain = registrable_domain(host)
    if not domain:
        return None
    pattern = DEPARTMENT_PATHS.get(domain)
    match = pattern.match(path or "") if pattern else None
    if match:
        return f"host:{domain}/{match.group(1)}", f"{domain}/{match.group(1)}", domain
    return f"host:{domain}", domain, domain


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


def _apply_owner_map(d: Derivation, owner_map: OwnerMap, *, source_id: str | None = None,
                     channel: str | None = None) -> Derivation:
    """The derivation after the owner map: a source entry, then a channel
    entry, then an entry for the derived group key. No entry, no change."""
    target = None
    if source_id is not None:
        target = owner_map.sources.get(source_id)
    if target is None and channel is not None:
        target = owner_map.channels.get(channel)
    if target is None:
        target = owner_map.groups.get(d.group_key)
    if target is None or target == d.group_key:
        return d
    return Derivation(d.origin_kind, d.ref, d.label, target,
                      owner_map.label_for(target, target.split(":", 1)[1]), "owner_map",
                      {**d.detail, "derived_group": d.group_key, "derived_rule": d.rule})


def derive_source(descriptor_id: str, kind: str | None, body: Mapping[str, Any] | None,
                  *, name: str | None = None, owner_map: OwnerMap = EMPTY_OWNER_MAP) -> Derivation:
    """The derived origin of one source descriptor head (pure), after the
    owner map."""
    return _apply_owner_map(_derive_source(descriptor_id, kind, body, name=name), owner_map,
                            source_id=descriptor_id)


def _derive_source(descriptor_id: str, kind: str | None, body: Mapping[str, Any] | None,
                   *, name: str | None = None) -> Derivation:
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
        pub = publisher_group(host, parsed.path)
        if pub:
            return _d(pub[0], pub[1], "publisher_host", host=host)
    return _d(f"descriptor:{descriptor_id}", label, "descriptor_self",
              why="no address the rule could read")


def derive_ledger_host(host: str, *, owner_map: OwnerMap = EMPTY_OWNER_MAP) -> Derivation:
    """The origin of a host on the licence ledger (``source_credibility``)."""
    domain = registrable_domain(host) or _slug(host)
    return _apply_owner_map(
        Derivation("ledger_host", host.strip().lower(), host.strip().lower(),
                   f"host:{domain}", domain, "licence_ledger_host", {"host": host}),
        owner_map)


def _address_host_path(config: Mapping[str, Any]) -> tuple[str | None, str]:
    address = _address(config)
    if not address:
        return None, ""
    parsed = urlparse(address if "//" in address else f"//{address}")
    return parsed.hostname, parsed.path


def is_aggregator(descriptor_id: str, kind: str | None, body: Mapping[str, Any] | None,
                  owner_map: OwnerMap = EMPTY_OWNER_MAP) -> bool:
    """A source whose items are articles of other publishers: an aggregator
    kind, an address on an aggregator host, or a source the owner map lists."""
    if kind in AGGREGATOR_KINDS or descriptor_id in owner_map.aggregators:
        return True
    config = (body or {}).get("config") if isinstance((body or {}).get("config"), Mapping) else {}
    host, _ = _address_host_path(config)
    return registrable_domain(host) in AGGREGATOR_HOSTS


_TG_URL = re.compile(r"^https?://t\.me/(?:s/)?([A-Za-z0-9_]+)(?:/|$)")


def _item_channel(payload: Mapping[str, Any] | None, canonical_url: str | None) -> str | None:
    """The Telegram channel an item came from: the stored channel username,
    else the channel in the post link, else the numeric channel id."""
    channel = (payload or {}).get("channel") if isinstance((payload or {}).get("channel"), Mapping) else {}
    name = channel_slug(channel.get("username"))
    if not name:
        m = _TG_URL.match(canonical_url or "")
        name = channel_slug(m.group(1)) if m else ""
    if not name and channel.get("id") not in (None, ""):
        name = f"id{channel_slug(channel.get('id'))}"
    return name or None


def _item_article(payload: Mapping[str, Any] | None, canonical_url: str | None) -> tuple[str | None, str]:
    """``(host, path)`` of the article behind an aggregator item: the URL when
    the item has one, else the domain the aggregator reports."""
    if canonical_url:
        parsed = urlparse(canonical_url if "//" in canonical_url else f"//{canonical_url}")
        if parsed.hostname:
            return parsed.hostname, parsed.path
    item = (payload or {}).get("item") if isinstance((payload or {}).get("item"), Mapping) else {}
    domain = str(item.get("domain") or "").strip()
    return (domain or None), ""


def derive_item_origin(descriptor_id: str, kind: str | None, body: Mapping[str, Any] | None, *,
                       canonical_url: str | None = None, payload: Mapping[str, Any] | None = None,
                       owner_map: OwnerMap = EMPTY_OWNER_MAP) -> Derivation | None:
    """The origin of ONE ITEM when it is finer than its source's, else ``None``
    (the item counts as its source's group). A Telegram item is its channel; an
    aggregator item is the publisher domain of its article."""
    if kind == "telegram_channel":
        channel = _item_channel(payload, canonical_url)
        if channel:
            d = Derivation("telegram_channel", channel, f"@{channel}", f"telegram:{channel}",
                           f"@{channel}", "telegram_item_channel", {"channel": channel})
            return _apply_owner_map(d, owner_map, channel=channel)
        return None
    if is_aggregator(descriptor_id, kind, body, owner_map):
        host, path = _item_article(payload, canonical_url)
        pub = publisher_group(host, path)
        if pub is None:
            return None
        d = Derivation("publisher_domain", pub[1], pub[1], pub[0], pub[1],
                       "aggregator_item_domain", {"host": host})
        return _apply_owner_map(d, owner_map)
    return None


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


# What the stored items of one source say about who published them, grouped so
# one row comes back per distinct voice. Read-only scans of one source's rows
# (the ``source_id`` leading index prefix serves them).
_TELEGRAM_ITEMS_SQL = """
SELECT payload #>> '{channel,username}' AS username,
       payload #>> '{channel,id}' AS channel_id,
       substring(canonical_url from '^https?://t\\.me/(?:s/)?([A-Za-z0-9_]+)/') AS url_channel,
       count(*) AS items
  FROM signals WHERE source_id = $1
 GROUP BY 1, 2, 3
"""
_ARTICLE_ITEMS_SQL = """
SELECT payload #>> '{item,domain}' AS domain,
       substring(canonical_url from '^https?://([^/?#:]+)') AS host,
       substring(canonical_url from '^https?://[^/?#]+/government/organisations/([a-z0-9-]+)') AS dept,
       count(*) AS items
  FROM signals WHERE source_id = $1
 GROUP BY 1, 2, 3
"""


async def derived_item_assignments(conn: Any, heads: Iterable[Mapping[str, Any]],
                                   owner_map: OwnerMap = EMPTY_OWNER_MAP,
                                   ) -> list[tuple[Derivation, int]]:
    """The item-level origins (a channel, an article publisher) the stored items
    of every source head show, each with how many items carry it (read-only).
    One origin seen under several sources is returned once."""
    seen: dict[str, tuple[Derivation, int]] = {}
    for h in heads:
        sid, kind, body = h["descriptor_id"], h["kind"], h["body"]
        if kind == "telegram_channel":
            rows = await conn.fetch(_TELEGRAM_ITEMS_SQL, sid)
            samples = [({"channel": {"username": r["username"], "id": r["channel_id"]}},
                        f"https://t.me/{r['url_channel']}/1" if r["url_channel"] else None,
                        r["items"]) for r in rows]
        elif is_aggregator(sid, kind, body, owner_map):
            rows = await conn.fetch(_ARTICLE_ITEMS_SQL, sid)
            samples = []
            for r in rows:
                url = (f"https://{r['host']}/government/organisations/{r['dept']}"
                       if r["host"] and r["dept"] else (f"https://{r['host']}/" if r["host"] else None))
                samples.append(({"item": {"domain": r["domain"]}}, url, r["items"]))
        else:
            continue
        for payload, url, items in samples:
            d = derive_item_origin(sid, kind, body, canonical_url=url, payload=payload,
                                   owner_map=owner_map)
            if d is None:
                continue
            prev = seen.get(d.origin_key)
            seen[d.origin_key] = (prev[0] if prev else d, (prev[1] if prev else 0) + int(items))
    return list(seen.values())


async def _source_heads(conn: Any) -> list[dict[str, Any]]:
    heads = []
    for r in await conn.fetch(_SOURCE_HEADS_SQL):
        body = r["body"]
        if isinstance(body, (str, bytes)):
            body = json.loads(body)
        heads.append({"descriptor_id": r["descriptor_id"], "kind": r["kind"], "name": r["name"],
                      "body": body})
    return heads


async def derived_assignments(conn: Any, *, items: bool = False,
                              owner_map: OwnerMap = EMPTY_OWNER_MAP) -> list[Derivation]:
    """Every origin derivable from what the database holds now (read-only).
    ``items`` adds the item-level origins (per Telegram channel, per aggregator
    article publisher), which need a scan of the stored items."""
    out: list[Derivation] = []
    heads = await _source_heads(conn)
    for h in heads:
        out.append(derive_source(h["descriptor_id"], h["kind"], h["body"], name=h["name"],
                                 owner_map=owner_map))
    for r in await conn.fetch(_LEDGER_HOSTS_SQL):
        if r["source_host"]:
            out.append(derive_ledger_host(str(r["source_host"]), owner_map=owner_map))
    for r in await conn.fetch(_KNOWLEDGE_SETS_SQL):
        out.append(derive_knowledge_set(str(r["kind"])))
    if items:
        out.extend(d for d, _ in await derived_item_assignments(conn, heads, owner_map))
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


_HEADS_BY_ID_SQL = """
SELECT descriptor_id, kind, name, body FROM source_descriptors
 WHERE is_head AND descriptor_id = ANY($1::text[])
"""
_GROUPS_BY_ORIGIN_KEY_SQL = """
SELECT o.key AS origin_key, g.id::text AS group_id, g.key
  FROM origins o JOIN origin_groups g ON g.id = o.origin_group_id
 WHERE o.key = ANY($1::text[])
"""


async def origin_groups_for_items(conn: Any, items: Iterable[Mapping[str, Any]],
                                  owner_map: OwnerMap = EMPTY_OWNER_MAP) -> list[OriginGroupRead]:
    """THE READ for counting independent voices among ITEMS: each item's group
    at the finest level that is a real voice, in input order. An item is a
    mapping with ``source_id``, ``canonical_url`` and ``payload``. The stored
    origin row wins (so an admin correction counts); a voice with no row yet
    reads as the group the rules derive now, ``stored`` False; an item whose
    source is unknown reads as its own source group."""
    rows = [dict(i) for i in items]
    ids = sorted({str(r.get("source_id") or "") for r in rows} - {""})
    heads = {r["descriptor_id"]: r for r in await conn.fetch(_HEADS_BY_ID_SQL, ids)} if ids else {}
    plan: list[tuple[str, Derivation | None]] = []
    keys: set[str] = set()
    for r in rows:
        sid = str(r.get("source_id") or "")
        h = heads.get(sid)
        payload = r.get("payload")
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        d = None
        if h is not None:
            body = h["body"]
            if isinstance(body, (str, bytes)):
                body = json.loads(body)
            d = derive_item_origin(sid, h["kind"], body, canonical_url=r.get("canonical_url"),
                                   payload=payload, owner_map=owner_map)
        plan.append((sid, d))
        keys.add(d.origin_key if d else f"source:{sid}")
    stored = {r["origin_key"]: r for r in await conn.fetch(_GROUPS_BY_ORIGIN_KEY_SQL, sorted(keys))}
    out: list[OriginGroupRead] = []
    for sid, d in plan:
        key = d.origin_key if d else f"source:{sid}"
        row = stored.get(key)
        if row is not None:
            out.append(OriginGroupRead(row["key"], row["group_id"], True))
        elif d is not None:
            out.append(OriginGroupRead(d.group_key, None, False))
        else:
            out.append(OriginGroupRead(f"source:{sid}", None, False))
    return out
