"""Structured watchlist evaluation — pure functions, no DB access.

Evaluates watchlist queries against events using entity, location, severity,
and category matching. Used by the ingestion clusterer and agent tools.
"""

from __future__ import annotations

# Severity ordering for threshold comparison
_SEVERITY_RANK = {
    "routine": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def evaluate_structured_query(
    query: dict,
    event: dict,
    entity_links: list[dict] | None = None,
) -> dict | None:
    """Evaluate a structured watchlist query against an event.

    query format::

        {
            "entity": "IRGC Navy",           # entity name to match
            "relationship": "OperatesIn",     # relationship type (optional, TODO)
            "location": "Strait of Hormuz",   # location to match (optional)
            "severity_min": "high",           # minimum severity (optional)
            "category": "conflict",           # category match (optional)
            "logic": "AND"                    # AND = all criteria, OR = any criterion
        }

    Args:
        query: Structured query dict with matching criteria.
        event: Event dict (from JSONB ``data`` column or tool output).
        entity_links: Optional list of entity link dicts with at least
            ``{"entity_name": "..."}`` entries (from signal_entity_links or
            graph neighbours).

    Returns:
        ``{"matched": True, "reasons": [...]}`` when the event matches, or
        ``None`` if it does not.
    """
    if not query or not event:
        return None

    logic = query.get("logic", "AND").upper()
    reasons: list[str] = []
    criteria_checked = 0

    # --- entity ---
    q_entity = query.get("entity", "").strip()
    if q_entity:
        criteria_checked += 1
        entity_match = _check_entity(q_entity, event, entity_links)
        if entity_match:
            reasons.append(entity_match)
        elif logic == "AND":
            return None

    # --- relationship ---
    # TODO: SLM-assisted matching — would need graph query context.
    # Skipped for now; structured_query consumers should rely on entity +
    # location + category until graph-aware evaluation is available.

    # --- location ---
    q_location = query.get("location", "").strip()
    if q_location:
        criteria_checked += 1
        location_match = _check_location(q_location, event)
        if location_match:
            reasons.append(location_match)
        elif logic == "AND":
            return None

    # --- severity_min ---
    q_severity = query.get("severity_min", "").strip().lower()
    if q_severity and q_severity in _SEVERITY_RANK:
        criteria_checked += 1
        severity_match = _check_severity(q_severity, event)
        if severity_match:
            reasons.append(severity_match)
        elif logic == "AND":
            return None

    # --- category ---
    q_category = query.get("category", "").strip().lower()
    if q_category:
        criteria_checked += 1
        category_match = _check_category(q_category, event)
        if category_match:
            reasons.append(category_match)
        elif logic == "AND":
            return None

    if not criteria_checked:
        return None  # empty query

    if not reasons:
        return None  # OR mode, nothing matched

    return {"matched": True, "reasons": reasons}


def evaluate_keyword_query(
    keywords: list[str],
    entities: list[str],
    event: dict,
) -> dict | None:
    """Evaluate a keyword/entity watchlist query against an event.

    This is the existing keyword matching logic extracted as a pure function
    so it can be shared between the ingestion clusterer and agent tools.

    Args:
        keywords: List of keywords to match in event title/summary.
        entities: List of entity names to match in event actors/locations.
        event: Event dict with title, summary, actors, locations, category.

    Returns:
        ``{"matched": True, "reasons": [...]}`` when matched, else ``None``.
    """
    if not keywords and not entities:
        return None

    event_text = (
        (event.get("title") or "") + " " + (event.get("summary") or "")
    ).lower()
    event_actors = {a.lower() for a in (event.get("actors") or []) if a}
    event_locations = {loc.lower() for loc in (event.get("locations") or []) if loc}

    reasons: list[str] = []
    failed = False

    # Entity matching (AND with other criteria types, OR within entities list)
    if entities:
        entities_lower = [e.lower() for e in entities]
        hit = next(
            (
                e
                for e in entities_lower
                if e in event_actors or e in event_locations or e in event_text
            ),
            None,
        )
        if hit:
            reasons.append(f"entity:{hit}")
        else:
            failed = True

    # Keyword matching
    if not failed and keywords:
        keywords_lower = [k.lower() for k in keywords]
        hit = next((kw for kw in keywords_lower if kw in event_text), None)
        if hit:
            reasons.append(f"keyword:{hit}")
        else:
            failed = True

    if failed or not reasons:
        return None

    return {"matched": True, "reasons": reasons}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_entity(
    q_entity: str,
    event: dict,
    entity_links: list[dict] | None,
) -> str | None:
    """Check if query entity appears in event actors, locations, or entity_links."""
    q_lower = q_entity.lower()

    # Check actors
    for actor in event.get("actors") or []:
        if actor and q_lower in actor.lower():
            return f"entity:{q_entity} in actors"

    # Check locations
    for loc in event.get("locations") or []:
        if loc and q_lower in loc.lower():
            return f"entity:{q_entity} in locations"

    # Check entity_links (from signal_entity_links or graph neighbours)
    if entity_links:
        for link in entity_links:
            name = link.get("entity_name") or link.get("canonical_name") or ""
            if name and q_lower in name.lower():
                return f"entity:{q_entity} in entity_links"

    # Check title as fallback
    title = (event.get("title") or "").lower()
    if q_lower in title:
        return f"entity:{q_entity} in title"

    return None


def _check_location(q_location: str, event: dict) -> str | None:
    """Check if query location appears in event locations or geo_countries."""
    q_lower = q_location.lower()

    for loc in event.get("locations") or []:
        if loc and q_lower in loc.lower():
            return f"location:{q_location} in locations"

    for country in event.get("geo_countries") or []:
        if country and q_lower in country.lower():
            return f"location:{q_location} in geo_countries"

    # Check title as fallback
    title = (event.get("title") or "").lower()
    if q_lower in title:
        return f"location:{q_location} in title"

    return None


def _check_severity(q_severity_min: str, event: dict) -> str | None:
    """Check if event severity meets or exceeds the minimum threshold."""
    event_severity = (event.get("severity") or "medium").lower()
    min_rank = _SEVERITY_RANK.get(q_severity_min, 0)
    event_rank = _SEVERITY_RANK.get(event_severity, 2)  # default medium

    if event_rank >= min_rank:
        return f"severity:{event_severity}>={q_severity_min}"
    return None


def _check_category(q_category: str, event: dict) -> str | None:
    """Check if event category matches the query category."""
    event_category = (event.get("category") or "").lower()
    if event_category == q_category:
        return f"category:{q_category} matches"
    return None
