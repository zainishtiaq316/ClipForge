"""Project endpoints: ingest, status, source streaming, clip editing."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from ..config import settings
from ..core import pipeline
from ..core.storage import ProjectNotFound, store
from ..schemas import ClipsUpdate, Project, ProjectOut, ResegmentRequest, YouTubeRequest
from ..services.downloader import normalize_youtube_url

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects", tags=["projects"])

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
CHUNK = 1024 * 1024


def get_project(project_id: str) -> Project:
    try:
        return store.get(project_id)
    except ProjectNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from None


def _display_name(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[_\-.]+", " ", stem).strip()
    return (stem or "Uploaded video")[:120]


@router.get("", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut.of(p) for p in store.list()[:50]]


@router.post("/upload", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def upload_video(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    target_length: float = Query(default=30, ge=10, le=180),
) -> ProjectOut:
    """Stream the raw request body to disk.

    A raw body (instead of multipart) lets us enforce the size limit while
    streaming and write the file exactly once, which matters for multi-GB videos.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is larger than {settings.max_upload_mb} MB"
        )

    project = Project(
        id=store.new_id(),
        name=_display_name(filename),
        source_type="upload",
        source_file=f"source{ext}",  # never trust the client file name on disk
        target_length=target_length,
        stage="Uploading",
    )
    store.create(project)
    target = store.dir(project.id) / project.source_file

    received = 0
    try:
        with target.open("wb") as fh:
            async for chunk in request.stream():
                received += len(chunk)
                if received > settings.max_upload_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is larger than {settings.max_upload_mb} MB"
                    )
                fh.write(chunk)
        if received == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty")
    except BaseException:
        store.delete(project.id)
        raise

    pipeline.submit(project.id)
    return ProjectOut.of(store.update(project.id, stage="Waiting to start"))


@router.post("/youtube", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def import_youtube(body: YouTubeRequest) -> ProjectOut:
    try:
        url = normalize_youtube_url(body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    project = Project(
        id=store.new_id(),
        name="YouTube video",
        source_type="youtube",
        source_url=url,
        target_length=body.target_length,
    )
    store.create(project)
    pipeline.submit(project.id)
    return ProjectOut.of(project)


@router.get("/{project_id}", response_model=ProjectOut)
def read_project(project_id: str) -> ProjectOut:
    return ProjectOut.of(get_project(project_id))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str) -> None:
    project = get_project(project_id)
    if project.status in {"queued", "downloading", "analyzing"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for processing to finish before deleting")
    store.delete(project_id)


@router.get("/{project_id}/video")
def stream_video(project_id: str) -> FileResponse:
    """Serves the (browser-friendly) source video. Supports HTTP Range for seeking."""
    project = get_project(project_id)
    name = project.preview_file or project.source_file
    if not name or project.status in {"queued", "downloading"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Video is not available yet")
    path = store.file(project_id, name)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video file missing")
    media_type = "video/webm" if path.suffix == ".webm" else "video/mp4"
    return FileResponse(path, media_type=media_type)


def _require_ready(project: Project) -> None:
    if project.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "The project is still processing")


@router.get("/{project_id}/camera-path")
def camera_path(project_id: str) -> dict:
    project = get_project(project_id)
    _require_ready(project)
    return store.read_json(project_id, "camera_path.json")


@router.put("/{project_id}/clips", response_model=ProjectOut)
def update_clips(project_id: str, body: ClipsUpdate) -> ProjectOut:
    with store.lock(project_id):
        project = get_project(project_id)
        _require_ready(project)
        duration = project.media.duration if project.media else 0
        for clip in body.clips:
            if clip.end > duration + 0.05:
                raise HTTPException(
                    422,
                    f"'{clip.title or clip.id}' ends after the video ends ({duration:.1f}s)",
                )
            clip.end = min(clip.end, duration)
        project.clips = sorted(body.clips, key=lambda c: (c.start, c.end))
        store.save(project)
        return ProjectOut.of(project)


@router.post("/{project_id}/resegment", response_model=ProjectOut)
def resegment(project_id: str, body: ResegmentRequest) -> ProjectOut:
    with store.lock(project_id):
        project = get_project(project_id)
        _require_ready(project)
        assert project.media is not None
        project.target_length = body.target_length
        project.clips = pipeline.build_clips(project_id, project.media.duration, body.target_length)
        store.save(project)
        return ProjectOut.of(project)
