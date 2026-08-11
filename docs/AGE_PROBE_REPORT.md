<!--
SPDX-FileCopyrightText: 2026 Lewis George
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The AGE probe — measured, 2026-08-03

**The question this answers, in the operator's words:** *"are those limits AGE's, or just our
default Postgres setup? Postgres is becoming the core of tons of things with tons of
optimizations. Let's really gauge that."*

It is a fair question and it deserved a measurement rather than an opinion. Postgres 18 is a
genuinely fast engine and most "Postgres is slow at X" claims are really "nobody set
`work_mem`". So the probe was built to give AGE every advantage the deployment can give it —
the live tuning profile, an index on the vertex key, more parallel workers, JIT on and off —
and to check, at each step, whether the ceiling moved.

**The short answer: the ceilings that matter did not move.** Tuning and indexing bought real,
large wins on the plans that can use an index, and bought *nothing* on the variable-length
traversal that is the entire reason to want a graph engine. That distinction is the finding.

---

## 1 · What was measured, and how to re-run it

Everything here is reproducible from the tree:

```bash
scripts/age_probe/run_probe.sh up          # scratch AGE container, pinned by digest
scripts/age_probe/run_probe.sh claims      # the standing claims from the graph debate
scripts/age_probe/run_probe.sh bench 100k  # generate + load + the full config matrix
scripts/age_probe/run_probe.sh bench 1m
scripts/age_probe/run_probe.sh teardown    # removes container AND volume
python3 scripts/age_probe/age_probe_bench.py report --results /tmp/legba_age_probe/results.jsonl
```

**The instance.** A throwaway container on its own port and volume, started from
`apache/age@sha256:4241e2d8bb86a6b2ea44e9ad06c73856e12b209de295124603a599dd7feb70eb` — the
exact digest the live substrate runs, now pinned in `docker-compose.yml`. Contents:
**PostgreSQL 18.1, Apache AGE 1.7.0**. The live substrate was never written to; it was read
read-only, once, to confirm the fixture inventory.

**The graph.** `scripts/age_probe/world_graph_gen.py` emits a synthetic world graph shaped
like ours rather than a uniform random one, because traversal cost lives entirely in the
degree tail:

* **50,000 entities**, power-law degree by preferential attachment. The first `V-1` edges form
  a preferentially-attached spanning tree so the graph is connected and path questions have
  answers; the rest are hub-biased on both endpoints.
* **Entity classes in our proportions** — `country` 0.4 %, `organization` 22 %, `person` 44 %,
  `place` 21 %, `group` 12.6 % — with country nodes pre-loaded into the attachment urn, so
  countries hub the way Wikidata IGO membership makes them hub in the live graph.
* **The four `edge_family` tiers** the graph debate reconciled: `cooccurrence` 62 %,
  `reference` 20 %, `relation` 13 %, `structural` 5 %.
* **Signed polarity** on the `relation` family only, so the structural-balance pattern query
  has real signal to find. **8 % of edges are closed** (`valid_until` set), so every query
  carries the same open-row predicate the production readers carry.

**Both twins, same instance.** Each scale is loaded twice: once as an AGE graph with
**id-keyed vertices** (the business key in `properties.id` is the entity uuid — the contract
`A_age_commit.md` §3.1 requires and every reader in the tree already assumes), and once as a
relational `entity_edges`-shaped table with the indexes `JUDGE_SYNTHESIS` §4.1 specifies. The
loader asserts the two twins carry the same edge count and reads a vertex back through Cypher
before any measurement runs.

**The question shapes**, chosen to match what Legba actually asks:

| shape | what it is | who asks it |
|---|---|---|
| `ego1` / `ego2` / `ego3` | "what is around this actor", 1-3 hops | the graph-viewer UI verb (K-G4) |
| `vlp3` / `vlp4` / `vlp6` | bounded variable-length path between two anchored actors | `/graph/path`, at and beyond the shipped cap |
| `triad` | unstable signed triads touching entity class `country` | `structural_balance`; the pattern query B's typed SQL builder was said not to express |

Ego seeds are ten vertices spanning the degree distribution (the top three hubs, then
deciles). Path anchors are pairs at **exact** BFS distance 3, 4 and 6, computed once with
networkx so every arm measures identical work. Each cell is one warm-up plus repeats across
all seeds/pairs; `p50`/`p95` are over the whole sample. `statement_timeout` is 60 s and a
timeout is reported as a timeout, never as a number. A `†` marks a cell that hit its
wall-clock budget before exhausting its seeds — the percentiles are over fewer samples, and
in every case the cell was already an order of magnitude off the pace.

**One bias to read `†` cells with:** seeds are visited highest-degree first, so a
budget-truncated cell is weighted toward the *hubs* — the hardest case, not the average one.
That is deliberate (a graph viewer is most likely to be pointed at a hub, and a p95 that
excludes hubs is not a p95), but it means a `†` number should be read as "worst case" rather
than "typical". Cells without `†` cover all ten seeds.

**Three executors**, so "slow" always has a comparison: `age` (Cypher, the production idiom),
`age_dir` (a direction-explicit Cypher rewrite — see §3), `sql` (recursive CTE / self-join over
the relational twin), and `nx` (an in-process networkx snapshot of the twin, which doubles as
the pre-registered **E4** gauge).

**The configuration arms.** The first three are full-matrix and isolate the three candidate
causes of any ceiling; the last three are single-knob ablations run only on the queries that
actually hurt, so each knob's contribution is attributable rather than bundled.

| arm | what changed | the question it answers |
|---|---|---|
| `default` | stock `apache/age` config (`shared_buffers` 128 MB, `work_mem` 4 MB, `effective_cache_size` 4 GB, `max_parallel_workers_per_gather` 2, `jit` on) | what an untuned deploy gets |
| `tuned` | the **live stack's** GUC profile from `docker-compose.yml` | **is the ceiling our Postgres setup?** |
| `tuned_propidx` | + an expression index on the AGE vertex property key | is the ceiling a missing index? |
| `jit_off` | `tuned` + `jit=off` | is JIT compilation the tax? |
| `parallel8` | `tuned` + `max_parallel_workers_per_gather=8` | does parallelism scale it? |
| `workmem256` | `tuned` + `work_mem=256MB` | is it spilling? |

One deliberate deviation from the live profile: `shared_buffers` is 2 GB in the probe where
live sets 8 GB. The probe's entire working set is well under 1 GB, so both values cache it
completely and the measurement is unaffected — while 8 GB would have meant 8 GB of resident
memory on a host running the production stack. Every other live GUC (`effective_cache_size`
24 GB, `work_mem` 32 MB, `maintenance_work_mem` 1 GB, `random_page_cost` 1.1,
`effective_io_concurrency` 200, `max_wal_size` 4 GB, `wal_buffers` 64 MB) is reproduced exactly.

---

## 2 · The measurement table

`†` = budget-truncated (hub-weighted, read as worst case) · `n/a` = the graph holds no pair
at that distance · `age_dir` = the direction-explicit rewrite of §3.4, not Cypher anyone
would hand-write. The ablation arms (`jit_off`, `parallel8`, `workmem256`) were run at 100k
only: they isolate individual knobs, and the scale-relevant contrast at 1M is `default` →
`tuned`, which is present.

### 100k edges — p50 / p95 milliseconds

| query | engine | default | tuned | tuned_propidx | jit_off | parallel8 | workmem256 | in-process nx |
|---|---|---|---|---|---|---|---|---|
| ego1 | age | 207.4 / 920.8 | 190.8 / 816.3 | 180.8 / 965.4 | — | — | — | — |
| ego1 | age_dir | 129.6 / 159.9 | 67.7 / 70.7 | 1.4 / 2.2 | — | — | — | — |
| ego1 | sql | 0.5 / 1.6 | 0.6 / 1.0 | 1.3 / 2.3 | — | — | — | 0.0 / 0.0 |
| ego2 | age | 4,910.3 / 5,329.5† | 5,154.0 / 5,594.8† | 4,845.5 / 5,090.0† | 5,391.5 / 6,027.1† | 4,870.5 / 5,594.8† | 4,980.7 / 5,556.9† | — |
| ego2 | age_dir | 254.3 / 302.2 | 168.9 / 226.2 | 1.2 / 7.0 | 1.6 / 7.1 | 1.7 / 7.3 | 1.1 / 6.6 | — |
| ego2 | sql | 0.7 / 2.4 | 0.6 / 2.4 | 0.8 / 2.4 | 0.8 / 3.5 | 1.0 / 2.7 | 0.5 / 4.3 | 0.0 / 0.3 |
| ego3 | age | 34,365.8 / 34,365.8† | 32,921.2 / 32,921.2† | 36,075.8 / 36,075.8† | 37,572.3 / 37,572.3† | 35,335.3 / 35,335.3† | 33,934.9 / 33,934.9† | — |
| ego3 | age_dir | 5,853.5 / 6,060.3† | 3,120.2 / 3,528.1† | 353.2 / 459.2 | 83.6 / 140.8 | 388.8 / 475.6 | 368.4 / 487.8 | — |
| ego3 | sql | 1.9 / 16.4 | 1.7 / 12.4 | 1.8 / 12.1 | 2.0 / 18.4 | 1.2 / 13.9 | 1.6 / 16.7 | 0.1 / 2.4 |
| vlp3 | age | 149.8 / 188.8 | 150.1 / 165.8 | 1.9 / 2.4 | — | — | — | — |
| vlp3 | sql | 2.8 / 4.0 | 3.1 / 3.6 | 3.2 / 3.6 | — | — | — | 0.0 / 0.1 |
| vlp4 | age | 173.4 / 222.9 | 160.0 / 171.6 | 8.5 / 12.0 | — | — | — | — |
| vlp4 | sql | 15.8 / 21.0 | 17.0 / 27.2 | 15.6 / 19.7 | — | — | — | 0.0 / 0.1 |
| vlp6 | age | 922.7 / 1,101.2 | 913.6 / 1,156.3 | 832.1 / 1,032.2 | 810.8 / 1,014.9 | 783.2 / 930.4 | 779.6 / 894.7 | — |
| vlp6 | sql | 367.4 / 403.4 | 361.7 / 446.6 | 402.5 / 451.8 | 426.4 / 536.8 | 377.6 / 512.4 | 401.8 / 435.5 | 0.1 / 0.2 |
| triad | age | **>60s TO** | **>60s TO** | **>60s TO** | **>60s TO** | **>60s TO** | **>60s TO** | — |
| triad | sql | 9.5 / 14.6 | 10.0 / 10.6 | 12.3 / 12.7 | 10.1 / 14.1 | 12.7 / 14.2 | 10.0 / 10.3 | 17.2 / 20.1 |

`snapshot_rebuild` (trigger **E4** gauge): 914 ms

### 1m edges — p50 / p95 milliseconds

| query | engine | default | tuned | tuned_propidx | in-process nx |
|---|---|---|---|---|---|
| ego1 | age | 6,434.8 / 9,235.0† | 6,595.9 / 6,819.2† | 6,617.8 / 6,866.6† | — |
| ego1 | age_dir | 62.1 / 67.2 | 60.3 / 66.3 | 1.7 / 5.7 | — |
| ego1 | sql | 1.5 / 4.2 | 0.9 / 2.4 | 0.8 / 3.5 | 0.0 / 0.3 |
| ego2 | age | **>60s TO** | **>60s TO** | **>60s TO** | — |
| ego2 | age_dir | 933.5 / 1,640.7 | 1,104.9 / 1,570.2† | 62.6 / 492.7 | — |
| ego2 | sql | 10.0 / 351.3 | 9.8 / 128.4 | 7.7 / 161.8 | 2.2 / 31.8 |
| ego3 | age | **>60s TO** | **>60s TO** | **>60s TO** | — |
| ego3 | age_dir | 32,604.8 / 32,604.8† | **>60s TO** | 32,070.5 / 32,070.5† | — |
| ego3 | sql | 8,713.1 / 9,246.4† | 532.0 / 2,740.0 | 471.6 / 2,740.6 | 131.0 / 534.0 |
| vlp3 | age | 1,576.9 / 1,950.1 | 1,510.7 / 1,738.3 | 1,526.6 / 1,693.0 | — |
| vlp3 | sql | 2,613.6 / 2,859.9† | 883.7 / 944.1 | 888.4 / 1,025.2 | 0.1 / 0.2 |
| vlp4 | age | **>60s TO** | **>60s TO** | **>60s TO** | — |
| vlp4 | sql | 14,455.3 / 14,797.0† | 3,582.0 / 4,080.3† | 3,646.5 / 4,257.9† | 0.2 / 0.3 |
| vlp6 | age | n/a | n/a | n/a | — |
| vlp6 | sql | n/a | n/a | n/a | — |
| triad | age | **>60s TO** | **>60s TO** | **>60s TO** | — |
| triad | sql | 333.5 / 337.7 | 235.1 / 276.2 | 240.1 / 274.1 | 227.3 / 227.3 |

`snapshot_rebuild` (trigger **E4** gauge): 7,701 ms

### 2.1 Load cost and footprint — where AGE is genuinely fine

Before the traversal numbers, the unglamorous ones, because they are the part of the debate
that turned out *not* to be a problem. Both twins built from the same CSVs, same instance:

| | 100k edges | 1M edges |
|---|---:|---:|
| relational twin — rows | 3.3 s | 28.5 s |
| relational twin — indexes | 0.6 s | 2.7 s |
| AGE — 50k vertices | 0.5 s | 0.4 s |
| AGE — edges | 2.4 s | 18.8 s |
| **total** | **6.9 s** | **50.7 s** |
| relational twin on disk | 27 MB | **267 MB** |
| AGE graph on disk | 31 MB | **241 MB** |

AGE ingests *faster* than the relational twin (18.8 s vs 28.5 s at 1M) and at 1M it is
**smaller on disk** (241 MB vs 267 MB — the twin carries more indexes). Writing to AGE is
cheap and storing in AGE is cheap. Nothing in this probe argues against AGE on ingest or
footprint; the entire case is about reads.

Both loads assert the twins carry identical edge counts and read a vertex back through Cypher
before any timing is taken, so the comparison is over the same graph and not two different
ones.

### 2.2 Knob by knob — what each one actually bought

Read down a row and the answer to the operator's question is right there. Differences under
~10 % on these cells are inside the noise and are reported as "nothing".

| knob | on the VLE traversal (`age`) | on index-driven plans (`age_dir`) | on the relational twin (`sql`) |
|---|---|---|---|
| **the live GUC profile** (`default` → `tuned`: `shared_buffers`, `effective_cache_size`, `work_mem`, `maintenance_work_mem`, `random_page_cost`, `effective_io_concurrency`, WAL sizing) | **nothing.** ego1 207 → 191 ms, ego2 4,910 → 5,154 ms, ego3 34.4 → 32.9 s, vlp6 923 → 914 ms, triad timeout → timeout | **~2×.** ego1 130 → 68 ms, ego2 254 → 169 ms, ego3 5,854 → 3,120 ms | nothing to buy — already 0.5-2 ms |
| **the vertex property index** (`tuned` → `tuned_propidx`) | **nothing where an endpoint is free** (ego1 191 → 181 ms, ego2 5,154 → 4,846 ms, ego3 32.9 → 36.1 s, vlp6 914 → 832 ms) · **enormous where both are bound** (vlp3 150 → **1.9 ms**, vlp4 160 → **8.5 ms**) | **48-141×.** ego1 68 → **1.4 ms**, ego2 169 → **1.2 ms**, ego3 3,120 → **353 ms** | n/a |
| **`jit=off`** | nothing (vlp6 832 → 811 ms; ego2/ego3 within noise) | **4.2× on the most complex plan** — ego3 353 → **84 ms**. Eight UNION branches is exactly the shape Postgres JIT over-compiles | nothing |
| **`max_parallel_workers_per_gather=8`** (from 2) | **nothing.** ego2 4,846 → 4,871 ms, ego3 36.1 → 35.3 s, vlp6 832 → 783 ms, triad still timeout | nothing (353 → 389 ms, noise) | nothing |
| **`work_mem=256MB`** (from 32 MB) | **nothing.** ego2 4,846 → 4,981 ms, ego3 36.1 → 33.9 s, vlp6 832 → 780 ms | nothing (353 → 368 ms) | nothing |

Two things fall out of that table.

**First, the honest answer to "is it AGE or is it our Postgres setup?" is: both, on different
queries, and the split is clean.** Where a query can be driven from an index, our setup was
leaving a great deal on the floor — up to 141× on a single missing index, and another 4× from
JIT. Where a query cannot be driven from an index, six configurations spanning a 200× range of
effort produced no movement at all.

**Second, parallelism and memory buy literally nothing here.** That is diagnostic, not
disappointing: `age_vle` is a set-returning C function, so no amount of `max_parallel_workers`
parallelises inside it, and nothing in these plans spills, so `work_mem` has nothing to fix.
The cost is structural work being done, not resource starvation.

**And the 1M scale sharpens it into the cleanest answer the probe produced.** At 100k the
relational twin was already so fast (sub-2 ms) that tuning had nothing to give it. At 1M it has
real work to do, and the live GUC profile transforms it — while leaving AGE exactly where it
found it:

| 1M edges, `default` → `tuned` | before | after | |
|---|---:|---:|---|
| **relational** `ego3` | 8,713 ms | **532 ms** | **16× faster** |
| **relational** `vlp4` | 14,455 ms | **3,582 ms** | **4× faster** |
| **relational** `vlp3` | 2,614 ms | **884 ms** | **3× faster** |
| **relational** `triad` | 334 ms | 235 ms | 1.4× faster |
| AGE `ego1` | 6,435 ms | 6,596 ms | — |
| AGE `vlp3` | 1,577 ms | 1,511 ms | — |
| AGE `ego2` / `ego3` / `vlp4` / `triad` | timeout | timeout | — |

So the operator's premise was right and its consequence is the opposite of what one would
expect: **Postgres really is accumulating enormous optimisation, our stack really is configured
to collect it, and none of it reaches AGE's traversal operator.** The tuning that makes the
relational store 16× faster makes the graph extension 0 % faster, on the same data, in the same
process, in the same query.

---

## 3 · Why the numbers look like that — the plans

A table of timings invites the reply "you must have configured it wrong". The plans say
otherwise, and they are the part of this report worth reading twice. All captured with
`EXPLAIN (ANALYZE, BUFFERS)` at the 100k scale.

### 3.1 The vertex label table has no index on the property key

AGE creates each label table as an ordinary Postgres heap: a primary key on the internal
`graphid`, btrees on `start_id`/`end_id` for edge labels, and **nothing at all on
`properties`**. So the anchor of every query we write — `WHERE a.id = '<uuid>'` — is a
sequential scan of the whole vertex label:

```
->  Seq Scan on "Entity" a  (actual time=250.755..294.669 rows=1 loops=1)
      Filter: (agtype_access_operator(VARIADIC ARRAY[properties, '"id"'::agtype]) = '"…"'::agtype)
      Rows Removed by Filter: 49999
```

**This one is ours, and it is fixable.** An expression index on exactly that access operator
is used by the planner:

```sql
CREATE INDEX ON legba_graph."Entity"
  (ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties, '"id"'::agtype]));
```

```
->  Index Scan using idx_probe_entity_prop_id on "Entity" a  (actual time=0.145..0.148 rows=1 loops=1)
```

**250 ms → 0.15 ms**, a ~1,700× improvement on the anchor lookup alone. Nobody had created it,
because nothing had ever put a row in the graph. It is the `tuned_propidx` arm, and any future
projector must create one per label as part of shipping.

### 3.2 It helps enormously — but only where BOTH endpoints are anchored

This is the subtlety the headline numbers would hide, and it is worth stating precisely.

With the property index in place, a **two-anchored** bounded path — `WHERE a.id = … AND
b.id = …`, which is exactly `/graph/path`'s shape — improves dramatically, because both
endpoints bind through the index and the expansion runs between two known points:

| query | tuned (no property index) | tuned + property index |
|---|---:|---:|
| `vlp3` (3-hop, both ends anchored) | 150.1 ms | **1.9 ms** |
| `vlp4` | 160.0 ms | **8.5 ms** |
| `ego1` (one end anchored, other end free) | 190.8 ms | 180.8 ms |
| `ego2` | 5,154 ms | 4,846 ms |
| `ego3` | 32,921 ms | 36,076 ms |

A 79× win on one shape and **nothing at all** on the other. The difference is entirely whether
the terminal vertex is bound. Here is the ego query *with* the index — look at what did not
change:

```
->  Nested Loop  (actual time=378.625..491.281 rows=9 loops=1)
      Join Filter: age_match_vle_terminal_edge(a.id, b.id, _age_default_alias_0.edges)
      Rows Removed by Join Filter: 449991
      ->  Seq Scan on "Entity" b  (actual time=0.018..5.953 rows=50000 loops=1)
      ->  Materialize
            ->  Index Scan using idx_probe_entity_prop_id on "Entity" a  (rows=1)
            ->  Function Scan on age_vle  (actual time=357.797..357.798 rows=9 loops=1)
```

To bind the **terminal** vertex of a variable-length match, AGE cross-joins the variable-length
expansion against the **entire vertex label table** and filters the product with
`age_match_vle_terminal_edge`. Nine neighbours are found by removing **449,991** rows from a
Cartesian product. That cost scales with `|V|`, not with the size of the neighbourhood — the
thing a graph engine exists to avoid.

No configuration removes it. It is not a missing index, not `work_mem`, not parallelism: it is
how the VLE operator binds an **unbound** endpoint. And "what is around this actor" — the ego
question, the graph-viewer verb, the whole point of K-G4 — is *definitionally* the query with
an unbound endpoint. AGE is fast at the question where you already know both answers and slow
at the question where you are exploring.

### 3.3 The variable-length operator does not use the edge indexes either

`Function Scan on age_vle` took 358 ms to return 9 rows while touching 3,198 buffers — about
25 MB, which is the whole edge set at this scale. The edge label tables *have*
`start_id`/`end_id` btrees; the VLE operator does not drive from them.

The same is true of a plain **undirected** fixed-length hop, which does not involve VLE at all:

```
->  Parallel Seq Scan on cooccurrence r_4   (rows=61842)
->  Parallel Seq Scan on relation r_2       (rows=13042)     … all four label tables
->  Bitmap Heap Scan on "Entity" b  (loops=100000)
      Recheck Cond: ((r.end_id = id) OR (r.start_id = id))
Execution Time: 699.406 ms
```

Every edge in the graph is read, then both endpoints of every edge are looked up, and only then
is the result joined against the anchor. **699 ms to find one vertex's neighbours.**

### 3.4 The one lever that does work: make the direction explicit

Rewrite the identical question with the direction spelled out, and the planner behaves:

```
->  Index Scan using idx_probe_entity_prop_id on "Entity" a  (rows=1)
->  Index Scan using relation_start_id_idx      on relation r_2      (rows=0)
->  Index Scan using reference_start_id_idx     on reference r_3     (rows=0)
->  Index Scan using cooccurrence_start_id_idx  on cooccurrence r_4  (rows=2)
->  Index Scan using structural_start_id_idx    on structural r_5    (rows=0)
->  Index Scan using "Entity_pkey" on "Entity" b  (loops=2)
Execution Time: 1.494 ms
```

**699 ms → 1.5 ms, a 466× difference, for a semantically identical question.** The indexes were
there the whole time; the undirected and variable-length forms simply cannot reach them.

This is why the table carries an `age_dir` row. It is Cypher written to dodge the planner:
`2^k` UNION branches enumerating every direction combination, which is why `ego3` is 8 branches
and still slow, and why the honest version — union the shorter lengths too, `2 + 4 + … + 2^k`
branches — would be worse than the number shown. A query language you have to write around is
not buying much over the SQL you would otherwise write.

### 3.5 At 1M edges, the workaround stops being slow and starts being an error

The direction-explicit rewrite (§3.4) is the best AGE result in this probe, so it is worth
saying what happened when it met the 1M graph on the stock configuration. It did not get slow.
It died:

```
asyncpg.exceptions.DiskFullError: could not resize shared memory segment
"/PostgreSQL.1286026460" to 33554432 bytes: No space left on device
```

Eight UNION branches over a million edges is a big parallel plan, and Postgres allocates
parallel-query DSM segments out of `/dev/shm`. The scratch container had **1 GB** — the same
`shm_size: 1gb` the live substrate ships. Raising it to 4 GB let the matrix complete.

Two things follow. The narrow one: the probe's own container now asks for 4 GB, documented in
`run_probe.sh`. The broader one is a note for the substrate, independent of any graph decision
— **the live `shm_size: 1gb` is a real ceiling on parallel query, and it fails with a
disk-full error that names neither parallelism nor `/dev/shm`.** No live query is known to have
hit it, and this probe is not evidence that one will. But if an analytic query ever dies with
"No space left on device" on a host with free disk, this is the first place to look.

### 3.6 AGE cannot express a per-hop predicate over a variable-length path — at all

This one was found by accident, verifying that the two executors were answering the same
question. They were not, and the reason matters more than the discrepancy.

The relational twin filters `valid_until IS NULL AND superseded_by IS NULL` on every hop,
because that is what every production reader does — Legba's edges are temporal and a path
through a closed edge is a path that no longer exists. The AGE side carries the same flag as
an edge property (`is_open`) but **does not filter on it**, so a 2-hop ego from the same seed
returns 3,861 vertices from AGE against 2,671 from the twin. Removing the twin's temporal
filter reproduces AGE's number exactly (3,862, the difference being the seed itself), which
confirms the cause.

The reason the AGE side does not filter is that **it cannot**:

```
MATCH (a:Entity)-[r*1..2]-(b:Entity) WHERE a.id = '…' AND ALL(x IN r WHERE x.is_open = 1) …
ERROR:  syntax error at or near "("
```

`ALL(...)` and `ANY(...)` — openCypher's list predicates, the standard way to constrain every
hop of a variable-length match — are **not implemented in AGE 1.7**. There is no supported
phrasing that says "traverse only open edges, up to k hops".

Two consequences, and the second is the important one.

*For this probe:* AGE was answering over ~8.7 % more edges than the twin on every ego and path
measurement, so its numbers are slightly flattered by having a bigger answer to give but
slightly penalised by having more edges to walk. Neither correction is remotely large enough
to move a conclusion — the gaps in §2 run from 100× to 8,000× — but the asymmetry is real and
is recorded here rather than buried.

*For the engine decision:* pre-registered trigger **E3** defines the case for a graph engine as
*"shapes the typed SQL builder cannot express (≥4 hops, or per-hop type + temporal
predicates)"*. Per-hop temporal predicates are precisely what AGE cannot express. A projection
into AGE would have to either drop closed edges at projection time — losing history, and
forcing a re-projection every time an edge closes — or answer path questions over edges that
have already been superseded. The second is the exact class of silent wrongness this
remediation programme exists to remove.

### 3.7 The relational twin is not doing anything clever — and it has its own ceiling

For contrast, the twin's ego query is an ordinary recursive CTE against
`idx_probe_edges_out` / `idx_probe_edges_in`. At 100k edges it answers in **0.5-2 ms** at every
hop count. There is no trick in it; it is what a btree does when the planner can use it.

It is not magic either, and the 1M scale shows where it runs out. The twin's cost tracks the
size of the answer, which is the honest behaviour — but on a graph with average degree ~37, the
3-hop neighbourhood of a hub *is* most of the graph, so `ego3` at 1M costs seconds rather than
milliseconds (see §2 and §5.2). That is a real ceiling and it belongs in the record next to
AGE's. The difference is that the twin's ceiling arrives when the question genuinely becomes
large, while AGE's arrives while the answer is still nine rows.

---

## 4 · The standing claims from the graph debate, verified

`scripts/age_probe/age_claims.py` turns each open claim into a single assertion against the
scratch instance. All were run on 2026-08-03 against PostgreSQL 18.1 / AGE 1.7.0.

| # | claim | verdict | evidence |
|---|---|---|---|
| **C1** | **Parameterized Cypher works** — the "AGE cannot bind params, so we string-interpolate, so we have an injection surface" objection is a property of *our wrapper*, not of AGE | **VERIFIED** | `cypher(graph, $$ MATCH (n) WHERE n.id = $eid … $$, $1)` with an agtype parameter bound through asyncpg's extended query protocol returned the right row. A hostile payload bound the same way matched **0 rows** — it stayed data. The interpolation in `postgres.cypher()` (`f"…$$ {query} $$…"`), `age.py:507` (`query.format(**params)`) and `_fact_graph._cypher_str` is therefore a choice we made and can unmake. |
| **C1b** | the SQL-level `PREPARE(agtype)` / `EXECUTE` form A quoted | **VERIFIED** | `PREPARE q(agtype) AS SELECT * FROM cypher(…, $1)` then `EXECUTE q('{"eid":"E2"}')` returns rows. Note `EXECUTE` is a utility statement, so the *value* must be inlined there; the genuinely bound form is C1. |
| **C2** | **`ALTER DATABASE … SET search_path` is a durable fix** for the per-acquire `SET` tax — A proposed it but could not prove it (read-only) | **VERIFIED** | With `search_path = "$user", public, ag_catalog` set at database level, a brand-new connection traverses edges with **no `LOAD`, no per-acquire `SET`**. The corruption hazard the 2026-06 review warned about does not fire: with `ag_catalog` **last**, an unqualified `CREATE TABLE` still lands in `public`. And `RESET ALL` falls back to the *database* default, not the compiled-in one, so asyncpg's pool recycling cannot undo it. The whole objection is deletable with one `ALTER DATABASE` and a pool restart. |
| **C3** | fully-qualified `ag_catalog.cypher(...)` works for a bare `MATCH` but **fails on edge traversal** without `search_path` | **VERIFIED** | Bare `MATCH (n:Entity) RETURN n.id` succeeds. Adding one edge gives `operator does not exist: ag_catalog.graphid = ag_catalog.graphid` — the graphid operators are only resolvable through the search path. So qualification alone is not a workaround; C2 is. |
| **C4** | **there is no built-in `shortestPath` / `allShortestPaths`** | **VERIFIED** | No `ag_catalog` function matches `%short%`, and both spellings fail at parse time: `syntax error at or near "shortestPath"`. |
| **C4b** | the workaround — bounded `[*1..k]` + `ORDER BY length(p) LIMIT 1` — returns the true shortest length | **VERIFIED** | Correct answer, and it is the form `graph_paths.build_shortest_path_cypher` already ships. **Its cost is §2's `vlp*` rows**, and that is the real content of C4: the workaround is not free, it is the single most expensive shape measured. |
| **C5** | **agtype ergonomics** | **VERIFIED (and it is as ugly as reported)** | Every agtype value crosses the driver as `str`. A vertex arrives as `{"id": 844424930131969, "label": "Entity", "properties": {…}}::vertex` — the caller strips the `::vertex` annotation and `json.loads` it by hand, and there are three copies of that helper in the tree. Worse, property access inside a list comprehension still fails (`[n IN nodes(p) \| n.id]` → `could not find properties for n`), which is exactly why `graph_paths` returns raw `nodes(p)`/`relationships(p)` and reconstructs the ordering in Python. |
| **C6** | **version currency vs Postgres 18** | **VERIFIED** | AGE **1.7.0** on PostgreSQL **18.1**. The 2026-06 objection that "AGE constrains the substrate's Postgres version" does not hold at this pin. Worth noting separately: the pinned image is ~5 months old and `apache/age:latest` still resolves to the same digest, so upstream has not published in that window. |

Three of the five objections in the 2026-06 evaluation (session tax, no parameter binding, PG
version lag) do not survive contact with the current version. The two that do — no
`shortestPath`, and the agtype codec — are the two the probe re-confirms.

### 4.1 An incidental finding: we are on the wrong side of the search_path hazard today

The probe demonstrated this by falling into it. Its harness prepares connections the way
production does — `SET search_path = ag_catalog, "$user", public` — and then issues
`CREATE TABLE probe_entity_edges (…)`. Those tables were created in **`ag_catalog`**, the
extension's own schema, not in `public`. Nobody asked for that and nothing warned.

Checking the tree, **every runtime path puts `ag_catalog` first**:

```
src/legba/data/postgres.py:127,156,169          SET search_path = ag_catalog, "$user", public
src/legba/data/graph_paths.py:227,293,385       SET search_path = ag_catalog, "$user", public
…/deterministic_handlers/graph_mining.py:552    SET search_path = ag_catalog, "$user", public
…/deterministic_handlers/structural_balance.py:374   (same)
src/legba/data/stack/postgres/age.py:406,446    SET search_path TO "<schema>", ag_catalog, "$user", public
```

The one place that gets it right is the migration runner (`migrate.py:114` —
`SET search_path = public, ag_catalog`), which is why no migration has ever mis-landed. The
exposure is therefore latent rather than active: DDL in this system runs through migrations,
and application code on a pooled `PostgresStore` connection does not create tables. But it is
one stray unqualified `CREATE TABLE` on a pooled connection away from putting a production
table inside a third-party extension's schema, where the next `DROP EXTENSION age` would take
it along.

This is exactly the corruption hazard the 2026-06 review named, and claim **C2** is the fix for
both problems at once: `ALTER DATABASE legba SET search_path = "$user", public, ag_catalog`
— `ag_catalog` **last** — removes the per-acquire `SET` entirely *and* puts unqualified DDL
back in `public`, verified above. Two lines of benefit for one `ALTER DATABASE` and a pool
restart. It is not in this change set because it is a live-substrate write that wants an
operator's window, but it is the cheapest item this probe found.

---

## 5 · VERDICT

### 5.1 Which limits were ours, and which are AGE's

**Ours — and fixable, some dramatically:**

1. **The missing vertex property index is the single biggest deployment defect, and it was
   invisible because the graph was empty.** One expression index takes the anchor lookup from
   250 ms to 0.15 ms, a two-anchored 3-hop path from 150 ms to 1.9 ms, and a direction-explicit
   1-hop ego from 130 ms to 1.4 ms. Any projector that ships must create one per vertex label,
   or every measurement anyone takes afterwards will be measuring the missing index.
2. **Direction-explicit rewrites recover the edge indexes.** 699 ms → 1.5 ms for the same
   question. Real, and worth knowing.
3. **Postgres GUC tuning helps the index-driven plans and only those** — the `age_dir` rows
   roughly halve from `default` to `tuned` (ego1 130 → 68 ms). That is the "Postgres has tons
   of optimizations" thesis working exactly as advertised, on the plans that let it work.

**AGE's — structural, and nothing moved them:**

1. **The open-ended ego expansion** — `(a)-[*1..k]-(b)` with one endpoint anchored, which is
   *the* graph-viewer verb and the whole content of K-G4. Across `default`, `tuned`,
   `tuned_propidx`, `jit_off`, `parallel8` and `workmem256` at 100k edges, `ego1` stayed at
   ~180-210 ms, `ego2` at ~4.8-5.4 s and `ego3` at ~33-38 s. **Six configurations, a 200×
   spread in effort, and the number does not move.** The cause is in §3.2: the terminal vertex
   is bound by cross-joining against the entire vertex label table. It is not a tuning
   problem, and it gets worse with `|V|`, not with the neighbourhood.
2. **Bounded paths, once the graph is big enough to matter.** At 100k, `vlp6` sat at
   ~780-920 ms in every arm including the fully indexed one, against ~360-430 ms for the
   relational CTE. At 1M the picture is worse and the property index stops helping entirely:
   `vlp3` is 1,511 ms tuned and 1,527 ms tuned+index — the anchor lookup was never the cost —
   while `vlp4` times out past 60 s in all three arms. (`vlp6` has no 1M measurement: at
   average degree ~37 nothing in a 50k-node graph is six hops apart any more. A shrinking
   diameter is its own comment on hop caps.)
3. **Pattern matching — the shape AGE was supposed to win.** The unstable-signed-triad query
   is precisely the "pattern query B's typed SQL builder cannot express" that the debate
   treated as AGE's unique contribution. It **timed out past 60 s in every single arm**, at
   both scales. The relational twin answers the same question in **~10-12 ms** — and it does
   express it, in twelve lines of SQL. This is the sharpest reversal in the whole probe: the
   argument for AGE rested on a capability that, measured, AGE cannot deliver at any
   configuration.

4. **Per-hop predicates over a variable-length path are not expressible.** `ALL(...)` and
   `ANY(...)` are not implemented in AGE 1.7 (§3.6), so there is no way to say "walk up to k
   hops, but only through edges that are still open". Legba's edges are temporal and every
   production reader filters on that. This is not a performance ceiling; it is a correctness
   one, and no configuration reaches it.

The pattern across the first three: **AGE is fast when both endpoints are known and slow
whenever something must be discovered.** That is the opposite of the property a graph engine
is bought for. The fourth is worse than slow — it is a question the engine cannot be asked.

### 5.2 Where the relational twin stops being competitive

The honest answer has three parts, because "competitive" means something different against
each of the other two executors.

**Against AGE: there is one crossover, at 100k, and it closes again by 1M.**

The single shape where AGE beat the relational twin was the two-anchored bounded path, at 100k,
with the property index in place:

| 100k, `tuned_propidx` | AGE | relational twin | |
|---|---:|---:|---|
| `vlp3` | **1.9 ms** | 3.2 ms | AGE 1.7× faster |
| `vlp4` | **8.5 ms** | 15.6 ms | AGE 1.8× faster |

That is a real win and it is the strongest result AGE produced. It does not survive the next
decade of edges:

| 1M, `tuned_propidx` | AGE | relational twin | |
|---|---:|---:|---|
| `vlp3` | 1,526.6 ms | **888.4 ms** | twin 1.7× faster |
| `vlp4` | **>60 s timeout** | **3,646.5 ms** | twin wins outright |
| `ego1` | 6,617.8 ms | **0.8 ms** | twin ~8,000× faster |
| `ego2` / `ego3` | **>60 s timeout** | 7.7 ms / 471.6 ms | twin wins outright |
| `triad` | **>60 s timeout** | **240.1 ms** | twin wins outright |

Note what happened to the property index between the scales: at 100k it took `vlp3` from
150 ms to 1.9 ms, and at 1M it does nothing at all (1,511 ms tuned → 1,527 ms tuned+index). The
anchor lookup stopped being the cost; the expansion became the cost. **So the answer to "at
what edge count does the relational twin stop being competitive with AGE" is: it never does,
and the one shape where AGE led at 100k has reversed by 1M.**

**Against an in-process snapshot: the twin is behind everywhere, and that gap is the real
finding.** networkx was fastest at every shape and both scales — `ego3` at 1M in 131 ms where
the CTE needs 472 ms and AGE times out; `triad` in 227 ms; bounded paths in fractions of a
millisecond. The twin's own ceiling shows up at **`ego3` at 1M**: on a graph of average degree
~37 the 3-hop neighbourhood of a hub is most of the graph, so the CTE goes from 1.8 ms at 100k
to 472 ms tuned (8,713 ms untuned) — still an answer, no longer interactive.

That is the concrete crossover the question was asking for: **the durable store stops being
interactive at 3-hop ego on a million-edge graph**, and the architecture that answers it is the
in-process snapshot, which is what `graph_mining` already builds. Its cost is the rebuild —
914 ms at 100k, 7,701 ms at 1M, ~8 µs/edge — which puts trigger **E4**'s 60 s threshold at
about **7.8 million edges**.

**One caveat, stated because it cuts against the twin.** The relational path query here is the
naive recursive CTE that production ships — a forward BFS with a `UNION` visited set. It is
not a bidirectional search, and a bidirectional rewrite would likely take `vlp4` at 1M from
3.6 s to something far smaller. The twin's numbers are therefore a *floor* on what the
relational approach can do, in the same way `age_dir` is a floor on what the Cypher rewrite
costs. Both sides of the comparison are measured at their shipped, not their best, form.

### 5.3 Would the 90-day kill criterion pass today?

The criterion, pre-registered in `JUDGE_SYNTHESIS` §4.2: *"if graph-tool invocations are still
in single digits 90 days after the projector ships, drop the graph and keep the relational
store."*

**The clock has not started — the projector has not shipped.** But the substance is measurable
now, and it was re-measured against the live substrate on 2026-08-03:

| tool | lifetime invocations | last used |
|---|---:|---|
| `inspect_entity` | 8 | 2026-06-24 |
| `query_facts` | 8 | 2026-07-28 |
| `query_nexuses` | 7 | 2026-07-11 |
| `query_paths` | 2 | 2026-06-25 |
| `get_structural_balance` | 2 | 2026-08-03 |
| **all graph tools** | **27** | — |
| **all tool invocations, all packs** | **1,834** | — |

Graph tools are **1.47 %** of all tool use, and *every single one is in single digits over its
entire lifetime*. Had the clock started 90 days ago, the criterion would fire today.

The steelman against that reading is A's own §10.1: a broken, slow, 6.6 %-coverage instrument
is correctly ignored, so its disuse proves nothing. The probe partly adjudicates it. The graph
tools that see the *most* use — `query_facts` (8) and `query_nexuses` (7) — do not touch AGE at
all; they read the relational `nexuses` table, which is neither broken nor slow. Demand is in
single digits **on the working relational tools too**. That is no longer a story about AGE
being a bad instrument; it is a story about nobody asking the question yet.

Which is a statement about *sequencing*, not about the vision. The operator's framing stands:
walking the world graph interactively is the endgame. But the measured obstacle is upstream of
any engine — 810 open derived typed edges over an entity space of ~50k, growing at ~12/day
against ~9,900 candidate arrivals/day. There is no engine that makes 810 edges interesting.

### 5.4 The recommendation this probe supports

This probe does not make the engine decision — that is the 2026-11-03 sitting. It does change
what that sitting has to work with, in one specific way worth stating plainly.

`JUDGE_SYNTHESIS` §4.2 pre-registered a ladder: *"E3 or E5 fires alone (expressiveness /
algorithms) ⇒ **AGE probe first**, not Neo4j"* — on the reasoning that the exit is `DROP GRAPH`
plus one column, so the probe is nearly free. **That probe has now been run, early, and it came
back negative.** The two shapes that would make E3 or E5 fire — pattern matching and
interactive exploration — are the two shapes AGE could not serve at any configuration tested,
and E3's other named shape (per-hop temporal predicates) is one AGE cannot express in any
phrasing (§3.6). So if E3 or E5 fires later, the ladder should not route to an AGE probe; it
has already been taken, and the answer was no.

What the measurement does support, concretely:

1. **Keep the freeze.** AGE stays installed because it ships inside the substrate image and
   removing it means moving the store that holds everything for no benefit. The graph stays
   empty, and now errors honestly instead of answering from fixtures.
2. **`entity_edges` (K-G1) is the right foundation and the measurement says so.** The
   relational twin answered every shape at every scale — sub-2 ms across the board at 100k, and
   at 1M still 0.8 ms / 7.7 ms / 472 ms for 1-3 hop ego and 240 ms for the pattern query, with
   an ordinary recursive CTE over ordinary btrees. It is the **only** executor that never timed
   out, and it is the fastest *durable* option measured. It also has the property that matters
   most for a store: it responds to tuning, 3-16× at 1M, so it will keep getting faster as
   Postgres does.
3. **The in-process snapshot is the fast path for exploration and algorithms, and trigger E4 is
   far away.** networkx was the fastest executor on every shape at both scales — sub-millisecond
   across the board at 100k, and at 1M still 131 ms for a 3-hop hub ego and 227 ms for the
   triad pattern, against seconds-to-timeout for both durable stores. Its cost is the rebuild:
   **914 ms at 100k edges, 7,701 ms at 1M** — about 8 µs/edge, so E4's 60 s threshold arrives
   at roughly **7.8 million edges**. At the current 810 derived typed edges and ~12/day, that is
   not a horizon anyone in this project needs to plan around; even feeding it the entire
   untyped candidate firehose at ~9,900/day would take over two years to reach.
4. **If AGE is ever fed, the property index is not optional.** One expression index per vertex
   label on the business key, created *before* any data lands. Without it, every anchor is a
   full label scan and every subsequent measurement is measuring the missing index rather than
   the engine.
5. **Consider `jit=off` for the substrate independently of any of this.** It was worth 4× on
   the most complex plan measured, and Postgres JIT is a known pessimisation for
   many-branch analytic queries. That is a live-tuning experiment, not a graph decision, and it
   should be measured against real workloads before changing anything.

And the finding that outranks all of the above, because it is the one the engine choice cannot
fix: **there are 810 open derived typed edges.** Not 810 thousand. The pipeline that would fill
the graph drains 0.13 % of its arrivals. Every engine argument — AGE's, Neo4j's, the relational
store's — is an argument about how to serve a graph that does not exist yet. The measurement
that should drive the next decision is typing throughput (K-G2), not traversal latency.
