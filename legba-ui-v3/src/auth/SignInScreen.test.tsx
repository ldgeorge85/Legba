/**
 * M-0a — the sign-in form on a phone: 48 px touch targets and 16 px inputs
 * below Tailwind's `sm` breakpoint, and the desktop classes unchanged above it.
 * (jsdom does not lay out, so this holds the classes; the phone walk measures
 * the boxes.)
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SignInScreen } from './SignInScreen'

const DESKTOP_INPUT =
  'w-full rounded border border-line bg-surf-2 px-2 py-1.5 text-body text-ink-1 placeholder:text-ink-3 focus:outline-none focus:ring-1 focus:ring-accent-emphasis'
const DESKTOP_BUTTON =
  'w-full rounded-sm bg-surf-3 px-3 py-1.5 text-label font-medium tracking-wide text-ink-1 transition-colors hover:bg-surf-4 disabled:cursor-not-allowed disabled:text-ink-3'

const desktopOnly = (cls: string) =>
  cls
    .split(/\s+/)
    .filter((c) => c && !c.startsWith('max-sm:'))
    .join(' ')

describe('SignInScreen on a narrow screen (M-0a)', () => {
  it('inputs and the button take a 48 px touch target and 16 px text below sm', () => {
    render(<SignInScreen onDone={() => {}} />)
    for (const id of ['sign-in-email', 'sign-in-password']) {
      const cls = screen.getByTestId(id).className
      expect(cls).toContain('max-sm:min-h-12')
      expect(cls).toContain('max-sm:text-base')
    }
    expect(screen.getByTestId('sign-in-submit').className).toContain('max-sm:min-h-12')
  })

  it('the desktop look is unchanged: every class without the narrow variant is what it was', () => {
    render(<SignInScreen onDone={() => {}} />)
    expect(desktopOnly(screen.getByTestId('sign-in-email').className)).toBe(DESKTOP_INPUT)
    expect(desktopOnly(screen.getByTestId('sign-in-submit').className)).toBe(DESKTOP_BUTTON)
  })
})
