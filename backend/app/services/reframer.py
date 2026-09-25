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

The output is a list of ``(time, centre_x)`` keyframes in normalised source
coordinates. The same path drives both the browser preview and the final
render, so what the user sees is what they export.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .analyzer import Analysis, Sample
from .face_detector import Face

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

    def at(self, t: np.ndarray | float) -> np.ndarray:
        return np.interp(t, self.times, self.xs)

    def to_dict(self) -> dict:
        return {
            "crop_fraction": round(self.crop_fraction, 5),
            "times": [round(t, 3) for t in self.times],
            "xs": [round(x, 4) for x in self.xs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CameraPath:
        return cls(times=data["times"], xs=data["xs"], crop_fraction=data["crop_fraction"])


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


def build_camera_path(analysis: Analysis, width: int, height: int) -> CameraPath:
    frac = crop_fraction(width, height)
    half = frac / 2
    duration = analysis.duration
    if frac >= 1.0 or not analysis.samples:
        return CameraPath(times=[0.0, duration], xs=[0.5, 0.5], crop_fraction=frac)

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

    return CameraPath(times=times_out, xs=xs_out, crop_fraction=frac)
