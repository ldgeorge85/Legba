import { openRecord } from '@/lib/recordSheet'
import { navigate, type PhoneTab } from '@/mobile/shell/phoneAddress'
import type { PhoneChangedRow } from './changedModel'

/** What tapping a Changed row does: a read opens its page, a situation or an alert its Record, the rest its subject. */
export function openChangedRow(r: PhoneChangedRow, tab: PhoneTab = 'changed'): (() => void) | null {
  const o = r.open
  if (!o) return null
  if ('read' in o) return () => navigate({ tab, page: { kind: 'read', id: o.read }, record: null, peek: false }, 'push')
  if ('record' in o) return () => openRecord(o.record, { origin: 'phone' })
  if (tab === 'subjects') return null // already on that subject's page
  return () => navigate({ tab: 'subjects', page: { kind: 'subject', id: o.subject }, record: null, peek: false }, 'push')
}
