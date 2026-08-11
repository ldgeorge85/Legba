# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G3 · The measured AGE probe — load, then benchmark, against a SCRATCH instance.

Every measurement has a **relational twin**: the same synthetic world graph is
loaded twice into the same Postgres instance — once as an Apache-AGE graph
(id-keyed vertices, per A_age_commit.md §3.1's design: the vertex business key
is the entity uuid, never a name) and once as an ``entity_edges``-shaped
relational table with the indexes JUDGE_SYNTHESIS §4.1 specifies. Three
executors answer the same questions:

* ``age``   — Cypher through ``ag_catalog.cypher()``, the production idiom
* ``sql``   — a recursive CTE / self-join over the relational twin
* ``nx``    — an in-process ``networkx`` snapshot of the relational twin
              (this is also the gauge for pre-registered trigger **E4**)

Question shapes, chosen to match what Legba actually asks:

* ``ego1/ego2/ego3``  — "what is around this actor", 1-3 hops (the UI verb)
* ``vlp3/vlp4/vlp6``  — bounded variable-length path between two actors
                        (``/graph/path``'s verb, at and beyond the shipped cap)
* ``triad``           — "unstable signed triads touching entity class X"
                        (structural_balance's verb; the pattern query that
                        B's typed SQL builder is said not to express)

A fourth "executor", ``age_dir``, is the same question rewritten with every
edge direction spelled out — the workaround that recovers AGE's edge indexes
(see :func:`age_ego_directed`). It is reported separately because it is not
Cypher anyone would write by hand; it exists to separate "AGE is slow" from
"AGE's planner will not use the index for THIS phrasing".

Subcommands::

    load     --dsn ... --dir <generated csvs>
    measure  --dsn ... --dir ... --scale 100k --label default --out results.jsonl
    nx       --dir <generated csvs> --scale 100k --out results.jsonl
    report   --results results.jsonl        # renders the markdown table

The workload (seed vertices spanning the degree deciles, and anchor pairs at
known BFS distance) is derived deterministically from the graph itself and
cached next to the CSVs, so every arm measures the SAME work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

import asyncpg

GRAPH = "probe_graph"
NODES_T = "probe_nodes"
EDGES_T = "probe_entity_edges"
FAMILIES = ("relation", "reference", "cooccurrence", "structural")
PATTERN_CLASS = "country"


# ---------------------------------------------------------------------------
# connection helpers
# ---------------------------------------------------------------------------
async def prep(conn: asyncpg.Connection) -> None:
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')


# ---------------------------------------------------------------------------
# LOAD
# ---------------------------------------------------------------------------
TWIN_DDL = f"""
DROP TABLE IF EXISTS {EDGES_T};
DROP TABLE IF EXISTS {NODES_T};
CREATE TABLE {NODES_T} (
    id           uuid PRIMARY KEY,
    idx          int  NOT NULL,
    name         text NOT NULL,
    entity_class text NOT NULL
);
CREATE INDEX idx_probe_nodes_class ON {NODES_T} (entity_class);

-- Shape per JUDGE_SYNTHESIS §4.1 "The DDL, reconciled" (0143_entity_edges.sql),
-- trimmed to the columns a traversal plan can see.
CREATE TABLE {EDGES_T} (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_id        uuid NOT NULL REFERENCES {NODES_T}(id) ON DELETE CASCADE,
    dst_id        uuid NOT NULL REFERENCES {NODES_T}(id) ON DELETE CASCADE,
    edge_type     text NOT NULL,
    edge_family   text NOT NULL
        CHECK (edge_family IN ('relation','reference','cooccurrence','structural')),
    polarity      smallint NOT NULL DEFAULT 0 CHECK (polarity IN (-1,0,1)),
    confidence    real NOT NULL DEFAULT 0.5,
    valid_from    timestamptz,
    valid_until   timestamptz,
    superseded_by uuid,
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT probe_edges_no_self CHECK (src_id <> dst_id)
);
"""

TWIN_INDEXES = f"""
CREATE INDEX idx_probe_edges_out ON {EDGES_T} (src_id, edge_family, confidence DESC)
    WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX idx_probe_edges_in  ON {EDGES_T} (dst_id, edge_family, confidence DESC)
    WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX idx_probe_edges_signed ON {EDGES_T} (src_id, dst_id)
    WHERE polarity <> 0 AND valid_until IS NULL AND superseded_by IS NULL;
"""


async def cmd_load(dsn: str, data_dir: Path) -> None:
    conn = await asyncpg.connect(dsn, command_timeout=3600)
    try:
        await prep(conn)
        t_all = time.perf_counter()

        # ---- relational twin -------------------------------------------------
        t0 = time.perf_counter()
        await conn.execute(TWIN_DDL)
        with (data_dir / "entities.csv").open("rb") as fh:
            await conn.copy_to_table(
                NODES_T, source=fh, format="csv", header=True,
                columns=["idx", "id", "name", "entity_class"],
            )
        await conn.execute("DROP TABLE IF EXISTS probe_stage_edges")
        await conn.execute(
            """CREATE UNLOGGED TABLE probe_stage_edges (
                   src_uuid uuid, dst_uuid uuid, edge_family text, edge_type text,
                   polarity smallint, confidence real, is_open smallint)"""
        )
        with (data_dir / "edges.csv").open("rb") as fh:
            await conn.copy_to_table("probe_stage_edges", source=fh, format="csv", header=True)
        await conn.execute(
            f"""INSERT INTO {EDGES_T}
                    (src_id, dst_id, edge_type, edge_family, polarity, confidence,
                     valid_from, valid_until)
                SELECT src_uuid, dst_uuid, edge_type, edge_family, polarity, confidence,
                       now() - (random() * interval '400 days'),
                       CASE WHEN is_open = 1 THEN NULL ELSE now() END
                  FROM probe_stage_edges"""
        )
        t_rel_rows = time.perf_counter() - t0
        t0 = time.perf_counter()
        await conn.execute(TWIN_INDEXES)
        await conn.execute(f"ANALYZE {NODES_T}")
        await conn.execute(f"ANALYZE {EDGES_T}")
        t_rel_idx = time.perf_counter() - t0

        # ---- AGE graph -------------------------------------------------------
        t0 = time.perf_counter()
        exists = await conn.fetchval(
            "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = $1", GRAPH
        )
        if exists:
            await conn.execute(f"SELECT ag_catalog.drop_graph('{GRAPH}', true)")
        await conn.execute(f"SELECT ag_catalog.create_graph('{GRAPH}')")
        await conn.execute(f"SELECT ag_catalog.create_vlabel('{GRAPH}', 'Entity')")
        for fam in FAMILIES:
            await conn.execute(f"SELECT ag_catalog.create_elabel('{GRAPH}', '{fam}')")

        vlabel_id = await conn.fetchval(
            f"SELECT ag_catalog._label_id('{GRAPH}'::name, 'Entity'::name)"
        )
        # Vertices: business key is the entity uuid in properties.id — the
        # id-keyed contract A's §3.1 requires and the readers already assume.
        await conn.execute(
            f"""INSERT INTO {GRAPH}."Entity" (id, properties)
                SELECT ag_catalog._graphid({vlabel_id}, n.idx + 1),
                       ag_catalog.agtype_build_map(
                           'id', n.id::text, 'name', n.name, 'entity_class', n.entity_class)
                  FROM {NODES_T} n"""
        )
        n_nodes = await conn.fetchval(f'SELECT count(*) FROM {GRAPH}."Entity"')
        await conn.execute(
            f"""SELECT setval('{GRAPH}."Entity_id_seq"', {int(n_nodes)})"""
        )
        await conn.execute("DROP TABLE IF EXISTS probe_gid")
        await conn.execute(
            f"""CREATE UNLOGGED TABLE probe_gid AS
                SELECT n.id AS uuid, ag_catalog._graphid({vlabel_id}, n.idx + 1) AS gid
                  FROM {NODES_T} n"""
        )
        await conn.execute("ALTER TABLE probe_gid ADD PRIMARY KEY (uuid)")
        await conn.execute("ANALYZE probe_gid")
        t_age_v = time.perf_counter() - t0

        t0 = time.perf_counter()
        for fam in FAMILIES:
            await conn.execute(
                f"""INSERT INTO {GRAPH}."{fam}" (start_id, end_id, properties)
                    SELECT s.gid, d.gid,
                           ag_catalog.agtype_build_map(
                               'edge_type', e.edge_type,
                               'polarity', e.polarity::int,
                               'confidence', e.confidence::float8,
                               'is_open', e.is_open::int)
                      FROM probe_stage_edges e
                      JOIN probe_gid s ON s.uuid = e.src_uuid
                      JOIN probe_gid d ON d.uuid = e.dst_uuid
                     WHERE e.edge_family = '{fam}'"""
            )
        for fam in FAMILIES:
            await conn.execute(f'ANALYZE {GRAPH}."{fam}"')
        await conn.execute(f'ANALYZE {GRAPH}."Entity"')
        t_age_e = time.perf_counter() - t0

        # ---- verify the two twins agree -------------------------------------
        rel_edges = await conn.fetchval(f"SELECT count(*) FROM {EDGES_T}")
        age_edges = sum(
            [await conn.fetchval(f'SELECT count(*) FROM {GRAPH}."{f}"') for f in FAMILIES]
        )
        sample = await conn.fetchrow(f"SELECT id::text, name FROM {NODES_T} LIMIT 1")
        rt = await conn.fetch(
            f"""SELECT * FROM cypher('{GRAPH}',
                    $$ MATCH (n:Entity) WHERE n.id = '{sample['id']}' RETURN n.name $$)
                AS (name agtype)"""
        )
        sizes = {
            "relational_twin": await conn.fetchval(
                f"SELECT pg_size_pretty(pg_total_relation_size('{EDGES_T}'))"
            ),
            "age_graph": await conn.fetchval(
                f"""SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))::bigint)
                      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = '{GRAPH}' AND c.relkind = 'r'"""
            ),
        }
        print(json.dumps({
            "nodes": n_nodes,
            "relational_edges": rel_edges,
            "age_edges": age_edges,
            "twins_agree": rel_edges == age_edges,
            "age_readback": [r["name"] for r in rt],
            "readback_expected": sample["name"],
            "load_seconds": {
                "relational_rows": round(t_rel_rows, 2),
                "relational_indexes": round(t_rel_idx, 2),
                "age_vertices": round(t_age_v, 2),
                "age_edges": round(t_age_e, 2),
                "total": round(time.perf_counter() - t_all, 2),
            },
            "sizes": sizes,
        }, indent=2))
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# WORKLOAD — deterministic, derived from the graph, cached
# ---------------------------------------------------------------------------
def build_workload(data_dir: Path) -> dict[str, Any]:
    cache = data_dir / "workload.json"
    if cache.exists():
        return json.loads(cache.read_text())
    import csv as _csv

    import networkx as nx

    with (data_dir / "entities.csv").open(encoding="utf-8") as fh:
        nodes = list(_csv.DictReader(fh))
    g = nx.Graph()
    g.add_nodes_from(n["uuid"] for n in nodes)
    with (data_dir / "edges.csv").open(encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            if row["is_open"] == "1":
                g.add_edge(row["src_uuid"], row["dst_uuid"])

    by_deg = sorted(g.degree, key=lambda kv: (-kv[1], kv[0]))
    n = len(by_deg)
    # Seeds spanning the degree distribution: the top hub, then deciles.
    seed_idx = [0, 1, 2] + [int(n * f) for f in (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99)]
    seeds = [{"id": by_deg[i][0], "degree": by_deg[i][1]} for i in seed_idx if i < n]

    # Anchor pairs at EXACT BFS distance 3, 4 and 6, taken from a mid-degree
    # source so the pairs are not all hub-to-hub.
    #
    # A denser graph may simply have no pair at distance 6 — average degree ~37
    # at the 1M scale puts the whole 50k-node graph inside 4 hops — so the scan
    # is capped rather than open-ended, and a shape with no workload is recorded
    # as "n/a" rather than as a suspiciously fast zero.
    pairs: dict[str, list[dict[str, Any]]] = {"3": [], "4": [], "6": []}
    scan_cap = 80
    for src, _deg in by_deg[int(n * 0.02): int(n * 0.02) + scan_cap]:
        lengths = nx.single_source_shortest_path_length(g, src, cutoff=6)
        for want in (3, 4, 6):
            if len(pairs[str(want)]) >= 6:
                continue
            hit = next((t for t, d in lengths.items() if d == want), None)
            if hit:
                pairs[str(want)].append({"src": src, "dst": hit, "distance": want})
        if all(len(v) >= 6 for v in pairs.values()):
            break

    wl = {"seeds": seeds, "pairs": pairs, "pattern_class": PATTERN_CLASS}
    cache.write_text(json.dumps(wl, indent=2))
    return wl


# ---------------------------------------------------------------------------
# QUERIES
# ---------------------------------------------------------------------------
def age_ego(uuid_: str, k: int) -> str:
    """The natural Cypher ego query — an undirected variable-length match.

    **Known asymmetry, and it is not fixable in AGE 1.7.** The relational twin
    filters ``valid_until IS NULL`` on every hop because every production reader
    does; this query does not, because AGE has no way to express a per-hop
    predicate over a variable-length match — ``ALL(x IN r WHERE x.is_open = 1)``
    is a syntax error (``ALL``/``ANY`` list predicates are unimplemented). So
    the AGE side walks ~8.7% more edges and returns a correspondingly larger
    set. Measured at 1M, 2 hops: AGE 3,861 vertices vs the twin's 2,671, and
    dropping the twin's temporal filter reproduces AGE's number exactly.

    The asymmetry is recorded rather than corrected because the inability to
    correct it IS the finding — see docs/AGE_PROBE_REPORT.md §3.6.
    """
    return (
        f"""SELECT * FROM cypher('{GRAPH}',
              $$ MATCH (a:Entity)-[*1..{k}]-(b:Entity) WHERE a.id = '{uuid_}'
                 WITH DISTINCT b RETURN count(b) $$) AS (n agtype)"""
    )


def age_ego_directed(uuid_: str, k: int) -> str:
    """The direction-EXPLICIT rewrite of the same ego question.

    AGE's planner will not push an anchored-vertex predicate through an
    UNDIRECTED or variable-length edge match (measured: it seq-scans every
    edge label table), but it does use ``<label>_start_id_idx`` /
    ``_end_id_idx`` on a fixed-length DIRECTED hop. Spelling every direction
    combination out therefore recovers index-driven expansion — at the cost of
    ``2^k`` UNION branches, which is why this is a workaround, not a fix.

    **This is a LOWER BOUND on the rewrite's real cost, deliberately.** It
    enumerates the direction combinations at length exactly ``k``, whereas
    ``[*1..k]`` returns everything at length 1 THROUGH k. A semantically
    equivalent rewrite must union the shorter lengths too — ``2 + 4 + … + 2^k``
    branches — so the honest number is worse than what this measures. The
    comparison is kept favourable to AGE on purpose: the conclusion should not
    depend on having stacked the deck.
    """
    branches = []
    for mask in range(2 ** k):
        pat = "(a:Entity)"
        for hop in range(k):
            var = f"x{hop}" if hop < k - 1 else "b"
            if (mask >> hop) & 1:
                pat += f"-[]->({var}:Entity)"
            else:
                pat += f"<-[]-({var}:Entity)"
        branches.append(f"MATCH {pat} WHERE a.id = '{uuid_}' RETURN b.id AS bid")
    body = " UNION ".join(branches)
    return f"""SELECT count(*) FROM (SELECT * FROM cypher('{GRAPH}',
                 $$ {body} $$) AS (bid agtype)) q"""


def sql_ego(k: int) -> str:
    return f"""
        WITH RECURSIVE ego(node, depth) AS (
            SELECT $1::uuid, 0
          UNION
            SELECT CASE WHEN e.src_id = ego.node THEN e.dst_id ELSE e.src_id END,
                   ego.depth + 1
              FROM ego
              JOIN {EDGES_T} e
                ON (e.src_id = ego.node OR e.dst_id = ego.node)
             WHERE ego.depth < {k}
               AND e.valid_until IS NULL AND e.superseded_by IS NULL
        )
        SELECT count(*) FROM (SELECT DISTINCT node FROM ego WHERE node <> $1::uuid) s
    """


def age_vlp(src: str, dst: str, k: int) -> str:
    # The production form: graph_paths.build_shortest_path_cypher.
    return (
        f"""SELECT * FROM cypher('{GRAPH}',
              $$ MATCH p = (a:Entity)-[*1..{k}]-(b:Entity)
                 WHERE a.id = '{src}' AND b.id = '{dst}'
                 RETURN length(p) AS path_len ORDER BY path_len ASC LIMIT 1 $$)
            AS (path_len agtype)"""
    )


def sql_vlp(k: int) -> str:
    return f"""
        WITH RECURSIVE walk(node, depth) AS (
            SELECT $1::uuid, 0
          UNION
            SELECT CASE WHEN e.src_id = w.node THEN e.dst_id ELSE e.src_id END,
                   w.depth + 1
              FROM walk w
              JOIN {EDGES_T} e
                ON (e.src_id = w.node OR e.dst_id = w.node)
             WHERE w.depth < {k}
               AND e.valid_until IS NULL AND e.superseded_by IS NULL
        )
        SELECT min(depth) FROM walk WHERE node = $2::uuid
    """


AGE_TRIAD = f"""
    SELECT * FROM cypher('{GRAPH}', $$
        MATCH (a:Entity)-[r1:relation]-(b:Entity)-[r2:relation]-(c:Entity)-[r3:relation]-(a)
        WHERE a.entity_class = '{PATTERN_CLASS}'
          AND r1.polarity <> 0 AND r2.polarity <> 0 AND r3.polarity <> 0
          AND r1.polarity * r2.polarity * r3.polarity < 0
        RETURN count(*) $$) AS (n agtype)
"""

SQL_TRIAD = f"""
    WITH s AS (
        SELECT LEAST(src_id, dst_id) AS a, GREATEST(src_id, dst_id) AS b, polarity
          FROM {EDGES_T}
         WHERE edge_family = 'relation' AND polarity <> 0
           AND valid_until IS NULL AND superseded_by IS NULL
    )
    SELECT count(*)
      FROM s e1
      JOIN s e2 ON e2.a = e1.b
      JOIN s e3 ON e3.a = e1.a AND e3.b = e2.b
     WHERE e1.polarity * e2.polarity * e3.polarity < 0
       AND EXISTS (
             SELECT 1 FROM {NODES_T} n
              WHERE n.entity_class = '{PATTERN_CLASS}'
                AND n.id IN (e1.a, e1.b, e2.b))
"""


# ---------------------------------------------------------------------------
# MEASURE
# ---------------------------------------------------------------------------
async def _timed(conn: asyncpg.Connection, sql: str, *args: Any) -> tuple[float, bool]:
    t0 = time.perf_counter()
    try:
        await conn.fetch(sql, *args)
        return (time.perf_counter() - t0) * 1000.0, False
    except asyncpg.QueryCanceledError:
        return (time.perf_counter() - t0) * 1000.0, True


def _pcts(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "n": 0}
    s = sorted(samples)
    idx = min(len(s) - 1, int(round(0.95 * (len(s) - 1))))
    return {"p50": round(statistics.median(s), 1), "p95": round(s[idx], 1), "n": len(s)}


PROP_INDEX = "idx_probe_entity_prop_id"
PROP_INDEX_DDL = (
    f'CREATE INDEX {PROP_INDEX} ON {GRAPH}."Entity" '
    "(ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties, '\"id\"'::agtype]))"
)


async def _set_prop_index(conn: asyncpg.Connection, want: bool) -> bool:
    """Ensure the AGE vertex property index on ``properties->'id'`` is on/off.

    AGE creates label tables with a PK on the internal ``graphid`` and btrees on
    ``start_id``/``end_id`` — and NOTHING on ``properties``. Every
    ``WHERE a.id = '<uuid>'`` anchor is therefore a sequential scan of the
    vertex label table until this expression index exists.
    """
    present = bool(await conn.fetchval(
        "SELECT count(*) FROM pg_indexes WHERE schemaname = $1 AND indexname = $2",
        GRAPH, PROP_INDEX,
    ))
    if want and not present:
        await conn.execute(PROP_INDEX_DDL)
        await conn.execute(f'ANALYZE {GRAPH}."Entity"')
    elif not want and present:
        await conn.execute(f'DROP INDEX {GRAPH}."{PROP_INDEX}"')
        await conn.execute(f'ANALYZE {GRAPH}."Entity"')
    return want


def _specs(wl: dict[str, Any]) -> list[dict[str, Any]]:
    """The measurement matrix: (query, engine) -> the list of statements to time."""
    specs: list[dict[str, Any]] = []
    seeds = [s["id"] for s in wl["seeds"]]
    for k in (1, 2, 3):
        specs.append({"query": f"ego{k}", "engine": "age",
                      "runs": [(age_ego(s, k), ()) for s in seeds]})
        specs.append({"query": f"ego{k}", "engine": "age_dir",
                      "runs": [(age_ego_directed(s, k), ()) for s in seeds]})
        specs.append({"query": f"ego{k}", "engine": "sql",
                      "runs": [(sql_ego(k), (s,)) for s in seeds]})
    for k in (3, 4, 6):
        pairs = wl["pairs"][str(k)]
        specs.append({"query": f"vlp{k}", "engine": "age",
                      "runs": [(age_vlp(p["src"], p["dst"], k), ()) for p in pairs]})
        specs.append({"query": f"vlp{k}", "engine": "sql",
                      "runs": [(sql_vlp(k), (p["src"], p["dst"])) for p in pairs]})
    specs.append({"query": "triad", "engine": "age", "runs": [(AGE_TRIAD, ())]})
    specs.append({"query": "triad", "engine": "sql", "runs": [(SQL_TRIAD, ())]})
    return specs


async def cmd_measure(
    dsn: str, data_dir: Path, scale: str, label: str, out: Path,
    repeats: int, timeout_ms: int, only: list[str] | None, prop_index: bool,
    budget_ms: float,
) -> None:
    wl = build_workload(data_dir)
    results: list[dict[str, Any]] = []
    conn = await asyncpg.connect(dsn, command_timeout=timeout_ms / 1000.0 + 60)
    try:
        await prep(conn)
        has_prop_idx = await _set_prop_index(conn, prop_index)
        await conn.execute(f"SET statement_timeout = {timeout_ms}")
        settings = {
            k: await conn.fetchval(f"SHOW {k}")
            for k in (
                "shared_buffers", "work_mem", "effective_cache_size",
                "max_parallel_workers_per_gather", "jit", "random_page_cost",
            )
        }
        print(f"### arm={label} scale={scale} age_property_index={has_prop_idx}")
        print(f"### settings={settings}")

        for spec in _specs(wl):
            if only and spec["query"] not in only:
                continue
            if not spec["runs"]:
                # No workload for this shape — e.g. a denser graph has no pair
                # at BFS distance 6 because its diameter shrank. Say so; do NOT
                # emit a 0.0 ms cell, which would read as "infinitely fast".
                results.append({
                    "scale": scale, "arm": label, "engine": spec["engine"],
                    "query": spec["query"], "no_workload": True,
                    "p50": 0.0, "p95": 0.0, "n": 0, "timed_out": False,
                    "truncated": False, "settings": settings,
                    "age_property_index": has_prop_idx,
                })
                print(f"  {spec['engine']:<8} {spec['query']:<7} NO WORKLOAD (no pair at this distance)")
                continue
            samples: list[float] = []
            timed_out = False
            spent = 0.0
            truncated = False
            for sql, args in spec["runs"]:
                if spent > budget_ms:
                    truncated = True
                    break
                ms, to = await _timed(conn, sql, *args)  # warm-up, discarded
                spent += ms
                if to:
                    timed_out = True
                    break
                for _ in range(repeats):
                    m, t = await _timed(conn, sql, *args)
                    spent += m
                    if t:
                        timed_out = True
                        break
                    samples.append(m)
                    if spent > budget_ms:
                        truncated = True
                        break
                if timed_out:
                    break
            row: dict[str, Any] = {
                "scale": scale, "arm": label, "engine": spec["engine"],
                "query": spec["query"], "timed_out": timed_out,
                "truncated": truncated, "settings": settings,
                "age_property_index": has_prop_idx,
            }
            row.update(
                _pcts(samples) if not timed_out
                else {"p50": float(timeout_ms), "p95": float(timeout_ms), "n": len(samples)}
            )
            results.append(row)
            shown = (f"TIMEOUT >{timeout_ms} ms" if timed_out
                     else f"p50={row['p50']:>10.1f}  p95={row['p95']:>10.1f} ms  (n={row['n']}"
                          + (", budget-truncated)" if truncated else ")"))
            print(f"  {spec['engine']:<8} {spec['query']:<7} {shown}")
    finally:
        await conn.close()

    with out.open("a", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")
    print(f"appended {len(results)} rows -> {out}")


# ---------------------------------------------------------------------------
# NETWORKX arm — the in-process twin, and the E4 snapshot gauge
# ---------------------------------------------------------------------------
def cmd_nx(data_dir: Path, scale: str, out: Path, repeats: int) -> None:
    import csv as _csv

    import networkx as nx

    wl = build_workload(data_dir)
    results: list[dict[str, Any]] = []

    t0 = time.perf_counter()
    g = nx.Graph()
    signed = nx.Graph()
    classes: dict[str, str] = {}
    with (data_dir / "entities.csv").open(encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            classes[row["uuid"]] = row["entity_class"]
            g.add_node(row["uuid"])
    with (data_dir / "edges.csv").open(encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            if row["is_open"] != "1":
                continue
            g.add_edge(row["src_uuid"], row["dst_uuid"])
            if row["edge_family"] == "relation" and row["polarity"] != "0":
                signed.add_edge(row["src_uuid"], row["dst_uuid"], polarity=int(row["polarity"]))
    build_ms = (time.perf_counter() - t0) * 1000.0
    results.append({
        "scale": scale, "arm": "in_process", "engine": "nx", "query": "snapshot_rebuild",
        "p50": round(build_ms, 1), "p95": round(build_ms, 1), "n": 1, "timed_out": False,
        "note": "pre-registered trigger E4 gauge (full in-process rebuild)",
    })
    print(f"  nx   snapshot_rebuild {build_ms:.1f} ms  ({g.number_of_nodes()} nodes / {g.number_of_edges()} edges)")

    for k in (1, 2, 3):
        samples = []
        for seed in wl["seeds"]:
            for _ in range(repeats):
                t = time.perf_counter()
                nx.single_source_shortest_path_length(g, seed["id"], cutoff=k)
                samples.append((time.perf_counter() - t) * 1000.0)
        results.append({"scale": scale, "arm": "in_process", "engine": "nx",
                        "query": f"ego{k}", **_pcts(samples), "timed_out": False})
        print(f"  nx   ego{k}    p50={results[-1]['p50']:>9.1f} p95={results[-1]['p95']:>9.1f} ms")

    for k in (3, 4, 6):
        if not wl["pairs"][str(k)]:
            # The denser the graph, the smaller its diameter: at 1M edges over
            # 50k entities nothing is 6 hops apart. Record the absence; a 0.0 ms
            # cell would read as "instant" rather than "never asked".
            results.append({"scale": scale, "arm": "in_process", "engine": "nx",
                            "query": f"vlp{k}", "no_workload": True,
                            "p50": 0.0, "p95": 0.0, "n": 0, "timed_out": False})
            print(f"  nx   vlp{k}    NO WORKLOAD (no pair at this distance)")
            continue
        samples = []
        for pair in wl["pairs"][str(k)]:
            for _ in range(repeats):
                t = time.perf_counter()
                try:
                    nx.bidirectional_shortest_path(g, pair["src"], pair["dst"])
                except nx.NetworkXNoPath:
                    pass
                samples.append((time.perf_counter() - t) * 1000.0)
        results.append({"scale": scale, "arm": "in_process", "engine": "nx",
                        "query": f"vlp{k}", **_pcts(samples), "timed_out": False})
        print(f"  nx   vlp{k}    p50={results[-1]['p50']:>9.1f} p95={results[-1]['p95']:>9.1f} ms")

    samples = []
    for _ in range(max(1, repeats // 2)):
        t = time.perf_counter()
        count = 0
        for a, b, d1 in signed.edges(data=True):
            for c in nx.common_neighbors(signed, a, b):
                if c <= b:
                    continue
                p = d1["polarity"] * signed[b][c]["polarity"] * signed[a][c]["polarity"]
                if p < 0 and PATTERN_CLASS in (classes.get(a), classes.get(b), classes.get(c)):
                    count += 1
        samples.append((time.perf_counter() - t) * 1000.0)
    results.append({"scale": scale, "arm": "in_process", "engine": "nx",
                    "query": "triad", **_pcts(samples), "timed_out": False,
                    "note": f"unstable_triads={count}"})
    print(f"  nx   triad   p50={results[-1]['p50']:>9.1f} p95={results[-1]['p95']:>9.1f} ms (found {count})")

    with out.open("a", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")
    print(f"appended {len(results)} rows -> {out}")


# ---------------------------------------------------------------------------
# REPORT — render the results file as the markdown table the report carries
# ---------------------------------------------------------------------------
_QUERY_ORDER = ["ego1", "ego2", "ego3", "vlp3", "vlp4", "vlp6", "triad"]
_ARM_ORDER = ["default", "tuned", "tuned_propidx", "jit_off", "parallel8", "workmem256"]


def _cell(row: dict[str, Any] | None) -> str:
    if row is None:
        return "—"
    if row.get("no_workload"):
        return "n/a"
    if row.get("timed_out"):
        return f"**>{int(row['p50'] / 1000)}s TO**"
    mark = "†" if row.get("truncated") else ""
    return f"{row['p50']:,.1f} / {row['p95']:,.1f}{mark}"


def cmd_report(results: Path) -> None:
    rows = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    scales = sorted({r["scale"] for r in rows}, key=lambda s: (len(s), s))
    idx: dict[tuple, dict] = {
        (r["scale"], r["arm"], r["engine"], r["query"]): r for r in rows
    }
    engines = ["age", "age_dir", "sql", "nx"]
    for scale in scales:
        arms = [a for a in _ARM_ORDER if any(r["arm"] == a and r["scale"] == scale for r in rows)]
        print(f"\n### {scale} edges — p50 / p95 milliseconds\n")
        header = "| query | engine | " + " | ".join(arms) + " | in-process nx |"
        print(header)
        print("|---|---|" + "---|" * (len(arms) + 1))
        for q in _QUERY_ORDER:
            for eng in engines:
                if eng == "nx":
                    continue
                if not any(
                    (scale, a, eng, q) in idx for a in arms
                ):
                    continue
                cells = [_cell(idx.get((scale, a, eng, q))) for a in arms]
                nx_row = idx.get((scale, "in_process", "nx", q)) if eng == "sql" else None
                print(f"| {q} | {eng} | " + " | ".join(cells) + f" | {_cell(nx_row)} |")
        snap = idx.get((scale, "in_process", "nx", "snapshot_rebuild"))
        if snap:
            print(f"\n`snapshot_rebuild` (trigger **E4** gauge): {snap['p50']:,.0f} ms")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report")
    p.add_argument("--results", required=True)

    p = sub.add_parser("load")
    p.add_argument("--dsn", required=True)
    p.add_argument("--dir", required=True)

    p = sub.add_parser("measure")
    p.add_argument("--dsn", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--scale", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--timeout-ms", type=int, default=60_000)
    p.add_argument("--only", default=None, help="comma-separated query names")
    p.add_argument("--prop-index", choices=["on", "off"], default="off",
                   help="AGE vertex property index on properties->'id'")
    p.add_argument("--budget-ms", type=float, default=180_000,
                   help="wall-clock budget per (query, engine) cell")

    p = sub.add_parser("nx")
    p.add_argument("--dir", required=True)
    p.add_argument("--scale", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--repeats", type=int, default=5)

    args = ap.parse_args()
    if args.cmd == "report":
        cmd_report(Path(args.results))
    elif args.cmd == "load":
        asyncio.run(cmd_load(args.dsn, Path(args.dir)))
    elif args.cmd == "measure":
        asyncio.run(cmd_measure(
            args.dsn, Path(args.dir), args.scale, args.label, Path(args.out),
            args.repeats, args.timeout_ms,
            args.only.split(",") if args.only else None,
            args.prop_index == "on", args.budget_ms,
        ))
    else:
        cmd_nx(Path(args.dir), args.scale, Path(args.out), args.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
