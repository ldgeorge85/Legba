/**
 * InfoTip — the ONE shared "teach the vocabulary in place" affordance (U-5).
 *
 * A hostile UX review found the verification story (the product's whole
 * differentiator — ICD-203 likelihood/confidence, faithfulness, the verdict
 * chips, the honest-absence strings) explained in exactly ONE place (a `?`
 * icon in the Inspector). Everywhere else the same tokens render as
 * unexplained jargon. This component is the fix: wrap ANY token-bearing
 * chip/text in `<InfoTip text="...">` and every render site gets the same
 * plain-language, keyboard-accessible, touch-safe explainer — one component,
 * one interaction contract, reused verbatim by VerdictBadge, the desk card,
 * the eval badges, the Goldset cards, and the citation verdict line.
 *
 * ACCESSIBILITY CONTRACT (why this exists instead of a bare `title=`):
 *   - `title` attributes are mouse-hover-only in most browsers — a keyboard
 *     user tabbing through the page never sees them. This component makes
 *     the trigger itself focusable (`tabIndex=0`) and reveals the popover on
 *     BOTH `:hover` and `:focus-within` (the same contract `CitedProse`'s
 *     citation chips already use), so keyboard users get the same
 *     explanation sighted mouse users do.
 *   - Touch: tapping a focusable element focuses it on every mainstream
 *     mobile browser, which satisfies `:focus-within` — no separate touch
 *     handler needed.
 *   - `aria-describedby` links the trigger to the popover's text so a screen
 *     reader announces the explanation when the trigger receives focus,
 *     without needing `role="button"` (nothing happens on activation — this
 *     is a description, not a control).
 *
 * INTERACTIVE-PARENT MODE: several call sites (e.g. `VerdictBadge` inside a
 * feed row that is itself a `<button onClick=...>`, see
 * `panels/system/Findings.tsx`'s `FeedCard`) sit inside an ambient
 * clickable/interactive ancestor. Touch has no `:hover` — tapping the trigger
 * IS how a touch user reveals the popover (it focuses the span, satisfying
 * `:focus-within`) — but that same tap also bubbles up as a click and fires
 * the parent's `onClick`, so the popover can never actually be read there; the
 * parent's own interaction swallows it. `interactiveParent` stops that
 * propagation (click + Enter/Space) so the trigger reveals its popover
 * without ALSO triggering the ambient parent — the identical
 * `stopPropagation` discipline `FeedAddToExport` already applies for the same
 * reason. Leave it off (the default) when there is no such ambient parent.
 *
 * INTERNALS (UI-B1/tokens, UI-D1 §7): the floating box is now the `Tooltip`
 * primitive (`src/ui/tooltip.tsx`, Radix underneath) instead of a CSS
 * `:hover`/`:focus-within` reveal. The contract above is unchanged — hover,
 * keyboard focus and a tap all reveal it; `interactiveParent` still stops the
 * ambient parent firing — and three things improve: the box is positioned
 * against the viewport and flips at its edge (the CSS box was clipped by any
 * `overflow: hidden` tile and ran off the right of narrow panels), Escape
 * dismisses it, and it portals into the shell's overlay host so it paints
 * above neighbouring tiles. The explainer text is ALSO kept in the DOM as a
 * visually-hidden description the trigger names with `aria-describedby`, so a
 * screen reader announces it on focus exactly as before, open or not, and the
 * same `data-testid` still finds it.
 */
import { useId, type ReactNode } from 'react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/tooltip'

export interface InfoTipProps {
  /** The plain-language explainer sentence(s), written for a first-time
   *  analyst. Shown verbatim in the popover — never truncated or reworded
   *  per-instance, so the same token always teaches the same lesson. */
  text: string
  /** The trigger content — the chip/text the reader is already looking at. */
  children: ReactNode
  className?: string
  /** Extra classes for the popover box (e.g. to widen it). */
  popoverClassName?: string
  /** Optional `data-testid` on the trigger (the popover gets `${testId}-popover`). */
  testId?: string
  /** Set when the trigger sits inside an ambient clickable/interactive parent
   *  (a row `<button>`, an `<a>`, a card with its own `onClick`) — see the
   *  module doc above. Stops the trigger's click/Enter/Space from also
   *  firing the parent's handler. Default `false`. */
  interactiveParent?: boolean
}

export function InfoTip({
  text,
  children,
  className,
  popoverClassName,
  testId,
  interactiveParent = false,
}: InfoTipProps) {
  const id = useId()
  const popoverTestId = testId ? `${testId}-popover` : 'info-tip-popover'
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          aria-describedby={id}
          className={`relative inline-flex cursor-help items-center rounded-control ${className ?? ''}`}
          data-testid={testId}
          onClick={interactiveParent ? (e) => e.stopPropagation() : undefined}
          onKeyDown={
            interactiveParent
              ? (e) => {
                  if (e.key === 'Enter' || e.key === ' ') e.stopPropagation()
                }
              : undefined
          }
        >
          {children}
          {/* The accessible description: always present, never visible. */}
          <span id={id} className="sr-only" data-testid={popoverTestId}>
            {text}
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent
        side="bottom"
        align="start"
        data-testid={`${popoverTestId}-floating`}
        className={`w-64 text-label font-normal normal-case leading-relaxed ${popoverClassName ?? ''}`}
      >
        {text}
      </TooltipContent>
    </Tooltip>
  )
}
