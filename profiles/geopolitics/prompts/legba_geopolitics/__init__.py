# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba_geopolitics — the geopolitics profile's own prompt prose (W9 SPLIT-2).

The profile manifest's ``prompts:`` key names the directory that holds this
package (``profiles/geopolitics/prompts``). A profile's prompts directory holds
ONE top-level package named ``legba_<profile id>`` (``legba.data.profile.
prompts_package_name``): a name of its own, beside the platform's ``legba``
package rather than inside it, so two profiles' prose can never shadow each
other or a platform module.

What lives here is the exemplar's prose, not the platform's (DEC-14, taken
2026-10-04 on planning/PLATFORM_VS_EXEMPLAR_2026-09-29.md §1):

``lens_left`` … ``lens_isolationist``
    The six political-lean lenses (VOICES LEANS, 2026-09-21). Each module
    exports ``LENS_SYSTEM`` / ``LENS_PRIOR_BLOCK`` / ``LENS_ID`` and composes
    its system prompt with the platform's ``legba.prompts.lens_common`` frame.
    Their descriptors name ``legba_geopolitics.<id>:LENS_SYSTEM``. The ids are
    the ``geopolitics.lens`` vocabulary family
    (``descriptors/vocabulary_geopolitics_lens.yaml``).

The five epistemic lenses (trend, baserate, capability, intent, and the chorus
diff) are platform prose and stay in ``legba.prompts``.

HOW IT IS IMPORTED. Inside the images: the registry and runtime builds copy
this directory into ``/install/lib/python3.11/site-packages``
(``docker/Dockerfile.runtime`` / ``Dockerfile.registry``, build arg
``LEGBA_PROFILE``), and the build imports every module in it. From a checkout:
the profile's prompts directory on ``PYTHONPATH`` (``pyproject.toml``'s pytest
``pythonpath``; ``deploy/deploy.sh``'s registrar runs).
"""
