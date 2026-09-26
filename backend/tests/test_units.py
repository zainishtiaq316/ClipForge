"""Unit tests for the pure logic: segmentation, reframing, URL validation, clip validation."""

import numpy as np
import pytest
from pydantic import ValidationError

from app.schemas import Clip, ClipsUpdate
from app.services import segmenter
from app.services.analyzer import Analysis, Sample, detect_scene_cuts
from app.services.downloader import normalize_youtube_url
from app.services.face_detector import Face
from app.services.reframer import CameraPath, build_camera_path, crop_fraction
from app.services.renderer import crop_width
from app.services.text_detector import TextBox
from app.services.text_layout import (
    TextLayout,
    TextLine,
    View,
    fit_line,
    fit_text_window,
    is_text_block,
    layout_ops,
    text_runs,
)

# --- segmentation -------------------------------------------------------------


def _covers(segments, duration):
    assert segments[0].start == 0
    assert segments[-1].end == pytest.approx(duration)
    for a, b in zip(segments, segments[1:]):
        assert a.end == pytest.approx(b.start), "clips must be contiguous"


def test_segment_covers_whole_video_without_candidates():
    segs = segmenter.segment(125, [], [], target=30)
    _covers(segs, 125)
    assert all(15 <= s.end - s.start <= 45 for s in segs)


def test_segment_prefers_pauses_over_scene_cuts():
    # A scene cut exactly at the target and a pause 3 s later: the pause wins.
    segs = segmenter.segment(90, scene_cuts=[30.0], pauses=[(32.8, 33.4)], target=30)
    assert segs[0].end == pytest.approx(33.1)
    assert segs[0].reason == "pause"


def test_segment_short_video_is_single_clip():
    segs = segmenter.segment(20, [5.0], [], target=30)
    assert [(s.start, s.end) for s in segs] == [(0.0, 20.0)]


def test_segment_absorbs_tiny_tail():
    segs = segmenter.segment(95, [], [], target=30)
    _covers(segs, 95)
    assert segs[-1].end - segs[-1].start >= 18


# --- scene detection ----------------------------------------------------------


def test_scene_cut_detects_spike_but_not_sustained_pan():
    times = [i / 5 for i in range(20)]
    spike = [2.0] * 20
    spike[10] = 60.0
    assert detect_scene_cuts(times, spike) == [pytest.approx(1.9)]
    pan = [20.0] * 20  # constant high change = camera motion, not a cut
    assert detect_scene_cuts(times, pan) == []


# --- reframing ----------------------------------------------------------------


def _analysis(xs, cuts=(), fps=5):
    samples = [
        Sample(t=i / fps, faces=[] if x is None else [Face(x, 0.4, 0.08, 0.14, 0.9)], motion_x=None, motion=0)
        for i, x in enumerate(xs)
    ]
    return Analysis(sample_fps=fps, duration=len(xs) / fps, samples=samples, scene_cuts=list(cuts))


def test_crop_fraction_for_16_9():
    assert crop_fraction(1920, 1080) == pytest.approx(0.3164, abs=1e-3)
    assert crop_fraction(1080, 1920) == 1.0


def test_camera_follows_off_centre_subject():
    path = build_camera_path(_analysis([0.2] * 50), 1920, 1080)
    assert np.allclose(path.xs, 0.2, atol=0.01)


def test_camera_is_stable_under_jitter():
    rng = np.random.default_rng(0)
    xs = list(0.5 + rng.normal(0, 0.01, 100))
    path = build_camera_path(_analysis(xs), 1920, 1080)
    assert np.ptp(path.xs) < 0.01, "dead zone should absorb detector jitter"


def test_camera_reanchors_instantly_at_scene_cut():
    xs = [0.25] * 25 + [0.75] * 25
    path = build_camera_path(_analysis(xs, cuts=[5.0]), 1920, 1080)
    assert path.at(4.95) == pytest.approx(0.25, abs=0.01)
    assert path.at(5.05) == pytest.approx(0.75, abs=0.01)


def test_camera_pans_smoothly_within_a_shot():
    xs = [0.3] * 25 + [0.7] * 50
    path = build_camera_path(_analysis(xs), 1920, 1080)
    steps = np.abs(np.diff(path.xs))
    assert steps.max() < 0.05, "no jumps inside a shot"
    assert path.at(14.0) == pytest.approx(0.7, abs=0.06), "eventually reaches the subject"


def test_camera_stays_inside_frame():
    path = build_camera_path(_analysis([0.01] * 30), 1920, 1080)
    half = path.crop_fraction / 2
    assert min(path.xs) >= half - 1e-9


def test_no_faces_falls_back_to_centre():
    path = build_camera_path(_analysis([None] * 30), 1920, 1080)
    assert np.allclose(path.xs, 0.5)


def test_sparse_motion_does_not_move_crop():
    # A title card with two noisy motion samples near the left edge must stay centred.
    analysis = _analysis([None] * 20)
    analysis.samples[3].motion_x, analysis.samples[3].motion = 0.1, 0.05
    analysis.samples[9].motion_x, analysis.samples[9].motion = 0.12, 0.05
    path = build_camera_path(analysis, 1920, 1080)
    assert np.allclose(path.xs, 0.5)


def _with_text(analysis, start, end, box, every=0.4):
    for s in analysis.samples:
        if round(s.t / every, 6) % 1 == 0:
            s.text = [box] if start <= s.t < end else []
    return analysis


CAPTION = TextBox(0.05, 0.85, 0.6, 0.06)  # a lower-third wider than the 9:16 window
SHIRT_PRINT = TextBox(0.45, 0.7, 0.12, 0.06)  # short, blocky text on clothing


def test_caption_run_is_found():
    analysis = _with_text(_analysis([0.5] * 100), 6.0, 12.0, CAPTION)
    runs = text_runs(analysis)
    assert len(runs) == 1
    start, end, t_ref = runs[0]
    assert start <= 6.0 and end >= 12.0, "the whole caption must be covered"
    assert start < t_ref < end


def test_small_text_near_a_face_is_ignored():
    analysis = _with_text(_analysis([0.5] * 100), 0.0, 20.0, SHIRT_PRINT)
    assert text_runs(analysis) == []


def test_flashing_text_is_ignored():
    analysis = _with_text(_analysis([0.5] * 100), 6.0, 6.8, CAPTION)
    assert text_runs(analysis) == []


def _line(x=0.05, y=0.85, w=0.6, h=0.06, cuts=(0.2, 0.4, 0.6, 0.8), boxed=True):
    return TextLine(x=x, y=y, w=w, h=h, cuts=cuts, boxed=boxed, bg=(0, 0, 0), ink=0.3)


def test_long_line_wraps_into_balanced_lines():
    line = _line(cuts=(0.1, 0.45, 0.55, 0.9))
    segments, scale = fit_line(line, 1152, 1.78, 972)
    assert len(segments) == 2, "two lines are enough, so no third line"
    assert max(b - a for a, b in segments) == pytest.approx(0.55), "wrap at the most even word gap"
    assert scale >= 0.85 * 1.78, "text keeps (almost) its natural size"

    much_longer = _line(w=0.95, cuts=(0.1, 0.3, 0.45, 0.55, 0.7, 0.9))
    segments, _ = fit_line(much_longer, 1824, 1.78, 972)
    assert len(segments) == 3


def test_short_line_stays_on_one_line():
    segments, scale = fit_line(_line(w=0.2), 384, 1.78, 972)
    assert segments == [(0.0, 1.0)] and scale == pytest.approx(1.78)


def test_line_without_word_gaps_is_scaled_to_fit():
    segments, scale = fit_line(_line(cuts=()), 1152, 1.78, 972)
    assert segments == [(0.0, 1.0)]
    assert 1152 * scale == pytest.approx(972)


def test_reflowed_text_fits_the_frame_and_covers_the_cut_original():
    view = View(1920, 1080, crop_x0=656, crop_w=608, out_w=1080, out_h=1920)
    (op,) = layout_ops((_line(),), view)
    assert op.erase is not None and op.plate is not None
    for paste in op.pastes:
        x, y, w, h = paste.dst
        assert 0 <= x and x + w <= 1080 and 0 <= y and y + h <= 1920
    assert len(op.pastes) >= 2, "a caption 2x wider than the crop is wrapped"


def test_text_fully_inside_the_crop_is_left_alone():
    view = View(1920, 1080, crop_x0=0, crop_w=608, out_w=1080, out_h=1920)
    assert layout_ops((_line(x=0.05, w=0.2),), view) == []


def test_zoomed_out_text_moves_onto_the_bar():
    crop_w = crop_width(1920, 1080, 0.6)
    view = View(1920, 1080, crop_x0=0, crop_w=crop_w, out_w=1080, out_h=1920)
    assert view.video_top > 0, "zooming out leaves room above and below"
    (op,) = layout_ops((_line(x=0.5, w=0.45),), view)
    assert min(p.dst[1] for p in op.pastes) >= view.video_top + view.video_h, "caption sits on the bottom bar"


def test_window_shifts_to_include_text_that_fits():
    layout = TextLayout(0.0, 10.0, (_line(x=0.30, w=0.25),))
    x0, fits = fit_text_window(800, 608, 1920, 1000.0, layout, t=5.0)
    assert fits and x0 <= 0.30 * 1920 and x0 + 608 >= 0.55 * 1920


def test_window_does_not_abandon_the_subject_for_a_caption():
    layout = TextLayout(0.0, 10.0, (_line(x=0.0, w=0.25),))
    x0, fits = fit_text_window(1300, 608, 1920, 1604.0, layout, t=5.0)
    assert not fits and x0 == 1300


def test_slides_become_the_subject():
    lines = (_line(x=0.3, y=0.2, w=0.3, h=0.08), _line(x=0.3, y=0.3, w=0.3, h=0.08))
    assert is_text_block(lines)
    layout = TextLayout(0.0, 10.0, lines, block=True)
    x0, fits = fit_text_window(0, 608, 1920, 304.0, layout, t=5.0)
    assert fits and x0 <= 0.3 * 1920


def test_camera_path_carries_text_layouts_and_round_trips():
    layout = TextLayout(6.0, 12.0, (_line(),))
    path = build_camera_path(_analysis([0.5] * 100), 1920, 1080, (layout,))
    again = CameraPath.from_dict(path.to_dict())
    assert again.text_layouts == path.text_layouts
    assert again.text_at(9.0) == layout and again.text_at(15.0) is None


def test_zoom_widens_the_window():
    assert crop_width(1920, 1080, 0.0) == 608
    assert crop_width(1920, 1080, 1.0) == 1920
    assert 608 < crop_width(1920, 1080, 0.5) < 1920


def test_consistent_motion_is_followed_without_faces():
    # e.g. a product demo: the moving element sits at x=0.7 for the whole shot.
    analysis = _analysis([None] * 30)
    for s in analysis.samples:
        s.motion_x, s.motion = 0.7, 0.05
    path = build_camera_path(analysis, 1920, 1080)
    assert np.allclose(path.xs, 0.7, atol=0.02)


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/watch?v=AYcqLymo6M4",
        "youtube.com/watch?v=AYcqLymo6M4&t=10s",
        "https://youtu.be/AYcqLymo6M4?si=x",
        "https://m.youtube.com/shorts/AYcqLymo6M4",
    ],
)
def test_youtube_url_normalised(raw):
    assert normalize_youtube_url(raw) == "https://www.youtube.com/watch?v=AYcqLymo6M4"


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example.com/watch?v=AYcqLymo6M4",
        "http://169.254.169.254/latest/meta-data",
        "https://youtube.com.evil.com/watch?v=AYcqLymo6M4",
        "https://www.youtube.com/playlist?list=PL123",
        "file:///etc/passwd",
        "",
    ],
)
def test_youtube_url_rejected(raw):
    with pytest.raises(ValueError):
        normalize_youtube_url(raw)


def test_clip_validation():
    with pytest.raises(ValidationError):
        Clip(start=10, end=10.5)  # shorter than 1s
    with pytest.raises(ValidationError):
        Clip(start=-1, end=5)
    with pytest.raises(ValidationError):
        Clip(id="../../etc", start=0, end=5)
    clip = Clip(start=0, end=5)
    with pytest.raises(ValidationError):
        ClipsUpdate(clips=[clip, clip])  # duplicate ids


def test_text_detector_finds_a_caption_line():
    from pathlib import Path

    import cv2

    from app.services.text_detector import TextDetector

    frame = np.full((360, 640, 3), 40, dtype=np.uint8)
    cv2.putText(frame, "Subscribe for more tips", (40, 320), cv2.FONT_HERSHEY_DUPLEX, 1.2, (255, 255, 255), 2)
    detector = TextDetector(Path(__file__).resolve().parent.parent / "models")
    assert detector.available
    boxes = detector.detect(frame)
    assert boxes, "caption not detected"
    widest = max(boxes, key=lambda b: b.w)
    assert widest.w > 0.5 and widest.y > 0.75, widest


def test_legacy_fit_framing_becomes_zoom():
    clip = Clip.model_validate({"start": 0, "end": 5, "framing": "fit"})
    assert clip.framing == "auto" and clip.zoom == 1.0
    with pytest.raises(ValidationError):
        Clip(start=0, end=5, zoom=1.5)


# --- whole-person framing and auto zoom ---------------------------------------------


def _with_person(analysis, box, every=0.4):
    for s in analysis.samples:
        if round(s.t / every, 6) % 1 == 0:
            s.persons = [box]
    return analysis


def test_person_with_arms_out_is_zoomed_to_fit():
    from app.services.person_detector import PersonBox

    # Face at x=0.5 in a medium shot; arms and a held object span 0.28..0.78 (wider than the 0.32 crop).
    analysis = _with_person(_analysis([0.5] * 50), PersonBox(0.28, 0.1, 0.5, 0.9, 0.9))
    path = build_camera_path(analysis, 1920, 1080)
    zoom = float(path.zoom_at(2.0))
    visible = zoomed_width = 0.3164 + zoom * (1 - 0.3164)
    assert zoomed_width >= 0.5 + 2 * 0.03 - 1e-6, "the whole person plus margin fits"
    centre = float(path.at(2.0))
    assert centre - visible / 2 <= 0.28 and centre + visible / 2 >= 0.78


def test_close_up_is_not_zoomed_out():
    from app.services.person_detector import PersonBox

    analysis = _analysis([0.5] * 50)
    for s in analysis.samples:
        s.faces = [Face(0.5, 0.35, 0.2, 0.35, 0.9)]  # big face = close-up
    _with_person(analysis, PersonBox(0.15, 0.0, 0.7, 1.0, 0.9))  # shoulders fill the frame
    assert float(build_camera_path(analysis, 1920, 1080).zoom_at(2.0)) == 0.0


def test_title_card_without_faces_zooms_to_show_all_text():
    analysis = _with_text(_analysis([None] * 30), 0.0, 6.0, TextBox(0.1, 0.4, 0.7, 0.1))
    path = build_camera_path(analysis, 1920, 1080)
    visible = 0.3164 + float(path.zoom_at(3.0)) * (1 - 0.3164)
    assert visible >= 0.7 + 2 * 0.03 - 1e-6


def test_zoom_is_constant_within_a_shot():
    from app.services.person_detector import PersonBox

    analysis = _with_person(_analysis([0.5] * 50), PersonBox(0.28, 0.1, 0.5, 0.9, 0.9))
    path = build_camera_path(analysis, 1920, 1080)
    assert len(set(path.zs)) == 1, "no zoom breathing inside a shot"


# --- word gaps ----------------------------------------------------------------


def _mask_with_gaps(gap_widths, letter=20, height=30):
    columns = [np.ones((height, letter), np.uint8)]
    for g in gap_widths:
        columns += [np.zeros((height, g), np.uint8), np.ones((height, letter), np.uint8)]
    return np.hstack(columns)


def test_one_word_in_a_wide_font_is_never_split():
    from app.services.text_layout import _word_gaps

    assert _word_gaps(_mask_with_gaps([6, 7, 6, 6, 7, 6]), letter_h=30) == []


def test_word_spaces_are_found_between_letter_spacing():
    from app.services.text_layout import _word_gaps

    cuts = _word_gaps(_mask_with_gaps([2, 2, 9, 2, 2, 9, 2]), letter_h=30)
    assert len(cuts) == 2


def test_medium_close_up_shoulders_do_not_trigger_zoom():
    from app.services.person_detector import PersonBox

    analysis = _analysis([0.5] * 50)
    for s in analysis.samples:
        s.faces = [Face(0.55, 0.25, 0.096, 0.17, 0.9)]  # a talking head just under the close-up size
    _with_person(analysis, PersonBox(0.37, 0.1, 0.38, 0.9, 0.9))  # box = shoulders (~4 face widths)
    assert float(build_camera_path(analysis, 1920, 1080).zoom_at(2.0)) == 0.0


def test_name_caption_in_a_wide_shot_is_kept_by_zooming_not_reflowed():
    from app.services.person_detector import PersonBox

    analysis = _with_person(_analysis([0.5] * 50), PersonBox(0.44, 0.1, 0.12, 0.85, 0.9))  # full-body shot
    for s in analysis.samples:
        s.faces = [Face(0.5, 0.2, 0.03, 0.05, 0.9)]
    _with_text(analysis, 0.0, 10.0, TextBox(0.31, 0.8, 0.38, 0.07))  # "Name Surname" lower third
    path = build_camera_path(analysis, 1920, 1080)
    visible = 0.3164 + float(path.zoom_at(3.0)) * (1 - 0.3164)
    assert visible >= 0.38, "wide enough to show the caption untouched"
