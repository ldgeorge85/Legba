# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The six political-lean lenses live in the geopolitics profile.

The leans' prose moved from ``src/legba/prompts/lens_*`` to the profile's own
prompt package, ``profiles/geopolitics/prompts/legba_geopolitics/lens_*``; the
five epistemic lenses stay in the platform. This file proves:

  1. BYTE-IDENTITY — each lean's system prompt, rendered the way the runtime
     renders it (``analyst_deps_builder._resolve_prompt_module`` over the
     descriptor's own ``method.prompt_module``), and its declared-prior echo
     (``journal_assessor._lens_prior_block_for``) hash to the sha256 pinned
     from the text at the pin (d1893001, before the move); the journal tier
     still distills every lean to ``entry_kind='lens'``;
  2. THE PACKAGED LOCATION — the package imports from a copy of the profile's
     ``prompts/`` dropped into a bare site-packages-like directory (what the
     registry/runtime image builds do), with nothing else of the profile on the
     path; the build steps, the compose build arg and deploy.sh's registrar
     PYTHONPATH are all in place; the platform package no longer carries the
     lean prose;
  3. THE FAMILY DRIVES THE TUPLE — ``geopolitics.lens`` is seeded from
     ``descriptors/vocabulary_geopolitics_lens.yaml``, its values equal the
     documented literal (the drift guard), a loaded family reads through
     (narrowing or extending the roster), and an unloaded / absent / empty /
     failing family reads the literal;
  4. THE MANIFEST — a declared ``prompts`` directory must hold ``legba_<id>``.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

from legba.data import lens_family
from legba.data.lens_family import (
    LEAN_LENS_FALLBACK,
    LEAN_LENS_FAMILY,
    lean_lens_ids,
    lean_prompt_module,
    lens_family as family_of_profile,
)
from legba.data.profile import ProfileError, load_profile, prompts_package_name
from legba.data.tags import VocabularyIndex

REPO = pathlib.Path(__file__).resolve().parents[2]
GEO = REPO / "profiles" / "geopolitics"
PROMPTS = GEO / "prompts"
PACKAGE = "legba_geopolitics"

_SCRIPTS_DIR = REPO / "scripts"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    import bringup_register_descriptors as walker  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)

#: sha256 of each lean's ``LENS_SYSTEM`` and ``LENS_PRIOR_BLOCK``, computed from
#: ``legba.prompts.<id>`` at the pin d1893001 — BEFORE the move. A changed hash
#: here is a changed prompt, which is a descriptor re-author, never a move.
PINNED_SYSTEM_SHA256 = {
    "lens_left": "26d176bd1679b793a3f11b4887976658e8cf7db7c900c961950709965e89b78a",
    "lens_right": "a883af8c57ac0db3b51585994e95b2e5f33d1f3868431c09daa795500565a904",
    "lens_centre": "539f863082745001c7506835a95cf155b8c4209a8ae64186d9ee300f0f87479b",
    "lens_pragmatist": "64517a82ffb0c636c78d60400f03e7a84b4bc81a490374ae92b8148f1bc440fa",
    "lens_militarist": "72b09bf251741c889538813b700e404efaec7666895ac969e8c3a966b2414810",
    "lens_isolationist": "7899c7e5da1f36b64af384057d77a8e22486ec839ceb721e5ac044eefa727085",
}
PINNED_PRIOR_SHA256 = {
    "lens_left": "504ccceeea185cca9280fd441f8d5cb96909959617ff2664648f326d3956440d",
    "lens_right": "2a6cddc1b2c78972c3350c80608c101f8132d3212d2e8f219c39858e16282025",
    "lens_centre": "cd369b14fae673b322e3f47b5c13d7292bfc0bc4ff2f98f2647e1a668bf3fdac",
    "lens_pragmatist": "2f6916cf4fbf2094aedc2a5bfb64e6561bec32d31c06f27e5ed02f8d972529b2",
    "lens_militarist": "c9f5fe36b8376f014b68f4f927a95c75b88e913aba7c2883605f31add6192aa5",
    "lens_isolationist": "06a813de8f3385a8cc25fd5c13b395f588001b3fdecede6530587610ee2ff3d2",
}

_FACULTIES = ("lens_trend", "lens_baserate", "lens_capability", "lens_intent")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _descriptor(aid: str) -> dict:
    return yaml.safe_load((REPO / "descriptors" / f"analyst_{aid}.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. byte-identity
# ---------------------------------------------------------------------------


def test_the_pins_cover_exactly_the_six_leans():
    assert tuple(PINNED_SYSTEM_SHA256) == LEAN_LENS_FALLBACK
    assert tuple(PINNED_PRIOR_SHA256) == LEAN_LENS_FALLBACK


@pytest.mark.parametrize("aid", LEAN_LENS_FALLBACK)
def test_each_lean_renders_byte_identical_through_the_runtime_resolver(aid):
    from legba.runtime.analyst_deps_builder import _resolve_prompt_module

    spec = _descriptor(aid)["method"]["prompt_module"]
    assert spec == f"{PACKAGE}.{aid}:LENS_SYSTEM"
    system = _resolve_prompt_module(spec, analyst_id=aid)
    assert isinstance(system, str) and system
    assert _sha(system) == PINNED_SYSTEM_SHA256[aid], aid


@pytest.mark.parametrize("aid", LEAN_LENS_FALLBACK)
def test_each_lean_echoes_its_pinned_prior_and_stays_on_the_lens_tier(aid):
    from legba.data.analysts.journal_assessor import (
        LENS_PROMPT_MODULE_PATHS,
        _entry_kind_for_analyst,
        _lens_prior_block_for,
    )

    assert LENS_PROMPT_MODULE_PATHS[aid] == f"{lean_prompt_module(aid)}:LENS_SYSTEM"
    block = _lens_prior_block_for(aid)
    assert _sha(block) == PINNED_PRIOR_SHA256[aid], aid
    assert _entry_kind_for_analyst(aid) == "lens"


def test_the_faculties_stay_platform_prose():
    from legba.data.analysts.journal_assessor import (
        LENS_FACULTY_ANALYST_IDS,
        LENS_PROMPT_MODULE_PATHS,
    )

    assert LENS_FACULTY_ANALYST_IDS == _FACULTIES
    for aid in _FACULTIES + ("lens_diff",):
        assert (REPO / "src" / "legba" / "prompts" / aid / "__init__.py").is_file(), aid
        assert _descriptor(aid)["method"]["prompt_module"].startswith(f"legba.prompts.{aid}:")
    for aid in _FACULTIES:
        assert LENS_PROMPT_MODULE_PATHS[aid] == f"legba.prompts.{aid}:LENS_SYSTEM"
    assert not set(_FACULTIES) & set(LEAN_LENS_FALLBACK)


# ---------------------------------------------------------------------------
# 2. the packaged location
# ---------------------------------------------------------------------------


def test_the_platform_package_no_longer_carries_the_lean_prose():
    import importlib

    for aid in LEAN_LENS_FALLBACK:
        # the package FILE is what moved; a checkout that switched branches may
        # keep an empty directory behind (an untracked __pycache__), so the
        # directory's existence proves nothing
        assert not (REPO / "src" / "legba" / "prompts" / aid / "__init__.py").is_file(), aid
        # a leftover directory imports as an EMPTY namespace package — the claim
        # is that no lean prose is importable from the platform package
        try:
            stale = importlib.import_module(f"legba.prompts.{aid}")
        except ModuleNotFoundError:
            pass
        else:
            assert not hasattr(stale, "LENS_SYSTEM"), aid
        assert (PROMPTS / PACKAGE / aid / "__init__.py").is_file(), aid
    # the shared frame the leans compose with stays in the platform
    assert (REPO / "src" / "legba" / "prompts" / "lens_common" / "__init__.py").is_file()


def test_the_package_imports_from_the_profile_prompts_dir_in_a_checkout():
    import importlib

    mod = importlib.import_module(f"{PACKAGE}.lens_left")
    assert pathlib.Path(mod.__file__).resolve().is_relative_to(PROMPTS.resolve())


def test_the_package_resolves_from_a_packaged_copy(tmp_path):
    """What the image builds do: copy ``<profile>/prompts/.`` into a
    site-packages directory, then import. A fresh interpreter whose path holds
    that copy and the platform (and NOT the profile directory) resolves every
    lean to the pinned bytes, from the copy."""
    site = tmp_path / "site-packages"
    shutil.copytree(PROMPTS, site)
    env = dict(os.environ)
    inherited = [
        p for p in env.get("PYTHONPATH", "").split(os.pathsep)
        if p and pathlib.Path(p).resolve() != PROMPTS.resolve()
    ]
    env["PYTHONPATH"] = os.pathsep.join([str(site), str(REPO / "src"), *inherited])
    code = (
        "import hashlib, importlib, json, sys\n"
        f"ids = {list(LEAN_LENS_FALLBACK)!r}\n"
        "out = {}\n"
        "for a in ids:\n"
        f"    m = importlib.import_module('{PACKAGE}.' + a)\n"
        "    out[a] = [m.__file__, hashlib.sha256(m.LENS_SYSTEM.encode()).hexdigest()]\n"
        "print(json.dumps(out))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    import json

    got = json.loads(done.stdout.strip().splitlines()[-1])
    for aid in LEAN_LENS_FALLBACK:
        path, sha = got[aid]
        assert pathlib.Path(path).resolve().is_relative_to(site.resolve()), (aid, path)
        assert sha == PINNED_SYSTEM_SHA256[aid], aid


@pytest.mark.parametrize("dockerfile", ["Dockerfile.runtime", "Dockerfile.registry"])
def test_both_images_copy_and_import_the_profile_prompts(dockerfile):
    text = (REPO / "docker" / dockerfile).read_text(encoding="utf-8")
    assert "ARG LEGBA_PROFILE=profiles/geopolitics" in text
    assert "COPY ${LEGBA_PROFILE}/ /build/profile/" in text
    assert "cp -r /build/profile/prompts/. /install/lib/python3.11/site-packages/" in text
    assert "pkgutil.walk_packages(['/build/profile/prompts'])" in text
    # the copy happens BEFORE the bytecode/test strip and the runtime stage copy
    assert text.index("/build/profile/prompts") < text.index("COPY --from=builder /install /install")


def test_compose_passes_the_profile_build_arg():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    for svc in ("legba-registry", "legba-runtime-dapr"):
        args = compose["services"][svc]["build"].get("args") or {}
        assert args.get("LEGBA_PROFILE") == "${LEGBA_PROFILE:-profiles/geopolitics}", svc


def test_deploy_sh_puts_the_profile_prompts_on_the_registrar_path():
    text = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert 'PROFILE_PROMPTS="${PROFILE_ABS}/prompts"' in text
    assert (
        'REGISTRAR_PYTHONPATH="/install/lib/python3.11/site-packages'
        '${PROFILE_PROMPTS:+:${PROFILE_PROMPTS}}"'
    ) in text
    # both registrar-image runners carry it, and the runtime import is verified
    assert text.count('-e "PYTHONPATH=${REGISTRAR_PYTHONPATH}"') == 2
    assert 'dc exec -T legba-runtime-dapr python -c "import ${pkg}"' in text


def test_rolling_deploy_can_verify_an_import():
    text = (REPO / "scripts" / "rolling_deploy.sh").read_text(encoding="utf-8")
    assert "--verify-import)" in text
    assert 'python -c "import ${mod}"' in text


# ---------------------------------------------------------------------------
# 3. the family drives the tuple
# ---------------------------------------------------------------------------


def test_the_family_is_the_profiles_namespaced_lens_axis():
    from legba.data.schemas.vocabulary import validate_family

    assert LEAN_LENS_FAMILY == family_of_profile("geopolitics") == "geopolitics.lens"
    assert validate_family(LEAN_LENS_FAMILY, profile="geopolitics") == LEAN_LENS_FAMILY
    assert lean_prompt_module("lens_left") == f"{prompts_package_name('geopolitics')}.lens_left"


def test_the_seed_file_is_the_literal_in_order():
    """THE DRIFT GUARD: the walker's seed rows for the family equal the
    documented literal, value for value, in order."""
    seeds = [s for s in walker.load_vocabulary_seeds() if s.family == LEAN_LENS_FAMILY]
    assert tuple(s.value for s in seeds) == LEAN_LENS_FALLBACK
    assert {s.origin for s in seeds} == {"descriptors/vocabulary_geopolitics_lens.yaml"}
    assert all(s.notes for s in seeds)
    # each value names a descriptor that exists, on the shared journal kind
    for s in seeds:
        assert _descriptor(s.value)["identity"]["kind"] == "journal_assessor"
    # a seed file is not a walked registry descriptor
    planned, _ = walker.plan()
    assert "geopolitics_lens" not in {p.descriptor_id for p in planned}


def test_journal_assessor_reads_the_family_with_the_literal_fallback():
    from legba.data.analysts import journal_assessor as ja

    # no vocabulary installed at import in this process: the literal
    assert ja.LENS_LEAN_ANALYST_IDS == LEAN_LENS_FALLBACK
    assert ja.LENS_ANALYST_IDS == ja.LENS_FACULTY_ANALYST_IDS + ja.LENS_LEAN_ANALYST_IDS
    assert ja.LENS_FACULTY_ANALYST_IDS == _FACULTIES   # a platform tuple, never the family
    src = pathlib.Path(ja.__file__).read_text(encoding="utf-8")
    assert "LENS_LEAN_ANALYST_IDS: tuple[str, ...] = lean_lens_ids()" in src


class _Source:
    """A TAXON-style vocabulary source (the registry's VocabularyCache shape)."""

    def __init__(self, values=None, unloaded=(), boom=False):
        self._values = values or {}
        self.families_unloaded = [{"family": f} for f in unloaded]
        self._boom = boom

    def values(self, family):
        if self._boom:
            raise RuntimeError("registry down")
        return set(self._values.get(family, ()))


def test_a_loaded_family_drives_the_tuple():
    # narrowed: a lean the profile retired drops out, the literal's order kept
    narrowed = _Source({LEAN_LENS_FAMILY: {"lens_isolationist", "lens_left"}})
    assert lean_lens_ids(vocab=narrowed) == ("lens_left", "lens_isolationist")
    # extended: a new lean is read after the literal's, sorted
    extended = _Source({LEAN_LENS_FAMILY: set(LEAN_LENS_FALLBACK) | {"lens_zeta", "lens_alpha"}})
    assert lean_lens_ids(vocab=extended) == LEAN_LENS_FALLBACK + ("lens_alpha", "lens_zeta")
    # the runtime's snapshot shape reads the same way
    idx = VocabularyIndex(values={LEAN_LENS_FAMILY: frozenset({"lens_right"})})
    assert lean_lens_ids(vocab=idx) == ("lens_right",)


@pytest.mark.parametrize(
    "source",
    [
        _Source({}),                                                     # family absent
        _Source({LEAN_LENS_FAMILY: set()}),                              # family empty
        _Source({LEAN_LENS_FAMILY: {"lens_left"}}, unloaded=[LEAN_LENS_FAMILY]),
        _Source(boom=True),                                              # source fails
        _Source({"sanctions.lens": {"lens_x"}}),                         # another profile's
        VocabularyIndex(),                                               # empty snapshot
        VocabularyIndex(values={LEAN_LENS_FAMILY: frozenset({"lens_left"})},
                        unloaded=frozenset({LEAN_LENS_FAMILY})),
    ],
    ids=["absent", "empty", "unloaded", "raises", "other_profile", "empty_index", "index_unloaded"],
)
def test_an_unloaded_family_reads_the_literal(source):
    assert lean_lens_ids(vocab=source) == LEAN_LENS_FALLBACK


def test_the_installed_taxon_source_is_read_through():
    from legba.data import taxon_families

    before = taxon_families.installed_vocabulary()
    try:
        taxon_families.install_vocabulary(None)
        assert lean_lens_ids() == LEAN_LENS_FALLBACK          # nothing installed
        taxon_families.install_vocabulary(_Source({LEAN_LENS_FAMILY: {"lens_centre"}}))
        assert lean_lens_ids() == ("lens_centre",)
    finally:
        taxon_families.install_vocabulary(before)


def test_lens_family_is_a_leaf():
    src = pathlib.Path(lens_family.__file__).read_text(encoding="utf-8")
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert all(
        ln.startswith(("from __future__", "import logging", "from typing", "from .profile"))
        for ln in imports
    ), imports


# ---------------------------------------------------------------------------
# 4. the manifest
# ---------------------------------------------------------------------------


def test_the_geopolitics_manifest_declares_its_prompt_package():
    geo = load_profile(GEO)
    assert geo.prompts == "prompts"
    assert geo.prompts_path == PROMPTS.resolve()
    assert geo.prompts_package == PACKAGE == prompts_package_name(geo.id)
    assert (PROMPTS / PACKAGE / "__init__.py").is_file()
    # the package holds the six leans and nothing else of the platform's
    subpackages = sorted(
        p.name for p in (PROMPTS / PACKAGE).iterdir() if (p / "__init__.py").is_file()
    )
    assert subpackages == sorted(LEAN_LENS_FALLBACK)


def test_a_prompts_dir_without_the_profiles_package_is_refused(tmp_path):
    data = yaml.safe_load((GEO / "profile.yaml").read_text(encoding="utf-8"))
    data["descriptors_dir"] = "descriptors"
    data["lifecycle"] = None
    data["rosters"].pop("source_catalog", None)
    data["rosters"].pop("gazetteer", None)  # CANON-1: the gazetteer file is not copied
    data.pop("projection", None)  # VIEW-1: the view file is not copied
    data.pop("constants", None)  # SPLIT-3: inline instance data the copy does not need (validated against ui.frames)
    data.pop("subjects", None)  # SUBJECT-1: the label style; the platform default applies
    d = tmp_path / "geopolitics"
    (d / "descriptors").mkdir(parents=True)
    (d / "prompts" / "some_other_package").mkdir(parents=True)
    (d / "prompts" / "some_other_package" / "__init__.py").write_text("", encoding="utf-8")
    (d / "profile.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ProfileError, match="prompts holds no package legba_geopolitics"):
        load_profile(d)
    (d / "prompts" / PACKAGE).mkdir()
    (d / "prompts" / PACKAGE / "__init__.py").write_text("", encoding="utf-8")
    assert load_profile(d).prompts_package == PACKAGE
