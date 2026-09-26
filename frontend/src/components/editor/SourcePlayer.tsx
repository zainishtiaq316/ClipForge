import { useEffect, useRef, useState, type Ref, type RefObject } from 'react'
import { Pause, Play, ScanFace, Volume2, VolumeX } from 'lucide-react'
import type { CameraPathData, Clip, MediaInfo } from '../../lib/api'
import { frameLayout } from '../../lib/textLayout'
import { formatTime } from '../../lib/time'

interface Props {
  ref: Ref<HTMLVideoElement>
  src: string
  media: MediaInfo
  cameraPath: CameraPathData | null
  clip: Clip | null
}

/**
 * The original 16:9 video with the 9:16 crop window drawn on top, so the user
 * can see exactly which part of the frame each vertical clip will keep.
 */
export function SourcePlayer({ ref, src, media, cameraPath, clip }: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const overlayRef = useRef<HTMLDivElement>(null)
  const timeRef = useRef<HTMLSpanElement>(null)
  const [playing, setPlaying] = useState(false)
  const [muted, setMuted] = useState(false)
  const [showCrop, setShowCrop] = useState(true)
  const [loadError, setLoadError] = useState(false)

  const setRefs = (node: HTMLVideoElement | null) => {
    videoRef.current = node
    if (typeof ref === 'function') ref(node)
    else if (ref) (ref as RefObject<HTMLVideoElement | null>).current = node
  }

  // Move the crop overlay and time readout every frame without re-rendering React.
  useEffect(() => {
    let raf = 0
    const tick = () => {
      const video = videoRef.current
      if (video) {
        const t = video.currentTime
        if (overlayRef.current && cameraPath) {
          // Same window the renderer uses: camera path + zoom + shift to include text.
          const { x0, cropW } = frameLayout(cameraPath, clip, t, media.width, media.height)
          overlayRef.current.style.left = `${(x0 / media.width) * 100}%`
          overlayRef.current.style.width = `${(cropW / media.width) * 100}%`
        }
        if (timeRef.current) timeRef.current.textContent = formatTime(t)
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [cameraPath, clip, media.width, media.height])

  const toggle = () => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) video.play().catch(() => {})
    else video.pause()
  }

  return (
    <div className="card overflow-hidden">
      <div className="relative bg-black">
        {/* Width derived from the height cap so the overlay's % coordinates always match the picture. */}
        <div
          className="relative mx-auto"
          style={{
            aspectRatio: `${media.width} / ${media.height}`,
            width: `min(100%, calc(62vh * ${media.width / media.height}))`,
          }}
        >
          <video
            ref={setRefs}
            src={src}
            className="absolute inset-0 size-full cursor-pointer object-contain"
            preload="auto"
            playsInline
            muted={muted}
            onClick={toggle}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onError={() => setLoadError(true)}
          />
          {showCrop && cameraPath && cameraPath.crop_fraction < 1 && (
            <div
              ref={overlayRef}
              className="pointer-events-none absolute inset-y-0 rounded-sm border-2 border-accent shadow-[0_0_0_9999px_rgb(0_0_0/0.55)]"
              aria-hidden
            >
              <span className="absolute top-2 left-1/2 -translate-x-1/2 rounded bg-accent px-1.5 py-0.5 text-[10px] font-semibold text-white">
                9:16
              </span>
            </div>
          )}
          {loadError && (
            <div className="absolute inset-0 grid place-items-center bg-black/80 p-6 text-center text-sm text-muted">
              This video can't be played in your browser, but clips can still be exported.
            </div>
          )}
        </div>
      </div>
      <div className="flex items-center gap-1 border-t border-line px-2 py-1.5 sm:gap-2 sm:px-3">
        <button
          onClick={toggle}
          className="grid size-9 place-items-center rounded-lg text-fg hover:bg-surface-3"
          aria-label={playing ? 'Pause' : 'Play'}
          title="Play / pause (Space)"
        >
          {playing ? <Pause className="size-4" /> : <Play className="size-4 fill-current" />}
        </button>
        <button
          onClick={() => setMuted((m) => !m)}
          className="grid size-9 place-items-center rounded-lg text-muted hover:bg-surface-3 hover:text-fg"
          aria-label={muted ? 'Unmute' : 'Mute'}
        >
          {muted ? <VolumeX className="size-4" /> : <Volume2 className="size-4" />}
        </button>
        <p className="tabular font-mono text-xs text-muted">
          <span ref={timeRef} className="text-fg">
            0:00.0
          </span>{' '}
          / {formatTime(media.duration)}
        </p>
        <div className="flex-1" />
        <label className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-xs text-muted select-none hover:text-fg">
          <input type="checkbox" className="accent-accent" checked={showCrop} onChange={(e) => setShowCrop(e.target.checked)} />
          <ScanFace className="size-3.5" />
          <span className="hidden sm:inline">Show crop area</span>
        </label>
      </div>
    </div>
  )
}
