# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.nlp_service — hosted Legba-models NLP service client.

Phase-4 architectural correction (2026-05-22). The filter handlers
(``ner_multilingual``, ``classify``, optional translate/summarize) call the
hosted Legba-models FastAPI service over HTTP rather than loading the
underlying transformer models in-process. This module provides the typed
async client + the stack-component handler that the filter handlers bind
against via :class:`legba.data.schemas.properties.StackRef`.

Endpoint catalog: :doc:`docs/AI_MODELS.md`.
Per-endpoint payloads: :doc:`legba-models/USAGE.md`.

Public surface:
  * :class:`NlpServiceClient` — the typed async client. Wraps ``httpx`` with
    Basic Auth, per-endpoint methods, graceful degradation, and clear
    typed errors.
  * :class:`NlpServiceUnavailable` / :class:`NlpServiceAuthError` — typed
    exceptions for handler-level routing.
"""

from __future__ import annotations

from .client import (
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
)

__all__ = [
    "NlpServiceAuthError",
    "NlpServiceClient",
    "NlpServiceUnavailable",
]
