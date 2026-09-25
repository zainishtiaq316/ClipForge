"""Single-pass video analysis.

FFmpeg decodes the source once at a low frame rate and resolution
(default 5 fps, 640 px wide) and pipes raw frames to Python. For every sampled
frame we collect:

* **faces** (YuNet): the primary subject signal for reframing;
* **scene-change score**: mean HSV difference to the previous sample, the same
  signal PySceneDetect's ContentDetector uses, with an adaptive threshold;
* **motion centroid**: where pixels changed, used as the "key element"
  fallback for shots with no visible face (products, screen recordings,
  B-roll).

Decoding at 5 fps x 640 px makes a 20 minute 1080p video take roughly a minute
instead of decoding all ~36 000 full-resolution frames.

A second, audio-only pass finds natural pauses in speech, which make much
better clip boundaries than arbitrary timestamps.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import ffmpeg
from .face_detector import Face, FaceDetector

log = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]


@dataclass
class Sample:
    t: float
    faces: list[Face]
    motion_x: float | None  # normalised x of motion centroid, None if static
    motion: float  # fraction of pixels that changed


@dataclass
class Analysis:
    sample_fps: float
    duration: float
    samples: list[Sample] = field(default_factory=list)
    scene_cuts: list[float] = field(default_factory=list)
    pauses: list[tuple[float, float]] = field(default_factory=list)
    detector: str = ""

    def to_dict(self) -> dict:
        return {
            "sample_fps": self.sample_fps,
            "duration": self.duration,
            "detector": self.detector,
            "scene_cuts": self.scene_cuts,
            "pauses": [list(s) for s in self.pauses],
            "samples": [
                {
                    "t": s.t,
                    "m": s.motion,
                    "mx": s.motion_x,
                    "f": [[round(v, 4) for v in asdict(f).values()] for f in s.faces],
                }
                for s in self.samples
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Analysis:
        return cls(
            sample_fps=data["sample_fps"],
            duration=data["duration"],
            detector=data.get("detector", ""),
            scene_cuts=list(data["scene_cuts"]),
            pauses=[tuple(s) for s in data["pauses"]],
            samples=[
                Sample(t=s["t"], motion=s["m"], motion_x=s["mx"], faces=[Face(*f) for f in s["f"]])
                for s in data["samples"]
            ],
        )


# --- scene detection --------------------------------------------------------

SCENE_MIN_SCORE = 12.0  # ignore tiny changes (compression noise, lighting flicker)
SCENE_HARD_SCORE = 40.0  # always a cut, regardless of neighbours
SCENE_ADAPTIVE_RATIO = 3.0  # score must be N x the local average (fast pans are not cuts)
SCENE_MIN_GAP_S = 1.0


def detect_scene_cuts(times: list[float], scores: list[float]) -> list[float]:
    """Adaptive content-change detector (same idea as PySceneDetect's AdaptiveDetector).

    ``scores[i]`` is the change between sample ``i-1`` and ``i``. A camera pan
    produces a sustained high score; a hard cut produces a single spike, so we
    compare each score against the average of its neighbours.
    """
    cuts: list[float] = []
    n = len(scores)
    for i in range(1, n):
        score = scores[i]
        if score < SCENE_MIN_SCORE:
            continue
        neighbours = [scores[j] for j in range(max(1, i - 2), min(n, i + 3)) if j != i]
        local = (sum(neighbours) / len(neighbours)) if neighbours else 0.0
        is_cut = score >= SCENE_HARD_SCORE or score >= SCENE_ADAPTIVE_RATIO * max(local, 1.0)
        if is_cut and (not cuts or times[i] - cuts[-1] >= SCENE_MIN_GAP_S):
            # The cut happened between the two samples; midpoint is the best estimate.
            cuts.append(round((times[i - 1] + times[i]) / 2, 3))
    return cuts


# --- pause detection --------------------------------------------------------

PAUSE_SAMPLE_RATE = 8000
PAUSE_WINDOW_S = 0.05
PAUSE_BELOW_MEDIAN_DB = 10.0
PAUSE_MIN_S = 0.3


def detect_pauses(path: Path, duration: float) -> list[tuple[float, float]]:
    """Find pauses in speech with a loudness threshold relative to the video itself.

    A fixed threshold (e.g. ``silencedetect=noise=-35dB``) fails on most real
    content: background music keeps the level above -35 dB, so no pauses are
    found. Instead we measure the RMS loudness in 50 ms windows and call
    anything 10 dB below the video's median loudness a pause. On a talking-head
    test video with background music this finds 25 pauses where silencedetect
    found 1.
    """
    proc = ffmpeg.popen(
        ["-i", str(path), "-vn", "-sn", "-ac", "1", "-ar", str(PAUSE_SAMPLE_RATE), "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    raw, _ = proc.communicate(timeout=max(120.0, duration))
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    window = int(PAUSE_SAMPLE_RATE * PAUSE_WINDOW_S)
    count = audio.size // window
    if count < 10:
        return []
    rms = np.sqrt((audio[: count * window].reshape(count, window) ** 2).mean(axis=1)) + 1e-7
    db = 20 * np.log10(rms)
    if db.max() < -60:  # the audio track is effectively silent
        return []
    quiet = db < np.median(db) - PAUSE_BELOW_MEDIAN_DB

    pauses: list[tuple[float, float]] = []
    run_start: int | None = None
    for i, is_quiet in enumerate(np.append(quiet, False)):
        if is_quiet and run_start is None:
            run_start = i
        elif not is_quiet and run_start is not None:
            if (i - run_start) * PAUSE_WINDOW_S >= PAUSE_MIN_S:
                pauses.append((round(run_start * PAUSE_WINDOW_S, 3), round(min(i * PAUSE_WINDOW_S, duration), 3)))
            run_start = None
    return pauses


# --- main pass --------------------------------------------------------------


def analyze(
    path: Path,
    info: ffmpeg.MediaInfo,
    detector: FaceDetector,
    sample_fps: int = 5,
    width: int = 640,
    progress: ProgressFn | None = None,
) -> Analysis:
    width = min(width, info.width) // 2 * 2
    height = max(2, round(width * info.height / info.width / 2) * 2)
    frame_bytes = width * height * 3

    proc = ffmpeg.popen(
        [
            "-i", str(path),
            "-an", "-sn",
            "-vf", f"fps={sample_fps},scale={width}:{height}:flags=area",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=frame_bytes * 4,
    )

    result = Analysis(sample_fps=sample_fps, duration=info.duration, detector=detector.backend)
    times: list[float] = []
    scores: list[float] = []
    prev_hsv: np.ndarray | None = None
    prev_gray: np.ndarray | None = None
    index = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
            t = round(index / sample_fps, 3)

            small = cv2.resize(frame, (width // 2, height // 2), interpolation=cv2.INTER_AREA)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.int16)
            gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)

            score = float(np.abs(hsv - prev_hsv).mean(axis=(0, 1)).mean()) if prev_hsv is not None else 0.0
            motion_x, motion = None, 0.0
            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                mask = (diff > 25).astype(np.float32)
                motion = float(mask.mean())
                if 0.002 < motion < 0.5:  # ignore noise and full-frame changes (cuts)
                    column_energy = mask.sum(axis=0)
                    xs = np.arange(column_energy.size, dtype=np.float32)
                    motion_x = float((column_energy * xs).sum() / column_energy.sum() / column_energy.size)

            result.samples.append(
                Sample(t=t, faces=detector.detect(frame), motion_x=motion_x, motion=round(motion, 4))
            )
            times.append(t)
            scores.append(score)
            prev_hsv, prev_gray = hsv, gray
            index += 1
            if progress and index % sample_fps == 0 and info.duration > 0:
                progress(min(0.99, t / info.duration))
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait(timeout=30)

    if not result.samples:
        raise ffmpeg.FFmpegError("Could not decode any frames from this video.")

    result.scene_cuts = detect_scene_cuts(times, scores)
    if info.has_audio:
        try:
            result.pauses = detect_pauses(path, info.duration)
        except Exception:  # pauses are a nice-to-have signal, never fatal
            log.exception("Pause detection failed")
    log.info(
        "Analysed %s: %d samples, %d cuts, %d pauses, detector=%s",
        path.name, len(result.samples), len(result.scene_cuts), len(result.pauses), detector.backend,
    )
    return result
