"""Unit tests for the pure logic: segmentation, reframing, URL validation, clip validation."""

import numpy as np
import pytest
from pydantic import ValidationError

from app.schemas import Clip, ClipsUpdate
from app.services import segmenter
from app.services.analyzer import Analysis, Sample, detect_scene_cuts
from app.services.downloader import normalize_youtube_url
from app.services.face_detector import Face
from app.services.reframer import CameraPath, build_camera_path, build_fit_ranges, crop_fraction
from app.services.text_detector import TextBox

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


def test_wide_caption_switches_to_fit_layout():
    analysis = _with_text(_analysis([0.5] * 100), 6.0, 12.0, CAPTION)
    ranges = build_fit_ranges(analysis, crop_fraction(1920, 1080))
    assert len(ranges) == 1
    start, end = ranges[0]
    assert start <= 6.0 and end >= 12.0, "the whole caption must be covered"
    assert end - start < 8.0


def test_small_text_near_a_face_is_ignored():
    analysis = _with_text(_analysis([0.5] * 100), 0.0, 20.0, SHIRT_PRINT)
    assert build_fit_ranges(analysis, crop_fraction(1920, 1080)) == []


def test_flashing_text_is_ignored():
    analysis = _with_text(_analysis([0.5] * 100), 6.0, 6.5, CAPTION)
    assert build_fit_ranges(analysis, crop_fraction(1920, 1080)) == []


def test_fit_range_expands_to_scene_cut_but_never_shrinks():
    analysis = _with_text(_analysis([0.5] * 100, cuts=[5.6, 12.6]), 6.0, 12.0, CAPTION)
    (start, end), = build_fit_ranges(analysis, crop_fraction(1920, 1080))
    assert start == pytest.approx(5.6) and end == pytest.approx(12.6)


def test_camera_path_carries_fit_ranges_and_round_trips():
    analysis = _with_text(_analysis([0.5] * 100), 6.0, 12.0, CAPTION)
    path = build_camera_path(analysis, 1920, 1080)
    assert path.fit_ranges
    again = CameraPath.from_dict(path.to_dict())
    assert again.fit_ranges == path.fit_ranges
    assert again.fit_mask(np.array([9.0, 15.0])).tolist() == [True, False]


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
