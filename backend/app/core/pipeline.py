"""Background ingest pipeline: download -> probe -> preview proxy -> analyse -> segment."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from ..config import settings
from ..schemas import Clip, MediaInfoOut, Project
from ..services import analyzer, ffmpeg, reframer, segmenter
from ..services.downloader import DownloadError, download_youtube
from ..services.face_detector import FaceDetector
from .storage import store

log = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=settings.worker_threads, thread_name_prefix="ingest")

BROWSER_SAFE = {(".mp4", "h264"), (".m4v", "h264"), (".mov", "h264"), (".webm", "vp8"), (".webm", "vp9"), (".webm", "av1")}
MIN_DURATION_S = 3.0


class UserFacingError(Exception):
    """An error whose message is safe and useful to show in the UI."""


class _Throttle:
    """Persists progress at most every ``interval`` seconds (it's a file write)."""

    def __init__(self, project_id: str, interval: float = 0.7) -> None:
        self.project_id, self.interval, self.last = project_id, interval, 0.0

    def __call__(self, fraction: float) -> None:
        now = time.monotonic()
        if now - self.last >= self.interval:
            self.last = now
            store.update(self.project_id, progress=round(fraction, 3))


def submit(project_id: str) -> None:
    executor.submit(process_project, project_id)


def _stage(project_id: str, status: str, stage: str) -> None:
    store.update(project_id, status=status, stage=stage, progress=0.0)


def media_info(project: Project) -> ffmpeg.MediaInfo:
    m = project.media
    assert m is not None
    return ffmpeg.MediaInfo(m.duration, m.width, m.height, m.fps, m.has_audio, "")


def build_clips(project_id: str, duration: float, target: float) -> list[Clip]:
    data = store.read_json(project_id, "analysis.json")
    segments = segmenter.segment(duration, data["scene_cuts"], [tuple(s) for s in data["pauses"]], target)
    return [Clip(start=s.start, end=s.end, title=f"Clip {i + 1}") for i, s in enumerate(segments)]


def process_project(project_id: str) -> None:
    started = time.monotonic()
    try:
        project = store.get(project_id)
        folder = store.dir(project_id)

        if project.source_type == "youtube":
            _stage(project_id, "downloading", "Downloading video from YouTube")
            path, title = download_youtube(
                project.source_url or "",
                folder,
                max_height=settings.youtube_max_height,
                max_bytes=settings.max_upload_bytes,
                max_seconds=settings.max_video_minutes * 60,
                progress=_Throttle(project_id),
            )
            project = store.update(project_id, source_file=path.name, name=title)

        source = folder / (project.source_file or "")
        _stage(project_id, "analyzing", "Reading video")
        try:
            info = ffmpeg.probe(source)
        except ffmpeg.FFmpegError as exc:
            raise UserFacingError("This file isn't a video we can read. Try MP4, MOV, MKV or WEBM.") from exc
        if info.duration < MIN_DURATION_S:
            raise UserFacingError("The video is too short. It needs to be at least a few seconds long.")
        if info.duration > settings.max_video_minutes * 60:
            raise UserFacingError(f"The video is longer than the {settings.max_video_minutes} minute limit.")

        media = MediaInfoOut(
            duration=info.duration, width=info.width, height=info.height, fps=info.fps, has_audio=info.has_audio
        )
        store.update(project_id, media=media)

        if (source.suffix.lower(), info.video_codec) not in BROWSER_SAFE:
            _stage(project_id, "analyzing", "Preparing a browser preview")
            _make_preview(source, folder / "preview.mp4", info)
            store.update(project_id, preview_file="preview.mp4")

        _stage(project_id, "analyzing", "Finding scenes, pauses and faces")
        detector = FaceDetector(settings.models_dir)
        result = analyzer.analyze(
            source, info, detector,
            sample_fps=settings.analysis_fps, width=settings.analysis_width, progress=_Throttle(project_id),
        )
        store.write_json(project_id, "analysis.json", result.to_dict())

        _stage(project_id, "analyzing", "Planning the vertical framing")
        path = reframer.build_camera_path(result, info.width, info.height)
        store.write_json(project_id, "camera_path.json", path.to_dict())

        clips = build_clips(project_id, info.duration, project.target_length)
        store.update(
            project_id,
            status="ready",
            stage="Ready",
            progress=1.0,
            clips=clips,
            scene_cuts=result.scene_cuts,
            detector=result.detector,
        )
        log.info("Project %s ready in %.1fs (%d clips)", project_id, time.monotonic() - started, len(clips))
    except (UserFacingError, DownloadError) as exc:
        _fail(project_id, str(exc))
    except Exception:
        log.exception("Processing failed for project %s", project_id)
        _fail(project_id, "Something went wrong while processing this video. Please try again or use another file.")


def _fail(project_id: str, message: str) -> None:
    try:
        store.update(project_id, status="failed", stage="Failed", error=message)
    except Exception:
        log.exception("Could not mark project %s as failed", project_id)


def _make_preview(source, target, info: ffmpeg.MediaInfo) -> None:
    height = min(720, info.height) // 2 * 2
    args = [
        "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target),
    ]
    proc = ffmpeg.run(args, timeout=max(600.0, info.duration * 2))
    if proc.returncode != 0:
        raise UserFacingError("Could not convert this video for preview. Try exporting it as MP4 first.")
