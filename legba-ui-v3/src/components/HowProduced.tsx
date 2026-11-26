/**
 * HowProduced — the instrument readout behind a finding's trust words, in a
 * disclosure that is collapsed by default and open to every role.
 *
 * A reader first sees at most two plain words (`TrustWords`, from
 * `lib/trustWords.ts`). Everything the engine measured about the finding
 * lives one click further, here: how likely, how well supported, the check of
 * each claim against its sources, the same check over the producer's earlier
 * reads, a person's grading and an outside reference. Each figure is a row
 * labelled in plain words (`lib/readerVocabulary.ts`) with what it is taken
 * over, so two figures that measure different things never sit side by side
 * as bare numbers that seem to contradict each other.
 *
 * `children` is the admin's engine readout (the badges with their engine
 * terms), shown under the plain rows only to an admin.
 */
import { useEffect, useState, type ReactNode } from 'react'
import { useBelowAdminPassive as useBelowAdmin } from '@/auth/roles'
import { cn } from '@/lib/cn'
import { READER_WORDS, countOf, percent, producerLabel } from '@/lib/readerVocabulary'
import type { Verdict } from '@/lib/verdictModel'
import { fetchEvalScores, findUnitScore, type UnitEvalScore } from '@/lib/unitEvalModel'
import {
  fetchUnitCorrectness,
  findUnitCorrectness,
  type UnitCorrectness,
  type UnitCorrectnessPage,
} from '@/lib/unitCorrectnessModel'
import type { TrustWord } from '@/lib/trustWords'

export interface ProducedRow {
  label: string
  value: string
  testId?: string
}

/** The trust words, one line, plain. */
export function TrustWords({
  words,
  className,
  testId = 'trust-words',
}: {
  words: readonly TrustWord[]
  className?: string
  testId?: string
}) {
  if (words.length === 0) return null
  return (
    <span className={cn('inline-flex flex-wrap items-baseline gap-x-3 text-label text-ink-2', className)} data-testid={testId}>
      {words.map((w) => (
        <span key={w.text} title={w.title}>
          {w.text}
        </span>
      ))}
    </span>
  )
}

/** The confidence axis as a reader word. */
function confidenceWords(v: Verdict): string {
  if (v.confidence !== 'unassessed') return v.confidence
  if (v.structuralVerified) return 'recomputed from its inputs and matched'
  if (v.structural) return 'not checked: computed, not written'
  return 'not assessed'
}

/** The rows a verdict gives: how likely, how well supported, the per-claim check. */
export function verdictRows(
  v: Verdict,
  {
    citations,
    sources,
    flagged,
    checkKnown = true,
  }: { citations: number; sources: number; flagged: number | null; checkKnown?: boolean },
): ProducedRow[] {
  const rows: ProducedRow[] = []
  rows.push({
    label: READER_WORDS.likelihood,
    value:
      v.likelihood === 'unstated'
        ? 'not stated'
        : v.probability != null
          ? `${v.likelihood} (${percent(v.probability)})`
          : v.likelihood,
    testId: 'produced-likelihood',
  })
  rows.push({ label: READER_WORDS.confidence, value: confidenceWords(v), testId: 'produced-confidence' })
  rows.push({
    label: READER_WORDS.citations,
    value:
      citations === 0 ? 'none' : `${countOf(citations, 'citation')} from ${countOf(Math.max(sources, 1), 'source')}`,
    testId: 'produced-citations',
  })
  let held: string
  if (!checkKnown) held = 'not recorded with this read'
  else if (v.unverified) held = 'the check could not run'
  else if (flagged != null && flagged > 0) held = `${countOf(flagged, 'claim')} not supported by the sources cited`
  else if (v.faithfulness != null) held = `${percent(v.faithfulness)} of what it says is supported by its ${countOf(citations, 'citation')}`
  else held = 'not checked yet'
  rows.push({ label: READER_WORDS.heldAgainstSources, value: held, testId: 'produced-held' })
  return rows
}

/** The producer's record, in words: the check over its earlier reads, and a person's grading. */
export function producerRows(score: UnitEvalScore | null, analystId: string | null): ProducedRow[] {
  if (!score) return []
  const who = producerLabel(analystId) ?? 'this producer'
  const rows: ProducedRow[] = []
  const share = percent(score.faithfulness)
  rows.push({
    label: READER_WORDS.producerRecord,
    value: share
      ? `${who}: ${share} supported by their sources, over ${countOf(score.n_findings, 'read')}`
      : `${who}: not measured yet, over ${countOf(score.n_findings, 'read')}`,
    testId: 'produced-producer',
  })
  const graded = typeof score.n_operator_scored === 'number' ? score.n_operator_scored : score.n_operator_labels
  const right = percent(score.correctness_operator)
  rows.push({
    label: READER_WORDS.gradedByPerson,
    value:
      typeof graded === 'number' && graded > 0 && right
        ? `${right} judged right, over ${countOf(graded, 'read')}${score.operator_sufficient === false ? ' (too few to call a rate)' : ''}`
        : 'none graded yet',
    testId: 'produced-person',
  })
  return rows
}

/** An outside reference's verdict on the producer's claims, in words. */
export function referenceRow(row: UnitCorrectness | null, page: UnitCorrectnessPage | null): ProducedRow | null {
  if (!row) {
    const state = page?.reference?.state
    if (!state || state === 'current') return null
    return {
      label: READER_WORDS.outsideReference,
      value: state === 'stale' ? 'the reference is out of date, so no figure is given' : 'no outside reference yet',
      testId: 'produced-reference',
    }
  }
  const notCovered = Math.max(0, row.n_claims - row.n_decided)
  const parts = [`${row.n_contains} of ${countOf(row.n_decided, 'claim')} it could decide confirmed`]
  if (row.n_contradicts > 0) parts.push(`${row.n_contradicts} contradicted`)
  parts.push(`${notCovered} of ${row.n_claims} not covered by it`)
  return { label: READER_WORDS.outsideReference, value: parts.join('; '), testId: 'produced-reference' }
}

/** Reads the producer's record and the outside reference for the readout. */
function useProducerRecord(analystId: string | null, targetId: string | null) {
  const [score, setScore] = useState<UnitEvalScore | null>(null)
  const [page, setPage] = useState<UnitCorrectnessPage | null>(null)
  useEffect(() => {
    let cancelled = false
    setScore(null)
    if (analystId) {
      fetchEvalScores()
        .then((s) => !cancelled && setScore(findUnitScore(s, analystId)))
        .catch(() => !cancelled && setScore(null))
    }
    return () => {
      cancelled = true
    }
  }, [analystId])
  useEffect(() => {
    let cancelled = false
    setPage(null)
    if (analystId && targetId) {
      fetchUnitCorrectness(targetId)
        .then((p) => !cancelled && setPage(p && typeof p === 'object' ? p : null))
        .catch(() => !cancelled && setPage(null))
    }
    return () => {
      cancelled = true
    }
  }, [analystId, targetId])
  return { score, page, row: findUnitCorrectness(page, analystId) }
}

export function HowProduced({
  rows,
  analystId = null,
  targetId = null,
  children,
  className,
  testId = 'how-produced',
}: {
  rows: readonly ProducedRow[]
  /** The producer whose record and outside reference join the rows (none: left out). */
  analystId?: string | null
  targetId?: string | null
  /** The admin's engine readout, shown under the plain rows to an admin only. */
  children?: ReactNode
  className?: string
  testId?: string
}) {
  const belowAdmin = useBelowAdmin()
  const record = useProducerRecord(analystId, targetId)
  const reference = referenceRow(record.row, record.page)
  const all = [...rows, ...producerRows(record.score, analystId), ...(reference ? [reference] : [])]
  return (
    <details className={cn('group text-label', className)} data-testid={testId}>
      <summary className="cursor-pointer select-none text-ink-3 hover:text-ink-1">{READER_WORDS.howProduced}</summary>
      <dl className="mt-1.5 space-y-1 border-l border-line pl-3">
        {all.map((r) => (
          <div key={r.label} className="flex flex-wrap gap-x-2" data-testid={r.testId}>
            <dt className="text-ink-3">{r.label}</dt>
            <dd className="text-ink-1">{r.value}</dd>
          </div>
        ))}
      </dl>
      {!belowAdmin && children ? (
        <div className="mt-2 flex flex-wrap items-center gap-2 border-l border-line pl-3" data-testid={`${testId}-engine`}>
          {children}
        </div>
      ) : null}
    </details>
  )
}
