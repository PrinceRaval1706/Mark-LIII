"""
actions/execute_command.py — run a terminal command under the NEXUS safety layer.

WHY THIS TOOL EXISTS
    The coding agent and git tools in this repo run commands too, but they only
    ever fire known-safe invocations they built themselves. This tool is the
    open one: the model hands over an arbitrary command the user asked for, and
    core/cmd_runner.py decides what actually happens:

      SAFE                  → runs immediately        (git status, dir, pytest…)
      REQUIRES_CONFIRMATION → parked behind the HUD confirm gate (a human
                              presses CONFIRM — the model cannot)
      BLOCKED               → refused outright        (rm -rf /, format, …)

    Every run is written to the activity log with its tier and exit code, and a
    running command can be cancelled with the cancel_command tool. The model
    must NEVER claim a command ran — it only sees this tool's output.
"""

from __future__ import annotations

import os

from core import cmd_runner
from core.confirm import pending_title as _pending_title


def execute_command(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    p = parameters or {}
    command = str(p.get("command", "")).strip()
    if not command:
        return ("Tell me the exact terminal command to run — for example "
                "`dir` to list this directory.")

    cwd = str(p.get("cwd", "")).strip() or None
    try:
        timeout = int(p.get("timeout") or cmd_runner.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = cmd_runner.DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, 600))

    tier = cmd_runner.classify(command, cwd)

    if player is not None:
        try:
            player.write_log(f"[cmd:{tier}] {command[:160]}")
        except Exception:
            pass

    if tier == cmd_runner.BLOCKED:
        return cmd_runner.classify_reason(command, tier)

    if tier == cmd_runner.SAFE:
        result = cmd_runner.run_command(command, cwd=cwd, timeout=timeout)
        if player is not None:
            try:
                player.write_log(
                    f"[cmd] exit={result.get('exit_code')} "
                    f"in {result.get('duration', 0)}s — {command[:120]}")
            except Exception:
                pass
        if result.get("error"):
            return (f"I could not run `{command}`: {result['error']} "
                    f"Nothing was executed.")
        return cmd_runner.format_result(result)

    # REQUIRES_CONFIRMATION — park it behind the human gate.
    if _pending_title():
        return ("There is already a confirmation waiting on the HUD. "
                "Resolve that one first — I never stack a second gate.")

    detail = (
        f"Command: {command}\n\n"
        f"Working directory: {cwd or os.getcwd()}\n\n"
        "This command changes, installs, or runs something on this computer, "
        "so it will not run until you confirm it on the screen."
    )

    def _run():
        result = cmd_runner.run_command(command, cwd=cwd, timeout=timeout)
        if player is not None:
            try:
                player.write_log(
                    f"[cmd] exit={result.get('exit_code')} "
                    f"in {result.get('duration', 0)}s — {command[:120]}")
            except Exception:
                pass
        if result.get("error"):
            return f"The command could not execute: {result['error']}"
        return cmd_runner.format_result(result)

    from core.confirm import request
    return request("execute_command", "Run a command on this computer",
                   detail, _run)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "execute_command",
    "description": (
        "Runs a shell/terminal command on the user's computer with NEXUS's "
        "safety layer. Read-only inspection commands (git status, dir, ls, "
        "pytest) run immediately. Commands that change files, install packages "
        "or run arbitrary scripts ask the user to confirm on the HUD first. "
        "Dangerous or destructive commands are refused outright. Use this when "
        "the user asks to run a command, or when a coding or git task genuinely "
        "needs a build/test/install the dedicated tools cannot express."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "The exact command to run (a single line)."
            },
            "cwd": {
                "type": "STRING",
                "description": "Optional working directory. Defaults to the repo/project root."
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Timeout in seconds (default 120, max 600). A still-running command is killed after this."
            }
        },
        "required": ["command"]
    },
    "handler": execute_command,
}