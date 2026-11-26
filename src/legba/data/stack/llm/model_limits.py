# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The model's own token limits a stack component declares.

``max_output_tokens`` (the plain alias, ``model_name``),
``thinking_max_output_tokens`` (the thinking alias) and
``context_window_tokens`` on ``LLMProviderConfig``. Read here, once, for every
handler: the OpenAI-compatible handler sends an alias's maximum as
``max_tokens``, and the input budgets follow the window
(``analysts/_llm_budget``). Nothing here is a limit of ours.
"""

from __future__ import annotations

from typing import Any

from ...schemas.env_refs import resolve_token_count
from .thinking import configured_thinking_alias


def declared_count(cfg: Any, field: str, *, component_id: str) -> int | None:
    """A token count the component declares, or ``None`` when it declares none.

    An ``${env:NAME}`` value the builder has not already resolved is resolved
    here; an unset variable raises (``EnvRefError``), never a silent
    "undeclared"."""
    knob = getattr(cfg, field, None) if cfg is not None else None
    if knob is None:
        return None
    return resolve_token_count(
        getattr(knob, "raw", None), component_id=component_id or "<unconfigured>", field=field,
    )


def max_output_by_alias(cfg: Any, *, component_id: str) -> dict[str, int]:
    """``{alias: declared maximum output}`` for the plain and the thinking
    alias; an alias with no declared maximum is absent (never given the other
    alias's number)."""
    if cfg is None:
        return {}
    out: dict[str, int] = {}
    plain = declared_count(cfg, "max_output_tokens", component_id=component_id)
    if plain is not None:
        out[str(cfg.model_name.raw)] = plain
    alias = configured_thinking_alias(cfg)
    thinking = declared_count(cfg, "thinking_max_output_tokens", component_id=component_id)
    if alias is not None and thinking is not None:
        out[alias] = thinking
    return out


__all__ = ["declared_count", "max_output_by_alias"]
