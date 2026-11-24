# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2 — what a CLAIM row's text may keep, applied at ingest.

A claim source (``scope.source_class: claim``, or a Telegram channel marked
``claim`` in its descriptor's ``config.classes``) publishes a party's own
claim: a tracker listing what a group posted, a group saying it did
something. The claim itself is the point and is kept — the claimant, the
named victim ORGANISATION, the date, the words. What is not kept is what a
leak post carries along with it. Before a claim row is enriched or stored,
:func:`apply_claim_hygiene` rewrites its payload text so that:

* **every URL is reduced to its host** (``[link: example.org]``) — no path,
  no query, no file name. An onion address is not kept at all
  (``[onion link]``): that a link was present is recorded, where it pointed
  is not. Defanged forms (``hxxp://``, ``example[.]org``) are links too. The
  row's own address (``canonical_url`` / ``payload.link``) is kept when it is
  on the source's own host (the tracker's own item page, a ``t.me`` message);
  any other address is replaced by a non-resolvable key
  (``claim-item:<sha256/16>``) so dedupe still works and no link is stored.
* **personal data is redacted**: email addresses (also ``[at]``/``[dot]``
  forms), phone numbers, government-id-like numbers (US SSN, UK NI, numbers
  labelled as a passport / national / tax id), payment-card-like numbers
  (13-19 digits passing the Luhn check), IBANs, street addresses where the
  pattern is reliable ("12 Example Street"), and credentials / secrets
  (``password: …``, ``api_key=…``, ``Bearer …``, JWTs, private-key blocks,
  well-known token prefixes). Each becomes a bracketed kind (``[email]``);
  the value is gone.
* **file listings and sample-data dumps are cut** to a bounded description:
  three or more lines (or list items) that are file names or paths become
  ``[lists N files]``; three or more delimiter-separated record lines become
  ``[sample data: N rows withheld]``.

What is counted (:class:`HygieneReport`): how many of each KIND were
reduced, redacted or cut — never a value. The source actor adds the counts
to the poll's provenance row (``source_poll_outcomes.claim_hygiene``,
migration 0265).

What code does NOT attempt: names of PRIVATE INDIVIDUALS presented as victims.
Telling a person's name from an organisation's, a product's or a place's is a
judgement a pattern cannot make reliably (NER can propose, never decide), and
a redaction that misses half the names would read as a guarantee it is not.
The owner's sign-off note records that the feed's text was read with this in
mind; a source whose posts name private people is a source not to switch on.

The RAW fetch (the feed document as served) is still kept as the poll's
receipt (``fetch_versions``), unchanged — the evidence design keeps what was
received. Who may read that stored text is :data:`CLAIM_TEXT_ACCESS`
(``claim_support.claim_text_withheld``).

A LEAF (stdlib only): the runtime host, the registry's slim image and the
tests import it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

__all__ = [
    "HYGIENE_KINDS",
    "HygieneReport",
    "apply_claim_hygiene",
    "hygiene_text",
    "merge_counts",
    "own_hosts_of",
    "poll_claim_hygiene",
    "prepare_claim_row",
    "tally",
]

#: Every kind the report can count, in the order the poll record lists them.
HYGIENE_KINDS: tuple[str, ...] = (
    "rows", "url_to_host", "onion_link", "item_link_replaced", "email", "phone",
    "gov_id", "payment_card", "iban", "street_address", "secret",
    "files_cut", "sample_rows_cut",
)

#: Payload keys that hold an ADDRESS for the row itself (not claim text).
_STRUCTURAL_URL_KEYS = frozenset({"link", "url", "guid", "external_id"})
#: Payload keys never rewritten: the platform's own stamps and the feed's
#: address (the configured URL, never claim text).
_UNTOUCHED_KEYS = frozenset({"claim", "license_class", "source_url", "published_at",
                             "summary_original_chars"})

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_TLD = r"[a-z]{2,24}"
# A scheme'd URL, defanged schemes included. Stops at whitespace, quotes and
# angle brackets; trailing sentence punctuation is handed back.
_SCHEME_URL_RE = re.compile(
    r"\b(?:https?|hxxps?|h\[?xx\]?ps?|ftp|fxp)\[?:\]?//[^\s\"'<>]+", re.IGNORECASE,
)
# An onion address with or without a scheme (v2 16 chars, v3 56).
_ONION_RE = re.compile(r"\b[a-z2-7]{16,56}(?:\.|\[\.\]|\(\.\))onion\b[^\s\"'<>]*", re.IGNORECASE)
# A bare host WITH a path ("files.example.org/x/y.zip") — the path is the
# part a claim must not keep. A bare host with no path is left as written.
_BARE_PATH_URL_RE = re.compile(
    rf"\b(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?(?:\.|\[\.\]))+{_TLD}/[^\s\"'<>]+",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}"

# The obfuscated forms: "name [at] example [dot] org", "name(at)example.org".
_EMAIL_RE = re.compile(
    r"\b[\w.+-]+\s*(?:\[at\]|\(at\))\s*[\w-]+(?:\s*(?:\.|\[dot\]|\(dot\))\s*[\w-]+)+",
    re.IGNORECASE,
)
# The plain form (matched first).
_EMAIL_PLAIN_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b")

_SECRET_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:password|passwd|pwd|pass|passphrase|api[_ -]?key|apikey|secret|token|"
               r"access[_ -]?key|private[_ -]?key|auth|credentials?|login)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test)?[-_]?[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
)

# Government-id-like numbers: the reliable shapes, plus any number LABELLED
# as an id. A bare long number is not guessed at.
_GOV_ID_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),           # US SSN
    re.compile(r"\b[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),  # UK NI
    re.compile(r"\b(?:SSN|SIN|NIN|NINO|passport(?:\s+(?:no|number|#))?|national\s+id(?:\s+number)?|"
               r"id\s+(?:no|number)|tax\s+id|TIN|DNI|NIE|NIF|CPF|PESEL|Aadhaar|personnummer)"
               r"\s*[:#.]?\s*(?:is\s+)?[A-Z0-9][A-Z0-9 .-]{4,20}[A-Z0-9]\b", re.IGNORECASE),
)

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")
_CARD_CANDIDATE_RE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")

_PHONE_RES: tuple[re.Pattern[str], ...] = (
    # International: +CC or 00CC then >= 7 more digits with separators.
    re.compile(r"(?<![\w+])(?:\+|00)[1-9]\d{0,2}[\s.-]?(?:\(\d{1,4}\)[\s.-]?)?\d{1,4}(?:[\s.-]?\d{2,4}){2,4}\b"),
    # North-American: (555) 010-0199 / 555-010-0199 / 555.010.0199.
    re.compile(r"(?<![\w-])\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    # Labelled: "tel: 0123 456 789", "phone 0123456789".
    re.compile(r"\b(?:tel|phone|mobile|cell|fax|whatsapp)\s*[:.]?\s*\+?\d[\d\s().-]{6,18}\d", re.IGNORECASE),
)

_STREET_SUFFIX = (r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Lane|Ln\.?|"
                  r"Drive|Dr\.?|Court|Ct\.?|Place|Pl\.?|Terrace|Crescent|Highway|Hwy\.?|"
                  r"Parkway|Pkwy\.?|Strasse|Straße|Rue|Calle|Via)")
_STREET_RES: tuple[re.Pattern[str], ...] = (
    # "12 Example Street", "4500 North Example Ave."
    re.compile(rf"\b\d{{1,5}}[A-Za-z]?\s+(?:[A-Z][A-Za-z'-]+\s+){{1,4}}{_STREET_SUFFIX}(?![A-Za-z])"),
    # "Rue de l'Exemple 12" / "Examplestrasse 5" / "Calle Ejemplo 7"
    re.compile(r"\b(?:Rue|Calle|Via|Avenida|Rua)\s+(?:[A-Za-zÀ-ÿ'-]+\s+){1,4}\d{1,5}\b"),
    re.compile(r"\b[A-Z][a-zäöü]+(?:strasse|straße|weg|gasse|platz)\s+\d{1,5}[a-z]?\b"),
)

#: A file name (with a known-shaped extension) or a path.
_FILE_EXTS = (
    r"(?:pdf|docx?|xlsx?|csv|txt|zip|rar|7z|tar|gz|tgz|sql|bak|db|mdb|accdb|pst|ost|eml|msg|"
    r"jpe?g|png|tiff?|psd|dwg|pptx?|json|xml|log|conf|cfg|key|pem|pfx|p12|vmdk|vhdx?|iso|"
    r"py|js|java|cs|cpp|h|php|rb|go|rs|sh|ps1|bat|exe|dll|bin|dat|ldf|mdf|ndf|kdbx)\b"
)
_FILE_TOKEN_RE = re.compile(
    r"(?:[A-Za-z]:\\|/|\\\\|\./)?(?:[\w .()&-]+[\\/])*[\w .()&-]*\w\." + _FILE_EXTS,
    re.IGNORECASE,
)
#: One file name inside running text (no spaces, no path) and a run of them
#: joined by commas / semicolons / "and".
_INLINE_FILE_RE = re.compile(r"[\w()&.-]*\w\." + _FILE_EXTS, re.IGNORECASE)
_INLINE_RUN_RE = re.compile(
    rf"(?:{_INLINE_FILE_RE.pattern})(?:(?:\s*[,;]\s*(?:and\s+)?|\s+and\s+)(?:{_INLINE_FILE_RE.pattern}))+",
    re.IGNORECASE,
)
_PATH_LINE_RE = re.compile(r"^\s*(?:[-*•>|]\s*)?(?:[A-Za-z]:\\|/|\\\\|\./)\S+")
_RECORD_LINE_RE = re.compile(r"^[^\n]*?(?:[,;|\t][^\n,;|\t]*){3,}$")
_COMBO_LINE_RE = re.compile(r"^\s*\S+[:;|]\S+\s*$")
_LIST_SPLIT_RE = re.compile(r"\s*[,;]\s*")
_MIN_RUN = 3

_WS_RUN_RE = re.compile(r"[ \t]{2,}")


@dataclass
class HygieneReport:
    """What hygiene did to ONE row: kinds and counts, never values."""

    counts: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, n: int = 1) -> None:
        if n > 0:
            self.counts[kind] = self.counts.get(kind, 0) + n

    def merge(self, other: "HygieneReport") -> None:
        for k, v in other.counts.items():
            self.add(k, v)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _undefang(text: str) -> str:
    out = re.sub(r"\[\.\]|\(\.\)|\{\.\}", ".", text)
    out = re.sub(r"\[:\]", ":", out)
    out = re.sub(r"^h(?:\[?xx\]?)p", "http", out, flags=re.IGNORECASE)
    out = re.sub(r"^fxp", "ftp", out, flags=re.IGNORECASE)
    return out


def _host_of(raw: str) -> str:
    url = _undefang(raw)
    if "://" not in url:
        url = "http://" + url
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host.rstrip(".")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _split_trailing(match_text: str) -> tuple[str, str]:
    core = match_text
    tail = ""
    while core and core[-1] in _TRAILING:
        tail = core[-1] + tail
        core = core[:-1]
    return core, tail


def _sub_count(pattern: re.Pattern[str], repl: Any, text: str) -> tuple[str, int]:
    return pattern.subn(repl, text)


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------


def _reduce_urls(text: str, report: HygieneReport) -> str:
    def _onion(_m: re.Match[str]) -> str:
        report.add("onion_link")
        return "[onion link]"

    def _url(m: re.Match[str]) -> str:
        core, tail = _split_trailing(m.group(0))
        host = _host_of(core)
        if host.endswith(".onion"):
            report.add("onion_link")
            return "[onion link]" + tail
        report.add("url_to_host")
        return (f"[link: {host}]" if host else "[link]") + tail

    text = _SCHEME_URL_RE.sub(_url, text)
    text = _ONION_RE.sub(_onion, text)
    return _BARE_PATH_URL_RE.sub(_url, text)


def _cut_listings(text: str, report: HygieneReport) -> str:
    """Runs of >= 3 file lines / record lines become one bounded description."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and _is_file_line(lines[j]):
            j += 1
        if j - i >= _MIN_RUN:
            report.add("files_cut", j - i)
            out.append(f"[lists {j - i} files]")
            i = j
            continue
        j = i
        while j < len(lines) and (_RECORD_LINE_RE.match(lines[j]) or _COMBO_LINE_RE.match(lines[j])) \
                and lines[j].strip():
            j += 1
        if j - i >= _MIN_RUN:
            report.add("sample_rows_cut", j - i)
            out.append(f"[sample data: {j - i} rows withheld]")
            i = j
            continue
        out.append(_cut_inline_file_list(lines[i], report))
        i += 1
    return "\n".join(out)


def _is_file_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _PATH_LINE_RE.match(s):
        return True
    m = _FILE_TOKEN_RE.search(s)
    # A line that is (almost) only a file name, optionally with a size column.
    return bool(m) and len(_FILE_TOKEN_RE.sub("", s).strip(" -*•|\t0123456789.,KMGTBkmgtb()")) <= 3


def _cut_inline_file_list(line: str, report: HygieneReport) -> str:
    """"files: a.pdf, b.xlsx and c.sql" -> "files: [lists 3 files]"."""
    if len(_INLINE_FILE_RE.findall(line)) < _MIN_RUN:
        return line

    def _repl(m: re.Match[str]) -> str:
        n = len(_INLINE_FILE_RE.findall(m.group(0)))
        if n < _MIN_RUN:
            return m.group(0)
        report.add("files_cut", n)
        return f"[lists {n} files]"

    return _INLINE_RUN_RE.sub(_repl, line)


def _redact_cards(text: str, report: HygieneReport) -> str:
    def _repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            report.add("payment_card")
            return "[payment card number]"
        return m.group(0)

    return _CARD_CANDIDATE_RE.sub(_repl, text)


def hygiene_text(text: str, report: HygieneReport | None = None) -> tuple[str, HygieneReport]:
    """``text`` with the claim-text rules applied, and what was done.

    Order matters: secrets first (a credential can sit inside a URL or look
    like an email), then URLs (an email-shaped ``user@host`` inside a URL is
    the URL's), then listings (a file list is cut whole before its names
    could be read as anything else), then the personal-data kinds."""
    rep = report if report is not None else HygieneReport()
    if not text:
        return text, rep
    out = text
    for pat in _SECRET_RES:
        out, n = _sub_count(pat, "[credential]", out)
        rep.add("secret", n)
    out = _reduce_urls(out, rep)
    out = _cut_listings(out, rep)
    out, n = _sub_count(_EMAIL_PLAIN_RE, "[email]", out)
    rep.add("email", n)
    out, n = _sub_count(_EMAIL_RE, "[email]", out)
    rep.add("email", n)
    out, n = _sub_count(_IBAN_RE, "[bank account number]", out)
    rep.add("iban", n)
    out = _redact_cards(out, rep)
    for pat in _GOV_ID_RES:
        out, n = _sub_count(pat, "[id number]", out)
        rep.add("gov_id", n)
    for pat in _PHONE_RES:
        out, n = _sub_count(pat, "[phone number]", out)
        rep.add("phone", n)
    for pat in _STREET_RES:
        out, n = _sub_count(pat, "[street address]", out)
        rep.add("street_address", n)
    if out != text:
        out = _WS_RUN_RE.sub(" ", out)
    return out, rep


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


def own_hosts_of(*urls: Any) -> frozenset[str]:
    """The hosts a claim row's own address may sit on (the feed's host, plus
    ``t.me`` for a Telegram channel) — ``www.`` folded."""
    hosts: set[str] = set()
    for u in urls:
        if isinstance(u, str) and u.strip():
            h = _host_of(u.strip())
            if h:
                hosts.add(h)
                hosts.add(h[4:] if h.startswith("www.") else "www." + h)
    return frozenset(hosts)


def _item_key(value: str) -> str:
    return "claim-item:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _structural(value: str, own_hosts: frozenset[str], report: HygieneReport) -> str:
    """An ADDRESS field: kept on the source's own host, else a non-resolvable
    key (dedupe still keys on it; nothing to follow)."""
    v = value.strip()
    if not re.match(r"^(?:[a-z][a-z0-9+.-]*:)?//", v, re.IGNORECASE) and ".onion" not in v.lower():
        # Not an address (a feed's opaque guid): treated as text.
        return hygiene_text(value, report)[0]
    host = _host_of(v)
    if host and host in own_hosts and not host.endswith(".onion"):
        return value
    report.add("onion_link" if host.endswith(".onion") or ".onion" in v.lower() else "item_link_replaced")
    return _item_key(v)


def _walk(value: Any, key: str | None, own_hosts: frozenset[str], report: HygieneReport) -> Any:
    if key in _UNTOUCHED_KEYS or key is not None and key.startswith("_"):
        return value
    if isinstance(value, str):
        if key in _STRUCTURAL_URL_KEYS:
            return _structural(value, own_hosts, report)
        return hygiene_text(value, report)[0]
    if isinstance(value, Mapping):
        return {k: _walk(v, str(k), own_hosts, report) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, key, own_hosts, report) for v in value]
    return value


def apply_claim_hygiene(signal: Any, *, own_hosts: Iterable[str] = ()) -> HygieneReport:
    """Rewrite ``signal`` (a ``Signal``) in place under the claim-text rules
    and return what was done. ``canonical_url`` follows the address rule."""
    hosts = frozenset(own_hosts)
    report = HygieneReport()
    payload = getattr(signal, "payload", None)
    if isinstance(payload, dict):
        for k in list(payload):
            payload[k] = _walk(payload[k], str(k), hosts, report)
    canonical = getattr(signal, "canonical_url", None)
    if isinstance(canonical, str) and canonical:
        new = _structural(canonical, hosts, report)
        if new != canonical:
            signal.canonical_url = new
    report.add("rows")
    return report


#: The host a Telegram row's own address sits on.
_TELEGRAM_HOSTS = ("https://t.me/",)


def prepare_claim_row(descriptor: Any, signal: Any) -> HygieneReport | None:
    """The ingest step for ONE raw row, before enrichment (source actor).

    ``None`` — and the row untouched — unless ``descriptor`` makes this row a
    claim (a claim source, or a channel it marks ``claim``). A claim row is
    stamped (``payload.claim``), stripped of media references and rewritten
    under the hygiene rules; the report says what was done."""
    from .claim_support import CLAIM_PAYLOAD_KEY, claim_stamp_for, strip_claim_media  # noqa: PLC0415

    payload = getattr(signal, "payload", None)
    stamp = claim_stamp_for(descriptor, payload if isinstance(payload, Mapping) else None)
    if stamp is None:
        return None
    config = getattr(descriptor, "config", None) or {}
    url = config.get("url") if isinstance(config, Mapping) else None
    if isinstance(url, Mapping):
        url = url.get("raw")
    kind = str(getattr(getattr(descriptor, "identity", None), "kind", "") or "")
    hosts = own_hosts_of(url, payload.get("source_url") if isinstance(payload, Mapping) else None,
                         *(_TELEGRAM_HOSTS if kind == "telegram_channel" else ()))
    strip_claim_media(signal)
    report = apply_claim_hygiene(signal, own_hosts=hosts)
    if isinstance(signal.payload, dict):
        signal.payload[CLAIM_PAYLOAD_KEY] = stamp
    return report


def merge_counts(into: dict[str, int], report: HygieneReport) -> None:
    """Fold one row's report into a poll's running tally (in place)."""
    for k, v in report.counts.items():
        into[k] = into.get(k, 0) + v


#: The prefix a poll's per-row stats dict (the source actor's ``dedup_stats``)
#: carries the hygiene counts under, so no new state rides the actor.
_STATS_PREFIX = "claim_hygiene."


def tally(stats: dict[str, int] | None, report: HygieneReport | None) -> None:
    """Add one claim row's report to a poll's stats dict (no-op on ``None``)."""
    if stats is None or report is None:
        return
    for k, v in report.counts.items():
        stats[_STATS_PREFIX + k] = stats.get(_STATS_PREFIX + k, 0) + v


def poll_claim_hygiene(stats: Mapping[str, int] | None) -> dict[str, int] | None:
    """The poll record's ``claim_hygiene`` value: ``{kind: count}`` (with
    ``rows``), or ``None`` when the poll wrote no claim row."""
    if not stats:
        return None
    out = {k[len(_STATS_PREFIX):]: int(v) for k, v in stats.items() if k.startswith(_STATS_PREFIX)}
    return out or None
