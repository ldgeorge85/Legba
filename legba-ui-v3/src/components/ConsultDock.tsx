/**
 * ConsultDock — Consult's place in the frame (U-6; brief §12, superseding the
 * fixed right-hand column of the U-2 shell).
 *
 * The chat's default home is the lower half of the centre, under the stage,
 * with the ask box at its foot (the owner: "Consult on bottom … more like
 * halfway, so you can still see the chat, but so we can have things above").
 * From its header the reader can:
 *
 *   ▾ / ▴     collapse it to the ask bar alone, and restore it;
 *   ⤒         pop it to the full centre (the stage is one click away);
 *   ◧ / ◨     move it to a column on the left of the centre or the right of
 *             the panels (resizable there), and ⬓ bring it back under the stage.
 *
 * The divider above it (in the frame) resizes it from the ask bar to nearly
 * the full height. Placement, size and collapse are remembered per page
 * (`state/frame.ts`). It renders the same `system.consult` kind a panel would
 * (`SingletonView`, store slice `singleton:system.consult`), so the
 * conversation survives every move, resize and page switch.
 */
import type { ReactNode } from 'react'
import { ChevronDown, ChevronUp, Maximize2, Minimize2, PanelBottom, PanelLeft, PanelRight } from 'lucide-react'
import type { currentMode } from '@/auth/jwt'
import { SingletonView } from '@/components/SingletonView'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import { ConsultFrameProvider } from '@/components/ConsultFrameContext'
import type { ChatPlacement } from '@/state/frame'
import { cn } from '@/lib/cn'

function HeadButton({
  testId,
  title,
  onClick,
  children,
  on,
}: {
  testId: string
  title: string
  onClick: () => void
  children: ReactNode
  on?: boolean
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      title={title}
      aria-label={title}
      aria-pressed={on}
      onClick={onClick}
      className={cn(
        'flex h-6 w-6 items-center justify-center rounded-control hover:bg-surf-3 hover:text-ink-1',
        on ? 'text-point' : 'text-ink-3',
      )}
    >
      {children}
    </button>
  )
}

export function ConsultDock({
  mode,
  placement,
  collapsed,
  onCollapse,
  onMove,
}: {
  mode: ReturnType<typeof currentMode>
  placement: ChatPlacement
  collapsed: boolean
  onCollapse: (collapsed: boolean) => void
  onMove: (placement: ChatPlacement) => void
}) {
  const compact = collapsed && placement === 'bottom'
  return (
    <aside
      aria-label="Consult"
      data-testid="consult-dock"
      data-placement={placement}
      data-collapsed={compact ? 'true' : 'false'}
      className={cn(
        'flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-surf-2',
        placement === 'left' && 'border-r border-line',
        placement === 'right' && 'border-l border-line',
      )}
    >
      <div className="flex h-7 shrink-0 items-center gap-1 border-b border-line px-2" data-testid="consult-dock-head">
        <span className="text-label uppercase tracking-[0.06em] text-point">Consult</span>
        <span className="ml-auto flex items-center gap-0.5">
          {placement === 'bottom' &&
            (compact ? (
              <HeadButton testId="consult-expand" title="Open the conversation" onClick={() => onCollapse(false)}>
                <ChevronUp size={14} aria-hidden />
              </HeadButton>
            ) : (
              <HeadButton testId="consult-collapse" title="Collapse to the ask bar" onClick={() => onCollapse(true)}>
                <ChevronDown size={14} aria-hidden />
              </HeadButton>
            ))}
          {placement === 'full' ? (
            <HeadButton testId="consult-unfull" title="Back under the stage" onClick={() => onMove('bottom')}>
              <Minimize2 size={13} aria-hidden />
            </HeadButton>
          ) : (
            <HeadButton testId="consult-full" title="Use the whole centre" onClick={() => onMove('full')}>
              <Maximize2 size={13} aria-hidden />
            </HeadButton>
          )}
          <HeadButton testId="consult-move-left" title="Move to a column on the left" on={placement === 'left'} onClick={() => onMove('left')}>
            <PanelLeft size={13} aria-hidden />
          </HeadButton>
          <HeadButton testId="consult-move-bottom" title="Under the stage (the default)" on={placement === 'bottom'} onClick={() => onMove('bottom')}>
            <PanelBottom size={13} aria-hidden />
          </HeadButton>
          <HeadButton testId="consult-move-right" title="Move to a column on the right" on={placement === 'right'} onClick={() => onMove('right')}>
            <PanelRight size={13} aria-hidden />
          </HeadButton>
        </span>
      </div>
      <div className="min-h-0 flex-1">
        <ConsultFrameProvider value={{ compact }}>
          <PanelEmbedProvider>
            <SingletonView kind="system.consult" mode={mode} />
          </PanelEmbedProvider>
        </ConsultFrameProvider>
      </div>
    </aside>
  )
}
