"""Per-stage timing log (Phase 0 instrumentation).

Every CLI command run against a session appends one record to
``<session>/.work/timings.json``: which stage ran, for which chapter, when it
started, how long it took and whether it succeeded. This log is the baseline
every later speedup (trimmer validation, solver/OCR bake-offs, alignment and
render measurements) is compared against.

Deliberately passive: recording must never fail, block or change the command
it observes. Every failure path in here is swallowed on purpose.
"""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .util import atomic_write_text

TIMINGS_FILE = "timings.json"
_MAX_RECORDS = 5000  # per session; the oldest records drop off beyond this


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timings_path(session_path: str | Path) -> Path:
    return Path(session_path) / ".work" / TIMINGS_FILE


def read(session_path: str | Path) -> list[dict]:
    """All timing records for a session; [] when missing or unreadable."""
    try:
        data = json.loads(timings_path(session_path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record(session_path: str | Path, *, stage: str, chapter: Any = None,
           started: str, duration_s: float, ok: bool,
           exit_code: Optional[int] = None) -> None:
    """Append one timing record. Never raises."""
    try:
        rows = read(session_path)
        rows.append({
            "stage": stage,
            "chapter": chapter,
            "started": started,
            "duration_s": round(float(duration_s), 3),
            "ok": bool(ok),
            "exit": exit_code,
            "tool": {"app": __version__, "python": platform.python_version()},
        })
        path = timings_path(session_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(rows[-_MAX_RECORDS:], indent=1,
                                           ensure_ascii=False))
    except Exception:  # noqa: BLE001 - the log observes; it must never interfere
        pass


def stage_name(args) -> str:
    """'video plan', 'video render', 'prepare', ... from parsed CLI args."""
    stage = str(getattr(args, "command", "?") or "?")
    action = getattr(args, "action", None)
    if action:
        stage = f"{stage} {action}"
    return stage


def chapter_of(args):
    """The chapter spec a command acted on, when it named one."""
    return getattr(args, "chapter", None) or getattr(args, "chapters", None)


# ---------------------------------------------------------------- per-chapter report

REPORT_DIRNAME = "timing"
_REPORT_FIELDS = ("prepare", "read_tiles", "assemble", "answer_assembly", "finalize", "total")


def report_path(session_path: str | Path, chapter: str) -> Path:
    return Path(session_path) / ".work" / REPORT_DIRNAME / f"chapter_{chapter}.json"


def write_chapter_report(session_path: str | Path, chapter: str, report: dict) -> Path:
    """Write .work/timing/chapter_N.json. Never raises."""
    try:
        p = report_path(session_path, chapter)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "chapter": str(chapter),
            "generated_at": _now_iso(),
            "stages_s": {k: round(float(report.get("stages_s", {}).get(k) or 0.0), 1)
                         for k in _REPORT_FIELDS},
            **{k: v for k, v in report.items() if k != "stages_s"},
        }
        atomic_write_text(p, json.dumps(payload, indent=1, ensure_ascii=False))
        return p
    except Exception:  # noqa: BLE001 - reporting must never break a run
        return Path(session_path) / ".work" / REPORT_DIRNAME / f"chapter_{chapter}.json"


def render_report_table(report: dict) -> str:
    """One-glance per-stage seconds, for printing at finalize."""
    stages = report.get("stages_s", {})
    rows = [(k, stages.get(k)) for k in _REPORT_FIELDS]
    width = max(len(k) for k, _ in rows)
    lines = ["per-stage timing (seconds):"]
    for name, secs in rows:
        if secs is None:
            lines.append(f"  {name:<{width}}  -")
        else:
            lines.append(f"  {name:<{width}}  {secs:>8.1f}")
    extra = report.get("tiles")
    if extra:
        lines.append(f"  tiles: {extra.get('read')}/{extra.get('total')} read"
                     + (f", {extra.get('shards')} shards" if extra.get("shards") else ""))
    return "\n".join(lines)
