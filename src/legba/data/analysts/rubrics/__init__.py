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
    ``silent``). Its calibration gate passed — pooled 0.8444 over
    a fresh 30-atom draw, every pair above the 0.70 floor. sha256
    ``1b51d7f5187c7f93af2e2cccc0775e21ab7efc41678bc28c4a2dcbe287fe7d8c``.
    Kept for the rows graded under it.
``ANNEX_C_v5.md``
    The same three labels with scale and limiting words read as part of the
    core claim, a rule for the core claim of a compound assertion, a narrower
    "contains" for a broader development, "subject" in place of "country", and
    the output contract ordered core claim, span, reason, verdict. sha256
    ``954c4ac59280a089c09a0c41c80eb829df556b786fb153ed46cf65ac8d83ccfb``. Not
    yet calibrated: the grader refuses to grade under it until a passing
    re-gate row exists.
"""

from __future__ import annotations

import os

RUBRICS_DIR = os.path.dirname(os.path.abspath(__file__))

__all__ = ["RUBRICS_DIR"]
