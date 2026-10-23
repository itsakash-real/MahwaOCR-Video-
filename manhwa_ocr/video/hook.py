"""`video hook` / `video intro --auto` / `video previously`: auto-picked panels.

The manual intro (`video intro` from intro/intro.json) stays exactly as it was.
These commands add the automatic path on top of the same machinery:

* `video hook --chapters 1-6 --audio audio/chapter_0.mp3 --script intro/intro.txt`
  (or `--cues intro/intro.cues.json`) picks one panel per narration sentence
  from across the given chapters, writes the picks to intro/intro.auto.json
  for review, then builds the 30-second hook video through intro.build_intro
  (same planner, same renderer, continuous motion, callouts).
* `video previously --chapters 1-6 --audio audio/previously_7.mp3
  --script previously/previously_7.txt` builds a 30-second "Previously on..."
  bridge. Panels come ONLY from the given (previous) chapters -- never from
  the next part -- and prefer panels that already appeared in those chapters'
  videos. Panel picking scores each narration sentence against the previous
  part's recap sentences too, so the beats the recap called out are the beats
  that get shown.

Rules, all deterministic and local (no provider calls, no audio generation --
the user supplies every audio file):

* never a text-only / credits panel (same `text_only` / `credits` flags the
  chapter videos use);
* never the same panel twice in one video;
* story order never runs backwards: picks' global story position is
  non-decreasing across sentences, so a payoff panel can never appear before
  the narration reaches it. (Early sentences therefore get early-chapter
  panels -- the tease -- unless a later sentence names a later beat.)
* face / impact preference is a documented pixel heuristic, not vision:
  tall portrait panels read as close-ups, wide panels as establishing shots,
  low-dialogue panels as clean impact art. It is a tiebreak on top of the
  lexical overlap with the panel's own OCR text (align_engine's rule), never
  a replacement for it.
* `video/intro_memory.json` records which panels / beats intro + previously
  already used. A later run avoids those panels unless the script marks the
  sentence `[REUSE]` (or `--allow-reuse` is passed), and warns about any
  panel already used in intro + previously. `series_memory.json` is only
  ever READ (characters / facts / open threads); it is never rewritten here.
* hook structure: the timed sentences must fill 0-10s proof, 10-20s stakes,
  20-30s roadmap + open loop. Missing [HOOK] / stakes / [LOOP: ...] markers
  log a warning, they never stop the build.
* 30-second rule: target 30s, configurable 20-45s (`video.hook` in config or
  `--duration`). Audio longer than the target logs a warning; audio is never
  trimmed or generated here.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..memory import SeriesMemory
from ..util import UserError, atomic_write_text, chapter_key, normalize_label
from . import align_engine
from . import intro as intro_mod
from .media import video_dir
from .panels import load_panels
from .plan import save_plan

HOOK_DEFAULTS = {"target_seconds": 30.0, "min_seconds": 20.0, "max_seconds": 45.0, "seed": 7}
MEMORY_VERSION = 1
MAX_CALLOUTS = 1            # a 30-second hook gets at most one callout
PREVIOUSLY_BEATS = 8        # previously covers at most 8 recap beats
PREVIOUSLY_MIN_BEATS = 5

HOOK_MARKER = re.compile(r"\[HOOK\]", re.IGNORECASE)
LOOP_MARKER = re.compile(r"\[LOOP:\s*([^\]\n]+)\]", re.IGNORECASE)
REUSE_MARKER = re.compile(r"\[REUSE\]", re.IGNORECASE)
BEAT_MARKER = re.compile(r"\[BEAT\]", re.IGNORECASE)
MARKER_ONLY = re.compile(r"^(\s*\[(?:HOOK|REUSE|BEAT)\]|\s*\[LOOP:[^\]\n]+\])+\s*$", re.IGNORECASE)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
STAKES_WORDS = {"danger", "dangerous", "kill", "killed", "killing", "die", "dies", "death",
                "destroy", "destroyed", "threat", "threaten", "war", "invasion", "invade",
                "trap", "trapped", "escape", "survive", "survival", "attack", "attacked",
                "enemy", "enemies", "boss", "raid", "collapse", "lose", "lost", "fail",
                "failed", "doom", "doomed", "sacrifice", "betray", "betrayal", "hunt",
                "hunted", "chase", "chased", "countdown", "deadline", "final"}


def hook_settings(vcfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(HOOK_DEFAULTS)
    out.update({k: v for k, v in (vcfg.get("hook") or {}).items() if v is not None})
    out["target_seconds"] = max(float(out["min_seconds"]),
                                min(float(out["max_seconds"]), float(out["target_seconds"])))
    return out


# ------------------------------------------------------------------ parsing

def parse_chapters_spec(spec: str) -> List[str]:
    """'1-6' -> ['1'..'6']; '1-3,7' -> ['1','2','3','7']; '6' -> ['6']."""
    out: List[str] = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and not part.startswith("-"):
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                out.append(normalize_label(part))
                continue
            if hi < lo:
                raise UserError(f"Bad chapter range {part!r}: end before start.")
            out.extend(str(n) for n in range(lo, hi + 1))
        else:
            out.append(normalize_label(part))
    if not out:
        raise UserError("Give --chapters, e.g. 1-6 or 1-3,7.")
    seen, deduped = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def parse_script(text: str) -> List[Dict[str, Any]]:
    """One narration sentence per non-empty line.

    Whole-line markers ([HOOK], [LOOP: ...], [REUSE], [BEAT]) attach to the
    next sentence; inline markers attach to their own sentence. Markers are
    stripped from the spoken text. A script with no markers at all still
    parses -- the hook-structure check warns about that later.
    """
    sentences: List[Dict[str, Any]] = []
    pending: Dict[str, Any] = {"hook": False, "loops": [], "reuse": False, "beat": False}
    for ln in (text or "").splitlines():
        if MARKER_ONLY.match(ln):
            if HOOK_MARKER.search(ln):
                pending["hook"] = True
            if REUSE_MARKER.search(ln):
                pending["reuse"] = True
            if BEAT_MARKER.search(ln):
                pending["beat"] = True
            pending["loops"] += [m.strip() for m in LOOP_MARKER.findall(ln)]
            continue
        if not ln.strip():
            continue
        entry = {"text": ln.strip(), "hook": pending["hook"], "loops": list(pending["loops"]),
                 "reuse": pending["reuse"], "beat": pending["beat"]}
        pending = {"hook": False, "loops": [], "reuse": False, "beat": False}
        for m in LOOP_MARKER.findall(entry["text"]):
            if m.strip() not in entry["loops"]:
                entry["loops"].append(m.strip())
        entry["hook"] = entry["hook"] or bool(HOOK_MARKER.search(entry["text"]))
        entry["reuse"] = entry["reuse"] or bool(REUSE_MARKER.search(entry["text"]))
        entry["beat"] = entry["beat"] or bool(BEAT_MARKER.search(entry["text"]))
        entry["text"] = LOOP_MARKER.sub("", entry["text"])
        entry["text"] = HOOK_MARKER.sub("", entry["text"])
        entry["text"] = REUSE_MARKER.sub("", entry["text"])
        entry["text"] = BEAT_MARKER.sub("", entry["text"])
        entry["text"] = re.sub(r"\s+", " ", entry["text"]).strip()
        if entry["text"]:
            sentences.append(entry)
    if not sentences:
        raise UserError("The script has no narration sentences (one per line).")
    return sentences


def parse_cues_data(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Sentences from a cues.json file: {sentences: [{text, hook, loops, ...}]}.

    A sentence may also carry "panels": a list of panel refs like
    "ch2:P010-14" that pins exactly which panels illustrate it (see
    pinned_picks). No "panels" key -> the sentence is auto-picked.
    """
    raw = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise UserError("The cues file needs a non-empty \"sentences\" list.")
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
            continue
        loops = entry.get("loops") or []
        if isinstance(loops, str):
            loops = [loops]
        panels = entry.get("panels") or []
        if isinstance(panels, str):
            panels = [panels]
        out.append({"text": str(entry["text"]).strip(), "hook": bool(entry.get("hook")),
                    "loops": [str(x) for x in loops], "reuse": bool(entry.get("reuse")),
                    "beat": bool(entry.get("beat")),
                    "panels": [str(x).strip() for x in panels if str(x).strip()]})
    if not out:
        raise UserError("The cues file has no usable sentences.")
    return out


def load_script_sentences(*, script_path: Path | None, cues_path: Path | None,
                          default_path: Path | None = None) -> List[Dict[str, Any]]:
    if cues_path is not None:
        if not cues_path.exists():
            raise UserError(f"No cues file: {cues_path}")
        try:
            return parse_cues_data(json.loads(cues_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise UserError(f"{cues_path} is not valid JSON ({exc}).") from None
    path = script_path or default_path
    if path is None or not path.exists():
        raise UserError(f"No script file. Write one (one sentence per line) at {path} "
                        "or pass --script / --cues.")
    return parse_script(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ panel pool

def prior_video_panels(root: Path, chapters: List[str]) -> Dict[str, set]:
    """Panel ids each chapter's own video actually showed (from its plan)."""
    out: Dict[str, set] = {}
    for c in chapters:
        path = video_dir(root) / f"chapter_{c}.plan.json"
        shown: set = set()
        if path.exists():
            try:
                for shot in json.loads(path.read_text(encoding="utf-8")).get("shots") or []:
                    for card in shot.get("cards") or []:
                        if card.get("panel"):
                            shown.add(str(card["panel"]))
            except (OSError, ValueError):
                pass
        out[c] = shown
    return out


def build_pool(root: Path, chapters: List[str]) -> List[Dict[str, Any]]:
    """Every drawable panel of the given chapters, in story order.

    The pool is exactly the given chapters -- nothing else. That is what
    keeps previously from ever leaking a next-part panel: the caller passes
    only the previous chapters, so the next part's panels are never even
    candidates.
    """
    prior = prior_video_panels(root, chapters)
    pool: List[Dict[str, Any]] = []
    order = 0
    for c in chapters:
        data = load_panels(root, c)
        if not data:
            continue
        for p in data.get("panels") or []:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            w, h = float(p.get("width") or 1), float(p.get("height") or 1)
            pool.append({"ref": intro_mod.ref_id(c, pid), "chapter": c, "id": pid,
                         "order": order, "text": str(p.get("text") or ""),
                         "width": w, "height": h, "aspect": h / w if w else 1.0,
                         "area": w * h, "text_only": bool(p.get("text_only")),
                         "credits": bool(p.get("credits")),
                         "prior_video": pid in prior.get(c, set())})
            order += 1
    return pool


# ------------------------------------------------------------------ auto-pick

def _face_impact_bonus(panel: Dict[str, Any], first_sentence: bool, area_rank: float) -> Tuple[float, List[str]]:
    """Pixel-heuristic bonuses + the reason words for them. Deterministic."""
    bonus, reasons = 0.0, []
    aspect = panel["aspect"]
    if 1.2 <= aspect <= 3.0:
        bonus += 12.0
        reasons.append("portrait close-up proxy")
    elif aspect > 3.0:
        bonus += 4.0
        reasons.append("tall panel")
    elif first_sentence and aspect < 0.55:
        bonus += 6.0
        reasons.append("wide establishing shot")
    n_chars = len(panel["text"])
    if n_chars <= 20:
        bonus += 6.0
        reasons.append("clean art (little dialogue)")
    elif n_chars <= 80:
        bonus += 2.0
    bonus += 6.0 * area_rank
    if area_rank >= 0.8:
        reasons.append("large impact panel")
    return bonus, reasons


def auto_pick(sentences: List[Dict[str, Any]], pool: List[Dict[str, Any]], *,
              used_refs: set | frozenset = frozenset(), seed: int = 7,
              prefer_prior_video: bool = False,
              extra_texts: List[str] | None = None) -> List[Dict[str, Any]]:
    """One panel per sentence. Pure: no I/O, same inputs -> same picks.

    Scoring per candidate = lexical overlap with the sentence's words against
    the panel's own OCR text (align_engine's rule) + face/impact pixel
    heuristics + a tiny seeded jitter that only breaks near-ties. Picks walk
    story order (never backwards), never repeat, never use a memory panel
    unless that sentence is marked [REUSE]. Payoff guard: an early sentence
    may only pick from the early part of the pool (a proportional tease
    ceiling), unless it names a later beat -- overlap >= 6 with that panel's
    OCR text, i.e. one distinctive word -- in which case the ceiling lifts
    for that panel.
    """
    drawn = [p for p in pool if not p.get("text_only") and not p.get("credits")]
    if not drawn:
        raise UserError("No drawable panels in the given chapters (all text-only / credits).")
    areas = sorted(p["area"] for p in drawn)
    rank = {id(p): (areas.index(p["area"]) / max(1, len(areas) - 1)) for p in drawn}
    rng = random.Random(seed)
    jitter = {p["ref"]: rng.uniform(0, 0.5) for p in drawn}  # fixed order -> reproducible
    used: set = set()
    last_order = -1
    picks: List[Dict[str, Any]] = []
    n_ch = len({p["chapter"] for p in drawn})
    early_chapters = {p["chapter"] for p in drawn[: max(1, len(drawn) // 3)]}
    for i, sent in enumerate(sentences):
        text = sent["text"]
        candidates = []
        for p in drawn:
            if p["ref"] in used:
                continue
            if p["ref"] in used_refs and not sent.get("reuse"):
                continue
            if p["order"] < last_order:
                continue  # payoff ordering: story never runs backwards
            overlap = align_engine._lexical_overlap(text, p["text"])
            extra = 0
            for extra_text in (extra_texts or []):
                extra = max(extra, align_engine._lexical_overlap(extra_text, p["text"]))
            bonus, reasons = _face_impact_bonus(p, i == 0, rank[id(p)])
            score = overlap + bonus + min(20.0, extra * 0.5) + jitter[p["ref"]]
            # Tease ceiling: sentence i of N may only reach (i+1)/N of the
            # pool, unless it names the beat (overlap >= 6 with this panel).
            ceiling = (i + 1) * len(drawn) / len(sentences) + 2
            if p["order"] > ceiling and overlap < 6:
                score -= 1000.0
                reasons = ["beyond tease ceiling (beat not named)"] + reasons
            if p.get("prior_video") and prefer_prior_video:
                score += 10.0
                reasons = ["shown in the chapter video"] + reasons
            if i == 0 and p["chapter"] in early_chapters:
                score += 4.0
            if overlap:
                reasons = [f"OCR overlap {overlap}"] + reasons
            candidates.append((score, p, reasons))
        if not candidates and last_order >= 0:
            # Pool exhausted ahead of the story position (short pool, many
            # sentences): relax the order rule rather than fail, and say so.
            for p in drawn:
                if p["ref"] in used or (p["ref"] in used_refs and not sent.get("reuse")):
                    continue
                overlap = align_engine._lexical_overlap(text, p["text"])
                bonus, reasons = _face_impact_bonus(p, i == 0, rank[id(p)])
                candidates.append((overlap + bonus + jitter[p["ref"]], p,
                                   ["story-order relaxed: pool exhausted ahead"] + reasons))
        if not candidates:
            raise UserError(f"No panel left to pick for sentence {i + 1} ({text[:60]!r}): "
                            "every drawable panel is used or in intro_memory.json. "
                            "Add chapters, or mark a sentence [REUSE].")
        candidates.sort(key=lambda c: (-c[0], c[1]["ref"]))
        score, panel, reasons = candidates[0]
        used.add(panel["ref"])
        last_order = panel["order"]
        picks.append({"text": text, "refs": [panel["ref"]], "panels": [panel["ref"]],
                      "beat": bool(sent.get("beat")), "hook": bool(sent.get("hook")),
                      "loops": list(sent.get("loops") or []),
                      "reason": "; ".join(reasons) or "first drawable panel in story order",
                      "score": round(score, 2)})
    return picks


def pinned_picks(sentences: List[Dict[str, Any]], pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Picks taken verbatim from cues "panels" pins. Pure: no I/O.

    The manual override for hook / previously, mirroring the manual intro:
    when a cues file pins panels, EVERY sentence must pin 1-3 of them (the
    shape the manual intro accepts), each pin must name a drawable panel of
    the pool, and no panel may be pinned twice. Anything else is a UserError
    -- a half-pinned script never silently falls back to auto-pick for the
    remaining sentences, because the two picking modes read very differently
    on screen.
    """
    by_ref = {p["ref"]: p for p in pool}
    picks: List[Dict[str, Any]] = []
    seen: set = set()
    for i, sent in enumerate(sentences):
        refs = [str(r).strip() for r in (sent.get("panels") or []) if str(r).strip()]
        if not refs:
            raise UserError(
                f"Sentence {i + 1} pins no panels ({sent['text'][:60]!r}): when any sentence "
                "of a cues file pins its panels, every sentence must pin 1-3 refs "
                "like \"ch2:P010-14\".")
        if len(refs) > 3:
            raise UserError(f"Sentence {i + 1} pins {len(refs)} panels; the maximum is 3.")
        for ref in refs:
            panel = by_ref.get(ref)
            if panel is None:
                raise UserError(
                    f"Sentence {i + 1} pins {ref}, which is not a panel of the given chapters.")
            if panel.get("text_only") or panel.get("credits"):
                raise UserError(
                    f"Sentence {i + 1} pins {ref}, a text-only / credits panel, which cannot be shown.")
            if ref in seen:
                raise UserError(f"{ref} is pinned more than once (sentence {i + 1}); "
                                "a panel may appear only once in the video.")
            seen.add(ref)
        picks.append({"text": sent["text"], "refs": list(refs), "panels": list(refs),
                      "beat": bool(sent.get("beat")), "hook": bool(sent.get("hook")),
                      "loops": list(sent.get("loops") or []),
                      "reason": "pinned in cues file", "score": 0.0})
    return picks


# ------------------------------------------------------------------ hook structure

def hook_structure(timed: List[Dict[str, Any]], target: float) -> Dict[str, Any]:
    """Timed sentences -> 0-10 proof / 10-20 stakes / 20-30 roadmap + loop.

    `timed`: [{text, start, end, hook, loops, ...}]. Warnings only -- a hook
    without markers still builds, it just gets told what is missing.
    """
    third = target / 3.0
    sections = {"proof": [], "stakes": [], "roadmap": []}
    for n, s in enumerate(timed, start=1):
        mid = (float(s["start"]) + float(s["end"])) / 2.0
        key = "proof" if mid < third else "stakes" if mid < 2 * third else "roadmap"
        sections[key].append(n)
    warnings: List[str] = []
    if not any(timed[n - 1].get("hook") for n in sections["proof"]):
        warnings.append("no [HOOK] marker in the first 10s (proof) -- add [HOOK] to the opening sentence")
    stakes = [timed[n - 1] for n in sections["stakes"]]
    has_stakes = any(s.get("beat") or s.get("loops") for s in stakes) or any(
        w in (" " + re.sub(r"[^a-z ]", " ", s["text"].lower()) + " ") for s in stakes for w in STAKES_WORDS)
    if not has_stakes:
        warnings.append("no stakes signal in 10-20s -- add a stakes sentence or [BEAT]")
    if not any(timed[n - 1].get("loops") for n in sections["roadmap"]):
        warnings.append("no [LOOP: ...] marker in the last 10s (roadmap + open loop) -- add one so viewers stay")
    return {"sections": sections, "warnings": warnings,
            "target_seconds": target, "covered_seconds": round(float(timed[-1]["end"]), 2) if timed else 0.0}


def duration_warning(duration: float, settings: Dict[str, Any]) -> str | None:
    target = float(settings["target_seconds"])
    if duration > float(settings["max_seconds"]):
        return (f"hook audio is {duration:.1f}s, over the {settings['max_seconds']:g}s cap "
                f"(target {target:g}s) -- trim the narration or raise video.hook.max_seconds")
    if duration < float(settings["min_seconds"]):
        return (f"hook audio is {duration:.1f}s, under the {settings['min_seconds']:g}s floor "
                f"(target {target:g}s) -- record a longer narration")
    if duration > target + 2.0:
        return f"hook audio is {duration:.1f}s vs {target:g}s target -- close, but trim to land the loop on time"
    return None


# ------------------------------------------------------------------ memory

def memory_path(root: Path) -> Path:
    return video_dir(root) / "intro_memory.json"


def _empty_memory() -> Dict[str, Any]:
    return {"version": MEMORY_VERSION,
            "intro": {"panels_used": [], "beats_covered": [], "updated_at": None},
            "previously": {"panels_used": [], "beats_covered": [],
                           "last_previously_timestamp": None, "last_previously_for": []},
            "open_loops": []}


def load_memory(root: Path) -> Dict[str, Any]:
    path = memory_path(root)
    if not path.exists():
        return _empty_memory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_memory()
    if not isinstance(data, dict):
        return _empty_memory()
    mem = _empty_memory()
    for kind in ("intro", "previously"):
        prev = data.get(kind) or {}
        if isinstance(prev, dict):
            for key in mem[kind]:
                if prev.get(key) is not None:
                    mem[kind][key] = prev[key]
    if isinstance(data.get("open_loops"), list):
        mem["open_loops"] = data["open_loops"]
    mem["version"] = MEMORY_VERSION
    return mem


def save_memory(root: Path, mem: Dict[str, Any]) -> Path:
    mem = dict(mem)
    mem["version"] = MEMORY_VERSION
    path = memory_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(mem, indent=1, ensure_ascii=False))
    return path


def used_refs(mem: Dict[str, Any]) -> set:
    out = set()
    for kind in ("intro", "previously"):
        out.update((mem.get(kind) or {}).get("panels_used") or [])
    return out


def memory_warnings(mem: Dict[str, Any], refs: List[str]) -> List[str]:
    """Panels in `refs` that intro + previously already used."""
    used = used_refs(mem)
    return [f"{r} was already used in intro/previously -- mark its sentence [REUSE] to reuse it"
            for r in refs if r in used]


def record_use(mem: Dict[str, Any], kind: str, picks: List[Dict[str, Any]],
               *, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Union new panels / beats / loops into memory. Never drops old entries."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slot = mem.setdefault(kind, {"panels_used": [], "beats_covered": []})
    for pick in picks:
        for ref in pick.get("panels") or pick.get("refs") or []:
            if ref not in slot.setdefault("panels_used", []):
                slot["panels_used"].append(ref)
        beat = (pick.get("text") or "")[:160]
        if beat and beat not in slot.setdefault("beats_covered", []):
            slot["beats_covered"].append(beat)
        for loop in pick.get("loops") or []:
            entry = {"loop": loop, "opened_in": kind, "status": "open"}
            if all((e if isinstance(e, dict) else {}).get("loop") != loop for e in mem["open_loops"]):
                mem["open_loops"].append(entry)
    slot["updated_at"] = now
    if kind == "previously":
        slot["last_previously_timestamp"] = now
        if extra and extra.get("for_chapters") is not None:
            slot["last_previously_for"] = extra["for_chapters"]
    return mem


def read_series_memory(root: Path) -> SeriesMemory:
    """Read-only: characters / facts / open threads from series_memory.json."""
    path = Path(root) / "series_memory.json"
    return SeriesMemory.load(path, series=Path(root).name)


# ------------------------------------------------------------------ previously beats

def recap_sentences(root: Path, chapter: str) -> List[Dict[str, Any]]:
    """Sentences of one chapter's recap (recaps/chapter_N.md), headings stripped."""
    path = Path(root) / "recaps" / f"chapter_{chapter}.md"
    if not path.exists():
        return []
    lines = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        lines.append(ln)
    out = []
    for chunk in SENTENCE_SPLIT.split(" ".join(lines)):
        text = re.sub(r"\s+", " ", chunk).strip()
        if len(text.split()) >= 4:
            out.append({"text": text, "chapter": chapter})
    return out


def select_beats(sentences: List[Dict[str, Any]], *, count: int = PREVIOUSLY_BEATS,
                 open_threads: List[str] | None = None) -> List[Dict[str, Any]]:
    """Pick up to `count` key beats from recap sentences (pure).

    Evenly spaced across the recap so the whole part is covered; inside each
    window prefer a sentence that names an open thread, then a longer one.
    """
    if len(sentences) <= count:
        return list(sentences)
    thread_words: set = set()
    for t in (open_threads or []):
        thread_words.update(w for w in re.findall(r"[a-z]{4,}", str(t).lower())
                            if w not in {"this", "that", "with", "from", "they", "their"})
    picks: List[Dict[str, Any]] = []
    for w in range(count):
        lo = w * len(sentences) // count
        hi = (w + 1) * len(sentences) // count
        window = sentences[lo:hi] or sentences[lo:lo + 1]
        centre = (lo + hi - 1) / 2.0

        def key(idx_sent):
            idx, s = idx_sent
            words = set(re.findall(r"[a-z]{4,}", s["text"].lower()))
            return (len(words & thread_words), min(30, len(words)), -abs(idx - centre))

        best = max(enumerate(window, start=lo), key=key)[1]
        picks.append(best)
    return picks


# ------------------------------------------------------------------ building

def _resolve_audio(root: Path, audio_arg: str | None, default_name: str) -> Path:
    if audio_arg:
        p = Path(audio_arg)
        candidates = [p, Path(root) / p, Path(root) / "audio" / p.name]
        for cand in candidates:
            if cand.is_file():
                return cand
        raise UserError(f"No audio file: {audio_arg} (looked in {candidates[0]} and the session audio folder).")
    p = Path(root) / "audio" / default_name
    if not p.is_file():
        raise UserError(f"No narration audio. Save it as audio/{default_name} or pass --audio.")
    return p


def _cap_callouts_and_cards(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Hook / previously: no chapter cards, at most one callout in the video."""
    plan["chapter_cards"] = []
    kept = 0
    for shot in plan.get("shots") or []:
        callouts = shot.get("callouts") or []
        if kept >= MAX_CALLOUTS:
            shot["callouts"] = []
        elif len(callouts) > MAX_CALLOUTS - kept:
            shot["callouts"] = callouts[: MAX_CALLOUTS - kept]
        kept += len(shot.get("callouts") or [])
    plan["generated_hash"] = hashlib.sha256(
        json.dumps(plan["shots"], sort_keys=True).encode()).hexdigest()[:16]
    return plan


def _build_from_picks(sess, cfg, *, kind: str, out_chapter: str, audio: Path,
                      picks: List[Dict[str, Any]], settings: Dict[str, Any],
                      sync: str, fmt: str | None, plan_only: bool, workers: int,
                      log) -> Tuple[Dict[str, Any], Dict[str, Any], Path | None]:
    """Picks -> intro.build_intro (same planner/renderer) -> capped plan -> render."""
    sentences = [{"text": p["text"], "panels": list(p["panels"]), "beat": bool(p.get("beat"))}
                 for p in picks]
    plan, panels_data = intro_mod.build_intro(
        sess, cfg, sync=sync, fmt=fmt, log=log, sentences=sentences,
        out_chapter=out_chapter, audio=audio, kind=kind)
    _cap_callouts_and_cards(plan)
    save_plan(sess.path, out_chapter, plan)
    alignment = json.loads(
        (video_dir(sess.path) / f"chapter_{out_chapter}.alignment.json").read_text(encoding="utf-8"))
    timed = [{"text": s["text"], "start": s["start"], "end": s["end"],
              "hook": bool(p.get("hook")), "loops": list(p.get("loops") or []),
              "beat": bool(p.get("beat"))}
             for s, p in zip(alignment["segments"], picks)]
    for w in hook_structure(timed, float(settings["target_seconds"]))["warnings"]:
        log(f"  ! hook structure: {w}")
    dur = float((alignment.get("audio") or {}).get("duration") or 0.0)
    warn = duration_warning(dur, settings) if dur else None
    if warn:
        log(f"  ! {warn}")
    out = None
    if not plan_only:
        from .render import render

        out = render(sess.path, out_chapter, plan, cfg.video, preview=False,
                     workers=workers, log=log, panels_data=panels_data)
    return plan, panels_data, out


def _auto_file(root: Path, name: str) -> Path:
    return Path(root) / "intro" / name


def build_hook(sess, cfg, *, chapters: List[str] | None = None, audio: str | None = None,
               script: str | None = None, cues: str | None = None,
               target_seconds: float | None = None, seed: int | None = None,
               sync: str = "auto", fmt: str | None = None, plan_only: bool = False,
               workers: int = 0, allow_reuse: bool = False, log=print
               ) -> Tuple[Dict[str, Any], Path | None]:
    """`video hook` / `video intro --auto`: auto-pick + build the chapter-0 hook."""
    root = sess.path
    vcfg = dict(cfg.video)
    settings = hook_settings(vcfg)
    if target_seconds is not None:
        settings["target_seconds"] = max(float(settings["min_seconds"]),
                                         min(float(settings["max_seconds"]), float(target_seconds)))
    if seed is None:
        seed = int(settings["seed"])
    sentences = load_script_sentences(
        script_path=Path(script) if script else None,
        cues_path=Path(cues) if cues else None,
        default_path=root / "intro" / "intro.txt")
    if chapters is None:
        chapters = sorted(intro_mod._chapters_with_panels(sess),
                          key=lambda c: intro_mod._chapter_key(c))
    pool = build_pool(root, chapters)
    mem = load_memory(root)
    if any(s.get("panels") for s in sentences):
        picks = pinned_picks(sentences, pool)
        source = "panels pinned in cues file"
    else:
        used = set() if allow_reuse else used_refs(mem)
        picks = auto_pick(sentences, pool, used_refs=used, seed=seed)
        source = "auto-pick (edit panels, then re-run)"
    for w in memory_warnings(mem, [r for p in picks for r in p["panels"]]):
        log(f"  ! memory: {w}")
    auto_path = _auto_file(root, "intro.auto.json")
    auto_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(auto_path, json.dumps(
        {"version": 1, "kind": "hook", "source": source,
         "chapters": chapters, "seed": seed, "sentences": picks},
        indent=1, ensure_ascii=False))
    log(f"  wrote {auto_path.relative_to(root).as_posix()} (review / edit it, then re-run)")
    audio_path = _resolve_audio(root, audio, "chapter_0.mp3")
    plan, _, out = _build_from_picks(
        sess, cfg, kind="hook", out_chapter=intro_mod.INTRO_CHAPTER, audio=audio_path,
        picks=picks, settings=settings, sync=sync, fmt=fmt, plan_only=plan_only,
        workers=workers, log=log)
    record_use(mem, "intro", picks)
    save_memory(root, mem)
    return plan, out


def previously_chapter(chapters: List[str], audio: Path) -> str:
    """Output chapter for a previously video: 'prev' + next part number."""
    nums = [int(c) for c in chapters if str(c).isdigit()]
    if nums:
        return f"prev{max(nums) + 1}"
    m = re.search(r"(\d+)", audio.stem)
    return f"prev{m.group(1)}" if m else "prev1"


def build_previously(sess, cfg, *, chapters: List[str], audio: str | None = None,
                     script: str | None = None, cues: str | None = None,
                     target_seconds: float | None = None,
                     seed: int | None = None, sync: str = "auto", fmt: str | None = None,
                     plan_only: bool = False, workers: int = 0,
                     allow_reuse: bool = False, log=print
                     ) -> Tuple[Dict[str, Any], Path | None]:
    """`video previously`: 30s bridge over ONLY the given (previous) chapters."""
    root = sess.path
    vcfg = dict(cfg.video)
    settings = hook_settings(vcfg)
    if target_seconds is not None:
        settings["target_seconds"] = max(float(settings["min_seconds"]),
                                         min(float(settings["max_seconds"]), float(target_seconds)))
    if seed is None:
        seed = int(settings["seed"])
    if not chapters:
        raise UserError("video previously needs --chapters for the previous part, e.g. --chapters 1-6.")
    out_chapter = previously_chapter(chapters, Path(audio or ""))
    part_no = out_chapter[4:] if out_chapter.startswith("prev") else out_chapter
    default_script = root / "previously" / f"previously_{part_no}.txt"
    sentences = load_script_sentences(
        script_path=Path(script) if script else None,
        cues_path=Path(cues) if cues else None, default_path=default_script)
    # Beats come from the previous part's recaps (never its transcripts, never
    # the next part): they steer panel picking toward what the recap called out.
    series = read_series_memory(root)
    # Only threads that were already open at the end of the previous part:
    # a thread opened by a later chapter must never steer this bridge.
    max_chapter = chapters[-1] if chapters else None
    threads = []
    for t in (series.plot_threads or []):
        if (t.get("status") or "open") != "open":
            continue
        opened = str(t.get("opened") or "")
        if opened and max_chapter and chapter_key(opened) > chapter_key(max_chapter):
            continue
        threads.append(str(t.get("thread") or ""))
    recap_all: List[Dict[str, Any]] = []
    for c in chapters:
        recap_all.extend(recap_sentences(root, c))
    beats = select_beats(recap_all, count=PREVIOUSLY_BEATS, open_threads=threads)
    pool = build_pool(root, chapters)  # ONLY previous chapters: no next-part leak, by construction
    mem = load_memory(root)
    if any(s.get("panels") for s in sentences):
        picks = pinned_picks(sentences, pool)
        source = "panels pinned in cues file"
    else:
        used = set() if allow_reuse else used_refs(mem)
        picks = auto_pick(sentences, pool, used_refs=used, seed=seed,
                          prefer_prior_video=True,
                          extra_texts=[b["text"] for b in beats])
        source = "auto-pick from previous part recaps"
    for w in memory_warnings(mem, [r for p in picks for r in p["panels"]]):
        log(f"  ! memory: {w}")
    auto_path = root / "previously" / f"previously_{out_chapter}.auto.json"
    auto_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(auto_path, json.dumps(
        {"version": 1, "kind": "previously", "source": source,
         "chapters": chapters, "seed": seed,
         "beats": [b["text"] for b in beats],
         "sentences": picks}, indent=1, ensure_ascii=False))
    log(f"  wrote {auto_path.relative_to(root).as_posix()} (review / edit it, then re-run)")
    audio_path = _resolve_audio(root, audio, f"previously_{part_no}.mp3")
    plan, _, out = _build_from_picks(
        sess, cfg, kind="previously", out_chapter=out_chapter, audio=audio_path,
        picks=picks, settings=settings, sync=sync, fmt=fmt, plan_only=plan_only,
        workers=workers, log=log)
    record_use(mem, "previously", picks, extra={"for_chapters": chapters})
    save_memory(root, mem)
    return plan, out
