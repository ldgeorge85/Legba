-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0258_claim_host_class.sql
--
-- THE CONTRARY PASS KNOWS A CLAIM HOST (C-1).
--
-- WHY
-- ---
-- C-1 adds `claim` to the source-class taxonomy (`SourceScope.source_class`):
-- a source whose content is a party's own claim. The contrary pass's
-- `host_class` reuses that taxonomy (`_contrary_fences.HOST_CLASSES`), and a
-- page fetched from a claim source's host is labelled `claim` and fenced at
-- F1 like a reference host: it counts toward nothing (a claim cannot
-- contradict, or support, a finding's account of an event). The stored
-- `claim_contentions.host_class` CHECK (0222) enumerates the vocabulary, so
-- it must admit the new word or the row's insert fails.
--
-- WHAT THIS DOES
-- --------------
-- Replaces `ck_claim_contentions_host_class_vocab` with the same list plus
-- `claim`, NOT VALID (existing rows are unchanged and all satisfy the old,
-- narrower list).
--
-- SAFETY
-- ------
-- Idempotent (drop-if-exists, then add), routed through the runner (no inline
-- BEGIN/COMMIT). No data change.

ALTER TABLE public.claim_contentions
    DROP CONSTRAINT IF EXISTS ck_claim_contentions_host_class_vocab;

ALTER TABLE public.claim_contentions
    ADD CONSTRAINT ck_claim_contentions_host_class_vocab
    CHECK (
        host_class IS NULL
        OR host_class IN ('reference', 'reporting', 'analysis',
                          'official', 'state_media', 'claim', 'unknown')
    ) NOT VALID;
