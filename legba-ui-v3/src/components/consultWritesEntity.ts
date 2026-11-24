/**
 * W11 C-2b-2/entities — the consult write plane's ENTITY record class, the
 * workstation side: the pending card's sentence and the receipt line for an
 * entity write (`class === 'entity'`), rendered from the `consult_writes`
 * ledger row alone (design C-2a §9). Kept in its own file so each record class
 * (entities here, facts beside it) owns its renderer; `ConsultWrites.tsx`
 * dispatches to it.
 *
 * The pending card shows the PRE-IMAGE the executor recorded when it staged
 * the change — what the record looks like now, and what the change would do —
 * so the operator confirms against the server's account, not the answer's
 * prose. An entity has no registry head and no runtime activation: the
 * receipt says "record write" instead of an activation stamp, never
 * "activation pending". Absence renders as absence: a count the pre-image does
 * not carry is "not measured", never 0.
 */
import { clockZ, type ConsultWriteRow } from '@/lib/consultWrites'

/** The entity writable view the executor snapshots (pre_image.fields / .keeper / .loser). */
export interface EntityFields {
  canonical_name?: string | null
  entity_class?: string | null
  aliases?: string[] | null
  geo_country?: string | null
  geo_region?: string | null
  geo_lat?: number | null
  geo_lon?: number | null
  version?: string | null
  gc_status?: string | null
}

/** `consult_writes.pre_image` of an entity write. */
export interface EntityPreImage {
  version?: string | null
  state?: string | null
  sha256?: string | null
  fields?: EntityFields | null
  keeper?: EntityFields | null
  loser?: EntityFields | null
  facts?: { open_naming?: number | null; as_of?: string | null } | null
}

/** `consult_writes.reversal` of an entity write. */
export interface EntityReversal {
  kind?: string
  why?: string
  added_aliases?: string[] | null
  removed_aliases?: string[] | null
}

/** `consult_writes.args` of an entity write (the stored canonical args). */
interface EntityArgs {
  patch?: Record<string, unknown> | null
  keeper_id?: string | null
  loser_id?: string | null
}

export function isEntityWrite(row: Pick<ConsultWriteRow, 'class'>): boolean {
  return row.class === 'entity'
}

function pre(row: ConsultWriteRow): EntityPreImage {
  return (row.pre_image ?? {}) as EntityPreImage
}

function label(f: EntityFields | null | undefined, fallback: string): string {
  if (!f?.canonical_name) return fallback
  return f.entity_class ? `${f.canonical_name} (${f.entity_class})` : f.canonical_name
}

/** "2 aliases" — or `absent` when the ledger row does not carry the number. */
function count(n: number | null | undefined, one: string, many: string, absent: string): string {
  return typeof n === 'number' ? `${n} ${n === 1 ? one : many}` : absent
}

function list(v: unknown): string {
  return Array.isArray(v) ? v.map((x) => `“${String(x)}”`).join(', ') : String(v)
}

/** One clause per patch key, in the operator's words ("aliases +“Acme Group”"). */
export function entityPatchText(patch: Record<string, unknown> | null | undefined): string {
  if (!patch) return 'no patch recorded'
  const parts: string[] = []
  for (const [key, value] of Object.entries(patch)) {
    if (key === 'aliases_add') parts.push(`aliases +${list(value)}`)
    else if (key === 'aliases_remove') parts.push(`aliases −${list(value)}`)
    else if (key === 'canonical_name') parts.push(`name → “${String(value)}”`)
    else if (key === 'entity_class') parts.push(`class → ${String(value)}`)
    else parts.push(`${key} → ${value === null ? 'none' : String(value)}`)
  }
  return parts.join(' · ')
}

/** The amber pending card's sentence for an entity write, with its pre-image summary. */
export function entityPendingText(row: ConsultWriteRow): string {
  const p = pre(row)
  const args = (row.args ?? {}) as EntityArgs
  const rev = (row.reversal ?? {}) as EntityReversal
  const version = p.version ? ` v${p.version}` : ''
  switch (row.verb) {
    case 'edit': {
      const f = p.fields
      const now = f
        ? ` · now: ${count(f.aliases?.length, 'alias', 'aliases', 'aliases not recorded')}` +
          `${f.geo_country ? `, ${f.geo_country}` : ''}`
        : ''
      return `Edit entity ${label(f, row.object_ref)}${version} · ${entityPatchText(args.patch)}${now}`
    }
    case 'merge': {
      const facts = p.facts
      const asOf = facts?.as_of ? ` (as of ${clockZ(facts.as_of)})` : ''
      const folds = count(rev.added_aliases?.length, 'alias', 'aliases', 'aliases: not recorded')
      const named =
        typeof facts?.open_naming === 'number'
          ? `${count(facts.open_naming, 'open fact names', 'open facts name', '')} the loser${asOf}`
          : 'open facts naming the loser: not measured'
      return (
        `Merge entity ${label(p.loser, row.object_ref)} into ${label(p.keeper, args.keeper_id ?? 'the keeper')}` +
        ` · folds ${folds} · ${named} · unmergeable until compaction`
      )
    }
    case 'unmerge':
      return (
        `Unmerge entity ${label(p.loser, row.object_ref)} from ${label(p.keeper, args.keeper_id ?? 'its keeper')}` +
        ` · drops ${count(rev.removed_aliases?.length, 'folded alias', 'folded aliases', 'folded aliases: not recorded')}` +
        ' · records the two as distinct'
      )
    case 'revert':
      return `Revert entity ${label(p.loser ?? p.fields, row.object_ref)}${version}`
    default:
      return `${row.verb} entity ${row.object_ref}`
  }
}

const DONE: Record<string, string> = {
  edit: 'edited',
  merge: 'merged',
  unmerge: 'unmerged',
  revert: 'reverted',
}

/** The receipt line of an executed entity write. */
export function entityReceiptText(row: ConsultWriteRow): string {
  const r = row.receipt ?? {}
  const p = pre(row)
  const before = p.version ?? r.pre?.version ?? null
  const after = row.post_image?.version ?? r.post?.version ?? null
  const move = `v${before ?? '—'}→v${after ?? '—'}`
  const who = label(p.loser ?? p.fields, row.object_ref)
  const into = row.verb === 'merge' && p.keeper ? ` into ${label(p.keeper, '')}` : ''
  const from = row.verb === 'unmerge' && p.keeper ? ` from ${label(p.keeper, '')}` : ''
  const cost =
    typeof r.turn_cost_usd_at_write === 'number' ? `$${r.turn_cost_usd_at_write.toFixed(2)}` : 'cost not measured'
  return [
    `Written · entity ${who} ${DONE[row.verb] ?? row.verb}${into}${from} ${move}`,
    r.plane ?? 'plane not recorded',
    cost,
    clockZ(row.executed_at),
    'record write (no activation)',
  ].join(' · ')
}
