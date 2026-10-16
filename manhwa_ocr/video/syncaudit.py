"""Check a finished plan against the narration: does the picture match the voice?

`video sync-audit --chapter N` samples chapter_N.plan.json every half second and
compares the panel(s) on screen with the panel(s) the sentence playing at that
moment was tagged with (chapter_N.alignment.json). It uses no AI and no
rendering: whisper words come from the cached chapter_N.timeline.json, which the
audit also checks for staleness (audio hash, cached model) and reports the
plan's length against the audio's real length.

Every mismatch stretch longer than WINDOW_MIN seconds is reported with its
sentence, the panel shown, the panel expected and the likely cause:

  min_hold_shift        the cut for this sentence starts later than max_late
                        (min_hold retiming moved a shot boundary)
  interpolated_timing   whisper never matched this sentence; its time is a
                        text-length guess between neighbours
  filler_panel          the panel on screen was placed by the coverage pass,
                        not chosen for this sentence
  tag_override          the alignment engine replaced the writer's tag
  long_hold             one picture held past max_hold while the voice moved on
  unknown               none of the above (needs eyes)

The report goes to video/chapter_N.syncaudit.json with the calibration numbers:
strict_match (the exact tagged panel is on screen), wrong_picture (time where a
panel belonging to another sentence's tag is on screen; legitimate coverage
fillers and pair second cards are excluded) and a per-minute curve of both. The
gate `video build` enforces is on wrong-picture time and cut lateness - overall
wrong-picture share (gate_wrong), wrong-picture inside any five-second window
(gate_window), and cuts later than max_late + gate_grace.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Sequence

from ..util import UserError, atomic_write_text, require_chapter
from . import sync as sync_mod
from .align_engine import alignment_path
from .ffmpeg import audio_timing
from .media import audio_path, video_dir
from .panels import load_panels
from .plan import plan_path

SAMPLE_STEP = 0.5     # the plan is sampled every half second
WINDOW_MIN = 3.0      # mismatches shorter than this are noise, not reported windows
GATE_SPAN = 5.0       # "any five-second window" size for the gate
RUN_MIN = 5.0         # an interpolated stretch longer than this is a long run

CAUSES = ("min_hold_shift", "interpolated_timing", "filler_panel", "tag_override", "long_hold", "unknown")


def audit_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.syncaudit.json"


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _visible(shot: Dict[str, Any], t: float) -> List[str]:
    """Panels on screen inside `shot` at time t: the first card from the shot's
    start, the second once its `at` has passed."""
    out = []
    start = float(shot["start"])
    for c in shot.get("cards") or []:
        if start + float(c.get("at") or 0.0) <= t + 1e-9:
            out.append(c["panel"])
    return out


def _cut_offset(shots: Sequence[Dict[str, Any]], expected: Iterable[str], seg_start: float) -> float | None:
    """How late the first expected panel appears (0.0 if already up). None when
    no expected panel ever appears."""
    exp = set(expected)
    for shot in shots:
        for c in shot.get("cards") or []:
            if c.get("panel") in exp:
                at = float(shot["start"]) + float(c.get("at") or 0.0)
                if at <= seg_start + 1e-6:
                    return 0.0
                return at - seg_start
    return None


def _held_seconds(shots: Sequence[Dict[str, Any]], panel: str, t: float) -> float:
    """The uninterrupted stretch this panel is on screen for around time t."""
    intervals: List[List[float]] = []
    for s in shots:
        for c in s.get("cards") or []:
            if c.get("panel") == panel:
                intervals.append([float(s["start"]) + float(c.get("at") or 0.0), float(s["end"])])
    if not intervals:
        return 0.0
    intervals.sort()
    merged = [intervals[0]]
    for a, b in intervals[1:]:
        if a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in merged:
        if a - 1e-6 <= t <= b + 1e-6:
            return b - a
    return 0.0


def _expected_sets(segments: Sequence[Dict[str, Any]], text_only: set) -> List[List[str]]:
    """What each segment is supposed to show: the drawn panels it was tagged
    with (text-only tags can't be shown themselves). A hold keeps whatever the
    previous sentence legitimately showed (including coverage extras), since
    holding the shot is exactly its job. An empty expectation means the
    sentence can't be judged (nothing showable was ever named for it)."""
    exp: List[List[str]] = []
    carry: List[str] = []
    for seg in segments:
        tags = [p for p in (seg.get("tagged_panels") or []) if p not in text_only]
        if seg.get("display_action") == "SHOW":
            exp.append(list(tags))
            if tags:
                carry = list(seg.get("sequence") or tags)
        else:
            exp.append(list(carry))
    return exp


def _window_record(samples: List[Dict[str, Any]], i: int, j: int, segments: List[Dict[str, Any]],
                   shots: List[Dict[str, Any]], exp_sets: List[List[str]], interpolated: set,
                   st: Dict[str, float]) -> Dict[str, Any]:
    span = samples[i:j + 1]
    si = Counter(s["seg"] for s in span).most_common(1)[0][0]
    seg = segments[si]
    exp = exp_sets[si]
    shown_counts = Counter(p for s in span for p in s["shown"] if p not in exp)
    shown = shown_counts.most_common(1)[0][0] if shown_counts else None
    seg_start = float(seg["start"])
    offset = _cut_offset(shots, exp, seg_start)
    selected = seg.get("selected_by_align") or seg.get("selected_panel")
    sequence = set(seg.get("sequence") or ([selected] if selected else []))
    extras = sequence - ({selected} if selected else set())
    cause = "unknown"
    if offset is not None and offset > st["max_late"] + 1e-6:
        cause = "min_hold_shift"
    elif si in interpolated:
        cause = "interpolated_timing"
    elif shown and shown in extras:
        cause = "filler_panel"
    elif shown and selected and shown == selected and exp_sets[si] and selected not in exp:
        cause = "tag_override"
    elif shown and _held_seconds(shots, shown, samples[i]["t"]) > st["max_hold"] + 1e-6:
        cause = "long_hold"
    end = samples[j]["t"] + SAMPLE_STEP
    return {"start": round(samples[i]["t"], 3), "end": round(end, 3), "duration": round(end - samples[i]["t"], 3),
            "sentence": seg.get("sentence"), "text": (seg.get("text") or "")[:160],
            "shown": shown, "expected": list(exp),
            "cut_offset": None if offset is None else round(offset, 3), "cause": cause}


def audit(plan: Dict[str, Any], alignment: Dict[str, Any], words: List[Dict[str, Any]], *,
          audio: Dict[str, Any] | None = None, timeline: Dict[str, Any] | None = None,
          settings: Dict[str, float] | None = None, text_only: Iterable[str] = ()) -> Dict[str, Any]:
    """The pure check: plan + alignment + cached whisper words in, report dict out.

    `audio` is {"duration", "speech_end"} of the real recording (None if missing),
    `timeline` the cache facts ({"present", "fresh", "model", "configured_model", ...}),
    `settings` the effective sync/hold settings (sync.effective_settings).
    """
    st = {**sync_mod.SYNC_DEFAULTS, **(settings or {})}
    text_only = set(text_only)
    shots = sorted([s for s in (plan.get("shots") or [])], key=lambda s: float(s["start"]))
    segments = [s for s in (alignment.get("segments") or [])
                if float(s.get("end", 0)) > float(s.get("start", 0))]
    if not shots or not segments:
        raise UserError("The plan or alignment has no shots/segments to audit.")

    sh_starts = [float(s["start"]) for s in shots]
    seg_starts = [float(s["start"]) for s in segments]

    def shot_at(t: float) -> int:
        return max(0, bisect_right(sh_starts, t + 1e-6) - 1)

    def seg_at(t: float) -> int:
        return max(0, bisect_right(seg_starts, t + 1e-6) - 1)

    exp_sets = _expected_sets(segments, text_only)
    anchors = sync_mod.sentence_anchors(segments, words)
    interpolated = {i for i, a in enumerate(anchors) if a is None}

    # Wrong-picture attribution: a panel on screen that some OTHER sentence
    # tagged. Coverage fillers (tagged by nobody) and a pair shot's second card
    # don't count - only a foreign sentence's panel does.
    owners: Dict[str, set] = {}
    for si, seg in enumerate(segments):
        for p in (seg.get("tagged_panels") or []):
            if p not in text_only:
                owners.setdefault(p, set()).add(si)
    all_owned = set(owners)

    duration = float(plan.get("duration") or 0.0)
    samples: List[Dict[str, Any]] = []
    for k in range(max(0, int(round(duration / SAMPLE_STEP)))):
        t = k * SAMPLE_STEP
        si, sh = seg_at(t), shot_at(t)
        vis = _visible(shots[sh], t)
        exp = exp_sets[si]
        judged = bool(exp)
        match = judged and bool(set(vis) & set(exp))
        wrong = (not match and any(p in all_owned and si not in owners[p] for p in vis))
        samples.append({"t": t, "seg": si, "shot": sh, "shown": vis, "judged": judged,
                        "match": match, "wrong": wrong})

    windows: List[Dict[str, Any]] = []
    i = 0
    while i < len(samples):
        s = samples[i]
        if not s["judged"] or s["match"]:
            i += 1
            continue
        j = i
        while j + 1 < len(samples) and samples[j + 1]["judged"] and not samples[j + 1]["match"]:
            j += 1
        if samples[j]["t"] + SAMPLE_STEP - samples[i]["t"] >= WINDOW_MIN - 1e-9:
            windows.append(_window_record(samples, i, j, segments, shots, exp_sets, interpolated, st))
        i = j + 1

    judged = [s for s in samples if s["judged"]]
    overall = round(sum(1 for s in judged if s["match"]) / len(judged), 4) if judged else 0.0
    wrong_n = sum(1 for s in judged if s["wrong"])
    wrong = {"samples": wrong_n, "seconds": round(wrong_n * SAMPLE_STEP, 3),
             "ratio": round(wrong_n / len(judged), 4) if judged else 0.0}

    # Per-minute curve: cumulative drift (starting at one sentence) shows as a
    # cliff that never recovers; local mistakes show as one low minute.
    minute_curve: List[Dict[str, Any]] = []
    for m in range(int(samples[-1]["t"] // 60) + 1 if samples else 0):
        win = [s for s in judged if int(s["t"] // 60) == m]
        minute_curve.append({
            "minute": m,
            "match": round(sum(1 for s in win if s["match"]) / len(win), 4) if win else None,
            "wrong": round(sum(1 for s in win if s["wrong"]) / len(win), 4) if win else None,
            "judged": len(win),
        })

    worst_windows = sorted(windows, key=lambda w: w["duration"], reverse=True)[:5]

    # A cut later than max_late + gate_grace fails the gate even if it is short.
    late: List[Dict[str, Any]] = []
    for si, seg in enumerate(segments):
        if seg.get("display_action") != "SHOW" or not exp_sets[si]:
            continue
        off = _cut_offset(shots, exp_sets[si], float(seg["start"]))
        if off is not None and off > st["max_late"] + st["gate_grace"] + 1e-6:
            late.append({"sentence": seg.get("sentence"), "offset": round(off, 3)})

    over: List[Dict[str, Any]] = []
    t = 0.0
    while t + GATE_SPAN <= duration + 1e-6:
        win = [s for s in judged if t <= s["t"] < t + GATE_SPAN]
        if win:
            wfrac = sum(1 for s in win if s["wrong"]) / len(win)
            if wfrac > st["gate_window"] + 1e-9:
                over.append({"start": round(t, 2), "wrong": round(wfrac, 4),
                             "match": round(sum(1 for s in win if s["match"]) / len(win), 4),
                             "samples": len(win)})
        t += SAMPLE_STEP
    worst_window = max(over, key=lambda w: w["wrong"]) if over else None

    failures: List[str] = []
    if wrong["ratio"] > st["gate_wrong"] + 1e-9:
        failures.append(f"wrong-picture time {wrong['seconds']:.1f}s ({wrong['ratio']:.1%} of judged) "
                        f"is over {st['gate_wrong']:.0%}")
    if over and worst_window:
        failures.append(f"{len(over)} five-second window(s) over {st['gate_window']:.0%} wrong-picture "
                        f"(worst {worst_window['wrong']:.0%} at {_mmss(worst_window['start'])})")
    if late:
        worst_late = max(late, key=lambda x: x["offset"])
        failures.append(f"{len(late)} cut(s) more than {st['max_late'] + st['gate_grace']:.1f}s after their "
                        f"sentence starts (worst {worst_late['offset']:.1f}s, sentence {worst_late['sentence']})")

    # Interpolated sentences, grouped into runs.
    runs: List[Dict[str, Any]] = []
    for si in sorted(interpolated):
        if runs and runs[-1]["_last"] + 1 == si:
            runs[-1]["_last"] = si
        else:
            runs.append({"_first": si, "_last": si})
    for r in runs:
        r["first_sentence"] = segments[r["_first"]].get("sentence")
        r["last_sentence"] = segments[r["_last"]].get("sentence")
        r["seconds"] = round(sum(float(segments[k]["end"]) - float(segments[k]["start"])
                                 for k in range(r["_first"], r["_last"] + 1)), 3)
        del r["_first"], r["_last"]
    long_runs = [r for r in runs if r["seconds"] > RUN_MIN]

    residuals = [{"sentence": segments[i].get("sentence"), "start": round(float(segments[i]["start"]), 3),
                  "matched_word": round(a["time"], 3),
                  "residual": round(float(segments[i]["start"]) - a["time"], 3)}
                 for i, a in enumerate(anchors) if a is not None]
    worst_res: Dict[str, Any] | None = max(residuals, key=lambda r: abs(r["residual"])) if residuals else None

    first_word = float(words[0]["start"]) if words else None
    last_word = max(float(w["end"]) for w in words) if words else None
    leading = None if first_word is None else round(first_word - float(segments[0]["start"]), 3)
    trailing = None if last_word is None else round(float(shots[-1]["end"]) - last_word, 3)

    length_notes: List[str] = []
    if audio is None:
        length_notes.append("no narration audio found; length not checked")
    else:
        adur = float(audio["duration"])
        if duration > adur + 0.5:
            length_notes.append(f"the plan runs {duration - adur:.1f}s longer than the audio")
        if last_word is not None and duration + 1.5 < last_word:
            length_notes.append(f"the plan ends {last_word - duration:.1f}s before the last word")

    timeline = timeline or {"present": False}
    if not timeline.get("present"):
        timeline = {**timeline, "fresh": None,
                    "note": "no timeline.json: whisper words unavailable, anchors/interpolation unchecked"}
    elif timeline.get("current_audio") is None:
        timeline = {**timeline, "fresh": False, "note": "narration audio not found; cannot verify the cache"}

    return {
        "version": 1,
        "chapter": plan.get("chapter"),
        "duration": round(duration, 3),
        "audio": audio,
        "timeline": timeline,
        "settings": st,
        "strict_match": overall,
        "wrong_picture": wrong,
        "judged_samples": len(judged), "total_samples": len(samples),
        "windows": windows, "worst_windows": worst_windows,
        "late_cuts": late,
        "five_second": {"over_gate": over, "worst": worst_window},
        "minute_curve": minute_curve,
        "interpolated": {"sentences": [segments[i].get("sentence") for i in sorted(interpolated)],
                         "count": len(interpolated), "runs": runs, "long_runs": long_runs},
        "anchor_residuals": {"count": len(residuals), "worst": worst_res,
                             "per_sentence": residuals},
        "leading_offset": leading, "trailing_offset": trailing,
        "audio_length_ok": not length_notes, "length_notes": length_notes,
        "gate": {"pass": not failures, "failures": failures},
    }


def summary_lines(report: Dict[str, Any]) -> List[str]:
    """The terminal summary of an audit report."""
    out: List[str] = []
    lines: List[str] = []
    dur = report["duration"]
    tl = report.get("timeline") or {}
    aud = report.get("audio") or {}

    out.append(f"=== Chapter {report.get('chapter')} sync audit ===")
    judged, total = report["judged_samples"], report["total_samples"]
    wp = report["wrong_picture"]
    out.append(f"  strict match (the tagged panel itself): {report['strict_match']:.1%} "
               f"of {judged} judged samples ({total} total)")
    out.append("  wrong-picture time (another sentence's panel; fillers and pair second cards "
               f"excluded): {wp['seconds']:.1f}s ({wp['ratio']:.1%} of judged time)")
    if judged < total:
        out.append(f"  ({total - judged} samples had no showable tag on their sentence and were not judged)")

    wins = report["windows"]
    if wins:
        out.append(f"  mismatched stretches >= {WINDOW_MIN:g}s: {len(wins)}, "
                   f"{sum(w['duration'] for w in wins):.1f}s total")
        for w in wins[:8]:
            shown = w["shown"] or "(nothing)"
            tag = ", ".join(w["expected"]) or "-"
            cut = "" if w["cut_offset"] in (None, 0.0) else f"; cut {w['cut_offset']:+.1f}s"
            out.append(f"   {_mmss(w['start'])}-{_mmss(w['end'])} ({w['duration']:.1f}s) "
                       f"sentence {w['sentence']} \"{w['text'][:60]}\"")
            out.append(f"      shows {shown}, sentence tagged {tag}{cut}  [{w['cause']}]")
        if len(wins) > 8:
            out.append(f"   ... and {len(wins) - 8} more (full list in the JSON report)")
        out.append("  worst stretches by length (open the rendered MP4 at these times):")
        for w in report.get("worst_windows") or []:
            shown = w["shown"] or "(nothing)"
            tag = ", ".join(w["expected"]) or "-"
            cut = "" if w["cut_offset"] in (None, 0.0) else f", cut {w['cut_offset']:+.1f}s"
            out.append(f"   {_mmss(w['start'])}-{_mmss(w['end'])} ({w['duration']:.1f}s) sent {w['sentence']} "
                       f"shows {shown} / tagged {tag}{cut}  [{w['cause']}]")
    else:
        out.append(f"  mismatched stretches >= {WINDOW_MIN:g}s: none")

    mc = report.get("minute_curve") or []
    if mc:
        def _pct(v: Any) -> str:
            return "--" if v is None else f"{v:.0%}"

        out.append("  per-minute strict match:  "
                   + "  ".join(f"{m['minute']}:{_pct(m['match'])}" for m in mc))
        out.append("  per-minute wrong-picture: "
                   + "  ".join(f"{m['minute']}:{_pct(m['wrong'])}" for m in mc))

    worst = (report.get("five_second") or {}).get("worst")
    out.append(f"  five-second windows over {report['settings']['gate_window']:.0%} wrong-picture: "
               + (f"{len(report['five_second']['over_gate'])} "
                  f"(worst {worst['wrong']:.0%} at {_mmss(worst['start'])})" if worst else "none"))
    late = report["late_cuts"]
    if late:
        shown = ", ".join(f"{x['sentence']} ({x['offset']:+.1f}s)" for x in late[:8])
        out.append(f"  cuts late by more than "
                   f"{report['settings']['max_late'] + report['settings']['gate_grace']:.1f}s: {len(late)} → {shown}")

    if report["leading_offset"] is not None:
        lead = report["leading_offset"]
        side = "video leads the voice" if lead > 0 else "voice leads the video"
        out.append(f"  leading offset: the first sentence starts {abs(lead):.1f}s "
                   + ("after" if lead > 0 else "before") + f" the first word ({side})")
    if report["trailing_offset"] is not None:
        out.append(f"  trailing offset: the last shot ends {report['trailing_offset']:+.1f}s "
                   f"after the last word")
    if aud:
        out.append(f"  length: plan {dur:.1f}s vs audio {aud['duration']:.1f}s "
                   + ("- ok" if report["audio_length_ok"] else "- " + "; ".join(report["length_notes"])))
    else:
        out.append(f"  length: plan {dur:.1f}s; " + "; ".join(report["length_notes"]))

    if tl.get("present"):
        if tl.get("fresh"):
            out.append(f"  timeline: fresh (audio hash matches; model {tl.get('model')})")
        elif tl.get("note"):
            out.append(f"  timeline: NOT USABLE - {tl['note']}")
        else:
            out.append(f"  timeline: STALE - cached for audio {tl.get('cached_audio')} "
                       f"but the file hashes to {tl.get('current_audio')}; rerun `video align`")
        if tl.get("model") and tl.get("configured_model") and tl["model"] != tl["configured_model"]:
            out.append(f"  ! timeline was made with whisper '{tl['model']}', "
                       f"manhwa-ocr.yaml now says '{tl['configured_model']}'")
    else:
        out.append("  timeline: missing - no whisper words, anchors and interpolation unchecked")

    ip = report["interpolated"]
    if tl.get("present"):
        if ip["count"]:
            run_s = (f"; longest run #{ip['long_runs'][0]['first_sentence']}-{ip['long_runs'][0]['last_sentence']} "
                     f"({ip['long_runs'][0]['seconds']:.1f}s)" if ip["long_runs"] else "")
            out.append(f"  interpolated sentences (no matched word): {ip['count']}"
                       f" ({sum(r['seconds'] for r in ip['runs']):.1f}s total){run_s}")
            if ip["long_runs"]:
                out.append("  ! interpolated run(s) over "
                           + f"{RUN_MIN:g}s: " + ", ".join(f"#{r['first_sentence']}-{r['last_sentence']} "
                                                           f"({r['seconds']:.1f}s)" for r in ip["long_runs"][:6]))
        res = report["anchor_residuals"]
        if res["count"] and res["worst"]:
            w = res["worst"]
            out.append(f"  anchor residuals: worst {w['residual']:+.2f}s at sentence {w['sentence']} "
                       f"({res['count']} anchored sentences measured)")

    gate = report["gate"]
    if gate["pass"]:
        out.append("  gate: PASS")
    else:
        out.append("  gate: FAIL - " + "; ".join(gate["failures"]))
    return out


def make_report(root: Path, chapter: str, cfg, log: Callable = print) -> Dict[str, Any]:
    """Read a chapter's plan/alignment/timeline/audio and audit them. Writes
    video/chapter_N.syncaudit.json and returns the report."""
    root, chapter = Path(root), require_chapter(chapter)
    vcfg = dict(cfg.video) if hasattr(cfg, "video") else dict(cfg)
    st = sync_mod.effective_settings(vcfg, log=log)

    p = plan_path(root, chapter)
    if not p.exists():
        raise UserError(f"No plan yet for chapter {chapter}. Run:  video plan --chapter {chapter}")
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"{p.name} is unreadable ({exc}).") from None
    a = alignment_path(root, chapter)
    if not a.exists():
        raise UserError(f"No alignment yet for chapter {chapter}. Run:  video align --chapter {chapter}")
    try:
        alignment = json.loads(a.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"{a.name} is unreadable ({exc}). Run:  video align --chapter {chapter}") from None

    panels = load_panels(root, chapter)
    text_only = {x["id"] for x in (panels or {"panels": []})["panels"] if x.get("text_only")}

    words: List[Dict[str, Any]] = []
    tl: Dict[str, Any] = {"present": False}
    tl_path = sync_mod.timeline_path(root, chapter)
    audio = audio_path(root, chapter)
    if tl_path.exists():
        data: Dict[str, Any] = {}
        try:
            data = json.loads(tl_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        words = data.get("words") or []
        key = data.get("key") or {}
        current = sync_mod.file_hash(audio) if audio else None
        tl = {"present": True, "cached_audio": key.get("audio"), "current_audio": current,
              "fresh": bool(current and key.get("audio") == current),
              "model": key.get("model"), "configured_model": vcfg.get("whisper_model", "small.en")}
        if not current:
            tl["note"] = "narration audio not found"

    audio_facts: Dict[str, Any] | None = None
    if audio is not None:
        timing = audio_timing(audio)
        audio_facts = {"name": audio.name, "duration": round(float(timing["duration"]), 3),
                       "speech_end": round(float(timing["speech_end"]), 3)}

    report = audit(plan, alignment, words, audio=audio_facts, timeline=tl, settings=st, text_only=text_only)
    report["chapter"] = chapter
    try:
        atomic_write_text(audit_path(root, chapter), json.dumps(report, indent=1, ensure_ascii=False))
    except OSError as exc:
        raise UserError(f"Couldn't write the audit report: {exc}") from None
    return report


def run(root: Path, chapter: str, cfg, log: Callable = print) -> Dict[str, Any]:
    """`video sync-audit --chapter N`: audit, print the summary, save the JSON."""
    report = make_report(root, chapter, cfg, log=log)
    for line in summary_lines(report):
        log(line)
    log(f"  wrote video/chapter_{report['chapter']}.syncaudit.json")
    return report
