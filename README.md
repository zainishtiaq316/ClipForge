# ClipForge: Long Videos → Vertical Clips

ClipForge turns a long horizontal (16:9) video into short vertical (9:16) clips for TikTok, Reels and Shorts.
It splits the video at natural pauses, **tracks the speaker** so they stay in frame (no fixed center crop),
**keeps on-screen text readable** (captions and titles are re-wrapped into the vertical frame in their original style
instead of being cut in half), lets you **zoom out** to show more of the scene,
lets you fine-tune every clip in a visual editor, and exports 1080×1920 MP4 files one by one or all at once as a ZIP.

It runs **100% locally** and uses only free, open-source tools: no API keys, no accounts, no paid services.

---

## ✅ Requirements checklist

| # | Requirement | How ClipForge does it |
|---|---|---|
| 1 | Accept a YouTube URL or an uploaded video file (16:9) | Drag & drop / browse upload (MP4, MOV, MKV, WEBM, AVI) **or** paste a YouTube link (downloaded with `yt-dlp`, up to 1080p). |
| 2 | Automatically segment the full video, with manual adjustment of start/end | The whole video is covered with clips cut at **pauses in speech** and **scene changes**, near a target length you choose (15-90 s). Adjust with draggable timeline handles (snap to scene cuts), typed times, ±1 s buttons, "set to playhead", split, add, delete or re-split. |
| 3 | Convert 16:9 → 9:16 keeping the subject (face **or key element**) in frame | Faces are detected with **YuNet** and followed by a smoothed "virtual camera". In medium and wide shots the camera frames the **whole person** (NanoDet body box, arms and hands included) and **zooms out automatically** with solid bars when they don't fit a full-frame 9:16. Shots with no face follow the main moving element. **On-screen text** (captions, numbered tips, lower thirds, end screens) is detected with **PP-OCRv3**. If it fits in the 9:16 window, the window shifts to include it; if it's wider, the original text pixels are re-wrapped into 2-3 lines in the **same font, colour and caption box** and placed back into the frame. Never blurred. A per-clip **zoom-out** slider shows more of the scene (e.g. an object in the speaker's hand) with solid bars. A live preview shows the result before exporting. |
| 4 | Export clips one by one, or all at once | "Export" on any clip downloads an MP4. "Export all" (or select several) downloads a ZIP. |
| 5 | Adjust clip length (extend or shorten) before exporting | "Extend 5s" / "Shorten 5s", length presets (15/30/45/60 s), drag handles, or type exact times. |
| ● | Free & open-source only | FFmpeg, OpenCV, YuNet, yt-dlp, FastAPI, React. See [Tech stack](#tech-stack). |
| ● | Runs locally with clear setup | One double-click launcher (`start.bat` / `start.sh`) or Docker. |
| ● | Usable by a non-developer | Guided UI with drag & drop, progress steps, plain-English errors, autosave, tooltips and keyboard shortcuts. |
| ● | Readable, organized code | Small single-purpose modules, typed API models, 55 automated tests, lint-clean (ruff, oxlint). |

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
2. **Wait for the analysis.** A progress screen shows each step (a 10-minute video takes about two minutes).
3. **Review the clips.** The left player shows the original with the **9:16 crop window** moving over it;
   the phone frame on the right is a **live vertical preview**.
4. **Adjust.** Select a clip, then drag its edges on the timeline, type exact times, or use *Extend / Shorten*.
   Change *Framing* if needed. Changes save automatically. Blue marks above the timeline show on-screen text.

   | Framing | What it does |
   |---|---|
   | **Smart** (default) | Follows the speaker and keeps on-screen text whole (moves the frame to include it, or re-wraps it) |
   | **Track** | Follows the speaker or main moving element only; text may be cut |
   | **Center** | Fixed crop from the middle of the frame |

   **Zoom out** (0-100%) widens the view for any framing: 0% is a full-frame vertical video, higher values show more
   of the scene with solid black bars above and below (re-wrapped text moves onto the bottom bar). With *Smart*
   framing the app also zooms out on its own when a person with outstretched arms doesn't fit; the slider can only add
   to that.
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
            │  • PP-OCRv3 on-screen text (every 0.4 s)      │
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
- **Whole-person framing & auto zoom:** NanoDet (OpenCV Zoo, 3.8 MB) finds each person's full body box, arms and
  hands included. In medium and wide shots the camera frames that box instead of just the face. If the person is
  wider than a full-height 9:16 window (arms out, a camera held at arm's length) the shot zooms out just enough, with
  solid black bars. The zoom is one value per shot (the width needed 80% of the time), so it never "breathes".
  Close-ups (face wider than 11% of the frame) stay full-frame and face-centred. Title cards without a face zoom out to
  show all of their text. The per-clip slider can zoom out further, never less.

### On-screen text (never cut, never blurred)
- **Which text:** only overlay *lines* count (wide and thin, at least 2.5% of the frame tall, on screen for at least
  1.5 s), so T-shirt prints, logos and background signs are ignored. On the test video all 6 overlays were found with
  no false positives.
- **Plan (analysis time):** each overlay is measured once on a full-resolution frame: its rectangle, whether it sits on a
  solid caption box (and the box colour), the positions of word gaps, and a tiny fingerprint of how it looks.
- **Render time, in order of preference:**
  1. The text already fits in the 9:16 window → nothing to do.
  2. It fits if the window moves a little → the window eases over (the subject stays well inside). For slides and end
     cards (several stacked lines) the text itself becomes the subject.
  3. Otherwise → **re-flow**: the half-cut original is erased (filled with the box colour, or inpainted when the text
     sits on video), and the original text pixels are wrapped at word gaps into the fewest lines that keep it at
     85% or more of its natural size (balanced line widths), then placed back where the text was, with its caption
     box. Because the real pixels are reused (no OCR, no re-typing), font, colour and animation are preserved.
- The fingerprint check makes sure text is only moved while it is actually on screen (not while fading or sliding in).
- **Only the letters move:** text without a caption box is lifted by its own colour (measured when planning), inside
  the detected text area only, and only through a **letter stencil** (the letter shapes present in three frames of the
  overlay's run), so a shirt, a hand or the stage behind it stays behind, even when someone walks behind the text. Captions *with* a box keep
  their box, since the box is part of their style.
- Texts that share a row in the original (like "July 2017 … Denver Colorado") stay side by side on one row.
- Big display type (letters taller than 13% of the frame, e.g. an animated title card) is never cut into pieces;
  the card's own shot is framed to show it whole.
- The same camera path drives the **browser preview** and the **final render**, so the preview matches the export.

### Rendering
FFmpeg decodes the clip → Python crops each frame (a zero-copy NumPy slice following the camera path) → FFmpeg scales
to 1080×1920 and encodes H.264 + AAC with `+faststart`. Re-flowed text is drawn at crop resolution (so those clips are
as fast as plain crops, ~3× real time); zoomed-out clips are composed at 1080×1920 with OpenCV (~1.2× real time). Audio is trimmed from the source with the same timestamps. Renders are cached by a hash of
(start, end, framing, zoom, encoder settings), so "Export all" reuses clips already exported. The browser preview runs a
TypeScript port of the same geometry (`frontend/src/lib/textLayout.ts`), so what you see is what you export.

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

55 tests cover segmentation, scene detection, camera smoothing (jitter, cuts, panning, bounds, no-face fallback),
on-screen text (overlay detection, ignored T-shirt text and flashes, balanced wrapping, scaling, window shifting,
slides as subject, zoom-out bar placement), whole-person auto zoom (wide shots zoom, close-ups don't, title cards
fit, no zoom breathing), word gaps vs letter spacing, zoom geometry, the real text detector,
URL validation (including SSRF attempts), clip validation, and a full **end-to-end API run**: a synthetic video is
uploaded, analysed, edited, re-split and exported (single MP4, zoomed-out clip and ZIP), and the output is checked to be
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
      person_detector.py   NanoDet whole-body boxes (arms and hands included)
      text_detector.py     PP-OCRv3 on-screen text detection
      text_layout.py       plans and re-flows on-screen text into the vertical frame
      segmenter.py         automatic clip boundaries
      reframer.py          subject selection + virtual camera path
      renderer.py          9:16 render pipeline
  models/                YuNet (MIT), NanoDet and PP-OCRv3 (Apache-2.0) ONNX weights
  tests/                 unit + end-to-end API tests
frontend/
  src/
    lib/                 API client, time helpers, camera path + text layout geometry (mirrors the renderer)
    hooks/               routing, export polling
    components/home/     upload / YouTube import, recent projects
    components/editor/   player + crop overlay, live 9:16 preview, timeline, clip list, inspector, export tray
docs/ENGINEERING_NOTES.md  Design decisions, trade-offs and security in depth
start.bat / start.sh     One-click launchers
Dockerfile, docker-compose.yml
```

---

## 🧰 Tech stack

All free and open source:

| Layer | Tools (license) |
|---|---|
| Video | FFmpeg (LGPL/GPL, via `imageio-ffmpeg`), OpenCV (Apache-2.0), YuNet face model (MIT), NanoDet person model (Apache-2.0), PP-OCRv3 text model (Apache-2.0), NumPy (BSD) |
| Download | yt-dlp (Unlicense) |
| Backend | Python, FastAPI (MIT), Uvicorn (BSD), Pydantic (MIT) |
| Frontend | React 19 (MIT), TypeScript, Vite (MIT), Tailwind CSS 4 (MIT), Lucide icons (ISC) |

---

## ⚖️ Trade-offs & limitations

The full reasoning is in **[docs/ENGINEERING_NOTES.md](docs/ENGINEERING_NOTES.md)**. In short:

- **Local JSON store instead of a database:** a single-user local tool doesn't need one, and files stay inspectable. A
  multi-user deployment would move to Postgres plus a proper job queue (e.g. Redis/RQ) for horizontal scaling.
- **In-process thread pools instead of a job queue:** simple and dependency-free. Running jobs don't survive a
  restart (they are marked as failed with a clear message).
- **Python frame piping for the render:** FFmpeg's `crop` filter can't follow an arbitrary per-frame path without
  enormous expressions. Piping costs a little speed (~3× real-time on a laptop for 1080p) but is exact and simple.
- **A full-frame 9:16 from 16:9 can only be about 32% of the width.** Showing more (a person plus the camera in
  their hand) needs extra height the video doesn't have, so zooming out adds bars. I chose solid bars controlled by the
  user over blurred fills or automatic zooming, so every frame is either full-frame or a deliberate choice.
- **Re-flowed text instead of a smaller frame:** captions keep their own style and the face stays big. The trade-off
  is that the original text is erased (inpainted when it sits on video, which can leave a soft patch on busy
  backgrounds), and very long lines without word gaps are scaled down instead of wrapped.
- **Speaker choice by face size and continuity**, not by who is talking. Active-speaker detection (lip movement +
  audio) would improve two-person podcasts; users can switch a clip to *Center* or zoom out meanwhile.
- **Segmentation is structural (pauses and cuts), not semantic.** A free local speech-to-text model (e.g. Whisper)
  could add sentence-aware cuts and captions as a next step.
- **YouTube** can rate-limit or change its site; keeping `yt-dlp` up to date (`pip install -U yt-dlp`) fixes most issues.
  Only process videos you have the rights to use.
