/**
 * Dialog — the workstation's one modal primitive.
 *
 * shadcn's Dialog on `@radix-ui/react-dialog`: focus moves in on open and is
 * TRAPPED (Tab cycles inside), Escape and a press on the overlay close, focus
 * returns to the opener on close, the rest of the page is inert to assistive
 * tech while it is open, and the body does not scroll behind it. It replaces
 * the two hand-rolled `role="dialog"` overlays (the command palette and the
 * Flow canvas's wiring modal), each of which re-implemented a subset of that
 * by hand — neither trapped focus.
 *
 * The overlay and content portal into the shell's explicit host
 * (`./portal.tsx`), so they inherit the operator's density tokens.
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
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { cn } from './cn'
import { usePortalContainer } from './portal'

export const Dialog = DialogPrimitive.Root
export const DialogTrigger = DialogPrimitive.Trigger
export const DialogClose = DialogPrimitive.Close

/** The portal, bound to the shell's host. */
export function DialogPortal(props: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  const container = usePortalContainer()
  return <DialogPrimitive.Portal container={container} {...props} />
}

export const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(function DialogOverlay({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Overlay
      ref={ref}
      className={cn('fixed inset-0 z-50 bg-black/50', className)}
      {...props}
    />
  )
})

export interface DialogContentProps
  extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  /** Classes for the overlay (backdrop) behind the content. */
  overlayClassName?: string
  /** `data-testid` for the overlay. */
  overlayTestId?: string
  /** Render the corner close button (shadcn's default). Default `false`: the
   *  workstation's dialogs carry their own header controls. */
  showClose?: boolean
}

/**
 * The dialog surface: the overlay surface (L4), one hairline, the panel radius.
 * Centred by default; pass `className` to re-place it (the palette sits high).
 *
 * Focus RETURN: Radix returns focus on close only to a `DialogTrigger`. The
 * workstation's dialogs mostly open without one (⌘K, a canvas button in
 * another component), so this records whatever held focus when the dialog
 * opened and returns focus there on close — the opener, whoever it was. A
 * caller's own `onCloseAutoFocus` that calls `preventDefault()` still wins.
 */
export const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(function DialogContent(
  {
    className,
    children,
    overlayClassName,
    overlayTestId,
    showClose = false,
    onOpenAutoFocus,
    onCloseAutoFocus,
    ...props
  },
  ref,
) {
  const returnTo = React.useRef<HTMLElement | null>(null)
  return (
    <DialogPortal>
      <DialogOverlay className={overlayClassName} data-testid={overlayTestId} />
      <DialogPrimitive.Content
        ref={ref}
        onOpenAutoFocus={(e) => {
          const active = document.activeElement
          returnTo.current = active instanceof HTMLElement && active !== document.body ? active : null
          onOpenAutoFocus?.(e)
        }}
        onCloseAutoFocus={(e) => {
          onCloseAutoFocus?.(e)
          if (e.defaultPrevented) return
          const target = returnTo.current
          if (target && target.isConnected) {
            e.preventDefault()
            target.focus()
          }
        }}
        className={cn(
          'fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2',
          'rounded-panel border border-line bg-surf-4 text-ink-1 shadow-2xl outline-none',
          className,
        )}
        {...props}
      >
        {children}
        {showClose && (
          <DialogPrimitive.Close
            className="absolute right-3 top-3 rounded-control text-ink-3 hover:text-ink-1"
            aria-label="Close"
          >
            <X size={16} />
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
})

export const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(function DialogTitle({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Title
      ref={ref}
      className={cn('text-heading font-semibold text-ink-1', className)}
      {...props}
    />
  )
})

export const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(function DialogDescription({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Description
      ref={ref}
      className={cn('text-label text-ink-3', className)}
      {...props}
    />
  )
})
