"""Hybrid upgrade (AI_UPGRADE_SPECIFICATION): payload compression, key
rotation + circuit breaker, kind:auto resolution, and the agent-mode fallback
that preserves completed tiles and shards the remainder."""

import base64
import json
import types
import time
from pathlib import Path

import pytest

from manhwa_ocr.config import load_config
from manhwa_ocr.images import encode_image
from manhwa_ocr.pipeline import Pipeline
from manhwa_ocr.providers import (
    ProviderError,
    ProviderExhaustedError,
    ResilientKeyPool,
    build_provider,
)
from manhwa_ocr.util import UserError
from manhwa_ocr.workspace import chapter_stage, tile_progress


def _png(path: Path, width=900, height=1600) -> Path:
    from PIL import Image

    Image.new("RGB", (width, height), (200, 30, 30)).save(path, format="PNG")
    return path


# ---------------------------------------------------------- payload (3.2)


def test_encode_image_outputs_compressed_jpeg_under_400kb(tmp_path):
    tile = _png(tmp_path / "tile.png")
    data, media = encode_image(tile, max_width=800, jpeg_quality=85)
    assert media == "image/jpeg"
    raw = base64.b64decode(data)
    assert len(raw) < 400 * 1024, f"{len(raw) / 1024:.0f} KB exceeds the 400 KB budget"
    assert len(raw) < tile.stat().st_size          # strictly smaller than the PNG


def test_encode_image_downscales_to_max_width_and_flattens_alpha(tmp_path):
    from PIL import Image

    wide = tmp_path / "wide.png"
    Image.new("RGBA", (1400, 900), (10, 10, 10, 128)).save(wide, format="PNG")
    data, _ = encode_image(wide, max_width=800, jpeg_quality=85)
    out = tmp_path / "roundtrip.jpg"
    out.write_bytes(base64.b64decode(data))
    from PIL import Image as I

    with I.open(out) as img:
        assert img.width == 800 and img.height == round(900 * 800 / 1400)
        assert img.mode == "RGB"                    # alpha flattened, JPEG-safe


def test_encode_image_never_upscales_small_tiles(tmp_path):
    tile = _png(tmp_path / "small.png", width=500, height=400)
    data, _ = encode_image(tile, max_width=800, jpeg_quality=85)
    from PIL import Image

    out = tmp_path / "rt.jpg"
    out.write_bytes(base64.b64decode(data))
    with Image.open(out) as img:
        assert img.width == 500                     # unchanged


# ------------------------------------------------- key pool / breaker (3.3)


def test_pool_expands_comma_separated_keys_and_paces_rpm(monkeypatch):
    monkeypatch.setenv("TEST_KEYS", "k1,k2, k3")
    pool = ResilientKeyPool([{"kind": "gemini", "api_key_env": "TEST_KEYS", "rpm_limit": 600}])
    assert len(pool.slots) == 3
    first = pool.acquire_slot()
    second = pool.acquire_slot()
    assert first.key != second.key                  # rotation, not reuse


def test_empty_pool_raises_exhausted_in_under_a_second(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    pool = ResilientKeyPool([{"kind": "gemini", "api_key_env": "GEMINI_API_KEY"}])
    t0 = time.monotonic()
    with pytest.raises(ProviderExhaustedError):
        pool.acquire_slot()
    assert time.monotonic() - t0 < 1.0              # spec: zero backoff, < 1s


def test_rate_limit_cooldown_then_recovery(monkeypatch):
    monkeypatch.setenv("TEST_KEYS", "k1")
    pool = ResilientKeyPool([{"kind": "gemini", "api_key_env": "TEST_KEYS", "rpm_limit": 600}])
    slot = pool.acquire_slot()
    pool.report_rate_limit(slot, retry_after=0.05)  # tiny cooldown for the test
    pool.report_success(slot)
    assert pool.has_active_keys()
    time.sleep(0.06)
    assert pool.acquire_slot().key == "k1"          # cooled down and reused


def test_circuit_breaker_disables_after_consecutive_failures(monkeypatch):
    monkeypatch.setenv("TEST_KEYS", "k1")
    pool = ResilientKeyPool([{"kind": "gemini", "api_key_env": "TEST_KEYS"}],
                            circuit_breaker_threshold=3)
    slot = pool.acquire_slot()
    for _ in range(3):
        pool.report_error(slot)
    assert not pool.has_active_keys()
    with pytest.raises(ProviderExhaustedError):
        pool.acquire_slot()


def test_auth_failure_disables_key_immediately(monkeypatch):
    monkeypatch.setenv("TEST_KEYS", "k1,k2")
    pool = ResilientKeyPool([{"kind": "gemini", "api_key_env": "TEST_KEYS", "rpm_limit": 600}])
    slot = pool.acquire_slot()
    pool.report_fatal(slot)
    assert pool.acquire_slot().key == "k2"          # the dead key is skipped


# ------------------------------------------------------- kind: auto (3.3)


def test_auto_picks_first_kind_with_keys_and_its_endpoint(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    p = build_provider({"kind": "auto", "api_key_env": "GEMINI_API_KEY",
                        "fallback_providers": [
                            {"kind": "openrouter", "api_key_env": "OPENROUTER_API_KEY",
                             "vision_model": "qwen/qwen-2.5-vl-72b-instruct"}]})
    assert type(p).__name__ == "OpenAIProvider"
    assert p.vision_model == "qwen/qwen-2.5-vl-72b-instruct"
    assert p.base_url == "https://openrouter.ai/api/v1"


def test_auto_without_any_keys_raises_exhausted_fast(monkeypatch):
    for env in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    t0 = time.monotonic()
    with pytest.raises(ProviderExhaustedError):
        build_provider({"kind": "auto"})
    assert time.monotonic() - t0 < 1.0


def test_provider_routes_429_through_the_pool(monkeypatch):
    """A keyed provider must rotate to another key after a 429 instead of
    sleeping-and-retrying the same one."""
    import manhwa_ocr.providers as prov

    monkeypatch.setenv("TEST_KEYS", "k1,k2")
    state = {"calls": []}

    class FakeResponse:
        status_code = 429
        headers = {"Retry-After": "0"}
        text = ""

        def raise_for_status(self):
            import requests

            raise requests.HTTPError("429", response=self)

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        state["calls"].append(headers.get("authorization") or headers.get("x-goog-api-key"))
        if len(state["calls"]) == 1:
            return FakeResponse()
        resp = FakeResponse()
        resp.status_code = 200

        def ok():
            return None

        resp.raise_for_status = ok
        return resp

    monkeypatch.setattr(prov, "_SESSION", type("S", (), {"post": staticmethod(fake_post)})())
    p = build_provider({"kind": "openai", "api_key_env": "TEST_KEYS",
                        "base_url": "https://x.example", "rpm_limit": 600,
                        "max_retries": 3, "timeout": 5})
    out = p.text("sys", "prompt")
    assert state["calls"][0].endswith("k1") and state["calls"][1].endswith("k2")


# --------------------------------------------- hybrid fallback (3.4 / spec)


class _FailingProvider:
    """Succeeds for the first N tiles, then exhausts: simulates a quota wall
    mid-chapter."""

    def __init__(self, ok_count):
        self.ok_count = ok_count
        self.calls = 0

    def fingerprint(self):
        return "FakeProvider:test:0.0"

    def vision(self, system, prompt, images):
        from manhwa_ocr.util import UserError as _UE

        self.calls += 1
        if self.calls > self.ok_count:
            raise ProviderExhaustedError("quota blown mid-chapter")
        return json.dumps({"scene": f"scene {self.calls}", "blocks": [
            {"type": "dialogue", "speaker": None, "text": f"line {self.calls}", "truncated": False}
        ]})

    def text(self, system, prompt):
        raise AssertionError("assembly should not run in a handoff run")


def _bench_dir(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    from PIL import Image

    for i in range(3):        # 3 short UNIQUE pages (distinct colours, so no
        # two tiles share an image hash and pollute the test via the cache)
        Image.new("RGB", (600, 900), (40 * (i + 1), 30, 200 - 30 * i)).save(
            pages / f"page_{i + 1:03d}.png", format="PNG")
    return pages


def test_midrun_exhaustion_preserves_done_tiles_and_shards_the_rest(tmp_path, monkeypatch):
    from manhwa_ocr.config import load_config as lc

    pages = _bench_dir(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = lc(series__workdir=str(ws), series__name="HybridTest",
             pipeline__concurrency=2, pipeline__vision_batch=1,
             pipeline__agent_shard_tiles=2, pipeline__stall_minutes=1,
             slicing__target_height=500, slicing__min_height_to_slice=600,
             slicing__max_width=800)
    pipe = Pipeline(cfg, log=lambda *_: None)
    pipe.provider = _FailingProvider(ok_count=1)    # tile 1 reads, the rest fail

    with pytest.raises(UserError, match="Agent Mode"):
        pipe.run_chapter(chapter="1", folder=pages, keep_work=True)

    work = ws / ".work" / "ch1"
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    all_ids = [t["id"] for t in manifest["tiles"]]
    assert manifest["shards"]["count"] >= 1

    # completed tiles preserved in tile_results.json
    done = json.loads((work / "tile_results.json").read_text(encoding="utf-8"))
    done_ids = {e["id"] for e in done}
    assert len(done_ids) == 1 and done_ids <= set(all_ids)
    assert done[0]["scene"].startswith("scene")

    # shards cover exactly the UNRESOLVED tiles, TASK files exist, progress sees both halves
    got, total, _ = tile_progress(work)
    assert total == len(all_ids) and got == len(done_ids)   # shards not yet read
    shard_tasks = sorted(work.glob("TASK_*.md"))
    assert shard_tasks and (work / "TASK.md").exists()
    assert chapter_stage(ws, "1") == "read_tiles"
    unresolved_written = 0
    for f in work.glob("tile_results_shard_*.json"):
        unresolved_written += len(json.loads(f.read_text(encoding="utf-8")))
    assert unresolved_written == 0                     # agents have not run yet


def test_no_keys_at_all_shards_everything_immediately(tmp_path, monkeypatch):
    from manhwa_ocr.config import load_config as lc

    pages = _bench_dir(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    for env in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    cfg = lc(series__workdir=str(ws), series__name="Keyless",
             provider__kind="auto", provider__api_key_env="GEMINI_API_KEY",
             pipeline__agent_shard_tiles=2, pipeline__stall_minutes=1,
             slicing__target_height=500, slicing__min_height_to_slice=600)
    t0 = time.monotonic()
    pipe = Pipeline(cfg, log=lambda *_: None)          # must NOT raise
    assert pipe.provider is None
    assert time.monotonic() - t0 < 1.0                 # spec: exhaustion < 1s
    with pytest.raises(UserError, match="Agent Mode"):
        pipe.run_chapter(chapter="1", folder=pages, keep_work=True)
    work = ws / ".work" / "ch1"
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    assert not (work / "tile_results.json").exists()   # nothing was read via API
    assert len(list(work.glob("TASK_*.md"))) == manifest["shards"]["count"]


def test_hybrid_fallback_can_be_disabled(tmp_path):
    from manhwa_ocr.config import load_config as lc

    pages = _bench_dir(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = lc(series__workdir=str(ws), series__name="NoHybrid",
             pipeline__hybrid_agent_fallback=False, pipeline__concurrency=1,
             pipeline__vision_batch=1, pipeline__stall_minutes=1,
             slicing__target_height=500, slicing__min_height_to_slice=600)
    pipe = Pipeline(cfg, log=lambda *_: None)
    pipe.provider = _FailingProvider(ok_count=0)
    with pytest.raises(UserError, match="unusable responses"):
        pipe.run_chapter(chapter="1", folder=pages, keep_work=True)


def test_full_success_skips_the_handoff_entirely(tmp_path):
    from manhwa_ocr.config import load_config as lc

    pages = _bench_dir(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = lc(series__workdir=str(ws), series__name="AllGood",
             pipeline__concurrency=2, pipeline__vision_batch=2,
             pipeline__stall_minutes=1,
             slicing__target_height=500, slicing__min_height_to_slice=600)

    class _Good(_FailingProvider):
        def vision(self, system, prompt, images):
            self.calls += 1
            return json.dumps([{"scene": f"s{self.calls}", "blocks": []}] * len(images))

        def text(self, system, prompt):
            return json.dumps({"transcript": "## Scene 1 -- all fine\n\nBody text here.",
                               "summary": "s", "memory_update": {}})

    pipe = Pipeline(cfg, log=lambda *_: None)
    pipe.provider = _Good(ok_count=10 ** 6)
    result = pipe.run_chapter(chapter="1", folder=pages, keep_work=True)
    assert (ws / "chapter_1.md").exists()
    assert "Scene 1" in result.transcript


def test_inflight_budget_402_is_a_cooldown_not_a_kill(monkeypatch):
    """OpenRouter returns 402 WITH Retry-After while the key is alive (its
    in-flight credit reserve is busy): the pool must cool down and keep the
    key, not disable it. A bare 402 (real out-of-credit) still kills."""
    import manhwa_ocr.providers as prov

    monkeypatch.setenv("TEST_KEYS", "k1")
    state = {"calls": 0}

    class Resp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}

        def raise_for_status(self):
            import requests

            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code}", response=self)

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            r = Resp(402, {"Retry-After": "1"})   # in-flight budget: cooldown
            return r
        return Resp(200)

    monkeypatch.setattr(prov, "_SESSION", type("S", (), {"post": staticmethod(fake_post)})())
    p = build_provider({"kind": "openai", "api_key_env": "TEST_KEYS",
                        "base_url": "https://x.example", "rpm_limit": 600,
                        "max_retries": 3, "timeout": 5})
    out = p.text("sys", "prompt")
    assert out == "ok"
    assert p.pool.has_active_keys()          # survived the 402 cooldown

    # bare 402 without Retry-After = real billing death -> key disabled
    state["calls"] = 0
    monkeypatch.setenv("TEST_KEYS", "k1")
    p2 = build_provider({"kind": "openai", "api_key_env": "TEST_KEYS",
                         "base_url": "https://x.example", "rpm_limit": 600,
                         "max_retries": 1, "timeout": 5})

    def fake_post2(url, headers=None, json=None, timeout=None):
        return Resp(402)

    monkeypatch.setattr(prov, "_SESSION", type("S", (), {"post": staticmethod(fake_post2)})())
    try:
        p2.text("sys", "prompt")
    except Exception:
        pass
    assert not p2.pool.has_active_keys()


def test_key_wait_seconds_is_configurable(monkeypatch):
    """provider.key_wait_seconds widens the pool's cooldown patience (needed
    for OpenRouter's 120s in-flight Retry-After on small-balance keys)."""
    import time as _t

    monkeypatch.setenv("TEST_KEYS", "k1")
    from manhwa_ocr.providers import build_provider

    p = build_provider({"kind": "openai", "api_key_env": "TEST_KEYS",
                        "base_url": "https://x.example", "key_wait_seconds": 300})
    assert p.pool.max_wait == 300
    p2 = build_provider({"kind": "openai", "api_key_env": "TEST_KEYS",
                         "base_url": "https://x.example"})
    assert p2.pool.max_wait == 120.0


def test_runtime_failover_switches_to_next_auto_candidate(tmp_path, monkeypatch):
    """A provider that dies on its FIRST call (zero-credit 402) with zero
    tiles read must hand over to the next kind:auto candidate instead of
    falling straight to agent shards."""
    from manhwa_ocr import pipeline as pl
    from manhwa_ocr.config import load_config as lc
    from manhwa_ocr.providers import ProviderExhaustedError

    calls = {"built": 0}

    class DeadPool:
        def vision(self, *a):
            raise ProviderExhaustedError("credit(402)")

        def text(self, *a):
            raise AssertionError("should not assemble")

        def fingerprint(self):
            return "dead:1:0.0"

    class Good:
        auto_entry = ("openai", "OPENAI_API_KEY")

        def vision(self, system, prompt, images):
            calls.setdefault("vision", 0)
            calls["vision"] += 1
            import json as _json

            return _json.dumps([{"scene": "s", "blocks": []}] * len(images))

        def text(self, system, prompt):
            import json as _json

            return _json.dumps({"transcript": "## Scene 1 -- ok\n\nBody text.",
                                "summary": "s", "memory_update": {}})

        def fingerprint(self):
            return "good:1:0.0"

    dead = DeadPool()
    dead.auto_entry = ("gemini", "GEMINI_API_KEY")
    dead.pool = types.SimpleNamespace(has_active_keys=lambda: False)  # burned pool

    def fake_build(cfg, skip=None):
        calls["built"] += 1
        if not skip:
            return dead            # initial kind:auto resolution
        assert skip == {("gemini", "GEMINI_API_KEY")}
        return Good()              # runtime failover: next candidate

    monkeypatch.setattr(pl, "build_provider", fake_build)
    pages = _bench_dir(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = lc(series__workdir=str(ws), series__name="Failover",
             provider__kind="auto",
             pipeline__stall_minutes=1,
             slicing__target_height=500, slicing__min_height_to_slice=600)
    pipe = pl.Pipeline.__new__(pl.Pipeline)          # skip real __init__ provider build
    pipe.cfg = cfg
    pipe.log = lambda *_: None
    pipe.workdir = ws
    pipe.memory_path = ws / "series_memory.json"
    from manhwa_ocr.memory import SeriesMemory

    pipe.memory = SeriesMemory.load(pipe.memory_path, "Failover")
    pipe.cache_dir = ws / ".cache"
    pipe.provider = dead
    result = pipe.run_chapter(chapter="1", folder=pages, keep_work=True)
    assert (ws / "chapter_1.md").exists()
    assert calls["vision"] >= 1        # the second provider did the reading
