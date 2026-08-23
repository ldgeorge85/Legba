/**
 * Situation Trajectory (`system.situations`) — the register's frames, and how
 * each one MOVED.
 *
 * Two routes that existed with no consumer until this train:
 *   `GET /api/v1/situations`                              — the register
 *   `GET /api/v1/v3/situations/{id}/trajectory`           — the ledger
 *
 * Left rail = the frames. Right pane = the selected frame's append-only ledger,
 * newest first, each row dated by its EVIDENCE (the newest backing finding's
 * produced_at) rather than by when the tracker ran, and each row deep-linking
 * into the graded `situation_update` finding that asserted the delta.
 *
 * TOLERANT OF FRAME-COUNT CHANGES BY CONSTRUCTION. A later FRAME-2 train
 * repairs the register's aggregation, so how many frames exist and how they
 * cluster will change under this panel. Nothing here is keyed to a frame count:
 * the rail renders whatever the register returns (with its count stated rather
 * than assumed), the ledger derives its own delta legend from the rows present
 * (`deltaCounts`), and no delta vocabulary is hardcoded as exhaustive — an
 * unknown delta renders in neutral chrome instead of being dropped.
 *
 * The three honest zero-states are kept apart (see `lib/trajectoryModel.ts`):
 * a failed read ("could not look"), a known frame with an empty ledger ("never
 * assessed"), and a null state (never backfilled with a fabricated default).
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PanelChrome } from '@/components/PanelChrome'
import { cn } from '@/lib/cn'
import { relativeTime } from '@/lib/findingsViews'
import { selectRow, useSelection } from '@/state/selection'
import { ApiError, fetchSituationFrames, fetchSituationTrajectory } from '@/lib/api'
import type { SituationFrame } from '@/lib/api'
import {
  currentState,
  deltaCounts,
  deltaLabel,
  deltaTone,
  eventWhen,
  trajectoryStatus,
  trajectoryStatusText,
} from '@/lib/trajectoryModel'
import type { PanelProps } from '@/types'

export default function SituationTrajectoryPanel({ registration }: PanelProps) {
  const selection = useSelection((s) => s.selection)
  const [localId, setLocalId] = useState<string | null>(null)

  const frames = useQuery({
    queryKey: ['situation_frames'],
    queryFn: () => fetchSituationFrames({ limit: 200 }),
    refetchInterval: 120_000,
  })

  const rows = useMemo(() => frames.data?.data ?? [], [frames.data])

  // The shared selection wins when it names a situation, so clicking a frame in
  // the map / feed / timeline brushes this panel too; otherwise the panel's own
  // last click stands. Falls back to the first frame so a cold open shows a
  // ledger rather than a picker.
  const selectedId =
    (selection?.kind === 'situation' ? selection.id : null) ?? localId ?? rows[0]?.id ?? null

  const trajectory = useQuery({
    queryKey: ['situation_trajectory', selectedId],
    queryFn: () => fetchSituationTrajectory(selectedId as string, { limit: 200 }),
    enabled: selectedId != null,
    refetchInterval: 120_000,
  })

  const status = trajectoryStatus(trajectory.data)
  const state = currentState(trajectory.data)
  const events = trajectory.data?.events ?? []
  const legend = deltaCounts(events)
  const selectedFrame = rows.find((f) => f.id === selectedId) ?? null

  // A 404 is the route's deliberate "unknown situation" — distinct from a known
  // frame with an empty ledger, and it must not read as "nothing happened".
  const notFound = trajectory.error instanceof ApiError && trajectory.error.status === 404

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} frame${rows.length === 1 ? '' : 's'} in the register · ledger dated by evidence, not by run time`}
      onRefresh={() => {
        void frames.refetch()
        void trajectory.refetch()
      }}
    >
      <div className="flex h-full min-h-0 gap-2">
        {/* the register's frames */}
        <div
          className="w-64 shrink-0 overflow-auto border-r border-line pr-2"
          data-testid="trajectory-frames"
        >
          {frames.isLoading && <div className="text-body text-ink-3">loading frames…</div>}
          {frames.error != null && (
            <div className="text-body text-rose-300" data-testid="trajectory-frames-error">
              could not read the register
            </div>
          )}
          {!frames.isLoading && frames.error == null && rows.length === 0 && (
            <div className="text-body text-ink-3" data-testid="trajectory-frames-empty">
              the register holds no frames
            </div>
          )}
          <div className="space-y-0.5">
            {rows.map((f) => (
              <FrameRow
                key={f.id}
                frame={f}
                active={f.id === selectedId}
                onPick={() => {
                  setLocalId(f.id)
                  selectRow('situation', f.id, f.name, { origin: 'situation_trajectory' })
                }}
              />
            ))}
          </div>
        </div>

        {/* the selected frame's ledger */}
        <div className="min-w-0 flex-1 overflow-auto" data-testid="trajectory-ledger">
          {selectedId == null ? (
            <div className="text-body text-ink-3">pick a frame to read its trajectory</div>
          ) : (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="text-body-lg text-ink-1">
                  {trajectory.data?.name || selectedFrame?.name || 'situation'}
                </span>
                {state != null ? (
                  <span
                    className="rounded border border-line-strong bg-surf-3 px-1.5 py-0.5 text-label text-ink-1"
                    data-testid="trajectory-state"
                  >
                    state: {state}
                  </span>
                ) : (
                  <span
                    className="rounded border border-line bg-surf-2 px-1.5 py-0.5 text-label text-ink-3"
                    title="The ledger has never recorded a state for this frame. No default is invented."
                    data-testid="trajectory-state-null"
                  >
                    state: never recorded
                  </span>
                )}
                {legend.map((l) => (
                  <span
                    key={l.delta}
                    className={cn('rounded border px-1.5 py-0.5 text-label', deltaTone(l.delta))}
                    data-testid={`trajectory-legend-${l.delta}`}
                  >
                    {deltaLabel(l.delta)} ×{l.n}
                  </span>
                ))}
              </div>

              {notFound && (
                <div
                  className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
                  data-testid="trajectory-not-found"
                >
                  Unknown situation — this id is not in the register. That is a different
                  fact from a known frame with an empty ledger.
                </div>
              )}

              {!notFound && trajectory.isLoading && (
                <div className="text-body text-ink-3">loading the ledger…</div>
              )}

              {!notFound && !trajectory.isLoading && status !== 'ok' && (
                <div
                  className={cn(
                    'rounded border p-2 text-body',
                    status === 'unmeasured'
                      ? 'border-amber-500/40 bg-amber-500/10 text-amber-300'
                      : 'border-line bg-surf-1 text-ink-3',
                  )}
                  data-testid={`trajectory-${status}`}
                >
                  {trajectoryStatusText(status)}
                </div>
              )}

              {status === 'ok' && (
                <ol className="space-y-1.5" data-testid="trajectory-events">
                  {events.map((e) => {
                    const when = eventWhen(e)
                    return (
                      <li
                        key={e.id}
                        className="rounded border border-line bg-surf-1 p-2"
                        data-testid={`trajectory-event-${e.id}`}
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span
                            className={cn(
                              'rounded border px-1.5 py-0.5 text-label',
                              deltaTone(e.delta),
                            )}
                          >
                            {deltaLabel(e.delta)}
                          </span>
                          <span className="font-mono text-label text-ink-2">
                            {e.state_from} → {e.state_to}
                          </span>
                          <span
                            className="ml-auto text-label text-ink-3"
                            title={
                              when.basis === 'evidence'
                                ? 'evidence time — the newest backing finding'
                                : when.basis === 'recorded'
                                  ? 'no evidence time on this row; showing when it was recorded'
                                  : 'undated'
                            }
                          >
                            {when.iso ? relativeTime(when.iso) : 'undated'}
                            {when.basis === 'recorded' && ' (recorded)'}
                          </span>
                        </div>
                        <p className="mt-1 whitespace-pre-wrap text-body text-ink-2">{e.why}</p>
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          <button
                            type="button"
                            onClick={() =>
                              selectRow('finding', e.source_output_id, 'situation update', {
                                origin: 'situation_trajectory',
                              })
                            }
                            className="rounded border border-line bg-surf-2 px-1.5 py-0.5 font-mono text-label text-ink-2 hover:text-ink-1"
                            title="Open the graded situation_update finding that asserted this delta"
                            data-testid={`trajectory-source-${e.id}`}
                          >
                            asserted by {e.source_output_id.slice(0, 8)}…
                          </button>
                          {e.derived_from.length === 0 ? (
                            <span className="text-label text-ink-3">
                              {e.delta === 'unchanged_checkpoint'
                                ? 'no new findings (checkpoint)'
                                : 'no findings recorded'}
                            </span>
                          ) : (
                            e.derived_from.map((ref) => (
                              <button
                                key={ref}
                                type="button"
                                onClick={() =>
                                  selectRow('finding', ref, ref, {
                                    origin: 'situation_trajectory',
                                  })
                                }
                                className="rounded border border-line bg-surf-2 px-1.5 py-0.5 font-mono text-label text-ink-2 hover:text-ink-1"
                                title="A new finding that moved this frame"
                              >
                                {ref.slice(0, 8)}…
                              </button>
                            ))
                          )}
                        </div>
                      </li>
                    )
                  })}
                </ol>
              )}
            </>
          )}
        </div>
      </div>
    </PanelChrome>
  )
}

function FrameRow({
  frame,
  active,
  onPick,
}: {
  frame: SituationFrame
  active: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onPick}
      data-testid={`trajectory-frame-${frame.id}`}
      className={cn(
        'w-full rounded border px-1.5 py-1 text-left',
        active
          ? 'border-line-strong bg-surf-3 text-ink-1'
          : 'border-transparent text-ink-2 hover:bg-surf-2',
      )}
    >
      <div className="truncate text-body">{frame.name}</div>
      <div className="flex items-center gap-1.5 text-label text-ink-3">
        <span>{frame.status}</span>
        <span>·</span>
        <span>{frame.event_count} ev</span>
        {frame.last_event_at && (
          <>
            <span>·</span>
            <span>{relativeTime(frame.last_event_at)}</span>
          </>
        )}
      </div>
    </button>
  )
}
