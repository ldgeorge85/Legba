# Descriptors the bring-up does not register

`scripts/bringup_register_descriptors.py` registers every `descriptors/*.yaml` of
the four registry families (`action_pack`, `source`, `target`, `analyst`) plus
every entry of the source catalog (`profiles/geopolitics/rosters/source_catalog.yaml`),
except the ones listed here. Each row is one descriptor; the walker parses the
table rows and nothing else, so keep the five columns.

* **retired**: terminal. The walker never registers it. If a live head with
  this id is not retired, the walker reports `would-retire` and retires it on
  an apply run, so add a row only when you mean to retire the descriptor.
* **parked**: in the tree but not registered (a draft waiting for activation,
  a legacy example, a template). The walker skips it. To turn one on, delete
  its row, run the walker, then promote it through the lifecycle route.

`tests/test_bringup_accounts_for_live_heads.py` holds this file to a committed
roster of heads: a non-retired head may not sit in the `retired` rows without
the walker reporting it, and every tree descriptor whose head is retired must
be listed.

## Retired

| status | family | id | registrar | why |
|---|---|---|---|---|
| retired | analyst | country_assessor | descriptors/analyst_country_assessor.yaml | The per-country one-pager; the bounded units and country_composition replaced it. |
| retired | analyst | country_critic | descriptors/analyst_country_critic.yaml | It graded the retired country_assessor. |
| retired | analyst | country_predictor | descriptors/analyst_country_predictor.yaml | A reactive forecast-as-claim producer; forecasting is forecast_scoreboard. |
| retired | analyst | cross_correlator | descriptors/analyst_cross_correlator.yaml | Became a coverage-gap detector over retired outputs with no consumers. |
| retired | analyst | fact_decay | descriptors/analyst_fact_decay.yaml | fact_decay_scan carries the job. |
| retired | analyst | geo_convergence_scan | descriptors/analyst_geo_convergence_scan.yaml | Retired. |
| retired | analyst | hypothesis_lifecycle | descriptors/analyst_hypothesis_lifecycle.yaml | Superseded by competing_hypotheses. |
| retired | analyst | meta_synthesizer | descriptors/analyst_meta_synthesizer.yaml | Superseded by the compositions. |
| retired | source | source.gdelt.doc_api | descriptors/source_gdelt_doc_api.yaml | Replaced by source.gdelt.files. |
| retired | source | source.ucdp.ged | descriptors/source_ucdp_ged.yaml | Retired. |
| retired | source | source.bundeswehr.procurement | descriptors/source_bundeswehr_procurement.yaml | It reads a general news feed keyword-filtered for procurement and produced no signals; no procurement-specific feed exists. |
| retired | source | source.csis.analysis | catalog | Retired. |
| retired | source | source.voa.africa | catalog | Retired. |
| retired | source | source.xinhua.world | catalog | Retired; source_cgtn_world.yaml fills the same framing slot. |
| retired | source | source.globaltimes.all | catalog | Retired; source_cgtn_world.yaml fills the same framing slot. |
| retired | action_pack | discovery | descriptors/action_pack_discovery.yaml | The discover_sources tool's job moved elsewhere. |

## Parked

| status | family | id | registrar | why |
|---|---|---|---|---|
| parked | analyst | adversarial_signals | descriptors/analyst_adversarial_signals.yaml | Ships draft; registration and activation are an admin's step (file header). |
| parked | analyst | anomaly_detection | descriptors/analyst_anomaly_detection.yaml | Ships draft; registration and activation are an admin's step (file header). |
| parked | analyst | archive_retention | descriptors/analyst_archive_retention.yaml | Ships draft. |
| parked | analyst | cross_target_raw_g20 | descriptors/analyst_cross_target_raw_g20.yaml | Ships draft (a correlation pilot). |
| parked | analyst | signals_retention | descriptors/analyst_signals_retention.yaml | Ships draft. |
| parked | source | source.acled.conflict | descriptors/source_acled_conflict.yaml | Draft; needs a portal grant (the feed answers 403 without one). |
| parked | source | source.common_crawl.news | descriptors/source_common_crawl_news.yaml | Generic adapter template (draft). |
| parked | source | source.discord.default | descriptors/source_discord_webhook.yaml | Generic adapter template (draft). |
| parked | source | source.firecrawl.default | descriptors/source_firecrawl.yaml | Generic adapter template (draft). |
| parked | source | source.gdelt.bigquery | descriptors/source_gdelt_bigquery.yaml | Draft; needs cloud query credentials. |
| parked | source | source.generic_webhook.default | descriptors/source_generic_webhook.yaml | Generic adapter template (draft). |
| parked | source | source.intelmq.cisa_kev | descriptors/source_intelmq_cisa_kev.yaml | Draft. |
| parked | source | source.mediacloud.world | descriptors/source_mediacloud.yaml | Draft; needs an API key. |
| parked | source | source.opensanctions.api | descriptors/source_opensanctions_api.yaml | Draft; needs an API key. |
| parked | source | source.opensanctions.bulk | descriptors/source_opensanctions_bulk.yaml | Declares active but is not registered. |
| parked | source | source.query_source_discovery.template | descriptors/source_query_source_discovery_template.yaml | Template (draft). |
| parked | source | source.reliefweb.reports | descriptors/source_reliefweb_api.yaml | Declares active but is not registered (the API needs an application-name grant). |
| parked | source | source.usgs.earthquakes | descriptors/source_usgs_earthquakes.yaml | Declares active but is not registered; the catalog's source.usgs.earthquakes_m45 is the quake feed. |
| parked | target | discovery_geopolitical_countries | descriptors/discovery_geopolitical_countries.yaml | Discovery descriptor for an older materialiser path. |
| parked | target | discovery_geopolitical_g20 | descriptors/discovery_geopolitical_g20.yaml | Discovery descriptor for an older materialiser path. |
| parked | target | germany_news | descriptors/target_germany_news.yaml | Older example country target; the country_g20_* desks replaced it. |
| parked | target | japan_news | descriptors/target_japan_news.yaml | Older example country target; the country_g20_* desks replaced it. |
| parked | target | mexico_news | descriptors/target_mexico_news.yaml | Older example country target; the country_g20_* desks replaced it. |
| parked | target | nigeria_news | descriptors/target_nigeria_news.yaml | Older example country target; the country_watch_* desks replaced it. |
| parked | target | turkey_news | descriptors/target_turkey_news.yaml | Older example country target; the country_g20_* desks replaced it. |
| parked | target | template_country | descriptors/template_country.yaml | Template for the discovery materialiser. |
| parked | target | template_country_active | descriptors/template_country_active.yaml | Template for the discovery materialiser. |
