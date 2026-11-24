/**
 * Flow (N-J v0) — compare two stored versions of one descriptor: a semantic
 * diff of fields (added / removed / changed leaf paths) and a line diff of
 * every long text that changed (the prompt, a rubric). Read-only.
 */
import { useMemo, useState } from 'react'
import { cn } from '@/lib/cn'
import type { FlowVersionBody, VersionRow } from './flowTypes'
import { changedTexts, lineDiff, semanticDiff, type FieldChange } from './flowDiff'
import { useFlowVersion } from './useFlowData'
import { Section, asText } from './flowBits'

const short = (v: string) => v.slice(0, 12)

export function DiffView({ before, after }: { before: FlowVersionBody; after: FlowVersionBody }) {
  const changes = useMemo(() => semanticDiff(before.fields, after.fields), [before, after])
  const texts = useMemo(() => changedTexts(changes), [changes])
  if (changes.length === 0) {
    return <p className="text-body text-ink-3" data-testid="flow-diff-empty">The two versions have identical fields.</p>
  }
  return (
    <div className="space-y-3" data-testid="flow-diff">
      <Section title={`Fields — ${changes.length} changed paths`}>
        <table className="w-full text-[11px]">
          <thead className="text-left text-ink-3"><tr><th>path</th><th>change</th><th>{short(before.version)}</th><th>{short(after.version)}</th></tr></thead>
          <tbody>
            {changes.map((c: FieldChange) => (
              <tr key={c.path} className="align-top">
                <td className="font-mono text-ink-1 pr-2 break-all">{c.path}</td>
                <td className={cn(c.kind === 'added' && 'text-accent-ok', c.kind === 'removed' && 'text-accent-critical', c.kind === 'changed' && 'text-accent-warning')}>{c.kind}</td>
                <td className="text-ink-2 break-all">{c.kind === 'added' ? '' : asText(c.before).slice(0, 160)}</td>
                <td className="text-ink-2 break-all">{c.kind === 'removed' ? '' : asText(c.after).slice(0, 160)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      {texts.map((c) => (
        <Section key={c.path} title={`Text diff — ${c.path}`}>
          <div className="max-h-96 overflow-auto rounded-control border border-line bg-surf-1 p-2 font-mono text-[11px]">
            {lineDiff(String(c.before), String(c.after)).map((op, i) => (
              <div
                key={i}
                className={cn('whitespace-pre-wrap', op.op === 'add' && 'bg-accent-ok/15 text-ink-1', op.op === 'del' && 'bg-accent-critical/15 text-ink-1 line-through', op.op === 'same' && 'text-ink-3')}
              >
                {op.op === 'add' ? '+ ' : op.op === 'del' ? '- ' : '  '}{op.text}
              </div>
            ))}
          </div>
        </Section>
      ))}
    </div>
  )
}

export function FlowCompare({ nodeId, versions }: { nodeId: string; versions: VersionRow[] }) {
  const head = versions.find((v) => v.is_head)?.version ?? versions[0]?.version ?? null
  const prev = versions.find((v) => v.version !== head)?.version ?? null
  const [a, setA] = useState<string | null>(prev)
  const [b, setB] = useState<string | null>(head)
  const qa = useFlowVersion(nodeId, a)
  const qb = useFlowVersion(nodeId, b)
  if (versions.length < 2) {
    return <p className="text-body text-ink-3">Only one version is stored; there is nothing to compare.</p>
  }
  const picker = (value: string | null, set: (v: string) => void, label: string) => (
    <label className="flex items-center gap-1 text-[11px] text-ink-3">
      {label}
      <select
        className="rounded-control border border-line bg-surf-1 px-1 py-0.5 text-ink-1"
        value={value ?? ''}
        onChange={(e) => set(e.target.value)}
      >
        {versions.map((v) => (
          <option key={v.version} value={v.version}>
            {short(v.version)} · {v.state}{v.is_head ? ' · head' : ''} · {v.created_at?.slice(0, 10) ?? ''}
          </option>
        ))}
      </select>
    </label>
  )
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-3">
        {picker(a, setA, 'before')}
        {picker(b, setB, 'after')}
      </div>
      {qa.isError || qb.isError ? (
        <p className="text-body text-accent-critical">Could not load a version.</p>
      ) : qa.data && qb.data ? (
        <DiffView before={qa.data} after={qb.data} />
      ) : (
        <p className="text-body text-ink-3">Loading versions…</p>
      )}
    </div>
  )
}
