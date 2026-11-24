/**
 * boundRegistration — the registration a BOUND read renders against
 * (`<kind>:<record>`): a per-subject or per-unit kind on one record. The dock
 * (`App.tsx`) and the stage (`components/ViewHost.tsx`) build it the same way,
 * so a subject's page is the same read wherever it sits.
 */
import type { PanelKind, PanelRegistration } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { boundTileTitle } from '@/lib/subjectTitle'

export function boundRegistration(
  kind: PanelKind,
  recordId: string,
  mode: PanelRegistration['mode'],
  axis: 'target' | 'analyst',
): PanelRegistration | null {
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def) return null
  const id = `${kind}:${recordId}`
  return {
    id,
    panel_id: def.panelId,
    descriptor_id: recordId,
    descriptor_version: '00000000',
    descriptor_family: axis,
    analyst_id: axis === 'analyst' ? recordId : null,
    title: boundTileTitle(kind, def.defaultTitle, recordId, axis),
    mode,
    layout_slot: kind,
    data_query: {},
    binding: axis === 'analyst' ? { analyst_id: recordId } : { target_id: recordId },
    retired: false,
    created_at: new Date().toISOString(),
    retired_at: null,
  }
}
