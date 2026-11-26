# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declares the ``stream_json`` kind: the parser a SUBSCRIBED
source's received events go through (``acquisition: subscribe``)."""

from . import declare_source_kind

declare_source_kind("stream_json", "StreamJsonSourceHandler")
