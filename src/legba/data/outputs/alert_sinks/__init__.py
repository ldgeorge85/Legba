# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sub-output sinks for the ``alert`` kind (L-197).

Each module here exposes a single ``send_<surface>_alert`` coroutine plus
(for optional-extra-gated transports) an ``<surface>_AVAILABLE`` boolean
so the parent kind can record graceful-skip outcomes when the extra isn't
installed.

These are *transports* — they take an AlertPayload + OutputContext +
OutputDeps + per-surface destination override, and return a
:class:`SurfaceResult`. They do not validate the payload (already done
upstream), do not parse the descriptor (also upstream), and do not retry
(that is the parent kind's job for critical-severity).
"""

from __future__ import annotations

__all__ = ["nats", "pushover", "xmpp", "matrix"]
