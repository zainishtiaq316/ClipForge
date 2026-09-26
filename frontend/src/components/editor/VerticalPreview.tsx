import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react'
import { Crosshair, Smartphone } from 'lucide-react'
import type { CameraPathData, Clip } from '../../lib/api'
import { drawTextOps, frameLayout, layoutOps, videoHeight, videoTop, type View } from '../../lib/textLayout'

interface Props {
  videoRef: RefObject<HTMLVideoElement | null>
  cameraPath: CameraPathData | null
  clips: Clip[]
  selected: Clip | null
}

const FRAMING_LABEL = { auto: 'Smart framing', track: 'Subject tracking', center: 'Centered' }

const CANVAS_W = 540
const CANVAS_H = 960

/**
 * Live 9:16 preview drawn from the source <video> onto a canvas, using the same
 * camera path, zoom and text layout geometry as the renderer. No rendering needed.
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
        const srcW = video.videoWidth
        const srcH = video.videoHeight
        const { x0, cropW, layout } = frameLayout(cameraPath, active, t, srcW, srcH)
        const view: View = { srcW, srcH, cropX0: x0, cropW, outW: CANVAS_W, outH: CANVAS_H }
        const top = videoTop(view)
        const vh = videoHeight(view)

        // Solid bars (only visible when zoomed out) + the video window.
        ctx.fillStyle = '#000'
        ctx.fillRect(0, 0, CANVAS_W, CANVAS_H)
        ctx.drawImage(video, x0, 0, cropW, srcH, 0, top, CANVAS_W, vh)

        // On-screen text the crop would cut: erase the cut original, draw it re-flowed.
        if (layout) drawTextOps(ctx, video, layoutOps(layout.lines, view))

        if (labelRef.current) labelRef.current.textContent = active ? active.title || 'Clip' : 'Outside clips'
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
          {FRAMING_LABEL[selected?.framing ?? 'auto']}
          {selected && selected.zoom > 0 ? ` · zoom out ≥ ${Math.round(selected.zoom * 100)}%` : ''}
        </span>
      </div>
      <div className="mx-auto w-full max-w-[220px] sm:max-w-[260px] lg:max-w-[calc(56vh*0.5625)]">
        <div className="relative aspect-[9/16] overflow-hidden rounded-[1.4rem] border-4 border-surface-3 bg-black shadow-2xl">
          <canvas ref={canvasRef} width={CANVAS_W} height={CANVAS_H} className="size-full" />
          <span
            ref={labelRef}
            className="absolute top-3 left-1/2 max-w-[85%] -translate-x-1/2 truncate rounded-full bg-black/60 px-2.5 py-1 text-[11px] font-medium text-white backdrop-blur"
          />
        </div>
      </div>
      <p className="mt-3 text-center text-[11px] text-subtle">Exports at 1080×1920 · H.264 MP4</p>
    </div>
  )
}
