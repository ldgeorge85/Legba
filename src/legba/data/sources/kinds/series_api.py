# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plug-in declaration: the ``series_api`` kind (S-1) — a generic numeric
series from a JSON or CSV endpoint, declared in config, landing in the
``observation`` family."""

from . import declare_source_kind

declare_source_kind("series_api", "SeriesApiSourceHandler")
