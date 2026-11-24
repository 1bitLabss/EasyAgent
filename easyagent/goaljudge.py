"""Goal judge: a second model reads the goal and the last answer at each keep-going decision.

Ported from Hermes' goal loop (hermes_cli/goals.py). The digest witness is the
cheap deterministic path; when a chat arms the judge, the judge replaces it:
after each finish a small model reads the goal, the agent's most recent
response, and the plan state, and returns one verdict:

- done     the deliverable actually exists; the run closes honestly.
- blocked  the goal cannot be satisfied as stated; pause, do not call it done.
- continue there is a concrete next step right now; run one more continuation.
- wait     progress is gated on async work; park without burning a budget go.

An unparseable or unknown verdict fails open to continue. The judge's own
health is counted: three parse failures in a row or five transport failures in
a row pause the run with an honest line instead of burning the whole budget.
The reply is one JSON line; the legacy {"done": bool} shape is still accepted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Ported numbers (goals.py lines 33-46): the judge gets a short leash and a
# big enough output budget that reasoning models cannot truncate the verdict.
JUDGE_TIMEOUT = 30.0
JUDGE_MAX_TOKENS = 4096
GOAL_SNIPPET_CHARS = 2000
RESPONSE_SNIPPET_CHARS = 4000
PLAN_SNIPPET_CHARS = 2000
MAX_CONSECUTIVE_PARSE_FAILURES = 3
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5
# A wait parks at most this long; a judge that names no length waits this long.
MAX_WAIT_SECONDS = 1800
DEFAULT_WAIT_SECONDS = 300

JUDGE_SYSTEM_PROMPT = (
    "You are a strict judge deciding whether an agent has finished a job for "
    "a person. You get the goal, the agent's most recent reply, the state of "
    "its checklist, and how many background workers it has running. Answer "
    "with one of four verdicts.\n\n"
    "DONE - the goal is fully satisfied:\n"
    "- The reply says the job is finished, OR\n"
    "- The reply clearly shows the deliverable was produced.\n"
    "DONE requires the deliverable to actually exist. If the reply only "
    "explains why the goal cannot be reached, the verdict is BLOCKED, not "
    "DONE.\n\n"
    "BLOCKED - the goal cannot be satisfied as stated:\n"
    "- The reply shows the goal is impossible, out of scope, or has no path "
    "to the deliverable, or refuses to invent a deliverable that cannot "
    "exist, OR\n"
    "- The reply says progress needs input from the person to go on.\n"
    "Return BLOCKED with the reason saying what blocks it. BLOCKED is a "
    "refusal, not a completion - never return BLOCKED for a goal that was "
    "achieved.\n\n"
    "WAIT - not done, but the next step is to wait for work running on its "
    "own, not to act again. Choose this only when the agent's progress is "
    "genuinely gated on that work:\n"
    "- Background workers are still running and the reply shows the agent is "
    "waiting on their results, with nothing else it can do now.\n"
    "- The agent says it is rate-limited, backing off, or must wait a fixed "
    "time - return the seconds in wait_for_seconds.\n"
    "A wait parks the run without spending a go; it resumes on its own when "
    "the time passes or a worker lands. Do not pick WAIT just because work "
    "remains - only when asking the agent again now would be pure busy-work.\n\n"
    "CONTINUE - not done, and there is a concrete next step the agent can "
    "take right now. This is the default when in doubt.\n\n"
    "Reply ONLY with a single JSON object on one line. Shapes:\n"
    '{"verdict": "done", "reason": "<one sentence>"}\n'
    '{"verdict": "blocked", "reason": "<one sentence>"}\n'
    '{"verdict": "continue", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_for_seconds": <int>, "reason": "<one sentence>"}\n'
    'The old shape {"done": <true|false>, "reason": "..."} is still accepted '
    "(true=done, false=continue)."
)

JUDGE_USER_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "The agent's most recent reply:\n{response}\n\n"
    "Checklist state:\n{plan_block}\n\n"
    "Background workers running right now: {children}\n\n"
    "Current time: {current_time}\n"
)

_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "… [truncated]"


def extract_json_object(raw: str) -> dict | None:
    """Best effort: strip code fences, parse the blob, else pull the first {...} out."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")  # peel off a leading json/JSON tag
        if nl != -1:
            text = text[nl + 1 :]
    try:
        data = json.loads(text)
    except Exception:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def parse_judge_response(raw: str) -> tuple[str, str, bool, dict | None]:
    """Parse the judge's reply, fail-open. Returns (verdict, reason, parse_failed, wait_directive).

    parse_failed flags unusable output so the caller can auto-pause after a few
    in a row. A wait with no target is downgraded to continue. Accepts the
    {"verdict": ...} shape and the legacy {"done": bool} shape.
    """
    if not raw:
        return "continue", "judge returned empty response", True, None
    data = extract_json_object(raw)
    if data is None:
        return "continue", f"judge reply was not JSON: {truncate(raw, 200)!r}", True, None

    reason = str(data.get("reason") or "").strip() or "no reason provided"
    verdict_raw = data.get("verdict")
    if isinstance(verdict_raw, str):
        verdict = verdict_raw.strip().lower()
    else:
        done_val = data.get("done")
        done = (
            done_val.strip().lower() in {"true", "yes", "1", "done"}
            if isinstance(done_val, str)
            else bool(done_val)
        )
        verdict = "done" if done else "continue"
    if verdict not in {"done", "blocked", "continue", "wait"}:
        verdict = "continue"
    if verdict != "wait":
        return verdict, reason, False, None

    def _first_int(*keys: str) -> int | None:
        for k in keys:
            try:
                iv = int(data[k]) if data.get(k) is not None else 0
            except (TypeError, ValueError):
                continue
            if iv > 0:
                return iv
        return None

    seconds = _first_int("wait_for_seconds", "seconds", "wait_seconds")
    if seconds is not None:
        return "wait", reason, False, {"seconds": min(seconds, MAX_WAIT_SECONDS)}
    # EasyAgent has no pid/session barrier registry; a wait that names a
    # process parks on the clock instead, and the worker's own result wakes
    # the chat through the queue anyway.
    sess = data.get("wait_on_session") or data.get("session_id") or data.get("wait_session")
    if isinstance(sess, str) and sess.strip():
        return "wait", reason, False, {"seconds": DEFAULT_WAIT_SECONDS}
    pid = _first_int("wait_on_pid", "pid", "wait_pid")
    if pid is not None:
        return "wait", reason, False, {"seconds": DEFAULT_WAIT_SECONDS}
    return "continue", f"{reason} (wait verdict had no target — continuing)", False, None


def plan_state_block(plan: dict) -> str:
    """The open and done step texts, so the judge sees what the checklist claims."""
    steps = [s for s in (plan or {}).get("steps") or [] if isinstance(s, dict)]
    open_lines = [f"- {s.get('text') or ''}" for s in steps if s.get("state") != "done"]
    done_lines = [f"- {s.get('text') or ''}" for s in steps if s.get("state") == "done"]
    parts = []
    parts.append("Open:\n" + ("\n".join(open_lines) if open_lines else "- (none)"))
    parts.append("Done:\n" + ("\n".join(done_lines) if done_lines else "- (none)"))
    return truncate("\n".join(parts), PLAN_SNIPPET_CHARS)


def build_user_prompt(
    goal: str, answer: str, plan: dict, children: int, current_time: str | None = None
) -> str:
    return JUDGE_USER_TEMPLATE.format(
        goal=truncate(goal or "", GOAL_SNIPPET_CHARS),
        response=truncate(answer or "", RESPONSE_SNIPPET_CHARS),
        plan_block=plan_state_block(plan),
        children=int(children or 0),
        current_time=current_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def resolve_judge_endpoint(store, bot: dict) -> tuple[dict | None, dict]:
    """``(endpoint, route)`` for the goal judge call. An explicit task route wins.

    ``bot["task_routes"]["goal_judge"]`` may name its own connection (and
    optionally a model, output budget, and timeout). An empty or missing route
    inherits the bot's main connection — Hermes' provider:auto semantics. An
    unknown route endpoint falls back to the main connection with a note in
    the judge log; it never crashes the call.
    """
    routes = bot.get("task_routes")
    route = routes.get("goal_judge") if isinstance(routes, dict) else None
    route = route if isinstance(route, dict) else {}
    wanted = str(route.get("endpoint_id") or "").strip()
    if wanted:
        try:
            endpoint = store.get_endpoint(wanted)
        except Exception:
            endpoint = None
        if endpoint is None:
            logger.info(
                "goal judge: route connection %s is not a saved connection — using the bot's main connection",
                wanted,
            )
        else:
            return endpoint, route
    return store.get_endpoint(bot.get("endpoint_id")), {}


async def call_judge(store, bot_id: str, *, goal: str, answer: str, plan: dict, children: int):
    """Ask the judge model for a verdict. Returns (verdict, reason, parse_failed, wait, transport_failed).

    Runs on the existing connection path (llm.complete): temperature 0, 4096
    output tokens, 30 s, unless the bot's goal_judge task route overrides the
    connection, model, output budget, or timeout. A transport error fails open
    to continue and counts as a transport failure; unusable output counts as a
    parse failure.
    """
    if not (goal or "").strip():
        return "skipped", "empty goal", False, None, False
    if not (answer or "").strip():
        return "continue", "empty response (nothing to evaluate)", False, None, False
    from easyagent import llm
    from easyagent import turn as turn_mod

    user_prompt = build_user_prompt(goal, answer, plan, children)
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        bot = store.get_bot(bot_id)
        endpoint, route = resolve_judge_endpoint(store, bot)
        if not endpoint:
            return "continue", "no connection for this bot", False, None, True
        from easyagent.app import chosen_model

        route_model = str(route.get("model") or "").strip()
        if route_model:
            model = route_model
        elif route and str(endpoint.get("id") or "") != str(bot.get("endpoint_id") or ""):
            # A different connection answers with its own saved model: the
            # bot's model may not exist there (Hermes' provider:auto semantics).
            model = str(endpoint.get("model") or "").strip() or None
        else:
            model = chosen_model(bot, endpoint)
        max_tokens = int(route.get("max_tokens") or JUDGE_MAX_TOKENS)
        timeout = float(route.get("timeout_seconds") or JUDGE_TIMEOUT)
        fallbacks = None
        if route and endpoint.get("fallback_ids"):
            # The route's own saved fallbacks, tried in order after it (the
            # same plumbing the main calls use). Total failure still fails
            # open to continue through the transport-failure counter.
            from easyagent import providers

            fallbacks = providers.chain_for(store, endpoint)[1:] or None
        raw = await llm.complete(
            base_url=endpoint["base_url"],
            api_key=endpoint.get("api_key") or None,
            model=model,
            messages=messages,
            timeout=timeout,
            temperature=0,
            max_tokens=max_tokens,
            tools=False,
            fallbacks=fallbacks,
        )
    except (asyncio.CancelledError, turn_mod.TurnCancelled):
        raise  # a Stop is not a transport failure; let the turn unwind
    except Exception as exc:
        logger.info("goal judge: call failed (%s) — falling through to continue", exc)
        return "continue", f"judge error: {type(exc).__name__}", False, None, True
    verdict, reason, parse_failed, wait = parse_judge_response(raw or "")
    logger.info("goal judge: verdict=%s reason=%s", verdict, truncate(reason, 120))
    return verdict, reason, parse_failed, wait, False


def apply_health(plan: dict, parse_failed: bool, transport_failed: bool) -> str:
    """Update the judge-health counters on the plan dict. Returns a pause line or "".

    Three parse failures in a row or five transport failures in a row pause
    the run; any usable reply resets both counters. A transport failure
    resets the parse counter (a flaky line is the network's fault, not the
    judge model's), and a parse failure resets the transport counter (the
    call got through). The caller saves the plan.
    """
    if transport_failed:
        plan["judge_transport_fails"] = int(plan.get("judge_transport_fails") or 0) + 1
        plan["judge_parse_fails"] = 0
    elif parse_failed:
        plan["judge_parse_fails"] = int(plan.get("judge_parse_fails") or 0) + 1
        plan["judge_transport_fails"] = 0
    else:
        plan["judge_parse_fails"] = 0
        plan["judge_transport_fails"] = 0
    if int(plan.get("judge_transport_fails") or 0) >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
        n = int(plan["judge_transport_fails"])
        return (
            f"Stopped: the judge could not reach the model {n} times in a row. "
            "Check the connection, then say continue."
        )
    if int(plan.get("judge_parse_fails") or 0) >= MAX_CONSECUTIVE_PARSE_FAILURES:
        n = int(plan["judge_parse_fails"])
        return (
            f"Stopped: the judge sent back answers it could not read {n} times in a row. "
            "Say continue to keep going."
        )
    return ""
