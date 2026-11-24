/**
 * recordChain — the Record sheet's model (U-10): one statement read top to
 * bottom as a chain, each step in plain words, each gap stated where it
 * occurs. Pure and DOM-free; `components/record/RecordChain.tsx` renders it.
 *
 *   statement as published  ← the record's prose: the cited sentence, or its lead
 *   → the row(s)            ← the record itself (who or what wrote it, when)
 *   → the evidence          ← the citations / lineage parents (source and class)
 *   → the receipt           ← `/v3/record/receipts` (what was fetched, when,
 *                             from where, hash, protocol, stored version)
 *   → later versions        ← the page address's versions and the drift class
 *   → the integrity line    ← `/v3/record/integrity` (receipt re-hash,
 *                             checkpoint, daily anchor)
 *
 * The routes behind each step are named in {@link RECORD_ROUTES}. Every read
 * is bounded: the lineage walk at depth 1, at most {@link MAX_EVIDENCE}
 * receipts in one call, the stored text only on demand with a cap.
 */
import {
  extractCitations,
  normalizeCitationMarkers,
  type Citation,
} from '@/lib/citationsModel'
import { markerOrdinal } from '@/lib/claimVerdicts'
import { stripMarkdown, unwrapEnvelope } from '@/lib/proseText'
import { humanizeAnalystId } from '@/lib/analystNames'

// ---------------------------------------------------------------------------
// The routes and their bounds.
// ---------------------------------------------------------------------------

/** The lineage route's root kinds (`registry/lineage_api.py::_TABLES_BY_KIND`). */
export const LINEAGE_KINDS: ReadonlySet<string> = new Set([
  'finding',
  'signal',
  'event',
  'situation',
  'hypothesis',
  'fact',
  'alert',
  'critique',
  'meta_finding',
])

/** Evidence rows the sheet shows (and asks receipts for) at once. */
export const MAX_EVIDENCE = 8

/** The stored text one "show what was received" reads, in characters. */
export const TEXT_CAP = 20_000

export const RECORD_ROUTES = {
  lineage: (kind: string, id: string) =>
    `/lineage/${encodeURIComponent(kind)}/${encodeURIComponent(id)}?direction=upstream&depth=1`,
  receipts: (kind: 'signal' | 'observation', ids: readonly string[]) =>
    `/v3/record/receipts?kind=${kind}&ids=${ids.map(encodeURIComponent).join(',')}`,
  integrity: (runId: string) => `/v3/record/integrity?run_id=${encodeURIComponent(runId)}`,
  text: (versionId: number) => `/v3/record/version/${versionId}/text?max_chars=${TEXT_CAP}`,
  /** B-1: the item AS RECEIVED, cut from the stored response's bytes. */
  item: (signalId: string) => `/v3/record/signal/${encodeURIComponent(signalId)}/item?max_chars=${TEXT_CAP}`,
} as const

// ---------------------------------------------------------------------------
// Wire shapes (only the fields the sheet reads).
// ---------------------------------------------------------------------------

export interface LineageReceipt {
  run_id: string
  receipt_hash: string
  chain_consistent: boolean
  signer_did?: string | null
}

export interface LineageNodeWire {
  id: string
  row_kind: string
  title: string | null
  produced_at: string
  target_id?: string | null
  analyst_id?: string | null
  depth: number
  body?: Record<string, unknown> | null
  canonical_url?: string | null
  archived?: boolean
  archive_sha256?: string | null
  receipt?: LineageReceipt | null
}

export interface LineageWire {
  root: LineageNodeWire
  nodes: LineageNodeWire[]
  edges: { parent: string; child: string }[]
  truncated_at_depth: boolean
}

export interface VersionWire {
  id: number
  receipt_kind: string
  address: string | null
  url: string
  protocol: string
  role: string
  status: number
  content_type: string | null
  size_bytes: number | null
  sha256: string
  first_seen_at: string
  last_seen_at: string
  fetch_count: number | null
  source_id: string | null
  held: 'hot' | 'cold' | 'pruned' | 'not_stored'
  text_chars: number | null
  has_text: boolean
}

export interface ReceiptRecordWire {
  kind: string
  id: string
  found: boolean
  fetched_at: string | null
  canonical_url: string | null
  page_address: string | null
  license_class: string | null
  source: { id: string; name: string | null; kind: string | null; source_class: string | null } | null
  links: { role: string; version_id: number; sha256: string; linked_at: string }[]
  no_receipt_reason: 'before_receipts_start' | 'source_kind_without_receipts' | 'not_linked' | null
  /** B-1 (`byte_path.evidence_states`): the one evidence label; signals only. */
  evidence?: EvidenceStateWire | null
}

export type EvidenceLabel = 'received_bytes' | 'archived_later' | 'no_bytes_kept'

export interface EvidenceStateWire {
  label: EvidenceLabel
  label_text: string
  version_id: number | null
  link_role: string | null
  has_text: boolean
  hash_only: number[]
}

export interface ItemTextWire {
  available: boolean
  signal_id: string
  found: boolean
  evidence: EvidenceStateWire | null
  version_id: number | null
  basis: string | null
  exact: boolean | null
  shown: boolean
  withheld_reason:
    | 'no_bytes_kept'
    | 'license_forbids'
    | 'claim_text_admin_only'
    | 'reader_display_policy'
    | 'no_item_text'
    | 'item_reader_unavailable'
    | null
  chars_total: number | null
  chars_shown: number
  truncated: boolean
  text_sha256: string | null
  item_sha256: string | null
  version_sha256: string | null
  full_text: string | null
}

export interface DriftChangeWire {
  address: string
  earlier_version_id: number
  later_version_id: number | null
  drift_class: string
  later_fetched_at: string
  later_gone: boolean | null
}

export interface ReceiptsWire {
  available: boolean
  receipts_start: string | null
  records: ReceiptRecordWire[]
  versions: Record<string, VersionWire>
  pages: Record<string, { versions: VersionWire[]; more: boolean }>
  changes: DriftChangeWire[]
}

export type IntegrityState = 'verified' | 'covered' | 'not_covered' | 'failed'

export interface IntegrityWire {
  available: boolean
  run_id: string
  found: boolean
  unit?: string | null
  run_ended_at?: string | null
  receipt: { state: IntegrityState; receipt_hash?: string }
  checkpoint: { state: IntegrityState; signer_did?: string; checkpointed_at?: string; exact?: boolean; trace_count?: number }
  anchor: { state: IntegrityState; day?: string; calendars?: number; pending?: boolean }
}

export interface VersionTextWire {
  available: boolean
  version_id: number
  found: boolean
  shown: boolean
  withheld_reason: 'reader_display_policy' | 'license_forbids' | 'no_text' | null
  chars_total: number | null
  chars_shown: number
  truncated: boolean
  text_hash_ok: boolean | null
  full_text: string | null
  size_bytes?: number | null
  sha256?: string | null
}

// ---------------------------------------------------------------------------
// Small text helpers.
// ---------------------------------------------------------------------------

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null
}

/** A date-time as the sheet prints it: `13 Nov 2026, 14:05 UTC`. */
export function when(iso: string | null | undefined): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (!Number.isFinite(d.getTime())) return null
  const mon = d.toLocaleString('en-GB', { month: 'short', timeZone: 'UTC' })
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `${d.getUTCDate()} ${mon} ${d.getUTCFullYear()}, ${hh}:${mm} UTC`
}

/** A host name for a URL, `www.` dropped; null when it is not a URL. */
export function hostOf(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return null
  }
}

/** Bytes in words: `412 bytes`, `18.4 KB`, `2.1 MB`. */
export function bytesWords(n: number | null | undefined): string | null {
  if (typeof n !== 'number' || !Number.isFinite(n)) return null
  if (n < 1024) return `${n} bytes`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

/** The first 12 characters of a hash, for the eye; the whole one rides `title`. */
export function shortHash(h: string | null | undefined): string | null {
  return h ? h.slice(0, 12) : null
}

// ---------------------------------------------------------------------------
// Step 1 — the statement as published.
// ---------------------------------------------------------------------------

export interface Statement {
  /** The sentence (or title) the sheet leads with. */
  text: string | null
  /** Where it came from. */
  basis: 'cited_sentence' | 'lead' | 'title' | 'none'
  /** The citation that sentence carries, when one was asked for and found. */
  citation: Citation | null
  /** A gap, in words, when the asked-for sentence could not be found. */
  gap: string | null
}

/** The prose a row carries, unwrapped (a finding's `body`, a situation's summary, a signal's title). */
export function proseOf(body: Record<string, unknown> | null | undefined): string | null {
  if (!body) return null
  for (const key of ['body', 'summary', 'assessment', 'thesis', 'description', 'text']) {
    const v = str(body[key])
    if (v) return unwrapEnvelope(v)
  }
  return null
}

function sentences(paragraph: string): string[] {
  return paragraph
    .split(/(?<=[.!?]["'”’)\]]*)\s+(?=[A-Z0-9"'“‘(])/u)
    .map((s) => s.trim())
    .filter(Boolean)
}

const MARKER_ANY_RE = /\[\[ref:\d+\]\]|\[ref:\d+\]|\[\d+\]/g

/** A sentence with its citation markers and its markdown removed. */
export function cleanSentence(s: string): string {
  return stripMarkdown(s)
    .replace(MARKER_ANY_RE, '')
    .replace(/^\s*(BLUF|Bottom line|Summary)\s*:\s*/i, '')
    .replace(/\s+([.,;:!?])/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * The sentence of `prose` that carries citation marker `cite` (`[n]` or
 * `[[ref:n]]`), cleaned; null when no sentence carries it.
 */
export function citedSentence(prose: string, cite: number): string | null {
  const text = normalizeCitationMarkers(prose)
  for (const para of text.split(/\n+/)) {
    for (const s of sentences(para)) {
      for (const m of s.matchAll(MARKER_ANY_RE)) {
        if (markerOrdinal(m[0]) === cite) {
          const clean = cleanSentence(s)
          if (clean.length >= 3) return clean
        }
      }
    }
  }
  return null
}

/** The number of the first citation marker in `text` (the sentence a read quotes), or null. */
export function firstCitedOrdinal(text: string | null | undefined): number | null {
  if (!text) return null
  const m = normalizeCitationMarkers(text).match(MARKER_ANY_RE)
  return m ? markerOrdinal(m[0]) : null
}

/** The lead sentence of `prose`: its first sentence of real text (an "As of" line skipped). */
export function leadSentence(prose: string): string | null {
  for (const para of normalizeCitationMarkers(prose).split(/\n+/)) {
    const flat = para.trim()
    if (!flat || /^#/.test(flat)) continue
    if (/^\*?as of\b/i.test(stripMarkdown(flat).trim())) continue
    const first = sentences(flat)[0]
    if (!first) continue
    const clean = cleanSentence(first)
    if (clean.length >= 3) return clean
  }
  return null
}

export function statementOf(root: LineageNodeWire | null, cite: number | null): Statement {
  if (!root) return { text: null, basis: 'none', citation: null, gap: null }
  const prose = proseOf(root.body ?? null)
  const citations = extractCitations(root.body ?? null)
  const title = str(root.title)
  if (cite !== null) {
    const citation = citations.find((c) => markerOrdinal(c.marker) === cite) ?? null
    const sentence = prose ? citedSentence(prose, cite) : null
    if (sentence) {
      return {
        text: sentence,
        basis: 'cited_sentence',
        citation,
        gap: citation ? null : `This sentence carries marker ${cite}, but the record lists no citation with that number, so it maps to no row of evidence.`,
      }
    }
    return {
      text: title ?? (prose ? leadSentence(prose) : null),
      basis: title ? 'title' : 'lead',
      citation,
      gap: `No sentence of this record carries citation ${cite}; the record's ${title ? 'title' : 'lead'} is shown instead.`,
    }
  }
  const lead = prose ? leadSentence(prose) : null
  if (lead) return { text: lead, basis: 'lead', citation: null, gap: null }
  if (title) return { text: title, basis: 'title', citation: null, gap: null }
  return { text: null, basis: 'none', citation: null, gap: 'This record carries no text to quote.' }
}

// ---------------------------------------------------------------------------
// Step 2 — the row.
// ---------------------------------------------------------------------------

const ROW_NOUN: Record<string, string> = {
  finding: 'statement',
  signal: 'received item',
  event: 'event',
  situation: 'situation',
  hypothesis: 'forecast question',
  fact: 'fact',
  observation: 'series value',
  alert: 'alert',
  critique: 'check',
  meta_finding: 'statement',
}

export function rowNoun(kind: string): string {
  return ROW_NOUN[kind] ?? 'record'
}

/** "Written by Escalation on 14 Nov 2026, 01:33 UTC" — or the received form. */
export function rowLine(root: LineageNodeWire): string {
  const at = when(root.produced_at)
  if (root.row_kind === 'signal') {
    const host = hostOf(root.canonical_url)
    return `Received${host ? ` from ${host}` : ''}${at ? ` on ${at}` : ''}.`
  }
  const by = root.analyst_id ? humanizeAnalystId(root.analyst_id) : null
  return `Written${by ? ` by ${by}` : ' with no recorded writer'}${at ? ` on ${at}` : ''}.`
}

// ---------------------------------------------------------------------------
// Step 3 — the evidence.
// ---------------------------------------------------------------------------

export interface EvidenceItem {
  /** `signal` items carry receipts; any other kind is a row the statement rests on. */
  kind: string
  id: string
  title: string | null
  url: string | null
  /** The cited passage (a citation's snippet / evidence text), when recorded. */
  passage: string | null
  /** The marker number that cites it in the statement's record. */
  ordinal: number | null
  archived: boolean | null
}

export interface Evidence {
  items: EvidenceItem[]
  /** How many rows rest under the statement in all (before the cap). */
  total: number
  /** A gap, in words, when the statement maps to no row. */
  gap: string | null
}

function fromCitation(c: Citation, nodes: Map<string, LineageNodeWire>): EvidenceItem {
  const node = nodes.get(c.refId)
  return {
    kind: c.refKind === 'finding' || c.refKind === 'prior_read' ? 'finding' : c.refKind === 'signal' ? 'signal' : String(c.refKind),
    id: c.refId,
    title: str(c.title) ?? str(node?.title) ?? null,
    url: (c.source && /^https?:\/\//.test(c.source) ? c.source : null) ?? node?.canonical_url ?? null,
    passage: str(c.evidenceText) ?? null,
    ordinal: markerOrdinal(c.marker),
    archived: node ? Boolean(node.archived) : null,
  }
}

function fromNode(n: LineageNodeWire): EvidenceItem {
  return {
    kind: n.row_kind,
    id: n.id,
    title: str(n.title),
    url: n.canonical_url ?? null,
    passage: null,
    ordinal: null,
    archived: typeof n.archived === 'boolean' ? n.archived : null,
  }
}

export function evidenceOf(lineage: LineageWire | null, statement: Statement, cite: number | null): Evidence {
  if (!lineage) return { items: [], total: 0, gap: null }
  const root = lineage.root
  if (root.row_kind === 'signal') {
    return { items: [fromNode(root)], total: 1, gap: null }
  }
  const nodes = new Map(lineage.nodes.map((n) => [n.id, n]))
  if (cite !== null) {
    const c = statement.citation
    if (!c) return { items: [], total: 0, gap: statement.gap ?? `Citation ${cite} names no row.` }
    if (!c.refId) {
      return {
        items: [],
        total: 0,
        gap: `Citation ${cite} names a block of the record's own grounding, not a separate row, so there is no evidence row to follow.`,
      }
    }
    return { items: [fromCitation(c, nodes)], total: 1, gap: null }
  }
  const citations = extractCitations(root.body ?? null).filter((c) => c.refId)
  const seen = new Set<string>()
  const items: EvidenceItem[] = []
  for (const c of citations) {
    if (seen.has(c.refId)) continue
    seen.add(c.refId)
    items.push(fromCitation(c, nodes))
  }
  if (items.length === 0) {
    for (const n of lineage.nodes) {
      if (seen.has(n.id)) continue
      seen.add(n.id)
      items.push(fromNode(n))
    }
  }
  if (items.length === 0) {
    return { items: [], total: 0, gap: 'This record names no rows it was built from, so the chain stops here.' }
  }
  return { items: items.slice(0, MAX_EVIDENCE), total: items.length, gap: null }
}

/** The evidence ids that can carry a receipt (signals), capped for one call. */
export function receiptIds(evidence: Evidence): string[] {
  return evidence.items.filter((e) => e.kind === 'signal').map((e) => e.id).slice(0, MAX_EVIDENCE)
}

const SOURCE_CLASS_WORDS: Record<string, string> = {
  reporting: 'reporting',
  analysis: 'analysis',
  official: 'official statement',
  state_media: 'state media',
}

/** "France 24 — English · reporting" from the receipts route's source block. */
export function sourceLine(rec: ReceiptRecordWire | null): string | null {
  const s = rec?.source
  if (!s) return null
  const name = s.name ?? s.id.replace(/^source\./, '')
  const cls = s.source_class ? SOURCE_CLASS_WORDS[s.source_class] ?? s.source_class.replace(/_/g, ' ') : null
  return cls ? `${name} · ${cls}` : `${name} · class not recorded`
}

// ---------------------------------------------------------------------------
// Step 4 — the receipt.
// ---------------------------------------------------------------------------

const ROLE_WORDS: Record<string, string> = {
  response: 'the feed or API response it was read from',
  page: 'its article page, fetched when it arrived',
  archiver: 'its article page, fetched again to keep a copy',
  inbound: 'the message pushed to us',
}

export function roleWords(role: string): string {
  return ROLE_WORDS[role] ?? role
}

const HELD_WORDS: Record<VersionWire['held'], string> = {
  hot: 'the received bytes are kept on the host',
  cold: 'the received bytes are in cold storage (a monthly bundle), readable by hash, not from this sheet',
  pruned: 'the received bytes were pruned under the retention rule; their hash and size are kept',
  not_stored: 'the received bytes were not kept',
}

export function heldWords(held: VersionWire['held']): string {
  return HELD_WORDS[held]
}

/** One receipt in plain words (the facts; the sheet lays them out). */
export function receiptFacts(v: VersionWire): { label: string; value: string; mono?: boolean; title?: string }[] {
  const out: { label: string; value: string; mono?: boolean; title?: string }[] = []
  out.push({ label: 'Fetched', value: when(v.first_seen_at) ?? v.first_seen_at, mono: true })
  out.push({ label: 'From', value: v.url, mono: true, title: v.url })
  out.push({ label: 'Protocol', value: `${v.protocol}${v.status ? ` · status ${v.status}` : ''}${v.content_type ? ` · ${v.content_type.split(';')[0]}` : ''}`, mono: true })
  const size = bytesWords(v.size_bytes)
  out.push({ label: 'Content hash', value: `sha256 ${shortHash(v.sha256)}…${size ? ` · ${size}` : ''}`, mono: true, title: v.sha256 })
  const seen = v.fetch_count && v.fetch_count > 1 ? ` · the same bytes seen ${v.fetch_count} times, last ${when(v.last_seen_at) ?? v.last_seen_at}` : ''
  out.push({ label: 'Stored version', value: `version ${v.id}${seen}`, mono: true })
  return out
}

export function noReceiptWords(rec: ReceiptRecordWire, receiptsStart: string | null): string {
  switch (rec.no_receipt_reason) {
    case 'before_receipts_start':
      return `No receipt: this item arrived${rec.fetched_at ? ` on ${when(rec.fetched_at)}` : ''}, before fetch receipts were kept${receiptsStart ? ` (from ${when(receiptsStart)})` : ''}.`
    case 'source_kind_without_receipts':
      return `No receipt: ${rec.source?.name ?? 'its source'} is not fetched over the web (a ${String(rec.source?.kind ?? 'non-web').replace(/_/g, ' ')} source), so no fetch receipt is kept for it.`
    case 'not_linked':
      return 'No receipt: this item arrived after receipts began, from a fetching source, but no receipt was linked to it. The receipt write is best-effort and this one did not land.'
    default:
      return 'No receipt is recorded for this item.'
  }
}

// ---------------------------------------------------------------------------
// Step 5 — later versions of the address.
// ---------------------------------------------------------------------------

const DRIFT_WORDS: Record<string, string> = {
  identical_text: 'the bytes changed, the text did not',
  cosmetic: 'only page furniture changed',
  appended: 'text was added; nothing earlier was removed',
  minor_edit: 'a minor edit (spelling or punctuation)',
  title_changed: 'the headline changed',
  substantive_edit: 'a substantive edit: earlier text was removed or reworded, or a number, date or name changed',
  retracted_or_replaced: 'retracted or replaced',
  gone: 'the page no longer answers',
  unclassifiable: 'changed, and the change could not be classified',
}

export function driftWords(cls: string): string {
  return DRIFT_WORDS[cls] ?? cls.replace(/_/g, ' ')
}

/** Drift classes that mean the page changed in substance after it was read. */
export const SUBSTANTIVE_DRIFT: ReadonlySet<string> = new Set(['substantive_edit', 'retracted_or_replaced', 'gone', 'title_changed'])

export interface LaterVersions {
  /** The moment the evidence was read (its page version, else its arrival). */
  readAt: string | null
  later: { version: VersionWire; drift: string | null }[]
  more: boolean
  /** A page that went away after it was read, from the drift rows. */
  gone: DriftChangeWire | null
  /** One plain sentence: what is known about later fetches of this address. */
  summary: string
}

export function laterVersionsOf(rec: ReceiptRecordWire, receipts: ReceiptsWire): LaterVersions | null {
  const address = rec.page_address
  if (!address) return null
  const page = receipts.pages[address]
  const linkedPage = rec.links.find((l) => l.role === 'page')
  const linkedVersion = linkedPage ? receipts.versions[String(linkedPage.version_id)] : undefined
  const readAt = linkedVersion?.first_seen_at ?? rec.fetched_at
  const readMs = readAt ? Date.parse(readAt) : NaN
  const versions = page?.versions ?? []
  const changes = receipts.changes.filter((c) => c.address === address)
  const later = versions
    .filter((v) => v.id !== linkedVersion?.id && Number.isFinite(readMs) && Date.parse(v.first_seen_at) > readMs)
    .map((v) => ({ version: v, drift: changes.find((c) => c.later_version_id === v.id)?.drift_class ?? null }))
  const gone = changes.find((c) => c.later_gone && Number.isFinite(readMs) && Date.parse(c.later_fetched_at) > readMs) ?? null
  let summary: string
  if (later.length === 0 && !gone) {
    const sameLater = versions.find((v) => Date.parse(v.last_seen_at) > readMs && (!Number.isFinite(readMs) || Date.parse(v.first_seen_at) <= readMs))
    summary = versions.length === 0
      ? 'This page address was never fetched as a page, so whether it changed after it was read is not known.'
      : sameLater
        ? `Fetched again until ${when(sameLater.last_seen_at)} with the same bytes: no later version.`
        : 'Not fetched again after it was read, so a later change would not show here.'
  } else {
    const worst = later.find((l) => l.drift && SUBSTANTIVE_DRIFT.has(l.drift)) ?? (gone ? { drift: 'gone' } : null)
    summary = worst
      ? `Changed after it was read: ${driftWords(worst.drift ?? 'gone')}.`
      : `${later.length} later version${later.length === 1 ? '' : 's'}; none changed the text in substance.`
  }
  return { readAt, later, more: Boolean(page?.more), gone, summary }
}

// ---------------------------------------------------------------------------
// Step 6 — the integrity line.
// ---------------------------------------------------------------------------

export interface IntegrityLine {
  label: string
  state: IntegrityState
  words: string
}

export const STATE_WORDS: Record<IntegrityState, string> = {
  verified: 'verified',
  covered: 'covered, not checked here',
  not_covered: 'not covered',
  failed: 'failed',
}

export function integrityLines(root: LineageNodeWire, integrity: IntegrityWire | null): IntegrityLine[] {
  if (root.row_kind === 'signal' || !root.receipt) {
    return [
      {
        label: 'Run receipt',
        state: 'not_covered',
        words: root.row_kind === 'signal'
          ? 'A received item is not written by a run, so there is no run receipt; its check is the content hash on its fetch receipt above.'
          : 'No run receipt names this row: it was written before the receipt chain, or by a path that keeps none.',
      },
    ]
  }
  const lines: IntegrityLine[] = []
  const rcpt = integrity?.receipt ?? null
  const chainOk = rcpt ? rcpt.state === 'verified' : root.receipt.chain_consistent
  lines.push({
    label: 'Run receipt',
    state: rcpt ? rcpt.state : chainOk ? 'verified' : 'failed',
    words: chainOk
      ? `The run that wrote this row re-hashes to its stored receipt (sha256 ${shortHash(root.receipt.receipt_hash)}…): the row was not changed after it was recorded.`
      : `The run's stored receipt does not match its contents when re-hashed (sha256 ${shortHash(root.receipt.receipt_hash)}…): this row may have been changed after it was recorded.`,
  })
  if (!integrity) {
    lines.push({ label: 'Checkpoint', state: 'not_covered', words: 'The checkpoint and the daily anchor were not read.' })
    return lines
  }
  const cp = integrity.checkpoint
  lines.push({
    label: 'Checkpoint',
    state: cp.state,
    words:
      cp.state === 'verified'
        ? `A signed checkpoint names this exact receipt as its head (signed by ${cp.signer_did ?? 'the registry'}).`
        : cp.state === 'covered'
          ? `No checkpoint names this receipt itself; a later signed checkpoint of the same writer's chain was made on ${when(cp.checkpointed_at) ?? cp.checkpointed_at ?? 'a later date'}. The links from this receipt to it are not re-walked here.`
          : 'No signed checkpoint covers this receipt yet.',
  })
  const an = integrity.anchor
  lines.push({
    label: 'Daily anchor',
    state: an.state,
    words:
      an.state === 'covered'
        ? `The day's external timestamp (${an.day ?? 'its day'}, ${an.calendars ?? 1} public calendar${an.calendars === 1 ? '' : 's'}) was made after this run; its proof is checked offline, not here.`
        : an.pending
          ? `The external timestamp for ${an.day ?? 'that day'} is still waiting on its calendar.`
          : 'No external timestamp has been made since this run.',
  })
  return lines
}

export function textWithheldWords(t: VersionTextWire): string {
  switch (t.withheld_reason) {
    case 'reader_display_policy':
      return 'The full received text is shown to admins and owners only; readers see the cited passage.'
    case 'license_forbids':
      return "This source's licence does not allow its text to be kept or shown; the receipt's hash and size stand in for it."
    case 'no_text':
      return 'This version kept no text: a feed or API response is kept as bytes only, and the stored text view reads pages.'
    default:
      return t.found ? 'The text could not be read.' : 'This stored version was not found.'
  }
}

// ---------------------------------------------------------------------------
// B-1 — the evidence label and the item as received.
// ---------------------------------------------------------------------------

const EVIDENCE_WORDS: Record<EvidenceLabel, string> = {
  received_bytes: 'Received bytes: the bytes the poll received are held.',
  archived_later: 'Archived later: only a copy fetched when the page was archived is held, not the bytes the poll received; the page may have changed in between.',
  no_bytes_kept: 'No bytes kept: nothing this item arrived in is held.',
}

/** The evidence label in words (`byte_path`'s three labels), or null when the
 * receipts read carried none (an observation, or an older server). */
export function evidenceLabelWords(ev: EvidenceStateWire | null | undefined): string | null {
  if (!ev) return null
  const base = EVIDENCE_WORDS[ev.label] ?? ev.label_text
  return ev.label === 'no_bytes_kept' && ev.hash_only.length > 0
    ? `${base.replace(/\.$/, '')}; ${ev.hash_only.length === 1 ? 'one receipt is' : `${ev.hash_only.length} receipts are`} named by hash only.`
    : base
}

/** Whether the item as received can be asked for on THIS receipt: the label's
 * version is this one, it is a response or pushed envelope (a page shows its
 * own stored text), and byte_path says text can be read from it. */
export function canReadItem(ev: EvidenceStateWire | null | undefined, versionId: number, role: string): boolean {
  return !!ev && ev.version_id === versionId && ev.has_text && (role === 'response' || role === 'inbound')
}

export function itemWithheldWords(t: ItemTextWire): string {
  switch (t.withheld_reason) {
    case 'no_bytes_kept':
      return 'No bytes were kept for this item, so the item as received cannot be shown.'
    case 'license_forbids':
      return "This source's licence does not allow its text to be kept or shown; the receipt's hash and size stand in for it."
    case 'claim_text_admin_only':
      return "This is a claim source: its received text is shown to admins only."
    case 'reader_display_policy':
      return 'The full received text is shown to admins and owners only; readers see the cited passage.'
    case 'no_item_text':
      return 'The stored response holds no item at this record\'s place in it, so no item text can be cut from it.'
    case 'item_reader_unavailable':
      return 'This deployment cannot read stored bytes, so the item as received cannot be shown here.'
    default:
      return t.found ? 'The item could not be read.' : 'This item was not found among received items.'
  }
}

