# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-analyst DSPy prompt modules for the ``country_assessor`` analyst.

The L-176 optimizer resolves an analyst's parent prompt module by the
convention ``legba.prompts.<analyst_id>.v{N}`` (see
``legba.data.analysts.optimizer``). ``country_assessor`` therefore owns this
package; GEPA evolves :mod:`legba.prompts.country_assessor.v1` and promoted
candidates land as ``v2``, ``v3``, … per L-105 §2.3.

This package imports ``dspy`` (a worker-only dep) lazily via the version
modules — import them only where dspy is present (the GEPA worker).
"""
