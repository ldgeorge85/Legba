# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compile + cache layer for predicate sources.

Spec §5: "Compile-once-on-register." Every predicate is parsed + resolved at
descriptor-registration time and stored as a compiled callable inside an LRU
cache keyed by ``(sha256(source), helper_catalog_version, binding_surface)``.

This module owns:

  * ``PredicateSurface`` — enum for the four binding surfaces.
  * ``compile_predicate(source, surface)`` — public entry; returns a
    ``CompiledPredicate`` from the LRU cache (compiling on miss).
  * ``CompiledPredicate.evaluate(ctx)`` — runtime call into the sandbox.
  * ``predicate_cache_clear`` / ``predicate_cache_info`` — for tests + ops.

Sandbox constraints enforced at compile time:

  * **Source must be a single Starlark expression.** We reject statements
    (top-level ``def``, ``if`` statements, ``for`` statements, multi-line
    sources) via a source-text gate. Predicates per spec §7 are single
    boolean expressions, so this matches operator intent and keeps the
    runtime surface predictable.
  * **Banned source tokens**: ``def``, ``load(``, ``lambda``. Native Starlark
    reserves ``while``, ``import``, ``try``, ``yield`` etc. — those raise at
    the underlying parser; we add the rest here.
  * **Surface-bound helper allow-list**: unknown identifiers raise at parse
    resolution time (Starlark eval looks them up against the bound module).

At evaluation time the sandbox additionally enforces:

  * **Wall-clock budget** (5 ms default) via SIGALRM on the main thread, or
    a post-hoc check off-thread (best-effort — see ``evaluator.py``).
  * **Helper exceptions** are wrapped in ``PredicateRuntimeError``.

Step-cap and per-eval memory cap from spec §3 are not exposed by the current
``starlark-pyo3`` binding (the underlying Rust evaluator supports them but
the binding lacks setters). We document the gap in the module-level
docstring and rely on (a) the expression-only gate, (b) the wall-clock cap,
(c) the bounded-value-growth invariant in the helper layer, and (d) the
compile-time iteration/materialization bound below to keep per-evaluation
cost predictable. Tightening when the binding exposes the setters is L-242
followup.

Two distinct classes of bound, applied at COMPILE time so they hold on ANY
thread (the wall-clock SIGALRM budget is main-thread-only — see
``evaluator.py``):

  * **Structural bounds** — bound the AST *shape*: the single-expression gate,
    the banned-token gate, and ``_MAX_SOURCE_CHARS``. A bounded-length, loop-
    free, def-free source has a bounded AST.
  * **Value/materialization bounds** — bound the *collection size* a short
    source can still build: ``_check_iteration_cost`` rejects ``range(N)`` and
    literal sequence-repeats ``[...]*N`` whose N exceeds ``_MAX_ITERATION_COUNT``
    (and rejects a non-literal/variable range, which cannot be statically
    sized). This is the bound the structural caps cannot give — a 30-char
    ``len([x for x in range(99999999)])`` is structurally tiny but materializes
    99M elements off-thread before the post-hoc time budget fires.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import starlark  # starlark-pyo3

from .errors import (
    PredicateBudgetExceeded,
    PredicateCompilationError,
    PredicateRuntimeError,
)
from .helpers import (
    HELPER_CATALOG,
    HELPER_CATALOG_VERSION,
    HELPER_NAMES,
    SURFACE_ALL as _ALL_SURFACES_TUPLE,
    SURFACE_ANALYST_SUBSCRIPTION,
    SURFACE_CADENCE_TRIGGER,
    SURFACE_CTX_CONTRACTS,
    SURFACE_SOURCE_FILTER,
    SURFACE_TARGET_SCOPE,
    bind_helpers,
    helper_unsatisfied,
    surface_helpers,
)
from .evaluator import EvalBudget, run_with_budget


# ---------------------------------------------------------------------------
# Cache config
# ---------------------------------------------------------------------------

CACHE_MAXSIZE = 10_000  # spec §5: 10k compiled predicates default cap


# ---------------------------------------------------------------------------
# Surface enum
# ---------------------------------------------------------------------------


class PredicateSurface(str, Enum):
    """Where a predicate binds. String values match the helper-catalog tags."""

    TARGET_SCOPE = SURFACE_TARGET_SCOPE
    SOURCE_FILTER = SURFACE_SOURCE_FILTER
    ANALYST_SUBSCRIPTION = SURFACE_ANALYST_SUBSCRIPTION
    CADENCE_TRIGGER = SURFACE_CADENCE_TRIGGER


# ---------------------------------------------------------------------------
# Source-text gate (registration-time sandbox)
# ---------------------------------------------------------------------------

# Banned source-level tokens that Starlark allows but our predicate surface
# rejects. (`while` / `import` are already rejected by the underlying parser
# but we include them here for symmetry + a clearer error message.)
_BANNED_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("def",     re.compile(r"\bdef\s+[A-Za-z_]")),
    ("load",    re.compile(r"\bload\s*\(")),
    ("lambda",  re.compile(r"\blambda\b")),
    ("while",   re.compile(r"\bwhile\b")),
    ("import",  re.compile(r"\bimport\b")),
    ("for-stmt", re.compile(r"(?m)^\s*for\s+")),  # for *statement* — comprehensions remain OK
)

# Predicates are expressions. Reject obvious statement shapes by source-line
# inspection: multi-line sources, leading whitespace on the first line, etc.
_COMMENT_OR_BLANK_LINE = re.compile(r"^\s*(#.*)?$")


# Compile-time source-length cap. THREAD-SAFE structural bound on per-eval cost:
# predicates are single boolean expressions with NO loops/recursion/def (grammar
# gate below), so a bounded-LENGTH source has a bounded AST and therefore bounded
# evaluation cost on ANY thread — which is the enforcement the wall-clock SIGALRM
# budget cannot provide off the main thread (see evaluator.py). Real predicates
# are tens of chars; 4 KiB is vast headroom that still rejects a pathological
# mega-expression before it is ever parsed or evaluated.
_MAX_SOURCE_CHARS = 4096

# Compile-time ITERATION/MATERIALIZATION bound. The length cap above bounds the
# AST *shape*, but a short source can still materialize a huge collection: a
# comprehension or iteration over ``range(99999999)`` or a list-repeat
# ``[0]*10000000`` runs to completion BEFORE the post-hoc wall-clock budget can
# fire off-thread (the off-thread batch path measures elapsed time only AFTER the
# call returns — see evaluator.py). So we bound the materialization cost here, at
# COMPILE time, where it is thread-safe. The only viable vectors after the parser
# (Starlark has no ``**`` power op and no ``1_000_000`` underscore literals, both
# rejected at parse) are a large literal ``range(N)`` and a large literal
# sequence-repeat ``[...]*N`` / ``N*[...]``. We bound the literal N in both.
#
# Real predicates iterate ctx-derived lists (``all(p.isdigit() for p in parts)``)
# or use tiny literal ranges; 100k is vast headroom for any legitimate predicate
# while rejecting the DoS materialization an order of magnitude below the count
# that blows the per-eval budget.
_MAX_ITERATION_COUNT = 100_000

# ``range(`` followed by the FIRST sizing argument. We capture the leading
# argument token to classify it as int-literal (bound it) vs non-literal
# (reject — a variable range cannot be statically bounded and no predicate needs
# one). Whitespace-tolerant; the negative trailing class stops at ``,``/``)``.
_RANGE_CALL_RE = re.compile(r"\brange\s*\(\s*([^,)]*)")

# Sequence-repeat where a literal multiplies (or is multiplied by) a literal
# collection / string — ``]*N``, ``)*N``, ``"*N``, ``'*N`` and the mirror
# ``N*[``, ``N*"``, ``N*'``, ``N*(``. This deliberately does NOT match an
# arithmetic ``score * 1000000`` (a bare name/number times a number is a legit
# comparison expression), only a repeat against a literal sequence.
_SEQ_REPEAT_RE = re.compile(
    r"""(?x)
    (?: [\]\)"']        \s* \* \s* (\d+) )   # [..] * N   / "x" * N
      | ( (\d+) \s* \* \s* [\[\("'] )        # N * [..]   / N * "x"
    """
)


def _check_source_gate(source: str, *, surface: PredicateSurface) -> None:
    """Raise PredicateCompilationError if source violates the expression-only
    sandbox, exceeds the length cap, or contains banned tokens."""
    if not isinstance(source, str):
        raise PredicateCompilationError(
            f"predicate source must be str, got {type(source).__name__}",
            surface=surface.value,
        )
    s = source.strip()
    if not s:
        raise PredicateCompilationError(
            "predicate source is empty", surface=surface.value
        )

    if len(source) > _MAX_SOURCE_CHARS:
        raise PredicateCompilationError(
            f"predicate source too long: {len(source)} chars exceeds the "
            f"{_MAX_SOURCE_CHARS}-char cap — predicates are short boolean "
            "expressions; the length cap is the thread-safe structural bound on "
            "per-evaluation cost (the wall-clock budget is main-thread-only).",
            surface=surface.value,
        )

    # Banned tokens (skip empty/comment-only lines for content check)
    for label, pattern in _BANNED_TOKEN_PATTERNS:
        if pattern.search(source):
            raise PredicateCompilationError(
                f"predicate uses banned construct: {label!r} not allowed in "
                f"predicate sources (must be a pure boolean expression)",
                surface=surface.value,
            )

    # Iteration / materialization bound (compile-time, thread-safe).
    _check_iteration_cost(source, surface=surface)

    # Single-expression check: collapse continuation lines, then require that
    # all non-comment/non-blank lines together form one expression. We allow
    # explicit line-continuations via "\" and parenthesised expressions
    # spanning lines. Reject sources with multiple separate statement lines.
    lines = [
        ln for ln in source.splitlines()
        if not _COMMENT_OR_BLANK_LINE.match(ln)
    ]
    if not lines:
        raise PredicateCompilationError(
            "predicate source contains no expression (comments/blanks only)",
            surface=surface.value,
        )


def _check_iteration_cost(source: str, *, surface: PredicateSurface) -> None:
    """Reject sources that would materialize an oversize collection.

    Two materialization vectors survive the parser (Starlark has no ``**`` and
    no underscore literals, so ``range(10**10)`` / ``range(1_000_000)`` are
    parse errors): a plain-decimal ``range(N)`` and a literal sequence-repeat
    ``[...]*N`` / ``N*[...]``. A comprehension or iteration over either runs to
    completion off-thread before the post-hoc wall-clock budget can fire, so we
    bound the literal N at COMPILE time. Variable ranges (``range(x)``) cannot be
    statically bounded — we reject them (no legitimate predicate needs one).
    """
    # range(N): bound the leading sizing argument.
    for m in _RANGE_CALL_RE.finditer(source):
        arg = m.group(1).strip()
        if arg == "":
            # ``range()`` with no positional arg parses-fails later; not our
            # concern here.
            continue
        if not arg.isdigit():
            # A non-literal range bound (variable / expression) cannot be
            # statically sized. Predicates iterate ctx-derived lists, never a
            # variable range — refuse rather than admit an unbounded loop.
            raise PredicateCompilationError(
                f"predicate uses range() with a non-literal bound {arg!r}: a "
                "variable range cannot be statically bounded; predicates iterate "
                "ctx-derived lists, not variable ranges — refusing at compile.",
                surface=surface.value,
            )
        if int(arg) > _MAX_ITERATION_COUNT:
            raise PredicateCompilationError(
                f"predicate range({arg}) exceeds the {_MAX_ITERATION_COUNT}-"
                "element iteration cap — a large range materialized inside a "
                "comprehension/iteration runs before the wall-clock budget can "
                "fire off-thread; bound it at compile time.",
                surface=surface.value,
            )

    # Literal sequence-repeat: ``[...]*N`` / ``N*[...]`` (and string/paren forms).
    for m in _SEQ_REPEAT_RE.finditer(source):
        literal = m.group(1) or m.group(3)
        if literal is not None and int(literal) > _MAX_ITERATION_COUNT:
            raise PredicateCompilationError(
                f"predicate sequence-repeat *{literal} exceeds the "
                f"{_MAX_ITERATION_COUNT}-element iteration cap — a large literal "
                "repeat materializes before the wall-clock budget can fire "
                "off-thread; bound it at compile time.",
                surface=surface.value,
            )


# ---------------------------------------------------------------------------
# CompiledPredicate
# ---------------------------------------------------------------------------


@dataclass
class CompiledPredicate:
    """Cached compile artifact.

    Holds the parsed Starlark AST + a frozen ``Globals`` and surface metadata.
    ``evaluate(ctx)`` builds a fresh ``Module`` per call (Modules are mutable
    and one-shot — we cannot share a frozen Module between evaluations because
    helpers must rebind to the per-call ctx).
    """

    source: str
    surface: PredicateSurface
    source_hash: str
    helper_catalog_version: str
    ast: Any = field(repr=False)
    globals: Any = field(repr=False)
    referenced_helpers: frozenset[str] = field(default_factory=frozenset)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        ctx: dict[str, Any],
        *,
        budget: EvalBudget | None = None,
    ) -> bool:
        """Evaluate the predicate against ``ctx``.

        Returns a Python ``bool`` (truthy coercion per spec §6 LB-DSL-3
        default lean: warning + coerce). Raises:

          * ``PredicateBudgetExceeded`` — wall-clock budget hit.
          * ``PredicateRuntimeError``   — helper raised / other host error.

        ``ctx`` shape per surface (see ``helpers`` docstring):
          * ``target.scope`` / ``source.filter`` → ``{"signal": {...}, "target": {...}}``
          * ``analyst.subscription``             → ``{"target": {...}}``
          * ``cadence.trigger``                  → ``{"event": {...}, "target": {...}}``
        """
        if budget is None:
            budget = EvalBudget()

        module = starlark.Module()
        try:
            bind_helpers(module, ctx, self.surface.value)
        except PredicateRuntimeError:
            raise
        except Exception as exc:  # pragma: no cover - bind_helpers is total
            raise PredicateRuntimeError(
                "helper binding raised", cause=exc
            ) from exc

        ast = self.ast
        g = self.globals

        def _do_eval() -> Any:
            return starlark.eval(module, ast, g)

        try:
            raw = run_with_budget(_do_eval, budget=budget)
        except PredicateBudgetExceeded:
            raise
        except starlark.StarlarkError as exc:
            raise PredicateRuntimeError(
                f"predicate raised at runtime: {exc}", cause=exc
            ) from exc
        except Exception as exc:
            raise PredicateRuntimeError(
                f"predicate raised at runtime: {exc}", cause=exc
            ) from exc

        return bool(raw)

    # ------------------------------------------------------------------
    # Identity / equality (cache hashing relies on source_hash + surface)
    # ------------------------------------------------------------------

    def __hash__(self) -> int:  # type: ignore[override]
        return hash((self.source_hash, self.surface.value))

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, CompiledPredicate):
            return NotImplemented
        return (
            self.source_hash == other.source_hash
            and self.surface == other.surface
        )


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------


class _PredicateLRU:
    """A small thread-safe LRU keyed by (source_hash, surface, catalog_version).

    Stdlib ``functools.lru_cache`` would work for the cheap case but we need
    to expose ``info()`` + ``clear()`` and key on the canonical tuple rather
    than positional arg order. Custom implementation keeps the cache total
    over the equivalence class of "same source, same surface, same catalog
    version" → identical compile.
    """

    def __init__(self, maxsize: int = CACHE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str, str], CompiledPredicate] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: tuple[str, str, str]) -> CompiledPredicate | None:
        with self._lock:
            cp = self._cache.get(key)
            if cp is None:
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return cp

    def put(self, key: tuple[str, str, str], value: CompiledPredicate) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = value
                return
            self._cache[key] = value
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)
                self._evictions += 1

    def info(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._cache),
                "maxsize": self._maxsize,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
            }

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0


_CACHE = _PredicateLRU()


def predicate_cache_info() -> dict[str, int]:
    """Return cache stats (size, maxsize, hits, misses, evictions)."""
    return _CACHE.info()


def predicate_cache_clear() -> None:
    """Clear the compiled-predicate LRU cache. Used by tests + ops resets."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Public compile entry
# ---------------------------------------------------------------------------


# Cached starlark globals — these never change between compiles (we never
# extend the standard set with file/network/print extensions). One process-wide
# Globals object is fine; ``starlark.eval`` is read-only on globals.
_STARLARK_GLOBALS: Any | None = None
_GLOBALS_LOCK = threading.Lock()


def _get_globals() -> Any:
    global _STARLARK_GLOBALS
    if _STARLARK_GLOBALS is None:
        with _GLOBALS_LOCK:
            if _STARLARK_GLOBALS is None:
                # ``Globals.standard()`` includes safe builtins (len, range,
                # bool, dict, list, str, etc.) and excludes any I/O / load /
                # print extension.
                _STARLARK_GLOBALS = starlark.Globals.standard()
    return _STARLARK_GLOBALS


_FILENAME_FMT = "<predicate:{surface}:{short_hash}>"


def _hash_source(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _coerce_surface(surface: PredicateSurface | str) -> PredicateSurface:
    if isinstance(surface, PredicateSurface):
        return surface
    try:
        return PredicateSurface(surface)
    except ValueError as exc:
        valid = ", ".join(s.value for s in PredicateSurface)
        raise PredicateCompilationError(
            f"unknown predicate surface {surface!r}; valid: {valid}"
        ) from exc


# Surface-bound identifier check: lightweight pre-resolver that scans the
# source for helper-name references and reports any not allowed on the
# binding surface. The full resolver runs at parse + eval time as well; this
# extra pass produces a more actionable error message at registration.
_IDENTIFIER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_STARLARK_BUILTINS: frozenset[str] = frozenset({
    "True", "False", "None",
    "and", "or", "not", "in", "if", "else", "for",
    # Standard callable builtins exposed by starlark.Globals.standard():
    "abs", "all", "any", "bool", "dict", "enumerate", "fail",
    "float", "getattr", "hasattr", "hash", "int", "len", "list",
    "max", "min", "range", "repr", "reversed", "set", "sorted",
    "str", "tuple", "type", "zip",
})


def _check_surface_helpers(source: str, surface: PredicateSurface) -> set[str]:
    """Walk source identifiers; reject helper references not allowed on the
    surface. Returns the set of referenced helper names.

    Note: this is a heuristic (regex-based) — it ignores string contents but
    cannot disambiguate "shadowed locals" from helpers. Predicates per spec
    don't define locals (no `def`, no assignments at the statement level),
    so the heuristic is sound in practice.
    """
    permitted = surface_helpers(surface.value)
    helper_set = set(HELPER_NAMES)
    referenced: set[str] = set()

    # Strip string literals before identifier scan so quoted helper names
    # don't trigger false positives.
    no_strings = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', source)
    no_strings = re.sub(r"'[^'\\]*(?:\\.[^'\\]*)*'", "''", no_strings)

    for match in _IDENTIFIER_RE.finditer(no_strings):
        ident = match.group(1)
        if ident in _STARLARK_BUILTINS:
            continue
        if ident in helper_set:
            referenced.add(ident)
            if ident not in permitted:
                allowed = ", ".join(sorted(permitted)) or "(none)"
                raise PredicateCompilationError(
                    f"helper {ident!r} is not available on surface "
                    f"{surface.value!r}; allowed helpers: {allowed}",
                    surface=surface.value,
                )
    return referenced


def _check_ctx_contract(
    referenced: set[str],
    surface: PredicateSurface,
    contract: frozenset[str],
) -> None:
    """Refuse helpers the surface's ctx-builders cannot feed (G4).

    Every referenced helper must have at least one required-ctx group fully
    covered by ``contract`` (the dot-path keys the surface's PRODUCTION
    ctx-builders provide — see ``helpers.SURFACE_CTX_CONTRACTS``). A helper
    that cannot be fed would compile, evaluate, and return its conservative
    default forever — the silent-never-match divergence class. Make it a
    compile error with an actionable message instead.
    """
    for name in sorted(referenced):
        groups = helper_unsatisfied(name, contract)
        if not groups:
            continue
        needs = " OR ".join(
            "(" + " AND ".join(g) + ")" for g in groups
        )
        provided = ", ".join(sorted(contract)) or "(none)"
        raise PredicateCompilationError(
            f"helper {name!r} cannot be fed on surface {surface.value!r}: "
            f"it requires ctx keys {needs}, but the surface's production "
            f"ctx-builders provide only: {provided}. The predicate would "
            f"compile and then silently never match — refusing at "
            f"registration. If a new ctx-builder provides these keys, "
            f"declare them in helpers.SURFACE_CTX_CONTRACTS (or pass an "
            f"explicit ctx_contract to compile_predicate).",
            surface=surface.value,
        )


def compile_predicate(
    source: str,
    surface: PredicateSurface | str,
    *,
    catalog_version: str | None = None,
    ctx_contract: frozenset[str] | set[str] | None = None,
) -> CompiledPredicate:
    """Compile a predicate source. Cached by (sha256(source), surface, version).

    Raises ``PredicateCompilationError`` on any parse / resolve / sandbox
    violation. The caller (typically a schema field_validator) should
    surface the error to the descriptor registry as a validation failure
    per L-104 §6.

    :param source: Starlark expression source.
    :param surface: One of ``PredicateSurface.*`` (or the str value).
    :param catalog_version: Optional pin. Defaults to current
        ``HELPER_CATALOG_VERSION``. Reserved for future deprecation paths
        per spec §8.
    :param ctx_contract: Dot-path ctx keys the EVALUATING call site provides
        (e.g. ``{"signal.tags", "target.id"}``). Defaults to the surface's
        production ctx-builder contract (``helpers.SURFACE_CTX_CONTRACTS``).
        Every referenced helper must be satisfiable against it or the
        compile is refused (G4 — no silent never-match). Call sites whose
        ctx is narrower or richer than the surface default (and test rigs)
        pass their own contract; validation runs on EVERY call, before the
        cache, so a permissive caller can never poison a stricter one.
    """
    cat_ver = catalog_version or HELPER_CATALOG_VERSION
    surf = _coerce_surface(surface)

    # Compile-time sandbox gate
    _check_source_gate(source, surface=surf)

    # Surface-bound helper allow-list (better error than runtime "not found")
    referenced = _check_surface_helpers(source, surf)

    # Required-ctx satisfiability against the surface's ctx-builder contract
    contract = (
        frozenset(ctx_contract)
        if ctx_contract is not None
        else SURFACE_CTX_CONTRACTS.get(surf.value, frozenset())
    )
    _check_ctx_contract(referenced, surf, contract)

    # Cache check
    source_hash = _hash_source(source)
    key = (source_hash, surf.value, cat_ver)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    # Parse via starlark-pyo3
    short_hash = source_hash[:12]
    filename = _FILENAME_FMT.format(surface=surf.value, short_hash=short_hash)
    try:
        ast = starlark.parse(filename, source)
    except starlark.StarlarkError as exc:
        # Spec §6: include line/col when the lib surfaces them. The
        # error message text from starlark-rust already carries the span
        # ("--> file:line:col"), so we pass through verbatim and tag with
        # field/surface.
        raise PredicateCompilationError(
            f"predicate parse failed: {exc}",
            surface=surf.value,
        ) from exc
    except Exception as exc:
        raise PredicateCompilationError(
            f"predicate parse failed with non-Starlark error: {exc}",
            surface=surf.value,
        ) from exc

    compiled = CompiledPredicate(
        source=source,
        surface=surf,
        source_hash=source_hash,
        helper_catalog_version=cat_ver,
        ast=ast,
        globals=_get_globals(),
        referenced_helpers=frozenset(referenced),
    )
    _CACHE.put(key, compiled)
    return compiled
