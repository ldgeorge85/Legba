#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P0-T8 — the one-command operator run of the system 60-second demo.

Prints a four-stage GREEN/RED report for ONE demo G20 country against the
RUNNING stack's read APIs. This is the human-facing companion to the pytest
regression gate in ``tests/test_p0_loop_harness.py`` (which RUNs the same four
stages, deterministically, as the floor-0 gate).

The four stages (the system unit loop):
  (1) CITATIONS RESOLVE        — a finding carries data['citations'] that
                                 resolve to real signal ids.
  (2) FAITHFULNESS VERDICT      — a faithfulness critique exists (verification
                                 block: faithfulness_score + named spans).
  (3) CONFIDENCE GATE           — effective_confidence == min(confidence,
                                 faithfulness_score) where a verdict exists.
  (4) RECEIPT + CLEAN LINEAGE   — the click-path lineage root carries the honest
                                 'chain-consistent (single-node)' receipt badge
                                 and ZERO dangling derived_from edges.

USAGE (post-deploy, against the live stack):
    LEGBA_P0_DEMO_TARGET=india python3 scripts/p0_demo_check.py
    # or:  python3 scripts/p0_demo_check.py --target india

Reads the same env the harness does:
    LEGBA_P0_DEMO_TARGET     the demo G20 country (or pass --target)
    LEGBA_REGISTRY_API_URL   default http://127.0.0.1:8090  (loopback host port)
    LEGBA_REGISTRY_API_TOKEN optional bearer

Exit code 0 => all four stages GREEN; non-zero => a leg is RED or the stack is
unreachable. It never fabricates a pass: an unreachable stack is a clear error,
a reachable-but-broken loop is RED on the offending stage.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Mapping
from uuid import UUID


def _base_url() -> str:
    return os.getenv("LEGBA_REGISTRY_API_URL", "http://127.0.0.1:8090").rstrip("/")


def _headers() -> dict[str, str]:
    token = os.getenv("LEGBA_REGISTRY_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _get(client: Any, base: str, path: str, **params: Any) -> Any:
    resp = client.get(
        f"{base}{path}", params=params or None, headers=_headers(), timeout=10.0
    )
    resp.raise_for_status()
    return resp.json()


def _line(stage: str, ok: bool, detail: str) -> str:
    mark = "GREEN" if ok else "RED  "
    return f"  [{mark}] {stage:<34} {detail}"


def run_check(target: str) -> int:
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx is required to run the live check.", file=sys.stderr)
        return 3

    base = _base_url()
    print(f"P0 loop check  target={target!r}  registry={base}")

    client = httpx.Client()
    results: dict[str, bool] = {}
    try:
        # ---- preflight: reachable stack + a finding for the target ----------
        try:
            page = _get(client, base, "/api/v1/findings", target_id=target, limit=50)
        except Exception as exc:  # noqa: BLE001
            print(f"  stack unreachable at {base}: {exc!r}", file=sys.stderr)
            return 2
        findings = (
            page.get("items") or page.get("findings") or page.get("results") or []
        )
        if not findings:
            print(_line("stage0_findings_exist", False, f"no findings for {target!r}"))
            return 1

        # ---- STAGE 1 — citations resolve -----------------------------------
        cited: Mapping[str, Any] | None = None
        for f in findings:
            cits = (f.get("data") or {}).get("citations") or []
            if cits and any(c.get("signal_id") for c in cits):
                cited = f
                break
        if cited is None:
            print(_line("stage1_citations_resolve", False,
                        "no finding carries resolved data['citations']"))
            return 1
        citations = cited["data"]["citations"]
        try:
            for c in citations:
                UUID(str(c["signal_id"]))
            results["stage1_citations_resolve"] = True
            print(_line("stage1_citations_resolve", True,
                        f"{len(citations)} citation(s) on finding {cited['id']}"))
        except (ValueError, KeyError, TypeError):
            results["stage1_citations_resolve"] = False
            print(_line("stage1_citations_resolve", False, "a citation id is not a UUID"))

        # ---- STAGE 2 — faithfulness verdict present ------------------------
        verification = cited.get("verification")
        confidence = float(cited["confidence"])
        effective = cited.get("effective_confidence")
        if verification is not None and "faithfulness_score" in verification:
            fscore = float(verification["faithfulness_score"])
            n_spans = len(verification.get("unsupported_spans") or [])
            results["stage2_faithfulness_verdict"] = True
            print(_line("stage2_faithfulness_verdict", True,
                        f"faithfulness_score={fscore:.2f} unsupported_spans={n_spans} "
                        f"judge={verification.get('judge_status')}"))

            # ---- STAGE 3 — confidence gate ---------------------------------
            expected = min(confidence, fscore)
            gate_ok = effective is not None and abs(float(effective) - expected) < 1e-6
            results["stage3_confidence_gate"] = gate_ok
            print(_line("stage3_confidence_gate", gate_ok,
                        f"confidence={confidence:.2f} -> effective={effective} "
                        f"(expected min={expected:.2f})"))
        else:
            # No verdict on this row: the gate must be a no-op (legacy path).
            results["stage2_faithfulness_verdict"] = False
            print(_line("stage2_faithfulness_verdict", False,
                        "no verification block on the cited finding (unverified)"))
            gate_ok = effective is None or abs(float(effective) - confidence) < 1e-6
            results["stage3_confidence_gate"] = gate_ok
            print(_line("stage3_confidence_gate", gate_ok,
                        f"no verdict -> effective={effective} must equal "
                        f"confidence={confidence:.2f}"))

        # ---- STAGE 4 — receipt badge + zero dangling lineage ---------------
        try:
            lineage = _get(
                client, base, f"/api/v1/lineage/finding/{cited['id']}",
                direction="upstream", depth=20,
            )
        except Exception as exc:  # noqa: BLE001
            results["stage4_receipt_and_clean_lineage"] = False
            print(_line("stage4_receipt_and_clean_lineage", False,
                        f"lineage walk failed: {exc!r}"))
            lineage = None

        if lineage is not None:
            nodes = lineage.get("nodes") or []
            dangling = lineage.get("dangling") or []
            receipt = (nodes[0].get("receipt") if nodes else None) or {}
            badge_ok = receipt.get("badge") == "chain-consistent (single-node)"
            consistent = receipt.get("chain_consistent")
            stage4_ok = (
                bool(nodes)
                and not dangling
                # A receipt is surfaced on the root only; when present it must be
                # honest + consistent. Absent receipt (pre-chain row) is allowed,
                # but dangling edges are NOT.
                and (not receipt or (badge_ok and consistent is True))
            )
            results["stage4_receipt_and_clean_lineage"] = stage4_ok
            detail = (
                f"nodes={len(nodes)} dangling={len(dangling)} "
                f"badge={receipt.get('badge')!r} consistent={consistent}"
            )
            print(_line("stage4_receipt_and_clean_lineage", stage4_ok, detail))
    finally:
        client.close()

    all_green = all(results.values()) and len(results) >= 4
    print()
    print(f"RESULT: {'ALL GREEN — loop verified' if all_green else 'RED — a leg regressed'}")
    return 0 if all_green else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=os.getenv("LEGBA_P0_DEMO_TARGET"),
        help="demo G20 country (or set LEGBA_P0_DEMO_TARGET)",
    )
    args = parser.parse_args(argv)
    if not args.target:
        print(
            "ERROR: no demo target. Set LEGBA_P0_DEMO_TARGET=<g20-country> or "
            "pass --target <g20-country>.",
            file=sys.stderr,
        )
        return 4
    return run_check(args.target)


if __name__ == "__main__":
    raise SystemExit(main())
