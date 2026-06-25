/**
 * SeverityBadge — categorical severity as icon + label, never color alone
 * (redesign Move 5, WCAG-AA / colorblind pass P-C4).
 *
 * Color is retained as a *redundant* channel; the icon shape + text label are
 * the primary distinguishers so the category survives a colorblind viewer or a
 * grayscale print. Used by the live feed, findings, and any categorical state.
 */
import {
  AlertOctagon,
  AlertTriangle,
  AlertCircle,
  Info,
  CircleDot,
  type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import { SEVERITY_COLOR, type Severity } from '@/v4/world/types'

const SEVERITY_ICON: Record<Severity, LucideIcon> = {
  critical: AlertOctagon,
  high: AlertTriangle,
  medium: AlertCircle,
  low: CircleDot,
  info: Info,
}

/** A small shape-coded severity dot (icon, not a bare colored circle). */
export function SeverityDot({ severity, className }: { severity: Severity; className?: string }) {
  const Icon = SEVERITY_ICON[severity] ?? Info
  return (
    <Icon
      className={cn('shrink-0', className)}
      style={{ color: SEVERITY_COLOR[severity] }}
      aria-hidden
    />
  )
}

/** Icon + text label — the full, AA-safe categorical chip. */
export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  return (
    <span
      className={cn('inline-flex items-center gap-1 text-label', className)}
      title={`severity: ${severity}`}
    >
      <SeverityDot severity={severity} className="h-3 w-3" />
      <span className="capitalize">{severity}</span>
      <span className="sr-only">severity {severity}</span>
    </span>
  )
}
