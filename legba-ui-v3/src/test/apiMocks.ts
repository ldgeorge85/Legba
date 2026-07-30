/**
 * Shared vitest fetch-mock helper for a fake non-ok `Response`.
 *
 * `lib/api.ts`'s `readErrorBody` reads every non-2xx body via `res.text()`
 * ONCE, then opportunistically `JSON.parse`s it (a single-use stream can't be
 * read twice, so the old `try { res.json() } catch { res.text() }` pattern is
 * gone — see the comment on `readErrorBody`). A hand-rolled fake `Response`
 * like `{ ok: false, status, json: async () => body }` that omits `text()`
 * throws `res.text is not a function` the instant a panel under test hits
 * that non-ok path — the fixture bug that broke DescriptorBuilder / Claims /
 * Findings / Signals / Situations (all built their own copy of this shape).
 * Build 404/422/etc fakes through here so every one carries `text()` too.
 */
export function mockErrorResponse(status: number, body: unknown): Response {
  const raw = JSON.stringify(body)
  return {
    ok: false,
    status,
    json: async () => body,
    text: async () => raw,
  } as unknown as Response
}
