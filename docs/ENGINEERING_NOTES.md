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

### 2.2 Small OpenCV Zoo models instead of heavy frameworks
- **Haar cascades** (OpenCV's classic) are fast but miss profile views and small faces and give many false positives.
- **YOLO / MediaPipe** are accurate but add large dependencies, model downloads, or version constraints.
- **YuNet** (OpenCV Zoo, MIT, ~230 KB) is accurate on the WIDER Face benchmark, runs in ~15 ms per frame on CPU, and
  needs nothing beyond OpenCV. A Haar fallback keeps the tool working on unusual OpenCV builds.
- The same reasoning picked **NanoDet-Plus** (3.8 MB, ~20 ms) for whole-body person boxes and **PP-OCRv3** (2.4 MB)
  for text. All three are ONNX files run by OpenCV's DNN module: no PyTorch, no TensorFlow, no GPU, and 6.5 MB of
  models in total. The heavier two run every 0.4 s instead of on every sample.

### 2.3 A "virtual camera operator", not raw face coordinates
Following the detected face frame by frame looks jittery and robotic. The reframer works like a camera operator:
- **Median + zero-phase Gaussian smoothing** remove detector noise. Zero-phase means the camera starts moving slightly
  *before* the subject, as a human operator anticipates.
- A **dead zone** ignores small movements, then the camera follows proportionally with **speed and acceleration limits**.
- At a **scene cut** the camera **jumps immediately** instead of panning across the edit.
- **Subject selection with continuity bonus:** avoids ping-ponging between two people. If everyone fits in the crop,
  the group is framed together.
- **Whole person, not just the face:** NanoDet (OpenCV Zoo, Apache-2.0, 3.8 MB, ~20 ms per frame) gives each person's
  full body box, arms and hands included, every 0.4 s. In medium and wide shots the camera centres that box (the face
  keeps a 12% margin). Close-ups (face wider than 11% of the frame) stay face-centred, because cutting the shoulders
  is normal in vertical video.

*Trade-off:* the speaker is chosen by face size and continuity, not by who is talking. Active-speaker detection
(lip motion + audio) would improve two-person podcasts but adds complexity and processing time. The per-clip
framing override covers those cases for now.

### 2.4 On-screen text: re-flow it, never cut it, never blur it
The brief asks to keep "the face **or key element**" in frame. On-screen text (captions, numbered tips, lower thirds,
end cards) is often the key element, and it's usually **wider than a 9:16 window**, so a face crop cuts it in half.

My first version switched those moments to a "fit" layout (whole frame over a blurred background). It kept the
text readable, but the speaker became small and the blur looked like a fallback. The final version keeps the frame
full and moves the *text* instead.

**Plan (once per overlay, at analysis time).** PP-OCRv3 (OpenCV Zoo, Apache-2.0, 2.4 MB) finds text lines every 0.4 s.
Consecutive detections of the same lines form a run; each run is measured once on a full-resolution frame:
- the line rectangle, and whether it sits on a **solid caption box** (uniform pixels around and between the letters;
  the box's real edges are found by growing outwards while the colour matches);
- **word gaps** (ink-free column runs), so a line can be wrapped between words;
- a 48×8 grayscale **fingerprint**, used at render time to confirm the text is really on screen.

**Render (per frame), in order of preference:**
1. Text already inside the crop window → untouched.
2. Text fits if the window moves → the window eases over in 0.3 s, as long as the subject keeps a 15% margin. For
   slides and end cards (two or more stacked lines) the text itself is the subject, so the margin rule is dropped.
3. Otherwise **re-flow**: erase the half-cut original (fill with the box colour, or OpenCV inpainting when the text
   sits on video), then take the original text pixels, split them at word gaps into the fewest lines that keep the
   text at ≥ 85% of its natural size (choosing the most balanced split), draw the caption box behind them and place
   them where the text was. In a zoomed-out clip, the text moves onto the free bar instead and is enlarged there.

Because the actual pixels are reused (no OCR, no re-typing), the font, colours, caption box and even animations are
preserved. For text without a box only the letters move: they are selected by the letters' own colour (measured at
planning time) and only inside the detected text area, so the person or stage behind them is left behind. On top of that, each caption gets a **letter stencil**: the
intersection of its letter masks on three frames spread over the run. Overlay text stays put while people move, so the
stencil holds exactly the letters; pasting only through it means a hand or a white sleeve crossing the caption is
never moved with it. Texts sharing a row stay on one row,
and big display type (title cards) is never cut up, because the card's shot is zoomed to show it whole. The fingerprint check prevents pasting text while it is fading or sliding in, when the reference
rectangle would contain something else.

*Trade-offs:*
- Erasing text that sits directly on video uses inpainting, which can leave a soft patch on busy backgrounds. Caption
  boxes (the common case) are erased perfectly with their own colour.
- A line with no word gaps (one long word, or tightly kerned script) can't be wrapped, so it is scaled down to fit.
- Only horizontal lines are handled; vertical or curved text isn't re-flowed.

*Measured on the test video:* all 6 overlays were found (5 boxed numbered tips + the end card), 0 false positives
from clothing or background text; long captions wrapped into two balanced lines; the end card became the subject.

### 2.4b Zoom out: solid bars, user-controlled
A full-height 9:16 window from a 16:9 frame is only ~32% of the width (608 of 1920 px). Showing a person *and* the
camera in their hand needs ~825 px of width, which at 9:16 means ~1470 px of height. The video only has 1080, so the
extra room has to be filled with something.

**Decision:** full-frame whenever the subject fits; **zoom out automatically** only when it doesn't, filling the
space above and below with **solid black**, never blur.
- The zoom is decided **per shot**: the width the person needs 80% of the time (so one wild gesture doesn't shrink
  the whole shot), rounded up to 5% steps and capped at 45% so people never become tiny. It stays constant for the
  whole shot and changes only at a scene cut, where a change is invisible. No "breathing" zoom.
- Title cards and end cards without a face zoom to show their widest text (up to the full width).
- A per-clip **slider** lets the user zoom out further (e.g. to show a prop), never less than the automatic value.
- Re-flowed text moves onto the bottom bar and is enlarged there.

My first version only had the manual slider. Testing on real footage showed hands and held objects leaving the frame
in medium shots, so detection-driven zoom became the default in Smart framing. *Track* framing keeps a pure
full-frame crop for anyone who never wants bars.

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
FFmpeg's `crop` filter can't follow an arbitrary per-frame path without enormous expressions, and it can't re-flow
text. So FFmpeg decodes, Python composes each frame, and FFmpeg encodes.

- **Fast path** (crop only): Python just slices the array (zero-copy) and FFmpeg upscales. About 3× faster than real time.
- **Re-flowed text** is composed at crop resolution (the geometry is scale-invariant) and FFmpeg still upscales,
  so these clips render as fast as plain crops. Measured on a laptop: 25 s clip with a caption in 7.5 s (3.4× real
  time); a plain crop 3.1×.
- **Zoomed-out clips** are composed at 1080×1920 (bars + scaled video + text) with OpenCV: about 1.2× real time.
- **Same data for preview and export:** the browser preview uses the exact camera path and text layouts, and a
  TypeScript port of the same geometry (`frontend/src/lib/textLayout.ts`), so there are no surprises after export.
  The only difference: the preview fills erased text with the box colour instead of inpainting it.
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
  bounds), no-face fallback, on-screen text (overlay detection, ignored T-shirt text and flashes, balanced wrapping,
  scaling, window shifting, slides as subject, zoom-out bars), whole-person auto zoom (wide shots zoom, close-ups
  don't, title cards fit, constant within a shot), word gaps vs letter spacing, zoom geometry, legacy data migration,
  and URL and clip
  validation, including SSRF attempts.
- **Model test:** the real text detector must find a caption on a synthetic frame.
- **End-to-end API test:** a synthetic video with a gap in the audio is uploaded, analysed, edited, re-split and
  exported (single MP4, zoomed-out clip, and ZIP). The outputs are probed: 9:16, audio present, correct duration.
  No network needed.
- **Manual end-to-end:** real talking-head video (upload) and TEDx talk (YouTube link), in the browser, desktop and
  mobile widths. A fresh clone was started with `start.bat` to verify the non-developer path.

---

## 5. What I'd do next

1. **Active-speaker detection** for podcasts (mouth movement correlated with audio).
2. **Whisper transcription** for sentence-aware cuts, burned-in captions and highlight ranking.
3. **Job queue + object storage** if this becomes a shared internal service.
4. **Per-clip manual keyframes** (drag the crop window in the preview) for the rare cases automation gets wrong.
