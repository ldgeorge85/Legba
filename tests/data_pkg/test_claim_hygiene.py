# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claim-text hygiene at ingest (pure tests; every fixture INVENTED).

A claim row keeps the claim (claimant, victim ORGANISATION, date, words) and
loses what a leak post carries with it: URL paths (host only), onion
addresses, personal data, file listings and sample dumps. The counts (never
the values) reach the poll record. Private individuals' NAMES are not
redacted by pattern — stated in ``claim_hygiene``'s docstring, pinned here.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data import claim_hygiene as H
from legba.data import claim_support as CS
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import Signal

ONION = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"


def _clean(text: str) -> tuple[str, dict[str, int]]:
    out, rep = H.hygiene_text(text)
    return out, rep.counts


# ---------------------------------------------------------------------------
# 1. URLs: host only; onion: presence only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expect,kind", [
    ("see https://files.example-share.net/d/acme_dump.zip?k=1#x now",
     "see [link: files.example-share.net] now", "url_to_host"),
    ("mirror hxxps://leak[.]example[.]org/acme/part1.rar.", "mirror [link: leak.example.org].", "url_to_host"),
    ("at files.example-share.net/x/y.7z", "at [link: files.example-share.net]", "url_to_host"),
    (f"blog http://{ONION}/post/acme", "blog [onion link]", "onion_link"),
    (f"blog {ONION}/post/acme", "blog [onion link]", "onion_link"),
    ("ftp://user:secret@files.example.org/a.zip", "[link: files.example.org]", "url_to_host"),
])
def test_urls_keep_only_their_host(raw: str, expect: str, kind: str):
    out, counts = _clean(raw)
    assert out == expect
    assert counts.get(kind) == 1
    assert "/" not in out.replace("[link: ", "")


def test_a_bare_host_without_a_path_is_text():
    assert _clean("Nightjar listed acme-widgets.example on 12 November")[1] == {}


# ---------------------------------------------------------------------------
# 2. Personal data: redacted by kind, never stored, counted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,token,kind", [
    ("mail jane.roe@acme-widgets.example today", "[email]", "email"),
    ("write ops [at] acme [dot] example", "[email]", "email"),
    ("call +44 20 7946 0958", "[phone number]", "phone"),
    ("call (555) 010-0199", "[phone number]", "phone"),
    ("tel: 0123 456 7890", "[phone number]", "phone"),
    ("SSN 078-05-1120", "[id number]", "gov_id"),
    ("NI number AB 12 34 56 C", "[id number]", "gov_id"),
    ("passport no: X1234567", "[id number]", "gov_id"),
    ("card 4111 1111 1111 1111", "[payment card number]", "payment_card"),
    ("IBAN GB82 WEST 1234 5698 7654 32", "[bank account number]", "iban"),
    ("lives at 12 Example Street", "[street address]", "street_address"),
    ("password: hunter2", "[credential]", "secret"),
    ("api_key=abcd1234efgh5678", "[credential]", "secret"),
    ("Bearer abcdefghijklmnop", "[credential]", "secret"),
    ("token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl", "[credential]", "secret"),
])
def test_personal_data_is_redacted_and_counted(raw: str, token: str, kind: str):
    out, counts = _clean(raw)
    assert token in out
    assert counts.get(kind, 0) >= 1


def test_a_non_luhn_number_and_dates_and_sizes_are_kept():
    text = "Posted 2026-11-14T10:00:00Z, 1.2 TB, 12,000 files, ref 4111 1111 1111 1112"
    out, counts = _clean(text)
    assert out == text and counts == {}


def test_organisation_names_are_kept_and_person_names_are_not_attempted():
    text = "Nightjar posted Acme Widgets Ltd; the post names its CFO Jane Roe"
    assert _clean(text) == (text, {})


# ---------------------------------------------------------------------------
# 3. Listings and dumps: a bounded description
# ---------------------------------------------------------------------------


def test_a_file_listing_becomes_a_count():
    text = "Files:\n/finance/payroll.xlsx\n/hr/emp_001.pdf\n/legal/nda.docx\n/it/backup.sql\nEnd."
    out, counts = _clean(text)
    assert out == "Files:\n[lists 4 files]\nEnd."
    assert counts == {"files_cut": 4}


def test_an_inline_file_list_becomes_a_count():
    out, counts = _clean("Leaked: invoices.pdf, payroll.xlsx, customers.csv and backup.sql from Acme.")
    assert out == "Leaked: [lists 4 files] from Acme."
    assert counts == {"files_cut": 4}


def test_a_sample_dump_becomes_a_count():
    out, counts = _clean("Sample:\nid,name,email,phone\n1,A,B,C\n2,D,E,F\n3,G,H,I")
    assert out == "Sample:\n[sample data: 4 rows withheld]"
    assert counts == {"sample_rows_cut": 4}


def test_two_file_names_are_left_as_written():
    text = "The post mentions invoices.pdf and payroll.xlsx"
    assert _clean(text) == (text, {})


# ---------------------------------------------------------------------------
# 4. The row: stamp, own-host address, media, other sources untouched
# ---------------------------------------------------------------------------

DECL = {"claimant": "Example Tracker (leak-site tracker)", "verb": "listed"}


def _claim_desc(**scope: Any) -> SourceDescriptor:
    return SourceDescriptor.model_validate({
        "identity": {"id": "source.test.claims", "name": "claims", "kind": "rss",
                     "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
                     "owner": "t", "created": "2026-11-14T00:00:00Z", "state": "draft"},
        "scope": {"source_class": "claim", "claim": dict(DECL), "access_class": "public", **scope},
        "cadence": {"schedule": {"raw": "0 * * * *"}},
        "config": {"url": {"factory_kind": "text", "raw": "https://tracker.example.org/rss.xml"}},
    }, strict=False)


def _plain_desc() -> SourceDescriptor:
    return SourceDescriptor.model_validate({
        "identity": {"id": "source.test.feed", "name": "feed", "kind": "rss",
                     "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
                     "owner": "t", "created": "2026-11-14T00:00:00Z", "state": "draft"},
        "scope": {"access_class": "public"},
        "cadence": {"schedule": {"raw": "0 * * * *"}},
        "config": {"url": {"factory_kind": "text", "raw": "https://news.example.org/rss.xml"}},
    }, strict=False)


def _row(link: str) -> Signal:
    return Signal(
        source_id="source.test.claims", canonical_url=link, media_ref="https://x.example/img.png",
        payload={"title": "Nightjar posted Acme Widgets", "link": link,
                 "summary": f"Data at http://{ONION}/acme, contact jane.roe@acme-widgets.example",
                 "raw_body": "<p>files: a.pdf, b.xlsx, c.sql</p>",
                 "source_url": "https://tracker.example.org/rss.xml",
                 "published_at": "2026-11-14T10:00:00+00:00", "media": {"x": 1},
                 "tags": ["ransomware"]},
    )


def test_a_claim_row_is_stamped_cleaned_and_counted():
    sig = _row("https://tracker.example.org/victim/acme-widgets")
    rep = H.prepare_claim_row(_claim_desc(), sig)
    assert rep is not None
    p = sig.payload
    assert p["claim"]["claimant"] == DECL["claimant"] and p["claim"]["source_class"] == "claim"
    # The tracker's own item page is kept (dedupe, the receipt address).
    assert sig.canonical_url == p["link"] == "https://tracker.example.org/victim/acme-widgets"
    assert p["summary"] == "Data at [onion link], contact [email]"
    assert "[lists 3 files]" in p["raw_body"]
    assert p["source_url"] == "https://tracker.example.org/rss.xml"
    assert "media" not in p and sig.media_ref is None
    assert rep.counts == {"onion_link": 1, "email": 1, "files_cut": 3, "rows": 1}


def test_a_foreign_item_link_becomes_a_non_resolvable_key():
    sig = _row(f"http://{ONION}/post/acme")
    rep = H.prepare_claim_row(_claim_desc(), sig)
    assert sig.canonical_url.startswith("claim-item:") and sig.payload["link"] == sig.canonical_url
    assert "onion" not in sig.canonical_url
    assert rep.counts["onion_link"] >= 2
    other = _row("https://gang-mirror.example.net/acme")
    H.prepare_claim_row(_claim_desc(), other)
    assert other.canonical_url.startswith("claim-item:")


def test_hygiene_is_stable_on_its_own_output():
    sig = _row("https://tracker.example.org/victim/acme-widgets")
    H.prepare_claim_row(_claim_desc(), sig)
    before = dict(sig.payload)
    again = H.prepare_claim_row(_claim_desc(), sig)
    assert sig.payload == before
    assert again.counts == {"rows": 1}


def test_any_other_source_is_untouched_byte_for_byte():
    sig = _row(f"http://{ONION}/post/acme")
    sig.payload["claim"] = {"source_class": "claim", "claimant": "spoof"}
    snapshot = sig.model_dump_json()
    assert H.prepare_claim_row(_plain_desc(), sig) is None
    assert sig.model_dump_json() == snapshot


def test_the_poll_tally_reaches_the_record_shape():
    stats: dict[str, int] = {"reserve_unchanged": 2}
    H.tally(stats, None)
    assert H.poll_claim_hygiene(stats) is None
    for _ in range(2):
        H.tally(stats, H.prepare_claim_row(_claim_desc(), _row("https://tracker.example.org/v/a")))
    assert H.poll_claim_hygiene(stats) == {"onion_link": 2, "email": 2, "files_cut": 6, "rows": 2}
    assert stats["reserve_unchanged"] == 2
    assert set(H.poll_claim_hygiene(stats)) <= set(H.HYGIENE_KINDS)


def test_the_poll_writer_appends_the_column_only_when_present():
    import asyncio

    from legba.runtime.source_poll_outcome import write_poll_outcome

    seen: list[tuple[str, tuple]] = []

    class _Conn:
        async def execute(self, sql: str, *args: Any) -> None:
            seen.append((sql, args))

    common = dict(source_id="s", source_version="v", owner_tenant="t", outcome="success",
                  health_state=None, capped=False, signals_written=1, error=None)
    asyncio.run(write_poll_outcome(_Conn(), **common))
    asyncio.run(write_poll_outcome(_Conn(), **common, claim_hygiene={"rows": 1, "email": 2}))
    assert "claim_hygiene" not in seen[0][0] and len(seen[0][1]) == 11
    assert "claim_hygiene" in seen[1][0] and "$12::jsonb" in seen[1][0]
    assert seen[1][1][-1] == '{"rows": 1, "email": 2}'


def test_the_source_actor_runs_the_step_before_enrichment():
    """The ONE call site: ``_process_one`` cleans before ``run_baseline``."""
    import inspect

    from legba.runtime.source_actor import SourceCore

    src = inspect.getsource(SourceCore._process_one)
    assert src.index("prepare_claim_row(self.descriptor, raw)") < src.index("run_baseline(")


# ---------------------------------------------------------------------------
# 5. Who may read a claim source's stored text
# ---------------------------------------------------------------------------


def test_stored_claim_text_is_operator_class_only():
    from legba.data.display_policy import OPERATOR, READER

    assert CS.claim_text_withheld(OPERATOR, is_claim=True) is None
    assert CS.claim_text_withheld(READER, is_claim=True) == "claim_text_admin_only"
    assert CS.claim_text_withheld("contributor", is_claim=True) == "claim_text_admin_only"
    assert CS.claim_text_withheld(READER, is_claim=False) is None


def test_a_contributor_session_is_reader_class_for_display(monkeypatch):
    """A configured registry (a token set, no dev mode): a signed-in
    contributor is READER class, so the stored claim text is withheld."""
    from legba.data.registry import _deps
    from legba.data.registry.display_class import READER, class_for_principal

    monkeypatch.setenv(_deps.API_TOKEN_ENV, "t" * 40)
    monkeypatch.delenv(_deps.DEV_MODE_ENV, raising=False)
    assert class_for_principal("user:42", {"legba_role": "contributor"}) == READER
