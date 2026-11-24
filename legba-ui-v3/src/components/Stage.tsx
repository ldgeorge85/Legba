/**
 * Stage — the top of the centre column (U-6; brief §12): today's read by
 * default on the Board, and whatever the reader or Consult pulls up — a
 * subject, a record, the forecasts, any view — with a small breadcrumb that
 * always leads back to where the page started.
 *
 *   ◂  Today's read  ›  Brazil  ›  Record · a finding            ⇥ to panels
 *
 * The stage is composable: it shows any registered view (`ViewHost`), and the
 * one in front can be sent to the panel side. A record pulled up onto the
 * stage makes it the selection while it is in front, so the Inspector reads
 * it and Consult's point follows it.
 */
import { ArrowLeft, PanelRightOpen } from 'lucide-react'
import type { currentMode } from '@/auth/jwt'
import { ViewHost } from '@/components/ViewHost'
import { SingletonView } from '@/components/SingletonView'
import { currentStage, stageInstanceId, useFrame, type StageEntry } from '@/state/frame'
import { useSelection, selectionKindOf } from '@/state/selection'
import { getFramePort } from '@/lib/panelActions'
import { pinForSelection, usePinScope } from '@/lib/consultContext'
import { cn } from '@/lib/cn'

type Mode = ReturnType<typeof currentMode>

/**
 * A record on the stage: the Record (the Inspector) reads the selection, which
 * the pull-up set and every step back onto this crumb sets again.
 */
function RecordOnStage({ kind, mode }: { kind: string; mode: Mode }) {
  const selection = useSelection((s) => s.selection)
  usePinScope(pinForSelection(selection, [selectionKindOf(kind)]), 'stage')
  return <SingletonView kind="system.inspector" mode={mode} />
}

/** Going to a crumb that holds a record makes it the selection again. */
function reselect(e: StageEntry | undefined): void {
  if (!e?.record) return
  const cur = useSelection.getState().selection
  const kind = selectionKindOf(e.record.kind)
  if (cur?.kind === kind && cur.id === e.record.id) return
  useSelection.getState().select({ kind, id: e.record.id, label: e.record.label, origin: 'stage' })
}

export function Stage({ surface, mode }: { surface: string; mode: Mode }) {
  const st = useFrame((s) => s.stages[surface])
  const goRaw = useFrame((s) => s.goStage)
  const go = (sf: string, i: number) => {
    reselect(st?.stack[i])
    goRaw(sf, i)
  }
  const entry = currentStage(st)
  if (!entry || !st) {
    return <div className="h-full w-full bg-surf-1" data-testid="stage" data-stage-view="" />
  }
  const index = st.index
  const canBack = index > 0
  const toPanels = () => {
    const port = getFramePort()
    if (!port || entry.record) return
    if (port.openView(entry.view, { place: 'panel', recordId: entry.recordId, axis: entry.axis, tab: entry.tab, origin: 'operator' })) {
      go(surface, Math.max(0, index - 1))
    }
  }
  return (
    <section
      className="flex h-full min-h-0 w-full flex-col bg-surf-1"
      data-testid="stage"
      data-stage-view={stageInstanceId(entry)}
      aria-label="Stage"
    >
      {st.stack.length > 1 && (
        <nav
          className="flex h-8 shrink-0 items-center gap-1 border-b border-line px-2 text-label text-ink-3"
          aria-label="Stage history"
          data-testid="stage-trail"
        >
          <button
            type="button"
            onClick={() => go(surface, index - 1)}
            disabled={!canBack}
            className="flex h-6 items-center gap-1 rounded-control px-1.5 text-ink-2 hover:bg-surf-3 hover:text-ink-1 disabled:opacity-40"
            data-testid="stage-back"
            title={canBack ? `Back to ${st.stack[index - 1].title}` : 'Nothing before this'}
          >
            <ArrowLeft size={13} aria-hidden />
            Back
          </button>
          <ol className="flex min-w-0 items-center gap-1 overflow-hidden">
            {st.stack.map((e, i) => (
              <li key={`${stageInstanceId(e)}#${i}`} className="flex min-w-0 items-center gap-1">
                {i > 0 && <span aria-hidden>›</span>}
                <button
                  type="button"
                  onClick={() => go(surface, i)}
                  aria-current={i === index ? 'page' : undefined}
                  data-testid={`stage-crumb-${i}`}
                  className={cn(
                    'max-w-[16rem] truncate rounded-control px-1 hover:text-ink-1',
                    i === index ? 'text-ink-1' : 'text-ink-3',
                    i === index && e.origin === 'case' && 'text-point',
                  )}
                  title={e.origin === 'case' ? `${e.title} — pulled up by Consult` : e.title}
                >
                  {e.title}
                </button>
              </li>
            ))}
          </ol>
          {canBack && !entry.record && (
            <button
              type="button"
              onClick={toPanels}
              className="ml-auto flex h-6 shrink-0 items-center gap-1 rounded-control px-1.5 text-ink-3 hover:bg-surf-3 hover:text-ink-1"
              data-testid="stage-to-panels"
              title="Keep this beside the stage, as a panel, and go back"
            >
              <PanelRightOpen size={13} aria-hidden />
              To the panels
            </button>
          )}
        </nav>
      )}
      <div className="relative min-h-0 flex-1 overflow-hidden" data-testid="stage-body">
        {entry.record ? (
          <RecordOnStage kind={entry.record.kind} mode={mode} />
        ) : (
          <ViewHost
            key={stageInstanceId(entry)}
            view={entry.view}
            recordId={entry.recordId}
            axis={entry.axis}
            tab={entry.tab}
            mode={mode}
            pinSurface="stage"
          />
        )}
      </div>
    </section>
  )
}
