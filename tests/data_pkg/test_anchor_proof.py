# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-1 — the anchor's Merkle tree, inclusion proofs, and the seal-proof route.

  * ONE fold: the handler's ``merkle_root`` is ``anchor_proof.merkle_root``;
  * a path for every leaf of every tree size 1..33 folds to the root; a
    tampered leaf, a tampered sibling or a flipped side does not;
  * the leaf recipes (a chain head; a ledger row) match the ledger's;
  * ``store_tree`` stores only the tree the day anchored, never twice;
  * ``inclusion_proof`` over stored rows: the first day holding the leaf, the
    path, the root re-folded, the calendar rows "not verified here";
    an absent leaf names the first stored day;
  * ``GET /v3/ledger/seal-proof``: the question and each number proven
    against the same day; 404 on an unknown question; ``available: false``
    when the 0262 tables cannot be read.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import anchor_proof as ap
from legba.data import forecast_ledger as fl
from legba.data.analysts.deterministic_handlers import receipt_anchor as ra
from legba.data.registry._deps import require_bearer
from legba.data.registry.seal_proof_api import NUMBERS_SQL, QUESTION_SQL, build_seal_proof_router


def _h(i: int) -> bytes:
    return hashlib.sha256(f"leaf-{i}".encode()).digest()


def test_the_handler_folds_with_the_proof_module():
    for n in (0, 1, 2, 3, 5, 131):
        leaves = [_h(i) for i in range(n)]
        assert ra.merkle_root(leaves) == ap.merkle_root(leaves)


@pytest.mark.parametrize("n", range(1, 34))
def test_every_leaf_of_every_size_proves_and_tampering_fails(n):
    leaves = [_h(i) for i in range(n)]
    root = ap.merkle_root(leaves)
    for i in range(n):
        path = ap.merkle_path(leaves, i)
        assert ap.fold_path(leaves[i], path) == root
        # A tampered leaf fails.
        assert ap.fold_path(_h(10_000 + i), path) != root
        if path:
            side, sib = path[0]
            # A tampered sibling fails; a flipped side fails unless both are equal.
            assert ap.fold_path(leaves[i], [(side, _h(99_999))] + path[1:]) != root
            flipped = [("left" if side == "right" else "right", sib)] + path[1:]
            assert (ap.fold_path(leaves[i], flipped) == root) == (sib == leaves[i])


def test_out_of_range_index_is_refused():
    with pytest.raises(IndexError):
        ap.merkle_path([_h(0)], 1)


def test_leaf_recipes():
    head = ap.AnchorLeaf(ap.HEAD_KIND, "analyst_a", "aa" * 32)
    assert head.digest == ra.leaf_for("analyst_a", "aa" * 32)
    q = ap.AnchorLeaf("forecast_question", "q-1", "bb" * 32)
    assert q.digest == fl.ledger_leaf("forecast_question", "q-1", "bb" * 32)
    assert ap.leaf_input("forecast_question", "q-1", "bb" * 32) == "forecast_question:q-1" + "bb" * 32
    audit = ap.AnchorLeaf(ap.AUDIT_KIND, "7", "cc" * 32)
    assert audit.digest == ra.audit_chain_leaf(7, "cc" * 32)


class _TreeConn:
    """The 0262 tables and receipt_anchors, in memory, append-only."""

    def __init__(self) -> None:
        self.anchors: dict[date, list[dict]] = {}
        self.trees: dict[date, dict] = {}
        self.leaves: dict[date, list[dict]] = {}
        self.questions: dict[str, dict] = {}
        self.numbers: list[dict] = []
        self.fail = False

    def anchor(self, day: date, root: str) -> None:
        self.anchors[day] = [
            {"calendar": c, "status": "submitted" if i == 0 else "pending", "root_hash": root,
             "submitted_at": datetime(2026, 1, 1, tzinfo=timezone.utc) if i == 0 else None,
             "created_at": None, "has_proof": i == 0}
            for i, c in enumerate(ra.CALENDARS)
        ]

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    async def fetchval(self, sql, *a):
        if self.fail:
            raise RuntimeError('relation "receipt_anchor_leaves" does not exist')
        if sql == ap.ANCHORED_ROOT_SQL:
            rows = self.anchors.get(a[0])
            return rows[0]["root_hash"] if rows else None
        if sql == ap.FIRST_STORED_DAY_SQL:
            return min(self.trees) if self.trees else None
        raise AssertionError(sql)

    async def fetchrow(self, sql, *a):
        if self.fail:
            raise RuntimeError('relation "receipt_anchor_leaves" does not exist')
        if sql == ap.INSERT_TREE_SQL:
            if a[0] in self.trees:
                return None
            self.trees[a[0]] = {"root_hash": a[1], "leaf_count": a[2], "anchor_version": a[3],
                                "recorded_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
            return {"day": a[0]}
        if sql == ap.TREE_FOR_LEAF_SQL:
            for d in sorted(self.leaves):
                if a[1] is not None and d != a[1]:
                    continue
                for lf in self.leaves[d]:
                    if lf["leaf_hash"] == a[0]:
                        return {"day": d, "position": lf["position"], **self.trees[d]}
            return None
        if sql == QUESTION_SQL:
            return self.questions.get(a[0])
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        if sql == ap.INSERT_LEAVES_SQL:
            assert a[0] not in self.leaves, "append-only"
            self.leaves[a[0]] = [{"position": p, "leaf_hash": l, "kind": k, "ref": r, "content_hash": c}
                                 for p, l, k, r, c in zip(a[1], a[2], a[3], a[4], a[5])]
            return "INSERT"
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        if self.fail:
            raise RuntimeError('relation "receipt_anchor_leaves" does not exist')
        if sql == ap.LEAVES_FOR_DAY_SQL:
            return [{"position": x["position"], "leaf_hash": x["leaf_hash"]} for x in self.leaves[a[0]]]
        if sql == ap.ANCHOR_ROWS_FOR_DAY_SQL:
            return self.anchors.get(a[0], [])
        if sql == NUMBERS_SQL:
            return [n for n in self.numbers if n["question_id"] == a[0]]
        raise AssertionError(sql)


def _tree(qid: str, qsha: str, nums: list[tuple[str, str]]) -> list[ap.AnchorLeaf]:
    heads = [ap.AnchorLeaf(ap.HEAD_KIND, f"analyst_{i}", f"{i:02d}" * 32) for i in range(5)]
    ledger = [ap.AnchorLeaf("forecast_number", nid, sha) for nid, sha in nums]
    ledger += [ap.AnchorLeaf("forecast_question", qid, qsha)]
    return heads + ledger + [ap.AnchorLeaf(ap.AUDIT_KIND, "12", "ee" * 32)]


async def test_store_tree_keeps_only_the_anchored_tree_and_never_rewrites():
    conn = _TreeConn()
    day = date(2031, 2, 3)
    tree = _tree("q-1", "ab" * 32, [("n-1", "cd" * 32)])
    root = ap.merkle_root([lf.digest for lf in tree]).hex()
    # The day's anchor holds another root (an earlier tick): nothing stored.
    conn.anchor(day, "00" * 32)
    assert await ap.store_tree(conn, day, root, tree, anchor_version="v") == "not_the_anchored_root"
    assert not conn.trees
    conn.anchor(day, root)
    assert await ap.store_tree(conn, day, root, tree, anchor_version="v") == "stored"
    assert await ap.store_tree(conn, day, root, tree, anchor_version="v") == "already_stored"
    assert [x["kind"] for x in conn.leaves[day]][:1] == [ap.HEAD_KIND]


async def test_inclusion_proof_over_stored_rows():
    conn = _TreeConn()
    assert (await ap.inclusion_proof(conn, "ab" * 32)) == {
        "included": False, "leaf": "ab" * 32, "first_stored_day": None}
    tree = _tree("q-1", "ab" * 32, [("n-1", "cd" * 32)])
    root = ap.merkle_root([lf.digest for lf in tree]).hex()
    for day in (date(2031, 2, 3), date(2031, 2, 4)):
        conn.anchor(day, root)
        await ap.store_tree(conn, day, root, tree, anchor_version="2026-11/f1-leaves")
    leaf = fl.ledger_leaf("forecast_question", "q-1", "ab" * 32)
    p = await ap.inclusion_proof(conn, leaf.hex())
    assert p["included"] and p["day"] == "2031-02-03" and p["root"] == root
    assert p["recomputed"] is True and p["anchor_root_matches"] is True
    assert p["position"] == 6 and p["leaf_count"] == len(tree)
    path = [(s["side"], bytes.fromhex(s["hash"])) for s in p["path"]]
    assert ap.fold_path(leaf, path).hex() == root
    assert [t["status"] for t in p["timestamps"]] == ["submitted", "pending"]
    assert all(t["verified_here"] is False for t in p["timestamps"])
    assert "does not verify" in p["external_timestamp_note"]
    # A named day.
    assert (await ap.inclusion_proof(conn, leaf.hex(), day=date(2031, 2, 4)))["day"] == "2031-02-04"
    # A leaf in no stored tree names the first stored day.
    miss = await ap.inclusion_proof(conn, fl.ledger_leaf("forecast_question", "q-2", "ab" * 32).hex())
    assert miss == {"included": False, "leaf": miss["leaf"], "first_stored_day": "2031-02-03"}


def _app(conn) -> FastAPI:
    class _Pool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *exc):
                    return False

            return _Ctx()

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=_Pool()))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_seal_proof_router(deps), prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    return app


QID = "0b5a1c3e-1111-4222-8333-944455556666"
NID = "0b5a1c3e-1111-4222-8333-944455557777"


async def _get(app, qs: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(f"/api/v1/v3/ledger/seal-proof{qs}")


async def test_the_seal_proof_route_proves_the_question_and_its_numbers():
    conn = _TreeConn()
    qsha, nsha = "ab" * 32, "cd" * 32
    conn.questions[QID] = {"id": QID, "slug": "FX-p1", "content_sha256": qsha,
                           "sealed_at": datetime(2031, 2, 2, tzinfo=timezone.utc)}
    conn.numbers = [{"id": NID, "question_id": QID, "issuer_kind": "operator", "issuer_ref": "operator",
                     "content_sha256": nsha}]
    app = _app(conn)

    body = (await _get(app, f"?question_id={QID}")).json()
    assert body["available"] is True and body["question"]["included"] is False
    assert body["question"]["leaf_input"] == f"forecast_question:{QID}{qsha}"

    day = date(2031, 2, 3)
    tree = _tree(QID, qsha, [(NID, nsha)])
    root = ap.merkle_root([lf.digest for lf in tree]).hex()
    conn.anchor(day, root)
    await ap.store_tree(conn, day, root, tree, anchor_version="2026-11/f1-leaves")
    body = (await _get(app, f"?question_id={QID}")).json()
    q = body["question"]
    assert q["included"] and q["day"] == "2031-02-03" and q["root"] == root
    assert q["leaf"] == hashlib.sha256(q["leaf_input"].encode()).hexdigest()
    (n,) = body["numbers"]
    assert n["included"] and n["day"] == q["day"] and n["issuer_kind"] == "operator"
    assert "issuer_ref" not in n
    for proof in (q, n):
        path = [(s["side"], bytes.fromhex(s["hash"])) for s in proof["path"]]
        assert ap.fold_path(bytes.fromhex(proof["leaf"]), path).hex() == root

    r = await _get(app, "?question_id=0b5a1c3e-1111-4222-8333-000000000000")
    assert r.status_code == 404
    assert (await _get(app, "?question_id=nope")).status_code == 422
    conn.fail = True
    body = (await _get(app, f"?question_id={QID}")).json()
    assert body == {"version": body["version"], "question_id": QID, "available": False}
