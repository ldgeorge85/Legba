-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0248_descriptor_audit_log_chain.sql
--
-- The administrative audit log becomes a hash chain (outside audit A-06 /
-- A-07, register 2026-11-09).
--
-- WHY
-- ---
-- `descriptor_audit_log` rows are individually Ed25519-signed but were not
-- chained, so a holder of database write access could delete or reorder rows
-- undetected, and a holder of the signing key could re-sign an edited row
-- (scripts/scrub_audit_actor_tokens.py did exactly that, once, by design).
-- This adds the receipt-chain convention (provenance/receipts.py:
-- receipt_hash / prev_receipt_hash, ZERO_HASH genesis) to the audit log:
--
--   * chain_seq  — the row's position in the recorded order (1, 2, ...);
--   * prev_hash  — the previous row's row_hash (ZERO_HASH for seq 1);
--   * row_hash   — sha256 over canonical JSON of the row's signed content,
--                  its signature, signer_did, id, chain_seq and prev_hash
--                  (legba.data.registry.audit_chain.row_hash_for).
--
-- The hash is computed in Python (canonical JSON is the platform's one
-- serialization) by AuditLogger.record in the SAME transaction as the
-- insert, under a transaction-scoped advisory lock so two writers can never
-- share a predecessor. Existing rows are chained by
-- scripts/audit_chain.py backfill (dry-run by default), in (occurred_at, id)
-- order, without touching their signed content. Until the backfill has run,
-- record() leaves new rows unchained too (the backfill then chains them in
-- order), so the chain never has a hole in the middle.
--
-- IMMUTABILITY (the trigger below)
-- -------------------------------
--   * Once a row's row_hash is set, chain_seq / prev_hash / row_hash never
--     change and the row can never be deleted.
--   * An unchained row may be updated ONLY to set its chain columns (the
--     backfill); its content columns must stay as they are.
--   * A chained row's CONTENT may change only when a chained `redact` event
--     naming it (change_summary.redacts_row_id) already exists — the
--     redaction is itself history (legba.data.registry.audit_chain.redact_row);
--     the original row_hash stays in the chain, and the verifier reports the
--     row as `redacted` instead of recomputing it.
-- A database owner can still disable the trigger; what the chain plus the
-- daily outside anchor (receipt_anchor) makes DETECTABLE is set out in
-- docs/ANALYSIS.md (audit-log chain).
--
-- Additive: three nullable columns (catalog-only), two small indexes on a
-- table of ~1,800 rows, one trigger. Re-apply is a no-op. No inline
-- BEGIN/COMMIT (the runner wraps the file in one transaction).

SET LOCAL lock_timeout = '10s';

ALTER TABLE public.descriptor_audit_log
    ADD COLUMN IF NOT EXISTS chain_seq BIGINT,
    ADD COLUMN IF NOT EXISTS prev_hash TEXT,
    ADD COLUMN IF NOT EXISTS row_hash  TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_descriptor_audit_log_chain_seq
    ON public.descriptor_audit_log (chain_seq)
    WHERE chain_seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_descriptor_audit_log_unchained
    ON public.descriptor_audit_log (occurred_at, id)
    WHERE row_hash IS NULL;

CREATE OR REPLACE FUNCTION public.descriptor_audit_log_chain_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.row_hash IS NOT NULL THEN
            RAISE EXCEPTION 'descriptor_audit_log: row % is chained (seq %) and cannot be deleted; append a redact event instead', OLD.id, OLD.chain_seq;
        END IF;
        RETURN OLD;
    END IF;
    -- UPDATE
    IF OLD.row_hash IS NOT NULL THEN
        IF NEW.chain_seq IS DISTINCT FROM OLD.chain_seq
           OR NEW.prev_hash IS DISTINCT FROM OLD.prev_hash
           OR NEW.row_hash IS DISTINCT FROM OLD.row_hash
           OR NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'descriptor_audit_log: chain columns of row % are insert-only', OLD.id;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.descriptor_audit_log r
             WHERE r.action = 'redact'
               AND r.row_hash IS NOT NULL
               AND r.chain_seq > OLD.chain_seq
               AND r.change_summary->>'redacts_row_id' = OLD.id::text
               AND r.change_summary->>'redacted_row_hash' = OLD.row_hash
        ) THEN
            RAISE EXCEPTION 'descriptor_audit_log: row % is chained; its content changes only after a chained redact event names it', OLD.id;
        END IF;
        RETURN NEW;
    END IF;
    -- An unchained row: only the chain columns may be set.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
       OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
       OR NEW.actor_role IS DISTINCT FROM OLD.actor_role
       OR NEW.namespace IS DISTINCT FROM OLD.namespace
       OR NEW.descriptor_id IS DISTINCT FROM OLD.descriptor_id
       OR NEW.action IS DISTINCT FROM OLD.action
       OR NEW.from_version IS DISTINCT FROM OLD.from_version
       OR NEW.to_version IS DISTINCT FROM OLD.to_version
       OR NEW.change_summary IS DISTINCT FROM OLD.change_summary
       OR NEW.signed_payload IS DISTINCT FROM OLD.signed_payload
       OR NEW.signer_did IS DISTINCT FROM OLD.signer_did THEN
        RAISE EXCEPTION 'descriptor_audit_log: an unchained row may only be chained, not edited (row %)', OLD.id;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_descriptor_audit_log_chain_guard ON public.descriptor_audit_log;
CREATE TRIGGER trg_descriptor_audit_log_chain_guard
    BEFORE UPDATE OR DELETE ON public.descriptor_audit_log
    FOR EACH ROW EXECUTE FUNCTION public.descriptor_audit_log_chain_guard();
