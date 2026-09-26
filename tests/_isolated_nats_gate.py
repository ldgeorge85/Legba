# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The opt-in gate for tests that need a NATS of their OWN.

Five tests across three files publish to subjects that the LIVE runtime's
streams already capture — ``legba.dlq.>`` for the two webhook DLQ pairs,
``LEGBA_JOBS``' ``jobs.>`` for the agency pack test. The container runner runs
``--network host``, so "the dev-rig NATS on 127.0.0.1:4222" IS the operator's
production bus: running these against it would push rows into the live DLQ and
the live job queue. That is not a rig that is down; it is a rig that is UP, and
the reason these five cannot use it.

They were marked ``@pytest.mark.skip`` with that explanation in prose. Prose is
where it went wrong: the C-1 strict classifier (``tests/conftest.py``) reads the
reason text, saw "NATS", and escalated all five to FAILURES — which is how a
deliberate, documented decision spent a week sitting in the suite's "known
baseline" as five unexplained errors. Strict mode is right to demand that a
skip be *declared* rather than ambient; the declaration just has to be in the
form it can read, which is an opt-in env gate.

So: they are opt-in, and this module is the one place that says so.

**Running them.** They need a NATS the live runtime is not attached to::

    docker run -d --name legba-test-nats-isolated -p 127.0.0.1:4223:4222 \\
        nats:latest -js
    LEGBA_DATA_NATS_URL=nats://127.0.0.1:4223 LEGBA_TEST_ISOLATED_NATS=1 \\
        bash scripts/run_tests_in_container.sh <the file>

Setting ``LEGBA_TEST_ISOLATED_NATS=1`` without repointing
``LEGBA_DATA_NATS_URL`` is the one way to get this wrong: the gate opens and
the tests write to production. The flag is the operator's assertion that the
URL points somewhere disposable — nothing here can check that for them.
"""

from __future__ import annotations

import os

import pytest

#: The operator's assertion that ``LEGBA_DATA_NATS_URL`` points at a NATS
#: nothing else is reading.
ISOLATED_NATS_ENV = "LEGBA_TEST_ISOLATED_NATS"


def isolated_nats_opted_in() -> bool:
    """True when the caller has declared an isolated NATS for this run."""
    return os.getenv(ISOLATED_NATS_ENV, "").strip() == "1"


def isolated_nats_skip_reason(overlap: str) -> str:
    """The skip reason for a test whose subjects collide with ``overlap``.

    The leading ``LEGBA_TEST_ISOLATED_NATS=1 not set`` is load-bearing: it is
    what ``tests/conftest.py``'s ``_STRICT_EXEMPT_REASON_PATTERNS`` matches on,
    so strict mode reads this as the declared opt-in gate it is instead of a
    broken rig. ``tests/test_strict_mode_gate.py`` pins that classification.
    """
    return (
        f"{ISOLATED_NATS_ENV}=1 not set; this test's subjects overlap {overlap}, "
        f"and the container runner's --network host makes the dev-rig NATS the "
        f"LIVE bus — running it there would write production rows. Point "
        f"LEGBA_DATA_NATS_URL at a disposable NATS and set {ISOLATED_NATS_ENV}=1 "
        f"(see tests/_isolated_nats_gate.py)."
    )


def requires_isolated_nats(overlap: str) -> pytest.MarkDecorator:
    """``skipif`` for a test that may only run against a disposable NATS."""
    return pytest.mark.skipif(
        not isolated_nats_opted_in(),
        reason=isolated_nats_skip_reason(overlap),
    )
