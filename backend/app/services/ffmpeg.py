"""Thin, safe wrapper around the FFmpeg binary.

* Prefers an ``ffmpeg`` found on PATH, otherwise falls back to the static build
  shipped with the ``imageio-ffmpeg`` wheel, so end users never have to install
  FFmpeg by hand.
* Commands are always passed as argument lists (never through a shell), so
  user-controlled values can't inject shell syntax.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Hide the console window that would otherwise flash for every FFmpeg call on Windows.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegError(RuntimeError):
    """Raised when FFmpeg fails or the input can't be decoded."""


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on the host
        raise FFmpegError(
            "FFmpeg was not found. Install it or run `pip install imageio-ffmpeg`."
        ) from exc


def run(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg with ``args`` and return the completed process (stderr captured)."""
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", *args]
    log.debug("ffmpeg %s", " ".join(args))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def popen(args: list[str], **kwargs) -> subprocess.Popen:
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    return subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW, **kwargs)


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fps|tbr)")
_ROTATE_RE = re.compile(r"rotation of (-?\d+(?:\.\d+)?)|rotate\s*:\s*(-?\d+)")


def probe(path: Path) -> MediaInfo:
    """Read basic stream information by parsing ``ffmpeg -i`` output.

    The bundled static build has no ``ffprobe``, so this parses the banner that
    ``ffmpeg -i`` prints. It's a stable, documented format.
    """
    proc = run(["-i", str(path)], timeout=60)
    text = proc.stderr

    video = next((m for m in _VIDEO_RE.finditer(text)), None)
    duration = _DURATION_RE.search(text)
    if not video or not duration:
        raise FFmpegError("The file does not contain a readable video stream.")

    hours, minutes, seconds = duration.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    video_line = text[video.start(): text.find("\n", video.start())]
    fps_match = _FPS_RE.search(video_line)
    fps = float(fps_match.group(1)) if fps_match else 30.0
    if not 1 <= fps <= 240:
        fps = 30.0

    width, height = int(video.group(2)), int(video.group(3))
    rotate = _ROTATE_RE.search(text)
    if rotate:
        angle = abs(float(rotate.group(1) or rotate.group(2))) % 180
        if angle == 90:  # phone footage stored sideways; FFmpeg auto-rotates on decode
            width, height = height, width

    return MediaInfo(
        duration=round(total, 3),
        width=width,
        height=height,
        fps=round(fps, 3),
        has_audio=bool(re.search(r"Stream #\d+:\d+.*?: Audio:", text)),
        video_codec=video.group(1),
    )
