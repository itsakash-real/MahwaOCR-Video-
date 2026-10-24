"""Tests for the mechanical pre-synthesis TTS-readiness lint (video/tts_lint.py)."""

import pytest

from manhwa_ocr.util import UserError
from manhwa_ocr.video import tts as tts_mod
from manhwa_ocr.video.tts_lint import format_report, lint_sentence, lint_sentences


def test_clean_sentence_has_no_issues():
    assert lint_sentence(1, "A clean, well-formed sentence.") == []


def test_digits_flagged():
    issues = lint_sentence(1, "He trained for 5 years.")
    assert any(i.code == "digits" for i in issues)


def test_dash_pause_flagged():
    issues = lint_sentence(1, "He paused -- then he ran.")
    assert any(i.code == "dash-pause" for i in issues)


def test_em_dash_pause_flagged():
    issues = lint_sentence(1, "He paused — then he ran.")
    assert any(i.code == "dash-pause" for i in issues)


def test_ellipsis_flagged():
    issues = lint_sentence(1, "He wasn't sure...")
    assert any(i.code == "ellipsis" for i in issues)


def test_double_punctuation_flagged():
    issues = lint_sentence(1, "Wait, what?!")
    assert any(i.code == "double-punct" for i in issues)


def test_parenthetical_aside_ending_in_period_is_not_flagged_unterminated():
    issues = lint_sentence(1, "She finally made it (after all that time.)")
    assert not any(i.code == "unterminated" for i in issues)


def test_quoted_sentence_ending_is_not_flagged_unterminated():
    issues = lint_sentence(1, 'He said, "I am done."')
    assert not any(i.code == "unterminated" for i in issues)


def test_unterminated_sentence_flagged():
    issues = lint_sentence(1, "He ran until he couldn't anymore")
    assert any(i.code == "unterminated" for i in issues)


def test_unbalanced_quotes_flagged():
    issues = lint_sentence(1, 'He said "I\'m done.')
    assert any(i.code == "quotes" for i in issues)


def test_run_on_sentence_flagged():
    text = " ".join(["word"] * 31) + "."
    issues = lint_sentence(1, text)
    assert any(i.code == "run-on" for i in issues)


def test_empty_sentence_is_skipped():
    assert lint_sentence(1, "   ") == []


def test_lint_sentences_collects_across_all_sentences_in_order():
    issues = lint_sentences(["Clean one.", "Bad one 5", "Also clean."])
    assert [i.n for i in issues] == [2, 2]  # digits + unterminated


def test_format_report_includes_sentence_number_and_fix():
    issues = lint_sentence(3, "5")
    report = format_report(issues)
    assert "sentence 3" in report
    assert "Fix:" in report


def test_synthesize_chapter_refuses_on_lint_issues(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import json

    recaps = tmp_path / "recaps"
    recaps.mkdir(parents=True)
    cues = {"version": 1, "chapter": "1",
            "sentences": [{"text": "He trained for 5 years.", "panels": ["P001-01"], "same": False}]}
    (recaps / "chapter_1.cues.json").write_text(json.dumps(cues), encoding="utf-8")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "SECRETKEY")

    with pytest.raises(UserError, match="TTS-readiness issue"):
        tts_mod.synthesize_chapter(
            tmp_path, "1", SimpleNamespace(extra={"tts": {"model": "aura-2-thalia-en"}}),
            log=print,
                                   speak=lambda *a: (_ for _ in ()).throw(AssertionError("should not be called")))
