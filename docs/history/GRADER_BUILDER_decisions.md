# Decision records — the correctness grader and the reference builder

Every dated statement, decision record, ticket id, incident and one-week measurement pulled out of
`docs/CORRECTNESS_GRADER.md` and `docs/REFERENCE_BUILDER.md` when those two were rewritten in the
present tense. Oldest first. Each entry is **date · area — what changed, and why**.

Ticket ids (G1, G2, R1, R2, R2-FIX(2), R2-FIX-3, D1, D3, D4, D5, D6, H2) are preserved exactly as the
source docs carried them; `TICKETS.md` is where they are defined. Planning-directory paths are kept as
the source docs cited them, as provenance for the measurements quoted here.

---

## 2026-09

**2026-09-16 · grader, rubric** — The five-label rubric (round 4), with its tiers and its precedence
ladder, produced pooled three-family agreement of **0.42** against a 0.75 bar: the round's verdict was
instrument-limited, not a finding about the reads. ANNEX C v4 removed the tiers and the ladder and asked
one question with three answers — `contains` / `contradicts` / `silent` — and measured **0.8444** pooled
on a fresh 30-atom draw over the frozen corpus with zero overlap with the v3 draw; pairs 0.833 / 0.900 /
0.800 against a 0.70 floor; 0 `UNPARSEABLE` of 90 calls; 1 span flagged, label stood
(`planning/PROGRAM1_2026-09-16/VERDICT_P1v4.md`). Rubric sha256
`1b51d7f5187c7f93af2e2cccc0775e21ab7efc41678bc28c4a2dcbe287fe7d8c`.

**2026-09-16 · grader** — The instrument was run **by hand** over one country: Israel, 8 desk heads plus
the composition, 54 claims, an independent reference built blind to the substrate, three grader families
reading ANNEX C v4. It found two claims the platform had published that day that the reference
contradicted — a desk asserting a "deadlock over list submissions" on a day the candidate lists were
filed, and the composition asserting "no price spikes" while crude was over $100. The desk had hedged the
second claim; the composition un-hedged it. That is the laundering hazard the spine names, caught by
measurement rather than by argument, and it is why the instrument was registered as a job (G1, Program 2,
`LEDGER_RESET_2026-09-16` §3).

**2026-09-16 · grader** — That run's result was seeded as the first passing `grader_calibrations` row
(`scripts/load_unit_reference.py --seed-calibration`), so the job had the passing gate it refuses to run
without. Model ids: F0 `gpt-oss-120b`, F2 `meta-llama/llama-3.3-70b-instruct`, F3
`mistralai/mistral-large-2512`.

**2026-09-16 · grader** — Migration **0196** created `unit_references`, `unit_correctness`,
`unit_correctness_claims` and `grader_calibrations`. Migration **0197** added
`unit_correctness.reference_age_days` (numeric, nullable); NULL is not zero, because every row written
before 0197 measured against a reference whose window contained the stamp but never recorded the
distance, and backfilling a `0` would assert a freshness nobody measured.

**2026-09-16 · grader** — 0196 shipped the reference lookup as `window_start <= as_of AND window_end >=
as_of`, which made the job ungradable by construction: a reference covering 09-02T19:30Z → 09-16T19:30Z
matched nothing at 22:32Z the same day, the sweep reported "no `unit_references` row covers this stamp",
and the nightly 01:20 UTC run would never have graded anything at all. Fixed by the grace window
(`LEGBA_GRADER_REFERENCE_GRACE_DAYS`, default 7, env over descriptor option), `reference_age_days` on
every row, and `reference_stale` separated from `no_reference` as a distinct outcome.

**2026-09-16 · grader (defect D1)** — The first live forced run stamped `as_of = now()` although its body
carried a past stamp: the actor merges a handler's per-run parameters from the nested `options` object
only, and a parameter at the top level is dropped silently — no error, no log line. `build_body()` became
the one place that envelope is written, and `tests/runtime/test_correctness_grader_method_body.py` drives
the real actor with the body that function returns, asserting on the `as_of` column of the row that lands.

**2026-09-16 · grader (G2)** — The composition gate shipped behind `LEGBA_COMPOSITION_CORRECTNESS_GATE`,
default off, with `LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS` 0.8, `LEGBA_COMPOSITION_GATE_MIN_COVERAGE` 0.2
and `LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY` off. Both bars were made mandatory together by one
measurement from the hand run: `economic_coercion` measured 100% correct — on one decided claim out of
seven — so a correctness-only gate would have ranked it above a desk that was right about two claims of
three. The pre-G2 periphery header told the model every quarantined item "did NOT clear the verification
floor", which is false for a correctness-gated read; with a gated row present the header now names both
reasons and counts them. Flag-off byte-identity is pinned by
`tests/data_pkg/test_composition_correctness_gate.py::test_flag_off_per_country_read_slice_is_byte_identical`
and `::test_flag_off_run_payload_and_prompt_are_byte_identical`.

**2026-09-16 · read surface** — `UnitCorrectnessBadge` and `GET /api/v1/units/{target_id}/correctness`
shipped. The route first reached the reference-currency rule through `correctness_grader`, whose import
graph reaches `feedparser`; the registry image carries no runtime analyst dependencies and the deployed
route answered **HTTP 500, `ModuleNotFoundError: No module named 'feedparser'`**. A deferred import did
not help and could not — deferring moves *when* the graph is walked, never *how far* it reaches — so the
rule moved to the stdlib-only leaf `src/legba/data/correctness_reference_currency.py`, with a subprocess
guard test that poisons `sys.modules['feedparser']` and imports the route.
[UNVERIFIED: exact date — the source doc narrated this under the read surface without one; the commit
that added the leaf would settle it.]

**2026-09-16 · builder (R1)** — "Let a good model read the web and write the reference" was measured
twice, on the same instruction and the same web
(`planning/PROGRAM2_2026-09-16/R1/COMPARISON.md`). The verdict was no:

| | run 1 (no fences) | run 2 (three fences) |
|---|---|---|
| developments | 13 | 19 |
| spans verified against the archived page | 1 (7.7%) | 15 (78.9%) |
| cited a URL it never fetched | 11 | 0 |
| dimensions carrying ≥2 verified developments | 0 of 8 | 8 of 8 |
| of a knowledgeable reader's 15 major developments | 2 (13%) | 5 (33%) |

Run 1 also burned 12 of 17 fetches on one host that 401'd it every time, and its tiering was inverted:
nine items labelled Tier 1, seven of them pages it had never read. Run 2 cleared verification outright and
failed substance, anchoring on `londondaily.com`, `hidabroot.com` and `energy-pedia.com` and tiering them
2 — and it shipped one development that was simply **false**: "Israel Katz… has been named the new Defence
Minister", dated in-window, span verbatim, URL really fetched. The page carried no publish date in any
metadata, so the model read the site's masthead as the article date; the article was from November 2024.
That asymmetry — a thin reference under-covers, a false one actively mis-grades — decided the hybrid
design and the six fences (R2). Run 2's harness refused 21 tool calls for skipping the note turn.

**2026-09-16 · builder** — `docs/SEAMS.md` #55 (the grader's reference dependency) closed: the builder
ships as the only writer of `unit_references`, no new migration. Builder pipeline stamp `2026-09-16/1`.

**2026-09-17 · both** — Budgets zeroed on every descriptor under the fleet rule (no per-analyst token
budgets; temperature 1.0 everywhere). Recorded in that day's calibration row's own notes. The builder
descriptor's comment still narrates the 8,000,000-token sizing this replaced.

**2026-09-17 · grader, calibration** — A re-gate at temperature 1.0 with budgets zeroed returned
`gate_pass = false` for F0 `gpt-oss-120b` / F2 `meta-llama/llama-3.3-70b-instruct` / F3
`mistralai/mistral-large-2512`. A fired gate still writes its row, and the refusal downstream kept the
grader off until the 09-20 re-gate landed.

**2026-09-17 · builder (D3/D4)** — The 00:43Z autonomous tick picked Argentina and ran **927.9 s for
4,510,885 prompt tokens over 67 tool calls**, committing nothing. The `web_access` pack's invocation
governor (`max_invocations_per_hour: 120`, a bucket shared with `standing_auditor` and `desk_reference`)
was already spent when the tick fired, so **63 of those 67 calls came back "not admitted by the pack"**.
The loop had no opinion about any of it: its only exits toward a commit were the 80-call tool budget
running out or the model volunteering `REFERENCE COMPLETE`, so it ran to `max_rounds` with every round
re-sending the whole conversation. 4.5 M tokens against `budget_tokens_per_day: 4000000` exhausted the day
bucket; the 01:43 precheck returned `exhausted` and `pause_until_next_window` stamped `cooldown_until =
now + 3600 s` **with no log line at all**; the 02:43 tick lost that race by 0.99 s, no-op'd
`reason=cooldown scope=global`, and the liveness watchdog called it a `cadence_stall`. The 15.5-minute
turn also outlived the reconciler's 20 s `ENSURE_ACTIVE` heal twice
(`actor_turn.budget_exceeded op=reconcile.activate`, which is the reconciler's per-heal deadline and not a
budget on the run). Fix: three walls in code — 420 s wall clock (`build_max_seconds`), 1,200,000 tokens
(`build_max_tokens`), 80 tool calls (`tool_call_cap`) — with the forced commit turn at 70% of whichever
comes first and a dead-search trigger. Sizing rather than resuming: a resumable build would need a
per-target state row, conversation rehydration and three ticks per reference, hold the actor's turn three
times, and cost *more* tokens. 420 s sat above the 294 s the one good live build needed and below both
`cadence.cooldown_seconds` (3000) and the hourly tick period;
`tests/runtime/test_reference_builder_wiring.py::test_the_build_cap_default_fits_the_actor_plane` asserts
those relations.

**2026-09-17 · builder** — The web carve-out. `Agency.run_pack_tool` resolves the ledger account as
`res.governor.budget_account or call.budget_account`, so the pack's account beats the per-analyst one the
binding passes and every analyst holding the grant drew on one hourly bucket; measured live over a
two-hour window, `standing_auditor` alone took **120 invocations — the entire cap**. Fixed by
`ActionPackRef.governor_override` on the analyst's own grant: `budget_account:
web_access_reference_builder`, `max_invocations_per_hour: 100` (one whole build at an 80-call cap, plus
25% headroom). `agency.resolution._merge_governor` applies the grant override last, takes the more
restrictive of every numeric cap and lets `budget_account` follow the override, because re-targeting the
ledger account is not a loosening. The shared 120 was deliberately left untouched — raising it would just
have handed the auditor a bigger hour. A second registered pack (`web_access_reference`) was rejected: it
duplicates the tool specs, the SSRF-guarded route and the prompt rules, and adds a second lifecycle to
drift.

**2026-09-17 · builder** — `budget_tokens_per_day` raised 4,000,000 → **8,000,000**. The arithmetic: a
32-target roster on a 7-day cadence needs `ceil(32/7)` = 5 builds a day, each capped at 1.2 M tokens, so
worst case is 6 M and the old bucket was structurally guaranteed to cool down roughly daily.
`tests/runtime/test_reference_builder_wiring.py::test_the_day_bucket_covers_the_cadence_it_is_scheduled_at`
asserts `builds/day × build_max_tokens ≤ budget_tokens_per_day`; `BUDGET_SIZED_FOR_ROSTER` is the roster
figure it uses.

**2026-09-17 · builder (D5, R2-FIX(2))** — The due queue ordered by `unit_references.built_at`, the newest
**successful** reference, and a failed build writes no row — so a target that *cannot* be built never
moved. Argentina (`country_g20_ar`) is largely unfetchable to this lane (**3 of 16 pages usable**) and had
never produced a reference, which made it permanently the most overdue target on a 32-country roster: two
consecutive builds went to it (00:43Z burned 928 s for `all_rejected`/0 committed; the 05:20Z dry run
stopped correctly at 304 s with `no_commit`), and thirty other due targets were never going to be reached.
Fix, with no new table and no migration: order by **last attempt** (`NULLS FIRST`), then most-overdue,
then target id, reading the attempt ledger off the lane's own `analyst_outputs` receipts
(`data.attempts`, the last 500 rows from the past 90 days, `_reference_roster.ATTEMPTS_SQL`); a
**24-hour retry backoff** after `no_commit` or `all_rejected`, with
`LEGBA_REFERENCE_RETRY_BACKOFF_HOURS` winning over the descriptor knob because it is the lever reached for
while the lane is stuck; and a **three-strike `unbuildable_by_lane` flag** that never drops a target and
never stops retrying it, routing it instead to `scripts/reference_topup_packet.py --candidates`.
`no_model` and `no_web` are not attempts — they are refusals before the build, and recording them would
rotate the queue on a misconfiguration that hits every target equally.

**2026-09-17 · builder (R2-FIX-3, the yield analysis)** — The commit wall made every build terminate and
every failure named; it did not make builds yield. Israel 9 developments, Ukraine 5, Argentina 1 on its
third attempt, Australia 0. The 06:43Z Australia trace (`analyst_traces` run `14e81d75`) was read call by
call:

| what the trace showed | the fix |
|---|---|
| The model was shown `https://www.theguardian.com/…imf-downgrades-…` and fetched `https://theguardian.com/…` — a 404 of 202 bytes; the real page is 6,538 chars dated 2026-09-17. It did it twice. | a fetch URL is repaired to the result the model was shown whenever the loose key (host without `www.` + path) matches |
| 4 of 8 fetches went to `reuters.com` / `bloomberg.com`, measured unreadable, from URLs written out of the model's head. The unreadable list was a discovery fence with no fetch fence behind it. | those hosts are refused at the fetch call, free, before the pack is touched; `bloomberg.com` joined the list (D6) |
| The repair only catches a near-miss; a URL with no offered near-match, the loop has no way to know exists. | a fetch of a URL no search offered is refused, free, and the URLs actually on offer for that host are named back |
| 6 queries carried `site:`, 8 carried a date literal, 13 named an outlet this lane cannot read | queries are rewritten before the pack sees them, with the reason returned; the instruction carries 3–5 event-oriented patterns per dimension |
| 36 searches / 8 fetches, and the streak rule (4) never caught it | the streak rule became **2**, plus a search/fetch ratio guard (> 3 searches per fetch after 12 calls) |
| The model wrote one span-carrying NOTE and the loop discarded it — it arrived beside a tool call. 18 refusals, 4 exceptions. | a note beside a tool call is recorded and satisfies the gate; the gate refuses at most once per fetch |
| At 230 s the model emitted the complete final JSON as prose; lacking the words REFERENCE COMPLETE, the loop said "Noted. Continue." and bought 13 more calls. | a turn that parses as the reference object enters the commit turn |
| The forced turn said "commit from the notes you have" to a model with no notes. | the commit turn is built from the manifest |

The same pass added the **manifest** (the loop's own record of every page it fetched: url, outlet, title,
the page's machine-readable date, in-window or not, archive digest, and the first
`MANIFEST_EXCERPT_CHARS` = 1,400 characters of archived text), the **`PAGE:` carry** beside the model's
`NOTE:` lines, and `empty_commit_with_material` as a third named outcome. It also separated `no_commit`
from `all_rejected`: the 00:43Z receipt had read `all_rejected` with "0 verified of 0 committed" and a
rejection table of seven zeros, a sentence that cannot be true — `all_rejected` is now reachable only with
at least one committed development.

**2026-09-17 · builder** — The 07:23Z Australia dry run spent 35 calls instead of 44, rewrote 17 queries
and carried 3 NOTE lines instead of none, and still committed nothing, because it put **0 in-window
pages** on its manifest. Four fetches were lost to `dfat.gov.au` and `defence.gov.au` with
`httpx.ReadTimeout` and an **empty message**, surfaced as `web_fetch.http_error url=… err=` and recorded
by the blocklist as ordinary host failures — yet those are real pages
(`dfat.gov.au/international-relations/security/sanctions` was dated inside the window) and the same URLs
served 200 through the same guarded client from inside the runtime network ten minutes earlier. Logged as
the fetcher's problem (`FETCH_REVIEW.md`), not this lane's, and as the largest measured residual on yield.

**2026-09-20 · grader** — F3 repointed. OpenRouter removed `mistralai/mistral-large-2512`, the id the v4
gate ran against, so every F3 call was a dead call against a model that no longer resolved — the
silent-dead-analyst class, here silently turning a three-family number into a two-family one. The
component id did not move (`llm.judge.openrouter_mistral_large.openai_compat` was repointed at the stack);
the model id became `mistralai/mistral-medium-3.1` and the prices backing the ceiling became $0.40 in /
$2.00 out per million tokens. The calibration interlock then fired on purpose, because no passing row
covered the new id. A **frozen-packet re-gate** on the v4 atom set
(`planning/PROGRAM1_2026-09-16/calibration_v4/packet_F0.json`) passed at 22:22Z and is the row the grader
runs under: rubric sha unchanged, model ids `{F0: gpt-oss-120b, F2: meta-llama/llama-3.3-70b-instruct, F3:
mistralai/mistral-medium-3.1}`. The `--packet` path exists for exactly this case: a live draw asks whether
the families agree on today's claims, which is the wrong question when a family moves rather than the
rubric.

**2026-09-20 · grader** — The `prior_relative` exclusion class. Measured live over the 980 rows then in
`unit_correctness_claims`, **13 of the 48 `contradicts` labels** sat on spans asserting this read's
relation to its own previous read; two Brazil desks — `leadership_transition` and `energy_security` —
each took a **0.00** correctness share off exactly one of them, each unit's only decided claim. The
shipped joined form (a continuity assertion grammatically joined to a reference to the unit's own earlier
read) excludes 33 of those 980 rows; the looser form — a prior-read mention anywhere plus a no-change
phrase anywhere — excluded 49 and swallowed two genuine world claims. `GRADER_PIPELINE_VERSION` went
`2026-09-16/1` → `2026-09-20/1`, because the class changes every unit's denominator and shares either
side of it are not the same measurement. No re-gate was needed or done: the rubric text is untouched, and
this changes which spans are handed to the rubric.

**2026-09-20 · grader** — Target rotation. The population query was `SELECT DISTINCT target_id … ORDER BY
target_id` and the run took `[:max_targets]`; with the shipped cap of 5 that prefix was the same prefix
every night, so AR, AU, BR, CA and CN were graded on every sweep and the other 27 members of the roster
were never graded once. Replaced with a `LEFT JOIN` on `MAX(created_at)` per target ordered
`last_graded_at ASC NULLS FIRST, target_id`, so any cap cycles the roster and the receipt says when a cap
bites. `max_targets_per_run` was raised 5 → **32** alongside it as a stopgap that pays for the whole
roster nightly and breaks again the moment the roster grows.

**2026-09-20 · builder, operator knobs** — `tool_call_cap` 80 → 200 (yield); `page_chars_to_model` 7,000 →
16,000; `min_developments` 20 → 5 (20 was unreachable and only delayed the commit); `build_max_seconds`
420 → 900; the builder's `governor_override.max_invocations_per_hour` 100 → 1,000,000; the `web_access`
pack's own `max_invocations_per_hour` 120 → 1,000,000 and `api_rate_per_minute` 20 → 1,000,000, because
the shared cap was starving the auditor and the researcher; and `max_cost_usd_per_day` $1.00 on the pack
with `search.serper.paid` declared as the fallback rung (serper at ~$0.001/query, so ≤1,000 paid searches
a day; the free rung is uncounted).

**2026-09-21/22 · grader (H2)** — The sweep held **one actor turn for the whole roster** — 32 targets at
~2.5 minutes each is ~75 minutes — so reconcile's deadline blew behind it
(`actor_turn.budget_exceeded`) and the 2026-09-21 redeploy cut the sweep at 26/32 with **no receipt**,
because the receipt is the turn's return value. `LEGBA_GRADER_PASS_BUDGET_SECONDS` (600) now bounds the
wall clock the target loop may hold, checked **between** targets so a target in flight always finishes,
with the deferred tail named on the receipt and least-recently-graded-first putting it at the head of the
next tick's queue. The cadence became **nine hourly ticks** (`20 1-9 * * *`, ~4 targets each) with
`cooldown_seconds` 79,200 → 3,600; the rotation has no freshness gate, so an all-day hourly cadence would
grade the roster three times and triple the spend. Eight ticks measured 30/32 on 09-22.

---

## Undated at the time of the rewrite

**grader ceiling** — `LEGBA_GRADER_DAILY_CEILING_USD` was shipped at `0` (free core-plane family only,
every number `single_family`) and is live at `$2.50`/day, roughly 45¢ per roster.
[UNVERIFIED: the date it was raised — the environment file carries the value but no dated note, and the
descriptor comments still describe the $0 posture.]

**builder token ceiling** — `LEGBA_REFERENCE_BUILD_MAX_TOKENS` is set to `1000000000` in the environment
while the descriptor keeps `build_max_tokens: 1200000`; the descriptor knob wins for the two build walls,
so the effective per-build ceiling is 1,200,000. [UNVERIFIED: the date and intent of the environment
value — it reads as a deliberate "unbounded unless the descriptor says otherwise".]

**builder day bucket** — `budget_tokens_per_day` is `0` (unlimited) on both descriptors under the
09-17 fleet rule, superseding the 8,000,000 sized on 2026-09-17. The wiring test that asserts
`builds/day × build_max_tokens ≤ budget_tokens_per_day` is named in the source doc;
[UNVERIFIED: how that assertion reads a `0` bucket — running
`tests/runtime/test_reference_builder_wiring.py` would settle it.]

**measured-unreachable hosts** — `reuters.com` (robots `Disallow: /`), `apnews.com` and
`timesofisrael.com` (Cloudflare challenge), `haaretz.com` and `ft.com` (paywall), `bloomberg.com` (added
under D6, 2026-09-17). `curl_cffi` browser impersonation was measured to change nothing on any of them.

**seam #54(a)** — Licence classes stay the operator's: with no `license_class` recorded for a host,
`depth_for_license` puts it at teaser depth, the body is not archived, and the development carries
`span_source: snippet`.
