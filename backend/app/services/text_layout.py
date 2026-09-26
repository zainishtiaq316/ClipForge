"""Reflow on-screen text into the vertical frame.

A 9:16 crop of a 16:9 frame cuts wide captions, lower thirds and titles in half.
Instead of shrinking the whole frame, the text itself is moved: the original
text pixels are cut out, re-wrapped at word gaps into up to three lines, scaled
to fit, and placed back into the vertical frame. Because the actual pixels are
reused (no OCR, no re-typing) the font, colours, caption box and even animation
of the original overlay are kept. The half-cut original is erased first: filled
with the caption box colour, or inpainted when the text sits directly on video.

Two stages:

* ``plan_text_layouts`` (analysis time): splits the timeline into runs where the
  same overlay is on screen, then measures each overlay on one full-resolution
  frame: line rectangles, whether they sit on a solid box, the box colour, and
  where the word gaps are.
* ``layout_ops`` (render time, mirrored in the frontend preview): pure geometry
  that turns a planned line plus the current crop window into erase / paste
  rectangles in output pixels.
"""

from __future__ import annotations

import base64
import dataclasses
import itertools
import logging
import math
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from . import ffmpeg
from .analyzer import Analysis
from .text_detector import TextBox, TextDetector

log = logging.getLogger(__name__)

# Which detections count as overlay text (captions, titles), not scene text.
LINE_MIN_ASPECT = 3.5  # a line of text is much wider than tall (ignores logos, T-shirt prints)
LINE_MIN_HEIGHT = 0.025  # ignore tiny text (watermarks, background signs)

# Grouping detections over time into runs of "the same overlay".
RUN_MIN_S = 1.5  # ignore text that flashes by (and scene text caught for a moment)
RUN_MAX_GAP_S = 1.2  # bridge detector dropouts shorter than this
RUN_MATCH_IOU = 0.4
RUN_MATCH_RATIO = 0.5

# Reflow geometry (fractions of the output frame).
MAX_LINE_WIDTH = 0.9
MAX_SEGMENTS = 3
SEGMENT_GAP = 0.18  # vertical gap between wrapped lines, relative to line height
PLATE_PAD = 0.22  # caption-box padding around wrapped lines, relative to line height
SAFE_TOP, SAFE_BOTTOM = 0.05, 0.95
MIN_WRAP_SCALE = 0.85  # prefer fewer lines as long as the text stays at >= 85% of its natural size
ROW_GAP = 0.6  # space between texts that share a row, relative to their height
ROW_MIN_FIT = 0.6  # texts sharing a row may shrink this much to stay on one row
TITLE_LETTER_HEIGHT = 0.13  # letters taller than this are a title graphic, not a caption: never cut it up
BLOB_MAX_HEIGHT = 1.4  # a 'letter' taller than this x the typical letter is something else (a hand)
BAR_TEXT_HEIGHT = 0.055  # text moved onto a zoom-out bar is at least this tall (fraction of output)
WINDOW_SUBJECT_MARGIN = 0.15  # when shifting the window to fit text, the subject stays this far from its edges
WINDOW_RAMP_S = 0.3  # ease the window shift in / out
LETTER_CONTRAST = 50  # grey-level difference that separates letters from a plain background
TEXT_COLOR_TOL = 80  # a pixel belongs to the letters when it is this close to their colour (RGB distance)
BOX_CONTRAST = 28  # a caption box must differ at least this much from what surrounds it
ERASE_COVERED = 0.8  # the re-flowed text hides the erased original when it covers this much of it
PRESENCE_MIN_CORR = 0.5  # the region must still look like the reference text (0..1 correlation)
THUMB_W, THUMB_H = 48, 8  # tiny grayscale fingerprint of each line for that check


@dataclass(frozen=True)
class TextLine:
    """One planned line of overlay text, normalised to the source frame."""

    x: float
    y: float
    w: float
    h: float
    cuts: tuple[float, ...]  # word-gap centres, 0..1 along the line
    boxed: bool  # sits on a solid caption box
    bg: tuple[int, int, int]  # box / background colour, BGR
    ink: float  # fraction of "ink" pixels on the reference frame
    thumb: tuple[int, ...] = ()  # THUMB_W x THUMB_H grayscale fingerprint of the reference text
    fg: tuple[int, ...] = ()  # the letters' own colour (BGR), for text that isn't on a caption box
    core: tuple[float, float] = (0.0, 1.0)  # where the letters are across the rect (the rest is padding)
    stencil: str = ""  # letter shapes over the whole run (packed bits, base64), see _stencil
    stencil_shape: tuple[int, int] = (0, 0)  # rows, cols of the stencil

    def to_dict(self) -> dict:
        data = asdict(self)
        data["cuts"] = [round(c, 4) for c in self.cuts]
        data["bg"] = list(self.bg)
        data["thumb"] = list(self.thumb)
        data["fg"] = list(self.fg)
        data["core"] = [round(c, 4) for c in self.core]
        data["stencil_shape"] = list(self.stencil_shape)
        for key in ("x", "y", "w", "h", "ink"):
            data[key] = round(data[key], 4)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TextLine:
        return cls(
            x=data["x"], y=data["y"], w=data["w"], h=data["h"], cuts=tuple(data["cuts"]),
            boxed=data["boxed"], bg=tuple(data["bg"]), ink=data["ink"],
            thumb=tuple(data.get("thumb", ())), fg=tuple(data.get("fg", ())),
            core=tuple(data.get("core", (0.0, 1.0))),
            stencil=data.get("stencil", ""), stencil_shape=tuple(data.get("stencil_shape", (0, 0))),
        )


@dataclass(frozen=True)
class TextLayout:
    start: float
    end: float
    lines: tuple[TextLine, ...]
    block: bool = False  # several stacked lines (slide, end card): the text *is* the subject

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3), "end": round(self.end, 3), "block": self.block,
            "lines": [ln.to_dict() for ln in self.lines],
        }

    @classmethod
    def from_dict(cls, data: dict) -> TextLayout:
        lines = tuple(TextLine.from_dict(ln) for ln in data["lines"])
        return cls(data["start"], data["end"], lines, bool(data.get("block", False)))


def is_text_block(lines: tuple[TextLine, ...]) -> bool:
    """Two or more lines stacked above each other, like a slide or an end card."""
    for a, b in itertools.combinations(lines, 2):
        overlap = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
        if overlap >= 0.5 * min(a.w, b.w) and abs(a.y - b.y) >= 0.5 * min(a.h, b.h):
            return True
    return False


# --- stage 1: when is which overlay on screen ------------------------------------------


def overlay_lines(boxes: list[TextBox]) -> list[TextBox]:
    return [b for b in boxes if b.h >= LINE_MIN_HEIGHT and b.w / max(b.h, 1e-6) >= LINE_MIN_ASPECT]


def _iou(a: TextBox, b: TextBox) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _same_overlay(a: list[TextBox], b: list[TextBox]) -> bool:
    matched = sum(1 for x in a if any(_iou(x, y) >= RUN_MATCH_IOU for y in b))
    return matched / max(len(a), len(b)) >= RUN_MATCH_RATIO


def text_runs(analysis: Analysis) -> list[tuple[float, float, float]]:
    """``(start, end, reference_time)`` for every stretch showing the same overlay text."""
    checked = [s for s in analysis.samples if s.text is not None]
    if len(checked) < 2:
        return []
    step = checked[1].t - checked[0].t
    runs: list[dict] = []
    current: dict | None = None
    for sample in checked:
        lines = overlay_lines(sample.text or [])
        if lines and current and _same_overlay(lines, current["last"]):
            current.update(end=sample.t + step, last=lines)
            current["members"].append((sample.t, lines))
        elif lines:
            if current:
                runs.append(current)
            current = {"start": sample.t, "end": sample.t + step, "last": lines, "members": [(sample.t, lines)]}
        elif current and sample.t - current["end"] > RUN_MAX_GAP_S:
            runs.append(current)
            current = None
    if current:
        runs.append(current)

    out = []
    for run in runs:
        if run["end"] - run["start"] < RUN_MIN_S:
            continue
        # Reference frame: the moment with the most text (fully faded in), nearest the middle.
        middle = (run["start"] + run["end"]) / 2
        t_ref, _ = max(run["members"], key=lambda m: (round(sum(b.w * b.h for b in m[1]), 3), -abs(m[0] - middle)))
        start = max(0.0, run["start"] - step, out[-1][1] if out else 0.0)
        out.append((round(start, 3), round(min(run["end"], analysis.duration), 3), t_ref))
    return out


# --- stage 2: measure each overlay on a full-resolution frame --------------------------


def _read_frame(path: Path, info: ffmpeg.MediaInfo, t: float) -> np.ndarray | None:
    proc = ffmpeg.popen(
        ["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-an", "-sn",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    raw, _ = proc.communicate(timeout=60)
    size = info.width * info.height * 3
    if len(raw) < size:
        return None
    return np.frombuffer(raw[:size], dtype=np.uint8).reshape(info.height, info.width, 3)


def _merge_same_line(boxes: list[TextBox]) -> list[TextBox]:
    """The detector sometimes splits one caption into word groups; join them back."""
    boxes = sorted(boxes, key=lambda b: (round(b.y / max(b.h, 1e-6)), b.x))
    merged: list[TextBox] = []
    for b in sorted(boxes, key=lambda b: b.x):
        for i, m in enumerate(merged):
            overlap = min(m.y + m.h, b.y + b.h) - max(m.y, b.y)
            gap = b.x - (m.x + m.w)
            if overlap >= 0.6 * min(m.h, b.h) and gap <= 1.5 * max(m.h, b.h):
                x0, y0 = min(m.x, b.x), min(m.y, b.y)
                merged[i] = TextBox(x0, y0, max(m.x + m.w, b.x + b.w) - x0, max(m.y + m.h, b.y + b.h) - y0)
                break
        else:
            merged.append(b)
    return merged


def ink_mask(patch: np.ndarray, boxed: bool, bg: tuple[int, int, int]) -> np.ndarray:
    """Pixels that belong to the letters (not the box / background)."""
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    if boxed:
        bg_gray = cv2.cvtColor(np.uint8([[bg]]), cv2.COLOR_BGR2GRAY)[0, 0]
        return (cv2.absdiff(gray, np.full_like(gray, bg_gray)) > 50).astype(np.uint8)
    k = max(3, min(31, (patch.shape[0] // 2) | 1))
    return (cv2.absdiff(gray, cv2.medianBlur(gray, k)) > 40).astype(np.uint8)


def letter_mask(patch: np.ndarray, bg: tuple[int, int, int], fg: tuple[int, ...] = ()) -> np.ndarray:
    """The letters of text that is not on a caption box, without anything behind them.

    With the letters' colour known (``fg``, measured at planning time) only pixels of
    that colour count, so a shirt, a face or the stage behind the text is left
    behind when the text is moved. Without it, fall back to "unlike the background".
    The frontend preview uses the same rule (textLayout.ts), so both look the same.
    """
    if fg:
        diff = patch.astype(np.int16) - np.asarray(fg, dtype=np.int16)
        return ((diff.astype(np.int32) ** 2).sum(axis=2) < TEXT_COLOR_TOL**2).astype(np.uint8)
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.int16)
    bg_gray = int(cv2.cvtColor(np.uint8([[bg]]), cv2.COLOR_BGR2GRAY)[0, 0])
    return (np.abs(gray - bg_gray) > LETTER_CONTRAST).astype(np.uint8)


def _letter_colour(patch: np.ndarray, bg: tuple[int, int, int]) -> tuple[int, ...]:
    """The dominant colour of the letters: the most contrasting pixels vs. the background."""
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.int16)
    bg_gray = int(cv2.cvtColor(np.uint8([[bg]]), cv2.COLOR_BGR2GRAY)[0, 0])
    contrast = np.abs(gray - bg_gray)
    strong = contrast >= max(LETTER_CONTRAST, np.percentile(contrast, 90) * 0.8)
    if strong.sum() < 20:
        return ()
    return tuple(int(v) for v in np.median(patch[strong], axis=0))


def _grow_box(frame: np.ndarray, rect: list[int], bg: np.ndarray, limit: int) -> list[int]:
    """Extend a rectangle outwards while the next row/column still has the box colour."""
    fh, fw = frame.shape[:2]

    def matches(strip: np.ndarray) -> bool:
        return strip.size > 0 and float(np.abs(strip.astype(np.int16) - bg).mean()) < 22

    x0, y0, x1, y1 = rect
    for _ in range(limit):
        grew = False
        if y0 > 0 and matches(frame[y0 - 1, x0:x1]):
            y0, grew = y0 - 1, True
        if y1 < fh and matches(frame[y1, x0:x1]):
            y1, grew = y1 + 1, True
        if x0 > 0 and matches(frame[y0:y1, x0 - 1]):
            x0, grew = x0 - 1, True
        if x1 < fw and matches(frame[y0:y1, x1]):
            x1, grew = x1 + 1, True
        if not grew:
            break
    return [x0, y0, x1, y1]


def _measure_line(frame: np.ndarray, box: TextBox) -> TextLine | None:
    fh, fw = frame.shape[:2]
    core = [int(box.x * fw), int(box.y * fh), int((box.x + box.w) * fw), int((box.y + box.h) * fh)]
    patch = frame[core[1]:core[3], core[0]:core[2]]
    if patch.size == 0 or patch.shape[0] < 6 or patch.shape[1] < 12:
        return None

    # Caption box or text straight on video? The detector's box hugs the letters, so its
    # edge pixels are the caption box when there is one: uniform, and filling most of the
    # gaps. Try the outermost pixels and a slightly inset ring (the outermost ones can
    # already be video when the detection overshoots the box by a pixel or two).
    boxed, edge_bg = False, None
    for inset in (0, max(2, int(0.12 * patch.shape[0]))):
        inner = patch[inset:patch.shape[0] - inset, inset:patch.shape[1] - inset]
        if inner.shape[0] < 6 or inner.shape[1] < 6:
            continue
        edge = np.concatenate([inner[:2].reshape(-1, 3), inner[-2:].reshape(-1, 3),
                               inner[:, :2].reshape(-1, 3), inner[:, -2:].reshape(-1, 3)])
        edge_bg = np.median(edge, axis=0)
        uniform = float(cv2.cvtColor(edge.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY).std()) < 22
        filling = (np.abs(patch.astype(np.int16) - edge_bg).mean(axis=2) < 30).mean() >= 0.35
        if uniform and filling:
            boxed = True
            break
    if boxed:
        bg_arr = edge_bg
        x0, y0, x1, y1 = _grow_box(frame, core, bg_arr, limit=int(0.8 * (core[3] - core[1])))
        # A real caption box stands out from its surroundings; a plain backdrop (a dark
        # stage, a white title card) just continues outside, so it isn't a box.
        o = 3
        outside = np.concatenate([
            frame[max(0, y0 - o):y0, x0:x1].reshape(-1, 3), frame[y1:y1 + o, x0:x1].reshape(-1, 3),
            frame[y0:y1, max(0, x0 - o):x0].reshape(-1, 3), frame[y0:y1, x1:x1 + o].reshape(-1, 3),
        ])
        if outside.size and float(np.abs(outside.astype(np.int16) - bg_arr).mean()) < BOX_CONTRAST:
            boxed = False
    if not boxed:
        pad_x, pad_y = 0.35 * box.h * fh, 0.25 * box.h * fh
        x0, y0 = int(max(0, core[0] - pad_x)), int(max(0, core[1] - pad_y))
        x1, y1 = int(min(fw, core[2] + pad_x)), int(min(fh, core[3] + pad_y))
        ring = np.concatenate([frame[y0:y0 + 2, x0:x1].reshape(-1, 3), frame[y1 - 2:y1, x0:x1].reshape(-1, 3)])
        bg_arr = np.median(ring, axis=0)
    bg_arr = np.asarray(bg_arr)
    bg = tuple(int(v) for v in bg_arr)

    patch = frame[y0:y1, x0:x1]
    mask = ink_mask(patch, boxed, bg)
    ink = float(mask.mean())
    if ink < 0.01:
        return None

    cuts = _word_gaps(mask, core[3] - core[1])

    return TextLine(
        x=x0 / fw, y=y0 / fh, w=(x1 - x0) / fw, h=(y1 - y0) / fh,
        cuts=tuple(cuts), boxed=bool(boxed), bg=bg, ink=ink, thumb=tuple(int(v) for v in _thumb(patch).ravel()),
        fg=() if boxed else _letter_colour(patch, bg),
        core=(0.0, 1.0) if boxed else ((core[0] - x0) / (x1 - x0), (core[2] - x0) / (x1 - x0)),
    )


def _word_gaps(mask: np.ndarray, letter_h: int) -> list[float]:
    """Centres (0..1) of the spaces between words.

    Ink-free column runs are either letter spacing or word spacing. When the gaps
    fall into two clearly different sizes, the bigger group is word spacing. When
    they are all alike (one word in a wide display font, like "DEPARTURE"), only a
    gap wider than a third of the letter height counts, so a word is never split.
    """
    columns = mask.sum(axis=0) > 0
    inked = np.flatnonzero(columns)
    if inked.size == 0:
        return []
    gaps, run_start = [], None
    for i in range(inked[0], inked[-1] + 1):
        if not columns[i] and run_start is None:
            run_start = i
        elif columns[i] and run_start is not None:
            gaps.append((run_start, i))
            run_start = None
    if not gaps:
        return []
    min_gap = max(3, int(0.13 * letter_h))
    always = max(min_gap, int(0.35 * letter_h))
    widths = sorted(b - a for a, b in gaps)
    threshold = always
    jumps = [(widths[i + 1] / max(1, widths[i]), i) for i in range(len(widths) - 1)]
    if jumps:
        ratio, i = max(jumps)
        if ratio >= 1.5:
            threshold = max(min_gap, widths[i + 1])
    return [(a + b) / 2 / len(columns) for a, b in gaps if b - a >= min(threshold, always)]


def _with_companions(lines: list[TextBox], all_boxes: list[TextBox]) -> list[TextBox]:
    """Add short text that sits on the same row as an overlay line ("July 2017" next to a place name)."""
    out = list(lines)
    for b in all_boxes:
        if b in out or b.h < LINE_MIN_HEIGHT or b.w / max(b.h, 1e-6) < 1.5:
            continue
        if any(min(b.y + b.h, ln.y + ln.h) - max(b.y, ln.y) >= 0.6 * min(b.h, ln.h) for ln in lines):
            out.append(b)
    return out


STENCIL_MAX_PIXELS = 120_000  # stencils are stored at most this big (keeps project files small)


def _with_stencil(line: TextLine, frames: list[np.ndarray]) -> TextLine:
    """Letter shapes that are present in every sampled frame of the run.

    Overlay text stays put while people move, so intersecting the letter masks of a
    few frames keeps exactly the letters and drops anything that just passed by.
    Pasting only through this stencil means nothing but the text is ever moved.
    """
    if line.boxed or not line.fg:
        return line
    fh, fw = frames[0].shape[:2]
    x0, y0 = int(line.x * fw), int(line.y * fh)
    x1, y1 = int((line.x + line.w) * fw), int((line.y + line.h) * fh)
    common = None
    for frame in frames:
        patch = frame[y0:y1, x0:x1]
        mask = letter_mask(patch, line.bg, line.fg)
        common = mask if common is None else common & mask
    if common is None or not common.any():
        return line
    cols = np.arange(common.shape[1]) / common.shape[1]
    common[:, (cols < line.core[0]) | (cols > line.core[1])] = 0
    common = cv2.dilate(common, np.ones((3, 3), np.uint8))  # keep anti-aliased edges
    scale = min(1.0, (STENCIL_MAX_PIXELS / common.size) ** 0.5)
    if scale < 1.0:
        size = (max(1, int(common.shape[1] * scale)), max(1, int(common.shape[0] * scale)))
        common = (cv2.resize(common.astype(np.float32), size, interpolation=cv2.INTER_AREA) > 0.2).astype(np.uint8)
    packed = base64.b64encode(np.packbits(common.ravel()).tobytes()).decode("ascii")
    return dataclasses.replace(line, stencil=packed, stencil_shape=(int(common.shape[0]), int(common.shape[1])))


def stencil_mask(line: TextLine) -> np.ndarray | None:
    if not line.stencil:
        return None
    rows, cols = line.stencil_shape
    bits = np.unpackbits(np.frombuffer(base64.b64decode(line.stencil), np.uint8))[: rows * cols]
    return bits.reshape(rows, cols)


def plan_text_layouts(
    path: Path, info: ffmpeg.MediaInfo, analysis: Analysis, detector: TextDetector
) -> list[TextLayout]:
    if not detector.available:
        return []
    layouts: list[TextLayout] = []
    for start, end, t_ref in text_runs(analysis):
        frame = _read_frame(path, info, t_ref)
        if frame is None:
            continue
        scale = 640 / info.width
        small = cv2.resize(frame, (640, max(32, round(info.height * scale))), interpolation=cv2.INTER_AREA)
        detected = detector.detect(small)
        boxes = _merge_same_line(_with_companions(overlay_lines(detected), detected))
        lines = [ln for ln in (_measure_line(frame, b) for b in boxes) if ln is not None]
        # Frames from elsewhere in the run: whatever isn't a letter in all of them (a hand,
        # a sleeve passing behind the caption) is left out of the stencil.
        others = [_read_frame(path, info, start + k * (end - start)) for k in (0.25, 0.75)]
        others = [f for f in others if f is not None]
        lines = [_with_stencil(ln, [frame, *others]) for ln in lines]
        if lines:
            ordered = tuple(sorted(lines, key=lambda ln: ln.y))
            layouts.append(TextLayout(start, end, ordered, is_text_block(ordered)))
    log.info("Planned %d text layouts", len(layouts))
    return layouts


# --- render-time geometry (mirrored in frontend/src/lib/textLayout.ts) -----------------


@dataclass(frozen=True)
class View:
    """Where the source frame lands in the output frame."""

    src_w: int
    src_h: int
    crop_x0: int
    crop_w: int
    out_w: int
    out_h: int

    @property
    def scale(self) -> float:
        return self.out_w / self.crop_w

    @property
    def video_h(self) -> int:
        return min(self.out_h, int(round(self.src_h * self.scale)))

    @property
    def video_top(self) -> int:
        return (self.out_h - self.video_h) // 2


@dataclass(frozen=True)
class Paste:
    src: tuple[int, int, int, int]  # x, y, w, h in source pixels
    dst: tuple[int, int, int, int]  # x, y, w, h in output pixels


@dataclass
class LineOps:
    line: TextLine
    erase: tuple[int, int, int, int] | None  # visible part of the half-cut original, output px
    plate: tuple[int, int, int, int] | None  # caption box behind the reflowed text, output px
    pastes: list[Paste]
    segments: list[tuple[float, float]]  # which part of the line (0..1) each paste shows


def _best_split(cuts: tuple[float, ...], k: int) -> list[tuple[float, float]]:
    """Choose k-1 word gaps so the widest of the k lines is as narrow as possible."""
    if k == 1 or len(cuts) < k - 1:
        return [(0.0, 1.0)]
    best, best_widest = None, math.inf
    for chosen in itertools.combinations(cuts, k - 1):
        bounds = [0.0, *chosen, 1.0]
        widest = max(b - a for a, b in zip(bounds[:-1], bounds[1:]))
        if widest < best_widest:
            best, best_widest = bounds, widest
    return list(zip(best[:-1], best[1:]))


def fit_line(line: TextLine, width_px: float, scale: float, max_w: float) -> tuple[list[tuple[float, float]], float]:
    """Wrap a line into as few lines as keep it readable; returns (segments, final scale)."""
    best: tuple[list[tuple[float, float]], float] | None = None
    for k in range(1, MAX_SEGMENTS + 1):
        segments = _best_split(line.cuts, k)
        widest = max(b - a for a, b in segments) * width_px * scale
        line_s = scale * min(1.0, max_w / widest)
        if line_s >= MIN_WRAP_SCALE * scale:
            return segments, line_s
        if best is None or line_s > best[1] + 1e-9:
            best = (segments, line_s)
    assert best is not None
    return best


def fit_text_window(x0: int, crop_w: int, src_w: int, center_px: float, layout: TextLayout,
                    t: float) -> tuple[int, bool]:
    """Shift the crop window so overlay text is fully inside it, if that's possible.

    Text is a key element too: when all of it fits in the window (end screens,
    short titles, lower thirds in a zoomed-out clip) it is better to move the
    camera a little than to cut or re-flow the text. The subject (the window's
    original centre) must stay well inside, unless the text is a whole slide / end
    card, which then becomes the subject. Returns (new x0, text fits).
    """
    tx0 = min(ln.x for ln in layout.lines) * src_w
    tx1 = max(ln.x + ln.w for ln in layout.lines) * src_w
    if tx1 - tx0 > crop_w:
        return x0, False
    lo, hi = max(0.0, tx1 - crop_w), min(float(src_w - crop_w), tx0)
    if not layout.block:
        lo = max(lo, center_px - (1 - WINDOW_SUBJECT_MARGIN) * crop_w)
        hi = min(hi, center_px - WINDOW_SUBJECT_MARGIN * crop_w)
    if lo > hi:
        return x0, False
    target = min(max(x0, lo), hi)
    ramp = min(1.0, max(0.0, min(t - layout.start, layout.end - t) / WINDOW_RAMP_S))
    return int(round(x0 + (target - x0) * ramp)), True


def _block(line: TextLine, width: float, height: float, scale: float, max_w: float):
    """Wrapped segments, their scale, line height, gap and total block height."""
    segments, line_s = fit_line(line, width, scale, max_w)
    seg_h = height * line_s
    gap = SEGMENT_GAP * seg_h
    return segments, line_s, seg_h, gap, len(segments) * seg_h + (len(segments) - 1) * gap


def _entry(line: TextLine, view: View, max_w: float) -> dict | None:
    """Where one line goes: wrapped segments, scale, and its anchor (on a bar if zoomed out)."""
    s = view.scale
    crop_x1 = view.crop_x0 + view.crop_w
    video_top, video_h = view.video_top, view.video_h
    X, Y, W, H = line.x * view.src_w, line.y * view.src_h, line.w * view.src_w, line.h * view.src_h
    if X >= view.crop_x0 - 1 and X + W <= crop_x1 + 1:
        return None  # already fully visible in the crop: leave it untouched
    if line.h / (1.0 if line.boxed else 1.5) > TITLE_LETTER_HEIGHT:
        return None  # big display type (an animated title card): framing handles it, cutting it up never looks right
    erase = None
    vx0, vx1 = max(X, view.crop_x0), min(X + W, crop_x1)
    if vx1 > vx0:
        erase = (int((vx0 - view.crop_x0) * s), int(video_top + Y * s), int(math.ceil((vx1 - vx0) * s)),
                 int(math.ceil(H * s)))

    scale0 = s
    segments, line_s, seg_h, gap, block_h = _block(line, W, H, s, max_w)
    # Anchor where the text was; in zoomed-out frames, move it onto the free bar
    # and let it grow there, since the video itself is smaller.
    anchor = video_top + (Y + H / 2) * s
    relative = (Y + H / 2) / view.src_h
    bar_h = view.out_h - video_top - video_h if relative >= 0.5 else video_top
    if bar_h > 0:
        big = max(s, BAR_TEXT_HEIGHT * view.out_h / H)
        for scale in (big, s):
            candidate = _block(line, W, H, scale, max_w)
            if candidate[4] * 1.25 <= bar_h:
                segments, line_s, seg_h, gap, block_h = candidate
                scale0 = scale
                anchor = (video_top + video_h + bar_h / 2) if relative >= 0.5 else bar_h / 2
                break
    return {"line": line, "erase": erase, "segments": segments, "line_s": line_s, "seg_h": seg_h, "gap": gap,
            "block_h": block_h, "anchor": anchor, "scale0": scale0, "X": X, "Y": Y, "W": W, "H": H}


def _groups(entries: list[dict], max_w: float) -> list[dict]:
    """Lines on the same row ("July 2017 ... Denver Colorado") stay side by side when they fit."""
    rows: list[list[dict]] = []
    for e in sorted(entries, key=lambda e: (e["Y"], e["X"])):
        for row in rows:
            r = row[0]
            if min(e["Y"] + e["H"], r["Y"] + r["H"]) - max(e["Y"], r["Y"]) >= 0.5 * min(e["H"], r["H"]):
                row.append(e)
                break
        else:
            rows.append([e])

    groups = []
    for row in rows:
        if len(row) > 1:
            row.sort(key=lambda e: e["X"])
            scale = min(e["scale0"] for e in row)
            h_max = max(e["H"] for e in row)
            total = sum(e["W"] for e in row) * scale + ROW_GAP * h_max * scale * (len(row) - 1)
            fit = min(1.0, max_w / total)
            if fit >= ROW_MIN_FIT:
                scale *= fit
                block_h = h_max * scale
                anchor = sum(e["anchor"] for e in row) / len(row)
                groups.append({"row": row, "scale": scale, "seg_h": block_h, "block_h": block_h,
                               "top": anchor - block_h / 2})
                continue
        for e in row:
            groups.append({"row": None, "entry": e, "seg_h": e["seg_h"], "block_h": e["block_h"],
                           "top": e["anchor"] - e["block_h"] / 2})
    return groups


def layout_ops(lines: tuple[TextLine, ...], view: View) -> list[LineOps]:
    max_w = MAX_LINE_WIDTH * view.out_w
    entries = [e for e in (_entry(line, view, max_w) for line in lines) if e is not None]
    groups = _groups(entries, max_w)

    # Keep blocks from overlapping each other and inside the safe area.
    groups.sort(key=lambda g: g["top"])
    for prev, cur in zip(groups, groups[1:]):
        cur["top"] = max(cur["top"], prev["top"] + prev["block_h"] + prev["seg_h"] * 0.6)
    if groups:
        shift = max(0.0, groups[-1]["top"] + groups[-1]["block_h"] - SAFE_BOTTOM * view.out_h)
        top_room = groups[0]["top"] - shift - SAFE_TOP * view.out_h
        for g in groups:
            g["top"] -= shift + min(0.0, top_room)

    ops = []
    for g in groups:
        if g["row"]:
            scale, top = g["scale"], g["top"]
            widths = [e["W"] * scale for e in g["row"]]
            gap_x = ROW_GAP * g["block_h"]
            x = (view.out_w - sum(widths) - gap_x * (len(widths) - 1)) / 2
            for e, w in zip(g["row"], widths):
                h = e["H"] * scale
                y = top + (g["block_h"] - h) / 2
                paste = Paste(src=(int(e["X"]), int(e["Y"]), max(1, int(e["W"])), max(1, int(e["H"]))),
                              dst=(int(x), int(y), max(1, int(w)), max(1, int(h))))
                plate = None
                if e["line"].boxed:
                    pad = PLATE_PAD * h
                    plate = (int(x - pad), int(y - pad), int(w + 2 * pad), int(h + 2 * pad))
                ops.append(LineOps(line=e["line"], erase=e["erase"], plate=plate, pastes=[paste],
                                   segments=[(0.0, 1.0)]))
                x += w + gap_x
            continue

        e = g["entry"]
        line, X, Y, W, H = e["line"], e["X"], e["Y"], e["W"], e["H"]
        pastes = []
        y = g["top"]
        widest_px = 0.0
        for a, b in e["segments"]:
            src_w = (b - a) * W
            dst_w = src_w * e["line_s"]
            widest_px = max(widest_px, dst_w)
            pastes.append(Paste(
                src=(int(X + a * W), int(Y), max(1, int(src_w)), max(1, int(H))),
                dst=(int((view.out_w - dst_w) / 2), int(y), max(1, int(dst_w)), max(1, int(e["seg_h"]))),
            ))
            y += e["seg_h"] + e["gap"]
        plate = None
        if line.boxed:
            pad = PLATE_PAD * e["seg_h"]
            plate = (int((view.out_w - widest_px) / 2 - pad), int(g["top"] - pad), int(widest_px + 2 * pad),
                     int(e["block_h"] + 2 * pad))
        ops.append(LineOps(line=line, erase=e["erase"], plate=plate, pastes=pastes, segments=list(e["segments"])))
    return ops


# --- render-time drawing ---------------------------------------------------------------


def _clip_rect(rect: tuple[int, int, int, int], w: int, h: int) -> tuple[int, int, int, int] | None:
    x, y, rw, rh = rect
    x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + rw), min(h, y + rh)
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _thumb(patch: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (THUMB_W, THUMB_H), interpolation=cv2.INTER_AREA)


def is_present(frame: np.ndarray, line: TextLine) -> bool:
    """Is this exact overlay on screen right now (not fading in, sliding in, or already gone)?"""
    fh, fw = frame.shape[:2]
    patch = frame[int(line.y * fh): int((line.y + line.h) * fh), int(line.x * fw): int((line.x + line.w) * fw)]
    if patch.size == 0 or not line.thumb:
        return patch.size > 0
    now = _thumb(patch).astype(np.float32).ravel()
    ref = np.asarray(line.thumb, dtype=np.float32)
    if now.std() < 1 or ref.std() < 1:
        return False
    return float(np.corrcoef(now, ref)[0, 1]) >= PRESENCE_MIN_CORR


def _drop_blobs(mask: np.ndarray) -> np.ndarray:
    """Trim shapes much taller than the letters (a hand or a sleeve crossing the text).

    Only the part outside the band the letters occupy is removed, so a letter that
    touches the hand (and became one shape with it) is kept.
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 2:
        return mask
    tops, heights = stats[1:, cv2.CC_STAT_TOP], stats[1:, cv2.CC_STAT_HEIGHT]
    letters = heights >= 0.3 * mask.shape[0]
    if not letters.any():
        return mask
    typical = float(np.median(heights[letters]))
    band_top = int(np.percentile(tops[letters], 10)) - 1
    band_bottom = int(np.percentile(tops[letters] + heights[letters], 90)) + 1
    out = mask.copy()
    for i in np.flatnonzero(heights > BLOB_MAX_HEIGHT * typical) + 1:
        blob = labels == i
        blob[max(0, band_top):band_bottom] = False  # keep whatever lies within the letter band
        out[blob] = 0
    return out


def _covered_fraction(rect: tuple[int, int, int, int], op: LineOps) -> float:
    """How much of ``rect`` the re-flowed text (plate or pieces) will paint over."""
    x, y, w, h = rect
    mask = np.zeros((max(1, h), max(1, w)), bool)
    for ox, oy, ow, oh in [op.plate] if op.plate else [p.dst for p in op.pastes]:
        ax0, ay0 = max(0, ox - x), max(0, oy - y)
        ax1, ay1 = min(w, ox + ow - x), min(h, oy + oh - y)
        if ax1 > ax0 and ay1 > ay0:
            mask[ay0:ay1, ax0:ax1] = True
    return float(mask.mean())


def erase_rect(op: LineOps) -> tuple[int, int, int, int] | None:
    if op.erase is None:
        return None
    pad = int(0.15 * op.erase[3])
    return op.erase[0], op.erase[1] - pad, op.erase[2], op.erase[3] + 2 * pad


def draw_ops(canvas: np.ndarray, frame: np.ndarray, ops: list[LineOps]) -> None:
    """Erase half-cut originals, then paste the reflowed text (in place, on ``canvas``)."""
    oh, ow = canvas.shape[:2]
    present = [op for op in ops if is_present(frame, op.line)]
    for op in present:
        rect = erase_rect(op)
        r = _clip_rect(rect, ow, oh) if rect else None
        if r is None:
            continue
        x0, y0, x1, y1 = r
        if op.line.boxed and _covered_fraction(rect, op) < ERASE_COVERED and y0 - (y1 - y0) >= 0:
            # The text moved elsewhere (e.g. onto a zoom-out bar): hide the old spot with a
            # mirror of what is just above it, so no empty box or smear is left behind.
            canvas[y0:y1, x0:x1] = canvas[y0 - (y1 - y0):y0, x0:x1][::-1]
        elif op.line.boxed:
            canvas[y0:y1, x0:x1] = op.line.bg
        else:
            region = canvas[y0:y1, x0:x1]
            mask = ink_mask(region, False, op.line.bg) | letter_mask(region, op.line.bg, op.line.fg)
            mask = cv2.dilate(mask * 255, np.ones((5, 5), np.uint8))
            canvas[y0:y1, x0:x1] = cv2.inpaint(region, mask, 3, cv2.INPAINT_TELEA)

    for op in present:
        if op.plate is not None and (r := _clip_rect(op.plate, ow, oh)):
            x0, y0, x1, y1 = r
            canvas[y0:y1, x0:x1] = op.line.bg
        for paste, (seg_a, seg_b) in zip(op.pastes, op.segments):
            sx, sy, sw, sh = paste.src
            piece = frame[sy: sy + sh, sx: sx + sw]
            if piece.size == 0:
                continue
            dx, dy, dw, dh = paste.dst
            piece = cv2.resize(piece, (dw, dh), interpolation=cv2.INTER_AREA if dw < sw else cv2.INTER_CUBIC)
            r = _clip_rect(paste.dst, ow, oh)
            if r is None:
                continue
            x0, y0, x1, y1 = r
            piece = piece[y0 - dy: y1 - dy, x0 - dx: x1 - dx]
            if op.line.boxed:
                canvas[y0:y1, x0:x1] = piece
            else:
                # Text straight on video / a plain backdrop: paste only the letters.
                letters = _drop_blobs(letter_mask(piece, op.line.bg, op.line.fg))
                letters = cv2.dilate(letters, np.ones((2, 2), np.uint8))
                # Keep only columns inside the detected text (the padding may hold a shirt or a hand).
                cols = seg_a + (np.arange(dw)[x0 - dx: x1 - dx] + 0.5) / dw * (seg_b - seg_a)
                letters[:, (cols < op.line.core[0]) | (cols > op.line.core[1])] = 0
                stencil = stencil_mask(op.line)
                if stencil is not None:
                    # Only the letters' own shapes: nothing that merely crosses the text gets moved.
                    sc0 = int(seg_a * stencil.shape[1])
                    sc1 = max(sc0 + 1, int(seg_b * stencil.shape[1]))
                    part = cv2.resize(stencil[:, sc0:sc1], (dw, dh), interpolation=cv2.INTER_NEAREST)
                    letters &= part[y0 - dy: y1 - dy, x0 - dx: x1 - dx]
                alpha = cv2.GaussianBlur(letters.astype(np.float32), (0, 0), 0.8)[..., None]
                base = canvas[y0:y1, x0:x1].astype(np.float32)
                canvas[y0:y1, x0:x1] = (piece * alpha + base * (1 - alpha)).astype(np.uint8)
