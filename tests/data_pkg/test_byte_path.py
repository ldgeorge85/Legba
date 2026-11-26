# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a reader can say about a signal's bytes (no database).

* :func:`classify` returns EXACTLY one of the three labels, with the version
  it rests on: received bytes beat an archived copy; bytes that are gone fall
  through and are still named (hash only);
* :func:`slice_item` cuts the item as the response carried it — an RSS item
  and an Atom entry by guid / link, a JSON API object, a GDELT export row by
  GLOBALEVENTID, a Telegram message receipt — and says ``None`` rather than
  guess;
* :func:`locator_from_signal` is the one rule for where an item sits;
* the Telegram receipt keeps text and metadata only: no media bytes;
* the capture log follows a redirect (the live GDELT gap).
"""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from legba.data import byte_path as bp
from legba.data.sources import message_receipt as mr
from legba.data.sources.fetch_capture import CaptureLog, ReceiptTeeTransport, capture_received, capture_scope


def _row(role, vid, *, vrole="response", held=True, locator=None, has_text=False):
    return {"role": role, "version_id": vid, "locator": locator, "version_role": vrole,
            "object_ref": f"cas:sha256/{vid:064x}" if held else None, "bytes_pruned_at": None,
            "has_text": has_text}


def test_three_labels_exactly_one_each():
    s = bp.classify("s1", [_row("response", 7, locator={"item_id": "g1"}), _row("archived", 9, vrole="archiver")])
    assert (s.label, s.version_id, s.link_role, s.has_text) == (bp.RECEIVED_BYTES, 7, "response", True)
    assert s.label_text == "received bytes" and s.links == {"response": 7, "archived": 9}

    s = bp.classify("s2", [_row("archived", 9, vrole="archiver")])
    assert (s.label, s.version_id, s.link_role) == (bp.ARCHIVED_LATER, 9, "archived")
    assert s.label_text == "archived later" and s.has_text

    s = bp.classify("s3", [])
    assert (s.label, s.version_id, s.label_text) == (bp.NO_BYTES_KEPT, None, "no bytes kept")


def test_bytes_that_are_gone_fall_through_and_are_named():
    pruned = _row("response", 7)
    pruned["bytes_pruned_at"] = "2026-11-14"
    s = bp.classify("s", [pruned, _row("archived", 9, vrole="archiver")])
    assert s.label == bp.ARCHIVED_LATER and s.hash_only == (7,)
    s = bp.classify("s", [_row("response", 7, held=False)])
    assert s.label == bp.NO_BYTES_KEPT and s.hash_only == (7,)


def test_a_page_is_preferred_and_an_archiver_version_is_never_received():
    s = bp.classify("s", [_row("response", 3), _row("page", 4, vrole="page", has_text=True)])
    assert (s.link_role, s.version_id) == ("page", 4)
    # A received-role link that points at an archiver copy is still "archived later".
    s = bp.classify("s", [_row("page", 5, vrole="archiver")])
    assert s.label == bp.ARCHIVED_LATER


RSS = (b'<?xml version="1.0"?><rss><channel><title>x</title>'
       b"<item><title>One</title><guid>https://n.example/a/1</guid>"
       b"<description>&lt;p&gt;Dockers &amp;amp; crane operators struck at 06:00.&lt;/p&gt;</description></item>"
       b"<item><title>Two</title><link>https://n.example/a/2?x=1&amp;y=2</link>"
       b"<description><![CDATA[<b>Second</b> item text.]]></description></item>"
       b"</channel></rss>")


def test_an_rss_item_is_cut_by_guid_and_by_escaped_link():
    one = bp.slice_item(RSS, "application/rss+xml", {"item_id": "https://n.example/a/1"})
    assert one is not None and one.basis == "feed_item" and one.exact
    assert one.raw.startswith(b"<item>") and one.raw in RSS
    assert one.text == "One https://n.example/a/1 Dockers & crane operators struck at 06:00."
    two = bp.slice_item(RSS, None, {"item_link": "https://n.example/a/2?x=1&y=2"})
    assert two is not None and "Second item text." in two.text and "One" not in two.text
    assert bp.slice_item(RSS, None, {"item_id": "https://n.example/a/404"}) is None


def test_an_atom_entry_is_cut():
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:x,1</id><title>A</title></entry>'
            b"<entry><id>tag:x,2</id><summary>Port closed to tankers.</summary></entry></feed>")
    cut = bp.slice_item(atom, "application/atom+xml", {"item_id": "tag:x,2"})
    assert cut is not None and "Port closed to tankers." in cut.text and "tag:x,1" not in cut.text


def test_a_json_item_is_the_deepest_object_naming_it():
    body = json.dumps({"value": [{"Id": "don-1", "Title": "Outbreak one"},
                                 {"Id": "don-2", "Title": "Outbreak two", "Summary": "Cases rose."}]}).encode()
    cut = bp.slice_item(body, "application/json", {"item_id": "don-2"})
    assert cut is not None and cut.basis == "json_object" and not cut.exact
    assert "Cases rose." in cut.text and "Outbreak one" not in cut.text


def _zip(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.export.CSV", "\n".join(lines) + "\n")
    return buf.getvalue()


def test_a_gdelt_row_is_cut_by_globaleventid():
    body = _zip(["111\t20261114\tA", "222\t20261114\tB", "2222\t20261114\tC"])
    loc = {"format": "zip_csv", "key_column": "GLOBALEVENTID", "key_index": 0, "key": "222"}
    cut = bp.slice_item(body, "application/zip", loc)
    assert cut is not None and cut.raw == b"222\t20261114\tB" and cut.basis == "zip_csv_row"
    assert bp.slice_item(body, None, {**loc, "key": "333"}) is None
    assert bp.slice_item(b"not a zip", None, loc) is None


class _Media:
    def to_dict(self):
        return {"_": "MessageMediaPhoto", "photo": {"file_reference": b"\x01\x02secret", "stripped": b"\xff" * 40}}


class _TelethonMsg:
    id = 42

    def to_dict(self):
        from datetime import datetime, timezone

        return {"_": "Message", "id": 42, "message": "Shelling reported near the bridge.",
                "date": datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc), "media": _Media(), "views": 10}


def test_the_telegram_receipt_is_text_and_metadata_never_media_bytes():
    body = mr.serialise_message(_TelethonMsg(), {"username": "warnews", "id": 1001, "title": "W"})
    doc = json.loads(body)
    assert doc["format"] == mr.MESSAGE_RECEIPT_FORMAT
    assert doc["message"]["message"] == "Shelling reported near the bridge."
    photo = doc["message"]["media"]["photo"]
    assert set(photo["file_reference"]) == {"bytes_sha256", "bytes_len"}
    assert b"secret" not in body and b"\xff" not in body
    assert body == mr.serialise_message(_TelethonMsg(), {"username": "warnews", "id": 1001, "title": "W"})
    assert mr.message_address({"username": "warnews"}, 42) == (
        "https://t.me/warnews", "https://t.me/warnews#message=42")
    cut = bp.slice_item(body, "application/json", {"whole": True})
    assert cut is not None and cut.text == "Shelling reported near the bridge." and cut.raw == body


def test_capture_received_records_only_inside_a_scope():
    assert capture_received(url="https://t.me/x", body=b"{}", content_type="application/json") is False
    log = CaptureLog()
    with capture_scope(log):
        assert mr.capture_telegram_message(_TelethonMsg(), {"username": "warnews"})
    (cap,) = log.captures
    assert cap.address == "https://t.me/warnews#message=42" and cap.complete and cap.role == "response"


def test_locator_from_signal_is_the_one_rule():
    assert bp.locator_from_signal({"external_id": "g1", "link": "https://a/1"}, {}, None) == {
        "item_id": "g1", "item_link": "https://a/1"}
    assert bp.locator_from_signal({}, {"kind": "gdelt_files", "global_event_id": "9"}, "https://src") == {
        "format": "zip_csv", "key_column": "GLOBALEVENTID", "key_index": 0, "key": "9"}
    assert bp.locator_from_signal({}, {"source_kind": "telegram_channel"}, None) == {"whole": True}
    assert bp.locator_from_signal({}, {}, None) is None


@pytest.mark.asyncio
async def test_the_capture_log_follows_a_redirect_to_the_url_asked_for():
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"location": str(request.url.copy_with(scheme="https"))})
        return httpx.Response(200, content=b"zipbytes")

    log = CaptureLog()
    async with httpx.AsyncClient(transport=ReceiptTeeTransport(httpx.MockTransport(serve)),
                                 follow_redirects=True) as client:
        with capture_scope(log):
            await client.get("http://data.example/g/1.zip")
    assert [c.url for c in log.captures] == ["https://data.example/g/1.zip"]
    assert log.latest_response(url="http://data.example/g/1.zip") == 0
    assert log.latest_response(url="http://data.example/g/2.zip") is None


def test_the_recorder_links_by_receipt_address_then_by_url():
    from legba.runtime.fetch_receipts import ReceiptRecorder

    rec = ReceiptRecorder(source_id="s", enabled=True)
    with capture_scope(rec.log):
        mr.capture_telegram_message(SimpleNamespace(id=1, message="a"), {"username": "c"})
        mr.capture_telegram_message(SimpleNamespace(id=2, message="b"), {"username": "c"})
    from legba.data.sources.telegram import TelegramChannelSourceHandler as T

    first = SimpleNamespace(raw_provenance={"channel_username": "c", "message_id": 1, "source_kind": "telegram_channel"},
                            payload={}, canonical_url=None)
    link = rec.link_for(first, T)
    assert link.response == 0 and link.locator == {"whole": True, "format": mr.MESSAGE_RECEIPT_FORMAT}


class UndefinedColumnError(Exception):
    """Named like asyncpg's: a database before migration 0266."""


class _PreMigrationConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, q: str, *args):
        if "locator" in q:
            raise UndefinedColumnError("column locator does not exist")
        self.calls.append((" ".join(q.split()), args))


@pytest.mark.asyncio
async def test_a_link_still_lands_before_the_migration_without_its_locator():
    from legba.runtime.fetch_receipts import _exec_link

    conn = _PreMigrationConn()
    await _exec_link(conn, ("signal", "id-1", "response", 7, "a" * 64, '{"item_id": "g"}'))
    await _exec_link(conn, ("signal", "id-2", "response", 7, "a" * 64, None))
    assert [c[1] for c in conn.calls] == [("signal", "id-1", "response", 7, "a" * 64),
                                         ("signal", "id-2", "response", 7, "a" * 64)]
