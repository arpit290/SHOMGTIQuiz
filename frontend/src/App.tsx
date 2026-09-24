import { FormEvent, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { adminAction, adminLogin, adminWsUrl, getAdminState, joinGame, playerWsUrl, submitAction } from './api'
import type { AdminState, GameEvent, Player, PlayerGameState, Zone } from './types'

const PLAYER_STORAGE_KEY = 'arena_player_session'
const ADMIN_STORAGE_KEY = 'arena_admin_token'

type StoredSession = { player: Player; sessionToken: string }

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link to="/" className="brand">THE ARENA</Link>
        <nav><Link to="/admin">ARENA CONTROL</Link></nav>
      </header>
      <main>{children}</main>
    </div>
  )
}

function LandingPage() {
  return (
    <section className="hero panel">
      <p className="eyebrow">COLLEGE EVENT // ONE SHARED ARENA</p>
      <h1>THE<br />ARENA</h1>
      <p className="hero-copy">A text-first survival game. Enter your name, join the lobby, and survive the rounds.</p>
      <div className="button-row">
        <Link className="button button-primary" to="/join">ENTER THE ARENA</Link>
        <Link className="button button-secondary" to="/admin">ADMIN CONTROL</Link>
      </div>
    </section>
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
  )
}

function EventFeed({ events }: { events: GameEvent[] }) {
  return (
    <div className="event-feed">
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

  useEffect(() => {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw) {
      navigate('/join', { replace: true })
      return
    }
    let session: StoredSession
    try {
      session = JSON.parse(raw) as StoredSession
    } catch {
      localStorage.removeItem(PLAYER_STORAGE_KEY)
      navigate('/join', { replace: true })
      return
    }

    let closedByEffect = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | undefined

    const connect = () => {
      if (closedByEffect) return
      ws = new WebSocket(playerWsUrl(session.player.id, session.sessionToken))
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
      ws.onerror = () => setError('Connection interrupted. Reconnecting…')
      ws.onclose = () => {
        if (!closedByEffect) reconnectTimer = window.setTimeout(connect, 1200)
      }
    }

    connect()
    return () => {
      closedByEffect = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [navigate])

  return { state, error }
}

function PlayerLobby() {
  const navigate = useNavigate()
  const { state, error } = usePlayerSocket()

  useEffect(() => {
    if (state?.status === 'ACTIVE' || state?.status === 'PAUSED' || state?.status === 'GAME_OVER') {
      navigate('/game', { replace: true })
    }
  }, [navigate, state?.status])

  if (!state) return <LoadingState text={error || 'Connecting to the arena…'} />

  return (
    <section className="panel stack">
      <div>
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
        <div><strong>Waiting for the admin to start the game.</strong><p className="muted">Once the game begins, your zone and first timed round will appear here.</p></div>
      </div>
      <EventFeed events={state.events} />
    </section>
  )
}

function Countdown({ deadline, serverNow, paused = false }: { deadline: string | null; serverNow: string; paused?: boolean }) {
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
  const fraction = deadline && !paused ? Math.min(1, remaining / Math.max(1000, new Date(deadline).getTime() - (new Date(serverNow).getTime()))) : 0

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

function PlayerGame() {
  const { state, error } = usePlayerSocket()
  const [actionError, setActionError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [selectedAction, setSelectedAction] = useState<string | null>(null)

  if (!state) return <LoadingState text={error || 'Entering the arena…'} />

  const isPlayable = state.status === 'ACTIVE' && state.player.alive && !state.player.actionTaken
  const isFinished = state.status === 'GAME_OVER'
  const isPaused = state.status === 'PAUSED'

  async function doAction(action: string, targetZoneId?: string) {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw || submitting) return
    const session = JSON.parse(raw) as StoredSession
    setActionError('')
    setSubmitting(true)
    setSelectedAction(action)
    try {
      await submitAction(session.sessionToken, session.player.id, action, targetZoneId)
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
          <Countdown deadline={state.roundDeadline} serverNow={state.serverNow} paused={isPaused} />
          <div className="round-strip-right"><span>ALIVE</span><strong>{state.aliveCount}/{state.playerCount}</strong></div>
        </div>

        {!state.player.alive ? (
          <div className="eliminated-panel"><p className="eyebrow">ELIMINATED</p><h3>You are out of the arena.</h3><p>{state.player.lastResult}</p></div>
        ) : isFinished ? (
          <div className="eliminated-panel"><p className="eyebrow">GAME OVER</p><h3>{state.winnerId === state.player.id ? 'YOU SURVIVED.' : 'THE GAME HAS ENDED.'}</h3><p>{state.player.lastResult}</p></div>
        ) : isPaused ? (
          <div className="placeholder-gameplay"><p className="eyebrow">ARENA PAUSED</p><h3>WAIT FOR THE ADMIN</h3><p>The current round is paused. Your remaining time will resume when the admin resumes the arena.</p></div>
        ) : (
          <>
            <div className="event-focus"><p className="eyebrow">CURRENT RESULT</p><p>{state.player.lastResult}</p></div>
            {state.player.actionTaken ? (
              <div className="waiting-panel"><div className="loading-dot" /><div><strong>You chose: {state.player.currentAction}</strong><p className="muted">Wait for the round to resolve. Your next choices will appear automatically.</p></div></div>
            ) : (
              <div className="action-panel">
                <div><p className="eyebrow">WHAT WILL YOU DO?</p><h3>Choose one action.</h3></div>
                <div className="action-grid">
                  {state.availableActions.map((action) => (
                    <button key={action} className={`button action-button ${selectedAction === action ? 'selected' : ''}`} disabled={!isPlayable || submitting} onClick={() => action === 'MOVE' ? undefined : void doAction(action)}>
                      {action}
                    </button>
                  ))}
                </div>
                {state.availableActions.includes('MOVE') && (
                  <div className="move-panel">
                    <div className="section-heading">MOVE TO</div>
                    <div className="move-grid">
                      {state.adjacentZones.map((zone) => (
                        <button key={zone.id} className="button button-secondary move-button" disabled={!isPlayable || submitting} onClick={() => void doAction('MOVE', zone.id)}>
                          <strong>{zone.name}</strong><small>{zone.description}</small>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </>
        )}

        {actionError && <div className="error">{actionError}</div>}
      </div>

      <aside className="stack">
        <div className="panel">
          <div className="section-heading">CURRENT ZONE</div>
          <p className="zone-big">{state.currentZone.name}</p>
          <p className="muted">Players are not shown individually in Phase 2. Population counts will be exposed as the game systems expand.</p>
        </div>
        <div className="panel"><EventFeed events={state.events} /></div>
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
      ws = new WebSocket(adminWsUrl(token))
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as { type: string; data: AdminState }
        if (payload.type === 'ADMIN_STATE') setState(payload.data)
      }
      ws.onerror = () => setError('Admin live connection interrupted. Reconnecting…')
      ws.onclose = () => { if (!closed) reconnectTimer = window.setTimeout(connect, 1200) }
    }

    void load()
    connect()
    return () => {
      closed = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [authenticated])

  async function doAction(action: string) {
    const token = localStorage.getItem(ADMIN_STORAGE_KEY)
    if (!token) return
    setError('')
    try {
      const next = await adminAction(token, action) as AdminState
      setState(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed')
    }
  }

  function logout() {
    localStorage.removeItem(ADMIN_STORAGE_KEY)
    setAuthenticated(false)
    setState(null)
  }

  const filteredPlayers = useMemo(() => {
    if (!state) return []
    const term = search.trim().toLowerCase()
    return state.players.filter((player) => !term || player.name.toLowerCase().includes(term) || player.id.toLowerCase().includes(term))
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
          <div><p className="eyebrow">ARENA CONTROL</p><h2>MAIN GAME</h2></div>
          <button className="button button-secondary button-small" onClick={logout}>LOG OUT</button>
        </div>
        <div className="admin-metrics">
          <Stat label="STATUS" value={state.status} />
          <Stat label="PHASE" value={state.phase} />
          <Stat label="ROUND" value={String(state.round)} />
          <Stat label="ALIVE" value={String(state.aliveCount)} />
          <Stat label="REGISTERED" value={`${state.playerCount}/${state.maxPlayers}`} />
        </div>
        <div className="button-row wrap">
          {state.status === 'LOBBY' && <button className="button button-primary" onClick={() => void doAction('START_GAME')}>START GAME</button>}
          {state.status === 'ACTIVE' && <button className="button button-secondary" onClick={() => void doAction('PAUSE_GAME')}>PAUSE</button>}
          {state.status === 'ACTIVE' && <button className="button button-secondary" onClick={() => void doAction('END_ROUND')}>END ROUND</button>}
          {state.status === 'PAUSED' && <button className="button button-primary" onClick={() => void doAction('RESUME_GAME')}>RESUME</button>}
          <button className="button button-danger" onClick={() => { if (window.confirm('Reset the entire arena? All players will be removed.')) void doAction('RESET_GAME') }}>RESET</button>
        </div>
        {state.status === 'ACTIVE' || state.status === 'PAUSED' ? <Countdown deadline={state.roundDeadline} serverNow={state.serverNow} paused={state.status === 'PAUSED'} /> : null}
        {error && <div className="error">{error}</div>}
      </div>

      <div className="zone-dashboard panel">
        <div className="section-heading">ARENA ZONES</div>
        <div className="zone-grid">
          {state.zones.map((zone) => <ZoneAdminCard key={zone.id} zone={zone} />)}
        </div>
      </div>

      <div className="panel">
        <div className="table-toolbar"><div className="section-heading">PLAYERS</div><input className="table-search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search player or ID" /></div>
        <div className="player-table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Name</th><th>Zone</th><th>HP</th><th>ATK</th><th>SPD</th><th>Action</th><th>Status</th><th>Connection</th></tr></thead>
            <tbody>{filteredPlayers.map((player) => (
              <tr key={player.id}>
                <td>{player.id}</td><td>{player.name}</td><td>{player.zoneName}</td><td>{player.health}</td><td>{player.attack}</td><td>{player.speed}</td>
                <td>{player.currentAction ?? (player.actionTaken ? 'DONE' : 'WAITING')}</td>
                <td><span className={player.alive ? 'tag alive' : 'tag dead'}>{player.alive ? player.statusEffect : 'DEAD'}</span></td>
                <td>{player.connected ? 'ONLINE' : 'OFFLINE'}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>

      <div className="panel"><EventFeed events={state.events} /></div>
    </section>
  )
}

function ZoneAdminCard({ zone }: { zone: Zone }) {
  const count = zone.playerCount ?? 0
  return <div className={`zone-card ${zone.id === 'cornucopia' ? 'cornucopia' : ''}`}><div className="zone-card-top"><strong>{zone.name}</strong><span>{count}</span></div><small>{zone.description}</small></div>
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
        <Route path="*" element={<LandingPage />} />
      </Routes>
    </Shell>
  )
}

export default App
