# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``keyword_gate`` — the droppable keyword filter kind (W3 SRC-3).

ONE matcher, two entry points:

  * the ``keyword_gate`` pipeline stage, declared under a source descriptor's
    ``pipeline.ingestion_filters`` and built by
    :func:`legba.runtime.pipeline.build_filter_handler` into a
    :class:`KeywordGateHandler`;
  * the per-feed sugar ``RSSConfig.keyword_include`` / ``keyword_exclude``,
    which the RSS handler turns into the SAME :class:`KeywordGate` via
    :meth:`KeywordGate.from_lists` and exposes as ``handler.keyword_gate``;
    SourceCore applies it at the same point as the stages (see
    :mod:`legba.runtime.source_keyword_gate`). There is no second matcher.

Decision (:meth:`KeywordGate.evaluate`), in order:

  1. any ``exclude`` term matches → DROP (exclude always wins);
  2. ``include`` is non-empty and does not match → DROP. ``mode="any"`` needs
     one include term; ``mode="all"`` needs every include term;
  3. otherwise → KEEP.

Matching is whole-word, Unicode-aware and (by default) case-insensitive.
Both the text and each term are NFKC-normalised and, when
``case_insensitive``, ``casefold()``-ed, then a term matches only where it is
not flanked by a word character (``\\w`` in a ``str`` regex is Unicode-aware),
so ``iran`` never matches ``Mirandola`` and ``Iran`` does match ``Iran-backed``
or ``Iran's``. A multi-word phrase matches across any run of whitespace
(``export control`` matches ``export\\n  control``); it does not match a
plural or other inflection (``export controls``) — list both forms if both
are wanted. HTML tags are stripped and entities unescaped before matching, so
a term inside an ``href`` does not admit an item.

PER-TERM PARTIAL MATCH (off unless a term asks for it). A ``*`` at
either END of a term drops the word boundary on that side, so a compounding
language can be gated: ``Beschaffung*`` (prefix) matches "Beschaffungsamt",
``*vertrag`` (suffix) matches "Rahmenvertrag", ``*rüstung*`` (substring)
matches "Rüstungsindustrie" and "Ausrüstung". A term without ``*`` is
whole-word exactly as before; a ``*`` anywhere but the ends, or a term that is
only ``*``, is refused at config validation.

WHERE IT RUNS (and why it must not be in ``enrichment``). On the source-first
runtime, :class:`legba.runtime.source_actor.SourceCore` applies the
``keyword_gate`` stages of ``pipeline.ingestion_filters`` to each raw signal
the handler yields BEFORE :func:`legba.data.sources.baseline.run_baseline`,
which is where ``pipeline.enrichment`` (language_detect / ner_multilingual /
fact_extractor / geocode — the NER POSTs) runs. So a dropped item never
reaches enrichment, the substrate write or the fan-out. The other
``ingestion_filters`` kinds (``dedupe_tier_1`` / ``dedupe_tier_2``) are NOT
droppers there: they alias-link AFTER the write (``ingest_dedupe``). The
``enrichment`` chain, by contrast, is annotate-only — the host's enrichment
stage falls back to the unenriched signal when a stage returns ``None`` — so a
gate declared there would silently drop nothing; the source schema refuses it
(:class:`legba.data.schemas.source.SourcePipeline`).

Observability: each gate keeps per-instance drop counts
(:class:`KeywordGateStats`) that the source actor drains once per poll into
the source's persisted ``cursor`` state. A drop is logged at DEBUG with the
reason (the term that dropped it) — never the body or the title.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..sources._contract import Signal

if TYPE_CHECKING:  # pragma: no cover — type-only
    # Not imported at module load: ``legba.data.sources.rss`` imports this
    # module while ``filters._contract`` may itself be mid-import (it imports
    # ``sources._contract``, whose package init imports rss) — a cycle.
    from ._contract import FilterContext, FilterHealth

logger = logging.getLogger(__name__)

KEYWORD_GATE_KIND = "keyword_gate"

#: Default payload fields scanned. ``text`` is the extracted article body
#: where a handler provides one; ``raw_body`` is the RSS content/summary.
DEFAULT_FIELDS: tuple[str, ...] = ("title", "text", "raw_body")

#: Drop-reason key when ``include`` is set and nothing matched (mode ``any``).
REASON_INCLUDE_NONE = "include:none"

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def _normalise(text: str, *, case_insensitive: bool) -> str:
    out = unicodedata.normalize("NFKC", text)
    return out.casefold() if case_insensitive else out


def _clean_terms(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        if not isinstance(v, str):
            raise ValueError(f"keyword terms must be strings, got {type(v).__name__}")
        term = _WS_RE.sub(" ", v).strip()
        if not term:
            raise ValueError("keyword terms must be non-empty")
        if "*" in term.strip("*") or not term.strip("*").strip():
            raise ValueError(
                f"keyword term {term!r}: '*' marks a prefix/suffix/substring "
                "match and may appear only at the start and/or end of a term"
            )
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


class KeywordGateConfig(BaseModel):
    """Config for the ``keyword_gate`` stage (``PipelineStage.config``)."""

    model_config = ConfigDict(extra="forbid")

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=lambda: list(DEFAULT_FIELDS))
    mode: Literal["any", "all"] = "any"
    case_insensitive: bool = True

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def _terms(cls, v: Any) -> list[str]:
        return _clean_terms(v or [])

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: list[str]) -> list[str]:
        cleaned = [f.strip() for f in v if isinstance(f, str) and f.strip()]
        if not cleaned:
            raise ValueError("keyword_gate.fields must name at least one payload field")
        return cleaned

    @model_validator(mode="after")
    def _has_terms(self) -> "KeywordGateConfig":
        # No-stubs: a gate with no terms admits everything — a silent no-op
        # stage. Refuse it at construction rather than run it.
        if not self.include and not self.exclude:
            raise ValueError(
                "keyword_gate needs at least one include or exclude term "
                "(an empty gate would admit everything)"
            )
        return self


@dataclass(frozen=True)
class KeywordDecision:
    """Outcome of one evaluation. ``reason`` is set only on a drop."""

    admit: bool
    reason: str | None = None


@dataclass
class KeywordGateStats:
    """Drop counts since the last :meth:`KeywordGate.drain_stats`."""

    dropped: int = 0
    by_reason: Counter = field(default_factory=Counter)


def _compile(term: str, *, case_insensitive: bool) -> re.Pattern[str]:
    open_left, open_right = term.startswith("*"), term.endswith("*")
    words = _normalise(term.strip("*"), case_insensitive=case_insensitive).split()
    body = r"\s+".join(re.escape(w) for w in words)
    left = "" if open_left else r"(?<!\w)"
    right = "" if open_right else r"(?!\w)"
    return re.compile(f"{left}{body}{right}")


class KeywordGate:
    """The single keyword matcher (stage kind AND the RSS sugar)."""

    def __init__(self, config: KeywordGateConfig) -> None:
        self.config = config
        ci = config.case_insensitive
        self._include = [(t, _compile(t, case_insensitive=ci)) for t in config.include]
        self._exclude = [(t, _compile(t, case_insensitive=ci)) for t in config.exclude]
        self.stats = KeywordGateStats()

    @classmethod
    def from_lists(
        cls,
        include: Iterable[str] | None,
        exclude: Iterable[str] | None,
    ) -> "KeywordGate | None":
        """Build the gate from include/exclude lists; ``None`` when both are
        empty (the per-feed sugar's "not gated")."""
        include = list(include or [])
        exclude = list(exclude or [])
        if not include and not exclude:
            return None
        return cls(KeywordGateConfig(include=include, exclude=exclude))

    def _haystack(self, signal: Signal) -> str:
        payload = signal.payload if isinstance(signal.payload, Mapping) else {}
        parts: list[str] = []
        for name in self.config.fields:
            value = payload.get(name)
            if isinstance(value, str) and value:
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(v for v in value if isinstance(v, str) and v)
        text = html.unescape(_TAG_RE.sub(" ", "\n".join(parts)))
        return _normalise(text, case_insensitive=self.config.case_insensitive)

    def evaluate(self, signal: Signal) -> KeywordDecision:
        """Pure decision for one signal (no counting, no logging)."""
        text = self._haystack(signal)
        for term, pattern in self._exclude:
            if pattern.search(text):
                return KeywordDecision(admit=False, reason=f"exclude:{term}")
        if self._include:
            if self.config.mode == "all":
                for term, pattern in self._include:
                    if not pattern.search(text):
                        return KeywordDecision(
                            admit=False, reason=f"include:missing:{term}",
                        )
            elif not any(p.search(text) for _, p in self._include):
                return KeywordDecision(admit=False, reason=REASON_INCLUDE_NONE)
        return KeywordDecision(admit=True)

    def admit(self, signal: Signal, *, source_id: str | None = None) -> bool:
        """Evaluate, count a drop, DEBUG-log the reason; True = keep."""
        decision = self.evaluate(signal)
        if decision.admit:
            return True
        self.stats.dropped += 1
        self.stats.by_reason[decision.reason or "unknown"] += 1
        logger.debug(
            "keyword_gate.drop source=%s signal=%s reason=%s",
            source_id or signal.source_id, signal.signal_id, decision.reason,
        )
        return False

    def drain_stats(self) -> KeywordGateStats:
        """Return the counts accumulated so far and reset them."""
        out, self.stats = self.stats, KeywordGateStats()
        return out


class KeywordGateHandler:
    """``keyword_gate`` StreamHandler: returns ``None`` to drop a signal."""

    kind: ClassVar[str] = KEYWORD_GATE_KIND
    family: ClassVar[str] = "filter"
    schema_version: ClassVar[str] = "legba/filter.keyword_gate/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = KeywordGateConfig
    output_contract: ClassVar[Mapping[str, type]] = {}

    def __init__(self, config: KeywordGateConfig) -> None:
        self.config = config
        self.gate = KeywordGate(config)
        self._last_success_at: datetime | None = None

    async def transform(
        self, signal: Signal, ctx: "FilterContext | Any",
    ) -> Signal | None:
        source_id = getattr(ctx, "source_id", None) or getattr(ctx, "target_id", None)
        self._last_success_at = datetime.now(tz=timezone.utc)
        return signal if self.gate.admit(signal, source_id=source_id) else None

    async def health_check(self, ctx: "FilterContext | Any") -> "FilterHealth":
        from ._contract import FilterHealth

        return FilterHealth(
            state="healthy",
            last_success_at=self._last_success_at,
            detail={
                "include_terms": len(self.config.include),
                "exclude_terms": len(self.config.exclude),
                "mode": self.config.mode,
                "pending_dropped": self.gate.stats.dropped,
            },
        )


__all__ = [
    "DEFAULT_FIELDS",
    "KEYWORD_GATE_KIND",
    "REASON_INCLUDE_NONE",
    "KeywordDecision",
    "KeywordGate",
    "KeywordGateConfig",
    "KeywordGateHandler",
    "KeywordGateStats",
]
