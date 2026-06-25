/**
 * DescriptorView — render a descriptor body human-readably.
 *
 * Registry panels historically expanded a descriptor row to a raw
 * `JSON.stringify` dump, so understanding the operating config (targets,
 * sources, analysts, packs) meant reading raw JSON. This component renders the
 * body as a labeled key/value tree — scalars inline, arrays as chips or nested
 * lists, objects indented — with a one-click toggle back to raw JSON for power
 * users. It is the shared readable surface adopted across the registry panels
 * (Targets first, then Sources / Analysts / Stack / ActionPacks).
 */

import { useState } from 'react'

interface DescriptorViewProps {
  body: Record<string, unknown>
  /** Field keys to float to the top, in order; the rest follow alphabetically. */
  primaryKeys?: readonly string[]
  /** Start in raw-JSON mode (default false → readable). */
  defaultRaw?: boolean
}

export function DescriptorView({ body, primaryKeys, defaultRaw = false }: DescriptorViewProps) {
  const [raw, setRaw] = useState(defaultRaw)
  return (
    <div className="mt-2">
      <div className="flex justify-end mb-1">
        <button
          type="button"
          onClick={() => setRaw((r) => !r)}
          className="text-label text-ink-3 hover:text-ink-1 underline decoration-dotted"
        >
          {raw ? 'readable view' : 'raw JSON'}
        </button>
      </div>
      {raw ? (
        <pre className="bg-surf-1 pad-density rounded overflow-x-auto text-body text-ink-2 max-h-96">
          {JSON.stringify(body, null, 2)}
        </pre>
      ) : (
        <div className="bg-surf-1 pad-density rounded max-h-96 overflow-auto">
          <FieldList obj={body} primaryKeys={primaryKeys} />
        </div>
      )}
    </div>
  )
}

/** Entries ordered: primaryKeys (in given order) first, then the rest sorted. */
function orderedEntries(
  obj: Record<string, unknown>,
  primaryKeys?: readonly string[],
): Array<[string, unknown]> {
  const primary = (primaryKeys ?? []).filter((k) => k in obj)
  const rest = Object.keys(obj)
    .filter((k) => !primary.includes(k))
    .sort()
  return [...primary, ...rest].map((k): [string, unknown] => [k, obj[k]])
}

function FieldList({
  obj,
  primaryKeys,
}: {
  obj: Record<string, unknown>
  primaryKeys?: readonly string[]
}) {
  const entries = orderedEntries(obj, primaryKeys)
  if (entries.length === 0) return <span className="text-ink-3 text-body">(empty)</span>
  return (
    <dl className="space-y-rows">
      {entries.map(([key, value]) => (
        <div key={key} className="flex gap-2 text-body">
          <dt className="text-ink-2 shrink-0 min-w-[8rem] font-mono">{key}</dt>
          <dd className="text-ink-1 min-w-0 flex-1 break-words">
            <DescriptorValue value={value} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

function DescriptorValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-ink-3">—</span>
  if (typeof value === 'boolean') return <span className="text-sky-400">{value ? 'true' : 'false'}</span>
  if (typeof value === 'number') return <span className="text-amber-400">{value}</span>
  if (typeof value === 'string') return <span>{value}</span>
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-ink-3">[]</span>
    const allScalar = value.every((v) => v === null || typeof v !== 'object')
    if (allScalar) {
      return (
        <span className="flex flex-wrap gap-1">
          {value.map((v, i) => (
            <span key={i} className="bg-surf-2 rounded px-1 text-label text-ink-1">
              {v === null ? '—' : String(v)}
            </span>
          ))}
        </span>
      )
    }
    return (
      <div className="space-y-rows border-l border-line pl-2">
        {value.map((v, i) => (
          <div key={i}>
            <DescriptorValue value={v} />
          </div>
        ))}
      </div>
    )
  }
  return (
    <div className="border-l border-line pl-2">
      <FieldList obj={value as Record<string, unknown>} />
    </div>
  )
}
