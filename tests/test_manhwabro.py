"""Manhwa Bro recap style (recap.style: manhwabro): routing, prompt, lint, and
that the older lanes are unaffected."""

import json
from pathlib import Path

from manhwa_ocr.config import load_config
from manhwa_ocr.script import (
    MANHWABRO_SYSTEM,
    POWERFANTASY_SYSTEM,
    SCRIPT_SYSTEM,
    SPEEDRUN_SYSTEM,
    RecapBuilder,
)
from manhwa_ocr.script_lint import lint_manhwabro

OPENER = (
    "Mikail casually catches the fire lance in one hand and crushes it like it is nothing. "
    "The poor bastard across from him can only stare while the whole arena goes silent. "
    "Our boy then summons an endless storm of magic missiles that flattens every defense. "
    "So, why is this brat so strong? "
    "Well, he is actually a legendary archmage who came back twenty years into the past. "
    "Unfortunately the clown of a hero ruined the final fight in the dumbest way possible."
)
CONTINUATION = (
    "Bro slams the door on his old fart of a butler and tests the one thing that matters. "
    "He has three unstable circles left, which is shameful for a Wal Perus heir. "
    "But this man is hellbent on making sure the clown's mistakes never happen again."
)


def _ws(tmp_path: Path, style="manhwabro", labels=("1", "2")):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    for c in labels:
        (ws / f"chapter_{c}.md").write_text(
            f"# Ch {c}\n\n## Scene 1 -- duel\n\n**Mikail:** \"Fight me.\"\n"
            f"*(Mikail: silver hair, blue robe, calm smirk)*\n", encoding="utf-8")
    cfg = load_config(series__workdir=str(ws), series__name="T", recap__style=style)
    return RecapBuilder(cfg, log=lambda *_: None)


def _codes(issues):
    return [i.code for i in issues]


def test_routing_and_other_lanes_unchanged(tmp_path):
    assert _ws(tmp_path / "a")._system_prompt() is MANHWABRO_SYSTEM
    assert _ws(tmp_path / "b", style="powerfantasy")._system_prompt() is POWERFANTASY_SYSTEM
    assert _ws(tmp_path / "c", style="speedrun")._system_prompt() is SPEEDRUN_SYSTEM
    assert _ws(tmp_path / "d", style="default")._system_prompt() is SCRIPT_SYSTEM


def test_prompt_carries_full_transcript_and_scope(tmp_path):
    b = _ws(tmp_path)
    p1 = b._prompt_for("1", 6, range_chapters=["1", "2"])
    p2 = b._prompt_for("2", 6, range_chapters=["1", "2"])
    assert "silver hair, blue robe, calm smirk" in p1       # descriptions reach the writer
    assert "So, why is" in p1 and "OPENS the range" in p1
    assert "CONTINUES the range" in p2


def test_lint_clean_opener_and_continuation():
    assert lint_manhwabro(OPENER, range_opener=True) == []
    assert lint_manhwabro(CONTINUATION, range_opener=False) == []


def test_lint_catches_each_rule():
    no_pivot = OPENER.replace("So, why is this brat so strong? ", "")
    assert "bro-pivot" in _codes(lint_manhwabro(no_pivot, range_opener=True))
    past = "He was reborn after his death in a past life. " + OPENER
    assert "past-life-opening" in _codes(lint_manhwabro(past, range_opener=True))
    plain = "The mage walks in. He casts a spell. The enemy falls down badly."
    assert "bro-epithet" in _codes(lint_manhwabro(plain, range_opener=False))
    assert "finale-ending" in _codes(
        lint_manhwabro(CONTINUATION + " And so ends the tale.", range_opener=False))
    assert "restart-opening" in _codes(
        lint_manhwabro("Welcome back to the story. " + CONTINUATION, range_opener=False))
    # word-bounded: "brother" / "broken" must NOT count as the epithet "bro"
    assert "bro-epithet" in _codes(lint_manhwabro("My brother was broken.", range_opener=False))


def test_finalize_rejects_bad_chapter(tmp_path):
    b = _ws(tmp_path, labels=("1",))
    b.prepare(["1"], 1)
    bad = "The mage walks in and casts a spell while the enemy falls down. " * 8
    (b.task_dir / "parts.json").write_text(
        json.dumps({"1": {"narration": bad, "hook": "h", "summary": "s"}}), encoding="utf-8")
    try:
        b.finalize()
        raised = False
    except Exception as exc:  # UserError
        raised = "manhwa-bro lint" in str(exc)
    assert raised
    assert b.recapped() == []


def test_style_doc_exists():
    root = Path(__file__).resolve().parent.parent
    doc = (root / "docs" / "RECAP_STYLE_MANHWABRO.md").read_text(encoding="utf-8")
    for needle in ("style: manhwabro", "bro-pivot", "bro-epithet"):
        assert needle in doc
