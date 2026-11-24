<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
<p align="center">
  <img src="logo_small.png" alt="Legba" width="400">
</p>

# Legba

Legba is a self-hosted system for following a set of subjects over time from sources you choose. It
collects items from those sources, runs scheduled analysis over them with a language model, and stores
the results as short written findings in which each claim cites a stored source item.

It runs on a single host under Docker Compose and is single-tenant. You supply the model endpoints.
The repository includes one example configuration, for open-source geopolitical analysis, which is
what you get if you deploy without changing anything. The platform itself has no domain built in; the
example is a set of configuration files you can replace.

## What it does

- Collects items from RSS feeds, JSON and time-series APIs, GeoJSON, GDELT, Telegram channels,
  webhooks and fetched web pages. Each item is stored once, enriched once (language, translation,
  places, named entities) and routed to every subject whose filter matches it.
- Runs analysts on a schedule or when new items arrive. An analyst answers one narrow question about
  one subject and writes a finding: a headline, a few sentences with numbered citations to stored
  items, and indicators to watch.
- Checks each finding after it is written. A deterministic pass confirms that the claims carry
  citations; a model pass then asks whether each claim follows from what it cites, and stores a
  verdict per claim. A finding that fails is kept and marked low-confidence, not deleted.
- Records what it could not cover. Where a subject has no usable evidence on a question,
  `GET /api/v1/v3/absence?scope=<subject>` returns the kind of gap and what was checked, so "not
  checked" and "nothing found" read differently.
- Keeps a forecast ledger. A dated question is sealed with its probabilities before the outcome is
  known, resolved against a rule stated in advance (a person confirms most outcomes), and given a
  Brier score. No score is shown until five questions have resolved.
- Archives the pages that findings cite, so a citation still resolves after the original page changes.

A finding looks roughly like this. The example is illustrative, not output from a real run:

```
Supplier warns of delays at its main plant

The company told customers on 3 March that output at its main plant would fall for at least two
weeks [1]. A regional paper reported a fire at the site the day before [2]. No other source in the
collection mentions the plant this week.

Watch: a revised delivery date; any statement from the plant's operator.

[1] company press release, fetched 3 March   [2] local news report, fetched 2 March
```

In the console each `[N]` opens the stored item, the passage cited, the verdict for that claim, and
the fetch record with its hash.

The web console has four reader pages (Board, Subject, Ledger, Watch), a map, alert acknowledgement,
and Consult, which answers a typed question from the stored record and shows its citations and the
tools it called. A cut-down shell for phones is served under `/m/`. Alerts are sent to an
[ntfy](https://ntfy.sh) server; one is defined in `docker-compose.yml` under the `alerts` profile,
which `deploy.sh` does not start.

## Requirements

- A Linux host with Docker and Compose v2. The stack is large: Postgres, Redis, Qdrant, OpenSearch,
  NATS, Dapr, SearXNG and RSSHub run beside Legba's own services, and the memory limits in
  `docker-compose.yml` add up to about 34 GB. Host scripts and systemd units under `deploy/` assume
  a fixed install path; [docs/SETUP.md](docs/SETUP.md) says which, and what to edit if yours differs.
- An OpenAI-compatible chat endpoint (a self-hosted vLLM works) and an embedding endpoint. Every
  scheduled analyst uses the chat endpoint, and by default the claim-checking pass uses the same one.
- The NLP service in [`legba-models/`](legba-models/USAGE.md) for translation and entity
  extraction. It has its own compose file and expects an NVIDIA GPU. Without it Legba still runs, with
  poorer enrichment.
- A domain name for the Caddy edge if you want the console on the public internet. Caddy obtains the
  certificate.

Nothing is billed to a third party unless you point a route at a paid endpoint.

## Running it

```bash
cp .env.example .env      # then set the keys listed in docs/SETUP.md
docker compose --profile runtime build
deploy/deploy.sh
```

`deploy.sh` brings the stack up in order: databases and message bus, schema migrations, credentials
into the encrypted vault, then the descriptors (sources, subjects, analysts), then the application and
the Caddy edge. It is safe to re-run. It needs fresh volumes; there is no migration from older
versions. Options worth knowing:

- `--no-caddy` skips the edge. The registry API is then on `127.0.0.1:8090` and no console is served.
- `--profile DIR` picks the configuration to register (default `profiles/geopolitics`).
- `--seed` loads the profile's background data. The data itself is not in the repository;
  `seeds/*.example.yaml` show the format, and the step does nothing if the files are absent.
- `--teardown` stops the stack and keeps the volumes.

To check that it is working:

```bash
docker compose ps
curl -s http://127.0.0.1:8090/api/v1/registry/healthz
docker compose exec -T postgres psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT kind, count(*) FROM analyst_outputs GROUP BY kind;"
```

Items arrive within the first poll cycle; findings follow on each analyst's schedule. The console is
at `https://<LEGBA_PUBLIC_DOMAIN>/`. Out of the box it is behind a single basic-auth password and
everyone who has it is an admin. Setting `LEGBA_AUTH_SIGNIN=1` turns on individual accounts with the
roles reader, contributor and admin, plus one owner created from the command line. The steps are in
[Operating your instance](docs/OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in).

[docs/SETUP.md](docs/SETUP.md) has the full procedure and [docs/TOUR.md](docs/TOUR.md) walks through
the console once data is flowing.

## Configuration

An instance is defined by YAML files called descriptors, registered through the registry API. The
registry stores each one with a content hash and keeps an audit log of changes. The main kinds are:

| Prefix in `descriptors/` | Describes |
|---|---|
| `source_` | a feed or API: how to fetch it, how often, which filters and enrichment to apply |
| `target_` | a subject to follow and the filter that decides which items belong to it (the example calls these desks) |
| `analyst_` | a scheduled or triggered job: its question, its inputs, its model route, its cadence |
| `action_pack_` | a set of tools an analyst or Consult may call, and the grants they need |
| `stack_component_` | a model or service endpoint |

A profile under `profiles/` names which descriptors to register and supplies the labels, rosters and
prompts specific to one instance. The descriptors at the top of `descriptors/` are the geopolitics
example's. [`descriptors/examples/`](descriptors/examples/) has small annotated source examples, and
[docs/PROFILES.md](docs/PROFILES.md) describes the profile format.

Adding a feed of a kind Legba already handles, or a new subject, is a new descriptor and a
registration call. A new kind of source needs code.

## How it fits together

Sources and analysts both run as Dapr actors in the runtime service. A source turns each new item
into a signal, stores it in Postgres and publishes it on NATS; a fan-out step matches it against each
subject's filter and triggers the analysts that watch that subject. Signals are also indexed for
search in OpenSearch and Qdrant. Analysts call the model endpoints and write their findings, citations,
verdicts and lineage back to Postgres, and each row records which person, service or rule wrote it.
The registry is a Python API service that owns descriptors, credentials and the routes the console
reads. The console is a React application, built into static files and served by Caddy.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers this properly, and
[docs/CODE_MAP.md](docs/CODE_MAP.md) maps it onto the source tree.

## Limits

- One tenant, one node. There are no groups, no single sign-on and no per-source permissions.
- The claim check tests whether a claim follows from its cited sources, not whether it is true. A
  claim faithfully copied from a wrong source passes. Separate jobs grade findings against
  independently built references and against web search, but they cover a small share of what is
  written.
- Findings that combine several analysts' work are assembled from verbatim quotes of the underlying
  claims. Any interpretation is kept in a separate, labelled section and checked on its own.
- The forecast ledger makes no claim of skill until enough questions resolve.
- Some parts are built but off by default: the event model, the graph projection, on-request page
  fetching and stream subscriptions. Each is controlled by an environment variable.
- The platform is meant to be domain-neutral, but some prompts and modules still assume the
  geopolitics example's shape. The second profile in the tree is a skeleton and has not been run.

[docs/STATUS.md](docs/STATUS.md) gives the state of each capability, and
[docs/SEAMS.md](docs/SEAMS.md) lists what is deliberately not built and how each gap fails.

## Documentation

[docs/README.md](docs/README.md) indexes everything. The pages most people need are
[Setup](docs/SETUP.md), [Tour](docs/TOUR.md), [Runbook](docs/RUNBOOK.md),
[Architecture](docs/ARCHITECTURE.md), [Data sources](docs/DATA_SOURCES.md),
[AI models](docs/AI_MODELS.md) and [Tunables](docs/TUNABLES.md). Changes are in
[CHANGELOG.md](CHANGELOG.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the checks the tree enforces and why code contributions
need a CLA. Bug reports, and reports of places where the docs and the code disagree, need no CLA.

## Licence and contact

Copyright (C) 2026 Lewis George. Licensed under the GNU Affero General Public License, version 3 or
later; see [LICENSE](LICENSE). If you run a modified version as a network service, the licence
requires you to offer its source to the people who use it.

Contact: legba@civislux.us. Repository: <https://github.com/ldgeorge85/legba>.
