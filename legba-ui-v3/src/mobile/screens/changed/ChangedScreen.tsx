/**
 * Changed — the phone's landing (M-3a; plan §2.3 row 1, §2.4, §2.5).
 *
 *   Since you last looked   one line: what changed and what needs you after
 *                           the seen cursor (the person's when sign-in is
 *                           on, this phone's when it is off), and "Mark seen"
 *   Window                  three chips — the one clock — and the map
 *   Needs you               what PAGED, worst first; a row opens its alert
 *   Changed                 the Board's ranked list; a read opens its page,
 *                           a situation or an alert its Record, a band or a
 *                           due forecast its subject
 *
 * One read per window (`changedModel`), the subject roster for names, the
 * seen cursor; nothing at full row weight. The list never moves under the
 * thumb: the top bar's refresh asks again.
 */
import { useMemo } from 'react'
import { subjectName, useSubjectIndex } from '@/lib/deskNames'
import { useWorldState } from '@/v4/world/worldState'
import { whenText } from '@/v4/watch/model'
import { navigate } from '@/mobile/shell/phoneAddress'
import { seenWhereWords, useMarkSeen, useSeen } from '@/mobile/shell/seenCursor'
import { HomeRow, Quiet, SectionHead, WindowChips } from '@/mobile/shell/ui'
import { changedRows, pagedOnly, phoneWindowOf, sinceCounts, useChanged } from './changedModel'
import { openChangedRow } from './openRow'

const SEVERITY_WORD: Record<string, string> = { critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low', info: 'Info' }

function hhmm(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const d = new Date(t)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`
}

export default function ChangedScreen() {
  const spanMs = useWorldState((s) => s.spanMs)
  const win = phoneWindowOf(spanMs)
  const q = useChanged(win.id)
  const subjects = useSubjectIndex()
  const nameOf = useMemo(() => (id: string) => subjectName(id, subjects) ?? id.replace(/[._-]+/g, ' '), [subjects])
  const seen = useSeen()
  const markSeen = useMarkSeen()
  const seenAt = seen.data?.seen.changed ?? null

  const rows = useMemo(() => changedRows(q.data, nameOf), [q.data, nameOf])
  const paged = useMemo(() => pagedOnly(q.data), [q.data])
  const counts = useMemo(() => sinceCounts(q.data, rows, seenAt), [q.data, rows, seenAt])
  const checked = q.data?.server_now ?? null

  const cursorOlder = seenAt && Date.parse(seenAt) < Date.now() - win.ms

  return (
    <div className="ph-screen" data-testid="phone-changed" data-read={q.data ? '1' : '0'}>
      <section className="ph-since" data-testid="phone-since-line">
        <p className="ph-since-head">
          {seenAt ? <>Since you last looked · {hhmm(seenAt)}</> : <>Since you opened this {seen.data?.where === 'account' ? 'account' : 'phone'}</>}
        </p>
        {q.data ? (
          <p className="ph-since-counts">
            <span className="ph-fig">{counts.changed}</span> changed · <span className="ph-fig">{counts.needsYou}</span> need you
            {cursorOlder ? <span className="ph-dim"> · more than {win.label} ago: showing the last {win.label}</span> : null}
          </p>
        ) : null}
        <div className="ph-since-foot">
          <span className="ph-dim">{seenWhereWords(seen.data)}</span>
          {q.data && seen.data ? (
            <button
              type="button"
              className="ph-btn"
              data-testid="phone-mark-seen"
              onClick={() => void markSeen('changed', q.data!.server_now, seen.data!.where)}
            >
              Mark seen
            </button>
          ) : null}
        </div>
      </section>

      <WindowChips
        extra={
          <button
            type="button"
            className="ph-chip ph-chip-map"
            data-testid="phone-open-map"
            onClick={() => navigate({ tab: 'changed', page: { kind: 'map' }, record: null, peek: false }, 'push')}
          >
            Map
          </button>
        }
      />

      {q.isPending ? (
        <Quiet testId="phone-changed-reading">Reading what changed…</Quiet>
      ) : q.isError && !q.data ? (
        <Quiet testId="phone-changed-error">What changed could not be read. Nothing is shown in its place; refresh to ask again.</Quiet>
      ) : (
        <>
          <SectionHead title="Needs you" count={paged.length} testId="phone-needs-you-head" />
          {paged.length === 0 ? (
            <Quiet testId="phone-needs-you-none">Nothing paged in the last {win.label}.</Quiet>
          ) : (
            <ul className="ph-list" data-testid="phone-needs-you">
              {paged.map((e) => (
                <HomeRow
                  key={e.id}
                  testId="phone-needs-you-row"
                  kind="alert"
                  headline={e.summary}
                  word={SEVERITY_WORD[e.paged_severity ?? e.severity] ?? e.severity}
                  subject={e.subject_id ? nameOf(e.subject_id) : ''}
                  at={e.last_paged_at ?? e.opened_at}
                  chip="paged"
                  rule={e.paged_severity ?? e.severity}
                  tail={e.acked_at ? 'acknowledged' : e.snoozed_until ? `snoozed to ${whenText(e.snoozed_until)}` : 'open'}
                  onOpen={() => navigate({ tab: 'watch', page: { kind: 'alert', id: e.id }, record: null, peek: false }, 'push')}
                />
              ))}
            </ul>
          )}
          {q.data?.needs_you?.truncated ? <Quiet>Only the first {q.data.needs_you.items.length} are listed; Watch has the full log.</Quiet> : null}
          {q.data && !q.data.needs_you ? <Quiet testId="phone-needs-you-unrecorded">Not recorded on this server: it does not answer what needs you yet.</Quiet> : null}

          <SectionHead title="Changed" count={rows.length} testId="phone-changed-head" />
          {rows.length === 0 ? (
            <Quiet testId="phone-changed-none">
              Nothing has changed in the last {win.label}. Last checked {hhmm(checked)}.
            </Quiet>
          ) : (
            <ul className="ph-list" data-testid="phone-changed-list">
              {rows.map((r) => (
                <HomeRow
                  key={r.key}
                  testId="changed-row"
                  kind={r.kind}
                  headline={r.headline}
                  word={r.word ? (SEVERITY_WORD[r.word] ?? r.word) : null}
                  subject={r.subject}
                  at={r.at}
                  chip={r.chip}
                  rule={r.kind === 'alert' ? r.word : null}
                  onOpen={openChangedRow(r)}
                />
              ))}
            </ul>
          )}
          {checked ? <Quiet>Last checked {hhmm(checked)}.</Quiet> : null}
        </>
      )}
    </div>
  )
}
