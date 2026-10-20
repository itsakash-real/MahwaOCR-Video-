"""Wide mode ("video.mode: wide"): classic editing, full-height panels,
min 8.5/16 of the frame wide, upscale capped for 1080p sharpness."""
from PIL import Image

from manhwa_ocr.video.frames import Card, Format
from manhwa_ocr.video.plan import _wide_pass, wide_crop


def _panel(w, h, color=(180, 60, 60)):
    return Image.new("RGB", (w, h), color)


def _shot(layout, pid="P001-01", crop=None):
    return {"id": "S001", "start": 0.0, "end": 4.0, "layout": layout,
            "cards": [{"panel": pid, "crop": crop or [0, 0, 1, 1],
                       "motion": "push_in", "enter": "cut", "at": 0.0}],
            "background": "blur"}


class _Designer:
    def __init__(self, dims):
        self.by_id = {pid: {"width": w, "height": h} for pid, (w, h) in dims.items()}


FMT = Format(1920, 1080, 30)


# ------------------------------------------------------------- wide_crop


def test_wide_crop_window_fills_min_width_and_contains_subject():
    # A tall panel, classic crop = whole panel: the window is the tallest
    # the min-width rule allows (900/0.889 = 1012px), centred on the subject.
    box = wide_crop(900, 2406, [0, 0, 1, 1], min_win_h=720)
    w = (box[2] - box[0]) * 900
    h = (box[3] - box[1]) * 2406
    assert abs(w / h - 0.889) < 0.01
    assert h >= 720


def test_wide_crop_wide_panel_goes_fullbleed():
    # A wide panel (>= 0.889): shown whole, never cropped.
    box = wide_crop(1600, 900, [0, 0, 1, 1], min_win_h=720)
    assert box == [0.0, 0.0, 1.0, 1.0]


def test_wide_crop_superwide_panel_shown_whole():
    # An extra-wide panel: shown whole too (the card goes full-bleed
    # with fill top/bottom rather than cropping the sides).
    box = wide_crop(900, 361, [0, 0, 1, 1], min_win_h=720)
    assert box == [0.0, 0.0, 1.0, 1.0]


def test_wide_crop_contains_subject_with_headroom():
    # The subject (classic crop) sits fully inside the window, with room
    # to spare on both ends (the zoom eats the headroom, not the subject).
    box = wide_crop(900, 2406, [0, 0.30, 1, 0.55], min_win_h=720)
    assert box[1] * 2406 < 0.30 * 2406
    assert box[3] * 2406 > 0.55 * 2406


def test_wide_crop_bottom_subject_stays_in_panel():
    box = wide_crop(900, 2406, [0, 0.7, 1, 1.0], min_win_h=720)
    assert box[3] == 1.0                      # window bottom = panel bottom
    assert box[1] * 2406 <= 0.7 * 2406       # subject top inside the window


def test_wide_crop_short_panel_shown_whole():
    # A short panel (< the upscale floor tall): shown whole rather than
    # cropped -- a chat window's text must not be cut at the sides.
    box = wide_crop(900, 579, [0, 0, 1, 1], min_win_h=720)
    assert box == [0.0, 0.0, 1.0, 1.0]


def test_wide_crop_never_leaves_the_panel():
    for pw, ph in ((900, 2406), (900, 579), (500, 1200), (1600, 900)):
        for box0 in ([0, 0, 1, 1], [0.1, 0.4, 0.9, 0.8]):
            b = wide_crop(pw, ph, box0, min_win_h=720)
            assert 0.0 <= b[0] <= b[2] <= 1.0 and 0.0 <= b[1] <= b[3] <= 1.0


def test_wide_crop_pair_window():
    box = wide_crop(900, 2406, [0.1, 0.1, 0.9, 0.9], min_win_h=720,
                    pair_aspect=0.889)
    w = (box[2] - box[0]) * 900
    h = (box[3] - box[1]) * 2406
    assert abs(w / h - 0.889) < 1e-6
    assert h >= 720


def test_wide_crop_shows_whole_panel_when_wide_enough():
    # A 1.0-aspect panel (>= 0.889) shown whole at full height is 1080px
    # wide: the full figure stays visible, nothing cropped.
    box = wide_crop(900, 900, [0, 0.1, 1, 0.6], min_win_h=720)
    assert box == [0.0, 0.0, 1.0, 1.0]


def test_wide_crop_narrow_panel_window():
    # Aspect 0.71 < 0.889: a window at the min display width, containing
    # the classic crop's subject.
    box = wide_crop(900, 1273, [0, 0, 1, 0.9], min_win_h=720)
    w = (box[2] - box[0]) * 900
    h = (box[3] - box[1]) * 1273
    assert abs(w / h - 0.889) < 0.01
    # Subject (1146px) is taller than the min-width cap (1012px): the
    # window covers its centre instead.
    assert box[1] * 1273 < 0.45 * 1273 < box[3] * 1273


def test_wide_crop_pair_aspect():
    box = wide_crop(900, 1200, [0, 0, 1, 1], min_win_h=720, pair_aspect=0.8778)
    w = (box[2] - box[0]) * 900
    h = (box[3] - box[1]) * 1200
    assert abs(w / h - 0.8778) < 1e-9


# ------------------------------------------------------------- _wide_pass


def test_wide_pass_rewrites_shots():
    d = _Designer({"P001-01": (900, 2406), "P002-01": (1600, 900)})
    shots = [_shot("single", "P001-01"), _shot("wide", "P002-01")]
    out = _wide_pass(shots, d, FMT, {"max_upscale": 1.5})
    for s in out:
        assert s["wide"] is True
        assert s["caption_bar"] is False
        assert s["layout"] == "single"
        assert len(s["cards"]) == 1
        c = s["cards"][0]
        assert c["wide_display"] is True
        assert c["pad_y"] == 0.0
        box = c["crop"]
        assert 0.0 <= box[0] < box[2] <= 1.0
        assert 0.0 <= box[1] < box[3] <= 1.0


def test_wide_pass_keeps_pairs_but_widens_crops():
    d = _Designer({"P001-01": (900, 1200), "P002-01": (900, 1200)})
    s = _shot("pair", "P001-01")
    s["cards"].append({"panel": "P002-01", "crop": [0, 0, 1, 1],
                       "motion": "push_in", "enter": "cut", "at": 1.0})
    out = _wide_pass([s], d, FMT, {})
    assert out[0]["layout"] == "pair"
    assert len(out[0]["cards"]) == 2
    for c in out[0]["cards"]:
        box = c["crop"]
        w = (box[2] - box[0]) * 900
        h = (box[3] - box[1]) * 1200
        assert abs(w / h - (960 / 1080)) < 0.01


def test_wide_pass_skips_portrait_formats():
    d = _Designer({"P001-01": (900, 2406)})
    shots = [_shot("single", "P001-01")]
    out = _wide_pass(shots, d, Format(1080, 1920, 30), {})
    assert "wide" not in out[0]


# ------------------------------------------------------------- Card


def test_card_wide_display_min_width():
    card = Card(_panel(900, 1200), {"panel": "A", "crop": [0, 0, 1, 1],
                                   "wide_display": True},
                "single", 0, 0.0, 4.0, FMT, None)
    assert card.wide_display is True
    assert card.ch == 1080
    assert card.cw >= round(0.53125 * 1920)  # 8.5/16 of the frame
    assert card.rest_c == (960.0, 540.0)


def test_card_wide_display_superwide_fullbleed():
    card = Card(_panel(900, 361), {"panel": "A", "crop": [0, 0, 1, 1],
                                  "wide_display": True},
                "single", 0, 0.0, 4.0, FMT, None)
    assert card.cw == 1920
    assert card.ch == round(1920 / (900 / 361))   # full width, scaled height
    assert card.rest_c == (960.0, 540.0)           # centred vertically


def test_card_wide_display_pair_splits_frame():
    mk = lambda spec, i: Card(_panel(900, 1200), spec, "pair", i, 0.0, 4.0, FMT, None)
    c0 = mk({"panel": "A", "crop": [0, 0, 1, 1], "wide_display": True}, 0)
    c1 = mk({"panel": "B", "crop": [0, 0, 1, 1], "wide_display": True}, 1)
    assert c0.cw == c1.cw == 960  # exactly 8/16 each
    assert c0.ch == c1.ch == 1080
    assert c0.rest_c[0] < 960 < c1.rest_c[0]
    assert c0.rest_c[1] == c1.rest_c[1] == 540.0


def test_card_classic_sizing_unchanged():
    card = Card(_panel(900, 1200), {"panel": "A", "crop": [0, 0, 1, 1]},
                "single", 0, 0.0, 4.0, FMT, None)
    assert card.wide_display is False
    assert card.ch < 1080  # classic cards stay in the top 92%


def test_wide_background_is_brighter_than_classic():
    from manhwa_ocr.video.frames import background
    panel = _panel(900, 1200, (120, 40, 40))
    classic = background("blur", panel, FMT).mean()
    light = background("blur", panel, FMT, light=True).mean()
    assert light > classic * 1.3


def test_write_ass_margin_override():
    import tempfile
    from manhwa_ocr.video.captions import write_ass
    with tempfile.TemporaryDirectory() as d:
        ass = write_ass([{"start": 0.0, "end": 1.0, "text": "hello"}], d + "/c.ass", fmt=FMT)
        text = ass.read_text()
        assert ",16,1" in text  # default MarginV
    with tempfile.TemporaryDirectory() as d:
        ass = write_ass([{"start": 0.0, "end": 1.0, "text": "hello"}], d + "/c.ass", fmt=FMT, margin_v=90)
        assert ",90,1" in ass.read_text()
