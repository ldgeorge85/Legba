-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0118_strip_telegram_widget_archived_text.sql
--
-- DATA REPAIR (2026-07-31 DQ sweep, finding R6b). Trafilatura extraction
-- against t.me embed pages produced `payload.archived_text` values that are
-- Telegram widget UI chrome ("Download\nContext\nEmbed\n…telegram-widget.js…"),
-- never message content — every telegram-source archived_text is this class.
-- The archiver now skips text-extraction for t.me hosts (the bytes archive is
-- untouched); this clears the historical pollution and re-marks the rows for
-- corpus re-projection, where `payload.text` (now a first-class best-body
-- field) takes over.

UPDATE signals
   SET payload = payload - 'archived_text',
       indexed_at = NULL,
       updated_at = now()
 WHERE source_id = 'source.telegram.org_channels'
   AND payload ? 'archived_text';
