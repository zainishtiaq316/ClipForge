"""Export jobs: render selected clips to vertical MP4 and bundle them.

Rendered clips are cached by a hash of everything that affects the output
(start, end, framing, encoder settings), so exporting the same clip twice, or
exporting it alone and then again in "Export all", only encodes it once.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import settings
from ..schemas import Clip, ExportJob, Project
from ..services import ffmpeg
from ..services.reframer import CameraPath
from ..services.renderer import render_vertical_clip
from .pipeline import media_info
from .storage import store

log = logging.getLogger(__name__)

RENDER_VERSION = "v5"  # bump to invalidate cached renders after changing the renderer
JOB_TTL_S = 6 * 3600

# x264 already uses every core, so clips are rendered one at a time.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="export")


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:40]
    return slug or fallback


def _timestamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}m{s:02d}s"


def clip_file_name(project: Project, clip: Clip) -> str:
    index = next((i for i, c in enumerate(project.clips) if c.id == clip.id), 0) + 1
    return f"{index:02d}_{_slug(clip.title, 'clip')}_{_timestamp(clip.start)}.mp4"


def render_key(clip: Clip) -> str:
    raw = (
        f"{RENDER_VERSION}|{clip.start:.3f}|{clip.end:.3f}|{clip.framing}|{clip.zoom:.3f}|"
        f"{settings.output_width}x{settings.output_height}|{settings.x264_preset}|{settings.x264_crf}"
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


class ExportManager:
    def __init__(self) -> None:
        self._jobs: dict[str, ExportJob] = {}
        self._files: dict[str, Path] = {}
        self._lock = threading.Lock()

    def create(self, project: Project, clip_ids: list[str]) -> ExportJob:
        known = {c.id for c in project.clips}
        missing = [cid for cid in clip_ids if cid not in known]
        if missing:
            raise KeyError(f"Unknown clip ids: {', '.join(missing)}")
        # Keep timeline order and drop duplicates.
        ordered = [c.id for c in project.clips if c.id in set(clip_ids)]
        job = ExportJob(id=secrets.token_hex(12), project_id=project.id, clip_ids=ordered)
        with self._lock:
            self._prune()
            self._jobs[job.id] = job
        executor.submit(self._run, job.id)
        return job

    def get(self, job_id: str) -> ExportJob:
        with self._lock:
            return self._jobs[job_id].model_copy()

    def file(self, job_id: str) -> Path:
        with self._lock:
            return self._files[job_id]

    def _set(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)

    def _prune(self) -> None:
        cutoff = time.time() - JOB_TTL_S
        for job_id in [j for j, job in self._jobs.items() if job.created_at < cutoff]:
            self._jobs.pop(job_id, None)
            self._files.pop(job_id, None)

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        try:
            project = store.get(job.project_id)
            clips = [c for c in project.clips if c.id in set(job.clip_ids)]
            if not clips:
                raise ValueError("The selected clips no longer exist.")
            folder = store.dir(project.id)
            source = folder / (project.source_file or "")
            info = media_info(project)
            path = CameraPath.from_dict(store.read_json(project.id, "camera_path.json"))
            renders = folder / "renders"

            outputs: list[tuple[Path, str]] = []
            total = len(clips)
            for i, clip in enumerate(clips):
                self._set(job_id, status="rendering", current=i + 1, progress=round(i / total, 3))
                target = renders / f"{render_key(clip)}.mp4"
                if not target.exists():
                    render_vertical_clip(
                        source, info, path, clip.start, min(clip.end, info.duration), target,
                        framing=clip.framing, zoom=clip.zoom,
                        out_w=settings.output_width, out_h=settings.output_height,
                        preset=settings.x264_preset, crf=settings.x264_crf,
                        progress=lambda p, i=i: self._set(job_id, progress=round((i + p) / total, 3)),
                    )
                outputs.append((target, clip_file_name(project, clip)))

            if len(outputs) == 1:
                file_path, name = outputs[0]
            else:
                self._set(job_id, status="packaging", progress=0.99)
                exports = folder / "exports"
                exports.mkdir(exist_ok=True)
                file_path = exports / f"{job_id}.zip"
                # MP4 is already compressed; storing avoids burning CPU for ~0% gain.
                with zipfile.ZipFile(file_path, "w", compression=zipfile.ZIP_STORED) as bundle:
                    for clip_path, clip_name in outputs:
                        bundle.write(clip_path, arcname=clip_name)
                name = f"{_slug(project.name, 'project')}_vertical_clips.zip"

            with self._lock:
                self._files[job_id] = file_path
            self._set(job_id, status="done", progress=1.0, file_name=name)
        except ffmpeg.FFmpegError:
            log.exception("Export %s failed", job_id)
            self._set(job_id, status="failed", error="Rendering failed. The source video may be damaged.")
        except Exception as exc:
            log.exception("Export %s failed", job_id)
            message = str(exc) if isinstance(exc, ValueError) else "Export failed unexpectedly. Please try again."
            self._set(job_id, status="failed", error=message)


exports = ExportManager()
