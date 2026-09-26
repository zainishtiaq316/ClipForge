"""Turns raw detections into a smooth virtual-camera path for the 9:16 crop.

Pipeline (per shot, where a shot is the span between two scene cuts):

1. **Subject selection**: pick the face to follow in each sample. Bigger and
   more confident faces win, but the face nearest the current subject gets a
   continuity bonus (hysteresis), so the camera doesn't ping-pong between two
   people. If every face fits inside the crop at once, frame the whole group.
2. **Gap filling**: short detection dropouts (head turns, blinks of the
   detector) are interpolated. Shots with no face at all fall back to the
   motion centroid (the "key element"), then to the centre.
3. **Denoising**: a median filter removes single-sample outliers, then a
   zero-phase Gaussian smooths the target. Zero-phase means the camera starts
   moving slightly *before* the subject does, which looks like a human operator.
4. **Virtual camera**: a follower with a dead zone (small moves are ignored),
   proportional speed and velocity/acceleration clamps. At a scene cut the
   camera re-anchors instantly instead of panning across the edit.

5. **Text-aware layout**: when on-screen text (a caption, title overlay, slide)
   is wider than the 9:16 window, or a shot shows text but no face, those
   seconds switch to a *fit* layout (whole frame over a blurred background)
   so the text is never cut in half. See ``build_fit_ranges``.

The output is a list of ``(time, centre_x)`` keyframes in normalised source
coordinates plus the time ranges that use the fit layout. The same data drives
both the browser preview and the final render, so what the user sees is what
they export.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .analyzer import Analysis, Sample
from .face_detector import Face
from .text_detector import TextBox

PATH_FPS = 10  # resolution of the stored camera path

# Subject selection
MIN_FACE_WIDTH = 0.012  # ignore faces narrower than 1.2% of the frame (crowds, posters)
CONTINUITY_RADIUS = 0.08
CONTINUITY_BONUS = 2.0

# Fallback when a shot has no faces
MIN_MOTION = 0.01  # at least 1% of pixels changed
MIN_MOTION_COVERAGE = 0.5  # in at least half of the shot's samples

# Smoothing
MAX_GAP_S = 2.5  # longer gaps than this hold the last position instead of interpolating
MEDIAN_WINDOW_S = 1.0
GAUSSIAN_SIGMA_S = 0.45

# Text-aware layout
TEXT_LINE_MIN_ASPECT = 3.5  # a line of text is much wider than tall (ignores logos, T-shirt prints)
TEXT_LINE_MIN_HEIGHT = 0.025  # ignore tiny text (watermarks, background signs)
TEXT_BLOCK_MIN_AREA = 0.004  # enough text to be the point of a face-less shot
FIT_MIN_S = 1.0  # ignore text that flashes by
FIT_MERGE_GAP_S = 1.5  # bridge short gaps so the layout doesn't flicker
FIT_PAD_S = 0.3
FIT_SNAP_S = 0.6  # snap layout switches onto nearby scene cuts
FIT_SHOT_MAJORITY = 0.6  # if most of a shot needs fit, use fit for the whole shot

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
    fit_ranges: tuple[tuple[float, float], ...] = ()  # time ranges shown with the fit layout
    scene_cuts: tuple[float, ...] = ()  # layout switches on a cut are hard cuts, not crossfades

    def at(self, t: np.ndarray | float) -> np.ndarray:
        return np.interp(t, self.times, self.xs)

    def fit_mask(self, t: np.ndarray) -> np.ndarray:
        mask = np.zeros(np.shape(t), dtype=bool)
        for start, end in self.fit_ranges:
            mask |= (t >= start) & (t < end)
        return mask

    def to_dict(self) -> dict:
        return {
            "crop_fraction": round(self.crop_fraction, 5),
            "times": [round(t, 3) for t in self.times],
            "xs": [round(x, 4) for x in self.xs],
            "fit_ranges": [[round(a, 3), round(b, 3)] for a, b in self.fit_ranges],
            "scene_cuts": [round(c, 3) for c in self.scene_cuts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CameraPath:
        return cls(
            times=data["times"],
            xs=data["xs"],
            crop_fraction=data["crop_fraction"],
            fit_ranges=tuple(tuple(r) for r in data.get("fit_ranges", [])),
            scene_cuts=tuple(data.get("scene_cuts", [])),
        )


def crop_fraction(width: int, height: int) -> float:
    """Width of a full-height 9:16 window relative to the source width (capped at 1)."""
    return min(1.0, (height * 9 / 16) / width)


def _select_subject(faces: list[Face], previous: float | None, crop_frac: float) -> float | None:
    faces = [f for f in faces if f.w >= MIN_FACE_WIDTH]
    if not faces:
        return None

    # Frame the group when everyone fits comfortably (e.g. a two-shot interview).
    if len(faces) > 1:
        left = min(f.cx - f.w / 2 for f in faces)
        right = max(f.cx + f.w / 2 for f in faces)
        if right - left <= crop_frac * 0.85:
            return (left + right) / 2

    def weight(face: Face) -> float:
        w = face.area * face.score
        if previous is not None:
            w *= 1 + CONTINUITY_BONUS * np.exp(-abs(face.cx - previous) / CONTINUITY_RADIUS)
        return w

    return max(faces, key=weight).cx


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


def _shot_target(samples: list[Sample], crop_frac: float, sample_fps: float) -> np.ndarray:
    """Smoothed subject x for every sample of one shot."""
    times = np.array([s.t for s in samples])
    raw = np.full(len(samples), np.nan)
    previous: float | None = None
    for i, sample in enumerate(samples):
        x = _select_subject(sample.faces, previous, crop_frac)
        if x is not None:
            raw[i] = previous = x

    face_coverage = np.count_nonzero(~np.isnan(raw)) / len(raw)
    if face_coverage == 0:
        # No faces: follow the main moving element, but only if it moves in most
        # of the shot. A few noisy samples (fades, animated logos, compression
        # flicker) would otherwise shove the crop to the edge of a title card.
        motion = np.array(
            [s.motion_x if s.motion_x is not None and s.motion >= MIN_MOTION else np.nan for s in samples]
        )
        if np.count_nonzero(~np.isnan(motion)) / len(motion) >= MIN_MOTION_COVERAGE:
            raw = motion
        else:
            return np.full(len(samples), 0.5)

    filled = _fill_gaps(times, raw)
    filled = _median_filter(filled, max(3, int(MEDIAN_WINDOW_S * sample_fps) | 1))
    return _gaussian(filled, GAUSSIAN_SIGMA_S * sample_fps)


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


def _needs_fit(text: list[TextBox], faces: list[Face], crop_frac: float) -> bool:
    """Would a 9:16 crop cut important text in this frame?"""
    lines = [b for b in text if b.h >= TEXT_LINE_MIN_HEIGHT and b.w / max(b.h, 1e-6) >= TEXT_LINE_MIN_ASPECT]
    if any(line.w > crop_frac for line in lines):
        return True  # a caption / title line wider than the crop can never fit
    has_face = any(f.w >= MIN_FACE_WIDTH for f in faces)
    text_area = sum(b.w * b.h for b in text if b.h >= TEXT_LINE_MIN_HEIGHT)
    if not has_face and text_area >= TEXT_BLOCK_MIN_AREA:
        left = min(b.x for b in text)
        right = max(b.x + b.w for b in text)
        return right - left > crop_frac * 0.9  # a slide / title card that doesn't fit in the crop
    return False


def _runs(times: list[float], flags: list[bool], step: float) -> list[list[float]]:
    runs: list[list[float]] = []
    for t, flag in zip(times, flags):
        if not flag:
            continue
        if runs and t - runs[-1][1] <= step * 1.5:
            runs[-1][1] = t + step
        else:
            runs.append([t, t + step])
    return runs


def build_fit_ranges(analysis: Analysis, crop_frac: float) -> list[tuple[float, float]]:
    """Time ranges where on-screen text needs the whole frame (fit layout)."""
    checked = [s for s in analysis.samples if s.text is not None]
    if len(checked) < 2 or crop_frac >= 1.0:
        return []
    step = checked[1].t - checked[0].t
    runs = _runs([s.t for s in checked], [_needs_fit(s.text or [], s.faces, crop_frac) for s in checked], step)

    # Merge near-by runs, drop flashes, pad a little.
    merged: list[list[float]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= FIT_MERGE_GAP_S:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    ranges = [[a - FIT_PAD_S, b + FIT_PAD_S] for a, b in merged if b - a >= FIT_MIN_S]

    # Layout switches look intentional when they happen on an edit. Only ever
    # snap outwards: shrinking the range would cut text that is already visible.
    cuts = analysis.scene_cuts
    for r in ranges:
        before = [c for c in cuts if r[0] - FIT_SNAP_S <= c <= r[0]]
        after = [c for c in cuts if r[1] <= c <= r[1] + FIT_SNAP_S]
        if before:
            r[0] = max(before)
        if after:
            r[1] = min(after)

    # A shot that is mostly text gets the fit layout for its whole length.
    bounds = [0.0, *cuts, analysis.duration]
    for a, b in zip(bounds[:-1], bounds[1:]):
        covered = sum(max(0.0, min(b, e) - max(a, s)) for s, e in ranges)
        if b > a and covered / (b - a) >= FIT_SHOT_MAJORITY:
            ranges.append([a, b])

    ranges.sort()
    out: list[list[float]] = []
    for s, e in ranges:
        s, e = max(0.0, s), min(analysis.duration, e)
        if out and s <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], e)
        elif e > s:
            out.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in out]


def build_camera_path(analysis: Analysis, width: int, height: int) -> CameraPath:
    frac = crop_fraction(width, height)
    half = frac / 2
    duration = analysis.duration
    if frac >= 1.0 or not analysis.samples:
        return CameraPath(times=[0.0, duration], xs=[0.5, 0.5], crop_fraction=frac)
    fit_ranges = tuple(build_fit_ranges(analysis, frac))

    bounds = [0.0, *[c for c in analysis.scene_cuts if 0 < c < duration], duration]
    times_out: list[float] = []
    xs_out: list[float] = []
    dt = 1 / PATH_FPS

    for start, end in zip(bounds[:-1], bounds[1:]):
        shot = [s for s in analysis.samples if start <= s.t < end]
        if not shot:
            continue
        target = _shot_target(shot, frac, analysis.sample_fps)
        grid = np.arange(start, end, dt)
        if grid.size == 0:
            grid = np.array([start])
        upsampled = np.interp(grid, [s.t for s in shot], target)
        cam = np.clip(_follow(upsampled, dt, frac), half, 1 - half)

        # Duplicate keyframes at the cut so interpolation jumps instead of panning.
        times_out.extend(grid.tolist())
        xs_out.extend(cam.tolist())
        times_out.append(max(end - 1e-3, grid[-1]))
        xs_out.append(float(cam[-1]))

    return CameraPath(
        times=times_out, xs=xs_out, crop_fraction=frac, fit_ranges=fit_ranges, scene_cuts=tuple(analysis.scene_cuts)
    )
