/**
 * UI-4 (Tier D) — starter-descriptor picker.
 *
 * Small list UI that lets the operator clone-and-edit a starter descriptor
 * for a given family. On pick it calls `onClone` with a *fresh* deep-cloned
 * body — the registry panel then drops that body into the inline
 * `DescriptorEditor` (the YAML escape hatch) pre-filled. This is the "less
 * raw" win for families/panels that don't have a guided form builder
 * (e.g. the stack registry, which uses a separate API path + 9 component
 * kinds): instead of a blank textarea, start from a working example.
 */

import { startersForFamily, type StarterFamily } from '@/lib/starter-descriptors'

interface StarterPickerProps {
  family: StarterFamily
  /** Called with a fresh clone of the chosen starter's body. */
  onClone: (body: Record<string, unknown>) => void
  onCancel?: () => void
}

export function StarterPicker({ family, onClone, onCancel }: StarterPickerProps) {
  const starters = startersForFamily(family)
  return (
    <div
      className="bg-surface-200 border border-sky-800/60 rounded p-2 space-y-2 mt-2"
      data-testid="starter-picker"
    >
      <div className="flex items-center justify-between">
        <div className="text-sky-300 text-[10px] uppercase tracking-wide">
          clone a starter {family}
        </div>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="text-slate-400 hover:text-slate-200 text-[10px] underline"
            data-testid="starter-cancel"
          >
            cancel
          </button>
        )}
      </div>
      {starters.length === 0 && (
        <div className="text-slate-500 text-[11px]">no starters for this family</div>
      )}
      <div className="space-y-1">
        {starters.map((s) => (
          <button
            key={s.key}
            type="button"
            onClick={() => onClone(s.build())}
            className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2"
            data-testid={`starter-${s.key}`}
          >
            <div className="text-slate-200 text-[11px] font-medium">{s.label}</div>
            <div className="text-slate-500 text-[10px] mt-0.5">{s.blurb}</div>
          </button>
        ))}
      </div>
    </div>
  )
}
