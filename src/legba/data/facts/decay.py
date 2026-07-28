# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fact confidence-decay model (C4) — MISP decaying-indicators / OpenCTI
decay-rules mechanics ported onto the temporal facts substrate.

PURE LIBRARY. Nothing in this module reads or writes the database, and nothing
here EVER mutates a stored ``facts.confidence`` — the model computes a
*derived* ``decayed_confidence`` readout from the stored confidence + the time
since the fact was last SIGHTED (last corroborating observation). The stamping
consumer (``deterministic_handlers/fact_decay_scan``) writes the readout to
the ``fact_decay_states`` SIDECAR (migration 0098); the consumption seam
(grounding ``_query_facts``) reads the sidecar only when the default-OFF flag
:data:`FACT_DECAY_WEIGHTING_ENV` is enabled.

The model (MISP polynomial)
---------------------------
::

    retention(t) = 1 - (t / lifetime) ** (1 / decay_speed)      for t < lifetime
    retention(t) = 0                                            for t >= lifetime
    decayed_confidence = stored_confidence * retention(t)

where ``t`` = days since the last sighting, ``lifetime`` (MISP tau) is the
per-CLASS base lifetime scaled by an optional per-``source_type`` multiplier
(operator-curated seed/curated rows age slower), and ``decay_speed`` (MISP
delta, < 1) shapes the curve: a SMALL delta keeps retention near 1.0 for most
of the lifetime then drops steeply near the end (a "confident until proven
stale" curve); delta → 1 approaches linear decay.

Sightings
---------
A corroborating re-assert of the same open triple is the sighting event: both
fact producers (the analyst ``_insert_fact`` upsert and the ingest
``fact_extractor`` upsert) UNION the corroborating signal ids into
``facts.derived_from`` on every same-triple re-observation. The scan therefore
DERIVES ``last_sighting_at`` = max backing-signal observation time
(``COALESCE(signals.fetched_at, signals.created_at)`` over ``derived_from``),
falling back to ``facts.created_at`` (birth = first sighting) for rows with no
surviving backing signal (seed facts; signals purged by retention). No new
write path and no new column on ``facts``. NB ``facts.updated_at`` is
deliberately NOT used: it is polluted by non-sighting touches (the legacy
``fact_decay`` mutation sweep, the contention arbiter's marker stamps,
entity_gc subject renames), so a disputed or renamed fact would look
perpetually fresh.

Reaction points + revoke threshold (OpenCTI decay-rule shape)
-------------------------------------------------------------
Named constants, operator-overridable via the config file:

* ``REACTION_POINT_FRESH``  — retention >= this → ``fresh``
* ``REACTION_POINT_AGING``  — retention >= this → ``aging`` (else ``stale``)
* ``REVOKE_THRESHOLD``      — decayed_confidence <= this (ABSOLUTE, the MISP
  score cutoff) → ``revoke_candidate``: the fact should stop feeding slices
  at full weight. The consumption seam (flag ON) excludes these rows from the
  grounding preamble.

Operator override
-----------------
``LEGBA_FACT_DECAY_CONFIG`` may point at a JSON file overlaying any part of
the default table::

    {
      "classes": {"event": {"lifetime_days": 30, "decay_speed": 0.5},
                  "cyber": {"lifetime_days": 14, "decay_speed": 0.6}},
      "predicate_classes": {"controls": "event"},
      "source_type_multipliers": {"seed": 3.0},
      "reaction_fresh": 0.9,
      "reaction_aging": 0.5,
      "revoke_threshold": 0.25
    }

A malformed file logs a loud warning and falls back to the defaults (the
model is measurement machinery behind a default-OFF flag — degrade, never
crash an analyst run over an operator typo).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ..vocabulary import normalize_predicate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Named reaction points + flags (operator-overridable via the config file)
# ---------------------------------------------------------------------------

#: retention >= this → ``fresh`` (recently sighted; full trust).
REACTION_POINT_FRESH: float = 0.85

#: retention >= this → ``aging`` (still usable, visibly older); below → ``stale``.
REACTION_POINT_AGING: float = 0.50

#: decayed_confidence <= this (ABSOLUTE, the MISP score-cutoff semantic) →
#: ``revoke_candidate``: below it the fact should stop feeding slices at full
#: weight (the flag-ON grounding seam excludes it from the preamble).
REVOKE_THRESHOLD: float = 0.20

#: The closed decay-state vocabulary, least → most decayed.
DECAY_STATES: tuple[str, ...] = ("fresh", "aging", "stale", "revoke_candidate")

#: The consumption-seam flag (default OFF). When enabled the grounding fact
#: read joins the ``fact_decay_states`` sidecar: revoke candidates are
#: excluded from the preamble and decayed_confidence annotates the rendered
#: lines. Ships OFF; flipping it is a measured operator step.
FACT_DECAY_WEIGHTING_ENV: str = "LEGBA_FACT_DECAY_WEIGHTING"

#: Optional operator config-file overlay (JSON; see the module docstring).
FACT_DECAY_CONFIG_ENV: str = "LEGBA_FACT_DECAY_CONFIG"


def fact_decay_weighting_enabled() -> bool:
    """Honor :data:`FACT_DECAY_WEIGHTING_ENV` (default OFF).

    Same truthy set as the sibling write-path flag ``LEGBA_FACT_CONTENTION``:
    only "1"/"true"/"yes"/"on" enable; unset/empty/anything-else keeps the
    grounding fact read byte-identical to the pre-C4 behavior (zero extra
    joins, zero exclusions, zero annotations).
    """
    raw = os.environ.get(FACT_DECAY_WEIGHTING_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Config shapes + the default table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecayClass:
    """One decay curve: a base lifetime (MISP tau, days) + curve shape
    (MISP delta — smaller = flatter early, steeper near the end)."""

    name: str
    lifetime_days: float
    decay_speed: float

    def __post_init__(self) -> None:
        if not (self.lifetime_days > 0):
            raise ValueError(f"decay class {self.name!r}: lifetime_days must be > 0")
        if not (0 < self.decay_speed <= 1):
            raise ValueError(f"decay class {self.name!r}: decay_speed must be in (0, 1]")


#: Default per-class curves. Lifetimes reflect how long each fact class stays
#: true WITHOUT re-observation: structural geography ~forever, officeholders
#: change on election cycles, stances shift within a year, events are stale in
#: weeks. All operator-overridable (see module docstring).
DEFAULT_DECAY_CLASSES: dict[str, DecayClass] = {
    # Near-permanent world structure (capitals, borders, containment).
    "structural":   DecayClass("structural", lifetime_days=1460.0, decay_speed=0.20),
    # Offices + roles: leaders, heads of state/government, employment.
    "officeholder": DecayClass("officeholder", lifetime_days=730.0, decay_speed=0.25),
    # Org/alliance membership + operating presence.
    "affiliation":  DecayClass("affiliation", lifetime_days=365.0, decay_speed=0.30),
    # Postures + stances: hostility, sanctions, control, supply relationships.
    "stance":       DecayClass("stance", lifetime_days=180.0, decay_speed=0.30),
    # Event-shaped observations: fast decay, near-linear.
    "event":        DecayClass("event", lifetime_days=45.0, decay_speed=0.50),
    # Everything unmapped.
    "default":      DecayClass("default", lifetime_days=120.0, decay_speed=0.30),
}

#: Canonical predicate (post ``normalize_predicate``, casefolded) → class.
#: Covers the live vocabulary (the predicate distribution on the running
#: substrate) + the seed adapters' canonical forms. Unmapped predicates take
#: the ``default`` class.
DEFAULT_PREDICATE_CLASSES: dict[str, str] = {
    # structural
    "located in": "structural",
    "capital of": "structural",
    "part of": "structural",
    "border with": "structural",
    "borders": "structural",
    "headquarters in": "structural",
    "headquartered in": "structural",
    "subsidiary of": "structural",
    "founded by": "structural",
    # officeholder / role
    "leader of": "officeholder",
    "head of state": "officeholder",
    "head of government": "officeholder",
    "spokesperson for": "officeholder",
    "employed by": "officeholder",
    "works for": "officeholder",
    # affiliation
    "member of": "affiliation",
    "allied with": "affiliation",
    "ally of": "affiliation",
    "affiliated with": "affiliation",
    "operates in": "affiliation",
    "party to": "affiliation",
    # stance
    "hostile to": "stance",
    "in active conflict with": "stance",
    "conflict with": "stance",
    "controls": "stance",
    "opponent of": "stance",
    "sanctioned by": "stance",
    "sanctions": "stance",
    "supplies to": "stance",
    "supplies weapons to": "stance",
    "arms transfer to": "stance",
    "signed agreement with": "stance",
    # event
    "involved in conflict event": "event",
    "involved in": "event",
    "targets": "event",
    "conducted via": "event",
    "attacked": "event",
    "reported": "event",
    "negotiated": "event",
    "imposed": "event",
}

#: Effective lifetime = class lifetime × this per-``source_type`` multiplier.
#: Operator-curated rows (the grounding provenance gate's trusted set) age
#: slower — a curated baseline is vetted, not scraped. Unlisted → 1.0.
DEFAULT_SOURCE_TYPE_MULTIPLIERS: dict[str, float] = {
    "seed": 2.0,
    "curated": 2.0,
}


@dataclass(frozen=True)
class DecayConfig:
    """The full resolved model: curves + classification + reaction points."""

    classes: Mapping[str, DecayClass] = field(
        default_factory=lambda: dict(DEFAULT_DECAY_CLASSES)
    )
    predicate_classes: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PREDICATE_CLASSES)
    )
    source_type_multipliers: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SOURCE_TYPE_MULTIPLIERS)
    )
    reaction_fresh: float = REACTION_POINT_FRESH
    reaction_aging: float = REACTION_POINT_AGING
    revoke_threshold: float = REVOKE_THRESHOLD

    def __post_init__(self) -> None:
        if "default" not in self.classes:
            raise ValueError("DecayConfig.classes must include a 'default' class")
        if not (0 < self.reaction_aging <= self.reaction_fresh <= 1):
            raise ValueError(
                "reaction points must satisfy 0 < aging <= fresh <= 1 "
                f"(got aging={self.reaction_aging}, fresh={self.reaction_fresh})"
            )
        if not (0 <= self.revoke_threshold < 1):
            raise ValueError(
                f"revoke_threshold must be in [0, 1) (got {self.revoke_threshold})"
            )
        for pred, cls in self.predicate_classes.items():
            if cls not in self.classes:
                raise ValueError(
                    f"predicate {pred!r} maps to unknown decay class {cls!r}"
                )


def default_decay_config() -> DecayConfig:
    """The in-repo default table (no env, no file)."""
    return DecayConfig()


def load_decay_config(environ: Mapping[str, str] | None = None) -> DecayConfig:
    """Resolve the model config: defaults overlaid by the optional operator
    JSON file at :data:`FACT_DECAY_CONFIG_ENV`.

    Overlay semantics — MERGE, not replace: a listed class updates/creates
    that class only; unlisted defaults stay. A malformed file (unreadable /
    bad JSON / invalid values) logs a loud warning and returns the pure
    defaults — this is default-OFF measurement machinery, so an operator typo
    must degrade, never crash the scan.
    """
    env = environ if environ is not None else os.environ
    path = (env.get(FACT_DECAY_CONFIG_ENV) or "").strip()
    if not path:
        return default_decay_config()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON must be an object")
        classes = dict(DEFAULT_DECAY_CLASSES)
        for name, spec in (raw.get("classes") or {}).items():
            base = classes.get(name)
            classes[str(name)] = DecayClass(
                name=str(name),
                lifetime_days=float(
                    spec.get(
                        "lifetime_days",
                        base.lifetime_days if base else 0.0,
                    )
                ),
                decay_speed=float(
                    spec.get(
                        "decay_speed",
                        base.decay_speed if base else 0.0,
                    )
                ),
            )
        predicate_classes = dict(DEFAULT_PREDICATE_CLASSES)
        for pred, cls in (raw.get("predicate_classes") or {}).items():
            predicate_classes[str(pred).strip().casefold()] = str(cls)
        multipliers = dict(DEFAULT_SOURCE_TYPE_MULTIPLIERS)
        for st, mult in (raw.get("source_type_multipliers") or {}).items():
            multipliers[str(st)] = float(mult)
        return DecayConfig(
            classes=classes,
            predicate_classes=predicate_classes,
            source_type_multipliers=multipliers,
            reaction_fresh=float(raw.get("reaction_fresh", REACTION_POINT_FRESH)),
            reaction_aging=float(raw.get("reaction_aging", REACTION_POINT_AGING)),
            revoke_threshold=float(raw.get("revoke_threshold", REVOKE_THRESHOLD)),
        )
    except Exception as exc:
        logger.warning(
            "fact_decay.config_load_failed path=%s err=%s — using defaults",
            path,
            exc,
        )
        return default_decay_config()


# ---------------------------------------------------------------------------
# Classification + curve math
# ---------------------------------------------------------------------------


def classify_fact(
    predicate: str | None, *, config: DecayConfig | None = None
) -> DecayClass:
    """Resolve a fact's decay class from its predicate.

    Runs the write-path :func:`normalize_predicate` first so both surface
    forms ("LeaderOf" / "leader of") land on one class. Unknown predicates get
    the ``default`` class — classification, never fabrication.
    """
    cfg = config or default_decay_config()
    key = ""
    if predicate and predicate.strip():
        key = normalize_predicate(predicate).strip().casefold()
    name = cfg.predicate_classes.get(key, "default")
    return cfg.classes.get(name) or cfg.classes["default"]


def effective_lifetime_days(
    decay_class: DecayClass,
    *,
    source_type: str | None,
    config: DecayConfig | None = None,
) -> float:
    """Class lifetime × the per-source_type multiplier (unlisted → 1.0)."""
    cfg = config or default_decay_config()
    mult = 1.0
    if source_type:
        try:
            mult = float(cfg.source_type_multipliers.get(source_type.strip().lower(), 1.0))
        except (TypeError, ValueError):
            mult = 1.0
    if mult <= 0:
        mult = 1.0
    return decay_class.lifetime_days * mult


def retention_factor(
    elapsed_days: float, *, lifetime_days: float, decay_speed: float
) -> float:
    """The MISP polynomial retention in [0, 1].

    ``1 - (t / lifetime) ** (1 / decay_speed)`` clamped; ``t < 0`` (a future
    sighting timestamp — clock skew) clamps to full retention; ``t >=
    lifetime`` is 0 (fully decayed).
    """
    if lifetime_days <= 0:
        return 0.0
    t = max(0.0, float(elapsed_days))
    if t >= lifetime_days:
        return 0.0
    factor = 1.0 - (t / lifetime_days) ** (1.0 / decay_speed)
    return min(1.0, max(0.0, factor))


@dataclass(frozen=True)
class DecayReadout:
    """One fact's derived decay readout — NEVER written back to ``facts``."""

    decayed_confidence: float
    decay_state: str            # one of DECAY_STATES
    decay_class: str
    retention: float            # the raw curve factor in [0, 1]
    elapsed_days: float
    lifetime_days: float        # effective (multiplier applied)


def decay_state_for(
    *, retention: float, decayed_confidence: float, config: DecayConfig | None = None
) -> str:
    """Map a (retention, decayed_confidence) pair onto the state vocabulary.

    The revoke check runs FIRST and on the ABSOLUTE decayed confidence (the
    MISP score-cutoff semantic): a fact whose decayed belief sits at/below the
    threshold is a revoke candidate regardless of which reaction band its
    curve position falls in.
    """
    cfg = config or default_decay_config()
    if decayed_confidence <= cfg.revoke_threshold:
        return "revoke_candidate"
    if retention >= cfg.reaction_fresh:
        return "fresh"
    if retention >= cfg.reaction_aging:
        return "aging"
    return "stale"


def decayed_confidence(
    *,
    confidence: float | None,
    predicate: str | None,
    source_type: str | None,
    now: datetime,
    last_sighting_at: datetime | None,
    config: DecayConfig | None = None,
) -> DecayReadout:
    """Compute one fact's full decay readout (pure; no I/O; no mutation).

    ``last_sighting_at=None`` (nothing derivable at all — no backing signal
    AND no created_at handed in by the caller) is treated as elapsed = full
    lifetime: an unsightable fact is fully decayed, honestly, rather than
    silently fresh.
    """
    cfg = config or default_decay_config()
    cls = classify_fact(predicate, config=cfg)
    lifetime = effective_lifetime_days(cls, source_type=source_type, config=cfg)
    if last_sighting_at is None:
        elapsed = lifetime
    else:
        sighted = last_sighting_at
        if sighted.tzinfo is None:
            sighted = sighted.replace(tzinfo=timezone.utc)
        ref = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        elapsed = (ref - sighted).total_seconds() / 86400.0
    factor = retention_factor(
        elapsed, lifetime_days=lifetime, decay_speed=cls.decay_speed
    )
    stored = float(confidence) if confidence is not None else 0.0
    stored = min(1.0, max(0.0, stored))
    decayed = stored * factor
    state = decay_state_for(
        retention=factor, decayed_confidence=decayed, config=cfg
    )
    return DecayReadout(
        decayed_confidence=decayed,
        decay_state=state,
        decay_class=cls.name,
        retention=factor,
        elapsed_days=max(0.0, elapsed),
        lifetime_days=lifetime,
    )


__all__ = [
    "DECAY_STATES",
    "DEFAULT_DECAY_CLASSES",
    "DEFAULT_PREDICATE_CLASSES",
    "DEFAULT_SOURCE_TYPE_MULTIPLIERS",
    "FACT_DECAY_CONFIG_ENV",
    "FACT_DECAY_WEIGHTING_ENV",
    "REACTION_POINT_AGING",
    "REACTION_POINT_FRESH",
    "REVOKE_THRESHOLD",
    "DecayClass",
    "DecayConfig",
    "DecayReadout",
    "classify_fact",
    "decay_state_for",
    "decayed_confidence",
    "default_decay_config",
    "effective_lifetime_days",
    "fact_decay_weighting_enabled",
    "load_decay_config",
    "retention_factor",
]
