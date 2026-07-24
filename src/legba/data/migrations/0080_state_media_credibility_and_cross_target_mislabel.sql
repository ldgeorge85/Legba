-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0080_state_media_credibility_and_cross_target_mislabel.sql
--   (2026-07-06 live audit — M9 state-media source_credibility NULL +
--    M15 cross-target unit-finding mislabel close)
--
-- ===========================================================================
-- PART A (M9) — seed source_credibility for un-scored state / social media.
-- ===========================================================================
-- PROBLEM: presstv / irna / telegram / ukrinform have NO source_credibility row,
--   so their signals (and the facts derived from them) fall back to the INGESTION
--   NOMINAL 0.5 — which is HIGHER than the 0.3 state-affiliation penalty a seeded
--   peer carries (tehrantimes.com=0.3, tass.com=0.3). Net effect: un-scored
--   Iranian state media (Press TV, IRNA) OUTRANKS its own seeded peer tehrantimes.
--   The credibility table keys on the URL HOST (source_credibility.source_host);
--   the write-path host lookup (source_actor.lookup_source_credibility →
--   filters.source_credibility.extract_lookup_hosts) probes the exact host then
--   progressively-trimmed parents, so a row keyed on the registrable domain
--   matches every subdomain of that outlet.
--
-- PAIRED CODE FIX: none required — the lookup path already backfills any host that
--   HAS a row. This migration only adds the missing rows (the fix is the data).
--
-- HOSTS (resolved LIVE from signals.canonical_url; the lookup trims subdomains so
--   the registrable domain matches — e.g. en.irna.ir → irna.ir):
--     source.presstv.english        → presstv.ir      (1936 signals)
--     source.irna.english           → en.irna.ir      (196 signals; → irna.ir)
--     source.ukrinform.english      → www.ukrinform.net (201 signals; → ukrinform.net)
--     source.telegram.org_channels  → t.me            (7164 signals)
--
-- VOCABULARY matches the existing tehrantimes/tass rows EXACTLY: tier ∈
--   (wire|gov|aggregator|thinktank|social); state_affiliation is a BOOLEAN column
--   (NOT a tier). State news wires get tier='wire' + state_affiliation=true, scored
--   at/under the seeded state-media band (tehrantimes/tass=0.3); Telegram public
--   channels get tier='social' (unverified broadcast), well under the 0.5 nominal.
--     presstv.ir     0.25 wire   state (Press TV — IRIB English; overt state propaganda, < tehrantimes)
--     irna.ir        0.30 wire   state (IRNA — Iranian state news agency; == tehrantimes band)
--     ukrinform.net  0.45 wire   state (Ukrinform — Ukrainian state agency; wartime official-position wire)
--     t.me           0.30 social       (Telegram public channels — unverified user/org broadcast)
--
-- ===========================================================================
-- PART B (M15) — close a cross-target-leaked leadership_transition head.
-- ===========================================================================
-- PROBLEM: a per-country UNIT finding occasionally describes the WRONG country. The
--   live case: the Turkey (country_g20_tr) leadership_transition head
--   6b4feccb-8d30-4c40-a1f5-0e348bb62f8c was TITLED + BODIED entirely "Romania"
--   (BLUF: "Near-term change in ROMANIA's head of government…"). A Turkey desk
--   product about Romania is a mislabel, not intelligence.
--
-- PAIRED CODE FIX (already in this bundle, live on rebuild): a cross-target guard
--   in the verify pass (verify.py cross_target_leak_span, wired via
--   actor_critic.verify_inline_target_finding) FLAGS a per-country finding that
--   names ONLY other countries than its desk target → demotes effective_confidence
--   via the min(confidence, faithfulness) gate. Conservative: flag, never delete.
--
-- THIS MIGRATION closes any REMAINING live leadership_transition finding for
--   country_g20_tr whose title/body names Romania (and NOT its own Turkey) as
--   superseded_by the CURRENT correct live Turkey head — mirroring a
--   finding_supersessions audit edge (produced_by='migration_0080'). The specific
--   6b4feccb row ALREADY self-superseded (2026-07-06 08:40Z, → 8674092b "Turkey –
--   low leadership transition risk"), so at write-time this matches 0 rows — a
--   clean no-op that documents the intent and catches a same-shape re-emission
--   between measurement and apply. The superseding head is resolved LIVE as the
--   latest live TR leadership_transition finding that names Turkey (never a Romania
--   row), so it is always the clean head.
--
-- REVERSIBLE (NO row deleted):
--   -- reverse PART B:
--   UPDATE analyst_outputs o
--      SET superseded_by = NULLIF(o.data->>'_m0080_prior_superseded_by','')::uuid,
--          superseded_at = NULL,
--          data = o.data - '_m0080_prior_superseded_by'
--    WHERE o.data ? '_m0080_prior_superseded_by';
--   DELETE FROM finding_supersessions WHERE produced_by = 'migration_0080';
--   -- reverse PART A:
--   DELETE FROM source_credibility WHERE scored_by = 'migration.0080';
--
-- IDEMPOTENT: PART A is ON CONFLICT (source_host) DO NOTHING (a re-run, or a host
--   an operator already scored, is untouched). PART B's UPDATE guards on
--   superseded_by IS NULL + the absence of the stash key (a re-run finds nothing
--   live), and the edge INSERT is ON CONFLICT DO NOTHING. On a fresh substrate both
--   parts match 0 rows — a clean no-op. Routed through the migration runner (ONE
--   txn + ledger; NO inline BEGIN/COMMIT, matching 0074/0079).
--
-- MEASURED (live `legba`, 2026-07-06, migration head 0079):
--   PART A: 4 rows inserted (0 pre-existing for the 4 hosts) — covering 9,497 live
--     signals now on the 0.5 nominal (t.me 7164, presstv.ir 1936, ukrinform.net
--     201, en.irna.ir 196).
--   PART B: 0 rows (6b4feccb already self-superseded → 8674092b at 2026-07-06
--     08:40Z); statement retained as a guarded no-op / recurrence catch.

-- PART A (M9) — seed the four missing source_credibility rows.
INSERT INTO source_credibility
    (source_host, score, score_rationale, tier, state_affiliation, scored_by)
VALUES
    ('presstv.ir', 0.25,
     'Iranian state broadcaster (IRIB Press TV, English); overt state-propaganda outlet — treat as official-position signal, below its state-media peers.',
     'wire', true, 'migration.0080'),
    ('irna.ir', 0.30,
     'Islamic Republic News Agency (IRNA) — Iranian state news agency; conduit for official positions (peer of tehrantimes/tass).',
     'wire', true, 'migration.0080'),
    ('ukrinform.net', 0.45,
     'Ukrinform — Ukrainian national (state-owned) news agency; wartime official-position wire.',
     'wire', true, 'migration.0080'),
    ('t.me', 0.30,
     'Telegram public channels — unverified user/organization-run broadcast; treat as low-credibility social signal (well below the ingestion nominal).',
     'social', false, 'migration.0080')
ON CONFLICT (source_host) DO NOTHING;

-- PART B (M15) — close any live cross-target-leaked Turkey/Romania head under the
--   current correct live Turkey head (stash prior superseded_by for reversibility).
WITH head AS (
    SELECT id
    FROM analyst_outputs
    WHERE analyst_id = 'leadership_transition'
      AND target_id = 'country_g20_tr'
      AND kind = 'finding'
      AND superseded_by IS NULL
      AND (title ILIKE '%turk%' OR body ILIKE '%turk%')
      AND title NOT ILIKE '%romania%'
    ORDER BY produced_at DESC, id DESC
    LIMIT 1
)
UPDATE analyst_outputs o
SET superseded_by = h.id,
    superseded_at = COALESCE(o.superseded_at, now()),
    data = jsonb_set(
        COALESCE(o.data, '{}'::jsonb),
        '{_m0080_prior_superseded_by}',
        to_jsonb(COALESCE(o.superseded_by::text, '')), true
    )
FROM head h
WHERE o.analyst_id = 'leadership_transition'
  AND o.target_id = 'country_g20_tr'
  AND o.kind = 'finding'
  AND o.superseded_by IS NULL
  AND o.id <> h.id
  AND (o.title ILIKE '%romania%' OR o.body ILIKE '%romania%')
  AND o.title NOT ILIKE '%turk%'
  AND NOT (COALESCE(o.data, '{}'::jsonb) ? '_m0080_prior_superseded_by');

-- PART B' — mirror the live path's finding_supersessions audit edge for the close.
WITH head AS (
    SELECT id
    FROM analyst_outputs
    WHERE analyst_id = 'leadership_transition'
      AND target_id = 'country_g20_tr'
      AND kind = 'finding'
      AND superseded_by IS NULL
      AND (title ILIKE '%turk%' OR body ILIKE '%turk%')
      AND title NOT ILIKE '%romania%'
    ORDER BY produced_at DESC, id DESC
    LIMIT 1
)
INSERT INTO finding_supersessions
    (superseded_finding_id, superseding_finding_id,
     situation_signature, reason, score, produced_by)
SELECT o.id, h.id,
       'sig:country_g20_tr',
       'cross-target mislabel close (M15 — desk Turkey, finding about Romania)',
       1.0,
       'migration_0080'
FROM analyst_outputs o, head h
WHERE o.analyst_id = 'leadership_transition'
  AND o.target_id = 'country_g20_tr'
  AND o.kind = 'finding'
  AND o.superseded_by = h.id
  AND o.id <> h.id
  AND (o.title ILIKE '%romania%' OR o.body ILIKE '%romania%')
  AND o.title NOT ILIKE '%turk%'
ON CONFLICT (superseded_finding_id, superseding_finding_id) DO NOTHING;
