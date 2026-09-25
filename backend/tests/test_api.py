"""End-to-end API test against a synthetic video (no network needed).

FFmpeg generates a 16:9 test pattern with a tone that has a gap in the middle,
the video goes through the real pipeline, and the exported files are checked.
"""

import io
import os
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

os.environ["CLIPFORGE_DATA_DIR"] = tempfile.mkdtemp(prefix="clipforge-test-")
os.environ["CLIPFORGE_OUTPUT_WIDTH"] = "360"  # small renders keep the test fast
os.environ["CLIPFORGE_OUTPUT_HEIGHT"] = "640"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import ffmpeg  # noqa: E402


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    proc = ffmpeg.run([
        "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=40",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
        "-af", "volume='if(between(t,18,19.5),0,1)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])
    assert proc.returncode == 0, proc.stderr
    return path


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _wait(client, url, done, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(url).json()
        if done(data):
            return data
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {url}: {data}")


@pytest.fixture(scope="module")
def project(client, sample_video):
    res = client.post(
        "/api/projects/upload",
        params={"filename": "My Test Video.mp4", "target_length": 15},
        content=sample_video.read_bytes(),
    )
    assert res.status_code == 201, res.text
    pid = res.json()["id"]
    data = _wait(client, f"/api/projects/{pid}", lambda d: d["status"] in {"ready", "failed"})
    assert data["status"] == "ready", data
    return data


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["face_detector"].startswith("yunet")


def test_project_ready_with_clips(project):
    assert project["name"] == "My Test Video"
    assert project["media"]["width"] == 640 and project["media"]["has_audio"]
    clips = project["clips"]
    assert len(clips) >= 2
    assert clips[0]["start"] == 0 and clips[-1]["end"] == pytest.approx(40, abs=0.1)
    # The 1.5 s gap in the tone is the most natural boundary near the 15 s target.
    assert any(abs(c["end"] - 18.75) < 0.5 for c in clips)


def test_video_supports_range_requests(client, project):
    res = client.get(f"/api/projects/{project['id']}/video", headers={"Range": "bytes=0-99"})
    assert res.status_code == 206
    assert len(res.content) == 100


def test_camera_path(client, project):
    path = client.get(f"/api/projects/{project['id']}/camera-path").json()
    assert len(path["times"]) == len(path["xs"]) > 10


def test_edit_clips_and_validation(client, project):
    pid = project["id"]
    clips = project["clips"]
    clips[0]["end"] = 12.5  # shorten
    res = client.put(f"/api/projects/{pid}/clips", json={"clips": clips})
    assert res.status_code == 200
    assert res.json()["clips"][0]["end"] == 12.5

    bad = [dict(clips[0], end=999)]
    assert client.put(f"/api/projects/{pid}/clips", json={"clips": bad}).status_code == 422


def test_resegment(client, project):
    res = client.post(f"/api/projects/{project['id']}/resegment", json={"target_length": 10})
    assert res.status_code == 200
    assert len(res.json()["clips"]) >= 3


def test_export_single_and_all(client, project):
    pid = project["id"]
    clips = client.get(f"/api/projects/{pid}").json()["clips"]

    job = client.post(f"/api/projects/{pid}/exports", json={"clip_ids": [clips[0]["id"]]}).json()
    job = _wait(client, f"/api/exports/{job['id']}", lambda d: d["status"] in {"done", "failed"})
    assert job["status"] == "done", job
    res = client.get(f"/api/exports/{job['id']}/download")
    assert res.status_code == 200 and res.headers["content-type"] == "video/mp4"
    assert job["file_name"].endswith(".mp4")

    out = Path(tempfile.mkdtemp()) / "clip.mp4"
    out.write_bytes(res.content)
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (360, 640), "output must be 9:16"
    assert info.has_audio
    assert info.duration == pytest.approx(clips[0]["end"] - clips[0]["start"], abs=0.2)

    # "Fit" framing: whole frame over a blurred background, still 9:16.
    fit = [dict(c, framing="fit") if i == 1 else c for i, c in enumerate(clips)]
    assert client.put(f"/api/projects/{pid}/clips", json={"clips": fit}).status_code == 200
    job = client.post(f"/api/projects/{pid}/exports", json={"clip_ids": [clips[1]["id"]]}).json()
    job = _wait(client, f"/api/exports/{job['id']}", lambda d: d["status"] in {"done", "failed"})
    assert job["status"] == "done", job
    out.write_bytes(client.get(f"/api/exports/{job['id']}/download").content)
    assert (ffmpeg.probe(out).width, ffmpeg.probe(out).height) == (360, 640)

    ids = [c["id"] for c in clips]
    job = client.post(f"/api/projects/{pid}/exports", json={"clip_ids": ids}).json()
    job = _wait(client, f"/api/exports/{job['id']}", lambda d: d["status"] in {"done", "failed"})
    assert job["status"] == "done", job
    res = client.get(f"/api/exports/{job['id']}/download")
    assert res.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert len(names) == len(ids) and all(n.endswith(".mp4") for n in names)


def test_rejects_bad_input(client):
    assert client.post("/api/projects/upload", params={"filename": "x.exe"}, content=b"MZ").status_code == 415
    res = client.post("/api/projects/upload", params={"filename": "fake.mp4"}, content=b"not a video")
    pid = res.json()["id"]
    data = _wait(client, f"/api/projects/{pid}", lambda d: d["status"] in {"ready", "failed"})
    assert data["status"] == "failed" and "video" in data["error"].lower()
    assert client.post("/api/projects/youtube", json={"url": "https://evil.com/v"}).status_code == 422
    assert client.get("/api/projects/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/projects/" + "0" * 32).status_code == 404
