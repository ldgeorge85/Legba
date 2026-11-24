# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-1 — deciding a forecast's outcome from the console (``forecast_resolution_api``).

Over the ledger fake (the production store and SQL constants; the records
append-only as in Postgres), with the request's principal injected:

  * who may decide: admin and owner any question; a contributor any question
    except one where they entered a number; a reader and a service never;
  * confirm writes the store's confirmation (the same call the host script
    makes) with the actor and the role, plus one audit row; a repeat is the
    first one (200); a proposal whose reading changed is refused; void needs
    a reason;
  * reject closes nothing, the proposal can no longer be confirmed, the
    question stays open; a repeat is the first rejection;
  * a judgement decides a needs_judgement proposal only;
  * THE SUPERSEDE RULE: a confirmation is never edited — a correction (admin
    only) supersedes it with a reason, both kept; a correction naming an act
    that is no longer the newest is refused;
  * the queue: a contributor sees only the questions they may decide;
  * THE FIFTH RESOLUTION: four resolved, the scorecard withholds every score;
    confirming the fifth through the route is what opens it;
  * the slim-image import guard for both new routes.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import forecast_ledger as fl
from legba.data import forecast_resolution as fr
from legba.data import forecast_resolvers as rules
from legba.data.registry._deps import require_bearer
from legba.data.registry.forecast_resolution_api import build_forecast_resolution_router
from legba.data.registry.forecast_scorecard_api import build_forecast_scorecard_router
from legba.data.registry.route_roles import RequestPrincipal, request_principal

from ._forecast_ledger_fakes import FakePool
from ._forecast_resolution_fakes import FakeResolutionConn

SEALED = datetime(2026, 3, 1, tzinfo=timezone.utc)
DUE = datetime(2026, 3, 20, 23, 59, 59, tzinfo=timezone.utc)
AFTER = DUE + timedelta(hours=80)
V3 = "/api/v1/v3"
CONTRIB_ID = "11111111-2222-4333-8444-555555555555"
CONTRIB = f"user:{CONTRIB_ID}"
ADMIN = "user:aaaaaaaa-2222-4333-8444-555555555555"


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def record(self, conn, **kw):
        self.rows.append(kw)
        return SimpleNamespace(id=f"audit-{len(self.rows)}")


def _who(role: str, actor: str, *, kind: str = "user", owner: bool = False, user_id: str | None = None):
    return RequestPrincipal(actor=actor, kind=kind, role=role,
                            user_id=user_id if user_id is not None else (actor.split(":", 1)[1]
                                                                          if actor.startswith("user:") else None),
                            is_owner=owner)


class _Rig:
    def __init__(self) -> None:
        self.conn = FakeResolutionConn()
        self.audit = _Audit()
        self.who = _who("admin", ADMIN)
        deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=FakePool(self.conn)),
                               audit_logger=self.audit)
        app = FastAPI()
        app.state.registry_deps = deps
        app.include_router(build_forecast_resolution_router(deps), prefix=V3)
        app.include_router(build_forecast_scorecard_router(deps), prefix=V3)
        app.dependency_overrides[require_bearer] = lambda: "test"
        app.dependency_overrides[request_principal] = lambda: self.who
        self.app = app

    async def post(self, path: str, body: dict):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t") as c:
            return await c.post(f"{V3}{path}", json=body)

    async def get(self, path: str):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t") as c:
            return await c.get(f"{V3}{path}")


def _question(slug: str) -> fl.ForecastQuestion:
    return fl.ForecastQuestion(
        slug=slug, question=f"An invented event ({slug}) happens in the window.",
        resolver_kind="event_reported",
        resolver_spec={"class": "invented event", "actors": ["XA"], "window": ["2026-03-01", "2026-03-20"]},
        due_at=DUE, window_start=SEALED, window_end=DUE,
        void_rules="Nobody reports on the invented event by due + grace.",
    )


async def _sealed(conn, slug: str, *numbers: tuple[str, str, float]) -> str:
    _, qid = await fl.upsert_question(conn, _question(slug))
    for kind, ref, p in numbers or (("operator", "operator", 0.6),):
        await fl.state_number(conn, qid, fl.ForecastNumber(kind, ref, p, SEALED - timedelta(days=1)))
    await fl.seal_question(conn, qid, at=SEALED)
    return qid


async def _propose(conn, qid: str, outcome: str = "yes", judgements=None) -> str:
    q = dict(await conn.fetchrow(fl._SELECT_QUESTION_BY_ID, qid))  # noqa: SLF001
    p = rules.Proposal(outcome=outcome, resolver_kind="event_reported", as_of=AFTER,
                       evidence=[{"ref": f"signal:00000000-0000-0000-0000-{len(conn.records) + 1:012d}",
                                  "url": "https://example.org/x", "title": "An invented report"}],
                       judgements=judgements or [])
    status, rec = await fr.write_proposal(conn, q, p)
    assert status == "written"
    return rec


@pytest.fixture
def rig(monkeypatch) -> _Rig:
    # The store reads the clock: every act lands after due + grace.
    real = fr._now  # noqa: SLF001
    monkeypatch.setattr(fr, "_now", lambda now: real(now or AFTER))
    return _Rig()


# ---------------------------------------------------------------------------
# Who may decide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who,code,error", [
    (_who("reader", "user:bbbbbbbb-2222-4333-8444-555555555555"), 403, "role_forbidden"),
    (_who("admin", "service:runtime", kind="service"), 403, "person_required"),
    (_who("admin", "anonymous", kind="dev"), 403, "person_required"),
])
async def test_readers_services_and_unnamed_credentials_never_decide(rig, who, code, error):
    qid = await _sealed(rig.conn, "FX-r1")
    rec = await _propose(rig.conn, qid)
    rig.who = who
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "yes"})
    assert r.status_code == code, r.text
    assert r.json()["detail"]["error"] == error
    assert [x["record_kind"] for x in rig.conn.records] == ["proposal"] and not rig.audit.rows


async def test_a_contributor_decides_except_where_they_entered_a_number(rig):
    theirs = await _sealed(rig.conn, "FX-c1", ("operator", "operator", 0.6), ("seat", CONTRIB, 0.4))
    other = await _sealed(rig.conn, "FX-c2")
    p_theirs, p_other = await _propose(rig.conn, theirs), await _propose(rig.conn, other)
    rig.who = _who("contributor", CONTRIB)

    q = (await rig.get("/ledger/resolution-queue")).json()
    assert q["role"] == "contributor" and q["may_correct"] is False
    assert [i["slug"] for i in q["items"]] == ["FX-c2"]
    assert q["hidden"] == 1 and "entered a number" in q["hidden_reason"]

    for verb, body in (("confirm", {"outcome": "yes"}), ("reject", {"reason": "wrong event"}),
                       ("judgement", {"outcome": "no", "reason": "x"})):
        r = await rig.post(f"/ledger/resolutions/{p_theirs}/{verb}", body)
        assert r.status_code == 403 and r.json()["detail"]["error"] == "entered_a_number", r.text

    r = await rig.post(f"/ledger/resolutions/{p_other}/confirm", {"outcome": "yes"})
    assert r.status_code == 201, r.text
    assert r.json()["actor_role"] == "contributor"
    conf = rig.conn.records[-1]
    assert (conf["record_kind"], conf["actor"], conf["actor_role"]) == ("confirmation", CONTRIB, "contributor")
    # A contributor never corrects a decided outcome.
    r = await rig.post(f"/ledger/resolutions/{conf['id']}/correct", {"outcome": "no", "reason": "x"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "role_forbidden"


async def test_the_entered_by_detail_counts_as_entering_a_number(rig):
    _, qid = await fl.upsert_question(rig.conn, _question("FX-d1"))
    await fl.state_number(rig.conn, qid, fl.ForecastNumber("operator", "operator", 0.6, SEALED))
    await fl.state_number(rig.conn, qid, fl.ForecastNumber(
        "consult", "consult:turn-1", 0.3, SEALED, detail={"entered_by": CONTRIB}))
    await fl.seal_question(rig.conn, qid, at=SEALED)
    rec = await _propose(rig.conn, qid)
    rig.who = _who("contributor", CONTRIB)
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "yes"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "entered_a_number"


@pytest.mark.parametrize("who,role", [
    (_who("admin", ADMIN, owner=True), "owner"),
    (_who("admin", ADMIN), "admin"),
    # Sign-in switch off: the bearer's stable actor, the October class.
    (_who("operator", "operator:bearer:0123456789ab", kind="operator", user_id=""), "admin"),
])
async def test_admins_and_the_owner_decide_any_question(rig, who, role):
    qid = await _sealed(rig.conn, "FX-a1", ("operator", "operator", 0.6), ("seat", ADMIN, 0.5))
    rec = await _propose(rig.conn, qid)
    rig.who = who
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "yes"})
    assert r.status_code == 201, r.text
    conf = rig.conn.records[-1]
    assert conf["actor"] == who.actor and conf["actor_role"] == role
    assert "bearer:" not in conf["actor"] or conf["actor"].startswith("operator:bearer:")


# ---------------------------------------------------------------------------
# Confirm, reject, judgement
# ---------------------------------------------------------------------------


async def test_confirm_is_the_host_command_plus_an_audit_row_and_is_idempotent(rig):
    qid = await _sealed(rig.conn, "FX-k1")
    rec = await _propose(rig.conn, qid)
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "yes", "reason": "two outlets"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["status"], body["question_status"], body["slug"]) == ("confirmed", "resolved", "FX-k1")
    assert rig.conn.questions[qid]["status"] == "resolved"
    res = [x for x in rig.conn.resolutions.values() if x["question_id"] == qid]
    assert len(res) == 1 and res[0]["outcome"] == "yes" and res[0]["resolved_by"] == ADMIN
    (audit,) = rig.audit.rows
    assert audit["action"] == "forecast_resolution_confirm" and audit["actor_id"] == ADMIN
    assert audit["from_version"] == rec and audit["to_version"] == body["record_id"]
    assert audit["change_summary"]["actor_role"] == "admin"

    again = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "yes"})
    assert again.status_code == 200 and again.json()["status"] == "already_recorded"
    assert again.json()["record_id"] == body["record_id"]
    assert len(rig.audit.rows) == 1 and len([x for x in rig.conn.records if x["record_kind"] == "confirmation"]) == 1


async def test_a_stale_proposal_is_refused(rig):
    qid = await _sealed(rig.conn, "FX-s1")
    first = await _propose(rig.conn, qid, "yes")
    # The reading the person saw is not the proposal's reading.
    r = await rig.post(f"/ledger/resolutions/{first}/confirm", {"outcome": "no"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_proposal"
    # A newer reading supersedes the one the person opened.
    await _propose(rig.conn, qid, "no")
    r = await rig.post(f"/ledger/resolutions/{first}/confirm", {"outcome": "yes"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_proposal"
    r = await rig.post(f"/ledger/resolutions/{first}/reject", {"reason": "old"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_proposal"
    assert rig.conn.questions[qid]["status"] == "open" and not rig.audit.rows


async def test_void_needs_its_reason(rig):
    qid = await _sealed(rig.conn, "FX-v1")
    rec = await _propose(rig.conn, qid, "void")
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "void"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "reason_required"
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm",
                       {"outcome": "void", "reason": "nobody reported on it by due + grace"})
    assert r.status_code == 201 and r.json()["question_status"] == "void"
    assert rig.conn.records[-1]["reason"] == "nobody reported on it by due + grace"


async def test_reject_closes_nothing_and_the_rejected_reading_cannot_be_confirmed(rig):
    qid = await _sealed(rig.conn, "FX-j1")
    rec = await _propose(rig.conn, qid)
    r = await rig.post(f"/ledger/resolutions/{rec}/reject", {"reason": "  "})
    assert r.status_code == 422
    r = await rig.post(f"/ledger/resolutions/{rec}/reject", {"reason": "the report is about another exchange"})
    assert r.status_code == 201, r.text
    assert r.json()["question_status"] == "open"
    rej = rig.conn.records[-1]
    assert (rej["record_kind"], rej["basis_record_id"], rej["resolution_id"]) == ("rejection", rec, None)
    assert rig.conn.questions[qid]["status"] == "open" and not rig.conn.resolutions
    again = await rig.post(f"/ledger/resolutions/{rec}/reject", {"reason": "again"})
    assert again.status_code == 200 and again.json()["record_id"] == rej["id"]
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "yes"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "proposal_rejected"
    # The host script's path refuses the same.
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fr.confirm_proposal(rig.conn, rec, actor="operator:1", now=AFTER)
    assert exc.value.code == "proposal_rejected"
    # The queue no longer lists it; a NEW reading puts it back.
    assert (await rig.get("/ledger/resolution-queue")).json()["items"] == []
    await _propose(rig.conn, qid, "no")
    assert [i["proposal"]["outcome"] for i in (await rig.get("/ledger/resolution-queue")).json()["items"]] == ["no"]
    assert [a["action"] for a in rig.audit.rows] == ["forecast_resolution_reject"]


async def test_a_judgement_decides_only_what_the_rule_cannot(rig):
    qid = await _sealed(rig.conn, "FX-g1")
    rec = await _propose(rig.conn, qid, "yes")
    r = await rig.post(f"/ledger/resolutions/{rec}/judgement", {"outcome": "yes", "reason": "x"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "no_judgement_needed"
    rec = await _propose(rig.conn, qid, "needs_judgement",
                         judgements=[{"key": "attribution", "question": "Is the strike attributed?"}])
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "needs_judgement"})
    assert r.status_code == 422  # not a closing outcome
    r = await rig.post(f"/ledger/resolutions/{rec}/judgement",
                       {"outcome": "no", "reason": "x", "judgements": ["nope"]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "unknown_judgement"
    r = await rig.post(f"/ledger/resolutions/{rec}/judgement",
                       {"outcome": "no", "reason": "the strike is not attributed by either government"})
    assert r.status_code == 201, r.text
    ov = rig.conn.records[-1]
    assert ov["record_kind"] == "override" and ov["basis_record_id"] == rec and ov["actor_role"] == "admin"
    assert ov["judgements"] == [{"key": "attribution", "decided": "no"}]
    assert rig.conn.override_rows and rig.conn.questions[qid]["status"] == "resolved"
    again = await rig.post(f"/ledger/resolutions/{rec}/judgement", {"outcome": "no", "reason": "y"})
    assert again.status_code == 200 and again.json()["record_id"] == ov["id"]


# ---------------------------------------------------------------------------
# The supersede rule
# ---------------------------------------------------------------------------


async def test_a_wrong_confirmation_is_superseded_never_edited(rig):
    qid = await _sealed(rig.conn, "FX-x1")
    rec = await _propose(rig.conn, qid)
    conf = (await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "yes"})).json()["record_id"]
    before = [dict(x) for x in rig.conn.records]

    r = await rig.post(f"/ledger/resolutions/{conf}/correct", {"outcome": "no", "reason": " "})
    assert r.status_code == 422
    r = await rig.post(f"/ledger/resolutions/{conf}/correct",
                       {"outcome": "no", "reason": "the cited exchange was in the previous window"})
    assert r.status_code == 201, r.text
    corr = r.json()
    assert corr["supersedes"] == conf and corr["question_status"] == "resolved"
    # Both kept, nothing edited: every earlier record is byte-for-byte as it was.
    assert rig.conn.records[: len(before)] == before
    kinds = [x["record_kind"] for x in rig.conn.records]
    assert kinds == ["proposal", "confirmation", "correction"]
    new = rig.conn.records[-1]
    assert new["supersedes_id"] == conf and new["reason"].startswith("the cited exchange")
    active = [x for x in rig.conn.resolutions.values() if x["question_id"] == qid and x["active"]]
    assert [x["outcome"] for x in active] == ["no"]
    assert len([x for x in rig.conn.resolutions.values() if x["question_id"] == qid]) == 2
    # Repeating the correction is the first one; correcting the superseded
    # confirmation again (a stale view) is refused.
    again = await rig.post(f"/ledger/resolutions/{conf}/correct", {"outcome": "no", "reason": "again"})
    assert again.status_code == 200 and again.json()["record_id"] == corr["record_id"]
    r = await rig.post(f"/ledger/resolutions/{conf}/correct", {"outcome": "void", "reason": "z"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_act"
    assert [a["action"] for a in rig.audit.rows] == ["forecast_resolution_confirm", "forecast_resolution_correct"]


# ---------------------------------------------------------------------------
# The fifth resolution opens the scorecard
# ---------------------------------------------------------------------------


async def test_confirming_the_fifth_resolution_is_what_opens_the_scorecard(rig):
    # Two issuers on each: a single-arm question is never in the arm table.
    qids = [await _sealed(rig.conn, f"FX-5{i}", ("operator", "operator", 0.7), ("tower", "tower", 0.5))
            for i in range(5)]
    for qid in qids[:4]:
        conf = await fr.confirm_proposal(rig.conn, await _propose(rig.conn, qid, "yes"),
                                         actor="operator:host", now=AFTER)
        assert conf
    sc = (await rig.get("/ledger/scorecard")).json()
    assert sc["available"] is True and sc["header"]["n_resolved"] == 4
    assert sc["scores_withheld"] is True
    assert all(a.get("mean_brier") is None for a in sc["arms"])
    assert all(n.get("brier") is None for q in sc["questions"] for n in q["numbers"])

    rec = await _propose(rig.conn, qids[4], "no")
    r = await rig.post(f"/ledger/resolutions/{rec}/confirm", {"outcome": "no"})
    assert r.status_code == 201, r.text

    sc = (await rig.get("/ledger/scorecard")).json()
    assert sc["header"]["n_resolved"] == 5 and sc["scores_withheld"] is False
    (arm,) = [a for a in sc["arms"] if a["issuer_kind"] == "operator"]
    # 4 × (0.7-1)² + (0.7-0)² over five: the miss counts as plainly as the hits.
    assert arm["n"] == 5 and arm["mean_brier"] == pytest.approx((4 * 0.09 + 0.49) / 5)
    last = next(q for q in sc["questions"] if q["id"] == qids[4])
    assert last["latest_act"]["record_kind"] == "confirmation"
    assert last["latest_act"]["actor_role"] == "admin"


# ---------------------------------------------------------------------------
# The slim-image import guard.
# ---------------------------------------------------------------------------

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


@pytest.mark.parametrize("module", ["forecast_resolution_api", "seal_proof_api"])
def test_the_routes_import_without_the_runtime_stack(module) -> None:
    probe = textwrap.dedent(
        f"""
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.{module} as api
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, "analyst/runtime package reachable from the route: %r" % leaked
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
