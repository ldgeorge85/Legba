"""
Event lifecycle state machine.

Deterministic, pure functions for managing event lifecycle transitions.
No database access — callers pass in event dicts and receive the new
status (or None if no transition fires).

State machine diagram::

    ┌──────────┐  signal_count >= 3   ┌────────────┐
    │ EMERGING ├─────────────────────>│ DEVELOPING │
    │          │                       │            │
    │          │  no signals 48h       │            │  signal_count >= 5
    │          ├──────────┐            │            │  AND confidence >= 0.6
    └──────────┘          │            │            ├──────────┐
                          v            └─────┬──────┘          v
                    ┌──────────┐             │          ┌──────────┐
                    │ RESOLVED │<────────────┘          │  ACTIVE  │
                    │          │  no signals 72h        │          │
                    │          │                        │          │  velocity > 2.0
                    │          │<───────────────────────┤          ├──────────┐
                    │          │  no signals 7d         └─────┬────┘          v
                    └─────┬────┘                              ^        ┌──────────┐
                          │                                   │        │ EVOLVING │
                          │  new signal linked                │        │          │
                          v                                   │        │          │
                    ┌──────────────┐   immediate              │        └─────┬────┘
                    │ REACTIVATED  ├──────────────>───────────┘              │
                    └──────────────┘   (-> DEVELOPING)           velocity   │
                                                                 < 1.5     │
                                                                 ──────────┘

Transition rules are evaluated top-to-bottom; the first match wins.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------

class EventLifecycleStatus(str, Enum):
    """Lifecycle states for a derived event."""
    EMERGING     = "emerging"
    DEVELOPING   = "developing"
    ACTIVE       = "active"
    EVOLVING     = "evolving"
    RESOLVED     = "resolved"
    REACTIVATED  = "reactivated"


# ---------------------------------------------------------------------------
# Condition functions
#
# Each receives an event dict with at minimum:
#   signal_count: int
#   confidence: float
#   last_signal_at: datetime | str | None
#   lifecycle_status: str
#   created_at: datetime | str
#   velocity_change: float  (optional, default 0.0)
#
# Returns True if the transition condition is met.
# ---------------------------------------------------------------------------

def _parse_dt(val: datetime | str | None) -> datetime | None:
    """Coerce a value to a tz-aware datetime, or None."""
    if val is None:
        return None
    if isinstance(val, str):
        # ISO-8601 parsing
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    return None


def _hours_since_last_signal(event: dict) -> float | None:
    """Return hours since last signal, or None if unknown."""
    last = _parse_dt(event.get("last_signal_at"))
    if last is None:
        # Fall back to created_at if no signals recorded
        last = _parse_dt(event.get("created_at"))
    if last is None:
        return None
    now = datetime.now(timezone.utc)
    return max((now - last).total_seconds() / 3600.0, 0.0)


def _cond_emerging_to_developing(event: dict) -> bool:
    return event.get("signal_count", 0) >= 3


def _cond_emerging_to_resolved(event: dict) -> bool:
    hours = _hours_since_last_signal(event)
    return hours is not None and hours >= 48.0


def _cond_developing_to_active(event: dict) -> bool:
    return (event.get("signal_count", 0) >= 5
            and event.get("confidence", 0.0) >= 0.6)


def _cond_developing_to_resolved(event: dict) -> bool:
    hours = _hours_since_last_signal(event)
    return hours is not None and hours >= 72.0


def _cond_active_to_evolving(event: dict) -> bool:
    return event.get("velocity_change", 0.0) > 2.0


def _cond_active_to_resolved(event: dict) -> bool:
    hours = _hours_since_last_signal(event)
    return hours is not None and hours >= 168.0  # 7 days


def _cond_evolving_to_active(event: dict) -> bool:
    return event.get("velocity_change", 0.0) < 1.5


def _cond_reactivated_to_developing(_event: dict) -> bool:
    # Immediate / automatic transition
    return True


# ---------------------------------------------------------------------------
# Transition rules
#
# Evaluated in order — first match wins.  Tuple:
#   (from_status, to_status, condition_fn)
# ---------------------------------------------------------------------------

TRANSITION_RULES: list[
    tuple[EventLifecycleStatus, EventLifecycleStatus, Callable[[dict], bool]]
] = [
    # EMERGING transitions (check positive progress before timeout)
    (EventLifecycleStatus.EMERGING,    EventLifecycleStatus.DEVELOPING,  _cond_emerging_to_developing),
    (EventLifecycleStatus.EMERGING,    EventLifecycleStatus.RESOLVED,    _cond_emerging_to_resolved),
    # DEVELOPING transitions (check promotion before timeout)
    (EventLifecycleStatus.DEVELOPING,  EventLifecycleStatus.ACTIVE,      _cond_developing_to_active),
    (EventLifecycleStatus.DEVELOPING,  EventLifecycleStatus.RESOLVED,    _cond_developing_to_resolved),
    # ACTIVE transitions
    (EventLifecycleStatus.ACTIVE,      EventLifecycleStatus.EVOLVING,    _cond_active_to_evolving),
    (EventLifecycleStatus.ACTIVE,      EventLifecycleStatus.RESOLVED,    _cond_active_to_resolved),
    # EVOLVING transitions
    (EventLifecycleStatus.EVOLVING,    EventLifecycleStatus.ACTIVE,      _cond_evolving_to_active),
    # REACTIVATED transitions (immediate pass-through)
    (EventLifecycleStatus.REACTIVATED, EventLifecycleStatus.DEVELOPING,  _cond_reactivated_to_developing),
    # NOTE: RESOLVED -> REACTIVATED is triggered externally when a new
    # signal is linked, not by a condition function.  The caller sets the
    # status to REACTIVATED directly and then calls check_transition()
    # which will immediately promote to DEVELOPING.
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_transition(event: dict) -> EventLifecycleStatus | None:
    """Evaluate transition rules against an event and return the new status.

    Parameters
    ----------
    event : dict
        Must contain at minimum:
        - ``lifecycle_status`` (str) — current status
        - ``signal_count`` (int)
        - ``confidence`` (float)
        - ``last_signal_at`` (datetime | str | None)
        - ``created_at`` (datetime | str)
        - ``velocity_change`` (float, optional — default 0.0)

    Returns
    -------
    EventLifecycleStatus | None
        The new status if a transition fires, or ``None`` if the event
        stays in its current state.
    """
    current_raw = event.get("lifecycle_status", "emerging")
    try:
        current = EventLifecycleStatus(current_raw)
    except ValueError:
        return None

    for from_status, to_status, condition_fn in TRANSITION_RULES:
        if current == from_status and condition_fn(event):
            return to_status

    return None
