"""
actions/coding_agent.py — the NEXUS coding agent for EXISTING projects.

dev_agent scaffolds brand-new multi-file projects; code_helper handles single
files. This agent works in the middle ground the user actually lives in: a
project on disk that needs analyzing, editing, testing, and debugging.

Sub-actions (parameter `action`):

    analyze    → structural survey: files, entry points, manifests, test layout
    read       → read one project file (bounded output, never dumps megabyte
                 files wholesale; a size cap keeps context sane)
    write      → write or fully replace a file. The previous content (or the
                 very existence of the file) is pushed to core/undo FIRST, so
                 the user can reverse any agent change with "undo" — nothing
                 here needs a confirmation banner.
    edit       → targeted change to one file, applied by the model; undo-able
    run_tests  → run the project's test suite through the cmd_runner safety
                 layer (test commands are tier SAFE). Detects pytest, npm,
                 go, cargo and make if no command is passed.
    fix        → iterate: run → read failures → Gemini fixes the broken file
                 (undo-able) → rerun, up to MAX_FIX_ATTEMPTS. Dependency or
                 permission errors are NOT auto-installed or auto-fixed — the
                 agent stops and reports them, because installing packages is
                 a system change that belongs behind the confirmation gate.
"""

from __future__ import annotations

from pathlib import Path

from core import cmd_runner
from core.undo import push_undo

try:
    from actions.code_helper import (
        _get_gemini, _read_file, _clean_code, _preview,
    )
    from actions.dev_agent import (
        _parse_traceback, _classify_error, _is_rate_limit,
    )
except Exception:  # pragma: no cover — discovery logs and skips this file
    raise

MAX_FIX_ATTEMPTS = 5
MODEL = "gemini-flash-latest"
_EXT_TREE = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs",
             ".java", ".c", ".cpp", ".h", ".html", ".css", ".sh",
             ".json", ".yaml", ".yml", ".toml", ".ini", ".md", ".rb", ".php"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve(repo: Path, file_path: str) -> Path:
    p = Path(file_path).expanduser()
    if p.is_absolute():
        return p
    return (repo / p).resolve()


def _collect_py_files(repo: Path) -> list[str]:
    if not repo.is_dir():
        return []
    return [str(p) for p in repo.rglob("*.py") if "site-packages" not in str(p)]


def _analyze(repo: Path) -> str:
    if not repo.is_dir():
        return f"Project path does not exist: {repo}"
    entries = sorted(repo.iterdir(), key=lambda e: (e.is_file(), e.name))
    parts = [f"Project: {repo}"]

    markers = {
        "pyproject.toml": "Python (pyproject)",
        "requirements.txt": "Python (requirements.txt)",
        "setup.py": "Python (setup.py)",
        "package.json": "Node.js",
        "go.mod": "Go",
        "Cargo.toml": "Rust",
        "pom.xml": "Java (Maven)",
        "Makefile": "Makefile",
        "CMakeLists.txt": "CMake",
    }
    manifest = next((m for m in markers if (repo / m).exists()), None)
    if manifest:
        parts.append(f"Manifest: {markers[manifest]}")

    entry_candidates = [n for n in ("main.py", "index.js", "index.ts",
                                    "app.py", "manage.py", "main.go", "main.rs")]
    found = [n for n in entry_candidates if (repo / n).exists()]
    if found:
        parts.append(f"Likely entry point: {', '.join(found)}")

    dirs = [e.name for e in repo.iterdir() if e.is_dir() and not e.name.startswith((".", "__"))]
    if dirs:
        parts.append(f"Directories: {', '.join(sorted(dirs)[:12])}")

    tests = [p for p in repo.rglob("*") if p.suffix.lower() in (".py", ".js", ".ts")
             and ("test" in p.name.lower() or "tests" in str(p.parent).lower())]
    if tests:
        parts.append(f"Test files: {len(tests)} (e.g. {tests[0].name})")

    ext_count: dict[str, int] = {}
    file_count = 0
    for p in repo.rglob("*"):
        if p.is_file() and p.suffix in _EXT_TREE:
            ext_count[p.suffix] = ext_count.get(p.suffix, 0) + 1
            file_count += 1
    if ext_count:
        top = ", ".join(f"{k}: {v}" for k, v in
                        sorted(ext_count.items(), key=lambda kv: -kv[1])[:6])
        parts.append(f"Files: {file_count} ({top})")

    parts.append("Top-level entries:")
    lines = []
    for e in entries[:40]:
        tag = "📁 " if e.is_dir() else "📄 "
        size = f" ({e.stat().st_size:,} B)" if e.is_file() and e.stat().st_size < 1_000_000 else ""
        lines.append(f"  {tag}{e.name}{size}")
    parts.append("\n".join(lines) if lines else "  (empty)")
    return "\n".join(parts)


def _read(repo: Path, file_path: str, max_chars: int = 6000) -> str:
    p = _resolve(repo, file_path)
    content, err = _read_file(str(p))
    if err:
        return err
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n… (truncated, {len(content):,} chars total)"
    return f"{p}\n\n" + content


def _write(repo: Path, file_path: str, code: str) -> str:
    p = _resolve(repo, file_path)
    old = ""
    try:
        old = p.read_text(encoding="utf-8")
    except Exception:
        old = None          # file does not exist (or is binary) — treat as new

    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(code, encoding="utf-8")
    except Exception as e:
        return f"Could not write {p}: {e}"

    if old is None:
        push_undo(f"created {file_path}",
                  lambda p=p: (p.unlink(missing_ok=True), f"deleted {p}")[1])
    else:
        push_undo(f"rewrote {file_path}",
                  lambda p=p, o=old: (p.write_text(o, encoding="utf-8"),
                                      f"restored original {p}")[1])
    return f"Written to {p}.\n\nPreview:\n{_preview(code)}"


def _edit(repo: Path, file_path: str, instruction: str) -> str:
    p = _resolve(repo, file_path)
    content, err = _read_file(str(p))
    if err:
        return err
    try:
        model = _get_gemini(MODEL)
        response = model.generate_content(
            "You are an expert editor. Apply the change below to the code.\n"
            "Return ONLY the complete updated code — no explanation, no "
            "markdown, no backticks.\n\n"
            f"Change: {instruction}\n\n"
            f"Original code:\n{content}\n\nUpdated code:")
        edited = _clean_code(response.text)
    except Exception as e:
        return f"Could not edit {file_path}: {e}"

    old = content
    try:
        p.write_text(edited, encoding="utf-8")
    except Exception as e:
        return f"Could not save {p}: {e}"
    push_undo(f"edited {file_path}",
              lambda o=old: (p.write_text(o, encoding="utf-8"),
                             f"restored original {p}")[1])
    return f"Edited {p}.\n\nPreview:\n{_preview(edited)}"


def _detect_test_command(repo: Path) -> str | None:
    if (repo / "Makefile").exists():
        return "make test"
    if (repo / "package.json").exists():
        return "npm test"
    if (repo / "go.mod").exists():
        return "go test ./..."
    if (repo / "Cargo.toml").exists():
        return "cargo test"
    if ((repo / "pytest.ini").exists() or (repo / "pyproject.toml").exists()
            or (repo / "setup.cfg").exists()
            or (repo / "requirements.txt").exists() and (repo / "tests").is_dir()):
        return "python -m pytest -q"
    if any(repo.glob("test_*.py")) or (repo / "tests").is_dir():
        return "python -m pytest -q"
    return "python -m pytest -q"


def _run_tests(repo: Path, command: str | None, player) -> str:
    test_cmd = (command or _detect_test_command(repo)).strip()
    if not test_cmd:
        return "Could not detect a test setup — pass a command to run."

    if player is not None:
        try:
            player.write_log(f"[coding] run_tests — {test_cmd} in {repo}")
        except Exception:
            pass

    result = cmd_runner.run_command(test_cmd, cwd=str(repo), timeout=180)
    if result.get("error"):
        return f"Could not run tests: {result['error']}"
    if result.get("timed_out"):
        return f"Tests timed out after 180s (killed, not finished)."
    return cmd_runner.format_result(result, max_chars=2500)


def _fix(repo: Path, command: str, goal: str, player) -> str:
    if not command.strip():
        return "Provide the command to run (e.g. 'python -m pytest -q') for the fix loop."
    if not (repo.is_dir()):
        return f"Project path does not exist: {repo}"

    if player is not None:
        try:
            player.write_log(f"[coding] fix loop started — {command}")
        except Exception:
            pass

    py_files = _collect_py_files(repo)
    undone: set[str] = set()

    def _apply_fix(rel_path: str, fixed: str) -> None:
        p = _resolve(repo, rel_path)
        old = p.read_text(encoding="utf-8")
        p.write_text(fixed, encoding="utf-8")
        if rel_path not in undone:
            undone.add(rel_path)
            push_undo(f"coding fix {rel_path}",
                      lambda o=old: (p.write_text(o, encoding="utf-8"),
                                     f"restored {p}")[1])

    last = ""
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        result = cmd_runner.run_command(command, cwd=str(repo), timeout=180)
        combined = (result.get("stdout", "") + "\n" + result.get("stderr", "")).strip()
        exit_code = result.get("exit_code")
        last = combined

        if player is not None:
            try:
                player.write_log(f"[coding] fix attempt {attempt} — exit={exit_code}")
            except Exception:
                pass

        if result.get("error"):
            return f"Could not run the command: {result['error']}"
        if exit_code == 0 and _classify_error(combined) == "none":
            return (f"FIXED after {attempt} attempt(s): {command} now exits 0.\n\n"
                    f"Last output:\n{combined[-1500:]}")
        if result.get("timed_out"):
            return ("The command timed out — it is a long-running app, not a "
                    "fixable failure. Use cancel_command if needed.")

        if attempt == MAX_FIX_ATTEMPTS:
            break

        error_type = _classify_error(combined)
        if error_type == "dependency_error":
            return ("A dependency is missing — NEXUS will not install it silently. "
                    "Ask the user to confirm installing the missing package "
                    "(execute_command will show a gate).\n\n"
                    f"Error sample:\n{combined[-800:]}")

        # Which file is broken?
        target_file = None
        if py_files:
            hit, _ = _parse_traceback(combined, py_files)
            if hit:
                target_file = hit
        if not target_file:
            candidates = [n for n in ("main.py", "app.py", "test_main.py")
                          if (repo / n).exists()]
            target_file = str(repo / candidates[0]) if candidates else None
        if not target_file:
            return (f"Could not locate the file at fault. Error output:\n{combined[-1000:]}")

        rel = str(Path(target_file).relative_to(repo))
        code, err = _read_file(str(_resolve(repo, rel)))
        if err:
            return err

        try:
            model = _get_gemini(MODEL)
            response = model.generate_content(
                "You are an expert debugger. Fix the broken file below so the "
                f"project works. Goal: {goal}\n\n"
                f"Error type: {error_type}\n"
                f"Error output:\n{combined[-2500:]}\n\n"
                f"Broken file ({rel}):\n{code}\n\n"
                "Rules: output ONLY the complete fixed code. No explanation, "
                "no markdown, no backticks. Keep working logic intact.")
            fixed = _clean_code(response.text)
            _apply_fix(rel, fixed)
            if player is not None:
                try:
                    player.write_log(f"[coding] fixed {rel} (attempt {attempt})")
                except Exception:
                    pass
        except Exception as e:
            if _is_rate_limit(e):
                return "Rate limit hit during the fix. Try again in a moment."
            return f"Fix attempt {attempt} failed: {e}"

    return (f"Not fully fixed after {MAX_FIX_ATTEMPTS} attempts. "
            f"Last output:\n{last[-1500:]}")


def coding_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    p = parameters or {}
    action = str(p.get("action", "analyze")).lower().strip()
    repo = str(p.get("repo_path", "")).strip() or str(Path.cwd())
    repo_path = Path(repo).expanduser().resolve()

    if action == "analyze":
        return _analyze(repo_path)
    if action == "read":
        return _read(repo_path, str(p.get("file_path", "")).strip(),
                     int(p.get("max_chars") or 6000))
    if action == "write":
        return _write(repo_path, str(p.get("file_path", "")).strip(),
                      str(p.get("code", "")))
    if action == "edit":
        return _edit(repo_path, str(p.get("file_path", "")).strip(),
                     str(p.get("instruction", "")).strip())
    if action == "run_tests":
        return _run_tests(repo_path, str(p.get("command", "")).strip() or None,
                          player)
    if action == "fix":
        return _fix(repo_path, str(p.get("command", "")).strip(),
                    str(p.get("goal", "make the tests pass")), player)

    return ("Unknown coding action. Use analyze | read | write | edit | "
            "run_tests | fix.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "coding_agent",
    "description": (
        "Works on an existing coding project on disk: analyze (project "
        "overview, entry points, manifests, test layout), read (one file, "
        "bounded), write (new or full rewrite, undo-able), edit (targeted "
        "change to one file, undo-able), run_tests (git-style test runner, "
        "detects pytest/npm/go/cargo/make), fix (iterate run→fix→rerun up to "
        "5 attempts). repo_path defaults to the current directory. Use for "
        "anything within an existing project; use dev_agent for scaffolding "
        "brand-new projects from scratch."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "analyze | read | write | edit | run_tests | fix (default: analyze)"
            },
            "repo_path": {
                "type": "STRING",
                "description": "Project root directory (default: current directory)"
            },
            "file_path": {
                "type": "STRING",
                "description": "File to read/write/edit, absolute or repo-relative"
            },
            "code": {
                "type": "STRING",
                "description": "Full file contents for write"
            },
            "instruction": {
                "type": "STRING",
                "description": "What to change, for edit"
            },
            "command": {
                "type": "STRING",
                "description": "Exact test/build command for run_tests or fix (optional)"
            },
            "goal": {
                "type": "STRING",
                "description": "What the fix loop should achieve (default: make the tests pass)"
            },
            "max_chars": {
                "type": "INTEGER",
                "description": "Read cap in characters (default 6000)"
            }
        },
        "required": ["action"]
    },
    "handler": coding_agent,
}