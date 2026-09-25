"""Export endpoints: start a render job, poll it, download the result."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from ..core.exports import exports
from ..schemas import ExportJob, ExportRequest
from .projects import _require_ready, get_project

router = APIRouter(prefix="/api", tags=["exports"])


@router.post("/projects/{project_id}/exports", response_model=ExportJob, status_code=status.HTTP_202_ACCEPTED)
def start_export(project_id: str, body: ExportRequest) -> ExportJob:
    project = get_project(project_id)
    _require_ready(project)
    try:
        return exports.create(project, body.clip_ids)
    except KeyError as exc:
        raise HTTPException(422, str(exc.args[0])) from None


def _job(job_id: str) -> ExportJob:
    try:
        return exports.get(job_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export not found or expired") from None


@router.get("/exports/{job_id}", response_model=ExportJob)
def export_status(job_id: str) -> ExportJob:
    return _job(job_id)


@router.get("/exports/{job_id}/download")
def download_export(job_id: str) -> FileResponse:
    job = _job(job_id)
    if job.status != "done" or not job.file_name:
        raise HTTPException(status.HTTP_409_CONFLICT, "The export is not finished yet")
    path = exports.file(job_id)
    if not path.exists():
        raise HTTPException(status.HTTP_410_GONE, "The exported file has been cleaned up. Please export again.")
    media_type = "application/zip" if path.suffix == ".zip" else "video/mp4"
    return FileResponse(path, media_type=media_type, filename=job.file_name)
