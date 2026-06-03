import { ROUTE_ACCESS } from '../../contexts/PersonaContext'
import { PAGE_TITLES } from '../Sidebar'

// The content pages a persona can reach, as [{ path, label }], derived from the
// single-source-of-truth ROUTE_ACCESS map crossed with the persona's access
// keys. Used by the Account section (accessible-pages list) and the Appearance
// section (default-landing-page picker). /personas and /settings are universal
// and intentionally absent from ROUTE_ACCESS, so callers add them as needed.
export function accessibleRoutes(persona) {
  if (!persona) return []
  return Object.entries(ROUTE_ACCESS)
    .filter(([, key]) => persona.access.includes(key))
    .map(([path]) => ({ path, label: PAGE_TITLES[path] || path }))
}
