/**
 * Flow (N-J v0) — the compare view's two diffs, both pure.
 *
 * `semanticDiff`: two descriptor bodies → the leaf paths that were ADDED,
 * REMOVED or CHANGED (the bpmn-js-differ buckets; there is no layout bucket
 * because a descriptor carries no layout). Arrays diff by index; a string is a
 * leaf however long, and long strings get a line diff of their own.
 *
 * `lineDiff`: two texts → an LCS line diff (same / add / del), for prompts.
 */

export type DiffKind = 'added' | 'removed' | 'changed'

export interface FieldChange {
  path: string
  kind: DiffKind
  before?: unknown
  after?: unknown
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Flatten to leaf path → value. Empty containers are leaves. */
export function flatten(v: unknown, prefix = '', out = new Map<string, unknown>()): Map<string, unknown> {
  if (isObj(v) && Object.keys(v).length > 0) {
    for (const k of Object.keys(v).sort()) flatten(v[k], prefix ? `${prefix}.${k}` : k, out)
  } else if (Array.isArray(v) && v.length > 0) {
    v.forEach((item, i) => flatten(item, `${prefix}[${i}]`, out))
  } else {
    out.set(prefix || '(root)', v)
  }
  return out
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

export function semanticDiff(before: unknown, after: unknown): FieldChange[] {
  const a = flatten(before)
  const b = flatten(after)
  const out: FieldChange[] = []
  for (const path of [...new Set([...a.keys(), ...b.keys()])].sort()) {
    const inA = a.has(path)
    const inB = b.has(path)
    if (inA && !inB) out.push({ path, kind: 'removed', before: a.get(path) })
    else if (!inA && inB) out.push({ path, kind: 'added', after: b.get(path) })
    else if (!same(a.get(path), b.get(path))) out.push({ path, kind: 'changed', before: a.get(path), after: b.get(path) })
  }
  return out
}

export interface LineOp {
  op: 'same' | 'add' | 'del'
  text: string
}

/** LCS line diff. Inputs past `maxLines` lines are compared as whole texts. */
export function lineDiff(before: string, after: string, maxLines = 4000): LineOp[] {
  const a = before.split('\n')
  const b = after.split('\n')
  if (a.length > maxLines || b.length > maxLines) {
    return before === after
      ? [{ op: 'same', text: before }]
      : [{ op: 'del', text: before }, { op: 'add', text: after }]
  }
  const n = a.length
  const m = b.length
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }
  const ops: LineOp[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ op: 'same', text: a[i] })
      i++
      j++
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      ops.push({ op: 'del', text: a[i++] })
    } else {
      ops.push({ op: 'add', text: b[j++] })
    }
  }
  while (i < n) ops.push({ op: 'del', text: a[i++] })
  while (j < m) ops.push({ op: 'add', text: b[j++] })
  return ops
}

/** The string leaves that changed — each gets a text diff in the view. */
export function changedTexts(changes: readonly FieldChange[]): FieldChange[] {
  return changes.filter(
    (c) => c.kind === 'changed' && typeof c.before === 'string' && typeof c.after === 'string'
      && ((c.before as string).includes('\n') || (c.after as string).includes('\n')
        || (c.before as string).length > 120 || (c.after as string).length > 120),
  )
}
