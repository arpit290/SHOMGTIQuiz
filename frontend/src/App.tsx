import { FormEvent, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { adminAction, adminLogin, adminWsUrl, getAdminState, getPlayerState, getSpectateState, joinGame, playerWsUrl, spectateWsUrl, submitAction, type AdminActionOptions } from './api'
import type { AdminState, Battle, GameEvent, InventoryItem, Player, PlayerGameState, SpectateState, VisibleOpponent, Zone } from './types'

const PLAYER_STORAGE_KEY = 'arena_player_session'
const ADMIN_STORAGE_KEY = 'arena_admin_token'
const SPECTATE_STORAGE_KEY = 'arena_spectate_token'

type StoredSession = { player: Player; sessionToken: string }

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link to="/" className="brand">THE ARENA</Link>
      <nav><Link to="/">Arena</Link></nav>
      </header>
      <main>{children}</main>
    </div>
  )
}

function ExistingSessionRedirect() {
  const navigate = useNavigate()

  useEffect(() => {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw) return

    try {
      const session = JSON.parse(raw) as StoredSession
      void getPlayerState(session.player.id, session.sessionToken)
        .then((state: PlayerGameState) => {
          navigate(state.status === 'LOBBY' ? '/lobby' : '/game', { replace: true })
        })
        .catch(() => {
          localStorage.removeItem(PLAYER_STORAGE_KEY)
        })
    } catch {
      localStorage.removeItem(PLAYER_STORAGE_KEY)
    }
  }, [navigate])

  return null
}

function LandingPage() {
  return (
    <>
      <ExistingSessionRedirect />
      <section className="hero panel">
      <p className="eyebrow">COLLEGE EVENT // ONE SHARED ARENA</p>
      <h1>THE<br />ARENA</h1>
      <p className="hero-copy">A text-first survival game. Enter your name, make your choices, and survive the rounds.</p>
      <div className="button-row">
        <Link className="button button-primary" to="/join">ENTER THE ARENA</Link>
      </div>
      </section>
    </>
  )
}

function JoinPage() {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError('')
    setLoading(true)
    try {
      const specialPaths: Record<string, string> = {
        trinav: '/trinav',
        chewie: '/chewie',
        arpit: '/arpit',
      }
      const specialPath = specialPaths[name.trim().toLowerCase()]
      if (specialPath) {
        navigate(specialPath)
        return
      }
      const result = await joinGame(name)
      localStorage.setItem(PLAYER_STORAGE_KEY, JSON.stringify(result))
      navigate('/lobby')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to join')
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <ExistingSessionRedirect />
      <section className="narrow panel">
      <p className="eyebrow">PLAYER REGISTRATION</p>
      <h2>ENTER YOUR NAME</h2>
      <p className="muted">One name per participant. Registration closes when the admin starts the game.</p>
      <form onSubmit={handleSubmit} className="stack">
        <input autoFocus maxLength={32} value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" />
        <button disabled={loading || !name.trim()} className="button button-primary" type="submit">
          {loading ? 'JOINING…' : 'JOIN GAME'}
        </button>
      </form>
      {error && <div className="error">{error}</div>}
      </section>
    </>
  )
}

function EventFeed({ events, compact = false }: { events: GameEvent[]; compact?: boolean }) {
  return (
    <div className={`event-feed ${compact ? 'compact' : ''}`}>
      <div className="section-heading">LIVE FEED</div>
      {events.length === 0 ? <p className="muted">No events yet.</p> : events.slice().reverse().map((event) => (
        <div key={event.id} className="event-item">
          <span className="event-time">{new Date(event.timestamp).toLocaleTimeString()}</span>
          <span>{event.message}</span>
        </div>
      ))}
    </div>
  )
}

function usePlayerSocket() {
  const navigate = useNavigate()
  const [state, setState] = useState<PlayerGameState | null>(null)
  const [error, setError] = useState('')
  const [connectionStatus, setConnectionStatus] = useState<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>('CONNECTING')

  useEffect(() => {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw) {
      navigate('/', { replace: true })
      return
    }
    let session: StoredSession
    try {
      session = JSON.parse(raw) as StoredSession
    } catch {
      localStorage.removeItem(PLAYER_STORAGE_KEY)
      navigate('/', { replace: true })
      return
    }

    let closedByEffect = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | undefined

    const connect = () => {
      if (closedByEffect) return
      setConnectionStatus(reconnectTimer ? 'RECONNECTING' : 'CONNECTING')
      ws = new WebSocket(playerWsUrl(session.player.id, session.sessionToken))
      ws.onopen = () => { setError(''); setConnectionStatus('CONNECTED') }
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as { type: string; data: PlayerGameState | GameEvent }
        if (payload.type === 'RESET') {
          localStorage.removeItem(PLAYER_STORAGE_KEY)
          navigate('/join', { replace: true })
          return
        }
        if (payload.type === 'GAME_STATE') setState(payload.data as PlayerGameState)
        if (payload.type === 'PUBLIC_EVENT') {
          setState((current) => current ? { ...current, events: [...current.events, payload.data as GameEvent].slice(-30) } : current)
        }
      }
      ws.onerror = () => { setError('Connection interrupted. Reconnecting…'); setConnectionStatus('RECONNECTING') }
      ws.onclose = () => {
        if (!closedByEffect) {
          setConnectionStatus('RECONNECTING')
          reconnectTimer = window.setTimeout(connect, 1200)
        }
      }
    }

    connect()
    return () => {
      closedByEffect = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [navigate])

  return { state, error, connectionStatus }
}

function PlayerLobby() {
  const navigate = useNavigate()
  const { state, error, connectionStatus } = usePlayerSocket()

  useEffect(() => {
    if (state?.status === 'ACTIVE' || state?.status === 'PAUSED' || state?.status === 'GAME_OVER') {
      navigate('/game', { replace: true })
    }
  }, [navigate, state?.status])

  if (!state) return <LoadingState text={error || 'Connecting to the arena…'} />

  return (
    <section className="panel stack">
      <div>
        <div className="connection-line"><span className={`connection-dot ${connectionStatus.toLowerCase()}`} /> {connectionStatus === 'CONNECTED' ? 'LIVE CONNECTION' : 'RECONNECTING'}</div>
        <p className="eyebrow">LOBBY</p>
        <h2>WELCOME, {state.player.name.toUpperCase()}</h2>
        <p className="muted">Your player ID is <strong>{state.player.id}</strong>. Keep this page open.</p>
      </div>
      <div className="stat-grid">
        <Stat label="REGISTERED" value={`${state.playerCount}/${state.maxPlayers}`} />
        <Stat label="ALIVE" value={String(state.aliveCount)} />
        <Stat label="STATUS" value="WAITING" />
      </div>
      <div className="waiting-panel">
        <div className="loading-dot" />
        <div><strong>Waiting for the admin to start the game.</strong><p className="muted">Once the game begins, your zone, inventory, and first timed round will appear here.</p></div>
      </div>
      <EventFeed events={state.events} />
    </section>
  )
}

function Countdown({ deadline, serverNow, durationSeconds, paused = false }: { deadline: string | null; serverNow: string; durationSeconds: number; paused?: boolean }) {
  const offsetMs = useMemo(() => new Date(serverNow).getTime() - Date.now(), [serverNow])
  const [remaining, setRemaining] = useState(0)

  useEffect(() => {
    const update = () => {
      if (paused || !deadline) {
        setRemaining(0)
        return
      }
      setRemaining(Math.max(0, new Date(deadline).getTime() - (Date.now() + offsetMs)))
    }
    update()
    const id = window.setInterval(update, 100)
    return () => window.clearInterval(id)
  }, [deadline, offsetMs, paused])

  const seconds = Math.ceil(remaining / 1000)
  const fraction = paused ? 0 : Math.min(1, remaining / Math.max(1000, durationSeconds * 1000))

  return (
    <div className={`countdown ${seconds <= 5 && seconds > 0 ? 'urgent' : ''} ${paused ? 'is-paused' : ''}`}>
      <div className="countdown-label">{paused ? 'PAUSED' : 'TIME REMAINING'}</div>
      <div className="countdown-number">{paused ? '—' : seconds}</div>
      <div className="countdown-track"><span style={{ width: `${Math.max(0, fraction * 100)}%` }} /></div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span>{label}</span><strong>{value}</strong></div>
}

function PlayerStats({ player }: { player: Player }) {
  return (
    <div className="stat-grid three">
      <div className="stat stat-health"><span>HEALTH</span><strong>{player.health}/{player.maxHealth}</strong><div className="health-bar"><span style={{ width: `${Math.max(0, Math.min(100, player.health / player.maxHealth * 100))}%` }} /></div></div>
      <Stat label="ATTACK" value={String(player.attack)} />
      <Stat label="SPEED" value={String(player.speed)} />
    </div>
  )
}

function Inventory({ items, disabled, onUse }: { items: InventoryItem[]; disabled: boolean; onUse: (itemId: string) => void }) {
  return (
    <div className="subpanel">
      <div className="section-heading">INVENTORY <span className="heading-count">{items.length}</span></div>
      {items.length === 0 ? <p className="muted">Empty. Grab supplies at the Cornucopia.</p> : (
        <div className="inventory-grid">
          {items.map((item) => (
            <div key={item.id} className="item-card">
              <div><strong>{item.name}</strong><span>{item.type.replace('_', ' ')}</span></div>
              <p>{item.description}</p>
              <button className="button button-secondary button-small" disabled={disabled} onClick={() => onUse(item.id)}>USE</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function OpponentList({ opponents, disabled, onAttack }: { opponents: VisibleOpponent[]; disabled: boolean; onAttack: (playerId: string) => void }) {
  const [selectedOpponentId, setSelectedOpponentId] = useState('')
  const selectedOpponent = opponents.find((opponent) => opponent.id === selectedOpponentId)

  useEffect(() => {
    if (!opponents.some((opponent) => opponent.id === selectedOpponentId)) {
      setSelectedOpponentId(opponents[0]?.id ?? '')
    }
  }, [opponents, selectedOpponentId])

  return (
    <div className="subpanel opponent-panel">
      <div className="section-heading">PLAYERS IN YOUR ZONE <span className="heading-count">{opponents.length}</span></div>
      {opponents.length === 0 ? <p className="muted">No other players are currently visible in this zone.</p> : (
        <div className="opponent-picker">
          <select value={selectedOpponentId} onChange={(e) => setSelectedOpponentId(e.target.value)} disabled={disabled}>
            {opponents.map((opponent) => <option key={opponent.id} value={opponent.id}>{opponent.name} · {opponent.health}/{opponent.maxHealth} HP</option>)}
          </select>
          <button className="button button-danger" disabled={disabled || !selectedOpponent} onClick={() => selectedOpponent && onAttack(selectedOpponent.id)}>ATTACK</button>
        </div>
      )}
    </div>
  )
}

function BattlePanel({ battle, opponent, serverNow, durationSeconds, disabled, onAction }: {
  battle: Battle
  opponent?: VisibleOpponent
  serverNow: string
  durationSeconds: number
  disabled: boolean
  onAction: (action: string) => void
}) {
  const options = [
    { id: 'BATTLE_ATTACK', label: 'ATTACK', detail: 'Base damage from ATK plus a small speed edge; 12% crit chance.' },
    { id: 'BATTLE_DEFEND', label: 'DEFEND', detail: 'Reduces incoming damage by 55%; armor can reduce it by another 8.' },
    { id: 'BATTLE_RUN', label: 'RUN', detail: 'Escape chance starts at 50% and shifts by 4% per SPEED point.' },
  ]
  return (
    <div className="battle-panel">
      <div className="battle-heading">
        <div>
          <p className="eyebrow">ENGAGED // TURN {battle.turn}</p>
          <h3>BATTLE WITH {opponent?.name?.toUpperCase() ?? battle.playerBId}</h3>
          <p className="muted">You cannot move, rest, use items, scout, or take supplies until the battle ends.</p>
        </div>
        <Countdown deadline={battle.deadline} serverNow={serverNow} durationSeconds={durationSeconds} />
      </div>
      <div className="battle-status-row">
        <span>{battle.yourAction ? `YOU: ${battle.yourAction.replace('BATTLE_', '')}` : 'YOU: CHOOSE A MOVE'}</span>
        <span>{battle.opponentActionSubmitted ? 'OPPONENT: MOVE LOCKED' : 'OPPONENT: THINKING'}</span>
      </div>
      <div className="battle-actions">
        {options.map((option) => (
          <button key={option.id} className={`button battle-action ${battle.yourAction === option.id ? 'selected' : ''}`} disabled={disabled} onClick={() => onAction(option.id)}>
            <strong>{option.label}</strong><small>{option.detail}</small>
          </button>
        ))}
      </div>
    </div>
  )
}

function PlayerGame() {
  const { state, error, connectionStatus } = usePlayerSocket()
  const [actionError, setActionError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [selectedAction, setSelectedAction] = useState<string | null>(null)

  if (!state) return <LoadingState text={error || 'Entering the arena…'} />

  const isPlayable = state.status === 'ACTIVE' && state.player.alive && !state.player.actionTaken && !state.battle
  const isFinished = state.status === 'GAME_OVER'
  const isPaused = state.status === 'PAUSED'

  async function doAction(action: string, options?: { targetZoneId?: string; targetPlayerId?: string; itemId?: string }) {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw || submitting) return
    const session = JSON.parse(raw) as StoredSession
    setActionError('')
    setSubmitting(true)
    setSelectedAction(action)
    try {
      await submitAction(session.sessionToken, session.player.id, action, options?.targetZoneId, options?.targetPlayerId, options?.itemId)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Action failed')
      setSelectedAction(null)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="game-layout">
      <div className="panel stack">
        <div className="game-header">
          <div><p className="eyebrow">ROUND {state.round} // {state.phase}</p><h2>{state.currentZone.name}</h2><p className="muted">{state.currentZone.description}</p></div>
          <span className="status-pill">{state.status}</span>
        </div>

        <PlayerStats player={state.player} />

        <div className="round-strip">
          <div><span className="eyebrow">YOU</span><strong>{state.player.name}</strong><small>{state.player.id}</small></div>
          <Countdown deadline={state.roundDeadline} serverNow={state.serverNow} durationSeconds={state.roundDurationSeconds} paused={isPaused} />
          <div className="round-strip-right"><span>ALIVE</span><strong>{state.aliveCount}/{state.playerCount}</strong></div>
        </div>
        {connectionStatus !== 'CONNECTED' && <div className="connection-warning"><span className="connection-dot reconnecting" /><strong>Connection unstable.</strong><span>Your arena state will resync automatically.</span></div>}

        {state.zoneHazard && state.player.alive && <div className="warning-panel"><strong>HAZARD ACTIVE</strong><span>This zone will deal {state.hazardDamage} damage at the end of the round.</span></div>}

        {!state.player.alive ? (
          <div className="eliminated-panel"><p className="eyebrow">ELIMINATED</p><h3>The arena has claimed you.</h3><p>{state.player.lastResult}</p>{state.winnerId && <p>Winner: <strong>{state.winnerId}</strong></p>}</div>
        ) : isFinished ? (
          <div className="event-focus"><p className="eyebrow">GAME OVER</p><h3>{state.winnerId === state.player.id ? 'YOU SURVIVED.' : 'THE ARENA IS CLOSED.'}</h3><p>{state.player.lastResult}</p></div>
        ) : isPaused ? (
          <div className="event-focus"><p className="eyebrow">GAME PAUSED</p><h3>Stand by.</h3><p>The admin has paused the arena. Your timer is frozen.</p></div>
        ) : (
          <>
            <div className="event-focus">
              <p className="eyebrow">YOUR CURRENT RESULT</p>
              <h3>{selectedAction ? `ACTION: ${selectedAction}` : 'Choose carefully.'}</h3>
              <p>{state.player.lastResult}</p>
              <p className="minor-note">You get one action per round. Missing the timer means elimination.</p>
            </div>

            <Inventory items={state.player.inventory} disabled={!isPlayable || submitting} onUse={(itemId) => void doAction('USE_ITEM', { itemId })} />

            {state.battle ? (
              <BattlePanel
                battle={state.battle}
                opponent={state.visibleOpponents.find((opponent) => opponent.id === state.battle?.playerAId || opponent.id === state.battle?.playerBId)}
                serverNow={state.serverNow}
                durationSeconds={state.battleTurnDurationSeconds}
                disabled={submitting || Boolean(state.battle.yourAction) || !state.availableActions.some((action) => action.startsWith('BATTLE_'))}
                onAction={(action) => void doAction(action)}
              />
            ) : (
              <>
                <OpponentList opponents={state.visibleOpponents} disabled={!isPlayable || submitting || !state.availableActions.includes('ATTACK')} onAttack={(playerId) => void doAction('ATTACK', { targetPlayerId: playerId })} />

                <div className="action-panel">
                  <div>
                    <div className="section-heading">WHAT DO YOU DO?</div>
                    <p className="muted">Choose one action. Supplies can only be grabbed at the Cornucopia.</p>
                  </div>
                  <div className="action-grid">
                    {['REST', 'SCOUT', 'WAIT'].map((action) => (
                      <button key={action} className={`button action-button ${selectedAction === action ? 'selected' : ''}`} disabled={!isPlayable || submitting || !state.availableActions.includes(action)} onClick={() => void doAction(action)}>{action}</button>
                    ))}
                    {state.availableActions.includes('GRAB_ITEM') && <button className={`button action-button grab-action ${selectedAction === 'GRAB_ITEM' ? 'selected' : ''}`} disabled={!isPlayable || submitting} onClick={() => void doAction('GRAB_ITEM')}>GRAB ITEM</button>}
                  </div>
                  <div className="move-panel">
                    <div className="section-heading">MOVE TO AN ADJACENT ZONE</div>
                    <div className="move-grid">
                      {state.adjacentZones.map((zone) => (
                        <button key={zone.id} className="button button-secondary move-button" disabled={!isPlayable || submitting || !state.availableActions.includes('MOVE')} onClick={() => void doAction('MOVE', { targetZoneId: zone.id })}>
                          <strong>{zone.name}</strong><small>{zone.description}</small>
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </>
            )}
          </>
        )}

        {actionError && <div className="error">{actionError}</div>}
      </div>

      <aside className="stack">
        <div className="panel">
          <div className="section-heading">CURRENT ZONE</div>
          <p className="zone-big">{state.currentZone.name}</p>
          <p className="muted">{state.visibleOpponents.length + 1} active player{state.visibleOpponents.length === 0 ? '' : 's'} in this zone.</p>
          <div className="zone-mini-stats">
            <Stat label="LOOT CACHE" value={String(state.zoneLootCount)} />
            <Stat label="YOUR KILLS" value={String(state.player.kills)} />
          </div>
        </div>
        <div className="panel"><EventFeed events={state.events} compact /></div>
      </aside>
    </section>
  )
}

function AdminPage() {
  const [authenticated, setAuthenticated] = useState(Boolean(localStorage.getItem(ADMIN_STORAGE_KEY)))
  const [tokenInput, setTokenInput] = useState('')
  const [state, setState] = useState<AdminState | null>(null)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [eventZone, setEventZone] = useState('')
  const [selectedPlayerId, setSelectedPlayerId] = useState('')
  const [operatorValue, setOperatorValue] = useState('50')
  const [operatorAttack, setOperatorAttack] = useState('10')
  const [operatorSpeed, setOperatorSpeed] = useState('10')
  const [operatorItem, setOperatorItem] = useState('MEDKIT')
  const [operatorZone, setOperatorZone] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [adminConnection, setAdminConnection] = useState<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>('CONNECTING')

  async function login(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await adminLogin(tokenInput)
      localStorage.setItem(ADMIN_STORAGE_KEY, tokenInput)
      setAuthenticated(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    }
  }

  useEffect(() => {
    if (!authenticated) return
    const token = localStorage.getItem(ADMIN_STORAGE_KEY)
    if (!token) return

    let closed = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | undefined

    const load = async () => {
      try {
        const data = await getAdminState(token) as AdminState
        if (!closed) setState(data)
      } catch {
        localStorage.removeItem(ADMIN_STORAGE_KEY)
        setAuthenticated(false)
      }
    }

    const connect = () => {
      if (closed) return
      setAdminConnection(reconnectTimer ? 'RECONNECTING' : 'CONNECTING')
      ws = new WebSocket(adminWsUrl(token))
      ws.onopen = () => { setError(''); setAdminConnection('CONNECTED') }
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as { type: string; data: AdminState }
        if (payload.type === 'ADMIN_STATE') setState(payload.data)
      }
      ws.onerror = () => { setError('Admin live connection interrupted. Reconnecting…'); setAdminConnection('RECONNECTING') }
      ws.onclose = () => { if (!closed) { setAdminConnection('RECONNECTING'); reconnectTimer = window.setTimeout(connect, 1200) } }
    }

    void load()
    connect()
    return () => {
      closed = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [authenticated])

  async function doAction(action: string, options: AdminActionOptions = {}) {
    const token = localStorage.getItem(ADMIN_STORAGE_KEY)
    if (!token) return
    setError('')
    try {
      const next = await adminAction(token, action, options) as AdminState
      setState(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed')
    }
  }

  const selectedPlayer = state?.players.find((player) => player.id === selectedPlayerId) ?? null

  useEffect(() => {
    if (selectedPlayer) setOperatorZone(selectedPlayer.zoneId)
  }, [selectedPlayerId, selectedPlayer?.zoneId])

  async function runPlayerAction(action: string) {
    if (!selectedPlayer) return
    if (action === 'ELIMINATE_PLAYER' && !window.confirm(`Eliminate ${selectedPlayer.name}?`)) return
    if (action === 'MOVE_PLAYER') {
      const destination = operatorZone || selectedPlayer.zoneId
      await doAction(action, { targetPlayerId: selectedPlayer.id, targetZoneId: destination })
      return
    }
    if (action === 'GIVE_ITEM') {
      await doAction(action, { targetPlayerId: selectedPlayer.id, itemType: operatorItem })
      return
    }
    if (action === 'SET_HEALTH') {
      await doAction(action, { targetPlayerId: selectedPlayer.id, value: Number(operatorValue) })
      return
    }
    if (action === 'SET_STATS') {
      await doAction(action, { targetPlayerId: selectedPlayer.id, value: Number(operatorValue), attack: Number(operatorAttack), speed: Number(operatorSpeed) })
      return
    }
    await doAction(action, { targetPlayerId: selectedPlayer.id, value: Number(operatorValue) })
  }

  function logout() {
    localStorage.removeItem(ADMIN_STORAGE_KEY)
    setAuthenticated(false)
    setState(null)
  }

  const filteredPlayers = useMemo(() => {
    if (!state) return []
    const term = search.trim().toLowerCase()
    return state.players.filter((player) => !term || player.name.toLowerCase().includes(term) || player.id.toLowerCase().includes(term) || player.zoneName.toLowerCase().includes(term))
  }, [search, state])

  if (!authenticated) {
    return (
      <section className="narrow panel">
        <p className="eyebrow">ARENA CONTROL</p>
        <h2>ADMIN LOGIN</h2>
        <p className="muted">Use the admin token configured on the FastAPI server.</p>
        <form onSubmit={login} className="stack">
          <input type="password" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="Admin token" />
          <button disabled={!tokenInput} className="button button-primary" type="submit">UNLOCK DASHBOARD</button>
        </form>
        {error && <div className="error">{error}</div>}
      </section>
    )
  }

  if (!state) return <LoadingState text="Loading arena control…" />

  return (
    <section className="admin-layout">
      <div className="panel stack">
        <div className="game-header">
          <div><div className="connection-line"><span className={`connection-dot ${adminConnection.toLowerCase()}`} /> {adminConnection === 'CONNECTED' ? 'LIVE ADMIN CONNECTION' : 'ADMIN RECONNECTING'}</div><p className="eyebrow">ARENA CONTROL</p><h2>MAIN GAME</h2></div>
          <button className="button button-secondary button-small" onClick={logout}>LOG OUT</button>
        </div>
        <div className="admin-metrics">
          <Stat label="STATUS" value={state.status} />
          <Stat label="PHASE" value={state.phase} />
          <Stat label="ROUND" value={String(state.round)} />
          <Stat label="ALIVE" value={String(state.aliveCount)} />
          <Stat label="ONLINE" value={String(state.onlineCount)} />
          <Stat label="REGISTERED" value={`${state.playerCount}/${state.maxPlayers}`} />
          <Stat label="ACTED" value={`${state.actedCount}/${state.aliveCount}`} />
          <Stat label="WAITING" value={String(state.waitingCount)} />
        </div>
        {(state.status === 'ACTIVE' || state.status === 'PAUSED') && <div className="round-progress">
          <div className="round-progress-top"><span>ACTION PROGRESS</span><strong>{state.actedCount}/{state.aliveCount} alive players acted</strong></div>
          <div className="round-progress-track"><span style={{ width: `${state.aliveCount ? (state.actedCount / state.aliveCount) * 100 : 0}%` }} /></div>
        </div>}
        <div className="button-row wrap">
          {state.status === 'LOBBY' && <button className="button button-primary" onClick={() => void doAction('START_GAME')}>START GAME</button>}
          {state.status === 'ACTIVE' && <button className="button button-secondary" onClick={() => void doAction('PAUSE_GAME')}>PAUSE</button>}
          {state.status === 'ACTIVE' && <button className="button button-secondary" onClick={() => void doAction('END_ROUND')}>END ROUND</button>}
          {state.status === 'PAUSED' && <button className="button button-primary" onClick={() => void doAction('RESUME_GAME')}>RESUME</button>}
          <button className="button button-danger" onClick={() => { if (window.confirm('Reset the entire arena? All players will be removed.')) void doAction('RESET_GAME') }}>RESET</button>
        </div>
        {(state.status === 'ACTIVE' || state.status === 'PAUSED') && <Countdown deadline={state.roundDeadline} serverNow={state.serverNow} durationSeconds={state.roundDurationSeconds} paused={state.status === 'PAUSED'} />}
        {error && <div className="error">{error}</div>}
      </div>

      {(state.status === 'ACTIVE' || state.status === 'PAUSED') && (
        <div className="panel admin-events-controls">
          <div>
            <div className="section-heading">EVENT CONTROLS</div>
            <p className="muted">Manual controls are useful if the organizers want to push the arena along during the live event.</p>
          </div>
          <div className="event-control-row">
            <select value={eventZone} onChange={(e) => setEventZone(e.target.value)}>
              <option value="">Random zone</option>
              {state.zones.filter((zone) => zone.id !== 'cornucopia').map((zone) => <option key={zone.id} value={zone.id}>{zone.name}</option>)}
            </select>
            <button className="button button-secondary" onClick={() => void doAction('SPAWN_SUPPLY_DROP', { targetZoneId: eventZone || undefined })}>DROP SUPPLY</button>
            <button className="button button-danger" onClick={() => void doAction('TRIGGER_HAZARD', { targetZoneId: eventZone || undefined })}>TRIGGER HAZARD</button>
          </div>
          <div className="event-control-row">
            <input value={announcement} onChange={(e) => setAnnouncement(e.target.value)} placeholder="Announcement visible to every player" maxLength={240} />
            <button className="button button-primary" disabled={!announcement.trim()} onClick={() => { void doAction('BROADCAST_ANNOUNCEMENT', { message: announcement.trim() }); setAnnouncement('') }}>BROADCAST</button>
            <button className="button button-secondary" onClick={() => void doAction('CLEAR_HAZARDS')}>CLEAR HAZARDS</button>
          </div>
        </div>
      )}

      <div className="panel operator-panel">
        <div>
          <div className="section-heading">PLAYER OPERATOR TOOLS</div>
          <p className="muted">Select a participant to correct an event mistake or administer a manual arena intervention.</p>
        </div>
        <div className="operator-grid">
          <div className="operator-selected">
            <label>SELECTED PLAYER</label>
            <select value={selectedPlayerId} onChange={(e) => setSelectedPlayerId(e.target.value)}>
              <option value="">Choose a player</option>
              {state.players.map((player) => <option key={player.id} value={player.id}>{player.id} — {player.name}</option>)}
            </select>
            {selectedPlayer && <div className="selected-player-summary"><strong>{selectedPlayer.name}</strong><span>{selectedPlayer.id} · {selectedPlayer.zoneName} · {selectedPlayer.health}/{selectedPlayer.maxHealth} HP</span></div>}
          </div>
          <div className="operator-values">
            <label>HEALTH</label><input type="number" min="0" max="500" value={operatorValue} onChange={(e) => setOperatorValue(e.target.value)} />
            <label>ATTACK</label><input type="number" min="1" max="100" value={operatorAttack} onChange={(e) => setOperatorAttack(e.target.value)} />
            <label>SPEED</label><input type="number" min="1" max="100" value={operatorSpeed} onChange={(e) => setOperatorSpeed(e.target.value)} />
            <label>ITEM</label><select value={operatorItem} onChange={(e) => setOperatorItem(e.target.value)}>{['MEDKIT','FOOD','WEAPON','ARMOR','SPEED_BOOST'].map((item) => <option key={item}>{item}</option>)}</select>
            <label>DESTINATION</label><select value={operatorZone || selectedPlayer?.zoneId || ''} onChange={(e) => setOperatorZone(e.target.value)}>{state.zones.map((zone) => <option key={zone.id} value={zone.id}>{zone.name}</option>)}</select>
          </div>
          <div className="button-row wrap operator-actions">
            <button className="button button-danger" disabled={!selectedPlayer} onClick={() => void runPlayerAction('ELIMINATE_PLAYER')}>ELIMINATE</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('RESTORE_PLAYER')}>RESTORE</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('SET_HEALTH')}>SET HP</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('SET_STATS')}>SET STATS</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('MOVE_PLAYER')}>MOVE PLAYER</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('GIVE_ITEM')}>GIVE ITEM</button>
          </div>
        </div>
      </div>

      <div className="zone-dashboard panel">
        <div className="section-heading">ARENA ZONES</div>
        <div className="zone-grid">
          {state.zones.map((zone) => <ZoneAdminCard key={zone.id} zone={zone} />)}
        </div>
      </div>

      <div className="panel">
        <div className="table-toolbar"><div className="section-heading">PLAYERS</div><input className="table-search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search player, ID or zone" /></div>
        <div className="player-table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Name</th><th>Zone</th><th>HP</th><th>ATK</th><th>SPD</th><th>Items</th><th>Kills</th><th>Action</th><th>Status</th><th>Connection</th></tr></thead>
            <tbody>{filteredPlayers.map((player) => (
              <tr key={player.id} className={selectedPlayerId === player.id ? 'selected-row' : ''} onClick={() => setSelectedPlayerId(player.id)}>
                <td>{player.id}</td><td>{player.name}</td><td>{player.zoneName}</td><td>{player.health}</td><td>{player.attack}</td><td>{player.speed}</td>
                <td>{player.inventory.length}</td><td>{player.kills}</td>
                <td>{player.currentAction ?? (player.actionTaken ? 'DONE' : 'WAITING')}</td>
                <td><span className={player.alive ? 'tag alive' : 'tag dead'}>{player.alive ? player.statusEffect : 'DEAD'}</span></td>
                <td>{player.connected ? 'ONLINE' : 'OFFLINE'}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>

      <div className="panel"><EventFeed events={state.events} compact /></div>
    </section>
  )
}

function SpectatePage() {
  const [authenticated, setAuthenticated] = useState(Boolean(localStorage.getItem(SPECTATE_STORAGE_KEY)))
  const [tokenInput, setTokenInput] = useState('')
  const [state, setState] = useState<SpectateState | null>(null)
  const [error, setError] = useState('')
  const [connectionStatus, setConnectionStatus] = useState<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>('CONNECTING')

  async function login(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await adminLogin(tokenInput)
      localStorage.setItem(SPECTATE_STORAGE_KEY, tokenInput)
      setAuthenticated(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    }
  }

  useEffect(() => {
    if (!authenticated) return
    const token = localStorage.getItem(SPECTATE_STORAGE_KEY)
    if (!token) return

    let closed = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | undefined

    const load = async () => {
      try {
        const data = await getSpectateState(token) as SpectateState
        if (!closed) setState(data)
      } catch {
        localStorage.removeItem(SPECTATE_STORAGE_KEY)
        setAuthenticated(false)
      }
    }

    const connect = () => {
      if (closed) return
      setConnectionStatus(reconnectTimer ? 'RECONNECTING' : 'CONNECTING')
      ws = new WebSocket(spectateWsUrl(token))
      ws.onopen = () => { setError(''); setConnectionStatus('CONNECTED') }
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as { type: string; data: SpectateState }
        if (payload.type === 'SPECTATE_STATE') setState(payload.data)
      }
      ws.onerror = () => { setError('Live spectator connection interrupted. Reconnecting…'); setConnectionStatus('RECONNECTING') }
      ws.onclose = () => { if (!closed) { setConnectionStatus('RECONNECTING'); reconnectTimer = window.setTimeout(connect, 1200) } }
    }

    void load()
    connect()
    return () => {
      closed = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [authenticated])

  function logout() {
    localStorage.removeItem(SPECTATE_STORAGE_KEY)
    setAuthenticated(false)
    setState(null)
  }

  if (!authenticated) {
    return (
      <section className="narrow panel">
        <p className="eyebrow">LIVE SPECTATOR ACCESS</p>
        <h2>UNLOCK THE ARENA FEED</h2>
        <p className="muted">Enter the admin token to open the live spectator view.</p>
        <form onSubmit={login} className="stack">
          <input type="password" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="Admin token" />
          <button disabled={!tokenInput} className="button button-primary" type="submit">WATCH LIVE</button>
        </form>
        {error && <div className="error">{error}</div>}
      </section>
    )
  }

  if (!state) return <LoadingState text="Opening live spectator feed…" />

  const playerById = new Map(state.players.map((player) => [player.id, player]))
  const activeBattles = state.battles.filter((battle) => playerById.get(battle.playerAId)?.alive && playerById.get(battle.playerBId)?.alive)
  const orderedPlayers = state.players.slice().sort((a, b) => Number(b.alive) - Number(a.alive) || Number(Boolean(b.battle)) - Number(Boolean(a.battle)) || a.name.localeCompare(b.name))

  return (
    <section className="spectate-layout">
      <div className="panel spectate-hero">
        <div className="game-header">
          <div>
            <div className="connection-line"><span className={`connection-dot ${connectionStatus.toLowerCase()}`} /> {connectionStatus === 'CONNECTED' ? 'LIVE SPECTATOR FEED' : 'SPECTATOR RECONNECTING'}</div>
            <p className="eyebrow">THE ARENA // SPECTATE</p>
            <h2>WATCH THE ARENA UNFOLD</h2>
          </div>
          <button className="button button-secondary button-small" onClick={logout}>LOCK</button>
        </div>
        <div className="spectate-metrics">
          <Stat label="STATUS" value={state.status} />
          <Stat label="PHASE" value={state.phase} />
          <Stat label="ROUND" value={String(state.round)} />
          <Stat label="ALIVE" value={`${state.aliveCount}/${state.playerCount}`} />
          <Stat label="BATTLES" value={String(activeBattles.length)} />
        </div>
      </div>

      <div className="panel spectate-spotlight">
        <div className="section-heading">BATTLES IN PROGRESS <span className="heading-count">{activeBattles.length}</span></div>
        {activeBattles.length === 0 ? <div className="spectate-empty"><strong>No clashes right now.</strong><span>The feed is quiet… for the moment.</span></div> : (
          <div className="battle-spotlight-grid">
            {activeBattles.map((battle) => {
              const a = playerById.get(battle.playerAId)
              const b = playerById.get(battle.playerBId)
              return (
                <div className="battle-spotlight-card" key={battle.id}>
                  <div className="battle-spotlight-top"><span>TURN {battle.turn}</span><strong>{a?.zoneName ?? 'ARENA'}</strong></div>
                  <div className="versus-row">
                    <SpectateCombatant player={a} />
                    <span className="versus-mark">VS</span>
                    <SpectateCombatant player={b} />
                  </div>
                  <div className="battle-spotlight-status"><span>{battle.actions?.[battle.playerAId] ? battle.actions[battle.playerAId].replace('BATTLE_', '') : 'THINKING'}</span><span>{battle.actions?.[battle.playerBId] ? battle.actions[battle.playerBId].replace('BATTLE_', '') : 'THINKING'}</span></div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="section-heading">ARENA MAP</div>
        <div className="spectate-zone-grid">
          {state.zones.map((zone) => <ZoneAdminCard key={zone.id} zone={zone} />)}
        </div>
      </div>

      <div className="panel">
        <div className="section-heading">ALL PLAYERS <span className="heading-count">{state.playerCount}</span></div>
        <div className="spectate-player-grid">
          {orderedPlayers.map((player) => <SpectatePlayerCard key={player.id} player={player} />)}
        </div>
      </div>

      <div className="panel"><EventFeed events={state.events} compact /></div>
    </section>
  )
}

function SpectateCombatant({ player }: { player?: SpectateState['players'][number] }) {
  if (!player) return <div className="spectate-combatant"><strong>UNKNOWN</strong><span>—</span></div>
  return <div className="spectate-combatant"><strong>{player.name}</strong><span>{player.health}/{player.maxHealth} HP · ATK {player.attack}</span></div>
}

function SpectatePlayerCard({ player }: { player: SpectateState['players'][number] }) {
  const health = `${Math.max(0, Math.min(100, (player.health / player.maxHealth) * 100))}%`
  return (
    <div className={`spectate-player-card ${player.alive ? 'alive' : 'dead'} ${player.battle ? 'in-battle' : ''}`}>
      <div className="spectate-player-top"><strong>{player.name}</strong><span>{player.alive ? (player.battle ? 'IN BATTLE' : player.statusEffect) : 'ELIMINATED'}</span></div>
      <div className="spectate-player-zone">{player.zoneName} · {player.id}</div>
      <div className="spectate-health"><span style={{ width: health }} /></div>
      <div className="spectate-player-stats"><span>{player.health}/{player.maxHealth} HP</span><span>ATK {player.attack}</span><span>SPD {player.speed}</span><span>{player.inventory.length} ITEM{player.inventory.length === 1 ? '' : 'S'}</span></div>
      {player.battle && <div className="spectate-battle-note">Turn {player.battle.turn} · {player.battle.yourAction ? player.battle.yourAction.replace('BATTLE_', '') : 'CHOOSING'}</div>}
      <p>{player.lastResult}</p>
    </div>
  )
}

function ZoneAdminCard({ zone }: { zone: Zone }) {
  const count = zone.playerCount ?? 0
  const loot = zone.lootCount ?? 0
  return (
    <div className={`zone-card ${zone.id === 'cornucopia' ? 'cornucopia' : ''} ${zone.hazard ? 'hazard' : ''}`}>
      <div className="zone-card-top"><strong>{zone.name}</strong><span>{count}</span></div>
      <small>{zone.description}</small>
      <div className="zone-flags"><span>{loot} loot</span>{zone.hazard && <span className="hazard-flag">HAZARD</span>}</div>
    </div>
  )
}

function LoadingState({ text }: { text: string }) {
  return <section className="narrow panel"><div className="loading-dot" /><p>{text}</p></section>
}

function App() {
  const location = useLocation()
  const key = useMemo(() => location.pathname, [location.pathname])
  return (
    <Shell>
      <Routes key={key}>
        <Route path="/" element={<LandingPage />} />
        <Route path="/join" element={<JoinPage />} />
        <Route path="/lobby" element={<PlayerLobby />} />
        <Route path="/game" element={<PlayerGame />} />
        <Route path="/admin" element={<AdminPage />} />
        <Route path="/spectate" element={<SpectatePage />} />
        <Route path="/trinav" element={<section />} />
        <Route path="/chewie" element={<section />} />
        <Route path="/arpit" element={<section />} />
        <Route path="*" element={<LandingPage />} />
      </Routes>
    </Shell>
  )
}

export default App
