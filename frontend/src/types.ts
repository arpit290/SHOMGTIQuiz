export type GameStatus = 'LOBBY' | 'ACTIVE' | 'PAUSED' | 'GAME_OVER'
export type GamePhase = 'OPENING' | 'MAIN' | 'FINAL' | 'GAME_OVER'

export type InventoryItem = {
  id: string
  type: string
  name: string
  description: string
}

export type VisibleOpponent = {
  id: string
  name: string
  statusEffect: string
  health: number
  maxHealth: number
}

export type Zone = {
  id: string
  name: string
  description: string
  connectedZones: string[]
  playerCount?: number
  lootCount?: number
  hazard?: boolean
}

export type Player = {
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
  currentAction: string | null
  actionTaken: boolean
  actionDeadline: string | null
  statusEffect: string
  lastResult: string
  kills: number
  inventory: InventoryItem[]
  joinedAt?: string
}

export type GameEvent = {
  id: string
  timestamp: string
  type: string
  message: string
}

export type PlayerGameState = {
  gameId: string
  status: GameStatus
  phase: GamePhase
  round: number
  roundDurationSeconds: number
  serverNow: string
  roundDeadline: string | null
  player: Player
  availableActions: string[]
  currentZone: Zone
  adjacentZones: Zone[]
  visibleOpponents: VisibleOpponent[]
  zoneLootCount: number
  zoneHazard: boolean
  hazardDamage: number
  playerCount: number
  aliveCount: number
  maxPlayers: number
  winnerId: string | null
  events: GameEvent[]
}

export type AdminState = {
  gameId: string
  status: GameStatus
  phase: GamePhase
  round: number
  roundDurationSeconds: number
  serverNow: string
  roundDeadline: string | null
  playerCount: number
  aliveCount: number
  maxPlayers: number
  winnerId: string | null
  zones: Zone[]
  players: Player[]
  events: GameEvent[]
}
