"""B1f — landmark slice hints: editor marks that cut ONE panel into
independent sub-panels where trim_v2's gutter slicing cannot see the
cut (a diagonal band, a continuous piece of art, a sub-15px black
separator). The marks live in cues.json "slice_hints" (the motion
hints' home), round-trip through pairs.json "_slice_hints", and are
consumed by the panel-fit planner: a hinted panel's unit becomes one
unit per kept band (P012-03#a, #b, ...), each fitted on its own
dimensions. Also covered: trim_v2 black-gutter slicing, the "zoom_in"
motion hint, and the precheck diagonal-band review flag.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from manhwa_ocr.config import load_config
from manhwa_ocr.video import build as build_mod
from manhwa_ocr.video import cues as cues_mod
from manhwa_ocr.video import plan as plan_mod
from manhwa_ocr.video import precheck as precheck_mod
from manhwa_ocr.video import slicehints as sh
from manhwa_ocr.video.frames import Format, ShotRenderer, format_from
from manhwa_ocr.video.plan import build_plan, panelfit_spec, validate
from manhwa_ocr.video.trim import slice_gaps

VCFG = load_config().video
FMT = format_from("16:9", 30)   # 1920x1080

PANELS = [
    {"id": "P001-01", "width": 900, "height": 1200, "text": ""},    # pillar -> zoom
    {"id": "P001-02", "width": 900, "height": 1400, "text": ""},    # narrow pillar -> drift
    {"id": "P001-03", "width": 1500, "height": 900, "text": ""},    # aspect 1.67 -> fill
]


def _img_for(p):
    img = Image.new("RGB", (int(p["width"]), int(p["height"])), (120, 90, 70))
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 11):
        d.line((x, 0, x, img.height), fill=(40, 30, 20))
    return img


def _pf_plan(sentences, duration, slice_hints=None, panels=None):
    vcfg = {**VCFG, "mode": "panelfit"}
    return build_plan(chapter="1", panels_data={"panels": panels or PANELS,
                                                "fingerprint": "pf"},
                      duration=duration, audio=None, vcfg=vcfg, image_for=_img_for,
                      sentences=sentences, slice_hints=slice_hints)


def _one_sentence(pid="P001-01", motion=None):
    s = {"start": 0.0, "text": "the feather drifts down", "panels": [pid],
         "cues": [], "emphasis": []}
    if motion is not None:
        s["motion"] = motion
    return [s]


# ------------------------------------------------------- spec parsing

def test_slice_spec_parsing_normalises_shorthand_and_object_forms():
    raw = {"P012-03": {"slice": [0.52], "discard": [[0.0, 0.06]]},
           "P013-01": [0.4, 0.7]}
    hints, problems = sh.parse_slice_hints(raw)
    assert problems == []
    assert hints == {"P012-03": {"slice": [0.52], "discard": [[0.0, 0.06]]},
                     "P013-01": {"slice": [0.4, 0.7], "discard": []}}
    # cues.slice_hints_of reads the same document shape.
    assert cues_mod.slice_hints_of({"slice_hints": raw}) == hints
    assert cues_mod.slice_hints_of({"sentences": []}) == {}
    assert cues_mod.slice_hints_of(None) == {}


@pytest.mark.parametrize("raw", [
    {"P001-01": {"slice": [0.0]}},                       # cut on the edge
    {"P001-01": {"slice": [1.0]}},                       # cut on the edge
    {"P001-01": {"slice": [0.6, 0.4]}},                  # not ascending
    {"P001-01": {"slice": [0.5, 0.5]}},                  # duplicate cut
    {"P001-01": {"slice": [], "discard": [[0.2, 0.6], [0.5, 0.8]]}},   # overlapping discards
    {"P001-01": {"slice": [0.5], "discard": [[0.4, 0.6]]}},            # cut inside a discard
    {"P001-01": {"slice": [], "discard": [[0.0, 1.0]]}},                # discards everything
    {"P001-01": {"slice": [0.5], "discard": [[0.52, 1.0]]}},           # sliver band left
    {"P001-01": {"slice": [0.5], "speed": 2}},                          # unknown key
    {"P001-01": {"discard": [[0.8, 0.2]]}},                            # inverted band
    {"P001-01": "0.5"},                                                # not a list/object
])
def test_slice_spec_parsing_rejects_bad_specs(raw):
    hints, problems = sh.parse_slice_hints(raw)
    assert problems and "P001-01" in problems[0]
    assert "P001-01" not in hints
    # ... and validate_cues refuses a cues document carrying them.
    doc = {"sentences": [{"text": "x", "panels": ["P001-01"]}], "slice_hints": raw}
    assert any("slice_hints" in p for p in cues_mod.validate_cues(doc))


def test_validate_cues_accepts_good_slice_hints():
    doc = {"sentences": [{"text": "x", "panels": ["P001-01"]}],
           "slice_hints": {"P001-01": {"slice": [0.52], "discard": [[0.0, 0.06]]}}}
    assert cues_mod.validate_cues(doc) == []


def test_kept_bands_split_at_cuts_and_drop_discards():
    assert sh.kept_bands({"slice": [0.5], "discard": [[0.0, 0.2]]}) == [(0.2, 0.5), (0.5, 1.0)]
    assert sh.kept_bands({"slice": [], "discard": [[0.4, 0.6]]}) == [(0.0, 0.4), (0.6, 1.0)]
    assert sh.kept_bands({"slice": [0.25, 0.75], "discard": []}) == \
        [(0.0, 0.25), (0.25, 0.75), (0.75, 1.0)]
    assert sh.slice_ids("P012-03", {"slice": [0.52], "discard": []}) == \
        ["P012-03#a", "P012-03#b"]
    assert sh.slice_id("P012-03", 0) == "P012-03#a"


# ------------------------------------------------------- unit expansion

def test_expand_units_splits_in_story_order_proportionally():
    unit = {"start": 0.0, "end": 10.0, "panels": ["P001-01"], "cues": ["impact"],
            "emphasis": [], "sentence": "x", "sentence_starts": [0.0]}
    other = {"start": 10.0, "end": 12.0, "panels": ["P001-02"], "cues": [],
             "emphasis": [], "sentence": "y", "sentence_starts": [10.0]}
    hints, problems = sh.parse_slice_hints({"P001-01": {"slice": [0.5]}})
    assert problems == []
    out = sh.expand_units([unit, other], hints)
    assert len(out) == 3
    a, b = out[0], out[1]
    assert (a["slice"], a["slice_id"]) == ([0.0, 0.5], "P001-01#a")
    assert (b["slice"], b["slice_id"]) == ([0.5, 1.0], "P001-01#b")
    assert (a["start"], a["end"], b["start"], b["end"]) == (0.0, 5.0, 5.0, 10.0)
    assert a["cues"] == ["impact"] and b["cues"] == []   # cues ride the first piece
    assert out[2] is other                               # unhinted: the same object
    assert sh.expand_units([unit], {}) == [unit]        # no hints: untouched


# ------------------------------------------------------------- planner

def test_build_plan_slices_a_hinted_panel_into_two_units_in_story_order():
    plan = _pf_plan(_one_sentence(), 5.0, slice_hints={"P001-01": {"slice": [0.52]}})
    cards = [c for s in plan["shots"] for c in s["cards"]]
    assert [c["slice_id"] for c in cards] == ["P001-01#a", "P001-01#b"]
    assert [c["slice"] for c in cards] == [[0.0, 0.52], [0.52, 1.0]]
    assert [c["panel"] for c in cards] == ["P001-01", "P001-01"]   # base id resolves the image
    assert [c["crop"] for c in cards] == [[0.0, 0.0, 1.0, 0.52], [0.0, 0.52, 1.0, 1.0]]
    shots = plan["shots"]
    assert len(shots) == 2
    assert float(shots[0]["end"]) == pytest.approx(float(shots[1]["start"]))
    # The window split proportionally to band lengths (0.52 / 0.48 of 5s),
    # inside hold_groups' cut budget.
    assert float(shots[0]["end"]) - float(shots[0]["start"]) == pytest.approx(2.6, abs=0.2)
    # Each slice was fitted on its OWN dimensions (900 x band height):
    # aspect 900/624 and 900/576 both route to a height-fit zoom.
    for c, band_h in zip(cards, (624.0, 576.0)):
        sp = c["panelfit"]
        assert sp["kind"] == "zoom"
        assert sp["base_w"] / sp["base_h"] == pytest.approx(900 / band_h, rel=0.01)
    assert plan["panelfit_slices"] == ["P001-01#a", "P001-01#b"]
    assert validate(plan, {p["id"] for p in PANELS}) == []
    qa = build_mod.quality_report(plan, {"panels": PANELS}, None, 3.0)
    assert not any("panel-fit" in f for f in qa["failures"])
    assert qa["panelfit_slices"] == ["P001-01#a", "P001-01#b"]


def test_build_plan_discard_band_is_removed_before_the_fit():
    plan = _pf_plan(_one_sentence(), 5.0,
                    slice_hints={"P001-01": {"slice": [0.6], "discard": [[0.0, 0.2]]}})
    cards = [c for s in plan["shots"] for c in s["cards"]]
    # The discarded [0, 0.2] band is in NO slice; the kept bands start at 0.2.
    assert [c["slice"] for c in cards] == [[0.2, 0.6], [0.6, 1.0]]
    assert [c["slice_id"] for c in cards] == ["P001-01#a", "P001-01#b"]
    assert validate(plan, {p["id"] for p in PANELS}) == []
    qa = build_mod.quality_report(plan, {"panels": PANELS}, None, 3.0)
    assert not any("panel-fit" in f for f in qa["failures"])


def test_build_plan_slices_on_the_alignment_units_path():
    # The live path: make_plan passes alignment_units (not sentences).
    units = [{"start": 0.0, "end": 5.0, "panels": ["P001-01"], "cues": [],
              "emphasis": [], "sentence": "the feather drifts down",
              "sentence_starts": [0.0]}]
    vcfg = {**VCFG, "mode": "panelfit"}
    plan = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "pf"},
                      duration=5.0, audio=None, vcfg=vcfg, image_for=_img_for,
                      alignment_units=units,
                      slice_hints={"P001-01": [0.5]})
    cards = [c for s in plan["shots"] for c in s["cards"]]
    assert [c["slice_id"] for c in cards] == ["P001-01#a", "P001-01#b"]
    assert validate(plan, {p["id"] for p in PANELS}) == []


def test_build_plan_bad_slice_hint_is_a_user_error():
    from manhwa_ocr.util import UserError

    with pytest.raises(UserError, match="slice_hints"):
        _pf_plan(_one_sentence(), 5.0, slice_hints={"P001-01": {"slice": [1.5]}})


def test_inert_without_hints_plan_is_identical():
    sentences = [
        {"start": 0.0, "text": "one", "panels": ["P001-01"], "cues": [], "emphasis": []},
        {"start": 5.0, "text": "two", "panels": ["P001-02"], "cues": [], "emphasis": []},
        {"start": 10.0, "text": "three", "panels": ["P001-03"], "cues": ["impact"],
         "emphasis": []},
    ]
    plain = _pf_plan(sentences, 15.0)
    none_kwarg = _pf_plan(sentences, 15.0, slice_hints=None)
    empty = _pf_plan(sentences, 15.0, slice_hints={})
    blob = json.dumps(plain, sort_keys=True)
    assert json.dumps(none_kwarg, sort_keys=True) == blob
    assert json.dumps(empty, sort_keys=True) == blob
    assert plain["panelfit_slices"] == []
    assert all("slice" not in c for s in plain["shots"] for c in s["cards"])
    # Other modes ignore the hints entirely (they are a panelfit surface).
    classic = {**VCFG, "mode": "classic"}
    a = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "pf"},
                   duration=15.0, audio=None, vcfg=classic, image_for=_img_for,
                   sentences=sentences)
    b = build_plan(chapter="1", panels_data={"panels": PANELS, "fingerprint": "pf"},
                   duration=15.0, audio=None, vcfg=classic, image_for=_img_for,
                   sentences=sentences, slice_hints={"P001-01": {"slice": [0.5]}})
    assert json.dumps(a["shots"], sort_keys=True) == json.dumps(b["shots"], sort_keys=True)


def test_render_slice_card_shows_only_its_band():
    # PanelFitView crops the panel image to the card's band: the top
    # half of this panel is red, the bottom half blue — slice #b must
    # compose from blue alone (its centre pixel is blue, never red).
    img = Image.new("RGB", (900, 1200), (255, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 600, 900, 1199), fill=(0, 0, 255))
    spec = panelfit_spec(900, 600, "push_in", FMT, {}, dur=4.0)
    shot = {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
            "background": "blur", "panelfit": True, "sentence": None,
            "cards": [{"panel": "P001-01", "crop": [0, 0.5, 1, 1], "enter": "cut",
                       "at": 0.0, "motion": "push_in", "panelfit": spec,
                       "slice": [0.5, 1.0], "slice_id": "P001-01#b"}],
            "effects": [], "callouts": []}
    r = ShotRenderer(shot, {"P001-01": img}, FMT)
    frame = r.frame_array(0.5)
    assert tuple(frame[540, 960]) == (0, 0, 255)


# --------------------------------------------------------- black gutters

def _strip(gap_lo, gap_hi, h=600, gap_fill=(255, 255, 255)):
    arr = np.zeros((h, 60, 3), dtype=np.uint8)
    arr[:] = (255, 255, 255)
    rng = np.random.default_rng(3)
    arr[0:gap_lo, :] = rng.integers(60, 200, size=(gap_lo, 60, 3), dtype=np.uint8)
    arr[gap_lo:gap_hi] = gap_fill
    arr[gap_hi:h, :] = rng.integers(60, 200, size=(h - gap_hi, 60, 3), dtype=np.uint8)
    return Image.fromarray(arr, "RGB")


def test_black_gutter_over_the_floor_slices_like_white():
    assert slice_gaps(_strip(200, 240, gap_fill=(0, 0, 0))) == [(0, 200), (240, 600)]
    assert slice_gaps(_strip(200, 240)) == [(0, 200), (240, 600)]   # white unchanged


def test_black_gutter_under_the_floor_does_not_slice():
    assert slice_gaps(_strip(200, 210, gap_fill=(0, 0, 0))) == [(0, 600)]


def test_mixed_black_white_rows_are_not_a_gutter():
    # A checkerboard band: neither the white share nor the black share
    # of any row is near-pure, so it must never slice.
    arr = np.zeros((600, 60, 3), dtype=np.uint8)
    rng = np.random.default_rng(3)
    arr[0:200, :] = rng.integers(60, 200, size=(200, 60, 3), dtype=np.uint8)
    arr[400:600, :] = rng.integers(60, 200, size=(200, 60, 3), dtype=np.uint8)
    band = np.indices((200, 60)).sum(axis=0) % 2
    arr[200:400, :] = np.where(band[..., None] == 0, 255, 0).astype(np.uint8)
    assert slice_gaps(Image.fromarray(arr, "RGB")) == [(0, 600)]


# -------------------------------------------------------------- zoom_in

def test_zoom_in_hint_accepted_everywhere_motion_hints_are():
    assert "zoom_in" in cues_mod.MOTION_HINTS
    assert plan_mod.PF_MOTION_HINTS == cues_mod.MOTION_HINTS
    ok = {"sentences": [{"text": "x", "panels": ["P001-01"], "motion": "zoom_in"},
                        {"text": "y", "panels": ["P001-01", "P001-02"],
                         "motion": ["zoom_in", "default"]}]}
    assert cues_mod.validate_cues(ok) == []
    from manhwa_ocr.video.pairing import validate_pairs

    by_id = {"P001-01": {}, "P001-02": {}}
    assert validate_pairs({"1": {"panels": ["P001-01"], "motion": "zoom_in"}}, 3, by_id) == []


def _plan_with_spec(spec):
    return {"version": plan_mod.PLAN_VERSION, "duration": 4.0, "shots": [
        {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single", "background": "blur",
         "exit": "cut", "transition": "cut",
         "cards": [{"panel": "P001-01", "crop": [0, 0, 1, 1], "enter": "cut", "at": 0.0,
                    "motion": "push_in", "panelfit": spec}],
         "effects": [], "callouts": []}],
        "chapter_cards": [], "sfx": [], "captions": []}


def test_spec_zoom_in_forces_the_zoom_geometry_at_the_standard_rate():
    # A narrow pillar (900x1400) would DRIFT by aspect; the hint forces
    # a zoom instead, at the standard rate — the mirror of zoom_out.
    plain = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=4.0)
    assert plain["kind"] == "drift"
    sp = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=4.0, hint="zoom_in")
    assert sp["kind"] == "zoom" and sp["dir"] == "in"
    assert sp["zoom_rate"] == plan_mod.PF_ZOOM_RATE == 0.05
    assert sp["zoom_start"] == pytest.approx(1.0)
    assert sp["zoom_end"] == pytest.approx(1.20)
    # A strip (900x2400) would TRAVERSE; the hint forces a zoom there too.
    strip = panelfit_spec(900, 2400, "pan_down", FMT, {}, dur=4.0, hint="zoom_in")
    assert strip["kind"] == "zoom" and strip["dir"] == "in"
    # Span slice: a sub-shot opens at the scale the span has reached.
    mid = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=2.0, span_dur=4.0,
                        elapsed=2.0, hint="zoom_in")
    assert mid["zoom_start"] == pytest.approx(1.10)
    assert validate(_plan_with_spec(sp), {"P001-01"}) == []
    assert validate(_plan_with_spec(strip), {"P001-01"}) == []


def test_build_plan_zoom_in_hint_reaches_the_spec_and_the_report():
    plan = _pf_plan(_one_sentence(pid="P001-02", motion="zoom_in"), 5.0)
    specs = [c["panelfit"] for s in plan["shots"] for c in s["cards"]]
    assert specs and all(sp["kind"] == "zoom" and sp["dir"] == "in"
                         and sp["zoom_rate"] == 0.05 for sp in specs)
    assert set(plan["panelfit_zoom_in"]) == {s["id"] for s in plan["shots"]}
    assert validate(plan, {p["id"] for p in PANELS}) == []
    qa = build_mod.quality_report(plan, {"panels": PANELS}, None, 3.0)
    assert qa["panelfit_zoom_in"] == plan["panelfit_zoom_in"]


def test_render_zoom_in_hint_grows_at_the_standard_rate():
    img = Image.new("RGB", (900, 1400), (200, 40, 40))
    ImageDraw.Draw(img).rectangle((300, 500, 600, 900), fill=(40, 40, 200))
    spec = panelfit_spec(900, 1400, "pan_down", FMT, {}, dur=4.0, hint="zoom_in")
    shot = {"id": "S001", "start": 0.0, "end": 4.0, "layout": "single",
            "background": "blur", "panelfit": True, "sentence": None,
            "cards": [{"panel": "P001-02", "crop": [0, 0, 1, 1], "enter": "cut",
                       "at": 0.0, "motion": "push_in", "panelfit": spec}],
            "effects": [], "callouts": []}
    r = ShotRenderer(shot, {"P001-02": img}, FMT)

    def half_width(arr):
        row = arr[540]
        blue = np.flatnonzero((row[:, 2] > 128) & (row[:, 0] < 128))
        return (blue[-1] - blue[0]) / 2.0 if len(blue) else None

    w1, w2 = half_width(r.frame_array(1.0)), half_width(r.frame_array(2.0))
    assert w1 and w2 and w2 / w1 == pytest.approx(1.10 / 1.05, abs=0.015)


# ----------------------------------------------------- pairs round-trip

def _pairing_session(tmp_path):
    root = tmp_path / "s"
    (root / "media" / "ch1").mkdir(parents=True)
    (root / "recaps").mkdir(parents=True)
    (root / ".work" / "video" / "ch1").mkdir(parents=True)
    (root / "media" / "ch1" / "panels.json").write_text(json.dumps({
        "version": 4, "chapter": "1", "norm_width": 900,
        "panels": [{"id": "P001-01"}, {"id": "P001-02"}]}), encoding="utf-8")
    cues = {"version": 1, "chapter": "1",
            "sentences": [{"text": "x", "panels": ["P001-01"], "cues": [],
                           "emphasis": [], "same": False}],
            "tagged_ratio": 1.0, "unknown_panels": []}
    (root / "recaps" / "chapter_1.cues.json").write_text(json.dumps(cues), encoding="utf-8")
    return root


def test_pairs_slice_hints_validate_and_apply_into_cues(tmp_path):
    from manhwa_ocr.video import pairing

    root = _pairing_session(tmp_path)
    by_id = {"P001-01": {}, "P001-02": {}}
    good = {"1": ["P001-01"], "_slice_hints": {"P001-01": {"slice": [0.5]}}}
    assert pairing.validate_pairs(good, 1, by_id) == []
    bad_panel = {"1": ["P001-01"], "_slice_hints": {"P009-99": {"slice": [0.5]}}}
    assert any("_slice_hints" in e for e in pairing.validate_pairs(bad_panel, 1, by_id))
    bad_spec = {"1": ["P001-01"], "_slice_hints": {"P001-01": {"slice": [2.0]}}}
    assert any("_slice_hints" in e for e in pairing.validate_pairs(bad_spec, 1, by_id))

    (root / ".work" / "video" / "ch1" / "pairs.json").write_text(
        json.dumps(good), encoding="utf-8")
    pairing.apply(root, "1", log=lambda *a: None)
    cues = json.loads((root / "recaps" / "chapter_1.cues.json").read_text(encoding="utf-8"))
    assert cues["slice_hints"] == {"P001-01": {"slice": [0.5], "discard": []}}
    assert cues_mod.validate_cues(cues) == []
    assert cues_mod.slice_hints_of(cues) == {"P001-01": {"slice": [0.5], "discard": []}}


# ------------------------------------------------- diagonal precheck flag

def _diagonal_img(w=300, h=400):
    img = Image.new("RGB", (w, h), (30, 30, 30))
    d = ImageDraw.Draw(img)
    # A 60px-wide white band walking from the top-left to the
    # bottom-right: in every row it crosses, the light pixels' centre
    # moves steadily right.
    d.polygon([(0, 0), (60, 0), (w, h), (w - 60, h)], fill=(255, 255, 255))
    return img


def test_diagonal_light_band_fires_on_a_synthetic_slash():
    band = precheck_mod.diagonal_light_band(_diagonal_img())
    assert band is not None
    assert band["shift"] > 0.3 and band["r2"] >= 0.85
    assert band["span"][1] - band["span"][0] >= 0.25


def test_diagonal_light_band_quiet_on_clean_panels():
    # Textured mid-tone art: no light pixels at all.
    rng = np.random.default_rng(3)
    art = Image.fromarray(rng.integers(40, 180, size=(400, 300, 3), dtype=np.uint8), "RGB")
    assert precheck_mod.diagonal_light_band(art) is None
    # A horizontal white gutter: the centroid sits at the centre and
    # never walks — that is trim_v2's slice, not a diagonal.
    img = Image.new("RGB", (300, 400), (30, 30, 30))
    ImageDraw.Draw(img).rectangle((0, 180, 299, 219), fill=(255, 255, 255))
    assert precheck_mod.diagonal_light_band(img) is None
    # A vertical white band: constant centroid across every row.
    img = Image.new("RGB", (300, 400), (30, 30, 30))
    ImageDraw.Draw(img).rectangle((120, 0, 179, 399), fill=(255, 255, 255))
    assert precheck_mod.diagonal_light_band(img) is None


def test_precheck_chapter_flags_a_diagonal_panel_and_stays_inert_without_one(tmp_path):
    from manhwa_ocr.video import detect

    def session(diagonal: bool):
        root = tmp_path / ("diag" if diagonal else "clean")
        (root / "media" / "ch1" / "pages").mkdir(parents=True)
        (root / "video").mkdir(parents=True)
        page = _diagonal_img() if diagonal else Image.new("RGB", (300, 400), (30, 30, 30))
        page.save(root / "media" / "ch1" / "pages" / "page_001.png")
        panel = {"id": "P001-01", "index": 1, "y0": 0.0, "y1": 400.0, "x0": 0.0,
                 "x1": 300.0, "width": 300, "height": 400,
                 "segments": [{"page": 1, "file": "page_001.png", "y0": 0, "y1": 400,
                               "page_w": 300}]}
        (root / "media" / "ch1" / "panels.json").write_text(json.dumps({
            "version": 4, "chapter": "1", "norm_width": 300, "panels": [panel]}),
            encoding="utf-8")
        plan = {"shots": [{"id": "S001", "cards": [
            {"panel": "P001-01", "crop": [0.0, 0.0, 1.0, 1.0]}]}]}
        (root / "video" / "chapter_1.plan.json").write_text(json.dumps(plan), encoding="utf-8")
        d = detect.boxes_dir(root, "1")
        d.mkdir(parents=True, exist_ok=True)
        (d / "P001-01.json").write_text(json.dumps(
            {"panel": "P001-01", "key": "x", "boxes": [
                {"label": "bubble", "score": 0.9, "x0": 0.2, "y0": 0.2, "x1": 0.8, "y1": 0.4}]}),
            encoding="utf-8")
        return root

    rep = precheck_mod.precheck_chapter(session(True), "1")
    assert len(rep["needs_slice_hint"]) == 1
    flag = rep["needs_slice_hint"][0]
    assert flag["panel"] == "P001-01" and flag["flag"] == "needs_slice_hint"
    assert flag["shots"] == ["S001"]
    # The flag is a review item only: the bisection findings are untouched.
    assert rep["findings"] == []

    rep = precheck_mod.precheck_chapter(session(False), "1")
    assert rep["needs_slice_hint"] == []
