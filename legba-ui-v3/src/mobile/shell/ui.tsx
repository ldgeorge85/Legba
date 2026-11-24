/**
 * The phone shell's small shared pieces (M-2): the window chips (the one
 * clock, three spans, no time band), the two-line home row, a section head,
 * and the "Ask about this" row every detail page carries.
 */
import type { ReactNode } from 'react'
import { useWorldState } from '@/v4/world/worldState'
import { PHONE_WINDOWS, phoneWindowOf } from '@/mobile/screens/changed/changedModel'
import { agoText } from '@/v4/watch/model'
import type { Selection } from '@/state/selection'
import { currentScreen, usePhoneShell } from './phoneState'
import { navigate } from './phoneAddress'

/** The window, as the three chips (writes `useWorldState`: every view follows). */
export function WindowChips({ extra }: { extra?: ReactNode }) {
  const spanMs = useWorldState((s) => s.spanMs)
  const on = phoneWindowOf(spanMs)
  return (
    <div className="ph-chips" role="group" aria-label="Window" data-testid="phone-window-chips">
      {PHONE_WINDOWS.map((w) => (
        <button
          key={w.id}
          type="button"
          className="ph-chip"
          aria-pressed={on.id === w.id}
          data-testid={`phone-window-${w.id}`}
          onClick={() => useWorldState.getState().setSpan(w.ms)}
        >
          {w.label}
        </button>
      ))}
      {extra}
    </div>
  )
}

/** The severity words a row may carry, and the semantic rule each draws (alerts only). */
const SEV_CLASS: Record<string, string> = {
  critical: 'ph-sev-critical',
  high: 'ph-sev-high',
  medium: 'ph-sev-medium',
  low: 'ph-sev-low',
  info: 'ph-sev-info',
}

/** One home row: the statement (two lines at most), then word · subject · age · kind. */
export function HomeRow({
  headline,
  word,
  subject,
  at,
  chip,
  rule,
  tail,
  onOpen,
  testId,
  kind,
}: {
  headline: string
  word: string | null
  subject: string
  at: string | null
  chip: string
  /** A 2 px rule in the severity's colour (alerts only). */
  rule?: string | null
  tail?: string | null
  onOpen: (() => void) | null
  testId: string
  kind?: string
}) {
  const body = (
    <>
      <span className="ph-row-head">{headline}</span>
      <span className="ph-row-meta">
        {[word, subject, at ? agoText(at, Date.now()) : null].filter(Boolean).join(' · ')}
        <span className="ph-kind">{chip}</span>
        {tail ? <span className="ph-row-tail"> · {tail}</span> : null}
      </span>
    </>
  )
  const cls = `ph-row ${rule ? `ph-rule ${SEV_CLASS[rule] ?? ''}` : ''}`
  return onOpen ? (
    <li>
      <button type="button" className={cls} onClick={onOpen} data-testid={testId} data-kind={kind}>
        {body}
      </button>
    </li>
  ) : (
    <li className={cls} data-testid={testId} data-kind={kind}>
      {body}
    </li>
  )
}

export function SectionHead({ title, count, testId }: { title: string; count?: number | string | null; testId?: string }) {
  return (
    <h2 className="ph-section" data-testid={testId}>
      {title}
      {count != null ? <span className="ph-count">{count}</span> : null}
    </h2>
  )
}

/**
 * "Ask about this" — pins the page's point for Consult and opens the Consult
 * tab. `pins` are the desktop's pins (`Selection`s): the Consult screen sends
 * them through the desktop's builders. The screen the person came from stays
 * the on-screen report's screen.
 */
export function AskAboutThis({ label, pins = [] }: { label: string; pins?: Selection[] }) {
  return (
    <button
      type="button"
      className="ph-ask"
      data-testid="phone-ask-about-this"
      onClick={() => {
        usePhoneShell.setState({ asking: { pins, label }, lastScreen: currentScreen() })
        navigate({ tab: 'consult', page: null, record: null, peek: false }, 'push')
      }}
    >
      <span className="ph-ask-mark" aria-hidden>
        ?
      </span>
      <span>
        Ask about this
        <span className="ph-ask-sub">{label}</span>
      </span>
    </button>
  )
}

export function Quiet({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <p className="ph-quiet" data-testid={testId}>
      {children}
    </p>
  )
}
