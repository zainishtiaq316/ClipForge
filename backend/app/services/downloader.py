"""YouTube ingest through yt-dlp (Unlicense).

Security: only YouTube hostnames are accepted. yt-dlp supports 1 800+ sites,
and an unrestricted URL field on an internal tool is an SSRF / abuse vector
(it could be pointed at internal hosts or huge files). Playlists are disabled
and size and duration limits are enforced *before* downloading.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import ffmpeg

log = logging.getLogger(__name__)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class DownloadError(RuntimeError):
    pass


def normalize_youtube_url(raw: str) -> str:
    """Validate a user-supplied URL and return a canonical watch URL.

    Raises ``ValueError`` with a user-friendly message if it isn't a YouTube video.
    """
    raw = (raw or "").strip()
    if len(raw) > 500:
        raise ValueError("That link is too long to be a YouTube video URL.")
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError("Please paste a YouTube link (youtube.com or youtu.be).")

    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [None])[0]
    else:
        match = re.match(r"^/(?:shorts|live|embed|v)/([^/?#]+)", parsed.path)
        video_id = match.group(1) if match else None

    if not video_id or not _VIDEO_ID.match(video_id):
        raise ValueError("That YouTube link doesn't point to a single video.")
    return f"https://www.youtube.com/watch?v={video_id}"


def download_youtube(
    url: str,
    dest_dir: Path,
    *,
    max_height: int,
    max_bytes: int,
    max_seconds: int,
    progress: Callable[[float], None] | None = None,
) -> tuple[Path, str]:
    """Download a YouTube video as MP4. Returns ``(path, title)``."""
    import yt_dlp

    # Video and audio are separate downloads; weight them roughly by size
    # (video ~90%) so the bar never jumps backwards when the audio starts.
    state = {"files_done": 0, "best": 0.0}

    def hook(status: dict) -> None:
        if not progress:
            return
        if status.get("status") == "finished":
            state["files_done"] += 1
            return
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            if total:
                fraction = status.get("downloaded_bytes", 0) / total
                overall = 0.9 * fraction if state["files_done"] == 0 else 0.9 + 0.1 * fraction
                state["best"] = max(state["best"], min(0.99, overall))
                progress(state["best"])

    def guard(info: dict, *, incomplete: bool) -> str | None:
        duration = info.get("duration")
        if duration and duration > max_seconds:
            return f"This video is longer than the {max_seconds // 60} minute limit."
        if info.get("is_live"):
            return "Live streams can't be processed. Please use a finished video."
        return None

    options = {
        # Prefer H.264 + AAC so every browser can preview it and FFmpeg seeks fast.
        "format": (
            f"bv*[height<={max_height}][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "noplaylist": True,
        "max_filesize": max_bytes,
        "match_filter": guard,
        "ffmpeg_location": ffmpeg.ffmpeg_path(),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "")
        raise DownloadError(f"YouTube download failed: {message}") from exc

    if info is None:
        raise DownloadError("This video was skipped (it may be too long or a live stream).")

    files = sorted(dest_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix.lower() in {".mp4", ".mkv", ".webm"} and ".part" not in f.name]
    if not files:
        raise DownloadError("The download finished but no video file was produced.")
    return files[0], (info.get("title") or "YouTube video")[:200]
