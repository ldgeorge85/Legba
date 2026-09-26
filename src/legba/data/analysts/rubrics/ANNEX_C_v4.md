# ANNEX C v4 — the three-label correctness rubric (Program 1, revision 1)

You are grading ONE assertion from a country read against ONE committed reference for the same
country and window. The reference is a list of developments (each with a summary, a decisive span,
an outlet and a date) plus a band per dimension. You cannot browse. You grade from the reference's
developments exactly as written; nothing else counts.

## The one question
Does the reference SUPPORT the assertion's core factual claim?

## The three labels (exactly one per atom)
- **contains** — a development states the core claim's MATTER: the same event, actor, or condition,
  whether the development phrases it more broadly or more narrowly than the assertion does. Exact
  wording is not required; a different event is not enough.
- **contradicts** — a development states something incompatible with the core claim: a different
  outcome, actor, direction, or timing for the same matter; OR the core claim says something did not
  happen, did not change, or remains as it was, and a development reports that it did happen or did
  change in that same matter.
- **silent** — no development bears on the core claim's matter at all. The claim may well be true;
  you cannot tell from this reference.

## Rules (binding)
1. **Grade the CORE claim only.** The core claim is the main clause of the assertion: what it says
   happened, is happening, or is the case. Write it in your own words in `core_claim` (≤ 25 words)
   before you choose a label.
2. **Modifiers do not count.** Hedges ("likely", "remains"), severity and band words ("moderate",
   "elevated", "watch"), source counts, and citation markers are NOT part of the core claim and never
   decide the label.
3. **Absence claims are claims.** "No new incidents", "no material change", "has not deteriorated",
   "continuity across dimensions", "neither X nor Y is driving Z" each assert that something did not
   occur. If any development reports an occurrence in that matter, the label is `contradicts`. Such a
   claim is `silent` only when no development bears on that matter.
4. **No partial label.** If part of the core claim is contained and part is contradicted → `contradicts`.
   If part is contained and the rest is silent → `contains`. Only when nothing is contained and nothing
   is contradicted → `silent`.
5. **Tier, hindsight, and source quality are not your job.** Do not downgrade for a weak outlet, do not
   reason about what you know from elsewhere, do not consider publication dates except to read what
   the reference says happened when.
6. **Bands are not evidence.** The band table never decides a label. A decisive span quoted from the
   band table is malformed and will be returned to you.
7. **Decisive span.** For `contains` and `contradicts`, quote the ONE development sentence or fragment,
   verbatim from a development's summary or decisive span, that decided it, in `decisive_span`. For
   `silent`, leave `decisive_span` empty.
8. **One line of reason**, in `reason`, ≤ 30 words, naming the rule that decided it.

## Output contract (one JSON object, nothing else — no prose, no code fence)
{"verdict": "contains|contradicts|silent", "core_claim": "<≤25 words>", "decisive_span": "<verbatim or empty>", "reason": "<≤30 words>"}
