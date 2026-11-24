# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W5 SRC-8 — the ``email_imap`` source kind.

Driven against an in-process IMAP double that answers in imaplib's own
return shapes (``("OK", [b"1 2 3"])`` for UID SEARCH, the ``(header, bytes)``
tuple for UID FETCH, ``response("UIDVALIDITY")``), so the handler's real
session code (``_fetch_batch``: login, read-only SELECT, the SEARCH criteria,
BODY.PEEK fetches, logout) runs end to end without a network.

Covers: plain / html / multipart parsing, attachments counted, the URL policy,
``allow_from``, the subject gate through the SHARED keyword matcher (applied
by the real SourceCore poll path, counted in the cursor row), the UID cursor
(advance, resume after an interrupted consumer, UIDVALIDITY reset), dedupe on
re-poll through the real content-hash write, the SEAM (SEAMS #63) failing loud
at activation / pull / health, the egress guard, and the worked example
descriptor validating as the registrar and the factory see it.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import imaplib
import pytest
import yaml
from pydantic import ValidationError

from legba.data.filters.keyword_gate import KeywordGate
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.sources._egress import EgressBlockedError
from legba.data.sources.email_imap import (
    EMAIL_MAILBOX_NOT_CONFIGURED,
    EmailConfig,
    EmailImapSourceHandler,
    EmailMailboxNotConfigured,
    _open_imap,
    parse_message,
    pick_url,
    sender_allowed,
)
from legba.runtime import source_actor as source_actor_mod
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps, _RawConfig
from legba.runtime.source_factory import (
    _unwrap_factory_dict,
    build_source_handler,
    discover_source_kinds,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "descriptors" / "examples" / "source_example_email_list.yaml"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
SECRET_ID = "source.email.test.password"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _msg(
    *,
    subject: str = "Recent Actions",
    sender: str = "Bulletins <bulletins@lists.agency.gov>",
    message_id: str | None = "<abc123@lists.agency.gov>",
    plain: str | None = (
        "Body text. Mirror https://www.agency.gov/m/1 or read more at "
        "https://agency.gov/news/1 today."
    ),
    html: str | None = None,
    attachment: bytes | None = None,
    date: datetime | None = NOW - timedelta(hours=1),
) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "reader@example.org"
    m["Subject"] = subject
    if message_id:
        m["Message-ID"] = message_id
    if date is not None:
        m["Date"] = format_datetime(date)
    if plain is not None:
        m.set_content(plain)
        if html is not None:
            m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    if attachment is not None:
        m.add_attachment(attachment, maintype="application", subtype="pdf", filename="notice.pdf")
    return m.as_bytes()


def _cfg(**kw: Any) -> EmailConfig:
    base: dict[str, Any] = {
        "host": "imap.mail.test",
        "username": "reader@example.org",
        "password_ref": SECRET_ID,
        "allow_from": ["lists.agency.gov"],
    }
    base.update(kw)
    return EmailConfig(**base)


# ---------------------------------------------------------------------------
# The in-process IMAP double (imaplib return shapes)
# ---------------------------------------------------------------------------


class ImapDouble:
    """One mailbox folder. Records every command for assertions."""

    def __init__(self, password: str = "pw", uidvalidity: int = 7) -> None:
        self.password = password
        self.uidvalidity = uidvalidity
        self.messages: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.selected_readonly: bool | None = None

    def add(self, uid: int, raw: bytes, *, seen: bool = False,
            internal: datetime = NOW - timedelta(hours=1)) -> None:
        self.messages[uid] = {"raw": raw, "seen": seen, "internal": internal}

    # -- the factory the handler calls --------------------------------------
    def factory(self, config: EmailConfig) -> "ImapDouble":
        self.calls.append(("connect", config.host, config.port))
        return self

    # -- imaplib.IMAP4 surface ----------------------------------------------
    def login(self, user: str, password: str):
        self.calls.append(("login", user))
        if password != self.password:
            raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials'")
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: str, readonly: bool = False):
        self.calls.append(("select", mailbox, readonly))
        self.selected_readonly = readonly
        if mailbox.strip('"') != "INBOX":
            return "NO", [b"Mailbox does not exist"]
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code: str):
        if code == "UIDVALIDITY":
            return code, [str(self.uidvalidity).encode()]
        return code, [None]

    def uid(self, command: str, *args: str):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [self._search(list(args))]
        if command == "FETCH":
            uid, what = int(args[0]), args[1]
            assert what == "(BODY.PEEK[])", "the handler must never set \\Seen"
            raw = self.messages[uid]["raw"]
            return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        raise AssertionError(f"unexpected UID {command}")

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b"logging out"]

    def _search(self, args: list[str]) -> bytes:
        since: datetime | None = None
        unseen = False
        lo = 1
        i = 0
        while i < len(args):
            key = args[i].upper()
            if key == "SINCE":
                since = datetime.strptime(args[i + 1], "%d-%b-%Y").replace(tzinfo=timezone.utc)
                i += 2
            elif key == "UNSEEN":
                unseen = True
                i += 1
            elif key == "UID":
                lo = int(args[i + 1].split(":")[0])
                i += 2
            else:
                raise AssertionError(f"unexpected search key {key}")
        hits = [
            u for u, m in sorted(self.messages.items())
            if u >= lo
            and (since is None or m["internal"] >= since)
            and not (unseen and m["seen"])
        ]
        # RFC 3501: ``N:*`` always includes the highest UID, even below N.
        if self.messages and not hits:
            top = max(self.messages)
            if not (unseen and self.messages[top]["seen"]):
                hits = [top]
        return " ".join(str(u) for u in hits).encode()


def _resolver(secret: str = "pw"):
    async def _resolve(secret_id: str) -> bytes:
        if secret_id != SECRET_ID:
            raise KeyError(secret_id)
        return secret.encode()
    return _resolve


def _ctx(config: EmailConfig, store: InMemoryStateStore | None = None,
         *, secrets_resolve: Any = None) -> SourceContext:
    return SourceContext(
        target_id="source.test.email", target_version="v1",
        source_id="source.test.email", config=config,
        state_store=store if store is not None else InMemoryStateStore(),
        secrets_resolve=secrets_resolve, now_fn=lambda: NOW,
    )


async def _drain(handler, ctx, limit: int | None = None):
    out = []
    async for s in handler.pull(ctx, None):
        out.append(s)
        if limit is not None and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def test_parse_plain_body_fields_url_under_sender_domain_and_attachment_counted():
    raw = _msg(attachment=b"%PDF-1.4 fake")
    got = parse_message(raw, config=_cfg(), source_id="s", uid=5, uidvalidity=7)
    sig = got.signal
    assert sig is not None
    p = sig.payload
    assert p["title"] == "Recent Actions"
    assert p["text"].startswith("Body text.")
    assert p["publisher"] == "lists.agency.gov"
    assert p["author"] == "Bulletins"
    assert p["message_id"] == "abc123@lists.agency.gov"
    assert p["published_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert p["body_source"] == "text/plain"
    assert p["attachments_ignored"] == 1 and got.attachments_ignored == 1
    # www.agency.gov is a SIBLING of the sender's lists.agency.gov — not under
    # it; agency.gov is its PARENT — admitted. First admitted link wins.
    assert sig.canonical_url == "https://agency.gov/news/1"
    assert sig.raw_provenance["uid"] == 5 and sig.raw_provenance["fetch_kind"] == "email_imap"


def test_parse_html_only_strips_tags_scripts_entities_and_takes_href():
    html = (
        "<html><head><title>x</title><style>p{}</style></head><body>"
        "<script>evil()</script><p>Designation&nbsp;update &amp; notes</p>"
        '<a href="https://tracker.example.net/c/1">track</a>'
        '<a href="https://lists.agency.gov/item/9">read</a></body></html>'
    )
    sig = parse_message(_msg(plain=None, html=html), config=_cfg(),
                        source_id="s", uid=1, uidvalidity=1).signal
    assert sig is not None
    assert sig.payload["body_source"] == "text/html"
    assert "Designation update & notes" in sig.payload["text"]
    assert "evil" not in sig.payload["text"] and "p{}" not in sig.payload["text"]
    assert sig.canonical_url == "https://lists.agency.gov/item/9"


def test_parse_multipart_alternative_prefers_plain():
    raw = _msg(plain="Plain wins", html="<p>Html loses</p>")
    sig = parse_message(raw, config=_cfg(), source_id="s", uid=1, uidvalidity=1).signal
    assert sig.payload["text"] == "Plain wins"
    assert sig.payload["body_source"] == "text/plain"


def test_url_policy_falls_back_to_message_id_uri():
    raw = _msg(plain="Only an off-domain link https://lnks.example.net/x here")
    sig = parse_message(raw, config=_cfg(), source_id="s", uid=1, uidvalidity=1).signal
    assert sig.canonical_url == "mid:abc123@lists.agency.gov"
    assert pick_url([], "a.gov", "") is None


def test_content_hash_keys_on_message_id():
    a = parse_message(_msg(subject="v1"), config=_cfg(), source_id="s", uid=1, uidvalidity=1)
    b = parse_message(_msg(subject="v1 edited"), config=_cfg(), source_id="s", uid=9, uidvalidity=2)
    c = parse_message(_msg(message_id="<other@lists.agency.gov>"), config=_cfg(),
                      source_id="s", uid=1, uidvalidity=1)
    assert a.signal.content_hash == b.signal.content_hash
    assert a.signal.content_hash != c.signal.content_hash


def test_allow_from_domain_subdomain_and_exact_address():
    allow = ["agency.gov", "alerts@example.int"]
    assert sender_allowed("x@agency.gov", allow)
    assert sender_allowed("x@lists.agency.gov", allow)
    assert not sender_allowed("x@notagency.gov", allow)
    assert not sender_allowed("x@agency.gov.evil.test", allow)
    assert sender_allowed("alerts@example.int", allow)
    assert not sender_allowed("other@example.int", allow)
    got = parse_message(_msg(sender="Spam <a@evil.test>"), config=_cfg(),
                        source_id="s", uid=1, uidvalidity=1)
    assert got.signal is None and got.rejected == "sender_not_allowed"


def test_config_refuses_literal_password_open_mailbox_and_cleartext():
    with pytest.raises(ValidationError, match="dotted vault credential id"):
        _cfg(password_ref="hunter2")
    with pytest.raises(ValidationError):
        _cfg(allow_from=[])
    with pytest.raises(ValidationError):
        _cfg(security="plain")
    with pytest.raises(ValidationError, match="bare hostname"):
        _cfg(host="imaps://imap.mail.test")


# ---------------------------------------------------------------------------
# The poll against the IMAP double
# ---------------------------------------------------------------------------


async def test_pull_reads_readonly_peek_and_counts_rejections():
    box = ImapDouble()
    box.add(1, _msg(message_id="<1@lists.agency.gov>"))
    box.add(2, _msg(sender="x <x@evil.test>", message_id="<2@evil.test>"))
    box.add(3, _msg(message_id="<3@lists.agency.gov>", attachment=b"x"))
    store = InMemoryStateStore()
    h = EmailImapSourceHandler(_cfg(), imap_factory=box.factory)
    got = await _drain(h, _ctx(_cfg(), store, secrets_resolve=_resolver()))
    assert [s.payload["message_id"] for s in got] == ["1@lists.agency.gov", "3@lists.agency.gov"]
    assert box.selected_readonly is True
    search = next(c for c in box.calls if c[:2] == ("uid", "SEARCH"))
    assert search[2:] == ("SINCE", "26-Sep-2026", "UNSEEN", "UID", "1:*")
    assert box.calls[-1] == ("logout",)
    health = store.snapshot()["email_imap_health"]
    assert health["state"] == "healthy"
    d = health["detail"]
    assert (d["messages_yielded"], d["rejected_sender"], d["attachments_ignored"]) == (2, 1, 1)
    assert store.snapshot()["email_imap_cursor"]["folders"]["INBOX"]["last_uid"] == 3


async def test_uid_cursor_advances_and_repoll_reads_only_new():
    box = ImapDouble()
    box.add(1, _msg(message_id="<1@lists.agency.gov>"))
    box.add(2, _msg(message_id="<2@lists.agency.gov>"))
    store = InMemoryStateStore()
    cfg = _cfg()
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory)
    assert len(await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()))) == 2
    cur = store.snapshot()["email_imap_cursor"]["folders"]["INBOX"]
    assert (cur["last_uid"], cur["uidvalidity"]) == (2, 7)
    # Nothing new: the RFC ``N:*`` quirk returns UID 2 again; it is filtered.
    assert await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver())) == []
    box.add(3, _msg(message_id="<3@lists.agency.gov>"))
    got = await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()))
    assert [s.raw_provenance["uid"] for s in got] == [3]
    assert ("uid", "SEARCH", "SINCE", "26-Sep-2026", "UNSEEN", "UID", "3:*") in box.calls


async def test_interrupted_consumer_resumes_at_first_undecided_message():
    box = ImapDouble()
    for uid in (1, 2, 3):
        box.add(uid, _msg(message_id=f"<{uid}@lists.agency.gov>"))
    store = InMemoryStateStore()
    cfg = _cfg()
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory)
    first = await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()), limit=2)
    assert [s.raw_provenance["uid"] for s in first] == [1, 2]
    # The consumer took 1 and 2 but stopped while 2 was in hand: only 1 is decided.
    assert store.snapshot()["email_imap_cursor"]["folders"]["INBOX"]["last_uid"] == 1
    rest = await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()))
    assert [s.raw_provenance["uid"] for s in rest] == [2, 3]


async def test_max_messages_per_poll_bounds_the_batch():
    box = ImapDouble()
    for uid in range(1, 6):
        box.add(uid, _msg(message_id=f"<{uid}@lists.agency.gov>"))
    store = InMemoryStateStore()
    cfg = _cfg(max_messages_per_poll=2)
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory)
    assert [s.raw_provenance["uid"] for s in await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()))] == [1, 2]
    assert [s.raw_provenance["uid"] for s in await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()))] == [3, 4]


async def test_uidvalidity_change_restarts_walk_and_hash_is_stable():
    box = ImapDouble(uidvalidity=7)
    box.add(4, _msg(message_id="<4@lists.agency.gov>"))
    store = InMemoryStateStore()
    cfg = _cfg()
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory)
    before = await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()))
    # The folder is re-created: new validity, UIDs restart below the cursor.
    box.uidvalidity = 99
    box.messages = {}
    box.add(1, _msg(message_id="<4@lists.agency.gov>"))
    after = await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver()))
    assert [s.raw_provenance["uid"] for s in after] == [1]
    assert after[0].content_hash == before[0].content_hash
    cur = store.snapshot()["email_imap_cursor"]["folders"]["INBOX"]
    assert (cur["uidvalidity"], cur["last_uid"]) == (99, 1)


async def test_login_refusal_is_unhealthy_and_holds_the_cursor():
    box = ImapDouble(password="right")
    box.add(1, _msg())
    store = InMemoryStateStore()
    cfg = _cfg()
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory)
    assert await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver("wrong"))) == []
    health = store.snapshot()["email_imap_health"]
    assert health["state"] == "unhealthy"
    assert "AUTHENTICATIONFAILED" in health["last_error"]
    assert "wrong" not in health["last_error"]
    assert "email_imap_cursor" not in store.snapshot()


async def test_network_error_is_degraded():
    def _down(config):
        raise ConnectionRefusedError(111, "Connection refused")
    store = InMemoryStateStore()
    cfg = _cfg()
    h = EmailImapSourceHandler(cfg, imap_factory=_down)
    assert await _drain(h, _ctx(cfg, store, secrets_resolve=_resolver())) == []
    assert store.snapshot()["email_imap_health"]["state"] == "degraded"


def test_egress_guard_refuses_a_private_mail_host(monkeypatch):
    monkeypatch.delenv("LEGBA_EGRESS_ALLOW_HOSTS", raising=False)
    with pytest.raises(EgressBlockedError):
        _open_imap(_cfg(host="127.0.0.1"))


# ---------------------------------------------------------------------------
# SEAMS #63 — no mailbox credential fails loud, never an empty poll
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resolver", [None, "missing", "empty"])
async def test_seam_refuses_activation_pull_and_health(resolver):
    async def _missing(secret_id):
        raise KeyError(secret_id)  # CredentialVault.resolve -> MissingSecretError(KeyError)

    async def _empty(secret_id):
        return b""

    secrets = {None: None, "missing": _missing, "empty": _empty}[resolver]
    box = ImapDouble()
    box.add(1, _msg())
    store = InMemoryStateStore()
    cfg = _cfg()
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory)
    ctx = _ctx(cfg, store, secrets_resolve=secrets)
    for hook in (h.activation_check, h.on_configure, h.on_activate):
        with pytest.raises(EmailMailboxNotConfigured, match=EMAIL_MAILBOX_NOT_CONFIGURED):
            await hook(ctx)
    with pytest.raises(EmailMailboxNotConfigured) as exc:
        await _drain(h, ctx)
    assert exc.value.reason == EMAIL_MAILBOX_NOT_CONFIGURED
    assert box.calls == []  # never connected, never logged in
    rec = store.snapshot()["email_imap_health"]
    assert rec["state"] == "unhealthy" and rec["detail"]["reason"] == EMAIL_MAILBOX_NOT_CONFIGURED
    health = await h.health_check(ctx)
    assert health.state == "unhealthy"
    assert health.last_error.startswith(EMAIL_MAILBOX_NOT_CONFIGURED)


async def test_health_check_logs_in_readonly_when_configured():
    box = ImapDouble()
    cfg = _cfg()
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory)
    health = await h.health_check(_ctx(cfg, secrets_resolve=_resolver()))
    assert health.state == "healthy" and health.detail["uidvalidity"] == 7
    assert box.selected_readonly is True


# ---------------------------------------------------------------------------
# The real SourceCore poll path: activation refusal, gate + counter, dedupe
# ---------------------------------------------------------------------------


class _Conn:
    """Enough of asyncpg for write_canonical_signal + the poll-outcome row,
    with the S-4 collapse keyed on (source_id, content_hash) like the SQL."""

    def __init__(self, db: dict[str, Any]) -> None:
        self.db = db

    async def fetchval(self, query, *args):
        if "UPDATE signals" in query:
            return self.db["hashes"].get((args[0], args[1]))
        return None

    async def fetchrow(self, query, *args):
        if "INSERT INTO signals" in query:
            self.db["hashes"][(args[1], args[23])] = args[0]
            self.db["inserted"].append(args[0])
            return {"id": args[0]}
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, query, *args):
        if "source_poll_outcomes" in query:
            self.db["outcomes"].append({"outcome": args[3], "error": args[7],
                                        "keyword_filtered": args[10]})
        return None


class _Acquire:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return _Conn(self.db)

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self):
        self.db: dict[str, Any] = {"hashes": {}, "inserted": [], "outcomes": []}

    def acquire(self):
        return _Acquire(self.db)


def _descriptor(config: dict[str, Any], kind: str = "email_imap") -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id="source.test.email", name="email-test", kind=kind,
            schema_uri="legba/source/1.0.0", version="b" * 16,
            owner="test:email", created=NOW, state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme", languages=["en"]),
        acquisition="poll",
        config=config,
        cadence=CadenceBlock(schedule=Cron(raw="*/15 * * * *")),
        pipeline=SourcePipeline(media="reference"),
    )


_RAW_CFG = {
    "host": "imap.mail.test", "username": "reader@example.org",
    "password_ref": SECRET_ID, "allow_from": ["lists.agency.gov"],
}


def _core(config: dict[str, Any], *, secrets_resolve=None, handler=None,
          store: InMemoryStateStore | None = None, kind: str = "email_imap"):
    sd = _descriptor(config, kind)
    pool = _Pool()
    enriched: list[str] = []

    async def _enrichment(signal, ctx):
        enriched.append(signal.payload.get("title"))
        return signal

    deps = StandardDeps(pg_pool=pool, nats_publish=None, secrets_resolve=secrets_resolve)
    core = SourceCore(
        f"source::{sd.identity.id}::email",
        SourceDeps(descriptor=sd, deps=deps, enrichment_stage=_enrichment, handler=handler),
    )
    state = store if store is not None else InMemoryStateStore()

    def _make_context():
        return SourceContext(
            target_id=sd.identity.id, target_version=sd.identity.version,
            source_id=sd.identity.id, config=_RawConfig(**(sd.config or {})),
            state_store=state, secrets_resolve=secrets_resolve, now_fn=lambda: NOW,
        )

    core._make_context = _make_context  # type: ignore[method-assign]
    return core, pool, state, enriched


async def test_activation_refuses_loud_through_the_real_factory_path():
    # sd.handler is None: SourceCore builds the handler through
    # build_source_handler, exactly as the actor does.
    core, _pool, _state, _ = _core(dict(_RAW_CFG), secrets_resolve=None)
    with pytest.raises(EmailMailboxNotConfigured, match=EMAIL_MAILBOX_NOT_CONFIGURED):
        await core.activation_check()

    async def _missing(secret_id):
        raise KeyError(secret_id)

    core, *_ = _core(dict(_RAW_CFG), secrets_resolve=_missing)
    with pytest.raises(EmailMailboxNotConfigured):
        await core.activation_check()
    core, *_ = _core(dict(_RAW_CFG), secrets_resolve=_resolver())
    await core.activation_check()  # configured: activation proceeds


async def test_activation_check_is_a_noop_for_kinds_without_a_guard():
    core, *_ = _core({"url": "http://unused"}, kind="rss")
    await core.activation_check()


def test_actor_activate_runs_the_guard_before_going_active():
    if not getattr(source_actor_mod, "_DAPR_AVAILABLE", False):
        pytest.skip("dapr not importable here")
    src = inspect.getsource(source_actor_mod.SourceActor.activate)
    assert src.index("activation_check") < src.index("rec[\"lifecycle\"] = ACTIVE")


async def test_unconfigured_poll_records_an_error_row_not_an_empty_one():
    core, pool, _state, _ = _core(dict(_RAW_CFG), secrets_resolve=None)
    out = await core.pull_once()
    assert out["outcome"] == "hard_fail"
    assert out["error"].startswith(EMAIL_MAILBOX_NOT_CONFIGURED)
    assert pool.db["outcomes"][-1]["outcome"] == "error"
    assert pool.db["outcomes"][-1]["error"].startswith(EMAIL_MAILBOX_NOT_CONFIGURED)


async def test_subject_gate_is_the_shared_matcher_applied_and_counted_by_sourcecore():
    cfg = _cfg(subject_include=["sanctions"], subject_exclude=["unsubscribe"])
    box = ImapDouble()
    box.add(1, _msg(subject="New sanctions designations", message_id="<1@lists.agency.gov>"))
    box.add(2, _msg(subject="Weekly digest", plain="sanctions in the body only",
                    message_id="<2@lists.agency.gov>"))
    box.add(3, _msg(subject="Sanctions: how to unsubscribe", message_id="<3@lists.agency.gov>"))
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory, secret_resolver=_resolver())
    assert isinstance(h.keyword_gate, KeywordGate)
    assert h.keyword_gate.config.fields == ["title"]
    assert EmailImapSourceHandler(_cfg(), imap_factory=box.factory).keyword_gate is None

    core, pool, state, enriched = _core(cfg.model_dump(), secrets_resolve=_resolver(), handler=h)
    out = await core.pull_once()
    assert out["signals_written"] == 1 and out["keyword_filtered"] == 2
    assert enriched == ["New sanctions designations"]  # gate ran BEFORE enrichment
    cursor = state.snapshot()["cursor"]
    assert cursor["keyword_filtered_total"] == 2
    assert cursor["keyword_filtered_by_reason"] == {"include:none": 1, "exclude:unsubscribe": 1}
    # Dropped messages are still DECIDED: the UID cursor passed all three.
    assert state.snapshot()["email_imap_cursor"]["folders"]["INBOX"]["last_uid"] == 3
    assert pool.db["outcomes"][-1]["outcome"] == "success"


async def test_repoll_of_the_same_message_collapses_in_the_content_hash_write():
    box = ImapDouble(uidvalidity=7)
    box.add(1, _msg(message_id="<same@lists.agency.gov>"))
    cfg = _cfg()
    store = InMemoryStateStore()
    h = EmailImapSourceHandler(cfg, imap_factory=box.factory, secret_resolver=_resolver())
    core, pool, _state, _ = _core(cfg.model_dump(), secrets_resolve=_resolver(),
                                  handler=h, store=store)
    first = await core.pull_once()
    assert first["signals_written"] == 1 and len(pool.db["inserted"]) == 1
    # The folder is re-created and the same message is re-delivered.
    box.uidvalidity = 8
    out = await core.pull_once()
    assert out["signals_written"] == 0
    assert len(pool.db["inserted"]) == 1  # no second row
    assert pool.db["outcomes"][-1]["outcome"] == "success"  # an unchanged re-serve


# ---------------------------------------------------------------------------
# Registration + the worked example
# ---------------------------------------------------------------------------


def test_kind_is_registered_and_the_factory_threads_the_resolver():
    kinds = discover_source_kinds()
    assert kinds["email_imap"] is EmailImapSourceHandler
    resolve = _resolver()
    h = build_source_handler("email_imap", dict(_RAW_CFG), secrets_resolve=resolve)
    assert isinstance(h, EmailImapSourceHandler)
    assert h._secret_resolver is resolve


def test_example_descriptor_validates_as_the_registrar_and_factory_do():
    body = yaml.safe_load(EXAMPLE.read_text())
    sd = SourceDescriptor.model_validate(body, strict=False)
    assert sd.identity.kind == "email_imap"
    assert sd.identity.kind in discover_source_kinds()
    cfg = EmailConfig(**_unwrap_factory_dict(sd.config))
    assert cfg.password_ref == "source.email.example_list.password"
    assert cfg.host == "imap.example.org"
    h = build_source_handler(sd.identity.kind, sd.config)
    assert isinstance(h.keyword_gate, KeywordGate)
    # No literal credential anywhere in the example: the only secret-shaped
    # value is the vault id.
    text = EXAMPLE.read_text()
    assert not re.search(r"^\s*password\s*:", text, re.MULTILINE)
