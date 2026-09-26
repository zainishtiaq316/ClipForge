# Engineering Notes: Design Decisions, Trade-offs & Security

This document explains *why* ClipForge is built the way it is: what I optimised for, what I deliberately
did **not** do, and how the tool is hardened. It is meant to be read alongside the [README](../README.md).

---

## 1. Goals I optimised for

1. **Correct output first.** The subject stays in frame, text is never cut in half, clips don't stop mid-sentence,
   and audio stays in sync.
2. **Runs on any laptop, for free.** No GPU, no API keys, no paid services, no system-wide installs (FFmpeg ships as a Python wheel).
3. **Usable by a non-developer.** One-click start, drag & drop, plain-English errors, autosave, and a live preview so
   nobody has to export just to see the result.
4. **Production discipline in a small codebase.** Input validation, bounded resources, atomic writes, tests, and small
   single-purpose modules that another developer can pick up quickly.

---

## 2. Key design decisions & trade-offs

### 2.1 Analyse once, at low resolution
**Decision:** decode the video once at **5 fps / 640 px** and extract every signal (faces, scene changes, motion, text)
in that single pass. Audio is analysed in a separate, very cheap pass.

- ✅ A 20-minute 1080p video decodes ~6 000 small frames instead of ~36 000 full-size ones. Analysis runs several times
  faster than real time on a laptop CPU.
- ❌ Very fast motion between samples (200 ms apart) is interpolated, not measured. That's fine for talking heads and
  interviews, the target content. Sports or action footage would need a higher sample rate (it's a config value).
- **Text detection runs every 0.4 s** instead of on every sample: text changes slowly, and the detector is the most
  expensive model (~40 ms per frame).

### 2.2 YuNet for faces instead of Haar cascades or heavy detectors
- **Haar cascades** (OpenCV's classic) are fast but miss profile views and small faces and give many false positives.
- **YOLO / MediaPipe** are accurate but add large dependencies, model downloads, or version constraints.
- **YuNet** (OpenCV Zoo, MIT, ~230 KB) is accurate on the WIDER Face benchmark, runs in ~15 ms per frame on CPU, and
  needs nothing beyond OpenCV. A Haar fallback keeps the tool working on unusual OpenCV builds.

### 2.3 A "virtual camera operator", not raw face coordinates
Following the detected face frame by frame looks jittery and robotic. The reframer works like a camera operator:
- **Median + zero-phase Gaussian smoothing** remove detector noise. Zero-phase means the camera starts moving slightly
  *before* the subject, as a human operator anticipates.
- A **dead zone** ignores small movements, then the camera follows proportionally with **speed and acceleration limits**.
- At a **scene cut** the camera **jumps immediately** instead of panning across the edit.
- **Subject selection with continuity bonus:** avoids ping-ponging between two people. If everyone fits in the crop,
  the group is framed together.

*Trade-off:* the speaker is chosen by face size and continuity, not by who is talking. Active-speaker detection
(lip motion + audio) would improve two-person podcasts but adds complexity and processing time. The per-clip
framing override covers those cases for now.

### 2.4 Text-aware layout ("key element" is not only faces)
The brief asks to keep "the face **or key element**" in frame. On-screen text (captions, numbered tips, slides, title
cards, end screens) is often the key element, and it's usually **wider than a 9:16 window**, so a face crop cuts it
in half.

**Decision:** detect text with the PP-OCRv3 detector (OpenCV Zoo, Apache-2.0, 2.4 MB). When a line of text is wider
than the crop window, or a shot has text but no face, those seconds switch to a **fit layout**: the whole frame over a
blurred copy of itself.

Rules that keep this from misfiring:
- Only **lines** of text count (wide and thin), so T-shirt prints, logos and background signs are ignored.
- Text shorter than **1 s** is ignored; gaps under **1.5 s** are bridged so the layout doesn't flicker.
- Switches are **snapped outward to scene cuts** (a switch on an edit looks intentional). Snapping never shrinks a range,
  because that would cut text that is already on screen.
- Switches mid-shot use a **0.25 s crossfade**; switches on a cut are hard cuts.

*Trade-off:* in fit layout the speaker is smaller for those seconds. I chose readable text over a bigger face because
cut text looks broken, while a brief layout change looks deliberate. Users who disagree can pick **Track** framing
(always crop) per clip.

*Measured on the test video:* all 6 detected ranges were real overlays (5 numbered tips + end screen), with 0 false
positives from clothing or background text.

### 2.5 Segmentation: structure over semantics
**Decision:** cut at **pauses in speech** (preferred) and **scene cuts**, as close as possible to the target length.

- Pauses are found with an **adaptive threshold**: 10 dB below the video's own median loudness. A fixed threshold such
  as `silencedetect=-35dB` found **1** pause on a video with background music; the adaptive one found **25**.
- A pause beats a scene cut, because B-roll edits often happen mid-sentence.

*Trade-off:* this is structural, not semantic. It doesn't know which moment is the most "viral". Adding a free local
speech-to-text model (Whisper) would allow sentence-aware cuts, captions and topic-based clipping. I left it out
because it adds a large model download and multiplies processing time, and the brief asks for segmentation, not
highlight ranking.

### 2.6 Rendering: FFmpeg → Python → FFmpeg
FFmpeg's `crop` filter can't follow an arbitrary per-frame path without enormous expressions, and it can't switch
layouts with crossfades cleanly. So FFmpeg decodes, Python positions each frame, and FFmpeg encodes.

- **Fast path** (crop only): Python just slices the array (zero-copy) and FFmpeg upscales. About 3× faster than real time.
- **Composed path** (clips with fit sections): Python composes 1080×1920 frames with OpenCV. The blur is done on a
  1/8-size copy for the same look at ~1/64 of the cost.
- **Same data for preview and export:** the browser preview reads the exact camera path and fit ranges the renderer
  uses, so there are no surprises after export.
- Renders are **cached** by a hash of every input that affects the output, so "Export all" after exporting one clip
  doesn't re-encode it.

### 2.7 Storage & jobs: deliberately simple
- **JSON files per project** instead of a database. A local single-user tool doesn't need one, the state stays
  human-readable, and writes are atomic (temp file + rename).
- **In-process thread pools** instead of Celery/Redis: zero extra services to install. Ingest runs 2 jobs in parallel;
  exports run 1 at a time because x264 already uses every CPU core.
- *Trade-off:* jobs don't survive a server restart (they're marked failed with a clear message), and the app doesn't
  scale horizontally. For a multi-user internal service I'd move to Postgres, object storage (S3/GCS) and a job queue,
  with workers in separate containers. The service layer (`app/services/`) has no HTTP or storage code in it, so that
  move wouldn't touch the video logic.

### 2.8 Frontend
- **React + Vite + Tailwind**, no UI kit: fast to load, and every interaction (timeline drag, snapping, live canvas
  preview) is custom-built for this workflow.
- The **playhead, crop overlay and 9:16 preview update through `requestAnimationFrame`**, not React state, so
  playback stays smooth at 60 fps.
- **Autosave** (debounced) plus a flush before every export: the server never renders stale clip times.
- A **hash router** instead of a routing library: two screens don't justify a dependency.

---

## 3. Security

The tool processes untrusted input (arbitrary files and URLs), so it's treated as hostile by default.

| Threat | What could happen | Mitigation |
|---|---|---|
| **SSRF via the URL field** | yt-dlp supports 1 800+ sites and can fetch arbitrary URLs. An open URL field could be pointed at internal services (`http://169.254.169.254/…`, `localhost` admin panels). | **Host allow-list** (YouTube domains only). The URL is parsed, the 11-char video ID is validated, and a canonical `https://www.youtube.com/watch?v=<id>` is rebuilt. The user's raw string is never passed to yt-dlp. Playlists are disabled. Tests cover metadata IPs, look-alike domains (`youtube.com.evil.com`), `file://` URLs and playlists. |
| **Oversized uploads / disk exhaustion** | A multi-GB upload fills the disk. | Size limit enforced **while streaming** (the upload is aborted at the limit, not after). `Content-Length` is checked up front. The same limit applies to YouTube downloads (`max_filesize`), plus a duration limit. Old projects are auto-deleted (TTL). |
| **Malicious / non-video files** | A renamed executable or crafted file reaches the pipeline. | Extension allow-list, then the file must be decodable by FFmpeg as a video stream, or the project fails with a friendly message. |
| **Path traversal** | `../../` in a file name or ID reads or writes outside the data folder. | Client file names are **never** used on disk (stored as `source.<ext>`). Project IDs must be 32 hex chars and clip IDs 8 hex chars, validated before any path is built. The static-file handler checks `is_relative_to(dist)`. |
| **Command injection** | A crafted title or URL executes shell commands. | FFmpeg is always called with **argument lists**, never `shell=True`. No user text is ever put into a command string. |
| **Resource exhaustion (CPU)** | Many parallel jobs freeze the machine. | Bounded worker pools, max 300 clips per project, max video length, and 1080p download cap. |
| **Information disclosure** | Stack traces reveal file paths and internals. | A global exception handler returns a generic message; details go to server logs only. Pydantic validation messages are user-safe. |
| **Clickjacking / MIME sniffing** | The UI is embedded in a hostile page, or a file is mis-sniffed. | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`. |
| **Network exposure** | Other machines on the network use the tool. | Binds to **127.0.0.1** by default. CORS only allows the dev server origin. In Docker the port is published on `127.0.0.1` and the process runs as a **non-root** user. |
| **Data corruption** | A crash mid-write leaves broken state. | Atomic JSON writes (temp + rename). Interrupted jobs are marked failed on restart instead of hanging forever. |
| **Copyright / terms of use** | Users process content they don't own. | The UI states that only videos you have rights to should be used. The tool does not bypass DRM. |

**Not in scope for a local tool (would be added for a shared deployment):** authentication (e.g. SSO), per-user
quotas and rate limiting, virus scanning of uploads, HTTPS termination, audit logs, and signed download URLs.

---

## 4. Testing strategy

- **Unit tests** for the pure logic: segmentation, scene-cut detection, camera smoothing (jitter, cuts, panning,
  bounds), no-face fallback, text-aware layout rules, and URL and clip validation, including SSRF attempts.
- **Model test:** the real text detector must find a caption on a synthetic frame.
- **End-to-end API test:** a synthetic video with a gap in the audio is uploaded, analysed, edited, re-split and
  exported (single MP4, *Fit* framing, and ZIP). The outputs are probed: 9:16, audio present, correct duration.
  No network needed.
- **Manual end-to-end:** real talking-head video (upload) and TEDx talk (YouTube link), in the browser, desktop and
  mobile widths. A fresh clone was started with `start.bat` to verify the non-developer path.

---

## 5. What I'd do next

1. **Active-speaker detection** for podcasts (mouth movement correlated with audio).
2. **Whisper transcription** for sentence-aware cuts, burned-in captions and highlight ranking.
3. **Job queue + object storage** if this becomes a shared internal service.
4. **Per-clip manual keyframes** (drag the crop window in the preview) for the rare cases automation gets wrong.
