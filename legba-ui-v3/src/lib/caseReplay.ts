/**
 * caseReplay — a recorded case as an OBJECT the shell can reach (UI-B1/shell;
 * UI-D1 §2.1 "case": the rail's Cases, ⌘K's Cases family, `#case=`).
 *
 * A case is the `case` key of a persisted consult turn (C-1a §3). Replaying it
 * re-runs its actions through the ONE executor (`applyAction`, driver `case`)
 * with no consult POST and no tool call — the C-1b player does that; this
 * module only finds the record and hands it to the player, in the Consult
 * column, whose rail carries the step list, STOP and Undo.
 *
 * Three producers, one path:
 *   * the rail and ⌘K list the turns already in the Consult store that carry a
 *     case ({@link recordedCases});
 *   * `#case=<session>:<turn>` fetches the session, adopts it into the Consult
 *     tile, and replays that turn's case ({@link openCaseAddress}).
 */
import { loadConsultSession, type ConsultSessionDetail } from '@/lib/api'
import { parseCaseRecord, type CaseRecord } from '@/lib/caseModel'
import { drive } from '@/lib/applyAction'
import { openPanel } from '@/lib/commands'
import { casePlayer } from '@/state/casePlayer'
import { useConsultSessions, type ConsultPanelState } from '@/state/consultSession'
import type { CaseAddress } from '@/lib/address'

/** The Consult singleton's store slice — the tile a replay plays in. */
export const CONSULT_PANEL_ID = 'singleton:system.consult'

export interface RecordedCase {
  /** The Consult slice the turn lives in. */
  panelId: string
  sessionId: string | null
  /** Position of the turn in that slice's transcript. */
  turnIndex: number
  /** The question that turn answered (its preceding user turn), or the case's own summary. */
  label: string
  record: CaseRecord
}

function clip(s: string, n = 80): string {
  const one = s.replace(/\s+/g, ' ').trim()
  return one.length > n ? `${one.slice(0, n - 1)}…` : one
}

/** Every turn in the Consult store that presented a case, newest first. */
export function recordedCases(panels: Readonly<Record<string, ConsultPanelState>>): RecordedCase[] {
  const out: RecordedCase[] = []
  for (const [panelId, panel] of Object.entries(panels)) {
    panel.transcript.forEach((turn, i) => {
      const rec = turn.caseRecord
      if (!rec) return
      const asked = [...panel.transcript.slice(0, i)].reverse().find((t) => t.role === 'user')
      out.push({
        panelId,
        sessionId: panel.sessionId,
        turnIndex: i,
        label: clip(asked?.content || rec.summary || `case · ${rec.actions.length} steps`),
        record: rec,
      })
    })
  }
  return out.sort((a, b) => Date.parse(b.record.as_of ?? '') - Date.parse(a.record.as_of ?? '') || 0)
}

/** Replay the case in the Consult column (the open is a no-op: the column is always there). */
export function replayRecordedCase(c: Pick<RecordedCase, 'panelId' | 'record'>): void {
  drive(openPanel('system.consult'))
  casePlayer().replay(c.record, c.panelId)
}

export type CaseOpenResult = { ok: true; turnIndex: number } | { ok: false; reason: string }

/** Find the addressed turn: by id first, then by 0-based position. */
export function findCaseTurn(detail: ConsultSessionDetail, turn: string): number {
  const byId = detail.turns.findIndex((t) => t.id === turn)
  if (byId >= 0) return byId
  return /^\d+$/.test(turn) && Number(turn) < detail.turns.length ? Number(turn) : -1
}

/**
 * `#case=<session>:<turn>` → a replay. The session is adopted into the Consult
 * tile so the replayed case sits in its own conversation; a turn that presented
 * no case, or a session that cannot be read, says so — nothing is replayed.
 */
export async function openCaseAddress(
  addr: CaseAddress,
  load: (sessionId: string) => Promise<ConsultSessionDetail> = loadConsultSession,
): Promise<CaseOpenResult> {
  let detail: ConsultSessionDetail
  try {
    detail = await load(addr.session)
  } catch (err) {
    return { ok: false, reason: `session ${addr.session} could not be read: ${err instanceof Error ? err.message : String(err)}` }
  }
  const i = findCaseTurn(detail, addr.turn)
  if (i < 0) return { ok: false, reason: `no turn ${addr.turn} in session ${addr.session}` }
  const rec = parseCaseRecord(detail.turns[i].case)
  if (!rec) return { ok: false, reason: `turn ${addr.turn} presented no case` }
  useConsultSessions.getState().adoptSession(CONSULT_PANEL_ID, detail)
  replayRecordedCase({ panelId: CONSULT_PANEL_ID, record: rec })
  return { ok: true, turnIndex: i }
}
