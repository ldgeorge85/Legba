# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L0 — the layer table + aperture declaration (migration 0214).

``_vocab.py`` is the closed vocabulary (a leaf, stdlib-only). ``loader.py`` is
the idempotent upsert into ``source_layers`` / ``desk_apertures``, shared by
``scripts/load_layer_map.py`` (the only writer this lane ships — no analyst,
no route, no registry-lifecycle registration). The descriptor shape itself
(``layer_map``) is validated by ``legba.data.registry.layer_map_schema``.
"""

from __future__ import annotations

from ._vocab import APERTURE_DECLARED_VOCAB, LAYER_VOCAB, is_valid_declared, is_valid_layer

__all__ = [
    "APERTURE_DECLARED_VOCAB",
    "LAYER_VOCAB",
    "is_valid_declared",
    "is_valid_layer",
]
