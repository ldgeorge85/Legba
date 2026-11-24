/**
 * Tabs — the workstation's one tab-strip primitive.
 *
 * shadcn's Tabs on `@radix-ui/react-tabs`: a real `tablist` with roving focus
 * (one Tab stop for the strip; ←/→ move between tabs, Home/End jump; a focused
 * tab activates), `aria-selected` / `aria-controls` wired by Radix. It is what
 * `PanelTabStrip` — the merged panels' tab bar — renders.
 *
 * One addition: a CLICK also activates. Radix activates a tab on `mousedown`
 * (and on focus); a synthetic click (a test's `fireEvent.click`, an
 * assistive-tech "activate" that dispatches only `click`) would otherwise do
 * nothing. Activation is idempotent, so a real mouse press activating twice
 * (mousedown, then click) is a no-op the second time.
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
import * as TabsPrimitive from '@radix-ui/react-tabs'
import { cn } from './cn'

const TabsChangeCtx = React.createContext<((value: string) => void) | null>(null)

/** The tabs root. Controlled (`value` + `onValueChange`) or uncontrolled. */
export function Tabs({
  value: valueProp,
  defaultValue,
  onValueChange,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  const [valueState, setValueState] = React.useState(defaultValue)
  const controlled = valueProp !== undefined
  const value = controlled ? valueProp : valueState
  const change = React.useCallback(
    (next: string) => {
      if (!controlled) setValueState(next)
      onValueChange?.(next)
    },
    [controlled, onValueChange],
  )
  return (
    <TabsChangeCtx.Provider value={change}>
      <TabsPrimitive.Root value={value} onValueChange={change} {...props} />
    </TabsChangeCtx.Provider>
  )
}

export const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(function TabsList({ className, ...props }, ref) {
  return <TabsPrimitive.List ref={ref} className={cn('flex items-center gap-1', className)} {...props} />
})

export const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(function TabsTrigger({ className, onClick, value, disabled, ...props }, ref) {
  const change = React.useContext(TabsChangeCtx)
  return (
    <TabsPrimitive.Trigger
      ref={ref}
      value={value}
      disabled={disabled}
      onClick={(e) => {
        onClick?.(e)
        if (!e.defaultPrevented && !disabled) change?.(value)
      }}
      className={cn(
        'flex items-center gap-1 rounded-control border px-2 py-0.5 text-label font-medium',
        'data-[state=inactive]:border-line data-[state=inactive]:text-ink-3',
        'data-[state=inactive]:hover:bg-surf-2 data-[state=inactive]:hover:text-ink-1',
        'data-[state=active]:border-line-strong data-[state=active]:bg-surf-3 data-[state=active]:text-ink-1',
        className,
      )}
      {...props}
    />
  )
})

export const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(function TabsContent({ className, ...props }, ref) {
  return <TabsPrimitive.Content ref={ref} className={cn('outline-none', className)} {...props} />
})
