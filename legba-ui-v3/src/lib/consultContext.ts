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
import { useEffect, useId, useMemo } from 'react'
import { create } from 'zustand'
import { useScope, type Scope } from '@/state/scope'
import type { Selection, SelectionKind } from '@/state/selection'

/** `Selection.origin` marking the ONE ambient, auto-managed pin. */
export const SCOPE_PIN_ORIGIN = 'scope'

/**
 * The kinds the registry's `pinned_context` field accepts
 * (`src/legba/data/pinned_context.py::PINNED_KINDS`). Anything else is a 422
 * at the front door, so a pin of another kind (an event the Timeline reads,
 * the roster the Accuracy page reads) rides the `[Pinned …]` text prefix only.
 */
export const PINNED_CONTEXT_KINDS: ReadonlySet<string> = new Set([
  'report',
  'finding',
  'meta_finding',
  'entity',
  'target',
  'signal',
  'situation',
  'source',
  'analyst',
  'alert',
  'critique',
  'hypothesis',
  'prediction',
])

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
  return pins.filter((p) => PINNED_CONTEXT_KINDS.has(p.kind)).map((p) => ({
    kind: p.kind,
    id: p.id,
    title: p.label ?? p.preview?.title ?? null,
    text: p.preview?.body ?? null,
  }))
}

// ---------------------------------------------------------------------------
// THE PIN HOOK (UI-B6; UI-D1 §2.1, the 09-26 research note §4).
//
// Every object surface tells Consult what it is reading, so the ONE ambient
// pin (`SCOPE_PIN_ORIGIN`) follows the reader from surface to surface and the
// same session keeps answering about the object on screen — with its
// provenance census still reporting what each answer rested on.
//
// Why a CLAIM LIST and not `setScope`. The scope is what the wall FILTERS by,
// written only by deliberate acts (`state/scope.ts`); a unit, a source or the
// whole roster has no scope kind, and a surface that set the scope could not
// let go of it on close without un-filtering the wall. So each surface CLAIMS
// the pin while it reads an object and RELEASES the claim when it reads none
// or unmounts; the wall scope is one more claimant. The ambient pin is the
// NEWEST live claim — the last thing the reader turned to — and a released
// claim can never leave a stale pin behind.
// ---------------------------------------------------------------------------

/** The kind of object a surface can pin: a record kind, or the whole roster. */
export type PinObjectKind = SelectionKind | 'roster'

/** What a surface is reading. */
export interface PinObject {
  kind: PinObjectKind
  id: string
  label?: string | null
}

/** The roster-wide pin (the Accuracy page reads every unit over every subject). */
export const ROSTER_PIN: PinObject = { kind: 'roster', id: 'roster', label: 'the whole roster' }

/** One live claim on the ambient pin. */
export interface PinClaim {
  /** Unique per mounted surface instance. */
  key: string
  /** The surface's name, as the census line prints it ("timeline", "rail"…). */
  surface: string
  object: PinObject
  /** Monotonic — the newest claim is the pin. */
  seq: number
}

/** The wall scope's claim key (its surface name is "scope"). */
export const WALL_SCOPE_CLAIM = 'wall-scope'

let claimSeq = 0

interface AmbientPinState {
  claims: PinClaim[]
  /** Claim (object) or release (null) the pin for one surface instance. */
  claim: (key: string, surface: string, object: PinObject | null) => void
}

export const useAmbientPins = create<AmbientPinState>((set) => ({
  claims: [],
  claim: (key, surface, object) =>
    set((s) => {
      const rest = s.claims.filter((c) => c.key !== key)
      if (!object) return rest.length === s.claims.length ? s : { claims: rest }
      return { claims: [...rest, { key, surface, object, seq: ++claimSeq }] }
    }),
}))

/** Test/teardown helper. */
export function resetAmbientPins(): void {
  useAmbientPins.setState({ claims: [] })
}

// The wall scope is a claimant: a DELIBERATE scope change (a new kind or id —
// never the Navigator re-affirming the same one) is the newest thing the
// reader did, so it takes the pin.
useScope.subscribe((s, prev) => {
  const a = s.scope
  const b = prev.scope
  if (a?.kind === b?.kind && a?.id === b?.id) return
  useAmbientPins
    .getState()
    .claim(WALL_SCOPE_CLAIM, 'scope', a ? { kind: 'report', id: a.id, label: a.label } : null)
})

/** The pin a resolved claim projects to, in the shape the Consult store keeps. */
export interface AmbientPin {
  pin: Selection
  /** The surface that set it ("scope" for the wall scope). */
  surface: string
}

/** The sentence a surface's pin carries in its preview body (the model reads it). */
function objectSummary(o: PinObject, surface: string): string {
  if (o.kind === 'roster') {
    return `The operator is reading the ${surface} page across ${o.label ?? 'the whole roster'}.`
  }
  return `The operator is reading this ${o.kind} on the ${surface}.`
}

/**
 * The ONE ambient pin: the newest live claim. The wall scope's claim projects
 * through {@link scopePin} (its members, its summary); a scope set before this
 * module loaded still counts, as the OLDEST claim. No claim and no scope → no
 * pin (never a stale one).
 */
export function resolveAmbientPin(claims: readonly PinClaim[], scope: Scope | null): AmbientPin | null {
  const live = claims.filter((c) => c.key !== WALL_SCOPE_CLAIM || !!scope)
  const hasScopeClaim = live.some((c) => c.key === WALL_SCOPE_CLAIM)
  const top = live.reduce<PinClaim | null>((best, c) => (!best || c.seq > best.seq ? c : best), null)
  if (!top) return scope && !hasScopeClaim ? { pin: scopePin(scope), surface: 'scope' } : null
  if (top.key === WALL_SCOPE_CLAIM && scope) return { pin: scopePin(scope), surface: 'scope' }
  const o = top.object
  const label = o.label ?? o.id
  return {
    surface: top.surface,
    pin: {
      // The roster is not a record kind: the `[Pinned …]` prefix carries it,
      // and `pinnedContext` keeps it off the structured wire field.
      kind: o.kind as SelectionKind,
      id: o.id,
      label,
      origin: SCOPE_PIN_ORIGIN,
      preview: { title: label, body: objectSummary(o, top.surface) },
    },
  }
}

/** React: the ambient pin, live. */
export function useAmbientPin(): AmbientPin | null {
  const claims = useAmbientPins((s) => s.claims)
  const scope = useScope((s) => s.scope)
  return useMemo(() => resolveAmbientPin(claims, scope), [claims, scope])
}

/**
 * THE PIN HOOK. Call it from every object surface with the object it is
 * reading — or null when it reads none, which RELEASES its claim (never a
 * stale pin). `revision` re-asserts the claim — a tile becoming the active
 * one — without a new object.
 */
export function usePinScope(object: PinObject | null | undefined, surface: string, revision = 0): void {
  const key = `${surface}#${useId()}`
  const kind = object?.kind
  const id = object?.id
  const label = object?.label ?? null
  useEffect(() => {
    if (!kind || !id) {
      useAmbientPins.getState().claim(key, surface, null)
      return
    }
    useAmbientPins.getState().claim(key, surface, { kind, id, label })
    return () => useAmbientPins.getState().claim(key, surface, null)
  }, [key, surface, kind, id, label, revision])
}

/** A selection as a pin object — only when its kind is one the surface reads. */
export function pinForSelection(
  selection: Selection | null | undefined,
  kinds: readonly SelectionKind[],
): PinObject | null {
  if (!selection || !kinds.includes(selection.kind)) return null
  return { kind: selection.kind, id: selection.id, label: selection.label ?? null }
}

/** The rail's objects: Subjects, Units, Sources. */
export const RAIL_PIN_KINDS: readonly SelectionKind[] = ['target', 'analyst', 'source']

/** What the Timelines place in time (and the subject the validity view reads). */
export const TIMELINE_PIN_KINDS: readonly SelectionKind[] = [
  'event',
  'signal',
  'finding',
  'report',
  'situation',
  'target',
]

/** The Register's Situations tab reads situations; its Narratives tab selects nothing. */
export function registerPinFor(tab: string, selection: Selection | null | undefined): PinObject | null {
  return tab === 'situations' ? pinForSelection(selection, ['situation']) : null
}

/** A bound tile's record — the subject or unit its registration is bound to. */
export function boundPinFor(
  binding: Record<string, unknown> | null | undefined,
  label?: string | null,
): PinObject | null {
  if (!binding) return null
  if (typeof binding.target_id === 'string' && binding.target_id) {
    return { kind: 'target', id: binding.target_id, label: label ?? null }
  }
  if (typeof binding.analyst_id === 'string' && binding.analyst_id) {
    return { kind: 'analyst', id: binding.analyst_id, label: label ?? null }
  }
  return null
}

/** What a turn was pinned to when it was asked — the census line prints it. */
export interface TurnPin {
  kind: string
  id: string
  label: string | null
  surface: string
}

export function turnPinOf(a: AmbientPin | null): TurnPin | null {
  return a ? { kind: a.pin.kind, id: a.pin.id, label: a.pin.label ?? null, surface: a.surface } : null
}

/** "pinned: target · Brazil (rail)", or "pin: none" when the turn was asked unpinned. */
export function turnPinLine(p: TurnPin | null): string {
  if (!p) return 'pin: none'
  const name = p.label && p.label !== p.id ? p.label : p.id
  return `pinned: ${p.kind} · ${name} (${p.surface})`
}
