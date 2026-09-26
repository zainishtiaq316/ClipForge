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
BAR_TEXT_HEIGHT = 0.055  # text moved onto a zoom-out bar is at least this tall (fraction of output)
WINDOW_SUBJECT_MARGIN = 0.15  # when shifting the window to fit text, the subject stays this far from its edges
WINDOW_RAMP_S = 0.3  # ease the window shift in / out
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

    def to_dict(self) -> dict:
        data = asdict(self)
        data["cuts"] = [round(c, 4) for c in self.cuts]
        data["bg"] = list(self.bg)
        data["thumb"] = list(self.thumb)
        for key in ("x", "y", "w", "h", "ink"):
            data[key] = round(data[key], 4)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TextLine:
        return cls(
            x=data["x"], y=data["y"], w=data["w"], h=data["h"], cuts=tuple(data["cuts"]),
            boxed=data["boxed"], bg=tuple(data["bg"]), ink=data["ink"], thumb=tuple(data.get("thumb", ())),
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
    else:
        pad_x, pad_y = 0.35 * box.h * fh, 0.25 * box.h * fh
        x0, y0 = int(max(0, core[0] - pad_x)), int(max(0, core[1] - pad_y))
        x1, y1 = int(min(fw, core[2] + pad_x)), int(min(fh, core[3] + pad_y))
        ring = np.concatenate([frame[y0:y0 + 2, x0:x1].reshape(-1, 3), frame[y1 - 2:y1, x0:x1].reshape(-1, 3)])
        bg_arr = np.median(ring, axis=0)
    bg = tuple(int(v) for v in bg_arr)

    patch = frame[y0:y1, x0:x1]
    mask = ink_mask(patch, boxed, bg)
    ink = float(mask.mean())
    if ink < 0.01:
        return None

    # Word gaps: runs of ink-free columns wider than ~a fifth of the letter height.
    columns = mask.sum(axis=0) > 0
    min_gap = max(3, int(0.13 * (core[3] - core[1])))
    cuts, run_start = [], None
    inked = np.flatnonzero(columns)
    first, last = (inked[0], inked[-1]) if inked.size else (0, len(columns) - 1)
    for i in range(first, last + 1):
        if not columns[i] and run_start is None:
            run_start = i
        elif columns[i] and run_start is not None:
            if i - run_start >= min_gap:
                cuts.append((run_start + i) / 2 / len(columns))
            run_start = None

    return TextLine(
        x=x0 / fw, y=y0 / fh, w=(x1 - x0) / fw, h=(y1 - y0) / fh,
        cuts=tuple(cuts), boxed=bool(boxed), bg=bg, ink=ink, thumb=tuple(int(v) for v in _thumb(patch).ravel()),
    )


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
        boxes = _merge_same_line(overlay_lines(detector.detect(small)))
        lines = [ln for ln in (_measure_line(frame, b) for b in boxes) if ln is not None]
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


def layout_ops(lines: tuple[TextLine, ...], view: View) -> list[LineOps]:
    s = view.scale
    max_w = MAX_LINE_WIDTH * view.out_w
    crop_x1 = view.crop_x0 + view.crop_w
    video_top, video_h = view.video_top, view.video_h
    bar_top_h, bar_bottom_h = video_top, view.out_h - video_top - video_h

    blocks = []
    for line in lines:
        X, Y, W, H = line.x * view.src_w, line.y * view.src_h, line.w * view.src_w, line.h * view.src_h
        if X >= view.crop_x0 - 1 and X + W <= crop_x1 + 1:
            continue  # already fully visible in the crop: leave it untouched
        erase = None
        vx0, vx1 = max(X, view.crop_x0), min(X + W, crop_x1)
        if vx1 > vx0:
            erase = (int((vx0 - view.crop_x0) * s), int(video_top + Y * s), int(math.ceil((vx1 - vx0) * s)),
                     int(math.ceil(H * s)))

        segments, line_s, seg_h, gap, block_h = _block(line, W, H, s, max_w)
        # Anchor where the text was; in zoomed-out frames, move it onto the free bar
        # and let it grow there, since the video itself is smaller.
        anchor = video_top + (Y + H / 2) * s
        relative = (Y + H / 2) / view.src_h
        bar_h = bar_bottom_h if relative >= 0.5 else bar_top_h
        if bar_h > 0:
            bigger = _block(line, W, H, max(s, BAR_TEXT_HEIGHT * view.out_h / H), max_w)
            for candidate in (bigger, (segments, line_s, seg_h, gap, block_h)):
                if candidate[4] * 1.25 <= bar_h:
                    segments, line_s, seg_h, gap, block_h = candidate
                    anchor = (video_top + video_h + bar_h / 2) if relative >= 0.5 else bar_h / 2
                    break
        blocks.append([line, erase, segments, line_s, seg_h, gap, block_h, anchor - block_h / 2])

    # Keep blocks from overlapping each other and inside the safe area.
    blocks.sort(key=lambda b: b[7])
    for prev, cur in zip(blocks, blocks[1:]):
        cur[7] = max(cur[7], prev[7] + prev[6] + prev[4] * 0.6)
    if blocks:
        overflow = blocks[-1][7] + blocks[-1][6] - SAFE_BOTTOM * view.out_h
        shift = max(0.0, overflow)
        top_room = blocks[0][7] - shift - SAFE_TOP * view.out_h
        for b in blocks:
            b[7] -= shift + min(0.0, top_room)

    ops = []
    for line, erase, segments, line_s, seg_h, gap, block_h, top in blocks:
        X, Y, W, H = line.x * view.src_w, line.y * view.src_h, line.w * view.src_w, line.h * view.src_h
        pastes = []
        y = top
        widest_px = 0.0
        for a, b in segments:
            src_w = (b - a) * W
            dst_w = src_w * line_s
            widest_px = max(widest_px, dst_w)
            pastes.append(Paste(
                src=(int(X + a * W), int(Y), max(1, int(src_w)), max(1, int(H))),
                dst=(int((view.out_w - dst_w) / 2), int(y), max(1, int(dst_w)), max(1, int(seg_h))),
            ))
            y += seg_h + gap
        plate = None
        if line.boxed:
            pad = PLATE_PAD * seg_h
            plate = (int((view.out_w - widest_px) / 2 - pad), int(top - pad), int(widest_px + 2 * pad),
                     int(block_h + 2 * pad))
        ops.append(LineOps(line=line, erase=erase, plate=plate, pastes=pastes))
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


def draw_ops(canvas: np.ndarray, frame: np.ndarray, ops: list[LineOps]) -> None:
    """Erase half-cut originals, then paste the reflowed text (in place, on ``canvas``)."""
    oh, ow = canvas.shape[:2]
    present = [op for op in ops if is_present(frame, op.line)]
    for op in present:
        if op.erase is None:
            continue
        pad = int(0.15 * op.erase[3])
        r = _clip_rect((op.erase[0], op.erase[1] - pad, op.erase[2], op.erase[3] + 2 * pad), ow, oh)
        if r is None:
            continue
        x0, y0, x1, y1 = r
        if op.line.boxed:
            canvas[y0:y1, x0:x1] = op.line.bg
        else:
            region = canvas[y0:y1, x0:x1]
            mask = cv2.dilate(ink_mask(region, False, op.line.bg) * 255, np.ones((5, 5), np.uint8))
            canvas[y0:y1, x0:x1] = cv2.inpaint(region, mask, 3, cv2.INPAINT_TELEA)

    for op in present:
        if op.plate is not None and (r := _clip_rect(op.plate, ow, oh)):
            x0, y0, x1, y1 = r
            canvas[y0:y1, x0:x1] = op.line.bg
        for paste in op.pastes:
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
                # Text straight on video: feather the edges so no rectangle shows.
                alpha = np.zeros(piece.shape[:2], np.float32)
                edge = max(2, min(piece.shape[:2]) // 6)
                alpha[edge:-edge, edge:-edge] = 1.0
                alpha = cv2.GaussianBlur(alpha, (0, 0), edge / 2)[..., None]
                base = canvas[y0:y1, x0:x1].astype(np.float32)
                canvas[y0:y1, x0:x1] = (piece * alpha + base * (1 - alpha)).astype(np.uint8)
