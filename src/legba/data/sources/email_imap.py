# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Email (IMAP) source handler — kind ``email_imap`` (W5 SRC-8, DEC-2).

Some publishers deliver only by mailing list: agency bulletin services
(GovDelivery and its kin), e-alert lists with no feed, lists whose RSS was
retired. This kind reads one folder of one mailbox over IMAP and turns each
message from an allowed sender into one :class:`Signal`. The mailbox and the
list signups belong to the operator; the platform ships the reader.

Shape (L-102 source-kind contract, mirroring the RSS handler):

  * ``pull(ctx, since)``: resolve the mailbox password from the vault
    (``password_ref`` — a dotted credential id, never a literal), log in over
    TLS (implicit ``ssl`` on 993, or ``starttls``), ``SELECT`` the folder
    READ-ONLY, ``UID SEARCH SINCE <now - since_days> [UNSEEN] UID <last+1>:*``,
    fetch up to ``max_messages_per_poll`` messages with ``BODY.PEEK[]`` (the
    mailbox is never modified — nothing is marked seen, moved or deleted) and
    yield one Signal per message whose From address passes ``allow_from``.
  * Body: the first ``text/plain`` part, else the first ``text/html`` part
    reduced to text (tags stripped, entities unescaped, script/style
    dropped). Attachments are ignored and counted.
  * Signal: ``title`` = Subject, ``text`` = body, ``published_at`` = the Date
    header, ``publisher`` = the From domain, ``canonical_url`` = the first
    http(s) link in the body whose host sits under the sender's domain, else
    the RFC 2392 ``mid:`` URI of the Message-ID. ``content_hash`` is keyed on
    the Message-ID, so a message seen twice collapses in the existing
    content-hash write (S-4) rather than landing a second row.
  * Subject gate: ``subject_include`` / ``subject_exclude`` build the ONE
    keyword matcher (:class:`legba.data.filters.keyword_gate.KeywordGate`,
    fields = ``title``) and expose it as ``handler.keyword_gate``; SourceCore
    applies it pre-baseline and drains its drop counts into the source's
    ``cursor`` row exactly as for the RSS sugar (W3 SRC-3).
  * Cursor: the last UID per folder, with the folder's UIDVALIDITY, in the
    source's own state row ``email_imap_cursor`` (``actor_filter_state``,
    beside the actor's ``cursor`` row). It advances only past messages the
    consumer took or the handler decided (rejected sender, unparseable), so a
    capped or interrupted poll resumes at the first undecided message. A
    changed UIDVALIDITY (the folder was re-created) restarts the walk; the
    Message-ID hash absorbs the re-read. ``since`` is not used: a mailbox has
    no publication order of its own and the Date header is sender-controlled.

FAIL-LOUD rule (no-stubs, SEAMS #63): with no mailbox credential — no
secrets resolver wired, or ``password_ref`` not resolvable, or resolving to
an empty value — the kind REFUSES with reason ``email_mailbox_not_configured``
(:class:`EmailMailboxNotConfigured`): at activation (SourceCore runs the
handler's ``activation_check``), at ``on_configure`` / ``on_activate``, and at
``pull``; ``health_check`` reports ``unhealthy`` with the same reason. It
never logs in with a fabricated credential and never presents an empty poll.

Failure semantics (L-102 §7), mirroring the RSS handler: a login refusal or a
missing folder is ``unhealthy``; a network error, timeout or server abort is
``degraded``; both yield nothing and leave the cursor where it was.

Dependencies: the stdlib (``imaplib``, ``email``, ``html.parser``, ``ssl``)
plus the in-tree egress guard (``_egress.assert_public_host`` — the same
SSRF check every HTTP fetcher passes; ``LEGBA_EGRESS_ALLOW_HOSTS`` admits a
private mail host) and the keyword matcher. No new dependency.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import hashlib
import html as _html
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, AsyncIterator, Callable, ClassVar, Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..filters.keyword_gate import KeywordGate, KeywordGateConfig
from ._contract import Signal, SourceContext, SourceHealth
from ._egress import EgressBlockedError, assert_public_host


logger = logging.getLogger(__name__)


#: The seam reason every refusal carries (SEAMS #63).
EMAIL_MAILBOX_NOT_CONFIGURED = "email_mailbox_not_configured"

_CURSOR_KEY = "email_imap_cursor"
_HEALTH_KEY = "email_imap_health"

#: IMAP date strings use English month abbreviations regardless of locale.
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

#: A dotted vault credential id (two or more segments) — the shape every
#: keyed descriptor uses (``source.acled.password``). Refusing anything else
#: keeps a pasted password out of a descriptor body.
_VAULT_REF_RE = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_DOMAIN_RE = re.compile(r"^[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
_FOLDER_RE = re.compile(r"^[\x20-\x7e]+$")
_URL_RE = re.compile(r"https?://[^\s<>\"'\[\]{}|\\^`]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?)("
_SUMMARY_CHARS = 500
_NEWEST_ENTRY_MAX_FUTURE_SKEW = timedelta(hours=26)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class EmailConfig(BaseModel):
    """Pydantic config schema for :class:`EmailImapSourceHandler`."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(..., min_length=1, max_length=253)
    port: int = Field(default=993, ge=1, le=65535)
    #: ``ssl`` = implicit TLS (IMAPS, 993); ``starttls`` = upgrade on 143.
    #: There is no cleartext mode: the login carries the mailbox password.
    security: Literal["ssl", "starttls"] = "ssl"
    username: str = Field(..., min_length=1, max_length=320)
    #: Vault credential id of the mailbox password — NEVER the password.
    password_ref: str = Field(..., min_length=3, max_length=256)
    folder: str = Field(default="INBOX", min_length=1, max_length=256)
    #: The IMAP ``SINCE`` window, in days back from now (internal date).
    since_days: int = Field(default=7, ge=1, le=365)
    #: Add ``UNSEEN`` to the search. The handler itself never sets \\Seen.
    unseen_only: bool = True
    #: Senders admitted: a full address (``bulletins@example.gov``) matches
    #: exactly; a bare domain (``example.gov``) matches that domain and its
    #: subdomains. Required — a mailbox open to any sender would let anyone
    #: who learns the address write signals.
    allow_from: list[str] = Field(..., min_length=1, max_length=64)
    #: Subject gate — the shared keyword matcher over ``title`` (= Subject).
    subject_include: list[str] = Field(default_factory=list)
    subject_exclude: list[str] = Field(default_factory=list)
    max_messages_per_poll: int = Field(default=50, ge=1, le=500)
    timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("host")
    @classmethod
    def _validate_host(cls, v: str) -> str:
        v = v.strip()
        if not _HOST_RE.match(v):
            raise ValueError(
                "host must be a bare hostname or IP (no scheme, port or path)"
            )
        return v

    @field_validator("password_ref")
    @classmethod
    def _validate_password_ref(cls, v: str) -> str:
        if not _VAULT_REF_RE.match(v):
            raise ValueError(
                "password_ref must be a dotted vault credential id "
                "(e.g. source.email.<list>.password), never the password"
            )
        return v

    @field_validator("folder")
    @classmethod
    def _validate_folder(cls, v: str) -> str:
        if not _FOLDER_RE.match(v):
            raise ValueError("folder must be printable ASCII")
        return v

    @field_validator("allow_from")
    @classmethod
    def _validate_allow_from(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            entry = str(raw).strip().lower().lstrip("@")
            local, at, domain = entry.rpartition("@")
            if at and not local:
                raise ValueError(f"allow_from entry {raw!r} has an empty local part")
            if not _DOMAIN_RE.match(domain):
                raise ValueError(
                    f"allow_from entry {raw!r} is not an address or a domain"
                )
            if entry not in out:
                out.append(entry)
        return out


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmailMailboxNotConfigured(RuntimeError):
    """No usable mailbox credential (SEAMS #63). A typed refusal, not a stub:
    the source refuses activation and every pull rather than polling empty."""

    reason: ClassVar[str] = EMAIL_MAILBOX_NOT_CONFIGURED

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"{EMAIL_MAILBOX_NOT_CONFIGURED}: {detail} — the mailbox is the "
            "operator's to configure (store the password in the vault under "
            "password_ref); refusing (SEAMS #63)"
        )


class _ImapRefused(Exception):
    """A non-OK tagged response on SELECT / SEARCH / FETCH."""


# ---------------------------------------------------------------------------
# HTML -> text (and link harvest)
# ---------------------------------------------------------------------------


_DROP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template"})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5",
    "h6", "table", "section", "article", "blockquote", "ul", "ol", "hr", "pre",
})


class _HtmlText(HTMLParser):
    """Readable text out of an HTML mail body, plus its ``href`` links in
    document order. Script / style / head content is dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.out.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("http://", "https://")):
                self.links.append(href.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)


def html_to_text(raw: str) -> tuple[str, list[str]]:
    """``(text, links)`` for an HTML body. A malformed body yields what parsed."""
    parser = _HtmlText()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 — a broken body still yields its prefix
        pass
    return _squash(_html.unescape("".join(parser.out))), parser.links


def _squash(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _text_links(text: str) -> list[str]:
    return [m.group(0).rstrip(_URL_TRAILING) for m in _URL_RE.finditer(text or "")]


# ---------------------------------------------------------------------------
# One message -> one Signal (pure)
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    """The outcome for one fetched message. ``signal`` is None when the
    message was rejected (``rejected`` names why)."""

    signal: Signal | None
    rejected: str | None = None
    attachments_ignored: int = 0
    published_at: datetime | None = None


def sender_allowed(address: str, allow_from: list[str]) -> bool:
    """True iff ``address`` matches an ``allow_from`` entry (see EmailConfig)."""
    address = (address or "").strip().lower()
    if "@" not in address:
        return False
    domain = address.rpartition("@")[2]
    for entry in allow_from:
        if "@" in entry:
            if address == entry:
                return True
        elif domain == entry or domain.endswith("." + entry):
            return True
    return False


def _host_under(host: str, domain: str) -> bool:
    """A link host sits under the sender's domain: equal, a subdomain of it,
    or its parent (a bulletin from ``news.agency.gov`` linking ``agency.gov``)."""
    host = host.lower().rstrip(".")
    if not host or not domain or "." not in host:
        return False
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host)


def pick_url(links: list[str], sender_domain: str, message_id: str) -> str | None:
    """The URL policy: the first http(s) link under the sender's domain, else
    the RFC 2392 ``mid:`` URI of the Message-ID, else None."""
    for link in links:
        try:
            host = urlsplit(link).hostname or ""
        except ValueError:
            continue
        if _host_under(host, sender_domain):
            return link
    if message_id:
        return "mid:" + quote(message_id, safe="@!$&'()*+,;=:-._~")
    return None


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 — a malformed header reads as absent
        return ""
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        if isinstance(content, str):
            return content
    except Exception:  # noqa: BLE001 — unknown charset etc.: decode by hand
        pass
    payload = part.get_payload(decode=True)
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_message(
    raw: bytes,
    *,
    config: EmailConfig,
    source_id: str,
    uid: int,
    uidvalidity: int | None,
) -> ParsedMessage:
    """Map one RFC 5322 message to a :class:`Signal` (or a rejection)."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:  # noqa: BLE001
        return ParsedMessage(signal=None, rejected="unparseable")

    display, address = parseaddr(_header(msg, "From"))
    address = address.strip().lower()
    if not sender_allowed(address, config.allow_from):
        return ParsedMessage(signal=None, rejected="sender_not_allowed")
    sender_domain = address.rpartition("@")[2]

    subject = _header(msg, "Subject")
    message_id = _header(msg, "Message-ID").strip("<> ")
    published = _parse_date(_header(msg, "Date"))

    plain: str | None = None
    html_body: str | None = None
    attachments = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment" or part.get_filename():
            attachments += 1
            continue
        if ctype == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ctype == "text/html" and html_body is None:
            html_body = _part_text(part)
        elif not ctype.startswith("text/"):
            attachments += 1  # inline image / calendar / binary leaf

    links: list[str] = []
    body_source = "none"
    text = ""
    if plain is not None and plain.strip():
        text = _squash(plain)
        body_source = "text/plain"
        links.extend(_text_links(plain))
    if html_body is not None:
        html_text, html_links = html_to_text(html_body)
        if not text and html_text:
            text = html_text
            body_source = "text/html"
        links.extend(html_links)

    if not subject and not text:
        return ParsedMessage(signal=None, rejected="empty", attachments_ignored=attachments)

    url = pick_url(links, sender_domain, message_id)
    if message_id:
        basis = "email_imap\x1f" + message_id
    else:
        basis = "\x1f".join((
            "email_imap", address, published.isoformat() if published else "",
            subject, text,
        ))
    payload: dict[str, Any] = {
        "external_id": message_id or f"uid:{uidvalidity}:{uid}",
        "message_id": message_id or None,
        "published_at": published.isoformat() if published else None,
        "title": subject,
        "text": text,
        "summary": text[:_SUMMARY_CHARS],
        "link": url or "",
        "author": display.strip() or address,
        "from_address": address,
        "publisher": sender_domain,
        "tags": [],
        "body_source": body_source,
        "attachments_ignored": attachments,
    }
    if published is not None:
        payload["_published_at_dt"] = published
    signal = Signal(
        source_id=source_id,
        modality="text",
        payload=payload,
        content_hash=hashlib.sha256(basis.encode("utf-8")).hexdigest(),
        canonical_url=url,
        language_hint=_header(msg, "Content-Language")[:16] or None,
        raw_provenance={
            "fetch_kind": "email_imap",
            "mailbox_host": config.host,
            "folder": config.folder,
            "uid": uid,
            "uidvalidity": uidvalidity,
            "message_id": message_id or None,
        },
    )
    return ParsedMessage(
        signal=signal, attachments_ignored=attachments, published_at=published,
    )


# ---------------------------------------------------------------------------
# IMAP session (sync; run in a worker thread)
# ---------------------------------------------------------------------------


@dataclass
class _Batch:
    uidvalidity: int | None
    start_uid: int
    cursor_reset: bool
    matched: int
    messages: list[tuple[int, bytes | None]] = field(default_factory=list)


def _open_imap(config: EmailConfig) -> Any:
    """The production connection: egress-guarded, TLS-verified, timed out."""
    assert_public_host(config.host, config.port)
    tls = ssl.create_default_context()
    if config.security == "ssl":
        return imaplib.IMAP4_SSL(
            config.host, config.port, ssl_context=tls, timeout=config.timeout_seconds,
        )
    conn = imaplib.IMAP4(config.host, config.port, timeout=config.timeout_seconds)
    conn.starttls(ssl_context=tls)
    return conn


def _imap_date(when: datetime) -> str:
    return f"{when.day:02d}-{_IMAP_MONTHS[when.month - 1]}-{when.year:04d}"


def _quote_mailbox(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.\-/]+", name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ok(typ: Any, what: str) -> None:
    if typ != "OK":
        raise _ImapRefused(f"{what} -> {typ}")


def _uidvalidity(conn: Any) -> int | None:
    try:
        _code, data = conn.response("UIDVALIDITY")
    except Exception:  # noqa: BLE001
        return None
    for item in data or []:
        if item is None:
            continue
        text = item.decode("ascii", "replace") if isinstance(item, bytes) else str(item)
        m = re.search(r"\d+", text)
        if m:
            return int(m.group(0))
    return None


def _fetched_bytes(data: Any) -> bytes | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def _login_select(conn: Any, config: EmailConfig, password: str) -> None:
    conn.login(config.username, password)
    typ, _ = conn.select(_quote_mailbox(config.folder), readonly=True)
    _ok(typ, f"SELECT {config.folder!r}")


def _fetch_batch(
    conn: Any,
    config: EmailConfig,
    password: str,
    *,
    last_uid: int,
    prior_validity: int | None,
    now: datetime,
) -> _Batch:
    try:
        _login_select(conn, config, password)
        validity = _uidvalidity(conn)
        reset = (
            prior_validity is not None and validity is not None
            and validity != prior_validity
        )
        start = 0 if reset else last_uid
        criteria = ["SINCE", _imap_date(now - timedelta(days=config.since_days))]
        if config.unseen_only:
            criteria.append("UNSEEN")
        criteria += ["UID", f"{start + 1}:*"]
        typ, data = conn.uid("SEARCH", *criteria)
        _ok(typ, "UID SEARCH")
        found: set[int] = set()
        for chunk in data or []:
            if isinstance(chunk, (bytes, bytearray)):
                found.update(int(t) for t in bytes(chunk).split() if t.isdigit())
        # ``N:*`` always matches the highest UID even when it is below N.
        uids = sorted(u for u in found if u > start)
        batch = _Batch(
            uidvalidity=validity, start_uid=start, cursor_reset=reset, matched=len(uids),
        )
        for uid in uids[: config.max_messages_per_poll]:
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            _ok(typ, f"UID FETCH {uid}")
            batch.messages.append((uid, _fetched_bytes(data)))
        return batch
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 — the session is done either way
            pass


def _probe(conn: Any, config: EmailConfig, password: str) -> int | None:
    try:
        _login_select(conn, config, password)
        return _uidvalidity(conn)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _classify_imap_error(exc: BaseException) -> str:
    """``unhealthy`` for a refusal that will not self-heal (bad login, missing
    folder, egress block); ``degraded`` for a transient (network, abort)."""
    if isinstance(exc, imaplib.IMAP4.abort):
        return "degraded"
    if isinstance(exc, (
        imaplib.IMAP4.error, _ImapRefused, EgressBlockedError,
        ssl.SSLCertVerificationError,
    )):
        return "unhealthy"
    return "degraded"


_IMAP_ERRORS: tuple[type[BaseException], ...] = (
    imaplib.IMAP4.error, _ImapRefused, EgressBlockedError, OSError, EOFError,
)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class EmailImapSourceHandler:
    """Source handler for one IMAP mailbox folder (kind ``email_imap``)."""

    kind: ClassVar[str] = "email_imap"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.email_imap/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = EmailConfig
    handler_version: ClassVar[str] = "0.1.0"
    health_state_key: ClassVar[str] = _HEALTH_KEY

    def __init__(
        self,
        config: EmailConfig,
        *,
        secret_resolver: Any = None,
        imap_factory: Callable[[EmailConfig], Any] | None = None,
    ) -> None:
        """``secret_resolver`` is the factory-threaded ``secrets_resolve``
        (``async (vault_id) -> bytes``); the context's resolver wins at call
        time. ``imap_factory`` builds one connected ``imaplib.IMAP4``-shaped
        session (tests pass an in-process double)."""
        self._config = config
        self._secret_resolver = secret_resolver
        self._imap_factory = imap_factory or _open_imap
        self.keyword_gate: KeywordGate | None = None
        if config.subject_include or config.subject_exclude:
            self.keyword_gate = KeywordGate(KeywordGateConfig(
                include=config.subject_include,
                exclude=config.subject_exclude,
                fields=["title"],
            ))

    # ------------------------------------------------------- the credential

    async def _resolve_password(self, ctx: SourceContext) -> str:
        ref = self._config.password_ref
        resolver = getattr(ctx, "secrets_resolve", None) or self._secret_resolver
        if resolver is None:
            raise EmailMailboxNotConfigured(
                f"no secrets resolver is wired for password_ref={ref!r}"
            )
        try:
            raw = await resolver(ref)
        except Exception as exc:  # noqa: BLE001 — any miss is "not configured"
            # The exception TYPE only: a resolver's message is not ours to echo.
            raise EmailMailboxNotConfigured(
                f"password_ref={ref!r} did not resolve ({type(exc).__name__})"
            ) from exc
        if isinstance(raw, (bytes, bytearray)):
            secret = bytes(raw).decode("utf-8", "replace")
        else:
            secret = "" if raw is None else str(raw)
        if not secret:
            raise EmailMailboxNotConfigured(
                f"password_ref={ref!r} resolved to an empty credential"
            )
        return secret

    async def activation_check(self, ctx: SourceContext) -> None:
        """Refuse activation unless the mailbox credential resolves (SEAMS
        #63). Called by SourceCore before the actor goes ACTIVE. No network."""
        await self._resolve_password(ctx)

    # ------------------------------------------------------------------ pull

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Yield one Signal per new message from an allowed sender."""
        try:
            password = await self._resolve_password(ctx)
        except EmailMailboxNotConfigured as exc:
            await self._record_health(
                ctx, state="unhealthy", last_error=str(exc),
                detail={"reason": EMAIL_MAILBOX_NOT_CONFIGURED},
            )
            raise

        cursor = await self._load_cursor(ctx)
        now = ctx.utcnow()
        try:
            conn = await asyncio.to_thread(self._imap_factory, self._config)
            batch = await asyncio.to_thread(
                _fetch_batch, conn, self._config, password,
                last_uid=cursor["last_uid"], prior_validity=cursor["uidvalidity"],
                now=now,
            )
        except _IMAP_ERRORS as exc:
            ctx.logger.warning(
                "email_imap.fetch.failed host=%s folder=%s err=%s: %s",
                self._config.host, self._config.folder, type(exc).__name__, exc,
            )
            await self._record_health(
                ctx, state=_classify_imap_error(exc),
                last_error=f"imap {type(exc).__name__}: {exc}",
                detail={"host": self._config.host, "folder": self._config.folder},
            )
            return

        decided = batch.start_uid
        # -1 forces a write when the stored UIDVALIDITY is absent or stale, so
        # the folder's validity is recorded even on a poll that yields nothing.
        persisted = (
            cursor["last_uid"]
            if not batch.cursor_reset and cursor["uidvalidity"] == batch.uidvalidity
            else -1
        )
        counts = {"rejected_sender": 0, "unparseable": 0, "empty": 0, "yielded": 0}
        attachments = 0
        newest: datetime | None = None
        ceiling = datetime.now(tz=timezone.utc) + _NEWEST_ENTRY_MAX_FUTURE_SKEW
        for uid, raw in batch.messages:
            parsed = (
                parse_message(
                    raw, config=self._config, source_id=ctx.source_id,
                    uid=uid, uidvalidity=batch.uidvalidity,
                )
                if raw is not None
                else ParsedMessage(signal=None, rejected="unparseable")
            )
            attachments += parsed.attachments_ignored
            if parsed.signal is None:
                key = "rejected_sender" if parsed.rejected == "sender_not_allowed" else (
                    parsed.rejected or "unparseable"
                )
                counts[key] = counts.get(key, 0) + 1
                decided = uid
                continue
            if parsed.published_at is not None and parsed.published_at <= ceiling and (
                newest is None or parsed.published_at > newest
            ):
                newest = parsed.published_at
            # Everything before this message is decided; persist BEFORE the
            # yield so a consumer that stops here resumes at this message.
            if decided != persisted:
                await self._save_cursor(ctx, batch.uidvalidity, decided)
                persisted = decided
            counts["yielded"] += 1
            yield parsed.signal
            decided = uid

        if decided != persisted:
            await self._save_cursor(ctx, batch.uidvalidity, decided)
        await self._record_health(
            ctx, state="healthy", last_success_at=datetime.now(tz=timezone.utc),
            detail={
                "folder": self._config.folder,
                "uidvalidity": batch.uidvalidity,
                "cursor_reset": batch.cursor_reset,
                "matched": batch.matched,
                "fetched": len(batch.messages),
                "last_uid": decided,
                "messages_yielded": counts["yielded"],
                "rejected_sender": counts["rejected_sender"],
                "unparseable": counts["unparseable"],
                "empty": counts["empty"],
                "attachments_ignored": attachments,
                "newest_entry_ts": newest.isoformat() if newest else None,
            },
        )

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Log in and SELECT the folder read-only. The missing credential is
        ``unhealthy`` with the seam reason."""
        cursor = await self._load_cursor(ctx)
        last = str(cursor["last_uid"]) if cursor["last_uid"] else None
        try:
            password = await self._resolve_password(ctx)
        except EmailMailboxNotConfigured as exc:
            return SourceHealth(
                state="unhealthy", last_error=str(exc),
                detail={"reason": EMAIL_MAILBOX_NOT_CONFIGURED}, last_cursor=last,
            )
        try:
            conn = await asyncio.to_thread(self._imap_factory, self._config)
            validity = await asyncio.to_thread(_probe, conn, self._config, password)
        except _IMAP_ERRORS as exc:
            return SourceHealth(
                state=_classify_imap_error(exc),
                last_error=f"imap {type(exc).__name__}: {exc}",
                detail={"probe": "imap"}, last_cursor=last,
            )
        return SourceHealth(
            state="healthy", last_success_at=datetime.now(tz=timezone.utc),
            detail={"folder": self._config.folder, "uidvalidity": validity},
            last_cursor=last,
        )

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(self, ctx: SourceContext) -> None:
        await self.activation_check(ctx)

    async def on_activate(self, ctx: SourceContext) -> None:
        await self.activation_check(ctx)

    async def on_pause(self, ctx: SourceContext) -> None:
        return None

    async def on_resume(self, ctx: SourceContext) -> None:
        return None

    async def on_retire(self, ctx: SourceContext) -> None:
        return None

    # ------------------------------------------------------------- internals

    async def _load_cursor(self, ctx: SourceContext) -> dict[str, Any]:
        raw = await ctx.state_store.get(_CURSOR_KEY)
        folders = raw.get("folders") if isinstance(raw, dict) else None
        entry = folders.get(self._config.folder) if isinstance(folders, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        return {
            "last_uid": _non_negative_int(entry.get("last_uid")),
            "uidvalidity": _optional_int(entry.get("uidvalidity")),
        }

    async def _save_cursor(
        self, ctx: SourceContext, uidvalidity: int | None, last_uid: int,
    ) -> None:
        raw = await ctx.state_store.get(_CURSOR_KEY)
        state = dict(raw) if isinstance(raw, dict) else {}
        folders = dict(state.get("folders") or {})
        folders[self._config.folder] = {
            "uidvalidity": uidvalidity,
            "last_uid": int(last_uid),
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        state["folders"] = folders
        await ctx.state_store.set(_CURSOR_KEY, state)

    async def _record_health(
        self,
        ctx: SourceContext,
        *,
        state: str,
        last_success_at: datetime | None = None,
        last_error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "state": state,
            "last_success_at": (
                last_success_at.astimezone(timezone.utc).isoformat()
                if last_success_at is not None else None
            ),
            "last_error": last_error,
            "detail": detail or {},
        }
        try:
            await ctx.state_store.set(_HEALTH_KEY, record)
        except Exception:  # pragma: no cover
            ctx.logger.warning("email_imap.health.persist_failed", exc_info=True)


def _non_negative_int(val: Any) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _optional_int(val: Any) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


__all__ = [
    "EMAIL_MAILBOX_NOT_CONFIGURED",
    "EmailConfig",
    "EmailImapSourceHandler",
    "EmailMailboxNotConfigured",
    "ParsedMessage",
    "html_to_text",
    "parse_message",
    "pick_url",
    "sender_allowed",
]
