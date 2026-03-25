import { formatDistanceToNow } from 'date-fns'

interface Props {
  date: string | Date
  className?: string
}

export function TimeAgo({ date, className }: Props) {
  if (!date) return <span className={className}>--</span>
  const d = typeof date === 'string' ? new Date(date) : date
  if (isNaN(d.getTime())) return <span className={className}>--</span>
  return (
    <span className={className} title={d.toISOString()}>
      {formatDistanceToNow(d, { addSuffix: true })}
    </span>
  )
}
