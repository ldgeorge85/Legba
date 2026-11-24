/**
 * Select — the workstation's one single-choice dropdown primitive.
 *
 * shadcn's Select on `@radix-ui/react-select`: a button trigger that opens a
 * listbox (Enter / Space / ↑ / ↓ open it; ↑/↓ move; typeahead jumps; Enter
 * picks; Escape closes and returns focus), styled on the workstation tokens
 * instead of the browser's native `<select>`, which ignores the theme in most
 * engines (a light-grey OS menu over the dark dock). It is what `ScopePicker`
 * — the registry-backed descriptor dropdown — renders.
 *
 * Two departures from shadcn, both deliberate:
 *   - `position="popper"` by default: the listbox drops below the trigger like
 *     every other overlay here, rather than Radix's macOS-style "item-aligned"
 *     placement that slides the list over the trigger.
 *   - The trigger stamps `data-value` with the current value, so a test (or a
 *     screenshot harness) can read the selection off the DOM the way it read a
 *     native `<select>`'s `.value`.
 *
 * Radix forbids an empty-string item value (it is reserved for "clear back to
 * the placeholder"); a caller that offers an explicit "none" row maps it to a
 * sentinel — see `ScopePicker`.
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
import * as SelectPrimitive from '@radix-ui/react-select'
import { Check, ChevronDown } from 'lucide-react'
import { cn } from './cn'
import { usePortalContainer } from './portal'

export const SelectGroup = SelectPrimitive.Group
export const SelectValue = SelectPrimitive.Value

const SelectValueCtx = React.createContext<string | undefined>(undefined)

/** The select root. Controlled (`value` + `onValueChange`) or uncontrolled. */
export function Select({
  value: valueProp,
  defaultValue,
  onValueChange,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Root>) {
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
    <SelectValueCtx.Provider value={value}>
      <SelectPrimitive.Root value={value} onValueChange={change} {...props} />
    </SelectValueCtx.Provider>
  )
}

export const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(function SelectTrigger({ className, children, ...props }, ref) {
  const value = React.useContext(SelectValueCtx)
  return (
    <SelectPrimitive.Trigger
      ref={ref}
      data-value={value ?? ''}
      className={cn(
        'inline-flex items-center justify-between gap-1 rounded-control border border-line bg-surf-1 px-2 py-0.5',
        'text-label text-ink-1 hover:border-line-strong data-[placeholder]:text-ink-3',
        'disabled:cursor-not-allowed disabled:opacity-50 [&>span]:truncate',
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <ChevronDown className="h-3 w-3 shrink-0 opacity-60" aria-hidden />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  )
})

export const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(function SelectContent({ className, children, position = 'popper', sideOffset = 4, ...props }, ref) {
  const container = usePortalContainer()
  return (
    <SelectPrimitive.Portal container={container}>
      <SelectPrimitive.Content
        ref={ref}
        position={position}
        sideOffset={sideOffset}
        className={cn(
          'relative z-50 max-h-[min(24rem,var(--radix-select-content-available-height))] min-w-[8rem] overflow-hidden',
          'rounded-panel border border-line-strong bg-surf-4 text-ink-1 shadow-xl',
          position === 'popper' && 'w-full min-w-[var(--radix-select-trigger-width)]',
          className,
        )}
        {...props}
      >
        <SelectPrimitive.Viewport className="p-1">{children}</SelectPrimitive.Viewport>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  )
})

export const SelectLabel = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(function SelectLabel({ className, ...props }, ref) {
  return (
    <SelectPrimitive.Label
      ref={ref}
      className={cn('px-2 py-1 text-label uppercase tracking-wider text-ink-3', className)}
      {...props}
    />
  )
})

export const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(function SelectItem({ className, children, ...props }, ref) {
  return (
    <SelectPrimitive.Item
      ref={ref}
      className={cn(
        'relative flex w-full cursor-default select-none items-center rounded-control py-1 pl-6 pr-2 text-label outline-none',
        'text-ink-2 data-[highlighted]:bg-surf-3 data-[highlighted]:text-ink-1',
        'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
        className,
      )}
      {...props}
    >
      <span className="absolute left-1.5 flex h-3 w-3 items-center justify-center">
        <SelectPrimitive.ItemIndicator>
          <Check className="h-3 w-3" aria-hidden />
        </SelectPrimitive.ItemIndicator>
      </span>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  )
})

export const SelectSeparator = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(function SelectSeparator({ className, ...props }, ref) {
  return <SelectPrimitive.Separator ref={ref} className={cn('-mx-1 my-1 h-px bg-line', className)} {...props} />
})
