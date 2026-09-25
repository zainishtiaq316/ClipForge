import { Crosshair, Scissors, Smartphone } from 'lucide-react'
import { ImportCard } from './ImportCard'
import { RecentProjects } from './RecentProjects'

const features = [
  {
    icon: Scissors,
    title: 'Smart segmentation',
    text: 'Cuts on pauses in speech and scene changes, so clips never stop mid-sentence.',
  },
  {
    icon: Crosshair,
    title: 'Subject tracking',
    text: 'Follows faces (or the main moving element) with a smooth virtual camera instead of a center crop.',
  },
  {
    icon: Smartphone,
    title: 'Ready to post',
    text: '1080×1920 MP4 files for TikTok, Reels and Shorts. Export one clip or all of them as a ZIP.',
  },
]

export function HomeView() {
  return (
    <div className="mx-auto max-w-6xl px-4 pt-10 pb-20 sm:px-6 sm:pt-16">
      <section className="mx-auto max-w-3xl text-center">
        <span className="inline-flex items-center gap-2 rounded-full border border-accent/30 bg-accent-soft px-3 py-1 text-xs font-medium text-accent">
          16:9 → 9:16 · Free & open source
        </span>
        <h1 className="mt-5 text-4xl font-bold tracking-tight text-balance sm:text-5xl">
          Turn long videos into <span className="bg-gradient-to-r from-accent to-[#c08cff] bg-clip-text text-transparent">vertical clips</span>
        </h1>
        <p className="mx-auto mt-4 max-w-xl text-base text-muted text-pretty sm:text-lg">
          Upload a video or paste a YouTube link. ClipForge splits it into clips and keeps the speaker in frame. Review,
          adjust and export.
        </p>
      </section>

      <div className="mx-auto mt-10 max-w-2xl">
        <ImportCard />
      </div>

      <section className="mx-auto mt-14 grid max-w-5xl gap-4 sm:grid-cols-3">
        {features.map(({ icon: Icon, title, text }) => (
          <div key={title} className="rounded-2xl border border-line/70 bg-surface/60 p-5">
            <div className="grid size-9 place-items-center rounded-lg bg-accent-soft text-accent">
              <Icon className="size-[18px]" />
            </div>
            <h3 className="mt-3 text-sm font-semibold">{title}</h3>
            <p className="mt-1 text-sm leading-relaxed text-muted">{text}</p>
          </div>
        ))}
      </section>

      <RecentProjects />
    </div>
  )
}
