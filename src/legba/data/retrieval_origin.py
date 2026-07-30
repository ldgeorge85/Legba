# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RETRIEVAL-ORIGIN axis — where evidence came from (migration 0112).

Three ORTHOGONAL properties travel with a piece of evidence and are routinely
confused. This module owns the third one, and exists partly so the first two
are not overloaded:

===================  ====================================  ==================
Property             Question it answers                   Owner
===================  ====================================  ==================
``source_class``     who published it, with what           ``schemas/source.py``
                     editorial authority                   → ``signal_salience.
                     (reporting/analysis/official/          AUTHORITY_RANK``
                     state_media)
``license_class``    what may we KEEP of it                ``schemas/source.py``
                     (12-value licence vocabulary)         → ``evidence_archiver``
``retrieval_origin`` how did it reach us                   **this module**
===================  ====================================  ==================

DO NOT express retrieval origin as a ``source_class`` value. A Reuters article
found via search is still ``reporting``; and an unrecognised ``source_class``
silently drops to authority rank 0, corrupting salience for every row carrying
it. That is a category error with a silent blast radius, which is exactly why
this is a separate field.

VOCABULARY
----------
``None`` / absent
    A CURATED registered source. The honest default — every row written before
    migration 0112 is exactly this, so there is no backfill and no retroactive
    claim about rows from before the concept existed.
:data:`CURATED_SOURCE`
    An explicit statement of the same thing.
``web_search:<component_id>``
    Retrieved through the named search provider. The component id rides along
    so a later audit can ask WHICH provider introduced WHICH claims — the same
    discipline as ``judge_llm_ref`` on the critique row.

WHY IT IS LOAD-BEARING
----------------------
Two gates read it, and both fail badly without it:

* **Calibration.** :data:`WEB_EVIDENCE_RESOLUTION` is the ``hypotheses.
  resolved_by`` label for "a web-retrieved fact resolved this hypothesis". It
  is placed in the WEAK tier by ``calibration_tracking``, never in headline
  exogenous. Web evidence is cheap and abundant and will dominate volume within
  weeks of search going live; untagged, the headline exogenous Brier silently
  degrades into "how well do we predict things that are easy to search" — with
  no test failing. The in-code comment there insists the project "must never
  quote a Brier earned on a lexical proxy as calibration"; this is the same
  claim about a different proxy.
* **Archive retention.** ``evidence_archiver``'s licence gate fails OPEN for an
  unknown licence, justified by a one-time audit of ~48 curated sources. Search
  makes the domain set unbounded and unaudited, so for web-origin rows the
  default inverts — see ``evidence_archiver.WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES``.

DO NOT "FIX" THE FAIL-SAFES. ``_is_exogenous`` returning False for an
unrecognised label, and this module returning "not web" only when it is sure,
are guardrails, not bugs.
"""

from __future__ import annotations

from typing import Any, Mapping

#: An explicit "this came from a registered, curated source".
CURATED_SOURCE = "curated_source"

#: Prefix for a web-retrieved origin; the suffix is the provider component id.
WEB_SEARCH_PREFIX = "web_search:"

#: The ``hypotheses.resolved_by`` label for a resolution grounded in
#: web-retrieved evidence. Placed in the WEAK tier by ``calibration_tracking``
#: — never headline exogenous — until a LABELLED GOLD SET shows web-sourced
#: resolutions are as reliable as operator labels. The gold-set loop already
#: exists and is "never pooled"; this is the same discipline.
WEB_EVIDENCE_RESOLUTION = "web_evidence"

#: The column both ``signals`` and ``evidence_archive`` carry (migration 0112),
#: and the payload key the ingest path stamps.
FIELD = "retrieval_origin"


def web_search_origin(component_id: str) -> str:
    """Build the origin value for a hit from ``component_id``.

    A blank component id yields the bare prefix rather than an empty string:
    "we know this came from search but not which provider" must still read as
    web-retrieved by :func:`is_web_retrieved`.
    """
    return f"{WEB_SEARCH_PREFIX}{str(component_id or '').strip()}"


def web_evidence_resolution(component_id: str = "") -> str:
    """Build the ``resolved_by`` label, optionally naming the provider."""
    component_id = str(component_id or "").strip()
    return (
        f"{WEB_EVIDENCE_RESOLUTION}:{component_id}" if component_id
        else WEB_EVIDENCE_RESOLUTION
    )


def is_web_retrieved(origin: Any) -> bool:
    """True when ``origin`` says the evidence came off the open web.

    Matches the bare prefix and any ``web_search:<id>`` value. Everything else
    — including ``None``, ``""`` and :data:`CURATED_SOURCE` — is False, which is
    what keeps this change a NO-OP for every existing registered source.
    """
    if not isinstance(origin, str):
        return False
    return origin.strip().startswith(WEB_SEARCH_PREFIX)


def is_web_evidence_resolution(resolved_by: Any) -> bool:
    """True for the ``web_evidence`` resolution label, bare or provider-suffixed.

    The suffixed form (``web_evidence:search.searxng.local``) MUST match: a
    provider-stamped label that fell through this check would land in headline
    exogenous, which is the precise silent corruption the label exists to
    prevent.
    """
    if not isinstance(resolved_by, str):
        return False
    label = resolved_by.strip()
    return (
        label == WEB_EVIDENCE_RESOLUTION
        or label.startswith(f"{WEB_EVIDENCE_RESOLUTION}:")
    )


def provider_of(origin: Any) -> str:
    """The provider component id inside a web origin, else ``""``."""
    if not is_web_retrieved(origin):
        return ""
    return str(origin).strip()[len(WEB_SEARCH_PREFIX):]


def resolve_retrieval_origin(
    row: Mapping[str, Any], payload: Mapping[str, Any] | None = None,
) -> str | None:
    """The retrieval origin of a ``signals``-shaped row.

    Reads the migration-0112 COLUMN first, then the payload stamp — the same
    two-place resolution ``signal_license_class`` performs, so the archive gate
    and the corpus facet can never disagree.

    ONE deliberate asymmetry: if EITHER place says web-retrieved, the answer is
    web-retrieved. The two gates that read this both get STRICTER on a web
    origin, so a disagreement must resolve toward the stricter reading — a
    fail-safe, not a merge rule.
    """
    column = row.get(FIELD)
    if payload is None:
        payload = _as_mapping(row.get("payload"))
    stamped = payload.get(FIELD) if payload else None

    if is_web_retrieved(column):
        return str(column).strip()
    if is_web_retrieved(stamped):
        return str(stamped).strip()
    if isinstance(column, str) and column.strip():
        return column.strip()
    if isinstance(stamped, str) and stamped.strip():
        return stamped.strip()
    return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Coerce a jsonb value to a mapping (asyncpg may hand back a str)."""
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (str, bytes)):
        import json

        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


__all__ = [
    "CURATED_SOURCE",
    "FIELD",
    "WEB_EVIDENCE_RESOLUTION",
    "WEB_SEARCH_PREFIX",
    "is_web_evidence_resolution",
    "is_web_retrieved",
    "provider_of",
    "resolve_retrieval_origin",
    "web_evidence_resolution",
    "web_search_origin",
]
