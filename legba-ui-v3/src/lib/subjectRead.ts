/**
 * subjectRead — what a click on a SUBJECT does (X3/D3; review D §C5).
 *
 * A subject picked from the rail (or ⌘K) used to be a bare `focus`: the
 * Inspector opened on the subject's DESCRIPTOR (allowed_action_packs,
 * cycle_hop_limit, a 64-hex version) while the Morning Read beside it kept
 * showing another subject. The click now compiles to three drive actions, in
 * this order, through the ONE executor:
 *
 *   1. `set_scope {target}` — the wall is about this subject: the Live Feed
 *      filters to it and the Morning Read re-scopes to it;
 *   2. `focus {target}`     — the Inspector and Consult's pin follow it (the
 *      Inspector opens on the subject's READ STATE; the descriptor is one
 *      click further, under "Configuration");
 *   3. `open_panel subject.page {subject, tab: brief}` — its page, on the
 *      brief.
 *
 * X4/D4: `lib/commands.ts::railActions.subject` now compiles exactly these
 * three, and this is the one definition (`subjectReadActions` below IS
 * `railActions.subject`) — the rail, ⌘K and any other caller of either name get
 * the same read. The on-screen noun lives in the leaf `lib/subjectNoun.ts`.
 */
import type { WorkstationAction } from '@/lib/applyAction'
import { railActions } from '@/lib/commands'

/**
 * A click on a subject: scope the wall, focus it, open its read. The order is
 * the contract (the scope lands first so the page opens already scoped).
 */
export function subjectReadActions(id: string, label?: string): WorkstationAction[] {
  return railActions.subject(id, label)
}
