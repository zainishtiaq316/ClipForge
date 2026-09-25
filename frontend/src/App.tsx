import { HomeView } from './components/home/HomeView'
import { ProjectView } from './components/ProjectView'
import { ToastProvider } from './components/ui/Toast'
import { navigate, useHashRoute } from './hooks/useHashRoute'

function Logo() {
  return (
    <button onClick={navigate.home} className="flex items-center gap-2.5" aria-label="ClipForge home">
      <span className="grid size-8 place-items-center rounded-lg bg-accent shadow-[0_6px_20px_-6px_rgb(124_92_255/0.9)]">
        <svg viewBox="0 0 32 32" className="size-5" aria-hidden>
          <rect x="11" y="6" width="10" height="20" rx="2.5" fill="none" stroke="#fff" strokeWidth="2.6" />
          <path d="M4 12v8M28 12v8" stroke="#fff" strokeOpacity=".55" strokeWidth="2.6" strokeLinecap="round" />
        </svg>
      </span>
      <span className="text-[15px] font-semibold tracking-tight">
        Clip<span className="text-accent">Forge</span>
      </span>
    </button>
  )
}

export default function App() {
  const route = useHashRoute()
  return (
    <ToastProvider>
      <div className="flex min-h-dvh flex-col">
        <header className="sticky top-0 z-30 border-b border-line/70 bg-bg/80 backdrop-blur-xl">
          <div className="mx-auto flex h-14 max-w-[1500px] items-center justify-between px-4 sm:px-6">
            <Logo />
            <div className="flex items-center gap-3 text-xs text-muted">
              <span className="hidden items-center gap-1.5 rounded-full border border-line px-2.5 py-1 sm:inline-flex">
                <span className="size-1.5 rounded-full bg-success" /> Runs 100% locally
              </span>
            </div>
          </div>
        </header>
        <main className="flex-1">
          {route.name === 'project' ? <ProjectView key={route.id} projectId={route.id} /> : <HomeView />}
        </main>
      </div>
    </ToastProvider>
  )
}
