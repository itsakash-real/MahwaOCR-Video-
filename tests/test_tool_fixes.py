"""Regression tests for the tool-fix session (Lumberjack bugs).

One section per fix, so a failure points straight at the behavior that regressed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manhwa_ocr.video import sync  # noqa: E402
from manhwa_ocr.video.validate import validate  # noqa: E402

# ====================================================================== FIX 1: sync


def _w(text: str, start: float, per: float = 0.35):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "start": round(t, 3), "end": round(t + per * 0.8, 3)})
        t += per
    return out


def _timeline(sentences, words, duration, **kw):
    """Run sync.align the way align_engine does, then validate() the result."""
    res = sync.align(sentences, words, duration, **kw)
    timed = res["sentences"]
    segs = []
    for i, s in enumerate(timed):
        end = float(timed[i + 1]["start"]) if i + 1 < len(timed) else duration
        segs.append({"sentence": i + 1, "start": s["start"], "end": end,
                     "selected_panel": f"P{i + 1:03d}-01", "confidence": "tag_confirmed_in_window"})
    order = {s["selected_panel"]: i for i, s in enumerate(segs)}
    return res, segs, validate(segs, order, duration)


def _assert_clean(res, segs, report, min_seg):
    assert report["status"] == "PASS", report["problems"]
    starts = [s["start"] for s in segs]
    assert starts == sorted(starts)
    for s in segs:
        assert s["end"] - s["start"] >= min_seg - 1e-3, s
    assert segs[-1]["end"] <= 40.0 + 1e-6


def test_tokens_spell_every_integer_and_split_hyphens():
    assert sync.tokens("Number 99.") == ["number", "ninety", "nine"]
    assert sync.tokens("ninety-nine") == ["ninety", "nine"]
    assert sync.tokens("100") == ["one", "hundred"]
    assert sync.tokens("16") == ["sixteen"]
    assert sync.tokens("1,000") == ["one", "thousand"]
    assert sync.tokens("2026") == ["two", "thousand", "twenty", "six"]
    assert sync.tokens("1999") == ["nineteen", "ninety", "nine"]
    assert sync.tokens("21st") == ["twenty", "first"]


def test_digit_in_script_matches_word_whisper_heard():
    sents = [{"text": "The count begins."}, {"text": "Number 99."}, {"text": "Then it ends."}]
    heard = _w("The count begins.", 1.0) + _w("Number ninety-nine.", 4.0) + _w("Then it ends.", 7.0)
    res, segs, report = _timeline(sents, heard, 10.0)
    assert res["matched"] == 1.0                      # every sentence anchored, none interpolated
    assert segs[1]["start"] == pytest.approx(4.0, abs=0.05)
    assert report["status"] == "PASS", report["problems"]


def test_digit_100_matches_one_hundred_spoken():
    sents = [{"text": "Level 100 reached."}]
    res = sync.align(sents, _w("Level one hundred reached.", 0.5), 6.0)
    assert res["matched"] == 1.0


def test_same_anchor_cluster_is_spread_not_collapsed():
    """Whisper gave a burst of words one identical timestamp, so several short
    sentences shared one anchor: this used to give 'end is not after start'."""
    sents = [{"text": "He walked in slowly and sat down."},
             {"text": "Number 99."}, {"text": "Rem."}, {"text": "Ram."}, {"text": "Emilia."},
             {"text": "Then everything went quiet for a long while after that."}]
    words = _w("He walked in slowly and sat down.", 1.0)
    burst = [{"w": w, "start": 5.0, "end": 5.0} for w in "Number ninety nine Rem Ram Emilia".split()]
    words += burst + _w("Then everything went quiet for a long while after that.", 9.0)
    res, segs, report = _timeline(sents, words, 20.0)
    assert res["spread"] >= 2
    starts = [s["start"] for s in segs]
    assert len(set(starts)) == len(starts)
    _assert_clean(res, segs, report, 0.25)
    # The window is shared in proportion to word counts: "Number 99." (3 words) gets more than "Rem." (1).
    assert (segs[2]["start"] - segs[1]["start"]) > (segs[3]["start"] - segs[2]["start"]) - 1e-6


def test_run_of_one_word_sentences_gets_minimum_duration():
    names = ["Rem", "Ram", "Emilia", "Beatrice", "Roswaal", "Subaru", "Felt", "Reinhard"]
    sents = [{"text": "They all gathered in the hall."}] + [{"text": f"{n}."} for n in names] + \
            [{"text": "Nobody spoke after that."}]
    words = _w("They all gathered in the hall.", 0.5) + _w(" ".join(names), 3.0, per=0.12) + \
        _w("Nobody spoke after that.", 6.0)
    res, segs, report = _timeline(sents, words, 12.0)
    assert report["status"] == "PASS", report["problems"]
    for s in segs:
        assert s["end"] - s["start"] >= 0.25 - 1e-3


def test_unmatched_tail_cluster_never_zero_length():
    """Unheard sentences interpolated near the end of the audio."""
    sents = [{"text": "A long first sentence that is heard."}] + [{"text": f"Word{i}."} for i in range(6)]
    words = _w("A long first sentence that is heard.", 0.5)
    res, segs, report = _timeline(sents, words, 6.0)
    assert report["status"] == "PASS", report["problems"]
    assert segs[-1]["end"] - segs[-1]["start"] >= 0.25 - 1e-3


def test_min_segment_is_configurable_and_capped_by_audio_length():
    sents = [{"text": "One."}, {"text": "Two."}, {"text": "Three."}]
    res = sync.align(sents, [], 0.6, min_segment=1.0)    # impossible: 3 x 1.0s in 0.6s
    starts = [s["start"] for s in res["sentences"]]
    assert starts == sorted(starts) and len(set(starts)) == 3
    assert starts[-1] < 0.6
    res = sync.align(sents, [], 30.0, min_segment=2.0)
    s = [x["start"] for x in res["sentences"]]
    assert all(b - a >= 2.0 - 1e-6 for a, b in zip(s, s[1:]))


def test_well_spaced_sentences_are_untouched():
    sents = [{"text": "The hero wakes up."}, {"text": "Then the ground explodes."}]
    res = sync.align(sents, _w("The hero wakes up. Then the ground explodes.", 0.5), 10.0)
    assert res["spread"] == 0
    assert res["sentences"][1]["start"] == pytest.approx(0.5 + 4 * 0.35, abs=0.01)
