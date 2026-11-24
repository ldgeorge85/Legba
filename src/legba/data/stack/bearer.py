# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The outbound ``Authorization: Bearer`` header for a hosted stack endpoint.

A leaf module on purpose — it imports nothing of Legba's. Two readers depend on
that: the registry's credential guard (tests/data_pkg/test_audit_actor_never_token.py)
refuses any REGISTRY module that names the authorization header, because inside
the registry that string means "reading the caller's credential", while an
outbound bearer to a stack component's endpoint is a different thing; and the
registry's health checker must import this without dragging the LLM handler
package in (that package imports the registry back — a cycle).
"""
from __future__ import annotations


def bearer_headers(token: str) -> dict[str, str]:
    """``{"Authorization": "Bearer <token>"}`` for an outbound call."""
    return {"Authorization": f"Bearer {token}"}
