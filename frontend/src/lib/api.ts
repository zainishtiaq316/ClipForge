// Typed client for the ClipForge API. All requests are same-origin (`/api`),
// proxied by Vite in development and served by FastAPI in production.

export type ProjectStatus = 'queued' | 'downloading' | 'analyzing' | 'ready' | 'failed'
export type Framing = 'auto' | 'track' | 'center'

export interface MediaInfo {
  duration: number
  width: number
  height: number
  fps: number
  has_audio: boolean
}

export interface Clip {
  id: string
  start: number
  end: number
  framing: Framing
  /** 0 = full-frame 9:16, 1 = whole width with solid bars above and below. */
  zoom: number
  title: string
}

export interface Project {
  id: string
  name: string
  source_type: 'upload' | 'youtube'
  source_url: string | null
  status: ProjectStatus
  stage: string
  progress: number
  error: string | null
  created_at: number
  target_length: number
  media: MediaInfo | null
  scene_cuts: number[]
  clips: Clip[]
  detector: string
}

export interface TextLineData {
  x: number
  y: number
  w: number
  h: number
  cuts: number[]
  boxed: boolean
  bg: number[] // BGR
  ink: number
  fg?: number[] // letters' colour (BGR) for text that isn't on a caption box
  core?: [number, number] // where the letters are across the rect (0..1); the rest is padding
}

/** Overlay text on screen from `start` to `end`, re-flowed into the vertical frame. */
export interface TextLayoutData {
  start: number
  end: number
  block: boolean
  lines: TextLineData[]
}

export interface CameraPathData {
  crop_fraction: number
  times: number[]
  xs: number[]
  /** Automatic zoom-out per keyframe (whole person / title card in frame). */
  zs?: number[]
  text_layouts: TextLayoutData[]
}

export type ExportStatus = 'queued' | 'rendering' | 'packaging' | 'done' | 'failed'

export interface ExportJob {
  id: string
  project_id: string
  clip_ids: string[]
  status: ExportStatus
  progress: number
  current: number
  error: string | null
  file_name: string | null
}

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function errorMessage(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  // FastAPI validation errors: [{ msg: "Value error, ..." }]
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg).replace(/^Value error, /, '')
  return fallback
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, init)
  } catch {
    throw new ApiError(0, 'Cannot reach the ClipForge server. Is it still running?')
  }
  if (res.status === 204) return undefined as T
  const body = await res.json().catch(() => null)
  if (!res.ok) throw new ApiError(res.status, errorMessage(body, `Request failed (${res.status})`))
  return body as T
}

const json = (method: string, data: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data),
})

export const api = {
  listProjects: () => request<Project[]>('/api/projects'),
  getProject: (id: string) => request<Project>(`/api/projects/${id}`),
  deleteProject: (id: string) => request<void>(`/api/projects/${id}`, { method: 'DELETE' }),
  importYouTube: (url: string, targetLength: number) =>
    request<Project>('/api/projects/youtube', json('POST', { url, target_length: targetLength })),
  cameraPath: (id: string) => request<CameraPathData>(`/api/projects/${id}/camera-path`),
  saveClips: (id: string, clips: Clip[]) => request<Project>(`/api/projects/${id}/clips`, json('PUT', { clips })),
  resegment: (id: string, targetLength: number) =>
    request<Project>(`/api/projects/${id}/resegment`, json('POST', { target_length: targetLength })),
  startExport: (id: string, clipIds: string[]) =>
    request<ExportJob>(`/api/projects/${id}/exports`, json('POST', { clip_ids: clipIds })),
  exportStatus: (jobId: string) => request<ExportJob>(`/api/exports/${jobId}`),
  videoUrl: (id: string) => `/api/projects/${id}/video`,
  downloadUrl: (jobId: string) => `/api/exports/${jobId}/download`,

  /** Upload with progress events (fetch has no upload progress, so this uses XHR). */
  uploadVideo(
    file: File,
    targetLength: number,
    onProgress: (fraction: number) => void,
    signal?: AbortSignal,
  ): Promise<Project> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      const params = new URLSearchParams({ filename: file.name, target_length: String(targetLength) })
      xhr.open('POST', `/api/projects/upload?${params}`)
      xhr.setRequestHeader('Content-Type', 'application/octet-stream')
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total)
      xhr.onload = () => {
        let body: unknown = null
        try {
          body = JSON.parse(xhr.responseText)
        } catch {
          /* non-JSON error page */
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body as Project)
        else reject(new ApiError(xhr.status, errorMessage(body, `Upload failed (${xhr.status})`)))
      }
      xhr.onerror = () => reject(new ApiError(0, 'Upload failed. Check your connection and try again.'))
      xhr.onabort = () => reject(new ApiError(0, 'Upload cancelled'))
      signal?.addEventListener('abort', () => xhr.abort())
      xhr.send(file)
    })
  },
}
