# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TEST-ONLY: a trivial deterministic handler (counts its inputs; no model)."""
from __future__ import annotations

from typing import Any, Mapping

from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    n = len(inputs)
    return AnalystMethodResult(
        finding=FindingPayload(
            title=f"fixture_echo: {n} input rows",
            body=f"{n} input rows were handed to this run.",
            confidence=1.0,
            evidence=[],
            tags=["deterministic", "fixture_echo"],
            data={"sub_handler": "fixture_echo", "rows": n,
                  "label": options.get("label", "")},
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )
