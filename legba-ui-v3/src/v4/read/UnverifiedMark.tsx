/**
 * UnverifiedMark — the one render of a finding with no verify verdict
 * (X4 READPATH, W11-F53): `unverified · judge pending` or
 * `unverified · not judged`, never a plain row and never a 0.
 *
 * The state is the registry's (`lib/unverifiedState`); this component only
 * prints it, with the meaning and the clock in the tooltip. Used by the Read's
 * header, the Assessment band, the CHECKED band's rows and the Live Feed's
 * badge line, so the words cannot drift between surfaces.
 */
import { UNVERIFIED_LABEL, unverifiedExplain, type UnverifiedInfo } from '@/lib/unverifiedState'

const CHIP =
  'inline-flex items-center gap-1 rounded border border-dashed border-line-strong bg-transparent px-1.5 py-0.5 text-label leading-none italic text-ink-2'

export default function UnverifiedMark({
  info,
  className,
  testId = 'unverified-state',
}: {
  info: UnverifiedInfo
  className?: string
  testId?: string
}) {
  return (
    <span
      className={`${CHIP} ${className ?? ''}`}
      title={unverifiedExplain(info)}
      data-testid={testId}
      data-state={info.state}
      data-basis={info.basis}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full border border-line-strong ${
          info.state === 'pending' ? 'bg-transparent' : 'bg-ink-3/40'
        }`}
        aria-hidden
      />
      {UNVERIFIED_LABEL[info.state]}
    </span>
  )
}
