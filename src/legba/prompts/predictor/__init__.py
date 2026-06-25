# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DSPy modules for the predictor analyst kind (L-174).

Versions land here as ``v1.py``, ``v2.py``, … per L-105 §2.3.

Note: the predictor's core is statistical (AutoARIMA, fitted in pure
Python in the kind handler).  The DSPy module wraps the *optional*
narrative LLM call — the part that explains "why this number."  When no
narrative LLM is supplied, the kind handler bypasses this module entirely
and falls back to the fixed terse narrative.
"""
