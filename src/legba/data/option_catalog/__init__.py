# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The X-1 handler/kind option catalog — a LEAF package.

``HANDLER_OPTIONS`` / ``ANALYST_KIND_OPTIONS`` and the resolve/degrade
machinery over them (``handler_options``), with the spec primitives
(``handler_options_base``) and the per-feature catalogs (``_events``,
``_inquiry``, ``_enrichment``, ``_programs``, ``_catalog``).

**Why this is its own package and not under** ``legba.data.analysts``**.**
Importing any ``legba.data.analysts.<x>`` runs ``analysts/__init__.py`` first,
and that eagerly imports every kind module (``deterministic`` ->
``deterministic_handlers`` -> ``sources`` -> ``feedparser``; ``networkx`` too).
The slim registry image ships neither, so while the catalog lived there
``schemas/analyst.py`` could not import it in the registry process and
``method.options`` went unvalidated at PUT (the 2026-09-28 dead-analyst
incident). Every module here imports ONLY the stdlib and its siblings; keep it
that way — ``tests/data_pkg/test_option_catalog_leaf.py`` poisons the
runtime-only dependencies and fails if this package (or
``legba.data.schemas.analyst``) reaches any of them.

``legba.data.analysts.handler_options*`` remain as import-path aliases.
"""
