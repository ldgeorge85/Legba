/**
 * DeepConsultResult — what a deep question shows in the Consult transcript:
 * the task's state, its answer once written, and the finding it produced.
 */
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { DeepConsultStatus } from '@/lib/api'

export function DeepConsultResult({ status: resp }: { status: DeepConsultStatus }) {
  const status = resp.status
  const terminal = status === 'completed' || status === 'failed'
  return (
    <div className="space-y-2 border-t border-line pt-2 text-sm" data-testid="deep-consult-result">
      <div className="flex items-center gap-2 text-label text-ink-3">
        <span className="text-point">Deep question</span>
        <span className={status === 'failed' ? 'text-accent-critical' : 'text-ink-2'} data-testid="deep-consult-status">
          {status === 'completed' ? 'answered' : status === 'failed' ? 'failed' : 'working on it'}
        </span>
        {!terminal && <span>checking every few seconds</span>}
        <span className="ml-auto font-mono">{resp.task_id}</span>
      </div>

      {status === 'completed' && resp.answer && (
        <div className="prose prose-invert prose-sm max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{resp.answer}</ReactMarkdown>
        </div>
      )}

      {status === 'failed' && <div className="text-accent-critical">{resp.detail ?? 'The task failed.'}</div>}

      {status === 'completed' && resp.finding_id && (
        <div className="flex flex-col gap-1 text-label text-ink-3">
          <span>
            Finding:{' '}
            <a className="text-point underline underline-offset-2" href={`#/lineage/${resp.finding_id}`}>
              {resp.finding_id}
            </a>
          </span>
          {resp.uncertainty != null && <span>uncertainty: {resp.uncertainty.toFixed(2)}</span>}
          {resp.cited_refs.length > 0 && <span>records cited: {resp.cited_refs.length}</span>}
          {resp.fact_ids.length > 0 && <span>facts: {resp.fact_ids.length}</span>}
          {resp.hypothesis_ids.length > 0 && <span>hypotheses: {resp.hypothesis_ids.length}</span>}
        </div>
      )}
    </div>
  )
}
