# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE page rule (L-1): which alerts page; everything else is logged only.

The rule is a list of ``(kind, minimum severity, optional subject filter)``
entries in a YAML file (:data:`DEFAULT_RULE_PATH`, overridable by
``LEGBA_ALERT_PAGE_RULE_PATH``). An alert pages when ANY entry matches it. That
is the whole rule — no escalation chains, no per-producer cases. Repeats are
stopped in one place, the alert episode (:mod:`.raise_alert`), not here.

A rule file that does not load or names a kind outside the closed list is a
hard error at load time (:class:`PageRuleError`): a typo must never silently
turn paging off. The runtime loads the rule once per process and logs it.

Stdlib + PyYAML: the registry's alert-log route imports it in the slim image.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .kinds import KINDS
from .ladder import SEVERITY_ORDER, normalise_severity

logger = logging.getLogger(__name__)

ENV_PAGE_RULE_PATH = "LEGBA_ALERT_PAGE_RULE_PATH"
DEFAULT_RULE_PATH = Path(__file__).with_name("page_rule.yaml")


class PageRuleError(ValueError):
    """The page-rule file is unreadable or names something that does not exist."""


@dataclass(frozen=True)
class PageRuleEntry:
    kind: str
    min_severity: str
    subject: Optional[str] = None

    def matches(self, kind: str, severity: str, subject_id: Optional[str]) -> bool:
        if kind != self.kind:
            return False
        if SEVERITY_ORDER.get(normalise_severity(severity), 0) < SEVERITY_ORDER[self.min_severity]:
            return False
        if self.subject is None:
            return True
        return fnmatch.fnmatchcase(str(subject_id or ""), self.subject)

    def describe(self) -> str:
        """The entry as one plain line (stored on every paged alert event)."""
        text = f"{self.kind} >= {self.min_severity}"
        return f"{text} (subject {self.subject})" if self.subject else text


@dataclass(frozen=True)
class PageRule:
    version: str
    entries: tuple[PageRuleEntry, ...]
    path: str = ""

    def match(
        self, kind: str, severity: str, subject_id: Optional[str] = None
    ) -> Optional[PageRuleEntry]:
        """The first entry that pages this alert, or ``None`` (logged only)."""
        for entry in self.entries:
            if entry.matches(kind, severity, subject_id):
                return entry
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "path": self.path,
            "page": [
                {"kind": e.kind, "min_severity": e.min_severity,
                 **({"subject": e.subject} if e.subject else {})}
                for e in self.entries
            ],
        }


def parse_rule(doc: Any, *, path: str = "") -> PageRule:
    """Validate a loaded YAML document into a :class:`PageRule` (strict)."""
    if not isinstance(doc, dict):
        raise PageRuleError(f"page rule {path or '<doc>'}: top level must be a mapping")
    raw_entries = doc.get("page")
    if not isinstance(raw_entries, list):
        raise PageRuleError(f"page rule {path or '<doc>'}: 'page' must be a list")
    entries: list[PageRuleEntry] = []
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise PageRuleError(f"page rule entry {i}: must be a mapping")
        extra = set(raw) - {"kind", "min_severity", "subject"}
        if extra:
            raise PageRuleError(f"page rule entry {i}: unknown keys {sorted(extra)}")
        kind = str(raw.get("kind") or "").strip()
        if kind not in KINDS:
            raise PageRuleError(
                f"page rule entry {i}: kind {kind!r} is not in the closed list"
            )
        sev = str(raw.get("min_severity") or "").strip().lower()
        if sev not in SEVERITY_ORDER:
            raise PageRuleError(
                f"page rule entry {i}: min_severity {sev!r} is not on the ladder "
                f"({', '.join(SEVERITY_ORDER)})"
            )
        subject = raw.get("subject")
        subject = str(subject).strip() if subject not in (None, "") else None
        entries.append(PageRuleEntry(kind=kind, min_severity=sev, subject=subject))
    return PageRule(version=str(doc.get("version") or ""), entries=tuple(entries), path=path)


def load_rule(path: str | os.PathLike[str] | None = None) -> PageRule:
    """Load and validate the rule file (``path`` > env > the shipped default)."""
    import yaml

    chosen = Path(path or os.environ.get(ENV_PAGE_RULE_PATH) or DEFAULT_RULE_PATH)
    try:
        doc = yaml.safe_load(chosen.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PageRuleError(f"page rule {chosen}: {type(exc).__name__}: {exc}") from exc
    return parse_rule(doc, path=str(chosen))


_CACHED: dict[str, PageRule] = {}


def current_rule() -> PageRule:
    """The process's rule, loaded once per path (logged on first load)."""
    key = os.environ.get(ENV_PAGE_RULE_PATH) or str(DEFAULT_RULE_PATH)
    rule = _CACHED.get(key)
    if rule is None:
        rule = load_rule(key)
        _CACHED[key] = rule
        logger.info(
            "alert_page_rule.loaded version=%s path=%s entries=%d",
            rule.version, rule.path, len(rule.entries),
        )
    return rule


def reset_cache() -> None:
    """Forget the loaded rule (tests; a roll restarts the process anyway)."""
    _CACHED.clear()


def entries_for(rule: PageRule, kinds: Iterable[str]) -> dict[str, list[str]]:
    wanted = set(kinds)
    out: dict[str, list[str]] = {}
    for e in rule.entries:
        if e.kind in wanted:
            out.setdefault(e.kind, []).append(e.describe())
    return out


__all__ = [
    "DEFAULT_RULE_PATH",
    "ENV_PAGE_RULE_PATH",
    "PageRule",
    "PageRuleEntry",
    "PageRuleError",
    "current_rule",
    "entries_for",
    "load_rule",
    "parse_rule",
    "reset_cache",
]
