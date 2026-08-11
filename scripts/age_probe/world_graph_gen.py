# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G3 · Synthetic world-graph generator for the AGE probe.

Emits a graph whose SHAPE matches Legba's measured world graph rather than a
uniform-random one, because traversal cost is dominated by the degree tail:

* **Power-law degree** via preferential attachment. The first ``V-1`` edges
  form a preferentially-attached spanning tree (so the graph is CONNECTED and
  path queries have answers), the remainder are hub-biased on both endpoints.
* **Entity classes** in Legba's proportions — a handful of ``country`` nodes
  that are super-hubs (Wikidata IGO membership makes countries the densest
  vertices in the live graph), then organisations, people, places, groups.
  Country nodes are pre-loaded into the attachment urn so they hub naturally.
* **The four `edge_family` tiers** the judge reconciled (JUDGE_SYNTHESIS
  §4.1 decision 3): ``relation`` (derived typed, signed) · ``reference``
  (imported seed lattice) · ``cooccurrence`` (the co-mention firehose) ·
  ``structural``. Mix follows the live ratio — cooccurrence dominant,
  relation sparse and precious.
* **Signed polarity** on the ``relation`` family only, so the structural-
  balance / unstable-triad pattern query has real signal to find.
* **A temporal tail**: ~8 % of edges are closed (``valid_until`` set), so
  every query carries the same ``WHERE valid_until IS NULL`` predicate the
  production readers carry.

Deterministic for a given ``--seed``: the same CSVs come out every time, so a
measurement can be re-run and compared.

Usage::

    python3 scripts/age_probe/world_graph_gen.py \
        --entities 50000 --edges 1000000 --out /tmp/probe_1m
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import uuid
from pathlib import Path

# Live-shaped class mix. Countries are few and are the hubs.
ENTITY_CLASSES: list[tuple[str, float]] = [
    ("country", 0.004),
    ("organization", 0.22),
    ("person", 0.44),
    ("place", 0.21),
    ("group", 0.126),
]

# JUDGE_SYNTHESIS §4.1 decision 3 tiers, in roughly the live ratio
# (cooccurrence is the 9.9k/day firehose; relation is 12/day and precious).
EDGE_FAMILIES: list[tuple[str, float]] = [
    ("cooccurrence", 0.62),
    ("reference", 0.20),
    ("relation", 0.13),
    ("structural", 0.05),
]

EDGE_TYPES: dict[str, list[str]] = {
    "relation": [
        "allied with", "hostile to", "member of", "employed by",
        "located in", "supplies", "sanctions", "negotiating with",
    ],
    "reference": ["member of igo", "signatory of", "headquartered in", "cited alongside"],
    "cooccurrence": ["co occurs"],
    "structural": ["subsidiary of", "bears on", "successor of"],
}

# Namespace so entity uuids are reproducible across runs.
_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def entity_uuid(i: int) -> str:
    return str(uuid.uuid5(_NS, f"probe-entity-{i}"))


def _weighted_classes(rng: random.Random, n: int) -> list[str]:
    names = [c for c, _ in ENTITY_CLASSES]
    weights = [w for _, w in ENTITY_CLASSES]
    return rng.choices(names, weights=weights, k=n)


def generate(entities: int, edges: int, out_dir: Path, seed: int) -> dict[str, int]:
    if edges < entities - 1:
        raise SystemExit(f"--edges ({edges}) must be >= --entities-1 ({entities - 1})")
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    classes = _weighted_classes(rng, entities)
    uuids = [entity_uuid(i) for i in range(entities)]

    with (out_dir / "entities.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["idx", "uuid", "name", "entity_class"])
        for i in range(entities):
            w.writerow([i, uuids[i], f"{classes[i].title()} {i:06d}", classes[i]])

    # Preferential-attachment urn. Countries seeded heavily so the degree tail
    # is anchored on them, as in the live graph.
    urn: list[int] = []
    for i in range(entities):
        if classes[i] == "country":
            urn.extend([i] * 24)
        elif classes[i] == "organization":
            urn.extend([i] * 2)

    fam_names = [f for f, _ in EDGE_FAMILIES]
    fam_weights = [w for _, w in EDGE_FAMILIES]

    def pick() -> int:
        # 12 % uniform keeps the tail from collapsing onto pure BA.
        if urn and rng.random() > 0.12:
            return urn[rng.randrange(len(urn))]
        return rng.randrange(entities)

    seen: set[tuple[int, int]] = set()
    counts = {f: 0 for f in fam_names}
    written = 0

    with (out_dir / "edges.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "src_uuid", "dst_uuid", "edge_family", "edge_type",
            "polarity", "confidence", "is_open",
        ])

        def emit(a: int, b: int) -> bool:
            nonlocal written
            key = (a, b) if a < b else (b, a)
            if a == b or key in seen:
                return False
            seen.add(key)
            fam = rng.choices(fam_names, weights=fam_weights, k=1)[0]
            etype = rng.choice(EDGE_TYPES[fam])
            if fam == "relation":
                polarity = rng.choices([-1, 0, 1], weights=[0.26, 0.52, 0.22], k=1)[0]
            elif fam == "reference":
                polarity = rng.choices([0, 1], weights=[0.9, 0.1], k=1)[0]
            else:
                polarity = 0
            conf = round(min(0.99, max(0.05, rng.betavariate(5, 3))), 4)
            is_open = 0 if rng.random() < 0.08 else 1
            w.writerow([uuids[a], uuids[b], fam, etype, polarity, conf, is_open])
            counts[fam] += 1
            written += 1
            urn.append(a)
            urn.append(b)
            return True

        # Phase 1 — preferentially-attached spanning tree => connected graph.
        urn.append(0)
        for i in range(1, entities):
            target = pick()
            while target == i:
                target = rng.randrange(entities)
            emit(i, target)

        # Phase 2 — hub-biased fill to the requested edge count.
        guard = 0
        while written < edges and guard < edges * 40:
            guard += 1
            emit(pick(), pick())

    stats = {"entities": entities, "edges": written, **counts}
    with (out_dir / "MANIFEST.txt").open("w", encoding="utf-8") as fh:
        fh.write(f"seed={seed}\n")
        for k, v in stats.items():
            fh.write(f"{k}={v}\n")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entities", type=int, default=50_000)
    ap.add_argument("--edges", type=int, default=100_000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20260803)
    args = ap.parse_args()
    stats = generate(args.entities, args.edges, Path(args.out), args.seed)
    print(f"wrote {args.out}: " + " ".join(f"{k}={v}" for k, v in stats.items()))
    print("files:", ", ".join(sorted(os.listdir(args.out))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
