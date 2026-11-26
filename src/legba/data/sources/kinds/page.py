# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plug-in declaration: the ``page`` kind — one web page, fetched
and landed as one document; the kind the ingestion intake runs an address
under (``descriptors/source_page_pipeline_default.yaml``)."""

from . import declare_source_kind

declare_source_kind("page", "PageSourceHandler")
