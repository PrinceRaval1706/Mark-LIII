"""
actions/git_tool.py — Git integration for NEXUS.

Covers the everyday, non-destructive surface of git:

    status    → did anything change, and where
    diff      → what exactly changed (working tree, or staged with --staged)
    log       → recent history, one line per commit
    commit    → stage-all + commit (local only; reversible with git reset)

Everything else about git runs through the generic execute_command tool, which
routes destructive or history-rewriting operations (reset --hard, clean,
push --force, …) to the confirmation gate. Status/diff/log/commit are safe:
they touch the working copy but never lose history, so they are executed
immediately and logged. Every invocation reports the working directory it ran
against and its exit code.
"""

from __future__ import annotations

from pathlib import Path

from core import cmd_runner

_VALID = {"status", "diff", "log", "commit"}


def _base_argv(repo: Path, sub: str, args: list[str]) -> list[str]:
    # Git >= 2.13 supports working-directory-relative -C; gives us cwd-free
    # paths without shell quoting worries.
    return ["git", "-C", str(repo), sub] + args


def git_tool(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    p = parameters or {}
    action = str(p.get("action", "status")).lower().strip()
    if action not in _VALID:
        return ("Unknown git action. Use status, diff, log, or commit.")

    repo = str(p.get("repo_path", "")).strip()
    if repo:
        repo_path = Path(repo).expanduser()
    else:
        repo_path = Path.cwd()
    repo_path = repo_path.resolve()

    if action == "commit":
        message = str(p.get("message", "")).strip()
        if not message:
            return "A commit needs a message. Pass the message to use."
        if not (repo_path / ".git").is_dir():
            return f"No git repository found at {repo_path} — nothing was committed."
        argv = _base_argv(repo_path, "commit", ["-m", message])
    else:
        argv = _base_argv(repo_path, action, [])

    display = f"git -C {repo_path} {action}"

    if player is not None:
        try:
            player.write_log(f"[git:{action}] {repo_path}")
        except Exception:
            pass

    result = cmd_runner.run_command(display, cwd=str(repo_path), argv=argv,
                                    timeout=90)
    if player is not None:
        try:
            player.write_log(f"[git:{action}] exit={result.get('exit_code')} "
                             f"in {result.get('duration', 0)}s")
        except Exception:
            pass

    if result.get("error"):
        return f"Could not run git {action}: {result['error']}"

    code = result.get("exit_code")
    if code not in (0, 1):   # git returns 1 for "no changes to show"
        return (f"git {action} failed with exit code {code}.\n"
                f"{result.get('stderr') or result.get('stdout')}")

    return cmd_runner.format_result(result, max_chars=3000)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "git_tool",
    "description": (
        "Git operations on a repository: status (what changed), diff (the "
        "actual changes), log (recent history), commit (stage all changes with "
        "a message). Give repo_path to target a repository, or omit it to use "
        "the current working directory. For anything heavier — push, pull, "
        "reset, merge — use execute_command instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | diff | log | commit (default: status)"
            },
            "repo_path": {
                "type": "STRING",
                "description": "Optional path to the git repository to operate on (default: current directory)"
            },
            "message": {
                "type": "STRING",
                "description": "Commit message — required when action is commit"
            }
        },
        "required": ["action"]
    },
    "handler": git_tool,
}