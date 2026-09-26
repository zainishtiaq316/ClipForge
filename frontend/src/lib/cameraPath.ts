import type { CameraPathData, Framing } from './api'

/**
 * Crop centre (0..1 of source width) at time `t`: linear interpolation of the
 * server's camera path, exactly matching NumPy's `np.interp` used by the renderer,
 * so the live preview matches the exported file.
 */
export function cropCenterAt(path: CameraPathData | null, t: number, framing: Framing = 'auto'): number {
  if (!path || framing === 'center' || framing === 'fit' || path.times.length === 0) return 0.5
  const { times, xs } = path
  if (t <= times[0]) return xs[0]
  const last = times.length - 1
  if (t >= times[last]) return xs[last]
  let lo = 0
  let hi = last
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= t) lo = mid
    else hi = mid
  }
  const span = times[hi] - times[lo]
  const k = span > 0 ? (t - times[lo]) / span : 0
  return xs[lo] + (xs[hi] - xs[lo]) * k
}

/** Whether time `t` uses the fit layout (whole frame over a blurred background). */
export function isFitAt(path: CameraPathData | null, t: number, framing: Framing = 'auto'): boolean {
  if (framing === 'fit') return true
  if (framing !== 'auto' || !path?.fit_ranges) return false
  return path.fit_ranges.some(([start, end]) => t >= start && t < end)
}

/** Left edge of the crop window (0..1), clamped inside the frame. */
export function cropLeft(center: number, fraction: number): number {
  return Math.min(1 - fraction, Math.max(0, center - fraction / 2))
}
