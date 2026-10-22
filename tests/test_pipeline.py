"""Build 0: the pipelining runner (manhwa_ocr/video/pipeline.py)."""

import json
import os
import time
from types import SimpleNamespace

import pytest

from manhwa_ocr.util import UserError
from manhwa_ocr.video import pipeline


def _fake_stages(root, calls, fail_at=None):
    stages = []
    for name in pipeline.STAGE_NAMES:
        out = root / f"{name}.out"
        inp = root / f"{name}.in"

        def make_run(n=name):
            def run(ctx):
                calls.append(n)
                if n == fail_at:
                    raise UserError(f"{n} broke")
                return f"{n} done"
            return run

        stages.append({
            "name": name,
            "inputs": lambda ctx, i=inp: [i],
            "output": lambda ctx, o=out: o,
            "run": make_run(),
        })
    return stages


def _sess(tmp_path):
    return SimpleNamespace(path=tmp_path, slug="demo")


def _touch(path, when=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if when is not None:
        os.utime(path, (when, when))


def test_pipeline_runs_all_stages_in_order(tmp_path):
    calls = []
    summary = pipeline.run_pipeline(_sess(tmp_path), "1", None,
                                    stages=_fake_stages(tmp_path, calls),
                                    log=lambda *a: None)
    assert calls == pipeline.STAGE_NAMES
    assert summary["outcome"] == "ok"
    assert [s["status"] for s in summary["stages"]] == ["ran"] * len(pipeline.STAGE_NAMES)
    saved = json.loads((tmp_path / ".work" / "video" / "ch1" / "pipeline.json").read_text())
    assert saved["outcome"] == "ok"


def test_pipeline_skips_fresh_stages(tmp_path):
    calls = []
    stages = _fake_stages(tmp_path, calls)
    now = time.time()
    for name in pipeline.STAGE_NAMES:
        _touch(tmp_path / f"{name}.in", when=now - 100)
        _touch(tmp_path / f"{name}.out", when=now)   # output newer than input
    summary = pipeline.run_pipeline(_sess(tmp_path), "1", None, stages=stages,
                                    log=lambda *a: None)
    assert calls == []
    assert all(s["status"] == "skipped" for s in summary["stages"])

    # an input changing re-runs just its stage (and nothing forces the rest)
    _touch(tmp_path / "plan.in", when=now + 100)
    summary = pipeline.run_pipeline(_sess(tmp_path), "1", None, stages=stages,
                                    log=lambda *a: None)
    assert calls == ["plan"]


def test_pipeline_stops_on_failure(tmp_path):
    calls = []
    stages = _fake_stages(tmp_path, calls, fail_at="plan")
    summary = pipeline.run_pipeline(_sess(tmp_path), "1", None, stages=stages,
                                    log=lambda *a: None)
    assert summary["outcome"] == "failed"
    assert calls == ["panels", "pair", "align", "plan"]
    assert summary["stages"][-1]["status"] == "failed"
    assert "plan broke" in summary["stages"][-1]["detail"]


def test_pipeline_from_stage_forces_rerun(tmp_path):
    calls = []
    stages = _fake_stages(tmp_path, calls)
    now = time.time()
    for name in pipeline.STAGE_NAMES:
        _touch(tmp_path / f"{name}.in", when=now - 100)
        _touch(tmp_path / f"{name}.out", when=now)
    summary = pipeline.run_pipeline(_sess(tmp_path), "1", None, stages=stages,
                                    from_stage="verify", log=lambda *a: None)
    assert calls == ["verify", "sync-audit", "render"]
    assert summary["outcome"] == "ok"


def test_pipeline_unknown_stage_rejected(tmp_path):
    with pytest.raises(UserError, match="Unknown stage"):
        pipeline.run_pipeline(_sess(tmp_path), "1", None, stages=_fake_stages(tmp_path, []),
                              from_stage="nope", log=lambda *a: None)
