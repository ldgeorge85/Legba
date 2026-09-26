/**
 * ReportView — the read itself, and the three sections it governs.
 *
 * Selecting a report on the previous screen set the context for everything
 * here: the Findings are the heads THIS record quotes, the Signals are the
 * sources THOSE heads cited, and the Entities are the targets it speaks about.
 * Tapping an entity narrows all three at once. That is the Crossroads rule on
 * a 390 px screen — one selection, everything follows.
 *
 * ── WHAT IS REUSED, AND WHY IT MATTERS ────────────────────────────────────
 * The prose is rendered by `CitedProse`, the workstation's own renderer, with
 * citations built by `RecordBlock`'s exported `blockCitations`. That is not
 * mere code thrift: it means a `[N]` marker on the phone resolves against the
 * same signal list, renders the same chip, and emits the same `citation_drill`
 * telemetry as it does on the desk. A second renderer would have been a second
 * definition of what a citation IS, and the two would have drifted.
 *
 * ── WHAT THIS SCREEN DOES NOT DO ──────────────────────────────────────────
 * It does not recompute a disclosure. The drop ledger line reads `drops.counts`
 * verbatim, exactly as the Morning Read does, because a reader that derives its
 * own aperture number can contradict the record it is disclosing.
 */

import { memo, useMemo, useState } from 'react'
import CitedProse from '@/components/CitedProse'
import { blockCitations, leadSpan } from '@/v4/read/RecordBlock'
import {
  projectArms,
  projectAssembly,
  projectAssessment,
  recordBadge,
  type ReadFindingRow,
} from '@/lib/assemblyModel'
import { age, longStamp, score } from '@/v4/read/readFormat'
import { emitRead } from '@/lib/readTelemetry'
import {
  filterToEntity,
  reportEntities,
  reportFindings,
  reportSignals,
  reportWhere,
  TIER_LABEL,
  tierOf,
} from '../mobileModel'

/** A collapsible section. Closed sections cost one line; open ones own the screen. */
function Section({
  id,
  title,
  count,
  children,
  defaultOpen = false,
}: {
  id: string
  title: string
  count: number
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="m-section" data-testid={`section-${id}`}>
      <button
        type="button"
        className="m-section-head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid={`section-toggle-${id}`}
      >
        <span>{title}</span>
        <span className="m-count">{count}</span>
        <span className="m-caret">{open ? '−' : '+'}</span>
      </button>
      {open ? <div className="m-section-body">{children}</div> : null}
    </section>
  )
}

export interface ReportViewProps {
  row: ReadFindingRow
  entityId: string | null
  onSelectEntity: (id: string | null) => void
  onBack: () => void
  cachedAt: string | null
}

function ReportViewImpl({ row, entityId, onSelectEntity, onBack, cachedAt }: ReportViewProps) {
  const assembly = useMemo(() => projectAssembly(row), [row])
  const assessment = useMemo(() => projectAssessment(row), [row])
  const arms = useMemo(() => projectArms(row), [row])

  const allFindings = useMemo(() => reportFindings(assembly), [assembly])
  const allSignals = useMemo(() => reportSignals(assembly), [assembly])
  const entities = useMemo(() => reportEntities(assembly), [assembly])
  const where = useMemo(() => reportWhere(assembly), [assembly])

  const { findings, signals } = useMemo(
    () => filterToEntity(entityId, allFindings, allSignals, entities),
    [entityId, allFindings, allSignals, entities],
  )

  const badge = assembly ? recordBadge(assembly, arms) : null
  const counts = assembly?.drops?.counts ?? null
  const tier = tierOf(row)
  const selectedEntity = entities.find((e) => e.targetId === entityId) ?? null

  return (
    <div className="m-screen" data-testid="report-view" data-report-id={row.id}>
      <header className="m-masthead">
        <button type="button" className="m-back" onClick={onBack} data-testid="report-back">
          ‹ Reports
        </button>
        <h1 className="m-title">{row.title || 'Untitled read'}</h1>
        <p className="m-sub">
          {tier ? TIER_LABEL[tier] : 'Read'} · {longStamp(row.produced_at) ?? row.produced_at}
        </p>
        {cachedAt ? (
          <p className="m-cached" data-testid="cached-banner">
            offline — {cachedAt}
          </p>
        ) : null}
        <p className="m-verify" data-testid="verify-line">
          verify {score(badge?.quoteFidelity ?? null)}
          {badge ? ` · gate ${badge.gate}` : ''}
          {assembly ? ` · ${assembly.blocks.length} blocks` : ''}
        </p>
        {counts ? (
          <p className="m-aperture" data-testid="drop-ledger">
            {counts.shown} shown · {counts.not_selected} below the line · {counts.below_floor}{' '}
            below floor
            {counts.invisible_heads > 0 ? ` · ${counts.invisible_heads} outside the aperture` : ''}
          </p>
        ) : null}
      </header>

      {selectedEntity ? (
        <div className="m-filterbar" data-testid="entity-filter-bar">
          <span>
            Filtered to <strong>{selectedEntity.name}</strong>
          </span>
          <button type="button" className="m-btn" onClick={() => onSelectEntity(null)}>
            Clear
          </button>
        </div>
      ) : null}

      {/* ── the record ────────────────────────────────────────────────── */}
      {assembly ? (
        <div className="m-record">
          {assembly.blocks
            .filter((b) => !entityId || b.target_id === entityId)
            .map((b) => {
              const head = leadSpan(b)
              const rest = b.spans.filter((s) => s !== head)
              const citations = blockCitations(b)
              return (
                <article key={b.ordinal} className="m-block" data-testid="record-block">
                  <h2 className="m-block-head">
                    <span className="m-ordinal">{b.ordinal}</span>
                    {b.target_name ?? b.desk}
                  </h2>
                  <p className="m-block-q">{b.question}</p>
                  {head ? (
                    <CitedProse text={head.text} citations={citations} variant="block" />
                  ) : null}
                  {rest.length > 0 ? (
                    <CitedProse
                      text={rest.map((s) => s.text).join('\n\n')}
                      citations={citations}
                      variant="block"
                    />
                  ) : null}
                  <p className="m-block-meta">
                    {b.severity ? `${b.severity} · ` : ''}
                    verify {score(b.verify?.overall_score ?? null)}
                    {b.evidence_age_h != null ? ` · ${age(b.evidence_age_h)}` : ''}
                  </p>
                </article>
              )
            })}
        </div>
      ) : assessment && row.body ? (
        <div className="m-record">
          <CitedProse text={row.body} citations={[]} variant="block" />
        </div>
      ) : row.body ? (
        <div className="m-record">
          <CitedProse text={row.body} citations={[]} variant="block" />
        </div>
      ) : (
        <p className="m-empty">This record carries no readable body.</p>
      )}

      {/* ── the sections the selection governs ─────────────────────────── */}
      <Section id="findings" title="Findings" count={findings.length}>
        <ul className="m-list">
          {findings.map((f) => (
            <li key={f.findingId} className="m-item" data-testid="finding-item">
              <span className="m-item-title">{f.targetName ?? f.desk}</span>
              <span className="m-item-meta">
                {f.desk} · verify {score(f.verifyScore)}
                {f.severity ? ` · ${f.severity}` : ''} · {f.signalCount} signals
              </span>
              {f.bluf ? <span className="m-item-body">{f.bluf}</span> : null}
            </li>
          ))}
          {findings.length === 0 ? <li className="m-quiet">No heads under this filter.</li> : null}
        </ul>
      </Section>

      <Section id="entities" title="Targets named" count={entities.length}>
        <p className="m-note">
          The targets this record speaks about. A substrate entity list per read is not
          servable today — see the build note.
        </p>
        <ul className="m-chips">
          {entities.map((e) => (
            <li key={e.targetId}>
              <button
                type="button"
                className={`m-chip m-chip-tap ${entityId === e.targetId ? 'm-chip-on' : ''}`}
                data-testid="entity-chip"
                data-entity-id={e.targetId}
                aria-pressed={entityId === e.targetId}
                onClick={() => {
                  const next = entityId === e.targetId ? null : e.targetId
                  onSelectEntity(next)
                  if (next) {
                    emitRead('finding_open', { subjectKind: 'target', subjectId: next })
                  }
                }}
              >
                {e.name} <span className="m-count">{e.ordinals.length}</span>
              </button>
            </li>
          ))}
        </ul>
      </Section>

      <Section id="signals" title="Signals cited" count={signals.length}>
        <ul className="m-list">
          {signals.map((s) => (
            <li key={s.signalId ?? s.marker} className="m-item" data-testid="signal-item">
              <span className="m-item-title">
                {s.url ? (
                  <a href={s.url} target="_blank" rel="noreferrer noopener" className="m-link">
                    {s.title || s.url}
                  </a>
                ) : (
                  s.title || 'untitled signal'
                )}
              </span>
              <span className="m-item-meta">
                {s.marker}
                {s.sourceId ? ` · ${s.sourceId}` : ''}
                {s.ageH != null ? ` · ${age(s.ageH)}` : ''}
                {s.ordinals.length > 1 ? ` · cited by ${s.ordinals.length} blocks` : ''}
              </span>
            </li>
          ))}
          {signals.length === 0 ? (
            <li className="m-quiet">No signals under this filter.</li>
          ) : null}
        </ul>
      </Section>

      <Section id="where" title="Where" count={where.length}>
        <p className="m-note">
          The record&rsquo;s geography as the targets it covers. No basemap on this surface —
          see the build note.
        </p>
        <ul className="m-chips">
          {where.map((w) => (
            <li key={w.targetId}>
              <span className={`m-chip m-sev-${w.severity ?? 'none'}`}>{w.name}</span>
            </li>
          ))}
        </ul>
      </Section>
    </div>
  )
}

export default memo(ReportViewImpl)
