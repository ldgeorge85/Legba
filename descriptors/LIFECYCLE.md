# Lifecycle states the reference host holds that the files do not ship

Descriptor files ship the state their header documents — usually `draft`:
registering a file must never schedule it; activation is an operator's flip.
On the reference host those flips happened (through the lifecycle route, or a
source circuit-breaker pause). The rows RECORD them, so
`scripts/bringup_register_descriptors.py` brings a fresh instance up the way
the reference host runs: it registers the file, then walks the head along the
legal lifecycle path to the state below. A descriptor whose shipped state is
already the host's state has no row. A row naming nothing the walker plans, or
naming the state the file already ships, fails the plan.

This is the reference host's overlay, not a property of the descriptors: a
different instance (a focused instance's profile) carries its own. Removing a
row returns that descriptor to its shipped state on the next apply (an
`update` the dry run names first). The walker parses the rows and nothing
else, so keep the four columns.

Recorded 2026-10-04 (W6 SPLIT-1) from the live heads behind
`tests/fixtures/bringup/live_head_roster_2026-10-04.json`.

SUBSCRIBED sources (`acquisition: subscribe`, lane I-4): none ships and none
has a row. When one is flipped `active`, the state alone holds no connection:
the runtime's subscribe supervisor must be on (`LEGBA_SUBSCRIBE_SUPERVISOR=1`)
for a connector to take the stream; with it off the source writes no window
rows and the watchdog raises `source_stream_down` (`silent`) after three
windows. Pausing or retiring the head stops the connection on the
supervisor's next pass (queued events landed, the final window written).

## Where the rows are (R-1, 2026-11-14)

One row per file, in [`LIFECYCLE.d/`](LIFECYCLE.d/): `<family>__<id>.md`
holding that row's table line (`| family | id | state | file ships |`). The
single table that used to sit here was edited by fifteen commits in three
days; with a file per row, two lanes recording different flips never touch
the same file. The walker reads the directory (and any row still written in
this file — a row named in both places fails the plan).

    python3 scripts/lifecycle_rows.py set analyst kev_digest active draft
    python3 scripts/lifecycle_rows.py unset analyst kev_digest
    python3 scripts/lifecycle_rows.py print      # the whole table
    python3 scripts/lifecycle_rows.py --check    # in the gate
