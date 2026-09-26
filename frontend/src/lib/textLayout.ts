// Mirror of backend/app/services/text_layout.py (geometry only) and renderer.crop_width,
// so the live 9:16 preview shows the same framing and re-flowed text as the export.
import type { CameraPathData, Clip, TextLayoutData, TextLineData } from './api'
import { cropCenterAt } from './cameraPath'

const MAX_LINE_WIDTH = 0.9
const MAX_SEGMENTS = 3
const SEGMENT_GAP = 0.18
const PLATE_PAD = 0.22
const SAFE_TOP = 0.05
const SAFE_BOTTOM = 0.95
const MIN_WRAP_SCALE = 0.85
const BAR_TEXT_HEIGHT = 0.055
const WINDOW_SUBJECT_MARGIN = 0.15
const WINDOW_RAMP_S = 0.3

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
}

function block(line: TextLineData, W: number, H: number, scale: number, maxW: number) {
  const [segments, lineS] = fitLine(line, W, scale, maxW)
  const segH = H * lineS
  const gap = SEGMENT_GAP * segH
  return { segments, lineS, segH, gap, blockH: segments.length * segH + (segments.length - 1) * gap }
}

export function layoutOps(lines: TextLineData[], v: View): LineOps[] {
  const s = viewScale(v)
  const maxW = MAX_LINE_WIDTH * v.outW
  const cropX1 = v.cropX0 + v.cropW
  const top = videoTop(v)
  const vh = videoHeight(v)
  const barTop = top
  const barBottom = v.outH - top - vh

  const blocks: { line: TextLineData; erase: Rect | null; b: ReturnType<typeof block>; y: number }[] = []
  for (const line of lines) {
    const X = line.x * v.srcW, Y = line.y * v.srcH, W = line.w * v.srcW, H = line.h * v.srcH
    if (X >= v.cropX0 - 1 && X + W <= cropX1 + 1) continue
    let erase: Rect | null = null
    const vx0 = Math.max(X, v.cropX0), vx1 = Math.min(X + W, cropX1)
    if (vx1 > vx0) erase = [Math.trunc((vx0 - v.cropX0) * s), Math.trunc(top + Y * s), Math.ceil((vx1 - vx0) * s), Math.ceil(H * s)]

    let b = block(line, W, H, s, maxW)
    let anchor = top + (Y + H / 2) * s
    const relative = (Y + H / 2) / v.srcH
    const barH = relative >= 0.5 ? barBottom : barTop
    if (barH > 0) {
      const bigger = block(line, W, H, Math.max(s, (BAR_TEXT_HEIGHT * v.outH) / H), maxW)
      for (const candidate of [bigger, b]) {
        if (candidate.blockH * 1.25 <= barH) {
          b = candidate
          anchor = relative >= 0.5 ? top + vh + barH / 2 : barH / 2
          break
        }
      }
    }
    blocks.push({ line, erase, b, y: anchor - b.blockH / 2 })
  }

  blocks.sort((a, c) => a.y - c.y)
  for (let i = 1; i < blocks.length; i++) {
    const prev = blocks[i - 1]
    blocks[i].y = Math.max(blocks[i].y, prev.y + prev.b.blockH + prev.b.segH * 0.6)
  }
  if (blocks.length) {
    const last = blocks[blocks.length - 1]
    const shift = Math.max(0, last.y + last.b.blockH - SAFE_BOTTOM * v.outH)
    const topRoom = blocks[0].y - shift - SAFE_TOP * v.outH
    for (const bl of blocks) bl.y -= shift + Math.min(0, topRoom)
  }

  return blocks.map(({ line, erase, b, y: blockTop }) => {
    const X = line.x * v.srcW, Y = line.y * v.srcH, W = line.w * v.srcW, H = line.h * v.srcH
    let y = blockTop
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
      plate = [Math.trunc((v.outW - widest) / 2 - pad), Math.trunc(blockTop - pad), Math.trunc(widest + 2 * pad), Math.trunc(b.blockH + 2 * pad)]
    }
    return { line, erase, plate, pastes }
  })
}

export interface FrameLayout {
  x0: number // crop window left edge, source px
  cropW: number
  layout: TextLayoutData | null // text to re-flow (null when none, or when it fits the window)
}

/** Everything the preview needs to draw one frame, exactly like the renderer. */
export function frameLayout(path: CameraPathData | null, clip: Clip | null | undefined, t: number, srcW: number, srcH: number): FrameLayout {
  const framing = clip?.framing ?? 'auto'
  const cropW = cropWidth(srcW, srcH, clip?.zoom ?? 0)
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
