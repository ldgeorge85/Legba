import { useState } from 'react'
import { useJournal } from '@/api/hooks'
import { TimeAgo } from '@/components/common/TimeAgo'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function JournalPanel() {
  const { data, isLoading } = useJournal()
  const [showEntries, setShowEntries] = useState(false)

  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>
  if (!data?.entries?.length && !data?.consolidation)
    return <div className="p-4 text-sm text-muted-foreground">No journal entries</div>

  const sortedEntries = [...(data.entries ?? [])].sort((a, b) => b.cycle - a.cycle)

  return (
    <div className="h-full overflow-auto p-4 space-y-4">
      {/* Consolidation report */}
      {data.consolidation && (
        <div className="bg-card border border-border rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-primary">Latest Consolidation</h3>
          </div>
          <div className="prose prose-invert prose-sm max-w-none
            prose-headings:text-foreground prose-headings:font-semibold
            prose-p:text-foreground/85 prose-p:leading-relaxed
            prose-strong:text-foreground
            prose-li:text-foreground/85
            prose-a:text-primary">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.consolidation}</ReactMarkdown>
          </div>
        </div>
      )}

      {/* Toggle for individual entries */}
      {sortedEntries.length > 0 && (
        <>
          <button
            onClick={() => setShowEntries(!showEntries)}
            className="flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <span className="font-mono">{showEntries ? '▼' : '▶'}</span>
            <span>Cycle entries ({sortedEntries.length})</span>
          </button>

          {showEntries && (
            <div className="space-y-3">
              {sortedEntries.map((entry) => (
                <div key={entry.cycle} className="bg-card border border-border rounded-lg p-3">
                  <div className="flex items-center gap-2 mb-2 text-xs text-muted-foreground">
                    <span className="font-mono font-medium text-primary">Cycle {entry.cycle}</span>
                    <TimeAgo date={entry.timestamp} />
                  </div>
                  {entry.entries ? (
                    <div className="space-y-2">
                      {entry.entries.map((line, j) => (
                        <p key={j} className="text-sm leading-relaxed text-foreground/90">{line}</p>
                      ))}
                    </div>
                  ) : entry.content ? (
                    <div className="text-sm whitespace-pre-wrap leading-relaxed">{entry.content}</div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
