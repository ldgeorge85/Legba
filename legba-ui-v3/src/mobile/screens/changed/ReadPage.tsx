/**
 * A read, opened from Changed or a subject page (M-3a; plan §2.3 row 2). One
 * full row, fetched on open by its id (`GET /findings/{id}`, P-8); the read's
 * blocks render through the phone read view (`ReportView`: the shared
 * `RecordBlock` / `CitedProse` pieces), statements first. A citation opens
 * the Record's peek (the shell follows the point). "Ask about this" pins the
 * read (and the subject in focus inside it) for Consult.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError, apiGet } from '@/lib/api'
import type { ReadFindingRow } from '@/lib/assemblyModel'
import { emitRead } from '@/lib/readTelemetry'
import type { Selection } from '@/state/selection'
import { subjectName, useSubjectIndex } from '@/lib/deskNames'
import { useReadTiers } from '@/lib/profileUi'
import ReportView from '@/mobile/components/ReportView'
import { readerSubjectName, tierLabel, tierOf, untargetedReadSubject } from '@/mobile/mobileModel'
import { back } from '@/mobile/shell/phoneAddress'
import { AskAboutThis, Quiet } from '@/mobile/shell/ui'

/** The one row a read page reads. */
export function readPath(id: string): string {
  return `/findings/${encodeURIComponent(id)}`
}

export default function ReadPage({ id }: { id: string }) {
  const q = useQuery<ReadFindingRow>({
    queryKey: ['phone', 'read', id],
    queryFn: () => apiGet<ReadFindingRow>(readPath(id)),
    staleTime: 5 * 60_000,
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 1,
  })
  const row = q.data && q.data.id === id ? q.data : null
  const [entity, setEntity] = useState<string | null>(null)
  const subjects = useSubjectIndex()
  const { tiers } = useReadTiers()
  const nameOf = useMemo(() => (t: string) => subjectName(t, subjects), [subjects])

  useEffect(() => {
    if (row) emitRead('brief_read', { subjectKind: 'finding', subjectId: row.id, workspace: 'mobile' })
  }, [row])

  if (!row) {
    return (
      <div className="ph-screen" data-testid="phone-read-gap">
        {q.isPending ? (
          <Quiet testId="report-loading">Opening the read…</Quiet>
        ) : q.error instanceof ApiError && q.error.status === 404 ? (
          <Quiet testId="report-missing">This read is not on record.</Quiet>
        ) : (
          <Quiet testId="report-missing">This read could not be read. Nothing is shown in its place.</Quiet>
        )}
      </div>
    )
  }

  const tier = tierOf(row, tiers)
  const target = row.target_id ? (nameOf(row.target_id) ?? row.target_id) : untargetedReadSubject(tier, tiers)
  const tierWord = tier ? tierLabel(tier, tiers) : 'Read'
  const entityName = entity ? (nameOf(entity) ?? entity) : null
  // the desktop's pins: the read, and the subject in focus inside it
  const pins: Selection[] = [
    {
      kind: 'finding',
      id: row.id,
      label: row.title?.trim() || `${tierWord} · ${readerSubjectName(target)}`,
      preview: { title: row.title ?? undefined, body: `${tierWord} read about ${readerSubjectName(target)}, written ${row.produced_at}.` },
    },
    ...(entity ? [{ kind: 'target' as const, id: entity, label: entityName ?? entity }] : []),
  ]
  return (
    <div className="ph-read">
      <ReportView
        row={row}
        entityId={entity}
        onSelectEntity={setEntity}
        onBack={back}
        offlineNote={q.isError ? 'No connection. Showing what was loaded earlier.' : null}
        nameOf={nameOf}
      />
      <div className="ph-screen ph-screen-tight">
        <AskAboutThis
          label={`${readerSubjectName(target)}${entityName ? ` › ${entityName}` : ''}`}
          pins={pins}
        />
      </div>
    </div>
  )
}
