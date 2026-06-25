# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coarse subject planning for a target subscription (P-08 / PIVOT §6.1).

JetStream filters SUBJECTS, not JSON. A subscription's *structured filter*
(``geo``/``languages``/``tags``/``entity_classes``/``modalities``) is far finer
than the four coarse subject axes (tenant / source / modality / event-class).
So at the subject layer we only translate the axes that ARE coarse — tenant
(from the resolved source's scope), source id, and modality — into a small set
of subject filters. Everything else (geo / tags / entity_classes / languages /
the Starlark residual) is matched downstream by SQL ``WHERE`` + the residual on
the narrowed set. We deliberately do NOT explode an arbitrary predicate into
subjects (PIVOT §4.4 / §6.1).

The output of :func:`subject_filters_for` is the set of coarse subject filters
a target's *aggregated* consumer (one per target) binds onto. Multiple resolved
sources + multiple modalities union into one filter set.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...data.nats import signal_subject_filter
from ...data.schemas.source import Subscription

# Modalities the substrate currently distinguishes. A subscription that does
# not constrain modality binds the wildcard ('*') rather than one filter per
# modality — coarser is fine (the residual + SQL narrow further).
_KNOWN_MODALITIES = ("text", "image", "audio", "video", "structured", "binary")


@dataclass(frozen=True)
class ResolvedBinding:
    """One resolved (source, tenant) the target subscribes to.

    Produced by :mod:`.sourceref` SourceRef resolution; consumed here to plan
    the coarse subject filters and downstream by the SQL builder.
    """

    source_id: str
    owner_tenant: str
    subscription: Subscription
    via_selector: bool  # True if matched via a SourceSelector, False if explicit


def subject_filters_for(bindings: list[ResolvedBinding]) -> list[str]:
    """Plan the coarse subject-filter set for a target's aggregated consumer.

    One filter per (binding × modality-axis). ``modalities`` on the
    subscription narrows the modality axis; an empty ``modalities`` binds the
    '*' wildcard for that axis. Tenant + source are always pinned to the
    resolved binding (they're known coarse facts). Event-class is left
    wildcard at the subject layer (``raw`` vs ``derived`` is a downstream
    concern; a target generally wants both unless it filters on
    ``produced_by_kind`` in the residual).

    Returns a de-duplicated, sorted list (stable for consumer-config equality).
    """
    filters: set[str] = set()
    for b in bindings:
        sub = b.subscription
        modalities: list[str | None]
        if sub.modalities:
            # Constrain to the requested modalities (ignore unknown ones —
            # they'd just never match; keep them out of the subject set).
            modalities = [m for m in sub.modalities if m in _KNOWN_MODALITIES]
            if not modalities:
                # All requested modalities are unknown → nothing matches; skip
                # this binding entirely rather than binding a '*' that would
                # over-deliver.
                continue
        else:
            modalities = [None]  # wildcard modality axis

        for modality in modalities:
            filters.add(
                signal_subject_filter(
                    tenant=b.owner_tenant,
                    source_id=b.source_id,
                    modality=modality,
                    event_class=None,  # both raw + derived
                )
            )
    return sorted(filters)


__all__ = ["ResolvedBinding", "subject_filters_for"]
