"""Export aligned manhwa recap timeline to external video editors.

Supports:
1. CapCut Desktop (Native draft generation: timeline, cuts, audio, subtitles).
2. Universal folder (InShot, CapCut Mobile, DaVinci Resolve, Premiere Pro):
   - Sequenced, pre-cropped panel images (001_P001-04_00m00s_00m04s.png)
   - Synchronized SRT subtitles
   - Detailed timeline CSV
   - CMX 3600 EDL sequence
   - Audio narration copy
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from PIL import Image

from ..util import UserError, require_chapter


def get_capcut_draft_root() -> Optional[Path]:
    """Locate the CapCut Desktop drafts directory on Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    p = Path(local_app_data) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    return p if p.is_dir() else None


def _sec_to_srt_time(sec: float) -> str:
    hrs = int(sec // 3600)
    rem = sec % 3600
    mins = int(rem // 60)
    sec_in_min = rem % 60
    secs = int(sec_in_min)
    millis = int(round((sec_in_min - secs) * 1000))
    if millis >= 1000:
        secs += 1
        millis -= 1000
    if secs >= 60:
        mins += 1
        secs -= 60
    if mins >= 60:
        hrs += 1
        mins -= 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def _sec_to_edl_time(sec: float, fps: float = 30.0) -> str:
    hrs = int(sec // 3600)
    rem = sec % 3600
    mins = int(rem // 60)
    sec_in_min = rem % 60
    secs = int(sec_in_min)
    frames = int(round((sec_in_min - secs) * fps))
    if frames >= int(fps):
        secs += 1
        frames -= int(fps)
    if secs >= 60:
        mins += 1
        secs -= 60
    if mins >= 60:
        hrs += 1
        mins -= 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}:{frames:02d}"


def _sec_to_filename_time(sec: float) -> str:
    mins = int(sec // 60)
    secs = int(sec % 60)
    return f"{mins:02d}m{secs:02d}s"


def _find_plan(series_dir: Path, chapter: str) -> Optional[Path]:
    c = require_chapter(chapter)
    candidates = [
        series_dir / "video" / f"chapter_{c}.plan.json",
        series_dir / ".work" / "video" / f"ch{c}" / "plan.json",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _find_audio(series_dir: Path, chapter: str, plan: Dict[str, Any]) -> Optional[Path]:
    c = require_chapter(chapter)
    plan_audio = plan.get("audio")
    if plan_audio:
        p = series_dir / plan_audio
        if p.is_file():
            return p
    for ext in (".mp3", ".m4a", ".wav", ".aac"):
        p = series_dir / "audio" / f"chapter_{c}{ext}"
        if p.is_file():
            return p
    return None


def _get_panel_image(series_dir: Path, chapter: str, panel_id: str, panels_data: Dict[str, Any]) -> Optional[Image.Image]:
    c = require_chapter(chapter)
    clean_dir = series_dir / ".work" / "video" / f"ch{c}" / "panels_clean"
    if clean_dir.is_dir():
        matches = list(clean_dir.glob(f"{panel_id}_*.png"))
        if matches:
            try:
                return Image.open(matches[0]).convert("RGB")
            except Exception:
                pass
    try:
        from .panels import panel_image
        panels_list = panels_data.get("panels", [])
        matched = [p for p in panels_list if p.get("id") == panel_id]
        if matched:
            return panel_image(
                series_dir,
                c,
                matched[0],
                norm_w=panels_data.get("norm_width"),
                clean=True,
                fingerprint=panels_data.get("fingerprint"),
            )
    except Exception:
        pass
    return None


def export_chapter(
    series_dir: Path,
    chapter: str,
    *,
    fmt: str | None = None,
    out_dir: str | Path | None = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Export chapter alignment and panels to external editor formats."""
    series_dir = Path(series_dir)
    c = require_chapter(chapter)
    plan_path = _find_plan(series_dir, c)
    if not plan_path:
        raise UserError(f"No video plan found for chapter {c}. Build it first:  python -m manhwa_ocr video build --chapter {c}")

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    audio_path = _find_audio(series_dir, c, plan)
    panels_json = series_dir / "media" / f"ch{c}" / "panels.json"
    panels_data = {}
    if panels_json.is_file():
        try:
            with open(panels_json, "r", encoding="utf-8") as f:
                panels_data = json.load(f)
        except Exception:
            pass

    shots = plan.get("shots", [])
    if not shots:
        raise UserError(f"Plan for chapter {c} contains no shots.")

    results: Dict[str, Any] = {}
    effective_fmt = (fmt or "all").lower()

    # 1. Universal Folder Export (InShot / mobile NLEs)
    if effective_fmt in ("folder", "all", "16:9", "9:16"):
        folder_path = Path(out_dir) if out_dir else series_dir / "export" / f"chapter_{c}"
        images_dir = folder_path / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        csv_rows = []
        srt_entries = []
        edl_lines = [
            f"TITLE: RECAP CH_{c}",
            "FCM: NON-DROP FRAME",
            "",
        ]

        cached_panels: Dict[str, Image.Image] = {}

        for idx, shot in enumerate(shots, start=1):
            s_id = shot.get("id", f"S{idx:03d}")
            start_sec = float(shot.get("start", 0.0))
            end_sec = float(shot.get("end", 0.0))
            dur_sec = max(0.01, end_sec - start_sec)
            sentence = shot.get("sentence", "").strip()

            cards = shot.get("cards") or []
            card = cards[0] if cards else {}
            panel_id = card.get("panel", "NONE")
            crop_box = card.get("crop", [0.0, 0.0, 1.0, 1.0])

            img_filename = f"{idx:03d}_{panel_id}_{_sec_to_filename_time(start_sec)}_{_sec_to_filename_time(end_sec)}.png"
            img_path = images_dir / img_filename

            if panel_id not in cached_panels:
                p_img = _get_panel_image(series_dir, c, panel_id, panels_data)
                if p_img:
                    cached_panels[panel_id] = p_img

            p_img = cached_panels.get(panel_id)
            if p_img:
                w, h = p_img.size
                x0, y0, x1, y1 = crop_box
                box = (
                    int(max(0, min(w, x0 * w))),
                    int(max(0, min(h, y0 * h))),
                    int(max(0, min(w, x1 * w))),
                    int(max(0, min(h, y1 * h))),
                )
                if box[2] > box[0] and box[3] > box[1]:
                    cropped = p_img.crop(box)
                else:
                    cropped = p_img
                cropped.save(img_path, format="PNG")
            else:
                blank = Image.new("RGB", (1920, 1080), (30, 30, 30))
                blank.save(img_path, format="PNG")

            csv_rows.append({
                "Index": idx,
                "Shot_ID": s_id,
                "Start_Sec": round(start_sec, 3),
                "End_Sec": round(end_sec, 3),
                "Duration_Sec": round(dur_sec, 3),
                "Panel": panel_id,
                "Image_File": img_filename,
                "Narration": sentence,
            })

            if sentence:
                srt_entries.append(
                    f"{idx}\n{_sec_to_srt_time(start_sec)} --> {_sec_to_srt_time(end_sec)}\n{sentence}\n"
                )

            src_in = "00:00:00:00"
            src_out = _sec_to_edl_time(dur_sec)
            dst_in = _sec_to_edl_time(start_sec)
            dst_out = _sec_to_edl_time(end_sec)
            edl_lines.append(f"{idx:03d}  AX       V     C        {src_in} {src_out} {dst_in} {dst_out}")
            edl_lines.append(f"* FROM CLIP NAME: {img_filename}")
            if sentence:
                edl_lines.append(f"* COMMENT: {sentence[:60]}")
            edl_lines.append("")

        with open(folder_path / "timeline.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Index", "Shot_ID", "Start_Sec", "End_Sec", "Duration_Sec", "Panel", "Image_File", "Narration"])
            writer.writeheader()
            writer.writerows(csv_rows)

        with open(folder_path / "timeline.srt", "w", encoding="utf-8") as f:
            f.write("\n".join(srt_entries))

        with open(folder_path / "timeline.edl", "w", encoding="utf-8") as f:
            f.write("\n".join(edl_lines))

        if audio_path and audio_path.is_file():
            shutil.copy2(audio_path, folder_path / f"narration{audio_path.suffix}")

        log(f"  [Universal Folder] {len(shots)} shots -> {folder_path.relative_to(series_dir).as_posix() if folder_path.is_relative_to(series_dir) else folder_path}")
        results["folder"] = folder_path

    # 2. CapCut Desktop Draft Export
    if effective_fmt in ("capcut", "all"):
        capcut_root = get_capcut_draft_root()
        if capcut_root:
            now_sec = int(time.time())
            now_us = int(time.time() * 1000000)
            draft_uuid = str(uuid.uuid4()).upper()
            project_name = f"Recap_{series_dir.name}_Ch{c}"

            draft_dir = capcut_root / project_name
            draft_dir.mkdir(parents=True, exist_ok=True)
            assets_dir = draft_dir / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)

            total_duration_sec = float(plan.get("duration", 0.0))
            if total_duration_sec <= 0 and shots:
                total_duration_sec = float(shots[-1].get("end", 0.0))
            total_duration_us = int(total_duration_sec * 1000000)

            # Inherit platform IDs from existing local draft
            existing_drafts = [d for d in capcut_root.iterdir() if d.is_dir() and (d / "draft_content.json").is_file()]
            tmpl_dir = None
            for d in existing_drafts:
                if d.name != project_name and (d / "draft_content.json").is_file():
                    tmpl_dir = d
                    break

            platform_info = {
                "app_id": 359289,
                "app_source": "cc",
                "app_version": "9.5.0",
                "device_id": "c4ca4238a0b923820dcc509a6f75849b",
                "hard_disk_id": "",
                "mac_address": "99607af315a76af2a6ad8ccd1a6f8512",
                "os": "windows",
                "os_version": "10.0.26300"
            }
            last_modified_platform = dict(platform_info)

            if tmpl_dir:
                try:
                    tmpl_content = json.loads((tmpl_dir / "draft_content.json").read_text(encoding="utf-8"))
                    if "platform" in tmpl_content:
                        platform_info = tmpl_content["platform"]
                    if "last_modified_platform" in tmpl_content:
                        last_modified_platform = tmpl_content["last_modified_platform"]
                except Exception:
                    pass

                for sc in ["attachment_editing.json", "draft_agency_config.json", "draft_biz_config.json",
                           "draft_virtual_store.json", "key_value.json", "performance_opt_info.json",
                           "timeline_layout.json", "template.tmp"]:
                    src_sc = tmpl_dir / sc
                    if src_sc.is_file() and not (draft_dir / sc).is_file():
                        shutil.copy2(src_sc, draft_dir / sc)

            (draft_dir / "draft_settings").write_text(
                f"[General]\ndraft_create_time={now_sec}\ndraft_last_edit_time={now_sec}\nreal_edit_seconds=0\nreal_edit_keys=1\ncloud_last_modify_platform=windows\n",
                encoding="utf-8"
            )

            video_materials: List[Dict[str, Any]] = []
            video_segments: List[Dict[str, Any]] = []
            audio_materials: List[Dict[str, Any]] = []
            audio_segments: List[Dict[str, Any]] = []
            meta_materials_value: List[Dict[str, Any]] = []
            total_materials_size = 0

            cached_panels = {}
            first_cover_img_path = None

            for idx, shot in enumerate(shots):
                start_sec = float(shot.get("start", 0.0))
                end_sec = float(shot.get("end", 0.0))
                dur_sec = max(0.01, end_sec - start_sec)
                start_us = int(start_sec * 1000000)
                dur_us = int(dur_sec * 1000000)

                cards = shot.get("cards") or []
                card = cards[0] if cards else {}
                panel_id = card.get("panel", "NONE")
                crop_box = card.get("crop", [0.0, 0.0, 1.0, 1.0])

                img_filename = f"{idx+1:03d}_{panel_id}.png"
                img_path = assets_dir / img_filename

                if panel_id not in cached_panels:
                    p_img = _get_panel_image(series_dir, c, panel_id, panels_data)
                    if p_img:
                        cached_panels[panel_id] = p_img

                p_img = cached_panels.get(panel_id)
                if p_img:
                    w, h = p_img.size
                    x0, y0, x1, y1 = crop_box
                    box = (
                        int(max(0, min(w, x0 * w))),
                        int(max(0, min(h, y0 * h))),
                        int(max(0, min(w, x1 * w))),
                        int(max(0, min(h, y1 * h))),
                    )
                    if box[2] > box[0] and box[3] > box[1]:
                        cropped = p_img.crop(box)
                    else:
                        cropped = p_img
                    cropped.save(img_path, format="PNG")
                else:
                    blank = Image.new("RGB", (1920, 1080), (30, 30, 30))
                    blank.save(img_path, format="PNG")

                if first_cover_img_path is None:
                    first_cover_img_path = img_path

                file_size = img_path.stat().st_size
                total_materials_size += file_size
                with Image.open(img_path) as im:
                    img_w, img_h = im.size

                mat_id = str(uuid.uuid4()).upper()
                seg_id = str(uuid.uuid4()).upper()
                resolved_img_path = str(img_path.resolve()).replace("\\", "/")

                video_materials.append({
                    "aigc_history_id": "", "aigc_item_id": "", "aigc_type": "none", "audio_fade": None,
                    "beauty_body_auto_preset": None, "beauty_body_preset_id": "",
                    "beauty_face_auto_preset": {"name": "", "preset_id": "", "rate_map": "", "scene": ""},
                    "beauty_face_auto_preset_infos": [], "beauty_face_preset_infos": [], "cartoon_path": "",
                    "category_id": "", "category_name": "local", "check_flag": 62978047, "content_feature_info": None,
                    "corner_pin": None,
                    "crop": {
                        "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0, "lower_right_y": 1.0,
                        "upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0, "upper_right_y": 0.0
                    },
                    "crop_ratio": "free", "crop_scale": 1.0, "duration": 10800000000, "extra_type_option": 0,
                    "formula_id": "", "freeze": None, "has_audio": False, "has_sound_separated": False,
                    "height": img_h, "id": mat_id, "intensifies_audio_path": "", "intensifies_path": "",
                    "is_ai_generate_content": False, "is_copyright": False, "is_set_beauty_mode": False,
                    "is_text_edit_overdub": False, "is_unified_beauty_mode": False, "is_video_copilot_aigc_content": False,
                    "live_photo_cover_path": "", "live_photo_timestamp": -1, "local_id": "", "local_material_from": "",
                    "local_material_id": str(uuid.uuid4()), "material_id": "", "material_name": img_filename,
                    "material_url": "", "matting": None, "media_path": "", "multi_camera_info": None, "object_locked": None,
                    "origin_material_id": "", "path": resolved_img_path, "picture_from": "none",
                    "picture_set_category_id": "", "picture_set_category_name": "", "pre_applied_vip_materials": [],
                    "request_id": "", "reverse_intensifies_path": "", "reverse_path": "", "smart_match_info": None,
                    "smart_motion": None, "source": 0, "source_platform": 0, "stable": None, "surface_trackings": [],
                    "team_id": "", "type": "photo", "unique_id": "", "video_algorithm": None, "video_mask_shadow": None,
                    "video_mask_stroke": None, "width": img_w, "workflow_node_id": ""
                })

                video_segments.append({
                    "caption_info": None, "cartoon": False,
                    "clip": {
                        "alpha": 1.0, "flip": {"horizontal": False, "vertical": False}, "rotation": 0.0,
                        "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}
                    },
                    "color_correct_alg_result": "", "common_keyframes": [], "desc": "",
                    "digital_human_template_group_id": "", "enable_adjust": True, "enable_adjust_mask": False,
                    "enable_color_adjust_pro": False, "enable_color_correct_adjust": False, "enable_color_curves": True,
                    "enable_color_match_adjust": False, "enable_color_wheels": True, "enable_hsl": False,
                    "enable_hsl_curves": True, "enable_lut": True, "enable_mask_shadow": False, "enable_mask_stroke": False,
                    "enable_smart_color_adjust": False, "enable_video_mask": True, "extra_material_refs": [],
                    "group_id": "", "hdr_settings": None, "hdr_vivid_settings": None, "id": seg_id,
                    "intensifies_audio": False, "is_loop": False, "is_placeholder": False, "is_tone_modify": False,
                    "keyframe_refs": [], "last_nonzero_volume": 1.0, "lyric_keyframes": None, "material_id": mat_id,
                    "raw_segment_id": "", "render_index": idx, "render_timerange": {"duration": 0, "start": 0},
                    "responsive_layout": {"enable": False, "horizontal_pos_layout": 0, "size_layout": 0, "target_follow": "", "vertical_pos_layout": 0},
                    "reverse": False, "segment_color_tag": "", "source": "segmentsourcenormal",
                    "source_timerange": {"duration": dur_us, "start": 0},
                    "speed": 1.0, "state": 0,
                    "target_timerange": {"duration": dur_us, "start": start_us},
                    "template_id": "", "template_scene": "default", "track_attribute": 0, "track_render_index": 0,
                    "uniform_scale": {"on": True, "value": 1.0}, "visible": True, "volume": 1.0
                })

                meta_materials_value.append({
                    "ai_group_type": "", "create_time": now_sec, "duration": dur_us, "enter_from": 0,
                    "extra_info": img_filename, "file_Path": resolved_img_path, "height": img_h,
                    "id": str(uuid.uuid4()), "import_time": now_sec, "import_time_ms": now_us, "item_source": 1,
                    "material_color_tag": "", "md5": "", "metetype": "photo",
                    "roughcut_time_range": {"duration": dur_us, "start": 0},
                    "sub_time_range": {"duration": -1, "start": -1}, "type": 0, "width": img_w
                })

            if audio_path and audio_path.is_file():
                audio_size = audio_path.stat().st_size
                total_materials_size += audio_size
                audio_mat_id = str(uuid.uuid4()).upper()
                audio_seg_id = str(uuid.uuid4()).upper()
                resolved_audio_path = str(audio_path.resolve()).replace("\\", "/")

                audio_materials.append({
                    "ai_music_enter_from": "", "ai_music_generate_scene": 0, "ai_music_type": 0, "aigc_history_id": "",
                    "aigc_item_id": "", "app_id": 0, "category_id": "", "category_name": "local", "check_flag": 1,
                    "cloned_model_type": "", "copyright_limit_type": "none", "duration": total_duration_us,
                    "effect_id": "", "formula_id": "", "id": audio_mat_id, "intensifies_path": "",
                    "is_ai_clone_tone": False, "is_ai_clone_tone_post": False, "is_text_edit_overdub": False,
                    "is_ugc": False, "local_material_id": str(uuid.uuid4()), "lyric_type": 0, "mock_tone_speaker": "",
                    "moyin_emotion": "", "music_id": "", "music_source": "", "name": audio_path.name,
                    "path": resolved_audio_path, "pgc_id": "", "pgc_name": "", "query": "", "request_id": "",
                    "resource_id": "", "search_id": "", "similiar_music_info": {"original_song_id": "", "original_song_name": ""},
                    "sound_separate_type": "", "source_from": "", "source_platform": 0, "team_id": "", "text_id": "",
                    "third_resource_id": "", "tone_category_id": "", "tone_category_name": "", "tone_effect_id": "",
                    "tone_effect_name": "", "tone_emotion_name_key": "", "tone_emotion_role": "", "tone_emotion_scale": 0.0,
                    "tone_emotion_selection": "", "tone_emotion_style": "", "tone_platform": "", "tone_second_category_id": "",
                    "tone_second_category_name": "", "tone_speaker": "", "tone_type": "", "tts_benefit_info": {"benefit_amount": -1, "benefit_log_extra": "", "benefit_log_id": "", "benefit_type": "none"},
                    "tts_generate_scene": "", "tts_language_info": None, "tts_task_id": "", "type": "extract_music",
                    "unique_id": "", "video_id": "", "wave_points": []
                })

                audio_segments.append({
                    "caption_info": None, "cartoon": False, "clip": None, "color_correct_alg_result": "",
                    "common_keyframes": [], "desc": "", "digital_human_template_group_id": "", "enable_adjust": False,
                    "enable_adjust_mask": False, "enable_color_adjust_pro": False, "enable_color_correct_adjust": False,
                    "enable_color_curves": True, "enable_color_match_adjust": False, "enable_color_wheels": True,
                    "enable_hsl": False, "enable_hsl_curves": True, "enable_lut": False, "enable_mask_shadow": False,
                    "enable_mask_stroke": False, "enable_smart_color_adjust": False, "enable_video_mask": True,
                    "extra_material_refs": [], "group_id": "", "hdr_settings": None, "hdr_vivid_settings": None,
                    "id": audio_seg_id, "intensifies_audio": False, "is_loop": False, "is_placeholder": False,
                    "is_tone_modify": False, "keyframe_refs": [], "last_nonzero_volume": 1.0, "lyric_keyframes": None,
                    "material_id": audio_mat_id, "raw_segment_id": "", "render_index": 0,
                    "render_timerange": {"duration": 0, "start": 0},
                    "responsive_layout": {"enable": False, "horizontal_pos_layout": 0, "size_layout": 0, "target_follow": "", "vertical_pos_layout": 0},
                    "reverse": False, "segment_color_tag": "", "source": "segmentsourcenormal",
                    "source_timerange": {"duration": total_duration_us, "start": 0},
                    "speed": 1.0, "state": 0,
                    "target_timerange": {"duration": total_duration_us, "start": 0},
                    "template_id": "", "template_scene": "default", "track_attribute": 0, "track_render_index": 0,
                    "uniform_scale": None, "visible": True, "volume": 1.0
                })

                meta_materials_value.append({
                    "ai_group_type": "", "create_time": now_sec, "duration": total_duration_us, "enter_from": 0,
                    "extra_info": audio_path.name, "file_Path": resolved_audio_path, "height": 0,
                    "id": str(uuid.uuid4()), "import_time": now_sec, "import_time_ms": now_us, "item_source": 1,
                    "material_color_tag": "", "md5": "", "metetype": "music",
                    "roughcut_time_range": {"duration": total_duration_us, "start": 0},
                    "sub_time_range": {"duration": -1, "start": -1}, "type": 0, "width": 0
                })

            tracks = []
            if video_segments:
                tracks.append({
                    "attribute": 0, "flag": 0, "id": str(uuid.uuid4()).upper(), "is_default_name": True,
                    "name": "", "segments": video_segments, "type": "video"
                })
            if audio_segments:
                tracks.append({
                    "attribute": 0, "flag": 0, "id": str(uuid.uuid4()).upper(), "is_default_name": True,
                    "name": "", "segments": audio_segments, "type": "audio"
                })

            draft_content = {
                "canvas_config": {"background": None, "height": 1080, "ratio": "16:9", "width": 1920},
                "color_space": 0,
                "config": {
                    "adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1,
                    "export_range": None, "extract_audio_last_index": 1, "hdr_vivid": False,
                    "lyrics_recognition_id": "", "lyrics_sync": True, "lyrics_taskinfo": [],
                    "maintrack_adsorb": True, "material_save_mode": 0, "multi_language_current": "none",
                    "multi_language_list": [], "multi_language_main": "none", "multi_language_mode": "none",
                    "original_sound_last_index": 1, "record_audio_last_index": 1, "roughcut_adsorb": True,
                    "sticker_max_index": 1, "subtitle_recognition_id": "", "subtitle_sync": True,
                    "subtitle_taskinfo": [], "system_font_list": [], "video_mute": False, "zoom_info_params": None
                },
                "cover": None, "create_time": 0, "draft_type": "video", "duration": total_duration_us,
                "extra_info": None, "fps": 30.0, "free_render_index_mode_on": False,
                "function_assistant_info": {"audio_noise_segid_list": [], "auto_adjust": False, "auto_adjust_fixed": False, "auto_adjust_fixed_value": 50.0, "auto_adjust_type": 0, "video_noise_segid_list": []},
                "group_container": None, "id": draft_uuid, "is_drop_frame_timecode": False, "keyframe_graph_list": [],
                "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [], "handwrites": [], "stickers": [], "texts": [], "videos": []},
                "last_modified_platform": last_modified_platform, "lyrics_effects": [],
                "materials": {
                    "ai_text_effects": [], "ai_translates": [], "audio_balances": [], "audio_effects": [],
                    "audio_fades": [], "audio_pannings": [], "audio_pitch_shifts": [], "audio_track_indexes": [],
                    "audios": audio_materials, "beats": [], "canvases": [], "chromas": [], "color_curves": [],
                    "common_mask": [], "digital_human_model_dressing": [], "digital_humans": [], "drafts": [],
                    "effects": [], "flowers": [], "green_screens": [], "handwrites": [], "hsl": [], "hsl_curves": [],
                    "images": [], "log_color_wheels": [], "loudnesses": [], "manual_beautys": [], "manual_deformations": [],
                    "material_animations": [], "material_colors": [], "multi_language_refs": [], "placeholder_infos": [],
                    "placeholders": [], "plugin_effects": [], "primary_color_wheels": [], "realtime_denoises": [],
                    "shapes": [], "smart_crops": [], "smart_relights": [], "sound_channel_mappings": [], "speeds": [],
                    "stickers": [], "tail_leaders": [], "text_templates": [], "texts": [], "time_marks": [],
                    "transitions": [], "video_effects": [], "video_radius": [], "video_shadows": [], "video_strokes": [],
                    "video_trackings": [], "videos": video_materials, "vocal_beautifys": [], "vocal_separations": []
                },
                "mixed_track_mode_on": False, "mutable_config": None, "name": "", "new_version": "187.0.0",
                "path": "", "platform": platform_info, "relationships": [], "render_index_track_mode_on": False,
                "retouch_cover": None, "smart_ads_info": {"draft_url": "", "page_from": "", "routine": ""},
                "source": "default", "static_cover_image_path": "", "time_marks": None, "tracks": tracks,
                "uneven_animation_template_info": {"composition": "", "content": "", "order": "", "sub_template_info_list": []},
                "update_time": 0, "version": 360000
            }

            content_json_str = json.dumps(draft_content, ensure_ascii=False)
            (draft_dir / "draft_content.json").write_text(content_json_str, encoding="utf-8")
            (draft_dir / "draft_content.json.bak").write_text(content_json_str, encoding="utf-8")
            (draft_dir / "template-2.tmp").write_text(content_json_str, encoding="utf-8")

            cover_path = draft_dir / "draft_cover.jpg"
            if first_cover_img_path and first_cover_img_path.is_file():
                try:
                    with Image.open(first_cover_img_path) as im:
                        im.convert("RGB").save(cover_path, "JPEG", quality=85)
                except Exception:
                    Image.new("RGB", (640, 360), (40, 40, 40)).save(cover_path, "JPEG")
            else:
                Image.new("RGB", (640, 360), (40, 40, 40)).save(cover_path, "JPEG")

            resolved_draft_dir = str(draft_dir.resolve()).replace("\\", "/")
            resolved_capcut_root = str(capcut_root.resolve()).replace("\\", "/")

            draft_meta = {
                "cloud_draft_cover": False, "cloud_draft_sync": False, "cloud_package_completed_time": "",
                "draft_cloud_capcut_purchase_info": "", "draft_cloud_last_action_download": False,
                "draft_cloud_package_type": "", "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
                "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "", "draft_cover": "draft_cover.jpg",
                "draft_deeplink_url": "",
                "draft_enterprise_info": {"draft_enterprise_extra": "", "draft_enterprise_id": "", "draft_enterprise_name": "", "enterprise_material": []},
                "draft_fold_path": resolved_draft_dir, "draft_has_unfinished_aigc_video_effect": False,
                "draft_id": draft_uuid, "draft_is_ae_produce": False, "draft_is_ai_packaging_used": False,
                "draft_is_ai_shorts": False, "draft_is_ai_translate": False, "draft_is_article_video_draft": False,
                "draft_is_cloud_temp_draft": False, "draft_is_from_deeplink": "false", "draft_is_infinite_canvas_draft": False,
                "draft_is_invisible": False, "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
                "draft_materials": [
                    {"type": 0, "value": meta_materials_value},
                    {"type": 1, "value": []}, {"type": 2, "value": []}, {"type": 3, "value": []},
                    {"type": 6, "value": []}, {"type": 7, "value": []}, {"type": 8, "value": []}
                ],
                "draft_materials_copied_info": [], "draft_name": project_name, "draft_need_rename_folder": False,
                "draft_new_version": "", "draft_removable_storage_device": "", "draft_root_path": resolved_capcut_root,
                "draft_segment_extra_info": [], "draft_timeline_materials_size_": total_materials_size, "draft_type": "",
                "draft_web_article_video_enter_from": "", "pippit_avatar_url": "", "pippit_extra_info": "",
                "pippit_id": "", "pippit_user_name": "", "tm_draft_cloud_completed": "", "tm_draft_cloud_entry_id": -1,
                "tm_draft_cloud_modified": 0, "tm_draft_cloud_parent_entry_id": -1, "tm_draft_cloud_space_id": -1,
                "tm_draft_cloud_user_id": -1, "tm_draft_create": now_us, "tm_draft_modified": now_us, "tm_draft_removed": 0,
                "tm_duration": total_duration_us
            }
            (draft_dir / "draft_meta_info.json").write_text(json.dumps(draft_meta, indent=2, ensure_ascii=False), encoding="utf-8")

            root_meta_file = capcut_root / "root_meta_info.json"
            if root_meta_file.is_file():
                try:
                    root_meta = json.loads(root_meta_file.read_text(encoding="utf-8"))
                    store = [d for d in root_meta.get("all_draft_store", []) if d.get("draft_name") != project_name]

                    new_entry = {
                        "cloud_draft_cover": False, "cloud_draft_sync": False, "draft_cloud_last_action_download": False,
                        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
                        "draft_cloud_videocut_purchase_info": "",
                        "draft_cover": resolved_draft_dir + "\\draft_cover.jpg",
                        "draft_fold_path": resolved_draft_dir,
                        "draft_id": draft_uuid,
                        "draft_is_ai_shorts": False, "draft_is_cloud_temp_draft": False,
                        "draft_is_infinite_canvas_draft": False, "draft_is_invisible": False,
                        "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
                        "draft_json_file": resolved_draft_dir + "\\draft_content.json",
                        "draft_name": project_name, "draft_new_version": "",
                        "draft_root_path": resolved_capcut_root,
                        "draft_timeline_materials_size": total_materials_size, "draft_type": "",
                        "draft_web_article_video_enter_from": "", "pippit_avatar_url": "", "pippit_extra_info": "",
                        "pippit_id": "", "pippit_user_name": "", "streaming_edit_draft_ready": True,
                        "tm_draft_cloud_completed": "", "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0,
                        "tm_draft_cloud_parent_entry_id": -1, "tm_draft_cloud_space_id": -1, "tm_draft_cloud_user_id": -1,
                        "tm_draft_create": now_us, "tm_draft_modified": now_us, "tm_draft_removed": 0,
                        "tm_duration": total_duration_us
                    }
                    store.insert(0, new_entry)
                    root_meta["all_draft_store"] = store
                    root_meta["draft_ids"] = len(store)
                    root_meta_file.write_text(json.dumps(root_meta, indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

            log(f"  [CapCut Desktop] Project ready -> Open CapCut to edit '{project_name}'")
            results["capcut"] = draft_dir
        else:
            log("  [CapCut Desktop] Skipped: CapCut Desktop draft directory not found.")

    return results
