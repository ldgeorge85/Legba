/**
 * Subjects on the phone (M-3d; plan §2.3 row "Subjects").
 *
 *   ROOT   a search box, the subjects opened lately on this phone (ids only,
 *          in `localStorage`), and the roster — the subject registry's own
 *          list (`SUBJECT_ROSTER_QUERY_KEY`, the desktop's key and route).
 *   PAGE   the subject page, narrow: its sections STACKED in the design's
 *          phone order instead of the desktop's eight tabs — the header, "Ask
 *          about this", the latest reads about it (a read opens its page),
 *          what changed about it in the window, the forecasts that name it,
 *          and the map strip (the Map screen, the subject as the point).
 *          The subject is the point while the page is open (the selection),
 *          so Consult, the map and the Record follow it.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { declaredSubject, subjectName, useSubjectIndex, type DeclaredSubject } from '@/lib/deskNames'
import { humanizeAnalystId } from '@/lib/analystNames'
import { PROFILE_UI } from '@/lib/profileUi'
import { useSelection } from '@/state/selection'
import { usePinScope } from '@/lib/consultContext'
import { useLedgerForecasts } from '@/panels/ledger/ledgerQueries'
import { dueIn } from '@/lib/ledgerModel'
import { useWorldState } from '@/v4/world/worldState'
import { navigate } from '@/mobile/shell/phoneAddress'
import { AskAboutThis, HomeRow, Quiet, SectionHead } from '@/mobile/shell/ui'
import { changedRows, phoneWindowOf, useChanged } from '@/mobile/screens/changed/changedModel'
import { openChangedRow } from '@/mobile/screens/changed/openRow'

export const RECENT_SUBJECTS_KEY = 'legba_phone_recent_subjects'
const RECENT_MAX = 6

function readRecents(): string[] {
  try {
    const v = JSON.parse(window.localStorage.getItem(RECENT_SUBJECTS_KEY) ?? '[]') as unknown
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string').slice(0, RECENT_MAX) : []
  } catch {
    return []
  }
}

function pushRecent(id: string): void {
  try {
    const next = [id, ...readRecents().filter((x) => x !== id)].slice(0, RECENT_MAX)
    window.localStorage.setItem(RECENT_SUBJECTS_KEY, JSON.stringify(next))
  } catch {
    // storage blocked: no recents
  }
}

const noun = PROFILE_UI.subject.singular
const plural = PROFILE_UI.subject.plural

function nameOf(s: DeclaredSubject, index: ReturnType<typeof useSubjectIndex>): string {
  return subjectName(s.id, index) ?? s.name ?? s.id
}

function openSubject(id: string): void {
  navigate({ tab: 'subjects', page: { kind: 'subject', id }, record: null, peek: false }, 'push')
}

export function SubjectsScreen() {
  const index = useSubjectIndex()
  const [q, setQ] = useState('')
  const recents = useMemo(() => readRecents(), [])
  const all = useMemo(
    () => [...index.values()].filter((s) => s.state !== 'retired').sort((a, b) => nameOf(a, index).localeCompare(nameOf(b, index))),
    [index],
  )
  const needle = q.trim().toLowerCase()
  const shown = needle ? all.filter((s) => `${nameOf(s, index)} ${s.id} ${s.tags.join(' ')}`.toLowerCase().includes(needle)) : all
  return (
    <div className="ph-screen" data-testid="phone-subjects">
      <label className="ph-search">
        <span className="sr-only">Find a {noun}</span>
        <input
          type="search"
          className="ph-input"
          placeholder={`Find a ${noun}`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          data-testid="phone-subject-search"
          autoComplete="off"
        />
      </label>
      {!needle && recents.length > 0 ? (
        <>
          <SectionHead title="Opened lately" testId="phone-subject-recents" />
          <ul className="ph-list">
            {recents.map((id) => {
              const s = declaredSubject(id, index)
              return (
                <HomeRow key={id} testId="phone-subject-row" headline={s ? nameOf(s, index) : id} word={null} subject={s?.geo.join(', ') ?? ''} at={null} chip={noun} onOpen={() => openSubject(id)} />
              )
            })}
          </ul>
        </>
      ) : null}
      <SectionHead title={needle ? 'Matches' : `Every ${noun}`} count={index.size ? shown.length : null} />
      {index.size === 0 ? (
        <Quiet testId="phone-subjects-reading">Reading the {plural}…</Quiet>
      ) : shown.length === 0 ? (
        <Quiet>No {noun} matches “{q}”.</Quiet>
      ) : (
        <ul className="ph-list" data-testid="phone-subject-list">
          {shown.slice(0, 200).map((s) => (
            <HomeRow key={s.id} testId="phone-subject-row" headline={nameOf(s, index)} word={null} subject={s.geo.join(', ')} at={null} chip={noun} onOpen={() => openSubject(s.id)} />
          ))}
        </ul>
      )}
    </div>
  )
}

interface SummaryRow {
  id: string
  title?: string | null
  analyst_id?: string | null
  target_id?: string | null
  produced_at: string
}

/** The latest reads about one subject, at summary weight. */
export function subjectReadsPath(id: string): string {
  return `/findings?target_id=${encodeURIComponent(id)}&limit=5&fields=summary`
}

export function SubjectPage({ id }: { id: string }) {
  const index = useSubjectIndex()
  const s = declaredSubject(id, index)
  const name = s ? nameOf(s, index) : (subjectName(id, index) ?? id)
  useEffect(() => pushRecent(id), [id])
  // the subject is the point while its page is open
  useEffect(() => {
    const cur = useSelection.getState().selection
    if (!cur || cur.kind !== 'target' || cur.id !== id) useSelection.getState().select({ kind: 'target', id, label: name, origin: 'phone' })
  }, [id, name])
  usePinScope({ kind: 'target', id, label: name }, 'subject')

  const reads = useQuery<{ data: SummaryRow[] }>({
    queryKey: ['phone', 'subject-reads', id],
    queryFn: () => apiGet<{ data: SummaryRow[] }>(subjectReadsPath(id)),
    staleTime: 5 * 60_000,
  })
  const spanMs = useWorldState((st) => st.spanMs)
  const win = phoneWindowOf(spanMs)
  const changed = useChanged(win.id)
  const nameFor = (t: string) => subjectName(t, index) ?? t
  const mine = useMemo(() => changedRows(changed.data, nameFor).filter((r) => r.targetId === id && r.kind !== 'forecast_due'), [changed.data, id]) // eslint-disable-line react-hooks/exhaustive-deps
  const forecasts = useLedgerForecasts()
  const fc = useMemo(() => (forecasts.data?.forecasts ?? []).filter((f) => (f.subject_keys ?? []).includes(id)), [forecasts.data, id])
  const now = new Date()

  return (
    <div className="ph-screen" data-testid="phone-subject-page" data-subject={id}>
      <p className="ph-eyebrow">{noun}</p>
      <h1 className="ph-title ph-point" data-testid="phone-subject-name">
        {name}
      </h1>
      {s && s.geo.length ? <p className="ph-dim">{s.geo.join(', ')}</p> : null}
      <AskAboutThis label={name} pins={[{ kind: 'target', id, label: name }]} />

      <SectionHead title="Latest reads" count={reads.data ? reads.data.data.length : null} />
      {reads.isPending ? (
        <Quiet>Reading…</Quiet>
      ) : reads.isError ? (
        <Quiet>The reads about {name} could not be read.</Quiet>
      ) : (reads.data?.data ?? []).length === 0 ? (
        <Quiet testId="phone-subject-no-reads">No read about {name} is on record.</Quiet>
      ) : (
        <ul className="ph-list" data-testid="phone-subject-reads">
          {reads.data!.data.map((r) => (
            <HomeRow
              key={r.id}
              testId="phone-subject-read"
              kind="finding"
              headline={r.title || 'Untitled read'}
              word={null}
              subject={humanizeAnalystId(r.analyst_id ?? null, 'unknown producer')}
              at={r.produced_at}
              chip="read"
              onOpen={
                () => navigate({ tab: 'subjects', page: { kind: 'read', id: r.id }, record: null, peek: false }, 'push')
              }
            />
          ))}
        </ul>
      )}

      <SectionHead title={`Changed in the last ${win.label}`} count={changed.data ? mine.length : null} />
      {changed.data && mine.length === 0 ? <Quiet>Nothing about {name} changed in the last {win.label}.</Quiet> : null}
      {mine.length > 0 ? (
        <ul className="ph-list">
          {mine.map((r) => (
            <HomeRow key={r.key} testId="phone-subject-changed" headline={r.headline} word={r.word} subject={r.subject} at={r.at} chip={r.chip} onOpen={openChangedRow(r, 'subjects')} />
          ))}
        </ul>
      ) : null}

      <SectionHead title="Forecasts" count={forecasts.data ? fc.length : null} />
      {forecasts.isError ? (
        <Quiet>The forecasts could not be read.</Quiet>
      ) : forecasts.data && fc.length === 0 ? (
        <Quiet testId="phone-subject-no-forecasts">No forecast names {name}.</Quiet>
      ) : (
        <ul className="ph-list">
          {fc.map((f) => (
            <HomeRow
              key={f.id}
              testId="phone-subject-forecast"
              headline={f.question}
              word={f.status === 'open' ? (f.overdue ? 'overdue' : `due ${dueIn(f.due_at, now)}`) : f.status}
              subject=""
              at={null}
              chip="forecast"
              onOpen={null}
            />
          ))}
        </ul>
      )}

      <SectionHead title="On the map" />
      <button
        type="button"
        className="ph-row ph-row-nav"
        data-testid="phone-subject-map"
        onClick={() => navigate({ tab: 'subjects', page: { kind: 'map' }, record: null, peek: false }, 'push')}
      >
        <span className="ph-row-head">Show {name} on the map</span>
        <span className="ph-row-meta">the drawn map; the globe on request</span>
      </button>
    </div>
  )
}
