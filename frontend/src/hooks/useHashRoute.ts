import { useEffect, useState } from 'react'

export type Route = { name: 'home' } | { name: 'project'; id: string }

function parse(hash: string): Route {
  const match = hash.match(/^#\/project\/([a-f0-9]{32})$/)
  return match ? { name: 'project', id: match[1] } : { name: 'home' }
}

/** Minimal hash router: two screens don't justify a routing dependency. */
export function useHashRoute(): Route {
  const [route, setRoute] = useState(() => parse(window.location.hash))
  useEffect(() => {
    const onChange = () => setRoute(parse(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

export const navigate = {
  home: () => (window.location.hash = '#/'),
  project: (id: string) => (window.location.hash = `#/project/${id}`),
}
