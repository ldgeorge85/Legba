# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import-path alias for :mod:`legba.data.option_catalog.handler_options_inquiry`.

The X-1 option catalog moved out of the ``analysts`` package (W3-F17) because
importing ANY ``legba.data.analysts.*`` module first runs the package
``__init__``, which eagerly imports every kind module and, through
``deterministic_handlers``, the runtime-only dependency stack (``feedparser``,
``networkx``, ...). The slim registry image lacks those, so the catalog was
unreachable from ``schemas/analyst.py`` there and ``method.options`` went
unvalidated. The catalog now lives in the leaf package
:mod:`legba.data.option_catalog`; this alias keeps every existing
``legba.data.analysts.handler_options_inquiry`` importer working.

The alias REPLACES this module in ``sys.modules`` with the real one, so the
two names are one object (``monkeypatch`` on either hits the same module).
"""

import sys

from ..option_catalog import handler_options_inquiry as _impl

# DEPRECATED (W4b-F4): no src/, tests/ or scripts/ file imports this path any
# more -- import ``legba.data.option_catalog.handler_options_inquiry`` instead (the leaf
# package, no ``analysts/__init__`` walk). This alias stays one more wave for
# out-of-tree importers, then goes; tests/data_pkg/test_option_catalog_imports.py
# keeps the tree off it.
sys.modules[__name__] = _impl
