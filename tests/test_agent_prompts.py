"""The agent prompts have one source (manhwa_ocr/agent_prompts.py). These tests keep every copy honest."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr import agent_prompts as ap  # noqa: E402
from manhwa_ocr.cli import build_parser  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "USER_GUIDE.md").read_text(encoding="utf-8")
NAMES = list(ap.TEMPLATES)


def _everything() -> dict:
    """Every agent-facing prompt text in the program, by where it comes from."""
    from manhwa_ocr.ui.server import index_bytes
    from manhwa_ocr.video.review import CHECKLIST

    out = {f"template:{n}": ap.TEMPLATES[n] for n in NAMES}
    out.update({"guide": GUIDE, "dashboard": index_bytes().decode("utf-8"), "checklist": CHECKLIST,
                "agents": (ROOT / "AGENTS.md").read_text(encoding="utf-8")})
    return out


def test_there_are_six_prompts_and_each_starts_with_the_common_rules():
    assert NAMES == ["transcribe", "recaps", "build", "review", "intro", "fix"]
    for name, text in ap.TEMPLATES.items():
        assert text.startswith(ap.COMMON_RULES), name
    for needle in ("Read AGENTS.md first", "--session \"{session}\"", "Never create, convert, trim or generate audio",
                   "STOP and show me the exact command", ".work\\agent_log.md",
                   "Ask me before running anything with --force"):
        assert needle in ap.COMMON_RULES


@pytest.mark.parametrize("where", ["template:transcribe", "template:recaps", "template:build", "template:review",
                                   "template:intro", "template:fix", "guide", "dashboard", "checklist", "agents"])
def test_no_prompt_tells_an_agent_to_hand_edit_state_or_skip_review(where):
    text = _everything()[where]
    for bad in ('"reviewed": true', 'reviewed\\": true', "alignment_review_queue", "y0 <="):
        assert bad not in text, (where, bad)
    for line in text.splitlines():
        if re.search(r"video render[^\n`]*--force|--force[^\n`]*video render", line):   # only ever mentioned to forbid it
            assert re.search(r"do not|never|doesn't|does not", line, re.I), (where, line)


def test_every_command_in_every_prompt_exists():
    parser = build_parser()
    sub_action = next(a for a in parser._actions if getattr(a, "choices", None) and "video" in a.choices)
    commands = set(sub_action.choices)
    video_actions = set()
    for act in sub_action.choices["video"]._actions:
        if getattr(act, "choices", None) and act.dest == "action":
            video_actions = set(act.choices)
    seen = 0
    for where, text in _everything().items():
        if where == "dashboard":
            continue
        for m in re.finditer(r"python -m manhwa_ocr ([a-z]+)(?: ([a-z]+))?", text):
            cmd, sub = m.group(1), m.group(2)
            assert cmd in commands, (where, m.group(0))
            if cmd == "video" and sub:
                assert sub in video_actions, (where, m.group(0))
            seen += 1
    assert seen > 30
    for needed in ("pair", "build", "verify", "review", "approve", "render", "storyboard", "intro", "align", "plan"):
        assert needed in video_actions


def test_user_guide_contains_each_canonical_prompt_verbatim():
    for name in NAMES:
        assert ap.guide_block(name) in GUIDE, f"USER_GUIDE.md's {name} prompt differs from agent_prompts.py"
    assert "## Which prompt do I use?" in GUIDE
    assert "set \"reviewed\" yourself" not in GUIDE and "PROMPTS.md" not in GUIDE


def test_dashboard_uses_the_source_and_holds_no_copy():
    from manhwa_ocr.ui.server import index_bytes

    html = (ROOT / "manhwa_ocr" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "__AGENT_PROMPTS_JSON__" in html and "Read AGENTS.md" not in html
    served = index_bytes().decode("utf-8")
    assert "__AGENT_PROMPTS_JSON__" not in served
    for key in ("transcribe", "recaps", "build", "review", "intro", "fix"):
        assert f'"{key}"' in served
    for act in ("agent-ocr", "agent-recap", "agent-video", "agent-fix", "agent-intro"):
        assert f'data-act="{act}"' in html
    assert "Read AGENTS.md first" in served


def test_fill_only_touches_known_placeholders():
    text = ap.render("recaps", series="S", session="s", chapters="1-3", minutes=6)
    assert "--chapters 1-3 --prepare --minutes 6" in text and "{same}" in text and "{series}" not in text
    assert ap.fill("tag {P003-02} {same} {N}", N=4) == "tag {P003-02} {same} 4"


def test_review_md_and_the_reviewer_checklist_share_the_prompt_d_problem_list(tmp_path):
    from manhwa_ocr.video.review import CHECKLIST
    from manhwa_ocr.video.storyboard import write_review_task

    path = write_review_task(tmp_path, "8", {"sync": "synced", "shots": []}, "manhwa-2")
    md = path.read_text(encoding="utf-8")
    assert ap.render("review", series="manhwa-2", session="manhwa-2", N="8") in md
    for problem in ap.REVIEW_PROBLEMS:
        assert problem in md and problem in CHECKLIST
    assert "WHITE GUTTERS" in md and "SIDES CROPPED OFF" in md and "Do not hand-edit alignment or plan files" in md
    assert "final message must list every shot you changed" in md


def test_recap_task_and_next_output_use_the_shared_rules():
    from manhwa_ocr import script

    assert "RECAP_RULES" in Path(script.__file__).read_text(encoding="utf-8")
    assert "Tag EVERY sentence" in ap.RECAP_RULES and "Spell numbers as words" in ap.RECAP_RULES
    status_src = (ROOT / "manhwa_ocr" / "status.py").read_text(encoding="utf-8")
    for step in ("video verify", "video approve", "video render"):
        assert step in status_src
