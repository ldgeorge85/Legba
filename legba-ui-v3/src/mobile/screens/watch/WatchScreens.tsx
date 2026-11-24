/**
 * Watch on the phone (M-3b; plan §2.3 row "Watch").
 *
 *   ROOT   a segmented control — Alerts · Silences · Edits — over U-9's three
 *          views mounted AS-IS through the registry (`v4/watch/register`:
 *          `AlertLog`, `Silences`, `SourceEdits`; single-column lists), and
 *          the window chips (the alert log reads the one clock). The views'
 *          filters, focus and Consult actions are theirs (`v4/watch/state`).
 *   ALERT  a row tapped (its focus) pushes the alert's page: what was raised,
 *          when, the decision (paged or logged, by which rule), its state, the
 *          subject (a link), who has acknowledged or snoozed it, and — for a
 *          contributor and above — Acknowledge and Snooze (S-2's routes,
 *          `AlertActions.tsx`); then "Ask about this".
 */
import { useEffect, useMemo, useState } from 'react'
import '@/v4/watch/register'
import { useAlertLog } from '@/v4/watch/AlertLog'
import { alertRows, DEFAULT_ALERT_FILTER, whenText } from '@/v4/watch/model'
import { useWatch } from '@/v4/watch/state'
import { CHAIN_KINDS, openRecord } from '@/lib/recordSheet'
import { selectionKindOf } from '@/state/selection'
import { navigate } from '@/mobile/shell/phoneAddress'
import { currentScreen, usePhoneShell, type WatchSegment } from '@/mobile/shell/phoneState'
import { SEGMENT_VIEW } from '@/mobile/shell/phoneFramePort'
import { PhoneViewHost } from '@/mobile/shell/PhoneViewHost'
import { AskAboutThis, Quiet, WindowChips } from '@/mobile/shell/ui'
import { AlertActions } from './AlertActions'

const SEGMENTS: readonly { id: WatchSegment; label: string }[] = [
  { id: 'alerts', label: 'Alerts' },
  { id: 'silences', label: 'Silences' },
  { id: 'edits', label: 'Edits' },
]

/** A row's focus (a tap, or Consult's `select_alert`) opens the alert's page. */
function useAlertFocusOpensPage(): void {
  useEffect(
    () =>
      useWatch.subscribe((s, prev) => {
        if (!s.focusedAlert || s.focusedAlert === prev.focusedAlert) return
        const cur = currentScreen()
        if (cur.tab !== 'watch' || cur.page) return
        navigate({ tab: 'watch', page: { kind: 'alert', id: s.focusedAlert }, record: null, peek: false }, 'push')
      }),
    [],
  )
}

export function WatchScreen() {
  const seg = usePhoneShell((s) => s.watchSegment)
  const filter = useWatch((s) => s.alertFilter)
  const [filters, setFilters] = useState(false)
  useAlertFocusOpensPage()
  const set = [
    filter.group !== DEFAULT_ALERT_FILTER.group ? 'kind' : null,
    filter.severity !== DEFAULT_ALERT_FILTER.severity ? 'severity' : null,
    filter.paged !== DEFAULT_ALERT_FILTER.paged ? 'paged' : null,
    filter.state !== DEFAULT_ALERT_FILTER.state ? 'state' : null,
  ].filter(Boolean)
  return (
    <div className="ph-screen ph-watch" data-testid="phone-watch" data-filters={filters ? '1' : '0'}>
      <div className="ph-seg" role="tablist" aria-label="Watch">
        {SEGMENTS.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={seg === s.id}
            className="ph-seg-btn"
            data-testid={`phone-watch-seg-${s.id}`}
            onClick={() => usePhoneShell.setState({ watchSegment: s.id })}
          >
            {s.label}
          </button>
        ))}
      </div>
      {seg === 'alerts' ? (
        <WindowChips
          extra={
            <button
              type="button"
              className="ph-chip ph-chip-map"
              aria-expanded={filters}
              data-testid="phone-watch-filters"
              onClick={() => setFilters((f) => !f)}
            >
              {filters ? 'Hide filters' : set.length ? `Filters · ${set.length}` : 'Filters'}
            </button>
          }
        />
      ) : null}
      <div className="ph-view" data-testid={`phone-watch-view-${seg}`}>
        <PhoneViewHost view={SEGMENT_VIEW[seg]} />
      </div>
    </div>
  )
}

export function AlertPage({ id }: { id: string }) {
  const { q, hours } = useAlertLog()
  const row = useMemo(() => alertRows(q.data).find((r) => r.id === id) ?? null, [q.data, id])
  // Leaving the page clears the focus, so tapping the same row opens it again.
  useEffect(() => () => {
    if (useWatch.getState().focusedAlert === id) useWatch.setState({ focusedAlert: null })
  }, [id])

  if (!row) {
    return (
      <div className="ph-screen" data-testid="phone-alert-page" data-state={q.isPending ? 'reading' : 'missing'}>
        {q.isPending ? (
          <Quiet>Reading the alert log…</Quiet>
        ) : q.isError ? (
          <Quiet>The alert log could not be read. Nothing is shown in its place.</Quiet>
        ) : (
          <Quiet testId="phone-alert-missing">
            This alert is not in the last {hours >= 48 ? `${Math.round(hours / 24)} days` : `${hours} hours`} of the log. A longer window on Changed or Watch reads further back.
          </Quiet>
        )}
      </div>
    )
  }

  const s = row.subject
  const canRecord = !!s.kind && CHAIN_KINDS.has(s.kind)
  return (
    <div className="ph-screen" data-testid="phone-alert-page" data-state="read">
      <p className={`ph-alert-sev ph-rule ph-sev-${row.severity}`}>
        <span className="ph-sev-word">{row.severityWord}</span> · {row.groupLabel}
      </p>
      <h1 className="ph-title" data-testid="phone-alert-what">
        {s.label} — {row.what}
      </h1>
      <dl className="ph-facts">
        <div>
          <dt>Decision</dt>
          <dd data-testid="phone-alert-decision">
            {row.decision.words} · {row.decision.rule}
            {row.decision.paged && row.decision.delivered != null ? ` · ${row.decision.delivered ? 'delivered' : 'not delivered'}` : ''}
          </dd>
        </div>
        <div>
          <dt>State</dt>
          <dd>{row.stateWords}</dd>
        </div>
        <div>
          <dt>Last raised</dt>
          <dd>
            {whenText(row.at)}
            {row.occurrences > 1 ? ` · raised ${row.occurrences} times` : ''}
          </dd>
        </div>
        {row.detail ? (
          <div>
            <dt>As raised</dt>
            <dd>{row.detail}</dd>
          </div>
        ) : null}
        <div>
          <dt>About</dt>
          <dd>
            {s.kind === 'target' ? (
              <button
                type="button"
                className="ph-link"
                data-testid="phone-alert-subject"
                onClick={() => navigate({ tab: 'subjects', page: { kind: 'subject', id: s.id }, record: null, peek: false }, 'push')}
              >
                {s.label}
              </button>
            ) : canRecord ? (
              <button type="button" className="ph-link" data-testid="phone-alert-subject" onClick={() => openRecord({ kind: s.kind!, id: s.id, label: s.label }, { origin: 'phone' })}>
                {s.label}
              </button>
            ) : (
              <span>
                {s.label} <span className="ph-dim">({s.noun})</span>
              </span>
            )}
          </dd>
        </div>
      </dl>
      <AlertActions id={row.id} />
      <AskAboutThis
        label={`${row.severityWord} alert · ${s.label}`}
        pins={s.kind ? [{ kind: selectionKindOf(s.kind), id: s.id, label: s.label }] : []}
      />
    </div>
  )
}
