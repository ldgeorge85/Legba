#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Correctness Grader analyst descriptor (G1).

Reads ``descriptors/analyst_correctness_grader.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the platform's first per-unit CORRECTNESS measurement — the thing
~84% of the fleet's self-grading LLM calls structurally cannot see. After this
script runs (and the operator flips ``state`` to ``active`` AND sets
``LEGBA_CORRECTNESS_GRADER_ENABLED=1``) the reconciler spins up a deterministic
actor that daily grades each country's desk and composition heads against an
independent reference and writes ``unit_correctness`` rows.

PREREQUISITES (all DEPLOY steps, none of them this script's job):

  * MIGRATION 0196 applied — ``unit_references``, ``unit_correctness``,
    ``unit_correctness_claims``, ``grader_calibrations``;
  * REGISTRY IMAGE REBUILT FIRST. A NEW deterministic sub-handler is compiled
    into the image and the registry validates descriptors against the
    sub-handler table; registering this file against a stale registry image is
    how a descriptor lands and then silently fails to bind. Rebuild the
    registry, wait healthy, THEN recreate the runtime;
  * the CALIBRATION GATE ROW seeded, or the sweep refuses to grade anything:
        python3 scripts/load_unit_reference.py --seed-calibration
  * at least ONE reference loaded, or every target records ``no_reference``:
        python3 scripts/load_unit_reference.py <ref.json> --target <target_id>
  * the two OpenRouter stack components (``llm.audit.openrouter_llama33_70b
    .openai_compat`` and ``llm.judge.openrouter_mistral_large.openai_compat``)
    registered and active IF the operator intends to run three families. With
    ``LEGBA_GRADER_DAILY_CEILING_USD`` at its $0 default they are never called
    and their absence costs nothing but a note on the receipt.

Registration is CREATE-ONLY. To change a live descriptor (e.g. widen
``method.options.grader_targets``) PUT it instead:

  curl -X PUT "$LEGBA_REGISTRY_URL/analyst/correctness_grader" \\
       -H "Authorization: Bearer $LEGBA_REGISTRY_TOKEN" \\
       -H 'Content-Type: application/json' \\
       --data-binary @<(python3 -c 'import json,sys,yaml; \\
           json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)' \\
           descriptors/analyst_correctness_grader.yaml)

THE SPEND CEILING IS NOT IN THE DESCRIPTOR and cannot be raised by a PUT. It is
``LEGBA_GRADER_DAILY_CEILING_USD`` (default 0), an env var, because raising the
amount of the operator's money this job may spend takes a deploy step with a
human at the other end.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — resolved via scripts/_token.py (env → .env → dev).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bringup_http import register_create_only, registry_base  # noqa: E402
from _token import resolve_token  # noqa: E402


BASE = registry_base()
TOKEN = resolve_token()

# (family, file, descriptor_id)
TO_REGISTER = [
    ("analyst", "analyst_correctness_grader.yaml", "correctness_grader"),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
