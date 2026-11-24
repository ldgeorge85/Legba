# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plug-in declaration: the ``portwatch`` kind (IMF PortWatch daily chokepoint
transits, an ArcGIS FeatureServer query) — the first kind added without
touching the factory."""

from . import declare_source_kind

declare_source_kind("portwatch", "PortWatchSourceHandler")
