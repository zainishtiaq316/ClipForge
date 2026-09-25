import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react'
import { Crosshair, Smartphone } from 'lucide-react'
import type { CameraPathData, Clip } from '../../lib/api'
import { cropCenterAt, cropLeft } from '../../lib/cameraPath'

interface Props {
  videoRef: RefObject<HTMLVideoElement | null>
  cameraPath: CameraPathData | null
  clips: Clip[]
  selected: Clip | null
}

const CANVAS_W = 540
const CANVAS_H = 960

/** Mirrors the renderer's "fit" filter: blurred cover background + letterboxed frame. */
function drawFit(ctx: CanvasRenderingContext2D, video: HTMLVideoElement, vw: number, vh: number) {
  const cover = Math.max(CANVAS_W / vw, CANVAS_H / vh)
  ctx.save()
  ctx.filter = 'blur(18px) brightness(0.8)'
  ctx.drawImage(video, (CANVAS_W - vw * cover) / 2, (CANVAS_H - vh * cover) / 2, vw * cover, vh * cover)
  ctx.restore()
  const fit = CANVAS_W / vw
  ctx.drawImage(video, 0, (CANVAS_H - vh * fit) / 2, CANVAS_W, vh * fit)
}

/**
 * Live 9:16 preview drawn from the source <video> onto a canvas, using the same
 * camera path the renderer uses. Instant feedback, no rendering needed.
 */
export function VerticalPreview({ videoRef, cameraPath, clips, selected }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const labelRef = useRef<HTMLSpanElement>(null)
  const latest = useRef({ cameraPath, clips, selected })
  useLayoutEffect(() => {
    latest.current = { cameraPath, clips, selected }
  })

  useEffect(() => {
    const ctx = canvasRef.current?.getContext('2d')
    if (!ctx) return
    ctx.imageSmoothingQuality = 'high'
    let raf = 0
    const draw = () => {
      const video = videoRef.current
      const { cameraPath, clips, selected } = latest.current
      if (video && video.readyState >= 2 && video.videoWidth) {
        const t = video.currentTime
        const active = selected && t >= selected.start && t <= selected.end ? selected : clips.find((c) => t >= c.start && t < c.end)
        const vw = video.videoWidth
        const vh = video.videoHeight
        if (active?.framing === 'fit') {
          drawFit(ctx, video, vw, vh)
          if (labelRef.current) labelRef.current.textContent = active.title || 'Clip'
          raf = requestAnimationFrame(draw)
          return
        }
        const frac = cameraPath?.crop_fraction ?? Math.min(1, (vh * 9) / 16 / vw)
        const sw = frac * vw
        const sh = frac >= 1 ? Math.min(vh, (vw * 16) / 9) : vh
        const sx = cropLeft(cropCenterAt(cameraPath, t, active?.framing ?? 'auto'), frac) * vw
        const sy = (vh - sh) / 2
        ctx.drawImage(video, sx, sy, sw, sh, 0, 0, CANVAS_W, CANVAS_H)
        if (labelRef.current) {
          labelRef.current.textContent = active ? active.title || 'Clip' : 'Outside clips'
        }
      }
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [videoRef])

  return (
    <div className="card flex flex-col p-3 sm:p-4">
      <div className="mb-3 flex items-center justify-between gap-2 text-xs">
        <span className="flex items-center gap-1.5 font-medium text-muted">
          <Smartphone className="size-3.5" /> Vertical preview
        </span>
        <span className="flex items-center gap-1 rounded-full bg-accent-soft px-2 py-0.5 text-[11px] font-medium text-accent">
          <Crosshair className="size-3" />
          {selected?.framing === 'center' ? 'Centered' : selected?.framing === 'fit' ? 'Fit + blur' : 'Auto-tracking'}
        </span>
      </div>
      <div className="mx-auto w-full max-w-[220px] sm:max-w-[260px] lg:max-w-[calc(56vh*0.5625)]">
        <div className="relative aspect-[9/16] overflow-hidden rounded-[1.4rem] border-4 border-surface-3 bg-black shadow-2xl">
          <canvas ref={canvasRef} width={CANVAS_W} height={CANVAS_H} className="size-full" />
          <span
            ref={labelRef}
            className="absolute bottom-3 left-1/2 max-w-[85%] -translate-x-1/2 truncate rounded-full bg-black/60 px-2.5 py-1 text-[11px] font-medium text-white backdrop-blur"
          />
        </div>
      </div>
      <p className="mt-3 text-center text-[11px] text-subtle">Exports at 1080×1920 · H.264 MP4</p>
    </div>
  )
}
