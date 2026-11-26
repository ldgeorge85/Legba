# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Presigned-URL redaction — ONE helper for every persisted redirected URL.

The OFAC download redirects to a PRESIGNED object-store URL whose
query carries a temporary credential (``X-Amz-Security-Token`` /
``X-Amz-Signature``). ``instrument_snapshot`` stripped that query from its
``final_url`` — but nothing stopped a different handler persisting a redirected
URL (the web-evidence fetcher's ``fetched_url_chain`` and ``canonical_url``,
a scraper's, a third-party crawler's metadata) with the same kind of query.

The rule here is an ALLOW-LIST of what is safe to keep: a URL whose query
carries NO signing parameter is returned UNCHANGED (``?id=123`` still names a
page, and dedupe keys on it); a URL whose query carries ANY signing parameter
loses its whole query and fragment (we cannot tell which of the remaining
parameters are bound to the signature). Scheme, host and path stay — they name
the published object.

:func:`redact_presigned_in` walks a JSON-shaped value (the ``raw_provenance``
dict) and applies the rule to every URL found in any string, so a write path
cannot forget a key. A LEAF: stdlib only.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

#: Query parameter names (lower-cased) that mean "this URL carries a signing
#: credential": AWS SigV4 / SigV2 presigns, GCS signed URLs.
SIGNING_PARAMS = frozenset({
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-algorithm",
    "x-amz-signedheaders",
    "signature",
    "awsaccesskeyid",
    "x-goog-signature",
    "x-goog-credential",
    "x-goog-algorithm",
    "googleaccessid",
})

#: Azure SAS: ``sig`` alone is too common a name; with a SAS sibling it is one.
_SAS_SIBLINGS = frozenset({"sv", "se", "sp", "sr", "st", "spr", "srt", "ss"})

# A URL inside free text: stops at whitespace and the usual JSON/markup
# delimiters. A trailing sentence punctuation mark is left outside the match.
_URL_IN_TEXT = re.compile(r"""https?://[^\s"'<>\\]+""")
_TRAILING = ".,;:)]}"


def strip_query(url: str) -> str:
    """``url`` without its query or fragment, ALWAYS (scheme, host, path stay)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def is_presigned(url: str) -> bool:
    """True when ``url``'s query carries a signing parameter."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return False
    if not query:
        return False
    names = {k.lower() for k, _ in parse_qsl(query, keep_blank_values=True)}
    if names & SIGNING_PARAMS:
        return True
    return "sig" in names and bool(names & _SAS_SIBLINGS)


def redact_presigned_url(url: str) -> str:
    """``url`` itself unless it is presigned; a presigned one loses its query."""
    if not isinstance(url, str) or not url:
        return url
    try:
        return strip_query(url) if is_presigned(url) else url
    except ValueError:
        return url


def _redact_text(text: str) -> str:
    if "://" not in text:
        return text

    def _one(m: re.Match[str]) -> str:
        raw = m.group(0)
        stripped = raw.rstrip(_TRAILING)
        tail = raw[len(stripped):]
        return redact_presigned_url(stripped) + tail

    return _URL_IN_TEXT.sub(_one, text)


def redact_presigned_in(value: Any) -> Any:
    """A copy of a JSON-shaped ``value`` with every presigned URL in any string
    (dict value, list item, or embedded in free text) reduced to its path.
    Non-container, non-string values pass through; the input is not mutated."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: redact_presigned_in(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_presigned_in(v) for v in value]
    return value


__all__ = [
    "SIGNING_PARAMS",
    "is_presigned",
    "redact_presigned_in",
    "redact_presigned_url",
    "strip_query",
]
