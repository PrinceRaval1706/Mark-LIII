"""
core/cmd_runner.py — NEXUS command execution with a modular safety layer.

WHY THIS EXISTS
    A personal assistant that can reason about the user's computer should be
    able to actually run things — build a project, run its tests, inspect a
    repo, install a dependency. But an arbitrary terminal is a loaded weapon:
    `rm -rf` has ended careers, and `pip install` from a random URL can hand a
    machine to somebody. So commands go through three tiers, and the tiers are
    the ONLY thing this module decides — the model never self-confirms:

      SAFE                   → runs immediately (git status, ls, pytest, ...)
      REQUIRES_CONFIRMATION  → parked behind the on-screen confirm gate
                               (core/confirm.py) — a button the USER presses
      BLOCKED                → refused outright with a plain explanation

    Every run returns stdout, stderr, exit code, the work directory, how long
    it took and whether it timed out. A running command can be cancelled from
    the UI / a later tool call, and every invocation is written to the activity
    log so there is always a record of what was run and under which tier.

THE POLICY IS MODULAR
    The three pattern lists below ARE the policy. Add a line to _SAFE or
    _CONFIRM to widen it; add one to _BLOCKED to close a hole. Matching is
    substring-on-normalised-command (lowercase, whitespace collapsed), which is
    deliberately simple and hard to get wrong — a longer list of clever rules
    is a list of clever holes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

SAFE   = "SAFE"
CONFIRM = "REQUIRES_CONFIRMATION"
BLOCKED = "BLOCKED"

DEFAULT_TIMEOUT = 120   # seconds; a build that hangs past this is killed

# ── SAFE: read-only / inspection. Runs immediately, no gate. ─────────────────
_SAFE = [
    "git status", "git diff", "git log", "git branch", "git ls-files",
    "git -c", "git rev-parse", "git stash list", "git remote",
    "git show", "git blame", "git shortlog", "git tag",
    "ls", "dir", "pwd", "echo", "type ", "cat ", "head ", "tail ", "less ",
    "find \"", "find ",
    "hostname", "whoami", "who am i", "uptime", "uname", "date", "time ",
    "ipconfig", "ifconfig", "ip addr", "netstat -", "route print",
    "ping ", "tracert", "traceroute", "nslookup",
    "systeminfo", "tasklist", "ps aux", "ps -", "top", "htop", "df -", "free ",
    "du -", "tree", "stat ", "file ", "where ", "which ",
    "python --version", "python -v", "python -V", "py --version",
    "node --version", "node -v", "npm --version", "npm -v",
    "java -version", "go version", "rustc --version", "cargo --version",
    "pip list", "pip show", "pip index", "npm ls", "npm outdated",
    "npm view", "dotnet --version", "flutter --version",
    "python -m pytest", "pytest", "python -m unittest", "go test", "cargo test",
    "npm test", "npm run test", "pnpm test", "yarn test", "flutter test",
    "cargo run", "flutter run",
    "get-content", "get-childitem", "get-aduser",
    "tasklist /fi", "sc query", "systeminfo",
    "git commit",   # local + freely reversible with git reset — not a gate
]

# ── BLOCKED: destructive / malicious. Refused even with confirmation. ─────────
_BLOCKED = [
    "rm -rf /", "rm -fr /", "rmdir /s", "rd /s",
    "format c:", "format c\\", "format /q",
    "mkfs.", "dd if=",
    "shutdown /r", "shutdown -r", "shutdown /s", "shutdown -h", "shutdown -p",
    "reboot", "poweroff", "halt",
    "diskpart", "clean all", "convert c:",
    "parted /dev/sd", "fdisk /dev/sd",
    "> /dev/sda", "> /dev/sdb",
    ":(){ :|:& };:",                    # fork bomb
    "rm -rf .git", "git clean -xfd", "git clean -fd", "git reset --hard",
    "update-grub", "install-mbr",
    "curl | sudo sh", "curl | sh", "wget | sh", "wget | bash", "curl | bash",
]

# ── REQUIRES_CONFIRMATION: everything that changes things but is not outright
#    dangerous. Anything NOT in SAFE and NOT in BLOCKED also lands here, so the
#    default is "untooled means gated" — a command that looks unknown to the
#    policy engine gets a human gate, never a silent run. ─────────────────────
_CONFIRM = [
    "rm ", "del ", "erase ", "rmdir ", "remove-item", "unlink ",
    "mv ", "move ", "ren ", "rename-item", "copy ", "cp ", "robocopy ", "xcopy ",
    "pip install", "pip uninstall", "pip3 install", "python -m pip",
    "npm install", "npm i ", "npm update", "npm uninstall", "yarn add",
    "yarn remove", "pnpm add", "pnpm install", "bun add",
    "pipenv install", "poetry add",
    "go get", "go install", "cargo install", "cargo add", "cargo remove",
    "apt install", "apt-get install", "apt remove", "dnf install", "pacman -s",
    "brew install", "brew uninstall", "choco install", "winget install",
    "sudo ", "runas ", "schtasks ", "sc config", "sc create", "sc delete",
    "netsh advfirewall", "netsh interface",
    "taskkill", "kill ", "pkill ", "tskill", "powershell stop-process",
    "git push", "git pull", "git fetch", "git merge", "git rebase", "git cherry-pick",
    "git reset", "git checkout --", "git restore", "git stash drop", "git gc",
    "git tag -", "git branch -d", "git branch -D", "git rm",
    "net user", "net localgroup", "net stop", "net start",
    "reg add", "reg delete", "regedit",
    "attrib -", "icacls ", "takeown ",
    "mklink", "new-item", "mkdir", "md ", "create-file", "set-content",
    "start ", "open ", "explorer ", "xdg-open", "subl ", "code ",
    "curl -o", "curl --output", "wget -o", "wget --output-document",
    "powershell -", "powershell.exe", "cmd /c", "cmd.exe",
    "shutdown", "osascript",
    "7z a", "7z d", "unzip -", "tar -x", "rar a", "rar d",
]

# -- not-yet-compiled-from-lists guard -----------------------------------------

def _normalise(command: str) -> str:
    return re.sub(r"\s+", " ", (command or "").strip().lower())


def classify(command: str, cwd: str | None = None) -> str:
    """Classify a command into one of the three tiers.

    Order matters: BLOCKED is checked FIRST so a destructive command can never
    slip through a SAFE entry that happens to share a prefix (e.g. "git reset"
    must lose to the BLOCKED "git reset --hard").
    """
    norm = _normalise(command)
    if not norm:
        return BLOCKED

    # Oh-so-common source of magic in the wild: a bare `sh`/`bash`/`powershell`
    # with no argument is read-only (a prompt), but a *script* argument is
    # arbitrary code — gate the latter.
    for bad in _BLOCKED:
        if bad in norm:
            return BLOCKED

    for ok in _SAFE:
        if ok in norm:
            return SAFE

    # Everything else — including installs, file mutation and arbitrary
    # script execution — is gated. Being strict here is the point.
    return CONFIRM


def classify_reason(command: str, tier: str) -> str:
    if tier == BLOCKED:
        return ("This command is blocked: it is destructive, irreversible, or "
                "outside what NEXUS is allowed to run. Nothing was executed.")
    if tier == SAFE:
        return "This command is read-only and safe to run."
    return ("This command changes your system or files, so a confirmation is "
            "required before it runs.")


# ── Process registry (one active command at a time, cancellable) ─────────────

@dataclass
class _Handle:
    token: str
    command: str
    proc: subprocess.Popen
    started: float = field(default_factory=time.monotonic)


_PROC: Optional[_Handle] = None
_PROC_LOCK = threading.Lock()


def _current_handle() -> Optional[_Handle]:
    with _PROC_LOCK:
        return _PROC


def active() -> bool:
    h = _current_handle()
    if h is None:
        return False
    alive = h.proc.poll() is None
    if not alive:
        with _PROC_LOCK:
            if _PROC is h:
                _PROC = None
        return False
    return True


def cancel_current() -> str:
    """Kill the running command (if any), process tree included. Idempotent."""
    h = _current_handle()
    if h is None or h.proc.poll() is not None:
        return "No command is currently running."
    try:
        _kill_tree(h.proc)
        with _PROC_LOCK:
            if _PROC is h:
                _PROC = None
        return f"Cancelled: {h.command[:80]}"
    except Exception as e:
        return f"Could not cancel the running command: {e}"


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=10,
        )
    else:
        import signal as _sig
        try:
            os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
        except Exception:
            proc.kill()


def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    env: dict | None = None,
    shell: bool = False,
    argv: list[str] | None = None,
) -> dict:
    """Run `command` and capture the full picture.

    `argv` (optional) is an already-tokenised command line — use it when a path
    may contain spaces (git_tool and coding_agent do). When given, it wins and
    `command` is only used for tier classification and bookkeeping.

    Returns a dict with: command, tier, stdout, stderr, exit_code,
    timed_out, cwd (resolved), duration and token. Never raises — every failure
    mode is reported inside the dict except a genuinely broken subprocess API,
    which is folded into the `error` field.
    """
    tier      = classify(command, cwd)
    token     = uuid.uuid4().hex[:12]
    started   = time.monotonic()
    resolved  = str(Path(cwd).expanduser().resolve() if cwd else Path.cwd())
    result: dict = {
        "token": token, "command": command, "tier": tier,
        "stdout": "", "stderr": "", "exit_code": None,
        "timed_out": False, "cwd": resolved, "duration": 0.0,
        "cancelled": False, "error": "",
    }

    if tier == BLOCKED:
        result["error"] = classify_reason(command, tier)
        return result

    # Ensure the working directory exists, else refuse cleanly.
    work = Path(resolved)
    if not work.is_dir():
        result["error"] = f"Working directory does not exist: {resolved}"
        return result

    # Merge caller env over the parent's, so PATH etc. are still available.
    merged = dict(os.environ)
    if env:
        merged.update(env)

    launch = argv if argv is not None else command.split()
    try:
        proc = subprocess.Popen(
            launch,
            cwd=str(work),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged,
            shell=shell,
            text=False,          # decode ourselves with errors="replace"
        )
    except Exception as e:
        result["error"] = f"Could not start the command: {e}"
        return result

    with _PROC_LOCK:
        _PROC = _Handle(token=token, command=command, proc=proc,
                        started=time.monotonic())

    cancelled = False
    try:
        out, err = proc.communicate(timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            start = time.monotonic()
            while proc.poll() is None and time.monotonic() - start < 5:
                time.sleep(0.05)
        except Exception:
            pass
        out, err = b"", b""
        proc.kill() if proc.poll() is None else None
        result["timed_out"] = True
    finally:
        with _PROC_LOCK:
            if _PROC is not None and _PROC.token == token:
                _PROC = None

    result["stdout"]    = out.decode("utf-8", errors="replace")
    result["stderr"]    = err.decode("utf-8", errors="replace")
    result["exit_code"] = proc.returncode
    result["duration"]  = round(time.monotonic() - started, 2)
    return result


def format_result(result: dict, max_chars: int = 4000) -> str:
    """Turn the structured dict into the text a tool hands back to the model."""
    lines = [f"[COMMAND] {result['command']}"]
    lines.append(f"[TIER] {result['tier']}")
    if result.get("cancelled"):
        lines.append("[CANCELLED] stopped by the user.")
    if result.get("timed_out"):
        lines.append(f"[TIMED_OUT] killed after {result['duration']}s.")
    if result.get("error"):
        lines.append(f"[ERROR] {result['error']}")
        return "\n".join(lines)

    code = result.get("exit_code")
    lines.append(f"[EXIT_CODE] {code if code is not None else 'n/a'}")
    if result.get("cwd"):
        lines.append(f"[WORK_DIR] {result['cwd']}")
    lines.append(f"[DURATION] {result.get('duration', 0)}s")

    body = result.get("stdout", "") + result.get("stderr", "")
    cap  = body.strip()
    if len(cap) > max_chars:
        cap = cap[:max_chars] + f"\n… (output truncated from {len(cap):,} chars)"
    if cap:
        lines.append("[OUTPUT]")
        lines.append("```")
        lines.append(cap)
        lines.append("```")
    else:
        lines.append("[OUTPUT] (no output)")
    return "\n".join(lines)