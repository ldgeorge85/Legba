/**
 * ConsultSheet — the Consult, back at the centre.
 *
 * In the v2 Crossroads the Consult was the middle of the workspace and always
 * knew what you were looking at. The workstation dropped it off the landing
 * surface entirely. Here it is a sheet anchored to the bottom of EVERY screen:
 * collapsed it is a single bar naming the pinned report, expanded it is the
 * conversation about that report.
 *
 * ── STATE IS THE SHARED STORE, ON PURPOSE ─────────────────────────────────
 * This sheet drives `state/consultSession`'s zustand store under the panel id
 * `mobile`. It is the same store the workstation's Consult panel uses, with the
 * same localStorage persistence, so a turn started on the phone survives a
 * reload — and the transcript is reconcilable with the server session by the
 * machinery that already exists. A private store would have been less code and
 * a second, divergent definition of "a consult turn".
 *
 * ── NO SSE HERE ───────────────────────────────────────────────────────────
 * The workstation subscribes to `/consult/stream/{request_id}` to render the
 * ReAct step trace live. This surface deliberately does not: the step trace is
 * an operator-debugging affordance that would cost a held-open EventSource on
 * a cellular connection for a pane the size of a business card. The answer
 * still arrives on the POST, which is the part a phone reader wants. The
 * request still carries its `request_id`, so a future pull-to-watch could
 * subscribe without a contract change.
 */

import { useCallback, useRef, useState } from 'react'
import {
  ApiError,
  loadConsultModel,
  saveConsultModel,
  CONSULT_MODEL_OPTIONS,
  type ConsultModel,
} from '@/lib/api'
import {
  CONSULT_PAGE_LOAD_ID,
  consultActions,
  useConsultPanel,
  type ChatTurn,
} from '@/state/consultSession'
import { emitRead } from '@/lib/readTelemetry'
import { sendConsult, type PinnedEntity, type PinnedReport } from '../mobileConsult'

/** The panel id this surface owns inside the shared consult store. */
export const MOBILE_PANEL_ID = 'mobile'

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `m-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export interface ConsultSheetProps {
  report: PinnedReport | null
  entity: PinnedEntity | null
}

export default function ConsultSheet({ report, entity }: ConsultSheetProps) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  // F1 model picker — same persisted choice + options as the workstation
  // Consult panel (shared `legba_consult_model` localStorage key via
  // `loadConsultModel`/`saveConsultModel`), so a plane picked on one surface
  // is the plane the other opens to.
  const [model, setModel] = useState<ConsultModel>(() => loadConsultModel())
  const panel = useConsultPanel(MOBILE_PANEL_ID)
  const scroller = useRef<HTMLDivElement | null>(null)

  const toggle = useCallback(() => {
    setOpen((wasOpen) => {
      if (!wasOpen) {
        emitRead('consult_open', {
          subjectKind: report ? 'finding' : null,
          subjectId: report ? report.id : null,
        })
      }
      return !wasOpen
    })
  }, [report])

  const send = useCallback(async () => {
    const question = panel.draft.trim()
    if (!question || busy) return
    const actions = consultActions()
    const requestId = newRequestId()
    setBusy(true)
    actions.setError(MOBILE_PANEL_ID, null)
    actions.startTurn(MOBILE_PANEL_ID, {
      requestId,
      question,
      mode: 'chat',
      startedAt: Date.now(),
      pageLoadId: CONSULT_PAGE_LOAD_ID,
      steps: [],
    })
    actions.setDraft(MOBILE_PANEL_ID, '')
    try {
      const res = await sendConsult({
        question,
        report,
        entity,
        transcript: panel.transcript,
        sessionId: panel.sessionId,
        model,
        maxRounds: panel.maxRounds,
        requestId,
      })
      const turn: ChatTurn = {
        role: 'assistant',
        content: res.answer,
        citedRefs: res.cited_refs ?? [],
        uncertainty: res.uncertainty ?? null,
        unansweredAspects: res.unanswered_aspects ?? [],
        findingId: res.finding_id ?? null,
        model: res.model ?? null,
      }
      actions.completeTurn(MOBILE_PANEL_ID, requestId, turn)
      if (res.session_id) actions.setSessionId(MOBILE_PANEL_ID, res.session_id)
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `consult failed (${err.status})`
          : err instanceof Error
            ? err.message
            : 'consult failed'
      actions.failTurn(MOBILE_PANEL_ID, requestId, msg)
    } finally {
      setBusy(false)
      requestAnimationFrame(() => {
        const el = scroller.current
        if (el) el.scrollTop = el.scrollHeight
      })
    }
  }, [busy, entity, model, panel.draft, panel.maxRounds, panel.sessionId, panel.transcript, report])

  const pinLabel = report
    ? `${report.target} · ${report.tier}${entity ? ` › ${entity.name}` : ''}`
    : 'No report pinned'

  return (
    <div className={`m-sheet ${open ? 'm-sheet-open' : ''}`} data-testid="consult-sheet">
      <button
        type="button"
        className="m-sheet-bar"
        onClick={toggle}
        aria-expanded={open}
        data-testid="consult-toggle"
      >
        <span className="m-sheet-label">Consult</span>
        <span className="m-sheet-pin" data-testid="consult-pin">
          {pinLabel}
        </span>
        <span className="m-caret">{open ? '▾' : '▴'}</span>
      </button>

      {open ? (
        <div className="m-sheet-body">
          <div className="m-sheet-scroll" ref={scroller} data-testid="consult-transcript">
            {panel.transcript.length === 0 && !panel.pendingTurn ? (
              <p className="m-quiet">
                {report
                  ? 'Ask about this report — it is already pinned as context.'
                  : 'Open a report to pin it as context, or ask anything.'}
              </p>
            ) : null}
            {panel.transcript.map((t, i) => (
              <div
                key={`${t.role}-${i}`}
                className={`m-turn m-turn-${t.role}`}
                data-testid={`consult-turn-${t.role}`}
              >
                {t.content}
              </div>
            ))}
            {panel.pendingTurn ? (
              <div className="m-turn m-turn-pending" data-testid="consult-pending">
                {panel.pendingTurn.question}
                <span className="m-quiet"> — thinking…</span>
              </div>
            ) : null}
            {panel.error ? (
              <p className="m-error" data-testid="consult-error">
                {panel.error}
              </p>
            ) : null}
          </div>

          <div className="m-model-row">
            <label htmlFor="consult-model-mobile" className="m-quiet">
              Model
            </label>
            <select
              id="consult-model-mobile"
              className="m-model-select"
              value={model}
              onChange={(e) => {
                const m = e.target.value as ConsultModel
                setModel(m)
                saveConsultModel(m)
              }}
              data-testid="consult-model"
            >
              {CONSULT_MODEL_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          <div className="m-sheet-compose">
            <textarea
              className="m-input"
              rows={2}
              placeholder="Ask about this report…"
              value={panel.draft}
              data-testid="consult-input"
              onChange={(e) => consultActions().setDraft(MOBILE_PANEL_ID, e.target.value)}
            />
            <button
              type="button"
              className="m-send"
              onClick={send}
              disabled={busy || !panel.draft.trim()}
              data-testid="consult-send"
            >
              {busy ? '…' : 'Ask'}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
