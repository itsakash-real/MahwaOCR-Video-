"""Check a built alignment timeline before it's trusted, not after.

The renderer never decides anything on its own - it only executes the
timeline in chapter_N.alignment.json. That makes this module the one place
that can catch a bad timeline before it becomes a bad video: every check here
runs over the *whole* finished timeline, not sentence-by-sentence in
isolation, because a problem (a jump, a gap) is a property of how two
segments relate, not of either one alone.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Set

JUMP_TOLERANCE = 2      # a panel can be this far "behind" the running position
                        # without it counting as a backward jump (recap hooks
                        # sometimes glance back a panel or two on purpose)
GAP_TOLERANCE = 0.25    # seconds of slack allowed between consecutive segments
MIN_SEGMENT_DUR = 0.05  # a segment shorter than this is almost certainly a bug


GROUPED_MIN_TAGS = 3     # a sentence with this many tags (or a long reach, see GROUPED_SPAN) is "grouped"
GROUPED_SPAN = 6         # ... or whose tags span this many panels of the story


def redistribute(tags: List[List[str]], n: int, holds: List[bool] | None = None) -> Dict[int, List[str]]:
    """How to spread sentence `n`'s tags (1-based) over the untagged sentences right after it.

    `tags[i]` is sentence i+1's tag list; `holds[i]` marks a sentence that can't carry a tag because
    the video keeps the previous picture for it ({same}, inner thoughts, pure sound). The first tag
    stays on sentence n; each later tag moves to the next free sentence, in order, stopping at the
    first sentence that already has tags of its own. Tags left over (not enough free sentences)
    stay together on the last sentence used. Returns {sentence: [tags]} for the sentences that change
    or keep a tag; {} when there is nothing to move (fewer than two tags, or no free sentence next)."""
    mine = list(dict.fromkeys(tags[n - 1])) if 0 < n <= len(tags) else []
    if len(mine) < 2:
        return {}
    out: Dict[int, List[str]] = {n: mine[:1]}
    rest = mine[1:]
    k, last = n, n                      # k is the 0-based index of the sentence after n
    while rest and k < len(tags) and not tags[k]:
        if holds and holds[k]:
            k += 1
            continue
        out[k + 1] = [rest.pop(0)]
        last = k + 1
        k += 1
    if rest and last == n:
        return {}                       # no free sentence after it: nothing can move
    if rest:
        out[last] = out[last] + rest
    return out


def is_grouped(tag_list: List[str], position: Dict[str, int] | None = None) -> bool:
    """Several tags on one sentence: three or more, or tags that reach across more than GROUPED_SPAN panels."""
    uniq = list(dict.fromkeys(tag_list))
    if len(uniq) >= GROUPED_MIN_TAGS:
        return True
    if position and len(uniq) >= 2:
        spots = [position[t] for t in uniq if t in position]
        return bool(spots) and max(spots) - min(spots) > GROUPED_SPAN
    return False


def _jump_hint(segments: List[Dict[str, Any]], i: int, ids_by_pos: Dict[int, str], last_pos: int, pos: int) -> str:
    """Plain words for a no_unexplained_jumps failure at segments[i]: the cause when a grouped
    sentence just before it carried the tags that were jumped over, and exactly which tag to move to
    which sentence in pairs.json."""
    tags = [list(s.get("tagged_panels") or []) for s in segments]
    holds = [s.get("narration_type") in {"INTERNAL", "PURE_AUDIO", "SAME"} for s in segments]
    for j in range(i - 1, -1, -1):
        if len(set(tags[j])) >= 2 and not any(tags[j + 1:i + 1]):
            move = redistribute(tags, j + 1, holds)
            if not move:
                break
            sentence = segments[j].get("sentence", j + 1)
            fix = ", ".join(f'"{s}": {json.dumps(t)}' for s, t in sorted(move.items()))
            return (f" Cause: sentence {sentence} carries {len(set(tags[j]))} tags ({', '.join(dict.fromkeys(tags[j]))}) "
                    f"and the sentence(s) after it have none, so the picture runs ahead of the words. "
                    f"Fix in pairs.json: {fix}")
    skipped = [ids_by_pos[p] for p in range(last_pos + 1, pos) if p in ids_by_pos][:4]
    near = f" The panels in between are {', '.join(skipped)}: tag this sentence with {skipped[0]} (or the one it is really about)." if skipped else ""
    return near


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

    ids_by_pos = {pos: pid for pid, pos in panel_order.items()}
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
                before = ids_by_pos.get(last_pos)
                fail("no_unexplained_jumps", n,
                    f"{pid} jumps from {before + ' ' if before else ''}(position {last_pos}) to {pid} (position {pos}) "
                    f"without strong supporting evidence (confidence={seg.get('confidence')!r})."
                    + _jump_hint(segments, i, ids_by_pos, last_pos, pos))
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
