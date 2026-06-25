/**
 * UI-6 (Tier G) — report-export data model.
 *
 * Pure, DOM-free builders that turn a selection of findings / situations
 * into a downloadable artifact:
 *   - a STIX 2.1 bundle (the `stix_bundle` output kind, mirrored client-side
 *     so the operator can export a curated selection on demand without
 *     waiting for a descriptor-driven export), and
 *   - a markdown intelligence report.
 *
 * The backend ships a `stix_bundle` output kind (Finding→report SDO,
 * Alert→indicator, TLP markings, `relationship` SDOs for derived_from
 * chains — see TRAVIS_ASM_BRIEF §1.6). This client builder produces the
 * same SDO shapes for an ad-hoc operator selection. Keeping it pure makes
 * the SDO mapping unit-testable; the panel only does fetch + download.
 */

export interface ReportItem {
  kind: 'finding' | 'situation'
  id: string
  title: string
  body: string
  severity: string | null
  target_id: string | null
  produced_at: string | null
  /** Provenance chain — becomes STIX `relationship` SDOs. */
  derived_from: string[]
}

export type Tlp = 'white' | 'green' | 'amber' | 'red'

/** STIX 2.1 marking-definition ids for the four TLP levels (canonical). */
const TLP_MARKING_IDS: Record<Tlp, string> = {
  white: 'marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9',
  green: 'marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da',
  amber: 'marking-definition--f88d31f6-486f-44da-b317-01333bde0b82',
  red: 'marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed',
}

/**
 * Deterministic STIX id from a kind + source id, so re-exporting the same
 * selection yields a stable bundle (idempotent). STIP ids must be
 * `<type>--<uuidv4-ish>`; we hash the source id into a uuid-shaped string.
 */
export function stixId(stixType: string, seed: string): string {
  // FNV-1a → hex, padded, formatted into the 8-4-4-4-12 uuid shape.
  let h = 0x811c9dc5
  const s = `${stixType}:${seed}`
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  // Expand the 32-bit hash into 32 hex chars by re-hashing slices.
  let hex = ''
  let cur = h >>> 0
  for (let i = 0; i < 8; i++) {
    hex += (cur >>> 0).toString(16).padStart(8, '0').slice(0, 4)
    cur = Math.imul(cur ^ (i + 1), 0x01000193) >>> 0
  }
  hex = hex.slice(0, 32).padEnd(32, '0')
  const uuid = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`
  return `${stixType}--${uuid}`
}

const SEVERITY_RANK: Record<string, number> = {
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
}

/**
 * Build a STIX 2.1 bundle object from the selection.
 *
 * Each finding/situation → a `report` SDO (with severity in `labels` and an
 * `object_refs` placeholder). Each `derived_from` link → a `relationship`
 * SDO of type `derived-from`. A single TLP marking-definition is attached
 * to every object via `object_marking_refs`.
 */
export function buildStixBundle(
  items: ReportItem[],
  opts: { tlp?: Tlp; created?: string } = {},
): Record<string, unknown> {
  const tlp = opts.tlp ?? 'amber'
  const created = opts.created ?? new Date().toISOString()
  const markingRef = TLP_MARKING_IDS[tlp]
  const objects: Array<Record<string, unknown>> = []

  for (const item of items) {
    const sdoId = stixId('report', `${item.kind}:${item.id}`)
    const labels: string[] = [item.kind]
    if (item.severity) labels.push(`severity:${item.severity}`)
    objects.push({
      type: 'report',
      spec_version: '2.1',
      id: sdoId,
      created,
      modified: created,
      name: item.title,
      description: item.body || item.title,
      published: item.produced_at ?? created,
      report_types: ['threat-report'],
      labels,
      object_refs: [],
      object_marking_refs: [markingRef],
      // Non-standard but namespaced extension carrying Legba provenance.
      x_legba_source_id: item.id,
      x_legba_target_id: item.target_id,
    })
    for (const parent of item.derived_from) {
      objects.push({
        type: 'relationship',
        spec_version: '2.1',
        id: stixId('relationship', `${item.id}<-${parent}`),
        created,
        modified: created,
        relationship_type: 'derived-from',
        source_ref: sdoId,
        target_ref: stixId('report', `parent:${parent}`),
        object_marking_refs: [markingRef],
        x_legba_parent_id: parent,
      })
    }
  }

  return {
    type: 'bundle',
    id: stixId('bundle', items.map((i) => `${i.kind}:${i.id}`).join('|')),
    objects,
  }
}

/** Pretty-printed STIX bundle JSON string (the downloadable artifact). */
export function stixBundleJson(items: ReportItem[], opts?: { tlp?: Tlp; created?: string }): string {
  return JSON.stringify(buildStixBundle(items, opts), null, 2)
}

/**
 * Build a markdown intelligence report from the selection — grouped by
 * severity (critical → low → unscored), with a provenance footnote per item.
 */
export function buildMarkdownReport(
  items: ReportItem[],
  opts: { title?: string; tlp?: Tlp; created?: string } = {},
): string {
  const title = opts.title ?? 'Legba Intelligence Report'
  const tlp = (opts.tlp ?? 'amber').toUpperCase()
  const created = opts.created ?? new Date().toISOString()
  const lines: string[] = []
  lines.push(`# ${title}`)
  lines.push('')
  lines.push(`> TLP:${tlp} — generated ${created} — ${items.length} item${items.length === 1 ? '' : 's'}`)
  lines.push('')

  const sorted = [...items].sort(
    (a, b) =>
      (SEVERITY_RANK[b.severity ?? ''] ?? 0) - (SEVERITY_RANK[a.severity ?? ''] ?? 0),
  )

  let lastSev: string | null = '__none__'
  for (const item of sorted) {
    const sev = item.severity ?? 'unscored'
    if (sev !== lastSev) {
      lines.push(`## ${sev.toUpperCase()}`)
      lines.push('')
      lastSev = sev
    }
    lines.push(`### ${item.title}`)
    lines.push('')
    const meta: string[] = [`kind: ${item.kind}`, `id: \`${item.id}\``]
    if (item.target_id) meta.push(`target: \`${item.target_id}\``)
    if (item.produced_at) meta.push(`at: ${item.produced_at}`)
    lines.push(`*${meta.join(' · ')}*`)
    lines.push('')
    if (item.body) {
      lines.push(item.body)
      lines.push('')
    }
    if (item.derived_from.length > 0) {
      lines.push(
        `**Provenance:** derived from ${item.derived_from.length} upstream row${
          item.derived_from.length === 1 ? '' : 's'
        } — ${item.derived_from.map((d) => `\`${d}\``).join(', ')}`,
      )
      lines.push('')
    }
    lines.push('---')
    lines.push('')
  }

  if (items.length === 0) {
    lines.push('_No items selected._')
    lines.push('')
  }
  return lines.join('\n')
}

export type ReportFormat = 'stix' | 'markdown'

export interface ReportArtifact {
  filename: string
  mime: string
  content: string
}

/** Build the downloadable artifact for the chosen format. */
export function buildArtifact(
  items: ReportItem[],
  format: ReportFormat,
  opts: { title?: string; tlp?: Tlp; created?: string } = {},
): ReportArtifact {
  const stamp = (opts.created ?? new Date().toISOString()).slice(0, 10)
  if (format === 'stix') {
    return {
      filename: `legba-report-${stamp}.stix.json`,
      mime: 'application/json',
      content: stixBundleJson(items, opts),
    }
  }
  return {
    filename: `legba-report-${stamp}.md`,
    mime: 'text/markdown',
    content: buildMarkdownReport(items, opts),
  }
}
