/**
 * ConsultPointLine — the line above Consult's question box that names what a
 * question will be asked ABOUT (U-2 shell; brief §2 "What it knows").
 *
 *   Asking about: Brazil · desk · window 7d · 3 pinned          [clear]
 *
 * It reads only what the existing stores already provide: the ambient pin
 * (`lib/consultContext.ts` — the newest live claim among the rail, the subject
 * page, the map and the wall scope), the one clock's span
 * (`v4/world/worldState.ts`) and the number of pins this Consult slice holds.
 * "Clear" drops the wall scope, which releases the ambient pin; nothing else
 * is touched. It sends nothing and changes nothing about how Consult answers.
 * Since U-8 Consult passes `windowWords` — the window its next turn is sent
 * with (`lib/consultContract.ts`) — so the line names the window actually asked.
 */
import { useAmbientPin } from '@/lib/consultContext'
import { humanizeId } from '@/lib/deskNames'
import { spanPresetFor } from '@/lib/mapTime'
import { selectionKindLabel } from '@/lib/subjectNoun'
import { useScope } from '@/state/scope'
import { useWorldState } from '@/v4/world/worldState'

function spanText(ms: number): string {
  const preset = spanPresetFor(ms)
  if (preset) return preset.label
  const h = Math.round(ms / 3_600_000)
  return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`
}

export function ConsultPointLine({ pinned, windowWords }: { pinned: number; windowWords?: string }) {
  const ambient = useAmbientPin()
  const spanMs = useWorldState((s) => s.spanMs)
  const scope = useScope((s) => s.scope)
  const pin = ambient?.pin ?? null
  // A subject is named by what it declares (`humanizeId` reads the roster),
  // never by its machine id; any other record by its label, else its id.
  const name = pin
    ? pin.label && pin.label !== pin.id
      ? pin.label
      : pin.kind === 'target'
        ? humanizeId(pin.id)
        : pin.id
    : null
  return (
    <div
      className="flex min-w-0 items-baseline gap-2 text-label text-ink-3"
      data-testid="consult-point-line"
      title={pin ? `${selectionKindLabel(pin.kind)} ${pin.id}` : 'no point is set — the question is asked about everything in view'}
    >
      <span className="shrink-0 uppercase tracking-[0.06em]">Asking about</span>
      <span className="min-w-0 truncate">
        {name ? (
          <>
            <span className="text-point" data-testid="consult-point-name">
              {name}
            </span>
            <span> · {selectionKindLabel(pin!.kind).toLowerCase()}</span>
          </>
        ) : (
          <span className="text-ink-2" data-testid="consult-point-name">
            everything in view
          </span>
        )}
        {/* U-8 — the window the turn is SENT with, when Consult chose one;
            else the time band's span (the line stays true to the request). */}
        <span className="font-mono" data-testid="consult-point-window"> · window {windowWords ?? spanText(spanMs)}</span>
        {pinned > 0 && <span className="font-mono"> · {pinned} pinned</span>}
      </span>
      {scope && (
        <button
          type="button"
          onClick={() => useScope.getState().setScope(null)}
          className="ml-auto shrink-0 text-ink-3 underline underline-offset-2 hover:text-ink-1"
          data-testid="consult-point-clear"
          title="clear the point: the next question is asked about everything in view"
        >
          clear
        </button>
      )}
    </div>
  )
}
