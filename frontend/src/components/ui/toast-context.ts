import { createContext, useContext } from 'react'

export type Tone = 'success' | 'error' | 'info'

export const ToastContext = createContext<(message: string, tone?: Tone) => void>(() => {})

export const useToast = () => useContext(ToastContext)
