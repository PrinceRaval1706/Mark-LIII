"""
actions/cancel_command.py — cancel the running command.

core/cmd_runner.py keeps a single active process handle (one command at a
time). This tool kills whatever that is — process tree included, so a spawned
child of a build step dies with it. Idempotent: when nothing is running it just
says so instead of failing.

Use it whenever the user says "stop that", "kill it", or "cancel the command".
"""

from __future__ import annotations

from core import cmd_runner


def cancel_command(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    if player is not None:
        try:
            player.write_log(f"[cmd] cancel requested while running={cmd_runner.active()}")
        except Exception:
            pass
    return cmd_runner.cancel_current()


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "cancel_command",
    "description": (
        "Cancels the command that is currently running (the one started by "
        "execute_command). Kills the whole process tree so background children "
        "stop too. Harmless when nothing is running."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {}
    },
    "handler": cancel_command,
}