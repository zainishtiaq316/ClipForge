"""Renders one clip as a 9:16 vertical MP4 that follows the camera path.

FFmpeg's crop filter can't follow an arbitrary per-frame trajectory without
generating huge expression strings, so rendering is a three-process pipeline:

    FFmpeg (decode + seek) --raw frames--> Python (per-frame layout) --raw frames--> FFmpeg (x264 + AAC)

Every output frame is the full-height 9:16 window that follows the subject. Two
things can change that:

* **zoom out** (per clip, chosen by the user): a wider window is shown and the
  space above and below is filled with solid black (never blurred);
* **on-screen text** that the crop would cut: if it fits in the window, the
  window shifts a little to include it; otherwise it is erased and re-flowed
  into the frame by ``text_layout``.

Clips with neither take the fast path: Python slices the frame (zero-copy) and
FFmpeg does the upscale. Re-flowed text is composed at crop resolution (FFmpeg still
upscales, so it stays almost as fast); zoomed-out clips are composed at output size.
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
from .text_layout import View, draw_ops, fit_text_window, layout_ops

log = logging.getLogger(__name__)

BAR_COLOR = (0, 0, 0)


def crop_width(src_w: int, src_h: int, zoom: float) -> int:
    """Source width shown in the vertical frame. zoom 0 = full-height 9:16 window, 1 = whole frame width."""
    base = min(src_w, int(round(src_h * 9 / 16 / 2)) * 2)
    return min(src_w, int(round((base + zoom * (src_w - base)) / 2)) * 2)


def render_vertical_clip(
    source: Path,
    info: ffmpeg.MediaInfo,
    path: CameraPath,
    start: float,
    end: float,
    output: Path,
    *,
    framing: str = "auto",
    zoom: float = 0.0,
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
    crop_w = crop_width(src_w, src_h, zoom)
    # A portrait source can be taller than 9:16; then crop its height instead.
    crop_h = src_h if crop_w < src_w or src_h * 9 <= src_w * 16 else int(round(src_w * 16 / 9 / 2)) * 2
    fps = info.fps
    frame_bytes = src_w * src_h * 3
    total_frames = max(1, int(round(duration * fps)))

    # Pre-compute the crop window of every output frame.
    frame_times = start + np.arange(total_frames) / fps
    follows_subject = framing in ("auto", "track") and crop_w < src_w
    centres = path.at(frame_times) if follows_subject else np.full(total_frames, 0.5)
    offsets = np.clip(np.round(centres * src_w - crop_w / 2), 0, src_w - crop_w).astype(int)
    y0 = (src_h - crop_h) // 2

    reflow_text = framing == "auto" and any(lay.start < end and lay.end > start for lay in path.text_layouts)
    with_bars = zoom > 0  # composed at output size; otherwise frames stay at crop size and FFmpeg upscales

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

    in_w, in_h = (out_w, out_h) if with_bars else (crop_w, crop_h)
    encoder_args = [
        "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{in_w}x{in_h}", "-r", f"{fps}", "-i", "pipe:0",
    ]
    if info.has_audio:
        encoder_args += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
        encoder_args += ["-map", "0:v", "-map", "1:a:0"]
    video_filter = "setsar=1" if with_bars else f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
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
            x0 = int(offsets[written])
            t = float(frame_times[written])
            layout = path.text_at(t) if reflow_text else None
            text_fits = False
            if layout is not None:
                x0, text_fits = fit_text_window(x0, crop_w, src_w, float(centres[written]) * src_w, layout, t)
            crop = frame[y0 : y0 + crop_h, x0 : x0 + crop_w]
            reflow = layout is not None and not text_fits
            if with_bars:
                view = View(src_w, src_h, x0, crop_w, out_w, out_h)
                canvas = np.empty((out_h, out_w, 3), np.uint8)
                canvas[:] = BAR_COLOR
                top, video_h = view.video_top, view.video_h
                interp = cv2.INTER_AREA if out_w < crop_w else cv2.INTER_CUBIC
                canvas[top : top + video_h] = cv2.resize(crop, (out_w, video_h), interpolation=interp)
                if reflow:
                    draw_ops(canvas, frame, layout_ops(layout.lines, view))
                encoder.stdin.write(canvas.tobytes())
            elif reflow:
                # Same geometry at crop resolution (it is scale-invariant); FFmpeg upscales.
                canvas = crop.copy()
                draw_ops(canvas, frame, layout_ops(layout.lines, View(src_w, src_h, x0, crop_w, crop_w, crop_h)))
                encoder.stdin.write(canvas.tobytes())
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
    log.info("Rendered %s (%.1fs, %d frames, zoom=%.2f, text reflow=%s)", output.name, duration, written, zoom,
             reflow_text)
    return output
