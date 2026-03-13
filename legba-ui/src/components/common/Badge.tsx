import { cn } from '@/lib/utils'

interface BadgeProps {
  children: React.ReactNode
  variant?: string
  className?: string
}

export function Badge({ children, variant, className }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium',
        variant ?? 'bg-secondary text-secondary-foreground border-transparent',
        className,
      )}
    >
      {children}
    </span>
  )
}
