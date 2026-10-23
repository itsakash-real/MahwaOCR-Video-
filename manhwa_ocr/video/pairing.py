"""Check and fix which panel goes with which sentence, before rendering.

The video is only as good as the recap's panel tags: a tag one panel off shows
the wrong picture for the whole sentence. This module makes the pairing easy to
see and to fix, for a person or an agent:

    video pair --chapter N            -> pairing sheets + PAIRING.md + pairs.json
    (look at the sheets, fix pairs.json)
    video pair --chapter N --apply    -> the fixed tags go into the cues

Each sheet row is one sentence: its time and words on the left, then the panels
it is tagged with (green), then the next drawn panels in the story (grey), so a
tag that's off by one is obvious at a glance. Problems that can be found without
looking are listed automatically: text/credits panels, unknown ids, untagged
sentences, the same panel tagged again later, and jumps backwards.
"""

from __future__ import annotations

import json
import textwrap
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image, ImageDraw

from ..util import UserError, atomic_save_image, atomic_write_text, read_agent_json, require_chapter
from ..workspace import work_root
from . import cues as cues_mod
from . import classify as classify_mod
from . import sync as sync_mod
from .validate import is_grouped, redistribute
from .panels import _font, load_panels, panel_image

ROWS_PER_SHEET = 6
THUMB_H = 230
NEXT_SHOWN = 3


def task_dir(root: Path, chapter: str) -> Path:
    return work_root(root) / "video" / f"ch{require_chapter(chapter)}"


def pairs_path(root: Path, chapter: str) -> Path:
    return task_dir(root, chapter) / "pairs.json"


def _need(root: Path, chapter: str):
    cue_data = cues_mod.load_cues(root, chapter)
    if not cue_data:
        raise UserError(f"Chapter {chapter}'s recap has no panel tags to check. Write the recap first "
                        f"(recap --chapters {chapter} --prepare).")
    panels = load_panels(root, chapter)
    if not panels:
        raise UserError(f"No panels for chapter {chapter} yet. Run:  video panels --chapter {chapter}")
    return cue_data, panels


def _times(root: Path, chapter: str, sentences: List[Dict[str, Any]]) -> List[float | None]:
    """When each sentence is spoken, if the audio has been transcribed."""
    try:
        data = json.loads(sync_mod.timeline_path(root, chapter).read_text(encoding="utf-8"))
        words = data.get("words") or []
    except (OSError, ValueError):
        return [None] * len(sentences)
    if not words:
        return [None] * len(sentences)
    aligned = sync_mod.align(sentences, words, float(words[-1]["end"]) + 1.0)
    return [s["start"] for s in aligned["sentences"]]


def check(root: Path, chapter: str) -> List[str]:
    """Pairing problems that can be found without looking at the pictures."""
    cue_data, panels = _need(root, chapter)
    by_id = {p["id"]: p for p in panels["panels"]}
    order = {p["id"]: i for i, p in enumerate(panels["panels"])}
    problems: List[str] = []
    seen: Dict[str, int] = {}
    last = -1
    for n, s in enumerate(cue_data["sentences"], start=1):
        tags = s.get("panels") or []
        if not tags and not s.get("same"):
            problems.append(f"sentence {n}: no panel tag (it keeps the previous picture)")
        for pid in tags:
            if pid not in by_id:
                problems.append(f"sentence {n}: {pid} doesn't exist")
                continue
            p = by_id[pid]
            if p.get("credits"):
                problems.append(f"sentence {n}: {pid} is a credits page")
            elif p.get("text_only"):
                problems.append(f"sentence {n}: {pid} is text only (bubbles/lettering); tag the drawn panel next to it")
            if pid in seen and seen[pid] < n - 1:
                problems.append(f"sentence {n}: {pid} was already shown at sentence {seen[pid]} (repeat)")
            seen[pid] = n
            if order[pid] < last - 2:
                problems.append(f"sentence {n}: {pid} jumps back in the story (after {panels['panels'][last]['id']})")
        here = [order[p] for p in tags if p in order]
        if here:
            last = max(here)     # compare with the previous sentence only (an opening hook may jump ahead)
    problems.extend(_grouped_lines(grouped_tags(root, chapter)))
    return problems


def grouped_tags(root: Path, chapter: str) -> List[Dict[str, Any]]:
    """Sentences that carry several tags with the next sentence untagged: the pattern that makes
    `video align` fail no_unexplained_jumps (the picture runs ahead of the words). Each:
    {"sentence", "tags", "move": {sentence: [tags]}, "text"}. `move` is a suggestion only."""
    cue_data, panels = _need(root, chapter)
    position = {p["id"]: i for i, p in enumerate(panels["panels"])}
    sentences = cue_data["sentences"]
    tags = [list(s.get("panels") or []) for s in sentences]
    holds = [bool(s.get("same")) or classify_mod.classify(s.get("text") or "")[0] in {"INTERNAL", "PURE_AUDIO"}
             for s in sentences]
    found = []
    for n, tag_list in enumerate(tags, start=1):
        nxt_free = n < len(tags) and not tags[n] and not sentences[n].get("same")
        if not nxt_free or not is_grouped(tag_list, position):
            continue
        move = redistribute(tags, n, holds)
        if move:
            found.append({"sentence": n, "tags": list(dict.fromkeys(tag_list)), "move": move,
                          "text": sentences[n - 1].get("text") or ""})
    return found


def _grouped_lines(found: List[Dict[str, Any]]) -> List[str]:
    out = []
    for g in found:
        fix = ", ".join(f'"{s}": {json.dumps(t)}' for s, t in sorted(g["move"].items()))
        out.append(f"sentence {g['sentence']} carries {len(g['tags'])} tags ({', '.join(g['tags'])}) and the next "
                   f"sentence has none; `video align` fails no_unexplained_jumps on this. Suggested fix in pairs.json: {fix}")
    return out


def write_sheets(root: Path, chapter: str, log=print) -> List[Path]:
    cue_data, panels = _need(root, chapter)
    plist = panels["panels"]
    by_id = {p["id"]: p for p in plist}
    order = {p["id"]: i for i, p in enumerate(plist)}
    sentences = cue_data["sentences"]
    times = _times(root, chapter, sentences)
    font, small = _font(20), _font(16)
    text_w, cell_w, pad = 520, 200, 10
    cols = 2 + NEXT_SHOWN
    width = text_w + cols * (cell_w + pad) + pad
    row_h = THUMB_H + 2 * pad + 26
    cache: "OrderedDict[str, Image.Image]" = OrderedDict()

    def thumb(pid: str) -> Image.Image:
        if pid in cache:
            cache.move_to_end(pid)
        else:
            # LRU: evict one entry, never wipe the whole cache (the old
            # clear() made every sheet re-decode panels it had just loaded).
            if len(cache) >= 40:
                cache.popitem(last=False)
            img = panel_image(root, chapter, by_id[pid], panels["norm_width"])
            img.thumbnail((cell_w, THUMB_H), Image.LANCZOS)
            cache[pid] = img
        return cache[pid]

    out: List[Path] = []
    folder = task_dir(root, chapter)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("pairing_*.png"):
        old.unlink()
    for sheet_no, start in enumerate(range(0, len(sentences), ROWS_PER_SHEET), start=1):
        chunk = sentences[start:start + ROWS_PER_SHEET]
        sheet = Image.new("RGB", (width, len(chunk) * row_h), (24, 24, 24))
        d = ImageDraw.Draw(sheet)
        for r, s in enumerate(chunk):
            n = start + r + 1
            y = r * row_h
            d.line((0, y, width, y), fill=(70, 70, 70))
            t = times[start + r]
            head = f"#{n}" + (f"   {int(t // 60)}:{t % 60:04.1f}" if t is not None else "")
            d.text((pad, y + pad), head, fill=(255, 214, 90), font=font)
            for k, line in enumerate(textwrap.wrap(s.get("text") or "", 46)[:9]):
                d.text((pad, y + pad + 30 + 22 * k), line, fill=(225, 225, 225), font=small)
            tags = [p for p in s.get("panels") or [] if p in by_id]
            after = max((order[p] for p in tags), default=-1)
            nxt = [p["id"] for p in plist[after + 1:] if not p.get("text_only") and p["id"] not in tags][:NEXT_SHOWN]
            x = text_w
            for pid, tagged in [(p, True) for p in tags[:2]] + [(p, False) for p in nxt]:
                im = thumb(pid)
                sheet.paste(im, (x + (cell_w - im.width) // 2, y + pad + 24))
                p = by_id[pid]
                colour = (90, 220, 120) if tagged else (150, 150, 150)
                if tagged and p.get("text_only"):
                    colour = (240, 80, 80)
                d.rectangle((x - 2, y + pad + 22, x + cell_w + 2, y + pad + 26 + THUMB_H), outline=colour, width=3)
                label = pid + ("  TAG" if tagged else "  next") + ("  TEXT!" if tagged and p.get("text_only") else "")
                d.text((x, y + pad), label, fill=colour, font=small)
                x += cell_w + pad
        path = folder / f"pairing_{sheet_no:02d}.png"
        atomic_save_image(sheet, path)
        out.append(path)
    log(f"  pairing: {len(out)} sheet(s) for {len(sentences)} sentences")
    return out


def write_task(root: Path, chapter: str, session: str | None = None, log=print) -> Path:
    """Sheets + pairs.json (current tags) + PAIRING.md, the instructions."""
    cue_data, _ = _need(root, chapter)
    sheets = write_sheets(root, chapter, log=log)

    def _entry(s: Dict[str, Any]) -> Any:
        # A sentence carrying a motion override (B1e) is written in the
        # object form so the field survives the pair round-trip.
        if s.get("motion") is not None:
            return {"panels": s.get("panels") or [], "motion": s["motion"]}
        return s.get("panels") or []

    pairs: Dict[str, Any] = {str(n): _entry(s) for n, s in enumerate(cue_data["sentences"], start=1)}
    # B1f: the chapter's landmark slice hints ride along as a note key
    # (like _suggestions) so the editor can adjust them in the same
    # file; unlike _suggestions, --apply DOES write this one back into
    # cues.json (see apply()).
    from .cues import slice_hints_of

    _slice_hints = slice_hints_of(cue_data)
    if _slice_hints:
        pairs["_slice_hints"] = _slice_hints
    grouped = grouped_tags(root, chapter)
    if grouped:
        # Suggestions only: apply ignores keys starting with "_". Copy a "move" into the numbered entries to use it.
        pairs["_suggestions"] = {str(g["sentence"]): {
            "why": "several tags on one sentence with the next sentence untagged (align fails no_unexplained_jumps)",
            "move": {str(s): t for s, t in sorted(g["move"].items())}} for g in grouped}
    atomic_write_text(pairs_path(root, chapter), json.dumps(pairs, indent=1))
    problems = check(root, chapter)
    sess = f' --session "{session}"' if session else ""
    lines = [
        f"# Check the picture for every sentence: chapter {chapter}",
        "",
        "Each row of a pairing sheet is one sentence of the narration (with the time it is spoken).",
        "Green = the panel(s) it is tagged with. Grey = the next drawn panels in the story.",
        "",
        "For every row ask: **does the green panel show what this sentence is about?**",
        "- If a grey panel fits better (common: tags one panel off), use that one.",
        "- A sentence about two moments can list two panels, in story order: they are shown one after the other.",
        "- Never use a panel marked TEXT (speech bubbles, captions, lettering) or credits pages.",
        "- Don't give the same panel to two sentences far apart; each panel is shown once.",
        "- Keep story order: a later sentence should not go back to an earlier panel.",
        "",
        "## Sheets",
        "",
        *[f"- {p.as_posix()}" for p in sheets],
        "",
        "## Fix",
        "",
        f"Edit `{pairs_path(root, chapter).as_posix()}`: sentence number -> list of panel ids, e.g.",
        '`"12": ["P006-02"]` or `"13": ["P006-03", "P006-04"]`. Leave sentences that are right as they are.',
        "",
        "## Motion (optional, panel-fit mode only)",
        "",
        "An entry may instead be an object carrying a per-sentence motion override:",
        '`"12": {"panels": ["P006-02"], "motion": "zoom_out"}`. Values: `default`, `zoom_in` (a push-in',
        "at the standard rate, even on a panel that would normally drift), `zoom_out` (a reveal),",
        "`zoom_out_fast` (a blast), `duo` (the sentence's two panels shown side by side). Or a list parallel",
        'to the panels — `"13": {"panels": ["P006-03", "P006-04"], "motion": ["default", "zoom_out_fast"]}`',
        "gives each tagged panel its own motion. Leave it off unless the shot needs it.",
        "",
        "## Slice hints (optional, panel-fit mode only)",
        "",
        "A panel whose real cut is a landmark — a diagonal band, a continuous piece of art, a separator",
        "thinner than a gutter — can be cut by hand with a top-level `\"_slice_hints\"` entry in pairs.json:",
        '``"_slice_hints": {"P006-02": {"slice": [0.52], "discard": [[0.0, 0.06]]}}``. `slice` lists cut',
        "positions as fractions of the panel's height (the panel becomes independent sub-panels there,",
        "shown in story order as P006-02#a, #b, ...); `discard` lists [start, end] fraction bands removed",
        "entirely. `--apply` writes `_slice_hints` into cues.json, where the planner reads it. Leave it",
        "off unless a panel needs it.",
        "",
        f"Then:  python -m manhwa_ocr video pair --chapter {chapter} --apply{sess}",
        f"then:  python -m manhwa_ocr video align --chapter {chapter}{sess}",
        f"then:  python -m manhwa_ocr video plan --chapter {chapter} --force{sess}",
        f"then:  python -m manhwa_ocr video verify --chapter {chapter}{sess}",
        "then check the storyboard and approve it (the build prompt in USER_GUIDE.md has the full routine):",
        f"       python -m manhwa_ocr video approve --chapter {chapter}{sess}",
        f"and:   python -m manhwa_ocr video render --chapter {chapter}{sess}",
        "",
        "## Found automatically",
        "",
        *([f"- {p}" for p in problems] or ["- nothing"]),
        "",
        *(["## Grouped tags: fix these before `video align`", "",
           "A sentence with several tags shows them all, so the picture races ahead of the words and the next",
           "sentence (which has no tag) looks like a jump. Spread the tags over the sentences that follow.",
           f"pairs.json has a `_suggestions` entry for each; nothing is applied until you copy a suggestion into the",
           "numbered entries.", "",
           *[f"- {line}" for line in _grouped_lines(grouped)], ""] if grouped else []),
    ]
    path = task_dir(root, chapter) / "PAIRING.md"
    atomic_write_text(path, "\n".join(lines))
    return path


def autofix(root: Path, chapter: str, log=print) -> int:
    """Replace text-only/credits tags with the nearest drawn panel.

    The mechanical fix for the problem `check()` flags: a text-only tag is
    never shown, and the nearest drawn panel is almost always the right
    replacement. Writes straight to the cues (like apply() does) and logs
    every change; run `check()` afterwards for anything left over.
    Returns how many sentences changed.
    """
    cue_data, panels = _need(root, chapter)
    plist = panels["panels"]
    by_id = {p["id"]: p for p in plist}
    sentences = cue_data["sentences"]
    changed = 0
    for n, s in enumerate(sentences, start=1):
        new_tags = []
        dirty = False
        for pid in s.get("panels") or []:
            p = by_id.get(pid)
            if p is not None and (p.get("text_only") or p.get("credits")):
                near = cues_mod.nearest_drawn_panel(plist, pid)
                if near and near != pid:
                    log(f"  sentence {n}: {pid} is text-only/credits -> {near}")
                    new_tags.append(near)
                    dirty = True
                else:
                    new_tags.append(pid)  # no drawn panel to retag to; leave it
            else:
                new_tags.append(pid)
        new_tags = list(dict.fromkeys(new_tags))
        if dirty:
            s["panels"] = new_tags
            changed += 1
    if changed:
        atomic_write_text(cues_mod.cues_path(root, chapter), json.dumps(cue_data, indent=1, ensure_ascii=False))
        log(f"  chapter {chapter}: {changed} sentence(s) re-tagged to the nearest drawn panel")
    else:
        log(f"  chapter {chapter}: no text-only/credits tags to fix")
    return changed


def _motion_problems(n: int, motion: Any, panel_count: int) -> List[str]:
    """Problems with one pairs entry's "motion" (B1e): the same rules
    cues.validate_cues applies to a cues.json sentence's field."""
    hints = ", ".join(cues_mod.MOTION_HINTS)
    if isinstance(motion, str):
        return [] if motion in cues_mod.MOTION_HINTS else \
            [f"sentence {n}: \"motion\" must be one of {hints} (got {motion!r})"]
    if isinstance(motion, list):
        if not all(isinstance(m, str) and m in cues_mod.MOTION_HINTS for m in motion):
            return [f"sentence {n}: every \"motion\" entry must be one of {hints}"]
        if len(motion) != panel_count:
            return [f"sentence {n}: \"motion\" list has {len(motion)} entries but the "
                    f"sentence tags {panel_count} panel(s) — they run in parallel"]
        if "duo" in motion:
            return [f"sentence {n}: \"duo\" is a whole-sentence motion — write it as a "
                    f"plain string, not in a list"]
        return []
    return [f"sentence {n}: \"motion\" must be a string or a list of strings"]


def validate_pairs(pairs: Any, sentence_count: int, by_id: Dict[str, Any]) -> List[str]:
    """Line-level problems in a pairs.json document (P0 safety lock).

    Keys starting with "_" are notes (_suggestions, _comment) and are
    ignored — except "_slice_hints" (B1f), the one note key --apply
    acts on, which is validated here (format + panel existence). An
    entry is a list of panel ids, or — to carry the
    per-sentence motion override (B1e) — an object
    {"panels": [...], "motion": ...}. Returns [] when every numbered
    entry is usable. Every problem names the sentence number and the
    exact ids, so a reviewer can fix the file without guessing.
    """
    errors: List[str] = []
    if not isinstance(pairs, dict):
        return ["pairs.json must be an object: sentence number -> list of panel ids"]
    for key, entry in pairs.items():
        if str(key).startswith("_"):
            if str(key) == "_slice_hints" and entry is not None:
                # B1f: the one note key --apply acts on, so it is
                # validated like the rest of the file (other "_" keys
                # stay pure notes). Panel existence is checked against
                # by_id here because the hints key panels, not sentences.
                from .slicehints import parse_slice_hints

                _hints, _problems = parse_slice_hints(entry)
                errors.extend(f"_slice_hints: {p}" for p in _problems)
                if isinstance(entry, dict):
                    errors.extend(f"_slice_hints: {pid} doesn't exist"
                                  for pid in entry if pid not in by_id)
            continue
        try:
            n = int(key)
        except (TypeError, ValueError):
            errors.append(f'"{key}" is not a sentence number')
            continue
        if not 1 <= n <= sentence_count:
            errors.append(f"sentence {n} doesn't exist (there are {sentence_count})")
            continue
        ids, motion = entry, None
        if isinstance(entry, dict):
            extra = set(entry) - {"panels", "motion"}
            if extra:
                errors.append(f"sentence {n}: unknown keys {sorted(extra)} — an entry is a list "
                              f"of panel ids, or {{\"panels\": [...], \"motion\": ...}}")
                continue
            if "panels" not in entry:
                errors.append(f"sentence {n}: the object form needs \"panels\"")
                continue
            ids, motion = entry.get("panels"), entry.get("motion")
        ids = [ids] if isinstance(ids, str) else ids
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            errors.append(f"sentence {n}: give a list of panel ids")
            continue
        bad = [x for x in ids if x not in by_id]
        if bad:
            errors.append(f"sentence {n}: {', '.join(bad)} doesn't exist")
            continue
        texty = [x for x in ids if by_id[x].get("text_only")]
        if texty:
            errors.append(f"sentence {n}: {', '.join(texty)} is a text/credits panel; pick a drawn one")
            continue
        if len(set(ids)) != len(ids):
            errors.append(f"sentence {n}: duplicate panel ids in {ids}")
            continue
        if motion is not None:
            errors.extend(_motion_problems(n, motion, len(ids)))
    return errors


def apply(root: Path, chapter: str, log=print) -> int:
    """Put the tags from pairs.json into the cues. Returns how many sentences changed."""
    cue_data, panels = _need(root, chapter)
    path = pairs_path(root, chapter)
    if not path.exists():
        raise UserError(f"Nothing to apply. Run first:  video pair --chapter {chapter}")
    pairs = read_agent_json(path, "pairs.json")
    if not isinstance(pairs, dict):
        raise UserError(f"{path} must be an object: sentence number -> list of panel ids.")
    # B1f: "_slice_hints" is extracted BEFORE the note keys are stripped
    # — it is the one "_" key --apply acts on (it lands in cues.json,
    # next to the motion hints it was modelled on). The raw document
    # (notes included) is what validate_pairs checks.
    raw_pairs = pairs
    slice_hints_raw = raw_pairs.get("_slice_hints")
    pairs = {k: v for k, v in pairs.items() if not str(k).startswith("_")}      # _suggestions, _comment: notes only
    by_id = {p["id"]: p for p in panels["panels"]}
    sentences = cue_data["sentences"]
    errors = validate_pairs(raw_pairs, len(sentences), by_id)
    changed = 0
    if not errors:
        if slice_hints_raw is not None:
            from .slicehints import parse_slice_hints

            new_hints, _ = parse_slice_hints(slice_hints_raw)
            if new_hints:
                if cue_data.get("slice_hints") != new_hints:
                    cue_data["slice_hints"] = new_hints
                    changed += 1
            elif "slice_hints" in cue_data:
                del cue_data["slice_hints"]
                changed += 1
        for key, entry in pairs.items():
            n = int(key)
            # The object form carries the sentence's motion override
            # (B1e); the plain list form clears any motion it had.
            if isinstance(entry, dict):
                ids = entry.get("panels")
                has_motion, motion = "motion" in entry, entry.get("motion")
            else:
                ids, has_motion, motion = entry, True, None
            ids = [ids] if isinstance(ids, str) else ids
            s = sentences[n - 1]
            if s.get("panels") != ids:
                s["panels"] = ids
                changed += 1
            if has_motion:
                if motion is None:
                    if "motion" in s:
                        del s["motion"]
                        changed += 1
                elif s.get("motion") != motion:
                    s["motion"] = motion
                    changed += 1
    if errors:
        raise UserError("pairs.json has problems, nothing was changed:\n  " + "\n  ".join(errors[:12]))
    tagged = sum(1 for s in sentences if s.get("panels"))
    cue_data["tagged_ratio"] = round(tagged / max(1, len(sentences)), 3)
    atomic_write_text(cues_mod.cues_path(root, chapter), json.dumps(cue_data, indent=1, ensure_ascii=False))
    log(f"  chapter {chapter}: {changed} sentence(s) re-paired")
    return changed
