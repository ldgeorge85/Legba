import { useDashboard } from '@/api/hooks'
import { Activity } from 'lucide-react'

export function StatusBar() {
  const { data } = useDashboard()

  return (
    <div className="flex items-center h-6 px-3 bg-card border-t border-border text-[11px] text-muted-foreground gap-4 shrink-0">
      <span className="flex items-center gap-1">
        <Activity size={12} className={data?.agent_status === 'running' ? 'text-green-400' : 'text-muted-foreground'} />
        {data?.agent_status ?? 'unknown'}
      </span>
      {data && (
        <>
          <span>Cycle {data.current_cycle}</span>
          <span>{data.signals} signals</span>
          <span>{data.events} events</span>
          <span>{data.entities} entities</span>
          <span>{data.relationships} relationships</span>
        </>
      )}
      <span className="ml-auto font-mono">
        {new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC
      </span>
    </div>
  )
}
