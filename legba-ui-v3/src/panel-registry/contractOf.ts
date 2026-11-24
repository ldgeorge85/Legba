/**
 * contractOf — every view's contract: its declared one (`contracts.ts`), or a
 * plain default derived from the registry for a view that has not declared
 * one yet (the operator panels listed in `OPERATOR_KINDS_WITHOUT_CONTRACT`):
 * the three common actions, a panel home, and a binding only when the kind
 * requires one.
 */
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { PANEL_DESCRIPTIONS } from '@/panel-registry/descriptions'
import { declaredContract, viewIdOf, type BindType, type PanelContract } from '@/panel-registry/contracts'
import type { PanelKind } from '@/types'

export function contractOf(id: string): PanelContract | undefined {
  const declared = declaredContract(id)
  if (declared) return declared
  const kind = viewIdOf(id) as PanelKind
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def) return undefined
  const binds: BindType[] = def.requiresBinding ? [def.category === 'analyst' ? 'unit' : 'subject'] : []
  return {
    id: kind,
    name: def.defaultTitle,
    description: def.description ?? PANEL_DESCRIPTIONS[kind] ?? '',
    group: 'Operator',
    side: 'operator',
    binds,
    size: 'column',
    home: 'panel',
    actions: [],
  }
}
