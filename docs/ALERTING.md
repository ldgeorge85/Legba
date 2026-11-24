# Alerting: one path, one log, one page rule

Everything in Legba can raise an alert. Almost every alert is only **logged**.
A few kinds, at or above a severity, **page** (go to the push service, read
through its web UI). One rule, in one file, decides which. There are no
escalation chains and no per-producer exceptions.

This page is the reference for the kinds, the severity ladder, the page rule,
and how to change it. The code is `src/legba/data/alerts/`.

## The path

Every producer calls one function,
`legba.data.alerts.raise_alert.raise_alert(...)`. In order, it:

1. refuses a kind that is not in the closed list (`UnknownAlertKind`, before
   anything is written);
2. maps the severity onto the ladder below;
3. records the **episode** in the `alerts` table: the first raise for a dedupe
   key opens it, later raises while it is open update it (count, last raised,
   severity, summary), a recovery (`close=True`) closes it;
4. decides paging — `paged`, `logged`, `held` or `deduped` — with a reason;
5. writes one `alert_events` row for the raise, with the decision, the reason
   and the page-rule entry that matched;
6. if it pages, hands the push to the delivery plane, which writes one
   `alert_sink_deliveries` row per sink outcome naming the episode and the
   raise.

Host scripts (disk floor, LLM heartbeat, search canary, nightly suite, stall
watchdog) reach the same function through
`python -m legba.data.alerts.cli raise ...` run inside the runtime container
(`scripts/lib/legba_alert.sh`). If the runtime or its database is down, the
script falls back to its own direct push, so losing the database never loses a
page. The cron files turn this on with `LEGBA_ALERT_RAISE=1`.

## The severity ladder

| Level | Meaning |
|---|---|
| `info` | a record: a recovery, a housekeeping fact |
| `low` | worth keeping; read it in the log, not on a phone |
| `medium` | worth a look today |
| `high` | worth acting on now |
| `critical` | needs a person |

The analytic units' own words map explicitly: `moderate` is `medium`,
`elevated` is `high`. Any other unknown word becomes `medium` and is logged
(`alert_sinks.severity_unknown`): an alerting path fails toward paging.

## The kinds (the closed list)

Each kind is one declaration module in
`src/legba/data/type_registry/alert_kinds/` (its subject, lifecycle, quiet
window, group, push topic, whether the shipped page rule pages it, and its
summary); `alerts/kinds.py` and `alerts/topics.py` are derived from them, and
the table below is generated from them (`scripts/gen_type_tables.py`). Adding
a kind is one new file: see [ADDING_A_TYPE.md](ADDING_A_TYPE.md). A kind is
about one kind of subject. A **condition** kind is
opened and closed by its producer; an **event** kind is closed by the log after
its quiet window with no new raise.

<!-- BEGIN GENERATED alert-kinds (scripts/gen_type_tables.py; do not edit) -->
| Group | Kinds | Subject | Topic |
|---|---|---|---|
| findings | `escalations`, `verified_finding`, `band_crossing`, `situation_escalation`, `watchlist_hit`, `contention_flip`, `geo_convergence`, `rollup`, `analyst_alert_output` | target | world |
| baseline | `baseline_deviation`, `volume_surge` | target | world |
| source health | `source_cadence_stall`, `source_degraded`, `source_staleness_stale`, `source_staleness_dead`, `source_stream_down`, `evidence_drift` | source | engine |
| analyst failure | `analyst_cadence_stall`, `analyst_cursor_lag`, `analyst_hard_fail`, `production_deficit`, `coverage_floor`, `external_audit`, `llm_route_dead`, `llm_route_auth_failed`, `search_degraded`, `thinking_fallback` | analyst / target / route / platform | engine |
| consult | `consult_unhealthy` | route | engine |
| pipeline | `liveness_stall`, `alert_volume_high`, `reminder_orphan_collected` | platform | engine |
| forecasts | `forecast_resolution` | question | engine |
| expectations | `due_and_absent` | question / target / analyst / source (the expectation's own) | engine |
| host | `host_disk`, `host_llm_heartbeat`, `host_search_canary`, `host_stall_recovered` | host | engine |
| deploy | `nightly_suite` | deploy | nightly |
<!-- END GENERATED alert-kinds -->

`volume_surge` fires when a country's fresh signal count over the last hour is
far above its own 14-day hourly baseline and comes from at least eight
independent outlets (`runtime/volume_surge_watch.py`; the thresholds and the
30-day backtest are in its docstring; `scripts/volume_surge_backtest.py`
replays it offline).

`evidence_drift` (`runtime/evidence_drift_loop.py`) says a source edited,
retracted or removed pages after the platform read them and something relied
on them. One episode per SOURCE (the host when the drift has no source) per
day, never one per article: each pass re-raises the open episode with that
pass's counts by class. It is raised at `low` (`medium` when a page that
something relied on was retracted, replaced or is gone) and the page rule has
no entry for it, so it is logged and shown on the Watch data, never pushed.
A daily roll-up (subject `platform`, `evidence_drift_rollup`) carries the
last 24 hours' counts by source and class.

`due_and_absent` (`data/analysts/deterministic_handlers/due_and_absent.py`,
the daily `due_and_absent` analyst) says a row of the record that carries an
explicit date — a sealed forecast, an acute forecast's window, a calendar row,
an inquiry's resolves-by, a live series' next period — is past it with no
matching record collected. It is a **condition**: the raise when a row is first
seen due and absent opens one episode per expectation
(`due_and_absent:<type>:<id>`), and the raise when the row is later resolved or
voided closes it; nothing re-raises while it stays absent. It is raised at
`low` and the page rule has no entry for it, so it is logged and never pushed.
A series whose own source's polls were silent or failing raises nothing: the
watchdog's source-health kinds above own that case.

`thinking_fallback` (`runtime/thinking_fallback_alert.py`) says a thinking call on a component
failed for a reason the thinking alias causes and was retried once on the plain alias
([AI_MODELS](AI_MODELS.md), "The safety net"). One episode per component and reason; every further
fallback re-raises it, so its occurrence count is the rate. Raised at `low`; the page rule has no
entry for it, so it is logged and shown on the Watch data, never pushed. The runtime raises it; a
fallback in the workflow worker (deep consult) is on its receipt and in its log only.

## Repeats: the episode is the one place

* An open episode pages once. A later raise pages again only when its severity
  rises above what already paged.
* A `critical` episode re-pages every `LEGBA_ALERT_CRITICAL_REPAGE_HOURS`
  (6) while it stays open.
* A recovery pages only for an episode that paged (and only if the rule
  matches the recovery's severity).
* A raise the producer itself measured as not-a-page is **held** with its
  reason: the trigger scan's steady-state guard and daily page budget, a level-
  only escalation. Held raises are logged, never paged.
* The delivery plane keeps one anti-burst limit: one push per sink and topic
  per `LEGBA_ALERT_SINK_COOLDOWN_SECONDS`, the suppressed ones folded into the
  next push.

## The page rule

`src/legba/data/alerts/page_rule.yaml`:

```yaml
version: 2026-11-14/1
page:
  - {kind: escalations,          min_severity: medium}
  - {kind: source_degraded,      min_severity: high}
  - {kind: host_disk,            min_severity: low}
  # ...
```

An alert pages when **any** entry matches: same kind, severity at least
`min_severity`, and — if the entry has one — a subject matching `subject` (a
plain id or a pattern with `*`). Anything no entry matches is logged only.

The shipped rule pages the world's kinds and the trigger scan's engine kinds at
`medium` and above, the watchdog's kinds at `high` (their `medium` raises are
5xx stalls recorded without paging), the disk floor and the nightly suite at
`low` (every notice they send is meant to be read), and the heartbeat and the
search canary at `high`. `volume_surge` pages at `medium`; `baseline_deviation`
is logged only. Every kind the shipped rule pages nothing for is declared
`paging="logged_only"` and listed as "logged only" in
[TYPES.md](TYPES.md#alert-kinds); a test holds the declarations and the shipped
rule to each other, so a kind is never logged-only by accident. Paging a
logged-only kind is a new rule version, an admin decision.

When the rule pages, it is the floor: the sinks' own
`LEGBA_ALERT_NTFY_MIN_SEVERITY` / `LEGBA_ALERT_WEBHOOK_MIN_SEVERITY` no longer
second-guess it (they still apply to anything handed to the delivery plane
some other way, which nothing does).

### Changing it

* **Page on baseline deviations again:** add `- {kind: baseline_deviation, min_severity: medium}`.
* **Only one country's surges:** replace the `volume_surge` entry with `- {kind: volume_surge, min_severity: medium, subject: "US"}`.
* **Stop the source-health pages for one noisy feed but keep the rest:** the
  rule has no "except"; narrow the entry instead, e.g. replace
  `{kind: source_degraded, min_severity: high}` with entries for the subjects
  you care about (`subject: "source.reuters.*"`), or raise it to `critical`.
* **Stop a kind paging altogether:** delete its entry. It is still logged.

Then roll the runtime (the rule is read once per process; a file that does not
parse, or names a kind or severity that does not exist, is refused at load and
the runtime pages every raise at `medium` and above until it is fixed, logged
`alert_raise.rule_unavailable`). `LEGBA_ALERT_PAGE_RULE_PATH` points at a copy
outside the tree if you prefer to keep your rule out of the repository.

## Reading the log

`GET /api/v1/v3/system/alerts/log?hours=168` (registry, bearer): the open
episodes, the episodes closed in the window, the raises by kind and severity
split by decision, what paged and why (the rule entry, the reason, the sink
outcomes), and the rule and kinds this deployment runs. The older routes still
answer from the delivery ledger: `/v3/system/escalations` (the escalations
panel) and `/v3/system/alerts/volume` (pushes per topic and kind).

A reader's "what needs me" reads the same log through
`GET /api/v1/v3/since?cursor=…&include=needs_you`: the open episodes that
PAGED (paged-only: an episode that sits at `medium` or above without paging
is on the Watch page's alert log, not here) and that opened or paged after
the cursor, worst first, capped at 50 with `total` and `truncated`; each
item's `why` is `paged`, with `paged_severity` and `last_paged_at`, less the ones a person acknowledged or snoozed (below). Below admin it carries
the `world` topic's kinds only. `GET /api/v1/v3/since/counts?cursor=…` gives
the same counts without rows (reused for 30 seconds), for a badge.

## Acknowledge and snooze

A person can say "seen" about an open episode, for everyone:

| Route | Who | What |
|---|---|---|
| `POST /api/v1/v3/system/alerts/{episode_id}/ack` `{"note"?}` | contributor, admin | acknowledge |
| `DELETE …/ack` | contributor, admin | withdraw the acknowledgement |
| `POST …/snooze` `{"duration": "1h" \| "4h" \| "24h" \| "until_closed", "note"?}` | contributor, admin | snooze |
| `DELETE …/snooze` | contributor, admin | end the snooze |
| `GET …/attention` | reader | the state and every act, closed episodes too |

With the sign-in switch off the shared admin credential is the one principal;
a service credential is refused either way (acknowledging is a person's act).
Below admin only the world topic's kinds are visible, so any other episode
is 404.

* **Shared.** The instance has one alert log: an acknowledgement or snooze
  takes the episode out of "needs you" (`/since?include=needs_you` and
  `/since/counts`) for every reader, not just the person who acted.
* **It comes back** when the episode pages at a severity **above** the one it
  stood at when the person acted (the higher of its severity and its paged
  severity then), or — for a snooze — when the time passes. A later episode
  for the same subject is a new episode and starts unacknowledged. A
  critical episode's six-hourly re-page is at the same severity, so it does
  not bring an acknowledged episode back.
* **Paging is unchanged.** Neither act closes the episode, stops its raises
  being recorded, or changes the page rule: pages still go out, and a snooze
  does not hold re-pages.
* **Recorded.** Each act is one `alert_episode_actions` row (who — the stable
  actor string, never a credential — when, the note, the snooze) and one
  signed, chained `descriptor_audit_log` row (namespace `alert`). The alert
  log route shows each episode's `attention`; a closed episode keeps it.
* Repeating an act that is already in force writes nothing (`changed:
  false`); a different snooze replaces the current one.

## Not yet

* No resolve by a person; an episode closes by its producer or its quiet
  window.
* No daily digest route; "logged" means the log and the Watch surface.
* The first-generation `alert` output kind keeps its own legacy transports
  (Pushover, XMPP, Matrix; unused) besides raising here.
* The models-host watchdogs (`models_host_vllm_watchdog.sh`,
  `models_host_saturation_watch.sh`) run on another machine and cannot reach the
  runtime; they still post directly if `NTFY_URL` is set there.
