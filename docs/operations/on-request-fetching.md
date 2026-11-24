# On-request fetching (the ingestion intake and the page pipeline)

How a single web page gets into the substrate when something ASKS for it —
a person, an agent, a source's follow policy, the drift re-check or the
evidence archiver — rather than because a feed listed it. The code is
[`src/legba/data/intake_store.py`](../../src/legba/data/intake_store.py) (submit
and admission),
[`src/legba/runtime/intake_drain.py`](../../src/legba/runtime/intake_drain.py)
and [`intake_run.py`](../../src/legba/runtime/intake_run.py) (the drain and one
run), the `page` source kind
[`src/legba/data/sources/page.py`](../../src/legba/data/sources/page.py) with
[`page_meta.py`](../../src/legba/data/sources/page_meta.py),
[`page_quality.py`](../../src/legba/data/sources/page_quality.py) and
[`page_text.py`](../../src/legba/data/sources/page_text.py), and
[`src/legba/runtime/page_landing.py`](../../src/legba/runtime/page_landing.py)
(how a page lands). Every switch named here is OFF by default.

## End to end

1. **A request.** One row in `ingestion_requests` (migration 0249), submitted
   by `submit_request` — the only submit path. An address request names the
   page and a PIPELINE: the source descriptor whose kind fetches one address.
   The instance's default is `source.page.default`
   ([`descriptors/source_page_pipeline_default.yaml`](../../descriptors/source_page_pipeline_default.yaml),
   kind `page`); the agent tool and the drift re-check use it when nothing
   else is named.
2. **Admission** decides in code, and every refusal is a row with its reason
   (`GET /v3/ingestion/intake` counts them): the egress posture of the
   address (http(s), no private or internal literal), the pipeline (it must
   exist and be runnable — an address may run under an `active`, `paused` or
   `configured` pipeline; the page pipeline is `configured` because it has no
   schedule), the sink, depth, the source's follow policy for a followed
   reference, one pending request per target, FRESHNESS (a page fetched in the
   last six hours is answered from the stored copy, nothing fetched), and the
   daily budgets per host, requester and class.
3. **The drain** (`LEGBA_INTAKE_DRAIN=on`, in the runtime host) claims a few
   requests at a time — one in flight per host, ten seconds apart — and runs
   each through the pipeline's own acquisition code (`SourceCore.pull_once`):
   there is no second write path.
4. **Before the fetch** (`page_landing.prepare`): the host licence ledger
   (`source_credibility.license_class`) — a host an admin classified
   `anti_ai_walled`, `tos_restrictive` or `personal_use_only` is NEVER
   FETCHED (the request fails, permanently); and the publisher — a registered
   source whose own URL is on the page's host.
5. **The fetch** (the `page` kind): `robots.txt` honoured as the scraper kind
   does; the guarded egress client (private addresses refused at every hop;
   the pipeline's `fetch.http_version`); at most five redirects; the body cut
   at 5 MiB. The host fetch policy registry
   ([`_fetch_policy.py`](../../src/legba/data/sources/_fetch_policy.py)) is
   the ONE place per-host posture lives: when an edge refuses the plain fetch
   (401/403/429 or a challenge page) and the host's policy resolves
   impersonation on, the fetch is re-issued once with the browser
   fingerprint — the evidence archiver's own rule. Every fetched body is kept
   as a fetch receipt (`fetch_versions`, role `page`, with its extracted
   text), refused or not.
6. **Extraction and the quality gates** (in a worker thread): the archiver's
   Trafilatura extraction (so the document's text equals the receipt's), the
   page's own markup for title, declared publication date, same-site
   canonical, language, author, site name, description and outbound links;
   then the gates — each refusal has a name: `unsupported_content_type` (PDFs
   included, see below), `too_large`, `blocked_by_challenge`, `wall_page`,
   `soft_404`, `login_wall`, `consent_wall`, `paywall`, `too_little_text`
   (under 400 characters), `mostly_navigation` (link density at or above
   0.5), `no_text_extracted`. A refused page writes no signal; the request
   finishes `done` with `result.page.refusal` saying why.
7. **The document.** One signal shaped like a feed entry (`title`,
   `raw_body`, `published_at`, `link`, `summary`, `author`, `tags`,
   `source_url`, `external_id`), through the pipeline's keyword gate,
   enrichment (language, NER, geocode) and the document sink. A publication
   date is only what the page DECLARES; otherwise `published_at` is null and
   `payload.page.fetched_at` is the fetch time, recorded as such.
8. **How it lands** (`page_landing.land`), by the canonical URL:
   * **new** — written; host credibility from the ledger by the page's host,
     or the pipeline's declared default (`unknown_host_credibility`, 0.3)
     flagged `declared_default`; the ledger's licence, when set, as
     `payload.license_class`;
   * **collapsed** — a document already holding the full text (a full-text
     feed entry, or this pipeline's earlier fetch) — nothing written; the
     request and the page version are linked to it;
   * **filled** — a SUMMARY-ONLY document (a feed teaser) — the page text is
     written onto THAT row as `payload.archived_text` (the field the evidence
     archiver fills and every reader already reads as "the article behind
     the teaser"), with `payload.page_fill` saying which request and parent
     brought it; the feed's own `raw_body`, `fetched_at` and `content_hash`
     are untouched and the corpus re-indexes the row.
9. **Provenance.** On a new document: `source_id` is the page pipeline;
   `payload.publisher` names the host, the site and (when there is one) the
   registered source on that host, whose `scope.geo` becomes
   `payload.publisher_origin` (the same geo FALLBACK a feed item carries);
   `payload.intake` names the request, its reason, requester, depth, link
   kind, parent signal and parent request — "fetched on request, followed
   from signal X". The request itself links every record it wrote, collapsed
   into or filled (`ingestion_request_records`).

## What is refused, and why

* **PDFs and other documents** — no PDF text extractor ships in the tree. A
  document family would need one (pypdf or pdfminer.six) in the runtime
  image, a page/size cap, an OCR decision for scanned files and records that
  keep page numbers so a citation can point at a page.
* **Paywalls, logins, consent walls** — never routed around; a page that
  declares a paywall but serves its full text lands, flagged
  `paywall_declared`.
* **Challenge pages** — refused under the archiver's name
  `blocked_by_challenge`; a 401/403 is recorded as BLOCKED, not as gone.
  So is a page on an access-wall sub-domain (`unblock.…`, `captcha.…`) and a
  redirect that left the requested host and landed on a page carrying any
  wall or challenge phrase, at any length (for example, www.federalregister.gov
  redirects programmatic fetches to unblock.federalregister.gov's "Request
  Access" CAPTCHA page). A redirected page's receipt is stored under the
  address that answered (the final URL) and is still linked to the request
  (`result_version_id`, matched on the body hash), landed or refused.
* **Robots.txt disallow** — never fetched; the request fails permanently.

## Switch-on order (and what to check at each step)

1. **The drain** — `LEGBA_INTAKE_DRAIN=on` on the runtime. Before it:
   register `source.page.default` and walk it to `configured`
   (the lifecycle overlay row is `descriptors/LIFECYCLE.d/source__source.page.default.md`). Check: the boot log
   `intake.drain.started`; submit one request by hand for a known article
   page and read it back (`GET /v3/ingestion/intake`, the request row's
   `result`): `signals_written: 1`, `landing: new`, a `fetch_versions` row
   with role `page`; the signal's `payload.intake.request_id` is the request.
   Then a second request for the same page inside six hours: `done`,
   `satisfied_from: stored_signal`, nothing fetched. Watch the loop-lag
   monitor while it runs.
2. **The drift re-check, receipts only** — `LEGBA_INTAKE_DRIFT_RECHECK=on`
   (pipeline defaults to `source.page.default`; `LEGBA_INTAKE_DRIFT_PIPELINE=none`
   turns it off). Check: one batch per day, at most `LEGBA_INTAKE_DRIFT_CAP`
   requests, sink `receipt` (no signals written), new `fetch_versions` and,
   for a changed page, a `fetch_drift_observations` row.
3. **The archiver's routing** — the `evidence_archiver` descriptor options
   `fetch_via_intake: true`, `intake_pipeline: source.page.default`. Check
   the archiver's `intake_deferred` counter rises then falls, archived rows
   whose bytes came from a request version, and that the two policy hosts
   (impersonation) still fall back to the archiver's own fetch.
4. **Follow policies on chosen sources** — a `follow` block on a source
   descriptor naming `pipeline: source.page.default`, then
   `LEGBA_INTAKE_FOLLOW=on`. Start with ONE summary-only feed and
   `kinds: [article]`, `same_host: true`. Check: requests with reason
   `followed_reference` and `parent_signal_id` set; their results are
   `filled` (the teaser row now carries `archived_text`) rather than new rows;
   refusals by reason; the per-host daily budget.
5. **Agent requests** — `LEGBA_INTAKE_AGENT_REQUESTS=on` and the
   `ingestion_intake` pack granted to the analysts that may ask. Check the
   `agent` class budget and the requester budget, and the action-pack
   invocation counts for `request_ingestion`.

## Knobs (the pipeline descriptor's `config`)

| key | default | meaning |
|---|---|---|
| `respect_robots` | `true` | obey robots.txt (404 = no rules; unreachable = permitted) |
| `timeout_seconds` | `30` | per-request timeout |
| `max_redirects` | `5` | redirect hops |
| `max_bytes` | `5242880` | body cap; above it `too_large` |
| `min_text_chars` | `400` | `too_little_text` below it |
| `max_link_density` | `0.5` | `mostly_navigation` at or above it |
| `max_text_chars` | `200000` | extracted-text cap (the archiver's) |
| `unknown_host_credibility` | `0.3` | credibility for a host the ledger does not know, flagged; `null` leaves it unscored |
