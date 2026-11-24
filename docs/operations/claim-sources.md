# Switching on a claim source

A claim source carries a party's own claim: a group saying it attacked
someone, or a tracker listing what such groups posted. What a claim row can
support is fixed in code ([DATA_SOURCES.md, "Claim sources"](../DATA_SOURCES.md#claim-sources)):
"<claimant> claimed / posted / listed ...", attributed and dated, never the
event itself. This note is what an owner does to switch one on, what they will
see afterwards, and what the platform refuses. Every name below is invented.

Two claim descriptors ship: `source.ransomware_live.claims` and
`source.ransomlook.claims`. Both read an aggregator's RSS feed and name the tracker as the claimant.

## Before switching on

1. **Read the feed yourself.** Fetch a week of it by hand and read it as text.
   Check what the items carry: a structured claimant field (a group name per
   item), links, the kinds of text. If items name private people as victims,
   do not switch the source on: the platform does not redact people's names
   (see "What the platform does to the text").
2. **Read the terms** of the site and record the licence class you find in
   `scope.license_class`.
3. **Declare the claimant.** `scope.claim.claimant` is the name every row is
   attributed to when the item gives no claimant field. If the feed names the
   group per item, set `scope.claim.claimant_path` to that payload field
   (for example `item.group_name`). If the claimant is written several ways
   ("Nightjar", "NJ Team"), list the others in `scope.claim.claimant_aliases`;
   the attribution check accepts any of them.
4. **Record the sign-off** with a PUT of the whole descriptor body (the route
   needs `admin`). The `note` is where you write what you read and what you
   accepted.

Example for a whole source (`scope.source_class: claim`):

```bash
# descriptors/source_example_tracker.yaml, with the claim block filled in:
#   scope:
#     source_class: claim
#     claim:
#       claimant: "Example Tracker (leak-site tracker)"
#       claimant_aliases: ["the tracker"]
#       verb: listed
#       sign_off:
#         by: "owner:alex.example"
#         at: "2026-11-20T09:00:00Z"
#         note: >-
#           Read the feed 13-19 Nov: item title + victim organisation + date +
#           group; no files, no personal data seen. Terms read: personal use,
#           attribution required. Accepting it as a claims listing only.
python3 -c 'import json,sys,yaml; json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)' \
  descriptors/source_example_tracker.yaml > /tmp/body.json
curl -sX PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/registry/descriptors/source/source.example_tracker.claims -d @/tmp/body.json

curl -sX POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/registry/descriptors/source/source.example_tracker.claims/transition \
  -d '{"to_state":"active","reason":"claim source signed off 2026-11-20"}'
```

Example for one Telegram channel on the shared Telegram descriptor (the
channel must already be in `config.channels`):

```yaml
config:
  classes:
    factory_kind: dict
    key_kind: text
    value_kind: text
    raw:
      exampleclaims: claim          # the channel's class
  claims:                           # one declaration per claim channel
    exampleclaims:
      claimant: "Nightjar"
      claimant_aliases: ["NJ Team"]
      verb: posted
      sign_off:
        by: "owner:alex.example"
        at: "2026-11-20T09:00:00Z"
        note: "Channel read 13-19 Nov: the group's own posts; text only; no personal data seen."
```

The PUT of the Telegram descriptor carries its live state (`active`), so it is
refused unless every claim channel has its own `sign_off`.

## What you will see

- **Every row is stamped** `payload.claim = {source_class: claim, claimant,
  claimant_from, verb}` (plus `aliases`, and `channel` for a Telegram
  channel). `GET /signals` shows `evidence_class: claim` and the claimant; an
  analyst's slice shows `CLAIM by <claimant> — supports only ...`; the
  console's citation chip reads `claim · <claimant>`.
- **The poll record counts the text rules.** `source_poll_outcomes.claim_hygiene`
  (migration 0265) holds, per poll, `{"rows": n, "url_to_host": n,
  "onion_link": n, "email": n, ...}`: kinds and counts, never a value.
- **Sentences are checked.** A sentence resting only on claim rows passes the
  verify floor only when it presents the claim as a claim ("Nightjar posted
  Acme Widgets ..."). A sentence that states the event ("Acme Widgets was
  breached"), says it through a factive verb ("Nightjar confirmed ..."), or
  adds an unattributed event clause ("..., and the plant was shut down")
  fails as `claim_as_fact`. Severity is soft (counted) unless
  `LEGBA_VERIFY_CLAIM_AS_FACT=hard` ([TUNABLES.md](../TUNABLES.md)).
- **One hop up.** A composition that cites a read which rested only on claim
  rows carries the claim stamp on that citation (`hop: 2`), and the same
  check applies to the composition's sentences. A fact whose lineage is only
  such reads is refused like a fact on claim rows.
- **Bearing edges say so.** A `claim_watch` edge from a claim row carries
  `evidence_class: claim` and the claimant in `bearing_edges.data`.

## What the platform does to the text

Before a claim row is enriched or stored (`src/legba/data/claim_hygiene.py`):
every URL becomes its host (`[link: files.example.net]`); an onion address
becomes `[onion link]`; the row's own address is kept only on the source's own
host (the tracker's item page, a `t.me` message) and otherwise becomes a
non-resolvable key `claim-item:<hash>`; email addresses, phone numbers,
government-id-like numbers, payment-card-like numbers, IBANs, street
addresses (where the pattern is reliable) and credentials are replaced by
their kind (`[email]`); three or more file names or record lines become
`[lists N files]` / `[sample data: N rows withheld]`. Organisation names stay:
they are the claim. **Names of private individuals are not redacted**: code
cannot tell a person's name from an organisation's reliably, and a partial
redaction would read as a guarantee.

The raw fetch is still kept as the poll's receipt (`fetch_versions`). Its
stored text is for the `admin` role and the platform's services only:
`claim_support.claim_text_withheld` answers `claim_text_admin_only` for
anyone else, and the Record sheet's version-text route
(`GET /v3/record/version/{id}/text`) applies it (SEAMS #90, resolved).

## What the platform refuses

| Attempt | Answer |
|---|---|
| `source_class: claim` without a `scope.claim` block, or the block on another class | 422 at save |
| A claim source with a `fact_extractor` stage, a `follow` policy, `pipeline.media: eager`, a media object store, or a non-document sink | 422 at save |
| A Telegram channel marked `claim` without `config.claims.<channel>`, or a declaration for a channel not marked `claim` | 422 at save |
| A descriptor with a claim channel and a `follow` policy, media retain or a non-document sink | 422 at save |
| `active` without the sign-off (the source's, or every claim channel's) | 422 at save; 409 `claim_sign_off_required` on the transition route (it names the channels) |
| An intake request born of a claim row's link to a host no ordinary source covers | refused `claim_link_refused` |
| A fact whose lineage is only claim rows, or only reads that rested only on claim rows, with an event predicate | refused `claim_only_lineage` |
| A claim row counted as an independent source, a corroboration, an event's second source or a credibility input | never counted |
