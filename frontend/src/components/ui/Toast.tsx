import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'

import { ToastContext, type Tone } from './toast-context'

interface Toast {
  id: number
  tone: Tone
  message: string
}

const icons = {
  success: <CheckCircle2 className="size-5 text-success" />,
  error: <AlertCircle className="size-5 text-danger" />,
  info: <Info className="size-5 text-accent" />,
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((t) => t.id !== id)), [])

  const push = useCallback(
    (message: string, tone: Tone = 'info') => {
      const id = Date.now() + Math.random()
      setToasts((all) => [...all.slice(-3), { id, tone, message }])
      setTimeout(() => dismiss(id), tone === 'error' ? 7000 : 4000)
    },
    [dismiss],
  )

  const value = useMemo(() => push, [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 top-4 z-50 flex flex-col items-center gap-2 px-4" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="card pointer-events-auto flex w-full max-w-md items-start gap-3 bg-surface-2 px-4 py-3 text-sm"
            role={t.tone === 'error' ? 'alert' : 'status'}
          >
            {icons[t.tone]}
            <p className="flex-1 pt-0.5 leading-snug">{t.message}</p>
            <button className="text-subtle hover:text-fg" onClick={() => dismiss(t.id)} aria-label="Dismiss">
              <X className="size-4" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
