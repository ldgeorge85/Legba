/**
 * ScopePicker — a descriptor dropdown sourced from the live registry.
 *
 * The "less raw" replacement for free-text id boxes: instead of typing a
 * target/source/analyst id, the operator picks a real, current descriptor.
 * Backed by GET /api/v1/registry/descriptors?family=…&head_only=true.
 *
 *   <ScopePicker family="target" value={id} onChange={setId} />
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'

export type ScopeFamily = 'target' | 'source' | 'analyst' | 'stack' | 'action_pack'

interface DescriptorRow {
  descriptor_id: string
  name?: string | null
  state?: string | null
}

export function ScopePicker({
  family,
  value,
  onChange,
  placeholder,
  allowEmpty = true,
  className,
  testId,
}: {
  family: ScopeFamily
  value: string
  onChange: (v: string) => void
  placeholder?: string
  allowEmpty?: boolean
  className?: string
  testId?: string
}) {
  const q = useQuery<DescriptorRow[]>({
    queryKey: ['scope-picker', family],
    queryFn: async () => {
      const rows = await apiGet<DescriptorRow[]>(
        `/registry/descriptors?family=${family}&head_only=true&limit=500`,
      )
      return (Array.isArray(rows) ? rows : []).filter(
        (r) => (r.state ?? 'active') === 'active',
      )
    },
    staleTime: 30_000,
  })

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={
        className ??
        'bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-[11px] text-slate-200 max-w-[200px]'
      }
      data-testid={testId ?? `scope-picker-${family}`}
    >
      {allowEmpty && <option value="">{placeholder ?? `select ${family}…`}</option>}
      {(q.data ?? []).map((r) => (
        <option key={r.descriptor_id} value={r.descriptor_id}>
          {r.descriptor_id}
          {r.name ? ` — ${r.name}` : ''}
        </option>
      ))}
    </select>
  )
}
