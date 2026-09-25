# ClipForge: Long Videos → Vertical Clips

ClipForge turns a long horizontal (16:9) video into short vertical (9:16) clips for TikTok, Reels and Shorts.
It splits the video at natural pauses, **tracks the speaker** so they stay in frame (no fixed center crop),
lets you fine-tune every clip in a visual editor, and exports 1080×1920 MP4 files one by one or all at once as a ZIP.

It runs **100% locally** and uses only free, open-source tools: no API keys, no accounts, no paid services.

---

## ✅ Requirements checklist

| # | Requirement | How ClipForge does it |
|---|---|---|
| 1 | Accept a YouTube URL or an uploaded video file (16:9) | Drag & drop / browse upload (MP4, MOV, MKV, WEBM, AVI) **or** paste a YouTube link (downloaded with `yt-dlp`, up to 1080p). |
| 2 | Automatically segment the full video, with manual adjustment of start/end | The whole video is covered with clips cut at **pauses in speech** and **scene changes**, near a target length you choose (15-90 s). Adjust with draggable timeline handles (snap to scene cuts), typed times, ±1 s buttons, "set to playhead", split, add, delete or re-split. |
| 3 | Convert 16:9 → 9:16 keeping the subject in frame | Faces are detected with **YuNet** (OpenCV) and followed by a smoothed "virtual camera". Shots with no face follow the main moving element. A live preview shows the result before exporting. Per-clip override: *Auto-track*, *Center* or *Fit + blur*. |
| 4 | Export clips one by one, or all at once | "Export" on any clip downloads an MP4. "Export all" (or select several) downloads a ZIP. |
| 5 | Adjust clip length (extend or shorten) before exporting | "Extend 5s" / "Shorten 5s", length presets (15/30/45/60 s), drag handles, or type exact times. |
| ● | Free & open-source only | FFmpeg, OpenCV, YuNet, yt-dlp, FastAPI, React. See [Tech stack](#tech-stack). |
| ● | Runs locally with clear setup | One double-click launcher (`start.bat` / `start.sh`) or Docker. |
| ● | Usable by a non-developer | Guided UI with drag & drop, progress steps, plain-English errors, autosave, tooltips and keyboard shortcuts. |
| ● | Readable, organized code | Small single-purpose modules, typed API models, 33 automated tests. |

---

## 🚀 Quick start

### Option A: one click (recommended)

**Prerequisites (one-time install):**
- [Python 3.10+](https://www.python.org/downloads/). On Windows, tick **"Add python.exe to PATH"** during installation.
- [Node.js 18+](https://nodejs.org/) (only used once, to build the interface).

FFmpeg does **not** need to be installed: a static build is bundled through the `imageio-ffmpeg` package.

**Run:**
- **Windows:** double-click **`start.bat`**
- **macOS / Linux:** run `./start.sh` in a terminal

The first run installs everything (a few minutes). After that it starts in seconds and opens
**http://localhost:8000** in your browser. If port 8000 is busy, the next free port is used and printed.
Press `Ctrl+C` in the window to stop.

### Option B: Docker

```bash
docker compose up --build
```
Then open http://localhost:8000. Projects are stored in the `clipforge-data` volume.

### Option C: manual (developers)

```bash
# Backend
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt
python -m app                      # http://localhost:8000

# Frontend with hot reload (second terminal)
cd frontend
npm install
npm run dev                        # http://localhost:5173 (proxies /api to :8000)
```

---

## 🎬 How to use it

1. **Add a video.** Drop a file or paste a YouTube link, pick a target clip length, and click **Create vertical clips**.
2. **Wait for the analysis.** A progress screen shows each step (a 10-minute video takes roughly a minute).
3. **Review the clips.** The left player shows the original with the **9:16 crop window** moving over it;
   the phone frame on the right is a **live vertical preview**.
4. **Adjust.** Select a clip, then drag its edges on the timeline, type exact times, or use *Extend / Shorten*.
   Change *Framing* if needed. Changes save automatically.
5. **Export.** Click the download icon on a clip for a single MP4, or **Export all** for a ZIP.

**Keyboard shortcuts:** `Space` play/pause · `←/→` seek 1 s (`Shift` = 5 s) · `I` / `O` set start/end to the playhead ·
`S` split at the playhead · `Alt` while dragging disables snapping.

---

## 🧠 How it works

```
                 ┌──────────── Ingest ────────────┐
 YouTube URL ──► │ yt-dlp (URL allow-list, limits) │
 Upload     ──►  │ streamed to disk (size limit)   │
                 └──────────────┬──────────────────┘
                                ▼
            ┌──────────── Analysis (one pass) ─────────────┐
            │ FFmpeg decodes at 5 fps, 640 px ──► Python    │
            │  • YuNet face detection                       │
            │  • HSV frame difference → scene cuts          │
            │  • motion centroid (no-face fallback)         │
            │ FFmpeg audio → RMS loudness → speech pauses   │
            └──────────────┬───────────────────────────────┘
                           ▼
     Segmenter (pauses > scene cuts > length)    Reframer (per shot: select subject →
     → clips covering the whole video            fill gaps → median + Gaussian → virtual
                                                 camera with dead zone & speed limits)
                           ▼                                   ▼
                     React editor  ◄──── same camera path ────►  Renderer
                 (timeline, live preview)               FFmpeg decode → NumPy crop →
                                                        FFmpeg x264/AAC 1080×1920 → MP4 / ZIP
```

### Segmentation
- **Pauses in speech** are found by measuring loudness in 50 ms windows and marking stretches **10 dB below the
  video's own median loudness**. A fixed threshold such as FFmpeg's `silencedetect=-35dB` found **1** pause on a test
  video with background music; the adaptive threshold found **25**.
- **Scene cuts** use an adaptive content detector (same idea as PySceneDetect's `AdaptiveDetector`): a cut is a spike in
  HSV difference relative to its neighbours, so camera pans are not mistaken for cuts.
- The segmenter walks the timeline and, within `[0.6×, 1.5×]` of the target length, picks the most natural boundary.
  Pauses beat scene cuts (B-roll edits often happen mid-sentence), and a pause that coincides with a cut is best.

### Subject tracking & reframing
- **Detection:** YuNet (OpenCV Zoo, ~230 KB CNN) on 5 fps / 640 px frames. It is much more robust than Haar
  cascades to profiles and small faces (a Haar fallback is kept for unusual OpenCV builds).
- **Subject selection:** bigger, more confident faces win, with a **continuity bonus** for the face near the current
  subject so the camera doesn't jump between people. If all faces fit in the crop, the group is framed together.
- **Smoothing:** short detection gaps are interpolated, a median filter removes outliers, and a zero-phase Gaussian
  smooths the path (the camera starts moving *just before* the subject, like a human operator).
- **Virtual camera:** a **dead zone** ignores small movements, then proportional follow with **velocity and
  acceleration limits**. At a **scene cut the camera re-anchors instantly** instead of panning across the edit.
- **No face?** It follows the main moving element if it moves in most of the shot; otherwise it stays centered.
- The same camera path drives the **browser preview** and the **final render**, so the preview matches the export.

### Rendering
FFmpeg decodes the clip → Python crops each frame (a zero-copy NumPy slice following the camera path) → FFmpeg scales
to 1080×1920 and encodes H.264 + AAC with `+faststart`. Audio is trimmed from the source with the same timestamps.
Renders are cached by a hash of (start, end, framing, encoder settings), so "Export all" reuses clips already exported.

---

## 🔐 Security & robustness

| Concern | Mitigation |
|---|---|
| SSRF / abuse through the URL field | Only YouTube hostnames are accepted; the URL is parsed and rebuilt as a canonical `watch?v=<id>`; playlists disabled. |
| Huge or malicious uploads | Size limit enforced **while streaming** (not after), extension allow-list, and the file must decode as video or it's rejected. |
| Path traversal | Client file names are never used on disk; project IDs must be 32-hex, clip IDs 8-hex, both validated. |
| Command injection | FFmpeg is always called with argument lists, never through a shell. |
| Resource exhaustion | Max video length (default 3 h), max 1080p download, bounded worker pools, max 300 clips. |
| Information leaks | Unhandled errors return a generic message (details stay in server logs); security headers (`nosniff`, `DENY` framing). |
| Exposure | Binds to `127.0.0.1` by default; CORS limited to the dev server; Docker runs as a non-root user. |
| Data integrity | Atomic JSON writes (temp file + rename); interrupted jobs are marked failed on restart; old projects auto-deleted after 72 h. |

---

## ⚙️ Configuration

All settings are optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `CLIPFORGE_PORT` | `8000` | Preferred port (falls back to the next free one) |
| `CLIPFORGE_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` inside Docker) |
| `CLIPFORGE_DATA_DIR` | `./data` | Where projects, renders and exports are stored |
| `CLIPFORGE_MAX_UPLOAD_MB` | `4096` | Upload / download size limit |
| `CLIPFORGE_MAX_VIDEO_MINUTES` | `180` | Longest accepted video |
| `CLIPFORGE_YOUTUBE_MAX_HEIGHT` | `1080` | Max YouTube resolution |
| `CLIPFORGE_OUTPUT_WIDTH` / `_HEIGHT` | `1080` / `1920` | Export resolution |
| `CLIPFORGE_X264_PRESET` / `_CRF` | `veryfast` / `20` | Encoder speed / quality |
| `CLIPFORGE_PROJECT_TTL_HOURS` | `72` | Auto-delete old projects (0 = never) |
| `CLIPFORGE_OPEN_BROWSER` | `1` | Open the browser on start |

---

## 🧪 Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q      # macOS/Linux: .venv/bin/python -m pytest -q
```

33 tests cover segmentation, scene detection, camera smoothing (jitter, cuts, panning, bounds, no-face fallback),
URL validation (including SSRF attempts), clip validation, and a full **end-to-end API run**: a synthetic video is
uploaded, analysed, edited, re-split and exported (single MP4, *Fit* framing and ZIP), and the output is checked to be
9:16 with audio and the right duration. No network access is needed.

Manually verified end to end in the browser with a 4.8-minute talking-head video (uploaded) and a TEDx talk
(YouTube link): processing, timeline editing, live preview, single export and ZIP export, on desktop and mobile widths.

```bash
cd frontend
npm run build    # type-check + production build
npm run lint
```

---

## 🗂️ Project structure

```
backend/
  app/
    main.py              FastAPI app, security headers, serves the built UI
    config.py            Environment-based settings
    schemas.py           Pydantic models (validation lives here)
    api/                 HTTP routes: projects.py (ingest, clips), exports.py
    core/                storage.py (atomic JSON store), pipeline.py (ingest jobs), exports.py (render jobs)
    services/            Pure video logic, no HTTP:
      ffmpeg.py            FFmpeg discovery / probing
      downloader.py        yt-dlp + URL validation
      analyzer.py          one-pass faces / scenes / motion + pause detection
      face_detector.py     YuNet with Haar fallback
      segmenter.py         automatic clip boundaries
      reframer.py          subject selection + virtual camera path
      renderer.py          9:16 render pipeline
  models/                YuNet ONNX weights (MIT)
  tests/                 unit + end-to-end API tests
frontend/
  src/
    lib/                 API client, time helpers, camera-path interpolation
    hooks/               routing, export polling
    components/home/     upload / YouTube import, recent projects
    components/editor/   player + crop overlay, live 9:16 preview, timeline, clip list, inspector, export tray
start.bat / start.sh     One-click launchers
Dockerfile, docker-compose.yml
```

---

## 🧰 Tech stack

All free and open source:

| Layer | Tools (license) |
|---|---|
| Video | FFmpeg (LGPL/GPL, via `imageio-ffmpeg`), OpenCV (Apache-2.0), YuNet model (MIT), NumPy (BSD) |
| Download | yt-dlp (Unlicense) |
| Backend | Python, FastAPI (MIT), Uvicorn (BSD), Pydantic (MIT) |
| Frontend | React 19 (MIT), TypeScript, Vite (MIT), Tailwind CSS 4 (MIT), Lucide icons (ISC) |

---

## ⚖️ Trade-offs & limitations

- **Local JSON store instead of a database:** a single-user local tool doesn't need one, and files stay inspectable. A
  multi-user deployment would move to Postgres plus a proper job queue (e.g. Redis/RQ) for horizontal scaling.
- **In-process thread pools instead of a job queue:** simple and dependency-free. Running jobs don't survive a
  restart (they are marked as failed with a clear message).
- **Python frame piping for the render:** FFmpeg's `crop` filter can't follow an arbitrary per-frame path without
  enormous expressions. Piping costs a little speed (~3× real-time on a laptop for 1080p) but is exact and simple.
- **Speaker choice by face size and continuity**, not by who is talking. Active-speaker detection (lip movement +
  audio) would improve two-person podcasts; users can switch a clip to *Center* or *Fit + blur* meanwhile.
- **Segmentation is structural (pauses and cuts), not semantic.** A free local speech-to-text model (e.g. Whisper)
  could add sentence-aware cuts and captions as a next step.
- **YouTube** can rate-limit or change its site; keeping `yt-dlp` up to date (`pip install -U yt-dlp`) fixes most issues.
  Only process videos you have the rights to use.
