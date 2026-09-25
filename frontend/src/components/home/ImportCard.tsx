import { useRef, useState, type DragEvent, type FormEvent } from 'react'
import { FileVideo, Link2, UploadCloud, X } from 'lucide-react'
import { api, ApiError } from '../../lib/api'
import { navigate } from '../../hooks/useHashRoute'
import { Button } from '../ui/Button'
import { ProgressBar } from '../ui/ProgressBar'
import { useToast } from '../ui/toast-context'

const LENGTHS = [15, 30, 45, 60, 90]
const ACCEPT = '.mp4,.mov,.m4v,.mkv,.webm,.avi,video/*'

function formatBytes(bytes: number) {
  if (bytes > 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

export function ImportCard() {
  const toast = useToast()
  const [mode, setMode] = useState<'file' | 'youtube'>('file')
  const [file, setFile] = useState<File | null>(null)
  const [url, setUrl] = useState('')
  const [length, setLength] = useState(30)
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const pickFile = (candidate: File | undefined | null) => {
    if (!candidate) return
    if (!/\.(mp4|mov|m4v|mkv|webm|avi)$/i.test(candidate.name)) {
      toast('Please choose a video file (MP4, MOV, MKV, WEBM or AVI).', 'error')
      return
    }
    setFile(candidate)
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    setMode('file')
    pickFile(e.dataTransfer.files?.[0])
  }

  const submit = async (e?: FormEvent) => {
    e?.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      if (mode === 'file') {
        if (!file) return toast('Choose a video file first.', 'error')
        abortRef.current = new AbortController()
        setUploadProgress(0)
        const project = await api.uploadVideo(file, length, setUploadProgress, abortRef.current.signal)
        navigate.project(project.id)
      } else {
        if (!url.trim()) return toast('Paste a YouTube link first.', 'error')
        const project = await api.importYouTube(url.trim(), length)
        navigate.project(project.id)
      }
    } catch (err) {
      if (!(err instanceof ApiError && err.message === 'Upload cancelled')) {
        toast(err instanceof Error ? err.message : 'Something went wrong', 'error')
      }
    } finally {
      setBusy(false)
      setUploadProgress(null)
    }
  }

  const uploading = uploadProgress !== null

  return (
    <form
      onSubmit={submit}
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className="card p-2"
    >
      <div className="grid grid-cols-2 gap-1 rounded-xl bg-surface-2 p-1" role="tablist">
        {(
          [
            ['file', 'Upload a file', UploadCloud],
            ['youtube', 'YouTube link', Link2],
          ] as const
        ).map(([key, label, Icon]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={mode === key}
            disabled={busy}
            onClick={() => setMode(key)}
            className={`flex h-10 items-center justify-center gap-2 rounded-lg text-sm font-medium transition-colors ${
              mode === key ? 'bg-surface-3 text-fg shadow-sm' : 'text-muted hover:text-fg'
            }`}
          >
            <Icon className="size-4" /> {label}
          </button>
        ))}
      </div>

      <div className="p-4 sm:p-5">
        {mode === 'file' ? (
          file ? (
            <div className="flex items-center gap-3 rounded-xl border border-line bg-surface-2 p-3.5">
              <div className="grid size-11 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent">
                <FileVideo className="size-5" />
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{file.name}</p>
                <p className="text-xs text-muted">{formatBytes(file.size)}</p>
                {uploading && (
                  <div className="mt-2 flex items-center gap-2">
                    <ProgressBar value={uploadProgress} className="flex-1" />
                    <span className="tabular w-10 text-right text-xs text-muted">{Math.round(uploadProgress * 100)}%</span>
                  </div>
                )}
              </div>
              <Button
                variant="ghost"
                size="icon"
                aria-label={uploading ? 'Cancel upload' : 'Remove file'}
                onClick={() => (uploading ? abortRef.current?.abort() : setFile(null))}
                icon={<X className="size-4" />}
              />
            </div>
          ) : (
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              className={`flex w-full flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
                dragging ? 'border-accent bg-accent-soft' : 'border-line-strong hover:border-accent/60 hover:bg-surface-2'
              }`}
            >
              <div className="grid size-12 place-items-center rounded-full bg-surface-3 text-accent">
                <UploadCloud className="size-6" />
              </div>
              <p className="mt-3 text-sm font-medium">
                Drag & drop your video here, or <span className="text-accent">browse</span>
              </p>
              <p className="mt-1 text-xs text-subtle">MP4, MOV, MKV, WEBM or AVI · horizontal (16:9) works best</p>
            </button>
          )
        ) : (
          <div>
            <label htmlFor="yt-url" className="mb-1.5 block text-xs font-medium text-muted">
              YouTube video link
            </label>
            <div className="relative">
              <Link2 className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-subtle" />
              <input
                id="yt-url"
                type="url"
                inputMode="url"
                autoComplete="off"
                placeholder="https://www.youtube.com/watch?v=…"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                className="field pl-10"
                disabled={busy}
              />
            </div>
            <p className="mt-2 text-xs text-subtle">Downloads up to 1080p. Only use videos you have the rights to edit.</p>
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => {
            pickFile(e.target.files?.[0])
            e.target.value = ''
          }}
        />

        <fieldset className="mt-5">
          <legend className="mb-2 text-xs font-medium text-muted">Target clip length</legend>
          <div className="flex flex-wrap gap-2">
            {LENGTHS.map((value) => (
              <button
                key={value}
                type="button"
                disabled={busy}
                onClick={() => setLength(value)}
                aria-pressed={length === value}
                className={`h-9 min-w-14 rounded-lg border px-3 text-sm font-medium transition-colors ${
                  length === value
                    ? 'border-accent bg-accent-soft text-fg'
                    : 'border-line text-muted hover:border-line-strong hover:text-fg'
                }`}
              >
                {value}s
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-subtle">You can fine-tune every clip afterwards.</p>
        </fieldset>

        <Button type="submit" variant="primary" size="lg" className="mt-6 w-full" loading={busy}>
          {uploading ? 'Uploading…' : 'Create vertical clips'}
        </Button>
      </div>
    </form>
  )
}
