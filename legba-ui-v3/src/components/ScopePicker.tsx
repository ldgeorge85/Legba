/**
 * ScopePicker — a descriptor dropdown sourced from the live registry.
 *
 * The "less raw" replacement for free-text id boxes: instead of typing a
 * target/source/analyst id, the operator picks a real, current descriptor.
 * Backed by GET /api/v1/registry/descriptors?family=…&head_only=true.
 *
 *   <ScopePicker family="target" value={id} onChange={setId} />
 *
 * Since UI-B1/tokens it renders the `Select` primitive (`src/ui/select.tsx`)
 * rather than a native `<select>`: the menu is drawn on the workstation tokens
 * (a native menu ignores the theme), it portals into the shell's overlay host,
 * and it keeps the keyboard contract (Enter/Space/↑/↓ open, typeahead, Enter
 * picks, Escape closes). The props are unchanged. `data-testid` sits on the
 * trigger, which also stamps `data-value`; each option carries
 * `<testId>-option-<descriptor_id>`. An id that is not (yet) in the loaded
 * list still shows as itself rather than as a blank.
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { rosterRoute } from '@/lib/deskNames'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'

/** Radix reserves "" for "back to the placeholder"; the explicit none-row uses this. */
const NONE = '__scope_picker_none__'

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
      // RA-1: the three roster families read the reader's roster (id, name,
      // state are all a picker reads); stack and action packs are the admin's.
      const rows = await apiGet<DescriptorRow[]>(
        family === 'target' || family === 'analyst' || family === 'source'
          ? rosterRoute(family, family === 'source' ? 1000 : 500)
          : `/registry/descriptors?family=${family}&head_only=true&limit=500`,
      )
      return (Array.isArray(rows) ? rows : []).filter(
        (r) => (r.state ?? 'active') === 'active',
      )
    },
    staleTime: 30_000,
  })

  const rows = q.data ?? []
  const pickerId = testId ?? `scope-picker-${family}`
  const current = rows.find((r) => r.descriptor_id === value)
  const valueLabel = current ? rowLabel(current) : value
  return (
    <Select value={value} onValueChange={(v) => onChange(v === NONE ? '' : v)}>
      <SelectTrigger
        className={className ?? 'max-w-[200px] text-[11px]'}
        data-testid={pickerId}
        aria-label={placeholder ?? `select ${family}`}
      >
        <SelectValue placeholder={placeholder ?? `select ${family}…`}>
          {value ? valueLabel : undefined}
        </SelectValue>
      </SelectTrigger>
      <SelectContent data-testid={`${pickerId}-content`}>
        {allowEmpty && (
          <SelectItem value={NONE} data-testid={`${pickerId}-option-none`}>
            {placeholder ?? `select ${family}…`}
          </SelectItem>
        )}
        {rows.map((r) => (
          <SelectItem
            key={r.descriptor_id}
            value={r.descriptor_id}
            data-testid={`${pickerId}-option-${r.descriptor_id}`}
          >
            {rowLabel(r)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/** "<id> — <name>", or the bare id when the descriptor has no name. */
function rowLabel(r: DescriptorRow): string {
  return r.name ? `${r.descriptor_id} — ${r.name}` : r.descriptor_id
}
