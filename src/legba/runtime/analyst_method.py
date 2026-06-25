# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
# superseded by legba.data.analysts.inline_target
"""Compat shim for the pre-L-170 spike's analyst-method module.

The real implementation now lives in
:mod:`legba.data.analysts.inline_target` (per L-170).  This module
preserves the public surface the Phase 5a spike imported — so
``host.py``, ``dapr_actors.py``, and ``tests/runtime/test_spike_integration.py``
keep working without edits.

Direct callers should migrate to ``legba.data.analysts.inline_target``;
this shim will retire when those import sites land their own update.
"""

from __future__ import annotations

from ..data.analysts.inline_target import (
    AnalystMethodResult,
    InlineTargetRunner as LLMAnalystRunner,
    LLMHandlerLike,
)

# The default inline_target system prompt, re-exported under the historical
# ``legba.runtime.analyst_method:_DEFAULT_SYSTEM`` path the descriptors point
# their ``method.prompt_module`` at (country_assessor / india_energy_inline).
# The canonical definition lives in the kind module as ``_SYSTEM_PROMPT``.
from ..data.analysts.inline_target import _SYSTEM_PROMPT as _DEFAULT_SYSTEM


# --- world_assessor (META global LLM one-pager) ----------------------------
#
# Sibling system prompt for the ``world_assessor`` analyst (descriptors/
# analyst_world_assessor.yaml — kind=inline_target, no subscription.targets →
# a single GLOBAL cadence run every 6h over the tenant-wide 24h signal slice).
#
# It MUST emit the SAME strict-JSON envelope ``_DEFAULT_SYSTEM`` does
# (``title``/``body``/``confidence``/``evidence``/``tags``) — that is the
# exact contract ``inline_target._coerce_finding`` parses into a
# ``FindingPayload``; only the human guidance differs. The world-assessment
# one-pager (executive summary + per-region "Key developments" + "Top risks")
# is carried as MARKDOWN in ``body``; the requested ``summary``/``severity``
# fields ride inside that envelope (``summary`` == the markdown ``body``,
# ``severity`` is surfaced both as a ``severity:<level>`` tag and reflected in
# ``confidence``) so no out-of-contract key reaches the coercer.
from ..data.analysts._tradecraft import with_preamble

_WORLD_ASSESSOR_SYSTEM = with_preamble(
    """TASK — world situational awareness. You are given the GLOBAL slice of the most recent ~24h of signals from every monitored region at once (NOT scoped to one country). Produce ONE world situational-assessment FINDING: an executive one-pager a duty officer could read in two minutes.
Structure `body` as GitHub-flavored markdown, in order:
  1. a 2-4 sentence executive summary (no heading) that leads with the BLUF;
  2. "## Key developments" grouped under "### <Region>" subheadings (Americas; Europe; Middle East & North Africa; Sub-Saharan Africa; South Asia; East Asia & Pacific) — omit regions with nothing to report; cite signals [N];
  3. "## Top risks" — an ordered list, most consequential near-term risks first;
  4. "## Indicators to watch" — concrete developments that would materially change this picture.
Assign an overall world severity: one of low / moderate / elevated / high / critical.
Respond with STRICT JSON, nothing else, using EXACTLY this envelope:
{"title": "...", "body": "...", "confidence": 0.0-1.0, "evidence": ["..."], "tags": ["..."]}
`title`: a short dated headline (e.g. 'World situational assessment — <UTC date>'). `body`: the FULL markdown one-pager. `evidence`: the most load-bearing source headlines/observations. `tags`: ALWAYS include exactly one 'severity:<level>' tag plus 'world_assessment' and salient region/topic tags."""
)


__all__ = [
    "AnalystMethodResult",
    "LLMAnalystRunner",
    "LLMHandlerLike",
    "_DEFAULT_SYSTEM",
    "_WORLD_ASSESSOR_SYSTEM",
]
