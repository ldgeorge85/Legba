/**
 * consultContext — what the operator's current posture contributes to a consult
 * turn, in the two forms the backend can read.
 *
 * Consult already had manual pins: the operator clicks "Pin selection" and the
 * record rides along on every turn. What it did NOT have is the ambient half —
 * the SCOPE, the report or desk the whole wall is currently about. So the model
 * was answering questions about "the world" while the operator was looking at
 * one region's read, and neither of them said so.
 *
 * ## Two channels, one source
 *
 *  1. `pinnedPrefix()` — the `[Pinned <kind>: "<label>" (id=…)]` text block that
 *     rides inside `question`. It works against ANY backend, including today's,
 *     which is why the flow ships UI-only.
 *  2. `pinnedContext()` — the structured `pinned_context` field
 *     (`[{kind, id, title, text}]`) a backend hydrates full record bodies from.
 *     The registry-side field is landing on its own lane; until it does, the
 *     prefix carries the whole load and this field is silently ignored, which
 *     is exactly the degradation we want (never a 422, never a lost turn).
 *
 * ## Why the scope pin is REPLACED rather than appended
 *
 * A scope is a single ambient fact — "the wall is about this" — so there is at
 * most one of it. Appending would grow an unbounded stack of stale scopes
 * across a morning's navigation and quietly poison every later turn. The manual
 * pin keeps its own semantics untouched: it pins FOCUS, and the operator can
 * hold three findings pinned under one report scope, which is the v2 working
 * posture the design is restoring.
 */
import type { Scope } from '@/state/scope'
import type { Selection } from '@/state/selection'

/** `Selection.origin` marking the ONE ambient, auto-managed pin. */
export const SCOPE_PIN_ORIGIN = 'scope'

/** One structured pinned record, as the `pinned_context` wire field. */
export interface PinnedRef {
  kind: string
  id: string
  title: string | null
  text: string | null
}

/** Whether a pin is the auto-managed scope pin (as opposed to a manual one). */
export function isScopePin(p: Selection): boolean {
  return p.origin === SCOPE_PIN_ORIGIN
}

/**
 * A one-line, entirely derived description of what a scope covers — the text
 * the model gets so it knows the aperture it is answering inside.
 *
 * Every number here is a length of a list the scope already holds. Nothing is
 * estimated, and an empty axis is omitted rather than printed as zero.
 */
export function scopeSummary(scope: Scope): string {
  const m = scope.members
  const parts: string[] = []
  if (m.targetIds.length > 0) parts.push(`${m.targetIds.length} desks`)
  if (m.findingIds.length > 0) parts.push(`${m.findingIds.length} findings`)
  if (m.signalIds.length > 0) parts.push(`${m.signalIds.length} signals`)
  if (m.sourceIds.length > 0) parts.push(`${m.sourceIds.length} sources`)
  if (m.asOf) parts.push(`as of ${m.asOf}`)
  if (m.windowHours != null) parts.push(`${m.windowHours}h window`)
  return parts.length > 0
    ? `The operator's wall is scoped to this ${scope.kind}: ${parts.join(' · ')}.`
    : `The operator's wall is scoped to this ${scope.kind}.`
}

/**
 * Project a scope into the pin shape the store already persists (`Selection[]`).
 *
 * A scope is not a selection, but it is addressed the same way — `{kind, id}` —
 * and reusing the pin list means the scope pin survives `api.clear()` and a
 * reload for free, through machinery that is already tested.
 */
export function scopePin(scope: Scope): Selection {
  return {
    kind: 'report',
    id: scope.id,
    label: scope.label,
    origin: SCOPE_PIN_ORIGIN,
    preview: { title: scope.label, body: scopeSummary(scope) },
  }
}

/** The `[Pinned …]` text block prepended to a question. Empty when no pins. */
export function pinnedPrefix(pins: readonly Selection[]): string {
  return pins.map((p) => `[Pinned ${p.kind}: "${p.label ?? p.id}" (id=${p.id})]`).join('\n')
}

/** Prepend the pin block to a question, or return the question unchanged. */
export function questionWithPins(question: string, pins: readonly Selection[]): string {
  const prefix = pinnedPrefix(pins)
  return prefix ? `${prefix}\n\n${question}` : question
}

/**
 * The structured `pinned_context` payload — `[{kind, id, title, text}]`.
 *
 * `text` is only what the caller ALREADY HAS (the optimistic preview body a row
 * click carried, or the scope's own derived summary). This module never fetches
 * a record body to fill it: a consult request must not fan out N reads on the
 * way to the model, and a hydrating backend can resolve the id far better than
 * the client can.
 */
export function pinnedContext(pins: readonly Selection[]): PinnedRef[] {
  return pins.map((p) => ({
    kind: p.kind,
    id: p.id,
    title: p.label ?? p.preview?.title ?? null,
    text: p.preview?.body ?? null,
  }))
}
