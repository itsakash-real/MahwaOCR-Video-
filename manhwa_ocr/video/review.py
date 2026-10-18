"""The AI review of a chapter's video, from the terminal (no IDE agent needed).

The reviewer (any vision model in manhwa-ocr.yaml's `provider`; DeepSeek flash
is the default there) looks at the storyboard sheets together with the
narration, and answers with strict JSON: approval plus fixes in the shape
fixes.py understands. The code applies the fixes, rebuilds the plan, and asks
again, up to max_rounds. It approves only when the reviewer says the chapter is
clean. Nothing is approved on a failed or unparseable reply.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List

from .. import agent_prompts
from ..util import UserError, atomic_write_text, require_chapter
from ..workspace import work_root
from . import fixes as fixes_mod
from .media import video_dir
from .panels import load_panels
from .plan import load_plan
from .storyboard import storyboard_paths

SYSTEM = (
    "You review the storyboard of a manhwa recap video. The storyboard shows every shot's "
    "picture, with its time, its panel id and the narration sentence spoken over it. You "
    "answer with strict JSON only."
)

# The numbered problem list is agent_prompts.REVIEW_PROBLEMS: the same items prompt D shows an IDE agent.
CHECKLIST = agent_prompts.review_checklist() + """
Answer with ONE JSON object and nothing else:
{"approved": true or false,
 "drop":  [{"panel": "P016-06", "reason": "sound-effect lettering only"}],
 "face":  [{"panel": "P017-02", "top": 0.12, "bottom": 0.46}],
 "crop":  [{"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}],
 "notes": "one line per problem, with the shot id"}
Set "approved": true only when every shot passes checks 1 to 9 with no fixes needed.
"""


def narration_lines(root: Path, chapter: str, plan: Dict[str, Any]) -> str:
    """One line per shot: time, panels, and the words spoken over it."""
    lines = []
    for s in plan.get("shots") or []:
        panels = " + ".join(c.get("panel", "?") for c in s.get("cards") or [])
        said = (s.get("sentence") or "(no sentence: even timing)").replace("\n", " ")
        lines.append(f"{s.get('id')}  {float(s['start']):.1f}-{float(s['end']):.1f}s  {panels}  :  {said}")
    return "\n".join(lines)


def write_narration(root: Path, chapter: str, plan: Dict[str, Any]) -> Path:
    path = work_root(root) / "video" / f"ch{require_chapter(chapter)}" / "NARRATION.md"
    atomic_write_text(path, f"# Chapter {chapter}: shot list with narration\n\n```\n{narration_lines(root, chapter, plan)}\n```\n")
    return path


def parse_reply(text: str) -> Dict[str, Any]:
    """The reviewer's JSON object. Raises UserError with the text when there is none."""
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        raise UserError(f"The reviewer's reply had no JSON object:\n{(text or '')[:400]}")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise UserError(f"The reviewer's reply was not valid JSON ({exc}):\n{m.group(0)[:400]}") from None
    if not isinstance(data, dict) or "approved" not in data:
        raise UserError("The reviewer's JSON must have an \"approved\" field.")
    return data


def merge(current: Dict[str, Any], reply: Dict[str, Any]) -> Dict[str, Any]:
    """Add the reviewer's entries to the chapter's fixes. A later entry for the same
    panel and kind replaces the earlier one."""
    out = {"version": fixes_mod.VERSION, "drop": [], "face": [], "crop": [], "shot": []}
    for kind, key in (("drop", "panel"), ("face", "panel"), ("crop", "panel"), ("shot", "panel")):
        by_panel = {e.get(key): e for e in (current.get(kind) or []) if isinstance(e, dict)}
        for e in reply.get(kind) or []:
            if isinstance(e, dict) and e.get(key):
                by_panel[e[key]] = e
        out[kind] = list(by_panel.values())
    return out


def approve(root: Path, chapter: str, plan: Dict[str, Any], note: str = "") -> Path:
    """Record that this exact plan was approved for rendering."""
    path = video_dir(root) / f"chapter_{require_chapter(chapter)}.approved.json"
    atomic_write_text(path, json.dumps({
        "plan_hash": plan.get("generated_hash"), "note": note,
        "approved_at": dt.datetime.now().isoformat(timespec="seconds"),
    }, indent=1))
    return path


def is_approved(root: Path, chapter: str, plan: Dict[str, Any]) -> bool:
    path = video_dir(root) / f"chapter_{require_chapter(chapter)}.approved.json"
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("plan_hash") == plan.get("generated_hash")
    except (OSError, ValueError):
        return False


def run_review(sess, chapter: str, cfg, *, provider=None, max_rounds: int = 3, dry_run: bool = False,
               log: Callable = print) -> Dict[str, Any]:
    """Review, fix, rebuild, repeat. Returns {"approved": bool, "rounds": n}."""
    from . import build as vb
    from ..providers import build_provider

    root, chapter = sess.path, require_chapter(chapter)
    review_cfg = (cfg.video or {}).get("review") or {}
    provider = provider or build_provider(cfg.provider)
    if review_cfg.get("model"):
        provider.vision_model = review_cfg["model"]
    rounds_allowed = int(review_cfg.get("max_rounds", max_rounds))
    result = {"approved": False, "rounds": 0}
    for n in range(1, rounds_allowed + 1):
        result["rounds"] = n
        log(f"=== review round {n} of {rounds_allowed} (chapter {chapter}) ===")
        plan = vb.make_plan(sess, chapter, cfg, force=True, log=lambda *_: None)
        write_narration(root, chapter, plan)
        boards = storyboard_paths(root, chapter)
        if not boards:
            raise UserError("No storyboard images were made; run  video build  first.")
        prompt = CHECKLIST + "\nNarration, shot by shot:\n" + narration_lines(root, chapter, plan)
        reply_text = provider.vision(SYSTEM, prompt, boards)
        reply = parse_reply(reply_text)
        log(f"  reviewer: {'approved' if reply['approved'] else 'changes needed'}")
        for note in str(reply.get("notes") or "").splitlines()[:20]:
            if note.strip():
                log(f"    {note.strip()}")
        if reply["approved"]:
            result["approved"] = True
            break
        if dry_run:
            log("  dry run: nothing applied")
            break
        panels = load_panels(root, chapter) or {"panels": []}
        dims = {p["id"]: (float(p.get("width") or 1), float(p.get("height") or 1)) for p in panels["panels"]}
        fp = fixes_mod.fixes_path(root, chapter)
        current = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else fixes_mod.empty()
        merged = merge(current, reply)
        _, problems = fixes_mod.validate(merged, dims)
        if problems:
            raise UserError("The reviewer proposed fixes that do not fit this chapter (nothing applied):\n  - "
                            + "\n  - ".join(problems))
        fixes_mod.save(root, chapter, merged)
        log(f"  applied: {len(reply.get('drop') or [])} drop, {len(reply.get('face') or [])} face, "
            f"{len(reply.get('crop') or [])} crop")
    if result["approved"]:
        plan = vb.make_plan(sess, chapter, cfg, force=True, log=lambda *_: None)
        approve(root, chapter, plan, note=f"approved by review after {result['rounds']} round(s)")
        log("  approved: yes, you can render the video")
    else:
        log("  not approved: the reviewer still lists problems (see the notes above). Nothing was rendered.")
    return result
