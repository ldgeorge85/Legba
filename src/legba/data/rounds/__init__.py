# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rounds as data: a multi-seat analysis declared as a spec, not a handler.

A *round* is one piece of analysis done by several seats that hand typed rows
to each other under rules about who may read what, and when. A round TYPE is a
spec file (``specs/*.yaml``): seats, one flow tree of bounded control shapes,
and budgets counted in calls and seconds. Design: plan P4 "the round as a
data-defined graph" (2026-11-14).

Modules, leaf first:

* :mod:`.refs` — the closed grammar of read references and predicates.
* :mod:`.spec` — the typed spec and its loader (lane G-1).
* :mod:`.catalog` — the registered names a spec may use (cards, packs,
  planes and their model families, code functions, gates, row types).
* :mod:`.budget` — the worst-case calculator (calls, seconds, peak parallel).
* :mod:`.validate` — the refusal rules V1-V12 and the CLI.
* :mod:`.rows`, :mod:`.tally`, :mod:`.tables`, :mod:`.gates` — typed output
  rows, panel tallies, sentence/position tables and matrices, handoff gates
  (lane G-3; pure, no tables).
* :mod:`.store`, :mod:`.visibility` — the run store protocol (in-memory and
  file-backed) and the ONE function that builds a seat's inputs.
* :mod:`.interpreter`, :mod:`.seats`, :mod:`.harness` — the interpreter, the
  seat-call contract and the command-line harness host (lane G-2).

Nothing here touches a database, the registry, a container or a model.
Importing this package is cheap; the honesty-gate library is imported only
when a gate runs.
"""
