# Legba — Data Sources

This doc catalogs Legba's data sources: how many there are and at what scope
(the three-tier model, the 46-source repo catalog, and the breadth batches
layered on top of it — the live active count is generated in
[RELEASE_STATE.md](RELEASE_STATE.md), not repeated here), the fifteen source-handler
kinds a descriptor can use, the editorial `source_class` taxonomy a descriptor
declares, and how to register a source or add a new handler
kind. It is written for operators deciding what to register and for developers
adding a handler. For how a feed becomes a canonical signal (actor → baseline
enrichment → publish) see `ACQUISITION.md`; for the exact registration
commands see `SETUP.md` and `RUNBOOK.md`.

**Contents**

- [1. The three-tier scope model](#1-the-three-tier-scope-model)
- [2. The full catalog (46 sources)](#2-the-full-catalog-46-sources)
- [3. Credentialed sources: live status](#3-credentialed-sources-live-status)
- [4. Registering sources (reaching full scope)](#4-registering-sources-reaching-full-scope)
- [5. The Signal a handler produces](#5-the-signal-a-handler-produces)
- [6. Acquisition modes: poll vs push](#6-acquisition-modes-poll-vs-push)
- [7. Implemented source kinds](#7-implemented-source-kinds)
- [8. Cost-tier summary](#8-cost-tier-summary)
- [9. Source credibility scoring](#9-source-credibility-scoring)
- [10. NER and relation-extraction backend](#10-ner-and-relation-extraction-backend)
- [11. Adding a new source kind](#11-adding-a-new-source-kind)
- [12. Future seams (not yet working)](#12-future-seams-not-yet-working)

---

## 1. The three-tier scope model

"How many sources?" has three honest answers, because three different things
are being counted. Anchor every scope claim to these:

| Tier | Count | What it is | Where it comes from |
|---|---|---|---|
| **1 — Minimal cold-start** | **3** shared RSS | The smallest loop that proves the path from empty volumes: BBC World, Al Jazeera World, Deutsche Welle. This is *all a fresh deploy gets* if it stops at the documented working-set. | `scripts/bringup_register_sources.py` (standalone 3-source registrar) and the working-set script `scripts/bringup_register_p17_workingset.py` (RUNBOOK §7) |
| **2 — Full repo catalog** | **46** sources | The full catalog of independently-verified, no-auth feeds: **43 `rss` + 3 `geojson`** hazard feeds. **NOT auto-run on deploy and NOT part of the working-set bring-up** — a separate manual step a fresh operator currently misses. Running it is how you reach current/full scope. | `scripts/bringup_register_source_catalog.py` (the `CATALOG` tuple — 46 `CatalogEntry`, owner `s1_catalog`) |
| **3 — Live-productive** | **~117** sources (moving) | The real "productive scope" of a representative running deployment: distinct `source_id` values that have actually emitted signals — the catalog sources **plus** the operator-pinned standalone descriptors (state-media, Telegram, …; §2.3) **plus** the activated breadth batches (§2.5–§2.7) **plus** seed / world-baseline curated adapters. Exceeds the active-registered count because retired and paused feeds keep the signals they already produced. | Live reconcile against the production `signals` table |

Two adjacent counts round out the picture (neither is a different set of
*feeds* — they are registry / fan-out bookkeeping):

- **105 registered head source descriptors** — distinct non-autowired **active**
  head `source_descriptors` rows in the registry (the catalog plus the
  operator-pinned `descriptors/source_*.yaml`, the activated breadth batches and
  seed sources; a further 9 are registered but **paused** and 9 **retired**,
  which is why the active-registered count and the live-productive count
  differ). **This number moves** — it is generated
  from the live registry, never hand-maintained here: see
  [RELEASE_STATE.md](RELEASE_STATE.md) for the current breakdown by kind.
- **Autowired per-target fan-out templates** (`src_autowire*` / `src_tmpl*`) —
  these are **generated, not hand-authored** feeds, materialized on demand by the
  discovery/auto-wire machinery; they are not new upstreams, and this reference
  deployment currently materializes **none** (fan-out rides the shared NATS
  subjects instead — one published BBC feed fans out to the G20 targets without
  re-fetching anything; see `ACQUISITION.md` §6). Do **not** count them as ingest
  sources.

> **Why three numbers.** (The live ACTIVE count is a fourth, moving number —
> generated, not documented here: see [RELEASE_STATE.md](RELEASE_STATE.md).)
> A fresh deploy that stops at the documented
> working-set gets **only 3 RSS feeds** — the minimal cold-start verification
> set, not the catalog and not the live scope. The full 46-source catalog
> lives in a separate, manually-run registration script that the working-set
> bring-up does **not** invoke (§4). A review that sees "only 3 RSS feeds" is
> reading the cold-start set.

**The one-line answer.** *3 minimal · 46 catalog · a moving live scope* — the
active-registered head-descriptor count is generated, never hand-typed here
(105 at the last [RELEASE_STATE.md](RELEASE_STATE.md) regeneration; no
autowired fan-out templates materialized in this deployment). The fix for the "only 3 feeds" state is to
register the 46-source catalog (§4), then the breadth batches (§2.5–§2.7) an
operator activates deliberately.

---

## 2. The full catalog (46 sources)

The catalog is **43 `rss` + 3 `geojson`** entries — every feed below was probed
live (HTTP GET + `feedparser` / GeoJSON parse) before inclusion. Each `rss`
entry runs the baseline enrichment chain `dedupe (tiers 1–2) → language_detect →
ner_multilingual → geocode` (plus `fact_extractor` on the four feeds flagged
below); `geojson` is **geocode-only** by default, except NWS, which opts into
`language_detect + ner_multilingual` (its alerts carry rich English headline /
description text). Registration mechanics are in §4.

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

### 2.3 Pinned standalone sources — state-media and UCDP

Beyond the 46-entry catalog, a handful of **operator-pinned** `descriptors/source_*.yaml`
sources are registered individually (the same standalone path ACLED / Telegram
use), NOT via the catalog script — so they are counted in the live-productive /
registered tiers of §1 but are **not** part of the "46 catalog". Three
**state-media** RSS feeds and one **UCDP** conflict-event feed landed with the
bounded-unit waves:

| Source id | Kind | Auth | `source_class` | Live status |
|---|---|---|---|---|
| `source.irna.english` | `rss` | none | `state_media` | **Active** — polls healthy (empty windows in the observed period). Islamic Republic News Agency (Iran). |
| `source.presstv.english` | `rss` | none | `state_media` | **Paused** (was active). Press TV (Iran, English). The descriptor records no pause reason — read it as an operator disposition, not a documented verdict on the feed. |
| `source.ukrinform.english` | `rss` | none | `state_media` | **Active** — registered and polling (has hit transient feed errors). Ukrinform (Ukraine state wire). |
| `source.ucdp.ged` | `ucdp` (§7.15) | free access token | `analysis` | **RETIRED** (live head `state='retired'`). The descriptor shipped against a no-auth public GED API; upstream had introduced a token, so the single poll it ever made returned **`401 Unauthorized`**. Token auth landed in the handler the same morning, but rather than leave a credential-less feed registered it was retired outright — one poll, one 401, zero signals, ever. The handler kind (§7.15) remains built and token-ready; obtaining a token and re-registering is the path back (§7.15). |

The state-media feeds exist precisely so the `narrative_coordination` unit (and
the `source_class` weighting, §2.4) can read them as **framing** — an
official-position signal, LOW-tier for establishing facts — rather than as
neutral reporting. The **two Ansar Allah / Houthi channels** added with the
supply-chain wave (§2.7) belong to the same doctrine and are pinned `state_media`
by the per-channel override in §2.4 rather than by their descriptor's class.
**Honest note:** productive signal volume on this class is thin
(IRNA/Press TV/Ukrinform windows have largely been empty or transient-error in
the observed period), Press TV is now paused, and UCDP has been retired outright
as noted above.

### 2.4 The `source_class` editorial taxonomy

Every `SourceDescriptor.scope` now carries a **`source_class`** field
(`src/legba/data/schemas/source.py`, `SourceClass` literal) — the source's
**editorial class**, so the analysis plane can weight a claim by *what kind of
source made it*, not just by host credibility (§9). It is one of four values,
defaulted to the conservative `reporting` bucket so every pre-existing descriptor
still validates unchanged:

| `source_class` | Meaning | Examples |
|---|---|---|
| `reporting` | Straight news / wire reporting (the default) | BBC, Deutsche Welle, Al Jazeera, GDELT, MediaCloud, Telegram |
| `analysis` | Research / data projects and coded-event datasets | ACLED, UCDP |
| `official` | Primary government / IGO sources | USGS, OpenSanctions, CISA KEV, ReliefWeb (UN OCHA) |
| `state_media` | State-controlled outlets — read as *framing*, not fact | IRNA, Press TV, Ukrinform |

The `narrative_coordination` unit weighs `state_media == framing`, and the
`collection_gap` I&W analyst names which `source_class` would plausibly feed a
starved desk × dimension cell (see `ANALYSIS.md` §3.11). The class is also what
the hourly `signal_salience` sweep turns into a signal's deterministic
`authority` rank (`official` 4 · `reporting` 3 · `analysis` 2 · `state_media` 1)
— never model-chosen.

**Per-channel override (multi-publisher descriptors).** One `source_class` per
descriptor is the right granularity for a feed, and the wrong granularity for a
descriptor that fronts *many* publishers. The Telegram monitor is one descriptor
carrying 28 channels; classing the whole thing `reporting` would launder a state
outlet riding alongside Bloomberg and the Guardian into neutral reporting. So a
`telegram_channel` descriptor may carry an optional **`config.classes`** map —
channel handle → `source_class` — and `signal_salience` prefers that override
over the descriptor's own class when stamping `authority`. Two channels use it
today, both official Ansar Allah media pinned to `state_media` (§2.7), which
demotes them from authority rank 3 to rank 1.

Three properties make it safe rather than another place for truth to drift:
the map is **choice-locked** to the `SourceClass` vocabulary, so an
off-vocabulary value fails at registration rather than silently degrading; a
validator **rejects any key that is not a configured channel**, so a typo or a
removed channel fails loud instead of quietly overriding nothing; and handles
are normalized identically (`@`-prefix and `telegram://` stripped) on both the
config side and the payload side, so a match cannot be missed on formatting.
Absent an override, the descriptor's own class applies unchanged.

### 2.5 The RSSHub lane (profile-gated — a starved-desk booster; all 10 now ACTIVE)

Many credible **regional / gov / major-media** outlets publish **no native
RSS** (or only a homepage feed), so the worst-covered watch desks stay thin no
matter how many global wires we poll. The **RSSHub lane** closes that gap
*without a new source kind*: a self-hosted **RSSHub** sidecar bridges a chosen
outlet **route** into a standard RSS feed the existing `rss` handler polls.

- **Machinery.** `docker-compose.yml` adds an `rsshub` service
  (`diygod/rsshub`, the non-chromium / puppeteer-less image) behind its **own
  profile `sources-extra`** — `docker compose up -d` and `--profile runtime`
  **never** start it. It is loopback-bound (`127.0.0.1:1200`), memory-capped
  (512 MB), and dependency-free by default (`CACHE_TYPE=memory`; it can be
  pointed at the substrate redis via env). The curated descriptors poll it over
  the compose network at `http://rsshub:1200/<route>`.
- **Ships inert; since activated.** The ten `descriptors/source_rsshub_*.yaml`
  feeds all ship **`state: draft`**, so bulk registration creates **no live
  actor** — even with the sidecar up, nothing polls until the operator activates
  each descriptor (`draft → configured → active`). All ten were subsequently
  **activated** in this deployment, so the lane is no longer inert here — and
  the five `apnews` topic-hub routes have since been **paused** (the operator's
  running curation; the RFI / Focus Taiwan / RFA / Al Jazeera routes plus
  `apnews.world` stay active). The descriptors on disk still read `draft`,
  which is the intended shape — the repo ships the safe default and the FSM
  records the operator's decisions. Registrar (idempotent, house pattern):
  `scripts/bringup_register_rsshub_sources.py`, which also seeds host-level
  `source_credibility` rows for the upstream outlets (the feed's article links
  are the *real* outlet URLs, so the credibility filter keys on `apnews.com` /
  `rfi.fr` / `focustaiwan.tw` / `aljazeera.com` / `rfa.org`, never on `rsshub`).
- **Egress.** The `rss` handler's SSRF egress guard blocks internal hosts, so
  activation also requires `rsshub` on the runtime's
  **`LEGBA_EGRESS_ALLOW_HOSTS`** allowlist (an exact-hostname permit, defaulted
  to `rsshub` on `legba-runtime-dapr`; inert until a draft is activated).
- **Curation (the five worst-covered non-G7 desks, chosen from a live 7-day
  signals-per-desk query).** Two feeds per desk — a top-tier English wire (AP
  News country hubs) plus a strong regional/local voice — all `source_class:
  reporting`; **no Chinese state media** (that is reserved for the CN desk,
  where it is knowingly labeled `state_media`):

  | Desk | Feeds (route → outlet) |
  |---|---|
  | **NE** Niger | `apnews/topics/niger` → AP · `rfi/fr/afrique` → RFI Afrique |
  | **TW** Taiwan | `apnews/topics/taiwan` → AP · `focustaiwan/news` → Focus Taiwan (CNA EN) |
  | **HT** Haiti | `apnews/topics/haiti` → AP · `rfi/fr/ameriques` → RFI Amériques |
  | **CD** DR Congo | `apnews/topics/democratic-republic-of-the-congo` → AP · `aljazeera/english/where/democratic-republic-of-the-congo` → Al Jazeera EN |
  | **KP** North Korea | `apnews/topics/north-korea` → AP · `rfa/english/news/korea` → Radio Free Asia |

  Deploy (operator-paced): `docker compose --profile sources-extra up -d rsshub`
  → `python scripts/bringup_register_rsshub_sources.py` (registers drafts +
  seeds credibility) → verify each route on the instance → activate per desk.

### 2.6 The Wave-A breadth batch (41 no-auth feeds — 38 now ACTIVE, 3 paused)

The **Wave-A** batch is the additive-breadth slice of the 2026-07-02/03
new-source research sweep (`planning/SOURCE_RESEARCH_2026-07-02.md`,
"Wave A — register this week (verified live, no auth)"). It adds **41
independently verified, keyless feeds** — **38 `rss` + 3 `json_api`** — with no
new source kind and no sidecar, straight onto the existing handlers. It is
**additive breadth, not revival**: the roster is otherwise healthy.

- **Ships inert; since activated.** Every `descriptors/source_*.yaml` in the
  batch ships **`state: draft`**, so bulk registration creates **no live actor**
  — nothing polls until the operator activates each descriptor (`draft →
  configured → active`) after verifying the route live on the instance. The three
  `json_api` feeds (WHO Disease Outbreak News, NHK World, ISW) carry field paths
  marked **VERIFY** in-descriptor — probe the live JSON and correct any before
  activating (a wrong path makes the handler emit nothing + report `unhealthy`;
  it never fabricates). **Live state in this deployment: 38 active, 3 paused**
  (`source.nhk.world_news` — one of the three `json_api` feeds — plus
  `source.spiegel.international` and `source.stategov.press_releases`; the
  descriptors record no pause reason, so treat a pause as an operator
  disposition, not a documented verdict on the route).
- **Registrar (idempotent, house pattern):**
  `scripts/bringup_register_wave_a_sources.py` — registers the 41 drafts and
  seeds host-level `source_credibility` rows for the upstream outlets
  (`INSERT .. ON CONFLICT DO NOTHING`, so operator overrides and any
  pre-existing seed — `eia.gov` / `state.gov` from the S-1 catalog — always win).
- **What's in it (by research section):**

  | Section | Count | Feeds |
  |---|---|---|
  | **A1** additive dead-feed complements | 3 | WHO Disease Outbreak News (json_api) · EIA Today in Energy · CGTN World |
  | **A2** desk-gap fills | 19 | Israel (Times of Israel, Jerusalem Post) · Taiwan (Taipei Times) · N.Korea (Daily NK, 38 North) · Japan (NHK json_api, Japan Times) · Brazil · Mexico · Argentina · Spain/LatAm (El País) · Gulf (Asharq Al-Awsat, Middle East Eye) · Indonesia (ANTARA) · Russia (Meduza) · RFE/RL · Australia · Canada · Italy |
  | **A3** topical / unit feeds | 13 | ISW (json_api) · State Dept press · UN press · Kremlin · EUvsDisinfo · DFRLab · Breaking Defense · Defense News · Naval News · OilPrice · Rigzone · World Nuclear News · Arms Control Association |
  | **A4** quality adds | 6 | Guardian World · Euronews · Le Monde EN · Der Spiegel Intl · Bangkok Post · Dawn |

- **State media / editorial labeling.** Only **`source.cgtn.world`** is
  state-controlled Chinese media — knowingly ingested for the **CN desk** as
  labeled `state_media` FRAMING (the house rule permits Chinese state media only
  for the CN desk). State-**funded** but editorially-conventional public
  broadcasters (NHK / ABC / CBC / RFE-RL / EBC-Agência Brasil / ANTARA) stay
  `source_class: reporting` with `state_affiliation = True` on the credibility
  row — the same honest-provenance / editorial-independence split §2.4 uses.
  Kremlin.ru is `official` (a government primary-source publisher) with a **low**
  credibility score.
- **Deliberately excluded** (documented in the registrar): the A1 pure
  re-points (WHO news, CDC travel/outbreaks, EIA press) are **already** registered
  in the S-1 catalog at their current endpoints; **EMSC FDSN** was retired
  2026-06-12 as duplicative-of-USGS noise; **Focus Taiwan/CNA** is already
  double-covered (`source.cna.all` + `source.rsshub.focustaiwan.news`); **OFAC
  SDN delta** is an XML two-step the generic handlers don't cover (sanctions ride
  `opensanctions.*`).

  Deploy (operator-paced): `python scripts/bringup_register_wave_a_sources.py`
  (registers drafts + seeds credibility) → verify each route on the instance
  (the `json_api` field paths especially) → activate per desk.

### 2.7 The supply-chain domain batch (8 registrations — 7 live RSS + a Telegram fold)

The collection half of the 2026-07-29 supply-chain wave: the feeds the
`disruption_status` unit and its thematic `lane_*` / `flow_*` desks need. Eight
registrations, of which seven are new `rss` descriptors that went **active**, and
the eighth is a Telegram descriptor that was registered and then deliberately
**retired** in favour of folding its channels into the existing monitor (below).
All seven carry `owner: supply_chain_top10` and a `supply_chain` scope tag, and
the registrar seeds host-level `source_credibility` rows the same
`ON CONFLICT DO NOTHING` way every other batch does.

| Source id | Feed URL | `source_class` | Scope tags (beyond `supply_chain`) | Notes |
|---|---|---|---|---|
| `source.pancanal.news` | `https://pancanal.com/en/feed/` | `official` | `maritime`, `canal`, `gov` | Panama Canal Authority (ACP) — the operator of the waterway, primary source for draft restrictions and slot auctions |
| `source.splash247.news` | `https://splash247.com/feed/` | `reporting` | `freight`, `logistics`, `maritime` | shipping trade press |
| `source.theloadstar.news` | `https://theloadstar.com/feed/` | `reporting` | `freight`, `logistics`, `maritime` | container/air-freight trade press |
| `source.maritimeexecutive.news` | `https://www.maritime-executive.com/articles.rss` | `reporting` | `maritime`, `shipping`, `news` | maritime industry reporting |
| `source.digitimes.news` | `https://www.digitimes.com/rss/daily.xml` | `reporting` | `semiconductor`, `technology`, `economic` | semiconductor supply chain — the `flow_semiconductors` desk's spine |
| `source.wto.news` | `https://www.wto.org/library/rss/latest_news_e.xml` | `official` | `trade`, `gov`, `economic` | IGO primary source for trade measures/disputes |
| `source.northernminer.news` | `https://www.northernminer.com/feed/` | `reporting` | `minerals`, `mining`, `economic` | critical-minerals production — feeds `flow_critical_minerals` |

Registrar: `scripts/bringup_register_supply_chain_sources.py` (idempotent, house
pattern). The trade-press cadences are 2-hourly on staggered minutes; the two
`official` feeds poll 6-hourly (they publish rarely, and polling a
low-volume IGO feed every two hours buys nothing but request count).

**Telegram channel additions — and why there is no second Telegram descriptor.**
Three channels joined the existing `source.telegram.org_channels` monitor
(25 → 28): `TankerTrackers` (vessel-tracking OSINT, left at the descriptor's
default `reporting`) and the two Ansar Allah / Houthi official media channels,
`Almasirah_En` and `ansarollah1`. A separate
`source.telegram.ansarallah_channels` descriptor *was* registered first and is
now **retired**: a second concurrent Telegram client on the same session
triggers an `AUTH_KEY_DUPLICATED` session kill, so one descriptor per session is
not a style preference but a protocol constraint.

That fold created a problem worth naming, because it is the reason the
`source_class` taxonomy grew a per-channel override (§2.4): the monitor
descriptor is `source_class: reporting`, and appending two state-media channels
to it would have laundered official Houthi media into neutral reporting for
every downstream consumer that reads the descriptor's class. Both channels are
therefore pinned `state_media` in the descriptor's `config.classes` map — the
doctrine §2.3 states for the state-media feeds applies unchanged here: these are
read as **framing**, an official-position signal, LOW-tier for establishing
facts, and deliberately ingested rather than excluded because the
`narrative_coordination` unit and the Red Sea lane desk both need to see what
the party to the conflict is *claiming*.

---

## 3. Credentialed sources: live status

The 46-source catalog exercises two handler kinds (`rss`, `geojson`); the full
set of fifteen kinds is documented in §7. The catalog is the **no-auth,
verified-live** subset that needs no credentials to register; the credentialed /
push kinds below need a per-deployment secret in the vault before they poll,
and each is registered by a per-source bringup. They are in **neither** the
working-set **nor** the no-auth catalog, so a fresh deploy lights up with zero
secrets and **none of these active** unless an operator opts each one in
(provision the secret in the vault, then register the descriptor). Status in
the reference deployment:

| Source (descriptor) | Auth | Status |
|---|---|---|
| `source.telegram.org_channels` | Telegram API (id/hash/session) | **Active** — re-authenticated 2026-07-16 with a fresh session, polling every 30 minutes (softened from 15). The live exemplar of a credentialed source. |
| `source.gdelt.doc_api` | none (free `json_api`) | **Retired 2026-08-02** (migration 0121), superseded by `source.gdelt.files`. It was never actually paused after the `429` incident — it stayed active and polled at an 84.3% error rate (273 errors / 326 polls / 229 signals over 7 days) while the file-dump successor did 8,974 signals at 92.3% success. Both share GDELT's per-IP rate limit, so running both cost the successor headroom. The `json_api` handler is unaffected and still exercised by the ReliefWeb example + the live probe. |
| ~~`source.acled.conflict`~~ | OAuth2 password grant | **Removed from the wired set (operator decision, 2026-07-16).** The account never received the portal data-API grant (reads 403'd; 0 signals ever), so the descriptor and its poll history were deleted and the seed entry removed. The OAuth2 handler + seed-adapter *machinery* remain in-tree; re-wiring is a registration away if access is ever granted. |
| `source.gdelt.bigquery` | GCP service account | descriptor-defined, **dormant** — no creds. |
| `source.mediacloud.world` | MediaCloud API key | descriptor-defined, **dormant** — no creds. |
| `source.opensanctions.api` · `.bulk` | API key / bulk download | descriptor-defined, **dormant**. |
| `source.intelmq.cisa_kev` | IntelMQ collector + Redis bridge | descriptor-defined, **dormant** — needs IntelMQ infrastructure, not just a key. |
| `source.reliefweb.reports` | keyless, but `appname` must be **approved** by ReliefWeb | descriptor-defined, **dormant** — would `403` until the appname is approved. |

The takeaway: of the credentialed tier only **Telegram** is fully productive;
**GDELT-DOC** is wired but currently `paused` (rate-limit-managed); **ACLED** has
been removed from the wiring entirely (operator decision — machinery kept); the
rest ship as ready descriptors awaiting creds/infra.

---

## 4. Registering sources (reaching full scope)

Registration is a deploy-time step, not a code change — the live source set is
the `source_descriptors` **DB rows**, not the `descriptors/*.yaml` files.

- **Minimal (Tier 1).** The working-set bring-up
  (`scripts/bringup_register_p17_workingset.py`) registers the 3 shared RSS
  sources alongside the G20 targets, analysts, and action packs. A fresh deploy
  that stops here has exactly 3 feeds.
- **Full catalog (Tier 2) — the step a fresh operator misses.** Run the catalog
  bring-up:

  ```
  python scripts/bringup_register_source_catalog.py           # register all 46
  python scripts/bringup_register_source_catalog.py --verify  # dry-run: probe feeds live, register nothing
  ```

  This registers the full **46-entry** `CATALOG` (43 `rss` + 3 `geojson`) and
  seeds host-level `source_credibility` rows (`ON CONFLICT DO NOTHING`, so
  operator overrides always win). The script is **idempotent** — re-runs report
  `unchanged` for already-current heads. Additional keyed kinds (GDELT, ACLED,
  MediaCloud, …) are then authored as individual `SourceDescriptor`s on top of
  this base (§3, §7).

The exact commands (image-mode vs. repo-mounted-container idioms, the
`LEGBA_DATA_PG_DB=legba` env pin, and the required order — migrate → register
stack → register working set → **register the catalog** → boot the runtime) are
in **`SETUP.md`** (the from-zero bootstrap guide) and **`RUNBOOK.md`** (operator
runbook). This doc does not duplicate them.

**Validated live scope** (a representative running deployment, point-in-time
reconcile against the production tables, 2026-08-10):

| Metric | Count |
|---|---|
| Distinct sources that have produced signals | **117** |
| Signals ingested | **140,258** |
| Analyst outputs (all kinds) | **41,912** |
| — of which findings | **19,682** |
| Facts | **41,247** |
| Nexuses | **17,690** |
| Situations | **89** |
| Hypotheses | **4,586** |

The ~117 live-productive sources = the 46 catalog integrations plus the
operator-pinned standalone descriptors (state-media, Telegram, …; §2.3), the
activated breadth batches (§2.5–§2.7), and the seed / world-baseline curated
sources — including retired/paused feeds that keep the signals they produced.

---

## 5. The Signal a handler produces

In Legba's source-first model, a **source owns acquisition**. A `SourceActor`
turns a `SourceDescriptor` into a running actor that either polls on a cadence
or receives a push, produces **one canonical, target-agnostic `Signal`**,
enriches it once (baseline language / geo / entity NER), and publishes it once
to NATS JetStream. Signals carry no `target_id` — they are observations, and the
fan-out plane routes each one to many subscribing targets by predicate. For the
descriptor schema see `src/legba/data/schemas/source.py`; for the acquisition
pipeline (actor → baseline → publish) see `ACQUISITION.md` and `DESIGN.md`; for
the predicate fan-out / subscription side see `DESIGN.md`.

Every handler — regardless of kind — yields the same `Signal` shape
(`src/legba/data/sources/_contract.py`). The handler fills only the
observation-side fields; the per-source baseline fills the rest:

- **Ownership / provenance** — `source_id`, `source_version`, `fetched_at`,
  `owner_tenant`. `produced_by_kind` is `"source"` for a raw row.
- **Modality-first** — `modality` (`text` / `image` / `audio` / `video` /
  `structured` / `binary`), `mime_type`, `media_ref` (a *reference*, never
  inlined bytes), `embedding_ref`.
- **Content** — open `payload` dict, `canonical_url`, `language_hint`,
  `raw_provenance`.
- **Indexed filter columns (set by baseline enrichment, not the handler)** —
  `language`, `geo`, `tags`, `entity_classes`, `source_credibility`. These are
  promoted to indexed columns on the `signals` table so subscription SQL +
  NATS subject pushdown can filter on them.
- **Dedup / lineage** — `content_hash`, `canonical_signal_id` (alias links,
  never destructive collapse), `derived_from`, `schema_uri`.

A handler MUST persist a **cursor** via `ctx.state_store` so a restart doesn't
re-pull the whole feed, and SHOULD treat `since` as a hint — downstream dedup
absorbs overlap windows.

---

## 6. Acquisition modes: poll vs push

`SourceDescriptor.acquisition` is `"poll"` or `"push"`
(`"stream"` is a documented future seam, not implemented). The two modes map to
two protocols in `src/legba/data/sources/_protocols.py`:

- **`PollSource`** — Legba *pulls* on a cadence. The `SourceActor` registers a
  Dapr **Reminder** from `descriptor.cadence.schedule`; on each fire it drains
  the handler's `pull(ctx, since)` async generator, runs the baseline, writes,
  and publishes. An active poll source must declare a `cadence.schedule`.
- **`PushSource`** — an upstream system *POSTs* events to Legba. The shared
  inbound-webhook router (`webhook_router.py`) receives the POST and hands the
  raw body + headers to the handler's `ingest(ctx, body, headers)`, which
  verifies + parses and yields the same `Signal` shape. Push sources never poll.

A third, **orthogonal** capability — `ProvisioningSource` — lets a poll *or* push
source register an outbound upstream watch at activation (`on_activate`) and
deregister at retirement (`on_retire`), reconciled idempotently via
`src/legba/data/sources/provision.py`.

Every concrete handler declares class-vars `kind`, `family = "source"`,
`schema_version`, and `config_schema` (a Pydantic model used for
descriptor-validation-time config parsing).

---

## 7. Implemented source kinds

The runtime discovers handlers by walking `legba.data.sources` and collecting
`kind → handler class` pairs (`src/legba/runtime/source_factory.py`, the
`_SOURCE_MODULE_TABLE`). **Fifteen**
first-party kinds are registered today. Credential references in config are
**vault references** (a dotted credential id), never the raw secret — the
runtime resolves them at call time and the handler never caches them past a
single pull.

### 7.1 `rss` — RSS 2.0 / Atom 1.0 feeds

*Poll · free · `RSSSourceHandler` (`rss.py`)*

Fetches any standards-conformant RSS/Atom feed via `httpx`, parses with
`feedparser`, and yields one signal per entry published after the cursor.
Honors conditional GET — stores `(ETag, Last-Modified)` in `state_store` and
sends `If-None-Match` / `If-Modified-Since`; HTTP 304 yields an empty pull.

**Key config (`RSSConfig`)** — `url` (required), `parser`
(`auto` | `rss` | `atom`), `user_agent`, `timeout_seconds`.

The broadest, most permissive surface: works against any feed, no auth. It is
the kind used for the 3-feed minimal cold-start set (BBC / Deutsche Welle /
Al Jazeera) and for 43 of the 46 catalog integrations (§2).

### 7.2 `gdelt_query` — GDELT 2.0 via BigQuery

*Poll · free tier (GCP-billed scan) · `GDELTBigQuerySourceHandler` (`gdelt.py`)*

Queries the public GDELT `events` / `gkg` tables in BigQuery. Pushes filters
(country, actor, event class, tone) and a date partition prune down into the
storage layer so scans stay small. 100+ languages, ~15-minute refresh,
CAMEO/FIPS event coding.

**Key config (`GDELTConfig`)** — `bq_credentials_secret` (vault ref to the GCP
service-account JSON), `bq_project_id`, `bq_location` (default `US`);
filters `cameo_country` (FIPS-10-4 two-letter), `actor_filter` (regex),
`event_root_codes` (CAMEO), `tone_filter` (min/max `AvgTone`); `lookback_minutes`
(default 15).

**Cost control is first-class.** Every pull does a BigQuery **dry-run** estimate
and refuses to execute if it exceeds `cost_cap_bytes_per_pull` (default 1 GiB;
hard ceiling 10 GiB). An optional `daily_cap_bytes` tracks cumulative bytes in
`state_store` and refuses pulls that would blow the rolling-day budget;
`max_rows_per_pull` caps the `LIMIT`. Over-cap raises `CostCapExceeded`.

### 7.3 `acled` — ACLED conflict / protest events

*Poll · free for non-commercial use · `ACLEDSourceHandler` (`acled.py`)*

Paginates the ACLED REST API at `api.acleddata.com/acled/read`. ACLED requires
both an API key **and** the registered email for rate-limit attribution.

**Key config (`ACLEDConfig`)** — `api_key_secret` (vault ref), `email`
(required); filters `country` (ISO-3166-1 alpha-3), `event_types` (validated
against the ACLED taxonomy), `region` (validated against ACLED regions);
`lookback_days` (default 7 — ACLED publishes weekly), `page_size` (≤ 5000).
Cursor tracks the highest `event_date` seen; same-day overlap is resolved by
downstream dedup on `external_id`.

ACLED's license is non-commercial (journalists / NGOs / academia); a commercial
license is required for business use.

### 7.4 `mediacloud` — Media Cloud open-news corpus

*Poll · free tier · `MediaCloudSourceHandler` (`mediacloud.py`)*

Pulls stories from the Berkman-Klein Media Cloud corpus (billion-story scale,
worldwide) over its v4 HTTP `search/story-list` API via `httpx` (the handler
deliberately does **not** take the synchronous upstream `mediacloud` client, to
keep the actor's event loop non-blocking).

**Key config (`MediaCloudConfig`)** — `api_key_secret` (vault ref), `query`
(Elasticsearch-style query-string DSL), `collections` (numeric collection ids),
`language` (ISO-639-1), `lookback_days` (default 1, floor on the cursor window),
`page_size` (≤ 1000); `fetch_missing_text` pulls article body when the response
omits it. 429s are retried with backoff (`rate_limit_*` knobs); exhaustion
raises `MediaCloudRateLimited`.

### 7.5 `opensanctions` — sanctions / PEPs / criminal lists

*Poll · free (bulk) or paid (API) · `OpenSanctionsSourceHandler` (`opensanctions.py`)*

Reaches the OpenSanctions consolidated entity datasets (sanctions targets,
politically-exposed persons, criminal lists) over three operator-chosen modes.
Entities follow the FollowTheMoney schema; the `followthemoney` package is an
optional dep with a pass-through fallback when absent.

**Key config (`OpenSanctionsConfig`)** — `mode`:

- `bulk_csv` (default, **no auth**) — full dataset from
  `data.opensanctions.org/datasets/latest/<dataset>/targets.simple.csv`.
- `api` — low-volume drill-down against `api.opensanctions.org`; requires
  `api_key_secret`.
- `self_hosted` — license-compliant high volume; requires `base_url`.

Plus `dataset` (e.g. `all` / `sanctions` / `peps` / `us_ofac_sdn`),
`schema_filter` (FollowTheMoney schemas to keep), `api_page_size`,
`max_bulk_rows` (cap per bulk pull for incremental backfill). The OpenSanctions
`topics` controlled vocabulary (`sanction`, `role.pep`, `crime`, …) is preserved
on the signal payload for downstream filters.

### 7.6 `scraper` — generic pluggable web scraper

*Poll · free (proxy costs optional) · `ScraperSourceHandler` (`scraper.py`)*

A generic crawler that owns rate-limiting, robots.txt, BFS depth, and proxying,
delegating site-specific URL discovery + extraction to a drop-in **scraper impl**
referenced by dotted path. Depends only on `httpx` + `trafilatura` +
`feedparser`. An example impl ships at
`legba.data.sources.scrapers.example_news:ExampleNewsScraper`.

**Key config (`ScraperConfig`)** — `impl` (dotted path `pkg.mod:Class`),
`seed_urls`, `max_depth` (0–10), `rate_limit` (e.g. `"10/min"`, enforced by a
sliding-window token bucket), `respect_robots` (per-host robots cache,
fail-open per RFC 9309), `request_timeout_seconds`, `user_agent`; optional
`proxy_pool` (a `StackRef` to a residential-proxy pool component) and
`proxy_country`.

### 7.7 `firecrawl` — AI-friendly URL extraction

*Poll · paid (credit-based) · `FirecrawlSourceHandler` (`firecrawl.py`)*

Wraps the Firecrawl REST API (`api.firecrawl.dev`) over `httpx` with
`Authorization: Bearer`. Distinct from `scraper`: the cleaned, LLM-consumable
**markdown is the product**. Mode-dispatched (`scrape` single-page sync,
`crawl` async job polled to completion, `map` URL discovery).

**Key config (`FirecrawlConfig`)** — `api_key_secret` (vault ref), `seed_urls`,
`mode` (`scrape` | `crawl` | `map`), `extract_format` (`markdown` | `html` |
`links` | `screenshot`), `max_depth`, `include_paths` / `exclude_paths`,
`crawl_limit`, `crawl_max_polls` / `crawl_poll_interval_seconds`. Emits a
`CreditUsageRecord` per pull for the budget ledger. 401/403 → `FirecrawlAuthError`
(hard); 429 → `FirecrawlRateLimited` (retryable).

### 7.8 `telegram_channel` — Telegram channels

*Poll · free (account-bound) · `TelegramChannelSourceHandler` (`telegram.py`)*

Reads public Telegram channels via Telethon (a user-session, MTProto client).
`on_activate` connects the client; `iter_messages` is paginated per channel.
Media metadata only (type / mime / size) is attached when `include_media` is
set — **bytes are never downloaded** by this handler; downstream analysts fan
out for OCR / transcription. `FloodWaitError` is honored inline up to a cap.

**Key config (`TelegramChannelSourceConfig`)** — `api_id_secret`,
`api_hash_secret`, `session_secret` (all vault refs; the base64 Telethon session
is generated out-of-band), `channels` (handles or numeric ids),
`lookback_hours` (default 24), `include_media`, `per_channel_message_limit`
(≤ 1000), retry / backoff / `flood_wait_cap_seconds`.

### 7.9 `discord_webhook` — Discord (inbound)

*Push · free · `DiscordWebhookSourceHandler` (`discord.py`)*

A **push** source: Discord POSTs interaction / message events to a registered
webhook; the shared router verifies the request and hands it to `ingest`.
Every request's **Ed25519 signature is verified** (PyNaCl) against the
application's public key before any event is emitted; a bad signature raises and
the router returns 4xx. Interaction `PING`s are answered without producing a
signal.

**Key config (`DiscordWebhookConfig`)** — `application_id` (stamped on every
signal for provenance), `public_key_secret` (vault ref → hex Ed25519 public
key), optional `allowed_event_types` whitelist (verification still runs on
filtered events; non-listed events simply aren't emitted).

### 7.10 `common_crawl_news` — Common Crawl CC-NEWS

*Poll · free (anonymous S3 read) · `CommonCrawlNewsSourceHandler` (`common_crawl.py`)*

Streams WARC records from the public Common Crawl CC-NEWS bucket
(`s3://commoncrawl/crawl-data/CC-NEWS`, `us-east-1`) via `aiobotocore` —
anonymous read, no auth. Primarily a historical-backfill / bulk-coverage source
(~daily partitions over a large news-site set).

**Key config (`CommonCrawlNewsConfig`)** — `s3_bucket` / `s3_region` /
`s3_endpoint_url` (override for a MinIO/LocalStack mirror) / `prefix`,
`lookback_days`, `language_filter` (ISO-639-3, matched against the WARC
content-language header), `host_filter` (regex against hostname, e.g. country
scoping), and hard caps `max_records_per_run` / `max_warc_files_per_run` /
`max_body_bytes` (each WARC is ~1 GB; the runtime re-invokes `pull` to continue
from the cursor).

### 7.11 `intelmq_collector_bridge` — IntelMQ collector bots

*Poll · free · `IntelMQCollectorBridge` (`intelmq.py`)*

Bridges IntelMQ's CERT-grade catalog of 200+ collector bots into Legba so we
reuse them rather than re-implementing feed collectors. Hooks the IntelMQ
Collector → Parser boundary: collector bots emit IDF (IntelMQ Data Format)
events, which the bridge translates to `Signal`s (full IDF preserved under
`payload['idf']`; geo / ASN / actor hints lifted to top-level slots).

**Key config (`IntelMQBridgeConfig`)** — `mode`:

- `subprocess` — runs `python -m <bot_module>` one shot per pull and reads
  JSON events from stdout. Requires the `legba[intelmq]` optional extra.
- `redis_pipe` — drains an IntelMQ Redis destination queue (`LPOP`/`RPOP`).
  Uses the base Redis client; does **not** require the extra.

Plus `bot_module`, opaque `bot_config` (validated by IntelMQ, not Legba), the
`intelmq_redis_*` connection block (`redis_pipe` mode), `subprocess_timeout_s`
(`subprocess` mode), and `max_events_per_pull`. IntelMQ is a heavy optional
dependency, imported lazily; enabling the kind without the extra raises
`IntelMQNotInstalled`.

### 7.12 `generic_webhook` — reference inbound webhook

*Push · free · `GenericWebhookSourceHandler` (`generic_webhook.py`)*

The minimal reference **push** kind, with no external dependency. An upstream
POSTs an arbitrary JSON body; the handler emits one signal. Optional minimal
auth via a constant-time `shared_secret` check against the `X-Webhook-Token`
header.

**Key config (`GenericWebhookConfig`)** — `shared_secret` (optional),
`modality` (stamped on the signal, default `structured`), `id_field` /
`url_field` (payload keys for external id + canonical URL), optional
`media_ref_field` (a payload key whose value becomes the signal's `media_ref`).

### 7.13 `json_api` — generic polled JSON/CSV HTTP API

*Poll · free (API-dependent) · `JsonApiSourceHandler` (`json_api.py`)*

The generic **poll** counterpart to `generic_webhook`: reaches any read-only
HTTP API that returns a JSON document (or CSV table) containing an array of
items — ReliefWeb, GDELT DOC 2.0, CKAN portals, status feeds — without a
bespoke handler per provider. The poll window is cursor-driven
(`state_store["json_api_cursor"].last_pulled_at`); `url_template` placeholders
`{date_today}` / `{date_yesterday}` / `{window_start_iso}` / `{window_end_iso}`
are substituted (URL-quoted) from that window. Items are located via a small
dot/bracket JSONPath-lite resolver (no new deps; `a.b`, `a[0]`, `a['k']`) and
mapped to signals through configurable field paths. Items timestamped at or
before the window start are skipped; unstamped items flow through and
downstream content-hash dedupe absorbs overlap.

**Key config (`JsonApiConfig`)** — `url_template` (required; GET only),
`response_format` (`json` | `csv` via stdlib `csv`), `items_path`, field
mappings `id_path` / `title_path` / `url_path` / `timestamp_path` /
`body_path` / `geo_path`, `modality` (`text` | `structured`), optional `auth`
(`mode` header|query, `name`, `secret_ref` vault ref, `value_template` e.g.
`"Bearer {secret}"`), `lookback_minutes` (first-run window),
`max_items_per_pull` (default 100 — the source plane's per-poll bound),
`static_tags`, `language`.

**Fail-loud auth.** When `auth` declares a `secret_ref` and no secrets
resolver is wired, `on_configure` / `on_activate` / `pull` raise
`JsonApiAuthNotConfigured` and `health_check` reports `unhealthy` — a keyed
descriptor never polls unauthenticated. The resolved secret is applied as a
separate request header/param, so the rendered URL stamped into
payload/provenance never contains it. Example descriptors:
`descriptors/source_reliefweb_api.yaml` (keyless, ReliefWeb `appname`
convention) and `descriptors/source_gdelt_doc_api.yaml` (keyless GDELT DOC
2.0 — the no-billing GDELT alternative).

### 7.14 `geojson` — GeoJSON / GIS documents (model-free, structured)

Polls a configurable GeoJSON (RFC 7946) document URL and emits
`modality="structured"` Signals (`mime_type="application/geo+json"`) with
geometry-derived `geo`. No ML model is in the loop — this is the model-free
structured/GIS path (the bundled USGS-earthquakes descriptor is the example).
The live geo view is the separate v4 Leaflet Dockview panel
(`MapPanel` → `LeafletWorldMap`), which renders geometry from the substrate; the
inline `application/geo+json` modality renderer remains a badged placeholder
(SEAMS #13) and is not the path used today.

Three of the 46 catalog entries are `geojson` (USGS `significant_week`, NWS
`severity=Severe,Extreme` alerts, NASA EONET `days=3`); the other 43 are `rss`
— see §2 for the full catalog table and §4 for the bring-up script.

### 7.15 `ucdp` — UCDP Georeferenced Event Dataset (conflict events)

*Poll · free · `UCDPSourceHandler` (`ucdp.py`)*

Polls the Uppsala Conflict Data Program **Georeferenced Event Dataset (GED)** —
global organized-violence events (state-based / non-state / one-sided), one row
per lethal event with geo (lat/long + country + admin), the two conflict sides,
violence type, and best/high/low fatality estimates. Each structured record
yields one `Signal`; enrichment is `geocode`-only (the records carry coordinates
already). The bounded-unit waves added it as a conflict-event feed for the
`escalation` and `military_posture` units — a UCDP counterpart to the `analysis`-class ACLED.

**Key config (`UCDPConfig`)** — `version` (GED dataset release string, e.g.
`"24.1"`, or a candidate release for the ~monthly Candidate Events Dataset),
`lookback_days` (default 365 — GED is a batch, not a stream). The cursor tracks a
`StartDate` high-water mark; external-id + content-hash dedupe absorb the overlap.

**Auth — a free access token, sent as a header.** GED lives at
`https://ucdpapi.pcr.uu.se/api/gedevents/{version}`. It was originally a
public, no-auth endpoint and this section used to say so; UCDP has since
introduced a free, registration-gated access token (to deter bot traffic) that
rides **every** request as `x-ucdp-access-token`. The descriptor carries
`config.access_token_secret` as a **vault ref** (`source.ucdp.access_token`,
never plaintext), with `LEGBA_UCDP_ACCESS_TOKEN` as an env fallback for a quick
bring-up. With no token resolvable the handler **skips the pull entirely** — no
HTTP, no 401 spam — and records `ucdp: no token configured`. **LICENSE:** UCDP
data is CC BY 4.0 (free reuse *with* attribution — attribute "Uppsala Conflict
Data Program (UCDP)" on any re-export).

> **Honest live status — RETIRED, not silently broken.** The full history, since
> a review re-raised this as "401 on every poll": the source registered `active`
> on 2026-07-03 01:56 UTC against the then-correct no-auth assumption, made
> **exactly one** poll (04:00 UTC) which returned `401 Unauthorized`, and was
> paused at 07:33. The handler gained token auth and the clean no-token degrade
> at 07:36 — three minutes later — so the 401 is only reproducible by code that
> no longer exists. The descriptor was retired outright on 2026-07-28 (live head
> `state='retired'`); it has issued no request since. **One poll, one 401, zero
> signals, ever** — not an ongoing failure and nothing left to pause.
>
> **The blocker is external, not a defect.** To bring UCDP back an operator
> must request a token at <https://ucdp.uu.se/apidocs/>, store it as vault
> secret `source.ucdp.access_token`, re-register the current in-tree descriptor
> (which ships `state: draft` on purpose — draft descriptors register in bulk
> but get no live actor), and transition `draft → configured → active`. The
> exact calls are in the descriptor's own OPERATOR FLIP header.

---

## 8. Cost-tier summary

| Kind | Mode | Cost tier | Auth | Reaches |
|---|---|---|---|---|
| `rss` | poll | free | none | any RSS/Atom feed |
| `geojson` | poll | free | none | any RFC-7946 GeoJSON document URL |
| `gdelt_query` | poll | free tier (GCP scan-billed; capped) | GCP service account | GDELT 2.0, 100+ langs, ~15-min |
| `acled` | poll | free (non-commercial) | API key + email | ACLED conflict/protest events |
| `ucdp` | poll | free (CC BY 4.0) | free access token (`x-ucdp-access-token`) | UCDP GED organized-violence events |
| `mediacloud` | poll | free tier | API key | Media Cloud open-news corpus |
| `opensanctions` | poll | free (bulk) / paid (API) | none (bulk) / API key | sanctions / PEPs / criminal lists |
| `scraper` | poll | free (+ optional proxy cost) | none / proxy pool | arbitrary sites via drop-in impl |
| `firecrawl` | poll | paid (credit-based) | API key | arbitrary URLs → clean markdown |
| `telegram_channel` | poll | free (account-bound) | Telegram API id/hash/session | public Telegram channels |
| `discord_webhook` | push | free | Ed25519 public key | Discord interactions / messages |
| `common_crawl_news` | poll | free (anon S3) | none | Common Crawl CC-NEWS WARCs |
| `intelmq_collector_bridge` | poll | free | per-bot | IntelMQ's 200+ collector bots |
| `generic_webhook` | push | free | optional shared secret | any inbound JSON POST |
| `json_api` | poll | free (API-dependent) | none / vault header or query key | any JSON/CSV HTTP API with an item array |

---

## 9. Source credibility scoring

Source credibility is a **per-signal float in `[0.0, 1.0]`** annotated by the
`source_credibility` filter (`src/legba/data/filters/source_credibility.py`),
not by the source handler itself. It runs in the baseline enrichment chain and
is informational — it **flags but never drops** a signal.

How it works:

1. Extract a source host from the signal — preferring `canonical_url`, falling
   back to `payload["source_url"]` / `url` / `link`.
2. Normalize the host: lowercase, strip `www.`, drop user-info + port, decode
   punycode (IDN).
3. Look it up in the registry-backed `source_credibility` table (created in
   `0001_baseline.sql`). On a miss, retry
   against progressively-trimmed parent domains
   (`news.bbc.co.uk` → `bbc.co.uk` → `co.uk`); first hit wins. A small
   per-instance TTL'd LRU cache (default 3600 s) avoids re-querying the same
   host.
4. Stamp `source_credibility` + `source_credibility_rationale` on the signal;
   set `below_credibility_threshold = True` when the score is strictly below the
   configured `min_score` (default 0.3).

**Config (`SourceCredibilityConfig`)** — `min_score`, optional `default_score`
(applied to unknown hosts; otherwise they stay null), `cache_ttl_seconds`,
`cache_max_entries`. The table is operator-curated through the registry API
(`/api/v1/registry/source_credibility`,
`src/legba/data/registry/source_credibility_api.py`); the filter is read-only.

Per-target credibility **floors** are evaluated at subscription / read time (a
predicate over the stored score), not on the signal row — so the same scored
signal can be admitted by a lenient target and filtered out by a strict one.

---

## 10. NER and relation-extraction backend

The baseline NER + relation enrichment (`ner_multilingual`,
`fact_extractor`) calls the hosted NLP stack, whose relation-extraction model
is **GLiREL** (`jackboyla/glirel-large-v0`). GLiREL emits **real per-relation
confidence scores** (live facts span 0.75 / 0.80 / 0.92 / 0.95, with only a
small tail at exactly 1.0), not a synthetic constant. See `AI_MODELS.md` for
the full model inventory.

> Note: some in-repo `src/*.py` code comments still name an older "REBEL"
> backend; those comments are stale and a tracked code-cleanup follow-up
> (reconciling the conf-1.0 sentinel against GLiREL's real scores). The
> deployed backend is GLiREL.

---

## 11. Adding a new source kind

A source kind is a drop-in. There is no central registration list to edit beyond
one table entry; the runtime discovers handlers structurally.

1. **Write a handler class** in a new module under
   `src/legba/data/sources/`. Implement the `SourceHandler` protocol from
   `_contract.py` — declare the class-vars:

   ```python
   kind: ClassVar[str] = "my_kind"
   family: ClassVar[str] = "source"
   schema_version: ClassVar[str] = "legba/source.my_kind/1-0-0"
   config_schema: ClassVar[type[BaseModel]] = MyConfig
   ```

   No base class to inherit — the contract is a `runtime_checkable` Protocol;
   structural conformance is enough.

2. **Pick a mode.** For a poll source, implement
   `pull(ctx, since) -> AsyncIterator[Signal]` (satisfies `PollSource`); for a
   push source, implement `ingest(ctx, body, headers) -> AsyncIterator[Signal]`
   and set `push_source = True` (satisfies `PushSource`). Both also implement
   `async health_check(ctx) -> SourceHealth`. If the source needs an outbound
   upstream registration, also implement `on_activate` / `on_retire`
   (`ProvisioningSource`).

3. **Define a Pydantic `config_schema`** with `extra="forbid"`. Store
   credentials as vault references (a dotted credential id), never raw secrets —
   resolve them at pull time via `ctx.secrets_resolve` (or a constructor-injected
   resolver). Validate filters at config time so bad descriptors fail fast.

4. **Yield `Signal`s**, not enriched rows. Set the observation-side fields
   (`modality`, `payload`, `canonical_url`, `language_hint`, `media_ref` as a
   reference). Leave `language` / `geo` / `tags` / `entity_classes` /
   `source_credibility` to the baseline. Persist a cursor in `ctx.state_store`.

5. **Register the module** by adding one `(module_name, class_name)` tuple to
   `_SOURCE_MODULE_TABLE` in `src/legba/runtime/source_factory.py`. The factory
   defensively imports each module, so a missing optional dependency logs +
   skips that one kind without poisoning the registry, and inspects your
   `__init__` signature to thread in only the dependencies you declare (config,
   and `secrets_resolve` under any of the `secret_resolver` /
   `credential_resolver` / `secrets_resolve` parameter names).

Once registered, an operator can author a `SourceDescriptor` of that `kind`, the
`SourceActor` schedules it, the baseline enriches its signals, and the fan-out
plane routes them to subscribing targets — no other code changes.

---

## 12. Future seams (not yet working)

- **Eager media extraction** — `descriptor.pipeline.media = "eager"` wires a
  modality → `MediaExtractor` registry (`baseline.py`); production handlers
  (Whisper / VLM / OCR against the hosted endpoints, SeaweedFS object store) are
  thin today. Handlers attach `media_ref` references; bytes are not fetched.
- **`stream` acquisition** — `SourceDescriptor.acquisition` documents a third
  `"stream"` mode; only `poll` / `push` are implemented.
- **Source-side dedup tiers** — `content_hash` / `canonical_signal_id` carry the
  alias-link contract. Source-side ingest dedupe tiers 1–2 (canonical-URL then
  content-hash) now run at ingest (`data/filters/ingest_dedupe.py`); the periodic
  `cross_source_dedup` analyst still owns tiers 3–4 (semantic / temporal).
