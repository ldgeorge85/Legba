# Sources — the catalog of what Legba ingests

*A legible inventory of the open-source feeds Legba acquires from: the
three-tier scope model (what a fresh deploy gets vs. the full repo catalog vs.
what is live-productive), the per-source catalog table, and where the
registration commands live. This is the "what feeds are there" doc. For **how**
a feed becomes a canonical signal — the `SourceActor`, baseline enrichment,
fan-out — see `ACQUISITION.md`; for the **handler kinds** (the dozen-plus source
contracts) see `DATA_SOURCES.md`; for the **registration commands** see
`SETUP.md` and `RUNBOOK.md`.*

> **Why this doc exists.** A fresh deploy that stops at the documented
> working-set gets **only 3 RSS feeds** — which is exactly what a reviewer
> reading the quick-start sees, and exactly why "Legba only has 3 RSS feeds"
> keeps getting (correctly, per the old docs) repeated. That is the *minimal
> cold-start verification set*, not the catalog and not the live scope. The
> full **46-source catalog** lives in a separate, manually-run registration
> script that the working-set bring-up does **not** invoke. This doc makes the
> real scope visible and points at the one step that reaches it.

---

## 1. The three-tier scope model

Legba's "how many sources?" has three honest answers, because three different
things are being counted. Anchor every scope claim to these:

| Tier | Count | What it is | Where it comes from |
|---|---|---|---|
| **1 — Minimal cold-start** | **3** shared RSS | The smallest loop that proves the path from empty volumes: BBC World, Al Jazeera World, Deutsche Welle. This is *all a fresh deploy gets* if it stops at the documented working-set. | `scripts/bringup_register_sources.py` (standalone 3-source registrar) and the working-set script `scripts/bringup_register_p17_workingset.py` (RUNBOOK §7) |
| **2 — Full repo catalog** | **46** sources | The full catalog of independently-verified, no-auth feeds: **43 `rss` + 3 `geojson`** hazard feeds. **NOT auto-run on deploy and NOT part of the working-set bring-up** — a separate manual step a fresh operator currently misses. Running it is how you reach current/full scope. | `scripts/bringup_register_source_catalog.py` (the `CATALOG` tuple — 46 `CatalogEntry`, owner `s1_catalog`) |
| **3 — Live-productive** | **49** sources | The real "productive scope" of a representative running deployment: distinct `source_id` values that have actually emitted signals — the 46-catalog sources **plus** seed / world-baseline curated adapters. | Live reconcile against the production `signals` table |

Two adjacent counts round out the picture (neither is a different set of
*feeds* — they are registry / fan-out bookkeeping):

- **52 registered head source descriptors** — distinct non-autowired active
  head `source_descriptors` rows in the registry (the catalog plus the
  operator-pinned `descriptors/source_*.yaml` and seed sources; a few of these
  are registered but quiet, which is why "registered" (52) ≥ "live-productive"
  (49)).
- **63 autowired per-target fan-out templates** (`src_autowire*` / `src_tmpl*`)
  — these are **generated, not hand-authored** feeds. They are not new
  upstreams; they are the discovery/auto-wire machinery's per-target binding
  templates (one published BBC feed fans out to nineteen G20 targets without
  re-fetching anything — see `ACQUISITION.md` §6). Do **not** count them as
  ingest sources.

> **The honest one-liner.** *3 minimal · 46 catalog · 49 live-productive* — plus
> 52 registered descriptors and 63 autowired fan-out templates. A review that
> sees "only 3 RSS feeds" is reading the cold-start verification set, not the
> catalog; the fix is to register the 46-source catalog (§4).

---

## 2. The full catalog (46 sources)

The catalog is **43 `rss` + 3 `geojson`** entries — every feed below was probed
live (HTTP GET + `feedparser` / GeoJSON parse) before inclusion. Each `rss`
entry runs the baseline enrichment chain `dedupe (tiers 1–2) → language_detect →
ner_multilingual → geocode` (and `fact_extractor` on the four feeds flagged
below); `geojson` is **geocode-only** by default, except NWS, which opts into
`language_detect + ner_multilingual` (its alerts carry rich English headline /
description text). The script seeds host-level `source_credibility` rows
(`ON CONFLICT DO NOTHING`, so operator overrides always win) and is **idempotent**
— re-runs report `unchanged` for already-current heads. It also supports
`--verify` (a live HTTP probe that registers nothing).

`fact_extract = True` on **4** feeds only: `source.cna.all`, `source.npr.world`,
`source.france24.english`, `source.economist.international`.

### 2.1 RSS / Atom feeds (43)

| # | Source id | Feed URL | Notes |
|---|---|---|---|
| 1 | `source.gdacs.alerts` | `https://www.gdacs.org/xml/rss.xml` | crisis / hazard, gov |
| 2 | `source.crisisgroup.latest` | `https://www.crisisgroup.org/rss` | thinktank |
| 3 | `source.newhumanitarian.all` | `https://www.thenewhumanitarian.org/rss.xml` | |
| 4 | `source.who.news` | `https://www.who.int/rss-feeds/news-english.xml` | gov / health |
| 5 | `source.iaea.topnews` | `https://www.iaea.org/feeds/topnews` | gov / nuclear |
| 6 | `source.cdc.travel_notices` | `https://wwwnc.cdc.gov/travel/rss/notices.xml` | gov |
| 7 | `source.cdc.outbreaks_us` | `https://tools.cdc.gov/api/v2/resources/media/285676.rss` | gov, geo=US |
| 8 | `source.hrw.news` | `https://www.hrw.org/rss/news` | human_rights |
| 9 | `source.amnesty.latest` | `https://www.amnesty.org/en/latest/feed/` | human_rights |
| 10 | `source.civicus.monitor` | `https://monitor.civicus.org/feed/` | human_rights |
| 11 | `source.csis.analysis` | `https://www.csis.org/rss.xml` | thinktank |
| 12 | `source.rand.press` | `https://www.rand.org/news/press.xml` | thinktank |
| 13 | `source.atlanticcouncil.all` | `https://www.atlanticcouncil.org/feed/` | thinktank |
| 14 | `source.warontherocks.all` | `https://warontherocks.com/feed/` | thinktank / defense |
| 15 | `source.bellingcat.all` | `https://www.bellingcat.com/feed/` | osint |
| 16 | `source.foreignpolicy.all` | `https://foreignpolicy.com/feed/` | |
| 17 | `source.foreignaffairs.all` | `https://www.foreignaffairs.com/rss.xml` | |
| 18 | `source.defenseone.all` | `https://www.defenseone.com/rss/all/` | defense |
| 19 | `source.twz.all` | `https://www.twz.com/feed` | defense / osint |
| 20 | `source.gcaptain.all` | `https://gcaptain.com/feed/` | maritime / defense |
| 21 | `source.taskandpurpose.all` | `https://taskandpurpose.com/feed/` | defense, geo=US |
| 22 | `source.yonhap.english` | `https://en.yna.co.kr/RSS/news.xml` | wire, geo=KR, state-affiliated |
| 23 | `source.cna.all` | `https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml` | geo=SG, **fact_extract** |
| 24 | `source.anadolu.english` | `https://www.aa.com.tr/en/rss/default?cat=guncel` | geo=TR, state-affiliated |
| 25 | `source.tehrantimes.all` | `https://www.tehrantimes.com/rss` | geo=IR, state-affiliated |
| 26 | `source.tass.english` | `https://tass.com/rss/v2.xml` | geo=RU, state-affiliated |
| 27 | `source.allafrica.headlines` | `https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf` | aggregator / africa |
| 28 | `source.africanews.all` | `https://www.africanews.com/feed/rss` | africa |
| 29 | `source.timesofindia.world` | `https://timesofindia.indiatimes.com/rssfeeds/296589292.cms` | south_asia |
| 30 | `source.hindustantimes.world` | `https://www.hindustantimes.com/feeds/rss/world-news/rssfeed.xml` | south_asia |
| 31 | `source.mercopress.all` | `https://en.mercopress.com/rss` | americas |
| 32 | `source.globalvoices.all` | `https://globalvoices.org/feed/` | aggregator |
| 33 | `source.voa.africa` | `https://www.voanews.com/api/z-botl-vomx-tpertmq` | africa, state-affiliated |
| 34 | `source.npr.world` | `https://feeds.npr.org/1004/rss.xml` | **fact_extract** |
| 35 | `source.france24.english` | `https://www.france24.com/en/rss` | state-affiliated, **fact_extract** |
| 36 | `source.economist.international` | `https://www.economist.com/international/rss.xml` | **fact_extract** |
| 37 | `source.xinhua.world` | `http://www.xinhuanet.com/english/rss/worldrss.xml` | state-affiliated |
| 38 | `source.globaltimes.all` | `https://www.globaltimes.cn/rss/outbrain.xml` | geo=CN, state-affiliated |
| 39 | `source.aljazeera.arabic` | `https://www.aljazeera.net/aljazeerarss/…` | lang=ar, state-affiliated |
| 40 | `source.federalreserve.press` | `https://www.federalreserve.gov/feeds/press_all.xml` | gov / finance, geo=US |
| 41 | `source.eia.press` | `https://www.eia.gov/rss/press_rss.xml` | gov / energy, geo=US |
| 42 | `source.stategov.travel_advisories` | `https://travel.state.gov/_res/rss/TAsTWs.xml` | gov / advisory |
| 43 | `source.fcdo.travel_advice` | `https://www.gov.uk/foreign-travel-advice.atom` | gov / advisory (Atom) |

### 2.2 GeoJSON hazard / structured feeds (3)

These are the **model-free, non-text** modality: the `geojson` handler emits
`structured` / `application/geo+json` signals (geometry inlined, coordinates
promoted to the `geo` column) with **no extraction model in the loop**. They are
the special hazard/structured feeds — and the exogenous resolution catalogs the
experimental forecast pilot reads (see `README.md` and `ANALYSIS.md`).

| # | Source id | Endpoint URL | Notes |
|---|---|---|---|
| 1 | `source.usgs.earthquakes_m45` | `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson` | seismic, gov; `max_features` 5000 |
| 2 | `source.nws.active_alerts` | `https://api.weather.gov/alerts/active?severity=Severe,Extreme` | weather, geo=US; `enrich_text=True` → also gets `language_detect` + `ner_multilingual` + `geocode` |
| 3 | `source.nasa.eonet_events` | `https://eonet.gsfc.nasa.gov/api/v3/events/geojson?days=3` | natural events, gov |

> **Probe-drops — NOT in the 46, do not count them.** The catalog script's
> docstring lists feeds that were probed and deliberately **kept out** (dead /
> blocked / discontinued as of 2026-06-12): ReliefWeb, ProMED, Brookings, Nation
> Africa, DFAT Smartraveller, CISA, Kyodo, SIPRI, EMSC. These are excluded from
> the catalog count.

---

## 3. Handler kinds beyond the catalog

The 46-source catalog exercises two handler kinds (`rss`, `geojson`), but the
acquisition plane ships a **dozen-plus** source-handler kinds, all conforming to
the one `SourceHandler` contract and yielding the one canonical `Signal` shape:
`rss`, `gdelt_query`, `acled`, `mediacloud`, `opensanctions`, `scraper`,
`firecrawl`, `telegram_channel`, `discord_webhook`, `common_crawl_news`,
`intelmq_collector_bridge`, `generic_webhook`, `json_api`, and `geojson`. The
catalog is the **no-auth, verified-live** subset that needs no credentials to
register; the credentialed / push kinds are registered per-deployment. The
per-kind contract and config detail live in `DATA_SOURCES.md`; the handler
modules live in `src/legba/data/sources/` (see `CODE_MAP.md` §2.5).

### 3.1 Credentialed sources — live status

These kinds need a per-deployment secret in the vault before they poll, and each
is registered by a per-source bringup — they are in **neither** the working-set
**nor** the no-auth catalog, so a fresh deploy lights up with zero secrets and
**none of these active** unless an operator opts each one in (provision the
secret in the vault, then register the descriptor). Status in the reference
deployment:

| Source (descriptor) | Auth | Status |
|---|---|---|
| `source.telegram.org_channels` | Telegram API (id/hash/session) | **Active** — thousands of signals, hourly. The live exemplar of a credentialed source. |
| `source.gdelt.doc_api` | none (free `json_api`) | **Active** — registered; the handler works, but GDELT's free DOC API rate-limits (`429`) bursty polls; it produces on its spaced cron cadence. |
| `source.acled.conflict` | **OAuth2 password grant** (account email + password) | **Handler migrated to OAuth2** (ACLED retired the legacy api-key method); creds **authenticate** (token issued). Activation is blocked one step upstream: the ACLED account must be **granted data-API access in the ACLED Access portal** (a read returns `403 Access denied` until then). Once granted, flip the descriptor to active. |
| `source.gdelt.bigquery` | GCP service account | descriptor-defined, **dormant** — no creds. |
| `source.mediacloud.world` | MediaCloud API key | descriptor-defined, **dormant** — no creds. |
| `source.opensanctions.api` · `.bulk` | API key / bulk download | descriptor-defined, **dormant**. |
| `source.intelmq.cisa_kev` | IntelMQ collector + Redis bridge | descriptor-defined, **dormant** — needs IntelMQ infrastructure, not just a key. |
| `source.reliefweb.reports` | keyless, but `appname` must be **approved** by ReliefWeb | descriptor-defined, **dormant** — would `403` until the appname is approved. |

The takeaway: of the credentialed tier only **Telegram** is fully productive;
**GDELT-DOC** is wired+active (rate-limit-pending); **ACLED** is code-ready and
auth-validated but blocked on ACLED-side account authorization; the rest ship as
ready descriptors awaiting creds/infra.

---

## 4. How to register them

Registration is a deploy-time step, not a code change — the live source set is
the `source_descriptors` **DB rows**, not the `descriptors/*.yaml` files.

- **Minimal (Tier 1).** The working-set bring-up
  (`scripts/bringup_register_p17_workingset.py`) registers the 3 shared RSS
  sources alongside the G20 targets, analysts, and action packs. A fresh deploy
  that stops here has exactly 3 feeds.
- **Full catalog (Tier 2) — the step a fresh operator misses.** Run
  `scripts/bringup_register_source_catalog.py` to register all 46. It is
  idempotent and supports `--verify` (a live HTTP probe that registers nothing).

The exact commands (image-mode vs. repo-mounted-container idioms, the
`LEGBA_DATA_PG_DB=legba` env pin, and the load-bearing order — migrate → register
stack → register working set → **register the catalog** → boot the runtime) are
in **`SETUP.md`** (the from-zero bootstrap guide) and **`RUNBOOK.md`** (operator
runbook). This doc does not duplicate them.
