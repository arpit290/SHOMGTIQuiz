const API_PORT = 8000

function hostBase() {
  const hostname = window.location.hostname || 'localhost'
  return `${window.location.protocol}//${hostname}:${API_PORT}`
}

const API_BASE = import.meta.env.VITE_API_BASE || hostBase()
const WS_BASE = import.meta.env.VITE_WS_BASE || API_BASE.replace(/^http/, 'ws')

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
    }
    sessionToken: string
    game: { gameId: string; status: string; phase: string; round: number; playerCount: number; maxPlayers: number }
  }>
}

export async function submitAction(token: string, playerId: string, action: string, targetZoneId?: string) {
  const response = await fetch(`${API_BASE}/api/action`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Player-Token': token,
    },
    body: JSON.stringify({ playerId, action, targetZoneId }),
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

export async function adminAction(token: string, action: string) {
  const response = await fetch(`${API_BASE}/api/admin/action`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Token': token,
    },
    body: JSON.stringify({ action }),
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
