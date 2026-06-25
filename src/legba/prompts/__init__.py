# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.prompts — DSPy prompt modules per analyst kind.

Per L-105 §2.3 import-path convention::

    legba.prompts.<analyst_kind>.<version>                    # base kind module
    legba.prompts.<analyst_kind>.<analyst_id_slug>.<version>  # per-target variants

Versions are monotonic integers (``v1``, ``v2``, …) plus content-hashed
``candidate_<short_hash>`` modules emitted by the optimizer (L-176).

Modules are pure Python.  Each module declares a single ``dspy.Module``
subclass (or a builder function returning one) so the runtime can
import + instantiate by string path.

This package is intentionally light — no DSPy import at package-import
time — so callers in environments without dspy installed don't trip
on missing transitive deps.
"""
