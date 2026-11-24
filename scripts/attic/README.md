# scripts/attic — retired bring-up scripts

Kept for the record; nothing runs them and they are not maintained (their
imports assume `scripts/` and may no longer resolve from here). Triage and
reasons: `docs/BRINGUP.md`. Moved 2026-10-04 (W6 SPLIT-1).

- `bringup_register_discovery.py` — registered the L2 `template_country` +
  `discovery_geopolitical_countries` pair (pre-pivot materialiser path) from a
  hard-coded main-checkout path; neither is registered on the reference host
  (both are `parked` in `descriptors/RETIRED.md`).
- `bringup_register_g20_discovery.py` — registered `template_country_active` +
  `discovery_geopolitical_g20` against a hard-coded registry URL and points at
  an activation script that does not exist; superseded by the `country_g20_*`
  target files (both descriptors `parked`).
- `bringup_register_multi_country_targets.py` — registered the five pre-pivot
  `*_news` example country targets; the `country_g20_*` / `country_watch_*`
  desks replaced them (all five `parked`).
