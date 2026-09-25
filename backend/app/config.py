"""Runtime configuration.

Every setting can be overridden with an environment variable prefixed with
``CLIPFORGE_`` (for example ``CLIPFORGE_MAX_UPLOAD_MB=4096``). Defaults are chosen
so a non-developer can run the app locally without touching anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(f"CLIPFORGE_{name}", default)


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:  # fail fast on misconfiguration
        raise RuntimeError(f"CLIPFORGE_{name} must be an integer, got {raw!r}") from exc


def _env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(PROJECT_ROOT / "data"))).resolve())
    frontend_dist: Path = field(
        default_factory=lambda: Path(_env("FRONTEND_DIST", str(PROJECT_ROOT / "frontend" / "dist"))).resolve()
    )
    models_dir: Path = BACKEND_DIR / "models"

    # Ingest limits
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 4096))
    max_video_minutes: int = field(default_factory=lambda: _env_int("MAX_VIDEO_MINUTES", 180))
    youtube_max_height: int = field(default_factory=lambda: _env_int("YOUTUBE_MAX_HEIGHT", 1080))

    # Processing
    worker_threads: int = field(default_factory=lambda: _env_int("WORKER_THREADS", 2))
    analysis_fps: int = field(default_factory=lambda: _env_int("ANALYSIS_FPS", 5))
    analysis_width: int = field(default_factory=lambda: _env_int("ANALYSIS_WIDTH", 640))
    output_width: int = field(default_factory=lambda: _env_int("OUTPUT_WIDTH", 1080))
    output_height: int = field(default_factory=lambda: _env_int("OUTPUT_HEIGHT", 1920))
    x264_preset: str = field(default_factory=lambda: _env("X264_PRESET", "veryfast"))
    x264_crf: int = field(default_factory=lambda: _env_int("X264_CRF", 20))

    # Housekeeping
    project_ttl_hours: int = field(default_factory=lambda: _env_int("PROJECT_TTL_HOURS", 72))
    cors_origins: list[str] = field(
        default_factory=lambda: _env_list("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    )

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings()
