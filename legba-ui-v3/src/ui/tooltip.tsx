/**
 * Tooltip — the workstation's one hover/focus/touch explainer primitive.
 *
 * shadcn's Tooltip on `@radix-ui/react-tooltip`, with ONE contract added that
 * Radix deliberately leaves out: TOUCH. Radix opens a tooltip on pointer hover
 * and on keyboard focus, and ignores a touch tap (a tap focuses with the
 * pointer down, and the click closes). The workstation's explainers are how a
 * first-time reader learns the vocabulary (`InfoTip`, U-5), and the phone has
 * no hover — so here a CLICK OR TAP on the trigger opens the tooltip and keeps
 * it open until focus leaves, the pointer leaves, Escape, or a press outside.
 *
 *   <Tooltip>
 *     <TooltipTrigger asChild><span tabIndex={0}>chip</span></TooltipTrigger>
 *     <TooltipContent>Plain-language explanation.</TooltipContent>
 *   </Tooltip>
 *
 * Every `Tooltip` carries its own provider (the shadcn v4 shape), so a lone
 * panel under test needs no app-level wrapper. The content portals into the
 * shell's explicit host (`./portal.tsx`), never straight onto <body>.
 *
 * ---------------------------------------------------------------------------
 * Adapted from shadcn/ui (https://github.com/shadcn-ui/ui), copied in as source
 * and hand-ported onto the workstation's tokens (--surf / --ink / --line). The
 * original is distributed under the MIT License, whose notice travels with
 * this file:
 *
 *   MIT License
 *
 *   Copyright (c) 2023 shadcn
 *
 *   Permission is hereby granted, free of charge, to any person obtaining a
 *   copy of this software and associated documentation files (the
 *   "Software"), to deal in the Software without restriction, including
 *   without limitation the rights to use, copy, modify, merge, publish,
 *   distribute, sublicense, and/or sell copies of the Software, and to permit
 *   persons to whom the Software is furnished to do so, subject to the
 *   following conditions:
 *
 *   The above copyright notice and this permission notice shall be included
 *   in all copies or substantial portions of the Software.
 *
 *   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
 *   OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 *   MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
 *   NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
 *   DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
 *   OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
 *   USE OR OTHER DEALINGS IN THE SOFTWARE.
 *
 * The Radix primitive underneath (@radix-ui/react-*) is MIT-licensed by
 * WorkOS; its notice ships inside its npm package. Legba's modifications to
 * this file are AGPL-3.0-or-later, like the rest of the tree.
 * ---------------------------------------------------------------------------
 */
import * as React from 'react'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import { cn } from './cn'
import { usePortalContainer } from './portal'

/** Hover delay. Zero: the explainer this replaced was a CSS `:hover` reveal. */
const DEFAULT_DELAY_MS = 0

interface TooltipOpenContext {
  /** Open and keep open (the click/tap reveal). */
  pin: () => void
}
const TooltipOpenCtx = React.createContext<TooltipOpenContext | null>(null)

export const TooltipProvider = TooltipPrimitive.Provider

export interface TooltipProps extends React.ComponentProps<typeof TooltipPrimitive.Root> {
  delayDuration?: number
}

/** A tooltip root. Uncontrolled by default; pass `open`/`onOpenChange` to own it. */
export function Tooltip({
  delayDuration = DEFAULT_DELAY_MS,
  open: openProp,
  defaultOpen = false,
  onOpenChange,
  children,
  ...props
}: TooltipProps) {
  const [openState, setOpenState] = React.useState(defaultOpen)
  const controlled = openProp !== undefined
  const open = controlled ? openProp : openState
  const setOpen = React.useCallback(
    (next: boolean) => {
      if (!controlled) setOpenState(next)
      onOpenChange?.(next)
    },
    [controlled, onOpenChange],
  )
  const ctx = React.useMemo<TooltipOpenContext>(() => ({ pin: () => setOpen(true) }), [setOpen])
  return (
    <TooltipPrimitive.Provider delayDuration={delayDuration}>
      <TooltipOpenCtx.Provider value={ctx}>
        <TooltipPrimitive.Root open={open} onOpenChange={setOpen} {...props}>
          {children}
        </TooltipPrimitive.Root>
      </TooltipOpenCtx.Provider>
    </TooltipPrimitive.Provider>
  )
}

/**
 * The trigger. A click or tap OPENS (and the default Radix "click closes" is
 * suppressed by `preventDefault`, which Radix's composed handler honours).
 */
export const TooltipTrigger = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Trigger>
>(function TooltipTrigger({ onClick, ...props }, ref) {
  const ctx = React.useContext(TooltipOpenCtx)
  return (
    <TooltipPrimitive.Trigger
      ref={ref}
      onClick={(e) => {
        onClick?.(e)
        if (!ctx) return
        e.preventDefault()
        ctx.pin()
      }}
      {...props}
    />
  )
})

/** The floating explainer, on the overlay surface (L4) with one hairline. */
export const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(function TooltipContent({ className, sideOffset = 4, collisionPadding = 8, ...props }, ref) {
  const container = usePortalContainer()
  return (
    <TooltipPrimitive.Portal container={container}>
      <TooltipPrimitive.Content
        ref={ref}
        sideOffset={sideOffset}
        collisionPadding={collisionPadding}
        className={cn(
          'z-50 max-w-[80vw] rounded-panel border border-line-strong bg-surf-4 p-2 text-label text-ink-2 shadow-xl',
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  )
})
