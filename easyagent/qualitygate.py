"""Quality gates: deterministic shell checks that run before the goal judge.

Ported from Hermes' goal loop (hermes_cli/goals.py 51-57, 97-111, 343-414,
1267-1359). A gate is a shell command that must pass before a goal may be
declared done. Gates live on the chat's plan (``plan["gates"]``) and run at
every keep-going boundary, in list order, BEFORE the judge: a failing gate is
deterministic evidence the goal is not done, so the remaining gates and the
judge are skipped entirely and the gate's bounded output becomes the
continuation prompt.

Retries are across turns, not within one: each failing boundary counts one
attempt and continues the run; past the retry cap the run pauses with an
honest line. A passing gate resets its attempt count. Every boundary
re-executes the command — no cached replay of a recorded failure.

Gates only apply when the chat has armed the judge (plan fence: judge on);
the deterministic digest witness path never sees them.
"""

from __future__ import annotations

import subprocess

# Ported numbers (goals.py 51-57).
DEFAULT_GATE_TIMEOUT_SECONDS = 300
DEFAULT_GATE_MAX_RETRIES = 3
# Bounded tail of a failed gate's combined stdout/stderr fed back to the agent.
GATE_OUTPUT_TAIL_CHARS = 3000

# Adapted from Hermes goals.py 97-111: the failing gate's output drives the
# next turn in place of the judge's reason.
GATE_FAILED_PROMPT_TEMPLATE = (
    "[Continuing toward your standing goal — a quality gate failed]\n"
    "Goal: {goal}\n\n"
    "The quality gate command below must pass before this goal can be "
    "declared done, and it just failed (attempt {attempt}/{max_retries}):\n"
    "  $ {command}\n"
    "Exit code: {exit_code}\n"
    "Output (tail):\n"
    "```\n"
    "{output}\n"
    "```\n\n"
    "Fix the underlying problem so this gate passes, then re-run it to "
    "confirm. Do not declare the goal complete while any gate fails. If the "
    "gate itself is wrong or cannot pass, say so clearly and stop."
)


def new_gate(command: str, *, timeout_seconds: int | None = None, max_retries: int | None = None) -> dict:
    """One gate record, shaped the way it is persisted on the plan."""
    return {
        "command": (command or "").strip(),
        "timeout_seconds": int(timeout_seconds) if timeout_seconds else DEFAULT_GATE_TIMEOUT_SECONDS,
        "max_retries": int(max_retries) if max_retries else DEFAULT_GATE_MAX_RETRIES,
        "attempts": 0,
        "last_exit_code": None,
    }


def add_gate(plan: dict, command: str, *, timeout_seconds: int | None = None, max_retries: int | None = None) -> dict:
    """Append a gate to the plan dict. The caller saves the plan."""
    gate = new_gate(command, timeout_seconds=timeout_seconds, max_retries=max_retries)
    if not gate["command"]:
        raise ValueError("gate command is empty")
    gates = plan.get("gates")
    if not isinstance(gates, list):
        gates = []
    gates.append(gate)
    plan["gates"] = gates
    return gate


def remove_gate(plan: dict, index_1based: int) -> str:
    """Remove a gate by 1-based index. Returns the removed command."""
    gates = plan.get("gates") or []
    if not (1 <= int(index_1based) <= len(gates)):
        raise ValueError("no gate with that number")
    gate = gates.pop(int(index_1based) - 1)
    plan["gates"] = gates
    return str(gate.get("command") or "")


def clear_gates(plan: dict) -> int:
    """Remove every gate. Returns the previous count."""
    gates = plan.get("gates") or []
    plan["gates"] = []
    return len(gates)


def render_gates(plan: dict | None) -> str:
    """The gate list, one line each, with the last known status."""
    gates = list((plan or {}).get("gates") or [])
    if not gates:
        return "(no quality gates — use gate add <command> to require one)"
    lines = []
    for i, gate in enumerate(gates, start=1):
        code = gate.get("last_exit_code")
        status = ""
        if code == 0:
            status = " ✓ passing"
        elif code is not None:
            status = (
                f" ✗ failing (exit {code}, attempt {int(gate.get('attempts') or 0)}"
                f"/{int(gate.get('max_retries') or DEFAULT_GATE_MAX_RETRIES)})"
            )
        lines.append(f"- {i}. $ {gate.get('command') or ''}{status}")
    return "\n".join(lines)


def run_gate(gate: dict, *, cwd: str | None = None) -> tuple[bool, int, str]:
    """Run one gate through the shell. Returns ``(passed, exit_code, output_tail)``.

    utf-8/replace: gate output is arbitrary bytes; strict codepage decoding of
    one unmappable byte kills the reader and the tail the agent needs arrives
    empty. A timeout kills the process and counts as exit code -1.
    """
    try:
        proc = subprocess.run(
            str(gate.get("command") or ""),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(gate.get("timeout_seconds") or DEFAULT_GATE_TIMEOUT_SECONDS)),
            cwd=cwd or None,
            check=False,
        )
        combined = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode == 0, proc.returncode, combined[-GATE_OUTPUT_TAIL_CHARS:]
    except subprocess.TimeoutExpired as exc:
        out = "".join(
            c if isinstance(c, str) else c.decode("utf-8", "replace")
            for c in (exc.stdout, exc.stderr)
            if c
        )
        tail = out + f"\n[gate timed out after {gate.get('timeout_seconds')}s]"
        return False, -1, tail[-GATE_OUTPUT_TAIL_CHARS:]
    except Exception as exc:  # a gate that cannot run is a failing gate, not a crash
        return False, -1, f"[gate could not run: {type(exc).__name__}: {exc}]"


def check_gates(plan: dict, *, goal: str = "", cwd: str | None = None) -> dict | None:
    """Run the gates in order. Returns a decision dict on the first failure.

    A passing gate resets its attempts and the next gate runs. The first
    failing gate short-circuits: the remaining gates and the judge are
    skipped. Each failing boundary counts one attempt; past ``max_retries``
    the decision is a pause instead of a continuation. Mutates the gate
    records on ``plan``; the caller persists the plan.
    """
    gates = [g for g in (plan.get("gates") or []) if isinstance(g, dict) and str(g.get("command") or "").strip()]
    plan["gates"] = gates
    for gate in gates:
        passed, exit_code, tail = run_gate(gate, cwd=cwd)
        gate["last_exit_code"] = exit_code
        gate["last_output_tail"] = tail
        if passed:
            gate["attempts"] = 0
            continue
        gate["attempts"] = int(gate.get("attempts") or 0) + 1
        max_retries = int(gate.get("max_retries") or DEFAULT_GATE_MAX_RETRIES)
        if gate["attempts"] > max_retries:
            return {
                "kind": "exhausted",
                "command": str(gate.get("command") or ""),
                "exit_code": exit_code,
                "attempts": gate["attempts"],
                "max_retries": max_retries,
                "pause_line": (
                    f"Stuck: quality gate still failing after {max_retries} retries: "
                    f"$ {gate.get('command')} (exit {exit_code}). "
                    "Fix it or remove the gate, then send continue."
                ),
            }
        return {
            "kind": "failed",
            "command": str(gate.get("command") or ""),
            "exit_code": exit_code,
            "output": tail,
            "attempts": gate["attempts"],
            "max_retries": max_retries,
            "prompt": GATE_FAILED_PROMPT_TEMPLATE.format(
                goal=goal or "(the standing goal)",
                command=gate.get("command"),
                exit_code=exit_code,
                attempt=gate["attempts"],
                max_retries=max_retries,
                output=tail or "(no output)",
            ),
        }
    return None


def failing_gate(plan: dict | None) -> dict | None:
    """The first gate whose last recorded run failed, or None."""
    for gate in (plan or {}).get("gates") or []:
        if isinstance(gate, dict) and gate.get("last_exit_code") not in (None, 0):
            return gate
    return None
