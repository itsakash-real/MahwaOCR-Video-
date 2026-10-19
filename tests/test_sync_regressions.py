"""Real drift cases from the Lumberjack chapters, trimmed to fixtures.

Each case is a window of the chapter's alignment segments around a sentence
the BEFORE sync-audit flagged (or, for the early case, found on screen far
before its words). The old plan's arrival for the panel is recorded so the
fixture proves it reproduced the bug; the rebuilt timing must then keep every
cut inside [sentence_start - lead, sentence_start + max_late] and land each
panel on its own piece's words.

Provenance (scratch audit of video/chapter_N.plan.json vs alignment):
  ch1 s125  "In a stone cell behind iron bars..."  tagged P051-01 arrived +11.7s late  [min_hold_shift]
  ch2 s2    "That's the number Jack has to say..." tagged P001-09 arrived +6.4s late   [min_hold_shift]
  ch4 s7    "Rem standing over him..."             tagged P003-07 arrived +10.0s late  [min_hold_shift]
  ch2 s23   "The other lumberjacks crowd in..."    tagged P003-06 was on screen 4.6s
            before its sentence started (the plan had run ahead)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video.plan import hold_groups, units_from_alignment  # noqa: E402

SETTINGS = {"lead": 0.2, "max_late": 0.3}


CH1_S119_131 = [
    {"sentence": 119, "text": "Survive one hundred expeditions, and freedom is granted.",
     "start": 258.4, "end": 261.77, "display_action": "SHOW", "selected_panel": "P046-04",
     "selected_by_align": "P046-04", "tagged_panels": ["P046-04"], "sequence": ["P046-04", "P047-01"]},
    {"sentence": 120, "text": "Each run carves another tally into the collar.",
     "start": 261.77, "end": 264.24, "display_action": "SHOW", "selected_panel": "P047-02",
     "selected_by_align": "P047-02", "tagged_panels": ["P047-02"], "sequence": ["P047-02"]},
    {"sentence": 121, "text": "Jack looks at his number and understands it isn't a number at all.",
     "start": 264.24, "end": 267.4, "display_action": "SHOW", "selected_panel": "P048-01",
     "selected_by_align": "P048-01", "tagged_panels": ["P048-01"], "sequence": ["P048-01", "P048-02"]},
    {"sentence": 122, "text": "It's the count of every time he's stood his life on the line.",
     "start": 267.4, "end": 270.64, "display_action": "SHOW", "selected_panel": "P048-04",
     "selected_by_align": "P048-04", "tagged_panels": ["P048-04"], "sequence": ["P048-03", "P048-04"]},
    {"sentence": 123, "text": "And now there's only one left.",
     "start": 270.64, "end": 271.96, "display_action": "SHOW", "selected_panel": "P049-03",
     "selected_by_align": "P049-03", "tagged_panels": ["P049-03"], "sequence": ["P049-01", "P049-03"]},
    {"sentence": 124, "text": "One expedition to go.",
     "start": 271.96, "end": 273.4, "display_action": "SHOW", "selected_panel": "P050-01",
     "selected_by_align": "P050-01", "tagged_panels": ["P050-01"], "sequence": ["P049-02", "P050-01"]},
    {"sentence": 125, "text": "In a stone cell behind iron bars, a brown-haired man sits and watches.",
     "start": 273.4, "end": 277.78, "display_action": "SHOW", "selected_panel": "P051-01",
     "selected_by_align": "P051-01", "tagged_panels": ["P051-01"], "sequence": ["P050-03", "P051-01"]},
    {"sentence": 126, "text": "Quite rare, he says.",
     "start": 277.78, "end": 279.0, "display_action": "SHOW", "selected_panel": "P051-02",
     "selected_by_align": "P051-02", "tagged_panels": ["P051-02"], "sequence": ["P051-02"]},
    {"sentence": 127, "text": "Seeing kids reach a hundred in a row like this.",
     "start": 279.0, "end": 281.36, "display_action": "SHOW", "selected_panel": "P051-02",
     "selected_by_align": "P051-02", "tagged_panels": [], "sequence": ["P051-02"]},
    {"sentence": 128, "text": "Things are about to get busy.",
     "start": 281.36, "end": 282.8, "display_action": "SHOW", "selected_panel": "P051-03",
     "selected_by_align": "P051-03", "tagged_panels": ["P051-03"], "sequence": ["P051-03"]},
    {"sentence": 129, "text": "And he's got a file on Jack.",
     "start": 282.8, "end": 284.8, "display_action": "SHOW", "selected_panel": "P052-01",
     "selected_by_align": "P052-01", "tagged_panels": ["P052-01"], "sequence": ["P052-01"]},
    {"sentence": 130, "text": "So that's the question this chapter leaves you with.",
     "start": 284.8, "end": 287.28, "display_action": "SHOW", "selected_panel": "P052-01",
     "selected_by_align": "P052-01", "tagged_panels": ["P050-03"], "sequence": ["P052-01"]},
    {"sentence": 131, "text": "Jack is one run away from freedom.",
     "start": 287.28, "end": 292.01, "display_action": "SHOW", "selected_panel": "P052-01",
     "selected_by_align": "P052-01", "tagged_panels": [], "sequence": ["P052-01"]},
]


CH2_S1_8 = [
    {"sentence": 1, "text": "Five dead.",
     "start": 0.16, "end": 0.97, "display_action": "SHOW", "selected_panel": "P001-04",
     "selected_by_align": "P001-04", "tagged_panels": ["P001-04"],
     "sequence": ["P001-03", "P001-04", "P001-08"]},
    {"sentence": 2, "text": "That's the number Jack has to say out loud the second he gets back.",
     "start": 0.97, "end": 4.96, "display_action": "SHOW", "selected_panel": "P001-09",
     "selected_by_align": "P001-09", "tagged_panels": ["P001-09"],
     "sequence": ["P001-05", "P001-07", "P001-09"]},
    {"sentence": 3, "text": "Ten lumberjacks went in.",
     "start": 4.96, "end": 6.3, "display_action": "SHOW", "selected_panel": "P002-02",
     "selected_by_align": "P002-02", "tagged_panels": [],
     "sequence": ["P001-06", "P001-11", "P002-02"]},
    {"sentence": 4, "text": "Four walked out unharmed, one injured, five in bags.",
     "start": 6.3, "end": 9.28, "display_action": "SHOW", "selected_panel": "P002-02",
     "selected_by_align": "P002-02", "tagged_panels": [],
     "sequence": ["P001-10", "P002-01", "P002-02"]},
    {"sentence": 5, "text": "And the man behind the desk doesn't even look up.",
     "start": 9.28, "end": 16.58, "display_action": "SHOW", "selected_panel": "P002-02",
     "selected_by_align": "P002-02", "tagged_panels": [],
     "sequence": ["P001-12", "P002-02", "P002-03"]},
    {"sentence": 6, "text": "Then he leans across the counter and asks Jack what kind of idiot.",
     "start": 16.58, "end": 21.22, "display_action": "SHOW", "selected_panel": "P001-11",
     "selected_by_align": "P002-05", "tagged_panels": ["P001-11"],
     "sequence": ["P002-04", "P002-05"]},
    {"sentence": 7, "text": "And here's the thing - Jack doesn't argue.",
     "start": 21.22, "end": 23.52, "display_action": "SHOW", "selected_panel": "P002-05",
     "selected_by_align": "P002-05", "tagged_panels": [], "sequence": ["P002-05", "P002-06"]},
    {"sentence": 8, "text": "He just stands there.",
     "start": 23.52, "end": 23.88, "display_action": "SHOW", "selected_panel": "P002-05",
     "selected_by_align": "P002-05", "tagged_panels": [], "sequence": ["P002-05", "P002-07"]},
]


CH4_S3_12 = [
    {"sentence": 3, "text": "And here's the thing that tells you who Jack is.",
     "start": 15.17, "end": 17.52, "display_action": "SHOW", "selected_panel": "P002-06",
     "selected_by_align": "P002-06", "tagged_panels": ["P002-06"],
     "sequence": ["P001-06", "P001-09", "P002-06"]},
    {"sentence": 4, "text": "He doesn't stay down.",
     "start": 17.52, "end": 18.6, "display_action": "SHOW", "selected_panel": "P002-06",
     "selected_by_align": "P002-06", "tagged_panels": [],
     "sequence": ["P002-04", "P002-05", "P002-06"]},
    {"sentence": 5, "text": "He plants his hand on the dirt and thinks: now's not the time for me to feel sad.",
     "start": 18.6, "end": 30.1, "display_action": "SHOW", "selected_panel": "P002-08",
     "selected_by_align": "P002-08", "tagged_panels": ["P002-08"],
     "sequence": ["P002-07", "P002-08", "P003-01"]},
    {"sentence": 6, "text": "Because we flash back to the bunk room.",
     "start": 30.1, "end": 32.1, "display_action": "SHOW", "selected_panel": "P003-04",
     "selected_by_align": "P003-04", "tagged_panels": ["P003-04"],
     "sequence": ["P003-02", "P003-04", "P003-05"]},
    {"sentence": 7, "text": "Rem standing over him, hands on his hips, asking the simplest question.",
     "start": 32.1, "end": 42.4, "display_action": "SHOW", "selected_panel": "P003-07",
     "selected_by_align": "P003-07", "tagged_panels": ["P003-07"],
     "sequence": ["P003-03", "P003-07"]},
    {"sentence": 8, "text": "He'll come back and give Rem that answer after he reaches the summit.",
     "start": 42.4, "end": 47.57, "display_action": "SHOW", "selected_panel": "P004-02",
     "selected_by_align": "P004-02", "tagged_panels": ["P004-02"],
     "sequence": ["P003-06", "P004-02"]},
    {"sentence": 9, "text": "After the top of the Demon God Tree.",
     "start": 47.57, "end": 49.6, "display_action": "SHOW", "selected_panel": "P004-02",
     "selected_by_align": "P004-02", "tagged_panels": [], "sequence": ["P003-08", "P004-02"]},
    {"sentence": 10, "text": "That's the promise he's carrying into this chapter.",
     "start": 49.6, "end": 53.46, "display_action": "SHOW", "selected_panel": "P004-05",
     "selected_by_align": "P004-05", "tagged_panels": [],
     "sequence": ["P004-03", "P004-04", "P004-05"]},
    {"sentence": 11, "text": "Tom comes crashing through the doorway shouting for everyone.",
     "start": 53.46, "end": 57.6, "display_action": "SHOW", "selected_panel": "P004-06",
     "selected_by_align": "P004-06", "tagged_panels": ["P004-06"],
     "sequence": ["P004-06", "P004-07"]},
    {"sentence": 12, "text": "A monster showed up inside the city last night.",
     "start": 57.6, "end": 60.21, "display_action": "SHOW", "selected_panel": "P005-01",
     "selected_by_align": "P005-01", "tagged_panels": ["P005-01"],
     "sequence": ["P005-01", "P005-02"]},
]


CH2_S18_28 = [
    {"sentence": 18, "text": "Could I have saved the others too?",
     "start": 46.64, "end": 48.4, "display_action": "SHOW", "selected_panel": "P003-07",
     "selected_by_align": "P003-07", "tagged_panels": [], "sequence": ["P003-07"]},
    {"sentence": 19, "text": "Then he makes himself a promise: everyone makes it back alive.",
     "start": 48.4, "end": 53.48, "display_action": "SHOW", "selected_panel": "P002-09",
     "selected_by_align": "P003-08", "tagged_panels": ["P002-09"], "sequence": ["P003-08"]},
    {"sentence": 20, "text": "And then somebody screams his name.",
     "start": 53.48, "end": 55.3, "display_action": "SHOW", "selected_panel": "P003-01",
     "selected_by_align": "P003-08", "tagged_panels": ["P003-01"], "sequence": ["P003-08"]},
    {"sentence": 21, "text": "It's Rem.",
     "start": 55.3, "end": 56.05, "display_action": "SHOW", "selected_panel": "P003-05",
     "selected_by_align": "P003-08", "tagged_panels": ["P003-05"], "sequence": ["P003-08"]},
    {"sentence": 22, "text": "White hair, gray skin, collar stamped one hundred - he slams into Jack with a hug.",
     "start": 56.05, "end": 62.64, "display_action": "SHOW", "selected_panel": "P003-08",
     "selected_by_align": "P003-08", "tagged_panels": [], "sequence": ["P003-08"]},
    {"sentence": 23, "text": "The other lumberjacks crowd in, shaking his hand, asking how the final run went.",
     "start": 62.64, "end": 67.2, "display_action": "SHOW", "selected_panel": "P003-06",
     "selected_by_align": "P003-06", "tagged_panels": ["P003-06"], "sequence": ["P003-04", "P003-06"]},
    {"sentence": 24, "text": "One of them says the quiet part out loud - a lot of the guys didn't make it.",
     "start": 67.2, "end": 71.12, "display_action": "SHOW", "selected_panel": "P003-09",
     "selected_by_align": "P003-09", "tagged_panels": ["P003-09"], "sequence": ["P003-09"]},
    {"sentence": 25, "text": "And the hooded one answers: don't be too hard on yourself.",
     "start": 71.12, "end": 75.12, "display_action": "SHOW", "selected_panel": "P003-09",
     "selected_by_align": "P003-09", "tagged_panels": [], "sequence": ["P003-09"]},
    {"sentence": 26, "text": "Those of us still breathing have to find a way to be happy.",
     "start": 75.12, "end": 78.16, "display_action": "SHOW", "selected_panel": "P003-10",
     "selected_by_align": "P003-10", "tagged_panels": [], "sequence": ["P003-10"]},
    {"sentence": 27, "text": "Then Jack says it.",
     "start": 78.16, "end": 79.36, "display_action": "SHOW", "selected_panel": "P004-01",
     "selected_by_align": "P004-01", "tagged_panels": ["P004-01"], "sequence": ["P004-01", "P004-04"]},
    {"sentence": 28, "text": "Rem, as of today, you're a lumberjack who completed one hundred expeditions.",
     "start": 79.36, "end": 83.9, "display_action": "SHOW", "selected_panel": "P004-01",
     "selected_by_align": "P004-01", "tagged_panels": [], "sequence": ["P004-01"]},
]


CASES = [
    pytest.param(CH1_S119_131, 125, "P051-01", 285.1,
                 id="ch1-s125-tagged-11.7s-late"),
    pytest.param(CH2_S1_8, 2, "P001-09", 7.37,
                 id="ch2-s2-tagged-6.4s-late"),
    pytest.param(CH4_S3_12, 7, "P003-07", 42.1,
                 id="ch4-s7-tagged-10.0s-late"),
    pytest.param(CH2_S18_28, 23, "P003-06", 57.98,
                 id="ch2-s23-panel-4.6s-early"),
]


def _arrival(shots, panel):
    best = None
    for s in shots:
        for i, p in enumerate(s["panels"]):
            if p == panel:
                t = float(s["start"]) + (float(s["second_at"]) if i else 0.0)
                best = t if best is None else min(best, t)
    return best


def _piece_start(seg, panel):
    seq = seg["sequence"] or [seg["selected_panel"]]
    idx = seq.index(panel)
    return seg["start"] + (seg["end"] - seg["start"]) * idx / len(seq)


@pytest.mark.parametrize("segments,sentence,panel,old_arrival", CASES)
def test_the_old_plan_violated_the_budget_and_the_rebuild_does_not(segments, sentence, panel, old_arrival):
    seg = next(s for s in segments if s["sentence"] == sentence)
    target = _piece_start(seg, panel)

    # The fixture reproduces the bug: the old plan showed this panel well
    # outside [sentence_start - lead, sentence_start + max_late].
    assert old_arrival < target - SETTINGS["lead"] - 0.05 or old_arrival > target + SETTINGS["max_late"] + 0.05

    units = units_from_alignment([dict(s) for s in segments])
    shots, stats = hold_groups(units, 3.0, lead=SETTINGS["lead"], max_late=SETTINGS["max_late"])

    # Every cut sits inside its own budget (the trimmed first shot starts at 0).
    for s in shots[1:]:
        s0 = float(s["sentence_starts"][0])
        assert s0 - SETTINGS["lead"] - 1e-6 <= float(s["start"]) <= s0 + SETTINGS["max_late"] + 1e-6, s.get("id")

    # The panel arrives on its own piece's words, not seconds away.
    t = _arrival(shots, panel)
    assert t is not None, f"{panel} must still be shown"
    assert target - SETTINGS["lead"] - 0.05 <= t <= target + SETTINGS["max_late"] + 0.05
