function hostBase() {
  // In development, Vite proxies /api and /ws to FastAPI.
  // In production, FastAPI can serve the React build from the same origin.
  return window.location.origin
}

const API_BASE = (import.meta.env.VITE_API_BASE || hostBase()).replace(/\/$/, '')
const WS_BASE = (import.meta.env.VITE_WS_BASE || API_BASE.replace(/^http/, 'ws')).replace(/\/$/, '')

async function parseResponse(response: Response) {
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? 'Request failed')
  return payload
}

export async function joinGame(name: string) {
  const response = await fetch(`${API_BASE}/api/join`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  return parseResponse(response) as Promise<{
    player: {
      id: string
      name: string
      health: number
      maxHealth: number
      attack: number
      speed: number
      zoneId: string
      zoneName: string
      alive: boolean
      connected: boolean
      inventory: { id: string; type: string; name: string; description: string }[]
    }
    sessionToken: string
    game: { gameId: string; status: string; phase: string; round: number; playerCount: number; maxPlayers: number }
  }>
}

export async function submitAction(
  token: string,
  playerId: string,
  action: string,
  targetZoneId?: string,
  targetPlayerId?: string,
  itemId?: string,
) {
  const response = await fetch(`${API_BASE}/api/action`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Player-Token': token,
    },
    body: JSON.stringify({ playerId, action, targetZoneId, targetPlayerId, itemId }),
  })
  return parseResponse(response)
}

export async function adminLogin(token: string) {
  const response = await fetch(`${API_BASE}/api/admin/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
  return parseResponse(response)
}

export type AdminActionOptions = {
  targetZoneId?: string
  targetPlayerId?: string
  itemType?: string
  message?: string
  reason?: string
  value?: number
  attack?: number
  speed?: number
}

export async function adminAction(token: string, action: string, options: AdminActionOptions = {}) {
  const response = await fetch(`${API_BASE}/api/admin/action`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Token': token,
    },
    body: JSON.stringify({ action, ...options }),
  })
  return parseResponse(response)
}

export async function getAdminState(token: string) {
  const response = await fetch(`${API_BASE}/api/admin/state`, {
    headers: { 'X-Admin-Token': token },
  })
  return parseResponse(response)
}

export function playerWsUrl(playerId: string, token: string) {
  return `${WS_BASE}/ws/player/${encodeURIComponent(playerId)}?token=${encodeURIComponent(token)}`
}

export function adminWsUrl(token: string) {
  return `${WS_BASE}/ws/admin?token=${encodeURIComponent(token)}`
}
