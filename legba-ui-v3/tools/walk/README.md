# The first-time-user browser walk

Every UI change is walked in a real browser before it ships. Two changes that
skipped this hurt: a Consult question box 34 px wide, and a Flow panel that
locked the browser laying out a 336-node graph on the main thread. This tool is
the walk: a headless Chromium session that does what a first-time user does,
measures it, and fails with a plain list when a budget breaks.

## What it walks

1. The sign-in screen, with no token.
2. The landing at 1680×1050 with **every API answer held**, to catch a count
   printed before anything was read.
3. The landing at 1680×1050, timed to "ready" (the shell drawn and the API
   quiet for one second). The report names the request that ended last — the
   one that held "ready" open.
3b. The map: the Board's map panel until it is drawn with its data
   (`[data-testid="globe-map"][data-ready="1"]`). Its ready time is the map's
   OWN stamp (`data-ready-at`, ms from navigation); the time the walk
   first looked is kept beside it as `seenMs` (it includes the landing's ready,
   its 3 s settle and its screenshot). Then spun by three drags and
   zoomed in by the wheel until it is flat, and reset (the longest task in that
   window is the "map: spin and zoom" step); the events layer toggled on over a
   7-day window; one changed area clicked (its subject becomes the point: the
   accent outline and Consult's line follow) and cleared with Escape. The
   pixel clicked is one a hand can reach (`pick.mjs`): the map's canvas is
   what is under it — not the layer card, the legend or a control — and a
   washed land area is the topmost pickable feature there. In
   fixture mode the events and incidents-at-sea answers are moved forward by
   (now − the fixture's `map_clock`), so the same marks fall in the window
   whenever the walk runs.
   The walk's headless Chromium renders WebGL in software (SwiftShader), and
   the map OPENS FLAT on a software renderer (`GlobeMap.tsx::softwareRenderer`);
   the globe itself is then switched on, its longest task REPORTED as
   `globeOnSoftwareLongTaskMs` (never enforced: it measures the host's CPU),
   screenshotted (`W03e`), and switched back.
3c. The Watch page, opened by its address (`#ws=watch`) on a fresh page
   at 1680: its own landing's requests and bytes (the `watch` phase); the
   alert log drawn with rows; a filter that narrows it without a new read; a
   row's subject made the point (the link takes the accent); the silences
   with both "looked, saw nothing" and "could not look" rows; a flag opened
   with both versions; the edit record. Then, on another fresh page with the
   edits summary answered as it is until the first re-reads land (all
   zeros), the region must say what will appear and why it is empty, and
   read no flag list (`W14-*.png`). Its answers are the INVENTED rows of
   `fixtures/watch.json`, laid over `api.json`.
4. The frame: the top bar (`surface-bar`), the rail (`frame-rail`),
   the stage (`stage`), Consult under it (`consult-dock`) and the panels
   (`panel-side`); then the reader side's four pages in the bar
   (`[data-testid^="surface-tab-"]`: Board, Subject, Ledger, Watch).
5. One subject opened from the top bar's switcher (`subject-switcher`, then
   `[data-testid^="rail-subject-"]`).
6. The Consult composer: the question box in Consult takes focus (nothing is
   typed or sent).
7. The Record sheet (`record-sheet`; walked after the reader surfaces,
   before a subject is opened), opened twice as a reader would:
   from the first change on the Board (live with no change row, a finding by
   its address `#rec=finding:<id>`), and from a citation in a Consult answer
   (a prior conversation reopened from the rail, its "Records cited" list,
   the first row). Each is timed from the click until the chain has read to
   its last step (`record-chain` with no `record-reading` left), its own reads
   are counted in their own phase (`record: …`), its address is checked, the
   text floor is measured inside the sheet, and "Show what was received" is
   opened once (`W06b-record-sheet*.png`, `W06c-record-sheet-consult*.png`).
   In fixture mode the sheet's depth-1 lineage read is answered by kind from
   `fixtures.routes` (a path pattern and its query pairs: the walk cannot know
   in advance which record it clicks); every string there is held to the
   same no-real-content check as `entries`.
7b. The Ledger: the forecasts on the stage, the track record and the
   evaluation record read (`ledger-forecasts`, `track-record`, `proof-record`),
   measured, screenshotted (`W06c-ledger`); its reads are counted as their own
   phase. While fewer than five forecasts have resolved, a score or a Brier
   figure in the track record FAILS the walk. The
   evaluation record (`GET /v3/eval/runs`) must answer: a published run
   or the designed "No evaluation published yet" (a 200 with `runs: []`); a
   404, an unreadable record or an error FAILS. The fixture carries one
   invented published run and one invented withdrawn run.
8. The operator side (`to-operator-side`) and its four sections: Engine,
   Evaluation, Gate, Investigate; on the Gate, the forecast resolution queue
   (`resolution-queue`), its first proposal opened when there is one
   (`W04g-gate-resolutions`). The queue is the registry's
   (`GET /v3/ledger/resolution-queue`; the fixture carries one invented
   proposal) and an opened proposal shows its evidence, the rule and the
   decision controls; the walk never decides (every write is refused and
   counted).
9. The Flow panel: the default overview, one small expansion that must draw,
   and expansions of the largest groups until the budget refuses one (it must
   say so and draw nothing).
10. The landing and each reader page at **1366×768, 1920×1080, 2560×1440 and
    3840×2160**: the text floor, the frame's geometry (the widest empty
    band, the median body text, the chat's reading width) and the Consult box
    width on each, with a screenshot (`W10-…-<width>.png`). At each screen
    the MAP is checked as a frame panel — on the Board as the lead panel
    column, on the Subject page as its inset, and sent to the stage
    (`region-to-stage` on the map's group; `W10-map-stage-<width>.png`): drawn
    with its data, and its chrome (the layer card or its button, the legend
    or its button, the projection switch, the zoom and reset controls, the
    attribution) inside the map, clear of every panel header and the top bar,
    the layer card clear of the legend and the controls (`map fits as a frame
    panel: …`). With the globe flag off the flat map stands in and is listed,
    not measured.
10a. The map's projection is the reader's, at 1680: on the walk's
    software renderer it opens flat; the globe is chosen, kept across a
    reload, and put back (`map: the projection choice is remembered`).
10b. The frame is composable, at 1920×1080: the chat divider, the
    centre's edge and a panel splitter (Dockview's sash) are dragged, measured,
    and measured again after a reload; Consult is moved to a right column and
    back, collapsed to the ask bar and restored, popped to the full centre and
    back, with the question box checked usable in each state
    (`W13-*.png`).
11. The subject page by its address with every API answer held.
12. The phone reader at 390×844 (a phone must be sent to `/m/`), then one card.
    The phone landing's own request window (`phone-landing`,
    navigation to quiet) and the card's (`phone-open`) are budgeted; Consult is
    opened and its composer row measured, then a question is sent and a
    MOCKED answer (the page's own route answers the POST in the browser: no
    consult request leaves it, no model is called) is checked for raw `**`
    and pipe-table text and for citation buttons reachable ±21.5 px from their
    centre (`W12b`/`W12c` screenshots). The composer row is measured again at
    360×740. `/favicon.svg` and `/m/favicon.svg` must come back as images.
    The phone's landing is the phone shell's Changed, the card is a
    Changed row, and Consult is reached through its tab.
13. The phone shell (`phone.mjs`), over the invented
    `fixtures/phone.json` (what needs you, the seen cursor answering "sign-in
    is off") laid on the recorded set. At 390×844 and 360×740 every screen,
    each in its own request phase with a screenshot (`P<width>-NN-*.png`):
    Changed (landing; "Needs you" must list exactly the two PAGED episodes,
    not the one open at medium that did not page), a window chip (asks once;
    going back asks nothing — at 390), a read, the Record's peek (a citation,
    else a situation row), the Record's page, Back step by step to the list,
    the drawn map (no map-library request) and, at 390, "Open the globe"
    (which must request it), Watch's three segments, an alert's page (no
    acknowledge or snooze button while the route is not served, and the page
    says so), the Ledger (no score below five resolutions), the evaluations,
    Subjects (search, open one), Settings and Consult. At 430×932 and in
    landscape (844×390) Changed and Watch. A touch screen 820×1180 must land on
    `/m/`. A `#rec=finding:<id>` link opened at 390 must land on that record
    on the phone, and Back must return to the landing. Every phone page must
    reach `/m/` without the desktop's entry script being requested.
    The phone runs the desktop's Consult turn, so the mocked
    POST answers 202 and the answer arrives on a mocked step stream with a
    turn contract carrying a cost stop (the block must read "Partial answer");
    the request must carry the window, the grounding mode and an on-screen
    report on the free route with no `allow_writes`. A second question's stream
    is held open and Stop is pressed: one stop call, one synthesize call on the
    free route, the answer lands and Ask returns (`W12d`/`W12e`). Stream, stop
    and synthesize are all answered inside the page.
14. The installable phone app, each a step that fails
    the walk: the manifest parses and meets the install criteria (scope `/m/`,
    standalone, 192 + 512 + maskable icons, each decoding at its declared
    size, and the Home Screen icon); the worker registers with scope `/m/` and
    controls the phone page, while the desktop at `/?desktop=1` is neither
    controlled nor registered; Cache Storage holds `/m/` shell files only, with
    no `/api/` entry; offline (the context offline AND the walk's static server
    dropping every connection) a reload draws the shell and shows
    "No connection" (`W14`), still with no `/api/` in any cache; back online the
    line goes; a byte-different worker (the walk serves `sw.js` plus a comment)
    waits, "Update available" shows and nothing reloads by itself (`W15`), and
    the Reload tap hands over with one reload.

## What fails it

| Budget | Value | Enforced |
|---|---|---|
| Longest main-thread task, per step | ≤ 1,000 ms | yes |
| Phone shell, per screen and width: API requests and bytes (fixture), duplicates, the longest task (≤ 200 ms), text under 12 px, controls under 44 × 44 px (inline text links and citation chips exempt), sideways scroll, the bottom bar on screen, engine words | see `budgets.mjs` `phoneScreens` | yes |
| Phone JS (gzip): the initial entry; everything loaded before any tap; no map library before "Open the globe" | ≤ 260 kB; ≤ 365 kB; none | yes |
| Page answers within 10 s at every step | — | yes |
| Landing ready | ≤ 8 s on fixtures, ≤ 20 s live (`--ready-ms`) | yes |
| Horizontal page overflow at 1680, 1366, 1920, 2560, 3840, 390 | none | yes |
| Consult question box width at every desk width | ≥ 300 px (`--consult-min`) | yes |
| An empty band: a full-height strip of the main area with no text, map or control | ≤ 6 % of the viewport width | yes |
| Body text: the median visible text size in the main area | ≥ 12 px at 1366–1920, 13.5 at 2560, 18 at 3840 (the 12 px label size × the frame's root step) | yes |
| The chat's reading width | 30–52 rem of the root size | yes |
| The chat divider, the centre's edge, a panel splitter | each moves ≥ 20 px when dragged and is the same (± 6 px) after a reload | yes |
| Consult moved, collapsed, popped out and back | each works and leaves the box ≥ 300 px and the page usable | yes |
| A "0" shown while no count has been read | none | yes |
| Page errors and console errors | none (the event-stream WebSocket, blocked outside origins and the sign-in 401 are excluded) | yes |
| A machine id as a title on the phone reader | none | yes |
| Phone landing requests, bytes, largest response, duplicates | see `budgets.mjs` (fixture and live) | yes |
| Phone: opening one read | ≤ 2 requests, bytes per `budgets.mjs` | yes |
| Phone composer: the question box's share of the row at 360 and 390; Ask on screen and ≥ 44 px | ≥ 60 % | yes |
| Phone Consult answer: no literal `**` or pipe-table text, a table element, no raw row ids | — | yes |
| Phone Consult answer: every citation a button with a 44 px target | ≥ 1 citation, all reachable | yes |
| `/favicon.svg` and `/m/favicon.svg` | an image type | yes |
| Phone Consult turn: the answer streams (POST 202, answer on the stream); the contract is drawn with "Partial answer" for a cost stop; the request carries window, grounding, on-screen, `core`, no writes; Stop stops, synthesizes, lands the answer and returns Ask | — | yes |
| Every walk step completes (sign-in shown, subject opens, Flow refuses) | — | yes |
| The Ledger: a score or a Brier figure while fewer than five forecasts have resolved | none | yes |
| The Ledger's requests and bytes (click to quiet) | see `budgets.mjs` | yes |
| Reader text under the floor | 12 px (`--text-floor`), on the four reader surfaces, the Record sheet and the phone | yes (`--report-text-floor` reports it without failing) |
| Landing requests (navigation to 3 s past "ready") | see `budgets.mjs` (fixture and live figures) | yes |
| Landing response bytes | see `budgets.mjs` | yes |
| Largest single landing response | see `budgets.mjs` | yes |
| Identical requests (same path and query) asked twice in the landing | 0 | yes |
| Query fetches made for a region nobody can see (a hidden tab, the closed palette) | 0 (read from the console's own `window.__legbaFetchLog`, `src/lib/queryLayer.tsx`) | yes |
| The Board map ready, from navigation | ≤ 12 s on fixtures, ≤ 20 s live | yes |
| The Watch page's requests, bytes and duplicates (opened by its address) | see `budgets.mjs` | yes |
| Watch steps (log drawn, filter without a read, point, both sides of a silence, a flag's versions, the designed empty state) | complete | yes |
| Map marks drawn | ≤ the layer caps summed (3,310) | yes |
| Map steps (ready, a layer toggled on, a place selected) | complete | yes |
| The Record sheet ready, from a Board row and from a Consult citation (click → the chain read to its last step) | ≤ 5 s on fixtures, ≤ 15 s live | yes |
| The Record sheet's own reads per open (the depth-1 lineage walk, receipts, integrity) | ≤ 3, and ≤ 0.15 MB on fixtures (2 MB live) | yes |
| The Record sheet's reads are bounded | lineage depth 1, ≤ 8 ids per receipts read, stored text ≤ 20,000 characters | yes |
| The Record sheet's address | `#rec=` names the record | yes |
| "Show what was received" | answers (text, or why it is withheld) in ≤ 1 read | yes |
| The Record sheet's reads in the landing | 0 | yes |

For the record it also prints, per surface: tiles and groups, visible text
elements, text under the floor (with samples in `result.json`), and engine words
it saw on the page (substrate, spine, `severity_delta`, "C-A R3", …).

## The landing's request budget

The walk logs every API request it answers (path and query, start, duration,
status, response bytes — never a header) into `result.json` as `requestLog`,
tagged with the walk phase; `result.landing` is the landing window's summary
(requests, bytes, `/findings` bytes, the largest response, duplicates, hidden
fetches). The landing window runs from navigation until 3 s past "ready", so
work deferred to idle is still counted.

A HELD page (steps 2 and 5, every answer held to catch a count printed before
anything was read) is closed with its held requests refused in the browser:
they never reach the registry. `--forward-held` releases and forwards them
instead, for comparison.

`--no-map-routes` answers the map's three read routes (`/v3/map/series`,
`/v3/map/at_sea`, `/v3/map/subject_reads`) with 404, as a registry not yet
rebuilt with them does: the subject reads fall back to the full-weight
`/findings` read and the series and incidents-at-sea rows say they cannot be
read. The fallback must still draw the map and keep every budget on fixtures.

Some `/findings` fixtures are DERIVED rather than recorded: the light
projections the landing asks for (`fields=summary`, `limit=1`,
`limit=3`) are cut from the recorded full-weight entry of the same query —
the first N rows, and for `fields=summary` each row reduced to the route's
`FindingSummaryRow` field set — so the fixture bytes reflect the lighter
weight. A live `--record` replaces them with recorded answers. The phone adds its
own: the navigator's `analyst_id_in=…&limit=80&fields=summary` page, and for
each of its rows the open-a-read query (`analyst_id=…&target_id[_null]=…&since=
<produced_at>&limit=N`) answered with that recorded full row.

## Running it

Both modes run in `mcr.microsoft.com/playwright:v1.49.1-noble` (Chromium 1148);
`run.sh` installs `playwright-core` into `tools/walk/node_modules` once.

```bash
# fixture mode: a bundle built from this tree + the committed synthetic fixtures; no network
legba-ui-v3/tools/walk/run.sh fixture --build     # --build rebuilds dist/ first (vite only)

# live mode: the live bundle (volume legba_ui_dist) + the live registry, GET-only
export LEGBA_TOKEN="…the registry bearer…"        # read from the environment, never printed
legba-ui-v3/tools/walk/run.sh live

# live mode with sign-in on (no bearer reaches a browser): an account instead
export LEGBA_WALK_EMAIL="walk@…"                   # passed to the container by name
export LEGBA_WALK_PASSWORD_FILE=/path/to/password   # a file holding the password, or a `password: …` line
legba-ui-v3/tools/walk/run.sh live

# extra walk arguments go after --
legba-ui-v3/tools/walk/run.sh live -- --no-shots
```

With an account, the walk signs in through the real sign-in screen (the one
write it lets through is `POST /auth/sign-in`; the session cookie stays in the
walk's memory) and walks as that account. A check that rests on a read or an
act above the account's role (`signin.mjs` `ROLE_GATED`, from
`route_policy.py`) is **skipped and named** with the role it needs, never
failed and never passed; a 403 is counted per route under "REFUSED for the
account's role" and kept out of the request and error budgets. Fixture mode
has no account and runs every check.

Output (`report.txt`, `result.json`, screenshots `W01…W12`) goes to
`$LEGBA_WALK_OUT`, by default `/tmp/legba-walk/<mode>-<utc>/`. Live screenshots
show your instance's data; keep them on the host. Exit 0 = pass, 1 = a budget failed,
2 = the walk could not run.

To walk a locally built bundle against the live registry (for example before a
deploy, or to record fixtures with the code that will ship):
`LEGBA_WALK_LIVE_DIST=$PWD/legba-ui-v3/dist legba-ui-v3/tools/walk/run.sh live`.

## Where it runs

- **The deploy gate.** `scripts/run_targeted_gate.sh` runs the fixture walk
  after `tsc`, vitest and the two lint ratchets whenever UI files changed (the
  `walk` phase; `walk=ok|fail` on the `GATE_VERDICT` line; the report and
  screenshots in the gate's log directory under `walk/`).
- **After every UI deploy**, a live walk (docs/operations/rolling-deploy.md,
  step 7).

## GET only, and the token

Every non-GET request to `/api/` is answered inside the browser with a 204 and
never reaches the registry. Consult and model routes are refused even as GETs.
Requests to any origin other than the local bundle server are blocked. In live
mode the bearer comes from `LEGBA_TOKEN`, is passed to the container by name
(never on a command line), and every string the walk writes is scrubbed of it.
With an account, the password file is mounted read-only into the container and
read only there; the password and the session cookie are scrubbed the same way.

## The fixtures

`fixtures/api.json` holds GET responses in their live **shapes** with the
content replaced. It is recorded from a live walk and sanitized before
it touches the disk (`sanitize.mjs`):

- strings the UI source names as literals (enums, route pieces), timestamps,
  numbers and generic state words are kept;
- identifiers are mapped to synthetic tokens (`ex_3k9f1q`), consistently across
  bodies and request paths, so a list row still opens its detail route;
- prose is rewritten word by word into neutral filler, keeping numbers,
  punctuation and markdown; links become `https://example.org/r/…`;
- outer arrays are cut to 10 items, nested ones to 4 and deeper ones to 2 (the
  Flow graph keeps all its nodes, so the budget still has something to refuse,
  and its first 500 edges), long strings to 600 characters at a word boundary,
  and pagination stops at the first page;
- the mapping key is random per recording and never written down.

`walk.test.ts` checks the sanitizer and runs a leak check over the committed
file: every string must be filler, a UI literal, a mapped id, a number or a
date. To re-record (after a route's shape changes, or when the walk reports
requests "not in fixtures"):

```bash
LEGBA_TOKEN=… LEGBA_WALK_LIVE_DIST=$PWD/legba-ui-v3/dist legba-ui-v3/tools/walk/run.sh live --record
```

then run vitest (the leak check) before committing the new file.

## Files

- `walk.mjs` — the walk.
- `budgets.mjs` — the budgets and the report (pure; unit-tested).
- `pick.mjs` — the map step's place pick, run in the browser by its source (unit-tested).
- `sanitize.mjs` — the fixture sanitizer and the leak check (pure; unit-tested).
- `run.sh` — the runner for both modes.
- `fixtures/api.json` — the synthetic fixture set.
- `fixtures/watch.json` — the Watch surface's invented example answers
  (hand-written, never recorded, overlaid on `api.json`; `walk.test.ts` checks
  every source is `source.example.*` and every page `example.org`).
- `walk.test.ts` — vitest over the pure parts and the committed fixture.
