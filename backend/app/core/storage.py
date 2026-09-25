"""File-system project store.

Each project lives in its own directory ``data/projects/<id>/``:

    project.json      metadata + clips (the source of truth)
    source.<ext>      the original video
    preview.mp4       browser-friendly proxy (only if the source isn't H.264/MP4)
    analysis.json     raw per-sample detections
    camera_path.json  smoothed crop trajectory
    renders/          cached vertical clips
    exports/          ZIP bundles

A database would be overkill for a single-user local tool; JSON files keep the
state inspectable and trivially portable. Writes are atomic (write to a temp
file, then rename) so a crash can never leave a half-written project.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path

from ..config import settings
from ..schemas import Project

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class ProjectNotFound(KeyError):
    pass


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
        self._guard = threading.Lock()

    # --- paths ------------------------------------------------------------

    def dir(self, project_id: str) -> Path:
        # Strict id format = no path traversal via "../" or absolute paths.
        if not _ID_RE.match(project_id or ""):
            raise ProjectNotFound(project_id)
        return self.root / project_id

    def file(self, project_id: str, name: str) -> Path:
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"Invalid file name: {name!r}")
        return self.dir(project_id) / name

    def lock(self, project_id: str) -> threading.RLock:
        with self._guard:
            return self._locks[project_id]

    # --- CRUD -------------------------------------------------------------

    def new_id(self) -> str:
        return secrets.token_hex(16)

    def create(self, project: Project) -> Project:
        self.dir(project.id).mkdir(parents=True, exist_ok=False)
        self.save(project)
        return project

    def get(self, project_id: str) -> Project:
        path = self.dir(project_id) / "project.json"
        if not path.exists():
            raise ProjectNotFound(project_id)
        return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, project: Project) -> None:
        project.updated_at = time.time()
        self.write_json(project.id, "project.json", project.model_dump(mode="json"))

    def update(self, project_id: str, **changes) -> Project:
        with self.lock(project_id):
            project = self.get(project_id)
            for key, value in changes.items():
                setattr(project, key, value)
            self.save(project)
            return project

    def delete(self, project_id: str) -> None:
        with self.lock(project_id):
            shutil.rmtree(self.dir(project_id), ignore_errors=True)

    def list(self) -> list[Project]:
        projects = []
        for child in self.root.iterdir():
            if child.is_dir() and _ID_RE.match(child.name):
                try:
                    projects.append(self.get(child.name))
                except Exception:
                    log.warning("Skipping unreadable project %s", child.name)
        return sorted(projects, key=lambda p: p.created_at, reverse=True)

    # --- JSON helpers -----------------------------------------------------

    def write_json(self, project_id: str, name: str, data) -> None:
        target = self.file(project_id, name)
        tmp = target.with_name(f".{name}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, target)

    def read_json(self, project_id: str, name: str):
        return json.loads(self.file(project_id, name).read_text(encoding="utf-8"))

    # --- housekeeping -----------------------------------------------------

    def recover_interrupted(self) -> None:
        """Projects that were mid-processing when the server stopped can't resume."""
        for project in self.list():
            if project.status in {"queued", "downloading", "analyzing"}:
                project.status = "failed"
                project.error = "Processing was interrupted because the server restarted. Please try again."
                self.save(project)

    def purge_older_than(self, hours: int) -> int:
        if hours <= 0:
            return 0
        cutoff = time.time() - hours * 3600
        removed = 0
        for project in self.list():
            if project.updated_at < cutoff:
                self.delete(project.id)
                removed += 1
        return removed


store = ProjectStore(settings.projects_dir)
