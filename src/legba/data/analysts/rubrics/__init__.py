# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frozen grading rubrics, carried IN THE REPO as data files.

A rubric is not code and it is not a prompt constant: it is a document that was
pre-registered, graded against, and whose sha256 IS the identity of every number
produced under it (``unit_correctness.rubric_sha``, ``grader_calibrations
.rubric_sha``). So it lives here as a file, verbatim, and
:mod:`legba.data.analysts.deterministic_handlers._correctness_rubric` checks its
digest at import — a rubric edited in place would otherwise silently re-label
every row already written under the old one.

Files
-----
``ANNEX_C_v4.md``
    The three-label correctness rubric (``contains`` / ``contradicts`` /
    ``silent``). Program 1's calibration gate PASSED on it — pooled 0.8444 over
    a fresh 30-atom draw, every pair above the 0.70 floor
    (``planning/PROGRAM1_2026-09-16/VERDICT_P1v4.md``). sha256
    ``1b51d7f5187c7f93af2e2cccc0775e21ab7efc41678bc28c4a2dcbe287fe7d8c``.
"""

from __future__ import annotations

import os

RUBRICS_DIR = os.path.dirname(os.path.abspath(__file__))

__all__ = ["RUBRICS_DIR"]
