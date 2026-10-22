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


def test_a_failed_stage_that_wrote_its_own_output_is_rerun_not_skipped(tmp_path):
    # Mirrors syncaudit.make_report(): it writes its report file
    # UNCONDITIONALLY, then the caller (pipeline._sync_audit) raises on a
    # gate failure found INSIDE that already-written report. The failed
    # stage's output therefore exists and looks "fresh" by mtime alone.
    # A plain re-run (nothing else touched, no --force) must still
    # re-attempt that stage -- never silently skip a failed gate and fall
    # through to the stages after it (e.g. render).
    calls = []

    def _stages():
        stages = []
        for name in pipeline.STAGE_NAMES:
            out = tmp_path / f"{name}.out"
            inp = tmp_path / f"{name}.in"

            def make_run(n=name, o=out):
                def run(ctx):
                    calls.append(n)
                    if n == "sync-audit":
                        o.write_text("gate failed", encoding="utf-8")
                        raise UserError("sync gate failed")
                    o.write_text(f"{n} done", encoding="utf-8")
                    return f"{n} done"
                return run

            stages.append({"name": name, "inputs": lambda ctx, i=inp: [i],
                           "output": lambda ctx, o=out: o, "run": make_run()})
        return stages

    for name in pipeline.STAGE_NAMES:
        _touch(tmp_path / f"{name}.in")

    summary1 = pipeline.run_pipeline(_sess(tmp_path), "1", None, stages=_stages(),
                                     log=lambda *a: None)
    assert summary1["outcome"] == "failed"
    assert calls == ["panels", "pair", "align", "plan", "verify", "sync-audit"]

    calls.clear()
    summary2 = pipeline.run_pipeline(_sess(tmp_path), "1", None, stages=_stages(),
                                     log=lambda *a: None)
    assert calls == ["sync-audit"], "a previously-failed stage must be re-run, not skipped"
    assert summary2["outcome"] == "failed"
    assert "render" not in [s["stage"] for s in summary2["stages"] if s["status"] == "ran"]


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
