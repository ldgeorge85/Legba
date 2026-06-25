# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DSPy modules for the competing_hypotheses (ACH) analyst kind (PIECE C).

Versions land here as ``v1.py``, ``v2.py``, … per L-105 §2.3. The module is the
optional GEPA optimization twin — it is NOT on the runtime hot path (the kind
calls ``chat_complete`` directly via ``competing_hypotheses._generate_hypotheses``).
"""
