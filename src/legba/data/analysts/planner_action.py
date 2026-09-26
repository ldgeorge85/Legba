# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a planner's emitted ACTION is read — and what happens when it is only
narrated.

THE DEFECT THIS MODULE FIXES (live, three dispatched runs, 2026-09-07/08)
------------------------------------------------------------------------
The GATHER protocol (``inline_target._GATHER_SYSTEM_SUFFIX``) asks for::

    {"tool": "<name>", "args": {...}}

The dispatched-assignment block asked for the same call in PYTHON syntax::

    web_evidence(query="<your query>", hypothesis_id="<uuid>")

A core-plane model handed both wrote neither. The 2026-09-07 15:37Z run emitted,
as its FINDING::

    Attempt to fetch external evidence.
    {
      "action": "web_evidence",
      "query": "Israel Palestine conflict September 2026 news",
      "hypothesis_id": "b904fc78-245a-4eb8-bfdf-517c34db6173"
    }

— the right tool, the right query, the right id, in the wrong envelope and at
the wrong phase. ``_gather`` reads ``parsed["tool"]``, saw ``None``, and folded
the turn back as ``unrecognized``; the synthesis call then re-emitted the same
object, ``_coerce_finding`` fell to its unstructured branch, and the run
PUBLISHED the narration as a finding — title and body both "Attempt to fetch
external evidence." — at faithfulness 1.00, because a claim about what the
model intends to do is trivially faithful to the evidence and says nothing
about the world. The 09-08 03:37Z run repeated it ("Attempt tool use."). The
09-08 15:37Z run narrated in a shape ``strip_tool_plan_preamble`` DID
recognise, which emptied the body and hard-failed the run.

TWO RULES, AND THEY ARE DELIBERATELY ASYMMETRIC
-----------------------------------------------
1. **Read the action generously.** :func:`normalize_planner_action` accepts the
   flat ``{"action": ..., "query": ...}`` shape the model actually emits, and
   the ``{"name": ..., "arguments": {...}}`` shape the OpenAI tool grammar
   trains it on, and folds each into the canonical ``(tool, args)`` the loop
   already executes. The canonical shape is returned untouched, so every
   existing caller is byte-for-byte unchanged. An intent this legible is not a
   parse failure; refusing it is.

2. **Refuse the narration absolutely.** :func:`narrated_action_in` detects a
   finding BODY that is an unexecuted action — narration plus (or nothing but)
   an object naming a tool. There is no generous reading of that: the model is
   describing a call it never made, and publishing it puts a sentence about the
   model's own intentions into the analytic record. The caller degrades loud
   (:data:`NARRATED_WITHOUT_ACTION`) rather than salvaging it.

Rule 1 makes rule 2 rare. Rule 2 is what makes rule 1 safe to be generous.

THE THIRD SHAPE (live, three hard fails, 2026-09-08/09) — THE FINAL TURN IS A
TOOL CALL
---------------------------------------------------------------------------
Rules 1 and 2 both assume the model reaches SYNTHESIS and writes something it
believes is a finding. It does not always. Every ``corpus_researcher`` run
from 09-08 15:37Z on hard-failed with the identical string::

    model returned a finding contract with no readable body
    (title='Assessment for target')

REPRODUCED on the live core plane (gpt-oss-120b, the endpoint the runtime
uses), rebuilding the 09-09 03:37Z synthesis prompt through the same
composition path — ``with_grounding_clause(with_preamble_if_absent(...))``,
verified BYTE-IDENTICAL against the last successful run's persisted
``prompt_rendered``. Five of six samples returned, as the whole completion::

    {"tool":"web_evidence","args":{"query":"…","hypothesis_id":"b904fc78-…"}}
    {"tool": "search_corpus", "args": {"query": "Palestine Israel", "size": 10}}

Not a narration, not a fenced envelope, not an empty body: a well-formed
GATHER protocol object, emitted at a turn where no gather loop is listening.
``_coerce_finding`` parses it (it IS valid JSON, and a dict), finds no
``title`` and no ``body``, falls the title back to the ``Assessment for
<target>`` placeholder, and raises on the empty body — the exact live string,
character for character.

AND IT IS THE PROMPT'S OWN FAULT. The synthesis call reuses the SAME system
prompt as GATHER (``_effective_system_prompt``), which since the 09-07/09-08
re-stamps carries hard, correct, unconditional imperatives to emit that
object — *"EMIT EVERY ACTION AS A TOOL CALL"*, *"a turn whose whole content is
this object"*, and, in the dispatched-assignment block, *"you MUST emit this
as a TOOL CALL … and then read what comes back"*. Nothing in the synthesis
turn ever said gathering was over. The model was obeying its instructions;
the instructions did not have a last page.

:data:`GATHERING_CLOSED_CLAUSE` is that last page, appended to the SYNTHESIS
prompt only (never to the gather rounds, which still need the protocol), and
:func:`unreadable_answer_step` is the receipt for when a model ignores it
anyway — naming the reason and the raw head, so the next occurrence is
diagnosable from the trace instead of from a reproduction like this one.
"""

from __future__ import annotations

import json
import re
from typing import Any, Container, Mapping

from .output_contract import OutputContractError, extract_json_object

__all__ = [
    "DISPATCH_SCOPED_TOOLS",
    "FINAL_ANSWER_UNREADABLE",
    "GATHERING_CLOSED_CLAUSE",
    "NARRATED_WITHOUT_ACTION",
    "dispatched_hypothesis_id",
    "final_answer_is_a_tool_call",
    "narrated_action_in",
    "narration_guard_step",
    "process_narration_in",
    "normalize_planner_action",
    "refuse_narration",
    "stamp_dispatch_hypothesis_id",
    "synthesis_prompt",
    "unreadable_answer_step",
]

#: Tools whose landed rows inherit a DESK's scope from the dispatching
#: question, and which therefore take the assignment's ``hypothesis_id``. This
#: is the tool's own contract, not a policy: ``web_evidence`` is the one tool
#: that resolves a target descriptor's ``scope.geo`` off that id and writes it
#: onto every row it lands. A read tool has nothing to inherit.
DISPATCH_SCOPED_TOOLS: tuple[str, ...] = ("web_evidence",)

#: The receipt a run leaves when the planner described a tool call instead of
#: making one. Stamped as an ``intermediate_steps`` step kind AND carried in the
#: ``OutputContractError`` message, so the rate is countable from traces and
#: from ``analyst_traces.error_payload`` alike (a hard-failed run's steps are
#: not always persisted; its error payload always is).
NARRATED_WITHOUT_ACTION = "planner_narrated_without_action"

#: The receipt a run leaves when its FINAL answer could not be read as a
#: finding at all. Distinct from :data:`NARRATED_WITHOUT_ACTION`: that one is a
#: body that says the wrong thing, this one is the absence of a body. Stamped
#: as an ``intermediate_steps`` kind so the rate is countable from traces.
FINAL_ANSWER_UNREADABLE = "final_answer_unreadable"

#: How much of an unreadable completion the receipt quotes. Long enough to name
#: the shape at a glance (all three live cases were under 180 chars, so this
#: holds them whole), short enough that a runaway completion cannot bloat the
#: step.
_RAW_HEAD_CHARS = 600

#: Appended to the SYNTHESIS prompt — and ONLY to it. The gather rounds still
#: need the protocol object and must never see this.
#:
#: WHY IT EXISTS: see the module docstring. The system prompt tells the model,
#: correctly and unconditionally, to emit every action as a bare protocol
#: object; nothing told it when to stop. On the live plane that produced a
#: ``{"tool": …, "args": {…}}`` object as the FINAL answer in five of six
#: samples. This says the turn has changed.
#:
#: It states the finding's REQUIRED keys rather than restating the whole
#: schema — the descriptor already carries the full contract, and two copies
#: of a schema drift the moment one is edited.
GATHERING_CLOSED_CLAUSE = (
    "GATHERING IS CLOSED — THIS TURN IS THE FINDING.\n"
    "Every tool round this run had has already been taken and everything it "
    "returned is printed above. No gathering loop is listening to this turn: "
    "a protocol object here — {\"tool\": …, \"args\": {…}} — calls nothing, "
    "fetches nothing, and is not a finding however well-formed it is. It is a "
    "run that did the research and reported none of it.\n"
    "Respond with the STRICT JSON FINDING CONTRACT your instructions specify "
    "and nothing else: a single object carrying at least \"title\" and "
    "\"body\", plus the \"confidence\", \"evidence\" and \"tags\" fields "
    "described above. No prose before or after it, no code fences.\n"
    "If you wanted another tool call and cannot make one, that is itself part "
    "of the finding: say in the body what you would still need and why, cite "
    "what you DID gather, and lower your confidence. An honest partial answer "
    "is a complete finding. An unmade call is not an answer at all."
)

#: The keys a flat action object uses to NAME its tool, in preference order.
#: ``tool`` is the protocol's own key and is checked first so a canonical
#: object never takes a different branch.
_NAME_KEYS = ("tool", "action", "name", "tool_name", "function")

#: The keys a nested action object uses to carry its ARGUMENTS. Checked in
#: order; the first that holds a mapping wins.
_ARG_KEYS = ("args", "arguments", "parameters", "input", "tool_input")

#: Keys that are envelope metadata, never tool arguments — dropped when a flat
#: object's remaining keys are folded into ``args``.
_ENVELOPE_KEYS = frozenset(_NAME_KEYS) | frozenset(_ARG_KEYS) | frozenset(
    {"thought", "reasoning", "rationale", "done", "type"}
)

#: The tool argument that carries a dispatched question's scope. Named here
#: rather than at the call site because two modules stamp and read it.
HYPOTHESIS_ARG = "hypothesis_id"

#: A JSON object embedded in prose, matched non-greedily from the first ``{``.
#: Used ONLY by :func:`narrated_action_in`, which is a detector — a false
#: negative costs a slightly worse error message, never a bad publish, because
#: the naming check below is the real gate.
_EMBEDDED_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

#: A body longer than this is analysis, whatever else it also contains. The
#: three live narrations were 26, 17 and 182 characters; the cap is two orders
#: of magnitude above them and still far below a real finding body.
_NARRATION_BODY_CAP = 2_000

#: A body that is ONLY an announcement of a tool interaction has to be short —
#: this is the length at which a body stops being a single announcement and
#: starts being prose that might carry an argument. Well above the live 17- and
#: 26-character cases, well below any finding that states what it found.
_PROCESS_NARRATION_CAP = 200

#: An attempt/call/fetch verb, then — inside the SAME sentence — a word naming
#: the INSTRUMENT rather than the world. Both halves are required: the verb
#: alone would catch "Attempts to broker a ceasefire failed", and the
#: instrument alone would catch a finding about a search engine.
_PROCESS_NARRATION_RE = re.compile(
    r"^\W*(?:attempt|call|invoke|invok|fetch|retriev|query|search|us|try|"
    r"perform|execut|proceed|run|issu)\w*\b[^.\n!?]{0,120}?\b"
    r"(?:tool|tools|call|calls|query|queries|search|searches|evidence|"
    r"endpoint|api|web_evidence|web_search|search_corpus)\b",
    re.IGNORECASE,
)


def _named_tool(obj: Mapping[str, Any]) -> str | None:
    """The tool name an action object names, or ``None``.

    ``function`` may itself be an object (``{"function": {"name": ...}}``) —
    the OpenAI wire shape — so that one indirection is followed.
    """
    for key in _NAME_KEYS:
        value = obj.get(key)
        if isinstance(value, Mapping):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalize_planner_action(
    parsed: Any, recognized: Container[str],
) -> tuple[str, Mapping[str, Any]] | None:
    """Read one parsed planner turn as ``(tool_name, args)``, or ``None``.

    Accepts, in this order:

      * ``{"tool": name, "args": {...}}`` — the protocol shape, returned with
        its own ``args`` mapping unchanged (the overwhelmingly common case, and
        byte-for-byte what the loop read before this function existed);
      * ``{"action": name, "query": ..., "hypothesis_id": ...}`` — THE LIVE
        SHAPE: a flat object whose non-envelope keys ARE the arguments;
      * ``{"name": name, "arguments": {...}}`` / ``parameters`` / ``input`` —
        the tool-grammar shapes the plane's models are trained on.

    Returns ``None`` when the object names nothing, names something
    ``recognized`` does not contain, or is not a mapping at all — so the
    caller's existing "unrecognized" nudge still fires for a genuine miss, and
    a ``{"done": true}`` object (which names no tool) is untouched.
    """
    if not isinstance(parsed, Mapping):
        return None
    name = _named_tool(parsed)
    if not name or name not in recognized:
        return None
    for key in _ARG_KEYS:
        value = parsed.get(key)
        if isinstance(value, Mapping):
            return name, value
        if value is not None:
            # An arguments key that is present and is NOT a mapping is a
            # malformed call, not a flat one — reading the siblings as its
            # arguments would invent a call the model did not make. Unreadable,
            # exactly as it was before this function existed.
            return None
    # Flat shape: everything that is not envelope metadata is an argument.
    return name, {
        k: v for k, v in parsed.items() if k not in _ENVELOPE_KEYS
    }


def dispatched_hypothesis_id(sink: Any) -> str | None:
    """The ASSIGNED question's id, read off the run's filled question sink.

    ``dispatched_question.fill_question_sink`` marks exactly one entry
    ``dispatched: True`` on an assigned run and none at all on a self-selected
    one, so this is ``None`` for every run that was not given a job — which is
    every run that existed before the dispatch path did.
    """
    if not isinstance(sink, Mapping):
        return None
    for entry in sink.values():
        if isinstance(entry, Mapping) and entry.get("dispatched"):
            qid = entry.get("id")
            if isinstance(qid, str) and qid.strip():
                return qid.strip()
    return None


def stamp_dispatch_hypothesis_id(
    args: Mapping[str, Any], hypothesis_id: str | None,
) -> tuple[dict[str, Any], bool]:
    """Ensure a dispatched run's ``web_evidence`` call carries its question id.

    Returns ``(args, stamped)``. THE REASON THIS IS NOT LEFT TO THE PROMPT: the
    id is the ONLY thing that carries landed evidence to the desk that asked
    (``research_tools._resolve_dispatch``: hypothesis -> target -> descriptor
    scope.geo), and the live 09-08 15:37Z run landed five signals with
    ``geo {}`` because nothing in its prompt printed one. Asking a model to
    transcribe a UUID correctly, every time, as a precondition for the evidence
    being readable at all, is a reachability guarantee resting on transcription.
    The run KNOWS its assignment; the loop stamps it.

    An id the planner supplied is never overwritten — a run investigating a
    different question it can name is making a real claim about what its
    evidence bears on, and silently re-pointing it would be worse than the bug.
    """
    out = dict(args or {})
    if not hypothesis_id:
        return out, False
    existing = out.get(HYPOTHESIS_ARG)
    if isinstance(existing, str) and existing.strip():
        return out, False
    out[HYPOTHESIS_ARG] = hypothesis_id
    return out, True


def narrated_action_in(body: str, recognized: Container[str]) -> str | None:
    """The tool a finding BODY describes calling but never called, or ``None``.

    True of both live shapes: a body that is nothing but an action object, and
    a body that is one narrating sentence followed by one
    (``"Attempt to fetch external evidence.\\n{...}"``). The naming check is the
    gate — the object has to name a tool this run could actually have called —
    so an analytic body that merely quotes JSON is untouched.

    Bounded deliberately: only a body SHORT enough to be pure narration is
    considered. A real finding that ends with a JSON appendix is analysis with
    an appendix, and this must never eat one.
    """
    if not body or len(body) > _NARRATION_BODY_CAP:
        return None
    match = _EMBEDDED_OBJECT_RE.search(body)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    # A finding envelope is not an action, even though both are objects: it
    # names no tool, so ``_named_tool`` returns None and this falls through.
    name = _named_tool(parsed)
    if not name or name not in recognized:
        return None
    return name


def process_narration_in(body: str) -> str | None:
    """The body when it is NOTHING but an announcement of a tool interaction,
    else ``None``.

    THE SECOND LIVE SHAPE. 09-07 15:37Z carried the action object and is caught
    above; 09-08 03:37Z published a body of exactly ``"Attempt tool use."`` —
    the same failure with the object left off, and it shipped as a finding
    whose TITLE was that sentence. ``output_contract.strip_tool_plan_preamble``
    does not reach it: its openers are first-person plan forms ("We will…",
    "Let me…"), and this is a bare imperative.

    Two conditions, both required, so this cannot eat a real finding:

      * the whole body is SHORT — a single announcement, under
        :data:`_PROCESS_NARRATION_CAP` characters. An assigned run that
        actually did its job says what the corpus held and what the web
        returned, and cannot say it in a clause.
      * it matches :data:`_PROCESS_NARRATION_RE` — an attempt/call/fetch verb
        followed, within one sentence, by the VOCABULARY OF THE INSTRUMENT
        (tool, call, query, search, evidence…). "Attempt tool use." matches;
        "Attempts to broker a ceasefire failed" does not, because nothing in
        it names an instrument.
    """
    text = (body or "").strip()
    if not text or len(text) > _PROCESS_NARRATION_CAP:
        return None
    return text if _PROCESS_NARRATION_RE.match(text) else None


def narration_guard_step(
    body: str, recognized: Container[str], hypothesis_id: str,
) -> dict[str, Any] | None:
    """The REFLECT-phase receipt for an ASSIGNED run that narrated its action,
    or ``None`` when the body is a real finding.

    Returns the ``intermediate_steps`` step the caller appends before raising,
    with the raiser's message under ``detail`` so the step and the
    ``OutputContractError`` cannot drift apart — the step is the trace receipt,
    the message is the ``analyst_traces.error_payload`` one, and a hard-failed
    run does not reliably persist both.

    WHY AN ASSIGNED RUN AND NOT EVERY RUN. An assignment is the one context in
    which "this run was told to call a tool" is a fact the RUNTIME holds rather
    than an inference from the prose. Outside it, a body that happens to embed
    a tool-shaped object might be analysis quoting a payload, and the house
    posture on ambiguity is degrade-not-fabricate — keep the prose. Inside it,
    there is no ambiguity left to respect: the run had one job, the body is the
    job\'s description instead of its result, and publishing it would answer a
    desk\'s standing question with a sentence about the model\'s intentions.

    Two shapes, one receipt, distinguished by ``reason`` so a trace reader can
    tell the 09-07 failure (an action object left in the body) from the 09-08
    one (the same intent with the object left off).
    """
    tool = narrated_action_in(body, recognized)
    if tool:
        reason, detail = "unexecuted_action", (
            f"the finding body describes a {tool} call the run never made"
        )
    else:
        narration = process_narration_in(body)
        if not narration:
            return None
        tool, reason = None, "process_narration"
        detail = (
            "the finding body announces a tool interaction and reports nothing "
            f"about the world ({narration!r})"
        )
    return {
        "phase": "reflect",
        "kind": NARRATED_WITHOUT_ACTION,
        "tool": tool,
        "reason": reason,
        "hypothesis_id": hypothesis_id,
        "detail": (
            f"{NARRATED_WITHOUT_ACTION}: {detail} "
            f"(assignment {hypothesis_id})"
        ),
    }


def refuse_narration(
    body: str,
    recognized: Container[str],
    hypothesis_id: str | None,
    steps: list[dict[str, Any]],
) -> None:
    """Refuse an ASSIGNED run whose finding BODY is a narrated action: append
    the receipt to ``steps`` and raise. A no-op for every other run.

    Extracted from ``inline_target``'s REFLECT phase (2026-09-09) so the
    predicate, its receipt and the raise it justifies sit together — three
    lines at the call site were carrying a policy decision (WHICH runs are
    refused, and that a refusal is an ``OutputContractError`` rather than a
    degrade) that belongs beside :func:`narration_guard_step`.

    ``hypothesis_id`` falsy ⇒ the run was not assigned ⇒ nothing to refuse:
    outside an assignment a tool-shaped body might be analysis quoting a
    payload, and the house posture on ambiguity is degrade-not-fabricate.
    """
    if not hypothesis_id:
        return
    step = narration_guard_step(body, recognized, hypothesis_id)
    if step is None:
        return
    steps.append(step)
    raise OutputContractError(step["detail"])


def synthesis_prompt(user_prompt: str, *, gathered: bool) -> str:
    """The user prompt for the SYNTHESIS call.

    ``gathered=False`` returns it untouched — a single-shot analyst was never
    told the gathering protocol, so it has nothing to be released from and its
    prompt stays byte-identical. ``gathered=True`` appends
    :data:`GATHERING_CLOSED_CLAUSE`.

    A function rather than an inline f-string at the call site because the
    condition IS the contract: the clause is the counterweight to the gather
    suffix, and it must reach exactly the runs that got the suffix.
    """
    if not gathered:
        return user_prompt
    return f"{user_prompt}\n\n{GATHERING_CLOSED_CLAUSE}\n"


def final_answer_is_a_tool_call(raw: str) -> str | None:
    """The tool the model's FINAL answer asks for, when the whole answer is a
    GATHER protocol object rather than a finding. ``None`` otherwise.

    Names the tool through :func:`_named_tool` — the same reader
    :func:`normalize_planner_action` uses — so every envelope the GATHER loop
    executes is recognised here too: the canonical ``{"tool": …, "args": …}``,
    the flat ``{"action": …, "query": …}``, and the two tool grammars. Not
    gated on a ``recognized`` set, deliberately: this is a post-mortem, and a
    final turn asking for a tool that does not exist is the same defect wearing
    a worse name — the receipt should say which name.

    Strictly a DIAGNOSIS: it names the shape for the receipt. A run whose final
    turn is a tool call has gathered nothing further and has written no
    finding, so there is nothing here to salvage and nothing is salvaged — a
    fabricated body would be worse than the loud failure.
    """
    text = (raw or "").strip()
    # The completion must OPEN with the object — a finding whose prose quotes a
    # payload is not a tool call, whatever it contains. Past that opening brace
    # the object is read the string-aware way, so trailing text (the live shape
    # emitted "[waiting]" after it) does not make the receipt illegible.
    if not text.startswith("{"):
        return None
    blob = extract_json_object(text)
    if blob is None:
        return None
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    # A real finding names no tool; a protocol object carries no body. Both
    # tests, so an analysis object that happens to carry a ``name`` key is
    # never mistaken for a call.
    if parsed.get("title") or parsed.get("body"):
        return None
    return _named_tool(parsed)


def unreadable_answer_step(
    raw: str, error: BaseException, *, hypothesis_id: str | None = None,
) -> dict[str, Any]:
    """The REFLECT receipt for a run whose final answer was not a finding.

    Replaces the bare ``{"phase": "reflect", "kind":
    "output_contract_violation"}`` step, which recorded THAT the contract broke
    and nothing about HOW — and which, until the failure trace learned to
    persist steps at all, was itself dropped on the floor.

    Carries three things a post-mortem needs and previously had to be obtained
    by reproducing the run against the live plane: ``reason`` (the shape,
    classified), ``raw_head`` (what the model actually returned, bounded), and
    ``raw_chars`` (so a truncated head is never mistaken for a short
    completion). ``hypothesis_id`` is present only on an assigned run.
    """
    tool = final_answer_is_a_tool_call(raw)
    if tool:
        reason = "final_turn_is_a_tool_call"
        detail = (
            f"the model's final answer was a {tool} tool call, not a finding — "
            "gathering was already closed, so it called nothing"
        )
    elif not (raw or "").strip():
        reason, detail = "empty_completion", "the model returned no text at all"
    else:
        reason, detail = "unreadable_body", str(error)
    step: dict[str, Any] = {
        "phase": "reflect",
        "kind": FINAL_ANSWER_UNREADABLE,
        "reason": reason,
        "tool": tool,
        "raw_chars": len(raw or ""),
        "raw_head": (raw or "").strip()[:_RAW_HEAD_CHARS],
        "detail": f"{FINAL_ANSWER_UNREADABLE}: {detail}",
    }
    if hypothesis_id:
        step["hypothesis_id"] = hypothesis_id
    return step
