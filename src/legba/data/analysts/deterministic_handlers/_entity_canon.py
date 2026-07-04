# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RE-EXPORT SHIM — the entity canon moved to the shared ``legba.data`` layer.

The canon now lives at :mod:`legba.data._entity_canon` (W1 / remediation #1) so
ingestion, the analyst resolver, the reifier, and ``proposed_edge_governance``
all import ONE canon without a layering violation. This module is a thin
backward-compatibility shim: every public name that used to live here is
re-exported verbatim from the new location, so existing imports
(``from ._entity_canon import canonicalize_entity`` etc.) keep working
unchanged. NEW code should import from ``legba.data._entity_canon`` directly.

Do NOT add logic here — it belongs in the shared module.
"""

from __future__ import annotations

# Star-import pulls everything in the shared module's __all__ (the public API).
from legba.data._entity_canon import *  # noqa: F401,F403

# Explicit re-exports of every public name the importers / tests / the
# 0045 migration mirror rely on — pinned so a future __all__ edit can't
# silently break a downstream import through this shim.
from legba.data._entity_canon import (  # noqa: F401
    COUNTRY_CLASS,
    DEFAULT_CLASS,
    ORGANIZATION_CLASS,
    _DEMONYM_MAP,
    _JUNK_ENTITIES,
    canonicalize_entity,
    identity_fold,
    is_demonym,
    is_junk_entity,
    is_org_surface,
)

# Re-export the shared module's __all__ so ``from ...shim import *`` matches the
# canon's public surface exactly.
from legba.data._entity_canon import __all__ as __all__  # noqa: F401
