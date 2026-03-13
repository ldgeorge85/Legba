import { useState } from 'react'
import { useCycles } from '@/api/hooks'
import { Badge } from '@/components/common/Badge'
import { ChevronLeft, ChevronRight } from 'lucide-react'

const PAGE_SIZE = 50

function cycleTypeColor(type: string) {
  switch (type) {
    case 'EVOLVE': return 'bg-orange-500/20 text-orange-400'
    case 'INTROSPECTION': return 'bg-purple-500/20 text-purple-400'
    case 'ANALYSIS': return 'bg-cyan-500/20 text-cyan-400'
    case 'RESEARCH': return 'bg-blue-500/20 text-blue-400'
    case 'ACQUIRE': return 'bg-green-500/20 text-green-400'
    case 'NORMAL': return 'bg-secondary text-muted-foreground'
    default: return 'bg-secondary text-muted-foreground'
  }
}

export function CycleMonitorPanel() {
  const [offset, setOffset] = useState(0)
  const { data, isLoading } = useCycles({ offset, limit: PAGE_SIZE })

  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>
  if (!data?.items.length) return <div className="p-4 text-sm text-muted-foreground">No cycles found</div>

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-card border-b border-border">
            <tr className="text-left text-xs text-muted-foreground">
              <th className="px-3 py-2 font-medium">#</th>
              <th className="px-3 py-2 font-medium">Type</th>
              <th className="px-3 py-2 font-medium">Duration</th>
              <th className="px-3 py-2 font-medium">Tools</th>
              <th className="px-3 py-2 font-medium">LLM</th>
              <th className="px-3 py-2 font-medium">Events</th>
              <th className="px-3 py-2 font-medium">Errors</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((cycle) => (
              <tr key={cycle.cycle_number} className="border-b border-border/50 hover:bg-secondary/50 cursor-pointer">
                <td className="px-3 py-2 font-mono">{cycle.cycle_number}</td>
                <td className="px-3 py-2">
                  <Badge className={`text-[10px] ${cycleTypeColor(cycle.cycle_type)}`}>{cycle.cycle_type}</Badge>
                </td>
                <td className="px-3 py-2 text-muted-foreground">{cycle.duration_s.toFixed(0)}s</td>
                <td className="px-3 py-2 text-muted-foreground">{cycle.tool_calls}</td>
                <td className="px-3 py-2 text-muted-foreground">{cycle.llm_calls}</td>
                <td className="px-3 py-2 text-muted-foreground">{cycle.events_stored}</td>
                <td className="px-3 py-2">
                  {cycle.errors > 0 ? (
                    <span className="text-red-400">{cycle.errors}</span>
                  ) : (
                    <span className="text-muted-foreground">0</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {data.total > PAGE_SIZE && (
        <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
          <span>{data.total} cycles</span>
          <div className="flex items-center gap-1">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} className="p-1 rounded hover:bg-secondary disabled:opacity-30">
              <ChevronLeft size={14} />
            </button>
            <span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(data.total / PAGE_SIZE)}</span>
            <button disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)} className="p-1 rounded hover:bg-secondary disabled:opacity-30">
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
