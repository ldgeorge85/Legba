import { formatDistanceToNow } from 'date-fns'

interface Props {
  date: string | Date
  className?: string
}

export function TimeAgo({ date, className }: Props) {
  const d = typeof date === 'string' ? new Date(date) : date
  return (
    <span className={className} title={d.toISOString()}>
      {formatDistanceToNow(d, { addSuffix: true })}
    </span>
  )
}
