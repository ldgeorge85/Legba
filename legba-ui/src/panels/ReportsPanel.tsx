import { useState } from 'react'
import { useReports } from '@/api/hooks'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Download } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

function downloadMarkdown(filename: string, content: string) {
  const blob = new Blob([content], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function ReportsPanel() {
  const { data, isLoading } = useReports()
  const [selectedCycle, setSelectedCycle] = useState<number | null>(null)

  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>
  if (!data?.length) return <div className="p-4 text-sm text-muted-foreground">No reports found</div>

  const selected = selectedCycle !== null ? data.find((r) => r.cycle === selectedCycle) : null

  return (
    <div className="flex h-full">
      {/* Report list */}
      <div className="w-56 shrink-0 border-r border-border overflow-auto">
        <div className="space-y-0.5 p-1">
          {data.map((report) => (
            <button
              key={report.cycle}
              onClick={() => setSelectedCycle(report.cycle)}
              className={`w-full text-left px-3 py-2 rounded text-sm transition-colors ${
                selectedCycle === report.cycle
                  ? 'bg-primary/10 text-primary'
                  : 'hover:bg-secondary text-foreground'
              }`}
            >
              <p className="font-medium truncate">Cycle {report.cycle}</p>
              <TimeAgo date={report.timestamp} className="text-[10px] text-muted-foreground" />
            </button>
          ))}
        </div>
      </div>

      {/* Report content */}
      <div className="flex-1 overflow-auto">
        {selected ? (
          <>
            <div className="flex items-center justify-between px-4 py-2 border-b border-border shrink-0">
              <span className="text-sm font-medium">Cycle {selected.cycle} Report</span>
              <button
                onClick={() => downloadMarkdown(`legba_report_cycle_${selected.cycle}.md`, selected.content)}
                className="flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-secondary hover:bg-secondary/80 text-muted-foreground hover:text-foreground transition-colors"
                title="Download as Markdown"
              >
                <Download size={12} />
                Download .md
              </button>
            </div>
            <div className="p-6 prose prose-invert prose-sm max-w-none
              prose-headings:text-foreground prose-headings:font-semibold prose-headings:border-b prose-headings:border-border/50 prose-headings:pb-2 prose-headings:mb-3
              prose-h1:text-lg prose-h2:text-base prose-h3:text-sm
              prose-p:text-foreground/85 prose-p:leading-relaxed
              prose-strong:text-foreground
              prose-li:text-foreground/85 prose-li:leading-relaxed
              prose-ul:my-2 prose-ol:my-2
              prose-table:text-xs
              prose-th:text-foreground prose-th:bg-secondary/50 prose-th:px-3 prose-th:py-1.5
              prose-td:px-3 prose-td:py-1.5 prose-td:border-border/30
              prose-hr:border-border/50
              prose-a:text-primary prose-a:no-underline hover:prose-a:underline
              prose-code:text-primary/80 prose-code:bg-secondary prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs
              prose-blockquote:border-primary/30 prose-blockquote:text-muted-foreground"
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{selected.content}</ReactMarkdown>
            </div>
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
            Select a report to view
          </div>
        )}
      </div>
    </div>
  )
}
