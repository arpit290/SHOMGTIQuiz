export type GameStatus = 'LOBBY' | 'STARTING' | 'ACTIVE' | 'PAUSED' | 'GAME_OVER'

export type Player = {
  id: string
  name: string
  alive: boolean
  connected: boolean
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
  player: Player
  playerCount: number
  aliveCount: number
  maxPlayers: number
  events: GameEvent[]
}

export type AdminState = {
  gameId: string
  status: GameStatus
  playerCount: number
  aliveCount: number
  maxPlayers: number
  players: Player[]
  events: GameEvent[]
}
