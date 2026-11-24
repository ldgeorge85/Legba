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
 * ── WHAT THIS SCREEN DOES NOT SHOW ────────────────────────────────────────
 * M-0a (2026-11-14): the engine's own figures — the verify score, the gate,
 * the block count, the drop ledger and the floor sentence — left the phone's
 * reader header and cards, as they left the desktop reader pages on 11-13. A
 * number a reader cannot interpret is not a disclosure to them.
 */

import { memo, useMemo, useState } from 'react'
import CitedProse from '@/components/CitedProse'
import { blockCitations, leadSpan } from '@/v4/read/RecordBlock'
import type { ReadFindingRow } from '@/lib/assemblyModel'
import { age, longStamp } from '@/v4/read/readFormat'
import { emitRead } from '@/lib/readTelemetry'
import { filterToEntity, readerBlockName, readerReportTitle, tierLabel, tierOf } from '../mobileModel'
import { humanizeId } from '@/lib/deskNames'
import { PROFILE_UI } from '@/lib/profileUi'

/** The entities section's heading, in the profile's subject noun (U-1). */
const SUBJECTS_NAMED = `${PROFILE_UI.subject.plural.charAt(0).toUpperCase()}${PROFILE_UI.subject.plural.slice(1)} named`
import { reportSections, type TargetNamer } from '../reportSections'

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
  /** `null` = not derivable for this read; rendered as a dash, never as 0. */
  count: number | null
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
        <span className="m-count">{count ?? '—'}</span>
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
  /** M-0a — set when re-reading failed: "No connection. Showing what was loaded at …". */
  offlineNote: string | null
  /**
   * The row an Assessment was written from (`assessment.spine_id`), when the
   * caller has it. It supplies the signals the Assessment's own citations lack.
   */
  spineRow?: ReadFindingRow | null
  /** Names a target id for the citation-only sections (the subject roster). */
  nameOf?: TargetNamer
}

function ReportViewImpl({
  row,
  entityId,
  onSelectEntity,
  onBack,
  offlineNote,
  spineRow = null,
  nameOf,
}: ReportViewProps) {
  // One projection for every tier: an assembly row's own blocks, or an
  // Assessment's cited spine blocks / own citations (see reportSections.ts).
  const sections = useMemo(
    () => reportSections(row, spineRow, nameOf),
    [row, spineRow, nameOf],
  )
  const { assembly, entities, where } = sections

  // `null` signals = unavailable for this read (not zero); the filter treats it as empty.
  const signalsKnown = sections.signals !== null
  const { findings, signals } = useMemo(
    () => filterToEntity(entityId, sections.findings, sections.signals ?? [], entities),
    [entityId, sections.findings, sections.signals, entities],
  )

  const tier = tierOf(row)
  const selectedEntity = entities.find((e) => e.targetId === entityId) ?? null

  return (
    <div className="m-screen" data-testid="report-view" data-report-id={row.id}>
      <header className="m-masthead">
        <button type="button" className="m-back" onClick={onBack} data-testid="report-back">
          ‹ Reports
        </button>
        <h1 className="m-title" data-testid="report-title">
          {row.title ? readerReportTitle(row.title, row.target_id ?? null, nameOf) : 'Untitled read'}
        </h1>
        <p className="m-sub">
          {tier ? tierLabel(tier) : 'Read'} · {longStamp(row.produced_at) ?? row.produced_at}
        </p>
        {offlineNote ? (
          <p className="m-cached" data-testid="offline-banner" role="status">
            {offlineNote}
          </p>
        ) : null}
        {/* M-0a — the engine line ("verify 0.97 · gate failed · 4 blocks",
            "35 shown · 20 below the line · 0 below floor", the floor
            sentence) left the phone's reader header, as it left the desktop
            reader pages on 11-13: unexplained numbers and pipeline words
            are not a reader's. */}
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
                    {readerBlockName(b.target_name, b.target_id ?? b.desk, nameOf)}
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
                  {b.severity || b.evidence_age_h != null ? (
                    <p className="m-block-meta">
                      {[
                        b.severity,
                        b.evidence_age_h != null ? `evidence ${age(b.evidence_age_h)} old` : null,
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                    </p>
                  ) : null}
                </article>
              )
            })}
        </div>
      ) : row.body ? (
        <div className="m-record">
          <CitedProse text={row.body} citations={sections.citations} variant="block" />
        </div>
      ) : (
        <p className="m-empty">This record carries no readable body.</p>
      )}

      {/* ── the sections the selection governs ─────────────────────────── */}
      <Section id="findings" title="Findings" count={findings.length}>
        <ul className="m-list">
          {findings.map((f) => (
            <li key={f.ordinal} className="m-item" data-testid="finding-item">
              <span className="m-item-title">{readerBlockName(f.targetName, f.desk, nameOf)}</span>
              <span className="m-item-meta">
                {humanizeId(f.desk)}
                {f.severity ? ` · ${f.severity}` : ''}
                {signalsKnown ? ` · ${f.signalCount} signals` : ''}
              </span>
              {f.bluf ? <span className="m-item-body">{f.bluf}</span> : null}
            </li>
          ))}
          {findings.length === 0 ? <li className="m-quiet">No findings under this filter.</li> : null}
        </ul>
      </Section>

      <Section id="entities" title={SUBJECTS_NAMED} count={entities.length}>
        <p className="m-note">
          The subjects this record speaks about. A full list of the people and organisations it
          names is not available yet.
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

      <Section id="signals" title="Signals cited" count={signalsKnown ? signals.length : null}>
        {signalsKnown ? null : (
          <p className="m-note" data-testid="signals-unavailable">
            This read quotes {findings.length} findings of the underlying record. That record is not
            in the loaded window, so its signals are not listed here.
          </p>
        )}
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
          {signalsKnown && signals.length === 0 ? (
            <li className="m-quiet">No signals under this filter.</li>
          ) : null}
        </ul>
      </Section>

      <Section id="where" title="Where" count={where.length}>
        <p className="m-note">
          The record&rsquo;s geography as the subjects it covers. There is no map on the phone
          yet.
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
