# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-1 — what a CLAIM signal may support, as code.

A source declared ``scope.source_class: claim`` (``schemas/source.py``,
``schemas/source_claim.py``) publishes a party's OWN claim. Every signal it
writes carries a stamp in ``payload.claim`` (:func:`claim_stamp`, applied by
the document sink at ingest), and every place that turns a signal into
SUPPORT reads that stamp through this module:

* a claim row may ground ONLY a sentence of the form "<claimant>
  claimed/posted/listed <what>" that names the claimant
  (:func:`claim_sentence_attributed`);
* it never by itself grounds the claimed event as fact (the fact extractor
  is refused on a claim source at save; the fact write path skips claim rows,
  :func:`is_claim_signal`);
* it never raises a fact's confirmation count, never feeds credibility as
  corroboration, and never counts as an independent source
  (:func:`drop_claim_rows`, used by the counting paths).

A LEAF (stdlib only) so both the slim registry image and the runtime import
it. Nothing here calls a model: the claimant comes from a structured feed
field or the source's declaration, never from generated text.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence, TypeVar

from .claim_attribution import ATTRIBUTION_VERBS, sentence_attributed

__all__ = [
    "CHANNEL_CLAIMS_CONFIG_KEY",
    "CLAIM_TEXT_ACCESS",
    "CLAIM_TEXT_WITHHELD",
    "channel_claims",
    "claim_stamp_for",
    "claim_text_withheld",
    "unsigned_claim_channels",
    "claim_only_failure",
    "claim_only_lineage",
    "ordinal_claims",
    "CLAIM_SIGN_OFF_REQUIRED",
    "claim_activation_refusal",
    "claim_sign_off_missing",
    "CLAIM_ATTRIBUTION_VERBS",
    "CLAIM_PAYLOAD_KEY",
    "CLAIM_SOURCE_CLASS",
    "NOT_CLAIM_SIGNAL_SQL",
    "claim_judge_line",
    "claim_of",
    "claim_ordinals",
    "claim_row_label",
    "claim_sentence_attributed",
    "claim_stamp",
    "drop_claim_rows",
    "is_claim_signal",
    "resolve_claimant",
    "strip_claim_media",
    "claim_fields",
    "with_claim_stamp",
]

CLAIM_SOURCE_CLASS = "claim"
#: The payload key the ingest stamp writes and every reader checks.
CLAIM_PAYLOAD_KEY = "claim"
#: The SQL predicate "this signals row is NOT a claim" — every SQL counting
#: path uses it. ``{alias}`` = the signals table alias.
NOT_CLAIM_SIGNAL_SQL = "COALESCE({alias}.payload->'claim'->>'source_class', '') <> 'claim'"

#: Reporting verbs that keep a sentence ABOUT the claim rather than about the
#: claimed event (C-2: the clause-level rule and its full vocabulary live in
#: ``claim_attribution``; this name stays for importers).
CLAIM_ATTRIBUTION_VERBS: tuple[str, ...] = ATTRIBUTION_VERBS

#: C-2 — the Telegram descriptor's per-channel claim declarations live in
#: ``config.claims`` (``{<channel>: <ClaimDeclaration>}``), beside the
#: ``config.classes`` entry that marks the channel ``claim``.
CHANNEL_CLAIMS_CONFIG_KEY = "claims"

#: C-2 — who may read a claim source's STORED text (the raw fetch kept as the
#: receipt, ``fetch_versions``): the operator display class only — the admin
#: role and the platform's own services. Every other caller is told the text
#: is withheld (:func:`claim_text_withheld`).
CLAIM_TEXT_ACCESS = "operator"
CLAIM_TEXT_WITHHELD = "claim_text_admin_only"

#: Payload keys a claim row never keeps: media references and enclosures.
_MEDIA_PAYLOAD_KEYS: tuple[str, ...] = (
    "media", "media_type", "media_ref", "enclosures", "enclosure", "images", "image",
    "thumbnail", "screenshot", "screenshots", "attachments",
)


def _scope_get(scope: Any, key: str) -> Any:
    if isinstance(scope, Mapping):
        return scope.get(key)
    return getattr(scope, key, None)


def _decl_get(decl: Any, key: str) -> Any:
    if decl is None:
        return None
    if isinstance(decl, Mapping):
        return decl.get(key)
    return getattr(decl, key, None)


def _dig(payload: Mapping[str, Any], path: str) -> Any:
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


def resolve_claimant(declaration: Any, payload: Mapping[str, Any] | None) -> tuple[str, str]:
    """``(claimant, claimant_from)`` — the feed's structured field when the
    declaration names one and it resolves to a non-empty string
    (``feed_field``), else the declared claimant (``declared``)."""
    path = _decl_get(declaration, "claimant_path")
    if path and isinstance(payload, Mapping):
        val = _dig(payload, str(path))
        if isinstance(val, str) and val.strip():
            return val.strip()[:200], "feed_field"
    return str(_decl_get(declaration, "claimant") or "").strip(), "declared"


def claim_stamp(scope: Any, payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The ``payload.claim`` stamp for a signal written by a source with this
    ``scope`` — ``None`` for any non-claim source."""
    if str(_scope_get(scope, "source_class") or "") != CLAIM_SOURCE_CLASS:
        return None
    return _stamp_from(_scope_get(scope, "claim"), payload)


def _stamp_from(decl: Any, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    claimant, claimant_from = resolve_claimant(decl, payload)
    stamp: dict[str, Any] = {
        "source_class": CLAIM_SOURCE_CLASS,
        "claimant": claimant,
        "claimant_from": claimant_from,
        "verb": str(_decl_get(decl, "verb") or "claimed"),
    }
    # C-2: the declared aliases ride the stamp only when declared, so every
    # C-1 stamp keeps its four keys.
    aliases = [str(a) for a in (_decl_get(decl, "claimant_aliases") or []) if str(a).strip()]
    if aliases:
        stamp["aliases"] = aliases
    return stamp


def _unwrap(value: Any) -> Any:
    """A factory-wrapped descriptor config value (``{factory_kind, raw}``) ->
    its raw value; anything else unchanged."""
    if isinstance(value, Mapping) and "raw" in value and (
        set(value) - {"raw", "ui_hint"} <= {"factory_kind", "key_kind", "value_kind", "item_kind"}
    ):
        return value["raw"]
    return value


def _channel_key(handle: Any) -> str:
    """The Telegram channel normalization (``telegram._strip_channel_prefix``),
    case-folded: one key for "@Name", "telegram://Name" and "name"."""
    s = str(handle or "").strip()
    if s.startswith("telegram://"):
        s = s[len("telegram://"):]
    return s.lstrip("@").casefold()


def channel_claims(config: Any) -> dict[str, Any]:
    """C-2 — ``{channel key: claim declaration}`` for every channel a
    descriptor's ``config`` marks ``claim`` (``config.classes``) AND declares
    (``config.claims``). Empty for every descriptor without a ``claims`` key
    — one dict lookup, no other work."""
    if not isinstance(config, Mapping) or CHANNEL_CLAIMS_CONFIG_KEY not in config:
        return {}
    classes = _unwrap(config.get("classes")) or {}
    claims = _unwrap(config.get(CHANNEL_CLAIMS_CONFIG_KEY)) or {}
    if not isinstance(classes, Mapping) or not isinstance(claims, Mapping):
        return {}
    marked = {_channel_key(k) for k, v in classes.items() if str(_unwrap(v)) == CLAIM_SOURCE_CLASS}
    out: dict[str, Any] = {}
    for handle, decl in claims.items():
        key = _channel_key(handle)
        decl = _unwrap(decl)
        if key in marked and decl is not None:
            out[key] = decl
    return out


def _payload_channel(payload: Mapping[str, Any] | None) -> str:
    chan = payload.get("channel") if isinstance(payload, Mapping) else None
    return _channel_key(chan.get("username")) if isinstance(chan, Mapping) else ""


def claim_stamp_for(descriptor: Any, payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The stamp for a row of ``descriptor``: the source-level claim stamp,
    else (C-2) the per-channel one when the row's ``payload.channel.username``
    is a channel the descriptor marks ``claim``. ``None`` otherwise."""
    stamp = claim_stamp(getattr(descriptor, "scope", None), payload)
    if stamp is not None:
        return stamp
    chans = channel_claims(getattr(descriptor, "config", None))
    if not chans:
        return None
    key = _payload_channel(payload)
    decl = chans.get(key) if key else None
    if decl is None:
        return None
    stamp = _stamp_from(decl, payload)
    stamp["channel"] = key
    return stamp


def claim_text_withheld(display_class: str, *, is_claim: bool) -> str | None:
    """C-2 — the withheld reason for a claim source's stored text, or ``None``
    when ``display_class`` may read it (:data:`CLAIM_TEXT_ACCESS`). Text that
    is not a claim source's is not this rule's to withhold (``None``)."""
    if not is_claim or display_class == CLAIM_TEXT_ACCESS:
        return None
    return CLAIM_TEXT_WITHHELD


def strip_claim_media(signal: Any) -> None:
    """Text only: a claim row keeps no media reference (in place)."""
    for attr in ("media_ref", "object_ref", "media_ref_expires_at"):
        if getattr(signal, attr, None) is not None:
            setattr(signal, attr, None)
    payload = getattr(signal, "payload", None)
    if isinstance(payload, dict):
        for key in _MEDIA_PAYLOAD_KEYS:
            payload.pop(key, None)


def claim_of(row: Any) -> dict[str, Any] | None:
    """The claim stamp of a signal row / payload / Signal, or ``None``.

    Accepts a ``Signal`` (``.payload``), a row mapping with ``payload`` (dict
    or JSON text), a bare payload mapping, or a row that carries a
    ``source_class`` column resolved from the descriptor."""
    payload: Any = getattr(row, "payload", None)
    if payload is None and isinstance(row, Mapping):
        payload = row.get("payload", row)
        if isinstance(payload, str):
            import json  # noqa: PLC0415

            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
    if isinstance(payload, Mapping):
        stamp = payload.get(CLAIM_PAYLOAD_KEY)
        if isinstance(stamp, Mapping) and stamp.get("source_class") == CLAIM_SOURCE_CLASS:
            return dict(stamp)
    if isinstance(row, Mapping) and str(row.get("source_class") or "") == CLAIM_SOURCE_CLASS:
        return {"source_class": CLAIM_SOURCE_CLASS, "claimant": str(row.get("claimant") or ""),
                "claimant_from": "declared", "verb": "claimed"}
    return None


def is_claim_signal(row: Any) -> bool:
    return claim_of(row) is not None


def with_claim_stamp(target: dict[str, Any], source: Any) -> dict[str, Any]:
    """``target`` with ``claim`` set to ``source``'s claim stamp when it has
    one (a citation built from a claim row); every other target unchanged."""
    claim = claim_of(source) if isinstance(source, Mapping) else None
    if claim is not None:
        target["claim"] = claim
    return target


def claim_fields(payload: Any) -> dict[str, Any]:
    """The API's ``{evidence_class, claimant}`` for a signal payload:
    ``('claim', <claimant>)`` on a claim row, ``(None, None)`` otherwise."""
    claim = claim_of(payload) if isinstance(payload, Mapping) else None
    if claim is None:
        return {"evidence_class": None, "claimant": None}
    return {"evidence_class": CLAIM_SOURCE_CLASS,
            "claimant": str(claim.get("claimant") or "") or None}


_T = TypeVar("_T")


def drop_claim_rows(rows: Iterable[_T]) -> list[_T]:
    """The rows that may COUNT as support (corroboration, confirmation,
    independence, credibility): every claim row removed."""
    return [r for r in rows if not is_claim_signal(r)]


def claim_sentence_attributed(sentence: str, claim: Mapping[str, Any] | None) -> bool:
    """True when ``sentence`` is a sentence a claim row may ground: it
    presents the claim AS a claim, bound to the claimant, and states no
    event outside that frame (C-2's clause rule,
    :func:`legba.data.claim_attribution.sentence_attributed`). A claim stamp
    with no claimant grounds nothing."""
    return sentence_attributed(sentence, claim)


def claim_row_label(claim: Mapping[str, Any] | None) -> str:
    """The slice-row label an analyst sees on a claim row."""
    if not claim:
        return ""
    claimant = str(claim.get("claimant") or "unnamed claimant")
    verb = str(claim.get("verb") or "claimed")
    return (
        f"CLAIM by {claimant} — supports only \"{claimant} {verb} ...\" "
        "(attributed, dated); never the event itself"
    )


def claim_judge_line(claim: Mapping[str, Any]) -> str:
    """The verify judge's evidence-view prefix for a claim citation."""
    claimant = str(claim.get("claimant") or "unnamed claimant")
    verb = str(claim.get("verb") or "claimed")
    return (
        f"CLAIM ONLY: this entry is {claimant}'s own claim/listing, not a report "
        f"of the event. It supports only \"{claimant} {verb} ...\", never that "
        "the claimed event happened.\n"
    )


CLAIM_SIGN_OFF_REQUIRED = "claim_sign_off_required"


def unsigned_claim_channels(config: Any) -> list[str]:
    """C-2 — the claim channels of ``config`` with no recorded sign-off."""
    return sorted(k for k, d in channel_claims(config).items() if _decl_get(d, "sign_off") is None)


def claim_sign_off_missing(descriptor: Any) -> bool:
    """True when ``descriptor`` (a typed SourceDescriptor) is a CLAIM source
    with no recorded ``scope.claim.sign_off`` — or (C-2) carries a claim
    CHANNEL with no recorded sign-off of its own."""
    scope = getattr(descriptor, "scope", None)
    if unsigned_claim_channels(getattr(descriptor, "config", None)):
        return True
    if str(_scope_get(scope, "source_class") or "") != CLAIM_SOURCE_CLASS:
        return False
    return _decl_get(_scope_get(scope, "claim"), "sign_off") is None


def claim_activation_refusal(descriptor: Any, to_state: Any) -> dict[str, Any] | None:
    """The transition route's refusal body when a claim source without a
    recorded sign-off is asked to go ``active``; else ``None``."""
    state = getattr(to_state, "value", to_state)
    if str(state) != "active" or not claim_sign_off_missing(descriptor):
        return None
    ident = getattr(descriptor, "identity", None)
    channels = unsigned_claim_channels(getattr(descriptor, "config", None))
    if channels:
        return {
            "error": CLAIM_SIGN_OFF_REQUIRED,
            "message": (
                f"source {getattr(ident, 'id', '?')!r} carries CLAIM channel(s) {channels} "
                "with no recorded sign-off: record config.claims.<channel>.sign_off "
                "{by, at, note} for each before the descriptor can go active"
            ),
            "descriptor_id": getattr(ident, "id", None),
            "channels": channels,
        }
    return {
        "error": CLAIM_SIGN_OFF_REQUIRED,
        "message": (
            f"source {getattr(ident, 'id', '?')!r} is a CLAIM source (scope.source_class "
            "'claim') and has no recorded sign-off: record scope.claim.sign_off "
            "{by, at, note} on the descriptor before it can go active"
        ),
        "descriptor_id": getattr(ident, "id", None),
    }


# ---------------------------------------------------------------------------
# C-2 — one more hop: a finding that rested only on claims
# ---------------------------------------------------------------------------


def claim_only_lineage(citations: Iterable[Any] | None) -> dict[str, Any] | None:
    """The hop-2 stamp for a citation of a FINDING whose own cited signals are
    ALL claim rows (at least one), else ``None``.

    Computed at the citing write from the cited finding's stored citations
    (``data.data.citations``, each claim row's entry stamped by
    ``with_claim_stamp``) — never a recursive query. The stamp is an ordinary
    claim stamp (``source_class: claim``), so every reader of the stamp treats
    the citation as a claim: claimant = the first claimant, ``claimants`` =
    all of them (each may be named), ``hop: 2``."""
    claimants: list[str] = []
    aliases: list[str] = []
    verb = "claimed"
    signals = 0
    for entry in citations or ():
        if not isinstance(entry, Mapping) or not entry.get("signal_id"):
            continue
        signals += 1
        stamp = claim_of(entry)
        if stamp is None:
            return None
        name = str(stamp.get("claimant") or "").strip()
        if name and name not in claimants:
            claimants.append(name)
        aliases += [str(a) for a in (stamp.get("aliases") or []) if str(a) not in aliases]
        verb = str(stamp.get("verb") or verb)
    if not signals or not claimants:
        return None
    out: dict[str, Any] = {"source_class": CLAIM_SOURCE_CLASS, "claimant": claimants[0],
                           "claimant_from": "lineage", "verb": verb, "hop": 2}
    if len(claimants) > 1:
        out["claimants"] = claimants
    if aliases:
        out["aliases"] = aliases
    return out


def ordinal_claims(citations: Any) -> dict[int, dict[str, Any]]:
    """A composition's ``ordinal -> claim stamp`` for its claim-only citations."""
    out: dict[int, dict[str, Any]] = {}
    for entry in citations if isinstance(citations, (list, tuple)) else ():
        stamp = claim_of(entry) if isinstance(entry, Mapping) else None
        if stamp is not None and isinstance(entry.get("ordinal"), int):
            out[int(entry["ordinal"])] = stamp
    return out


def claim_only_failure(clause: str, markers: Sequence[int], claim_map: Mapping[int, Any]) -> bool:
    """True when every resolving marker of ``clause`` is a claim citation and
    the clause is not attributed to any of their claimants (``claim_as_fact``)."""
    return bool(markers) and bool(claim_map) and all(n in claim_map for n in markers) and not any(
        claim_sentence_attributed(clause, claim_map[n]) for n in markers
    )


def claim_ordinals(rows: Sequence[Any]) -> list[int]:
    """1-based slice ordinals of the claim rows in ``rows``."""
    return [i for i, r in enumerate(rows, start=1) if is_claim_signal(r)]
