# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-party source kinds — the eighteen entries the closed
``_SOURCE_MODULE_TABLE`` tuple listed before N-L, declared the plug-in way, in
the same order (the order is informational; the registry is keyed by
``handler.kind``)."""

from . import declare_source_kind

for _module, _cls in (
    ("rss",            "RSSSourceHandler"),
    ("geojson",        "GeoJSONSourceHandler"),
    ("json_api",       "JsonApiSourceHandler"),
    ("email_imap",     "EmailImapSourceHandler"),
    ("generic_webhook", "GenericWebhookSourceHandler"),
    ("acled",          "ACLEDSourceHandler"),
    ("ucdp",           "UCDPSourceHandler"),
    ("mediacloud",     "MediaCloudSourceHandler"),
    ("opensanctions",  "OpenSanctionsSourceHandler"),
    ("gdelt",          "GDELTBigQuerySourceHandler"),
    ("gdelt_files",    "GDELTFilesSourceHandler"),
    ("scraper",        "ScraperSourceHandler"),
    ("firecrawl",      "FirecrawlSourceHandler"),
    ("discord",        "DiscordWebhookSourceHandler"),
    ("telegram",       "TelegramChannelSourceHandler"),
    ("common_crawl",   "CommonCrawlNewsSourceHandler"),
    ("intelmq",        "IntelMQCollectorBridge"),
    ("instrument_snapshot", "InstrumentSnapshotSourceHandler"),
):
    declare_source_kind(_module, _cls)
