"""FastAPI application entry point.

Run with ``python -m app`` (or ``uvicorn app.main:app``). In production mode the
built React app in ``frontend/dist`` is served from the same origin, so a single
process and a single URL is all a non-developer needs.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import exports as exports_api
from .api import projects as projects_api
from .config import settings
from .core.storage import store
from .services import ffmpeg
from .services.face_detector import FaceDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("clipforge")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.recover_interrupted()
    removed = store.purge_older_than(settings.project_ttl_hours)
    if removed:
        log.info("Removed %d projects older than %dh", removed, settings.project_ttl_hours)
    log.info("FFmpeg: %s", ffmpeg.ffmpeg_path())
    log.info("Data directory: %s", settings.data_dir)
    yield


app = FastAPI(title="ClipForge API", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    # Never leak stack traces or file paths to the client.
    log.exception("Unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "face_detector": FaceDetector(settings.models_dir).backend,
    }


app.include_router(projects_api.router)
app.include_router(exports_api.router)


# --- Serve the built frontend (single-page app) ------------------------------
dist = settings.frontend_dist
if (dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
else:
    log.warning("Frontend build not found at %s; API only. Run `npm run build` in frontend/.", dist)
