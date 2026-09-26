# Data-source activation history — extracted from docs/DATA_SOURCES.md

Every dated registration, activation, pause, retirement, incident and
point-in-time measurement pulled out of `docs/DATA_SOURCES.md` during the docs
pass. The design-tier page now states the scope model, the kinds and the rules;
the sequence lives here. Newest first within each group. Ticket ids are defined
in `docs/TICKETS.md`.

---

## Batches, in the order they landed

### P-17 / the minimal cold start (2026-06-03)

- `scripts/bringup_register_p17_workingset.py` registered 3 shared RSS sources
  (BBC World, Al Jazeera World, Deutsche Welle) alongside the G20 targets,
  analysts and action packs. A bring-up that stopped there had exactly 3 feeds.
  This legacy working-set path was superseded by `deploy/deploy.sh`.
- The BBC descriptor's feed URL was probed live 2026-06-03 (HTTP 200, valid
  RSS 2.0).

### S-1 — the no-auth catalog (46 entries)

- `scripts/bringup_register_source_catalog.py`, owner `s1_catalog`: 43 `rss` +
  3 `geojson`, each probed live (HTTP GET plus a parse) before inclusion.
- `fact_extract = True` on four feeds only: `source.cna.all`,
  `source.npr.world`, `source.france24.english`,
  `source.economist.international`.
- NWS is the one `geojson` entry that opts into `language_detect +
  ner_multilingual` (`enrich_text=True`), because its alerts carry rich English
  headline and description text; the other two are geocode-only.
- **Probe-drops as of 2026-06-12** — probed and deliberately kept out, dead or
  blocked or discontinued: ReliefWeb, ProMED, Brookings, Nation Africa, DFAT
  Smartraveller, CISA, Kyodo, SIPRI, EMSC.
- The catalog became an automatic step of `deploy.sh` (phase 5, step `[4b]`)
  when the one-command bring-up landed; before that it was a standalone run.

### S1-T8 — state-media voices and the `source_class` field (2026-07-02)

- Three state-media RSS feeds pinned as standalone descriptors, feed URLs probed
  live 2026-07-02: `source.irna.english` (IRNA, Iran),
  `source.presstv.english` (Press TV, Iran), `source.ukrinform.english`
  (Ukrinform, Ukraine).
- `SourceScope.source_class` added as a four-value literal defaulted to
  `reporting`, so every pre-existing descriptor validated unchanged.
- Live dispositions recorded at the time: IRNA active but with empty windows in
  the observed period; Press TV **paused** with no recorded reason (an operator
  disposition, not a documented verdict on the feed); Ukrinform active with
  transient feed errors. Productive volume on this class was honestly thin.

### S1-T9 / UCDP (2026-07-03 → 2026-07-28)

- `source.ucdp.ged` registered `active` 2026-07-03 01:56 UTC against a
  then-correct no-auth assumption, as a conflict-event counterpart to ACLED for
  the `escalation` and `military_posture` units.
- It made **exactly one** poll (04:00 UTC), which returned `401 Unauthorized` —
  UCDP had introduced a free, registration-gated access token. Paused 07:33.
- Token auth and a clean no-token degrade landed in the handler at 07:36, three
  minutes later, so the 401 is only reproducible by code that no longer exists.
- Retired outright 2026-07-28 (live head `state='retired'`) rather than left
  registered without a credential. One poll, one 401, zero signals, ever.
- The path back: request a token at `https://ucdp.uu.se/apidocs/`, store it as
  vault secret `source.ucdp.access_token`, re-register the in-tree descriptor
  (which ships `draft`), and transition it.
- `LEGBA_UCDP_ACCESS_TOKEN` exists as an env fallback for a quick bring-up.

### A7 — the RSSHub lane (10 feeds)

- Curated from a live 7-day signals-per-desk query naming the five worst-covered
  non-G7 desks: Niger, Taiwan, Haiti, DR Congo, North Korea. Two feeds per desk —
  an AP News country hub plus a strong regional voice (RFI Afrique, Focus
  Taiwan, RFI Amériques, Al Jazeera English, Radio Free Asia).
- All ten shipped `state: draft` and were subsequently **activated** in this
  deployment; the five `apnews` topic-hub routes were later **paused** as
  operator curation, leaving the RFI / Focus Taiwan / RFA / Al Jazeera routes
  plus `apnews.world` active.
- House rule applied: no Chinese state media in this lane — that is reserved for
  the CN desk, where it is knowingly labeled `state_media`.
- The `rsshub` service was added to `docker-compose.yml` behind its own
  `sources-extra` profile (the non-chromium image), loopback-bound on
  `127.0.0.1:1200`, 512 MB capped, `CACHE_TYPE=memory`.
- Activation also required `rsshub` on `LEGBA_EGRESS_ALLOW_HOSTS` for
  `legba-runtime-dapr`, because the `rss` handler's SSRF egress guard blocks
  internal hosts.
- Registrar: `scripts/bringup_register_rsshub_sources.py`, which seeds
  credibility rows keyed on `apnews.com` / `rfi.fr` / `focustaiwan.tw` /
  `aljazeera.com` / `rfa.org`, never on `rsshub`.

### P3-6 — the Wave-A breadth batch (41 feeds, 2026-07-02/03)

- The additive-breadth slice of the 2026-07-02/03 source research sweep: 41
  independently verified keyless feeds — 38 `rss` + 3 `json_api` — on existing
  handlers, with no new kind and no sidecar. Owner `p3_6_wave_a`.
- Composition by research section: A1 additive dead-feed complements (3) — WHO
  Disease Outbreak News (json_api), EIA Today in Energy, CGTN World; A2
  desk-gap fills (19) — Israel, Taiwan, North Korea, Japan, Brazil, Mexico,
  Argentina, Spain/LatAm, Gulf, Indonesia, Russia, RFE/RL, Australia, Canada,
  Italy; A3 topical and unit feeds (13) — ISW (json_api), State Dept press, UN
  press, Kremlin, EUvsDisinfo, DFRLab, Breaking Defense, Defense News, Naval
  News, OilPrice, Rigzone, World Nuclear News, Arms Control Association; A4
  quality adds (6) — Guardian World, Euronews, Le Monde EN, Der Spiegel Intl,
  Bangkok Post, Dawn.
- The three `json_api` feeds carried field paths marked VERIFY in-descriptor, to
  be probed against the live JSON before activation.
- Live state recorded at the time: **38 active, 3 paused** —
  `source.nhk.world_news`, `source.spiegel.international`,
  `source.stategov.press_releases`, each with no recorded pause reason.
- Editorial labeling: only `source.cgtn.world` is state-controlled Chinese media,
  ingested for the CN desk as labeled `state_media` framing. State-*funded* but
  editorially conventional broadcasters (NHK, ABC, CBC, RFE/RL, EBC-Agência
  Brasil, ANTARA) stayed `reporting` with `state_affiliation = True` on the
  credibility row. Kremlin.ru is `official` with a low credibility score.
- Deliberately excluded and documented in the registrar: the A1 pure re-points
  (WHO news, CDC travel/outbreaks, EIA press) were already registered at their
  current endpoints; EMSC FDSN had been retired 2026-06-12 as
  duplicative-of-USGS noise; Focus Taiwan/CNA was already double-covered; the
  OFAC SDN delta is an XML two-step the generic handlers do not cover.

### The supply-chain domain batch (2026-07-29, 8 registrations)

- Seven new `rss` descriptors went active, all `owner: supply_chain_top10` with
  a `supply_chain` scope tag: `source.pancanal.news` (official),
  `source.splash247.news`, `source.theloadstar.news`,
  `source.maritimeexecutive.news`, `source.digitimes.news`,
  `source.wto.news` (official), `source.northernminer.news`.
- Trade-press cadences are 2-hourly on staggered minutes; the two `official`
  feeds poll 6-hourly, because polling a low-volume IGO feed every two hours
  buys nothing but request count.
- The eighth registration, `source.telegram.ansarallah_channels`, was registered
  and then **retired**: a second concurrent Telegram client on the same session
  triggers an `AUTH_KEY_DUPLICATED` session kill, so one descriptor per session
  is a protocol constraint, not a style preference.
- Three channels joined the existing `source.telegram.org_channels` monitor
  instead (25 → 28): `TankerTrackers` (left at the descriptor's default
  `reporting`) and the two Ansar Allah / Houthi official media channels
  `Almasirah_En` and `ansarollah1`.
- That fold is what produced the `config.classes` per-channel override
  (2026-07-29 Ansar Allah decision): the monitor descriptor is `reporting`, and
  appending two state-media channels to it would have laundered official Houthi
  media into neutral reporting for every downstream consumer. Both are pinned
  `state_media`, demoting them from authority rank 3 to rank 1.

### The 2026-08-03 batch — AP re-route and Niger coverage (6 feeds)

- Context (remediation roadmap row B-7): the five AP country-hub feeds froze
  upstream on 2026-07-28 and were paused; the freeze is in the `apnews.com/hub/*`
  pages themselves, so they could not be re-pointed.
- `source.rsshub.apnews.world` rides the same RSSHub sidecar on
  `/apnews/nav/world-news`, re-verified live 2026-08-03 by reading
  `datePublished` off six linked article pages. It declares `geo: []`, with each
  desk narrowing per signal on its own geo predicate.
- A read-only 7-day count on the live signals table (`'NE' = ANY(geo)`) returned
  29 rows across four sources, 14 of them from the frozen AP Niger hub — so real
  inflow was about 15 signals a week, with not one domestic Nigerien outlet
  registered.
- Five feeds answered that, each verified by direct fetch 2026-08-03 with status,
  item count, newest item date and Niger place-name density recorded in the
  descriptor header: `source.actuniger.politique`, `source.actuniger.societe`,
  `source.studiokalangou.news`, `source.sahelintelligence.news`, plus
  `source.france24.afrique`.
- Registrar: `scripts/bringup_register_source_batch_2026_08.py`.

### The 2026-09-07 batch — Burkina Faso and Mali coverage (6 feeds)

- `country_watch_bf` had had no desk runs since 2026-09-05 13:06Z because no
  signal had been tagged BF since 09-02; the whole 7-day corpus held 5 mentions
  of Burkina / Ouagadougou / Traoré. `country_watch_ml` had 6 geo-tagged signals
  in 4 days. Neither desk had a single domestically headquartered outlet
  registered — the existing Sahel roster was regional wire copy.
- The batch mirrors the Niger batch's shape exactly, one state press agency plus
  one high-volume independent plus one Fondation Hirondelle station per country:
  `source.aib.news`, `source.lefaso.news`, `source.studioyafa.news` (Burkina
  Faso); `source.amap.news`, `source.malijet.news`, `source.studiotamani.news`
  (Mali).
- Registrar: `scripts/bringup_register_source_batch_2026_09.py`.

---

## Credentialed sources — the dated record

- **Telegram** (`source.telegram.org_channels`) — re-authenticated 2026-07-16
  with a fresh session; polling softened from every 15 minutes to every 30. The
  live exemplar of a credentialed source.
- **GDELT DOC API** (`source.gdelt.doc_api`, `json_api`) — started 429-ing at
  the IP level even for small, spaced queries (verified 2026-07-21). **Retired
  2026-08-02** (migration 0121), superseded by `source.gdelt.files`. It was never
  actually paused after the 429 incident: it stayed active and polled at an 84.3%
  error rate (273 errors / 326 polls / 229 signals over 7 days) while the
  file-dump successor did 8,974 signals at 92.3% success. Both share GDELT's
  per-IP rate limit, so running both cost the successor headroom. The `json_api`
  handler itself was unaffected.
- **ACLED** (`source.acled.conflict`) — **removed from the wired set by operator
  decision 2026-07-16.** The account never received the portal data-API grant
  (reads 403'd; zero signals ever), so the descriptor and its poll history were
  deleted and the seed entry removed. The OAuth2 handler and the seed-adapter
  machinery remain in-tree; re-wiring is a registration away if access is
  granted.
- **Dormant, descriptor-defined, awaiting credentials or infrastructure** at the
  time of writing: `source.gdelt.bigquery` (GCP service account),
  `source.mediacloud.world` (API key), `source.opensanctions.api` and `.bulk`,
  `source.intelmq.cisa_kev` (needs IntelMQ infrastructure, not just a key),
  `source.reliefweb.reports` (keyless, but its `appname` must be approved by
  ReliefWeb — would 403 until then).

## Point-in-time measurements the old page carried

- **2026-08-10 reconcile** against the production tables of a representative
  running deployment: 117 distinct sources that had produced signals; 140,258
  signals ingested; 41,912 analyst outputs of which 19,682 findings; 41,247
  facts; 17,690 nexuses; 89 situations; 4,586 hypotheses.
- Scope tiers as stated at the time: 3 minimal cold-start · 53 registered by
  `deploy.sh` (52 active) · 46 catalog · ~117 live-productive · 105 active head
  source descriptors, with a further 9 paused and 9 retired · zero autowired
  fan-out templates materialized in this deployment.
- At the time migration 0192 was written: none of the 123 registered sources
  carried a `license_class`, `evidence_archive.license_class` was NULL across
  all 70,836 rows, and `SearchResult.license_class` was hard-`None` by design.
  `source_credibility` held 119 host rows.
- Source-credibility defaults quoted: `min_score` 0.3, cache TTL 3600 s.

## Corrections and stale references the old page recorded

- The relation-extraction backend is **GLiREL** (`jackboyla/glirel-large-v0`),
  which emits real per-relation confidence scores (live facts span 0.75 / 0.80 /
  0.92 / 0.95 with a small tail at 1.0). Some in-repo code comments still name
  an older REBEL backend; those comments are stale and a tracked cleanup,
  together with reconciling the conf-1.0 sentinel against GLiREL's real scores.
- The `geojson` section noted that the inline `application/geo+json` modality
  renderer is a badged placeholder (SEAMS #13) and not the path used today; the
  live geo view is the console's map panel rendering geometry from the substrate.
- Source-side ingest dedupe tiers 1–2 (canonical-URL, then content-hash) now run
  at ingest; the periodic `cross_source_dedup` analyst still owns tiers 3–4
  (semantic, temporal).
- Migration references the page carried: 0001 (the `source_credibility` table),
  0055 (fact contention), 0057 (`unit_reference_labels`), 0121 (the GDELT DOC
  retirement), 0189 (the closed read-event vocabulary), 0192 (the
  `source_credibility.license_class` host licence ledger).
- LIC-2 named `SourceScope.license_class` and the orthogonal flags plus a derived
  `public_ledger_ok` firewall bit as the rest of the build, not yet fielded.
- `source.gdelt.files` was introduced as a new kind (`gdelt_files`) rather than a
  config variant, because it is a genuinely different transport — HTTP file fetch
  plus zip plus CSV, no query language and no billing — from both the BigQuery
  handler and the retired DOC-API descriptor.
