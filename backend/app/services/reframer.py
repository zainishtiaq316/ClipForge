"""Turns raw detections into a smooth virtual-camera path for the 9:16 crop.

Pipeline (per shot, where a shot is the span between two scene cuts):

1. **Subject selection**: pick the face to follow in each sample. Bigger and
   more confident faces win, but the face nearest the current subject gets a
   continuity bonus (hysteresis), so the camera doesn't ping-pong between two
   people. If every face fits inside the crop at once, frame the whole group.
2. **Whole-person framing**: in medium and wide shots the camera frames the
   *person* (NanoDet body box, arms and hands included), not just the face. If
   the person is wider than a full-height 9:16 window (arms out, an object held at
   arm's length), the shot is **zoomed out** just enough, with solid bars. The
   zoom is constant per shot so it never "breathes". Close-ups stay face-centred
   (cutting the shoulders is normal in vertical video). Shots with no face but
   with on-screen text (title cards, end cards) zoom to fit the text.
3. **Gap filling**: short detection dropouts are interpolated. Shots with no face
   and no text fall back to the motion centroid, then to the centre.
4. **Denoising**: a median filter removes single-sample outliers, then a
   zero-phase Gaussian smooths the target. Zero-phase means the camera starts
   moving slightly *before* the subject does, which looks like a human operator.
5. **Virtual camera**: a follower with a dead zone (small moves are ignored),
   proportional speed and velocity/acceleration clamps. At a scene cut the
   camera re-anchors instantly instead of panning across the edit.

On-screen text that still doesn't fit is re-flowed into the frame at render time
(see ``text_layout``); the text layouts are stored with the camera path.

The output is a list of ``(time, centre_x, zoom)`` keyframes in normalised source
coordinates. The same data drives both the browser preview and the final render,
so what the user sees is what they export.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .analyzer import Analysis, Sample
from .face_detector import Face
from .text_layout import TextLayout, overlay_lines

PATH_FPS = 10  # resolution of the stored camera path

# Subject selection
MIN_FACE_WIDTH = 0.012  # ignore faces narrower than 1.2% of the frame (crowds, posters)
CONTINUITY_RADIUS = 0.08
CONTINUITY_BONUS = 2.0

# Whole-person framing and auto zoom
CLOSE_UP_FACE_WIDTH = 0.11  # a face wider than this is a close-up: frame the face, not the body
ARMS_OUT_RATIO = 4.5  # shoulders are ~3-4 face widths; a wider body box means arms / a held object stick out
SHOULDERS_HEIGHT_RATIO = 6.0  # a body box under ~6 face heights is head-and-shoulders, not a full body
PERSON_MARGIN = 0.03  # room left of and right of the person (fraction of frame width)
TEXT_MARGIN = 0.03
CARD_TEXT_MIN_HEIGHT = 0.015  # on a title / end card, even small print (credits, licence) counts
FACE_MARGIN_OF_CROP = 0.12  # the face always keeps this much room to the crop edge
ZOOM_PERCENTILE = 80  # zoom for the width the person needs most of the time, not for one wild gesture
TEXT_ZOOM_PERCENTILE = 100  # a title card must be shown whole
MAX_ZOOM_WITH_PERSON = 0.45  # never shrink a person more than this (0 = full-frame, 1 = whole width)
MAX_ZOOM_TEXT_ONLY = 1.0
MIN_ZOOM_STEP = 0.05  # quantise so neighbouring shots match
MIN_USEFUL_ZOOM = 0.08  # smaller zooms would only add thin bars
DETECTION_REACH_S = 0.5  # person/text detections run every 0.4 s; reuse them for nearby samples

# Fallback when a shot has no faces
MIN_MOTION = 0.01  # at least 1% of pixels changed
MIN_MOTION_COVERAGE = 0.5  # in at least half of the shot's samples

# Smoothing
MAX_GAP_S = 2.5  # longer gaps than this hold the last position instead of interpolating
MEDIAN_WINDOW_S = 1.0
GAUSSIAN_SIGMA_S = 0.45

# Virtual camera (units: fraction of source width)
DEAD_ZONE_OF_CROP = 0.12  # subject may drift 12% of the crop width before we move
FOLLOW_GAIN = 2.5  # 1/s, how quickly the camera closes the gap
MAX_SPEED = 0.30  # per second
MAX_ACCEL = 0.9  # per second^2


@dataclass(frozen=True)
class CameraPath:
    times: list[float]
    xs: list[float]  # crop centre, normalised to source width
    crop_fraction: float
    text_layouts: tuple[TextLayout, ...] = ()  # overlay text to re-flow into the vertical frame
    zs: list[float] | None = None  # automatic zoom-out per keyframe (0 = full-frame 9:16)

    def at(self, t: np.ndarray | float) -> np.ndarray:
        return np.interp(t, self.times, self.xs)

    def zoom_at(self, t: np.ndarray | float) -> np.ndarray:
        if not self.zs:
            return np.zeros(np.shape(t)) if np.ndim(t) else np.float64(0.0)
        return np.interp(t, self.times, self.zs)

    def text_at(self, t: float) -> TextLayout | None:
        return next((lay for lay in self.text_layouts if lay.start <= t < lay.end), None)

    def to_dict(self) -> dict:
        return {
            "crop_fraction": round(self.crop_fraction, 5),
            "times": [round(t, 3) for t in self.times],
            "xs": [round(x, 4) for x in self.xs],
            "zs": [round(z, 3) for z in (self.zs or [0.0] * len(self.times))],
            "text_layouts": [lay.to_dict() for lay in self.text_layouts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CameraPath:
        return cls(
            times=data["times"],
            xs=data["xs"],
            crop_fraction=data["crop_fraction"],
            text_layouts=tuple(TextLayout.from_dict(d) for d in data.get("text_layouts", [])),
            zs=data.get("zs"),
        )


def crop_fraction(width: int, height: int) -> float:
    """Width of a full-height 9:16 window relative to the source width (capped at 1)."""
    return min(1.0, (height * 9 / 16) / width)


def zoomed_fraction(base: float, zoom: float) -> float:
    return base + zoom * (1 - base)


def _select_subject(faces: list[Face], previous: float | None, crop_frac: float) -> Face | None:
    """The face to follow, or a synthetic 'face' spanning a group that fits the crop."""
    faces = [f for f in faces if f.w >= MIN_FACE_WIDTH]
    if not faces:
        return None

    # Frame the group when everyone fits comfortably (e.g. a two-shot interview).
    if len(faces) > 1:
        left = min(f.cx - f.w / 2 for f in faces)
        right = max(f.cx + f.w / 2 for f in faces)
        if right - left <= crop_frac * 0.85:
            biggest = max(faces, key=lambda f: f.w)
            return Face((left + right) / 2, biggest.cy, right - left, biggest.h, biggest.score)

    def weight(face: Face) -> float:
        w = face.area * face.score
        if previous is not None:
            w *= 1 + CONTINUITY_BONUS * np.exp(-abs(face.cx - previous) / CONTINUITY_RADIUS)
        return w

    return max(faces, key=weight)


def _nearest(samples: list[Sample], i: int, attr: str):
    """The closest sample (in time) that ran the detector behind ``attr``."""
    best, best_dt = None, DETECTION_REACH_S
    for j in range(max(0, i - 4), min(len(samples), i + 5)):
        value = getattr(samples[j], attr)
        dt = abs(samples[j].t - samples[i].t)
        if value is not None and dt <= best_dt:
            best, best_dt = value, dt
    return best


def _person_span(face: Face, sample_persons) -> tuple[float, float] | None:
    """Horizontal extent of the body that belongs to ``face`` (arms and hands included)."""
    if face.w >= CLOSE_UP_FACE_WIDTH or not sample_persons:
        return None
    owners = [p for p in sample_persons if p.x <= face.cx <= p.x + p.w and p.y <= face.cy <= p.y + p.h]
    if not owners:
        return None
    body = max(owners, key=lambda p: p.w * p.h)
    # A medium close-up's box is just the shoulders; cutting them is normal in vertical video.
    # Only when arms or a held object stick out (or the whole body is small) is it worth framing.
    shoulders_only = body.h < SHOULDERS_HEIGHT_RATIO * face.h  # the box stops at the chest, not the feet
    if shoulders_only and body.w <= ARMS_OUT_RATIO * face.w:
        return None
    return max(0.0, body.x - PERSON_MARGIN), min(1.0, body.x + body.w + PERSON_MARGIN)


def _text_span(text, everything: bool = False) -> tuple[float, float] | None:
    """Horizontal extent of the overlay lines, or of all text (small print too) on a card."""
    lines = [b for b in text or [] if b.h >= CARD_TEXT_MIN_HEIGHT] if everything else overlay_lines(text or [])
    if not lines:
        return None
    return max(0.0, min(b.x for b in lines) - TEXT_MARGIN), min(1.0, max(b.x + b.w for b in lines) + TEXT_MARGIN)


def _quantised_zoom(width: float, base: float, cap: float) -> float:
    """Smallest zoom step that fits ``width`` (rounded up, so nothing is cut)."""
    if width <= base:
        return 0.0
    zoom = min(cap, (width - base) / (1 - base))
    if zoom < MIN_USEFUL_ZOOM:
        return 0.0  # a sliver of bars isn't worth it; the camera position handles small overhangs
    zoom = np.ceil(zoom / MIN_ZOOM_STEP - 1e-6) * MIN_ZOOM_STEP
    return float(min(cap, zoom))


def _fill_gaps(times: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Interpolate short NaN gaps, hold values across long gaps and at the edges."""
    known = ~np.isnan(values)
    if not known.any():
        return values
    idx = np.flatnonzero(known)
    filled = np.interp(times, times[idx], values[idx])  # holds edges automatically
    # For long gaps, hold the previous value instead of a slow cross-frame drift.
    for a, b in zip(idx[:-1], idx[1:]):
        if times[b] - times[a] > MAX_GAP_S:
            filled[a + 1 : b] = values[a]
    return filled


def _median_filter(values: np.ndarray, window: int) -> np.ndarray:
    if window < 3 or values.size < 3:
        return values
    half = window // 2
    padded = np.pad(values, half, mode="edge")
    stacked = np.lib.stride_tricks.sliding_window_view(padded, window)
    return np.median(stacked, axis=1)


def _gaussian(values: np.ndarray, sigma_samples: float) -> np.ndarray:
    if sigma_samples <= 0 or values.size < 3:
        return values
    radius = max(1, int(3 * sigma_samples))
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma_samples) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(values, radius, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def _shot_plan(samples: list[Sample], base: float, sample_fps: float) -> tuple[np.ndarray, float]:
    """Smoothed subject x for every sample of one shot, and the shot's zoom."""
    times = np.array([s.t for s in samples])
    faces: list[Face | None] = []
    previous: float | None = None
    for sample in samples:
        face = _select_subject(sample.faces, previous, base)
        faces.append(face)
        if face is not None:
            previous = face.cx

    raw = np.full(len(samples), np.nan)
    zoom = 0.0
    if any(f is not None for f in faces):
        # People: frame the whole body when it's a medium / wide shot.
        spans = [_person_span(f, _nearest(samples, i, "persons")) if f else None for i, f in enumerate(faces)]
        widths = [b - a for span in spans if span for a, b in [span]]
        if len(widths) >= max(2, 0.3 * sum(f is not None for f in faces)):
            zoom = _quantised_zoom(float(np.percentile(widths, ZOOM_PERCENTILE)), base, MAX_ZOOM_WITH_PERSON)
            # A caption beside a person in a wide shot (a name, a place): if widening a bit more
            # shows it untouched, that beats erasing and re-flowing it over the person's body.
            together = []
            for i, span in enumerate(spans):
                text = _text_span(_nearest(samples, i, "text")) if span else None
                if text:
                    together.append(max(span[1], text[1]) - min(span[0], text[0]))
            if len(together) >= 2 and max(together) <= zoomed_fraction(base, MAX_ZOOM_WITH_PERSON):
                zoom = max(zoom, _quantised_zoom(max(together), base, MAX_ZOOM_WITH_PERSON))
        crop = zoomed_fraction(base, zoom)
        for i, (face, span) in enumerate(zip(faces, spans)):
            if face is None:
                continue
            x = face.cx if span is None else (span[0] + span[1]) / 2
            # Whatever else we frame, the face keeps a comfortable margin inside the crop.
            room = max(0.0, crop / 2 - face.w / 2 - FACE_MARGIN_OF_CROP * crop)
            raw[i] = float(np.clip(x, face.cx - room, face.cx + room))
    else:
        # No face: title cards and end screens are about their text.
        texts = [_nearest(samples, i, "text") for i in range(len(samples))]
        overlay = [_text_span(t) for t in texts]
        # It's a card when overlay text is on screen for a good part of the shot; then its small
        # print counts too (credits, a licence line). Signs in B-roll alone never trigger this.
        spans = [_text_span(t, everything=True) if o else None for t, o in zip(texts, overlay)]
        text_spans = [s for s in spans if s]
        if text_spans and len(text_spans) >= 0.3 * len(samples):
            widths = [b - a for a, b in text_spans]
            zoom = _quantised_zoom(float(np.percentile(widths, TEXT_ZOOM_PERCENTILE)), base, MAX_ZOOM_TEXT_ONLY)
            for i, span in enumerate(spans):
                if span:
                    raw[i] = (span[0] + span[1]) / 2
        else:
            # Otherwise follow the main moving element, but only if it moves in most of
            # the shot. A few noisy samples (fades, animated logos, compression flicker)
            # would shove the crop to the edge of the frame.
            motion = np.array(
                [s.motion_x if s.motion_x is not None and s.motion >= MIN_MOTION else np.nan for s in samples]
            )
            if np.count_nonzero(~np.isnan(motion)) / len(motion) >= MIN_MOTION_COVERAGE:
                raw = motion
            else:
                return np.full(len(samples), 0.5), 0.0

    filled = _fill_gaps(times, raw)
    filled = _median_filter(filled, max(3, int(MEDIAN_WINDOW_S * sample_fps) | 1))
    return _gaussian(filled, GAUSSIAN_SIGMA_S * sample_fps), zoom


def _follow(target: np.ndarray, dt: float, crop_frac: float) -> np.ndarray:
    """Virtual camera operator: dead zone + proportional follow + speed/accel limits."""
    dead_zone = DEAD_ZONE_OF_CROP * crop_frac
    head = target[: max(1, int(1 / dt))]
    cam = float(np.median(head))  # start framed on where the subject spends the first second
    velocity = 0.0
    out = np.empty_like(target)
    for i, goal in enumerate(target):
        error = goal - cam
        if abs(error) > dead_zone:
            desired = FOLLOW_GAIN * (error - np.sign(error) * dead_zone * 0.5)
        else:
            desired = 0.0
        desired = float(np.clip(desired, -MAX_SPEED, MAX_SPEED))
        velocity += float(np.clip(desired - velocity, -MAX_ACCEL * dt, MAX_ACCEL * dt))
        cam += velocity * dt
        out[i] = cam
    return out


def build_camera_path(
    analysis: Analysis, width: int, height: int, text_layouts: tuple[TextLayout, ...] = ()
) -> CameraPath:
    base = crop_fraction(width, height)
    duration = analysis.duration
    if base >= 1.0 or not analysis.samples:
        return CameraPath(times=[0.0, duration], xs=[0.5, 0.5], crop_fraction=base, text_layouts=text_layouts,
                          zs=[0.0, 0.0])

    bounds = [0.0, *[c for c in analysis.scene_cuts if 0 < c < duration], duration]
    times_out: list[float] = []
    xs_out: list[float] = []
    zs_out: list[float] = []
    dt = 1 / PATH_FPS

    for start, end in zip(bounds[:-1], bounds[1:]):
        shot = [s for s in analysis.samples if start <= s.t < end]
        if not shot:
            continue
        target, zoom = _shot_plan(shot, base, analysis.sample_fps)
        crop = zoomed_fraction(base, zoom)
        half = crop / 2
        grid = np.arange(start, end, dt)
        if grid.size == 0:
            grid = np.array([start])
        upsampled = np.interp(grid, [s.t for s in shot], target)
        cam = np.clip(_follow(upsampled, dt, crop), half, 1 - half)

        # Duplicate keyframes at the cut so interpolation jumps instead of panning.
        times_out.extend(grid.tolist())
        xs_out.extend(cam.tolist())
        zs_out.extend([zoom] * grid.size)
        times_out.append(max(end - 1e-3, grid[-1]))
        xs_out.append(float(cam[-1]))
        zs_out.append(zoom)

    return CameraPath(times=times_out, xs=xs_out, crop_fraction=base, text_layouts=text_layouts, zs=zs_out)
