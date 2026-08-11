-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0185_repoint_proposed_edges_onto_merge_keepers.sql  (was 0181, renumbered
-- 0183, DEFERRED 2026-08-05 mid-train, reworked here as the promised fixpoint.
-- Six live-only collision shapes were found and fixed one by one on the 08-05
-- train — temp_buffers, quadratic election, plan-to-plan convergence, namesake
-- occupancy, write ordering, namesake fan-out — and the SEVENTH forced the
-- deferral. This file incorporates all seven.)
--
-- W3-A step 2 (data half) — repair the `proposed_edges` rows that name a merged
-- TOMBSTONE. The code half is `_repoint_proposed_edges` in
-- `deterministic_handlers/entity_gc.py`, which stops the class recurring; this
-- file clears the backlog that accumulated while the sweep did not exist.
--
-- ── the defect ─────────────────────────────────────────────────────────────
-- `_compact_merged_edges` has re-pointed `nexuses` and `facts` onto the merge
-- keeper since it was written, and has NEVER touched `proposed_edges`. There is
-- no other repoint path for that table (0143's header says so in as many
-- words), so every candidate naming a merged loser keeps naming a tombstone
-- permanently.
--
-- The cost is a RE-SPEND, not just untidiness. `uq_proposed_edges_triple` keys
-- the queue on (lower(source_entity), lower(target_entity), relationship_type),
-- so one real pair occupies the queue TWICE — once under the loser's surface
-- and once under the keeper's — and both copies get accrued by
-- `entity_resolution`, qualified by `edge_qualification` and typed by the
-- reifier. The duplicate is invisible precisely because the key is a NAME.
--
-- ── measured (live substrate, 2026-08-10, read-only replica of the data) ───
--   rows naming a mapped tombstone name    12,935
--     already on their final surface        1,060   -> untouched (fix 6b)
--     self-loops once re-pointed               61   -> `rejected`
--     collide with an existing row          3,205   -> fold + `merged`
--     demoted by the fixpoint (fix 7)           2   -> fold + `merged`
--     re-point cleanly                      8,607   -> endpoints rewritten
--   scratch replay: 2.7 s end-to-end; fixpoint converged in 2 passes; a
--   second and third run moved 0 rows and left a byte-identical table
--   (modulo reviewed_at re-stamps on the perpetual audit stayers).
--
-- ── outcomes, and why each is what it is ───────────────────────────────────
-- Exactly the three the code path takes, so a row repaired here and a row
-- repaired by the sweep tomorrow end up in the same state:
--   1. SELF-LOOP — both endpoints resolve to the same keeper. `rejected`, the
--      same verdict `proposed_edge_governance` gives a self-referential
--      candidate. The queue and the promoter must not disagree.
--   2. COLLISION — a keeper-named row already holds this triple. The loser
--      row's evidence is FOLDED onto that survivor first (confidence maxed,
--      `derived_from` unioned, an empty evidence_text filled, and `pending`
--      winning over any terminal status so an edge still in play stays in
--      play), and only then is the loser row marked `merged`. Folding before
--      marking is what keeps the merge from costing a citation. Fix 7 below
--      adds one more route into this outcome: a mover DEMOTED by the fixpoint
--      loop folds onto the row occupying its destination, exactly as the
--      sweep would have folded it had the occupant been visible up front.
--   3. CLEAN — the endpoints are rewritten to the keeper surface.
--
-- `merged` is a terminal status outside the governance `pending` work-set,
-- added exactly as `orphaned` was: the row is retained for audit, never
-- deleted, and it stops being re-counted every hour.
--
-- NOTHING IS DELETED. Every row survives with a status that says what happened
-- to it, which is what makes this reversible by inspection.
--
-- ── fix 7 — the seventh collision shape, and why it needs a FIXPOINT ───────
-- A stayer is a plan row that never moves: a rejected SELF-LOOP keeps its old
-- triple forever (outcome 1 rewrites nothing), and a folded LOSER keeps its
-- old tombstone-named triple forever (outcome 2 rewrites nothing). The
-- election (fix 4) sees a plan row only at its DESTINATION — never at the
-- triple it currently occupies — so a mover whose destination is a stayer's
-- CURRENT triple is elected keeper of an "empty" group and then lands on an
-- occupied one: uq_proposed_edges_triple, the failure that deferred 0183.
-- Live shape, caught on the 2026-08-05 apply: (Democratic Republic of the
-- Congo, The Democratic Republic of Congo, co_occurs) rewrites its source to
-- keeper surface "DR Congo" and lands on (dr congo, the democratic republic
-- of congo, co_occurs) — occupied by a sibling plan row that step 1 just
-- rejected as a self-loop and left in place. This shape exists at all only
-- because of NAMESAKES: a mover's destination is made of living keeper
-- surfaces, so the stayer occupying it must carry a tombstone name whose
-- lower-case surface a living keeper ALSO carries.
--
-- A single pass cannot fix this, because it is CIRCULAR: whether a row is a
-- stayer depends on the election, and who collides depends on who is a
-- stayer. Demoting a colliding mover (fold onto the occupant, mark `merged`)
-- turns the mover itself into a stayer at ITS old triple — which may be some
-- other mover's destination. So the mover set is computed to CLOSURE before
-- anything is written:
--
--     movers_0 = plan rows that are neither self-loops nor losers
--     repeat: demote every mover whose destination triple is currently
--             occupied by a row OUTSIDE the mover set; those rows leave the
--             mover set and become stayers
--     until no mover is demoted
--
-- TERMINATION: the mover set only ever shrinks, and each pass either demotes
-- at least one mover or exits, so the loop runs at most |movers_0| + 1
-- passes. A guard raises at |movers_0| + 2 — unreachable unless the loop
-- body itself is wrong, and loud if it ever is. The loop is pure scratch-
-- table computation (one indexed set-based join per pass, no row-by-row
-- work); all physical writes happen once, after closure.
--
-- WHY THE END STATE IS COLLISION-FREE: at closure, (a) every surviving
-- mover's destination is occupied by no row outside the mover set, (b) two
-- movers never share a destination — the election hands each destination
-- group to exactly one keeper, asserted below — and (c) phase A parks every
-- mover on an id-keyed triple before phase B lands any of them, so a mover
-- occupying another mover's destination has always vacated before landing.
-- Preflight asserts fail loud, printing the offending key, if the data ever
-- violates what (a)–(c) rest on.
--
-- Demotion folds use the mover row's LIVE values (not plan-time captures):
-- by demotion time the mover may itself have collected its own group's
-- losers in step 2, and those citations must reach the occupant too.
-- Evidence propagates ONE HOP per fold, exactly like consecutive runs of the
-- code-half sweep: in a demotion chain M2 -> M1 -> S, M2's evidence rests on
-- M1 (a retained audit row), not on S. The pending-wins status rule is kept
-- deliberately: a stayer's triple is made of surfaces a living keeper
-- carries (see above), so a pending mover demoted onto a terminal stayer
-- legitimately revives that triple — the queue row is the candidate for the
-- LIVING reading of the name, and the sweep/promoter own it from there.
--
-- SAFETY (idempotent, forward-only): a second run finds the same stayers
-- (still naming tombstones, by design), re-elects the same survivors, folds
-- no new evidence (GREATEST / DISTINCT-union / fill-if-empty are all
-- idempotent), demotes the same movers onto the same occupants, and moves
-- zero rows. The runner wraps this file in its own transaction.

DO $$
DECLARE
    v_total int; v_self int; v_selfset int; v_folded int; v_clean int;
    v_movers int; v_demoted int; v_parked int; v_residual int;
    v_iter int := 0; v_pass int; v_cap int;
    v_bad text;
BEGIN

-- ---------------------------------------------------------------------------
-- The repoint plan. Built once: a row can name a tombstone on EITHER endpoint
-- (or both), and every downstream branch needs the same resolved surface.
-- ---------------------------------------------------------------------------
-- 2026-08-05 live-apply fix: the staging tables are UNLOGGED SCRATCH tables,
-- not TEMP — the temp pool (temp_buffers, 8MB default) overran on the live
-- volume, and raising it mid-session is forbidden once any temp table was
-- touched (which fixture sessions have). Unlogged tables ride shared_buffers,
-- are transactional for content, and are dropped explicitly at the end.
-- 2026-08-05 fix 6 — THE ROOT of every collision shape: canonical_name is NOT
-- unique among tombstones ("the Democratic Republic of Congo" has THREE
-- tombstoned namesakes live; 2,039 proposed_edges rows joined more than one),
-- so the original per-endpoint joins FANNED OUT — one row, multiple plan
-- incarnations, divergent destinations, incoherent election. Each NAME now
-- resolves exactly once, deterministically (lowest keeper id among namesake
-- chains), and a hard assert makes any future fan-out fail by NAME instead of
-- as a unique-violation five steps downstream.
CREATE UNLOGGED TABLE public._mig0185_rp_names AS
SELECT DISTINCT ON (t.canonical_name)
       t.canonical_name AS tname,
       k.canonical_name AS keeper_name
  FROM public.entity_profiles t
  JOIN public.entity_profiles k
    ON k.id = public.resolve_entity(t.merged_into)
   AND k.merged_into IS NULL
 WHERE t.merged_into IS NOT NULL
 ORDER BY t.canonical_name, k.id ASC;
CREATE UNIQUE INDEX ON public._mig0185_rp_names (tname);

-- Fix 6b (found by the scratch idempotence replay of THIS rework): the map
-- above is ONE HOP, and a keeper surface can itself be some OTHER tombstone's
-- exact name (namesake chains — 1,581 rows re-planned on the replay's second
-- run because their run-1 landing surface was still a mapped name). Close the
-- map TRANSITIVELY so every row lands on its final surface in one run: each
-- pass substitutes mapping targets that are themselves mapped names
-- (path-doubling, so passes are O(log chain length)); a 2-cycle
-- (A -> B, B -> A) self-resolves to identity in one pass and is dropped with
-- the other identities below. The bound is generous — 32 doublings covers a
-- chain of 2^32 names. An ODD-length mapping cycle (three cross-class
-- namesake names whose merge chains map cyclically) rotates under doubling
-- instead of collapsing and trips the guard: deliberate — data that
-- pathological gets a loud rollback naming the offender, not a guess.
v_iter := 0;
LOOP
    v_iter := v_iter + 1;
    IF v_iter > 32 THEN
        SELECT n.tname INTO v_bad
          FROM public._mig0185_rp_names n
          JOIN public._mig0185_rp_names n2 ON n2.tname = n.keeper_name
         WHERE n2.keeper_name <> n.keeper_name
         LIMIT 1;
        RAISE EXCEPTION '0185: name-mapping closure did not converge in 32 passes — offending name %', v_bad;
    END IF;
    UPDATE public._mig0185_rp_names n
       SET keeper_name = n2.keeper_name
      FROM public._mig0185_rp_names n2
     WHERE n2.tname = n.keeper_name
       AND n2.keeper_name <> n.keeper_name;
    GET DIAGNOSTICS v_pass = ROW_COUNT;
    EXIT WHEN v_pass = 0;
END LOOP;
v_iter := 0;

-- An identity mapping (tname = final keeper surface, exact case) rewrites
-- nothing: the row already carries the one surface it would be sent to.
-- Dropping it keeps such rows out of the plan entirely, so a re-run stops
-- re-parking and re-landing them as no-op movers.
DELETE FROM public._mig0185_rp_names WHERE tname = keeper_name;

CREATE UNLOGGED TABLE public._mig0185_rp_plan AS
SELECT pe.id, pe.status, pe.relationship_type, pe.confidence,
       pe.evidence_text, pe.derived_from,
       pe.source_entity, pe.target_entity,
       COALESCE(ns.keeper_name, pe.source_entity) AS new_src,
       COALESCE(nt.keeper_name, pe.target_entity) AS new_tgt
  FROM public.proposed_edges pe
  LEFT JOIN public._mig0185_rp_names ns ON ns.tname = pe.source_entity
  LEFT JOIN public._mig0185_rp_names nt ON nt.tname = pe.target_entity
 WHERE ns.tname IS NOT NULL OR nt.tname IS NOT NULL;

-- ---------------------------------------------------------------------------
-- PREFLIGHT. Everything the fixpoint's collision-free argument rests on is
-- asserted here, before the first write, and every assert prints the
-- offending key so a failure on the live volume is diagnosable from the log.
-- ---------------------------------------------------------------------------
IF EXISTS (SELECT 1 FROM public._mig0185_rp_plan GROUP BY id HAVING count(*) > 1) THEN
    RAISE EXCEPTION '0185: plan fan-out — a proposed_edges row resolved to more than one destination';
END IF;

-- Phase A parks movers under an id-keyed \x01 namespace; a pre-existing row
-- already inside that namespace would make parking ambiguous and phase B's
-- "nothing left parked" postcondition meaningless.
SELECT pe.id::text INTO v_bad
  FROM public.proposed_edges pe
 WHERE starts_with(pe.source_entity, '\x01mig') OR starts_with(pe.target_entity, '\x01')
 LIMIT 1;
IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION '0185: preflight — proposed_edges row % already occupies the \x01 parking namespace; investigate before applying', v_bad;
END IF;

-- ---------------------------------------------------------------------------
-- 1. Self-loops. Rejected, never re-pointed: an entity is not related to
--    itself, and the promoter already rejects this shape on sight.
-- ---------------------------------------------------------------------------
UPDATE public.proposed_edges pe
   SET status = 'rejected', reviewed_at = now()
  FROM public._mig0185_rp_plan p
 WHERE pe.id = p.id
   AND lower(p.new_src) = lower(p.new_tgt)
   AND pe.status <> 'rejected';
GET DIAGNOSTICS v_self = ROW_COUNT;

-- ---------------------------------------------------------------------------
-- 2. Collisions. Elect ONE survivor per destination triple, fold every loser's
--    evidence onto it, then mark the losers `merged`.
--
--    The survivor election has to be deterministic and it has to prefer a row
--    that is NOT itself in the plan, because a plan row is about to be
--    rewritten: `in_plan ASC` puts the settled keeper-named row first, then
--    `pending` (still workable) ahead of terminal rows, then the oldest id as
--    the tie-break so a re-run elects the same survivor.
-- ---------------------------------------------------------------------------
-- 2026-08-05 live-apply fix: the original per-row keeper lookup searched only
-- rows ALREADY CARRYING the destination triple, so two plan rows converging on
-- the same FRESH triple (drc -> "dr congo" and dr. congo -> "dr congo", both
-- x djugu territory/co_occurs on the live volume) elected no keeper, both fell
-- to the "clean remainder", and the second rewrite hit
-- uq_proposed_edges_triple. Survivors are now elected ONCE PER DESTINATION
-- TRIPLE over BOTH populations — rows already at the triple AND fellow plan
-- rows projected onto it — so a convergence group has exactly one writer.
-- Losers keep their tombstone-named triples (no collision) and fold as before.
CREATE UNLOGGED TABLE public._mig0185_rp_groups AS
SELECT DISTINCT lower(new_src) AS gs, lower(new_tgt) AS gt,
       relationship_type AS rt
  FROM public._mig0185_rp_plan
 WHERE lower(new_src) <> lower(new_tgt);

-- 2026-08-05 fix 3: the correlated per-group subquery went QUADRATIC on the
-- live volume (254k proposed_edges x thousands of groups, unindexed scratch —
-- 15 min on CPU with no end in sight; cancelled). Set-based instead: one
-- indexed join for rows already at a group triple (uq_proposed_edges_triple's
-- expression matches exactly), one scan for plan-projected rows, one
-- DISTINCT ON to elect. Same election order, minutes -> seconds.
CREATE INDEX ON public._mig0185_rp_plan (id);
ANALYZE public._mig0185_rp_plan;

-- 2026-08-05 fix 4 (the duplicate-namesake class): a tombstone whose KEEPER
-- CARRIES THE SAME NAME ("the Middle East" x2 — one tombstoned, one keeper,
-- W3-A's own cross-class-duplicate finding) makes a plan row a NO-OP MOVER:
-- already sitting at its destination triple. If the election hands the
-- keepership to a pending sibling instead, the occupant stays put as a
-- "merged" loser AND the new keeper rewrites onto the occupied triple —
-- uq_proposed_edges_triple, third collision shape. OCCUPANCY NOW OUTRANKS
-- EVERYTHING: a candidate already at the destination triple wins, everyone
-- else folds onto it. An occupant-keeper's own step-3 rewrite is a same-row,
-- same-lower-triple write, which the constraint permits.
CREATE UNLOGGED TABLE public._mig0185_rp_cand AS
SELECT g.gs, g.gt, g.rt, o.id, false AS in_plan, o.status,
       true AS at_dest
  FROM public._mig0185_rp_groups g
  JOIN public.proposed_edges o
    ON lower(o.source_entity) = g.gs
   AND lower(o.target_entity) = g.gt
   AND o.relationship_type = g.rt
  LEFT JOIN public._mig0185_rp_plan op2 ON op2.id = o.id
 WHERE op2.id IS NULL
UNION ALL
SELECT lower(p.new_src), lower(p.new_tgt), p.relationship_type,
       p.id, true, p.status,
       (lower(p.source_entity) = lower(p.new_src)
        AND lower(p.target_entity) = lower(p.new_tgt)) AS at_dest
  FROM public._mig0185_rp_plan p
 WHERE lower(p.new_src) <> lower(p.new_tgt);

CREATE UNLOGGED TABLE public._mig0185_rp_keep AS
SELECT DISTINCT ON (gs, gt, rt) gs, gt, rt, id AS keep_id
  FROM public._mig0185_rp_cand
 ORDER BY gs, gt, rt, at_dest DESC, in_plan ASC,
          (status = 'pending') DESC, id ASC;

CREATE UNLOGGED TABLE public._mig0185_rp_target AS
SELECT p.id AS loser_id, p.confidence, p.evidence_text, p.derived_from,
       p.status AS loser_status, k.keep_id
  FROM public._mig0185_rp_plan p
  JOIN public._mig0185_rp_keep k
    ON k.gs = lower(p.new_src) AND k.gt = lower(p.new_tgt)
   AND k.rt = p.relationship_type
 WHERE lower(p.new_src) <> lower(p.new_tgt)
   AND k.keep_id IS NOT NULL
   AND k.keep_id <> p.id;

-- Fold the evidence onto the survivor. Aggregated per survivor so a survivor
-- collecting several losers is written exactly once.
--
-- The lineage union gets its OWN aggregate over an unnested set rather than an
-- `array_agg` of `uuid[]`: Postgres arrays are multidimensional, not arrays of
-- arrays, so aggregating uuid[] either flattens (making a nested `unnest`
-- undefined) or raises outright on ragged lengths. 0144 unions the same way.
UPDATE public.proposed_edges pe
   SET confidence    = GREATEST(pe.confidence, agg.conf),
       derived_from  = COALESCE((SELECT array_agg(DISTINCT e)
                        FROM unnest(pe.derived_from
                                    || COALESCE(dv.derv, '{}'::uuid[])) e),
                       '{}'::uuid[]),
       evidence_text = CASE WHEN pe.evidence_text = ''
                            THEN agg.evidence ELSE pe.evidence_text END,
       status        = CASE WHEN pe.status = 'pending' OR agg.any_pending
                            THEN 'pending' ELSE pe.status END
  FROM (
      SELECT t.keep_id,
             max(t.confidence)                              AS conf,
             COALESCE(max(NULLIF(t.evidence_text, '')), '') AS evidence,
             bool_or(t.loser_status = 'pending')            AS any_pending
        FROM public._mig0185_rp_target t GROUP BY t.keep_id
  ) agg
  LEFT JOIN (
      SELECT t.keep_id, array_agg(DISTINCT e) AS derv
        FROM public._mig0185_rp_target t, unnest(t.derived_from) e
       GROUP BY t.keep_id
  ) dv ON dv.keep_id = agg.keep_id
 WHERE pe.id = agg.keep_id;

UPDATE public.proposed_edges pe
   SET status = 'merged', reviewed_at = now()
  FROM public._mig0185_rp_target t
 WHERE pe.id = t.loser_id;
GET DIAGNOSTICS v_folded = ROW_COUNT;

-- ---------------------------------------------------------------------------
-- 2b. Fix 7 — the FIXPOINT. Compute the mover set to closure before any
--     endpoint is rewritten (design rationale in the header).
-- ---------------------------------------------------------------------------
CREATE UNLOGGED TABLE public._mig0185_rp_movers AS
SELECT p.id, lower(p.new_src) AS ds, lower(p.new_tgt) AS dt,
       p.relationship_type AS rt
  FROM public._mig0185_rp_plan p
 WHERE lower(p.new_src) <> lower(p.new_tgt)
   AND NOT EXISTS (SELECT 1 FROM public._mig0185_rp_target t
                    WHERE t.loser_id = p.id);
CREATE UNIQUE INDEX ON public._mig0185_rp_movers (id);
ANALYZE public._mig0185_rp_movers;

-- Loop assumption (b): one writer per destination triple. Guaranteed by the
-- DISTINCT ON election; asserted anyway, by key, because phase B's
-- correctness dies silently without it.
SELECT m.ds || ' -> ' || m.dt || ' [' || m.rt || ']' INTO v_bad
  FROM public._mig0185_rp_movers m
 GROUP BY m.ds, m.dt, m.rt HAVING count(*) > 1
 LIMIT 1;
IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION '0185: two movers share destination triple (%) — the election must hand each destination to exactly one writer', v_bad;
END IF;

CREATE UNLOGGED TABLE public._mig0185_rp_demoted (
    mover_id        uuid PRIMARY KEY,
    occupant_id     uuid NOT NULL,
    demoted_in_pass int  NOT NULL
);

SELECT count(*) INTO v_movers FROM public._mig0185_rp_movers;
v_cap := v_movers + 2;   -- unreachable: each pass demotes >= 1 mover or exits

LOOP
    v_iter := v_iter + 1;
    IF v_iter > v_cap THEN
        RAISE EXCEPTION '0185: fixpoint did not converge after % passes over % movers — the mover set can only shrink, so the loop body itself is wrong', v_iter, v_movers;
    END IF;

    -- Demote every mover whose destination triple is currently occupied by a
    -- row outside the mover set. Current triples are untouched until phase A,
    -- so occupancy only ever changes here, by demotion. `occ.id <> m.id`
    -- keeps case-only movers (already at their destination surface) movers:
    -- their phase-B write is same-row, same-lower-triple, which the
    -- constraint permits.
    INSERT INTO public._mig0185_rp_demoted (mover_id, occupant_id, demoted_in_pass)
    SELECT m.id, occ.id, v_iter
      FROM public._mig0185_rp_movers m
      JOIN public.proposed_edges occ
        ON lower(occ.source_entity) = m.ds
       AND lower(occ.target_entity) = m.dt
       AND occ.relationship_type    = m.rt
       AND occ.id <> m.id
      LEFT JOIN public._mig0185_rp_movers om ON om.id = occ.id
     WHERE om.id IS NULL;
    GET DIAGNOSTICS v_pass = ROW_COUNT;

    EXIT WHEN v_pass = 0;

    DELETE FROM public._mig0185_rp_movers m
     USING public._mig0185_rp_demoted d
     WHERE d.mover_id = m.id;

    RAISE NOTICE '0185 fixpoint pass %: % mover(s) demoted onto stayer-occupied triples', v_iter, v_pass;
END LOOP;

SELECT count(*) INTO v_demoted FROM public._mig0185_rp_demoted;

-- Fold each demoted mover onto its occupant — LIVE values, one write per
-- occupant (rationale in the header) — then retire it exactly like a step-2
-- loser: fold FIRST, mark second, or the demotion costs a citation.
UPDATE public.proposed_edges pe
   SET confidence    = GREATEST(pe.confidence, agg.conf),
       derived_from  = COALESCE((SELECT array_agg(DISTINCT e)
                        FROM unnest(pe.derived_from
                                    || COALESCE(dv.derv, '{}'::uuid[])) e),
                       '{}'::uuid[]),
       evidence_text = CASE WHEN pe.evidence_text = ''
                            THEN agg.evidence ELSE pe.evidence_text END,
       status        = CASE WHEN pe.status = 'pending' OR agg.any_pending
                            THEN 'pending' ELSE pe.status END
  FROM (
      SELECT d.occupant_id,
             max(src.confidence)                              AS conf,
             COALESCE(max(NULLIF(src.evidence_text, '')), '') AS evidence,
             bool_or(src.status = 'pending')                  AS any_pending
        FROM public._mig0185_rp_demoted d
        JOIN public.proposed_edges src ON src.id = d.mover_id
       GROUP BY d.occupant_id
  ) agg
  LEFT JOIN (
      SELECT d.occupant_id, array_agg(DISTINCT e) AS derv
        FROM public._mig0185_rp_demoted d
        JOIN public.proposed_edges src ON src.id = d.mover_id,
             unnest(src.derived_from) e
       GROUP BY d.occupant_id
  ) dv ON dv.occupant_id = agg.occupant_id
 WHERE pe.id = agg.occupant_id;

UPDATE public.proposed_edges pe
   SET status = 'merged', reviewed_at = now()
  FROM public._mig0185_rp_demoted d
 WHERE pe.id = d.mover_id;

-- ---------------------------------------------------------------------------
-- 3. The clean remainder — rewrite the endpoints onto the keeper surface.
--    The surviving mover set is exactly the post-fixpoint `_rp_movers`.
-- ---------------------------------------------------------------------------
-- 2026-08-05 fix 5 (ordering, the general case): unique checks are immediate
-- per row inside one UPDATE, so ANY schedule where a mover lands on a triple
-- its current occupant is about to vacate collides — (dr congo, the democratic
-- republic of congo) was that shape live. Two phases through a parking
-- namespace make the rewrite ORDER-IMMUNE: every mover vacates first (parking
-- triples are keyed by row id, collision-impossible), then lands. Phase B is
-- conflict-free by construction: one keeper per destination group (fix 3),
-- staying occupants hold their keepership (fix 4), every other contender is
-- either a folded loser or a demoted mover (fix 7), and neither ever moves.
UPDATE public.proposed_edges pe
   SET source_entity = '\x01mig0185-park:' || pe.id::text,
       target_entity = '\x01parked'
  FROM public._mig0185_rp_movers m
 WHERE pe.id = m.id;
GET DIAGNOSTICS v_parked = ROW_COUNT;

UPDATE public.proposed_edges pe
   SET source_entity = p.new_src,
       target_entity = p.new_tgt
  FROM public._mig0185_rp_movers m
  JOIN public._mig0185_rp_plan p ON p.id = m.id
 WHERE pe.id = m.id;
GET DIAGNOSTICS v_clean = ROW_COUNT;

-- ---------------------------------------------------------------------------
-- POSTCONDITIONS. Loud, by key. A failure here rolls the whole file back
-- (the runner's transaction), so a broken apply leaves NOTHING half-moved.
-- ---------------------------------------------------------------------------
-- Every parked row landed: phase A count = phase B count = surviving movers.
SELECT count(*) INTO v_total FROM public._mig0185_rp_movers;
IF v_parked <> v_total OR v_clean <> v_total THEN
    RAISE EXCEPTION '0185: mover reconciliation failed — % movers, % parked, % landed', v_total, v_parked, v_clean;
END IF;

-- ...and none is still wearing the parking namespace.
SELECT pe.id::text INTO v_bad
  FROM public.proposed_edges pe
 WHERE starts_with(pe.source_entity, '\x01mig0185-park:')
    OR pe.target_entity = '\x01parked'
 LIMIT 1;
IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION '0185: phase B left row % parked — the plan join lost a row phase A vacated', v_bad;
END IF;

-- The four outcome classes partition the plan: nothing handled twice,
-- nothing dropped on the floor.
SELECT count(*) INTO v_total FROM public._mig0185_rp_plan;
SELECT count(*) INTO v_selfset FROM public._mig0185_rp_plan
 WHERE lower(new_src) = lower(new_tgt);
IF v_total <> v_selfset + v_folded + v_demoted + v_clean THEN
    RAISE EXCEPTION '0185: outcome classes do not partition the plan — % rows planned vs % self-loops + % folded + % demoted + % re-pointed', v_total, v_selfset, v_folded, v_demoted, v_clean;
END IF;

-- Informational: `pending` rows still naming a tombstone surface after the
-- run. Expected 0; a nonzero count is the namesake-ambiguity residue (a
-- keeper surface that is also some tombstone's exact name), which the
-- code-half sweep owns from here.
SELECT count(*) INTO v_residual
  FROM public.proposed_edges pe
 WHERE pe.status = 'pending'
   AND (EXISTS (SELECT 1 FROM public._mig0185_rp_names n
                 WHERE n.tname = pe.source_entity)
     OR EXISTS (SELECT 1 FROM public._mig0185_rp_names n
                 WHERE n.tname = pe.target_entity));

RAISE NOTICE '0185 repoint: % proposed_edges named a tombstone — % rejected as '
             'self-loops (% newly), % folded onto an existing candidate and '
             'marked merged, % demoted by the fixpoint in % pass(es), % '
             're-pointed cleanly; % pending rows still on a namesake surface',
             v_total, v_selfset, v_self, v_folded, v_demoted, v_iter, v_clean,
             v_residual;


DROP TABLE IF EXISTS public._mig0185_rp_demoted;
DROP TABLE IF EXISTS public._mig0185_rp_movers;
DROP TABLE IF EXISTS public._mig0185_rp_target;
DROP TABLE IF EXISTS public._mig0185_rp_keep;
DROP TABLE IF EXISTS public._mig0185_rp_cand;
DROP TABLE IF EXISTS public._mig0185_rp_groups;
DROP TABLE IF EXISTS public._mig0185_rp_plan;
DROP TABLE IF EXISTS public._mig0185_rp_names;

END $$;
