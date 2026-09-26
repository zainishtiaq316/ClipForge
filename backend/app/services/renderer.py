"""Renders one clip as a 9:16 vertical MP4 that follows the camera path.

FFmpeg's crop filter can't follow an arbitrary per-frame trajectory without
generating huge expression strings, so rendering is a three-process pipeline:

    FFmpeg (decode + seek) --raw frames--> Python (per-frame layout) --raw frames--> FFmpeg (x264 + AAC)

Two layouts exist per frame:

* **crop**: a full-height 9:16 window that follows the subject;
* **fit**: the whole frame over a blurred, zoomed copy of itself, used when
  on-screen text would be cut (or when the user picks "Fit" for the clip).

A clip that only crops takes the fast path: Python slices the frame (zero-copy)
and FFmpeg does the upscale. Clips that switch layouts are composed in Python at
the output size, with a short crossfade so the switch doesn't look like a glitch.
Audio is taken straight from the source with the same trim, so A/V stay in sync.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

from . import ffmpeg
from .reframer import CameraPath

log = logging.getLogger(__name__)

LAYOUT_CROSSFADE_S = 0.25
BLUR_DOWNSCALE = 8  # blur a 1/8-size copy: same look, ~64x cheaper


def compose_fit(frame: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """Whole frame, letterboxed over a blurred and darkened 'cover' copy of itself."""
    src_h, src_w = frame.shape[:2]
    cover_w = min(src_w, int(src_h * out_w / out_h))
    x0 = (src_w - cover_w) // 2
    small = cv2.resize(frame[:, x0 : x0 + cover_w], (out_w // BLUR_DOWNSCALE, out_h // BLUR_DOWNSCALE),
                       interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 2.5)
    canvas = cv2.convertScaleAbs(cv2.resize(small, (out_w, out_h), interpolation=cv2.INTER_LINEAR), alpha=0.78)
    fg_h = min(out_h, int(round(out_w * src_h / src_w / 2)) * 2)
    fg = cv2.resize(frame, (out_w, fg_h), interpolation=cv2.INTER_AREA)
    y = (out_h - fg_h) // 2
    canvas[y : y + fg_h] = fg
    return canvas


def _fit_weights(frame_times: np.ndarray, path: CameraPath, framing: str, fps: float) -> np.ndarray:
    """Per-frame weight of the fit layout (0 = crop, 1 = fit).

    A switch that lands on a scene cut is a hard cut (the edit hides it); a switch
    in the middle of a shot is crossfaded over ``LAYOUT_CROSSFADE_S``.
    """
    if framing == "fit":
        return np.ones(frame_times.size)
    if framing != "auto" or not path.fit_ranges:
        return np.zeros(frame_times.size)
    mask = path.fit_mask(frame_times).astype(float)
    weights = mask.copy()
    half = max(1, int(round(LAYOUT_CROSSFADE_S * fps / 2)))
    for i in np.flatnonzero(np.diff(mask)) + 1:
        t = frame_times[i]
        if any(abs(t - cut) <= 1.5 / fps for cut in path.scene_cuts):
            continue
        lo, hi = max(0, i - half), min(mask.size, i + half)
        ramp = np.linspace(0.0, 1.0, hi - lo + 2)[1:-1]
        weights[lo:hi] = ramp if mask[i] > mask[i - 1] else ramp[::-1]
    return weights


def render_vertical_clip(
    source: Path,
    info: ffmpeg.MediaInfo,
    path: CameraPath,
    start: float,
    end: float,
    output: Path,
    *,
    framing: str = "auto",
    out_w: int = 1080,
    out_h: int = 1920,
    preset: str = "veryfast",
    crf: int = 20,
    progress: Callable[[float], None] | None = None,
) -> Path:
    duration = end - start
    if duration <= 0:
        raise ValueError("Clip end must be after its start")

    src_w, src_h = info.width, info.height
    crop_w = min(src_w, int(round(src_h * 9 / 16 / 2)) * 2)
    crop_h = src_h if crop_w < src_w else min(src_h, int(round(src_w * 16 / 9 / 2)) * 2)
    fps = info.fps
    frame_bytes = src_w * src_h * 3
    total_frames = max(1, int(round(duration * fps)))

    # Pre-compute the layout of every output frame.
    frame_times = start + np.arange(total_frames) / fps
    follows_subject = framing in ("auto", "track") and crop_w < src_w
    centres = path.at(frame_times) if follows_subject else np.full(total_frames, 0.5)
    offsets = np.clip(np.round(centres * src_w - crop_w / 2), 0, src_w - crop_w).astype(int)
    y0 = (src_h - crop_h) // 2
    fit_weight = _fit_weights(frame_times, path, framing, fps)
    composed = bool(fit_weight.any())  # otherwise take the fast crop-only path

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".part.mp4")

    decoder = ffmpeg.popen(
        [
            "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
            "-an", "-sn", "-map", "0:v:0",
            "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=frame_bytes * 2,
    )

    in_w, in_h = (out_w, out_h) if composed else (crop_w, crop_h)
    encoder_args = [
        "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{in_w}x{in_h}", "-r", f"{fps}", "-i", "pipe:0",
    ]
    if info.has_audio:
        encoder_args += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
        encoder_args += ["-map", "0:v", "-map", "1:a:0"]
    video_filter = "setsar=1" if composed else f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    encoder_args += [
        "-vf", video_filter,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-movflags", "+faststart",
    ]
    if info.has_audio:
        encoder_args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    encoder_args += ["-t", f"{duration:.3f}", str(tmp)]

    stderr_lines: list[bytes] = []
    encoder = ffmpeg.popen(encoder_args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    # Drain stderr on a thread so a chatty encoder can never block the pipe.
    drain = threading.Thread(target=lambda: stderr_lines.extend(encoder.stderr), daemon=True)
    drain.start()

    written = 0
    try:
        assert decoder.stdout is not None and encoder.stdin is not None
        while written < total_frames:
            buf = decoder.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(src_h, src_w, 3)
            x0 = offsets[written]
            crop = frame[y0 : y0 + crop_h, x0 : x0 + crop_w]
            if composed:
                weight = fit_weight[written]
                if weight >= 1.0:
                    out = compose_fit(frame, out_w, out_h)
                else:
                    out = cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_CUBIC)
                    if weight > 0.0:
                        out = cv2.addWeighted(compose_fit(frame, out_w, out_h), weight, out, 1.0 - weight, 0)
                encoder.stdin.write(out.tobytes())
            else:
                encoder.stdin.write(np.ascontiguousarray(crop).tobytes())
            written += 1
            if progress and written % 15 == 0:
                progress(written / total_frames)
    except BrokenPipeError:
        pass  # encoder died; its stderr below explains why
    finally:
        if decoder.stdout:
            decoder.stdout.close()
        decoder.kill()
        decoder.wait()
        if encoder.stdin and not encoder.stdin.closed:
            with contextlib.suppress(OSError):
                encoder.stdin.close()
        encoder.wait()
        drain.join(timeout=5)

    if encoder.returncode != 0 or written == 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        detail = b"".join(stderr_lines).decode("utf-8", "replace").strip()[-500:]
        raise ffmpeg.FFmpegError(f"Encoding failed ({written} frames written). {detail}")

    tmp.replace(output)
    if progress:
        progress(1.0)
    log.info("Rendered %s (%.1fs, %d frames, layout=%s)", output.name, duration, written,
             "composed" if composed else "crop")
    return output
