# The lifecycle overlay

Descriptor files ship the state their header documents, usually `draft`, so registering a file
never schedules anything; activation is an admin's step. The overlay records the state an instance
holds where it differs from what the file ships (an activation through the lifecycle route, or a
pause by the source circuit breaker). `scripts/bringup_register_descriptors.py` registers each file,
then moves the head along the legal lifecycle path to the state its row records, so a fresh instance
comes up the same way.

The rows here are the example profile's overlay; another instance keeps its own. A descriptor
whose shipped state is already the instance's state has no row. A row that names nothing the walker
plans, or names the state the file already ships, fails the plan. Removing a row returns that
descriptor to its shipped state on the next apply (the dry run names it as an `update` first).

Subscribed sources (`acquisition: subscribe`): none ships and none has a row. An active subscribed
source only connects while the runtime's subscribe supervisor is on (`LEGBA_SUBSCRIBE_SUPERVISOR=1`).
With it off, the source writes no window rows and the watchdog raises `source_stream_down`
(`silent`) after three windows. Pausing or retiring the head stops the connection on the
supervisor's next pass.

## Where the rows are

One row per file in [`LIFECYCLE.d/`](LIFECYCLE.d/): `<family>__<id>.md` holds that row's table line
(`| family | id | state | file ships |`). The walker parses the rows and nothing else, so keep the
four columns. It also reads any row left in this file; a row named in both places fails the plan.

    python3 scripts/lifecycle_rows.py set analyst kev_digest active draft
    python3 scripts/lifecycle_rows.py unset analyst kev_digest
    python3 scripts/lifecycle_rows.py print      # the whole table
    python3 scripts/lifecycle_rows.py --check    # in the gate
