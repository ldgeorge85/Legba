# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Property factory catalog (per L-101 §2).

Thirteen factories cover the common 80 % of descriptor-field shapes;
`Property.Free` is the escape hatch.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class FactoryValue(BaseModel):
    """Marker base — every factory output carries a `factory_kind`
    discriminator so the registry can render the right editor widget and run
    the right resolver."""

    model_config = ConfigDict(extra="allow")

    factory_kind: ClassVar[str] = "base"
    raw: Any = None
    ui_hint: dict[str, Any] = Field(default_factory=dict)


class Secret(FactoryValue):
    """Credential reference (never the credential itself)."""

    factory_kind: ClassVar[str] = "secret"
    raw: str

    @classmethod
    def of(cls, name: str) -> "Secret":
        if not name or "/" in name or " " in name:
            raise ValueError("Secret name must be a non-empty dotted identifier")
        return cls(raw=name, ui_hint={"masked": True})


class OAuth2(FactoryValue):
    """OAuth flow setup. Redirect handling owned by registry."""

    factory_kind: ClassVar[str] = "oauth2"
    raw: dict[str, Any]

    @classmethod
    def of(cls, provider: str, scopes: list[str]) -> "OAuth2":
        return cls(
            raw={"provider": provider, "scopes": scopes},
            ui_hint={"flow": "authorization_code"},
        )


class Text(FactoryValue):
    """Free string with optional regex / max-length validation."""

    factory_kind: ClassVar[str] = "text"
    raw: str
    regex: str | None = None
    max_length: int | None = None

    @classmethod
    def of(
        cls,
        default: str,
        regex: str | None = None,
        max_length: int | None = None,
    ) -> "Text":
        if regex:
            re.compile(regex)  # fail fast on bad pattern
        if max_length is not None and max_length <= 0:
            raise ValueError("max_length must be positive")
        return cls(raw=default, regex=regex, max_length=max_length)


class Number(FactoryValue):
    """Bounded numeric."""

    factory_kind: ClassVar[str] = "number"
    raw: float | int
    minimum: float | int | None = None
    maximum: float | int | None = None

    @classmethod
    def of(
        cls,
        default: float | int,
        *,
        minimum: float | int | None = None,
        maximum: float | int | None = None,
    ) -> "Number":
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("minimum > maximum")
        return cls(raw=default, minimum=minimum, maximum=maximum)


class Cron(FactoryValue):
    """Cron expression, parsed at registration if `croniter` is available."""

    factory_kind: ClassVar[str] = "cron"
    raw: str

    @classmethod
    def of(cls, expr: str) -> "Cron":
        try:
            from croniter import croniter

            if not croniter.is_valid(expr):
                raise ValueError(f"invalid cron expression: {expr!r}")
        except ImportError:
            # croniter optional at descriptor-author time; the registry
            # validates fully at register-time once it lands (L-110).
            pass
        return cls(raw=expr, ui_hint={"editor": "cron"})


class RateLimit(FactoryValue):
    """`N/period` with unit parser."""

    factory_kind: ClassVar[str] = "rate_limit"
    raw: str
    requests_per_second: float

    @classmethod
    def of(cls, spec: str) -> "RateLimit":
        n, _, period = spec.partition("/")
        per = {
            "s": 1, "sec": 1, "second": 1,
            "min": 60, "minute": 60,
            "h": 3600, "hour": 3600,
            "d": 86400, "day": 86400,
        }.get(period.strip().lower())
        if per is None:
            raise ValueError(f"unknown period in rate-limit spec: {spec!r}")
        try:
            count = float(n)
        except ValueError as exc:
            raise ValueError(f"bad count in rate-limit spec: {spec!r}") from exc
        return cls(raw=spec, requests_per_second=count / per)


class DropdownStatic(FactoryValue):
    """Fixed choice list."""

    factory_kind: ClassVar[str] = "dropdown_static"
    raw: str
    options: list[str]

    @classmethod
    def of(cls, default: str, options: list[str]) -> "DropdownStatic":
        if default not in options:
            raise ValueError("default must be in options")
        return cls(raw=default, options=options)


class DropdownRefreshable(FactoryValue):
    """Choices fetched at bind time."""

    factory_kind: ClassVar[str] = "dropdown_refreshable"
    raw: str
    fetcher: str


class TypedList(FactoryValue):
    """Typed list."""

    factory_kind: ClassVar[str] = "list"
    raw: list[Any]
    item_kind: str


class TypedDict(FactoryValue):
    """Typed dict."""

    factory_kind: ClassVar[str] = "dict"
    raw: dict[str, Any]
    key_kind: str = "text"
    value_kind: str


class StackRef(FactoryValue):
    """Typed reference into the stack registry."""

    factory_kind: ClassVar[str] = "stack_ref"
    raw: str
    expected_family: str | None = None


class DynamicSchema(FactoryValue):
    """Schema resolved at bind time (Workato pattern; deferred per topology §15)."""

    factory_kind: ClassVar[str] = "dynamic_schema"
    raw: dict[str, Any]
    schema_fetcher: str


class Free(FactoryValue):
    """Escape hatch."""

    factory_kind: ClassVar[str] = "free"
    raw: dict[str, Any]
    pydantic_model_ref: str | None = None


class _DropdownNamespace:
    Static = DropdownStatic
    Refreshable = DropdownRefreshable


class Property:
    """Aliased namespace matching topology §3.1 prose."""

    Secret = Secret
    OAuth2 = OAuth2
    Text = Text
    Number = Number
    Cron = Cron
    RateLimit = RateLimit
    Dropdown = _DropdownNamespace
    List = TypedList
    Dict = TypedDict
    StackRef = StackRef
    DynamicSchema = DynamicSchema
    Free = Free
