/**
 * Popover — the workstation's one anchored, interactive overlay primitive.
 *
 * shadcn's Popover on `@radix-ui/react-popover`: a click on the trigger toggles
 * it; focus moves into the content on open and returns to the trigger on
 * close; Escape and a press outside close it; the content is positioned
 * against the trigger and flips at the viewport edge. Unlike `Tooltip`, its
 * content may hold controls (a catalog list, a form) — it is the surface the
 * rail's `＋` catalog (UI-D1 §2.2) is built on.
 *
 * The content portals into the shell's explicit host (`./portal.tsx`).
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
import * as PopoverPrimitive from '@radix-ui/react-popover'
import { cn } from './cn'
import { usePortalContainer } from './portal'

export const Popover = PopoverPrimitive.Root
export const PopoverTrigger = PopoverPrimitive.Trigger
export const PopoverAnchor = PopoverPrimitive.Anchor
export const PopoverClose = PopoverPrimitive.Close

/** The floating panel, on the overlay surface (L4) with one hairline. */
export const PopoverContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(function PopoverContent({ className, align = 'start', sideOffset = 4, collisionPadding = 8, ...props }, ref) {
  const container = usePortalContainer()
  return (
    <PopoverPrimitive.Portal container={container}>
      <PopoverPrimitive.Content
        ref={ref}
        align={align}
        sideOffset={sideOffset}
        collisionPadding={collisionPadding}
        className={cn(
          'z-50 w-72 rounded-panel border border-line-strong bg-surf-4 p-2 text-body text-ink-1 shadow-xl outline-none',
          className,
        )}
        {...props}
      />
    </PopoverPrimitive.Portal>
  )
})
