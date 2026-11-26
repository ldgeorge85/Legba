/**
 * pageLoad — a counter for "this page load", for module state that must be
 * resolved once per load (the one "since", `lib/morningReadBands::pageVisit`).
 * A leaf: it imports nothing, so the test setup can start a fresh load before
 * every test without loading the profile ahead of a test's own mock of it.
 */
let generation = 0

/** The current page load's number. */
export function pageLoadGeneration(): number {
  return generation
}

/** Start a new page load (tests: each test is its own load). */
export function __newPageLoad(): void {
  generation += 1
}
