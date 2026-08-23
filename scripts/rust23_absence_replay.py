#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rust23_absence_replay.py — RUST-2's before/after on the adjudicated ABSENCE set.

WHAT THIS MEASURES. The 2026-08-16 panel
(``planning/PANEL_2026-08-16/lens4_groundtruth_attribution.md`` §"By prompt
branch") measured the adjudicated judge errors per prompt BRANCH and found the
negative route was the worst surface in the system: **ABSENCE 6 wrong of 7
(86%)**, GENERIC 8 of 30 (27%), NULL-RESULT 1 of 5 (20%). RUST-2 rewrote that
route's rubric (``absence.v3 -> absence.v4``). This script replays those seven
items through BOTH rubrics and reports the per-item movement against the
adjudicated truth.

n IS SEVEN. Every number this script prints carries its n, and seven is small.
It cannot establish a rate; it can only show whether the six named defects moved
and whether the one correct pass held. The panel's own caveat — "do not carry
the ABSENCE-branch 86% to better than +/-10 points" — applies to everything
downstream of it.

DESIGN INVARIANT (inherited from the D3 judge matrix, deliberately). The replay
drives **the repo's own** ``verify._run_judge`` with a shim LLM client. Evidence
rendering, claim segmentation, the kind classifier, partitioning, the quote ask,
the QUALIFIERS block and the ENTIRE severity chain run byte-identically to
production, so the ONLY variable between the two arms is the absence system
prompt. In particular **span verification stays inside the judge loop**: a
"contradicted" verdict is resolved against the evidence corpus this very call
rendered (``_quote_resolves`` -> ``_quote_refutes`` -> the seven demotion
tests), which is exactly how the recorded verdicts were produced. Scoring a
quote outside that loop would measure a different pipeline.

THE ARMS
  before : ``absence.v3`` — the rubric as it stood at 07c79f25, reconstructed as
           the historical rubric text plus TODAY'S shared quote/qualifier rules,
           so RUST-1's evidence-bytes fix is held constant across both arms and
           the contrast isolates the rubric.
  after  : ``absence.v4`` — whatever ``verify._ABSENCE_JUDGE_SYSTEM`` currently
           is. Read live, never copied, so this script cannot drift from the
           thing it claims to be measuring.

Only the ABSENCE partition is swapped. Every other route (the shared
unit/composition prompt, the M14 survey rubric) rides UNCHANGED in both arms —
the same containment D5 Q4 relied on when it held this route out of the judge
matrix as a control.

THE MODEL. The live judge: ``nvidia/nemotron-3-super-120b-a12b:free`` on
OpenRouter, temperature 0.0 (pinned inside ``_judge_claim_partition``), $0.
D5 finding F4 measured the SERVING PROVIDER moving 13.6% of verdicts on an
identical model id and prompt, so every row records ``served_by`` from
OpenRouter's own response field and the summary refuses to print a headline
without it.

CREDENTIALS. ``OPENROUTER_API_KEY`` is read from the checkout's ``.env`` at the
path given by ``--env`` and never logged, echoed or written to the output file.

USAGE
    python3 scripts/rust23_absence_replay.py --out /path/to/results.json
    python3 scripts/rust23_absence_replay.py --arm after --limit 2   # smoke

THE SET. Derived, not hand-copied: every ground-truth row whose claim text
appears in the CLAIM LIST of a preserved ABSENCE-rubric payload and whose
verdict came from the judge (``judge_path``), minus the one row whose recorded
``pipeline_detail`` shows the V-B slice route decided it rather than the rubric
(R5-P5, "scoped negative checked against the 15 colliding in-scope input-slice
rows"). That reconstruction reproduces the panel's n=7 / 6-wrong exactly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

import legba  # noqa: E402

# IMPORT-PROVENANCE GUARD. There is an editable-install ``.pth`` on this host
# pointing the ``legba`` package at the MAIN checkout, so a replay launched from
# a worktree can silently measure code that is not the code under test — and a
# before/after that measured the same bytes twice would look clean and mean
# nothing. Fail LOUD instead: the package must resolve inside THIS tree.
_expected = os.path.join(REPO, "src", "legba")
if not os.path.abspath(legba.__file__).startswith(_expected + os.sep):
    raise SystemExit(
        "IMPORT PROVENANCE FAILURE: 'legba' resolved to "
        f"{legba.__file__}\nexpected it under {_expected}. An editable-install "
        ".pth is shadowing this tree; re-run with PYTHONPATH="
        f"{os.path.join(REPO, 'src')}"
    )

from legba.data.provenance import verify  # noqa: E402
from legba.data.provenance.judge_quote_rules import (  # noqa: E402
    _JUDGE_QUALIFIER_RULE,
    _JUDGE_QUOTE_RULE,
)

#: The live judge lane. Free tier, so a full both-arm run costs $0.
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_NAME = "OPENROUTER_API_KEY"

#: ``absence.v3`` — the rubric text as it stood at 07c79f25, verbatim. The two
#: shared rules are appended from the LIVE module (not copied) so that RUST-1's
#: evidence-bytes fix and every later quote-rule edit are held constant across
#: both arms; the contrast is then the rubric and nothing else.
_ABSENCE_V3 = (
    "You are a faithfulness judge grading ABSENCE / NEGATIVE claims — statements "
    "that something did NOT occur, was NOT observed, or is NOT evidenced. You are "
    "given the evidence set the analyst searched (the [N] -> evidence map below) "
    "and a list of absence claims. For each absence claim decide EXACTLY ONE "
    "verdict:\n"
    "- supported: the evidence set genuinely does NOT contain the thing the claim "
    "says is absent, AND the claim's scope matches the evidence searched (a claim "
    "scoped to 'the reviewed signals' / a named country / a stated corpus is "
    "judged against THAT scope, not the whole world).\n"
    "- contradicted: the evidence set plainly SHOWS the very thing the claim says "
    "is absent (e.g. the claim says 'no strikes reported' but a cited item reports "
    "a strike). A contradicted absence is the highest-severity error.\n"
    "- unsupported: the claim asserts an absence that is UNBOUNDED or unscoped "
    "('nothing is happening', 'there is no risk anywhere') that the searched "
    "evidence cannot possibly establish, OR names a specific missing "
    "event/number/place with a scope the evidence set does not cover.\n"
    "Do NOT mark a scoped, evidence-consistent absence 'unsupported' merely "
    "because a negative has no citation — a correctly-scoped negative over a "
    "searched set is the normal, faithful shape of an honest low-risk read. "
    'Output strict JSON only: {"verdicts": ["supported"|"contradicted"|'
    '"unsupported", ...]} with one verdict per claim, in order.'
    + _JUDGE_QUOTE_RULE
    + _JUDGE_QUALIFIER_RULE
    + " Output only the JSON object."
)

ARMS = ("before", "after")
SETS = ("suppression", "catch", "catch_cd")


# ---------------------------------------------------------------------------
# The CATCH arm (built by scripts/rust23_absence_catchset.py)
# ---------------------------------------------------------------------------

#: Marker used for the reconstructed refuting citation. Deliberately far outside
#: any real ordinal so it cannot collide with a marker the product's own body
#: already carries.
_CATCH_MARKER_N = 901


def catch_payload(item: dict) -> tuple[str, list[dict]]:
    """``(body, citations)`` for one C-D true-fail item.

    BODY is the product's REAL published text — the composition body or the unit
    head the claim was located in — so the target span sits in its own context
    and is segmented by the pipeline exactly as production segmented it. Nothing
    is authored here.

    CITATIONS carry the grader's ARCHIVED page as a SIGNAL citation
    (``signal_id`` set), which is the one branch of ``_marker_to_evidence`` that
    renders text as SOURCE reporting — the only class V-G1 lets carry a hard
    fail. This is the counterfactual under test: the desk's own negative, facing
    the evidence the record says existed.

    The source text is the ADJUDICATED VERBATIM SPAN and nothing else. That is
    not a shortcut, it is a contamination fix, and it is the most important
    decision in this harness:

    every archived page in the round begins with a GRADER HEADER — ``URL:``,
    ``Outlet:``, ``Publish date:``, ``Fetched:`` and, fatally, ``Used for: … C-D
    D7 (economic_coercion evidence-existed — …)``. Feeding the file wholesale put
    the grader's own VERDICT into the judge's evidence map. The first catch run
    proved it live: on GB_A-CD3 and GB_A-CD7 the judge's "refuting quote" came
    back as the grader's annotation, not as source reporting. Those two rows were
    the judge reading the answer key, and any catch rate built on them is
    worthless. The header format varies across 140 files (four different capture
    markers, 31 files with no header at all), so parsing it off is fragile in
    exactly the direction that fails silently.

    The span itself is clean by construction: it is verbatim source text, and the
    round's own ``span_checker`` verified it in-loop against the fetched page.
    The trade is disclosed rather than hidden — the judge sees the decisive
    paragraph instead of a fuller article, which makes this an UPPER BOUND on
    catch rather than a production estimate. It is the identical bound for both
    arms, so the v3-vs-v4 contrast it supports stays valid.
    """
    citation = {
        "marker": f"[{_CATCH_MARKER_N}]",
        "signal_id": item["signal_id"],
        "title": item.get("outlet") or item.get("source_url") or "archived source",
        "source_text": item["refuting_span"],
        "source_id": item.get("source_url") or "",
    }
    return item["body"], [citation]


#: Vocabulary that can only come from the GRADER, never from a fetched page. If
#: any of it reaches the rendered evidence map the item is telling the judge the
#: answer, and the run is void.
_GRADER_LEAK = (
    "used for:", "evidence-existed", "under_hedged", "over_hedged",
    "c-a ", "c-b ", "c-c ", "c-d ", "c_a ", "c_b ", "c_c ", "c_d ",
    "product band", "ref band", "ref_band", "product_band", "discordant",
    "fetched:", "webfetch", "claude code", "tier 1)", "tier 2)",
)


def _env(name: str, path: str) -> str:
    """The named key from a dotenv file. NEVER logged; the caller never sees it
    leave this function."""
    with open(path) as fh:
        for line in fh:
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{name} not found in {path} (key names only, never values)")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _canon(text: str) -> str:
    """The evidence corpus in the SAME normalized form the severity chain builds
    it in, so the pre-flight check and the in-loop check agree."""
    return verify._normalize_quote_text(text)


_CLAIMS_MARKER_RE = re.compile(r"\n\n(?:ABSENCE )?CLAIMS:\n", re.DOTALL)


def claims_in_prompt(user_prompt: str) -> list[str]:
    """The numbered claim texts one partition grades (for target detection)."""
    match = _CLAIMS_MARKER_RE.search(user_prompt)
    if not match:
        return []
    out: list[str] = []
    for chunk in re.split(r"\n(?=\d+\.\s)", user_prompt[match.end():]):
        numbered = re.match(r"(\d+)\.\s(.*)", chunk, re.DOTALL)
        if numbered:
            out.append(numbered.group(2).split("\n   QUALIFIERS")[0])
    return out


# ---------------------------------------------------------------------------
# The replay set
# ---------------------------------------------------------------------------


def build_truefail_set(bakeoff: str) -> list[dict]:
    """The CATCH arm: adjudicated TRUE FAILS whose claim routes to the absence
    rubric — the population the suppression arm structurally cannot contain.

    Same construction as the suppression arm, which is the point: real preserved
    payloads, the desk's OWN body and OWN citations, the evidence the judge
    actually saw. That makes the refuting evidence IN-FRAME by construction — it
    sits inside the slice the claim is scoped to — which is exactly what the
    PROOF C-D lane cannot guarantee (its adjudications are at the product's
    14-day world frame, while the judge grades fidelity to the searched slice).

    Admission is mechanical: ``gt_fail`` is true, the claim routes to
    ``absence``, a payload exists, and the claim SURVIVES segmentation and the
    judgeable filter inside that body — i.e. the span really does reach the
    rubric under test rather than being dropped upstream.

    Frame note carried into the report: R2 items were graded before the
    prior-read, carve-out, scale and OUTLET rules shipped, and lens 4 excludes
    them from its ceiling arithmetic. They are REPLAYED here anyway — this is a
    replay on today's code, not a re-reading of the old verdict — and the per-item
    table keeps the round so a reader can drop them.
    """
    with open(os.path.join(bakeoff, "ground_truth.jsonl")) as fh:
        ground_truth = [json.loads(line) for line in fh]
    payloads = _payloads(bakeoff)
    out = []
    for row in ground_truth:
        if not row.get("gt_fail"):
            continue
        if verify._claim_kind(row["claim_text"]) != verify.CLAIM_KIND_ABSENCE:
            continue
        payload = payloads.get(row["critique_id"])
        if not payload or not payload.get("f_body"):
            continue
        target = norm(row["claim_text"])
        reaches = any(
            (target[:100] in norm(s) or norm(s)[:100] in target)
            and verify._is_judgeable_claim(s)
            and verify._claim_kind(s) == verify.CLAIM_KIND_ABSENCE
            for s in verify._segment_claims(payload["f_body"])
        )
        if reaches:
            out.append(row)
    return sorted(out, key=lambda r: r["tag"])


def _payloads(bakeoff: str) -> dict:
    payloads = json.load(open(os.path.join(bakeoff, "payloads.json")))
    extra = os.path.join(bakeoff, "journal_payloads.json")
    if os.path.exists(extra):
        payloads.update(json.load(open(extra)))
    return payloads


def build_set(bakeoff: str, prompts: str) -> list[dict]:
    """The ABSENCE-rubric, judge-decided adjudicated items — derived, not typed.

    Reproduces the panel's branch table from its own inputs: a ground-truth row
    joins the ABSENCE branch when its claim text is one of the numbered CLAIMS in
    a preserved payload whose SYSTEM prompt is the absence rubric. Rows the
    deterministic V-B slice route decided are dropped (their ``pipeline_detail``
    names the slice check) — they are not verdicts this rubric produced and no
    rubric rewrite can move them.
    """
    by_prefix: dict[str, list[dict]] = {}
    for name in sorted(os.listdir(prompts)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(prompts, name)) as fh:
            payload = json.load(fh)
        system = payload["messages"][0]["content"]
        if "ABSENCE / NEGATIVE claims" not in system:
            continue
        by_prefix.setdefault(name.split("_")[0], []).append(payload)

    with open(os.path.join(bakeoff, "ground_truth.jsonl")) as fh:
        ground_truth = [json.loads(line) for line in fh]

    items: list[dict] = []
    for row in ground_truth:
        if not row.get("judge_path"):
            continue
        for payload in by_prefix.get(row["critique_id"][:8], []):
            claims = [norm(c) for c in claims_in_prompt(payload["messages"][-1]["content"])]
            target = norm(row["claim_text"])
            if not any(target[:100] in c or c[:100] in target for c in claims):
                continue
            if "input-slice rows" in (row.get("pipeline_detail") or ""):
                break  # decided by the V-B slice route, not by this rubric
            items.append(row)
            break
    return sorted(items, key=lambda r: r["tag"])


# ---------------------------------------------------------------------------
# The shim
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class LaneJudge:
    """The ``chat_complete`` surface ``verify._run_judge`` expects.

    Swaps ONLY the absence rubric, and only on the ``before`` arm; every other
    system prompt is forwarded untouched, which is what keeps the two arms
    comparable. Partitions containing no target claim are answered locally
    (all-supported) rather than spending a call — their verdicts are never read.
    """

    subprovider = "openrouter"

    def __init__(self, arm: str, key: str, target_norm: str) -> None:
        self.arm = arm
        self._key = key
        self._target = target_norm
        self.calls = 0
        self.skipped = 0
        self.absence_calls = 0
        self.swapped = 0
        self.tok_in = 0
        self.tok_out = 0
        self.served_by: list[str] = []

    def _system_for(self, system: str | None) -> str | None:
        if not system or system != verify._ABSENCE_JUDGE_SYSTEM:
            return system
        self.absence_calls += 1
        if self.arm == "before":
            self.swapped += 1
            return _ABSENCE_V3
        return system

    async def chat_complete(
        self, messages, *, max_tokens=16384, temperature=0.0, system=None, **kw
    ):
        user = messages[0]["content"] if messages else ""
        partition = [norm(c) for c in claims_in_prompt(user)]
        system_text = self._system_for(system)
        if partition and not any(
            self._target[:100] in c or c[:100] in self._target for c in partition
        ):
            self.skipped += 1
            return _Resp(
                json.dumps(
                    {
                        "verdicts": ["supported"] * len(partition),
                        "quotes": [""] * len(partition),
                    }
                )
            )
        self.calls += 1
        wire = ([{"role": "system", "content": system_text}] if system_text else []) + [
            dict(m) for m in messages
        ]
        body = json.dumps(
            {
                "model": MODEL,
                "messages": wire,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        ).encode()
        last: Exception | None = None
        for attempt in range(5):
            try:
                content, usage, provider = await asyncio.to_thread(self._post, body)
                self.tok_in += usage.get("prompt_tokens", 0)
                self.tok_out += usage.get("completion_tokens", 0)
                if provider:
                    self.served_by.append(provider)
                return _Resp(content)
            except RuntimeError as exc:
                text = str(exc)
                if any(code in text for code in ("HTTP 401", "HTTP 402", "HTTP 404")):
                    raise  # a broken lane: never burn retries on it
                last = exc
                await asyncio.sleep((30 if "HTTP 429" in text else 8) * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last = exc
                await asyncio.sleep(8 * (attempt + 1))
        raise RuntimeError(f"{MODEL} failed after retries: {last}")

    def _post(self, data: bytes):
        request = urllib.request.Request(
            URL,
            data=data,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:300]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        if "error" in body and not body.get("choices"):
            raise RuntimeError(str(body["error"])[:300])
        message = body["choices"][0]["message"]
        content = message.get("content") or ""
        if "verdicts" not in content:
            # A reasoning-class model may leave the final JSON in `reasoning`.
            reasoning = message.get("reasoning") or ""
            if "verdicts" in reasoning:
                content = f"{content}\n{reasoning}"
        # D5 finding F4: the SERVING PROVIDER moved 13.6% of verdicts on an
        # identical model id and prompt. Every row carries it.
        return content, body.get("usage", {}), body.get("provider")


# ---------------------------------------------------------------------------
# One item, one arm
# ---------------------------------------------------------------------------


async def run_item(item: dict, arm: str, payload: dict, key: str) -> dict:
    shim = LaneJudge(arm, key, norm(item["claim_text"]))
    started = time.time()
    verdict, quote, error = None, "", None
    body = payload["f_body"] or ""
    citations = payload["cits"] or []
    # CATCH arm only: prove the refuting span actually SURVIVED into the rendered
    # evidence map before spending a call. Production caps source text, so an
    # item whose evidence was cut is not a catch test at all — it is a test of
    # whether the judge invents a refutation, and it would score as a v4 "miss"
    # for a reason that has nothing to do with the rubric. Fail LOUD.
    if item.get("gt_fail") and item.get("refuting_span"):
        rendered = " \n ".join(
            str(v) for v in verify._marker_to_evidence(citations).values()
        )
        if not verify._quote_resolves(item["refuting_span"], _canon(rendered)):
            raise SystemExit(
                f"EVIDENCE RECONSTRUCTION FAILURE on {item['tag']}: the grader's "
                "refuting span is not present in the rendered evidence map, so "
                "this item cannot measure catch rate."
            )
        low = rendered.lower()
        leaked = [w for w in _GRADER_LEAK if w in low]
        if leaked:
            raise SystemExit(
                f"GRADER LEAK on {item['tag']}: the rendered evidence map "
                f"contains adjudication vocabulary {leaked!r}. The judge would be "
                "reading the answer key, not the reporting; this run is void."
            )
    try:
        graded, branch = await verify._run_judge(
            shim, body=body, citations=citations
        )
        target = norm(item["claim_text"])
        for claim, claim_verdict, claim_quote in graded:
            claim_norm = norm(claim)
            if target[:100] in claim_norm or claim_norm[:100] in target:
                verdict, quote = claim_verdict, claim_quote
                break
    except Exception as exc:  # noqa: BLE001
        branch, error = {}, str(exc)[:400]
    return {
        "tag": item["tag"],
        "arm": arm,
        "round": item["round"],
        "stratum": item["stratum"],
        "analyst": item["analyst_id"],
        "critique_id": item["critique_id"],
        "claim_text": item["claim_text"],
        "recorded_verdict": item["pipeline_verdict"],
        "recorded_reason": item["pipeline_reason"],
        "adjudicated_fail": item["gt_fail"],
        "panel_call": item["panel_call"],
        "adjudication_note": item["note"],
        "replay_verdict": verdict,
        "replay_quote": quote,
        "replay_fail": None if verdict is None else verdict != "supported",
        "branch_scores": branch,
        "model": MODEL,
        # ``served_by`` is a LIST: a multi-call item may be served by more than
        # one provider and collapsing that to a scalar would hide the drift the
        # field exists to expose.
        "served_by": sorted(set(shim.served_by)),
        "absence_calls": shim.absence_calls,
        "rubric_swapped": shim.swapped,
        "llm_calls": shim.calls,
        "skipped_partitions": shim.skipped,
        "tok_in": shim.tok_in,
        "tok_out": shim.tok_out,
        "secs": round(time.time() - started, 1),
        "err": error,
    }


def score(rows: list[dict]) -> dict:
    """Per-arm agreement with the adjudicated truth. n travels with the number."""
    out: dict[str, dict] = {}
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm and r["replay_verdict"]]
        agree = [r for r in arm_rows if r["replay_fail"] == r["adjudicated_fail"]]
        providers = sorted({p for r in arm_rows for p in r["served_by"]})
        out[arm] = {
            "n": len(arm_rows),
            "agree": len(agree),
            "rate": None if not arm_rows else round(len(agree) / len(arm_rows), 4),
            "served_by": providers,
            "model": MODEL,
        }
    return out


def load_catch(path: str) -> tuple[list[dict], dict[str, dict]]:
    """The C-D true-fail arm: adjudicated ``gt_fail=True`` items in the SAME row
    shape the suppression arm uses, so one scorer serves both."""
    with open(path) as fh:
        fixture = json.load(fh)
    items, payloads = [], {}
    for entry in fixture["admitted"]:
        body, citations = catch_payload(entry)
        items.append({
            "tag": entry["item_id"],
            "round": "PROOF-R1",
            "stratum": "true_fail",
            "analyst_id": entry["lane"],
            "critique_id": entry["item_id"],
            "claim_text": entry["claim_text"],
            "pipeline_verdict": "supported",
            "pipeline_reason": None,
            "gt_fail": True,
            "panel_call": "disagree",
            "note": (entry.get("grader_why") or "")[:300],
            "refuting_span": entry["refuting_span"],
        })
        payloads[entry["item_id"]] = {"f_body": body, "cits": citations}
    return items, payloads


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=ARMS + ("both",), default="both")
    parser.add_argument("--set", choices=SETS, default="suppression", dest="which",
                        help="suppression = the 7 adjudicated false-fail items; "
                             "catch = the C-D true-fail arm (needs --catchset)")
    parser.add_argument("--catchset", default="", help="fixture from rust23_absence_catchset.py")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--env", default="/usr/local/deployments/active/legba/.env",
        help="dotenv holding OPENROUTER_API_KEY (name only is ever printed)",
    )
    parser.add_argument(
        "--bakeoff", default="",
        help="dir with ground_truth.jsonl + payloads.json (+ journal_payloads.json)",
    )
    parser.add_argument(
        "--prompts", default="", help="dir of preserved judge payloads (default: <bakeoff>/prompts)",
    )
    args = parser.parse_args()

    prompts = args.prompts or (os.path.join(args.bakeoff, "prompts") if args.bakeoff else "")
    key = _env(KEY_NAME, args.env)
    print(f"credentials: {KEY_NAME} loaded from {args.env} (value never printed)")

    if args.which == "catch_cd":
        if not args.catchset:
            sys.exit("--set catch_cd needs --catchset (rust23_absence_catchset.py)")
        items, payloads = load_catch(args.catchset)
        print(f"replay set: CATCH-CD arm (secondary), n={len(items)} PROOF C-D "
              "true-fail items — see the script header for its frame limits")
    elif args.which == "catch":
        items = build_truefail_set(args.bakeoff)
        payloads = _payloads(args.bakeoff)
        print(f"replay set: CATCH arm, n={len(items)} adjudicated TRUE-FAIL "
              "absence items (real preserved payloads, evidence in-frame)")
    else:
        items = build_set(args.bakeoff, prompts)
        wrong = sum(1 for i in items if i["panel_call"] != "agree")
        print(f"replay set: SUPPRESSION arm, n={len(items)} adjudicated "
              f"ABSENCE-rubric items, {wrong} recorded as MISJUDGED")
        payloads = _payloads(args.bakeoff)
    if args.limit:
        items = items[: args.limit]

    arms = ARMS if args.arm == "both" else (args.arm,)
    rows: list[dict] = []
    for arm in arms:
        for n, item in enumerate(items, start=1):
            payload = payloads.get(item["critique_id"])
            if payload is None:
                print(f"  [{arm} {n}/{len(items)}] {item['tag']}: NO PAYLOAD — skipped")
                continue
            row = await run_item(item, arm, payload, key)
            rows.append(row)
            print(
                f"  [{arm} {n}/{len(items)}] {row['tag']:8s} "
                f"replay={row['replay_verdict']} adjudicated_fail={row['adjudicated_fail']} "
                f"served_by={','.join(row['served_by']) or '?'} "
                f"calls={row['llm_calls']} swapped={row['rubric_swapped']} "
                f"{row['secs']}s {row['err'] or ''}"
            )

    summary = score(rows)
    print("\nSUMMARY (every figure carries its n and its serving provider)")
    for arm, stats in summary.items():
        if stats["n"]:
            print(f"  {arm:6s} agree {stats['agree']}/{stats['n']} "
                  f"({stats['rate']}) served_by={','.join(stats['served_by']) or '?'}")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"summary": summary, "rows": rows}, fh, indent=1)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
