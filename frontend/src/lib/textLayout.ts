// Mirror of backend/app/services/text_layout.py (geometry only) and renderer.crop_width,
// so the live 9:16 preview shows the same framing and re-flowed text as the export.
import type { CameraPathData, Clip, TextLayoutData, TextLineData } from './api'
import { cropCenterAt, zoomAt } from './cameraPath'

const MAX_LINE_WIDTH = 0.9
const MAX_SEGMENTS = 3
const SEGMENT_GAP = 0.18
const PLATE_PAD = 0.22
const SAFE_TOP = 0.05
const SAFE_BOTTOM = 0.95
const MIN_WRAP_SCALE = 0.85
const ROW_GAP = 0.6
const ROW_MIN_FIT = 0.6
const TITLE_LETTER_HEIGHT = 0.13
const BAR_TEXT_HEIGHT = 0.055
const WINDOW_SUBJECT_MARGIN = 0.15
const WINDOW_RAMP_S = 0.3
const LETTER_CONTRAST = 50
const TEXT_COLOR_TOL = 80
const ERASE_COVERED = 0.8

export type Rect = [number, number, number, number] // x, y, w, h

export interface View {
  srcW: number
  srcH: number
  cropX0: number
  cropW: number
  outW: number
  outH: number
}

export const viewScale = (v: View) => v.outW / v.cropW
export const videoHeight = (v: View) => Math.min(v.outH, Math.round(v.srcH * viewScale(v)))
export const videoTop = (v: View) => Math.floor((v.outH - videoHeight(v)) / 2)

/** Source width shown in the vertical frame. zoom 0 = full-height 9:16 window, 1 = whole frame width. */
export function cropWidth(srcW: number, srcH: number, zoom: number): number {
  const base = Math.min(srcW, Math.round((srcH * 9) / 16 / 2) * 2)
  return Math.min(srcW, Math.round((base + zoom * (srcW - base)) / 2) * 2)
}

export function textAt(path: CameraPathData | null, t: number): TextLayoutData | null {
  return path?.text_layouts?.find((l) => l.start <= t && t < l.end) ?? null
}

function combinations<T>(items: T[], k: number): T[][] {
  if (k === 0) return [[]]
  const out: T[][] = []
  items.forEach((item, i) => combinations(items.slice(i + 1), k - 1).forEach((rest) => out.push([item, ...rest])))
  return out
}

function bestSplit(cuts: number[], k: number): [number, number][] {
  if (k === 1 || cuts.length < k - 1) return [[0, 1]]
  let best: number[] | null = null
  let bestWidest = Infinity
  for (const chosen of combinations(cuts, k - 1)) {
    const bounds = [0, ...chosen, 1]
    const widest = Math.max(...bounds.slice(1).map((b, i) => b - bounds[i]))
    if (widest < bestWidest) {
      best = bounds
      bestWidest = widest
    }
  }
  return best!.slice(1).map((b, i) => [best![i], b])
}

export function fitLine(line: TextLineData, widthPx: number, scale: number, maxW: number): [[number, number][], number] {
  let best: [[number, number][], number] | null = null
  for (let k = 1; k <= MAX_SEGMENTS; k++) {
    const segments = bestSplit(line.cuts, k)
    const widest = Math.max(...segments.map(([a, b]) => b - a)) * widthPx * scale
    const lineS = scale * Math.min(1, maxW / widest)
    if (lineS >= MIN_WRAP_SCALE * scale) return [segments, lineS]
    if (!best || lineS > best[1] + 1e-9) best = [segments, lineS]
  }
  return best!
}

/** Shift the crop window so the text is fully inside it, when possible (see Python docstring). */
export function fitTextWindow(x0: number, cropW: number, srcW: number, centerPx: number, layout: TextLayoutData, t: number): [number, boolean] {
  const tx0 = Math.min(...layout.lines.map((l) => l.x)) * srcW
  const tx1 = Math.max(...layout.lines.map((l) => l.x + l.w)) * srcW
  if (tx1 - tx0 > cropW) return [x0, false]
  let lo = Math.max(0, tx1 - cropW)
  let hi = Math.min(srcW - cropW, tx0)
  if (!layout.block) {
    lo = Math.max(lo, centerPx - (1 - WINDOW_SUBJECT_MARGIN) * cropW)
    hi = Math.min(hi, centerPx - WINDOW_SUBJECT_MARGIN * cropW)
  }
  if (lo > hi) return [x0, false]
  const target = Math.min(Math.max(x0, lo), hi)
  const ramp = Math.min(1, Math.max(0, Math.min(t - layout.start, layout.end - t) / WINDOW_RAMP_S))
  return [Math.round(x0 + (target - x0) * ramp), true]
}

export interface LineOps {
  line: TextLineData
  erase: Rect | null
  plate: Rect | null
  pastes: { src: Rect; dst: Rect }[]
  segments: [number, number][] // which part of the line (0..1) each paste shows
}

function block(line: TextLineData, W: number, H: number, scale: number, maxW: number) {
  const [segments, lineS] = fitLine(line, W, scale, maxW)
  const segH = H * lineS
  const gap = SEGMENT_GAP * segH
  return { segments, lineS, segH, gap, blockH: segments.length * segH + (segments.length - 1) * gap }
}

interface Entry {
  line: TextLineData
  erase: Rect | null
  b: ReturnType<typeof block>
  anchor: number
  scale0: number
  X: number
  Y: number
  W: number
  H: number
}

function entry(line: TextLineData, v: View, maxW: number): Entry | null {
  const s = viewScale(v)
  const cropX1 = v.cropX0 + v.cropW
  const top = videoTop(v)
  const vh = videoHeight(v)
  const X = line.x * v.srcW, Y = line.y * v.srcH, W = line.w * v.srcW, H = line.h * v.srcH
  if (X >= v.cropX0 - 1 && X + W <= cropX1 + 1) return null
  if (line.h / (line.boxed ? 1 : 1.5) > TITLE_LETTER_HEIGHT) return null // big display type: never cut it up
  let erase: Rect | null = null
  const vx0 = Math.max(X, v.cropX0), vx1 = Math.min(X + W, cropX1)
  if (vx1 > vx0) erase = [Math.trunc((vx0 - v.cropX0) * s), Math.trunc(top + Y * s), Math.ceil((vx1 - vx0) * s), Math.ceil(H * s)]

  let scale0 = s
  let b = block(line, W, H, s, maxW)
  let anchor = top + (Y + H / 2) * s
  const relative = (Y + H / 2) / v.srcH
  const barH = relative >= 0.5 ? v.outH - top - vh : top
  if (barH > 0) {
    const big = Math.max(s, (BAR_TEXT_HEIGHT * v.outH) / H)
    for (const scale of [big, s]) {
      const candidate = block(line, W, H, scale, maxW)
      if (candidate.blockH * 1.25 <= barH) {
        b = candidate
        scale0 = scale
        anchor = relative >= 0.5 ? top + vh + barH / 2 : barH / 2
        break
      }
    }
  }
  return { line, erase, b, anchor, scale0, X, Y, W, H }
}

interface Group {
  row: Entry[] | null
  entry?: Entry
  scale: number
  segH: number
  blockH: number
  top: number
}

/** Lines on the same row stay side by side when they fit (mirrors text_layout._groups). */
function groups(entries: Entry[], maxW: number): Group[] {
  const rows: Entry[][] = []
  for (const e of [...entries].sort((a, c) => a.Y - c.Y || a.X - c.X)) {
    const row = rows.find((r) => Math.min(e.Y + e.H, r[0].Y + r[0].H) - Math.max(e.Y, r[0].Y) >= 0.5 * Math.min(e.H, r[0].H))
    if (row) row.push(e)
    else rows.push([e])
  }
  const out: Group[] = []
  for (const row of rows) {
    if (row.length > 1) {
      row.sort((a, c) => a.X - c.X)
      let scale = Math.min(...row.map((e) => e.scale0))
      const hMax = Math.max(...row.map((e) => e.H))
      const total = row.reduce((sum, e) => sum + e.W, 0) * scale + ROW_GAP * hMax * scale * (row.length - 1)
      const fit = Math.min(1, maxW / total)
      if (fit >= ROW_MIN_FIT) {
        scale *= fit
        const blockH = hMax * scale
        const anchor = row.reduce((sum, e) => sum + e.anchor, 0) / row.length
        out.push({ row, scale, segH: blockH, blockH, top: anchor - blockH / 2 })
        continue
      }
    }
    for (const e of row) out.push({ row: null, entry: e, scale: e.b.lineS, segH: e.b.segH, blockH: e.b.blockH, top: e.anchor - e.b.blockH / 2 })
  }
  return out
}

export function layoutOps(lines: TextLineData[], v: View): LineOps[] {
  const maxW = MAX_LINE_WIDTH * v.outW
  const entries = lines.map((l) => entry(l, v, maxW)).filter((e): e is Entry => e !== null)
  const gs = groups(entries, maxW).sort((a, c) => a.top - c.top)
  for (let i = 1; i < gs.length; i++) {
    const prev = gs[i - 1]
    gs[i].top = Math.max(gs[i].top, prev.top + prev.blockH + prev.segH * 0.6)
  }
  if (gs.length) {
    const last = gs[gs.length - 1]
    const shift = Math.max(0, last.top + last.blockH - SAFE_BOTTOM * v.outH)
    const topRoom = gs[0].top - shift - SAFE_TOP * v.outH
    for (const g of gs) g.top -= shift + Math.min(0, topRoom)
  }

  const ops: LineOps[] = []
  for (const g of gs) {
    if (g.row) {
      const widths = g.row.map((e) => e.W * g.scale)
      const gapX = ROW_GAP * g.blockH
      let x = (v.outW - widths.reduce((a, c) => a + c, 0) - gapX * (widths.length - 1)) / 2
      g.row.forEach((e, i) => {
        const w = widths[i]
        const h = e.H * g.scale
        const y = g.top + (g.blockH - h) / 2
        const paste = {
          src: [Math.trunc(e.X), Math.trunc(e.Y), Math.max(1, Math.trunc(e.W)), Math.max(1, Math.trunc(e.H))] as Rect,
          dst: [Math.trunc(x), Math.trunc(y), Math.max(1, Math.trunc(w)), Math.max(1, Math.trunc(h))] as Rect,
        }
        let plate: Rect | null = null
        if (e.line.boxed) {
          const pad = PLATE_PAD * h
          plate = [Math.trunc(x - pad), Math.trunc(y - pad), Math.trunc(w + 2 * pad), Math.trunc(h + 2 * pad)]
        }
        ops.push({ line: e.line, erase: e.erase, plate, pastes: [paste], segments: [[0, 1]] })
        x += w + gapX
      })
      continue
    }
    const e = g.entry!
    const { line, X, Y, W, H, b } = e
    let y = g.top
    let widest = 0
    const pastes = b.segments.map(([a, c]) => {
      const srcW = (c - a) * W
      const dstW = srcW * b.lineS
      widest = Math.max(widest, dstW)
      const paste = {
        src: [Math.trunc(X + a * W), Math.trunc(Y), Math.max(1, Math.trunc(srcW)), Math.max(1, Math.trunc(H))] as Rect,
        dst: [Math.trunc((v.outW - dstW) / 2), Math.trunc(y), Math.max(1, Math.trunc(dstW)), Math.max(1, Math.trunc(b.segH))] as Rect,
      }
      y += b.segH + b.gap
      return paste
    })
    let plate: Rect | null = null
    if (line.boxed) {
      const pad = PLATE_PAD * b.segH
      plate = [Math.trunc((v.outW - widest) / 2 - pad), Math.trunc(g.top - pad), Math.trunc(widest + 2 * pad), Math.trunc(b.blockH + 2 * pad)]
    }
    ops.push({ line, erase: e.erase, plate, pastes, segments: b.segments })
  }
  return ops
}

export interface FrameLayout {
  x0: number // crop window left edge, source px
  cropW: number
  layout: TextLayoutData | null // text to re-flow (null when none, or when it fits the window)
}

/** Everything the preview needs to draw one frame, exactly like the renderer. */
export function frameLayout(path: CameraPathData | null, clip: Clip | null | undefined, t: number, srcW: number, srcH: number): FrameLayout {
  const framing = clip?.framing ?? 'auto'
  // Smart framing zooms out automatically to keep a whole person / title card in frame.
  const zoom = Math.max(clip?.zoom ?? 0, framing === 'auto' ? zoomAt(path, t) : 0)
  const cropW = cropWidth(srcW, srcH, zoom)
  const follows = (framing === 'auto' || framing === 'track') && cropW < srcW
  const center = follows ? cropCenterAt(path, t, framing) : 0.5
  let x0 = Math.min(srcW - cropW, Math.max(0, Math.round(center * srcW - cropW / 2)))
  let layout = framing === 'auto' ? textAt(path, t) : null
  if (layout) {
    const [shifted, fits] = fitTextWindow(x0, cropW, srcW, center * srcW, layout, t)
    x0 = shifted
    if (fits) layout = null
  }
  return { x0, cropW, layout }
}

export const bgrToCss = ([b, g, r]: number[]) => `rgb(${r} ${g} ${b})`

export function eraseRect(op: LineOps): Rect | null {
  if (!op.erase) return null
  const pad = Math.trunc(0.15 * op.erase[3])
  return [op.erase[0], op.erase[1] - pad, op.erase[2], op.erase[3] + 2 * pad]
}

/** How much of `rect` the re-flowed text will paint over (mirrors text_layout._covered_fraction). */
export function coveredFraction([x, y, w, h]: Rect, op: LineOps): number {
  if (w <= 0 || h <= 0) return 0
  const rects = op.plate ? [op.plate] : op.pastes.map((p) => p.dst)
  const cols = new Uint8Array(w * h)
  for (const [ox, oy, ow, oh] of rects) {
    const ax0 = Math.max(0, ox - x), ay0 = Math.max(0, oy - y)
    const ax1 = Math.min(w, ox + ow - x), ay1 = Math.min(h, oy + oh - y)
    for (let r = ay0; r < ay1; r++) cols.fill(1, r * w + ax0, r * w + ax1)
  }
  let sum = 0
  for (let i = 0; i < cols.length; i++) sum += cols[i]
  return sum / cols.length
}

let scratch: HTMLCanvasElement | null = null
const stencils = new WeakMap<TextLineData, HTMLCanvasElement>()

/** The line's letter stencil as a canvas (opaque where the letters are), decoded once. */
function stencilCanvas(line: TextLineData): HTMLCanvasElement | null {
  if (!line.stencil || !line.stencil_shape) return null
  const cached = stencils.get(line)
  if (cached) return cached
  const [rows, cols] = line.stencil_shape
  const bytes = Uint8Array.from(atob(line.stencil), (c) => c.charCodeAt(0))
  const canvas = document.createElement('canvas')
  canvas.width = cols
  canvas.height = rows
  const ctx = canvas.getContext('2d')!
  const img = ctx.createImageData(cols, rows)
  for (let i = 0; i < rows * cols; i++) {
    if ((bytes[i >> 3] >> (7 - (i & 7))) & 1) img.data[i * 4 + 3] = 255
  }
  ctx.putImageData(img, 0, 0)
  stencils.set(line, canvas)
  return canvas
}

/** Draw erase + re-flowed text for one frame onto `ctx` (same rules as text_layout.draw_ops). */
export function drawTextOps(ctx: CanvasRenderingContext2D, source: CanvasImageSource, ops: LineOps[]) {
  const { width: ow, height: oh } = ctx.canvas
  for (const op of ops) {
    const rect = eraseRect(op)
    if (!rect) continue
    const [x, y, w, h] = rect
    if (op.line.boxed && coveredFraction(rect, op) < ERASE_COVERED && y - h >= 0) {
      // Mirror what is just above the old spot into it (no empty box left behind).
      // With y' = 2y - y_user, the user rect [y-h, y] lands on [y, y+h], flipped.
      ctx.save()
      ctx.translate(0, 2 * y)
      ctx.scale(1, -1)
      ctx.drawImage(ctx.canvas, x, y - h, w, h, x, y - h, w, h)
      ctx.restore()
    } else {
      ctx.fillStyle = bgrToCss(op.line.bg)
      ctx.fillRect(x, y, w, h)
    }
  }
  for (const op of ops) {
    if (op.plate) {
      ctx.fillStyle = bgrToCss(op.line.bg)
      ctx.fillRect(...op.plate)
    }
    for (const [k, { src, dst }] of op.pastes.entries()) {
      if (op.line.boxed) {
        ctx.drawImage(source, ...src, ...dst)
        continue
      }
      // Text on video / a plain backdrop: keep only the letters.
      const [, , dw, dh] = dst
      if (dw < 1 || dh < 1 || dw > ow || dh > oh) continue
      scratch ??= document.createElement('canvas')
      scratch.width = dw
      scratch.height = dh
      const sctx = scratch.getContext('2d', { willReadFrequently: true })!
      sctx.drawImage(source, ...src, 0, 0, dw, dh)
      const img = sctx.getImageData(0, 0, dw, dh)
      const d = img.data
      // Keep only columns inside the detected text (the padding may hold a shirt or a hand).
      const [segA, segB] = op.segments[k]
      const [coreA, coreB] = op.line.core ?? [0, 1]
      for (let col = 0; col < dw; col++) {
        const at = segA + ((col + 0.5) / dw) * (segB - segA)
        if (at < coreA || at > coreB) for (let row = 0; row < dh; row++) d[(row * dw + col) * 4 + 3] = 0
      }
      const fg = op.line.fg
      if (fg && fg.length === 3) {
        // Keep only pixels of the letters' own colour (same rule as text_layout.letter_mask).
        const [fb, fgG, fr] = fg
        const tol2 = TEXT_COLOR_TOL * TEXT_COLOR_TOL
        for (let i = 0; i < d.length; i += 4) {
          const dr = d[i] - fr, dg = d[i + 1] - fgG, db = d[i + 2] - fb
          if (dr * dr + dg * dg + db * db >= tol2) d[i + 3] = 0
        }
      } else {
        const [b, g, r] = op.line.bg
        const bgGray = 0.299 * r + 0.587 * g + 0.114 * b
        for (let i = 0; i < d.length; i += 4) {
          const gray = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]
          if (Math.abs(gray - bgGray) <= LETTER_CONTRAST) d[i + 3] = 0
        }
      }
      sctx.putImageData(img, 0, 0)
      // Only the letters' own shapes (same stencil the renderer uses).
      const stencil = stencilCanvas(op.line)
      if (stencil) {
        const [segA, segB] = op.segments[k]
        const sx = Math.floor(segA * stencil.width)
        const sw = Math.max(1, Math.floor(segB * stencil.width) - sx)
        sctx.globalCompositeOperation = 'destination-in'
        sctx.imageSmoothingEnabled = false
        sctx.drawImage(stencil, sx, 0, sw, stencil.height, 0, 0, dw, dh)
        sctx.globalCompositeOperation = 'source-over'
      }
      ctx.drawImage(scratch, dst[0], dst[1])
    }
  }
}
