# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.sources.scrapers — site-specific scraper-impl modules.

Each module here exports one or more :class:`~legba.data.sources.scraper.ScraperImpl`
implementations. The :class:`ScraperSourceHandler` (L-135) loads them
from the per-target config's ``impl`` dotted path.

Currently shipped:

  * :mod:`legba.data.sources.scrapers.example_news` — generic RSS- /
    sitemap-driven news-article scraper. Demonstrates the contract; not a
    production scraper for any specific site.
"""

from __future__ import annotations

__all__: list[str] = []
