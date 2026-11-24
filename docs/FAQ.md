<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# FAQ

Short answers to the questions people ask before trying Legba, each linked to the page with the
details. Terms are in the [Glossary](GLOSSARY.md); what is built and what is not is in
[STATUS.md](STATUS.md).

**What does Legba do?**
It collects items from the sources you configure, routes each one to the subjects that want it,
and runs analysts over them. The analysts write short reads in which every claim cites its source.
A checking pass then scores each claim against what it cites. You declare sources, subjects and
analysts in YAML descriptors. [Architecture](ARCHITECTURE.md).

**What is the example instance?**
An instance is the platform plus a profile: the descriptors, rosters, prompts and console labels
for one domain. The repository ships the `geopolitics` profile as a worked example (country desks,
regional and world reads) and a `sanctions_export_control` skeleton. Numbers you see from the
example belong to that example. [Instance profiles](PROFILES.md).

**What do I need to run it?**
One host with Docker and Compose v2, and the model endpoints below. `deploy/deploy.sh` brings the
stack up. [Setup](SETUP.md).

**Which models does it need?**
An OpenAI-compatible chat endpoint for the analysts, an embedding endpoint, and the `legba-models`
service for language detection, translation and entity extraction. Legba hosts no model itself.
By default the checking pass uses the same chat endpoint; you can point it at a different one.
Consult can also use a hosted provider if you configure one. [AI models](AI_MODELS.md).

**What does it cost to run?**
Your hardware and whatever your model endpoints charge. Every scheduled analyst uses the endpoint
you register. Paid providers (a hosted judge, paid search, a hosted Consult model) are used only if
you configure them, and each has a spending limit. [Tunables](TUNABLES.md).

**Does it check its output?**
Three ways, reported separately. The checking pass scores whether each claim follows from what it
cites; it cannot tell a well-cited claim from a true one. The correctness grader scores subjects
against references built without the platform's own data. The standing auditor checks sampled
claims against web search. The grader and the auditor ship `draft`. [Analysis](ANALYSIS.md).

**Does it forecast?**
Only through the forecast ledger: sealed questions, each with a resolution rule and a date. A
resolver proposes each outcome and a person decides it. The scorecard shows no score until five
questions have resolved. [Forecast ledger](LEDGER.md).

**How do I add a source?**
For a feed of a kind that already exists (`rss`, `json_api`, `geojson` and others), add a source
descriptor under your profile's descriptors, or an entry in its source catalog, and redeploy. A
new kind of source needs code. [Sources](DATA_SOURCES.md).

**How do I add a subject?**
Register a target descriptor with a scope and tags. The analysts pick it up through their
predicates; no code is needed. [Instance profiles](PROFILES.md).

**Who can see and change what?**
Out of the box, one perimeter password at the edge, and everyone who knows it is the same admin.
With `LEGBA_AUTH_SIGNIN=1`, people sign in with their own accounts and hold one of three roles:
reader, contributor or admin, plus one owner. [Sign-in](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in),
[API roles](API_ROLES.md).

**Is there a phone app?**
The reader pages work in a phone browser at `/m/`. With sign-in on, that page installs as a web
app. There is no app-store app and no web push; alerts go to ntfy. [The phone shell](UI.md#the-phone-shell).

**What is not built?**
Groups, single sign-on, per-source permissions, multi-tenant isolation, horizontal scale-out and
media extraction models, among others. Each is listed with what happens if you reach it in
[SEAMS.md](SEAMS.md).

**What is the licence?**
AGPL-3.0-or-later. Its network clause applies if you offer Legba to others over a network. Outside
code contributions need a contributor licence agreement ([CONTRIBUTING.md](../CONTRIBUTING.md)).
