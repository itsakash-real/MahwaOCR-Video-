"""B1f — landmark slice hints: editor marks that cut ONE panel into
independent sub-panels at chosen rows.

Why: the Rule 6 prep (trim.py trim_v2) slices glued panels only at pure
white/black gutter rows over 15px. Real editorial cuts are often not
gutters at all:

  * a panel split by a DIAGONAL white band (Solo ch1's "police slash"
    panel) — no single pure row exists to slice at;
  * one continuous piece of art whose cut is a LANDMARK, not a gap
    (Solo ch1's feather -> BUT panel);
  * a black separator thinner than the 15px gutter floor.

For those, the editor marks the cut by hand. The marks live in the same
place as the per-sentence motion hints (B1e): cues.json — with a
pairs.json round-trip (pairing.py: the editor edits "_slice_hints" in
pairs.json, `video pair --apply` writes it into cues.json) — but keyed
by PANEL id, because a cut is a property of the panel, not of one
sentence that happens to show it:

    "slice_hints": {
        "P012-03": {"slice": [0.52],
                    "discard": [[0.0, 0.06]]},
        "P013-01": [0.4, 0.7]
    }

Format (this docstring is the spec; cues.validate_cues enforces it):
  * each key is a panel id ("P012-03");
  * the value is either a list of cut fractions (shorthand), or an
    object {"slice": [...], "discard": [[start, end], ...]};
  * "slice" is a list of cut positions as fractions of the panel's
    height, each strictly inside (0, 1), ascending, no duplicates: the
    panel becomes independent sub-panels at those rows;
  * "discard" is a list of [start, end] fraction pairs (0 <= start <
    end <= 1), non-overlapping: those bands are removed entirely,
    BEFORE any trim/height-fit logic sees the slices;
  * unknown keys, out-of-range fractions, overlapping discards and
    cuts inside a discarded band are validation errors, never ignored.

Consumption (plan.py build_plan, panel-fit mode only): a timed unit
showing a hinted panel is replaced by one unit per KEPT band, in story
order, splitting the unit's window proportionally to band lengths.
Each slice unit's card keeps the base panel id (every downstream
consumer — validate, render, storyboard, sync audit — resolves panels
by that id) and carries:

    "slice":    [f0, f1]          the band, as panel-height fractions
    "slice_id": "P012-03#a"       deterministic id of the slice: the
                                  base id + "#" + a, b, c, ... in story
                                  order (trim_v2's sub-panels get fresh
                                  canonical ids in panels.json because
                                  they are minted at panel build; a plan
                                  -time slice keeps its base id for image
                                  lookup, so its id is the base id plus
                                  the piece letter, exactly the fallback
                                  form the B1f spec names)

The panelfit pass then fits each slice on its OWN dimensions (panel
width x band height) through the normal fit/drift/traverse logic, and
frames.PanelFitView crops the panel image to the band before composing.
With no hints present, nothing in this module runs and plans are
byte-identical to before.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

MIN_BAND = 0.05   # a kept band thinner than this share of the panel is a
                  # sliver, not a placeable slice: it is a spec error when
                  # it comes from cuts/discards colliding, so validation
                  # refuses it instead of planning an invisible shot (the
                  # floor matches plan.validate's minimum crop height)


def _is_frac(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_slice_hints(raw: Any) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Validate + normalise a raw "slice_hints" value.

    Returns (hints, problems): hints maps panel id ->
    {"slice": [fractions ascending], "discard": [[start, end], ...]}
    (both keys always present after normalisation). problems are
    line-level messages in the validate_cues style; when problems is
    non-empty the hints dict holds only the entries that parsed cleanly
    and callers must refuse the document (validate_cues does).
    """
    hints: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    if raw is None:
        return hints, problems
    if not isinstance(raw, dict):
        return hints, ['"slice_hints" must be an object: panel id -> '
                       '{"slice": [fractions], "discard": [[start, end], ...]}']
    for pid, spec in raw.items():
        where = f'"slice_hints" panel {pid!r}'
        if not isinstance(pid, str) or not pid:
            problems.append(f"{where}: panel ids must be non-empty strings")
            continue
        cuts: Any = None
        discards: Any = []
        if isinstance(spec, list):
            cuts = spec                     # shorthand: cuts only
        elif isinstance(spec, dict):
            extra = set(spec) - {"slice", "discard"}
            if extra:
                problems.append(f"{where}: unknown keys {sorted(extra)} — a slice hint is "
                                f"{{\"slice\": [...], \"discard\": [[start, end], ...]}}")
                continue
            cuts = spec.get("slice")
            discards = spec.get("discard") or []
        else:
            problems.append(f"{where}: must be a list of cut fractions or an object "
                            f"{{\"slice\": [...], \"discard\": [...]}}")
            continue
        ok = True
        if cuts is None:
            cuts = []
        if not isinstance(cuts, list) or not all(_is_frac(c) for c in cuts):
            problems.append(f"{where}: \"slice\" must be a list of numbers (fractions of "
                            f"the panel's height)")
            continue
        cuts = [float(c) for c in cuts]
        if any(not 0.0 < c < 1.0 for c in cuts):
            problems.append(f"{where}: every \"slice\" cut must lie strictly inside "
                            f"(0, 1) (got {cuts})")
            ok = False
        if cuts != sorted(set(cuts)):
            problems.append(f"{where}: \"slice\" cuts must be ascending with no duplicates "
                            f"(got {cuts})")
            ok = False
        if not isinstance(discards, list) or not all(
                isinstance(d, list) and len(d) == 2 and all(_is_frac(v) for v in d)
                for d in discards):
            problems.append(f"{where}: \"discard\" must be a list of [start, end] "
                            f"fraction pairs")
            continue
        bands = sorted((float(a), float(b)) for a, b in discards)
        if any(not 0.0 <= a < b <= 1.0 for a, b in bands):
            problems.append(f"{where}: every \"discard\" band must satisfy "
                            f"0 <= start < end <= 1 (got {discards})")
            ok = False
        if any(bands[i][1] > bands[i + 1][0] for i in range(len(bands) - 1)):
            problems.append(f"{where}: \"discard\" bands must not overlap (got {discards})")
            ok = False
        if ok:
            inside = [c for c in cuts
                      if any(a - 1e-9 <= c <= b + 1e-9 for a, b in bands)]
            if inside:
                problems.append(f"{where}: \"slice\" cut(s) {inside} fall inside a "
                                f"discarded band — move the cut or the band")
                ok = False
        if not ok:
            continue
        hints[pid] = {"slice": cuts, "discard": [[a, b] for a, b in bands]}
    # A spec whose cuts + discards leave a sliver band (or nothing at
    # all) cannot be planned honestly: refuse it here, by name.
    for pid, spec in list(hints.items()):
        bands = kept_bands(spec)
        if not bands:
            problems.append(f'"slice_hints" panel {pid!r}: the discards remove the '
                            f"whole panel — nothing would be left to show")
            del hints[pid]
        elif any(b - a < MIN_BAND for a, b in bands):
            problems.append(f'"slice_hints" panel {pid!r}: cuts/discards leave a band '
                            f"thinner than {MIN_BAND:.0%} of the panel "
                            f"({[(round(a, 3), round(b, 3)) for a, b in bands]})")
            del hints[pid]
    return hints, problems


def kept_bands(spec: Dict[str, Any]) -> List[Tuple[float, float]]:
    """The panel's kept bands after discards + cuts, in story order.

    Discard bands are removed first; the surviving intervals are then
    split at every cut fraction that falls inside them. Returns
    [(f0, f1), ...] with 0 <= f0 < f1 <= 1."""
    cuts = sorted(float(c) for c in (spec.get("slice") or []))
    kept: List[Tuple[float, float]] = [(0.0, 1.0)]
    for a, b in sorted((float(x), float(y)) for x, y in (spec.get("discard") or [])):
        nxt: List[Tuple[float, float]] = []
        for f0, f1 in kept:
            if b <= f0 or a >= f1:
                nxt.append((f0, f1))
                continue
            if a > f0:
                nxt.append((f0, a))
            if b < f1:
                nxt.append((b, f1))
        kept = nxt
    out: List[Tuple[float, float]] = []
    for f0, f1 in kept:
        marks = [f0] + [c for c in cuts if f0 < c < f1] + [f1]
        out.extend(zip(marks, marks[1:]))
    return [(a, b) for a, b in out if b - a > 1e-9]


def slice_id(pid: str, index: int) -> str:
    """The deterministic id of a panel's index-th slice (0-based):
    P012-03#a, P012-03#b, ... (past z, #a1, #b1, ...)."""
    letter = chr(ord("a") + index % 26)
    cycle = index // 26
    return f"{pid}#{letter}{cycle or ''}"


def slice_ids(pid: str, spec: Dict[str, Any]) -> List[str]:
    """The slice ids of one hinted panel, in story order."""
    return [slice_id(pid, i) for i in range(len(kept_bands(spec)))]


def expand_units(units: List[Dict[str, Any]],
                 hints: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replace every single-panel unit showing a hinted panel by one
    unit per kept band, in story order (B1f plumbing).

    The unit's window is split proportionally to band lengths; the
    first piece keeps the unit's cues (mirroring synced_units' split of
    a multi-panel sentence), emphasis entries are kept on the piece
    whose window contains them, and every piece carries "slice" /
    "slice_id" so the designer and the panelfit pass can treat it as an
    independent placeable unit. Units showing several panels (a duo)
    or an unhinted panel pass through unchanged — the SAME objects, so
    a hints-free chapter's units (and therefore its plan) are
    untouched. A unit whose panel is fully discarded vanishes (its
    neighbours absorb the time in hold_groups, as for any removed
    unit); parse_slice_hints already refuses such a spec, so this is
    only a defensive path for direct callers.
    """
    if not hints:
        return units
    out: List[Dict[str, Any]] = []
    for unit in units:
        panels = unit.get("panels") or []
        if len(panels) != 1 or panels[0] not in hints:
            out.append(unit)
            continue
        pid = panels[0]
        bands = kept_bands(hints[pid])
        if not bands:
            continue
        if len(bands) == 1 and bands[0] == (0.0, 1.0):
            out.append(unit)
            continue
        start, end = float(unit["start"]), float(unit["end"])
        total = sum(b - a for a, b in bands) or 1.0
        cursor = start
        for k, (f0, f1) in enumerate(bands):
            piece_end = end if k == len(bands) - 1 else cursor + (end - start) * (f1 - f0) / total
            piece = {**unit,
                     "start": cursor, "end": piece_end,
                     "cues": list(unit.get("cues") or []) if k == 0 else [],
                     "emphasis": [e for e in (unit.get("emphasis") or [])
                                  if isinstance(e, (int, float)) and cursor <= float(e) < piece_end],
                     "sentence_starts": [float(unit["sentence_starts"][0])]
                     if k == 0 and unit.get("sentence_starts") else [cursor],
                     "slice": [round(f0, 4), round(f1, 4)],
                     "slice_id": slice_id(pid, k)}
            out.append(piece)
            cursor = piece_end
    return out
