"""Renders one clip as a 9:16 vertical MP4 that follows the camera path.

FFmpeg's crop filter can't follow an arbitrary per-frame trajectory without
generating huge expression strings, so rendering is a three-process pipeline:

    FFmpeg (decode + seek) --raw frames--> Python (per-frame crop) --raw frames--> FFmpeg (scale + x264 + AAC)

Cropping in NumPy is a zero-copy slice, so Python adds very little overhead;
decoding and encoding stay in native FFmpeg code. Audio is taken straight from
the source with the same trim, so A/V stay in sync.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import ffmpeg
from .reframer import CameraPath

log = logging.getLogger(__name__)


class RenderCancelled(Exception):
    pass


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
    if framing == "fit":
        # Whole frame, letterboxed over a blurred, zoomed copy of itself.
        crop_w, crop_h = src_w, src_h
    else:
        crop_w = min(src_w, int(round(src_h * 9 / 16 / 2)) * 2)
        crop_h = src_h if crop_w < src_w else min(src_h, int(round(src_w * 16 / 9 / 2)) * 2)
    fps = info.fps
    frame_bytes = src_w * src_h * 3
    total_frames = max(1, int(round(duration * fps)))

    # Pre-compute the crop x offset of every output frame.
    frame_times = start + np.arange(total_frames) / fps
    if framing != "auto" or crop_w >= src_w:
        centres = np.full(total_frames, 0.5)
    else:
        centres = path.at(frame_times)
    offsets = np.clip(np.round(centres * src_w - crop_w / 2), 0, src_w - crop_w).astype(int)
    y0 = (src_h - crop_h) // 2

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

    encoder_args = [
        "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{crop_w}x{crop_h}", "-r", f"{fps}", "-i", "pipe:0",
    ]
    if info.has_audio:
        encoder_args += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-map", "0:v", "-map", "1:a:0"]
    if framing == "fit":
        video_filter = (
            f"split[a][b];"
            f"[a]scale={out_w // 4}:{out_h // 4}:force_original_aspect_ratio=increase,crop={out_w // 4}:{out_h // 4},"
            f"boxblur=8:2,eq=brightness=-0.12,scale={out_w}:{out_h}[bg];"
            f"[b]scale={out_w}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )  # blur at quarter resolution: same look, ~16x cheaper
    else:
        video_filter = f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
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
            try:
                encoder.stdin.close()
            except OSError:
                pass
        encoder.wait()
        drain.join(timeout=5)

    if encoder.returncode != 0 or written == 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        detail = b"".join(stderr_lines).decode("utf-8", "replace").strip()[-500:]
        raise ffmpeg.FFmpegError(f"Encoding failed ({written} frames written). {detail}")

    tmp.replace(output)
    if progress:
        progress(1.0)
    log.info("Rendered %s (%.1fs, %d frames)", output.name, duration, written)
    return output
