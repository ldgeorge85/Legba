# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Environment references in stack-component config values.

A stack component may name an environment variable instead of carrying a
value inline, for values that differ per instance but are not secrets (an
endpoint address, a served model name)::

    api_endpoint:
      factory_kind: text
      raw: ${env:LEGBA_GATEWAY_API_ENDPOINT}

The registry stores the reference exactly as written, so the stored body and
its content hash are the same on every instance. The reference is resolved
when the component is USED (a handler or client is built from the row, a
health probe runs), from the process environment. A variable that is unset or
blank raises :class:`EnvRefError` naming the variable, the component and the
field; there is no default and no fallback to another value.

A value that is not a reference (an address or model name written inline) is
returned unchanged, so components written that way keep working.

Standard library only: the registry service imports this on its slim image.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

#: The whole value must be the reference; no partial interpolation.
ENV_REF_PATTERN = re.compile(r"^\$\{env:([A-Z_][A-Z0-9_]*)\}$")
_ENV_REF_PREFIX = "${env:"


class EnvRefError(ValueError):
    """An environment reference could not be resolved."""


def env_ref_name(value: Any) -> str | None:
    """The variable a value names, or ``None`` for an inline value.

    A string that starts like a reference but is malformed raises, so a typo
    never travels on as a literal address or model name.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith(_ENV_REF_PREFIX):
        return None
    match = ENV_REF_PATTERN.match(text)
    if match is None:
        raise EnvRefError(
            f"malformed environment reference {value!r}: write it as "
            "${env:NAME} with NAME in upper case letters, digits and underscores"
        )
    return match.group(1)


def resolve_env_value(
    value: Any,
    *,
    component_id: str,
    field: str,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Return ``value`` with an environment reference replaced by its value."""
    name = env_ref_name(value)
    if name is None:
        return value
    env = os.environ if environ is None else environ
    resolved = (env.get(name) or "").strip()
    if not resolved:
        raise EnvRefError(
            f"stack component {component_id!r} field {field!r} names the "
            f"environment variable {name}, which is unset or empty; set it in "
            "this instance's .env"
        )
    return resolved


def parse_token_count(value: Any) -> int | None:
    """A declared token count: a positive whole number, or a string of digits
    (what a resolved reference reads as). ``None`` for an ``${env:NAME}``
    reference not yet resolved, which is valid until the component is used.
    Anything else raises ``ValueError``."""
    if isinstance(value, bool):
        raise ValueError(f"expected a positive whole number, got {value!r}")
    if isinstance(value, str):
        if env_ref_name(value) is not None:
            return None
        text = value.strip()
        if not text.isdigit():
            raise ValueError(
                f"expected a positive whole number or ${{env:NAME}}, got {value!r}"
            )
        value = int(text)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"expected a positive whole number, got {value!r}")
    return value


def resolve_token_count(
    value: Any,
    *,
    component_id: str,
    field: str,
    environ: Mapping[str, str] | None = None,
) -> int:
    """A declared token count as an int, resolving an env reference first.
    An unset variable raises :class:`EnvRefError`; a malformed value
    ``ValueError``."""
    resolved = resolve_env_value(value, component_id=component_id, field=field, environ=environ)
    count = parse_token_count(resolved)
    if count is None:  # pragma: no cover — a resolved value is never a reference
        raise EnvRefError(f"{component_id!r} field {field!r} did not resolve")
    return count


def resolve_config_env_refs(
    config: Mapping[str, Any],
    *,
    component_id: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A copy of a component's ``config`` mapping (the stored JSON shape) with
    every field's ``raw`` reference resolved. Top-level fields only: that is
    where endpoint and model fields live. Bare (unwrapped) string values are
    resolved too."""
    out: dict[str, Any] = {}
    for key, entry in config.items():
        if isinstance(entry, Mapping) and "raw" in entry:
            raw = entry.get("raw")
            resolved = resolve_env_value(
                raw, component_id=component_id, field=str(key), environ=environ,
            )
            out[key] = entry if resolved is raw else {**entry, "raw": resolved}
        else:
            out[key] = resolve_env_value(
                entry, component_id=component_id, field=str(key), environ=environ,
            )
    return out


def resolve_model_env_refs(
    config: Any,
    *,
    component_id: str,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """The typed-config twin of :func:`resolve_config_env_refs`: a copy of a
    pydantic config model whose factory values carry resolved ``raw``
    values. Returns the same object when nothing needed resolving."""
    fields = getattr(type(config), "model_fields", None)
    if fields is None:
        return config
    updates: dict[str, Any] = {}
    for name in fields:
        value = getattr(config, name, None)
        raw = getattr(value, "raw", None)
        if env_ref_name(raw) is None:
            continue
        resolved = resolve_env_value(
            raw, component_id=component_id, field=name, environ=environ,
        )
        updates[name] = value.model_copy(update={"raw": resolved})
    return config.model_copy(update=updates) if updates else config


__all__ = [
    "ENV_REF_PATTERN",
    "EnvRefError",
    "env_ref_name",
    "parse_token_count",
    "resolve_config_env_refs",
    "resolve_env_value",
    "resolve_model_env_refs",
    "resolve_token_count",
]
