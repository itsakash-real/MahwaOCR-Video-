from __future__ import annotations

import json
from pathlib import Path
from PIL import Image

from manhwa_ocr.cli import build_parser
from manhwa_ocr.video.export import export_chapter, _sec_to_srt_time, _sec_to_edl_time, _sec_to_filename_time


def test_time_conversions():
    assert _sec_to_srt_time(0.0) == "00:00:00,000"
    assert _sec_to_srt_time(65.5) == "00:01:05,500"
    assert _sec_to_srt_time(3661.123) == "01:01:01,123"

    assert _sec_to_edl_time(0.0) == "00:00:00:00"
    assert _sec_to_edl_time(1.5, fps=30.0) == "00:00:01:15"

    assert _sec_to_filename_time(0.0) == "00m00s"
    assert _sec_to_filename_time(125.0) == "02m05s"


def test_cli_export_parser():
    parser = build_parser()
    args = parser.parse_args(["video", "export", "--chapter", "1", "--format", "capcut"])
    assert args.command == "video"
    assert args.action == "export"
    assert args.chapter == "1"
    assert args.format == "capcut"


def test_universal_folder_export(tmp_path: Path):
    series = tmp_path / "test_series"
    series.mkdir()
    vid_dir = series / "video"
    vid_dir.mkdir()
    media_dir = series / "media" / "ch1"
    media_dir.mkdir(parents=True)
    audio_dir = series / "audio"
    audio_dir.mkdir()

    (audio_dir / "chapter_1.mp3").write_bytes(b"FAKE_AUDIO")

    clean_dir = series / ".work" / "video" / "ch1" / "panels_clean"
    clean_dir.mkdir(parents=True)
    img = Image.new("RGB", (800, 1000), (200, 50, 50))
    img.save(clean_dir / "P001-01_dummy.png")

    plan = {
        "chapter": "1",
        "duration": 10.0,
        "audio": "audio/chapter_1.mp3",
        "shots": [
            {
                "id": "S001",
                "start": 0.0,
                "end": 5.0,
                "sentence": "First sentence here.",
                "cards": [{"panel": "P001-01", "crop": [0.0, 0.0, 1.0, 1.0]}],
            },
            {
                "id": "S002",
                "start": 5.0,
                "end": 10.0,
                "sentence": "Second sentence here.",
                "cards": [{"panel": "P001-01", "crop": [0.0, 0.2, 1.0, 0.8]}],
            },
        ],
    }
    (vid_dir / "chapter_1.plan.json").write_text(json.dumps(plan), encoding="utf-8")

    out_res = export_chapter(series, "1", fmt="folder")
    assert "folder" in out_res
    export_folder = out_res["folder"]

    assert (export_folder / "timeline.csv").is_file()
    assert (export_folder / "timeline.srt").is_file()
    assert (export_folder / "timeline.edl").is_file()
    assert (export_folder / "narration.mp3").is_file()
    assert (export_folder / "images").is_dir()

    images = list((export_folder / "images").glob("*.png"))
    assert len(images) == 2

    srt_content = (export_folder / "timeline.srt").read_text(encoding="utf-8")
    assert "First sentence here." in srt_content
    assert "Second sentence here." in srt_content
