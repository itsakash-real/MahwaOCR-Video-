"""Code lock: AI agents may run this program, never change it.

    python -m manhwa_ocr guard lock      # you: record the program as it is now, make it read-only
    python -m manhwa_ocr guard status    # anyone: is the program exactly as locked?
    python -m manhwa_ocr guard unlock    # you: allow editing again (only when YOU want to change code)

While locked, every command first checks the program against the lock. If a
file was changed, added or deleted, nothing runs: the error names the files and
says how to get the locked version back. An agent that edited the code can't
quietly carry on with the edited version.

What's covered: everything in the program folder, so even a new scratch
script there stops every command. Not covered: manhwa-ocr.yaml (your settings),
caches, git's own folder, and everything in your output folder.

This is a guard, not a wall: any program running as you could undo it. It
turns a silent change into a loud stop, and git (if set up) brings the code back.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Dict, List

from .util import UserError, atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / ".code-lock.json"
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git", "manhwa_ocr.egg-info"}
SKIP_FILES = {"manhwa-ocr.yaml", ".code-lock.json"}   # your settings; the lock itself


def _files() -> List[Path]:
    out = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if (p.is_file() and not SKIP_PARTS & set(rel.parts) and rel.as_posix() not in SKIP_FILES
                and p.suffix not in {".pyc", ".pyo"}):
            out.append(p)
    return sorted(out)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot() -> Dict[str, str]:
    return {p.relative_to(ROOT).as_posix(): _digest(p) for p in _files()}


def _set_readonly(paths: List[Path], readonly: bool) -> None:
    for p in paths:
        try:
            mode = p.stat().st_mode
            os.chmod(p, (mode & ~stat.S_IWRITE) if readonly else (mode | stat.S_IWRITE))
        except OSError:
            pass


def is_locked() -> bool:
    return LOCK_FILE.is_file()


def changes() -> List[str]:
    """What differs from the lock, as readable lines. Empty when unlocked or intact."""
    if not is_locked():
        return []
    try:
        locked = json.loads(LOCK_FILE.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return ["the lock file .code-lock.json itself is damaged"]
    now = _snapshot()
    out = [f"changed: {f}" for f in sorted(locked) if f in now and now[f] != locked[f]]
    out += [f"deleted: {f}" for f in sorted(locked) if f not in now]
    out += [f"added:   {f}" for f in sorted(now) if f not in locked]
    return out


def check() -> None:
    """Called before every command. Refuses to run modified code."""
    problems = changes()
    if not problems:
        return
    shown = "\n  ".join(problems[:12]) + ("\n  ..." if len(problems) > 12 else "")
    raise UserError(
        "The program code was changed while it is locked, so nothing will run:\n  " + shown + "\n\n"
        "AI agents: you must not edit the program. Stop and tell the user exactly what you changed.\n"
        f"User: to undo the changes, in {ROOT} run:  git checkout -- .  &&  git clean -fd manhwa_ocr tests\n"
        "      if you made these changes yourself on purpose:  python -m manhwa_ocr guard lock"
    )


def lock(log=print) -> int:
    files = _files()
    atomic_write_text(LOCK_FILE, json.dumps({"files": _snapshot()}, indent=1))
    _set_readonly(files + [LOCK_FILE], True)
    log(f"Locked {len(files)} program files (read-only, checked before every command).")
    return len(files)


def unlock(log=print) -> None:
    _set_readonly(_files() + [LOCK_FILE], False)
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()
    log("Unlocked: the program can be edited. Run `python -m manhwa_ocr guard lock` when you're done.")
