/**
 * The Consult SCREEN (a full-screen tab).
 *
 * Always open, full height, no toggle. Top to bottom:
 *
 *   the thread       scrolls on its own; keeps its place (the distance from
 *                    its bottom) when the keyboard opens or closes, so the
 *                    last message stays in view; keeps it, too, across a
 *                    Record peek or page and Back (the tab stays mounted)
 *   the drive strip  what an answer opened, what the phone could not open
 *                    (refused by name), Stop and Undo (`drive.tsx`)
 *   "Asking about"   the point "Ask about this" pinned, removable (×)
 *   two controls     the window (the shell's chips: 24 h, 7 d, 30 d) said in
 *                    plain words, and "Only what is on record"
 *   the composer     the question box and Ask (Stop while a turn runs),
 *                    pinned above the keyboard (the shell follows
 *                    `visualViewport` and hides the bar while it is up)
 *
 * The turn is the desktop's (`state/useConsultTurn`): streaming,
 * the 202 left to the stream's terminal frame, the detached-turn pickup after
 * a reload, Stop then "answer from what it found", the partial and recovered
 * states. The phone always asks on the free route and never arms a write; no
 * model picker. Citations open the Record's peek (the shell turns a
 * `cited-prose` selection into `#rec=…&peek=1`).
 */
import { memo, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { consultActions, stripFinalFrame, useConsultPanel, type ChatTurn } from '@/state/consultSession'
import { useConsultTurn } from '@/state/useConsultTurn'
import { useDriveMode } from '@/state/driveMode'
import { useCasePlayer } from '@/state/casePlayer'
import { loadGroundingMode, saveGroundingMode, type GroundingMode } from '@/lib/consultContract'
import { emitRead } from '@/lib/readTelemetry'
import CitedProse from '@/components/CitedProse'
import { useWorldState } from '@/v4/world/worldState'
import { PHONE_WINDOWS, phoneWindowOf } from '@/mobile/screens/changed/changedModel'
import { usePhoneShell } from '@/mobile/shell/phoneState'
import type { PhoneScreen } from '@/mobile/shell/phoneAddress'
import { consultAnswerCitations } from '@/mobile/consultCitations'
import {
  MOBILE_PANEL_ID,
  PHONE_CONSULT_MODEL,
  QUESTION_BOX_MAX,
  phoneTurnRequest,
  phoneWindowWords,
  type PhoneAsking,
} from '@/mobile/mobileConsult'
import { PhoneContract } from './PhoneContract'
import { DriveStrip, runRecordedCase } from './drive'

/** One assistant answer, through the shared prose renderer (citations are 44 px chips). */
const AnswerProse = memo(function AnswerProse({ turn }: { turn: ChatTurn }) {
  const bound = useMemo(() => consultAnswerCitations(turn.content, turn.citedRefs), [turn.content, turn.citedRefs])
  return <CitedProse text={bound.text} citations={bound.citations} variant="block" className="ph-answer" />
})

/**
 * The answer as it is written. A stream can land dozens of frames in one
 * go; the markdown is re-rendered at a deferred priority, so the phone's
 * main thread is never held for the whole burst (the walk's 200 ms floor).
 */
const LiveProse = memo(function LiveProse({ text }: { text: string }) {
  return <CitedProse text={stripFinalFrame(text)} citations={[]} variant="block" className="ph-answer" />
})

function LiveAnswer({ text }: { text: string }) {
  const deferred = useDeferredValue(text)
  return <LiveProse text={deferred} />
}

/** Was this answer cut before its synthesis finished? */
function isCut(turn: ChatTurn): boolean {
  return turn.synthesisStatus === 'partial' || turn.synthesisStatus === 'none'
}

function clip(s: string, n: number): string {
  const t = s.trim()
  return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t
}

/** Two or three questions built from what is on screen (filled in, never sent by themselves). */
export function exampleQuestions(asking: PhoneAsking | null, last: PhoneScreen | null, windowWords: string): string[] {
  if (asking) {
    const x = clip(asking.label, 48)
    return [`What changed for ${x} in ${windowWords}?`, `Where do the sources disagree about ${x}?`, `What should I watch next about ${x}?`]
  }
  const page = last?.page?.kind ?? null
  const tab = last?.tab ?? 'changed'
  if (page === 'alert' || tab === 'watch') return ['Which alerts matter most right now?', `What set off the alerts in ${windowWords}?`]
  if (page === 'proof' || tab === 'ledger') return ['Which forecasts are due soon?', 'How have past forecasts turned out?']
  if (page === 'map') return [`Where did most change in ${windowWords}?`, 'Which places have gone quiet?']
  if (tab === 'subjects') return [`Which subjects changed most in ${windowWords}?`, 'Which subjects have no recent reporting?']
  return [`What changed in ${windowWords}?`, 'Where do the sources disagree?', 'What should I watch next?']
}

/**
 * Keep the thread's distance from its bottom across a resize (the keyboard
 * opening or closing, the bar coming back). A scroll event the anchor itself
 * caused is not the reader scrolling: it can land after the next layout
 * change, and must not re-measure the distance against that layout.
 */
const LAYOUT_SETTLE_MS = 400

function useBottomAnchor(el: React.RefObject<HTMLDivElement | null>) {
  const fromBottom = useRef(0)
  const setTop = useRef<number | null>(null)
  const frame = useRef<number | null>(null)
  const place = useCallback(
    (top: number) => {
      const s = el.current
      if (!s) return
      s.scrollTop = Math.max(0, top)
      setTop.current = s.scrollTop
    },
    [el],
  )
  // When the layout last moved under the thread (the keyboard, the bar): a
  // scroll event inside the settle window is the browser clamping, not the
  // reader — it is dispatched a frame late, against a layout that has moved
  // again, and would record a distance nobody chose.
  const layoutAt = useRef(0)
  const onScroll = useCallback(() => {
    const s = el.current
    if (!s) return
    if (setTop.current !== null && Math.abs(s.scrollTop - setTop.current) < 1) return
    if (Date.now() - layoutAt.current < LAYOUT_SETTLE_MS) return
    setTop.current = null
    fromBottom.current = Math.max(0, s.scrollHeight - s.scrollTop - s.clientHeight)
  }, [el])
  useEffect(() => {
    const s = el.current
    if (!s || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      layoutAt.current = Date.now()
      place(s.scrollHeight - s.clientHeight - fromBottom.current)
    })
    ro.observe(s)
    const vv = typeof window !== 'undefined' ? window.visualViewport : null
    const moved = () => {
      layoutAt.current = Date.now()
    }
    vv?.addEventListener('resize', moved)
    return () => {
      ro.disconnect()
      vv?.removeEventListener('resize', moved)
    }
  }, [el, place])
  useEffect(
    () => () => {
      if (frame.current !== null) cancelAnimationFrame(frame.current)
    },
    [],
  )
  /** To the bottom — once per frame, however many stream frames land in it. */
  const toBottom = useCallback(() => {
    fromBottom.current = 0
    if (typeof requestAnimationFrame === 'undefined') {
      const s = el.current
      if (s) place(s.scrollHeight)
      return
    }
    if (frame.current !== null) return
    frame.current = requestAnimationFrame(() => {
      frame.current = null
      const s = el.current
      if (s) place(s.scrollHeight)
    })
  }, [el, place])
  return { onScroll, toBottom }
}

export default function ConsultScreen() {
  const asking = usePhoneShell((s) => s.asking)
  const lastScreen = usePhoneShell((s) => s.lastScreen)
  const panel = useConsultPanel(MOBILE_PANEL_ID)
  const turn = useConsultTurn(MOBILE_PANEL_ID, { model: PHONE_CONSULT_MODEL })
  const { busy, detached, stopPhase } = turn
  const pending = panel.pendingTurn
  const spanMs = useWorldState((s) => s.spanMs)
  const win = phoneWindowOf(spanMs)
  const words = phoneWindowWords(spanMs)
  const driveOn = useDriveMode((s) => s.on)
  const liveCase = useCasePlayer((s) => (s.panelId === MOBILE_PANEL_ID ? (s.player?.requestId ?? null) : null))
  const [grounding, setGroundingState] = useState<GroundingMode>(() => loadGroundingMode())
  const setGrounding = (g: GroundingMode) => {
    setGroundingState(g)
    saveGroundingMode(g)
  }
  const scroller = useRef<HTMLDivElement | null>(null)
  const input = useRef<HTMLTextAreaElement | null>(null)
  const { onScroll, toBottom } = useBottomAnchor(scroller)

  useEffect(() => {
    const pin = usePhoneShell.getState().asking?.pins[0] ?? null
    emitRead('consult_open', { subjectKind: pin ? pin.kind : null, subjectId: pin ? pin.id : null })
  }, [])

  const send = () => {
    const question = panel.draft.trim()
    if (!question || busy) return
    void turn.send({
      question,
      mode: 'chat',
      request: phoneTurnRequest({
        question,
        asking,
        maxRounds: panel.maxRounds,
        spanMs: useWorldState.getState().spanMs,
        groundingMode: grounding,
        presentCase: driveOn,
      }),
    })
  }

  // A new turn, and the answer as it streams, stay in view.
  const count = panel.transcript.length
  const preview = pending?.answerPreview ?? ''
  const pendingNow = !!pending
  useEffect(() => {
    toBottom()
  }, [count, pendingNow, preview.length, toBottom])

  const pendingLabel = !pending
    ? ''
    : pending.stalled
      ? 'Stopped waiting — no answer was recorded for this question. It may still appear here later.'
      : stopPhase === 'stopping'
        ? 'Stopping…'
        : stopPhase === 'synthesizing'
          ? 'Answering from what it found so far…'
          : detached
            ? 'Picked up again after a reload — still working…'
            : pending.answerPreview
              ? 'Writing the answer…'
              : 'Thinking…'

  const empty = panel.transcript.length === 0 && !pending
  const examples = empty ? exampleQuestions(asking, lastScreen, words) : []

  return (
    <div className="ph-consult" data-testid="phone-consult">
      <div className="ph-thread" ref={scroller} onScroll={onScroll} data-testid="consult-transcript">
        <div className="ph-thread-inner">
          {empty ? (
            <div className="ph-consult-empty" data-testid="consult-empty">
              <p className="ph-body">
                Ask a question about what is on record{asking ? `, with ${clip(asking.label, 60)} in focus` : ''}. Each answer shows the
                records it rests on: tap a number to read one.
              </p>
              <ul className="ph-examples">
                {examples.map((q) => (
                  <li key={q}>
                    <button
                      type="button"
                      className="ph-example"
                      data-testid="consult-example"
                      onClick={() => {
                        consultActions().setDraft(MOBILE_PANEL_ID, q)
                        input.current?.focus()
                      }}
                    >
                      {q}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {panel.transcript.map((t, i) => (
            <div key={`${t.role}-${i}`} className={`ph-turn ph-turn-${t.role}`} data-testid={`consult-turn-${t.role}`}>
              {t.role === 'assistant' ? <AnswerProse turn={t} /> : <p className="ph-question">{t.content}</p>}
              {t.role === 'assistant' && t.contract ? <PhoneContract contract={t.contract} usage={t.usageReceipt} /> : null}
              {t.role === 'assistant' && isCut(t) ? (
                <p className="ph-quiet" data-testid="consult-partial">
                  Partial answer — the run stopped before it finished. What it gathered is kept with this conversation.
                </p>
              ) : null}
              {t.role === 'assistant' && t.replayFidelity && t.replayFidelity !== 'exact' ? (
                <p className="ph-quiet" data-testid="consult-replay-note">
                  {t.replayNote || 'Answered again from the evidence the run had gathered.'}
                </p>
              ) : null}
              {/* drive mode off (or a case recorded elsewhere): the answer OFFERS what it points to */}
              {t.role === 'assistant' && t.caseRecord && t.caseRecord.actions.length > 0 && t.caseRecord.request_id !== liveCase ? (
                <button type="button" className="ph-btn ph-btn-wide ph-offer" onClick={() => runRecordedCase(t.caseRecord!)} data-testid="consult-drive-offer">
                  Show what this answer points to
                </button>
              ) : null}
            </div>
          ))}
          {pending ? (
            <div className="ph-turn ph-turn-pending" data-testid="consult-pending">
              <p className="ph-quiet" data-testid="consult-pending-label">
                {pendingLabel}
              </p>
              {pending.answerPreview ? (
                <div data-testid="consult-answer-live">
                  <LiveAnswer text={pending.answerPreview} />
                </div>
              ) : null}
              {pending.stalled ? (
                <button type="button" className="ph-btn" onClick={() => consultActions().dismissPending(MOBILE_PANEL_ID)} data-testid="consult-pending-dismiss">
                  Dismiss
                </button>
              ) : null}
            </div>
          ) : null}
          {panel.error ? (
            <p className="ph-error" data-testid="consult-error">
              {panel.error}
            </p>
          ) : null}
        </div>
      </div>

      <DriveStrip />

      <div className="ph-dock" data-testid="consult-dock-phone">
        {asking ? (
          <div className="ph-asking" data-testid="phone-asking">
            <span className="ph-asking-text">
              <span className="ph-dim">Asking about </span>
              <span className="ph-point" data-testid="phone-asking-label">
                {asking.label}
              </span>
            </span>
            <button
              type="button"
              className="ph-icon-btn"
              aria-label="Ask about anything"
              data-testid="phone-asking-clear"
              onClick={() => usePhoneShell.setState({ asking: null })}
            >
              <X size={20} aria-hidden />
            </button>
          </div>
        ) : null}
        <div className="ph-consult-controls" data-testid="consult-controls">
          <label className="ph-select-wrap">
            <span className="ph-sr">Window</span>
            <select
              className="ph-select"
              value={win.id}
              onChange={(e) => {
                const w = PHONE_WINDOWS.find((x) => x.id === e.target.value)
                if (w) useWorldState.getState().setSpan(w.ms)
              }}
              data-testid="consult-window"
            >
              {PHONE_WINDOWS.map((w) => (
                <option key={w.id} value={w.id}>
                  {`Last ${phoneWindowWords(w.ms).replace(/^the last /, '')}`}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            role="switch"
            aria-checked={grounding === 'record_only'}
            className="ph-switch"
            onClick={() => setGrounding(grounding === 'record_only' ? 'default' : 'record_only')}
            data-testid="consult-record-only"
          >
            <span className="ph-switch-dot" aria-hidden />
            On record only
          </button>
        </div>
        <p className="ph-consult-words" data-testid="consult-window-words">
          Answers look at {words} unless your question names a time
          {grounding === 'record_only' ? '; every sentence must rest on a record' : ''}.
        </p>
        <div className="ph-composer" data-testid="consult-composer">
          <textarea
            ref={input}
            className="ph-question-box"
            rows={2}
            maxLength={QUESTION_BOX_MAX}
            placeholder={asking ? `Ask about ${clip(asking.label, 40)}…` : 'Ask a question…'}
            aria-label="Your question"
            value={panel.draft}
            data-testid="consult-input"
            onChange={(e) => consultActions().setDraft(MOBILE_PANEL_ID, e.target.value)}
          />
          {/* Stop takes Ask's place while a turn runs — never gated on `busy`:
              it is the control that ends the spending. */}
          {pending && !pending.stalled ? (
            <button type="button" className="ph-send ph-stop" onClick={() => void turn.stop()} disabled={stopPhase !== null} data-testid="consult-stop">
              {stopPhase ? '…' : 'Stop'}
            </button>
          ) : (
            <button type="button" className="ph-send" onClick={send} disabled={busy || !panel.draft.trim()} data-testid="consult-send">
              Ask
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
