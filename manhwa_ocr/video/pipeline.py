"""Build 0 — the pipelining runner: one chapter, every settled stage,
in order, resumable.

``video build`` already batches plan+render across chapters and continues
past failures. This runner is the complementary single-chapter driver:
panels -> pair -> align -> plan -> verify -> sync-audit -> render, with

- a freshness skip per stage (declared inputs vs output mtimes), so
  re-running after a fix only redoes what the fix invalidated;
- stop-on-first-failure with the stage named (a batch may continue,
  a chapter build may not);
- the sync-audit as a real gate before render;
- a machine-readable run summary at .work/video/chN/pipeline.json.

It deliberately stops before ``approve`` (a human gate) and does not do
the pairing fixes themselves — pair only writes the task and reports
its problems; fixing them is the agent's job between runs. ``--from
<stage>`` resumes at a stage, forcing it and everything after it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..util import UserError, atomic_write_text, require_chapter
from ..workspace import work_root

STAGE_NAMES = ["panels", "pair", "align", "plan", "verify", "sync-audit", "render", "qc"]


class Ctx:
    def __init__(self, sess, chapter: str, cfg, log: Callable):
        self.sess = sess
        self.root: Path = sess.path
        self.chapter = chapter
        self.cfg = cfg
        self.log = log
        self.force = False


# ---------------------------------------------------------------- freshness

def _newest(paths: List[Path]) -> float:
    times = [p.stat().st_mtime for p in paths if p.exists()]
    return max(times) if times else 0.0


def _fresh(output: Path, inputs: List[Path]) -> bool:
    return output.exists() and output.stat().st_mtime >= _newest(inputs)


def _pages(root: Path, chapter: str) -> List[Path]:
    from .media import pages_dir

    d = pages_dir(root, chapter)
    return sorted(d.glob("*")) if d.exists() else []


def _audio(root: Path, chapter: str) -> List[Path]:
    from .media import audio_path

    p = audio_path(root, chapter)
    return [p] if p else []


# ---------------------------------------------------------------- stages
# Each stage: inputs(ctx) -> [Path], output(ctx) -> Path|None (None = never
# skipped), run(ctx) -> detail string. A stage fails by raising UserError.

def _panels(ctx: Ctx):
    from .media import ensure_pages
    from .panels import build_panels

    ensure_pages(ctx.sess, ctx.chapter, ctx.cfg.download, log=ctx.log)
    build_panels(ctx.root, ctx.chapter, ctx.cfg.video.get("panels"), log=ctx.log)
    return "panels.json current"


def _pair(ctx: Ctx):
    from . import pairing

    pairing.write_task(ctx.root, ctx.chapter, ctx.sess.slug, log=ctx.log)
    problems = pairing.check(ctx.root, ctx.chapter)
    return f"{len(problems)} pairing problem(s) to review"


def _align(ctx: Ctx):
    from . import align_engine

    result = align_engine.build_alignment(ctx.root, ctx.chapter, ctx.cfg, log=ctx.log,
                                          force=ctx.force)
    return f"{len(result['segments'])} segments ({result['sync_mode']})"


def _plan(ctx: Ctx):
    from . import build as vb

    vb.make_plan(ctx.sess, ctx.chapter, ctx.cfg, force=ctx.force, log=ctx.log, storyboard=False)
    return "plan written"


def _verify(ctx: Ctx):
    from .media import video_dir

    vpath = video_dir(ctx.root) / f"chapter_{ctx.chapter}.verify.json"
    if not vpath.exists():
        raise UserError("No verify report — the plan stage did not produce one.")
    rep = json.loads(vpath.read_text(encoding="utf-8"))
    return f"dropped: {len(rep.get('dropped', []))}, needs eyes: {len(rep.get('needs_eyes', []))}"


def _sync_audit(ctx: Ctx):
    from . import syncaudit

    report = syncaudit.run(ctx.root, ctx.chapter, ctx.cfg, log=ctx.log)
    failures = (report.get("gate") or {}).get("failures") or []
    if failures:
        raise UserError("sync gate failed: " + "; ".join(failures))
    return "sync gate PASS"


def _render(ctx: Ctx):
    from . import build as vb

    path = vb.render_chapter(ctx.sess, ctx.chapter, ctx.cfg, log=ctx.log)
    return f"rendered {path.name}"


def _qc(ctx: Ctx):
    from .frameqc import qc_chapter

    report = qc_chapter(ctx.root, ctx.chapter, ctx.cfg, log=ctx.log)
    failures = report.get("failures") or []
    if failures:
        raise UserError("frame QC failed: " + "; ".join(failures[:6]))
    return f"frame QC pass ({len(report.get('shots') or [])} shots)"


def _plan_file(ctx: Ctx) -> Path:
    from .media import video_dir

    return video_dir(ctx.root) / f"chapter_{ctx.chapter}.plan.json"


def _fixes_file(ctx: Ctx) -> List[Path]:
    from .media import video_dir

    p = video_dir(ctx.root) / f"chapter_{ctx.chapter}.fixes.json"
    return [p] if p.exists() else []


STAGES: List[Dict[str, Any]] = [
    {"name": "panels",
     "inputs": lambda ctx: _pages(ctx.root, ctx.chapter),
     "output": lambda ctx: ctx.root / "media" / f"ch{ctx.chapter}" / "panels.json",
     "run": _panels},
    {"name": "pair", "inputs": lambda ctx: [], "output": lambda ctx: None, "run": _pair},
    {"name": "align",
     "inputs": lambda ctx: [ctx.root / "recaps" / f"chapter_{ctx.chapter}.cues.json",
                            ctx.root / "media" / f"ch{ctx.chapter}" / "panels.json",
                            *_audio(ctx.root, ctx.chapter)],
     "output": lambda ctx: ctx.root / "video" / f"chapter_{ctx.chapter}.alignment.json",
     "run": _align},
    {"name": "plan",
     "inputs": lambda ctx: [ctx.root / "video" / f"chapter_{ctx.chapter}.alignment.json",
                            ctx.root / "media" / f"ch{ctx.chapter}" / "panels.json",
                            *_fixes_file(ctx)],
     "output": _plan_file, "run": _plan},
    {"name": "verify", "inputs": lambda ctx: [], "output": lambda ctx: None, "run": _verify},
    {"name": "sync-audit",
     "inputs": lambda ctx: [_plan_file(ctx)],
     "output": lambda ctx: ctx.root / "video" / f"chapter_{ctx.chapter}.syncaudit.json",
     "run": _sync_audit},
    {"name": "render",
     "inputs": lambda ctx: [_plan_file(ctx)],
     "output": lambda ctx: ctx.root / "video" / f"chapter_{ctx.chapter}.mp4",
     "run": _render},
    {"name": "qc",
     "inputs": lambda ctx: [ctx.root / "video" / f"chapter_{ctx.chapter}.mp4"],
     "output": lambda ctx: ctx.root / "video" / f"chapter_{ctx.chapter}.frameqc.json",
     "run": _qc},
]


# ---------------------------------------------------------------- runner

def _previously_failed_stages(root: Path, chapter: str) -> set:
    """Stage names whose last recorded run in pipeline.json failed. A stage
    can write its own output file (e.g. the sync-audit report) and THEN
    raise on a gate failure, so the output can look "fresh" by mtime alone
    even though the run that produced it did not pass. Without this, a
    plain re-run (no --force, nothing else changed) would see that
    fresh-looking file and silently skip straight past a failed gate."""
    path = work_root(root) / "video" / f"ch{chapter}" / "pipeline.json"
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {r.get("stage") for r in prev.get("stages", []) if r.get("status") == "failed"}


def run_pipeline(sess, chapter: str, cfg, *, from_stage: Optional[str] = None,
                 force: bool = False, log: Callable = print,
                 stages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    chapter = require_chapter(chapter)
    stages = stages if stages is not None else STAGES
    names = [s["name"] for s in stages]
    if from_stage is not None and from_stage not in names:
        raise UserError(f"Unknown stage '{from_stage}'. Stages: {', '.join(names)}")
    ctx = Ctx(sess, chapter, cfg, log)
    forcing = force
    failed_last = _previously_failed_stages(ctx.root, chapter)
    results: List[Dict[str, Any]] = []
    outcome = "ok"
    for stage in stages:
        name = stage["name"]
        if from_stage is not None and name == from_stage:
            forcing = True
        out_path = stage["output"](ctx) if stage["output"] else None
        if (not forcing and name not in failed_last and out_path is not None
                and _fresh(out_path, stage["inputs"](ctx))):
            results.append({"stage": name, "status": "skipped", "seconds": 0.0,
                            "detail": "output is current"})
            log(f"  [{name}] skipped (current)")
            continue
        ctx.force = forcing
        log(f"  [{name}] running...")
        t0 = time.time()
        try:
            detail = stage["run"](ctx)
        except UserError as exc:
            results.append({"stage": name, "status": "failed",
                            "seconds": round(time.time() - t0, 1), "detail": str(exc)})
            log(f"  [{name}] FAILED: {exc}")
            outcome = "failed"
            break
        results.append({"stage": name, "status": "ran",
                        "seconds": round(time.time() - t0, 1), "detail": detail})
        log(f"  [{name}] {detail} ({time.time() - t0:.0f}s)")
    summary = {"chapter": chapter, "outcome": outcome, "stages": results,
               "total_seconds": round(sum(r["seconds"] for r in results), 1)}
    out = work_root(ctx.root) / "video" / f"ch{chapter}" / "pipeline.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, json.dumps(summary, indent=1, ensure_ascii=False))
    return summary
