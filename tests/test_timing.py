"""Phase 0: the per-stage timing log (manhwa_ocr/timing.py + the cli hook)."""

import json
from types import SimpleNamespace

import pytest

from manhwa_ocr import cli, timing


# --------------------------------------------------------------- recorder

def test_record_and_read_roundtrip(tmp_path):
    timing.record(tmp_path, stage="prepare", chapter="3",
                  started="2026-10-22T00:00:00+00:00", duration_s=12.3456,
                  ok=True, exit_code=0)
    timing.record(tmp_path, stage="video plan", chapter="3",
                  started="2026-10-22T00:01:00+00:00", duration_s=4.0,
                  ok=False, exit_code=2)
    rows = timing.read(tmp_path)
    assert [r["stage"] for r in rows] == ["prepare", "video plan"]
    assert rows[0]["chapter"] == "3"
    assert rows[0]["duration_s"] == 12.346
    assert rows[0]["ok"] is True and rows[0]["exit"] == 0
    assert rows[1]["ok"] is False and rows[1]["exit"] == 2
    assert rows[0]["tool"]["app"] and rows[0]["tool"]["python"]
    # the file lives under .work/, never in the session root
    assert (tmp_path / ".work" / "timings.json").is_file()
    assert not (tmp_path / "timings.json").exists()


def test_read_missing_and_corrupt(tmp_path):
    assert timing.read(tmp_path) == []
    p = tmp_path / ".work" / "timings.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert timing.read(tmp_path) == []
    # a corrupt log must not stop new records from being written
    timing.record(tmp_path, stage="status", started="t", duration_s=0.1, ok=True)
    assert [r["stage"] for r in timing.read(tmp_path)] == ["status"]


def test_record_never_raises(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    # session_path is a file: mkdir/read/write all fail -> swallowed
    timing.record(blocker, stage="status", started="t", duration_s=0.1, ok=True)


def test_record_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(timing, "_MAX_RECORDS", 3)
    for i in range(5):
        timing.record(tmp_path, stage=f"s{i}", started="t", duration_s=0.1, ok=True)
    assert [r["stage"] for r in timing.read(tmp_path)] == ["s2", "s3", "s4"]


# --------------------------------------------------------------- arg mapping

def test_stage_and_chapter_names():
    assert timing.stage_name(SimpleNamespace(command="prepare")) == "prepare"
    assert timing.stage_name(SimpleNamespace(command="video", action="plan")) == "video plan"
    assert timing.stage_name(SimpleNamespace(command="video", action=None)) == "video"
    assert timing.chapter_of(SimpleNamespace(chapter="7")) == "7"
    assert timing.chapter_of(SimpleNamespace(chapter=None, chapters="1-3")) == "1-3"
    assert timing.chapter_of(SimpleNamespace()) is None


# --------------------------------------------------------------- cli hook

def _make_session(tmp_path):
    from manhwa_ocr.sessions import SessionStore

    store = SessionStore(tmp_path)
    sess = store.create("Timing Series")
    return sess


def test_cli_records_stage_runs(tmp_path):
    sess = _make_session(tmp_path)
    code = cli.main(["status", "--sessions-root", str(tmp_path)])
    assert code == 0
    code = cli.main(["video", "status", "--session", sess.slug,
                     "--sessions-root", str(tmp_path)])
    assert code == 0
    rows = timing.read(sess.path)
    assert [r["stage"] for r in rows] == ["status", "video status"]
    assert all(r["ok"] for r in rows)
    assert all(r["duration_s"] >= 0 for r in rows)


def test_cli_records_failures_too(tmp_path):
    sess = _make_session(tmp_path)
    # video plan for a chapter that does not exist -> a clean error exit (2)
    code = cli.main(["video", "plan", "--chapter", "9",
                     "--session", sess.slug, "--sessions-root", str(tmp_path)])
    assert code != 0
    rows = timing.read(sess.path)
    assert rows and rows[-1]["stage"] == "video plan"
    assert rows[-1]["ok"] is False
    assert rows[-1]["chapter"] == "9"


def test_cli_without_session_records_nothing(tmp_path):
    # init writes a config file where told; no session is involved
    target = tmp_path / "cfg.yaml"
    code = cli.main(["init", str(target)])
    assert code == 0
    assert not list(tmp_path.rglob("timings.json"))
