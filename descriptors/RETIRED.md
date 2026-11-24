# Descriptors the bring-up does not register

`scripts/bringup_register_descriptors.py` registers every `descriptors/*.yaml` of
the four registry families (`action_pack`, `source`, `target`, `analyst`) plus
every entry of the source catalog (`profiles/geopolitics/rosters/source_catalog.yaml`),
EXCEPT the ones listed here. Each row is one descriptor; the walker parses the
table rows and nothing else, so keep the five columns.

* **retired** — retired on the reference host (or retired before it was ever
  registered). Terminal: the walker never registers it. If a live head with
  this id is NOT retired, the walker reports `would-retire` (and retires it on
  an apply run) — a `retired` row is the operator's word, so add one only on
  that word.
* **parked** — in the tree but never registered on the reference host (a
  draft that waits for an operator's activation, a legacy example, a
  template). The walker skips it. Turning one on = delete its row here, run
  the walker, then promote it through the lifecycle route.

`tests/test_bringup_accounts_for_live_heads.py` holds this file to the live
roster: no live non-retired head may sit in the `retired` rows without the
walker reporting it, and every tree descriptor whose head is retired on the
reference host must be listed.

Recorded 2026-10-04 (W6 SPLIT-1) from the live roster
`tests/fixtures/bringup/live_head_roster_2026-10-04.json`. No live head was
retired by this file: every `retired` row below was already retired live.

## Retired

| status | family | id | registrar | why |
|---|---|---|---|---|
| retired | analyst | country_assessor | descriptors/analyst_country_assessor.yaml | Operator decision 2026-07-01: the monolithic per-country one-pager; the bounded units → country_composition replaced it. |
| retired | analyst | country_critic | descriptors/analyst_country_critic.yaml | Operator decision 2026-07-02: it graded the retired country_assessor. |
| retired | analyst | country_predictor | descriptors/analyst_country_predictor.yaml | P3-T8 freeze: a reactive forecast-as-claim producer; forecasting returned as forecast_scoreboard. |
| retired | analyst | cross_correlator | descriptors/analyst_cross_correlator.yaml | Retired 2026-07-09: drifted into a coverage-gap detector over retired outputs, 0 consumers. |
| retired | analyst | fact_decay | descriptors/analyst_fact_decay.yaml | Retired live; tree reconciled 2026-09-06 (fact_decay_scan carries the job). |
| retired | analyst | geo_convergence_scan | descriptors/analyst_geo_convergence_scan.yaml | Retired live (POST /retire); tree reconciled 2026-09-06. |
| retired | analyst | hypothesis_lifecycle | descriptors/analyst_hypothesis_lifecycle.yaml | Superseded by competing_hypotheses (Piece C); its situation gate emitted 0 rows. |
| retired | analyst | meta_synthesizer | descriptors/analyst_meta_synthesizer.yaml | Operator decision 2026-07-02: superseded by the composition spine. |
| retired | source | source.gdelt.doc_api | descriptors/source_gdelt_doc_api.yaml | Retired 2026-08-02 (migration 0121); source.gdelt.files replaced it. |
| retired | source | source.ucdp.ged | descriptors/source_ucdp_ged.yaml | Retired live; tree reconciled 2026-09-06. |
| retired | source | source.bundeswehr.procurement | descriptors/source_bundeswehr_procurement.yaml | Retired 2026-11-14 (roll 84): it reads the general bundeswehr.de news feed keyword-filtered for procurement and never produced a signal; a search confirmed no procurement-specific feed exists. Transition the live head to retired through the lifecycle route. |
| retired | source | source.csis.analysis | catalog | Retired on the reference host; the reason is not recorded in the tree. |
| retired | source | source.voa.africa | catalog | Retired on the reference host; the reason is not recorded in the tree. |
| retired | source | source.xinhua.world | catalog | Retired on the reference host (source_cgtn_world.yaml fills the CN-desk framing slot it left). |
| retired | source | source.globaltimes.all | catalog | Retired on the reference host (source_cgtn_world.yaml fills the CN-desk framing slot it left). |
| retired | action_pack | discovery | descriptors/action_pack_discovery.yaml | Decision F-1 (2026-06-09): the discover_sources tool's job moved; never registered on the reference host. |

## Parked

| status | family | id | registrar | why |
|---|---|---|---|---|
| parked | analyst | adversarial_signals | descriptors/analyst_adversarial_signals.yaml | Ships draft; registration + activation is the operator's flip (file header). |
| parked | analyst | anomaly_detection | descriptors/analyst_anomaly_detection.yaml | Ships draft; registration + activation is the operator's flip (file header). |
| parked | analyst | archive_retention | descriptors/analyst_archive_retention.yaml | Ships draft (migration 0224 sweep); never registered on the reference host. |
| parked | analyst | cross_target_raw_g20 | descriptors/analyst_cross_target_raw_g20.yaml | Ships draft (W1-E correlation pilot); never registered on the reference host. |
| parked | analyst | signals_retention | descriptors/analyst_signals_retention.yaml | Ships draft; never registered on the reference host. |
| parked | source | source.acled.conflict | descriptors/source_acled_conflict.yaml | Draft; ACLED needs a portal grant (403); never registered on the reference host. |
| parked | source | source.common_crawl.news | descriptors/source_common_crawl_news.yaml | Generic adapter template (draft); never registered on the reference host. |
| parked | source | source.discord.default | descriptors/source_discord_webhook.yaml | Generic adapter template (draft); never registered on the reference host. |
| parked | source | source.firecrawl.default | descriptors/source_firecrawl.yaml | Generic adapter template (draft); never registered on the reference host. |
| parked | source | source.gdelt.bigquery | descriptors/source_gdelt_bigquery.yaml | Draft; needs BigQuery credentials; never registered on the reference host. |
| parked | source | source.generic_webhook.default | descriptors/source_generic_webhook.yaml | Generic adapter template (draft); never registered on the reference host. |
| parked | source | source.intelmq.cisa_kev | descriptors/source_intelmq_cisa_kev.yaml | Draft; never registered on the reference host. |
| parked | source | source.mediacloud.world | descriptors/source_mediacloud.yaml | Draft; needs a Media Cloud key; never registered on the reference host. |
| parked | source | source.opensanctions.api | descriptors/source_opensanctions_api.yaml | Draft; needs an API key; never registered on the reference host. |
| parked | source | source.opensanctions.bulk | descriptors/source_opensanctions_bulk.yaml | Declares active but was never registered on the reference host (licensed_noncommercial bulk set). |
| parked | source | source.query_source_discovery.template | descriptors/source_query_source_discovery_template.yaml | Template (draft); never registered on the reference host. |
| parked | source | source.reliefweb.reports | descriptors/source_reliefweb_api.yaml | Declares active but was never registered on the reference host (ReliefWeb needs an appname grant). |
| parked | source | source.usgs.earthquakes | descriptors/source_usgs_earthquakes.yaml | Declares active but was never registered on the reference host; the catalog's source.usgs.earthquakes_m45 is the live quake feed. |
| parked | target | discovery_geopolitical_countries | descriptors/discovery_geopolitical_countries.yaml | L2 discovery descriptor (pre-pivot materialiser path); never registered on the reference host. |
| parked | target | discovery_geopolitical_g20 | descriptors/discovery_geopolitical_g20.yaml | L2 discovery descriptor (pre-pivot materialiser path); never registered on the reference host. |
| parked | target | germany_news | descriptors/target_germany_news.yaml | Pre-pivot example country target; the country_g20_* desks replaced it. |
| parked | target | japan_news | descriptors/target_japan_news.yaml | Pre-pivot example country target; the country_g20_* desks replaced it. |
| parked | target | mexico_news | descriptors/target_mexico_news.yaml | Pre-pivot example country target; the country_g20_* desks replaced it. |
| parked | target | nigeria_news | descriptors/target_nigeria_news.yaml | Pre-pivot example country target; the country_watch_* desks replaced it. |
| parked | target | turkey_news | descriptors/target_turkey_news.yaml | Pre-pivot example country target; the country_g20_* desks replaced it. |
| parked | target | template_country | descriptors/template_country.yaml | L2 template for the discovery materialiser; never registered on the reference host. |
| parked | target | template_country_active | descriptors/template_country_active.yaml | L2 template for the discovery materialiser; never registered on the reference host. |
