"""Check a built alignment timeline before it's trusted, not after.

The renderer never decides anything on its own - it only executes the
timeline in chapter_N.alignment.json. That makes this module the one place
that can catch a bad timeline before it becomes a bad video: every check here
runs over the *whole* finished timeline, not sentence-by-sentence in
isolation, because a problem (a jump, a gap) is a property of how two
segments relate, not of either one alone.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

JUMP_TOLERANCE = 2      # a panel can be this far "behind" the running position
                        # without it counting as a backward jump (recap hooks
                        # sometimes glance back a panel or two on purpose)
GAP_TOLERANCE = 0.25    # seconds of slack allowed between consecutive segments
MIN_SEGMENT_DUR = 0.05  # a segment shorter than this is almost certainly a bug


def validate(segments: List[Dict[str, Any]], panel_order: Dict[str, int], duration: float) -> Dict[str, Any]:
    """{"status": "PASS"|"FAIL", "checks": {name: "PASS"|"FAIL"}, "problems": [...]}."""
    checks = {
        "timestamps_monotonic": "PASS",
        "panels_exist": "PASS",
        "no_backward_jumps": "PASS",
        "no_unexplained_jumps": "PASS",
        "no_outside_interval_display": "PASS",
        "no_gaps_or_overlaps": "PASS",
        "non_visual_no_panel_change": "PASS",
        "duration_matches_narration": "PASS",
    }
    problems: List[Dict[str, Any]] = []

    def fail(check: str, sentence: Any, detail: str) -> None:
        checks[check] = "FAIL"
        problems.append({"sentence": sentence, "check": check, "detail": detail})

    last_pos = -1
    last_end = 0.0
    for i, seg in enumerate(segments):
        n = seg.get("sentence")
        start, end = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))

        if end <= start:
            fail("timestamps_monotonic", n, f"end ({end}) is not after start ({start})")
        if i > 0 and start < segments[i - 1].get("start", 0.0) - 1e-6:
            fail("timestamps_monotonic", n, "starts before the previous segment")

        pid = seg.get("selected_panel")
        if pid is not None and pid not in panel_order:
            fail("panels_exist", n, f"{pid} is not a known panel id")

        is_hold = seg.get("display_action") == "HOLD_PREVIOUS"
        if seg.get("narration_type") in {"INTERNAL", "PURE_AUDIO", "SAME"}:
            if not is_hold or pid is not None:
                fail("non_visual_no_panel_change", n,
                    f"{seg.get('narration_type')} segment must hold the previous panel, "
                    f"got display_action={seg.get('display_action')!r} selected_panel={pid!r}")

        if pid is not None and pid in panel_order:
            pos = panel_order[pid]
            window = seg.get("candidate_window") or []
            if window and pid not in window:
                fail("no_outside_interval_display", n, f"{pid} was selected but isn't in its own candidate_window")
            if pos < last_pos - JUMP_TOLERANCE:
                fail("no_backward_jumps", n, f"{pid} (position {pos}) is behind the running position {last_pos}")
            span = max(1, len(window)) if window else 1
            if pos > last_pos + max(span, JUMP_TOLERANCE) and seg.get("confidence") not in {
                    "tag_confirmed_in_window", "ocr_match", "tag_beyond_window_accepted"}:
                fail("no_unexplained_jumps", n,
                    f"{pid} jumps from position {last_pos} to {pos} without strong supporting evidence "
                    f"(confidence={seg.get('confidence')!r})")
            last_pos = pos

        if i > 0:
            prev_end = segments[i - 1].get("end", 0.0)
            if abs(start - prev_end) > GAP_TOLERANCE:
                kind = "gap" if start > prev_end else "overlap"
                fail("no_gaps_or_overlaps", n, f"{kind} of {abs(start - prev_end):.2f}s before this segment")

        if end - start < MIN_SEGMENT_DUR:
            fail("duration_matches_narration", n, f"segment is only {end - start:.3f}s long")
        last_end = end

    if segments and abs(last_end - duration) > max(GAP_TOLERANCE, 1.0):
        fail("no_gaps_or_overlaps", segments[-1].get("sentence"),
            f"last segment ends at {last_end:.1f}s but audio is {duration:.1f}s long")

    status = "PASS" if all(v == "PASS" for v in checks.values()) else "FAIL"
    return {"status": status, "checks": checks, "problems": problems}


def review_queue(segments: List[Dict[str, Any]], validation: Dict[str, Any]) -> List[int]:
    """Sentence numbers that need a human/LLM look: flagged during alignment,
    or implicated by a failed validation check."""
    queue: Set[int] = {s["sentence"] for s in segments if s.get("needs_review")}
    queue |= {p["sentence"] for p in validation.get("problems", []) if p.get("sentence") is not None}
    return sorted(queue)
