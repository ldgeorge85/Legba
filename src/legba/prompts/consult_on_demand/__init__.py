# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DSPy modules for the consult_on_demand analyst kind (L-178).

Versions land here as ``v1.py``, ``v2.py``, … per L-105 §2.3.

Note: the kind itself is a ReAct loop (MAX_TOOL_ROUNDS=6), so the DSPy
module exposes a *per-round* signature.  The kind handler's outer loop
in :mod:`legba.data.analysts.consult_on_demand` orchestrates the
tool-call dispatch; this module is the LLM-bearing-step surface the
optimizer compiles against.
"""
