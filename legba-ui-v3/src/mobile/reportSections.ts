/**
 * reportSections — what the report screen counts and lists, for ANY read tier.
 *
 * WHY THIS EXISTS. `ReportView` used to take its four sections (Findings,
 * Targets named, Signals cited, Where) from `projectAssembly(row)` alone. An
 * Assessment row (`world_assessment`) carries `data.data.assessment`, not
 * `data.data.assembly`, so on every Assessment all four read 0 (since 09-06),
 * its `[[ref:N]]` markers rendered unresolved (`citations={[]}`), and the verify
 * line read a key no critique has ever carried.
 *
 * THE SOURCE CHOSEN FOR AN ASSESSMENT, and why. The assessment's own
 * `data.data.citations` (16 on 64f74201) are the record of what THIS voice
 * quoted: one per spine block it cites, each stamped with that block's
 * `ordinal` and `spine_block` {desk, target_id, finding_id}. They are
 * self-contained (no second fetch, work from the offline cache) and they are
 * exactly "the heads THIS record quotes". They carry no signals, though: the
 * signals live on the spine's blocks. So:
 *   1. When the spine row (`assessment.spine_id`) is available to the caller —
 *      the same `/findings` response already carries it — the sections are the
 *      spine's own projection, restricted to the blocks the assessment cites.
 *      Findings, Targets, Signals and Where then read exactly as they do on the
 *      spine, and a tap on a target filters all of them as before.
 *   2. Without the spine, Findings / Targets / Where come from the citations
 *      alone, and Signals is reported as UNAVAILABLE (`signals === null`), never
 *      as 0 — a zero there would be the original bug again.
 * Citations for the BODY renderer come from `extractCitations(row.data)`, the
 * desktop AssessmentBand's own call, so a marker resolves and drills the same
 * on the phone as on the desk.
 */

import { extractCitations, type Citation } from '@/lib/citationsModel'
import {
  projectAssembly,
  projectAssessment,
  projectAssessmentCitations,
  type AssemblyPayload,
  type AssessmentCitation,
  type ReadFindingRow,
} from '@/lib/assemblyModel'
import {
  reportEntities,
  reportFindings,
  reportSignals,
  reportWhere,
  type ReportEntity,
  type ReportFinding,
  type ReportSignal,
  type WherePoint,
} from './mobileModel'

export type SectionsSource =
  /** An assembly row (world / region / country / thematic): its own blocks. */
  | 'assembly'
  /** An Assessment, with its spine in hand: the spine's blocks it cites. */
  | 'assessment_spine'
  /** An Assessment alone: its own citations; signals unavailable. */
  | 'assessment_citations'
  /** A row with neither (legacy prose): no sections. */
  | 'none'

export interface ReportSections {
  source: SectionsSource
  assembly: AssemblyPayload | null
  findings: ReportFinding[]
  /** `null` = not derivable from what was loaded (NOT zero). */
  signals: ReportSignal[] | null
  entities: ReportEntity[]
  where: WherePoint[]
  /** Resolvable citations for the BODY renderer. */
  citations: Citation[]
}

/** A pretty name for a target id when the caller can supply one. */
export type TargetNamer = (targetId: string) => string | null

const EMPTY: ReportSections = {
  source: 'none',
  assembly: null,
  findings: [],
  signals: [],
  entities: [],
  where: [],
  citations: [],
}

/** Sections built from the assessment's own citations (no spine). */
function fromCitations(cites: AssessmentCitation[], nameOf: TargetNamer | undefined) {
  const findings: ReportFinding[] = []
  const byTarget = new Map<string, ReportEntity>()
  for (const c of [...cites].sort((x, y) => x.ordinal - y.ordinal)) {
    const sb = c.spine_block
    const targetId = sb?.target_id ?? null
    const name = targetId ? (nameOf?.(targetId) ?? targetId) : null
    findings.push({
      ordinal: c.ordinal,
      findingId: sb?.finding_id ?? `ordinal-${c.ordinal}`,
      desk: sb?.desk ?? c.title,
      targetId,
      targetName: name,
      question: c.title,
      severity: null,
      verifyScore: null,
      bluf: '',
      signalCount: 0,
    })
    if (!targetId) continue
    const found = byTarget.get(targetId)
    if (found) found.ordinals.push(c.ordinal)
    else byTarget.set(targetId, { targetId, name: name ?? targetId, ordinals: [c.ordinal], severity: null })
  }
  const entities = [...byTarget.values()].sort(
    (x, y) => y.ordinals.length - x.ordinals.length || x.name.localeCompare(y.name),
  )
  const where: WherePoint[] = entities.map((e) => ({
    targetId: e.targetId,
    name: e.name,
    severity: e.severity,
  }))
  return { findings, entities, where }
}

/**
 * Project the sections for `row`. `spineRow` is the row `assessment.spine_id`
 * names, when the caller has it (it is ignored unless the ids match).
 */
export function reportSections(
  row: ReadFindingRow,
  spineRow: ReadFindingRow | null = null,
  nameOf?: TargetNamer,
): ReportSections {
  const assembly = projectAssembly(row)
  if (assembly) {
    return {
      source: 'assembly',
      assembly,
      findings: reportFindings(assembly),
      signals: reportSignals(assembly),
      entities: reportEntities(assembly),
      where: reportWhere(assembly),
      // Each block renders with its own signal citations.
      citations: [],
    }
  }

  const bodyCitations = extractCitations(row.data as Record<string, unknown> | null)
  const assessment = projectAssessment(row)
  if (!assessment) return { ...EMPTY, citations: bodyCitations }

  const cites = projectAssessmentCitations(row)
  const spine = spineRow && spineRow.id === assessment.spine_id ? projectAssembly(spineRow) : null
  if (spine) {
    const cited = new Set(cites.map((c) => c.ordinal))
    const restricted: AssemblyPayload =
      cited.size > 0
        ? { ...spine, blocks: spine.blocks.filter((b) => cited.has(b.ordinal)) }
        : spine
    return {
      source: 'assessment_spine',
      assembly: null,
      findings: reportFindings(restricted),
      signals: reportSignals(restricted),
      entities: reportEntities(restricted),
      where: reportWhere(restricted),
      citations: bodyCitations,
    }
  }

  const own = fromCitations(cites, nameOf)
  return {
    source: 'assessment_citations',
    assembly: null,
    findings: own.findings,
    signals: null,
    entities: own.entities,
    where: own.where,
    citations: bodyCitations,
  }
}
