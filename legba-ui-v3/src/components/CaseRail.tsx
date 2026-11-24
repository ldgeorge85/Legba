/**
 * CaseRail — the case player strip inside the Consult tile (C-1b/ui; note §4).
 *
 * While a case plays the reader sees: the step list with the current step
 * highlighted, each step's census line ("rests on N reads", or "no read" —
 * never a blank), the narration with its cites as record chips, a STOP that is
 * ALWAYS on screen while anything can still move the wall, Replay controls for
 * a recorded case, and UNDO (the view as it was before step 1 — U-2: the
 * drive mode's one undo for every view action the case ran).
 *
 * `compact` is the narrow / mobile form: the step counter and STOP, nothing
 * else — the one control that must never be hidden is the brake.
 *
 * Everything shown comes from the player store (`state/casePlayer.ts`), so the
 * rail re-renders exactly what the player decided — it derives nothing.
 */
import { RecordLink } from '@/components/inspector/RecordLink'
import { selectionKindOf } from '@/state/selection'
import { isCasePlaying, useCasePlayer } from '@/state/casePlayer'
import { replayCaption, stepCounter, type CaseStep } from '@/lib/caseModel'

const STATE_TONE: Record<CaseStep['state'], string> = {
  pending: 'text-ink-3',
  executed: 'text-ink-2',
  refused: 'text-amber-300',
  skipped: 'text-amber-300',
  stopped: 'text-ink-3 line-through',
  unreadable: 'text-accent-critical',
}

const STATE_WORD: Record<CaseStep['state'], string> = {
  pending: 'waiting',
  executed: 'done',
  refused: 'refused',
  skipped: 'skipped',
  stopped: 'not executed — stopped',
  unreadable: 'unreadable',
}

export interface CaseRailProps {
  /** Render only for the Consult slice that presented the case. */
  panelId: string
  /** The narrow/mobile form: the step counter + STOP. */
  compact?: boolean
  /** The turn's provenance hold, when the server held it — framed before step 1. */
  held?: boolean
}

export function CaseRail({ panelId, compact = false, held = false }: CaseRailProps) {
  const player = useCasePlayer((s) => (s.panelId === panelId ? s.player : null))
  const snapshot = useCasePlayer((s) => (s.panelId === panelId ? s.snapshot : null))
  const autoplay = useCasePlayer((s) => s.autoplay)
  const stopError = useCasePlayer((s) => s.stopError)
  if (!player) return null
  const actions = useCasePlayer.getState()
  const playing = isCasePlaying(player)
  const replay = player.mode === 'replay'
  // The current step: the first not yet run, else the last that ran.
  const currentIdx = (() => {
    const i = player.steps.findIndex((s) => s.seq !== null && s.state === 'pending')
    if (i >= 0) return i
    for (let j = player.steps.length - 1; j >= 0; j--) if (player.steps[j].seq !== null) return j
    return -1
  })()

  const stopButton = playing && (
    <button
      onClick={() => void actions.stop()}
      className="rounded border border-accent-critical/40 bg-accent-critical/20 px-2 py-0.5 text-label text-accent-critical hover:bg-accent-critical/40"
      data-testid="case-stop"
      title="stop the case now — nothing after the current step runs"
    >
      STOP
    </button>
  )

  const statusWord = (() => {
    switch (player.status) {
      case 'live':
        return 'presenting'
      case 'paused_gap':
        return 'paused'
      case 'replay':
        return 'replaying'
      case 'stopped':
        return 'stopped'
      case 'done':
        return player.sealed ? 'sealed' : 'ended'
    }
  })()

  if (compact) {
    return (
      <div
        className="flex items-center gap-2 rounded border border-line bg-surf-3 px-2 py-1 text-label"
        data-testid="case-rail-compact"
      >
        <span className="uppercase tracking-wide text-ink-3">Case</span>
        <span className="font-mono text-ink-2" data-testid="case-step-counter">
          {stepCounter(player)}
        </span>
        <span className="text-ink-3">{statusWord}</span>
        <span className="ml-auto">{stopButton}</span>
      </div>
    )
  }

  return (
    <div className="space-y-1 rounded border border-line bg-surf-3 p-2" data-testid="case-rail">
      <div className="flex flex-wrap items-center gap-2 text-label">
        <span className="uppercase tracking-wide text-ink-3">Case</span>
        <span className="font-mono text-ink-2" data-testid="case-step-counter">
          {stepCounter(player)}
        </span>
        <span className="text-ink-3" data-testid="case-status">
          {statusWord}
        </span>
        {player.record?.plane && (
          <span className="font-mono text-ink-3" title="the LLM plane that presented this case">
            via {player.record.plane}
          </span>
        )}
        {player.record && (
          <span
            className="font-mono text-ink-3"
            title="gate refusals over this case, as the record counts them"
            data-testid="case-refused-count"
          >
            {player.record.refused === null ? 'refusals: not measured' : `${player.record.refused} refused`}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1">
          {replay && player.status === 'replay' && (
            <>
              <button
                onClick={() => actions.stepReplay()}
                className="rounded border border-line px-2 py-0.5 text-ink-2 hover:text-ink-1"
                data-testid="case-replay-next"
                title="run the next recorded step"
              >
                Next step
              </button>
              <button
                onClick={() => actions.setAutoplay(!autoplay)}
                className="rounded border border-line px-2 py-0.5 text-ink-2 hover:text-ink-1"
                data-testid="case-replay-play"
              >
                {autoplay ? 'Pause' : 'Play'}
              </button>
            </>
          )}
          {stopButton}
          {snapshot && !playing && (
            <button
              onClick={() => actions.restore()}
              className="rounded border border-line px-2 py-0.5 text-ink-2 hover:text-ink-1"
              data-testid="case-restore"
              title="undo what this case pulled up — the view goes back to how it was before step 1: surface, regions, point, window, layers"
            >
              Undo
            </button>
          )}
          {!playing && (
            <button
              onClick={() => actions.dismiss()}
              className="text-label text-ink-3 underline underline-offset-2 hover:text-ink-1"
              data-testid="case-dismiss"
            >
              Close
            </button>
          )}
        </span>
      </div>
      {held && (
        <div
          className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-label text-amber-300"
          data-testid="case-held"
        >
          held — nothing this case cites resolved to live or held reporting; read it as model
          knowledge until it is grounded
        </div>
      )}
      {replay && player.record && (
        <div className="text-label text-ink-3" data-testid="case-replay-caption">
          {replayCaption(player.record)}
        </div>
      )}
      {player.note && (
        <div className="text-label text-amber-300" data-testid="case-note">
          {player.note}
        </div>
      )}
      {stopError && (
        <div className="text-label text-accent-critical" data-testid="case-stop-error">
          stop route: {stopError} — nothing more ran here regardless
        </div>
      )}
      <ol className="max-h-48 space-y-0.5 overflow-y-auto text-label" data-testid="case-steps">
        {player.steps.map((st, i) => (
          <li
            key={st.seq !== null ? `s${st.seq}` : `r${i}`}
            className={
              'rounded px-1 ' +
              (i === currentIdx ? 'bg-accent-info/15 ' : '') +
              STATE_TONE[st.state]
            }
            data-testid="case-step"
            data-state={st.state}
            data-current={i === currentIdx ? 'true' : 'false'}
          >
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="font-mono text-ink-3">{st.seq !== null ? st.seq : '·'}</span>
              <span>{st.label}</span>
              <span className="font-mono text-label text-ink-3" data-testid="case-step-census">
                {st.restsOn}
              </span>
              <span className="text-label text-ink-3">
                {st.state === 'executed' ? '' : STATE_WORD[st.state]}
                {st.reason && st.state !== 'stopped' ? ` — ${st.reason}` : ''}
              </span>
            </div>
            {st.text && (
              <div className="whitespace-pre-wrap pl-4 text-ink-2" data-testid="case-narration">
                {st.text}
              </div>
            )}
            {st.action && st.action.cites.length > 0 && (st.text || st.action.action === 'focus') && (
              <div className="flex flex-wrap gap-1 pl-4">
                {st.action.cites.map((c) => (
                  <RecordLink
                    key={`${c.kind}:${c.id}`}
                    kind={selectionKindOf(c.kind)}
                    id={c.id}
                    origin="case"
                    mono
                    title={`inspect this ${c.kind}`}
                  />
                ))}
              </div>
            )}
          </li>
        ))}
      </ol>
      {player.summary && (
        <div className="text-label text-ink-2" data-testid="case-summary">
          {player.summary}
        </div>
      )}
    </div>
  )
}
