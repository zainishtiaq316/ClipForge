"""API and persistence models."""

from __future__ import annotations

import secrets
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ProjectStatus = Literal["queued", "downloading", "analyzing", "ready", "failed"]
ExportStatus = Literal["queued", "rendering", "packaging", "done", "failed"]
Framing = Literal["auto", "track", "center", "fit"]

MAX_CLIPS = 300
MIN_CLIP_LENGTH = 1.0


def new_clip_id() -> str:
    return secrets.token_hex(4)


class MediaInfoOut(BaseModel):
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


class Clip(BaseModel):
    id: str = Field(default_factory=new_clip_id, pattern=r"^[a-f0-9]{8}$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    framing: Framing = "auto"
    title: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def _ordered(self) -> Clip:
        self.start = round(self.start, 3)
        self.end = round(self.end, 3)
        if self.end - self.start < MIN_CLIP_LENGTH:
            raise ValueError(f"A clip must be at least {MIN_CLIP_LENGTH:g} second long")
        return self

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


class Project(BaseModel):
    id: str
    name: str
    source_type: Literal["upload", "youtube"]
    source_url: str | None = None
    status: ProjectStatus = "queued"
    stage: str = "Waiting to start"
    progress: float = 0.0
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    target_length: float = 30.0
    media: MediaInfoOut | None = None
    source_file: str | None = None  # file name inside the project dir
    preview_file: str | None = None  # browser-friendly copy, if the source isn't one
    scene_cuts: list[float] = Field(default_factory=list)
    clips: list[Clip] = Field(default_factory=list)
    detector: str = ""


class ProjectOut(BaseModel):
    """Public view of a project (no file-system details)."""

    id: str
    name: str
    source_type: str
    source_url: str | None
    status: ProjectStatus
    stage: str
    progress: float
    error: str | None
    created_at: float
    target_length: float
    media: MediaInfoOut | None
    scene_cuts: list[float]
    clips: list[Clip]
    detector: str

    @classmethod
    def of(cls, project: Project) -> ProjectOut:
        return cls(**project.model_dump(exclude={"source_file", "preview_file", "updated_at"}))


class YouTubeRequest(BaseModel):
    url: str = Field(min_length=5, max_length=500)
    target_length: float = Field(default=30, ge=10, le=180)


class ClipsUpdate(BaseModel):
    clips: list[Clip] = Field(max_length=MAX_CLIPS)

    @field_validator("clips")
    @classmethod
    def _unique_ids(cls, clips: list[Clip]) -> list[Clip]:
        ids = [c.id for c in clips]
        if len(ids) != len(set(ids)):
            raise ValueError("Clip ids must be unique")
        return clips


class ResegmentRequest(BaseModel):
    target_length: float = Field(ge=10, le=180)


class ExportRequest(BaseModel):
    clip_ids: list[str] = Field(min_length=1, max_length=MAX_CLIPS)


class ExportJob(BaseModel):
    id: str
    project_id: str
    clip_ids: list[str]
    status: ExportStatus = "queued"
    progress: float = 0.0
    current: int = 0  # 1-based index of the clip being rendered
    error: str | None = None
    file_name: str | None = None
    created_at: float = Field(default_factory=time.time)
