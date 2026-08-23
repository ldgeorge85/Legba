-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0186_analyst_traces_prompt_sha256.sql — RUST-5, prompt_rendered wiring.
--
-- ``analyst_traces.prompt_rendered`` has been declared (0001 baseline) and
-- NULL on every row ever, all-time — designed, never wired. The decision on
-- record is WIRE IT (observability won); the writer now caps the persisted
-- text at a bounded length with an explicit truncation marker (see
-- run_accounting.py's ``_MAX_PROMPT_RENDERED_CHARS``). This column carries
-- the sha256 of the FULL, untruncated prompt ALONGSIDE the possibly-capped
-- ``prompt_rendered`` text, so a truncated row is still byte-verifiable
-- against a re-rendered prompt — the claim ``scripts/render_prompt_pack.py``
-- depends on.
--
-- Deliberately NOT part of ``compute_receipt_hash``'s payload — same posture
-- as ``llm_calls`` / ``tool_calls``: supplementary provenance, not chain
-- material. CREATE-only per the migration policy in migrations/__init__.py.

ALTER TABLE public.analyst_traces
    ADD COLUMN IF NOT EXISTS prompt_sha256 text;

COMMENT ON COLUMN public.analyst_traces.prompt_sha256 IS
    'sha256 of the FULL, untruncated rendered prompt (RUST-5) — recorded '
    'alongside prompt_rendered, which may be capped with an explicit '
    'truncation marker. NOT part of compute_receipt_hash''s payload (same '
    'posture as llm_calls/tool_calls: supplementary provenance, not chain '
    'material). NULL for every trace written before this migration and for '
    'any deterministic (no-LLM) run.';
