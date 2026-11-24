/**
 * F-1 — the pure parts of deciding an outcome and of proving a seal:
 *
 *   * the browser's fold of an inclusion path agrees with the SERVER's module
 *     (the fixture proofs were computed by `legba.data.anchor_proof`);
 *   * "included" only when the leaf is recomputed from the Ledger's own hash,
 *     the path folds to the root, and the anchors route lists that root for
 *     that day — a tampered leaf, sibling, side or root fails;
 *   * a seal no stored anchor holds says when anchors began keeping leaves;
 *   * the reader's words: who confirmed (a role, never a name) and when; a
 *     rejected proposal leaves the forecast open and says so;
 *   * a decision's rules before anything is sent: a reason for reject, for
 *     void and for a judgement; the "are you sure" sentence; what happened.
 */
import { describe, expect, it } from 'vitest'
import {
  actWords,
  checkLeafProof,
  decisionBody,
  decisionDone,
  decisionPath,
  decisionProblem,
  decisionSummary,
  foldPath,
  forecastState,
  refusalWords,
  roleWords,
  sealProofCheck,
  webSha256,
  type LeafProof,
} from './ledgerProof'
import {
  ANCHORS,
  NUMBER_PROOF,
  PROOF_ANCHORS,
  PROOF_ROOT,
  QUESTION_PROOF,
  SEAL_PROOF,
  forecast,
  queueEntry,
} from '@/panels/ledger/__fixtures__/ledger'

const F1 = forecast(1)

describe('the inclusion path, folded in the browser', () => {
  it('folds the server-computed paths to the server-computed root', async () => {
    expect(await foldPath(QUESTION_PROOF.leaf, QUESTION_PROOF.path!, webSha256)).toBe(PROOF_ROOT)
    expect(await foldPath(NUMBER_PROOF.leaf, NUMBER_PROOF.path!, webSha256)).toBe(PROOF_ROOT)
  })

  it('checks the leaf recomputed from the forecast’s own hash against the anchors route', async () => {
    const ok = await checkLeafProof(QUESTION_PROOF, `forecast_question:${F1.id}${F1.content_sha256}`, PROOF_ANCHORS.anchors, webSha256)
    expect(ok).toEqual({ ok: true, reason: '' })
  })

  it('a tampered leaf, sibling, side or root fails', async () => {
    const input = `forecast_question:${F1.id}${F1.content_sha256}`
    const tamperedHash = await checkLeafProof(QUESTION_PROOF, `forecast_question:${F1.id}${'3'.repeat(64)}`, PROOF_ANCHORS.anchors, webSha256)
    expect(tamperedHash).toMatchObject({ ok: false, reason: 'the leaf does not match this forecast’s hash' })
    const sib: LeafProof = { ...QUESTION_PROOF, path: [{ ...QUESTION_PROOF.path![0], hash: '0'.repeat(64) }, ...QUESTION_PROOF.path!.slice(1)] }
    expect((await checkLeafProof(sib, input, PROOF_ANCHORS.anchors, webSha256)).reason).toBe('the path does not fold to the stated root')
    const side: LeafProof = { ...QUESTION_PROOF, path: [{ ...QUESTION_PROOF.path![0], side: 'right' }, ...QUESTION_PROOF.path!.slice(1)] }
    expect((await checkLeafProof(side, input, PROOF_ANCHORS.anchors, webSha256)).ok).toBe(false)
    // A proof whose own root agrees with its path, but not with the anchor the anchors route lists.
    expect((await checkLeafProof(QUESTION_PROOF, input, [{ ...PROOF_ANCHORS.anchors[0], root_hash: 'f'.repeat(64) }], webSha256)).reason).toBe(
      'the root does not match the anchor listed for 2026-03-02',
    )
    expect((await checkLeafProof(QUESTION_PROOF, input, ANCHORS.anchors, webSha256)).reason).toBe('the anchors list has no anchor for 2026-03-02')
    expect((await checkLeafProof({ ...QUESTION_PROOF, path: [{ side: 'left', hash: 'zz' }] }, input, PROOF_ANCHORS.anchors, webSha256)).ok).toBe(false)
  })
})

describe('what the seal says', () => {
  it('included, with its numbers, only when the path checks; the outside timestamp is never claimed', async () => {
    const r = await sealProofCheck(F1, SEAL_PROOF, PROOF_ANCHORS.anchors)
    expect(r.state).toBe('included')
    expect(r.text).toMatch(/^Included in the anchor of 2026-03-02: the path from this seal to that day's root checks in your browser\./)
    expect(r.text).toMatch(/Its stated number checked the same way\./)
    expect(r.text).toMatch(/It was sealed on 2026-02-27; the anchors before 2026-03-02 kept their roots only\./)
    expect(r.text).toMatch(/submitted to one public timestamp calendar; the timestamp proof is checked offline/)
    expect(r.text).not.toMatch(/\bverified\b/i)
    expect(r).toMatchObject({ numbersChecked: 1, numbersTotal: 1 })
  })

  it('a tampered proof is said plainly and claims nothing', async () => {
    const bad = { ...SEAL_PROOF, question: { ...QUESTION_PROOF, root: 'a'.repeat(64) } }
    const r = await sealProofCheck(F1, bad, PROOF_ANCHORS.anchors)
    expect(r.state).toBe('failed')
    expect(r.text).toMatch(/^The inclusion path did not check: the path does not fold to the stated root\./)
    expect(r.text).not.toMatch(/Included in/)
    // A forecast whose hash differs from the one proven.
    const other = await sealProofCheck({ ...F1, content_sha256: '9'.repeat(64) }, SEAL_PROOF, PROOF_ANCHORS.anchors)
    expect(other.state).toBe('failed')
  })

  it('a seal no stored anchor holds says since when anchors keep their leaves', async () => {
    const none = { ...SEAL_PROOF, question: { ...QUESTION_PROOF, included: false, path: undefined, first_stored_day: '2026-11-15' }, numbers: [] }
    const r = await sealProofCheck(F1, none, PROOF_ANCHORS.anchors)
    expect(r.state).toBe('not_stored')
    expect(r.text).toMatch(/Anchors keep their leaves from 2026-11-15; before that a seal was anchored by the day's root only/)
    const never = { ...none, question: { ...none.question, first_stored_day: null } }
    expect((await sealProofCheck(F1, never, PROOF_ANCHORS.anchors)).text).toMatch(/No anchor has kept its leaves yet/)
  })

  it('reading, unreadable and absent routes are their own answers', async () => {
    expect((await sealProofCheck(F1, undefined, PROOF_ANCHORS.anchors)).state).toBe('reading')
    expect((await sealProofCheck(F1, null, PROOF_ANCHORS.anchors)).state).toBe('unavailable')
    expect((await sealProofCheck(F1, { ...SEAL_PROOF, available: false }, PROOF_ANCHORS.anchors)).state).toBe('unavailable')
    expect((await sealProofCheck(F1, SEAL_PROOF, null)).state).toBe('unavailable')
  })
})

describe('who decided, in reader words', () => {
  it('a role and a date, never a name', () => {
    expect(roleWords('owner')).toBe('the owner')
    expect(roleWords('admin')).toBe('an admin')
    expect(roleWords('contributor')).toBe('a contributor')
    expect(roleWords(null, 'resolver:forecast_resolver@x')).toBe('the rule itself')
    expect(roleWords(null, 'operator:host')).toBe('a person')
    expect(actWords({ record_kind: 'confirmation', actor_role: 'admin', actor: 'user:abc', recorded_at: '2026-03-24T00:00:00Z' })).toBe(
      'confirmed by an admin on 2026-03-24',
    )
    expect(actWords({ record_kind: 'override', actor_role: 'owner', recorded_at: '2026-03-24T00:00:00Z' })).toBe('decided by the owner on 2026-03-24')
    expect(actWords({ record_kind: 'confirmation', actor: 'resolver:x' })).toBe('settled by the published value itself')
    expect(actWords({ record_kind: 'rejection', actor_role: 'admin' })).toBe('')
  })

  it('the reader sees the result and who confirmed it, then nothing of the person', () => {
    const f = forecast(1, { status: 'resolved', resolution: { id: 'r', outcome: 'no', resolved_at: '2026-03-24T00:00:00Z' } })
    const s = forecastState(f, {
      proposal: { outcome: 'no', recorded_at: '2026-03-23T00:00:00Z' },
      latest_act: { outcome: 'no', record_kind: 'confirmation', actor_role: 'contributor', actor: 'user:abc', recorded_at: '2026-03-24T00:00:00Z' },
    })
    expect(s.detail).toBe('Resolved no on 2026-03-24; confirmed by a contributor on 2026-03-24.')
    expect(s.detail).not.toMatch(/user:/)
  })

  it('a rejected proposal leaves the forecast open and says so', () => {
    const s = forecastState(
      forecast(1),
      {
        proposal: { outcome: 'yes', recorded_at: '2026-03-23T00:00:00Z' },
        latest_act: { outcome: 'yes', record_kind: 'rejection', actor_role: 'admin', recorded_at: '2026-03-24T00:00:00Z' },
      },
      null,
      new Date('2026-03-10T00:00:00Z'),
    )
    expect(s.kind).toBe('open')
    expect(s.detail).toBe('A proposed reading was not accepted by an admin on 2026-03-24; it stays open.')
  })
})

describe('a decision before it is sent', () => {
  const yes = queueEntry(1, 'yes')
  const vd = queueEntry(1, 'void')
  const nj = queueEntry(1, 'needs_judgement')

  it('needs a reason to reject, to void and for a judgement; confirms only the proposal’s own outcome', () => {
    expect(decisionProblem(yes, { verb: 'confirm', outcome: 'yes', reason: '' })).toBeNull()
    expect(decisionProblem(yes, { verb: 'confirm', outcome: 'no', reason: '' })).toMatch(/proposal’s own/)
    expect(decisionProblem(yes, { verb: 'reject', reason: '  ' })).toMatch(/a rejection needs its reason/)
    expect(decisionProblem(yes, { verb: 'reject', reason: 'wrong event' })).toBeNull()
    expect(decisionProblem(vd, { verb: 'confirm', outcome: 'void', reason: '' })).toMatch(/a void needs its reason/)
    expect(decisionProblem(vd, { verb: 'confirm', outcome: 'void', reason: 'nobody reported it' })).toBeNull()
    expect(decisionProblem(yes, { verb: 'judgement', outcome: 'no', reason: 'x' })).toMatch(/The rule decides this one/)
    expect(decisionProblem(nj, { verb: 'judgement', outcome: 'no', reason: '' })).toMatch(/a judgement needs its reason/)
    expect(decisionProblem(nj, { verb: 'confirm', outcome: 'needs_judgement' as never, reason: '' })).toMatch(/no outcome to confirm/)
  })

  it('the are-you-sure sentence says what closes and that nothing is ever edited', () => {
    const s = decisionSummary(yes, { verb: 'confirm', outcome: 'yes', reason: '' })
    expect(s).toMatch(/^Confirm the proposal: the forecast resolved "yes"\. This closes the forecast as "yes", and it counts in the track record\./)
    expect(s).toMatch(/cannot be edited later, and a wrong decision is corrected by a new record with a reason, both kept\./)
    expect(decisionSummary(yes, { verb: 'reject', reason: 'x' })).toMatch(/Nothing closes: the forecast stays open/)
    expect(decisionSummary(vd, { verb: 'confirm', outcome: 'void', reason: 'x' })).toMatch(/This voids the forecast: it will not be scored\./)
  })

  it('the route, the body and what happened', () => {
    expect(decisionPath('rec 1', 'confirm')).toBe('/v3/ledger/resolutions/rec%201/confirm')
    expect(decisionBody({ verb: 'confirm', outcome: 'yes', reason: ' ' })).toEqual({ outcome: 'yes' })
    expect(decisionBody({ verb: 'reject', reason: ' wrong ' })).toEqual({ reason: 'wrong' })
    expect(decisionBody({ verb: 'judgement', outcome: 'void', reason: 'x' })).toEqual({ outcome: 'void', reason: 'x' })
    const base = { record_id: 'r', record_kind: 'confirmation', question_id: 'q', slug: 's', actor_role: 'admin' }
    expect(decisionDone({ ...base, status: 'confirmed', outcome: 'yes' })).toBe(
      'Confirmed as an admin: the forecast resolved "yes". It now counts in the track record.',
    )
    expect(decisionDone({ ...base, status: 'rejected', outcome: 'yes', record_kind: 'rejection' })).toMatch(/^Rejected as an admin\. Nothing closed/)
    expect(decisionDone({ ...base, status: 'already_recorded', outcome: 'yes' })).toMatch(/already on record/)
    expect(refusalWords(403, { detail: { error: 'entered_a_number', message: 'you entered a number on this question' } })).toBe(
      'you entered a number on this question',
    )
    expect(refusalWords(409, null)).toMatch(/changed since you opened it/)
  })
})
