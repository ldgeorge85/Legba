# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound clients Legba uses to talk to sibling sovereignty services.

This package holds the *outbound* surface (Legba-as-caller); inbound A2A
routing for skills Legba *exposes* lives in :mod:`legba.data.outputs.a2a_skill`.

L-210 lands the :mod:`legba.clients.mnemosyne_a2a` client, which mirrors the
inbound envelope shape from ``a2a_skill.py`` for the general A2A surface and
provides a :meth:`MnemosyneA2AClient.trust_query` convenience wrapper used by
analyst tools (the L-211 trust-query tool path).
"""

from __future__ import annotations

from .mnemosyne_a2a import (
    A2ARemoteError,
    A2ASignatureError,
    A2ATransportError,
    MnemosyneA2AClient,
)

__all__ = [
    "A2ARemoteError",
    "A2ASignatureError",
    "A2ATransportError",
    "MnemosyneA2AClient",
]
