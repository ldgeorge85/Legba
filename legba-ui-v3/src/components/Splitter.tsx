/**
 * Splitter — a draggable divider of the frame (U-6): the stage/chat divider
 * (horizontal) and the column edges (vertical). Pointer-driven, keyboard-
 * reachable (arrows step 2 % of the span, Home/End to the ends), with a
 * hit area wider than the 1 px rule it draws. It owns no size: it reports the
 * pointer's position along its axis and the parent clamps and stores it.
 */
import { useCallback, useRef } from 'react'
import { cn } from '@/lib/cn'

export function Splitter({
  orientation,
  onMove,
  onEnd,
  onKeyStep,
  label,
  testId,
  value,
}: {
  /** `horizontal`: a bar between a top and a bottom (drags up and down). */
  orientation: 'horizontal' | 'vertical'
  /** The pointer's client coordinate along the drag axis. */
  onMove: (clientPos: number) => void
  onEnd?: () => void
  /** A keyboard step: −1 / +1 (arrows), or −Infinity / +Infinity (Home / End). */
  onKeyStep?: (step: number) => void
  label: string
  testId: string
  /** For assistive tech: the current share, 0–100. */
  value?: number
}) {
  const dragging = useRef(false)
  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.preventDefault()
      dragging.current = true
      const el = e.currentTarget
      try {
        el.setPointerCapture(e.pointerId)
      } catch {
        // capture is a nicety
      }
      document.body.style.userSelect = 'none'
      const move = (ev: PointerEvent) => {
        if (!dragging.current) return
        onMove(orientation === 'horizontal' ? ev.clientY : ev.clientX)
      }
      const up = () => {
        dragging.current = false
        document.body.style.userSelect = ''
        window.removeEventListener('pointermove', move)
        window.removeEventListener('pointerup', up)
        onEnd?.()
      }
      window.addEventListener('pointermove', move)
      window.addEventListener('pointerup', up)
    },
    [onMove, onEnd, orientation],
  )
  const horizontal = orientation === 'horizontal'
  return (
    <div
      role="separator"
      tabIndex={0}
      aria-orientation={horizontal ? 'horizontal' : 'vertical'}
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={100}
      title={`${label} — drag, or use the arrow keys`}
      data-testid={testId}
      onPointerDown={onPointerDown}
      onKeyDown={(e) => {
        if (!onKeyStep) return
        const back = horizontal ? 'ArrowUp' : 'ArrowLeft'
        const fwd = horizontal ? 'ArrowDown' : 'ArrowRight'
        const step = e.key === back ? -1 : e.key === fwd ? 1 : e.key === 'Home' ? -Infinity : e.key === 'End' ? Infinity : 0
        if (step !== 0) {
          e.preventDefault()
          onKeyStep(step)
        }
      }}
      className={cn(
        'group relative z-10 shrink-0 select-none touch-none',
        horizontal ? 'h-2 -my-[3px] w-full cursor-row-resize' : 'w-2 -mx-[3px] h-full cursor-col-resize',
      )}
    >
      <span
        aria-hidden
        className={cn(
          'absolute bg-line transition-colors group-hover:bg-point/70 group-focus-visible:bg-point',
          horizontal ? 'inset-x-0 top-1/2 h-px -translate-y-1/2' : 'inset-y-0 left-1/2 w-px -translate-x-1/2',
        )}
      />
    </div>
  )
}
